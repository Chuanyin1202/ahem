import struct
import wave

import pytest

from meeting_host.audio import FRAME_BYTES
from meeting_host.speaker import EARCON_PATH, Earcon


def test_bundled_earcon_loads_and_is_frame_aligned():
    e = Earcon(EARCON_PATH)
    assert 0.3 <= e.seconds <= 0.8
    assert len(e.pcm) % FRAME_BYTES == 0
    assert e.pcm != b"\x00" * len(e.pcm)


def test_wrong_format_is_rejected(tmp_path):
    bad = tmp_path / "bad.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * 1600)
    with pytest.raises(ValueError):
        Earcon(bad)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        Earcon(tmp_path / "nope.wav")
