"""Chair 場景 runner：把 tests/test_chair.py 既有的 make()/make_full()/
ticks()/drain() 收斂成一個物件，供迴歸 1、2 共用同一種寫法驅動劇本。

不重寫 Chair 的任何斷言邏輯——這裡只是把「建構 Chair＋跑 tick＋收集
callback」的樣板包起來，時鐘一律用 VirtualClock，播放消費一律用 FakePlayer
（迴歸 2 需要它的 frame ledger；迴歸 1 用不到 ledger 但共用同一個介面沒有
壞處）。
"""
import asyncio
from collections.abc import Callable

from meeting_host.speaker import Chair, Output

from .clock import VirtualClock
from .fake_player import FakePlayer
from .fake_voice import FakeEarcon


class ChairHarness:
    """`voice` 必須明確傳入（通常是 tests/harness/fake_voice.py 的
    `FakeVoice`）——不給預設值，避免不小心接上真的 `Voice`（會打 ElevenLabs
    API，違反這張單「不打任何 LLM／TTS API」的限制）。
    """

    def __init__(self, state, voice, *, clock: VirtualClock | None = None,
                 earcon=None, revision: Callable[[], int] = lambda: 0,
                 on_escalate: Callable | None = None,
                 on_dropped: Callable | None = None):
        """`on_dropped`：預設仍是純記錄用的 collector（`self.dropped`）。T21 的
        room-level revision 回歸測試需要接上真正的 `Session.on_dropped`（會重新
        呼叫 `self.chair.request(...)`），才傳自訂版本——傳了之後 `self.dropped`
        不會再被填，呼叫端自己想辦法觀察（例如接 Session 的 events／log）。
        """
        self.clock = clock if clock is not None else VirtualClock(start=100.0)
        self.state = state
        self.output = Output()
        self.player = FakePlayer(self.output)
        self.spoken: list[tuple] = []
        self.failed: list[tuple] = []
        self.dropped: list[tuple] = []
        self.chair = Chair(
            state, self.output, voice, earcon if earcon is not None else FakeEarcon(),
            clock=self.clock, sleep=self.clock.sleep, revision=revision,
            on_spoken=lambda iv, at: self.spoken.append((iv, at)),
            on_failed=lambda iv, r: self.failed.append((iv, r)),
            on_escalate=on_escalate or (lambda iv: iv),
            on_dropped=on_dropped or (lambda iv, r: self.dropped.append((iv, r))),
        )

    def request(self, iv) -> bool:
        return self.chair.request(iv)

    async def run_ticks(self, n: int, *, step: float = 0.1, drain: bool = False) -> None:
        """驅動 n 輪 tick：跟 tests/test_chair.py 的 `ticks()` 同一套節奏——
        `tick()` → 兩輪 `sleep(0)` 讓 `_speak` task 前進 → 時鐘前進一格 →
        視需要用 FakePlayer 把佇列讀空。"""
        for _ in range(n):
            await self.chair.tick()
            await self.clock.drain(2)
            await self.clock.advance(step)
            if drain:
                self.player.drain_all()

    async def wait_task_settled(self, timeout: float = 2.0) -> None:
        """用虛擬時間驅動 Chair task 完成；`timeout` 是虛擬秒數上限。"""
        task = self.chair._task
        if task is None:
            return
        steps = max(1, int(timeout / 0.05))
        for _ in range(steps):
            if task.done():
                break
            await self.clock.advance(0.05)
        assert task.done(), f"Chair task 在 {timeout:g} 虛擬秒內未完成"
        await task
