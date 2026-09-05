import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from aiohttp.test_utils import TestClient, TestServer
from meeting_host.enterprise import Workspace, create_app, metrics

ORIGIN = 'http://127.0.0.1:8890'
EVENTS = [{'kind': 'meeting', 't': 0, 'data': {'topic': 'SECRET-TOPIC', 'participants': ['SECRET-NAME']}},
          {'kind': 'utterance', 't': 10, 'data': {'speaker': 'SECRET-NAME', 'text': 'SECRET-CONTENT'}},
          {'kind': 'spoken', 't': 20, 'data': {'text': 'SECRET-CHAIR'}}]


def test_actual_ahem_event_contract():
    source = Path(__file__).resolve().parents[1] / 'examples/synthetic-meeting.events.jsonl'
    result = metrics([json.loads(line) for line in source.read_text().splitlines() if line.strip()])
    assert result['interventions'] == 1
    assert result['utterances'] == 15
    assert result['participants'] == 3


@pytest.mark.parametrize('token', [None, 42, [], {}, 'x' * 4097])
def test_malformed_login_token_returns_400(tmp_path, token):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path), ORIGIN))) as c:
            response = await c.post('/api/login', json={'token': token}, headers={'Origin': ORIGIN})
            assert response.status == 400
    asyncio.run(run())


def identities():
    return [dict(id=r, tenant='org-a', role=r, token=(r+'-')*32) for r in
            ['operator', 'viewer', 'manager', 'observer', 'support']] + [
        dict(id='cleared', tenant='org-a', role='operator', token='c'*32, regulated_content=True),
        dict(id='other', tenant='org-b', role='operator', token='b'*32)]


def setup(tmp_path):
    return Workspace(tmp_path/'store.db', b'k'*32, identities())


async def login(client, who):
    token = next(i['token'] for i in identities() if i['id']==who)
    r = await client.post('/api/login', json={'token': token}, headers={'Origin': ORIGIN})
    assert r.status == 200
    assert r.cookies['enterprise']['httponly']
    assert r.cookies['enterprise']['samesite'] == 'Strict'


@pytest.mark.parametrize('role', ['manager','observer','support','viewer','operator','other','cleared'])
def test_role_content_and_tenant_matrix(tmp_path, role):
    async def run():
        ws=setup(tmp_path)
        owner=next(i for i in identities() if i['id']=='operator')
        mid=ws.ingest(owner, EVENTS, 'regulated', 7)
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as c:
            await login(c, role)
            response=await c.post(f'/api/meetings/{mid}/content',json={'purpose':'meeting_review'},headers={'Origin':ORIGIN})
            assert response.status == (200 if role=='cleared' else 404 if role=='other' else 403)
            if role!='cleared':
                assert 'SECRET' not in await response.text()
    asyncio.run(run())


@pytest.mark.parametrize('role', ['manager','observer','support'])
def test_projections_never_send_content_or_names(tmp_path,role):
    async def run():
        ws=setup(tmp_path)
        ws.ingest(identities()[0],EVENTS,'team',7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,role)
            for path in ['analytics','health','audit']:
                r=await c.get('/api/'+path)
                assert 'SECRET' not in await r.text()
            r=await c.get('/api/health' if role=='support' else '/api/analytics')
            assert r.status==200
            if role=='support':
                assert set((await r.json())) == {'components'}
            else:
                data=await r.json()
                assert data['count']==1
                assert data['meetings'][0]['participants']==1
                assert data['meetings'][0]['interventions']==1
    asyncio.run(run())


def test_grant_revoke_and_cookie_logout(tmp_path):
    async def run():
        ws=setup(tmp_path); mid=ws.ingest(identities()[0],EVENTS,'team',7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            r=await c.post(f'/api/meetings/{mid}/grants',json={'actor':'viewer','allow':True},headers={'Origin':ORIGIN});assert r.status==200
            await login(c,'viewer')
            r=await c.post(f'/api/meetings/{mid}/content',json={'purpose':'meeting_review'},headers={'Origin':ORIGIN});assert r.status==200
            assert 'SECRET-CONTENT' in await r.text()
            await login(c,'operator')
            r=await c.post(f'/api/meetings/{mid}/grants',json={'actor':'viewer','allow':False},headers={'Origin':ORIGIN});assert r.status==200
            await login(c,'viewer')
            r=await c.post(f'/api/meetings/{mid}/content',json={'purpose':'meeting_review'},headers={'Origin':ORIGIN});assert r.status==403
            await c.post('/api/logout',json={},headers={'Origin':ORIGIN})
            assert (await c.get('/api/me')).status==401
    asyncio.run(run())


def test_csrf_unauthenticated_expiry_and_health_allowlist(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            assert (await c.get('/api/analytics')).status==401
            await login(c,'operator')
            assert (await c.post('/api/health',json={'component':'tts','state':'ok'})).status==403
            assert (await c.post('/api/health',json={'component':'tts','state':'SECRET'},headers={'Origin':ORIGIN})).status==400
            assert (await c.post('/api/health',json={'component':'tts','state':'ok'},headers={'Origin':ORIGIN})).status==200
            ws.db.execute('UPDATE health SET at=?',(time.time()-301,));ws.db.commit()
            r=await c.get('/api/health');assert all(x['state']=='unknown' for x in (await r.json())['components'])
            ws.sessions={k:(v[0],0) for k,v in ws.sessions.items()}
            assert (await c.get('/api/me')).status==401
    asyncio.run(run())


def test_encryption_restart_retention_and_no_plaintext(tmp_path):
    ws=setup(tmp_path);mid=ws.ingest(identities()[0],EVENTS,'team',7)
    ws.db.close()
    assert b'SECRET' not in (tmp_path/'store.db').read_bytes()
    ws=setup(tmp_path)
    row=ws.db.execute('SELECT * FROM meetings').fetchone()
    assert 'SECRET-CONTENT' in ws.store.decrypt_text(row['blob'],meeting_id=mid,artifact_type='events',purpose='review',operator=True)
    ws.db.execute('UPDATE meetings SET expires=0');ws.db.commit();ws.expire()
    assert ws.db.execute('SELECT count(*) FROM meetings').fetchone()[0]==0
    assert (tmp_path/'store.db').stat().st_mode & 0o777 == 0o600
    ws.db.close()


def test_import_delete_and_audit_do_not_echo_raw_input(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            r=await c.post('/api/meetings',json={'events':EVENTS,'policy':'team','days':1},headers={'Origin':ORIGIN})
            assert r.status==201;mid=(await r.json())['id']
            r=await c.post(f'/api/meetings/{mid}/content',json={'purpose':'SECRET-PURPOSE'},headers={'Origin':ORIGIN});assert r.status==403
            r=await c.get('/api/audit');assert 'SECRET' not in await r.text()
            assert (await c.delete(f'/api/meetings/{mid}',headers={'Origin':ORIGIN})).status==200
            assert (await c.post(f'/api/meetings/{mid}/content',json={'purpose':'meeting_review'},headers={'Origin':ORIGIN})).status==404
    asyncio.run(run())


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,True,'12'])
def test_metrics_reject_invalid_timestamps(bad):
    with pytest.raises(ValueError):metrics([{'kind':'meeting','t':bad,'data':{}}])


@pytest.mark.parametrize('role',['viewer','manager','observer','support'])
def test_read_only_roles_cannot_import_delete_grant_or_report_health(tmp_path,role):
    async def run():
        ws=setup(tmp_path);mid=ws.ingest(identities()[0],EVENTS,'team',7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,role)
            headers={'Origin':ORIGIN}
            for url,body in [('/api/meetings',{'events':EVENTS}),
                             (f'/api/meetings/{mid}/grants',{'actor':'viewer','allow':True}),
                             ('/api/health',{'component':'tts','state':'ok'})]:
                assert (await c.post(url,json=body,headers=headers)).status==403
            assert (await c.delete(f'/api/meetings/{mid}',headers=headers)).status==403
    asyncio.run(run())


def test_login_rate_limit_and_security_headers(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            r=await c.get('/')
            assert r.headers['Cache-Control']=='no-store'
            assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
            for _ in range(10):
                assert (await c.post('/api/login',json={'token':'wrong'},headers={'Origin':ORIGIN})).status==401
            assert (await c.post('/api/login',json={'token':'wrong'},headers={'Origin':ORIGIN})).status==429
    asyncio.run(run())


def test_foreign_tenant_and_foreign_grant_denied(tmp_path):
    async def run():
        ws=setup(tmp_path);mid=ws.ingest(identities()[0],EVENTS,'team',7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'other')
            assert (await (await c.get('/api/analytics')).json())['count']==0
            assert (await c.get(f'/api/meetings/{mid}/grants')).status==404
            assert (await c.delete(f'/api/meetings/{mid}',headers={'Origin':ORIGIN})).status==404
    asyncio.run(run())
