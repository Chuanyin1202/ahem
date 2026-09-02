"""階段偵測器的遲滯規則與 prompt 組裝。不呼叫 LLM。"""
from meeting_host import phase as ph
from meeting_host.state import MeetingState, Utterance


def r(p, c=0.9):
    return {"phase": p, "confidence": c, "reason": ""}


def test_one_reading_does_not_switch():
    d = ph.PhaseDetector(current="發散期")
    assert d.observe(r("呻吟區"), 60.0) is None
    assert d.current == "發散期"


def test_two_consecutive_agreeing_readings_switch():
    d = ph.PhaseDetector(current="發散期")
    assert d.observe(r("呻吟區"), 60.0) is None
    assert d.observe(r("呻吟區"), 120.0) == "呻吟區"
    assert d.current == "呻吟區"


def test_disagreeing_reading_resets_streak():
    d = ph.PhaseDetector(current="發散期")
    d.observe(r("呻吟區"), 60.0)
    d.observe(r("收斂期"), 120.0)
    assert d.observe(r("呻吟區"), 180.0) is None   # 重新從 1 算


def test_low_confidence_is_ignored():
    d = ph.PhaseDetector(current="發散期")
    d.observe(r("呻吟區", 0.9), 60.0)
    assert d.observe(r("呻吟區", 0.3), 120.0) is None
    assert d.current == "發散期"


def test_dwell_blocks_immediate_second_switch():
    d = ph.PhaseDetector(current="發散期")
    d.observe(r("呻吟區"), 60.0); d.observe(r("呻吟區"), 120.0)
    d.observe(r("收斂期"), 180.0)
    assert d.observe(r("收斂期"), 200.0) is None        # 切換後 80 秒內
    assert d.observe(r("收斂期"), 260.0) == "收斂期"     # 過了 MIN_DWELL


def test_reading_equal_to_current_clears_pending():
    d = ph.PhaseDetector(current="發散期")
    d.observe(r("呻吟區"), 60.0)
    d.observe(r("發散期"), 120.0)
    assert d.pending is None and d.streak == 0


def test_prompt_uses_window_and_structural_signals():
    st = MeetingState(topic="上線排程", duration_min=30, participants=["甲", "乙"])
    st.add(Utterance("甲", "很早以前的話", 10.0, 12.0))
    st.add(Utterance("甲", "報表要不要拆期", 500.0, 503.0))
    st.add(Utterance("乙", "拆吧，先做站內通知", 505.0, 508.0))
    p = ph.build_prompt(st, 600.0, "發散期", ["無", "重複", "重複", "僵局"])
    assert "很早以前的話" not in p             # 超出 150 秒窗口
    assert "報表要不要拆期" in p and "2 人參與" in p and "交替 1 次" in p
    assert "'重複': 2" in p and "'僵局': 1" in p
    assert "目前登記的階段：發散期" in p


def test_not_judgeable_with_one_speaker_or_too_few_turns():
    st = MeetingState(topic="t", duration_min=30, participants=["甲", "乙"])
    st.add(Utterance("甲", "一", 500.0, 501.0)); st.add(Utterance("甲", "二", 505.0, 506.0)); st.add(Utterance("甲", "三", 510.0, 511.0))
    assert ph.judgeable(st, 600.0) == "窗口內只有一個人說話"
    st2 = MeetingState(topic="t", duration_min=30, participants=["甲", "乙"])
    st2.add(Utterance("甲", "一", 500.0, 501.0)); st2.add(Utterance("乙", "二", 505.0, 506.0))
    assert ph.judgeable(st2, 600.0) == "窗口內只有 2 次發言"
    st2.add(Utterance("甲", "三", 510.0, 511.0))
    assert ph.judgeable(st2, 600.0) is None
    assert ph.judgeable(st2, 900.0) == "窗口內只有 0 次發言"   # 全部落在窗口外
