"""群體過程階段的自動判斷（Kaner Diamond：發散期 → 呻吟區 → 收斂期）。

兩個部分，刻意分開：

- `judge()`：問一次 LLM「現在在哪個階段」。餵最近一段逐字稿、輪替結構、以及慢路
  近期判出的介入類型（重複／僵局密集是呻吟區的徵兆，假共識與定案語是收斂的徵兆）。
  一次讀數只是一個抽樣——慢路判斷本身在同一批點上都會翻面，階段判斷沒有理由更穩。
- `PhaseDetector`：把讀數變成狀態。階段是慢變量，所以用遲滯：連續 `CONFIRM_READINGS`
  次讀到同一個、且不同於現況的階段才切換；信心不足的讀數不算；切換後 `MIN_DWELL_SECONDS`
  內不再切。純狀態機，不碰網路，單元測試直接餵讀數。

預設只**建議**（emit `phase_suggestion`），由人在觀戰畫面確認；`apply` 模式才自動改
`Session.phase`。理由：階段乘數（interruption-design 改動 2）會直接改變主席開不開口，
在還沒有跨階段真實錄音可以驗證之前，自動套用的代價是 demo 現場被錯誤階段帶偏。

已知限制：兩場既有真實錄音全程都在發散期，所以目前只能做**反面驗證**（偵測器不得
在它們身上亂跳，見 experiments/phase_replay.py）；正面驗證要等一場真的走完三階段的會議。
"""
from __future__ import annotations

import json
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

from .slow_path import API_URL, MODEL, _api_key
from .state import MeetingState

PHASES = ("發散期", "呻吟區", "收斂期")
PHASE_TICK_SECONDS = 60.0      # 多久問一次。階段以分鐘計，5 秒一問只會製造抖動
WINDOW_SECONDS = 150.0         # 餵給判斷的逐字稿窗口
TYPES_WINDOW = 12              # 納入最近幾次慢路的 type
CONFIRM_READINGS = 2           # 連續幾次一致才切換
MIN_CONFIDENCE = 0.6           # 低於此信心的讀數不計
MIN_DWELL_SECONDS = 120.0      # 切換後至少停留多久
MIN_SPEAKERS = 2               # 窗口內至少幾個人說過話才判——群體過程需要群體
MIN_TURNS = 3                  # 窗口內至少幾次發言才判——沒有內容就不問
EFFORT = "none"

SYSTEM = """你是一位熟悉 Sam Kaner「參與式決策鑽石模型」的會議引導師。你只判斷一件事：
這場會議此刻處於哪個階段。你不評論內容、不建議該做什麼。"""

CRITERIA = """三個階段的判準（Kaner Diamond）：
- 發散期：想法還在增加。人們提出新的選項、例子、疑問；彼此補充多於反駁；
  沒有人在要求做決定。
- 呻吟區：想法夠多了但彼此不相容，開始互相拉扯。同一個點反覆出現、有人重申立場、
  出現不耐或不適、討論繞圈；還沒有人能整合。這是必經階段，不是失敗。
- 收斂期：開始整合與取捨。出現「那就……」「先做 A 再做 B」「誰負責」「什麼時候」、
  刪選項、定時程、分派工作；大家在收，不在開。

不要因為出現一句結論就判收斂——要看最近幾分鐘的主要走向。
不要因為有人反對就判呻吟區——發散期本來就允許不同意見，呻吟區是「同一個衝突走不出去」。
**衝突的對象必須是議題本身才算呻吟區。** 針對主席、AI、工具或會議流程的不滿——抱怨被打斷、
爭論主席判得對不對、討論這個系統怎麼運作、互相叫對方閉嘴——是「對會議本身的評論」，
不是群體在議題上卡住。這種段落不算呻吟區，也不算收斂；議題內容沒有推進就仍是原本的階段。
下方的「主席介入次數」是線索：主席剛頻繁介入時，張力常來自主席，不是議題。"""

TEMPLATE = """## 會議
議題：{topic}
預計 {duration} 分鐘，目前第 {elapsed:.0f} 分鐘
目前登記的階段：{current}

## 最近 {window:.0f} 秒的逐字稿
{transcript}

## 結構訊號（程式算的，不是主觀）
- 這段有 {n_turns} 次發言，{n_speakers} 人參與，平均每次 {avg_len:.0f} 字
- 說話者交替 {alternations} 次
- 主席在這段窗口介入了 {chair_interventions} 次
- 慢路最近 {n_types} 次判斷的介入類型分佈：{types}

{criteria}

用以下 JSON 格式回覆，不要有其他內容：
{{"phase": "<發散期/呻吟區/收斂期>", "confidence": <0到1>, "reason": "<一句話，引用逐字稿裡真的出現的話>"}}"""


def build_prompt(st: MeetingState, now: float, current: str,
                 recent_types: list[str]) -> str:
    since = now - WINDOW_SECONDS
    utts = [u for u in st.utterances if u.start >= since]
    transcript = "\n".join(
        f"[{int(u.start) // 60:02d}:{int(u.start) % 60:02d}] {u.speaker}：{u.text}"
        for u in utts) or "（這段沒有人說話）"
    alternations = sum(1 for a, b in zip(utts, utts[1:]) if a.speaker != b.speaker)
    chair_interventions = sum(1 for t in st.interventions if since <= t <= now)
    avg_len = (sum(len(u.text) for u in utts) / len(utts)) if utts else 0.0
    types = Counter(t for t in recent_types[-TYPES_WINDOW:] if t)
    return TEMPLATE.format(
        topic=st.topic, duration=st.duration_min, elapsed=now / 60, current=current,
        window=WINDOW_SECONDS, transcript=transcript, n_turns=len(utts),
        n_speakers=len({u.speaker for u in utts}), avg_len=avg_len,
        alternations=alternations, chair_interventions=chair_interventions,
        n_types=sum(types.values()),
        types=dict(types) or "（無）", criteria=CRITERIA)


def judgeable(st: MeetingState, now: float) -> str | None:
    """這一刻能不能判。回 None＝可以；否則回不判的理由。

    兩場真實錄音教的：一個人在頻道裡講話（另一人還沒進來）被判成「呻吟區」，
    因為模型看到停滯就往衝突解讀；沒有人說話的尾段被判成 0.99 的「發散期」。
    階段是群體的屬性，窗口裡沒有群體就沒有階段，這時不問比問了再擋更誠實。
    """
    since = now - WINDOW_SECONDS
    utts = [u for u in st.utterances if u.start >= since]
    if len(utts) < MIN_TURNS:
        return f"窗口內只有 {len(utts)} 次發言"
    if len({u.speaker for u in utts}) < MIN_SPEAKERS:
        return "窗口內只有一個人說話"
    return None


def judge(st: MeetingState, now: float, current: str,
          recent_types: list[str]) -> dict:
    """問一次 LLM。回傳 {"phase", "confidence", "reason"}；解析失敗往上拋，由呼叫端決定。
    呼叫端應先用 `judgeable()` 決定要不要問。"""
    body = {
        "model": MODEL, "reasoning_effort": EFFORT,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(st, now, current, recent_types)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    r = json.loads(payload["choices"][0]["message"]["content"])
    phase = r.get("phase")
    if phase not in PHASES:
        raise ValueError(f"階段值不合法：{phase!r}")
    conf = float(r.get("confidence", 0.0))
    return {"phase": phase, "confidence": max(0.0, min(1.0, conf)),
            "reason": str(r.get("reason", "")).strip()}


@dataclass
class PhaseDetector:
    """讀數 → 狀態，帶遲滯。`observe()` 回傳切換後的新階段，沒切換回 None。"""
    current: str = PHASES[0]
    pending: str | None = None
    streak: int = 0
    last_switch_t: float | None = None
    history: list[dict] = field(default_factory=list)

    def observe(self, reading: dict, now: float) -> str | None:
        self.history.append({"t": now, **reading})
        phase, conf = reading["phase"], reading["confidence"]
        if conf < MIN_CONFIDENCE or phase == self.current:
            self.pending, self.streak = None, 0
            return None
        if phase == self.pending:
            self.streak += 1
        else:
            self.pending, self.streak = phase, 1
        if self.streak < CONFIRM_READINGS:
            return None
        if self.last_switch_t is not None and now - self.last_switch_t < MIN_DWELL_SECONDS:
            return None
        self.current, self.last_switch_t = phase, now
        self.pending, self.streak = None, 0
        return phase
