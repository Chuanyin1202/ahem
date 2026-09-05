import asyncio
import time

import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from aiohttp.test_utils import TestClient, TestServer
from meeting_host.enterprise import create_app
from test_enterprise import setup, identities, EVENTS, ORIGIN, login

HEADERS = {'Origin': ORIGIN}


def test_pagination_policy_and_expiry(tmp_path):
    async def run():
        ws = setup(tmp_path)
        mids = [ws.ingest(identities()[0], EVENTS, 'team', 7) for _ in range(3)]
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as c:
            await login(c, 'operator')
            data = await (await c.get('/api/analytics?limit=1&offset=1')).json()
            assert data['count'] == 1 and data['total_count'] == 3
            assert data['total_interventions'] == 3
            data = await (await c.get('/api/analytics?q='+mids[0])).json()
            assert data['total_count'] == 1 and data['meetings'][0]['id'] == mids[0]
            path = '/api/meetings/'+mids[0]+'/policy'
            response = await c.post(path, json={'days':1,'policy':'team'}, headers=HEADERS)
            assert response.status == 200
            expiry = (await response.json())['expires_at']
            response = await c.post(path, json={'days':30,'policy':'team'}, headers=HEADERS)
            assert (await response.json())['expires_at'] == expiry
            assert (await c.post(path,json={'days':1,'policy':'regulated'},headers=HEADERS)).status == 403
            await login(c,'cleared')
            assert (await c.post(path,json={'days':1,'policy':'regulated'},headers=HEADERS)).status == 200
            assert (await c.post(path,json={'days':1,'policy':'team'},headers=HEADERS)).status == 400
            ws.db.execute('UPDATE meetings SET expires=? WHERE id=?',(time.time()-1,mids[0])); ws.db.commit()
            assert (await c.get('/api/meetings/'+mids[0]+'/access')).status == 404
            assert (await (await c.get('/api/analytics')).json())['total_count'] == 2
    asyncio.run(run())


@pytest.mark.parametrize('query',['limit=0','limit=101','offset=-1','offset=abc','q=SECRET','policy=bad'])
def test_invalid_queries(tmp_path,query):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,'operator')
            assert (await c.get('/api/analytics?'+query)).status == 400
    asyncio.run(run())


@pytest.mark.parametrize('path',['login','meetings','health','members/revoke-sessions'])
def test_non_object_request(tmp_path,path):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,'operator')
            assert (await c.post('/api/'+path,json=[],headers=HEADERS)).status == 400
    asyncio.run(run())


def test_sessions_history_and_audit(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'viewer')
            viewer_sid=next(iter(ws.sessions))
            await login(c,'operator')
            response=await c.get('/api/members'); data=await response.json()
            assert len(data['members']) == 6
            assert 'token' not in str(data) and 'other' not in str(data)
            assert (await c.post('/api/members/revoke-sessions',json={'actor':'other'},headers=HEADERS)).status == 404
            assert (await c.post('/api/members/revoke-sessions',json={'actor':'cleared'},headers=HEADERS)).status == 403
            assert (await c.post('/api/members/revoke-sessions',json={'actor':'viewer'},headers=HEADERS)).status == 200
            assert viewer_sid not in ws.sessions
            for state in ['ok','ok','degraded']:
                assert (await c.post('/api/health',json={'component':'tts','state':state},headers=HEADERS)).status == 200
            data=await (await c.get('/api/health/history')).json()
            assert [x['state'] for x in data['entries']] == ['degraded','ok']
            assert 'SECRET' not in str(data)
            data=await (await c.get('/api/audit?limit=1&outcome=ok')).json()
            assert len(data['entries']) == 1 and data['total_count'] > 1
            await c.post('/api/logout-all',json={},headers=HEADERS)
            assert (await c.get('/api/me')).status == 401
            await login(c,'other')
            assert (await (await c.get('/api/health/history')).json())['entries'] == []
    asyncio.run(run())


def test_revoked_session_during_body_upload(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            started=asyncio.Event(); release=asyncio.Event()
            async def body():
                yield b'{"component":"tts",'
                started.set(); await release.wait()
                yield b'"state":"ok"}'
            task=asyncio.create_task(c.post('/api/health',data=body(),headers={**HEADERS,'Content-Type':'application/json'}))
            await started.wait()
            ws.sessions.clear(); release.set()
            assert (await task).status == 401
            assert ws.db.execute('SELECT count(*) FROM health').fetchone()[0] == 0
    asyncio.run(run())
