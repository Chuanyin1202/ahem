"""產生主席的提示音：兩聲上行短音（像會議鈴），48kHz 16-bit 立體聲，0.45 秒。
只跑一次，產物 assets/earcon.wav 進 repo。"""
import math
import struct
import wave
from pathlib import Path

RATE = 48000
OUT = Path(__file__).parent.parent / "assets" / "earcon.wav"


def tone(hz: float, secs: float, amp: float = 0.35) -> list[int]:
    n = int(RATE * secs)
    out = []
    for i in range(n):
        env = min(1.0, i / (RATE * 0.01), (n - i) / (RATE * 0.04))  # 10ms 淡入、40ms 淡出
        out.append(int(32767 * amp * env * math.sin(2 * math.pi * hz * i / RATE)))
    return out


samples = tone(880, 0.18) + [0] * int(RATE * 0.05) + tone(1318.5, 0.22)
OUT.parent.mkdir(exist_ok=True)
with wave.open(str(OUT), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(b"".join(struct.pack("<hh", s, s) for s in samples))
print(f"寫入 {OUT}，{len(samples) / RATE:.2f}s")
