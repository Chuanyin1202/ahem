"""POST 端點的權杖守門。

背景：觀戰服務綁 0.0.0.0，且預計掛在 Cloudflare Tunnel 後面對外（ahem.eighti.app）。
`POST /end` 走的是跟 SIGTERM 完全同一條收尾路徑，會寫記錄然後結束會議——不設防
等於任何知道網址的人都能中止進行中的 demo，而網址 demo 當下會投影出去。

刻意不做「來源 IP 是本機就放行」：cloudflared 在同一台機器上連 localhost，
過 tunnel 的請求來源全是 127.0.0.1，那條捷徑會把整個網際網路當成本機。
"""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from meeting_host import spectator

from test_spectator import FakeSession

TOKEN = "s3cret-token"


def _serve(token):
    return TestClient(TestServer(spectator._build_app(FakeSession(), token)))


def test_post_end_without_token_is_forbidden_and_does_not_end_meeting():
    async def body():
        client = _serve(TOKEN)
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            resp = await client.post("/end")
            assert resp.status == 403
            assert (await resp.json())["ok"] is False
            # 關鍵不變式：擋下來的請求不能碰到收尾開關
            assert session.end_calls == 0
        finally:
            await client.close()
    asyncio.run(body())


def test_post_end_with_wrong_token_is_forbidden():
    async def body():
        client = _serve(TOKEN)
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            resp = await client.post("/end", headers={spectator.TOKEN_HEADER: "wrong"})
            assert resp.status == 403
            assert session.end_calls == 0
        finally:
            await client.close()
    asyncio.run(body())


def test_post_end_with_token_works():
    async def body():
        client = _serve(TOKEN)
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            resp = await client.post("/end", headers={spectator.TOKEN_HEADER: TOKEN})
            assert resp.status == 200
            assert session.end_calls == 1
        finally:
            await client.close()
    asyncio.run(body())


def test_post_phase_needs_token_and_does_not_change_phase():
    async def body():
        client = _serve(TOKEN)
        session = client.server.app[spectator.SESSION_KEY]
        before = session.phase
        await client.start_server()
        try:
            resp = await client.post("/phase", json={"phase": "呻吟區"})
            assert resp.status == 403
            assert session.phase == before

            resp = await client.post("/phase", json={"phase": "呻吟區"},
                                     headers={spectator.TOKEN_HEADER: TOKEN})
            assert resp.status == 200
            assert session.phase == "呻吟區"
        finally:
            await client.close()
    asyncio.run(body())


def test_get_endpoints_stay_public():
    """唯讀的東西不擋——觀戰畫面本來就是要給人看的。"""
    async def body():
        client = _serve(TOKEN)
        await client.start_server()
        try:
            assert (await client.get("/health")).status == 200
            assert (await client.get("/")).status == 200
        finally:
            await client.close()
    asyncio.run(body())


def test_no_token_means_no_gate():
    """`_build_app(session)` 不給 token ⇒ 不設防，維持既有測試與回放模式的行為。
    正式啟動走 `serve()`，那條路一定會產生 token。"""
    async def body():
        client = _serve(None)
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            assert (await client.post("/end")).status == 200
            assert session.end_calls == 1
        finally:
            await client.close()
    asyncio.run(body())


# ── 瀏覽器端：唯讀觀眾看不到操作項，網址列不留權杖 ──────────────────────
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "synthetic-meeting.events.jsonl"


def _page_probe(query: str) -> dict:
    """把 examples 的會議餵進回放 session，用瀏覽器開 `/`＋query，回報操作項狀態。"""
    async def body():
        session = spectator.ReplaySession(spectator._load_events(SAMPLE))
        server = TestServer(spectator._build_app(session, TOKEN))
        await server.start_server()
        try:
            await session.replay(speed=1_000_000)
            url = str(server.make_url("/")) + query

            def check() -> dict:
                with sync_playwright() as p:
                    browser = p.chromium.launch(); page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(url); page.wait_for_timeout(400)
                    out = page.evaluate(
                        '({endHidden: document.getElementById("btn-end").hidden,'
                        '  url: location.href,'
                        '  stored: (function(){try{return sessionStorage.getItem("ahem-token")}'
                        '                      catch(e){return null}})()})')
                    out["errors"] = errors
                    browser.close()
                    return out
            return await asyncio.get_running_loop().run_in_executor(None, check)
        finally:
            await server.close()
    return asyncio.run(body())


def test_reader_without_token_sees_no_end_button():
    out = _page_probe("")
    assert out["errors"] == []
    assert out["endHidden"] is True
    assert out["stored"] in (None, "")


def test_operator_url_keeps_token_but_strips_it_from_the_address_bar():
    """demo 當下這個畫面會投影出去，權杖不能留在網址列。"""
    out = _page_probe("?k=" + TOKEN)
    assert out["errors"] == []
    assert out["endHidden"] is False
    assert "k=" not in out["url"], out["url"]
    assert out["stored"] == TOKEN
