"""本地麥克風音訊來源。

⚠️ 這條路徑**無法**區分說話者——單一麥克風收到的是混音。
   它的用途是驗證「音訊 → STT → Utterance → 快路／慢路」這條鏈路，
   不是 Discord 的替代品。要分辨誰在說話仍然需要每人一軌（Discord）。

介面刻意與 discord_source 一致：都是把 PCM 餵給 STTPool，
所以下游的 state / fast_path / slow_path 完全不需要知道音訊從哪來。
"""
import asyncio

import numpy as np
import sounddevice as sd

from .stt import STTPool

BLOCK_MS = 20
GATE_THRESHOLD = 0.02   # 低於此音量視為靜音（實測：靜音約 0.002，說話約 0.25）
HANGOVER_BLOCKS = 25    # 低於門檻後再送 0.5 秒，避免句中停頓被切斷


class MicSource:
    def __init__(self, pool: STTPool, speaker: str = "麥克風",
                 device: int | None = None, rate: int = 48000):
        self.pool = pool
        self.speaker = speaker
        self.device = device
        self.rate = rate
        self.loop: asyncio.AbstractEventLoop | None = None
        self.blocks = 0
        self.sent = 0
        self.peak = 0.0
        self._hangover = 0

    def _callback(self, indata, frames, time_info, status) -> None:
        """由 sounddevice 的音訊執行緒呼叫，不能直接碰 asyncio.Queue。"""
        mono = indata[:, 0] if indata.ndim > 1 else indata
        self.blocks += 1
        level = float(np.abs(mono).max())
        self.peak = max(self.peak, level)

        # 音量閘門：安靜時就別送。
        # STT 層靠「一段時間沒收到音訊」判斷句子結束並送 commit；
        # 麥克風不像 Discord 會自己停止送封包，不設閘門就永遠不會 commit、永遠不出字。
        if level >= GATE_THRESHOLD:
            self._hangover = HANGOVER_BLOCKS  # 句中短暫停頓不切斷
        elif self._hangover > 0:
            self._hangover -= 1
        else:
            return

        self.sent += 1
        # STTPool 期待 Discord 格式（48kHz 立體聲），這裡把單聲道複製成雙聲道
        stereo = np.repeat(mono.reshape(-1, 1), 2, axis=1)
        pcm = (stereo * 32767).astype(np.int16).tobytes()
        if self.loop:
            self.loop.call_soon_threadsafe(self.pool.feed, self.speaker, pcm)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        blocksize = int(self.rate * BLOCK_MS / 1000)
        with sd.InputStream(device=self.device, channels=1, samplerate=self.rate,
                            blocksize=blocksize, dtype="float32",
                            callback=self._callback):
            print(f"🎙  麥克風開啟（{self.rate}Hz，{BLOCK_MS}ms/block）")
            while True:
                await asyncio.sleep(3)
                print(f"    音量峰值 {self.peak:.3f}｜收 {self.blocks} 送 {self.sent} block"
                      + ("" if self.peak > GATE_THRESHOLD else "  （靜音，未送出）"))
                self.peak = 0.0
