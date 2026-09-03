"""虛擬時鐘：regression suite 唯一的時間來源（提案 §2 時鐘契約的測試側落地）。

`VirtualClock` 目前可同時注入 `Chair` 與 `Session` 的讀時鐘及 async sleep，
因此核心狀態機、提示音間隔、背景輪詢與 `Voice` 網路 timeout 不再依賴牆鐘。
Output 的 Discord 播放執行緒仍由 Discord 以 20ms 節奏拉取，不由 asyncio 驅動。

目前可接在
兩種地方：

1. 收 `clock`／`sleep` 建構參數的 `Chair` 與 `Session`。
2. 把時間當一般參數傳進去的純函式——`MeetingState.silent_for(now)`、
   `MeetingState.current_run_seconds(now)`、`fast_path.check(st, now, ...)`。
   這些函式本來就是決定性的，甚至不需要接這個類別，直接傳明確的 float 即可。

Output 播放執行緒的節奏由 Discord runtime 控制；回歸測試以 `FakePlayer` 的
frame ledger 取代牆鐘播放，驗證每幀順序、遺漏與重複。
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
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.t

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        """等待虛擬秒數；只有 `advance()` 到期限後才完成，不碰牆鐘。"""
        if seconds < 0:
            raise ValueError(f"時間不能倒流：{seconds}")
        if seconds == 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.t + seconds, future))
        self._sleepers.sort(key=lambda item: item[0])
        try:
            await future
        finally:
            self._sleepers = [item for item in self._sleepers if item[1] is not future]

    async def advance(self, seconds: float, *, drain_rounds: int = 3) -> None:
        if seconds < 0:
            raise ValueError(f"時間不能倒流：{seconds}")
        self.t += seconds
        ready = [item for item in self._sleepers if item[0] <= self.t]
        self._sleepers = [item for item in self._sleepers if item[0] > self.t]
        for _, future in ready:
            if not future.done():
                future.set_result(None)
        await self.drain(drain_rounds)

    async def drain(self, rounds: int = 3) -> None:
        """讓已排程的 callback／task 有機會前進 `rounds` 輪。呼叫端若需要
        『反覆推進直到某件事完成』，應該自己包一個有上限的迴圈重複呼叫
        （見 tests/harness/chair_runner.py::ChairHarness.run_ticks 的用法），
        而不是假設單次呼叫就能跑到底——這裡刻意不猜測要跑幾輪才夠。
        """
        for _ in range(rounds):
            await asyncio.sleep(0)
