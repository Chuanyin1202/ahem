"""I4：STT 連線結束（閒置關閉或斷線）必須清掉「正在說話」狀態。

不清的話：A 的 partial 已把 state.speaking["A"] 設起來，隨後 STT 斷線且 A 停止說話，
因為沒有 committed transcript，A 永遠留在 speaking——軟插入等不到停頓路徑，
15 秒後升級成一次不必要的硬打斷。
"""
import asyncio

from meeting_host.live import Session
from meeting_host.state import MeetingState
from meeting_host.stt import STTPool, SpeakingStopped


class FakeStream:
    """只提供 _on_stream_ended 需要的兩個欄位。"""

    def __init__(self, speaker: str):
        self.speaker = speaker
        self.out = asyncio.Queue()


class ExplodingStream(FakeStream):
    """run() 立刻斷線——驗 _guard 在進入重連退避前就送出 SpeakingStopped。"""

    async def run(self):
        raise RuntimeError("斷線")


class FakePool:
    """只吐固定事件就結束的假 STT pool。"""

    def __init__(self, events):
        self.events = events

    async def utterances(self):
        for ev in self.events:
            yield ev


def test_stream_end_emits_speaking_stopped():
    async def go():
        stream = FakeStream("A")
        await STTPool._on_stream_ended(stream)
        ev = stream.out.get_nowait()
        assert isinstance(ev, SpeakingStopped)
        assert ev.speaker == "A"
    asyncio.run(go())


def test_guard_emits_speaking_stopped_before_reconnect():
    """斷線後 _guard 會退避 2 秒再重連——事件必須在那之前就已經送出。"""
    async def go():
        stream = ExplodingStream("A")
        task = asyncio.create_task(STTPool._guard(stream))
        for _ in range(5):
            await asyncio.sleep(0)  # 讓 guard 跑到退避的 sleep
        assert not stream.out.empty()
        ev = stream.out.get_nowait()
        assert isinstance(ev, SpeakingStopped)
        assert ev.speaker == "A"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_consume_clears_speaking_on_stream_end():
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))
    session.st.speaking_now("A", 0.0)
    assert "A" in session.st.speaking
    asyncio.run(session.consume(FakePool([SpeakingStopped("A")])))
    assert "A" not in session.st.speaking
