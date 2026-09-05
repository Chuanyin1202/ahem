"""Eight review findings: synthetic-only regression coverage."""
import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
pytest.importorskip('cryptography')
from aiohttp.test_utils import TestClient, TestServer
from meeting_host.enterprise import create_app
from meeting_host.enterprise_security import FileKEK
from meeting_host.enterprise_backup import snapshot, maintain
from test_enterprise import setup, identities, EVENTS, ORIGIN, login

H = {'Origin': ORIGIN}

def test_restricted_delete_and_audit_target(tmp_path):
    async def run():
        ws = setup(tmp_path)
        mid = ws.ingest(identities()[0], EVENTS, 'regulated', 7)
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as c:
            await login(c, 'operator')
            assert (await c.delete('/api/meetings/'+mid, headers=H)).status == 403
            assert ws.db.execute('SELECT count(*) FROM meetings').fetchone()[0] == 1
            await login(c, 'cleared')
            assert (await c.delete('/api/meetings/'+mid, headers=H)).status == 200
            rows = [dict(r) for r in ws.db.execute("SELECT target,outcome FROM audit WHERE action='delete'")]
            assert rows == [{'target':mid,'outcome':'denied'}, {'target':mid,'outcome':'ok'}]
    asyncio.run(run())

def test_login_failure_and_volume_limits(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path), ORIGIN))) as c:
            for _ in range(12):
                await login(c, 'operator')
                assert (await c.post('/api/logout', json={}, headers=H)).status == 200
            for _ in range(10):
                assert (await c.post('/api/login', json={'token':'wrong'}, headers=H)).status == 401
            assert (await c.post('/api/login', json={'token':'wrong'}, headers=H)).status == 429
    asyncio.run(run())

def test_successful_login_total_cap(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path), ORIGIN))) as c:
            for _ in range(120):
                await login(c, 'viewer')
            assert (await c.post('/api/login',json={'token':identities()[1]['token']},headers=H)).status == 429
    asyncio.run(run())

def test_concurrent_failed_logins_are_counted(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path), ORIGIN))) as c:
            release=asyncio.Event()
            async def body():
                yield b'{"token":'
                await release.wait()
                yield b'"wrong"}'
            tasks=[asyncio.create_task(c.post('/api/login',data=body(),headers={**H,'Content-Type':'application/json'})) for _ in range(12)]
            await asyncio.sleep(.05)
            release.set()
            statuses=[r.status for r in await asyncio.gather(*tasks)]
            assert statuses.count(401)==10
            assert statuses.count(429)==2
    asyncio.run(run())

def test_taipei_midnight_date(tmp_path):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026,9,5,16,30,tzinfo=timezone.utc).astimezone(tz)
    async def run():
        ws=setup(tmp_path); mid=ws.ingest(identities()[0], EVENTS, 'team', 7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            with patch('meeting_host.enterprise.datetime',Frozen):
                assert (await c.post('/api/meetings/'+mid+'/date',json={'day':'2026-09-06'},headers=H)).status==200
                assert (await c.post('/api/meetings/'+mid+'/date',json={'day':'2026-09-07'},headers=H)).status==400
    asyncio.run(run())

def test_effective_session_expiry(tmp_path):
    async def run():
        ws=setup(tmp_path)
        token=ws.issue_credential({'id':'short','tenant':'org-a','role':'viewer'},1)['token']
        end=time.time()+10
        ws.db.execute('UPDATE member_credentials SET expires=?',(end,));ws.db.commit()
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            r=await c.post('/api/login',json={'token':token},headers=H)
            assert r.status==200
            assert int(r.cookies['enterprise']['max-age'])<=10
            assert (await (await c.get('/api/me')).json())['expires_at']==end
            with patch('meeting_host.enterprise.time.time',return_value=end+1):
                assert (await c.get('/api/me')).status==401
    asyncio.run(run())

def test_kek_rejects_fifo_and_large_file(tmp_path):
    fifo=tmp_path/'fifo';os.mkfifo(fifo,0o600)
    with pytest.raises(ValueError,match='一般檔案'):
        FileKEK(fifo).load()
    large=tmp_path/'large';large.write_text('A'*4097);large.chmod(0o600)
    with pytest.raises(ValueError,match='too large'):
        FileKEK(large).load()

def test_backup_failed_write_leaves_no_final(tmp_path):
    ws=setup(tmp_path);ws.ingest(identities()[0],EVENTS,'team',7)
    archives=tmp_path/'archives';archives.mkdir(mode=0o700)
    target=archives/'ahem-snapshot-123-abcdef12.enc'
    with patch('meeting_host.enterprise_backup.os.fsync',side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            snapshot(tmp_path/'store.db',target,b'k'*32)
    assert list(archives.iterdir())==[]
    assert maintain(tmp_path/'store.db',archives,b'k'*32)['verified']==1
    ws.db.close()

def test_retention_recovers_after_database_lock(tmp_path):
    async def run():
        ws=setup(tmp_path);ws.ingest(identities()[0],EVENTS,'team',7)
        ws.db.execute('UPDATE meetings SET expires=0');ws.db.commit()
        blocker=sqlite3.connect(tmp_path/'store.db');blocker.execute('BEGIN IMMEDIATE')
        app=create_app(ws,ORIGIN);ctx=app.cleanup_ctx[0](app)
        await anext(ctx)
        await asyncio.sleep(.2)
        assert ws.maintenance_failures==1
        blocker.rollback();blocker.close()
        for _ in range(30):
            if ws.db.execute('SELECT count(*) FROM meetings').fetchone()[0]==0:
                break
            await asyncio.sleep(.1)
        assert ws.maintenance_failures==0
        assert ws.db.execute('SELECT count(*) FROM meetings').fetchone()[0]==0
        with pytest.raises(StopAsyncIteration):
            await anext(ctx)
    asyncio.run(run())

def test_legacy_audit_schema_migrates(tmp_path):
    db=sqlite3.connect(tmp_path/'store.db')
    db.execute('CREATE TABLE audit (at REAL, tenant TEXT, actor TEXT, action TEXT, outcome TEXT)')
    db.execute("INSERT INTO audit VALUES (0,'org-a','hash','login','ok')");db.commit();db.close()
    ws=setup(tmp_path)
    assert ws.db.execute('SELECT target FROM audit').fetchone()[0] is None
    ws.db.close()
