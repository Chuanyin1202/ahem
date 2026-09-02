"""迴歸：會議收尾時，快路四條規則不再開口（T27）。

上一張單（4dee0e0）只把收尾閘門接在慢路的 `slow_result_admissible()` 上，
快路完全沒有保護。同一份真實會議事件檔
（`experiments/holdout/2026-08-29-two-person/meeting.events.jsonl`，只讀）
的尾段量到的數字是：最後一句話結束於 t=778.3，全場沉默在會議結束的那一刻
（最後一個事件 t=867.2）已經爬到 87.7 秒，門檻 `SILENCE_SECONDS = 90.0`——
差 2.3 秒。這場沒踩到只是運氣，會議多開十幾秒就會在大家講完拜拜之後
冒出一句「現場安靜了一陣子，要不要有人先分享一下目前的想法？」。

回放台直接複用 `test_regression_overtime_no_repeat._replay`（同一份 live.py
資料流），只多開兩個旗標：`closing_gate`（有／沒有閘門的對照）與 `joined_at`
（補回 `ensure_participant` 的加入時刻，見該檔案 `_load_joined_at`）。
"""
import pytest

from pathlib import Path as _P
_EVENTS_PATH = _P(__file__).parents[2] / "experiments" / "holdout" / "2026-08-29-two-person" / "meeting.events.jsonl"
pytestmark = pytest.mark.skipif(not _EVENTS_PATH.exists(), reason="需要 experiments/holdout/2026-08-29-two-person 的真實會議資料（不在公開 repo，見 experiments/holdout/README.md）")

from meeting_host import fast_path, live

from .test_regression_overtime_no_repeat import (
    _load_joined_at,
    _load_utterances,
    _replay,
)

# 詞表第一次出現在逐字稿裡的時刻（Alex Huang「……各位，拜拜。」t=634.3–641.0）。
# 驗收要求「收尾時段 t≥634 快路不觸發」就以這個為界。
FIRST_MARKER_T = 634.0
# 閘門實際轉為 True 的時刻：CLOSING_MIN_HITS=2，第二句道別（t=642.0–645.7）
# commit 進逐字稿之後的下一個 tick。
GATE_ON_T = 646.0
# 事件檔最後一個事件的時刻——這場會議真正結束的地方
REAL_MEETING_END = 867.2
# 「多開十幾秒」的反向驗證用：把回放延長到 880 秒，其餘一個字都不改
EXTENDED_END = 880.0
LAST_UTTERANCE_END = 778.3  # 最後一句話結束的時刻（事件檔實測 778.299…）


@pytest.fixture(scope="module")
def utterances():
    return _load_utterances()


@pytest.fixture(scope="module")
def joined_at():
    return _load_joined_at()


def _late(fired):
    return [f for f in fired if f[0] >= FIRST_MARKER_T]


# ── 驗收 1：真實會議收尾段，快路零觸發 ────────────────────────────────


def test_no_fast_path_trigger_after_closing_starts_on_the_real_meeting(
        utterances, joined_at):
    """接上閘門重放整場（含尾巴那段沉默）：t≥634 之後快路一次都不出聲。"""
    fired, _ = _replay(utterances, end=REAL_MEETING_END,
                       closing_gate=True, joined_at=joined_at)
    assert _late(fired) == []


def test_the_gate_is_actually_on_through_the_whole_closing_stretch(
        utterances, joined_at):
    """而且不是「規則本來就不成立」蒙混過去——閘門在整個收尾段都是 True。

    順便把「這場只差幾秒」釘住：t=866 時全場沉默 87.7 秒，最後一個事件
    （t=867.2）時 88.9 秒，門檻 `SILENCE_SECONDS = 90.0`。
    """
    from meeting_host.state import MeetingState

    st = MeetingState(topic="黑客松籌備", duration_min=30,
                      participants=["Alex Huang", "MiMi"])
    st.joined_at.update(joined_at)
    pending = list(utterances)
    on = []
    now = 0.0
    while now <= EXTENDED_END:
        while True:
            ready = [u for u in pending if u.end <= now]
            if not ready:
                break
            u = min(ready, key=lambda x: x.end)
            pending.remove(u)
            st.add(u)
            st.utterances.sort(key=lambda x: x.start)
        on.append((now, live.meeting_is_closing_for_rules(st, now)))
        now += 1.0

    first_on = next(t for t, c in on if c)
    assert first_on == pytest.approx(GATE_ON_T)
    assert all(c for t, c in on if t >= GATE_ON_T)   # 一路 True 到回放結束
    assert not any(c for t, c in on if t < GATE_ON_T)  # 中段一次都沒亮起

    assert 866.0 - LAST_UTTERANCE_END == pytest.approx(87.7, abs=0.2)
    room_silence_at_end = REAL_MEETING_END - LAST_UTTERANCE_END
    assert room_silence_at_end == pytest.approx(88.9, abs=0.2)
    assert room_silence_at_end < fast_path.SILENCE_SECONDS  # 差不到 1.5 秒


# ── 驗收 2：反向驗證——沒有閘門時它真的會觸發 ────────────────────────


def test_room_silence_fires_during_closing_without_the_gate(utterances, joined_at):
    """把同一份序列延長到 880 秒（＝這場會議多開十幾秒），不改任何門檻常數。

    沒有閘門：全場沉默在 t=869.0 觸發——那時房間最後一句話是 t=778.3，
    在那之前的整段逐字稿是「拜拜／再見／下線／結束通話」。這就是要擋的那一句。
    """
    fired, _ = _replay(utterances, end=EXTENDED_END,
                       closing_gate=False, joined_at=joined_at)
    late = _late(fired)
    assert [(kind, target) for _, kind, target in late] == [("全場沉默", None)]
    assert late[0][0] == pytest.approx(869.0, abs=1.0)


def test_room_silence_is_blocked_during_closing_with_the_gate(utterances, joined_at):
    """同一條延長序列，只多開閘門：那一次觸發消失。"""
    fired, _ = _replay(utterances, end=EXTENDED_END,
                       closing_gate=True, joined_at=joined_at)
    assert _late(fired) == []


def test_now_anchored_closing_check_would_not_have_blocked_it(utterances, joined_at):
    """為什麼快路要用 `meeting_is_closing_for_rules` 而不是直接用
    `meeting_is_closing(st, now)`：後者在觸發的那一刻早就失效了。

    兩個常數都是 90.0（`CLOSING_LOOKBACK_SECONDS` 與 `SILENCE_SECONDS`），
    道別詞變舊與沉默變長走的是同一根時間軸，兩個窗互相抵消——實測以 now
    為錨的判定在 t=836 就變 False，而全場沉默 t=869 才成立。
    """
    from meeting_host.state import MeetingState

    st = MeetingState(topic="黑客松籌備", duration_min=30,
                      participants=["Alex Huang", "MiMi"])
    st.joined_at.update(joined_at)
    for u in utterances:
        st.add(u)

    fire_t = 869.0
    assert live.meeting_is_closing(st, fire_t) is False        # 舊判定：擋不到
    assert live.meeting_is_closing_for_rules(st, fire_t) is True  # 凍結錨點：擋得到
    # 失效的那一刻（t=837 起 False），比觸發時刻早了 30 秒以上
    assert live.meeting_is_closing(st, 836.0) is True
    assert live.meeting_is_closing(st, 837.0) is False


# ── 驗收 3：中段行為不變（真實資料側；構造側見 test_fast_path_closing_gate.py）──


def test_mid_meeting_behaviour_is_identical_with_and_without_the_gate(
        utterances, joined_at):
    """t<634 的快路觸發清單，有閘門與沒閘門逐項相同。

    這場在 T15 之後本來就不觸發任何規則，所以這條只證明「沒有誤鎖」，
    不足以證明「規則照常成立時會觸發」——那部分由構造測試負責。
    """
    with_gate, _ = _replay(utterances, end=EXTENDED_END,
                           closing_gate=True, joined_at=joined_at)
    without_gate, _ = _replay(utterances, end=EXTENDED_END,
                              closing_gate=False, joined_at=joined_at)
    assert [f for f in with_gate if f[0] < FIRST_MARKER_T] == \
           [f for f in without_gate if f[0] < FIRST_MARKER_T]


def test_pre_gate_baseline_of_this_meeting_is_a_single_room_silence(
        utterances, joined_at):
    """基準數字：補上 joined_at 之後，整場（延長到 880s）只有尾段那一次
    全場沉默——確認上面兩條對照的差異來源只有收尾那一次，沒有別的雜訊。"""
    fired, _ = _replay(utterances, end=EXTENDED_END,
                       closing_gate=False, joined_at=joined_at)
    assert [(kind, target) for _, kind, target in fired] == [("全場沉默", None)]
