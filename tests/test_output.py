from meeting_host.audio import FRAME_BYTES
from meeting_host.speaker import Output

SILENCE = b"\x00" * FRAME_BYTES


def test_idle_reads_silence_and_is_not_busy():
    o = Output()
    assert o.read() == SILENCE
    assert o.is_busy() is False


def test_arbitrary_chunks_become_exact_frames_in_order():
    o = Output()
    o.enqueue(b"\x01" * 1000)
    o.enqueue(b"\x02" * 5000)  # 6000 → 1 幀（前 3840）+ 2160 餘
    f1 = o.read()
    assert len(f1) == FRAME_BYTES and f1[:1000] == b"\x01" * 1000 and f1[1000:] == b"\x02" * 2840
    assert o.is_busy() is True  # 還有 2160 在 framer 裡且 producer 未結束


def test_end_of_utterance_flushes_tail_then_idle():
    o = Output()
    o.enqueue(b"\x01" * 100)
    o.end_of_utterance()
    tail = o.read()
    assert tail[:100] == b"\x01" * 100 and tail[100:] == b"\x00" * (FRAME_BYTES - 100)
    assert o.read() == SILENCE
    assert o.is_busy() is False


def test_underflow_returns_silence_but_stays_busy():
    """producer 還沒送 EOS、佇列暫時空 → 回靜音，不能回空 bytes（那會終止播放器）"""
    o = Output()
    o.enqueue(b"\x01" * FRAME_BYTES)
    assert o.read() != SILENCE
    assert o.read() == SILENCE
    assert o.is_busy() is True
    o.end_of_utterance()
    o.read()
    assert o.is_busy() is False


def test_first_audible_at_marks_first_nonsilent_frame_only():
    o = Output()
    o.read()
    assert o.first_audible_at is None
    o.enqueue(b"\x01" * FRAME_BYTES)
    o.read()
    t = o.first_audible_at
    assert t is not None
    o.enqueue(b"\x01" * FRAME_BYTES)
    o.read()
    assert o.first_audible_at == t
    o.reset_marker()
    assert o.first_audible_at is None


def test_never_returns_empty_bytes():
    o = Output()
    for _ in range(50):
        assert len(o.read()) == FRAME_BYTES


def test_end_of_utterance_on_full_queue_does_not_block():
    """佇列滿代表播放器死了、沒人在消費——end_of_utterance() 不能用 blocking put()
    卡住呼叫者（Chair 那邊會在 event loop 裡呼叫，卡住就是整個 asyncio 卡死）。"""
    o = Output()
    for _ in range(1500):
        o.enqueue(b"\x01" * FRAME_BYTES)
    o.end_of_utterance()  # 佇列已滿，不能拋例外也不能卡住，必須立即返回
    assert o.is_busy() is True   # 佇列裡還有 1500 幀的資料，沒被清掉
    assert o._producing is False  # 但視為「這句話已結束」，不再等更多資料


def test_lost_eos_tail_does_not_keep_busy():
    """R4：佇列滿時 end_of_utterance() 走 Full 分支，EOS 直接遺失——framer 裡不足
    一幀的尾段永遠沒有 EOS 觸發 flush() 把它清出來，那段音訊已經跟 EOS 一起遺失了，
    不能讓它讓 is_busy() 卡成永遠 True。"""
    o = Output()
    o.enqueue(b"\x01" * 100)   # 100 bytes < FRAME_BYTES，framer 會留下這段尾段
    o.read()                   # 把這 100 bytes 推進 framer（不足一幀，pop() 拿不到，回靜音）
    for _ in range(1500):
        o.enqueue(b"\x02" * FRAME_BYTES)  # 塞滿佇列（MAX_QUEUED_FRAMES=1500）
    o.end_of_utterance()       # 佇列已滿，走 Full 分支：_producing=False，EOS 遺失
    while not o._q.empty():
        o.read()                # 把佇列裡排隊的 1500 幀讀空
    assert o._q.empty()
    assert o._producing is False
    assert 0 < len(o._framer) < FRAME_BYTES  # framer 裡還留著那段不足一幀的尾段，flush 不出來
    assert o.is_busy() is False  # 但不足一幀的尾段不算忙——R4 修正後的行為


def test_underflow_producing_still_keeps_busy_even_under_frame_bytes():
    """R4 修正的 len(framer) 門檻不能誤傷 test_underflow 那種正常案例——
    producer 還沒送 EOS 時，就算 framer 裡不足一幀，還是要算忙（見 test_underflow_returns_silence_but_stays_busy）。"""
    o = Output()
    o.enqueue(b"\x01" * 100)  # 不足一幀
    o.read()                   # framer 留下 100 bytes，回靜音
    assert len(o._framer) == 100 < FRAME_BYTES
    assert o.is_busy() is True  # _producing 還是 True（沒送 EOS），不是靠 framer 長度撐住
