import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')

from meeting_host.enterprise import Workspace
from meeting_host.enterprise_bridge import sync_once
from meeting_host.live import _write_events_jsonl
from test_enterprise import EVENTS, identities, setup


@dataclasses.dataclass
class Record:
    kind: str
    t: float
    data: dict


def export(root):
    source = root / 'meetings'
    source.mkdir(mode=0o700)
    path = source / 'meeting-test.events.jsonl'
    session = SimpleNamespace(events=[Record(**e) for e in EVENTS])
    _write_events_jsonl(session, path)
    return source, path


def test_live_export_to_encrypted_workspace_retry_and_restart(tmp_path):
    ws = setup(tmp_path)
    source, path = export(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(source.glob('*.partial'))
    token = identities()[0]['token']
    assert sync_once(ws, source, token) == dict(imported=1, duplicates=0, rejected=0)
    row = ws.db.execute('SELECT * FROM meetings').fetchone()
    assert b'SECRET-CONTENT' not in row['blob']
    assert 'SECRET-CONTENT' in ws.store.decrypt_text(row['blob'],meeting_id=row['id'],
                                                  artifact_type='events',purpose='review',operator=True)
    ws.db.close()
    ws = setup(tmp_path)
    assert sync_once(ws, source, token)['duplicates'] == 1
    ws.db.execute('DELETE FROM meetings'); ws.db.commit()
    assert sync_once(ws, source, token)['duplicates'] == 1
    assert ws.db.execute('SELECT COUNT(*) FROM meetings').fetchone()[0] == 0
    ws.db.close()


def test_failed_export_never_publishes_partial(tmp_path, monkeypatch):
    target = tmp_path / 'meeting.events.jsonl'
    target.write_text('previous')
    def fail(*args):
        raise OSError('simulated full disk')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        _write_events_jsonl(SimpleNamespace(events=[Record(**EVENTS[0])]), target)
    assert target.read_text() == 'previous'
    assert not list(tmp_path.glob('*.partial'))


def test_reject_links_invalid_data_and_ignore_partial(tmp_path):
    ws = setup(tmp_path)
    source, path = export(tmp_path)
    (source/'link.events.jsonl').symlink_to(path)
    bad = source/'bad.events.jsonl'; bad.write_text('bad'); bad.chmod(0o600)
    (source/'pending.partial').write_text('bad')
    assert sync_once(ws, source, identities()[0]['token']) == dict(imported=1,duplicates=0,rejected=2)
    ws.db.close()


def test_permission_boundaries_and_no_backend_needed_for_export(tmp_path):
    ws = setup(tmp_path)
    source, path = export(tmp_path)
    with pytest.raises(PermissionError):
        sync_once(ws, source, identities()[1]['token'])
    ws.db.execute("INSERT INTO disabled_members VALUES ('operator')"); ws.db.commit()
    with pytest.raises(PermissionError):
        sync_once(ws, source, identities()[0]['token'])
    # Live export does not connect to the database or import the sidecar.
    assert json.loads(path.read_text().splitlines()[0]) == EVENTS[0]
    ws.db.close()


def test_receipts_are_tenant_scoped_and_transactional(tmp_path):
    ws = setup(tmp_path)
    digest = 'a'*64
    first = ws.ingest(identities()[0], EVENTS, 'team', 7, source_id=digest)
    other = dict(id='other',tenant='org-b',role='operator')
    assert ws.ingest(other, EVENTS, 'team', 7, source_id=digest) != first
    ws.db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON import_receipts BEGIN SELECT RAISE(ABORT, 'test'); END;")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        ws.ingest(identities()[0], EVENTS, 'team', 7, source_id='b'*64)
    assert ws.db.execute('SELECT COUNT(*) FROM meetings').fetchone()[0] == 2
    ws.db.close()


def test_current_main_preview_events_survive_bridge(tmp_path):
    ws = setup(tmp_path)
    source = tmp_path / 'current-main'
    source.mkdir(mode=0o700)
    events = EVENTS + [
        dict(kind='minutes', t=21, data={'preview': True, 'participant_md': 'PRIVATE live summary'}),
        dict(kind='ai_critique', t=22, data={'text': 'PRIVATE chair observation'}),
        dict(kind='minutes', t=23, data={'participant_md': 'PRIVATE final summary', 'host_md': 'PRIVATE host record'}),
    ]
    _write_events_jsonl(SimpleNamespace(events=[Record(**e) for e in events]), source/'current.events.jsonl')
    assert sync_once(ws, source, identities()[0]['token']) == dict(imported=1, duplicates=0, rejected=0)
    row = ws.db.execute('SELECT * FROM meetings').fetchone()
    assert 'PRIVATE' not in row['aggregate']
    assert b'PRIVATE' not in row['blob']
    restored = json.loads(ws.store.decrypt_text(row['blob'], meeting_id=row['id'], artifact_type='events', purpose='integration test', operator=True))
    assert restored == events
    assert sync_once(ws, source, identities()[0]['token'])['duplicates'] == 1
    ws.db.close()


def test_revocation_during_file_read_prevents_import(tmp_path, monkeypatch):
    from meeting_host import enterprise_bridge as bridge
    ws=setup(tmp_path)
    source,path=export(tmp_path)
    original=bridge.read_private
    def revoke_then_read(path):
        raw=original(path)
        ws.db.execute("INSERT INTO disabled_members VALUES ('operator')")
        ws.db.commit()
        return raw
    monkeypatch.setattr(bridge,'read_private',revoke_then_read)
    assert sync_once(ws,source,identities()[0]['token'])['rejected']==1
    assert ws.db.execute('SELECT COUNT(*) FROM meetings').fetchone()[0]==0
    ws.db.close()
