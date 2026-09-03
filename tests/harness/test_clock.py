"""VirtualClock 自己的行為（驗收 1：harness 要有單元測試驗它自己）。"""
import asyncio

from .clock import VirtualClock


def test_now_and_call_return_start_value():
    c = VirtualClock(start=42.0)
    assert c() == 42.0
    assert c.now() == 42.0


def test_advance_moves_now_forward():
    async def go():
        c = VirtualClock(start=0.0)
        await c.advance(1.5)
        assert c() == 1.5
        await c.advance(0.5)
        assert c() == 2.0
    asyncio.run(go())


def test_advance_rejects_negative_seconds():
    async def go():
        c = VirtualClock()
        try:
            await c.advance(-1.0)
        except ValueError:
            return
        raise AssertionError("advance() 應該拒絕負值——時間不能倒流")
    asyncio.run(go())


def test_repeated_advance_eventually_lets_scheduled_task_finish():
    """`advance()` 不保證『一次呼叫就跑到底』（見 clock.py 模組說明的限制）——
    但被 scenario runner 反覆呼叫時（就像 ChairHarness.run_ticks 對
    Chair.tick() 那樣一輪一輪推進），排程過的 coroutine 應該持續往前走、
    最終跑完。這裡驗證的是這個『反覆呼叫會推進』的性質，不去斷言單次呼叫
    要跑幾輪才夠——那是 asyncio 排程器的實作細節，不該寫死進測試。
    """
    async def go():
        c = VirtualClock()
        state = {"steps": 0}

        async def stepper():
            for _ in range(3):
                await asyncio.sleep(0)
                state["steps"] += 1

        task = asyncio.create_task(stepper())
        for _ in range(20):
            if task.done():
                break
            await c.advance(0.1)
        assert task.done(), "20 輪 advance() 後 stepper 仍未跑完"
        assert state["steps"] == 3
    asyncio.run(go())


def test_sleep_finishes_only_after_virtual_deadline():
    async def go():
        c = VirtualClock(start=10.0)
        task = asyncio.create_task(c.sleep(1.0))
        await c.drain()
        assert not task.done()
        await c.advance(0.99)
        assert not task.done()
        await c.advance(0.01)
        assert task.done()

    asyncio.run(go())
