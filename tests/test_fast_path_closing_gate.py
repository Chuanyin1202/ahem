"""T27：收尾閘門延伸到快路——逐條規則的構造測試。

真實會議事件檔的回放證明見 `tests/harness/test_regression_closing_gate_fast_path.py`。
那場資料在 T15 之後本來就不觸發任何快路規則，所以「有閘門也照樣觸發」這件事
只能靠這裡構造出來的場景證明：每一條規則各給一個「規則成立且會議沒在收尾」
的狀態，先確認它會觸發，再確認同一個狀態在收尾時被壓住。

四條全部壓住的逐條理由寫在 `fast_path.CLOSING_SUPPRESSED_KINDS` 的註解——
共同點是每條規則的話術都預設「會議還要繼續」。
"""
import time

import pytest

from meeting_host import fast_path, live
from meeting_host.live import Session, escalate_with_current_facts, meeting_is_closing_for_rules
from meeting_host.speaker import Intervention
from meeting_host.state import MeetingState, Utterance


class RecordingChair:
    """只記錄 request()，不節流——沿用 test_live_wiring.py 同名替身的作法。"""

    def __init__(self):
        self.requests: list[Intervention] = []

    def request(self, iv: Intervention) -> bool:
        self.requests.append(iv)
        return True


def _kinds(triggers):
    return [t.kind for t in triggers]


# ── 四條規則各自的「成立」狀態（都沒有任何道別詞）────────────────────


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


CASES = [
    ("發言超時", _overtime_state),
    ("有人被冷落", _neglected_state),
    ("議程超時", _agenda_state),
    ("全場沉默", _room_silence_state),
]


@pytest.mark.parametrize(("kind", "make"), CASES)
def test_rule_still_fires_when_the_meeting_is_not_closing(kind, make):
    """驗收 3：規則成立且會議沒在收尾 → 照常觸發，而且閘門判定是 False
    （不是靠傳 closing=False 蒙混，是這份逐字稿真的沒有收尾跡象）。"""
    st, now = make()
    assert meeting_is_closing_for_rules(st, now) is False
    triggers = fast_path.check(st, now, set(),
                               closing=meeting_is_closing_for_rules(st, now))
    assert kind in _kinds(triggers)


@pytest.mark.parametrize(("kind", "make"), CASES)
def test_rule_is_suppressed_while_the_meeting_is_closing(kind, make):
    """同一個狀態、同一條規則，收尾時一句都不出。"""
    st, now = make()
    assert fast_path.check(st, now, set(), closing=True) == []


def test_default_closing_argument_keeps_the_old_behaviour():
    """`closing` 預設 False：沒改過的呼叫端（離線評分工具、既有測試）行為不變。"""
    st, now = _room_silence_state()
    assert _kinds(fast_path.check(st, now, set())) == ["全場沉默"]


# ── 壓下的代價有上限：是延後，不是永久取消 ─────────────────────────


def test_suppression_does_not_consume_the_claim_or_the_backoff_counter():
    """閘門只過濾 check() 的回傳值：`done` 不會被寫進去、退避次數不會遞增，
    所以收尾判定一旦解除（誤判的情況），規則照樣能觸發。"""
    st, now = _room_silence_state()
    done: set[tuple[str, str | None]] = set()

    assert fast_path.check(st, now, done, closing=True) == []
    assert done == set()                 # claim 沒被吃掉
    assert st.room_silence_hits == 0     # 退避次數沒動

    assert _kinds(fast_path.check(st, now, done, closing=False)) == ["全場沉默"]


def test_agenda_warning_is_only_delayed_by_the_gate():
    """議程超時整場只觸發一次，被壓下時尤其不能被吃掉——閘門解除後照樣講。"""
    st, now = _agenda_state()
    assert fast_path.check(st, now, set(), closing=True) == []
    assert _kinds(fast_path.check(st, now, set(), closing=False)) == ["議程超時"]


# ── meeting_is_closing_for_rules：錨點凍結在「最後有人講話的那一刻」 ──


def _farewell_state() -> MeetingState:
    """道別段：兩句含詞表的話，最後一句話結束於 t=305。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "好，那今天就到這邊，各位拜拜。", 295.0, 300.0))
    st.add(Utterance("B", "好，再見，我先下線囉。", 301.0, 305.0))
    return st


def test_closing_for_rules_stays_true_through_silence():
    """關鍵差異：道別之後沒有人講話，會議不會因為「安靜太久」變回進行中。"""
    st = _farewell_state()
    assert live.meeting_is_closing(st, 305.0) is True
    # 以 now 為錨的判定在道別詞滑出 90 秒窗之後失效——正是全場沉默要成立的時候
    assert live.meeting_is_closing(st, 400.0) is False
    assert meeting_is_closing_for_rules(st, 400.0) is True
    assert meeting_is_closing_for_rules(st, 4000.0) is True


def test_closing_for_rules_releases_once_the_room_talks_again():
    """房間重新開口，而且新的談話把道別詞推出 90 秒回看窗 → 閘門解除。"""
    st = _farewell_state()
    st.add(Utterance("A", "等一下，報名表單那件事還沒講完。", 500.0, 505.0))
    assert meeting_is_closing_for_rules(st, 506.0) is False
    # 快路規則跟著恢復：全場沉默在新的沉默達到門檻時照樣觸發
    assert _kinds(fast_path.check(st, 600.0, set(),
                                  closing=meeting_is_closing_for_rules(st, 600.0))) \
        == ["全場沉默"]


def test_closing_for_rules_empty_transcript_is_false():
    """一句話都還沒有 → 沒有錨點可凍結，不能把空會議當成收尾。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    assert meeting_is_closing_for_rules(st, 500.0) is False
    # 開場等人的那段規則照常會觸發（joined_at 未設 → 沉默從 0 起算）
    assert set(_kinds(fast_path.check(st, 500.0, set(),
                                      closing=meeting_is_closing_for_rules(st, 500.0)))) \
        == {"全場沉默", "有人被冷落"}


def test_closing_for_rules_does_not_lock_mid_meeting_wrapup_words():
    """詞表以外的「先這樣／離開這個話題」不算收尾——沿用慢路那一關的判準，
    快路不能因為換了錨點就變得更容易誤鎖。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "好，那這件事先這樣，我們往下一題。", 300.0, 303.0))
    st.add(Utterance("B", "對，先離開這個話題，等資料回來再談。", 304.0, 308.0))
    assert meeting_is_closing_for_rules(st, 400.0) is False


# ── 接線：兩個呼叫端都要真的把閘門帶進 fast_path.check ─────────────────


def _closing_session() -> tuple[Session, RecordingChair, MeetingState]:
    """A 在 t=715／720 講了兩句道別，B 整場沒開口（now=800 → 沉默 800s）。

    這是收尾段最典型的形狀：規則（有人被冷落）確實成立，話術卻是
    「你對這個提案的看法是什麼？」——會把一個已經道別的人拉回散場中的會議。
    """
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "好，那我們今天就到這裡，拜拜。", 710.0, 715.0))
    st.add(Utterance("A", "嗯，再見，我先下線了。", 716.0, 720.0))
    session = Session(st)
    chair = RecordingChair()
    session.chair = chair
    session.t0 = time.perf_counter() - 800.0
    return session, chair, st


def test_fast_tick_stays_quiet_while_the_meeting_is_closing():
    session, chair, _ = _closing_session()
    session._fast_tick(None)
    assert chair.requests == []
    assert "queued" not in [e.kind for e in session.events]
    assert "fast_timer" in [e.kind for e in session.events]  # 心跳照發


def test_fast_tick_still_speaks_when_the_same_rule_holds_without_farewells():
    """反向保險：同一個規則、同樣的時間軸，只把兩句道別換成一般發言 → 照常排入。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "好，那我們今天就到這裡，先講攤位。", 710.0, 715.0))
    st.add(Utterance("A", "嗯，我先把表單填一填。", 716.0, 720.0))
    session = Session(st)
    chair = RecordingChair()
    session.chair = chair
    session.t0 = time.perf_counter() - 800.0

    session._fast_tick(None)
    assert [iv.kind for iv in chair.requests] == ["有人被冷落"]
    assert chair.requests[0].target == "B"


def test_escalation_discards_a_fast_intervention_that_reaches_closing():
    """軟插入等停頓的 15 秒之間房間開始道別：升級時必須作廢，不能升成硬打斷。"""
    st = _farewell_state()
    st.add(Utterance("A", "我先掛了，拜拜。", 306.0, 308.0))
    iv = Intervention(kind="有人被冷落", target="B", text="B，你有一陣子沒說話了……",
                      hard=False, revision=0, created_at=300.0)
    assert escalate_with_current_facts(st, 320.0, 0, iv) is None


def test_escalation_still_regenerates_when_not_closing():
    """沒在收尾時升級路徑完全不變：規則仍成立 → 用當下事實重生、升級成硬打斷。"""
    st, now = _neglected_state()
    iv = Intervention(kind="有人被冷落", target="B", text="舊句子",
                      hard=False, revision=0, created_at=now - 15.0)
    out = escalate_with_current_facts(st, now, 0, iv)
    assert out is not None
    assert out.hard is True
    assert out.text != "舊句子"
