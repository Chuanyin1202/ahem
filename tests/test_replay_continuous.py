"""T-F 連帶修正：replay.load() 要把「同一人連續兩則摘要句」模擬成真的連續講話。

根因：scenarios.py 的逐字稿是代表性摘要句（句尾常接「...」，代表話還沒講完），
舊實作用 CHARS_PER_SECOND 對摘要文字估計講話時長，只估得出幾秒，但下一則
真實時間戳可能晚了幾十秒——同一人連續發言在時間軸上被誤建模成「講一下、
停頓幾十秒、再講一下」，導致 state.current_run_seconds() 的沉默判斷把這種
摘要斷句誤判成真的停頓（見 task-f-report.md）。
"""
from meeting_host.replay import load

# 同一人連續兩則，句尾接 "..."（代表摘要，實際上一直在講）
SAME_SPEAKER = {
    "topic": "t", "duration": 30, "elapsed": 5,
    "stats": {"Alex": {"spoke": "3分00秒", "last": "剛剛"}},
    "transcript": [
        ("00:00", "Alex", "第一句，其實還在講..."),
        ("00:40", "Alex", "第二句，接續前面的話..."),
    ],
}

# 換人：兩句分屬不同人
TURN_CHANGE = {
    "topic": "t", "duration": 30, "elapsed": 5,
    "stats": {
        "Alex": {"spoke": "1分00秒", "last": "剛剛"},
        "Bob": {"spoke": "1分00秒", "last": "剛剛"},
    },
    "transcript": [
        ("00:00", "Alex", "Alex 講的短句"),
        ("00:40", "Bob", "Bob 接著講"),
    ],
}


def test_consecutive_same_speaker_lines_are_modeled_as_continuous_speech():
    """同一人連續兩則 → 第一則 end 直接接到第二則 start，中間沒有人為空檔。"""
    _, utterances = load(SAME_SPEAKER)
    first, second = utterances
    assert first.speaker == second.speaker == "Alex"
    assert first.end == second.start == 40.0


def test_turn_change_keeps_estimated_end_capped_by_next_start():
    """換人的那一句，維持原本「用字數估計時長，但不超過下一句開頭」的行為。"""
    _, utterances = load(TURN_CHANGE)
    first, second = utterances
    assert first.speaker == "Alex"
    assert second.speaker == "Bob"
    # 估計時長遠小於 40 秒的間隔，不應該被硬拉到 40.0
    assert first.end < 40.0
    assert first.end == first.start + len(first.text) / 4.5
