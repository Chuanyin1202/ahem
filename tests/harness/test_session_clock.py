"""Session 的讀時鐘與背景 sleep 必須能由同一個 VirtualClock 驅動。"""
import asyncio

from meeting_host.live import Session
from meeting_host.state import MeetingState

from .clock import VirtualClock


class _IdleChair:
    pending = None
    playing = None

    def request(self, _intervention):
        return False


def _session(clock: VirtualClock) -> Session:
    state = MeetingState(topic="虛擬時鐘", duration_min=30, participants=[])
    session = Session(
        state,
        clock=clock,
        sleep=clock.sleep,
        wall_clock=lambda: 1_700_000_000.0,
    )
    session.chair = _IdleChair()
    return session


def test_session_now_and_event_timestamp_follow_virtual_clock():
    async def go():
        clock = VirtualClock(start=100.0)
        session = _session(clock)
        assert session.now == 0.0
        assert session.wall_start == 1_700_000_000.0
        await clock.advance(2.5)
        session.emit("probe", {"ok": True})
        assert session.events[-1].t == 2.5

    asyncio.run(go())


def test_watch_fast_wakes_only_when_virtual_second_elapsed():
    async def go():
        clock = VirtualClock(start=50.0)
        session = _session(clock)
        task = asyncio.create_task(session.watch_fast())
        try:
            await clock.drain()
            assert session.events == []
            await clock.advance(0.99)
            assert session.events == []
            await clock.advance(0.01)
            assert [event.kind for event in session.events] == ["fast_timer"]
            assert session.events[0].t == 1.0
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(go())
