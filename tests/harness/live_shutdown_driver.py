"""迴歸 4（提案 §3 第四列）的 subprocess 驅動腳本。

只重現 T-G／T-I／T20 三個回歸的真根因所在那一段收尾骨架（完整說明見
tests/test_live_shutdown.py 開頭與 meeting_host.live.shutdown 的
docstring）：

    signal handler 註冊 → asyncio.gather(背景 task) → 收到取消 →
    finally: await live.shutdown(session, bot, tasks)

`bot`／背景 task 全部用假的（跟 tests/test_live_shutdown.py 的 FakeBot／
_bg_task 同一種卡死語意：close() 要等所有背景 task 真的收尾才回傳），
會議產出（A 檔）的 LLM 呼叫換成固定回應——整支腳本完全不連 Discord、
不打任何 LLM／TTS API，只驗證 `live.shutdown()`／`live.summary()`
（兩者都是既有 production code，這裡沒有複製或修改它們的邏輯）在真實
SIGINT/SIGTERM 訊號、POST /end 下有沒有把 summary／events.jsonl／minutes
落地。訊號 handler 的註冊本身也是 production code（`live.
install_shutdown_signal_handlers`），這裡只 import 不複製，跟 `main_async`
共用同一份邏輯，不會各自維護一份容易脫鉤的訊號接管程式碼。

`--spectator-port` 開著時會真的把 `meeting_host.spectator.serve()` 掛進
`tasks`（跟 production `main_async` 一樣是 `asyncio.gather` 的一員），
`session` 也跟 production 一樣注入 `cancel=main_task.cancel`——POST /end
這段收尾行為因此跟真的 `python -m meeting_host.live` 完全一致，仍然不需要
Discord／LLM。

`--slow-flush-seconds` 用來重現「收尾中途再收到一次取消」這個時間點：跟
tests/test_live_shutdown.py::test_real_second_cancel_during_flush_still_closes_bot
（in-process 版本）同一招，把 `live._flush_spectator` 換成一個只會睡覺的
版本——這是 `shutdown()` 本來就會呼叫的同一個 module-level 名稱，換掉它不
算複製或修改 `shutdown()` 的邏輯，只是讓「shutdown 還在某個 await 上」這件事
可以用一個固定、不必賭真實 socket／aiohttp 清理時序的方式重現。沒有這個旗標
（預設 0＝不啟用）時 `_flush_spectator` 完全是 production 版本。

跑法（僅供人工核對；正式驗證走 tests/harness/test_regression_shutdown_writes_records.py
與 tests/test_live_shutdown.py）：
    PYTHONPATH=src python tests/harness/live_shutdown_driver.py [--spectator-port PORT] [--slow-flush-seconds N]
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from meeting_host import live  # noqa: E402
from meeting_host import minutes as minutes_mod  # noqa: E402
from meeting_host.state import MeetingState  # noqa: E402

# 不連任何 LLM API：跟 tests/test_live_shutdown.py 的 _stub_minutes_llm 做同一件
# 事，只是這裡是獨立行程，沒有 pytest 的 monkeypatch 可用，直接改模組屬性——
# write_minutes() 透過模組全域名稱查到 _call_minutes_llm，這樣換掉在呼叫時一樣生效。
minutes_mod._call_minutes_llm = lambda events: {
    "decisions": [], "todos": [], "unresolved": [], "stances": {},
}


class FakeBot:
    """跟 tests/test_live_shutdown.py 的 FakeBot 同一種卡死語意：close() 要
    等所有背景 task 真的收尾才回傳——這正是 T-G 實測卡住的那個依賴。"""

    def __init__(self, tasks_done: asyncio.Event):
        self.tasks_done = tasks_done

    async def close(self) -> None:
        await self.tasks_done.wait()


async def _bg_task(counter: list[int], tasks_done: asyncio.Event) -> None:
    """模擬 bot.start()／consume() 等背景 task：收到 cancel() 才算收尾。"""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        counter[0] -= 1
        if counter[0] == 0:
            tasks_done.set()
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spectator-port", type=int, default=0,
                     help="開觀戰 UI（0＝不開，跟 production 的 --spectator-port 同語意）")
    ap.add_argument("--spectator-token", default="",
                     help="釘住操作權杖，讓測試端知道要帶什麼 header（production 同名參數）")
    ap.add_argument("--slow-flush-seconds", type=float, default=0.0,
                     help="把 live._flush_spectator 換成只會睡這麼久的版本"
                          "（0＝不換，用 production 版本）")
    return ap.parse_args(argv)


def _install_slow_flush(seconds: float) -> None:
    """把 `live._flush_spectator`（`shutdown()` 呼叫的同一個 module-level 名稱）
    換成一個固定睡 `seconds` 秒的版本，用來在收尾中途製造一個確定會被
    第二次取消打中的 await 點——不改 `shutdown()` 本身一行程式碼。"""
    async def _slow_flush(session, timeout: float = 3.0) -> None:
        await asyncio.sleep(seconds)

    live._flush_spectator = _slow_flush


async def main_async(args: argparse.Namespace) -> None:
    # 跟 meeting_host.live.main_async 完全同一套接管方式：兩個訊號都明確接管，
    # 統一走 main_task.cancel()，落到 except (KeyboardInterrupt, CancelledError)
    # → finally: shutdown() 路徑。直接 import production 的註冊函式，不複製。
    main_task = asyncio.current_task()
    live.install_shutdown_signal_handlers(main_task)

    if args.slow_flush_seconds:
        _install_slow_flush(args.slow_flush_seconds)

    # cancel=main_task.cancel 跟 production main_async 一樣接上——POST /end
    # （spectator 的 `_end_handler` → `session.request_end()`）才會真的觸發
    # 這裡的收尾，語意與真的 live.py 完全一致。
    session = live.Session(MeetingState(topic="測試", duration_min=30, participants=[]),
                           cancel=main_task.cancel)

    tasks_done = asyncio.Event()
    counter = [2]
    tasks = [asyncio.create_task(_bg_task(counter, tasks_done)) for _ in range(2)]
    bot = FakeBot(tasks_done)

    if args.spectator_port:
        from meeting_host import spectator
        tasks.append(asyncio.create_task(
            spectator.serve(session, args.spectator_port, args.spectator_token or None)))

    print("議題：測試（harness 驅動，無 Discord／LLM）")
    print("READY", flush=True)  # 測試端等這一行出現才送訊號，不用固定 sleep 猜時機
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await live.shutdown(session, bot, tasks)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(main_async(args))
    except asyncio.CancelledError:
        # 第二次訊號：跟 live.main() 同樣的收尾語意，印一行說明、明確離開碼 1。
        print("    收到第二次結束訊號，強制退出")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
