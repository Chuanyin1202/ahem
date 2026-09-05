import asyncio
import time
import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from aiohttp.test_utils import TestClient,TestServer
from meeting_host.enterprise import create_app
from test_enterprise import setup,ORIGIN,login

H={'Origin':ORIGIN}

def test_alert_lifecycle_and_read_isolation(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            async def report(state):
                assert (await c.post('/api/health',json={'component':'tts','state':state,'error':'PRIVATE'},headers=H)).status==200
            await report('degraded')
            assert (await (await c.get('/api/notifications')).json())['total_count']==0
            assert (await c.post('/api/alert-rules',json={'component':'tts','enabled':True},headers=H)).status==200
            await report('degraded');await report('unavailable');await report('ok')
            data=await (await c.get('/api/notifications')).json()
            assert data['total_count']==3
            assert [n['kind'] for n in data['entries']]==['recovered','unavailable','degraded']
            assert 'PRIVATE' not in str(data)
            incidents=(await (await c.get('/api/incidents')).json())['entries']
            assert len(incidents)==1 and incidents[0]['status']=='open' and incidents[0]['severity']=='critical'
            nid=data['entries'][0]['id']
            assert (await c.post('/api/notifications/'+nid+'/read',json={},headers=H)).status==200
            assert (await (await c.get('/api/notifications')).json())['entries'][0]['is_read']==1
            await login(c,'support')
            assert (await (await c.get('/api/notifications')).json())['entries'][0]['is_read']==0
            await login(c,'other')
            assert (await (await c.get('/api/notifications')).json())['total_count']==0
            assert (await c.post('/api/notifications/'+nid+'/read',json={},headers=H)).status==404
    asyncio.run(run())

def test_stale_report_and_disable(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            await c.post('/api/alert-rules',json={'component':'tts','enabled':True},headers=H)
            assert ws.db.execute('SELECT count(*) FROM notifications').fetchone()[0]==0
            await c.post('/api/health',json={'component':'tts','state':'ok'},headers=H)
            ws.db.execute('UPDATE health SET at=?',(time.time()-301,));ws.db.commit()
            ws.expire();ws.expire()
            assert ws.db.execute('SELECT kind FROM notifications').fetchone()[0]=='unknown'
            assert ws.db.execute('SELECT count(*) FROM notifications').fetchone()[0]==1
            await c.post('/api/alert-rules',json={'component':'tts','enabled':False},headers=H)
            await c.post('/api/health',json={'component':'tts','state':'unavailable'},headers=H)
            assert ws.db.execute('SELECT count(*) FROM notifications').fetchone()[0]==1
    asyncio.run(run())

@pytest.mark.parametrize('role',['viewer','manager','observer','support'])
def test_rules_and_notification_roles(tmp_path,role):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,role)
            assert (await c.post('/api/alert-rules',json={'component':'tts','enabled':True},headers=H)).status==403
            assert (await c.get('/api/notifications')).status==(200 if role=='support' else 403)
    asyncio.run(run())

def test_rule_validation(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,'operator')
            assert (await c.post('/api/alert-rules',json={'component':'anything','enabled':True},headers=H)).status==400
            assert (await c.post('/api/alert-rules',json={'component':'tts','enabled':'yes'},headers=H)).status==400
            assert (await c.post('/api/alert-rules',json={'component':'tts','enabled':True})).status==403
    asyncio.run(run())
