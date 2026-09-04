"""真 Chromium 驗證 Token fragment → HttpOnly Cookie → SSE／角色權限完整流程。"""
from __future__ import annotations

import asyncio
import socket

import pytest
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from meeting_host import spectator  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_browser_exchanges_fragment_for_cookie_and_preserves_roles():
    async def body():
        viewer_token, operator_token = "v" * 32, "o" * 32
        port = _free_port()
        origin = f"http://127.0.0.1:{port}"
        security = spectator.SpectatorSecurity(
            viewer_token, operator_token, (origin,), session_ttl_seconds=300)
        session = spectator.ReplaySession([])
        server = TestServer(spectator._build_app(session, security), port=port)
        await server.start_server()
        try:
            def check() -> dict:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()

                    viewer_context = browser.new_context()
                    viewer = viewer_context.new_page()
                    viewer.goto(f"{origin}/#token={viewer_token}")
                    viewer.wait_for_function("location.hash === ''")
                    viewer.wait_for_function("window.__spectator !== undefined")
                    viewer_cookie = next(
                        cookie for cookie in viewer_context.cookies()
                        if cookie["name"] == security.cookie_name)
                    viewer_phase = viewer.evaluate(
                        "async () => (await fetch('/phase', {method:'POST', "
                        "headers:{'Content-Type':'application/json'}, "
                        "body:JSON.stringify({phase:'呻吟區'})})).status")

                    operator_context = browser.new_context()
                    operator = operator_context.new_page()
                    operator.goto(f"{origin}/#token={operator_token}")
                    operator.wait_for_function("location.hash === ''")
                    operator_phase = operator.evaluate(
                        "async () => (await fetch('/phase', {method:'POST', "
                        "headers:{'Content-Type':'application/json'}, "
                        "body:JSON.stringify({phase:'呻吟區'})})).status")
                    operator_cookie = next(
                        cookie for cookie in operator_context.cookies()
                        if cookie["name"] == security.cookie_name)
                    urls = (viewer.url, operator.url)
                    browser.close()
                    return {
                        "viewer_phase": viewer_phase,
                        "operator_phase": operator_phase,
                        "viewer_cookie": viewer_cookie,
                        "operator_cookie": operator_cookie,
                        "urls": urls,
                    }

            return await asyncio.to_thread(check)
        finally:
            await server.close()

    result = asyncio.run(body())
    assert result["viewer_phase"] == 401
    assert result["operator_phase"] == 200
    assert result["viewer_cookie"]["httpOnly"] is True
    assert result["viewer_cookie"]["sameSite"] == "Strict"
    assert result["operator_cookie"]["httpOnly"] is True
    assert "v" * 32 not in result["viewer_cookie"]["value"]
    assert "o" * 32 not in result["operator_cookie"]["value"]
    assert all("#" not in url and "token=" not in url for url in result["urls"])
