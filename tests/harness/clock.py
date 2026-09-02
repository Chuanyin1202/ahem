"""虛擬時鐘：regression suite 唯一的時間來源（提案 §2 時鐘契約的測試側落地）。

⚠️ 這張單不做時鐘注入到 production（第 3 步）。`VirtualClock` 目前只能接在
兩種地方：

1. 本來就把 `clock: Callable[[], float]` 當建構參數收的物件——目前只有
   `Chair`（見 `speaker.py` 的 `Chair.__init__`，production 已經支援注入，
   這張單不用改 `src/` 就能用）。
2. 把時間當一般參數傳進去的純函式——`MeetingState.silent_for(now)`、
   `MeetingState.current_run_seconds(now)`、`fast_path.check(st, now, ...)`。
   這些函式本來就是決定性的，甚至不需要接這個類別，直接傳明確的 float 即可。

`Session`（live.py）的 `now` 屬性、`Chair.run()`／`watch_fast`／`watch_slow`
的 `asyncio.sleep(...)`、`Chair._speak()` 的 EARCON_GATE 等待、`Voice.synth()`
的逾時計時，全部還是裸 `time.perf_counter()`／真實 `asyncio.sleep`——這個
模組完全推不動它們。那正是提案第七節第 3 步「Clock 注入」要解決的事，
這裡不越界去動 `src/`（見 tests/harness/chair_runner.py 的
`ChairHarness.wait_task_settled` docstring，那裡具體點出這個缺口打在哪）。
"""
import asyncio


class VirtualClock:
    """`now()`/`__call__()` 回傳目前虛擬時間，相容 `Chair(..., clock=...)`
    期待的 `Callable[[], float]`。

    `advance()` 只推進讀數＋讓 event loop 跑幾輪 `sleep(0)`——不是真正的
    『跑到完全靜止』（asyncio 沒有這種可觀測的 API），足夠讓 `Chair.tick()`
    驅動的簡單 coroutine（`_speak` 的邏輯分支、EOS 送出）在反覆呼叫下逐步
    前進，但推不動任何真實計時器（見上方模組說明的限制）。
    """

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    async def advance(self, seconds: float, *, drain_rounds: int = 3) -> None:
        if seconds < 0:
            raise ValueError(f"時間不能倒流：{seconds}")
        self.t += seconds
        await self.drain(drain_rounds)

    async def drain(self, rounds: int = 3) -> None:
        """讓已排程的 callback／task 有機會前進 `rounds` 輪。呼叫端若需要
        『反覆推進直到某件事完成』，應該自己包一個有上限的迴圈重複呼叫
        （見 tests/harness/chair_runner.py::ChairHarness.run_ticks 的用法），
        而不是假設單次呼叫就能跑到底——這裡刻意不猜測要跑幾輪才夠。
        """
        for _ in range(rounds):
            await asyncio.sleep(0)
