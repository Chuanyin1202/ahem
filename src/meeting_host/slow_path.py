"""慢路：LLM 判斷 ＋ LLM 話術，兩次獨立呼叫。背景持續跑，不阻塞介入決策。

定案依據（validation-results.md #3b，24/24 實測）：
    gpt-5.6-luna ＋ reasoning_effort=none ＋ 三軸相對比較規則

三個設計來自論文，不要隨意改動：
- 三軸必填不互斥、「不介入」獨立評分  ← To Facilitate or not to Facilitate
- 先列正反理由再給分                  ← Inner Thoughts §5.4
- 只餵最近 N 則                       ← 近因假設

## 為什麼判斷與話術要拆成兩次呼叫（T29）

原本是一次呼叫同時產出三軸分數與話術。實測（experiments/utterance_variants.py，
34 個真實評分點 × 4 個變體，結果在 experiments/out/utterance-variants-*/variants.json）
證明了三件事：

1. **罐頭話不是 prompt 逼出來的**。把「≤2句／要給出可執行的下一步」兩個約束整個
   拿掉（v1），帶逐字引號的話術反而從 2/31 掉到 1/29——模型對「主席該說什麼」的
   預設就是「我們先回到 X；接下來各自提出 Y」，拿掉約束只會讓它更空。
2. **「只講這場真的出現過的事」這個門檻有效**。v2 把這條寫進話術指令，30/30 帶
   逐字引號，逐句核對沒有捏造。
3. **同一次呼叫做不到**。v3 在話術前面多要一個 `notice` 欄位先寫觀察：`notice`
   本身 33/34 帶引號、抓的是對的東西，但同一次呼叫寫出來的話術只有 4/29 保留
   引號、28/29 退回「我聽到…」開頭，還有 5 個點 notice 寫滿而 utterance 是空的。
   **模型看得見具體事實，寫話術時把它丟掉了。**
   而且 v2 只改話術指令，介入次數卻從 9 跳到 17——pros/cons／三軸分數／話術在
   同一份 JSON 裡，話術指令會回頭污染判斷。**在一次呼叫的架構下這兩件事分不開。**

所以拆成：
- `score()`：維持 TICK=5s 的節奏與 `EFFORT`，只回三軸分數／pros／cons／type，
  **完全不提話術**——判斷不再被話術指令污染。
- `phrase()`：只在判定要介入、且通過第一關閘門之後才呼叫（一場 6-12 次），
  帶 v2 那條有效的內容門檻。因為次數少，可以用比判斷更貴的 effort。

兩次呼叫的接線與 TOCTOU 重驗在 `live.Session._run_slow_score`，那裡有完整說明。
"""
import json
import os
import urllib.request
from pathlib import Path

from .state import MeetingState

MODEL = "gpt-5.6-luna"
EFFORT = "none"
API_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM = """你是一位專業的會議主席，正在旁聽一場進行中的會議。
你的職責不是參與討論，而是管理「群體過程」——確保會議在時間內收斂、每個人都被聽見、討論不偏離議題。

你要判斷此刻是否需要介入。請注意：介入是有成本的。
介入太多會招致反感，沒有人想被一直告知該說什麼、該怎麼說。"""

KEY_RULES = """判斷原則：
1. 看意圖與影響——發言者想做什麼，那些話會如何影響其他人
2. 必看前後文——單獨看沒問題的話，放進脈絡可能正在升溫，反之亦然
3. 評行為不評觀點——你同不同意那個看法完全不影響判斷，只看語氣、清晰度、建設性、對討論的影響
4. 保持一致——全程用同一套標準，只用實際出現的內容，不要過度詮釋"""

PHASE_RULES = """## 會議階段（由背景任務持續判定，不是你要推斷的）
目前：{phase}

先分清楚兩件不一樣的事，不要混為一談：
- 「拉回議題」＝話題已經整個跑到跟議題無關的地方，把它帶回議題範圍——這是維持會議的邊界，**不分階段，任何時候都該做**
- 「收斂」＝催促下結論、逼大家選邊、評斷哪個想法比較好——這才是分階段的事

同一個念頭在不同階段的對錯完全相反：
- 發散期：想法還在長。只要還在議題範圍內，鼓勵更多不同意見，**絕對不要收斂、催促或下結論**；但如果話題已經整個離開議題本身，一樣要拉回來——拉回議題不算收斂
- 呻吟區：衝突與不適是必然的，必須讓它走完。只維持秩序，**不要急著調解或推共識**
- 收斂期：此時才可以推進、裁決、封板

注意別矯枉過正：發散期本來就允許在議題之內天馬行空、繞遠路、講反例，那不是離題，不要因為偏離主線敘事就出手——只有整個話題已經跟議題無關時，才需要拉回來。

還有一種也不是離題：會議自己的雜務——調設備、確認聽不聽得到、傳檔案、找是哪一份文件、決定誰先講。這些話跟議題無關，但它們是在讓會議能夠進行，不是離開會議。這時候不要拉回議題，雜務處理完自然會回來；真正要拉的是議題之外的閒聊或別的話題。"""

TEMPLATE = """## 會議資訊
議題：{topic}
預計時長：{duration} 分鐘，目前進行到第 {elapsed:.0f} 分鐘
參與者：{participants}

## 發言統計
{stats}
{phase_block}
## 最近的對話
{transcript}

{rules}

## 你的任務
先列出兩個「現在該介入」的理由，再列出兩個「現在不該介入」的理由，然後才評分。

用以下 JSON 格式回覆，不要有其他內容：
{{
  "pros": ["理由1", "理由2"],
  "cons": ["理由1", "理由2"],
  "positive": <1-5，該鼓勵某人或某種行為的強度>,
  "negative": <1-5，該抑制某人或某種行為的強度>,
  "none": <1-5，此刻不需要任何介入的強度。獨立評分，不是前兩者的殘差>,
  "type": "<離題/重複/假共識/僵局/事實錯誤/無>"
}}"""


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env = Path(__file__).parent.parent.parent / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("找不到 OPENAI_API_KEY")


def decide(r: dict) -> str:
    """三軸相對比較。「不介入」軸有否決權——這是它跟論文原始門檻規則的差別，
    實測把誤報從 60%/80% 降到 0%（validation-results.md #3b）。"""
    p, n, none = r.get("positive", 0), r.get("negative", 0), r.get("none", 0)
    if max(p, n) <= none:
        return "不介入"
    if p == n:
        return "兩者皆有"
    return "正向介入" if p > n else "負向介入"


def should_score(st: MeetingState, now: float, last_n: int, *, busy: bool = False) -> bool:
    """慢路要不要在這一 tick 評分。live 與 replay 共用，回放才量得到產品行為。

    三個條件：
    - busy：主席有 pending／playing 時不評——評了也是過期候選
    - 沒有新 utterance 就不評——同樣的逐字稿再問一次只會得到同樣的話，06:34 起每 5 秒噴一次
    - 尊重快路的冷卻期——兩條路共用 interventions，剛介入過就閉嘴
    """
    if busy:
        return False
    from .fast_path import COOLDOWN_SECONDS
    if len(st.utterances) < 2 or len(st.utterances) == last_n:
        return False
    return st.since_last_intervention(now) >= COOLDOWN_SECONDS


def is_intervention(r: dict) -> bool:
    """評分結果算不算一次介入。

    `type == 無` 是實測有效的「其實不用介入」訊號（validation-results.md #3、#3b），
    即使三軸分數過了 decide()，也不算數——否則模型會一直「先不打斷，請 X 說完」。
    """
    return r.get("verdict") != "不介入" and r.get("type") not in ("無", "", None)


def build_prompt(st: MeetingState, now: float, phase: str | None = None) -> str:
    stats = "\n".join(
        f"- {p}：發言 {st.spoke_seconds(p) / 60:.1f} 分鐘（佔 {st.share(p, now):.0%}），"
        f"已 {st.silent_seconds(p, now) / 60:.1f} 分鐘沒發言"
        for p in st.participants)
    transcript = "\n".join(
        f"[{int(u.start) // 60:02d}:{int(u.start) % 60:02d}] {u.speaker}：{u.text}"
        for u in st.recent())
    return TEMPLATE.format(
        topic=st.topic, duration=st.duration_min, elapsed=now / 60,
        participants="、".join(st.participants),
        stats=stats, transcript=transcript, rules=KEY_RULES,
        phase_block=f"\n{PHASE_RULES.format(phase=phase)}\n" if phase else "")


def score(st: MeetingState, now: float, phase: str | None = None) -> dict:
    body = {
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": build_prompt(st, now, phase)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    result = json.loads(payload["choices"][0]["message"]["content"])
    result["verdict"] = decide(result)
    return result


# ── 第二次呼叫：話術 ─────────────────────────────────────────────────────
#
# 只在「已經決定要介入」之後才跑，一場會議 6-12 次（判斷是每 5 秒一次，約 170 次）。
# 所以這裡可以用比判斷更貴的設定——但不是無上限，見 UTTERANCE_EFFORT 的說明。

# 話術呼叫的 reasoning effort。實測（`experiments/two_call_utterance.py --sweep`，
# 8 個真實評分點 × 同一份 prompt，原始輸出在
# experiments/out/two-call-2026-08-29-two-person/sweep.json）：
#
#     effort    往返中位   最長      可溯源引號   中位字數
#     none       1.55s    1.78s      8/8         36
#     low        3.47s    5.49s      8/8         36
#     medium     4.30s   11.44s      8/8         37
#
# 選 none 的理由不是省錢，是這次呼叫卡在「決定要講」與「排進 Chair」之間，
# 延遲直接吃掉主席的反應時間：Chair 的軟插入等不到停頓時 ESCALATE_SECONDS=15s
# 就會升級成硬打斷，話術多花的每一秒都是從那 15 秒裡扣的，也讓 TOCTOU 重驗
# （見 live.slow_recheck_admissible）更容易判定世界已經變了。
# 三檔的引號率、可溯源率與字數分佈量不出差異，多付的 2-3 秒（medium 還會出現
# 11.4s 的尾巴）買不到更好的句子——所以取最快的一檔。
#
# 注意這裡比判斷呼叫（EFFORT="none"，實測往返 4.3s）還快：話術的 prompt 短、
# 輸出只有一句話，不像判斷要吐 pros/cons 四條理由＋三軸分數。所以「第二次呼叫
# 可以慢一點、貴一點」這個前提在實測上根本沒有被用到——它本來就比較便宜。
UTTERANCE_EFFORT = "none"

# 話術字數上限（中文字，含標點）。這個數字直接決定主席獨白多長：
# 語速常數 replay.CHARS_PER_SECOND = 4.5 字/秒，所以 50 字 ≈ 11.1 秒。
#
# 取法：現況話術中位 40 字（8.9 秒），v2「只講這場的事」中位 66 字（14.7 秒）、
# 最長 96 字（21.3 秒）。21 秒的主席獨白本身就是干擾，句子再好也是淨負值；
# 但拿掉上限不會讓句子變好（v1 已證），所以上限不是問題來源，該留。
# 50 是「放得下一段逐字引號＋一個具體要求」的最小值——引用一句話大約 10-15 字，
# 前後各留一句短的敘述與要求就是 45-50 字，再壓下去就只剩引號沒有要求了。
#
# 實測三檔（同一份 sweep，8 點）：模型會貼著上限往下寫，不會頂到上限——
#     上限 40 → 中位 30 字（6.7s）／最長 36 字（8.0s）
#     上限 50 → 中位 36 字（8.0s）／最長 46 字（10.2s）
#     上限 70 → 中位 47 字（10.4s）／最長 55 字（12.2s）
# 三檔的可溯源引號都是 8/8，品質量不出差別，所以這是純粹的長度取捨。
# 取 50：中位 8.0 秒已經比現況的 8.9 秒短，而且句子裡多了一段逐字引號；
# 取 40 會開始出現只剩引號、把「要他們做什麼」壓掉的句子，取 70 則讓中位
# 回到 10 秒以上，換不到更好的內容。
MAX_UTTERANCE_CHARS = 50

# 超過這個長度就整句作廢，不截斷。截斷會把句子切在半句上，主席當眾講半句比不講
# 更糟；退回罐頭句則等於這次拆呼叫白做（罐頭話正是這次要修的東西）。
# 1.4 倍是「模型偶爾超一點就放過、明顯不聽話就丟掉」的分界：50×1.4=70 字 ≈ 15.6 秒，
# 已經是 Chair 軟插入升級門檻（ESCALATE_SECONDS=15s）的長度，再長就不該播。
#
# 執行這條規則的是 `live.slow_recheck_admissible`，不是下面的 `phrase()`——
# 作廢要留下一個說得出口的理由（reason="話術過長"），事件檔與觀戰 UI 才分得出
# 「生不出話術」跟「話術太長被丟掉」。`phrase()` 若自己先吞掉超長的句子回空字串，
# 這兩件事在事件檔上就長得一模一樣，那條 reason 永遠不會出現。
UTTERANCE_HARD_CAP = int(MAX_UTTERANCE_CHARS * 1.4)

UTTERANCE_SYSTEM = """你是一位專業的會議主席，剛剛已經決定現在要開口。

判斷已經做完了，不是你的工作——你不需要、也不可以重新評估該不該介入。
你唯一的工作是把「要說的那句話」寫出來。

這句話會直接用語音播進會議裡，打斷正在進行的討論。所以它必須值得那個打斷：
聽的人要立刻知道你聽見了什麼、你要他們做什麼。"""

UTTERANCE_TEMPLATE = """## 會議資訊
議題：{topic}
預計時長：{duration} 分鐘，目前進行到第 {elapsed:.0f} 分鐘
參與者：{participants}
{phase_line}
## 最近的對話
{transcript}

## 你剛剛做出的判斷（已定案，不要推翻）
判定類型：{type}
該介入的理由：
{pros}
不該介入的理由（僅供你拿捏語氣，不是要你改變決定）：
{cons}

## 你的任務
寫出主席現在要說的那一句話。

硬性要求：
1. 只講這場會議剛剛真的出現的東西——某人講過的原話、兩個人各自實際主張了什麼、
   哪件事被當成前提卻從頭到尾沒講明。把這句話原封不動貼到任何一場別的會議也
   一樣成立的話，就不要說，重寫。
2. 至少引用一段上面逐字稿裡真的出現過的字句，用「」框起來。原文照抄，不可以
   改寫、不可以自己補上沒有人說過的話。
3. 全句不超過 {max_chars} 個字（含標點）。這句話會被唸出來，超過就變成主席的
   獨白，本身就是干擾。
4. 不要用「我們先回到」「先拉回」這種開場。那是罐頭話，聽的人得不到任何新資訊；
   直接講你聽到了什麼。
5. 不要編造人名、數字、時間。名字只能用上面參與者名單裡有的。

用以下 JSON 格式回覆，不要有其他內容：
{{"utterance": "<你要說的話>"}}"""

UTTERANCE_PHASE_LINE = """會議階段：{phase}（發散期：想法還在長，不要催收斂、不要下結論；
呻吟區：只維持秩序，不急著調解；收斂期：此時才可以推進、裁決、封板）"""


def build_utterance_prompt(st: MeetingState, now: float, r: dict,
                           phase: str | None = None) -> str:
    """組話術 prompt。餵的是同一批 `st.recent()` 逐字稿 ＋ 第一次呼叫的判斷結果。

    為什麼要把 pros／cons 帶進來：話術要接著「當初為什麼決定講」往下寫，
    否則第二次呼叫只看得到逐字稿，會自己再判斷一次要講什麼，等於白拆。
    """
    transcript = "\n".join(
        f"[{int(u.start) // 60:02d}:{int(u.start) % 60:02d}] {u.speaker}：{u.text}"
        for u in st.recent())
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs) or "-（無）"  # noqa: E731
    return UTTERANCE_TEMPLATE.format(
        topic=st.topic, duration=st.duration_min, elapsed=now / 60,
        participants="、".join(st.participants),
        phase_line=f"{UTTERANCE_PHASE_LINE.format(phase=phase)}\n" if phase else "",
        transcript=transcript,
        type=r.get("type", ""),
        pros=bullets(r.get("pros", [])), cons=bullets(r.get("cons", [])),
        max_chars=MAX_UTTERANCE_CHARS)


def phrase(st: MeetingState, now: float, r: dict, phase: str | None = None) -> str:
    """第二次呼叫：把已定案的判斷寫成主席要說的那句話。回傳模型寫的原文。

    這裡**不做任何長度裁決**——超過 `UTTERANCE_HARD_CAP` 的處置屬於閘門
    （`live.slow_recheck_admissible`），理由見該常數的說明。回傳空字串只代表
    「模型什麼都沒寫」。呼叫端據此放棄這次介入，**不退回罐頭句**：罐頭句正是
    這次拆呼叫要修掉的東西，退回去等於白做。網路／解析層的例外直接往上拋，
    由呼叫端決定怎麼記錄，這裡不吞。
    """
    body = {
        "model": MODEL,
        "reasoning_effort": UTTERANCE_EFFORT,
        "messages": [{"role": "system", "content": UTTERANCE_SYSTEM},
                     {"role": "user", "content": build_utterance_prompt(st, now, r, phase)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    text = json.loads(payload["choices"][0]["message"]["content"]).get("utterance") or ""
    return text.strip() if isinstance(text, str) else ""
