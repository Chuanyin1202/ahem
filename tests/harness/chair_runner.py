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
            clock=self.clock, revision=revision,
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
            self.clock.t += step
            if drain:
                self.player.drain_all()

    async def wait_task_settled(self, timeout: float = 2.0) -> None:
        """⚠️ 時鐘契約缺口（見 clock.py 模組說明）：hard 路徑的 EARCON_GATE
        等待（`Chair._speak` 裡 `await asyncio.sleep(gap)`）與 `Voice` 的逾時
        計時，用的是真實 `asyncio.sleep`／`time.perf_counter`，`VirtualClock`
        完全推不動——這裡只能真的等 task 跑完（最多等 `timeout` 秒真實時間），
        跟 tests/test_chair.py 既有測試的 `asyncio.wait_for(c._task, ...)`
        是同一個妥協。第 3 步把 Clock 注入 `Chair._speak`／`Voice` 之後，
        這個方法理論上可以整個刪掉，改成單純反覆 `advance()`。
        """
        task = self.chair._task
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=timeout)
