"""迴歸 3（提案 §3 第三列）：`current_run_seconds` 在單人會議、講完就沉默時
必須真的歸零、不能觸發「發言超時」。

根因（T-F）：舊實作 `speaking` 為空時退回 `now - start`（run 起點到現在的
牆鐘時間），沒把「這個人已經停了多久」算進去——單人會議永遠不會換人，
run 因此永遠不會歸零，最終誤觸發「發言超時」硬打斷（見 task-f-report.md
的真實會議 log 證據）。

這條回歸不需要 VirtualClock：`MeetingState.current_run_seconds(now)` 與
`fast_path.check(st, now, ...)` 全部把時間當顯式參數收，本來就是決定性的
（見 state.py 開頭的模組說明），不必接任何時鐘物件——這正是「時鐘契約」
只需要處理 Session／Chair.run／Voice／Output 那幾套裸時鐘來源的原因，
`MeetingState` 從一開始就不是問題所在，這裡沒有第 3 步的缺口。

對應既有覆蓋：
tests/test_state_run.py::test_a_silence_after_last_utterance_ends_the_run
（只驗 `current_run_seconds` 本身回 `(None, 0.0)`）；這裡多接一層
`fast_path.check()`，確認歸零真的擋住了「發言超時」規則——那正是回報症狀
本身，不只是內部數值算對而已。
"""
from meeting_host import fast_path
from meeting_host.state import MeetingState, Utterance


def test_single_speaker_long_silence_resets_run_and_suppresses_overtime():
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    st.add(Utterance("Alex", "先講一小段就不說了", 0.0, 10.0))

    # 沉默 190 秒；舊實作會把 run 算成 now-start=200.0，遠超過
    # fast_path.OVERTIME_SECONDS(180.0)，誤觸發「發言超時」
    now = 200.0

    speaker, run = st.current_run_seconds(now)
    assert (speaker, run) == (None, 0.0)

    triggers = fast_path.check(st, now, set())
    assert not any(t.kind == "發言超時" for t in triggers)
