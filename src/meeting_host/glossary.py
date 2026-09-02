"""提示卡：會議裡出現術語／專有名詞時，靜默在觀戰 UI 印一張說明卡。

這是「貢獻」而不是「糾正」——它跟現有十種介入的性質完全不同：

- **不經過 Chair**：不建 `Intervention`、不呼叫 `chair.request()`、不寫
  `st.interventions`、不佔冷卻期額度、不發 TTS。主席一句話都不會多說。
- **不影響快路慢路**：`live.Session.watch_glossary()` 是獨立的背景 task，
  只讀 `st.utterances`，不寫任何共用狀態（`done`／`interventions`／`revision`）。
  它自己失敗（LLM 逾時、搜尋掛掉）只印一行警告，不往外傳。
- **延遲不重要**：完全靜默的東西沒有即時性壓力，術語講完 10 秒才印完全可接受。
  這個自由度換來的是「批次處理」——不必每 5 秒問一次 LLM。

## 判準：什麼算「關鍵字或專有名詞」

分兩段，各自負責它擅長的事：

1. **抽取（LLM，批次）**：只有語言模型分得出「八方雲集」是專有名詞而「準備」
   不是。中文沒有詞界，沒有斷詞器的情況下純規則抽詞會大量吐出「就是」「這樣」
   這種功能詞（實測過），所以這一步交給 LLM。但**絕對不是每 5 秒一次**——
   累積 `BATCH_MIN_UTTERANCES` 則新發言、或距上次超過 `BATCH_MAX_WAIT_SECONDS`
   才跑一次（見 `live.Session.watch_glossary`）。
2. **驗證與組卡（純程式，零成本）**：LLM 只回「詞」，卡片的每一個字都由這裡從
   `st.utterances` 組出來。LLM 回的詞若在逐字稿裡找不到就直接丟棄
   （`build_card` 回 None）——捏造在結構上不可能發生，不必靠 prompt 求它別編。

## 不得捏造：兩道結構性防線

每一張印出來的卡片都必然帶得出處，這不是靠提示詞，是靠資料流：

- 卡片主體（首次提到的人、時間、原話、提到次數、會議裡的解釋）**全部**是
  `Utterance` 欄位的直接引用，沒有任何一個字經過模型。
- 網路補充（`gloss`）只有在 `sources` 非空——也就是搜尋回傳裡真的有
  `url_citation` 標註——時才會保留；沒有來源連結就整段丟掉（`build_card`）。

`is_printable()` 再收一次口：卡片要嘛有來源連結、要嘛有會議裡真的有人解釋過的
原話、要嘛這個詞已經被提到夠多次（`MIN_MENTIONS_UNEXPLAINED`）。三個都不滿足
就不印——「這個詞出現過」本身不構成一則對會議有用的資訊。
"""
from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .state import Utterance

# ── 批次節奏（由 live.Session.watch_glossary 使用）────────────────────────
# 12 則發言 ≈ 實測會議的一分多鐘。延遲不重要，所以取值偏大：2026-08-29 那場
# 125 則發言、逐字稿橫跨 11 分鐘的會議，整場只跑 12 次抽取＋10 次網路查詢
# （實測），而慢路在同一場評分了 34 次。`BATCH_MAX_WAIT_SECONDS` 是給
# 「講得很慢的會議」的保險，避免發言數一直湊不滿 12 就永遠不抽。
BATCH_MIN_UTTERANCES = 12
BATCH_MAX_WAIT_SECONDS = 120.0

# 同一場會議的硬上限。抽取呼叫很便宜（實測整場 12 次、約 8k tokens），
# 貴的是帶網路搜尋的補充（每次約 8.5k tokens），所以只對後者設較緊的預算。
MAX_WEB_LOOKUPS = 12
MAX_CARDS = 20

# 沒有來源連結、會議裡也沒有人解釋過的詞，要被提到幾次才值得印一張
# 「這個詞可能不是大家都懂」的卡。1～2 次多半是隨口帶過，印了只是雜訊。
MIN_MENTIONS_UNEXPLAINED = 3

# 詞的長度界線：1 個字判不出是不是術語；超過 12 個字幾乎都是模型把一整句
# 話當成「詞」回來了（那種東西當卡片標題沒有意義）。
MIN_TERM_CHARS = 2
MAX_TERM_CHARS = 12

# ── 「會議裡有人解釋過」的判準 ────────────────────────────────────────────
# 只認「X 就是 ……」這種緊接在詞後面的解釋句型，不是「整句話裡有出現這些字」。
# 中文口語裡「就是」是超高頻語助詞（實測逐字稿：「就是，就是岔題太久」），
# 放寬成整句掃描的話幾乎每句都會被判成「有人解釋過」，那個標籤就沒有意義了。
EXPLAIN_MARKERS = (
    "就是", "意思是", "意思就是", "是指", "指的是", "叫做", "叫作", "也就是",
    "簡單說", "簡單講", "白話說", "定義是", "全名是", "英文叫", "中文叫", "指的就是",
)
EXPLAIN_GAP_CHARS = 4    # 提示詞必須出現在術語後面這麼多字以內
EXPLAIN_MIN_TAIL = 6     # 提示詞後面還要有這麼多字，否則那不是解釋，只是語助詞

_MD_LINK = re.compile(r"\(?\[[^\]]*\]\(https?://[^)]+\)\)?")
_NO_RESULT = "查無資料"


# ── 資料型別 ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Mention:
    """逐字稿裡的一個位置。三個欄位都是 `Utterance` 的直接複製，不加工。"""
    speaker: str
    t: float          # 會議相對秒，與 Utterance.start 同座標（觀戰 UI 據此換算時鐘）
    text: str         # 原話，逐字，不摘要

    def as_data(self) -> dict:
        return {"speaker": self.speaker, "t": self.t, "text": self.text}


@dataclass(frozen=True)
class Source:
    title: str
    url: str

    def as_data(self) -> dict:
        return {"title": self.title, "url": self.url}


@dataclass(frozen=True)
class Card:
    """一張提示卡。`first` 恆存在——所以每張卡都指得到逐字稿的具體位置。"""
    term: str
    mentions: int
    first: Mention
    explained: Mention | None = None
    gloss: str | None = None
    sources: tuple[Source, ...] = ()

    def as_data(self) -> dict:
        return {
            "term": self.term,
            "mentions": self.mentions,
            "first": self.first.as_data(),
            "explained": self.explained.as_data() if self.explained else None,
            "gloss": self.gloss,
            "sources": [s.as_data() for s in self.sources],
        }


# ── 純函式：驗證與組卡（零網路、零成本）──────────────────────────────────
def normalize(term: str) -> str:
    """比對用的正規化形式：去頭尾空白、轉小寫（`AI` 與 `ai` 是同一個詞）。"""
    return " ".join(str(term).split()).lower()


def looks_like_term(term: str, participants: Sequence[str] = ()) -> bool:
    """這個字串本身有沒有資格當一張卡的標題。純形狀檢查，不判斷語意。

    排除參與者名字：實測 LLM 會把 `Alex Huang`／`MiMi` 當成專有名詞回來
    （它們確實是專有名詞，但對在場的人來說是零資訊）。名單由呼叫端傳進來，
    不寫死。
    """
    t = " ".join(str(term).split())
    if not (MIN_TERM_CHARS <= len(t) <= MAX_TERM_CHARS):
        return False
    if not any(ch.isalnum() for ch in t):
        return False
    if t.isdigit():
        return False
    n = normalize(t)
    for p in participants:
        pn = normalize(p)
        if pn and (n == pn or n in pn or pn in n):
            return False
    return True


def find_mentions(term: str, utterances: Sequence[Utterance]) -> list[Utterance]:
    """逐字稿裡真的出現過這個詞的發言，依 `start` 排序。

    大小寫不敏感（`Line`／`LINE`／`line` 算同一個詞）。同一則發言裡講兩次
    只算一則——「提到幾次」的語意是「在幾段話裡被提起」，不是字串出現次數；
    後者會被口語重複（「你看一下漏梗，你看一下漏梗」）灌水。
    """
    n = normalize(term)
    if not n:
        return []
    hits = [u for u in utterances if n in u.text.lower()]
    return sorted(hits, key=lambda u: u.start)


def find_explanation(term: str, utterances: Sequence[Utterance]) -> Utterance | None:
    """會議裡有沒有人真的解釋過這個詞——回傳最早的那一則發言。

    判準見 `EXPLAIN_MARKERS` 上方說明：提示詞必須緊接在術語後面
    （`EXPLAIN_GAP_CHARS` 字以內）且後面還有實質內容（`EXPLAIN_MIN_TAIL` 字），
    才算解釋，不然只是把語助詞當成定義。
    """
    n = normalize(term)
    if not n:
        return None
    for u in sorted(utterances, key=lambda x: x.start):
        low = u.text.lower()
        start = low.find(n)
        while start != -1:
            after_term = start + len(n)
            window = u.text[after_term:after_term + EXPLAIN_GAP_CHARS + 4]
            for marker in EXPLAIN_MARKERS:
                pos = window.find(marker)
                if pos == -1 or pos > EXPLAIN_GAP_CHARS:
                    continue
                tail = u.text[after_term + pos + len(marker):]
                if len(tail.strip()) >= EXPLAIN_MIN_TAIL:
                    return u
            start = low.find(n, start + 1)
    return None


def _mention(u: Utterance) -> Mention:
    return Mention(speaker=u.speaker, t=u.start, text=u.text)


def build_card(term: str, utterances: Sequence[Utterance],
               gloss: str | None = None,
               sources: Sequence[Source] = ()) -> Card | None:
    """把一個候選詞組成卡片；組不出來回 None。

    **這是防捏造的關鍵一步**：詞在 `utterances` 裡找不到就直接回 None——
    不管 LLM 多有信心，沒有出處的東西不會變成卡片。同理，`gloss` 只有在
    `sources` 非空時才留下來：沒有來源連結的一句話說明，等於模型自由發揮。
    """
    hits = find_mentions(term, utterances)
    if not hits:
        return None
    src = tuple(sources)
    text = (gloss or "").strip()
    if not src or not text or text == _NO_RESULT:
        text, src = None, ()
    return Card(
        term=" ".join(str(term).split()),
        mentions=len(hits),
        first=_mention(hits[0]),
        explained=(lambda e: _mention(e) if e else None)(find_explanation(term, utterances)),
        gloss=text,
        sources=src,
    )


def is_printable(card: Card) -> bool:
    """這張卡有沒有值得佔畫面一格。

    三個門檻任一即可，而三個都保證卡片帶得出處：
    - 有來源連結（網路查到了，`gloss` 才會留著）
    - 會議裡有人解釋過（`explained` 帶著那句原話與時間）
    - 被提到夠多次（`MIN_MENTIONS_UNEXPLAINED`）——沒人解釋又反覆出現，
      本身就是「這個詞可能不是大家都懂」的訊號
    """
    return bool(card.sources) or card.explained is not None \
        or card.mentions >= MIN_MENTIONS_UNEXPLAINED


# ── LLM：抽取（批次，便宜）───────────────────────────────────────────────
# `API_URL`／`MODEL`／`EFFORT`／`_api_key()` 沿用 slow_path.py 既有的，
# 不另立一套（phrasing.py／minutes.py 也是這樣重用的）。

_EXTRACT_SYSTEM = """你在幫一場進行中的會議挑出「值得在旁邊補一張說明卡」的詞。

只挑這三種：
1. 專有名詞——公司、產品、品牌、服務、地名、活動名、專案代號
2. 專業術語或行話——某個領域才會這樣用的詞
3. 外語詞——夾在中文裡的英文或日文詞，且不是招呼語或語助詞

絕對不要挑：
- 一般常用詞、動詞、形容詞（開會、準備、比賽、講話、厲害）
- 語助詞與招呼語（OK、so、yeah、哈囉、拜拜）
- 代名詞、時間詞、數量詞（我們、明天、三次）
- 情緒用語與髒話
- 在場參與者的名字
- 句子或片語——只挑「詞」，不挑一整句
- 看起來被聽錯、拼錯、意思不通的字（逐字稿來自語音辨識，本來就會有錯字）

寧可少挑也不要濫挑：一段對話挑出 0 個詞是完全正常、也是最常見的結果。
每個詞必須**原封不動**照抄逐字稿裡出現的字，不要改寫、不要補字、不要翻譯。"""

_EXTRACT_USER = """在場參與者（他們的名字不要挑）：{participants}
已經挑過、不用再挑的詞：{known}

這是會議逐字稿的新片段：
{batch}

用以下 JSON 格式回覆，不要有其他內容：
{{"terms": ["詞1", "詞2"]}}
沒有值得挑的詞就回 {{"terms": []}}。"""


def format_batch(utterances: Sequence[Utterance]) -> str:
    """把一批發言排成餵給抽取模型的文字（格式與 slow_path 的逐字稿一致）。"""
    return "\n".join(
        f"[{int(u.start) // 60:02d}:{int(u.start) % 60:02d}] {u.speaker}：{u.text}"
        for u in utterances)


def extract_terms(utterances: Sequence[Utterance], known: Sequence[str],
                  participants: Sequence[str]) -> list[str]:
    """打一次 LLM，回傳候選詞（**未驗證**）。

    驗證一律由呼叫端的 `build_card()` 做——這裡回什麼都不重要，回不存在的詞
    只會在下一步被丟掉。例外不在這裡吞，交給 `Glossary.run_batch`。
    """
    from .slow_path import API_URL, EFFORT, MODEL, _api_key
    body = {
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "messages": [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": _EXTRACT_USER.format(
                participants="、".join(participants) or "（不詳）",
                known="、".join(known) or "（無）",
                batch=format_batch(utterances))},
        ],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    result = json.loads(payload["choices"][0]["message"]["content"])
    terms = result.get("terms", [])
    return [t for t in terms if isinstance(t, str)]


# ── LLM＋網路搜尋：補充說明（貴，有預算上限）─────────────────────────────
# 走 OpenAI Responses API 的 web_search 工具。實測（2026-08-31）三件事：
#   1. 沿用 slow_path 既有的 model／key 就能用，不必新增任何依賴或憑證。
#   2. **不強制**（沒有 `tool_choice`）時模型會直接憑記憶回答、完全不搜尋，
#      回傳裡連一個 `web_search_call` 都沒有——所以這裡把工具設成強制。
#   3. 就算搜了，**沒有明講要引用來源**時回傳也不會帶 `url_citation` 標註，
#      拿不到連結。所以提示詞裡「一定要引用你實際看過的網頁」那句是必要的，
#      不是禮貌用語——沒有它，`sources` 恆為空，卡片就永遠拿不到網路補充。
RESPONSES_URL = "https://api.openai.com/v1/responses"
LOOKUP_EFFORT = "low"

_LOOKUP_PROMPT = """一場會議的逐字稿裡出現了「{term}」這個詞。前後文：
{context}

請先用網路搜尋查證，再用繁體中文寫一句話（40 字以內）說明它是什麼，
只根據你實際看過的網頁內容寫，不要加入網頁上沒有的推論或情境解讀，
並且一定要引用那個網頁作為來源（附上連結）。

以下任一情況，只回覆四個字：{no_result}
- 搜尋不到，或無法確定它是什麼
- 它只是日常常用詞、一般動詞形容詞、或情緒用語——**在場的人本來就都懂，
  補一則辭典定義對這場會議沒有任何幫助**
- 你唯一找得到的來源是通用語言辭典，而這個詞並不是專有名詞或專業術語"""


def _clean_gloss(text: str) -> str:
    """去掉模型塞在句子裡的 markdown 連結——連結另外用 `sources` 呈現，
    留在正文裡只會把 40 字的說明撐爆。"""
    out = _MD_LINK.sub("", text)
    return out.replace("  ", " ").strip().strip("。 ()（）").strip("來源：").strip()


def look_up(term: str, context: str) -> tuple[str | None, list[Source]]:
    """打一次帶網路搜尋的 LLM，回傳（一句話說明, 來源清單）。

    沒有 `url_citation` 標註就回 `(None, [])`——呼叫端據此讓卡片退回
    「只有逐字稿出處」的版本。例外不在這裡吞，交給 `Glossary.run_batch`。
    """
    from .slow_path import MODEL, _api_key
    body = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "tool_choice": {"type": "web_search"},   # 不強制的話模型會憑記憶回答（見上方說明）
        "reasoning": {"effort": LOOKUP_EFFORT},
        "input": _LOOKUP_PROMPT.format(term=term, context=context, no_result=_NO_RESULT),
    }
    req = urllib.request.Request(
        RESPONSES_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())

    text, sources, seen = "", [], set()
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            text += part.get("text", "")
            for note in part.get("annotations", []):
                url = note.get("url")
                if note.get("type") != "url_citation" or not url or url in seen:
                    continue
                seen.add(url)
                sources.append(Source(title=(note.get("title") or url), url=url))
    gloss = _clean_gloss(text)
    if not gloss or gloss == _NO_RESULT or not sources:
        return None, []
    return gloss, sources


# ── 追蹤器：一場會議一個實例 ─────────────────────────────────────────────
Extractor = Callable[[Sequence[Utterance], Sequence[str], Sequence[str]], list[str]]
Lookup = Callable[[str, str], "tuple[str | None, list[Source]]"]


@dataclass
class Glossary:
    """一場會議的提示卡狀態：抽過哪些詞、印過哪些卡、網路預算還剩多少。

    `extractor`／`lookup` 可注入，測試因此完全不需要碰網路。
    """
    extractor: Extractor = extract_terms
    lookup: Lookup | None = look_up
    max_web_lookups: int = MAX_WEB_LOOKUPS
    max_cards: int = MAX_CARDS
    printed: list[str] = field(default_factory=list)   # 已印出來的詞（原樣）
    _considered: set[str] = field(default_factory=set)  # 已判斷過的詞（正規化）
    web_lookups: int = 0

    def _is_new(self, term: str) -> bool:
        """這個詞判斷過了沒。除了完全相同，也擋「包含關係」的近似重複——
        `Line` 印過之後 `Line 群組` 不必再印一張，反之亦然。"""
        n = normalize(term)
        if not n or n in self._considered:
            return False
        return not any(n in seen or seen in n for seen in self._considered)

    def _context(self, hits: Sequence[Utterance]) -> str:
        """給網路查詢用的前後文：最多兩則真的提到這個詞的原話。"""
        return "\n".join(f"{u.speaker}：{u.text}" for u in hits[:2])

    def run_batch(self, batch: Sequence[Utterance], utterances: Sequence[Utterance],
                  participants: Sequence[str] = ()) -> list[Card]:
        """處理一批新發言，回傳這一批要印的卡（可能是空的，那是正常結果）。

        會打網路（抽取一次；每個新詞最多一次補充查詢），所以呼叫端要丟到
        thread 裡跑（見 `live.Session.watch_glossary`）。例外照常往外拋——
        由那個 task 統一接住並隔離，這裡不吞，免得吞掉之後查不出原因。
        """
        if len(self.printed) >= self.max_cards:
            return []
        cards: list[Card] = []
        for term in self.extractor(batch, list(self.printed), list(participants)):
            if len(self.printed) + len(cards) >= self.max_cards:
                break
            if not looks_like_term(term, participants) or not self._is_new(term):
                continue
            self._considered.add(normalize(term))
            hits = find_mentions(term, utterances)
            if not hits:
                continue  # 逐字稿裡根本沒這個詞——捏造，丟掉
            gloss, sources = None, []
            if self.lookup is not None and self.web_lookups < self.max_web_lookups:
                self.web_lookups += 1
                try:
                    gloss, sources = self.lookup(term, self._context(hits))
                except Exception as e:  # noqa: BLE001
                    # 網路補充失敗只影響「這一個詞」，不該連累同一批其他詞，也不該
                    # 讓整批重跑（重跑會再花一次抽取的錢）。這個詞退回只有逐字稿
                    # 出處的版本——那本來就是它的合格形態之一。額度照扣：真的打
                    # 出去了，不扣的話一個持續逾時的服務可以無限重試。
                    print(f"    ⚠️ 提示卡查詢失敗【{term}】{type(e).__name__}，改用逐字稿出處")
                    gloss, sources = None, []
            card = build_card(term, utterances, gloss, sources)
            if card is not None and is_printable(card):
                cards.append(card)
        self.printed.extend(c.term for c in cards)
        return cards
