import time

from meeting_host.state import MeetingState, Utterance


def st():
    return MeetingState(topic="t", duration_min=30, participants=[])


def test_silent_for_is_zero_while_someone_talks():
    s = st()
    s.voice_started("A", now=10.0)
    assert s.silent_for(now=12.0) == 0.0


def test_silence_starts_when_last_speaker_stops():
    s = st()
    s.voice_started("A", now=10.0)
    s.voice_started("B", now=10.5)
    s.voice_stopped("A", now=11.0)
    assert s.silent_for(now=11.5) == 0.0  # B 還在講
    s.voice_stopped("B", now=12.0)
    assert s.silent_for(now=13.0) == 1.0


def test_fresh_state_counts_silence_from_construction_time():
    """I3：新建的 state 要用裸 perf_counter 起算沉默，不能是常數 0.0。

    常數 0.0 會讓 silent_for(perf_counter()) 得到「程序啟動至今」的巨大數值，
    首次 voice event 之前排入的軟插入會被當成早就達到停頓門檻，主席立刻開口。
    """
    s = st()
    silent = s.silent_for(time.perf_counter())
    assert 0.0 <= silent < 1.0


def test_ensure_participant_is_idempotent():
    s = st()
    s.ensure_participant("A")
    s.ensure_participant("A")
    assert s.participants == ["A"]


# ── T13 缺陷 A：silent_seconds() 從未發言者的沉默起點 ─────────────────
#
# 昨晚實測：使用者在會議進行到第 65 秒時才加入語音頻道，那一刻他的
# silent_seconds 立刻回傳 65（會議已進行的時間），觸發「全場沉默」——
# 而那段時間頻道裡根本沒有人。同一個毛病也影響「有人被冷落」。


def test_ensure_participant_without_now_does_not_record_joined_at():
    """沒給 now（既有呼叫、回放路徑）不記錄 joined_at，保留 fallback 行為。"""
    s = st()
    s.ensure_participant("A")
    assert "A" not in s.joined_at
    assert s.silent_seconds("A", now=100.0) == 100.0


def test_ensure_participant_records_joined_at_in_meeting_relative_time():
    """ensure_participant() 收到的 now 是裸 perf_counter（跟 voice_started 一樣），
    要換算成跟 Utterance.start/end 同座標（會議相對時間）存進 joined_at。"""
    s = st()
    s.ensure_participant("A", now=s._t0 + 60.0)  # 會議開始（_t0）後 60 秒才加入
    assert s.joined_at["A"] == 60.0


def test_joined_at_normalizes_perf_counter_cancellation_noise():
    """不同 runner 的 perf_counter 基準不可讓同一個相對秒數產生尾差。"""
    s = st()
    s._t0 = 94.537400798
    s.ensure_participant("A", now=s._t0 + 60.0)
    assert s.joined_at["A"] == 60.0
    assert s.silent_seconds("A", now=65.0) == 5.0


def test_silent_seconds_never_spoken_from_meeting_start_uses_elapsed_time():
    """驗收 1：從會議開始就在場（回放路徑：建構時直接餵 participants，
    從未走過 ensure_participant）、從未發言的人，沉默秒數維持舊行為——
    約等於會議已進行的時間。"""
    s = MeetingState(topic="t", duration_min=30, participants=["A"])
    assert s.silent_seconds("A", now=65.0) == 65.0


def test_silent_seconds_late_joiner_counts_from_join_time_not_meeting_start():
    """驗收 2：會議進行中才加入、從未發言的人，沉默秒數要從他加入的時刻起算，
    不是從會議開始算——否則一進來就被判定「已經沉默了一分鐘」。"""
    s = st()
    s.ensure_participant("A", now=s._t0 + 60.0)  # 會議開始 60 秒後才加入
    assert s.silent_seconds("A", now=65.0) == 5.0  # 加入後又過了 5 秒才查詢


def test_silent_seconds_is_near_zero_right_after_joining():
    """驗收 3：剛加入的那一刻沉默秒數 ≈ 0，不會立刻觸發「全場沉默」／
    「有人被冷落」。"""
    s = st()
    s.ensure_participant("A", now=s._t0 + 30.0)
    assert s.silent_seconds("A", now=30.0) == 0.0


def test_silent_seconds_already_spoken_unaffected_by_join_time():
    """驗收 4：已發言過的人，沉默秒數仍是距離上次發言結束多久，跟 joined_at 無關。"""
    s = st()
    s.ensure_participant("A", now=s._t0)  # 進場時刻遠早於發言
    s.add(Utterance("A", "hi", 10.0, 12.0))
    assert s.silent_seconds("A", now=20.0) == 8.0


def test_silent_seconds_replay_path_prior_last_unaffected():
    """驗收 5：回放路徑（prior_last，從未走過 ensure_participant）行為不變。"""
    s = MeetingState(topic="t", duration_min=30, participants=["A"])
    s.prior_last["A"] = 30.0
    assert s.silent_seconds("A", now=50.0) == 20.0
