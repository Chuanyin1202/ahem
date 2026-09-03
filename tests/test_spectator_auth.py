"""上游 237e945 的未授權控制風險，在雙角色短效 session 模型下的回歸測試。"""
from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from meeting_host import spectator
from test_spectator import FakeSession

VIEWER_TOKEN = "v" * 32
OPERATOR_TOKEN = "o" * 32


def _serve() -> TestClient:
    security = spectator.SpectatorSecurity(VIEWER_TOKEN, OPERATOR_TOKEN)
    return TestClient(TestServer(spectator._build_app(FakeSession(), security)))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_post_end_without_token_is_unauthorized_and_has_no_side_effect():
    async def body() -> None:
        client = _serve()
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            response = await client.post("/end")
            assert response.status == 401
            assert session.end_calls == 0
        finally:
            await client.close()

    asyncio.run(body())


def test_viewer_cannot_end_meeting_but_operator_can():
    async def body() -> None:
        client = _serve()
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            denied = await client.post("/end", headers=_bearer(VIEWER_TOKEN))
            assert denied.status == 401
            assert session.end_calls == 0
            allowed = await client.post("/end", headers=_bearer(OPERATOR_TOKEN))
            assert allowed.status == 200
            assert session.end_calls == 1
        finally:
            await client.close()

    asyncio.run(body())


def test_phase_change_requires_operator_and_rejected_request_does_not_mutate():
    async def body() -> None:
        client = _serve()
        session = client.server.app[spectator.SESSION_KEY]
        before = session.phase
        await client.start_server()
        try:
            denied = await client.post(
                "/phase", json={"phase": "呻吟區"}, headers=_bearer(VIEWER_TOKEN)
            )
            assert denied.status == 401
            assert session.phase == before
            allowed = await client.post(
                "/phase", json={"phase": "呻吟區"}, headers=_bearer(OPERATOR_TOKEN)
            )
            assert allowed.status == 200
            assert session.phase == "呻吟區"
        finally:
            await client.close()

    asyncio.run(body())


def test_index_and_health_are_public_but_transcript_stream_is_not():
    async def body() -> None:
        client = _serve()
        await client.start_server()
        try:
            assert (await client.get("/health")).status == 200
            assert (await client.get("/")).status == 200
            assert (await client.get("/events")).status == 401
        finally:
            await client.close()

    asyncio.run(body())


def test_query_string_token_is_rejected():
    async def body() -> None:
        client = _serve()
        session = client.server.app[spectator.SESSION_KEY]
        await client.start_server()
        try:
            response = await client.post(f"/end?token={OPERATOR_TOKEN}")
            assert response.status == 401
            assert session.end_calls == 0
        finally:
            await client.close()

    asyncio.run(body())
