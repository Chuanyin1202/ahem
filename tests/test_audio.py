import audioop
import math
import struct

from meeting_host.audio import FRAME_BYTES, Framer, Upsampler


def sine_mono(rate: int, secs: float, hz: float = 440.0) -> bytes:
    n = int(rate * secs)
    return b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / rate)))
                    for i in range(n))


def test_upsampler_24k_to_48k_stereo_length():
    up = Upsampler(24000)
    out = up.feed(sine_mono(24000, 0.5))
    # 0.5s @ 48k 立體聲 16-bit = 48000*0.5*2*2 bytes（允許 ratecv 首段 ±1 sample）
    assert abs(len(out) - 96000) <= 4


def test_upsampler_is_stateful_across_chunks():
    """分兩段餵與一次餵，輸出總長一致——沒保留 state 會在邊界少 sample"""
    whole = Upsampler(24000).feed(sine_mono(24000, 0.4))
    up = Upsampler(24000)
    a = sine_mono(24000, 0.4)
    split = up.feed(a[:len(a) // 2]) + up.feed(a[len(a) // 2:])
    assert abs(len(whole) - len(split)) <= 4


def test_upsampler_carries_odd_trailing_byte():
    up = Upsampler(24000)
    a = sine_mono(24000, 0.1)
    out = up.feed(a[:101]) + up.feed(a[101:])  # 101 是奇數，切在 sample 中間
    assert len(out) % 4 == 0
    assert abs(len(out) - Upsampler(24000).feed(a).__len__()) <= 4


def test_upsampler_output_is_stereo_with_equal_channels():
    out = Upsampler(24000).feed(sine_mono(24000, 0.1))
    left = audioop.tomono(out, 2, 1, 0)
    right = audioop.tomono(out, 2, 0, 1)
    assert left == right


def test_framer_splits_arbitrary_chunks_into_exact_frames():
    f = Framer()
    f.push(b"\x01" * 1200)
    assert f.pop() is None
    f.push(b"\x01" * 5000)  # 共 6200 → 1 幀 + 2360 餘
    frame = f.pop()
    assert frame is not None and len(frame) == FRAME_BYTES
    assert f.pop() is None
    assert len(f) == 6200 - FRAME_BYTES


def test_framer_flush_pads_with_zeros():
    f = Framer()
    f.push(b"\x01" * 100)
    tail = f.flush()
    assert len(tail) == FRAME_BYTES
    assert tail[:100] == b"\x01" * 100 and tail[100:] == b"\x00" * (FRAME_BYTES - 100)
    assert f.flush() is None and len(f) == 0
