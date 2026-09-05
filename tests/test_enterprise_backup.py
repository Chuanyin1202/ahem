import sqlite3
import pytest
pytest.importorskip('cryptography', reason='Install requirements-enterprise.txt for sidecar tests')
from cryptography.exceptions import InvalidTag
from meeting_host.enterprise_backup import snapshot, restore
from test_enterprise import setup, identities, EVENTS


def test_encrypted_backup_restore_and_no_overwrite(tmp_path):
    tmp_path.chmod(0o700)
    ws=setup(tmp_path)
    ws.ingest(identities()[0],EVENTS,'team',7)
    ws.db.execute("INSERT INTO disabled_members VALUES ('viewer')");ws.db.commit()
    backup=tmp_path/'snapshot.enc'; restored=tmp_path/'restored.db'
    assert snapshot(tmp_path/'store.db',backup,b'k'*32)==1
    assert b'SECRET' not in backup.read_bytes() and b'SQLite' not in backup.read_bytes()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert restore(backup,b'k'*32)==1
    assert restore(backup,b'k'*32,restored)==1
    with sqlite3.connect(restored) as db:
        assert db.execute('SELECT actor FROM disabled_members').fetchone()[0]=='viewer'
    with pytest.raises(FileExistsError): restore(backup,b'k'*32,restored)
    with pytest.raises(FileExistsError): snapshot(tmp_path/'store.db',backup,b'k'*32)
    ws.db.close()


def test_wrong_key_rejected_before_restore(tmp_path):
    tmp_path.chmod(0o700);ws=setup(tmp_path)
    backup=tmp_path/'snapshot.enc';target=tmp_path/'new.db'
    snapshot(tmp_path/'store.db',backup,b'k'*32)
    with pytest.raises(InvalidTag):restore(backup,b'x'*32,target)
    assert not target.exists()
    ws.db.close()


def test_backup_rejects_public_destination_directory(tmp_path):
    tmp_path.chmod(0o700);ws=setup(tmp_path)
    public=tmp_path/'public';public.mkdir(mode=0o755)
    with pytest.raises(ValueError):snapshot(tmp_path/'store.db',public/'snapshot.enc',b'k'*32)
    ws.db.close()
