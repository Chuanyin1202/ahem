import asyncio
import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from aiohttp.test_utils import TestClient, TestServer
from meeting_host.enterprise import create_app, Workspace
from test_enterprise import setup, identities, EVENTS, ORIGIN, login

H={'Origin':ORIGIN}

def test_member_disable_restore_persists(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'viewer'); old=list(ws.sessions)[0]
            await login(c,'operator')
            for target,status in [('other',404),('operator',400),('cleared',403)]:
                assert (await c.post('/api/members/status',json={'actor':target,'enabled':False},headers=H)).status==status
            assert (await c.post('/api/members/status',json={'actor':'viewer','enabled':False},headers=H)).status==200
            assert old not in ws.sessions
            token=identities()[1]['token']
            assert (await c.post('/api/login',json={'token':token},headers=H)).status==401
            reopened=Workspace(tmp_path/'store.db',b'k'*32,identities())
            assert reopened.identify(token) is None
            reopened.db.close()
            assert (await c.post('/api/members/status',json={'actor':'viewer','enabled':True},headers=H)).status==200
            await login(c,'viewer')
    asyncio.run(run())

@pytest.mark.parametrize('role',['operator','support','viewer','manager','observer','other'])
def test_incident_role_tenant_and_transitions(tmp_path,role):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            payload={'component':'tts','severity':'critical','text':'SECRET'}
            r=await c.post('/api/incidents',json=payload,headers=H); assert r.status==200
            iid=(await r.json())['entries'][0]['id']
            assert (await c.post('/api/incidents',json=payload,headers=H)).status==409
            await login(c,role)
            allowed=role in {'operator','support','other'}
            r=await c.get('/api/incidents');assert r.status==(200 if allowed else 403)
            assert 'SECRET' not in await r.text()
            r=await c.post('/api/incidents/'+iid,json={'status':'acknowledged'},headers=H)
            assert r.status==(404 if role=='other' else 200 if allowed else 403)
            if role in {'operator','support'}:
                assert (await c.post('/api/incidents/'+iid,json={'status':'open'},headers=H)).status==400
                assert (await c.post('/api/incidents/'+iid,json={'status':'resolved'},headers=H)).status==200
                assert (await c.post('/api/incidents/'+iid,json={'status':'acknowledged'},headers=H)).status==400
                assert ws.db.execute('SELECT count(*) FROM health').fetchone()[0]==0
    asyncio.run(run())

def test_trends_truth_and_tenant(tmp_path):
    async def run():
        ws=setup(tmp_path)
        mid=ws.ingest(identities()[0],EVENTS,'team',7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'manager')
            data=await (await c.get('/api/trends')).json()
            assert data['days'][0]['meetings']==1 and data['days'][0]['interventions']==1
            assert 'SECRET' not in str(data)
            ws.db.execute('DELETE FROM meeting_imports WHERE meeting=?',(mid,));ws.db.commit()
            data=await (await c.get('/api/trends')).json()
            assert data['unknown_import_dates']==1 and data['days']==[]
            await login(c,'other')
            assert (await (await c.get('/api/trends')).json())['unknown_import_dates']==0
            await login(c,'support');assert (await c.get('/api/trends')).status==403
    asyncio.run(run())
