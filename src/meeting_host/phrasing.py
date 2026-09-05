"""句型庫：背景預先向 LLM 要「帶插槽的句型」，快路觸發當下只做記憶體取用＋填值。

為什麼要拆成兩段（T14）：規則觸發當下不能等 LLM——那是快路（`fast_path.py`）
零延遲存在的理由；但主席開口的事實（名字、分鐘數）必須跟 `Trigger` 帶的完全
一致，不能讓 LLM 在講話當下順手編錯，也不能讓它「順便」自己編一個數字出來。
所以背景先請 LLM 想幾種「說法的骨架」（帶 `{target}`／`{mins}` 這類插槽，
不是成品句），觸發當下只是把事實用 `str.format` 填進去——這條路徑完全是
記憶體操作，沒有任何網路呼叫。

驗證（`validate_pattern`）是這裡最重要的安全性質：任何生成回來、驗證沒過的
候選一律丟棄，不修補、不降級使用——一句插槽對不上或帶了捏造數字的句型，
主席會當眾講錯話。佇列空了（LLM 還沒回、失敗、用完、或根本沒開 LLM）就讓
呼叫端（`fast_path.utterance_for`／`greeting_text`）退回既有寫死模板，
這裡的 `PhraseBank.take()` 對此完全無感——它只是回傳 None。

放在獨立模組而不是塞進 `fast_path.py`：`fast_path.py` 的定位是「純規則、
零延遲、不呼叫 LLM」（見該檔案 docstring），生成與驗證邏輯雖然不會在規則
觸發當下被呼叫，但它終究是「呼叫 LLM」的程式碼，混進去會讓那個檔案的
不變量（整個檔案不碰網路）變得不再一眼可辨。`live.py` 則是接線層，不適合
放演算法（插槽驗證、prompt 組裝）本身。
"""
import json
import re
import urllib.request
from collections.abc import Callable

# ── 句型規格：每個 kind 允許的插槽 ──────────────────────────────────────
# (必要插槽, 額外允許的插槽)。候選句型的插槽集合必須「恰好」涵蓋必要插槽、
# 且不能出現規格之外的插槽——見 validate_pattern。「問候」的 {topic} 是
# 額外允許但非必要：句型可以完全不提議題，也可以剛好用一次 {topic}。
SLOT_SPECS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "發言超時": (frozenset({"target", "mins"}), frozenset()),
    "有人被冷落": (frozenset({"target"}), frozenset()),
    "議程超時": (frozenset({"mins"}), frozenset()),
    "全場沉默": (frozenset(), frozenset()),
    "問候": (frozenset(), frozenset({"topic"})),
}
PHRASE_KINDS: tuple[str, ...] = tuple(SLOT_SPECS)

# 句型長度上下限（含插槽的原始字數，不是填值後的長度）。太短沒內容；
# 太長主席講起來會拖，也超出既有話術「≤2 句」的長度感（參考 slow_path.py
# TEMPLATE 對 utterance 的要求）。
MIN_CHARS = 6
MAX_CHARS = 50

# 同一場會議最多呼叫幾次生成器（不論成功失敗都算一次），避免長會議無限呼叫。
# 5 個 kind 開場各補一次是 5 次；之後偶爾補幾次頂多再抓 5 次——10 留一點餘裕，
# 不是精算出來的數字，是「夠開場＋幾次補充、又有明確上限」的取捨。
MAX_GENERATIONS_PER_MEETING = 10
# 佇列剩幾句（含）以下就該再補——不用等真的耗盡到 0 才補，留一點提前量。
REFILL_LOW_WATER_MARK = 1

_SLOT_RE = re.compile(r"\{(\w+)\}")


def _has_fabricated_number(text: str) -> bool:
    """插槽以外還出現數字，幾乎可以肯定是 LLM 自己編出來的事實（例如寫死
    一個分鐘數）。先把合法的 `{slot}` 挖掉，再檢查剩下的文字裡有沒有數字——
    含全形數字，`str.isdigit()` 對 Unicode digit 類別都算數。"""
    return any(ch.isdigit() for ch in _SLOT_RE.sub("", text))


# ── 模型輸出的字元衛生 ───────────────────────────────────────────────────
#
# 2026-09-05 實測：`gpt-5.6-luna` 寫參與者名字「Billis」時，會在 B 與 illis
# 之間插入雜字元。同一個評分點用真實 state 重跑 15 次，3 次（20%）出現
# 零寬空格 `B\u200b\u200billis`，另有一次整批跑出喬治亞字母 `Bილის`。
# 不是我們的程式弄壞的——OpenCC 只作用在 STT 輸入端，`slow_path.phrase()`
# 從 LLM 回來只做 `.strip()`；事件檔裡記的就是模型回傳的原文。
#
# 觀戰畫面會直接把這串字顯示出去，TTS 也會照唸。
#
# 分兩層處理，因為這兩種壞法可修復性不同：
#   零寬字元 → 直接清掉。它們不可見，移除之後得到的就是模型本來要寫的字，
#             沒有任何語意損失，這不是「刪掉當修好」。
#   其他外文字母 → 修不回來（把喬治亞字母從 Bილის 拿掉只剩 Bis），
#             交給呼叫端重生一次。

INVISIBLE = "\u200b\u200c\u200d\ufeff\u2060"
"""零寬空格／連接符／不換行零寬空格。純粹的傳輸雜訊，一律清掉。"""

# 允許出現在主席話術裡的字元。刻意用白名單，但範圍開得寬——寧可漏掉一種壞法，
# 也不要誤殺正常內容：
#   CJK 統一漢字、CJK 標點（含全形空格）、全形字母數字
#   日文假名（會議裡引用日文詞是合理的，術語卡判準也明文允許）
#   ASCII 字母數字與常見標點（中英夾雜是這個場景的常態）
ALLOWED = re.compile(
    r"[\u4e00-\u9fff"      # CJK 統一漢字
    r"\u3000-\u303f"       # CJK 標點與全形空格
    r"\u3040-\u30ff"       # 平假名、片假名
    r"\uff00-\uffef"       # 全形字母數字與標點
    r"\u2010-\u2027"       # 連字號、破折號、引號、刪節號
    r"\u00b0\u00b7\u2103\u2030"   # 度、間隔號、攝氏、千分號
    r"A-Za-z0-9\s"
    r"!-/:-@\[-`{-~"        # ASCII 標點全段
    r"]"
)


def strip_invisible(text: str) -> str:
    """清掉零寬字元。可逆的那一層——清完就是模型本來要寫的字。"""
    return text.translate({ord(c): None for c in INVISIBLE})


def unexpected_chars(text: str) -> list[str]:
    """回傳白名單以外的字元（去重、保序）。空 list ＝ 這句話乾淨。

    呼叫端**先** `strip_invisible()` 再問這裡——零寬字元屬於可修復的那一層，
    不該讓它把整句話判成壞掉。
    """
    return list(dict.fromkeys(c for c in text if not ALLOWED.match(c)))


def validate_pattern(kind: str, text: str) -> bool:
    """候選句型合不合格。不合格就丟棄，不修補——見模組 docstring。

    依序檢查：型別／長度 → 括號有沒有配對成合法的 `{slot}`（孤立的括號
    會讓後面的 `str.format` 直接炸掉）→ 插槽集合是否在允許範圍內、
    必要插槽有沒有齊全 → 有沒有插槽以外的數字（可能的捏造事實）。
    """
    if kind not in SLOT_SPECS or not isinstance(text, str):
        return False
    text = text.strip()
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    stripped = _SLOT_RE.sub("", text)
    if "{" in stripped or "}" in stripped:
        return False  # 孤立括號：不是合法的 {slot} 寫法
    required, optional = SLOT_SPECS[kind]
    found = set(_SLOT_RE.findall(text))
    if found - (required | optional):
        return False  # 出現規格之外的未知插槽
    if required - found:
        return False  # 缺少必要插槽
    if unexpected_chars(text):
        # 這裡刻意**不**先 strip_invisible：句型是重複使用的模板，帶著隱形字元
        # 會讓之後每一次填值都夾雜它。句型庫一次生 4 個候選，丟掉一個還有別的，
        # 所以沿用本模組既有的「不合格就丟棄，不修補」政策（見模組 docstring）。
        # 慢路的單句話術不同——那裡沒有備選，所以才做 strip ＋ 重生一次。
        return False
    if _has_fabricated_number(text):
        return False  # 插槽以外的數字：可能的捏造事實
    return True


def fill(pattern: str, **facts) -> str | None:
    """把事實填進句型。理論上通過 validate_pattern 的句型不會缺插槽，
    這裡仍防禦一次——寧可回 None 讓呼叫端退回寫死模板，也不要讓一句
    格式異常的模板讓主席當眾出包。"""
    try:
        return pattern.format(**facts)
    except (KeyError, IndexError, ValueError):
        return None


class PhraseBank:
    """每個 kind 一個句型佇列。

    `take()` 是純記憶體操作：規則觸發當下呼叫零延遲、零網路，這是這個類別
    最重要的性質。`refill()` 才會（若有注入生成器）打一次 LLM，且永遠由
    呼叫端排到背景執行緒／task 裡跑（見 `live.Session.watch_phrasing`）——
    這個類別本身完全不知道 asyncio，也不會自己決定什麼時候該補。

    `generator` 為 None（例如 `--no-llm`）時，`can_generate()` 恆為 False，
    佇列永遠是空的，`take()` 永遠回 None——呼叫端據此退回既有寫死模板，
    行為與沒有這個功能之前完全一致（驗收 12）。
    """

    def __init__(self, generator: Callable[[str, str | None], list[str]] | None = None,
                 topic: str | None = None):
        self._queues: dict[str, list[str]] = {k: [] for k in PHRASE_KINDS}
        self._generator = generator
        self.topic = topic
        self._generations = 0

    @property
    def generations(self) -> int:
        return self._generations

    def take(self, kind: str) -> str | None:
        q = self._queues.get(kind)
        if not q:
            return None
        return q.pop(0)

    def needs_refill(self, kind: str) -> bool:
        return len(self._queues.get(kind, [])) <= REFILL_LOW_WATER_MARK

    def can_generate(self) -> bool:
        return self._generator is not None and self._generations < MAX_GENERATIONS_PER_MEETING

    def refill(self, kind: str) -> None:
        """打一次生成器、驗證後把合格的句型加進佇列。

        任何失敗（例外、回傳格式不對、候選不合格）都只代表這次沒補到，
        絕對不能讓例外傳播出去影響會議進行（驗收 3）——保底層（既有寫死
        模板）永遠在，補不到就是繼續用保底層，不是錯誤。
        """
        if not self.can_generate():
            return
        self._generations += 1  # 不論成不成功都算一次呼叫，見模組常數說明
        try:
            candidates = self._generator(kind, self.topic)
        except Exception:  # noqa: BLE001 — 生成失敗不能拖垮會議
            return
        if not isinstance(candidates, list):
            return
        for c in candidates:
            if isinstance(c, str) and validate_pattern(kind, c.strip()):
                self._queues[kind].append(c.strip())


_GREETING_FALLBACK = "大家好，我是今天的主席，會議開始。"


def greeting_text(bank: PhraseBank, topic: str | None) -> str:
    """問候的話術：優先取用已生成好的版本，佇列空了或填值失敗就退回既有那句。

    `bank.take()` 是純記憶體操作——問候的送出時機完全由 `HelloGate` 決定，
    這裡不會、也不能因為句型還沒生成好而延後（驗收 9）。
    """
    pattern = bank.take("問候")
    if pattern is None:
        return _GREETING_FALLBACK
    filled = fill(pattern, topic=topic or "")
    return filled if filled is not None else _GREETING_FALLBACK


# ── 真實生成器：重用 slow_path 的 LLM 設定 ───────────────────────────────
#
# `API_URL`／`MODEL`／`EFFORT`／`_api_key()` 直接沿用 slow_path.py 既有的，
# 不另立一套（`minutes.py` 也是這樣重用的，見該檔案開頭 import）。

_SYSTEM = """你在幫一個開會主持 AI 準備「說話的骨架」，不是幫它想好要講的完整句子。

這些骨架之後會由程式自動把真正的事實（人名、分鐘數）填進去再講出來——你在這裡
完全不知道實際發生了什麼事，所以絕對不能自己編造任何具體的人名或數字，只能用
題目指定的 {插槽} 佔位。你的任務只有一個：想出幾種不同的說法，讓同一件事
聽起來不會每次都一模一樣。

硬性規則：
1. 只能使用題目指定的插槽名稱，一個都不能多、也不能漏
2. 絕對不可以出現任何阿拉伯數字或具體人名——凡是事實都必須留給插槽
3. 每句話 10 到 40 個字之間，太短沒內容、太長主席講起來會拖
4. 語氣自然、口語，像真人主持人臨場會說的話，不要書面語或條列式
5. 幾種說法之間語意要相同，但用詞、句型要有明顯差異，不能只是換一兩個字
6. 只能講題目情境明講的事，不可以自己補上任何「聽起來很合理但沒人告訴你」的
   前提——例如某人之前有沒有發言過、大家剛剛討論出了什麼、現在有沒有一個
   「提案」「方案」或已成形的方向。這些主席一律不知道，講出來就是當眾說錯話"""

# ⚠️ 這些情境敘述是句型的事實來源，寫錯一個字，LLM 生出來的每個變體都會帶著
# 同一個假前提（T33 就是這樣讓「從開會到現在還沒說話」長出一整批變體的）。
# 每一句都必須跟 fast_path.check() 真正檢查的條件對得起來，規則沒檢查的事
# 一律要在這裡明講「你不知道」。
_KIND_DESC = {
    "發言超時": "有人已經連續發言太久了，主席要請他先停下來，讓其他人有機會接話。",
    "有人被冷落": "有一位參與者已經有一陣子沒有發言了，主席想把發言權遞給他、"
                  "邀請他說說看法。⚠️ 主席並不知道他之前有沒有發言過——"
                  "他可能整場都沒開口，也可能剛剛才講了一大段、只是最近安靜下來。",
    "議程超時": "會議剩餘時間不多了，主席要提醒大家該開始收斂、往結論走。",
    "全場沉默": "現場已經有一陣子沒有任何人說話，主席想打破沉默、邀請大家先分享想法。"
                "⚠️ 主席不知道大家先前討論到哪裡，也不知道有沒有形成任何方向——"
                "有可能從頭到現在都還沒有人開口。",
    "問候": "會議剛開始，主席要跟大家打招呼、簡短開場。",
}

_SLOT_INSTRUCTIONS = {
    "發言超時": "這句話必須包含恰好兩個插槽：{target}（被提醒的人名）與 {mins}"
                "（他這一輪已經連續講滿的分鐘數，向下取整，所以只能當成"
                "「至少講了這麼久」來用，不要寫成「剛好」「整整」）。"
                "不要再額外提到任何名字或數字。",
    "有人被冷落": "這句話必須包含恰好一個插槽：{target}（被邀請發言的人名）。"
                  "不要提到任何數字。"
                  "⚠️ 絕對不可以斷言他沒發言過：「從開會到現在還沒說話」、"
                  "「都還沒聽過你的意見」、「還沒輪到你講」、「第一次請你說說」"
                  "這類講法一律不行——他很可能剛剛才講了一大段，只是最近安靜下來，"
                  "這句話會當場被戳破。只能用對「整場沒開口」與「講過但安靜很久」"
                  "兩種人都成立的說法，例如「有一陣子沒聽到你的聲音」。"
                  "也不要假設現在桌上有「這個提案」「這個方案」「剛剛的結論」——"
                  "主席不知道大家正在談的是什麼形態的東西，直接請他說看法就好。",
    "議程超時": "這句話必須包含恰好一個插槽：{mins}（會議剩下的分鐘數，"
                "向上取整，所以只能當成「最多還有這麼久」來用）。"
                "不要提到任何人名或其他數字。",
    "全場沉默": "這句話不能包含任何插槽（不能出現大括號），也不能提到任何"
                "人名或數字，因為現在不確定是誰、沉默了多久。"
                "⚠️ 也不可以提到先前的討論內容：「回顧一下剛剛講到哪」、"
                "「目前的方向大家覺得如何」、「剛剛那個提案」這類講法一律不行——"
                "主席不知道之前有沒有討論、更不知道有沒有方向，有可能整場還沒有人"
                "開口過。只能講「現在」這一刻確定成立的事：沒有人在說話，"
                "以及邀請大家開口。",
    "問候": "這句話原則上不需要任何插槽；如果想呼應會議主題，最多可以用一次"
            "{topic} 這個插槽，不要用其他任何插槽，也不要提到任何人名或數字。"
            "⚠️ {topic} 會被原樣替換成使用者自己下的議題名稱（可能是「黑客松籌備」、"
            "「Q3 產品規劃」、「新人 onboarding 流程」），你不知道它長什麼樣，所以要把它"
            "當成一個完整、不可拆解的名詞來用：不可以在它後面自己補「的進度」「的規劃」"
            "「的準備工作」這類尾巴，否則議題名稱本身已含那個詞時會變成疊字語病"
            "（例如議題是「黑客松籌備」，句型寫成「聊聊{topic}的籌備進度」，"
            "唸出來就是「聊聊黑客松籌備的籌備進度」）。用「今天我們來談{topic}」、"
            "「今天的主題是{topic}」這種對任何議題名稱都成立的寫法。",
}

_USER_TEMPLATE = """情境：{kind_desc}
{topic_line}{slot_instruction}

請給我 4 種不同的說法，用以下 JSON 格式回覆，不要有其他內容：
{{"phrasings": ["說法1", "說法2", "說法3", "說法4"]}}"""


def build_prompt(kind: str, topic: str | None) -> str:
    """只有「問候」的 prompt 會帶議題——其餘 kind 的插槽驗證會擋掉插槽以外
    的數字（`_has_fabricated_number`），議題本身若含數字（例如「黑客松2026」）
    被模型原樣引用進句型，會讓那個 kind 的候選整批被誤判成「捏造事實」而
    丟棄，白白浪費一次生成額度。問候不受這個限制——它允許的插槽本來就
    包含 {topic}，議題交給插槽帶入，不會被當成句型本文裡的數字。
    """
    topic_line = f"這場會議的議題是「{topic}」。\n" if (topic and kind == "問候") else ""
    return _USER_TEMPLATE.format(
        kind_desc=_KIND_DESC[kind],
        topic_line=topic_line,
        slot_instruction=_SLOT_INSTRUCTIONS[kind])


def generate_patterns(kind: str, topic: str | None) -> list[str]:
    """真正打一次 LLM，回傳候選句型（未驗證）。

    驗證與例外處理交給呼叫端（`PhraseBank.refill`）——這裡只管把 API
    回應解析成字串陣列，解析失敗（缺鍵、型別不對）就讓例外往上拋，
    `refill()` 的 try/except 會接住。
    """
    from .slow_path import API_URL, EFFORT, MODEL, _api_key
    body = {
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": build_prompt(kind, topic)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    result = json.loads(payload["choices"][0]["message"]["content"])
    phrasings = result.get("phrasings", [])
    return phrasings if isinstance(phrasings, list) else []
