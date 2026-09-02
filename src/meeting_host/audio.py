"""PCM 轉換：把 TTS 的 mono 音訊變成 Discord 要的 48kHz s16le 立體聲 20ms 幀。

Discord 播放執行緒每次 read() 必須拿到精確 3840 bytes；TTS 串流的 chunk 大小任意，
所以要有一層 buffer 負責切幀。重採樣跨 chunk 保留 state，否則每個邊界會有雜音
（stt.py 的反向路徑已踩過這個坑）。
"""
import audioop

DISCORD_RATE = 48000
FRAME_BYTES = 3840  # 20ms @ 48kHz × 16-bit × 2ch
SAMPLE_WIDTH = 2


class Upsampler:
    """mono s16le @ src_rate → 48kHz 立體聲。跨呼叫保留 ratecv state 與奇數尾 byte。"""

    def __init__(self, src_rate: int):
        self.src_rate = src_rate
        self._state = None
        self._carry = b""  # 上一 chunk 切在 sample 中間留下的那個 byte

    def feed(self, mono_pcm: bytes) -> bytes:
        data = self._carry + mono_pcm
        if len(data) % SAMPLE_WIDTH:
            data, self._carry = data[:-1], data[-1:]
        else:
            self._carry = b""
        if not data:
            return b""
        if self.src_rate != DISCORD_RATE:
            data, self._state = audioop.ratecv(
                data, SAMPLE_WIDTH, 1, self.src_rate, DISCORD_RATE, self._state)
        return audioop.tostereo(data, SAMPLE_WIDTH, 1.0, 1.0)


class Framer:
    """任意大小的 PCM 進，精確 FRAME_BYTES 的幀出。"""

    def __init__(self):
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def push(self, pcm: bytes) -> None:
        self._buf += pcm

    def pop(self) -> bytes | None:
        if len(self._buf) < FRAME_BYTES:
            return None
        frame = bytes(self._buf[:FRAME_BYTES])
        del self._buf[:FRAME_BYTES]
        return frame

    def flush(self) -> bytes | None:
        """一句講完：不足一幀的尾段補零送出，避免最後幾十毫秒被吃掉。"""
        if not self._buf:
            return None
        frame = bytes(self._buf) + b"\x00" * (FRAME_BYTES - len(self._buf))
        self._buf.clear()
        return frame
