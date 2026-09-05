"""Failure injection: HTTP errors must not leave committed business changes."""
import asyncio
import sqlite3
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_enterprise import EVENTS, ORIGIN, identities, login, setup
from meeting_host.enterprise import create_app


def snapshot(ws):
    return (list(ws.db.iterdump()), dict(ws.identities), dict(ws.sessions))


@pytest.mark.parametrize('case', [
    'create', 'rotate', 'grant', 'policy', 'disable', 'date', 'incident',
    'transition', 'alert', 'import', 'delete', 'revoke_sessions', 'logout_all',
])
def test_audit_failure_rolls_back_and_retry_succeeds(tmp_path, case):
    async def run():
        ws = setup(tmp_path)
        owner = identities()[0]
        mid = ws.ingest(owner, EVENTS, 'team', 7)
        with ws.db:
            ws.db.execute("INSERT INTO incidents VALUES ('incident','org-a','tts','warning','open',1,1)")
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as client:
            await login(client, 'viewer')
            await login(client, 'operator')
            requests = {
                'create': ('POST', '/api/members/create', {'actor': 'new-member', 'role': 'viewer'}),
                'rotate': ('POST', '/api/members/rotate', {'actor': 'viewer'}),
                'grant': ('POST', f'/api/meetings/{mid}/grants', {'actor': 'viewer', 'allow': True}),
                'policy': ('POST', f'/api/meetings/{mid}/policy', {'days': 1}),
                'disable': ('POST', '/api/members/status', {'actor': 'viewer', 'enabled': False}),
                'date': ('POST', f'/api/meetings/{mid}/date', {'day': '2020-01-01'}),
                'incident': ('POST', '/api/incidents', {'component': 'stt', 'severity': 'warning'}),
                'transition': ('POST', '/api/incidents/incident', {'status': 'acknowledged'}),
                'alert': ('POST', '/api/alert-rules', {'component': 'stt', 'enabled': True}),
                'import': ('POST', '/api/meetings', {'events': EVENTS}),
                'delete': ('DELETE', f'/api/meetings/{mid}', {}),
                'revoke_sessions': ('POST', '/api/members/revoke-sessions', {'actor': 'viewer'}),
                'logout_all': ('POST', '/api/logout-all', {}),
            }
            method, path, body = requests[case]
            before = snapshot(ws)
            audit = ws.audit

            def fail_after_insert(*args, **kwargs):
                # Proves the nested audit transaction cannot commit the outer one.
                audit(*args, **kwargs)
                raise sqlite3.OperationalError('injected audit failure')

            # Standalone logout audit must fail inside its transaction, not after
            # it has correctly committed; mutation handlers fail after insertion.
            side_effect = (sqlite3.OperationalError('injected audit failure')
                           if case == 'logout_all' else fail_after_insert)
            with patch.object(ws, 'audit', side_effect=side_effect):
                response = await client.request(method, path, json=body, headers={'Origin': ORIGIN})
                assert response.status == 503
            assert not ws.db.in_transaction
            assert snapshot(ws) == before
            response = await client.request(method, path, json=body, headers={'Origin': ORIGIN})
            assert response.status in {200, 201}
            assert not ws.db.in_transaction
            if case in {'create', 'rotate'}:
                result = await response.json()
                assert ws.identify(result['token'])['id'] == body['actor']
                if case == 'rotate':
                    assert ws.identify(identities()[1]['token']) is None
        ws.db.close()
    asyncio.run(run())


def test_real_commit_lock_rolls_back_and_next_request_succeeds(tmp_path):
    async def run():
        ws = setup(tmp_path)
        mid = ws.ingest(identities()[0], EVENTS, 'team', 7)
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as client:
            await login(client, 'operator')
            before = snapshot(ws)
            reader = sqlite3.connect(tmp_path / 'store.db')
            try:
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM meetings').fetchall()
                response = await client.post(f'/api/meetings/{mid}/content',
                    json={'purpose': 'meeting_review'}, headers={'Origin': ORIGIN})
                assert response.status == 503
                assert not ws.db.in_transaction
                assert snapshot(ws) == before
            finally:
                reader.rollback()
                reader.close()
            response = await client.post('/api/meetings', json={'events': EVENTS}, headers={'Origin': ORIGIN})
            assert response.status == 201  # First retry, not the second.
        ws.db.close()
    asyncio.run(run())


def test_import_receipt_rolls_back_with_audit(tmp_path):
    ws = setup(tmp_path)
    with patch.object(ws, 'audit', side_effect=sqlite3.OperationalError('audit failure')):
        with pytest.raises(sqlite3.OperationalError):
            ws.ingest(identities()[0], EVENTS, 'team', 7, source_id='a' * 64)
    assert ws.db.execute('SELECT count(*) FROM import_receipts').fetchone()[0] == 0
    assert ws.db.execute('SELECT count(*) FROM meetings').fetchone()[0] == 0
    mid = ws.ingest(identities()[0], EVENTS, 'team', 7, source_id='a' * 64)
    assert ws.ingest(identities()[0], EVENTS, 'team', 7, source_id='a' * 64) == mid
    assert ws.db.execute('SELECT count(*) FROM audit').fetchone()[0] == 1
    ws.db.close()


def test_credential_commit_failure_preserves_old_token_and_sessions(tmp_path):
    ws = setup(tmp_path)
    viewer = ws.identify(identities()[1]['token'])
    ws.sessions['existing'] = (viewer, 9999999999)
    before = snapshot(ws)
    reader = sqlite3.connect(tmp_path / 'store.db')
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM member_credentials').fetchall()
        with pytest.raises(sqlite3.OperationalError):
            ws.issue_credential(identities()[1], 5, audit_actor=identities()[0], action='credential_rotate')
        assert not ws.db.in_transaction
        assert snapshot(ws) == before
        assert ws.identify(identities()[1]['token']) is not None
    finally:
        reader.rollback()
        reader.close()
        ws.db.close()


def test_second_member_audit_failure_rolls_back_first_audit(tmp_path):
    async def run():
        ws = setup(tmp_path)
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as client:
            await login(client, 'viewer')
            await login(client, 'operator')
            before = snapshot(ws)
            audit = ws.audit
            def fail_second(actor, action, *args):
                if action == 'revoke_sessions':
                    raise sqlite3.OperationalError('second audit failed')
                audit(actor, action, *args)
            with patch.object(ws, 'audit', side_effect=fail_second):
                response = await client.post('/api/members/status',
                    json={'actor': 'viewer', 'enabled': False}, headers={'Origin': ORIGIN})
                assert response.status == 503
            assert snapshot(ws) == before
            assert not ws.db.in_transaction
        ws.db.close()
    asyncio.run(run())


@pytest.mark.parametrize('endpoint,body', [
    ('/api/health', {'component': 'stt', 'state': 'unavailable'}),
    ('/api/alert-rules', {'component': 'stt', 'enabled': True}),
])
def test_alert_evaluation_failure_is_atomic(tmp_path, endpoint, body):
    async def run():
        ws = setup(tmp_path)
        async with TestClient(TestServer(create_app(ws, ORIGIN))) as client:
            await login(client, 'operator')
            before = snapshot(ws)
            evaluate = ws.evaluate_alerts
            def fail():
                evaluate()
                raise sqlite3.OperationalError('evaluation failure')
            with patch.object(ws, 'evaluate_alerts', side_effect=fail):
                response = await client.post(endpoint, json=body, headers={'Origin': ORIGIN})
                assert response.status == 503
            assert snapshot(ws) == before
            assert not ws.db.in_transaction
        ws.db.close()
    asyncio.run(run())
