import asyncio
import struct

import pytest

from meeting_host.speaker import Voice, VoiceError


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
