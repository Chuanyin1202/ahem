"""「議程超時」門檻隨會議長度縮放（T24 任務一）。

原本 `AGENDA_WARN_SECONDS = 300.0` 是絕對值：宣告一場 5 分鐘的會議時，
`remaining` 從第 0 秒起就 ≤ 300，規則從開場就成立——主席會在剛開始沒多久
講一句「議程只剩 5.0 分鐘」。黑客松 demo 只有幾分鐘，這正是會被看見的情境。

這裡的硬要求是「正常長度會議行為完全不變」：≥ 30 分鐘的會議門檻仍是 300 秒，
逐字與改動前相同。只有短於 30 分鐘的會議才會走到比例那一側。
"""
import pytest

from meeting_host import fast_path
from meeting_host.state import MeetingState

MIN = 60.0


def _agenda_triggers(duration_min: float, now: float):
    """在 `now` 這一刻，一場 `duration_min` 分鐘的會議會不會提醒議程收尾。

    參與者留空：只留「議程超時」一條規則有資料可判，其餘規則（發言超時／
    有人被冷落／全場沉默）都不會產生 Trigger，斷言才只反映這一條。
    """
    st = MeetingState(topic="t", duration_min=duration_min, participants=[])
    return [t for t in fast_path.check(st, now, set()) if t.kind == "議程超時"]


# ── 硬要求：正常長度會議完全不變 ──────────────────────────────
@pytest.mark.parametrize("duration_min", [30, 45, 60, 90, 120])
def test_normal_length_meetings_keep_the_original_300s_threshold(duration_min):
    """≥ 30 分鐘的會議，門檻仍是原本的 AGENDA_WARN_SECONDS（300 秒）。"""
    assert fast_path.agenda_warn_seconds(duration_min) == fast_path.AGENDA_WARN_SECONDS


def test_sixty_minute_meeting_warns_exactly_at_five_minutes_left():
    """60 分鐘會議：剩 5 分 00 秒的那一刻提醒，剩 5 分 01 秒還不提醒。"""
    assert not _agenda_triggers(60, now=55 * MIN - 1.0)   # 剩 301 秒
    assert _agenda_triggers(60, now=55 * MIN)             # 剩 300 秒


def test_sixty_minute_meeting_stays_quiet_at_the_start():
    """60 分鐘會議開場不提醒——這條在改動前後都成立，作為對照組。"""
    assert not _agenda_triggers(60, now=0.0)


# ── 短會議 ────────────────────────────────────────────────
def test_five_minute_meeting_does_not_warn_at_the_start():
    """5 分鐘會議：門檻縮成 50 秒，開場（以及前 4 分鐘）都不提醒。

    改動前 `remaining` 從第 0 秒起就 ≤ 300，這裡的第一個斷言會失敗。
    """
    assert fast_path.agenda_warn_seconds(5) == pytest.approx(50.0)
    assert not _agenda_triggers(5, now=0.0)
    assert not _agenda_triggers(5, now=4 * MIN)           # 剩 60 秒，還沒到
    assert _agenda_triggers(5, now=5 * MIN - 50.0)        # 剩 50 秒，提醒


def test_two_minute_meeting_warns_only_in_the_last_twenty_seconds():
    """極短會議（2 分鐘）：門檻 20 秒，仍遠大於快路 1 秒的 tick。"""
    assert fast_path.agenda_warn_seconds(2) == pytest.approx(20.0)
    assert not _agenda_triggers(2, now=99.0)              # 剩 21 秒
    assert _agenda_triggers(2, now=100.0)                 # 剩 20 秒


# ── 邊界 ──────────────────────────────────────────────────
def test_zero_duration_never_warns():
    """duration 為 0（沒設定議程長度）→ 門檻 0，且 remaining 恆 ≤ 0，整場不觸發。"""
    assert fast_path.agenda_warn_seconds(0) == 0.0
    assert not _agenda_triggers(0, now=0.0)
    assert not _agenda_triggers(0, now=60.0)


def test_negative_remaining_still_suppressed():
    """已經超時（remaining < 0）不觸發——`0 < remaining` 的語意不能被這次改動動到。"""
    st = MeetingState(topic="t", duration_min=30, participants=[])
    assert st.remaining_seconds(now=40 * MIN) < 0
    assert not _agenda_triggers(30, now=40 * MIN)
    assert not _agenda_triggers(5, now=10 * MIN)


def test_threshold_never_exceeds_the_meeting_itself():
    """任何長度的會議，門檻都不會大過會議長度——大過就等於開場即提醒。"""
    for duration_min in [1, 2, 5, 10, 15, 25, 29, 30, 31, 60]:
        assert fast_path.agenda_warn_seconds(duration_min) < duration_min * MIN
