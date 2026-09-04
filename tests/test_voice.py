import asyncio
import struct

import pytest

from meeting_host.speaker import Voice, VoiceError
from tests.harness.clock import VirtualClock


def mono_24k(secs: float) -> bytes:
    return struct.pack("<h", 1000) * int(24000 * secs)


class FakeVoice(Voice):
    def __init__(self, chunks, delay=0.0):
        super().__init__(api_key="x")
        self._chunks, self._delay = chunks, delay

    async def _raw_stream(self, text):
        for c in self._chunks:
            await asyncio.sleep(self._delay)
            yield c


def test_synth_converts_to_48k_stereo_and_keeps_total_length():
    async def go():
        v = FakeVoice([mono_24k(0.1), mono_24k(0.1)[:-1], b"\x00"])  # 中間切在 sample 中間
        return b"".join([c async for c in v.synth("你好")])
    out = asyncio.run(go())
    assert abs(len(out) - 0.2 * 48000 * 4) <= 8


def test_first_byte_timeout_raises():
    async def go():
        v = FakeVoice([mono_24k(0.1)], delay=0.3)
        v.first_byte_timeout = 0.05
        async for _ in v.synth("x"):
            pass
    with pytest.raises(VoiceError):
        asyncio.run(go())


def test_total_timeout_raises_midstream():
    async def go():
        v = FakeVoice([mono_24k(0.01)] * 10, delay=0.03)
        v.first_byte_timeout = 1.0
        v.total_timeout = 0.1
        async for _ in v.synth("x"):
            pass
    with pytest.raises(VoiceError):
        asyncio.run(go())


def test_first_byte_timeout_can_be_driven_by_virtual_clock():
    class HangingVoice(Voice):
        async def _raw_stream(self, text):
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

    async def go():
        clock = VirtualClock(start=20.0)
        voice = HangingVoice(
            api_key="x", first_byte_timeout=1.0,
            clock=clock, sleep=clock.sleep,
        )

        async def consume():
            async for _ in voice.synth("測試"):
                pass

        task = asyncio.create_task(consume())
        await clock.drain(5)
        assert not task.done()
        await clock.advance(0.99)
        assert not task.done()
        await clock.advance(0.01, drain_rounds=8)
        with pytest.raises(VoiceError, match="首位元組逾時"):
            await task

    asyncio.run(go())


def test_cancelling_synth_cleans_up_virtual_timeout():
    class HangingVoice(Voice):
        async def _raw_stream(self, text):
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

    async def go():
        clock = VirtualClock()
        voice = HangingVoice(api_key="x", clock=clock, sleep=clock.sleep)

        async def consume():
            async for _ in voice.synth("測試"):
                pass

        task = asyncio.create_task(consume())
        await clock.drain(5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await clock.drain(5)
        assert clock._sleepers == []

    asyncio.run(go())
