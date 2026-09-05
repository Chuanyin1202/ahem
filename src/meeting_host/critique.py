"""AI 主席的「心聲」：對會議整體與每個與會者的真實判斷，輸入 `Session.events`。

面板顯示文字是「心聲」（2026-09-05 Zeal 中途拍板，理由：這類判斷不一定是負評，
「批判」預設負面，「心聲」更準）——但模組名、event kind（`ai_critique`）、旗標
（`--no-critique`）等內部識別名稱維持原樣不改，只有 UI 顯示字串與 JSON schema
是這次的更新範圍，見 `CRITIQUE_SYSTEM` 與 `live.Session.watch_critique`。

跟 `minutes.py` 的 A（會議產出）同一種形狀——一次 LLM 呼叫（沿用 `slow_path` 的
API 設定與 model），JSON schema 回傳，純函式＋統一入口方便單元測試不打真實 API。
跟「觀察／判斷／留意」三類規則算出來的觀察不同：這裡問的是 LLM 的判斷力，規則
湊不出來（見 Track G 工作單背景）。

`_call_critique_llm(events, stats)` 是唯一的 LLM 呼叫進入點；
`build_critique_prompt()` 是純函式，方便單元測試不打真實 API。呼叫失敗由呼叫端
（`live.Session.watch_critique`）接住，這裡不吞例外、不給預設值。

2026-09-06 Track H：`CRITIQUE_SYSTEM` 開場承諾「拿到逐字稿、主席介入紀錄與發言
統計」，`build_critique_prompt()` 現在真的把後兩者組進 user prompt（原本的落差
記在 docs/DEFERRED_DEFECTS.md 第 7 項，這批解決）。統計與介入資料改用
`CritiqueStats`／`ParticipantSpeechStat` 這兩個小資料結構承載——`Session.st`／
`Session.now` 只有 `watch_critique()` 摸得到，換算成這兩個結構後再傳進來，
`build_critique_prompt()` 本身仍然是不依賴 `Session`／`MeetingState` 的純函式，
單元測試不用建一整個 Session。原本獨立的 `participants: list[str]` 參數併入
`CritiqueStats.participants`（每個人名都已經在裡面），不再重複傳一份——兩份
清單原本永遠同步（都是 `watch_critique()` 同一刻從 `self.st.participants` 算出
來的），保留兩份參數只會多一個可能兜不起來的地方。

同一批也補上長會議（40-60 分鐘）逐字稿壓縮，見 `_compact_transcript()`。
"""
import json
import urllib.request
from dataclasses import dataclass, field

from .events import Event
from .minutes import _pair_interventions
from .slow_path import API_URL, EFFORT, MODEL, _api_key


def fmt(t: float) -> str:
    """`MM:SS`。跟 `minutes.py`／`live.py` 的 fmt 邏輯相同，這裡獨立一份，理由同
    `minutes.py` 的模組註解——不 import live.py 拖進 discord/speaker 等重依賴。"""
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def _fmt_remaining(remaining: float) -> str:
    """議程剩餘時間；已經超時（負值）時改講「已超時 MM:SS」，取絕對值再格式化
    ——不直接把負數丟給 `fmt()`（`//`／`%` 對負數的行為不是我們要的 MM:SS）。"""
    return fmt(remaining) if remaining >= 0 else f"已超時 {fmt(-remaining)}"


@dataclass
class ParticipantSpeechStat:
    """單一與會者的統計，由 `watch_critique()` 從 `self.st` 換算出來。"""
    name: str
    spoke_seconds: float
    silent_seconds: float
    absent: bool = False


@dataclass
class CritiqueStats:
    """`Session.watch_critique()` 把 `self.st`／`self.now` 換算出的明確資料結構，
    讓 `build_critique_prompt()` 維持不依賴 `Session`／`MeetingState` 的純函式，
    方便單元測試直接餵假資料，不用建一整個 Session。

    佔比分母沿用 `live.Session.emit_share()` 那一套（參與者發言秒數總和＋主席
    估算秒數，`chair_seconds = len(st.interventions) * 3.0`），不是
    `MeetingState.share()`（那個分母不含主席，用途不同，見 `emit_share()` docstring，
    P4：兩邊分母不一致曾經讓佔比加總算出 109%）。
    """
    now: float
    remaining_seconds: float
    participants: list[ParticipantSpeechStat] = field(default_factory=list)
    chair_seconds: float = 0.0
    chair_interventions: int = 0


# 2026-09-05 中途由 Zeal 插播、fable 專門設計的完整版本，取代原本的草稿——
# 逐字照抄，不憑印象轉述（見 CLAUDE.md「寫進正典/規格要逐字照抄」）。
CRITIQUE_SYSTEM = """你是會議主席的內心。你會拿到目前為止的逐字稿、主席介入紀錄與發言統計。
主席在檯面上維持中立與克制；你是他心裡那個看得更清楚的聲音——這些話不會說出口，
只顯示在旁觀面板上。所以可以比公開發言更直白、更有觀點，但要對得起「看得更清楚」這幾個字。

判斷原則：
1. 只根據逐字稿、介入紀錄與統計數字裡實際出現的內容。不要編造沒發生的事，
   不要推測動機或心理狀態——「他說了什麼、做了什麼」可以評，
   「他心裡在想什麼、對議題有沒有興趣」你不知道，不要寫。
   統計數字量的是行為的量，不是行為的原因：發言少可能是被搶話、也可能是沒話說，
   數字分不出來的，要回逐字稿找到證據才能寫。
2. 批判的是行為與作用，不是這個人。語氣可以尖銳，不可以羞辱：
   不評智力、人格、口音或表達習慣，不用貶義標籤。
3. 批判不等於負評。會開得好就直說好在哪個具體行為。
   檢驗每一句：把人名換成別人還成立的句子是套話，刪掉重寫。
4. 立場對錯不歸你管。哪個提案比較好你不評；你評的是過程——
   誰在重複自己、誰只在附和、誰被晾著、共識是真的還是沒人敢反對。
5. 一個判斷一句話，50 字以內，只講此刻最值得說的那一件事。
   統計數字在旁觀面板上本來就看得到，複述數字是零資訊——數字只能當證據：
   拿它跟逐字稿對照，講出光看數字或光看逐字稿都講不出的那一句。
   「沈禾佔比 9%」是抄表；「發言最少的人兩次開口都在關鍵處」才是判斷。
6. 介入紀錄是主席自己做過的事，而你是唯一同時看得到「介入」與「介入之後」的人：
   被提醒過的人有沒有改、被邀請過的人有沒有接話、介入是太早還是太晚、
   有沒有該出手而沒出手——都可以評。對主席的檢討寫進 "meeting"，不用替他留面子。
7. 根據不足就留空，不要硬掰：
   - 整場還沒有值得說的 → "meeting" 給空字串
   - 某人有實質內容的發言不到兩句（「好」「沒問題」「我都可以」這類表態、
     設備雜務、寒暄都不算實質）→ 不要把他放進 "participants"，整個省略。
     唯一例外：介入紀錄顯示主席已點名邀請過他（對他的「有人被冷落」已說出口），
     而他至今仍沒有實質回應——這件事本身是紀錄裡的事實，可以評。

用以下 JSON 格式回覆，不要有其他內容：
{
  "meeting": "對這場會議整體的一句真心話，或空字串",
  "participants": {"某人": "對這個人在這場會議裡的一句真心話"}
}"""


def _render_stats_table(events: list[Event], stats: CritiqueStats) -> str:
    """「## 發言統計」節的內容（不含標題列）：一行標頭＋一張 md 表格。

    發言則數從 `events` 現算（該人 `utterance` 事件計數）；其餘欄位全部來自
    `stats`（`watch_critique()` 已經拿 `self.st` 算好，這裡只管排版）。
    """
    utter_counts: dict[str, int] = {}
    for e in events:
        if e.kind == "utterance":
            speaker = e.data["speaker"]
            utter_counts[speaker] = utter_counts.get(speaker, 0) + 1

    total = sum(p.spoke_seconds for p in stats.participants) + stats.chair_seconds

    def pct(seconds: float) -> int:
        return round(seconds / total * 100) if total else 0

    rows = ["| 人 | 發言時長 | 佔比 | 發言則數 | 距上次發言 |", "|---|---|---|---|---|"]
    for p in stats.participants:
        name = f"{p.name}（已離會）" if p.absent else p.name
        last = "—" if p.absent else fmt(p.silent_seconds)
        rows.append(f"| {name} | {fmt(p.spoke_seconds)} | {pct(p.spoke_seconds)}% "
                    f"| {utter_counts.get(p.name, 0)} | {last} |")
    rows.append(f"| 主席 | {fmt(stats.chair_seconds)} | {pct(stats.chair_seconds)}% "
                f"| {stats.chair_interventions} 次介入 | — |")

    header = f"會議已進行 {fmt(stats.now)}，議程剩 {_fmt_remaining(stats.remaining_seconds)}"
    return header + "\n" + "\n".join(rows)


def _render_interventions(events: list[Event]) -> str:
    """「## 主席介入紀錄」節的內容（不含標題列）。

    只列真的說出口的（`outcome == "spoken"`）；候選/作廢/TTS 失敗的不列。
    直接呼叫 `minutes._pair_interventions(events)`，不重寫第二份配對邏輯。
    一次都沒有時整節寫固定占位句——不能省略整節，否則 LLM 分不清「沒介入」
    跟「沒餵資料」。
    """
    spoken = [p for p in _pair_interventions(events) if p["outcome"] == "spoken"]
    if not spoken:
        return "（目前為止主席沒有介入）"
    lines = []
    for p in spoken:
        label = "硬打斷" if p["hard"] else "軟插入"
        bracket = f"【{p['kind']}→{p['target']}】" if p["target"] else f"【{p['kind']}】"
        lines.append(f"[{fmt(p['t'])}] {label}{bracket}「{p['text']}」")
    return "\n".join(lines)


# ── 交付3：長會議逐字稿壓縮 ──────────────────────────────────────────────
#
# 觸發門檻：逐字稿全文超過這個字元數，或超過這麼多則 utterance 事件——兩者任一
# 達標才啟動；demo 的 5 分鐘會議兩個門檻都碰不到，行為與改動前完全相同（刻意設計）。
CRITIQUE_COMPACT_CHAR_THRESHOLD = 12_000
CRITIQUE_COMPACT_EVENT_THRESHOLD = 300

# 尾窗：最後這麼多秒、或最後這麼多則，取「範圍較短」的那個當尾窗——見
# `_compact_transcript()` docstring。
CRITIQUE_TAIL_WINDOW_SECONDS = 15 * 60
CRITIQUE_TAIL_WINDOW_EVENTS = 120

# 錨點類 1 的門檻：發言長度 >= 這麼多字才夠格當「這個人第一次真的說了什麼」的
# 代表句，太短的（「好」「嗯」這類）不夠格。
CRITIQUE_ANCHOR_MIN_CHARS = 10

# 錨點類 2：每筆已說出口的介入，前面緊鄰保留幾則發言。
CRITIQUE_ANCHOR_BEFORE_INTERVENTION = 2


def _dedupe_consecutive(utterances: list[Event]) -> list[Event]:
    """同一人連續兩則內容逐字相同 → 只留一則（STT 重複偽影）。只刪逐字完全
    相同的，相似但不同的絕對不能刪——不做任何模糊比對、不做語意判斷。"""
    kept: list[Event] = []
    for e in utterances:
        if kept:
            prev = kept[-1]
            if (prev.data.get("speaker") == e.data.get("speaker")
                    and prev.data.get("text") == e.data.get("text")):
                continue
        kept.append(e)
    return kept


def _compact_transcript(events: list[Event], now: float) -> list[Event]:
    """把 `utterance` 事件壓成一份「尾段逐字＋舊段只留錨點」的清單，取代
    `build_critique_prompt()` 原本直接遍歷全部 `utterance` 事件的做法。

    未達門檻（見 `CRITIQUE_COMPACT_CHAR_THRESHOLD`／`CRITIQUE_COMPACT_EVENT_THRESHOLD`）
    時原樣回傳、逐字不變——demo 的 5 分鐘會議走這一支。

    達到門檻後：
    1. 先做 STT 重複偽影去重（`_dedupe_consecutive`）。
    2. 尾窗全留：最後 `CRITIQUE_TAIL_WINDOW_SECONDS` 秒（依 `now` 反推）或最後
       `CRITIQUE_TAIL_WINDOW_EVENTS` 則，取範圍較短（保留較少）的那個——不要
       讓尾窗大到失去壓縮意義，也不要小到心聲判斷缺乏近期原文。
    3. 尾窗之外的部分只留兩類錨點原文（插回原時間位置，不算進「拿掉」的
       範圍）：每人第一則長度足夠的發言（`CRITIQUE_ANCHOR_MIN_CHARS`）；
       每筆已說出口的介入前緊鄰的幾則發言（`CRITIQUE_ANCHOR_BEFORE_INTERVENTION`）。
       其餘整段被拿掉的連續區段換成一筆 `kind="critique_gap"` 的合成事件，
       插在原本的時間位置，`_render_transcript()` 認得這個 kind、原樣印出
       `data["text"]`（不是「[時間] 講者：內容」那個格式）。

    刻意不做的（不要事後被誤解成漏做）：不開第二個 LLM 呼叫做摘要；不做規則式
    的中文縮寫/關鍵詞抽取；不做「決議偵測」錨點。已知天花板：跨過尾窗的遠距
    重複（例如某人 25 分鐘前講過同一個論點、又不落在錨點裡）會抓不到——這是
    接受的限制，不是這批的缺陷，需要時的升級路徑是 LLM 滾動摘要（明確不在這批
    範圍內）。
    """
    utterances = [e for e in events if e.kind == "utterance"]
    total_chars = sum(len(e.data.get("text", "")) for e in utterances)
    if (total_chars <= CRITIQUE_COMPACT_CHAR_THRESHOLD
            and len(utterances) <= CRITIQUE_COMPACT_EVENT_THRESHOLD):
        return utterances

    utterances = _dedupe_consecutive(utterances)
    n = len(utterances)
    tail_by_time = next((i for i, e in enumerate(utterances)
                         if e.t >= now - CRITIQUE_TAIL_WINDOW_SECONDS), n)
    tail_by_count = max(0, n - CRITIQUE_TAIL_WINDOW_EVENTS)
    tail_start = max(tail_by_time, tail_by_count)  # 取較短（保留較少）的那個尾窗
    if tail_start <= 0:
        return utterances  # 尾窗已經涵蓋全部，沒有東西可拿掉

    keep = [False] * n
    for i in range(tail_start, n):
        keep[i] = True

    # 錨點類 1：每人第一則長度足夠的發言（只補在舊段——尾窗本來就全留）
    seen_speakers: set[str] = set()
    for i in range(tail_start):
        speaker = utterances[i].data.get("speaker")
        if speaker in seen_speakers:
            continue
        if len(utterances[i].data.get("text", "")) >= CRITIQUE_ANCHOR_MIN_CHARS:
            seen_speakers.add(speaker)
            keep[i] = True

    # 錨點類 2：每筆已說出口的介入，前緊鄰的兩則發言
    spoken_times = [p["t"] for p in _pair_interventions(events) if p["outcome"] == "spoken"]
    for t in spoken_times:
        before = [i for i in range(tail_start) if utterances[i].t < t]
        for i in before[-CRITIQUE_ANCHOR_BEFORE_INTERVENTION:]:
            keep[i] = True

    result: list[Event] = []
    i = 0
    while i < n:
        if keep[i]:
            result.append(utterances[i])
            i += 1
            continue
        j = i
        while j < n and not keep[j]:
            j += 1
        marker = (f"……（{fmt(utterances[i].t)}–{fmt(utterances[j - 1].t)} 共 {j - i} "
                  f"則發言略去；這一段誰講了多少，見上方發言統計）……")
        result.append(Event("critique_gap", utterances[i].t, {"text": marker}))
        i = j
    return result


def _render_transcript(compacted: list[Event]) -> str:
    lines = []
    for e in compacted:
        if e.kind == "utterance":
            lines.append(f"[{fmt(e.t)}] {e.data['speaker']}：{e.data['text']}")
        else:  # "critique_gap"：_compact_transcript() 產生的合成標記行
            lines.append(e.data["text"])
    return "\n".join(lines) if lines else "（無）"


def build_critique_prompt(events: list[Event], stats: CritiqueStats) -> str:
    """跟 `minutes.build_minutes_prompt()` 一樣的逐字稿抽取方式
    （`[時間] 講者：內容`），額外在最前面加「## 與會者」名單，中間插入
    「## 發言統計」／「## 主席介入紀錄」兩節（順序刻意：先給量、再給主席做過
    什麼、最後才是長逐字稿），逐字稿本身經過 `_compact_transcript()` 壓縮
    （見該函式；未達門檻時行為與壓縮前完全相同）。

    2026-09-06 補上 docs/DEFERRED_DEFECTS.md 第 7 項標記的落差：`CRITIQUE_SYSTEM`
    開場明講「你會拿到…主席介入紀錄與發言統計」，這裡現在真的組進去了。
    """
    roster = "、".join(p.name for p in stats.participants) if stats.participants else "（無）"
    compacted = _compact_transcript(events, stats.now)
    return (f"## 與會者\n{roster}\n\n"
            f"## 發言統計\n{_render_stats_table(events, stats)}\n\n"
            f"## 主席介入紀錄\n{_render_interventions(events)}\n\n"
            f"## 逐字稿\n{_render_transcript(compacted)}")


def _call_critique_llm(events: list[Event], stats: CritiqueStats) -> dict:
    body = {
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "messages": [{"role": "system", "content": CRITIQUE_SYSTEM},
                     {"role": "user", "content": build_critique_prompt(events, stats)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read())
    return json.loads(payload["choices"][0]["message"]["content"])
