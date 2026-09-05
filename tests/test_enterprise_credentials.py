import asyncio
import time
import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from aiohttp.test_utils import TestClient, TestServer
from meeting_host.enterprise import create_app,Workspace
from test_enterprise import setup,identities,ORIGIN,login

H={'Origin':ORIGIN}

def test_create_rotate_expire_restart(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            payload={'actor':'demo-new','role':'viewer','days':1}
            r=await c.post('/api/members/create',json=payload,headers=H);assert r.status==200
            first=(await r.json())['token']
            assert ws.identify(first)['id']=='demo-new'
            assert first not in str(ws.db.execute('SELECT * FROM member_credentials').fetchall())
            assert first not in await (await c.get('/api/members')).text()
            assert (await c.post('/api/members/create',json=payload,headers=H)).status==409
            r=await c.post('/api/members/rotate',json={'actor':'demo-new','days':2},headers=H)
            assert r.status==200
            second=(await r.json())['token'];assert second!=first and ws.identify(first) is None
            reopened=Workspace(tmp_path/'store.db',b'k'*32,identities())
            assert reopened.identify(second)['id']=='demo-new'
            assert reopened.identify(first) is None
            reopened.db.close()
            ws.db.execute('UPDATE member_credentials SET expires=? WHERE actor=?',(time.time()-1,'demo-new'));ws.db.commit()
            assert ws.identify(second) is None
    asyncio.run(run())

def test_static_rotation_and_suspension(tmp_path):
    async def run():
        ws=setup(tmp_path)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'viewer');sid=next(iter(ws.sessions))
            await login(c,'operator')
            r=await c.post('/api/members/rotate',json={'actor':'viewer','days':7},headers=H);assert r.status==200
            token=(await r.json())['token']
            assert sid not in ws.sessions and ws.identify(identities()[1]['token']) is None
            reopened=Workspace(tmp_path/'store.db',b'k'*32,identities())
            assert reopened.identify(identities()[1]['token']) is None and reopened.identify(token)
            reopened.db.close()
            await c.post('/api/members/status',json={'actor':'viewer','enabled':False},headers=H)
            r=await c.post('/api/members/rotate',json={'actor':'viewer'},headers=H)
            assert ws.identify((await r.json())['token']) is None
    asyncio.run(run())

@pytest.mark.parametrize('target,status',[('other',404),('cleared',403),('operator',403)])
def test_rotation_scope(tmp_path,target,status):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,'operator')
            assert (await c.post('/api/members/rotate',json={'actor':target},headers=H)).status==status
    asyncio.run(run())

@pytest.mark.parametrize('payload,status',[
    ({'actor':'demo-x','role':'operator'},403),
    ({'actor':'demo-x','role':'viewer','regulated_content':True},403),
    ({'actor':'demo-x','role':'viewer','days':0},400),
    ({'actor':'Name@Email','role':'viewer'},400),
])
def test_create_validation(tmp_path,payload,status):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,'operator')
            assert (await c.post('/api/members/create',json=payload,headers=H)).status==status
    asyncio.run(run())

def test_non_operator_and_origin_rejected(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(setup(tmp_path),ORIGIN))) as c:
            await login(c,'support')
            assert (await c.post('/api/members/create',json={},headers=H)).status==403
            await login(c,'operator')
            assert (await c.post('/api/members/rotate',json={'actor':'viewer'})).status==403
    asyncio.run(run())
