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


def test_prompt_counts_chair_interventions_in_window_and_states_the_exclusion():
    st = MeetingState(topic="t", duration_min=30, participants=["甲", "乙"])
    st.add(Utterance("甲", "一", 500.0, 501.0)); st.add(Utterance("乙", "二", 505.0, 506.0))
    st.interventions = [100.0, 520.0, 590.0]     # 只有後兩次落在 600-150=450 之後
    p = ph.build_prompt(st, 600.0, "發散期", [])
    assert "主席在這段窗口介入了 2 次" in p
    assert "衝突的對象必須是議題本身才算呻吟區" in p


# ── watch_phase 整合：stub 掉 LLM，走真的 Session／emit ─────────────────
import asyncio
import sys
from pathlib import Path as _Path
from meeting_host.live import Session

sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "experiments"))
import phase_replay  # noqa: E402


class _FakeChair:
    pending = None
    playing = None


def _two_speaker_state():
    st = MeetingState(topic="t", duration_min=30, participants=["甲", "乙"])
    for i, (who, txt) in enumerate([("甲", "一"), ("乙", "二"), ("甲", "三"), ("乙", "四")]):
        st.add(Utterance(who, txt, 1.0 + i, 1.5 + i))
    return st


def _run_watch_phase(session, monkeypatch, readings, ticks):
    """跑 watch_phase 直到消耗掉 `readings`（每 tick 一筆），回傳 emit 的事件。"""
    it = iter(readings)
    monkeypatch.setattr(ph, "PHASE_TICK_SECONDS", 0.001)
    monkeypatch.setattr(ph, "MIN_DWELL_SECONDS", 0.0)
    monkeypatch.setattr(ph, "judge", lambda st, now, cur, types: next(it))
    monkeypatch.setattr(ph, "WINDOW_SECONDS", 10_000.0)   # 測試的 now 很小，窗口要蓋到所有發言
    session.chair = _FakeChair()
    got = []
    session.subscribers.append(lambda e: got.append(e))

    async def go():
        task = asyncio.create_task(session.watch_phase())
        # 以時間為準等到收齊，不用固定次數：機器負載高時固定次數會等不到（曾間歇失敗）。
        deadline = asyncio.get_running_loop().time() + 5.0
        while sum(1 for e in got if e.kind == "phase_suggestion") < ticks:
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.002)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    return got


def test_suggest_mode_emits_suggestions_but_never_changes_phase(monkeypatch):
    s = Session(_two_speaker_state(), auto_phase="suggest")
    got = _run_watch_phase(s, monkeypatch, [r("呻吟區"), r("呻吟區"), r("呻吟區")], ticks=3)
    sugg = [e for e in got if e.kind == "phase_suggestion"]
    assert len(sugg) == 3 and all(e.data["applied"] is False for e in sugg)
    assert not [e for e in got if e.kind == "phase"]
    assert s.phase == "發散期"
    assert sugg[1].data["phase"] == "呻吟區" and sugg[1].data["current"] == "發散期"


def test_apply_mode_switches_after_two_readings_and_emits_phase(monkeypatch):
    s = Session(_two_speaker_state(), auto_phase="apply")
    got = _run_watch_phase(s, monkeypatch, [r("呻吟區"), r("呻吟區")], ticks=2)
    phases = [e for e in got if e.kind == "phase"]
    assert len(phases) == 1 and phases[0].data == {"phase": "呻吟區", "source": "auto"}
    assert s.phase == "呻吟區"
    assert [e.data["applied"] for e in got if e.kind == "phase_suggestion"] == [False, True]


def test_manual_switch_goes_through_set_phase_and_emits_manual_source():
    s = Session(_two_speaker_state())
    got = []; s.subscribers.append(lambda e: got.append(e))
    s.set_phase("收斂期", "manual"); s.set_phase("收斂期", "manual")   # 第二次沒變，不 emit
    assert [e.data for e in got if e.kind == "phase"] == [{"phase": "收斂期", "source": "manual"}]


def test_watch_phase_off_by_default():
    assert Session(_two_speaker_state()).auto_phase is None


# ── 真值計分（純函式）──
def test_score_against_truth_reports_hits_latency_and_false_switches():
    readings = [{"t": t, "phase": p, "confidence": 0.9, "reason": ""} for t, p in
                [(60, "發散期"), (120, "發散期"), (180, "呻吟區"), (240, "呻吟區"), (300, "收斂期"), (360, "收斂期")]]
    switches = [(240.0, "呻吟區"), (360.0, "收斂期")]
    truth = [{"phase": "發散期", "range_seconds": [0, 150]},
             {"phase": "呻吟區", "range_seconds": [150, 280]},
             {"phase": "收斂期", "range_seconds": [280, 400]}]
    rep = phase_replay.score_against_truth(readings, switches, truth, "發散期")
    assert rep["hits"] == 3 and rep["false_switches"] == []
    lat = [w["latency_s"] for w in rep["windows"]]
    assert lat == [0.0, 90.0, 80.0]
    assert [w["majority"] for w in rep["windows"]] == ["發散期", "呻吟區", "收斂期"]


def test_score_against_truth_flags_a_switch_that_contradicts_truth():
    readings = []
    truth = [{"phase": "發散期", "range_seconds": [0, 300]}]
    rep = phase_replay.score_against_truth(readings, [(120.0, "收斂期")], truth, "發散期")
    assert rep["hits"] == 0 and rep["false_switches"] == [(120.0, "收斂期")]


# ── 風格檔位 ──
def test_style_none_changes_nothing_and_named_style_sets_only_its_keys(monkeypatch):
    from meeting_host import style, fast_path
    before = style.defaults()
    assert style.apply(None) == {} and style.defaults() == before
    applied = style.apply("strict")
    assert applied["OVERTIME_SECONDS"] == 120.0 and fast_path.OVERTIME_SECONDS == 120.0
    assert fast_path.AGENDA_WARN_RATIO == before["AGENDA_WARN_RATIO"]     # strict 沒動這個
    for k, v in before.items():
        setattr(fast_path, k, v)      # 還原，別影響其他測試
    import pytest
    with pytest.raises(ValueError):
        style.apply("angry")


def test_style_switch_does_not_keep_previous_profile_values():
    from meeting_host import style, fast_path
    before = style.defaults()
    try:
        style.apply("efficient")
        assert fast_path.AGENDA_WARN_RATIO == 1.0 / 4.0
        style.apply("strict")
        assert fast_path.AGENDA_WARN_RATIO == style._BASE_DEFAULTS["AGENDA_WARN_RATIO"]
    finally:
        for key, value in before.items():
            setattr(fast_path, key, value)


def test_style_profiles_are_ordered_from_strict_to_gentle():
    from meeting_host import style
    for key in ("OVERTIME_SECONDS", "NEGLECTED_SECONDS", "COOLDOWN_SECONDS", "SILENCE_SECONDS"):
        assert style.STYLES["strict"][key] <= style.STYLES["efficient"][key]
        assert style.STYLES["efficient"][key] <= style.STYLES["gentle"][key]
