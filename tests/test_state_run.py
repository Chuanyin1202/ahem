"""T-F：current_run_seconds 在「單人會議、講完就沉默」時的行為。

根因：舊實作 speaking 為空時退回 `now - start`（run 起點到現在的牆鐘時間），
沒有把「這個人已經停了多久」算進去——單人會議永遠不會換人，run 因此永遠不會
歸零，最終誤觸發「發言超時」硬打斷（見 task-f-report.md 的真實會議 log 證據）。
"""
import pytest

from meeting_host.state import MeetingState, Utterance


def st(**kw):
    return MeetingState(topic="t", duration_min=30, participants=[], **kw)


def test_a_silence_after_last_utterance_ends_the_run():
    """單人講完最後一句後沉默 6 秒（> RUN_GAP_SECONDS=5.0）→ run 已結束。"""
    s = st()
    s.add(Utterance("Alex", "最後一句", 0.0, 10.0))
    assert s.current_run_seconds(now=16.0) == (None, 0.0)


def test_b_three_utterances_small_gaps_accumulate_into_one_run():
    """同一人三句，句間 gap 各 2 秒（≤5.0）→ run 時長＝第三句 end − 第一句 start。"""
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 5.0))
    s.add(Utterance("Alex", "二", 7.0, 12.0))
    s.add(Utterance("Alex", "三", 14.0, 19.0))
    speaker, run = s.current_run_seconds(now=19.5)
    assert speaker == "Alex"
    assert run == 19.0 - 0.0


def test_c_large_gap_between_two_utterances_only_counts_the_last_one():
    """同一人兩句，gap 8 秒（>5.0）→ 只算最後一句，不往前串。"""
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 5.0))
    s.add(Utterance("Alex", "二", 13.0, 18.0))
    speaker, run = s.current_run_seconds(now=18.5)
    assert speaker == "Alex"
    assert run == 18.0 - 13.0


def test_d_speaking_now_still_uses_now_minus_since():
    """`speaking`（STT partial）非空時，仍是「正在講的人」，回 now - since。"""
    s = st()
    s.speaking_now("Alex", since=5.0)
    speaker, run = s.current_run_seconds(now=12.0)
    assert speaker == "Alex"
    assert run == 7.0


def test_e_run_restarts_from_the_new_speakers_first_utterance_after_a_turn_change():
    """換人之後，run 從新人的第一句起算，不含前一位的發言時間。"""
    s = st()
    s.add(Utterance("Alex", "舊的一句", 0.0, 5.0))
    s.add(Utterance("Bob", "新人開口", 6.0, 10.0))
    speaker, run = s.current_run_seconds(now=10.5)
    assert speaker == "Bob"
    assert run == 4.0


def test_f_speaking_chains_back_through_already_committed_utterances():
    """真人連續講很久，STT 依自然停頓切成多個 commit（gap ≤5s）——
    正在說的這一段（speaking/partial）要鏈回前面已 commit、屬於同一輪的
    句子，不能只看這一段自己的 since，否則「發言超時」會被系統性延後
    （見 task-f-report.md 第二輪 tick-by-tick 實測）。
    """
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 5.0))
    s.add(Utterance("Alex", "二", 7.0, 12.0))   # gap 2s，屬於同一輪
    s.speaking_now("Alex", since=13.0)          # gap 1s，緊接第二句之後
    speaker, run = s.current_run_seconds(now=20.0)
    assert speaker == "Alex"
    assert run == 20.0 - 0.0  # 鏈回第一句的 start


def test_g_speaking_chain_breaks_at_a_turn_change():
    """中間換人講過一句 → 鏈只到換人之後那句，不會跨過別人的發言往前串。"""
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 5.0))
    s.add(Utterance("Bob", "插話", 7.0, 12.0))
    s.add(Utterance("Alex", "三", 14.0, 19.0))  # gap 2s，接續自己上一句
    s.speaking_now("Alex", since=20.0)          # gap 1s，緊接第三句之後
    speaker, run = s.current_run_seconds(now=25.0)
    assert speaker == "Alex"
    assert run == 25.0 - 14.0  # 只鏈回「三」，不含 Bob 插話前的「一」


# T15：_chain_start 舊實作遇到別人的句子只是 continue（不中斷迴圈），
# 靠「gap 門檻跟目前鏈到的 start 比」順便讓別人插話的距離被算進去、
# 多半超過 RUN_GAP_SECONDS 而中止鏈——這個假設在多人會議快速交替時失效：
# 每個人自己前後兩句的 gap 仍可能落在 RUN_GAP_SECONDS 內，即使中間有人
# 講完整整一句話。實測：雙人會議 14 分鐘內同一人的「發言超時」誤觸發
# 11 次，run 從沒歸零過（見 task 交付訊息的真實會議 log 證據：49 句中
# 前後兩句間隔僅 2/48 超過 RUN_GAP_SECONDS）。


def test_h_own_gap_within_threshold_still_resets_when_someone_else_spoke_between():
    """A 講 → B 插話（哪怕很短）→ A 再講。即使 A 前後兩句自己的 gap（4.0-1.5=2.5s）
    落在 RUN_GAP_SECONDS 內，只要中間有人講過話，A 的 run 也要從 B 之後重新起算，
    不能鏈回 B 插話之前的那句。"""
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 1.5))
    s.add(Utterance("Bob", "插話", 2.0, 3.5))
    s.add(Utterance("Alex", "二", 4.0, 5.5))
    speaker, run = s.current_run_seconds(now=5.6)
    assert speaker == "Alex"
    assert run == 5.5 - 4.0  # 只算「二」，不含 Bob 插話前的「一」


def test_i_rapid_alternation_run_never_exceeds_the_last_continuous_segment():
    """重現真實情境：A、B 快速交替各講數句，A 自己前後兩句間隔都在
    RUN_GAP_SECONDS 內。A 的 run 不得累加穿越 B 的發言，只能是她最後一段
    不間斷連續發言（這裡是「四」接「五」兩句，3.5 秒），
    不是整場散落發言的總和（若誤累加會是 11.5 秒，即從 0.0 算到 11.5）。"""
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 1.5))
    s.add(Utterance("Bob", "甲", 2.0, 3.5))
    s.add(Utterance("Alex", "二", 4.0, 5.5))
    s.add(Utterance("Bob", "乙", 6.0, 7.5))
    s.add(Utterance("Alex", "三", 8.0, 9.5))
    s.add(Utterance("Alex", "四", 10.0, 11.5))  # 接續自己上一句，gap 0.5s，同一輪
    speaker, run = s.current_run_seconds(now=12.0)
    assert speaker == "Alex"
    assert run == 11.5 - 8.0  # 只鏈回「三」，不含 Bob「乙」之前的「一」「二」


def test_j_speaking_branch_also_resets_after_an_interruption():
    """speaking（正在說話）分支要適用同一條規則：B 插話後 A 才開口講到一半
    （partial），即使 gap 很小，也不能鏈回 B 插話之前 A 自己的發言。"""
    s = st()
    s.add(Utterance("Alex", "一", 0.0, 1.5))
    s.add(Utterance("Bob", "插話", 2.0, 3.5))
    s.speaking_now("Alex", since=4.0)  # gap 0.5s，緊接在 Bob 插話之後
    speaker, run = s.current_run_seconds(now=8.0)
    assert speaker == "Alex"
    assert run == 8.0 - 4.0  # 只算 Bob 插話之後這一段，不含插話前的「一」


# 2026-09-03：真實三人會議實測，「講話中」旗標卡住不放的迴歸。
#
# 症狀：達哥說完「請聽我」後 STT 187 秒完全沒有任何事件——不是連線斷掉
# （`stopped_speaking` 沒被呼叫），也不是他真的一直在講（Discord 音訊封包
# 持續在送，但 ElevenLabs 沒回任何 partial 或 commit）。`speaking["達哥"]`
# 卡住不放，run 跟著牆鐘一路長大，觸發「發言超時」，主席對著已經沉默 3 分鐘
# 的人說「你講了 3 分鐘」——使用者在會議現場當場發現並口頭確認。


def test_k_stale_speaking_flag_freezes_instead_of_growing_with_wall_clock():
    """`speaking_now` 帶 `seen_at`（生產路徑真正的用法）：太久沒有新訊號，
    旗標不再被信任，run 凍結在最後真的有動靜的時刻，不跟著牆鐘繼續長大。"""
    s = st()
    s.speaking_now("達哥", since=100.0, seen_at=100.0)   # 唯一一次真的收到訊號
    # 44.9 秒後（未過 SPEAKING_STALE_SECONDS=45.0）——旗標仍算新鮮
    speaker, run = s.current_run_seconds(now=100.0 + 44.9)
    assert speaker == "達哥"
    assert run == pytest.approx(44.9)

    # 187 秒後（今晚實測的落差）——旗標早已過期，不能再信任
    speaker, run = s.current_run_seconds(now=100.0 + 187.0)
    assert speaker is None
    assert run == 0.0  # 沒有任何已完成的發言可以回退，落回「無人在講」


def test_l_stale_speaking_flag_falls_back_to_last_real_utterance():
    """旗標過期時落到「已完成發言」那支，且看的是全體最後一句
    （可能是別人講的），不是死抓著卡住的那個人不放。「已完成」那支本來就有
    自己的 RUN_GAP_SECONDS 沉默判斷，這裡用一個離最後一句夠近的 now，
    單純驗證「過期就不再挑達哥」這一件事，不跟那條既有規則糾纏在一起。"""
    s = st()
    s.add(Utterance("光の神", "OK", 650.0, 653.1))  # 別人後來正常講了一句
    s.speaking_now("達哥", since=519.7, seen_at=519.7)
    speaker, run = s.current_run_seconds(now=655.0)  # 早就過了達哥的 45 秒門檻
    assert speaker == "光の神"  # 不是「達哥」——他的旗標早就過期了
    assert run == 653.1 - 650.0


def test_m_speaking_now_without_seen_at_never_goes_stale():
    """沒給 `seen_at`（既有呼叫方式：`run.py` 回放工具、本檔其餘所有測試）——
    完全不寫 `speaking_seen`，過期檢查形同不存在，行為跟這條防護加上去之前
    一模一樣。這條防止未來有人「順手」把預設行為改成會過期。"""
    s = st()
    s.speaking_now("Alex", since=0.0)  # 沒給 seen_at
    speaker, run = s.current_run_seconds(now=200.0)  # 遠超過 45 秒
    assert speaker == "Alex"
    assert run == 200.0


def test_n_fresh_repeated_signals_never_go_stale_even_across_a_long_monologue():
    """真的連續講很久（STT 持續每秒送 partial）：只要訊號沒斷過，
    無論講多久都不該被當成卡住——這是這條防護不能誤傷的正面案例。"""
    s = st()
    s.speaking_now("Alex", since=0.0, seen_at=0.0)
    for t in range(1, 201):  # 模擬 STT 每秒一筆 partial，連續 200 秒
        s.speaking_now("Alex", since=0.0, seen_at=float(t))
    speaker, run = s.current_run_seconds(now=200.0)
    assert speaker == "Alex"
    assert run == 200.0
