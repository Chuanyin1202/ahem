import base64
import importlib.util
import json
from pathlib import Path

import pytest
pytest.importorskip('cryptography',reason='Optional enterprise dependency')
from meeting_host.enterprise import Workspace


def test_six_demo_credentials_expire_in_exactly_five_days_after_restart(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('seed_demo',Path(__file__).resolve().parents[1]/'scripts/enterprise_local_demo.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    now=1_800_000_000.0
    monkeypatch.setattr('meeting_host.enterprise.time.time',lambda:now)
    root=tmp_path/'demo'
    module.seed(root,5,8910)
    identities=json.loads((root/'identities.json').read_text())
    manifest=json.loads((root/'credential-manifest.json').read_text())
    assert len(identities)==6
    assert all(r['expires_at']==now+5*86400 for r in manifest['roles'])
    cards=(root/'demo-login-cards.md').read_text()
    assert all(i['token'] in cards for i in identities)
    assert (root/'demo-login-cards.md').stat().st_mode & 0o777 == 0o600
    key=base64.b64decode((root/'kek').read_text())
    ws=Workspace(root/'enterprise.db',key,identities)
    assert all(ws.identify(i['token']) for i in identities)
    ws.db.close()
    monkeypatch.setattr('meeting_host.enterprise.time.time',lambda:now+5*86400)
    ws=Workspace(root/'enterprise.db',key,identities)
    assert all(ws.identify(i['token']) is None for i in identities)
    ws.db.close()
