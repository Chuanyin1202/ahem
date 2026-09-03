"""真實會議：Discord 語音 → STT → 快路／慢路 → 主席開口（同時寫終端）。

主席會出聲：決定介入後交給 Chair 用 TTS 播回語音頻道。每一次介入
「什麼時候、為什麼」同步攤在螢幕上並寫進 log，會後可以逐條檢視——
這是 evaluation.md 第 0 層盲標的素材。

用法:
    python -m meeting_host.live --topic "黑客松籌備" --duration 30
"""
import argparse
import asyncio
import dataclasses
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from . import fast_path, glossary
from .discord_source import MeetingBot
from .events import Event
from .hearing import HearingMonitor
from .phrasing import PHRASE_KINDS, PhraseBank, generate_patterns, greeting_text
from .speaker import ESCALATE_SECONDS, Chair, Earcon, Intervention, Voice
from .state import MeetingState
from .stt import STTPool

TICK = 5.0  # 慢路評分節奏
HELLO_POLL_SECONDS = 0.5  # --say-hello 等真人進場的輪詢間隔（測試可調小這個模組常數）
# T14：背景補句型的檢查間隔。不必密集——取用（take）完全不靠這個迴圈，
# 這裡只負責「佇列快空了再補一次」，慢一點也不影響任何介入時機。
PHRASING_POLL_SECONDS = 20.0
# --say-hello 等音訊路徑打通的逾時保險：真人加入頻道後這麼久都沒有偵測到他的音訊，
# 就直接問候，不再無限期等待（例如對方一進來就把麥克風靜音）。8 秒是實測 Discord
# 語音連線建立＋使用者反應的合理上限——比一般連線建立時間（多在 1–3 秒內）留了
# 充分餘裕，又不會讓開場乾等到讓人覺得主席沒反應。
HELLO_AUDIO_TIMEOUT_SECONDS = 8.0
# 提示卡背景任務的輪詢間隔。只是「有沒有累積夠一批」的檢查，真正的節奏由
# glossary.BATCH_MIN_UTTERANCES／BATCH_MAX_WAIT_SECONDS 決定。提示卡完全靜默、
# 不經過主席，所以延遲不重要——這裡刻意取得比 TICK 慢。
GLOSSARY_POLL_SECONDS = 10.0


def fmt(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


# ── 收尾偵測（慢路第三關）────────────────────────────────────────────────
# 只看一件事：最近這段時間裡，有沒有兩句以上出現「道別／掛斷通話」的用語。
#
# 為什麼要有這一關：T17 把「拉回議題」改成不分階段都可執行之後，模型在會議
# 自然結束的那幾分鐘仍然把道別當成「離題」，於是主席會在大家講完拜拜之後
# 補一句「我們是否要回到主題」（2026-08-29 雙人實測：t≈698s 與 t≈761s 兩次）。
# 模型手上本來就有完整逐字稿卻仍然這樣判，所以這裡用確定性規則擋，不再問模型。
#
# 詞表只收「講出來幾乎只可能是在道別或掛斷」的詞。刻意排除語意含糊的候選：
# 「離開」（離開這個話題）、「先這樣」（先這樣，下一題）、「掛了」（網站掛了）
# ——那些詞單獨出現不足以判定會議在收尾，收進來只會讓閘門在會議中段誤鎖。
CLOSING_MARKERS = (
    "拜拜", "掰掰", "再見", "bye",        # 道別
    "散會", "收工", "下次見",             # 結束會議
    "結束通話", "結束會議", "會議結束",     # 掛斷
    "下線", "離線",
)
CLOSING_LOOKBACK_SECONDS = 90.0  # 往回看多久
CLOSING_MIN_HITS = 2             # 要幾「句」命中才算收尾


def meeting_is_closing(st: MeetingState, now: float) -> bool:
    """會議是不是正在收尾——最近 CLOSING_LOOKBACK_SECONDS 秒內，
    有 CLOSING_MIN_HITS 句（含）以上出現 CLOSING_MARKERS 裡的詞。

    ⚠️ 這個判準的推導樣本只有一場會議：`experiments/holdout/2026-08-29-two-person`
    （雙人、14.5 分鐘、繁中口語、Discord 語音 STT）。詞表是從那場收尾段
    真的講出來的話挑的（實際出現：拜拜／再見／下線／結束通話），其餘是同一類
    的無歧義同義詞。它不是從語料統計來的，也沒有第二場資料佐證。

    設計上刻意保持「一句話講得清楚」：**只數最近的道別詞句數**。沒有其他條件
    ——特別是沒有綁「議程剩餘時間」，因為推導樣本這場宣告 30 分鐘卻在第 14.5
    分鐘就結束，任何「接近排定結束時間才算收尾」的條件在這場都不會成立。

    已知會失效的情況（不打算用特例規則補，補了就是往這場資料過擬合）：
    - 漏抓：用詞表以外的方式收尾（「那我們就到這邊」「OK 那今天先到這」），
      或收尾只講一句就結束——閘門不會啟動，慢路可能仍在收尾時開口。
    - 誤鎖：會議中段有人先行離開，其他人跟他道別兩句（「阿明拜拜」「拜拜」），
      接下來 90 秒慢路不會出聲。代價有上限——閘門靠時間窗自然過期，
      道別詞滑出 90 秒之後自動解除，不會鎖住整場。
    - 非中文／混語會議：詞表只涵蓋繁中口語與 "bye"。
    """
    cutoff = now - CLOSING_LOOKBACK_SECONDS
    hits = 0
    for u in st.utterances:
        if u.end < cutoff:
            continue
        text = u.text.lower()
        if any(m in text for m in CLOSING_MARKERS):
            hits += 1
            if hits >= CLOSING_MIN_HITS:
                return True
    return False


def meeting_is_closing_for_rules(st: MeetingState, now: float) -> bool:
    """快路（`fast_path.check`）用的收尾判定：把回看窗的錨點凍結在
    **最後有人講話的那一刻**，而不是 `now`。

    為什麼快路不能直接用 `meeting_is_closing(st, now)`——那樣對「全場沉默」
    這條規則是結構性無效的，不是保守一點而已：

        設最後一句話結束於 T_last、倒數第二句道別結束於 T_2（T_2 ≤ T_last）。
        全場沉默在 now = T_last + SILENCE_SECONDS 成立；
        以 now 為錨的收尾判定要成立則需要 now ≤ T_2 + CLOSING_LOOKBACK_SECONDS。
        兩個常數都是 90.0，於是條件化簡成 T_last ≤ T_2——只有「最後兩句都是
        道別、而且結束時間完全相同」才成立，實務上等於永不成立。

    大家講完拜拜之後就沒有人再說話，於是「道別詞變舊」與「沉默變長」是同一根
    時間軸在走，兩個 90 秒窗互相抵消。實測驗證（2026-08-29 雙人會議事件檔）：
    以 now 為錨的閘門在 t=836 就失效，全場沉默在 t=869 觸發，完全沒擋到。

    凍結錨點之後判準變成一句話：**沒有人講話，並不會讓一場正在收尾的會議
    變回進行中**。房間重新開口才會推進錨點；等新的談話把道別詞推出 90 秒窗，
    閘門自動解除，誤鎖的代價上限跟 `meeting_is_closing` 完全一樣。

    慢路為什麼不需要這一版：`slow_path.should_score` 要求「有新的 utterance
    才評分」，慢路只會在剛有人講完話的當下被呼叫，此時 `now` 本來就等於錨點，
    兩個函式取值相同。改用凍結版對慢路不會有任何差別，所以不動它——
    收尾閘門在慢路那一關的行為保持逐字不變。
    """
    ends = [u.end for u in st.utterances]
    if not ends:
        return False
    return meeting_is_closing(st, min(now, max(ends)))


# 「決定要講之後才發現不能講」的三個理由（T29）。跟第一關的「冷卻」「收尾」
# 用不同字串，是為了在事件檔與觀戰 UI 上分得出兩件不一樣的事：
#   - 第一關擋下 ＝ 主席看完局面決定不開口（忍住）
#   - 第二關擋下 ＝ 主席已經決定要開口，是後來的世界或話術生成把它擋掉了（受阻）
# 兩者混用會讓觀戰 UI 把「話術生成失敗」顯示成「主席選擇忍住」，那是假的。
SLOW_BLOCKED_AFTER_DECISION = ("話術失敗", "話術過長", "冷卻(話術後)", "收尾(話術後)",
                                "失聰(話術後)")


def slow_gate(st: MeetingState, now: float, r: dict, deaf: bool = False) -> tuple[bool, str]:
    """第一關：判斷結果本身要不要進到「產話術」這一步。回傳 (可送, 原因)。

    四關（T29 之前前三關跟話術檢查綁在同一支 `slow_result_admissible`，
    拆呼叫之後話術在這個時間點還不存在，所以必須分開）：
    - type=無：即使三軸分數過了門檻，也不算數（見 slow_path.is_intervention）
    - 收尾中：會議已經在道別了，這時候「拉回議題」是最難看的介入（見 meeting_is_closing）
    - 失聰中：主席聽不見了（見 hearing.py）
    - 冷卻期：LLM 跑了幾秒，這段期間快路可能已經開口——Chair.request() 本身不檢查冷卻，
      這裡不擋就會在 30 秒內連講兩次（TOCTOU：should_score 的 busy 只擋「評分前」忙，
      擋不到「評分中」快路才出聲的情況）

    收尾放在冷卻之前，是為了讓 log／事件檔的 reason 指出真正的原因——收尾段常常
    同時落在快路剛出聲的冷卻期內，先判冷卻的話這個系統性問題會被偶然的冷卻遮住。
    失聰緊接在收尾後面、同樣排在冷卻之前，理由一樣。

    ⚠️ **慢路本身在失聰期間結構上就不會產生新判斷**：`slow_path.should_score` 要求
    `len(st.utterances) != last_n`，逐字稿停了就不再評分，一次都不會。所以這一關
    真正擋到的只有一種情況——`score()` 是在 STT 還活著時發動的，那幾秒往返之間
    STT 才死掉（in-flight）。窗口很窄，但這正是 `slow_recheck_admissible` docstring
    講的那個 TOCTOU 缺口，不是可以省的一關。

    這一關擋下來就**不會**打第二次呼叫——話術呼叫的成本與延遲只花在真的要開口的
    那 6-12 次上，這是拆兩次呼叫的前提（見 slow_path 模組 docstring）。

    `deaf` 預設 False：跟 `fast_path.check` 的 `closing`／`deaf` 同一個約定——
    呼叫端（`Session._run_slow_score`）負責判斷並傳入，離線工具
    （`experiments/rescore_slow_path.py`）不傳就維持舊行為。
    """
    from .slow_path import is_intervention
    if not is_intervention(r):
        if r.get("type") in ("無", "", None) and r.get("verdict") != "不介入":
            return False, "type=無"
        return False, ""
    if meeting_is_closing(st, now):
        return False, "收尾"
    if deaf:
        return False, "失聰"
    if st.since_last_intervention(now) < fast_path.COOLDOWN_SECONDS:
        return False, "冷卻"
    return True, ""


def slow_recheck_admissible(st: MeetingState, now: float, r: dict,
                             deaf: bool = False) -> tuple[bool, str]:
    """第二關：話術回來之後的重驗。回傳 (可送, 原因)。

    為什麼一定要重驗（TOCTOU）：第二次呼叫要花幾秒，這幾秒世界會變——快路可能
    先開口了（冷卻期重新開始）、會議可能已經進入收尾段。第一關通過只代表
    「幾秒前可以講」，`Chair.request()` 又不檢查冷卻與收尾，這裡不重驗就會在
    30 秒內連講兩次、或在道別聲中插一句「拉回議題」。這正是既有 `slow_gate`
    docstring 描述的那個缺口，只是拆呼叫之後窗口從「一次呼叫」變成「兩次」，
    暴露時間更長，所以更要驗，不是可以省。

    **不重驗的三件事，各自有理由：**
    - `type=無`／三軸分數：那是第一次呼叫的判斷，重驗要再打一次 LLM，等於無限遞迴。
    - 「有沒有新的發言」：話術引用的是逐字稿裡真的說過的話，那些話不會因為又有人
      開口就變成沒說過。新發言只會讓話術稍微不是最新的，不會讓它變成假的。
    - Chair 忙不忙：`Chair.request()` 自己就會擋（忙碌／退避／已有 pending 回 False），
      觀戰 UI 也已經把「排不進佇列」算成受阻——這裡再檢查一次只是重複，且會有
      自己的 TOCTOU。

    話術檢查放在最前面：生不出話術是這次呼叫自己的結果，跟世界變不變無關，
    先判它才不會被偶然的冷卻遮住（跟 slow_gate 把收尾排在冷卻前是同一個理由）。
    """
    from .slow_path import UTTERANCE_HARD_CAP
    text = (r.get("utterance") or "").strip()
    if not text:
        return False, "話術失敗"
    if len(text) > UTTERANCE_HARD_CAP:
        return False, "話術過長"
    if meeting_is_closing(st, now):
        return False, "收尾(話術後)"
    # 話術那幾秒之間 STT 才死掉——跟收尾同一類「世界變了」，順序也跟第一關一致
    if deaf:
        return False, "失聰(話術後)"
    if st.since_last_intervention(now) < fast_path.COOLDOWN_SECONDS:
        return False, "冷卻(話術後)"
    return True, ""


def slow_result_admissible(st: MeetingState, now: float, r: dict) -> tuple[bool, str]:
    """單次呼叫時代的完整閘門（`slow_gate` ＋「判定介入但沒給話術」）。

    ⚠️ T29 拆成兩次呼叫之後，**production 已經不走這一支**——`_run_slow_score`
    改成先 `slow_gate()`、打話術呼叫、再 `slow_recheck_admissible()`。這裡原樣
    保留，是因為它是「r 裡同時有三軸分數與話術」這個形狀的唯一閘門定義，
    離線工具（`experiments/rescore_slow_path.py::recompute_gates`）與既有回歸
    測試都建立在這個形狀上。

    ⚠️ `slow_path.score()` 從 T29 起**不再回傳 `utterance`**。把那種形狀的 dict
    餵進來曾經會靜默得到 `(False, "無話術")`，離線重評因此假性歸零（`emit_t` 上
    34 點的 admissible 5 → 0，連「冷卻」「收尾」的歸因也被覆寫成「無話術」）。
    所以這裡改成**缺鍵直接拋 KeyError**：`utterance` 不存在＝餵錯形狀，
    `utterance` 是空字串＝模型沒寫出來，兩者是不同的事，不可以合併成同一個結果。
    要重跑離線重評，連話術呼叫一起跑（`experiments/rescore_slow_path.py` 已改成
    鏡射 production 的兩次呼叫）。

    順序沿用舊契約：type=無 → 無話術 → 收尾 → 冷卻。
    """
    if "utterance" not in r:
        raise KeyError(
            "slow_result_admissible() 收到不含 utterance 鍵的判斷結果——這是單次"
            "呼叫時代的閘門，只吃『三軸分數與話術在同一個 dict』的形狀。"
            "拆呼叫之後請改用 slow_gate() ＋ slow_recheck_admissible()。")
    ok, reason = slow_gate(st, now, r)
    if not ok and reason in ("type=無", ""):
        return ok, reason
    if not r.get("utterance"):
        return False, "無話術"
    return ok, reason


def channel_has_human(st: MeetingState) -> bool:
    """頻道內目前是否至少有一位「在場」的真人（在 participants 且不在 absent）。

    真人的認定完全交給 MeetingState：discord_source.state_sync() 在 bot 進頻道時
    同步過名單，on_voice_state_update() 之後每次進出都會更新 participants／absent，
    這裡不用連 Discord 就能判斷，方便單元測試。
    """
    return any(p not in st.absent for p in st.participants)


class HelloGate:
    """--say-hello 的問候時機：頻道是空的就先不問候；等第一個真人出現後，
    還要再等到「確認他的音訊路徑已經通」才問候，且整場只問候一次。

    T8 只做到「有真人才問候」，但真人剛加入語音頻道時 Discord 客戶端往往還在
    建立語音連線，這時候問候，使用者只會聽到半句（真實回報）。所以這裡多一層
    `note_audio()`：呼叫端在偵測到該真人的音訊真的進來時呼叫一次，之後
    `should_greet` 才會放行。加了逾時保險（見 `HELLO_AUDIO_TIMEOUT_SECONDS`）：
    真人一直沒有音訊（例如進來就靜音）不能無限期等下去。

    純狀態機，不碰 Chair／Discord——單元測試只需要灌布林值／時間戳進來。
    """

    def __init__(self) -> None:
        self.greeted = False
        self._audio = False  # 是否已確認收過在場真人的音訊——一旦 True 就不會再變回 False
        self._human_since: float | None = None  # 第一次偵測到真人在場的時刻（now 座標）

    def note_audio(self) -> None:
        """收到在場真人的音訊訊號——由呼叫端在偵測到音訊時呼叫一次；冪等，可重複呼叫。"""
        self._audio = True

    def should_greet(self, has_human: bool, now: float) -> bool:
        """回傳這次是否該問候；一旦回傳過一次 True，之後永遠回傳 False。

        真人不在場：重置計時（避免「短暫進來又離開」被誤判成已經等了很久），不問候。
        真人在場：音訊已確認 → 問候；音訊還沒來 → 等到 HELLO_AUDIO_TIMEOUT_SECONDS
        逾時才問候，逾時之前持續回 False。
        """
        if self.greeted:
            return False
        if not has_human:
            self._human_since = None
            return False
        if self._human_since is None:
            self._human_since = now
        waited = now - self._human_since
        if not self._audio and waited < HELLO_AUDIO_TIMEOUT_SECONDS:
            return False
        self.greeted = True
        return True


def build_hello_gate(say_hello: bool) -> HelloGate | None:
    """--say-hello 沒開就回 None——main_async 兩處問候路徑（start_chair 的立即檢查、
    watch_hello 的輪詢）都靠這個 None 短路，天生保證沒開旗標時任何情況都不問候。
    抽成函式方便單元測試，不用真的組一份 argparse 的 args。
    """
    return HelloGate() if say_hello else None


def escalate_with_current_facts(st: MeetingState, now: float, revision: int,
                                 iv: Intervention, bank: PhraseBank | None = None,
                                 deaf: bool = False) -> Intervention | None:
    """軟插入等超過 15s 升級時，用當下事實重生文字，不能沿用 15 秒前的舊句。

    快路 kind：規則現在還成立就用最新事實重生話術；規則已不成立就作廢。
    慢路 kind：規則不適用，沿用原本的話術，只是把它升級成硬打斷。

    `bank` 沿用跟第一次觸發相同的句型庫（T14）——重生文字時一樣可能取到
    生成過的變體，不必因為是升級路徑就退回寫死模板。

    收尾閘門在這裡也要帶上：軟插入在等停頓的那 15 秒之間房間開始道別，是很常見
    的時序（`ESCALATE_SECONDS` 15s 遠短於收尾段長度）。不帶的話，check() 會照樣
    回報規則成立，於是一句在會議還沒收尾時排入的軟插入，會在大家講完拜拜之後
    被升級成硬打斷——比第一次觸發還糟。帶上之後 check() 回空集合，快路 kind
    走下面「規則已不成立 → 作廢」那條路被丟掉，正是想要的結果。
    慢路 kind 不受影響（它們不在 FAST_KINDS 裡，走最後一行沿用原話術）。

    失聰閘門同理，而且更該帶：軟插入排入時 STT 還活著，等停頓的那 15 秒裡
    STT 死掉是完全可能的時序（2026-08-31 那場的斷線就發生在會議中段）。不帶的話
    `check()` 會拿失聰期間一路灌水的 `silent_seconds` 回報規則仍成立，於是把一句
    已經失去依據的軟插入**升級成硬打斷**——比第一次觸發還糟。帶上之後三條被壓住
    的快路 kind 走下面「規則已不成立 → 作廢」那條路被丟掉，正是想要的結果；
    「議程超時」不在壓制名單裡，失聰時照樣可以升級（它只看時鐘，前提仍然成立）。
    """
    closing = meeting_is_closing_for_rules(st, now)
    # 不帶 done，看規則現在還成不成立
    for t in fast_path.check(st, now, set(), closing=closing, deaf=deaf):
        if (t.kind, t.target) == (iv.kind, iv.target):
            return dataclasses.replace(iv, text=fast_path.utterance_for(t, bank), hard=True,
                                        revision=revision, created_at=now)
    if iv.kind in fast_path.FAST_KINDS:  # 規則已不成立 → 作廢
        return None
    return dataclasses.replace(iv, hard=True, revision=revision)  # 慢路：沿用話術


# Chair.tick() 判定「revision 過期」時用的兩個作廢理由字串（見 speaker.py 該方法）——
# 這裡只認這兩個，必須跟 speaker.py 的常值保持一致。
_REVISION_STALE_REASONS = ("revision 過期", "升級重生的介入 revision 已過期")


def resurrect_room_level(iv: Intervention, reason: str, now: float, revision: int
                          ) -> Intervention | None:
    """target=None（room-level）的介入被 Chair 判定 revision 過期時，把它救回來重排。

    T21：Chair 的 revision 機制是通用的——呼叫端說世界變了，Chair 就把手上的
    候選丟掉，這條規則本身沒有錯。但「世界變了」對不同 kind 的意義不一樣：
    `Session.note_speaker` 只在換人講話時遞增 revision（見該方法 docstring），
    這對「發言超時」「有人被冷落」這種綁著特定對象的規則是對的——換人講話
    代表那個人不再連續佔用發言權，或有別人先開口，規則的前提本身就不成立了。
    但「離題」「僵局」「議程超時」「全場沉默」問的是房間整體的狀態，跟誰在
    講話無關——換人講話（尤其兩人快速交替時幾乎每句都換）不代表話題自己
    回到正軌，卻會讓這些介入在真正開口前就被連續作廢，永遠等不到它要的那個
    停頓。（實測：18 分鐘雙人會議 11 次排入，6 次 target=None 全數被
    「revision 過期」丟掉，僅有的 5 次成功全部發生在全場沉默、沒有人換人講話
    期間；快速交替期間的離題介入 100% 被丟。）

    只重生這兩種明確是「revision 過期」的作廢理由——「被硬打斷取代」
    「播放器逾時，候選作廢」「升級時已不成立」都是呼叫端／Chair 自己判定
    這件事真的該收掉（分別是主動取代、播放器已死、規則重驗後確認不成立），
    不能救，救了就是蓋掉一個有意義的決定。

    `iv.created_at` 跨重生不變（`dataclasses.replace` 不動沒指定的欄位）——
    用它幫重生設存活上限，沿用 Chair 既有的 `ESCALATE_SECONDS`（軟插入等不到
    停頓就升級硬打斷的同一個門檻，不另外發明新數字）：換人換不停、真的等不到
    一次停頓的話，超過這個年紀**升級成硬打斷**，不是放棄。

    2026-09-03 三人真實會議實測發現：原本這裡超過年齡上限就 `return None`，
    交給 Chair 照它原本的行為真的作廢——但 Chair 的作廢路徑只在 revision
    不符時觸發，走到那條路根本沒機會經過 `Chair.tick()` 自己的
    `waited >= ESCALATE_SECONDS` 硬打斷判斷（那個判斷比較的是 `_pending_since`，
    每次重生都被 `request()` 重設成 `now`，在三人以上快速交替時永遠來不及
    累積滿）。結果是：換人換不停的 room-level 介入，明明是「Chair 該做卻做
    不到」的情境，反而永遠等不到 Chair 那條硬打斷路徑，只會靜靜作廢——
    那場會議 11 分鐘起兩次「離題」判定，各自被重生 3～4 次後放棄，全場只有
    開場問候一句話。

    這裡（`Session.on_dropped` 呼叫端）跟 `Chair.tick()` 是兩套時鐘座標：
    這裡用 `Session.now`（相對會議起點），`Chair.tick()` 用裸 `perf_counter`
    （見 `Chair` docstring 的座標警告）——`iv.created_at` 是前者，不能拿去跟
    `Chair.tick()` 的 `now` 比。升級決定必須留在這個座標系裡做，不能想著
    「反正 Chair.tick() 也有一個 ESCALATE_SECONDS 判斷，把值傳過去就好」。
    """
    if iv.target is not None:
        return None
    if reason not in _REVISION_STALE_REASONS:
        return None
    if now - iv.created_at >= ESCALATE_SECONDS:
        # 等不到一次停頓插進去，就不再客氣地排隊——直接升級成硬打斷重新排入，
        # 沿用原本判斷出的話術，只是不再等安靜。
        return dataclasses.replace(iv, revision=revision, hard=True)
    return dataclasses.replace(iv, revision=revision)


class Session:
    def __init__(self, st: MeetingState, phase: str = "發散期",
                 cancel: Callable[[], object] | None = None,
                 phrase_bank: PhraseBank | None = None,
                 auto_phase: str | None = None):
        self.st = st
        self.phase = phase
        # 階段自動判斷：None＝關（預設，行為與從前完全相同）、"suggest"＝只建議、
        # "apply"＝自動套用。偵測器與慢路近期 type 的緩衝見 phase.py。
        self.auto_phase = auto_phase
        self._recent_slow_types: list[str] = []
        # 觀戰 UI 的「結束會議」開關（POST /end）走的取消動作。main_async 注入
        # main_task.cancel，讓 UI 結束與 kill -TERM 落到完全同一條 shutdown()。
        self._cancel = cancel
        # T14：快路話術／問候的句型庫。不傳就給一個沒有生成器的空庫——
        # take() 永遠回 None，等同這個功能完全不存在，既有測試與 --no-llm
        # 因此不必知道這個參數就能維持原行為（驗收 12）。
        self.phrase_bank = phrase_bank if phrase_bank is not None else PhraseBank()
        # 收尾是否已經啟動。request_end() 與 shutdown() 都會設；設了之後
        # request_end() 不再送第二次 cancel（見該方法的 docstring）。
        self.ending = False
        self.t0 = time.perf_counter()
        # perf_counter 沒有掛鐘對應，t=0（now=0）那一刻的真實時間另外記一次 wall
        # clock，只給觀戰 UI 用（把逐字稿的相對秒換算成真實時鐘時間，見 emit_meeting）。
        self.wall_start = time.time()
        self.done: set[tuple[str, str | None]] = set()
        self.log: list[str] = []
        self.chair: Chair | None = None  # bot 進頻道後由 start_chair 填入
        self.revision = 0  # 世界版本；發言者換人就變，Chair 用來作廢過期候選
        self._last_speaker: str | None = None
        self.events: list[Event] = []
        self.subscribers: list[Callable[[Event], None]] = []
        self._last_participant_count = 0  # 供 consume() 偵測名單變動、重送 meeting
        # 失聯偵測（見 hearing.py）。`stt_pool` 由 main_async 接線；回放模式與
        # 單元測試不接，那時只剩「有人出聲但沒有逐字稿」那條臂（也可以由測試
        # 直接呼叫 hearing.note_stt_offline 灌值）。
        self.hearing = HearingMonitor()
        self.stt_pool: "STTPool | None" = None
        self._deaf_reason = ""  # 上一次判定的理由，用來只在狀態**變化**時 emit

    def request_end(self) -> bool:
        """觀戰 UI 的 POST /end：按下與 SIGTERM 同一個開關，不新增第二條收尾路徑。

        回 False＝沒接線（回放模式、單元測試），spectator 據此回 409。

        **防重入**：收尾一旦啟動就不再送第二次 cancel。第二次 cancel 會在
        `shutdown()` 正在 await 的地方（`_flush_spectator()` 最長 3 秒、
        `gather(*tasks)`）重新拋 CancelledError，把收尾整段掀掉——那正是
        `bot.close()` 與 events.jsonl 該做完的時候。`ending` 由 `shutdown()`
        入口也設一次，所以「SIGTERM 之後再按 /end」同樣擋得住（那條路徑不經過
        這個方法，只靠這裡自己的旗標會漏）。
        """
        if self._cancel is None:
            return False
        if self.ending:
            return True  # 收尾已經在跑：回報受理，但不再打斷它
        self.ending = True
        self._cancel()
        return True

    @property
    def now(self) -> float:
        return time.perf_counter() - self.t0

    def _log(self, line: str) -> None:
        print(line)
        self.log.append(line)

    def emit(self, kind: str, data: dict) -> None:
        """append 一個結構化事件並同步通知所有 subscriber（同步 callback，例外不得傳播）。"""
        event = Event(kind, self.now, data)
        self.events.append(event)
        for sub in self.subscribers:
            try:
                sub(event)
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠️ 事件訂閱者例外（{kind}）：{type(e).__name__}: {e}")

    def emit_meeting(self) -> None:
        self.emit("meeting", {
            "topic": self.st.topic, "duration_min": self.st.duration_min,
            "phase": self.phase, "participants": list(self.st.participants),
            # T16：會議開始（t=0）當下的 unix epoch 秒。觀戰 UI 用它把逐字稿／
            # 主席判斷每一列的相對秒換算成真實時鐘時間（固定 UTC+8）。
            "start_epoch": self.wall_start,
        })

    def emit_share(self) -> None:
        """發言時間分佈，含主席。主席沒有真正的「發言時長」——用介入次數 × 平均 3 秒估算（估值）。

        參與者與主席必須共用同一個分母（參與者發言秒數總和＋主席估算秒數），
        否則兩邊各自除以不同的分母，全部加總不會等於 1.0（P4：曾經算出 109%）。
        不能沿用 `state.share()`——它的分母不含主席，是給 summary() 等別的呼叫端用的。
        """
        chair_seconds = len(self.st.interventions) * 3.0
        participant_seconds = {p: self.st.spoke_seconds(p) for p in self.st.participants}
        total = sum(participant_seconds.values()) + chair_seconds
        data = {p: (s / total if total else 0.0) for p, s in participant_seconds.items()}
        data["主席"] = chair_seconds / total if total else 0.0
        self.emit("share", data)

    # ── 失聯偵測（見 hearing.py）────────────────────────────────────────
    def note_voice(self, speaker: str, active: bool) -> None:
        """RTP 層的「麥克風開始／停止送封包」——`bot.on_voice_activity` 的接線點。

        兩件事一起做，而不是各接一條回呼：`emit("voice", …)`（既有行為，事件
        契約不變）與餵給 `HearingMonitor`。綁在同一個方法裡是刻意的——失聰偵測
        唯一的參照訊號就是這條，分成兩條接線很容易之後只改到其中一條。
        """
        self.hearing.voice(speaker, active, self.now)
        self.emit("voice", {"speaker": speaker, "active": active})

    def deaf_reason(self) -> str:
        """主席現在是不是聽不見；聽得見回空字串。理由字串見 `hearing.REASON_*`。

        每次呼叫都重新判定：STT 連線狀態要即時從池子拿（`STTPool.offline()`），
        「出聲但沒有逐字稿」的累積量則本來就隨時間長。
        """
        if self.stt_pool is not None:
            self.hearing.note_stt_offline(self.stt_pool.offline())
        return self.hearing.reason(self.now)

    def _note_hearing(self, reason: str) -> None:
        """失聰狀態**變化**時 emit 一筆 `hearing` 事件並寫 log。

        只在邊緣 emit，不是每秒一筆：`fast_timer` 已經是每秒的心跳，失聰是
        少數幾次的狀態切換，每秒重送只會把事件檔灌爆。新連上的觀戰 UI 不會
        漏掉——`spectator._events_handler` 一律先送全量 snapshot。
        """
        if reason == self._deaf_reason:
            return
        self._deaf_reason = reason
        self.emit("hearing", {"ok": not reason, "reason": reason,
                              "voiced_seconds": round(self.hearing.voiced_seconds(self.now), 1)})
        if reason:
            self._log(f"    🔇 主席聽不到了（{reason}）——停止依賴逐字稿新鮮度的介入")
        else:
            self._log("    🔈 主席重新聽得到了——規則恢復")

    def _sync_participants(self) -> None:
        n = len(self.st.participants)
        if n != self._last_participant_count:
            self._last_participant_count = n
            self.emit_meeting()

    def release_claim(self, iv: Intervention) -> None:
        """介入沒能出聲（TTS 失敗、被取代、作廢）→ 解除 claim，同一個觸發之後才能重試。

        節流由 Chair 的 30 秒退避負責；claim 只用來防「每秒重送」，
        不該在失敗後把那件事永久壓掉。
        """
        self.done.discard((iv.kind, iv.target))

    def on_dropped(self, iv: Intervention, reason: str) -> None:
        """Chair 判定介入作廢時的回呼（T21）：預設 release claim＋記錄，但先讓
        `resurrect_room_level` 判斷這是不是「room-level 介入被換人講話誤傷」——
        是的話重新排入而不是真的放棄（見該函式 docstring）。

        真的放棄（沒被重生）才 release claim／emit「dropped」——重生的話這件事
        還沒結束，claim 不能解除，也不該讓觀戰 UI 顯示「已作廢」。

        `chair.request()` 不保證成功——同 kind 的硬打斷若剛因 TTS 失敗退避過
        （`speaker.py` 的 `FAIL_BACKOFF`／`_backoff_until`），這裡想重生也會被
        擋下回 False。這種情況不能沿用原本的 reason 字串蒙混過去：那句話
        其實根本沒被排入，claim 沒解除、也沒 emit「dropped」的話，這個介入
        會卡在「評估中」再也不會有下文，log 也會謊報「重生」成功。
        """
        revived = resurrect_room_level(iv, reason, self.now, self.revision)
        if revived is not None and self.chair is not None and self.chair.request(revived):
            self._log(f"    (主席重生【{iv.kind}】{reason}——房間整體的介入不因換人作廢)")
            return
        if revived is not None:
            reason = "重生時仍在退避，無法排入"
        self.release_claim(iv)
        self.emit("dropped", {"kind": iv.kind, "target": iv.target, "text": iv.text, "reason": reason})
        self._log(f"    (主席作廢【{iv.kind}】{reason})")

    def note_speaker(self, speaker: str) -> str | None:
        """記下這句話的發言者，回傳「上一位發言者」（第一句時為 None）。

        世界版本只在發言者換人時改變——每句都改的話，軟插入等到的那個停頓
        本身就會帶來一次 commit，介入永遠來不及開口就被作廢。
        """
        previous = self._last_speaker
        if speaker != self._last_speaker:
            self.revision += 1
            self._last_speaker = speaker
        return previous

    def note_target_spoke(self, speaker: str) -> bool:
        """被點名的對象自己開口了 → 世界變了，還沒出口的候選要作廢。

        「換人才遞增」擋不住這一種：A 是最後發言者、沉默五分鐘排了「有人被冷落：A」，
        A 自己開口後 _last_speaker 仍是 A，revision 不變，主席會問一個剛講完話的人
        「你對這個提案的看法是什麼？」
        """
        if self.chair is None:
            return False
        for iv in (self.chair.pending, self.chair.candidate):
            if iv is not None and iv.target == speaker:
                self.revision += 1
                return True
        return False

    def maybe_greet_hello(self, gate: HelloGate) -> None:
        """say-hello 問候：頻道內出現真人時經 gate 判斷觸發，整場只會真的送出一次。

        本身冪等——呼叫端（Chair 剛建好時、之後的輪詢）可以放心重複呼叫，
        不會重複問候；`gate` 一旦問候過就讓 watch_hello 的迴圈自然收工。
        """
        if self.chair is None:
            return
        if gate.should_greet(channel_has_human(self.st), self.now):
            # greeting_text 是純記憶體取用＋填值：句型還沒生成好就退回這句寫死的，
            # 絕不會因為等 LLM 而延後問候的送出時機（驗收 9）。
            text = greeting_text(self.phrase_bank, self.st.topic)
            self.chair.request(Intervention("問候", None, text, True, self.revision, self.now))

    def note_human_audio(self, gate: HelloGate) -> None:
        """在場真人的音訊訊號到了——記下來並立刻重新判斷一次是否該問候。

        不必等下一次 watch_hello 輪詢：MeetingBot._on_audio 收到第一個真人音訊封包
        時（經 call_soon_threadsafe 轉進 event loop）直接呼叫這裡，問候可以在
        音訊真的通了的那一刻立刻送出，而不是被 HELLO_POLL_SECONDS 的輪詢間隔拖慢。
        """
        gate.note_audio()
        self.maybe_greet_hello(gate)

    async def watch_hello(self, gate: HelloGate) -> None:
        """say-hello：頻道還空著就先不問候，輪詢等第一個真人出現、且音訊路徑確認打通
        （或逾時）才問候一次。

        獨立成一條輕量迴圈而非塞進 watch_fast——問候時機跟快路的規則判定無關，
        混在一起會讓 fast_path 的職責變模糊。頻道進 Chair 時已經有真人的情況，
        main_async 會在建好 Chair 當下先呼叫過一次 maybe_greet_hello，
        這裡一啟動就發現 gate.greeted 已是 True，立刻收工。

        音訊訊號本身是由 MeetingBot._on_audio 經 note_human_audio() 即時觸發，
        不靠這裡的輪詢——這個迴圈主要負責 HELLO_AUDIO_TIMEOUT_SECONDS 逾時保險
        （真人一直沒有音訊也要在有限時間內問候），順便當一層備援。
        """
        while not gate.greeted:
            await asyncio.sleep(HELLO_POLL_SECONDS)
            self.maybe_greet_hello(gate)

    async def consume(self, pool: STTPool) -> None:
        """收 STT 產出的事件，更新狀態。

        三種事件：
        - Speaking：某人「正在說話」（來自 partial，每秒）→ 餵給超時規則
        - SpeakingStopped：某人的 STT 連線結束 → 清掉他的「正在說話」，那句不會再 commit
        - Utterance：某人「講完一段」（來自 commit）→ 進逐字稿與統計
        """
        from .stt import Partial, Speaking, SpeakingStopped
        async for ev in pool.utterances():
            self._sync_participants()  # 名單長度變了（新人進頻道）→ 重送 meeting
            if isinstance(ev, Partial):
                self.on_partial(ev)
                continue
            if isinstance(ev, Speaking):
                self.st.speaking_now(ev.speaker, ev.since)
                self.emit("speaking", {"speaker": ev.speaker, "active": True})
                continue
            if isinstance(ev, SpeakingStopped):
                # 連線斷了，那句話不會再 commit——不清掉他就永遠算「正在說話」
                self.st.stopped_speaking(ev.speaker)
                self.emit("speaking", {"speaker": ev.speaker, "active": False})
                continue

            u = ev
            # STT 真的吐出一則逐字稿＝耳朵是好的。失聰判定的累積量歸零，
            # 閘門（若已鎖上）在下一個快路 tick 自動解除（見 hearing.py）。
            self.hearing.heard(self.now)
            self.st.stopped_speaking(u.speaker)
            # ⚠️ 名單不在這裡長——從未開口的人也必須在名單裡，
            #    否則「沉默者點名」永遠不會點到真正全程沉默的那個人
            self.st.add(u)
            self.st.utterances.sort(key=lambda x: x.start)  # 多人各自連線，到達順序不等於發生順序
            self.done.discard(("有人被冷落", u.speaker))
            # 「全場沉默」的 target 是 None，不會被上面那行解除——不管是誰開口，
            # 全場都已經不再沉默，claim 要解除，下一次冷場才會再觸發（不然整場只提醒一次）
            self.done.discard(("全場沉默", None))
            previous = self.note_speaker(u.speaker)  # 世界版本只在發言者換人時改變
            # 換人講話了 → 解除前一位的「發言超時」claim（與 run.py 回放路徑同邏輯）：
            # 他之後再連講三分鐘還是要提醒，不能被上一輪的 claim 永久壓掉
            if previous is not None and previous != u.speaker:
                self.done.discard(("發言超時", previous))
            self.note_target_spoke(u.speaker)  # 被點名的人自己開口了 → 候選作廢
            line = f"[{fmt(u.start)}] {u.speaker}：{u.text}"
            self._log(line)
            self.emit("utterance", {"speaker": u.speaker, "text": u.text, "start": u.start, "end": u.end})
            self.emit("speaking", {"speaker": u.speaker, "active": False})
            self.emit_share()

    def _fast_tick(self, hello_gate: "HelloGate | None") -> None:
        """快路單次檢查——拆出來方便測試（watch_fast 本身是不會停的迴圈，
        跟 _run_slow_score／watch_slow 的拆法一樣）。

        ⚠️ --say-hello 開啟且問候還沒送出時，只擋「介入」（下面 chair.request()
        那段），不擋 fast_timer 的 emit：問候是主席的開場自我介紹，理應是第一句；
        快路每秒檢查、比等音訊確認的問候快得多，不擋住介入的話「全場沉默」等
        規則會搶在問候之前開口（T13 缺陷 D，實測 log：使用者剛進頻道，主席先催
        了一次全場沉默，自我介紹才排在後面）。修好缺陷 A 之後這個情境本身會少
        很多，但兩者是獨立問題：A 解決「不該觸發卻觸發」，這裡解決「該問候先
        問候」。
        fast_timer 是觀戰 UI 唯一穩定的每秒事件源，用來推進畫面上的計時器——
        整段跳過的話，等真人加入並確認音訊之前，畫面會卡在 00:00 像壞掉一樣
        （T13 review 發現：曾經連 emit 都被一起擋掉，bot 先進空頻道等待的那
        段時間畫面完全靜止）。
        """
        if self.chair is None:
            return
        now = self.now
        speaker, run_seconds = self.st.current_run_seconds(now)
        self.emit("fast_timer", {
            "run": {"speaker": speaker, "seconds": run_seconds} if speaker is not None else None,
            "silent": {p: self.st.silent_seconds(p, now) for p in self.st.participants},
            "remaining": self.st.remaining_seconds(now),
        })
        # 失聰閘門：STT 斷線或逐字稿停止更新時，三條靠「逐字稿是新鮮的」量出來
        # 的規則量到的其實是自己的故障（逐條理由見 fast_path.DEAF_SUPPRESSED_KINDS，
        # 偵測與門檻依據見 hearing.py）。
        # ⚠️ 判定與 emit 放在 hello_gate 的 return 之前：問候還沒送出時介入本來
        # 就不排，但「主席聽不到」這件事現場必須看得見，不能被問候閘門一起吃掉。
        deaf_reason = self.deaf_reason()
        self._note_hearing(deaf_reason)
        if hello_gate is not None and not hello_gate.greeted:
            return  # 心跳照發，但問候還沒送出前不排入任何介入
        # 收尾閘門（快路那一關）：房間已經在道別了，四條規則的話術都預設會議
        # 還要繼續，這時候出聲就是在要求別人做一件已經結束的事
        # （逐條理由見 fast_path.CLOSING_SUPPRESSED_KINDS）。
        closing = meeting_is_closing_for_rules(self.st, now)
        for t in fast_path.check(self.st, now, self.done, closing=closing,
                                  deaf=bool(deaf_reason)):
            iv = Intervention(kind=t.kind, target=t.target,
                               text=fast_path.utterance_for(t, self.phrase_bank),
                               hard=t.hard, revision=self.revision, created_at=now)
            if self.chair.request(iv):
                self.done.add((t.kind, t.target))  # claimed：防每秒重送；interventions 由 Chair 寫
                if t.kind == "全場沉默":
                    self.st.note_room_silence_fired()  # 退避次數只在真的排入時遞增
                self.emit("queued", {"kind": iv.kind, "target": iv.target, "text": iv.text,
                                      "hard": iv.hard})
                mark = "🔔 硬打斷" if t.hard else "💬 軟插入"
                self._log(f"    ├─ {mark}【{t.kind}】{t.detail} → 排入")
            break

    async def watch_fast(self, hello_gate: "HelloGate | None" = None) -> None:
        """快路：規則、零延遲，每秒檢查。"""
        while True:
            await asyncio.sleep(1.0)
            self._fast_tick(hello_gate)

    async def _run_slow_score(self, last_n: int) -> int:
        """跑一次慢路評分（若 should_score 判定要跑），回傳新的 last_n。

        T29 起這裡是**兩次 LLM 呼叫**：先判斷、通過第一關閘門才產話術。
        為什麼要拆、拆的依據是什麼，見 `slow_path` 模組 docstring。

        三件事的順序是刻意的，改動前先讀完：

        1. **話術呼叫在 `emit("slow_score")` 之前**。觀戰 UI 的三態配對靠
           「admissible 的 slow_score 後面緊接著的那一筆事件就是它的 queued」
           （見 `spectator/index.html` handleEvent 開頭）。話術要跑好幾秒，
           在這段期間 emit 的話，中間會插進 utterance／speaking／fast_timer，
           每一筆 admissible 判斷都會被誤判成「受阻·主席忙碌中」。所以先把話術
           拿到手、算完最終 admissible，再 emit——emit 與 `chair.request()`
           之間沒有 await，其他 task 插不進來，配對關係跟拆呼叫之前完全一樣。
        2. **一次評分只 emit 一筆 `slow_score`**，帶最終結果。不 emit「判斷完成」
           與「話術完成」兩筆——那會讓 slowTotal 變兩倍，直接破壞守恆不變式。
        3. **第二關重驗（`slow_recheck_admissible`）不能省**，理由見該函式 docstring。

        拆成獨立方法純粹是為了可測試——watch_slow 本身用 TICK=5s 的迴圈跑不完測試。
        """
        from .slow_path import phrase, score, should_score
        busy = self.chair.pending is not None or self.chair.playing is not None
        if not should_score(self.st, self.now, last_n, busy=busy):
            return last_n
        n = len(self.st.utterances)
        try:
            r = await asyncio.to_thread(score, self.st, self.now, self.phase)
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️ 慢路失敗：{type(e).__name__}")
            return last_n  # last_n 不推進，下一 tick 用同一批 utterance 重試

        self._recent_slow_types.append(str(r.get("type") or ""))
        admissible, reason = slow_gate(self.st, self.now, r, deaf=bool(self.deaf_reason()))
        phrase_seconds = None
        if admissible:
            t0 = self.now
            try:
                r["utterance"] = await asyncio.to_thread(phrase, self.st, t0, r, self.phase)
            except Exception as e:  # noqa: BLE001
                # 話術呼叫失敗不退回罐頭句（罐頭句正是這次要修的東西），也不讓
                # 例外把整個 watch_slow 打死——這一次介入就是不出聲，理由誠實記下來。
                print(f"    ⚠️ 慢路話術失敗：{type(e).__name__}")
                r["utterance"] = ""
            phrase_seconds = round(self.now - t0, 2)
            admissible, reason = slow_recheck_admissible(self.st, self.now, r,
                                                          deaf=bool(self.deaf_reason()))

        self.emit("slow_score", {
            "positive": r.get("positive"), "negative": r.get("negative"), "none": r.get("none"),
            "type": r.get("type"), "verdict": r.get("verdict"), "utterance": r.get("utterance", ""),
            "pros": r.get("pros", []), "cons": r.get("cons", []),
            "admissible": admissible, "reason": reason,
            "utterance_seconds": phrase_seconds,
        })
        if not admissible:
            if reason == "type=無":
                self.log.append(
                    f"    (慢路被 type=無 壓掉) P{r['positive']}/N{r['negative']}"
                    f"/None{r['none']}")
            elif reason == "收尾":
                self.log.append(f"    (會議正在收尾，慢路不出聲)【{r['type']}】")
            elif reason in ("失聰", "失聰(話術後)"):
                self.log.append(f"    (主席聽不到，慢路不出聲：{reason})【{r['type']}】")
            elif reason == "冷卻":
                self.log.append(f"    (慢路結果在冷卻期內作廢)【{r['type']}】")
            elif reason == "話術失敗":
                self.log.append(f"    (慢路決定開口但話術生成失敗，放棄這次介入)【{r['type']}】")
            elif reason == "話術過長":
                self.log.append(f"    (慢路話術超過長度上限，整句作廢)【{r['type']}】"
                                f"{len(r.get('utterance') or '')}字")
            elif reason in ("收尾(話術後)", "冷卻(話術後)"):
                self.log.append(f"    (慢路話術生成期間世界已變：{reason})【{r['type']}】"
                                f"「{r.get('utterance', '')}」")
            return n
        iv = Intervention(kind=r["type"].strip(), target=None, text=r["utterance"], hard=False,
                           revision=self.revision, created_at=self.now)
        if self.chair.request(iv):
            self.emit("queued", {"kind": iv.kind, "target": iv.target, "text": iv.text, "hard": iv.hard})
            self._log(f"    └─ 🤔 慢路【{r['type']}】"
                      f"P{r['positive']}/N{r['negative']}/None{r['none']} → 排入")
        return n

    async def watch_slow(self) -> None:
        """慢路：LLM 評分，背景持續跑，不阻塞任何東西。"""
        last_n = 0
        while True:
            await asyncio.sleep(TICK)
            if self.chair is None:
                continue
            last_n = await self._run_slow_score(last_n)

    def on_partial(self, ev) -> None:
        """partial 逐字稿：只 emit 給畫面，不碰 MeetingState、不進逐字稿、不餵任何規則。
        規則只看 commit 後的 Utterance——partial 會被修正，拿它判斷等於拿草稿判斷。"""
        self.emit("partial", {"speaker": ev.speaker, "text": ev.text})

    def set_phase(self, phase: str, source: str) -> None:
        """改階段的唯一入口（觀戰畫面 POST /phase 與偵測器都走這裡），改了才 emit。"""
        if phase == self.phase:
            return
        self.phase = phase
        self.emit("phase", {"phase": phase, "source": source})

    async def watch_phase(self) -> None:
        """階段自動判斷：每 PHASE_TICK_SECONDS 問一次，遲滯後才建議或套用。
        沒開 --auto-phase 不會被排進 gather，行為與從前完全相同。"""
        from . import phase as ph
        det = ph.PhaseDetector(current=self.phase)
        while True:
            await asyncio.sleep(ph.PHASE_TICK_SECONDS)
            if self.chair is None or ph.judgeable(self.st, self.now) is not None:
                continue
            if det.current != self.phase:      # 人手動切了，偵測器跟著對齊
                det.current, det.pending, det.streak = self.phase, None, 0
            try:
                reading = await asyncio.to_thread(
                    ph.judge, self.st, self.now, self.phase, list(self._recent_slow_types))
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠️ 階段判斷失敗：{type(e).__name__}")
                continue
            switched = det.observe(reading, self.now)
            applied = bool(switched) and self.auto_phase == "apply"
            self.emit("phase_suggestion", {**reading, "current": self.phase, "applied": applied})
            if switched:
                self.log.append(f"    (階段判斷：{switched}，信心 {reading['confidence']:.2f}"
                                f"{'，已套用' if applied else '，待確認'}) {reading['reason']}")
                if applied:
                    self.set_phase(switched, "auto")

    async def watch_phrasing(self) -> None:
        """T14：背景幫每個 kind 預先生成句型變體，純背景工作，跟任何介入
        時機無關——快路取用（`fast_path.utterance_for`）與問候
        （`maybe_greet_hello`）完全不會等這個 task，佇列還空著就退回寫死
        模板（驗收 9）。

        開場先把每個 kind 都補一次，讓問候等一開始就有機會用到生成版本
        （驗收 10）；之後定期檢查有沒有 kind 佇列快用完，需要才再補。
        每次生成都經 `asyncio.to_thread` 丟到執行緒池——這裡是一支獨立的
        task，跟 `watch_fast`／`consume`／Chair 播放平行跑在同一個
        `asyncio.gather`（見 main_async），不會擋住其他任何東西。

        `phrase_bank.can_generate()` 同時擋住兩件事：沒有生成器（例如
        `--no-llm`）與同場會議呼叫次數已達上限（`MAX_GENERATIONS_PER_MEETING`）
        ——兩種情況這個迴圈都會很快自然結束，什麼都不做。
        """
        for kind in PHRASE_KINDS:
            if not self.phrase_bank.can_generate():
                return
            await asyncio.to_thread(self.phrase_bank.refill, kind)
        while self.phrase_bank.can_generate():
            await asyncio.sleep(PHRASING_POLL_SECONDS)
            for kind in PHRASE_KINDS:
                if not self.phrase_bank.can_generate():
                    break
                if self.phrase_bank.needs_refill(kind):
                    await asyncio.to_thread(self.phrase_bank.refill, kind)

    def glossary_batch_due(self, pending: int, now: float, last_run: float) -> bool:
        """這一輪要不要跑提示卡抽取。拆出來是為了可測（迴圈本身不會停）。

        兩個條件擇一：累積夠一批新發言，或距上次夠久了但還有沒處理的發言
        （講得慢的會議湊不滿一批，不能就永遠不抽）。完全靜默的功能沒有即時性
        壓力，所以刻意取大——2026-08-29 那場 125 則發言的實測會議因此只跑
        12 次抽取，而不是慢路那種每 5 秒一次（同一場慢路評分了 34 次）。
        """
        if pending <= 0:
            return False
        return (pending >= glossary.BATCH_MIN_UTTERANCES
                or now - last_run >= glossary.BATCH_MAX_WAIT_SECONDS)

    async def watch_glossary(self, book: "glossary.Glossary | None" = None) -> None:
        """提示卡：術語／專有名詞的說明卡，**完全靜默**地走事件匯流排送到觀戰 UI。

        跟這個檔案裡其他背景任務最大的差別是它**不碰主席**：不建 `Intervention`、
        不呼叫 `chair.request()`、不寫 `st.interventions`／`self.done`／
        `self.revision`，也不發 TTS。它唯一的輸出就是 `emit("glossary", …)`，
        因此不會讓主席多開口一次、不佔冷卻期額度（性質與判準見 glossary.py）。

        隔離：整段迴圈體包在 try/except 裡。這支 task 跟快路／慢路／播放器一起
        排在 `main_async` 的 `asyncio.gather` 上，例外逃出去會把整場會議一起拆掉
        ——所以 LLM 失敗、搜尋逾時、回傳格式壞掉一律只印一行就繼續，下一批再試。
        `CancelledError` 必須原樣往外拋，那是收尾路徑，不是錯誤。

        `seen` 用 `(speaker, start, text)` 當鍵而不是切片索引：`consume()` 每次
        收到新發言都會重新 `sort` 整個 `st.utterances`，語音重疊時新的一則可能被
        插到中間，用索引切會漏掉或重複送同一則。

        `book` 可注入（同 `phrase_bank` 的作法）——測試因此可以餵假的抽取器與
        查詢函式，完全不碰網路。正式路徑不傳，用預設的真實實作。

        ⚠️ 這裡的 `emit` 不能插進「`slow_score` 緊接 `queued`」那兩筆之間——
        `minutes.py` 的 `_pair_interventions` 與觀戰 UI 的 `awaitingQueuedJudgment`
        都靠那個相鄰關係認出「這次慢路判斷有沒有排進佇列」，中間插一筆別的事件
        會讓慢路介入被誤判成「受阻」。目前成立且不需要額外防護：`_run_slow_score`
        在那兩個 emit 之間沒有任何 await，同一個 event loop 裡沒有別的 coroutine
        插得進去。**之後若在那兩行之間加入任何 await，這個不變量就會被打破。**
        """
        book = book if book is not None else glossary.Glossary()
        seen: set[tuple[str, float, str]] = set()
        last_run = 0.0
        while True:
            await asyncio.sleep(GLOSSARY_POLL_SECONDS)
            try:
                snapshot = list(self.st.utterances)  # 同一個 event loop，複製期間不會被改
                batch = [u for u in snapshot if (u.speaker, u.start, u.text) not in seen]
                now = self.now
                if not self.glossary_batch_due(len(batch), now, last_run):
                    continue
                last_run = now
                cards = await asyncio.to_thread(
                    book.run_batch, batch, snapshot, list(self.st.participants))
                # 成功了才把這批標成處理過——跟慢路「失敗時 last_n 不推進、下一輪
                # 用同一批重試」同一個原則。標在呼叫之前的話，抽取失敗的那一批會
                # 被靜默吃掉，那些發言裡的術語整場再也不會被看到。
                seen.update((u.speaker, u.start, u.text) for u in batch)
                for card in cards:
                    self.emit("glossary", card.as_data())
                    self._log(f"    📎 提示卡【{card.term}】"
                              f"（{fmt(card.first.t)} {card.first.speaker} 首次提到）")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠️ 提示卡失敗（不影響快路／慢路）：{type(e).__name__}: {e}")


def install_shutdown_signal_handlers(main_task: asyncio.Task) -> None:
    """把 SIGINT／SIGTERM 都接管到 `main_task.cancel()`。

    部署端用 setsid nohup … & 啟動：非互動 shell 對背景工作會把 SIGINT 自動設成
    SIG_IGN 並讓子行程繼承（實測，見 T-G 的 task-g-report.md），kill -INT 因此完全
    沒反應，只有 SIGTERM 打得到——但 SIGTERM 預設行為是直接砍死，shutdown() 跑不到。
    add_signal_handler 會重設 disposition，就算繼承的是 SIG_IGN 也蓋得掉（已實測驗證）；
    兩個訊號都明確接管、統一走 main_task.cancel()，落到既有的
    except (KeyboardInterrupt, asyncio.CancelledError) → finally: shutdown() 路徑。

    抽成獨立函式讓 `tests/harness/live_shutdown_driver.py`（不連 Discord 的關機
    行為驅動腳本）可以直接 import 這裡的註冊邏輯，而不必自己複製一份容易與這裡
    脫鉤的訊號接管程式碼。
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except NotImplementedError:
            pass  # Windows 的 ProactorEventLoop 不支援 add_signal_handler；本專案只跑 macOS/Linux


async def main_async(args) -> None:
    load_dotenv(Path(__file__).parent.parent.parent / ".env")

    main_task = asyncio.current_task()
    install_shutdown_signal_handlers(main_task)

    st = MeetingState(topic=args.topic, duration_min=args.duration, participants=[])
    # T14：--no-llm 時不注入生成器——PhraseBank.can_generate() 恆為 False，
    # take() 永遠回 None，快路與問候的措辭行為與沒有這個功能之前完全一致。
    phrase_bank = PhraseBank(generator=None if args.no_llm else generate_patterns,
                              topic=args.topic)
    session = Session(st, args.phase, cancel=main_task.cancel, phrase_bank=phrase_bank,
                      auto_phase=(None if args.no_llm else args.auto_phase))
    # 議題與人名餵給 STT 當專有名詞提示，中英夾雜的辨識率差很多
    pool = STTPool(os.environ["ELEVENLABS_API_KEY"], keyterms=args.keyterms)
    # 失聰偵測的臂 (A)：連線層自己的健康狀態（見 hearing.py 與 STTPool.offline）。
    session.stt_pool = pool

    bot = MeetingBot(pool, args.channel, st)
    # RTP 層「麥克風正在／不在傳送」訊號進事件流——跟 session.consume() 裡的
    # "speaking"（來自 STT）是獨立來源，見 events.py 的 kind 說明與
    # discord_source.py 的 on_voice_activity docstring。
    # 走 `session.note_voice` 而不是直接 emit：同一顆訊號還要餵失聰偵測的臂 (B)，
    # 兩件事綁在一個方法裡才不會之後只改到其中一條（見 Session.note_voice）。
    bot.on_voice_activity = session.note_voice
    voice = Voice(os.environ["ELEVENLABS_API_KEY"])
    earcon = Earcon()  # 缺檔在這裡就炸，不要進了頻道才發現
    chair: Chair | None = None
    # 只有 --say-hello 才需要問候時機的判斷；沒開這個旗標就永遠不建 gate，
    # start_chair／watch_hello 據此完全跳過問候路徑（驗收 5）
    hello_gate = build_hello_gate(args.say_hello)
    if hello_gate is not None:
        # bot 收到在場真人的第一個音訊封包（音訊執行緒，已經過 call_soon_threadsafe
        # 轉進 event loop）→ 立刻重新判斷是否該問候，不等下一次輪詢
        bot.on_human_audio = lambda name: session.note_human_audio(hello_gate)

    def on_spoken(iv, at):
        # at 是 Chair 量到的「第一個可聽幀」時刻（裸 perf_counter）——
        # 這裡重新讀時鐘的話，callback 的時間契約就白給了
        at_relative = at - session.t0
        st.interventions.append(at_relative)
        session.emit("spoken", {"kind": iv.kind, "target": iv.target, "text": iv.text,
                                 "hard": iv.hard, "at": at_relative})
        session.emit_share()
        session._log(f"    🗣  主席【{iv.kind}】「{iv.text}」")

    def on_failed(iv, reason):
        session.release_claim(iv)  # 沒出聲就不算講過；重試的節流交給 Chair 的 30 秒退避
        session.emit("failed", {"kind": iv.kind, "target": iv.target, "text": iv.text, "reason": reason})
        session._log(f"    ⚠️ 主席開口失敗【{iv.kind}】{reason}；本該說：「{iv.text}」")

    def on_escalate(iv):
        return escalate_with_current_facts(st, session.now, session.revision, iv,
                                            session.phrase_bank,
                                            deaf=bool(session.deaf_reason()))

    async def start_chair():
        while getattr(bot, "output", None) is None:  # 等 bot 進頻道
            await asyncio.sleep(0.5)
        nonlocal chair
        chair = Chair(st, bot.output, voice, earcon,
                      revision=lambda: session.revision, on_spoken=on_spoken, on_failed=on_failed,
                      on_escalate=on_escalate, on_dropped=session.on_dropped)
        session.chair = chair
        # 播放器重建會換掉 bot.output；Chair 不跟著換就會對著沒人消費的舊佇列說話。
        # 註冊完必須再 re-sync 一次：上面「讀 bot.output → 建 Chair → 註冊」這幾步之間
        # 播放執行緒也可能重建過，那次通知沒人收得到（R1）
        bot.on_output_replaced = chair.replace_output
        chair.replace_output(bot.output)
        session.emit_meeting()
        session._last_participant_count = len(st.participants)
        if hello_gate is not None:
            # state_sync() 在 on_ready 就同步過名單——頻道裡已經有真人就立刻問候；
            # 空頻道則先不問候，交給下面排入 tasks 的 watch_hello 輪詢等第一個真人進來
            session.maybe_greet_hello(hello_gate)
        await chair.run()

    tasks = [
        asyncio.create_task(bot.start(os.environ["DISCORD_BOT_TOKEN"])),
        asyncio.create_task(start_chair()),
        asyncio.create_task(session.consume(pool)),
        asyncio.create_task(session.watch_fast(hello_gate)),
    ]
    if not args.no_llm:
        tasks.append(asyncio.create_task(session.watch_slow()))
        tasks.append(asyncio.create_task(session.watch_phrasing()))
        if session.auto_phase:
            tasks.append(asyncio.create_task(session.watch_phase()))
        # 提示卡：靜默、不經過 Chair、不影響冷卻期（見 Session.watch_glossary）。
        # 跟慢路一樣掛在 --no-llm 底下——它也是靠 LLM 抽詞的。
        tasks.append(asyncio.create_task(session.watch_glossary()))
    if hello_gate is not None:
        tasks.append(asyncio.create_task(session.watch_hello(hello_gate)))
    if args.spectator_port:
        serve = _try_import_spectator_serve()
        if serve is not None:
            tasks.append(asyncio.create_task(
                serve(session, args.spectator_port, args.spectator_token or None)))

    from . import style as _style
    _applied = _style.apply(args.style)
    print(f"議題：{args.topic}（預計 {args.duration} 分鐘，階段：{args.phase}）")
    if _applied:
        print(f"風格檔位：{_style.LABELS[args.style]}（{args.style}）→ " + "、".join(f"{k}={v:g}" for k, v in _applied.items()))
    print("等待語音頻道…（Ctrl-C 結束並輸出摘要）\n")
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await shutdown(session, bot, tasks)


def _drain_chair(session: Session) -> None:
    """把 chair 手上還沒出聲的介入收成 dropped（既有 on_dropped 路徑，reason=shutdown）。

    chair.playing 不動：它已經出聲、或即將由既有 `_spoken_reported`／`on_failed` 機制
    回報，這裡搶著標記反而會誤報。可重複呼叫——收完就設回 None。
    """
    chair = session.chair
    if chair is None:
        return
    if chair.pending is not None:
        iv, chair.pending = chair.pending, None
        chair.on_dropped(iv, "shutdown")
    if chair.candidate is not None:
        iv, chair.candidate = chair.candidate, None
        chair.on_dropped(iv, "shutdown")


async def _flush_spectator(session: Session, timeout: float = 3.0) -> None:
    """等已連線的觀戰 UI 把佇列裡的事件（重點是剛 emit 的 `minutes`）真的寫進 socket。

    沒有這一步的話，`summary()` emit 完就直接 cancel 掉 `serve()`，SSE handler 的
    `queue.get()` 連排程都還沒排到就被砍掉，頁面永遠收不到總結——事件發出去了，
    但沒有人收到。逾時（預設 3 秒）就不再等：收尾比一個 client 重要。
    """
    if not session.subscribers:
        return
    try:
        from .spectator import flush_streams
    except ImportError:
        return  # 觀戰 UI 模組不在，本來就沒有 SSE client 要等
    if not await flush_streams(session, timeout):
        print(f"    ⚠️ 觀戰 UI 事件推送逾時（{timeout:.0f} 秒），不再等待")


async def shutdown(session: Session, bot: MeetingBot, tasks: list[asyncio.Task]) -> None:
    """收尾：不論怎麼中斷都要保證會議摘要／逐字稿／事件紀錄寫得出去。

    實測（SIGINT → asyncio.Runner 的優雅取消路徑）發現兩件事：
    1. `asyncio.gather(*tasks)` 被取消只會讓等在它上面的這次 await 拋一次
       CancelledError；`tasks` 裡的個別 task（bot.start()／start_chair()／
       consume()／watch_fast()／watch_slow()）雖然被級聯取消，但這時還沒真正
       收尾完成。
    2. 若在它們收尾前就呼叫 `bot.close()`，discord.py 會因為 gateway／語音的
       讀取迴圈被腰斬而卡死等不到 ack（實測：15 秒不回）；先讓 tasks 收尾完
       再呼叫則正常在數秒內完成。

    順序（T3a 重排後）：

    1. `_drain_chair()`：把中斷當下卡在 chair.pending／candidate 的介入收成
       dropped，否則 events.jsonl 只有配不到對的 `queued`、host.md 也漏記（P7）。
    2. `summary()`：印摘要、寫 `.log`、寫 host.md／minutes.md，最後 emit `minutes`。
       純同步、不依賴 bot 或其他 task 是否還活著——所以擺在 cancel 之前，
       cancel／gather 出任何意外都不會賠掉這幾個檔案。
    3. `_flush_spectator()`：等已連線的 SSE client 收到剛才那個 `minutes`。
       必須排在第 4 步之前。注意 `main_task.cancel()`（SIGTERM 與 UI 的 POST /end
       都走這條）會讓 `main_async` 裡的 `asyncio.gather(*tasks)` 級聯 cancel 掉
       `serve()`——也就是進到 shutdown 時 `serve()` 其實「已經被要求取消」了。
       實測（真 live.py ＋ raw socket 讀 SSE）仍收得到 `minutes`：shutdown 的同步
       段（drain＋summary＋emit）在事件迴圈把取消送達之前就跑完，而 `serve()` 的
       `finally: runner.cleanup()` 還會給進行中的 handler 一秒 grace，flush 就在
       那個窗口內完成（實測 0.00–0.01 秒）。這也是 `serve()` 必須把
       `shutdown_timeout` 從預設 60 秒調小、但不能調成 0 的原因。
    4. cancel tasks 並 gather：讓所有背景 task 真正收尾（bot.close() 的前提）。
    5. `_drain_chair()` 第二次：關掉 T4 留下的殘餘窗口——慢路的 `to_thread` 若剛好
       在第一次 drain 之後、task 真的死掉之前回來，`chair.request()` 會塞進一個
       新的 pending，第一次 drain 撈不到它。
    6. `_write_events_jsonl()`：**最後**才寫，events.jsonl 才含得到第 5 步的 dropped。
       這是把 events.jsonl 從 `summary()` 拆出來的唯一理由；它同樣是純同步的，
       挪到 cancel 之後不違反第 2 步「不依賴 task 存活」的原則。第 3–5 步包在
       try/finally 裡，途中被取消也保證寫得出去。
    7. `bot.close()`：仍包一層 10 秒 timeout 當最後防線，避免以後別的卡死情境
       把整個進程吊住。

    第 3–7 步全部在同一個 finally 裡：收尾期間若再收到一次取消（第二次 SIGINT／
    SIGTERM），CancelledError 會從第 3 或第 4 步的 await 重新拋出來，**`bot.close()`
    不能因此被跳過**——跳過就等於把 Discord 連線丟著讓對方逾時。`session.ending`
    擋掉的是「第二次 POST /end」這個來源（見 `Session.request_end`）；訊號路徑
    刻意保留「按第二次就強制退出」的既有語意，所以這裡仍要能在被打斷的情況下
    盡量把 close 送出去。那條路徑上 tasks 沒機會收尾完，`bot.close()` 可能吃滿
    10 秒 timeout——那是刻意的取捨：一定嘗試 close，勝過保證不逾時。
    """
    _drain_chair(session)
    session.ending = True  # 擋掉收尾期間再進來的 POST /end（訊號路徑不經過 request_end）
    events_path = summary(session)
    try:
        await _flush_spectator(session)
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # 二次取消會從上面的 await 拋出來，tasks 可能還沒被 cancel 過。這一輪是純
        # 同步的、不會再被打斷，至少不留活著的背景 task（正常路徑上全都 done 了）。
        for t in tasks:
            if not t.done():
                t.cancel()
        _drain_chair(session)
        _write_events_jsonl(session, events_path)
        try:
            await asyncio.wait_for(bot.close(), timeout=10.0)
        except asyncio.TimeoutError:
            print("    ⚠️ bot.close() 逾時（10 秒），放棄等待")
        except asyncio.CancelledError:
            pass  # 又被打斷：close 已經送出去了，不再等


def _try_import_spectator_serve():
    """觀戰 UI 模組（T-D）尚未就緒時略過，不影響主流程。"""
    try:
        from .spectator import serve
    except ImportError:
        print("(觀戰 UI 模組尚未就緒，略過)")
        return None
    return serve


def _try_write_minutes(session: Session, out_dir: Path) -> tuple[Path, Path] | None:
    """會議記錄模組（T-C）尚未就緒時略過，不影響主流程。

    回傳 `write_minutes` 給的 `(host_path, minutes_path)`；模組不存在時回 None，
    呼叫端據此把 `minutes` 事件標成 `error`。
    """
    try:
        from .minutes import write_minutes
    except ImportError:
        print("(會議記錄模組尚未就緒，略過)")
        return None
    return write_minutes(session, out_dir)


def _read_md(path: Path) -> str:
    """讀回剛寫出的 md。理論上必定存在（上一行才寫的），但這是 shutdown 路徑——
    真的讀不到也只該讓 `minutes` 事件少一份內容，不能賠掉整個收尾。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"    ⚠️ 讀不回 {path}：{type(e).__name__}: {e}")
        return ""


def _emit_minutes(s: Session, paths: tuple[Path, Path] | None,
                  log_path: Path, events_path: Path) -> None:
    """把兩份 md 的完整內容與四個檔案路徑（相對 cwd）發成 `minutes` 事件。

    UI 直接吃事件裡的 md 內容，不用另外開檔——觀戰頁面跟 live.py 可能不在同一台。
    """
    data = {
        "host_md": "", "minutes_md": "",
        "host_path": "", "minutes_path": "",
        "log_path": str(log_path), "events_path": str(events_path),
    }
    if paths is None:
        data["error"] = "minutes module unavailable"
    else:
        host_path, minutes_path = paths
        data["host_md"] = _read_md(host_path)
        data["minutes_md"] = _read_md(minutes_path)
        data["host_path"] = str(host_path)
        data["minutes_path"] = str(minutes_path)
    s.emit("minutes", data)


def _write_events_jsonl(s: Session, events_path: Path | None) -> None:
    """把 `session.events` 寫成 events.jsonl（每行一個 Event）。

    由 `shutdown()` 在第二次 `_drain_chair()` 之後才呼叫——那次 drain 的 `dropped`
    必須進得了檔案（見 shutdown 的順序說明）。`events_path` 為 None 代表 `summary()`
    被測試換掉了，沒有路徑可寫。
    """
    if events_path is None:
        return
    with events_path.open("w", encoding="utf-8") as f:
        for event in s.events:
            f.write(json.dumps(dataclasses.asdict(event), ensure_ascii=False) + "\n")
    print(f"事件紀錄：{events_path}")


def summary(s: Session) -> Path:
    """印摘要、寫 `.log` 與兩份 md，最後 emit `minutes` 事件。

    回傳 events.jsonl 的**目標路徑**——內容不在這裡寫，由 `shutdown()` 在第二次
    drain 之後交給 `_write_events_jsonl()`。這樣 `minutes` 事件的 `events_path`
    才指得到與 `.log` 共用同一個時間戳的那個檔名。
    """
    print(f"\n{'─' * 60}\n會議摘要（{fmt(s.now)}）")
    for p in s.st.participants:
        print(f"- {p}：發言 {s.st.spoke_seconds(p) / 60:.1f} 分鐘"
              f"（佔 {s.st.share(p, s.now):.0%}）")
    out_dir = Path("meetings")
    out_dir.mkdir(exist_ok=True)
    ts = int(time.time())
    out = out_dir / f"meeting-{ts}.log"
    out.write_text("\n".join(s.log), encoding="utf-8")
    print(f"\n逐字稿與介入紀錄：{out}")

    events_out = out_dir / f"meeting-{ts}.events.jsonl"
    # md 先寫出來才讀得回內容；`minutes` 事件必須排在 events.jsonl 寫出之前，
    # 回放模式（只有 events.jsonl）才看得到總結
    _emit_minutes(s, _try_write_minutes(s, out_dir), out, events_out)
    return events_out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="會議")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--phase", default="發散期", choices=["發散期", "呻吟區", "收斂期"])
    ap.add_argument("--style", default=None, choices=["strict", "gentle", "efficient"],
                    help="主持風格檔位（既有快路門檻的組合，未調校；不給就用預設值）")
    ap.add_argument("--auto-phase", default=None, choices=["suggest", "apply"],
                    help="階段自動判斷：suggest 只在觀戰畫面建議，apply 自動套用（--no-llm 時無效）")
    ap.add_argument("--channel", type=int, default=None, help="語音頻道 ID")
    ap.add_argument("--keyterms", nargs="*", default=None, help="專有名詞提示")
    ap.add_argument("--no-llm", action="store_true", help="只跑快路")
    ap.add_argument("--say-hello", action="store_true", help="進頻道後主席先開口問候")
    ap.add_argument("--spectator-port", type=int, default=0, help="觀戰 UI 監聽埠（0＝不開）")
    ap.add_argument("--spectator-token", default="",
                    help="操作權杖（POST /phase、/end 要帶）。留空＝讀環境變數 "
                         "AHEM_SPECTATOR_TOKEN，再沒有就每次啟動隨機產生一組並印出來")
    try:
        asyncio.run(main_async(ap.parse_args()))
    except KeyboardInterrupt:
        # 第二次 Ctrl-C：asyncio.Runner 會直接讓 KeyboardInterrupt 逃出 asyncio.run()
        # （shutdown() 這時已經在跑或跑完了）；接住它單純是不讓終端印一截 traceback。
        pass
    except asyncio.CancelledError:
        # 第二次 SIGINT／SIGTERM：訊號 handler 對已在收尾的 main_task 再送一次
        # cancel，CancelledError 會從 shutdown() 正在等的地方逃出 asyncio.run()。
        # 「按第二次就強制退出」是刻意保留的語意（見 shutdown 的 docstring）——
        # 這裡只負責讓它變成一行說明加一個明確的離開碼，不噴 traceback。
        # 檔案與 bot.close() 已經由 shutdown() 的 finally 盡力做完了。
        print("    收到第二次結束訊號，強制退出")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
