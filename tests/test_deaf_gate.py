"""失聯偵測：`hearing.py` 的判準、`fast_path` 的逐條閘門、`live` 的接線。

真實會議事件檔的反向驗證（沒閘門會觸發／有閘門不會）在
`tests/harness/test_regression_stt_deaf_gate.py`。這裡負責構造出來的逐條行為：
每一條規則各給一個「規則成立且 STT 健康」的狀態，先確認它會觸發，再確認同一個
狀態在失聰時的結果——三條被壓住、「議程超時」照常觸發。

判準與 45 秒門檻的量測依據見 `src/meeting_host/hearing.py`；壓住哪三條、
「議程超時」為什麼不壓，見 `fast_path.DEAF_SUPPRESSED_KINDS`。
"""
import time

import pytest

from meeting_host import fast_path, live
from meeting_host.hearing import (
    DEAF_VOICED_SECONDS,
    REASON_NO_TRANSCRIPT,
    REASON_STT_OFFLINE,
    HearingMonitor,
)
from meeting_host.live import Session, escalate_with_current_facts
from meeting_host.speaker import Intervention
from meeting_host.state import MeetingState, Utterance
from meeting_host.stt import OFFLINE_FAILS, STTPool


class RecordingChair:
    """只記錄 request()，不節流——沿用 test_live_wiring.py 同名替身的作法。"""

    def __init__(self):
        self.requests: list[Intervention] = []
        # `Session.note_target_spoke` 會讀這兩個欄位（consume 路徑走得到）
        self.pending: Intervention | None = None
        self.candidate: Intervention | None = None

    def request(self, iv: Intervention) -> bool:
        self.requests.append(iv)
        return True


def _kinds(triggers):
    return [t.kind for t in triggers]


# ══ 一、HearingMonitor 的判準 ══════════════════════════════════════════


def test_silence_alone_never_looks_like_deafness():
    """核心區別：沒有人在出聲時，累積量不會長。

    「大家真的安靜下來」與「我的耳朵壞了」在 `silent_seconds` 上長得一模一樣，
    這條測的就是為什麼要改用「累積出聲量」而不是牆鐘時間——安靜一整個小時
    都不會被誤判成失聰。
    """
    h = HearingMonitor()
    assert h.voiced_seconds(3600.0) == 0.0
    assert h.deaf(3600.0) is False


def test_voice_without_transcript_accumulates_until_the_threshold():
    """有人一路對著麥克風講、逐字稿完全沒有新內容 → 累積滿門檻就判失聰。"""
    h = HearingMonitor()
    h.voice("A", True, 10.0)
    assert h.voiced_seconds(10.0 + DEAF_VOICED_SECONDS - 1.0) == DEAF_VOICED_SECONDS - 1.0
    assert h.deaf(10.0 + DEAF_VOICED_SECONDS - 1.0) is False
    assert h.deaf(10.0 + DEAF_VOICED_SECONDS) is True
    assert h.reason(10.0 + DEAF_VOICED_SECONDS) == REASON_NO_TRANSCRIPT


def test_accumulation_counts_the_room_not_each_person():
    """兩個人同時講的那一秒只算一秒——跟 `MeetingState.voice_stopped` 判斷
    `silence_since` 的作法一致（看的是「還有沒有任何人在出聲」）。"""
    h = HearingMonitor()
    h.voice("A", True, 0.0)
    h.voice("B", True, 0.0)
    h.voice("A", False, 30.0)
    h.voice("B", False, 30.0)
    assert h.voiced_seconds(30.0) == 30.0  # 不是 60.0


def test_accumulation_pauses_while_nobody_speaks():
    """出聲 20 秒 → 安靜 600 秒 → 再出聲 20 秒：累積是 40 秒，不是 640 秒。"""
    h = HearingMonitor()
    h.voice("A", True, 0.0)
    h.voice("A", False, 20.0)
    h.voice("A", True, 620.0)
    h.voice("A", False, 640.0)
    assert h.voiced_seconds(640.0) == 40.0
    assert h.deaf(640.0) is False


def test_a_transcript_clears_the_accumulator_and_releases_the_gate():
    """恢復：只要有任何一則逐字稿真的進來就解除，不需要重開會議。"""
    h = HearingMonitor()
    h.voice("A", True, 0.0)
    assert h.deaf(DEAF_VOICED_SECONDS) is True
    h.heard(DEAF_VOICED_SECONDS)
    assert h.deaf(DEAF_VOICED_SECONDS) is False
    # 而且還在講的那一段從現在重新起算，不是沿用舊的起點
    assert h.voiced_seconds(DEAF_VOICED_SECONDS + 5.0) == 5.0


def test_stt_offline_arm_is_immediate_and_independent_of_voice():
    """臂 (A)：連線層自己說連不上時立刻成立，不必等任何人出聲。

    這是 2026-08-31 那場事故的實際形狀（額度耗盡→握手 401），也是它比臂 (B)
    快 40 秒的地方。
    """
    h = HearingMonitor()
    h.note_stt_offline(True)
    assert h.deaf(0.0) is True
    assert h.reason(0.0) == REASON_STT_OFFLINE
    h.note_stt_offline(False)
    assert h.deaf(0.0) is False


def test_stt_offline_reason_wins_when_both_arms_hold():
    """兩條臂同時成立時報連線那條——讀 log 的人才知道要去看額度而不是猜麥克風。"""
    h = HearingMonitor()
    h.voice("A", True, 0.0)
    h.note_stt_offline(True)
    assert h.reason(DEAF_VOICED_SECONDS) == REASON_STT_OFFLINE


# ══ 二、STT 連線層的健康訊號（沿用既有欄位，不另造推論）══════════════


def _stream(pool: STTPool, name: str, *, fails: int, session_started: bool):
    """直接建一條 SpeakerStream 並灌入連線狀態——不連網路。"""
    from meeting_host.stt import SpeakerStream
    s = SpeakerStream(name, "unused-key", pool.out)
    s._fails = fails
    s._session_started = session_started
    pool.streams[name] = s
    return s


def test_stream_is_not_offline_during_a_normal_idle_reconnect():
    """ElevenLabs 靜音 16 秒的閒置關閉：`_guard` 把 `_fails` 歸零後立刻重連。
    這是每次會議停頓都會發生的正常行為，不能被算成失聯。"""
    pool = STTPool("unused-key")
    _stream(pool, "A", fails=0, session_started=False)  # 重連途中
    assert pool.offline() is False


def test_stream_is_not_offline_after_a_single_transient_failure():
    """單次抖動（`_fails == 1`）不算——OFFLINE_FAILS=2 要求兩次獨立嘗試都失敗。"""
    pool = STTPool("unused-key")
    _stream(pool, "A", fails=1, session_started=False)
    assert pool.offline() is False


def test_pool_is_offline_when_every_stream_keeps_failing_the_handshake():
    """額度耗盡的形狀：每一條連線都連不上，且沒有任何一條 session 活著。"""
    pool = STTPool("unused-key")
    _stream(pool, "A", fails=OFFLINE_FAILS, session_started=False)
    _stream(pool, "B", fails=5, session_started=False)
    assert pool.offline() is True


def test_pool_is_not_offline_while_any_stream_still_has_a_live_session():
    """一個人的連線壞掉不代表主席聾了——其他人的話照樣聽得到，這時壓規則是誤鎖。"""
    pool = STTPool("unused-key")
    _stream(pool, "A", fails=5, session_started=False)
    _stream(pool, "B", fails=0, session_started=True)
    assert pool.offline() is False


def test_pool_with_no_streams_is_not_offline():
    """會議剛開始、沒有人出過聲 → 沒有證據就不下判斷。"""
    assert STTPool("unused-key").offline() is False


# ══ 三、fast_path：逐條規則的閘門 ═════════════════════════════════════


def _overtime_state() -> tuple[MeetingState, float]:
    """A 連講 238 秒（句間 gap 2s，鏈不會斷）→ 遠超 OVERTIME_SECONDS(180)。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    t = 0.0
    for _ in range(20):
        st.add(Utterance("A", "我再講一段攤位的規劃", t, t + 10.0))
        t += 12.0
    return st, 240.0


def _neglected_state() -> tuple[MeetingState, float]:
    """B 從頭到尾沒開口，now=400 → 400s ≥ NEGLECTED_SECONDS(300)。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "攤位那邊我覺得可以放兩張桌子", 390.0, 395.0))
    return st, 400.0


def _agenda_state() -> tuple[MeetingState, float]:
    """30 分鐘的會議走到 26:40，剩 200 秒 ≤ agenda_warn_seconds(30)=300。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.add(Utterance("A", "那報名表單我等等寄出去", 1590.0, 1595.0))
    return st, 1600.0


def _room_silence_state() -> tuple[MeetingState, float]:
    """全場 95 秒沒人講話 ≥ SILENCE_SECONDS(90)。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.add(Utterance("A", "我覺得先確認場地比較重要", 0.0, 5.0))
    return st, 100.0


SUPPRESSED_CASES = [
    ("發言超時", _overtime_state),
    ("有人被冷落", _neglected_state),
    ("全場沉默", _room_silence_state),
]


@pytest.mark.parametrize(("kind", "make"), SUPPRESSED_CASES + [("議程超時", _agenda_state)])
def test_rule_fires_normally_when_the_chair_can_hear(kind, make):
    """四條規則在 STT 健康時全部照常觸發（對照組）。"""
    st, now = make()
    assert kind in _kinds(fast_path.check(st, now, set(), deaf=False))


@pytest.mark.parametrize(("kind", "make"), SUPPRESSED_CASES)
def test_transcript_dependent_rules_are_suppressed_while_deaf(kind, make):
    """三條靠「逐字稿是新鮮的」量出來的規則，失聰時一句都不出。"""
    st, now = make()
    assert _kinds(fast_path.check(st, now, set(), deaf=True)) == []


def test_agenda_rule_still_fires_while_deaf():
    """「議程超時」只看時鐘（`duration_min * 60 - now`），完全不碰逐字稿——
    主席聽不見不代表議程沒在走，這句話在失聰時仍然是真的，所以不壓。"""
    st, now = _agenda_state()
    assert _kinds(fast_path.check(st, now, set(), deaf=True)) == ["議程超時"]


def test_deaf_suppressed_kinds_is_exactly_fast_kinds_minus_agenda():
    """壓制名單與四條快路規則的關係釘死：只放過「議程超時」那一條。"""
    assert fast_path.DEAF_SUPPRESSED_KINDS == fast_path.FAST_KINDS - {"議程超時"}


def test_default_deaf_argument_keeps_the_old_behaviour():
    """`deaf` 預設 False：沒改過的呼叫端（離線評分工具、既有測試）行為不變。"""
    st, now = _room_silence_state()
    assert _kinds(fast_path.check(st, now, set())) == ["全場沉默"]


def test_suppression_does_not_consume_the_claim_or_the_backoff_counter():
    """跟收尾閘門一樣，壓下的代價有上限：只過濾回傳值，`done` 與退避次數都不動，
    所以 STT 一活過來規則就能照常觸發——被擋掉的是延後，不是永久取消。"""
    st, now = _room_silence_state()
    done: set[tuple[str, str | None]] = set()

    assert fast_path.check(st, now, done, deaf=True) == []
    assert done == set()
    assert st.room_silence_hits == 0

    assert _kinds(fast_path.check(st, now, done, deaf=False)) == ["全場沉默"]


def test_deaf_and_closing_gates_compose():
    """兩個閘門互相獨立、可以同時成立，不會互相蓋掉。"""
    st, now = _agenda_state()
    assert _kinds(fast_path.check(st, now, set(), closing=True, deaf=True)) == []
    assert _kinds(fast_path.check(st, now, set(), closing=False, deaf=True)) == ["議程超時"]


# ══ 四、live 接線 ═════════════════════════════════════════════════════


def _deaf_session(now: float = 400.0) -> tuple[Session, RecordingChair]:
    """B 整場沒開口、最後一句在 t=90 → 「有人被冷落」與「全場沉默」都成立。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "攤位那邊我覺得可以放兩張桌子", 85.0, 90.0))
    session = Session(st)
    chair = RecordingChair()
    session.chair = chair
    session.t0 = time.perf_counter() - now
    return session, chair


def test_fast_tick_fires_when_the_chair_can_hear():
    """對照組：耳朵好的時候，同一個狀態照常排入介入，而且一筆 `hearing` 都不發。

    後半條是「不誤鎖」在事件層的形式：健康的會議事件檔裡不該憑空多出這種事件，
    否則觀戰 UI 的告示會閃一下，會後記錄也會多出沒發生過的故障。
    """
    session, chair = _deaf_session()
    for _ in range(3):
        session._fast_tick(None)
    assert [iv.kind for iv in chair.requests] != []
    assert [e for e in session.events if e.kind == "hearing"] == []


def test_fast_tick_stays_quiet_while_deaf():
    """同一個狀態，失聰時不排入任何介入；心跳（fast_timer）照發。"""
    session, chair = _deaf_session()
    session.hearing.note_stt_offline(True)
    session._fast_tick(None)
    assert chair.requests == []
    kinds = [e.kind for e in session.events]
    assert "queued" not in kinds
    assert "fast_timer" in kinds


def test_fast_tick_emits_hearing_only_on_transitions():
    """`hearing` 是邊緣事件，不是每秒心跳——連跑三個 tick 只會有一筆。"""
    session, _ = _deaf_session()
    session.hearing.note_stt_offline(True)
    for _ in range(3):
        session._fast_tick(None)
    hearing_events = [e for e in session.events if e.kind == "hearing"]
    assert len(hearing_events) == 1
    assert hearing_events[0].data["ok"] is False
    assert hearing_events[0].data["reason"] == REASON_STT_OFFLINE

    # 活過來 → 再一筆，ok=True
    session.hearing.note_stt_offline(False)
    session._fast_tick(None)
    hearing_events = [e for e in session.events if e.kind == "hearing"]
    assert len(hearing_events) == 2
    assert hearing_events[1].data == {"ok": True, "reason": "", "voiced_seconds": 0.0}


def test_note_voice_emits_the_voice_event_and_feeds_the_monitor():
    """`bot.on_voice_activity` 的接線點：既有的 `voice` 事件契約不變，
    同一顆訊號同時餵給失聰偵測（兩件事綁在一個方法裡，見 Session.note_voice）。"""
    session, _ = _deaf_session(now=0.0)
    session.note_voice("A", True)
    voice_events = [e for e in session.events if e.kind == "voice"]
    assert [e.data for e in voice_events] == [{"speaker": "A", "active": True}]
    assert session.hearing.voiced_seconds(session.now) > 0.0


def test_consume_clears_the_accumulator_on_a_real_transcript():
    """`Session.consume` 收到 Utterance 時呼叫 `hearing.heard()`——這是恢復的唯一路徑。"""
    import asyncio

    class _Pool:
        async def utterances(self):
            yield Utterance("A", "回來了", 500.0, 502.0)

    session, _ = _deaf_session(now=500.0)
    session.hearing.voice("A", True, 0.0)
    assert session.hearing.deaf(session.now) is True
    asyncio.run(session.consume(_Pool()))
    assert session.hearing.deaf(session.now) is False


def test_escalate_drops_a_suppressed_soft_intervention_while_deaf():
    """軟插入在等停頓的 15 秒裡 STT 死掉 → 升級路徑必須把它作廢，
    而不是拿失聰期間灌水的沉默秒數把它升級成硬打斷。"""
    st, now = _neglected_state()
    iv = Intervention("有人被冷落", "B", "B，你怎麼看？", False, 0, now - 15.0)
    assert escalate_with_current_facts(st, now, 1, iv, deaf=False) is not None
    assert escalate_with_current_facts(st, now, 1, iv, deaf=True) is None


def test_escalate_still_upgrades_the_agenda_rule_while_deaf():
    """「議程超時」不在壓制名單裡：失聰時它的前提仍然成立，升級路徑照走。"""
    st, now = _agenda_state()
    iv = Intervention("議程超時", None, "只剩 3 分鐘，我們往結論收。", False, 0, now - 10.0)
    upgraded = escalate_with_current_facts(st, now, 1, iv, deaf=True)
    assert upgraded is not None and upgraded.hard is True


# ══ 五、慢路 ══════════════════════════════════════════════════════════


def test_slow_gate_blocks_an_in_flight_score_when_stt_died_during_the_call():
    """慢路在失聰期間結構上就不會產生新判斷（`should_score` 要求有新的
    utterance）；這一關真正擋到的只有 in-flight 那一種——`score()` 發動時
    STT 還活著，那幾秒往返之間才死掉。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1}
    assert live.slow_gate(st, 100.0, r) == (True, "")
    assert live.slow_gate(st, 100.0, r, deaf=True) == (False, "失聰")


def test_slow_recheck_blocks_when_stt_died_during_the_phrase_call():
    """第二關（TOCTOU 重驗）同理：話術那幾秒之間 STT 才死掉。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "utterance": "我們先回到黑客松籌備。"}
    assert live.slow_recheck_admissible(st, 100.0, r) == (True, "")
    assert live.slow_recheck_admissible(st, 100.0, r, deaf=True) == (False, "失聰(話術後)")


def test_deaf_after_decision_counts_as_blocked_not_withheld():
    """`失聰(話術後)` 必須在 SLOW_BLOCKED_AFTER_DECISION 裡：主席已經決定要開口，
    是後來的世界擋掉的，觀戰 UI 顯示成「忍住」會把失敗說成克制。
    第一關的 `失聰` 相反——那是決定之前就擋下，不進這份清單。"""
    assert "失聰(話術後)" in live.SLOW_BLOCKED_AFTER_DECISION
    assert "失聰" not in live.SLOW_BLOCKED_AFTER_DECISION
