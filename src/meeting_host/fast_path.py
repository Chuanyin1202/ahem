"""快路：純規則、零延遲、不呼叫 LLM。

實測依據（validation-results.md #3、#3b）：所有測過的模型在「介入類型」判斷上
都不可靠——deadlock 場景六個模型全部誤判成「有人被冷落」。
因此類型由這裡的規則決定，LLM 只負責「要不要介入」的強度評分。

話術的不變量（T33）：**每一句話只能斷言規則真正知道的事**。
2026-08-31 真實會議實測踩到的就是這條——「有人被冷落」的觸發條件是
「這個人已經 N 秒沒發言」，話術卻寫死「從開會到現在還沒說話」，結果對一位
佔了全場 69% 發言時間的參與者講了一句全場都知道是假的話。判斷失準還可以
辯論，陳述事實錯誤當場就沒有信任可言。所以這個檔案裡（以及 phrasing.py
給 LLM 生變體的說明裡）的每一句話術都必須通過同一個檢查：
**把話拆成一個個斷言，每個斷言都要能從 Trigger 帶的事實直接推出來。**
推不出來的一律改寫成成立範圍更小、但一定為真的說法——寧可講得保守。
"""
import math
from dataclasses import dataclass

from .phrasing import PhraseBank, fill
from .state import MeetingState

# 門檻。之後由主持風格檔位覆寫（development-plan.md P1#8）
OVERTIME_SECONDS = 180.0      # 單人連續發言多久算超時
NEGLECTED_SECONDS = 300.0     # 多久沒發言算被冷落
AGENDA_WARN_SECONDS = 300.0   # 剩餘時間少於此值開始提醒（長會議的絕對上限，見 agenda_warn_seconds）
# 短會議用的比例門檻。1/6 不是新猜的數字，是把既有的 AGENDA_WARN_SECONDS
# 除以產品預設會議長度反推出來的：live.py 的 `--duration` argparse 預設是
# 30 分鐘，300 / 1800 = 1/6。換句話說「剩最後六分之一開始收尾」
# 一直是這個常數隱含的意思，只是原本被寫死成 30 分鐘那一場的絕對秒數。
AGENDA_WARN_RATIO = 1.0 / 6.0
COOLDOWN_SECONDS = 30.0       # 剛介入過的冷卻期，避免碎念
# 全場（所有在場參與者）多久沒人發言算冷場。
# T13：原本 45.0 的依據是猜的，2026-08-28 晚一場 10 分 39 秒的真實會議實測發現
# 太短——19 段發言間隔的分佈裡有明顯斷層：一段 128.6s 的真冷場之後，剩下最長的
# 都是 50 秒上下的正常思考停頓（52.4s/52.3s/50.7s），再來就直接掉到 35.1s、
# 17.4s，其餘 13 段都在 4 秒以下。45s 那晚在這批資料裡誤觸發了 4 次（3 次是
# 正常停頓），60s 會誤觸發 1 次，90s／150s 都只抓到那段真冷場——90s 取斷層區間
# 內較保守（較短）的一端，離「正常停頓」的上緣（52.4s）還有安全距離。
# ⚠️ 這是單一場、單一參與者的樣本，90.0 是有依據的起點，不是統計定論——
# 之後若有更多真實會議資料，應該重新核算這個數字，不要只憑直覺調整。
SILENCE_SECONDS = 90.0
# 「全場沉默」重複觸發的退避倍率：第 N 次（0-index）觸發後，下一次需要的
# 沉默時間變成 SILENCE_SECONDS * SILENCE_BACKOFF_FACTOR ** N——愈到後面提醒
# 間隔要愈稀疏，用等比而非等差，才不會讓門檻在提醒次數變多後反而長得太慢。
# 1.5 這個倍率讓第 2 次門檻約為第 1 次的 1.5 倍（135s）、第 3 次約 2.25 倍
# （202.5s）：退避得夠快，又不會第幾次之後就幾乎永遠不再提醒。
SILENCE_BACKOFF_FACTOR = 1.5

FAST_KINDS = {"發言超時", "有人被冷落", "議程超時", "全場沉默"}  # 快路規則型 kind，供 escalate 判斷「規則是否已不成立」

# 會議正在收尾時要壓住的規則（`check(..., closing=True)`）。
#
# 四條全在裡面不是「一律擋」的偷懶——逐條的理由都指向同一件事實：
# **每一條規則的話術都預設「會議還要繼續」**，收尾時那個前提已經不成立，
# 話說出口就是在要求別人做一件已經結束的事。
#
# - 發言超時「先讓其他人接一下」：收尾時沒有人想接話。而且這是四條裡唯一的
#   硬打斷（earcon＋插話），在別人講結語時打斷是最難看的失敗形態。
# - 有人被冷落「想聽聽你的看法」：把一個已經道別的人拉回一場
#   正在散場的會議。要在收尾點名，該講的是「散會前還有沒有補充」——那是另一句
#   話術，不是這一條；沒有那句話術之前，這條在收尾就只能閉嘴。
# - 議程超時「我們往結論收」：房間已經收完了。四條裡語意最接近收尾、傷害最小的
#   一條，但也因此最沒有價值——它只能叫大家去做剛剛已經做完的事，還會暴露主席
#   沒在聽。它整場只觸發一次，被壓下也不進 done（見下），窗口過了照樣能講。
# - 全場沉默「要不要有人先分享一下目前的想法？」：道別之後安靜下來是必然結果，
#   這條等於用「會議結束」當觸發條件。2026-08-29 那場實測尾段全場沉默爬到
#   87.7s／門檻 90.0s，差 2.3 秒。
#
# 壓下的代價有上限：這裡只過濾 check() 的回傳值，不寫 `done`、不動
# `room_silence_hits`，所以收尾判定一旦解除（誤判的情況下），每一條規則都還能
# 照常觸發——被擋掉的是「延後」，不是「永久取消」。
CLOSING_SUPPRESSED_KINDS = frozenset(FAST_KINDS)

# 主席失聰（STT 斷線／逐字稿停止更新）時要壓住的規則（`check(..., deaf=True)`）。
#
# 判準只有一條，逐條套用：**這條規則的前提是不是「逐字稿是新鮮的」**。
# 是的話，STT 一停它量到的就不是世界，是自己的故障；不是的話，它照常成立。
# （失聰的偵測與門檻依據見 `hearing.py`；2026-08-31 那場事故的實際症狀見同處。）
#
# 壓住（三條）：
# - 有人被冷落：`st.silent_seconds(p, now)` 直接從 `Utterance.end` 起算。逐字稿
#   一停，每個人的沉默秒數就一起往上爬，五分鐘後全員越過 NEGLECTED_SECONDS。
#   2026-08-31 那場的四次錯誤介入裡有三次是這一條（41:19 Jax、41:51 Alex、
#   42:22 Alex、42:53 Jax）。**必須壓住**。
# - 全場沉默：`min(silent_seconds)`，同一個來源。它問的正是「是不是沒有人在講
#   話」——而那恰好是失聰時最無法分辨的一件事。那場的 41:50 就是這一條。
#   **必須壓住**。
# - 發言超時：`current_run_seconds()` 只讀 `st.speaking`（STT partial）與
#   `st.utterances`（STT commit），兩個來源都是 STT。實務上 STT 斷線時
#   `SpeakingStopped` 會清掉 `speaking`，`utterances` 也不再更新，這條規則會
#   自己停止觸發——但那是別的機制順手擋掉的副作用，不是這條規則自己知道要
#   閉嘴。留著唯一的風險是殘留的 stale partial 讓它在失聰期間硬打斷（四條裡
#   唯一的 earcon＋插話，最難看的失敗形態），壓住的代價是零，所以壓住。
#
# 不壓（一條）：
# - 議程超時：`st.remaining_seconds(now) = duration_min * 60 - now`，純時鐘，
#   完全不碰逐字稿。主席聽不見，不代表議程沒有在走——「只剩 N 分鐘，我們往
#   結論收」在失聰時仍然是**真的**。跟著一起壓掉是把「不知道」擴大成「什麼都
#   不說」，反而讓現場更難分辨主席是壞了還是在忍。它整場只觸發一次
#   （`("議程超時", None)` 進 done 之後沒有任何地方 discard），也不會洗版。
#
# 跟收尾閘門一樣，這裡只過濾 `check()` 的回傳值：不寫 `done`、不動
# `room_silence_hits`，所以 STT 一活過來（`hearing.HearingMonitor.heard`）每一條
# 規則都能照常觸發——被擋掉的是「延後」，不是「永久取消」。
DEAF_SUPPRESSED_KINDS = frozenset({"發言超時", "有人被冷落", "全場沉默"})

# 「全場沉默」的話術輪替版本——語意相同、措辭不同。T13：同一場會議連續兩次
# 全場沉默逐字相同（六次一字不差）聽起來像跳針，比原本完全不管冷場更糟。
# 第一個版本必須維持原本的逐字文字：既有測試與線上使用者已經聽過這句，
# 不能無故換掉第一次見面的話術。
#
# T33：這條規則唯一知道的事實是「已經 N 秒沒有任何人發言」。它不知道大家
# 剛剛討論出了什麼、有沒有形成方向、甚至不知道整場有沒有人開過口——
# 全場沉默完全可能是從第一秒就沒人講話（沒人開麥、都在讀文件）。
# 因此第三、第四句原本的「目前的方向」「回顧一下剛剛講到哪」都被改掉：
# 那兩句預設了「已經討論過、而且已經有方向」，是規則推不出來的前提。
_ROOM_SILENCE_UTTERANCES = [
    "現場安靜了一陣子，要不要有人先分享一下目前的想法？",
    "大家都還在想嗎？有想法可以先丟出來，不用想到最完整再說。",
    "現在大家各自在想什麼？誰先開個頭都可以。",
    "看起來一時沒人要開口，那換個方式問：現在大家最想先聊什麼？",
]


def agenda_warn_seconds(duration_min: float) -> float:
    """這場會議「剩多少秒開始提醒收尾」——絕對上限與比例門檻取小的那個。

        min(AGENDA_WARN_SECONDS, duration_min * 60 * AGENDA_WARN_RATIO)

    為什麼要縮放：原本的 300 秒是絕對值，對 30 分鐘以上的會議合理，
    但宣告一場 5 分鐘的會議時 `remaining` 從第 0 秒起就 ≤ 300，規則從開場
    就成立——主席會在剛開始沒多久講一句「議程只剩 5.0 分鐘」。這條規則
    整場只觸發一次（`("議程超時", None)` 進了 done 之後，live.py 沒有任何
    地方 discard 它），所以不會洗版，但那一次介入額度就浪費在開場了，
    語氣也不對。黑客松 demo 會議只有幾分鐘，這正是會被看見的情境。

    為什麼取 min 而不是純比例：長會議的行為必須完全不變。60 分鐘會議
    比例算出來是 600 秒，取 min 之後仍是 300 秒——所有 ≥ 30 分鐘的會議
    門檻都被上限壓成 300，跟改動前逐字相同。只有短於 30 分鐘的會議才會
    真的走到比例那一側。

    邊界：
    - 5 分鐘 → 50 秒（剩最後 50 秒才提醒，開場不會觸發）
    - 2 分鐘 → 20 秒。刻意不設下限：任何下限都是新的猜測值，而且下限一旦
      大過會議長度的六分之一，短會議就會退回「開場就提醒」的原問題。
      20 秒仍大於快路的 tick（1 秒），規則有足夠的窗口被檢查到。
    - duration 沒設定或為 0 → 門檻 0。`check()` 的 `0 < remaining` 本來就
      要求剩餘為正，而 duration=0 時 `remaining_seconds()` 回 `-now` 恆 ≤ 0，
      兩邊都不成立，這條規則整場不會觸發（沒有議程長度就無從提醒收尾）。
    - remaining 為負（已超時）→ 不受這裡影響，仍由 `check()` 的 `0 < remaining`
      擋掉，語意與改動前一致。
    """
    return min(AGENDA_WARN_SECONDS, max(0.0, duration_min) * 60.0 * AGENDA_WARN_RATIO)


@dataclass
class Trigger:
    kind: str          # 介入類型
    target: str | None  # 對象
    detail: str        # 給主席發言用的具體事實
    hard: bool         # True=硬打斷（earcon+說話），False=等停頓
    variant: int = 0   # 第幾次觸發（0-index）。目前只有「全場沉默」用它選話術輪替版本


def check(st: MeetingState, now: float, done: set[tuple[str, str | None]] | None = None,
          closing: bool = False, deaf: bool = False) -> list[Trigger]:
    """回傳所有成立的快路觸發，已依優先序排序。

    done: 已經處理過的 (類型, 對象)。同一件事講過就不再講——
          「提醒某人沉默」之後除非他真的開口了，否則重複提醒只是碎念。
          呼叫端負責在狀態改變時把項目移出集合。
    closing: 這場會議是不是正在收尾。True 會壓掉 CLOSING_SUPPRESSED_KINDS
          裡的規則（目前四條全部，逐條理由見該常數）。
          ⚠️ 由呼叫端判斷並傳入，這裡不自己算——`meeting_is_closing` 在
          `live.py`，而 `live.py` import 這個模組，反向 import 會循環。
          預設 False 表示「呼叫端沒判斷」：真實會議的兩個呼叫端
          （`live.Session._fast_tick`、`live.escalate_with_current_facts`）
          都必須傳，離線評分工具維持舊行為。
    deaf: 主席的耳朵是不是壞了（STT 斷線或逐字稿停止更新）。True 會壓掉
          DEAF_SUPPRESSED_KINDS 裡的規則（三條，逐條理由見該常數；「議程超時」
          不在裡面，它只看時鐘）。跟 `closing` 完全同一個接縫形狀：由呼叫端
          （`live.Session._fast_tick`／`live.escalate_with_current_facts`）
          判斷並傳入，這裡不自己算，預設 False 讓離線工具維持舊行為。
    """
    if st.since_last_intervention(now) < COOLDOWN_SECONDS:
        return []
    done = done or set()

    triggers: list[Trigger] = []

    speaker, run = st.current_run_seconds(now)
    if speaker and run >= OVERTIME_SECONDS:
        triggers.append(Trigger(
            "發言超時", speaker,
            f"{speaker} 已連續發言 {run / 60:.1f} 分鐘", hard=True))

    remaining = st.remaining_seconds(now)
    if 0 < remaining <= agenda_warn_seconds(st.duration_min):
        triggers.append(Trigger(
            "議程超時", None,
            f"議程只剩 {remaining / 60:.1f} 分鐘", hard=False))

    for p in st.participants:
        if p in st.absent:
            continue  # 已離會：統計照算，但不能對著一個不在場的人喊話
        silent = st.silent_seconds(p, now)
        if silent >= NEGLECTED_SECONDS:
            triggers.append(Trigger(
                "有人被冷落", p,
                f"{p} 已 {silent / 60:.1f} 分鐘沒有發言", hard=False))

    # 全場沉默：跟「有人被冷落」問的是不同問題——那條看「某一個人」是不是被晾在一旁；
    # 這條看「整個房間」是不是都停了。取在場者 silent_seconds 的最小值：
    # 只要還有任何一位在門檻內講過話，最小值就會被壓低，不算全場沉默。
    present = [p for p in st.participants if p not in st.absent]
    if present:
        room_silence = min(st.silent_seconds(p, now) for p in present)
        # 退避：門檻隨這場會議已經觸發過幾次等比拉高（見 SILENCE_BACKOFF_FACTOR）
        threshold = SILENCE_SECONDS * SILENCE_BACKOFF_FACTOR ** st.room_silence_hits
        if room_silence >= threshold:
            triggers.append(Trigger(
                "全場沉默", None,
                f"全場已 {room_silence / 60:.1f} 分鐘沒有人發言", hard=False,
                variant=st.room_silence_hits))

    if closing:
        triggers = [t for t in triggers if t.kind not in CLOSING_SUPPRESSED_KINDS]
    if deaf:
        triggers = [t for t in triggers if t.kind not in DEAF_SUPPRESSED_KINDS]
    triggers = [t for t in triggers if (t.kind, t.target) not in done]
    # 優先序：硬打斷 > 軟插入
    triggers.sort(key=lambda t: (not t.hard, t.kind))
    return triggers


def utterance_for(t: Trigger, bank: PhraseBank | None = None) -> str:
    """快路的話術：規則型介入不需要 LLM，事實已在 Trigger 裡。

    T14：`bank` 有已驗證過、還沒用掉的句型就優先取用（`bank.take()` 是純
    記憶體操作，不含任何網路呼叫——見 phrasing.py），讓話術有真正的變化；
    `bank` 為 None、佇列已空、或填值失敗，都退回這裡原本的寫死模板。
    保底層永遠要在：LLM 還沒生成好、生成失敗、或根本沒開 LLM（--no-llm）
    時，主席都必須照常開口。
    """
    if t.kind == "發言超時":
        # 向下取整，不四捨五入：「你已經講了 N 分鐘」是一個下界宣告，
        # 說出口的數字必須是他確實已經講滿的分鐘數。四捨五入會在 run=3.6 分時
        # 講成「4 分鐘」——多算了 24 秒，是規則沒有的事實（T33）。
        # 少講不會出事（他確實講了至少 3 分鐘），多講會被當場糾正。
        mins = math.floor(float(t.detail.split("連續發言 ")[1].split(" 分鐘")[0]))
        pattern = bank.take(t.kind) if bank is not None else None
        if pattern is not None:
            filled = fill(pattern, target=t.target, mins=mins)
            if filled is not None:
                return filled
        return f"{t.target}，你已經講了{mins}分鐘，先讓其他人接一下。"
    if t.kind == "有人被冷落":
        pattern = bank.take(t.kind) if bank is not None else None
        if pattern is not None:
            filled = fill(pattern, target=t.target)
            if filled is not None:
                return filled
        # T33：只斷言「有一陣子沒說話」——這正是觸發條件
        # （`silent_seconds(p) >= NEGLECTED_SECONDS`）本身，兩種人都成立：
        # 整場沒開過口的人成立，剛剛才講了一大段、最近安靜下來的人也成立。
        #
        # 原句「從開會到現在還沒說話」斷言的是「整場零發言」，那是規則從來
        # 沒有檢查過的條件。2026-08-31 實測對一位佔全場 69% 發言時間的參與者
        # 講了這句，當場被戳破。
        #
        # 為什麼不拆成「從未發言」／「發言過但安靜很久」兩條分支（本來是選項）：
        # 1. 兩條分支得共用同一個 kind 的句型佇列（`PhraseBank` 以 kind 為鍵），
        #    LLM 生的變體無法只套用在其中一種情況上——要分就得多開一個
        #    phrase kind，那會讓 `live.watch_phrasing` 的開場補句從 5 次變 6 次
        #    （額度上限 `MAX_GENERATIONS_PER_MEETING = 10`），是話術以外的行為變動。
        # 2. 「從未發言」這個判斷的可靠度不等於「沉默 N 秒」。兩者都建立在
        #    STT 有沒有收到他的話上，但錯的形態不同：STT 漏掉一段，
        #    「有一陣子沒說話」只是講得保守，「從開會到現在還沒說話」卻是
        #    一句可以被全場當場否證的話——恰好是這張單要消滅的失敗形態。
        # 3. 收益有限：真正要達成的是「把發言權遞給他」，這句話已經做到了。
        #
        # 另外拿掉了「你對這個提案的看法是什麼」——規則不知道桌上有沒有提案，
        # 那是同一類（規則推不出來卻講得很篤定）的假前提。
        return f"{t.target}，你有一陣子沒說話了，想聽聽你的看法。"
    if t.kind == "議程超時":
        # 向上取整，不四捨五入：「只剩 N 分鐘」是一個上界宣告，剩餘時間一定
        # 不超過說出口的數字。四捨五入有兩個問題（T33）：剩 4.2 分講成
        # 「只剩 4 分鐘」會低報；剩不到 30 秒時 round() 直接算出 0，主席會講
        # 「只剩0分鐘」——那既不是事實（還有時間），也不是人話。
        # 短會議的門檻可以低到 20 秒（`agenda_warn_seconds(2)`），這條規則又
        # 可能被冷卻期／硬打斷擠到很後面才第一次成立，0 是真的講得出來的。
        mins = math.ceil(float(t.detail.split("只剩 ")[1].split(" 分鐘")[0]))
        pattern = bank.take(t.kind) if bank is not None else None
        if pattern is not None:
            filled = fill(pattern, mins=mins)
            if filled is not None:
                return filled
        return f"只剩{mins}分鐘，我們往結論收。"
    if t.kind == "全場沉默":
        pattern = bank.take(t.kind) if bank is not None else None
        if pattern is not None:
            return pattern
        return _ROOM_SILENCE_UTTERANCES[t.variant % len(_ROOM_SILENCE_UTTERANCES)]
    return t.detail
