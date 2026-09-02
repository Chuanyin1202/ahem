"""假 TTS：吐出帶編號的 frame，讓 FakePlayer 的 frame ledger 能斷言
『每一幀恰好被消費一次』。"""
import asyncio

from meeting_host.audio import FRAME_BYTES
from meeting_host.speaker import VoiceError

from .frames import numbered_frame


class FakeVoice:
    """跟 tests/test_chair.py 的 FakeVoiceLongSentence 同一種串流模擬（每個
    chunk 前真的 `await asyncio.sleep(0)`，模擬逐塊到達），差別只在幀內容
    帶編號，供 FakePlayer 的 ledger 使用。"""

    def __init__(self, n_frames: int, *, fail_after: int | None = None):
        """n_frames：這句話的語音幀數。fail_after：吐完這麼多幀之後才拋
        VoiceError（模擬 TTS 中途失敗）；None 表示正常吐完，不失敗。"""
        self.n_frames = n_frames
        self.fail_after = fail_after
        self.calls: list[str] = []

    async def synth(self, text: str):
        self.calls.append(text)
        for i in range(self.n_frames):
            await asyncio.sleep(0)
            if self.fail_after is not None and i >= self.fail_after:
                raise VoiceError("harness FakeVoice：模擬中途失敗")
            yield numbered_frame(i)


class FakeEarcon:
    """跟 tests/test_chair.py 的 FakeEarcon 同型：固定內容、固定秒數。
    內容刻意不符合 numbered_frame() 的編碼格式，`frame_seq()` 對它一律回
    None，才不會混進語音幀的 ledger。"""

    pcm = b"\x01" * FRAME_BYTES * 5
    seconds = 0.1
