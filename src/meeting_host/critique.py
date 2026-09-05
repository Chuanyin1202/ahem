"""AI 主席的「心聲」：對會議整體與每個與會者的真實判斷，輸入 `Session.events`。

面板顯示文字是「心聲」（2026-09-05 Zeal 中途拍板，理由：這類判斷不一定是負評，
「批判」預設負面，「心聲」更準）——但模組名、event kind（`ai_critique`）、旗標
（`--no-critique`）等內部識別名稱維持原樣不改，只有 UI 顯示字串與 JSON schema
是這次的更新範圍，見 `CRITIQUE_SYSTEM` 與 `live.Session.watch_critique`。

跟 `minutes.py` 的 A（會議產出）同一種形狀——一次 LLM 呼叫（沿用 `slow_path` 的
API 設定與 model），JSON schema 回傳，純函式＋統一入口方便單元測試不打真實 API。
跟「觀察／判斷／留意」三類規則算出來的觀察不同：這裡問的是 LLM 的判斷力，規則
湊不出來（見 Track G 工作單背景）。

`_call_critique_llm(events, participants)` 是唯一的 LLM 呼叫進入點；
`build_critique_prompt()` 是純函式，方便單元測試不打真實 API。呼叫失敗由呼叫端
（`live.Session.watch_critique`）接住，這裡不吞例外、不給預設值。
"""
import json
import urllib.request

from .events import Event
from .slow_path import API_URL, EFFORT, MODEL, _api_key


def fmt(t: float) -> str:
    """`MM:SS`。跟 `minutes.py`／`live.py` 的 fmt 邏輯相同，這裡獨立一份，理由同
    `minutes.py` 的模組註解——不 import live.py 拖進 discord/speaker 等重依賴。"""
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


# 2026-09-05 中途由 Zeal 插播、fable 專門設計的完整版本，取代原本的草稿——
# 逐字照抄，不憑印象轉述（見 CLAUDE.md「寫進正典/規格要逐字照抄」）。
CRITIQUE_SYSTEM = """你是會議主席的內心。你會拿到目前為止的逐字稿、主席介入紀錄與發言統計。
主席在檯面上維持中立與克制；你是他心裡那個看得更清楚的聲音——這些話不會說出口，
只顯示在旁觀面板上。所以可以比公開發言更直白、更有觀點，但要對得起「看得更清楚」這幾個字。

判斷原則：
1. 只根據逐字稿與介入紀錄裡實際出現的內容。不要編造沒發生的事，
   不要推測動機或心理狀態——「他說了什麼、做了什麼」可以評，
   「他心裡在想什麼、對議題有沒有興趣」你不知道，不要寫。
2. 批判的是行為與作用，不是這個人。語氣可以尖銳，不可以羞辱：
   不評智力、人格、口音或表達習慣，不用貶義標籤。
3. 批判不等於負評。會開得好就直說好在哪個具體行為。
   檢驗每一句：把人名換成別人還成立的句子是套話，刪掉重寫。
4. 立場對錯不歸你管。哪個提案比較好你不評；你評的是過程——
   誰在重複自己、誰只在附和、誰被晾著、共識是真的還是沒人敢反對。
5. 一個判斷一句話，50 字以內，只講此刻最值得說的那一件事。
   統計面板已經會報誰講多講少；你只說統計看不出來的東西。
6. 根據不足就留空，不要硬掰：
   - 整場還沒有值得說的 → "meeting" 給空字串
   - 某人有實質內容的發言不到兩句（「好」「沒問題」「我都可以」這類表態、
     設備雜務、寒暄都不算實質）→ 不要把他放進 "participants"，整個省略

用以下 JSON 格式回覆，不要有其他內容：
{
  "meeting": "對這場會議整體的一句真心話，或空字串",
  "participants": {"某人": "對這個人在這場會議裡的一句真心話"}
}"""


def build_critique_prompt(events: list[Event], participants: list[str]) -> str:
    """跟 `minutes.build_minutes_prompt()` 一樣的逐字稿抽取方式（`[時間] 講者：內容`），
    額外在最前面加一段「## 與會者」列出 `participants` 名單，讓 LLM 知道有哪些人
    可以評論。

    已知落差（見 docs/DEFERRED_DEFECTS.md）：`CRITIQUE_SYSTEM` 開場明講「你會拿到
    …主席介入紀錄與發言統計」，但這裡目前只給逐字稿＋名單，沒有真的附上介入紀錄
    或發言分佈——2026-09-05 中途插播只要求換掉 system prompt 與 JSON schema，
    明確保留這個函式原樣，此處刻意不擴充，留給下一棒決定要不要補。"""
    roster = "、".join(participants) if participants else "（無）"
    transcript = "\n".join(
        f"[{fmt(e.t)}] {e.data['speaker']}：{e.data['text']}"
        for e in events if e.kind == "utterance")
    return f"## 與會者\n{roster}\n\n## 逐字稿\n{transcript or '（無）'}"


def _call_critique_llm(events: list[Event], participants: list[str]) -> dict:
    body = {
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "messages": [{"role": "system", "content": CRITIQUE_SYSTEM},
                     {"role": "user", "content": build_critique_prompt(events, participants)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read())
    return json.loads(payload["choices"][0]["message"]["content"])
