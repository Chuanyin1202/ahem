"""T-D／T-H：觀戰 UI 的 SSE 伺服器（`src/meeting_host/spectator.py`）。

涵蓋（見 T-D／T-H 驗收標準）：
(a) `GET /health` 200 且回報目前 `events` 筆數
(b) `GET /` 回傳 index.html（text/html）
(c) `GET /events`：連線先收到 `event: snapshot`（`session.events` 全部），
    之後對 session 直接呼叫 subscriber（模擬 `Session.emit`）能收到對應 kind 的一筆
(d) 斷線後 `session.subscribers` 數回到連線前的值（不洩漏 callback）
(e) `_offer`（佇列滿了丟最舊，不阻塞 emit）
(f) `POST /phase`：合法值 → 200、`session.phase` 更新、subscribers 收到 `meeting` 事件；
    非法值 → 400、`session.phase` 不變
(g) T3a `POST /end`：呼叫 `session.request_end()` 一次、回 `{"ok": true}`；
    重複呼叫冪等（第二次仍 200）；回放模式（`ReplaySession`）回 409、不 500
"""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from meeting_host.events import Event
from meeting_host import spectator


class FakeSession:
    """最小假 session：`events`、`subscribers`、`phase`、`emit_meeting`
    （同 live.Session 的介面子集，見 spectator.SessionLike）。"""

    def __init__(self, end_result: bool = True):
        self.events: list[Event] = []
        self.subscribers: list = []
        self.phase = "發散期"
        self.end_calls = 0
        self._end_result = end_result

    def request_end(self) -> bool:
        self.end_calls += 1
        return self._end_result

    def emit(self, kind: str, data: dict) -> None:
        event = Event(kind, float(len(self.events)), data)
        self.events.append(event)
        for sub in self.subscribers:
            sub(event)

    def emit_meeting(self) -> None:
        self.emit("meeting", {"topic": "t", "duration_min": 30, "phase": self.phase,
                               "participants": ["A"]})


async def _read_sse_message(reader) -> tuple[str, dict]:
    """讀一則 SSE 訊息（跳過 `: ping` 註解行），回傳 (event 名稱, 解析後的 data)。"""
    while True:
        event_line = (await reader.readline()).decode("utf-8").strip()
        if event_line.startswith(":"):
            await reader.readline()  # 註解後的空行
            continue
        assert event_line.startswith("event: "), event_line
        event_name = event_line.removeprefix("event: ")
        data_line = (await reader.readline()).decode("utf-8").strip()
        assert data_line.startswith("data: "), data_line
        data = json.loads(data_line.removeprefix("data: "))
        await reader.readline()  # 分隔用的空行
        return event_name, data


def test_health_and_index_and_sse_snapshot_then_live_event():
    async def body():
        session = FakeSession()
        session.emit("meeting", {"topic": "t", "duration_min": 30, "phase": "發散期",
                                  "participants": ["A"]})

        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            # (a) /health
            resp = await client.get("/health")
            assert resp.status == 200
            body_json = await resp.json()
            # public_read=True：這個 app 沒設參與者權杖，讀取端本來就不設防
            assert body_json == {"ok": True, "events": 1, "public_read": True}

            # (b) /
            resp = await client.get("/")
            assert resp.status == 200
            assert "text/html" in resp.headers["Content-Type"]
            text = await resp.text()
            assert "<html" in text

            # (c) /events：先收 snapshot，再收即時事件
            assert len(session.subscribers) == 0
            resp = await client.get("/events")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")

            name, data = await asyncio.wait_for(_read_sse_message(resp.content), timeout=2)
            assert name == "snapshot"
            assert isinstance(data, list) and len(data) == 1
            assert data[0]["kind"] == "meeting"

            assert len(session.subscribers) == 1  # 連線期間多了一個 subscriber

            session.emit("speaking", {"speaker": "A", "active": True})
            name, data = await asyncio.wait_for(_read_sse_message(resp.content), timeout=2)
            assert name == "speaking"
            assert data["kind"] == "speaking"
            assert data["data"] == {"speaker": "A", "active": True}

            # (d) 斷線後 subscribers 數回到連線前的值
            resp.close()
            for _ in range(40):
                if not session.subscribers:
                    break
                session.emit("speaking", {"speaker": "A", "active": False})  # 逼一次寫入偵測斷線
                await asyncio.sleep(0.05)
            assert len(session.subscribers) == 0
        finally:
            await client.close()

    asyncio.run(body())


def test_post_phase_valid_updates_session_and_notifies_subscribers():
    async def body():
        session = FakeSession()
        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            received = []
            session.subscribers.append(received.append)

            resp = await client.post("/phase", json={"phase": "呻吟區"})
            assert resp.status == 200
            body_json = await resp.json()
            assert body_json == {"ok": True, "phase": "呻吟區"}
            assert session.phase == "呻吟區"

            assert len(received) == 1
            assert received[0].kind == "meeting"
            assert received[0].data["phase"] == "呻吟區"
        finally:
            await client.close()

    asyncio.run(body())


def test_post_phase_invalid_returns_400_and_leaves_session_unchanged():
    async def body():
        session = FakeSession()
        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post("/phase", json={"phase": "不存在的階段"})
            assert resp.status == 400
            assert session.phase == "發散期"

            resp = await client.post("/phase", json={})
            assert resp.status == 400

            resp = await client.post("/phase", data="not json")
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(body())


def test_offer_drops_oldest_when_queue_full():
    async def body():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        e1 = Event("a", 0.0, {})
        e2 = Event("b", 1.0, {})
        spectator._offer(queue, e1)
        spectator._offer(queue, e2)  # 佇列滿了，丟最舊（e1），留下 e2
        assert queue.qsize() == 1
        assert queue.get_nowait() is e2

    asyncio.run(body())


# ── (g) T3a：POST /end ─────────────────────────────────────────────


def test_post_end_calls_request_end_once_and_returns_ok():
    """A1：`POST /end` 只按 `session.request_end()` 這一個開關（live.py 注入的
    `main_task.cancel`），handler 本身不做任何收尾。"""
    async def body():
        session = FakeSession()
        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post("/end")
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
            assert session.end_calls == 1
        finally:
            await client.close()

    asyncio.run(body())


def test_post_end_is_idempotent():
    """A1：連按兩次「結束會議」不能炸——第二次一樣 200。
    （真實情境：`main_task.cancel()` 打在已取消的 task 上只回 False、不拋。）"""
    async def body():
        session = FakeSession()
        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            for _ in range(2):
                resp = await client.post("/end")
                assert resp.status == 200
                assert await resp.json() == {"ok": True}
            assert session.end_calls == 2
        finally:
            await client.close()

    asyncio.run(body())


def test_post_end_in_replay_mode_returns_409_not_500():
    """A2：回放模式沒有進行中的會議可結束——用真的 `ReplaySession`（不是假 session）
    驗它的 `request_end()` 回 False，端點回 4xx 而不是 500。"""
    async def body():
        session = spectator.ReplaySession([])
        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post("/end")
            assert resp.status == 409
            payload = await resp.json()
            assert payload["ok"] is False
            assert payload["error"]
        finally:
            await client.close()

    asyncio.run(body())


def test_live_session_without_injection_refuses_end():
    """A2 的另一半：`live.Session` 沒被注入 `request_end` 時預設回 False——
    不能假裝結束成功（例如 spectator 單獨跑、或未來有人忘了接線）。"""
    from meeting_host.live import Session
    from meeting_host.state import MeetingState

    session = Session(MeetingState(topic="t", duration_min=30, participants=[]))
    assert session.request_end() is False


def test_flush_streams_returns_true_when_no_client_connected():
    """C1：沒有 SSE client 連著時 flush 立刻回 True，不會白等 3 秒。"""
    async def body():
        session = FakeSession()
        assert await spectator.flush_streams(session, timeout=0.1) is True

    asyncio.run(body())


def test_flush_streams_writes_pending_event_before_returning():
    """C1 的確定性證據：`session.emit()` 只是把事件同步塞進 SSE handler 的佇列
    （`_Stream.offer`），這一刻還沒有任何 byte 寫出去；`flush_streams()` 回 True
    才代表 `resp.write()` 已經完成。

    `live.shutdown()` 就是靠這個訊號決定「可以 cancel 掉 serve() 了」——
    沒有它就會在 handler 還沒被排程時把伺服器砍掉，總結永遠送不出去。
    """
    async def body():
        session = FakeSession()
        server = TestServer(spectator._build_app(session))
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/events")
            name, _ = await asyncio.wait_for(_read_sse_message(resp.content), timeout=2)
            assert name == "snapshot"
            assert len(spectator._STREAMS) == 1
            stream = spectator._STREAMS[0]
            assert stream.idle.is_set()  # 沒有待寫事件

            session.emit("minutes", {"minutes_md": "# 會議產出", "host_md": "# 主持記錄"})
            # emit 是同步 callback，中間沒有 await——這一刻事件只在佇列裡，還沒寫出去
            assert not stream.idle.is_set()

            assert await spectator.flush_streams(session, timeout=3.0) is True
            assert stream.idle.is_set()

            name, data = await asyncio.wait_for(_read_sse_message(resp.content), timeout=2)
            assert name == "minutes"
            assert data["data"]["minutes_md"] == "# 會議產出"
        finally:
            await client.close()

    asyncio.run(body())


def test_flush_streams_times_out_and_returns_false_when_client_never_drains():
    """flush 不是無限等：client 半死不活時回 False，呼叫端照樣往下收尾。
    用一個永遠不會被讀走的假 stream 直接驗這個上限。"""
    async def body():
        session = FakeSession()
        stream = spectator._Stream(session)
        stream.offer(Event("minutes", 0.0, {}))  # idle 被清掉，且沒有 handler 會來讀
        spectator._STREAMS.append(stream)
        try:
            assert await spectator.flush_streams(session, timeout=0.2) is False
        finally:
            spectator._STREAMS.remove(stream)

    asyncio.run(body())


def test_public_base_url_falls_back_loudly_when_unset(monkeypatch):
    """沒設 AHEM_PUBLIC_URL 時要退回 localhost，**並且說得出來自己退回了**。

    2026-09-05 的事故：真實會議該跑在部署主機、對外走 tunnel，卻被跑到開發機上。
    開發機沒有 AHEM_PUBLIC_URL，啟動橫幅安靜地印了 `http://localhost:8765/?k=…`，
    那串網址被交給外網的人，完全打不開。退回 localhost 本身沒錯，**不出聲才是錯**——
    所以第二個回傳值存在，讓呼叫端能印警告。
    """
    from meeting_host.spectator import public_base_url
    monkeypatch.delenv("AHEM_PUBLIC_URL", raising=False)
    assert public_base_url(8765) == ("http://localhost:8765", False)

    monkeypatch.setenv("AHEM_PUBLIC_URL", "https://host.example.com/")
    assert public_base_url(8765) == ("https://host.example.com", True)   # 尾斜線要去掉

    monkeypatch.setenv("AHEM_PUBLIC_URL", "   ")      # 只有空白＝沒設
    assert public_base_url(8765) == ("http://localhost:8765", False)


def test_banner_and_discord_notice_resolve_the_same_url(monkeypatch):
    """人眼看的橫幅與貼進 Discord 的網址必須來自同一支解析。

    出事的形狀就是兩邊各自讀環境變數：Discord 通知讀了 AHEM_PUBLIC_URL、
    啟動橫幅沒讀，同一場會議吐出兩種網址，而看橫幅的人不會知道。
    這條測試釘住「live.py 用的就是 spectator 那一支」，不是各寫各的。
    """
    import inspect
    from meeting_host import live, spectator
    src = inspect.getsource(live.main_async)
    assert "public_base_url" in src, "live.py 必須共用 spectator.public_base_url"
    assert "os.environ.get(\"AHEM_PUBLIC_URL\"" not in src, "live.py 不該自己再讀一次環境變數"
    monkeypatch.setenv("AHEM_PUBLIC_URL", "https://host.example.com")
    assert spectator.public_base_url(1234)[0] == "https://host.example.com"
