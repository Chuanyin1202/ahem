"""觀戰 UI（T-D）：aiohttp SSE 伺服器 + 無 Discord 時的回放模式。

`serve(session, port)` 是 `live.py` 接線呼叫的入口（`--spectator-port N`）——
只依賴 `session.events: list[Event]` 與 `session.subscribers: list[Callable[[Event], None]]`
這個最小介面（見 events.py / live.py 的 Session），不 import live.Session 本身，
好讓 `python -m meeting_host.spectator --replay file.jsonl` 能餵一個假 session 進來單獨跑。

靜態頁面在同名資料夾 `spectator/index.html`（純資料檔，不是 Python package——
`meeting_host.spectator` 這個名字本身解析到這支 .py module，`spectator/` 目錄
只是 `Path(__file__).parent` 底下的一個子目錄，不會被 import 系統當成 package）。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from .events import Event

INDEX_HTML_PATH = Path(__file__).parent / "spectator" / "index.html"
PING_INTERVAL = 15.0
QUEUE_MAXSIZE = 200
SESSION_KEY: web.AppKey[Any] = web.AppKey("session")
# 同 live.py `--phase` 的 choices／live.Session 建構子預設值——階段只能是這三個。
VALID_PHASES = ("發散期", "呻吟區", "收斂期")


class SessionLike(Protocol):
    """`serve()` 真正用到的最小介面——`live.Session` 與回放用的 `ReplaySession` 都滿足。"""

    events: list[Event]
    subscribers: list[Callable[[Event], None]]
    phase: str

    def emit_meeting(self) -> None: ...

    def request_end(self) -> bool:
        """要求結束會議。True＝已受理（`live.Session` 走 `live.py` 注入的
        `main_task.cancel`），False＝這個模式沒有會議可結束（回放模式），
        `POST /end` 據此回 409。

        實作**必須可重複呼叫**：收尾一旦啟動，後續呼叫要回 True 但不再送第二次
        取消（`live.Session.request_end` 用 `ending` 旗標擋）。這不只是禮貌——
        第二次取消會打斷正在跑的 `live.shutdown()`。"""
        ...


def _offer(queue: asyncio.Queue, event: Event) -> None:
    """把事件塞進佇列；滿了就丟最舊的一筆再塞（同步、不阻塞——供 subscriber callback 呼叫）。"""
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass  # 極端 race：塞不進去就算了，下一筆事件還有機會


class _Stream:
    """一條已連線的 SSE 串流。`idle` 用來讓 `live.shutdown()` 等最後一批事件（`minutes`）
    真的寫進 socket 再去 cancel 掉 `serve()`——沒有這個訊號的話，emit 完立刻 cancel，
    handler 的 `queue.get()` 還沒被排程就被砍掉，頁面永遠收不到總結。"""

    def __init__(self, session: SessionLike):
        self.session = session
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.idle = asyncio.Event()
        self.idle.set()

    def offer(self, event: Event) -> None:
        """subscriber callback：同步、不阻塞（見 `_offer`）。"""
        _offer(self.queue, event)
        self.idle.clear()

    def mark_idle_if_drained(self) -> None:
        """一筆事件 `resp.write()` 完成後呼叫：佇列空了才算「全部寫出去了」。"""
        if self.queue.empty():
            self.idle.set()


_STREAMS: list[_Stream] = []


async def flush_streams(session: SessionLike, timeout: float = 3.0) -> bool:
    """等這個 session 所有已連線的 SSE 串流把佇列寫乾淨，最多 `timeout` 秒。

    回傳 True＝都寫出去了；False＝逾時（例如 client 半死不活、socket 卡住）。
    逾時不是錯誤，只是不再等——呼叫端（`live.shutdown()`）本來就有更重要的收尾要做。
    """
    streams = [s for s in _STREAMS if s.session is session]
    if not streams:
        return True

    async def wait_all() -> None:
        # 逐條等，不用 asyncio.gather：gather 被外部取消時會把 CancelledError
        # set 進 _GatheringFuture，而醒過來的呼叫端早就走了、沒人取回它，直譯器
        # 結束時就會印一截 "_GatheringFuture exception was never retrieved"
        # 的 traceback（實測：第二次 SIGTERM 剛好打在 flush 上時）。
        # wait_for 包 coroutine 產生的是 Task，被取消是 cancelled 狀態、不會留例外。
        # 反正三條都要等到，逐條等與並行等的總時間一樣，上限由 timeout 把關。
        for stream in streams:
            await stream.idle.wait()

    try:
        await asyncio.wait_for(wait_all(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


def _sse_message(event_name: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


async def _index_handler(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML_PATH.read_text(encoding="utf-8"),
                         content_type="text/html", charset="utf-8")


async def _health_handler(request: web.Request) -> web.Response:
    session: SessionLike = request.app[SESSION_KEY]
    return web.json_response({"ok": True, "events": len(session.events)})


async def _phase_handler(request: web.Request) -> web.Response:
    """H3：工程視角的階段分頁改為可點——POST 一個合法階段，更新 session 並重送 meeting 事件。

    `session.phase` 是慢路 prompt 真正吃的欄位（`live.py` `_run_slow_score` 傳
    `self.phase`），所以這裡改了會**真的**影響下一次 LLM 評分，不只是畫面顯示。
    """
    session: SessionLike = request.app[SESSION_KEY]
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid json body"}, status=400)
    phase = payload.get("phase") if isinstance(payload, dict) else None
    if phase not in VALID_PHASES:
        return web.json_response(
            {"ok": False, "error": f"phase 必須是 {VALID_PHASES} 之一"}, status=400)
    if hasattr(session, "set_phase"):
        session.set_phase(phase, "manual")
    else:
        session.phase = phase
    session.emit_meeting()
    return web.json_response({"ok": True, "phase": phase})


async def _events_handler(request: web.Request) -> web.StreamResponse:
    session: SessionLike = request.app[SESSION_KEY]
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    stream = _Stream(session)

    def on_event(event: Event) -> None:
        stream.offer(event)

    # 先送 snapshot（連線當下已有的全部事件），再訂閱後續——
    # 中間有事件進來也不會漏，因為訂閱是在讀完 snapshot 之後才註冊，
    # 但 snapshot 本身已經涵蓋讀取當下 session.events 的內容。
    snapshot = [dataclasses.asdict(e) for e in session.events]
    await resp.write(_sse_message("snapshot", snapshot))

    session.subscribers.append(on_event)
    _STREAMS.append(stream)
    try:
        while True:
            try:
                event = await asyncio.wait_for(stream.queue.get(), timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                stream.mark_idle_if_drained()  # 沒事件可寫＝已經寫乾淨了
                continue
            await resp.write(_sse_message(event.kind, dataclasses.asdict(event)))
            stream.mark_idle_if_drained()  # write 回來了才算送出，flush_streams 等的就是這個
    except (asyncio.CancelledError, ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        if on_event in session.subscribers:
            session.subscribers.remove(on_event)
        if stream in _STREAMS:
            _STREAMS.remove(stream)
    return resp


async def _end_handler(request: web.Request) -> web.Response:
    """T3a：UI 的「結束會議」。只是按下與 `kill -TERM` 完全同一個開關
    （`live.py` 注入的 `main_task.cancel`），收尾一律走 `live.shutdown()`，
    這裡不做任何寫檔或清理——多一條收尾路徑就等於多一種漏寫總結的方式。

    冪等由 `session.request_end()` 那端保證（`live.Session` 的 `ending` 旗標）：
    連按兩次都是 200，但只有第一次真的送出取消。第二次送取消會打斷正在跑的
    `live.shutdown()`——`bot.close()` 與 events.jsonl 正好在那時候要做完。
    """
    session: SessionLike = request.app[SESSION_KEY]
    if not session.request_end():
        return web.json_response(
            {"ok": False, "error": "此模式沒有進行中的會議可結束（回放模式）"}, status=409)
    return web.json_response({"ok": True})


def _build_app(session: SessionLike) -> web.Application:
    app = web.Application()
    app[SESSION_KEY] = session
    app.router.add_get("/", _index_handler)
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/events", _events_handler)
    app.router.add_post("/phase", _phase_handler)
    app.router.add_post("/end", _end_handler)
    return app


async def serve(session: SessionLike, port: int) -> None:
    """啟動觀戰 UI 伺服器；掛著跑直到被取消（`main_async` 的 `asyncio.gather` 收 Ctrl-C）。"""
    app = _build_app(session)
    # shutdown_timeout：`live.shutdown()` cancel 掉這個 task 時會跑 `runner.cleanup()`，
    # 它會等所有還開著的連線收尾。SSE 連線本質上「永遠沒收完」，用預設的 60 秒
    # 會讓整個收尾卡住（實測：一個觀戰分頁連著時，cancel → task 收尾要 121 秒，
    # bot.close() 前的 gather 就吊在那裡）。總結已經由 `flush_streams()` 推出去了，
    # 這裡沒有必要再等 client 優雅斷線——直接砍掉連線即可。
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"觀戰 UI：http://localhost:{port}")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


# ── 回放模式：無 Discord 時看畫面 ──────────────────────────────────


class ReplaySession:
    """從 events.jsonl 讀出的事件，依 `t` 時間差重播——滿足 SessionLike 介面。"""

    def __init__(self, events: list[Event]):
        self.events: list[Event] = []
        self.subscribers: list[Callable[[Event], None]] = []
        self.phase = VALID_PHASES[0]
        self._all_events = events

    def request_end(self) -> bool:
        """回放模式沒有進行中的會議可以結束——回 False，讓 `POST /end` 回 409 而不是
        假裝成功。回放伺服器本來就靠 Ctrl-C 收掉，沒有要寫的總結。"""
        return False

    def emit_meeting(self) -> None:
        """no-op：回放沒有真正的 MeetingState 可以重建 meeting 事件的其他欄位
        （topic/duration_min/participants），POST /phase 在回放模式下只更新
        `self.phase` 供之後讀取，不會讓已連線的頁面立即看到新階段高亮——
        這是回放模式的已知限制，不是要修的 bug（見 T-H 工單）。"""

    async def replay(self, speed: float = 1.0) -> None:
        loop = asyncio.get_event_loop()
        start = loop.time()
        for event in self._all_events:
            target = event.t / speed if speed else 0.0
            elapsed = loop.time() - start
            if target > elapsed:
                await asyncio.sleep(target - elapsed)
            self.events.append(event)
            for sub in list(self.subscribers):
                try:
                    sub(event)
                except Exception as e:  # noqa: BLE001
                    print(f"    ⚠️ 回放訂閱者例外：{type(e).__name__}: {e}")
        print(f"(回放結束，共 {len(self.events)} 筆事件；伺服器繼續掛著跑)")


def _load_events(path: Path) -> list[Event]:
    events: list[Event] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            events.append(Event(kind=raw["kind"], t=raw["t"], data=raw["data"]))
    return events


async def _replay_main(path: Path, port: int, speed: float) -> None:
    session = ReplaySession(_load_events(path))
    await asyncio.gather(serve(session, port), session.replay(speed))


def main() -> None:
    ap = argparse.ArgumentParser(description="觀戰 UI 回放模式：無 Discord 時看畫面")
    ap.add_argument("--replay", required=True, help="events.jsonl 檔案路徑")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--speed", type=float, default=1.0, help="回放倍速")
    args = ap.parse_args()
    asyncio.run(_replay_main(Path(args.replay), args.port, args.speed))


if __name__ == "__main__":
    main()
