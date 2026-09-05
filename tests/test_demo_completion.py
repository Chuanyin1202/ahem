import asyncio
import os
import time
from datetime import datetime,timezone
import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from aiohttp.test_utils import TestClient,TestServer
from meeting_host.enterprise import create_app
from meeting_host.enterprise_backup import snapshot,maintain
from test_enterprise import setup,identities,EVENTS,ORIGIN,login

H={'Origin':ORIGIN}

def test_meeting_dates_and_privacy(tmp_path):
    async def run():
        ws=setup(tmp_path);mid=ws.ingest(identities()[0],EVENTS,'team',7)
        restricted=ws.ingest(identities()[0],EVENTS,'regulated',7)
        async with TestClient(TestServer(create_app(ws,ORIGIN))) as c:
            await login(c,'operator')
            day=datetime.now(timezone.utc).date().isoformat()
            assert (await c.post(f'/api/meetings/{mid}/date',json={'day':day},headers=H)).status==200
            assert (await c.post(f'/api/meetings/{restricted}/date',json={'day':day},headers=H)).status==403
            for invalid in ['2999-01-01','2026-02-30',None]:
                assert (await c.post(f'/api/meetings/{mid}/date',json={'day':invalid},headers=H)).status==400
            await login(c,'manager')
            data=await (await c.get('/api/meeting-trends?days=365')).json()
            assert data['missing_dates']==1 and data['days'][0]['meetings']==1
            assert data['days'][0]['interventions_per_10_minutes']==30
            assert 'SECRET' not in str(data)
            await login(c,'other')
            assert (await c.post(f'/api/meetings/{mid}/date',json={'day':day},headers=H)).status==404
            assert (await (await c.get('/api/meeting-trends')).json())['days']==[]
            await login(c,'support');assert (await c.get('/api/meeting-trends')).status==403
    asyncio.run(run())

def test_retention_dry_run_apply_and_unmanaged_files(tmp_path):
    tmp_path.chmod(0o700);ws=setup(tmp_path)
    directory=tmp_path/'backups';directory.mkdir(mode=0o700)
    for i in range(4):
        path=directory/f'ahem-snapshot-{i}-aaaaaaaa.enc'
        snapshot(tmp_path/'store.db',path,b'k'*32)
        os.utime(path,(time.time()-10*86400,time.time()-10*86400+i))
    unrelated=directory/'unmanaged.enc';snapshot(tmp_path/'store.db',unrelated,b'k'*32)
    result=maintain(tmp_path/'store.db',directory,b'k'*32,days=1)
    assert result['deleted']==0 and result['expired_candidates']==3
    result=maintain(tmp_path/'store.db',directory,b'k'*32,days=1,apply=True)
    assert result['deleted']==4
    assert len(list(directory.glob('ahem-snapshot-*.enc')))==2 and unrelated.exists()
    ws.db.close()

def test_invalid_backup_prevents_cleanup(tmp_path):
    tmp_path.chmod(0o700);ws=setup(tmp_path)
    directory=tmp_path/'backups';directory.mkdir(mode=0o700)
    old=directory/'ahem-snapshot-1-aaaaaaaa.enc'
    snapshot(tmp_path/'store.db',old,b'k'*32)
    os.utime(old,(0,0))
    bad=directory/'ahem-snapshot-2-bbbbbbbb.enc';bad.write_bytes(b'corrupt test fixture')
    with pytest.raises(ValueError):maintain(tmp_path/'store.db',directory,b'k'*32,days=1,apply=True)
    assert old.exists() and bad.exists()
    ws.db.close()
