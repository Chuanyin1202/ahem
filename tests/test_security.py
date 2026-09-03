import base64
import json
import stat

import pytest

from meeting_host.security import (
    ConsentPolicy,
    EnvelopeStore,
    audit_record,
    secure_write_text,
    redact_event_for_viewer,
)


def test_secure_write_uses_private_permissions(tmp_path):
    path = secure_write_text(tmp_path / "meetings" / "meeting.log", "機密內容")
    assert path.read_text() == "機密內容"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_envelope_roundtrip_and_tamper_detection():
    store = EnvelopeStore(b"k" * 32)
    blob = store.encrypt_text("逐字稿", meeting_id="m-1", artifact_type="minutes")
    assert b"\xe9\x80\x90\xe5\xad\x97" not in blob
    assert store.decrypt_text(
        blob, meeting_id="m-1", artifact_type="minutes",
        purpose="會後核對", operator=True) == "逐字稿"

    payload = json.loads(blob)
    ciphertext = bytearray(base64.b64decode(payload["ciphertext"]))
    ciphertext[-1] ^= 1
    payload["ciphertext"] = base64.b64encode(ciphertext).decode()
    with pytest.raises(Exception):
        store.decrypt_text(
            json.dumps(payload).encode(), meeting_id="m-1", artifact_type="minutes",
            purpose="會後核對", operator=True)


def test_decryption_requires_operator_and_purpose():
    store = EnvelopeStore(b"k" * 32)
    blob = store.encrypt_text("secret", meeting_id="m-1", artifact_type="events")
    with pytest.raises(PermissionError):
        store.decrypt_text(blob, meeting_id="m-1", artifact_type="events",
                           purpose="", operator=True)
    with pytest.raises(PermissionError):
        store.decrypt_text(blob, meeting_id="m-1", artifact_type="events",
                           purpose="觀看", operator=False)


def test_consent_gate_fails_closed():
    with pytest.raises(PermissionError):
        ConsentPolicy(False, "strict").require("OpenAI")
    ConsentPolicy(False, "development").require("OpenAI")
    ConsentPolicy(True, "strict").require("OpenAI")


def test_audit_record_contains_no_plain_actor():
    record = audit_record(
        "decrypt", actor="alice@example.com", meeting_id="m-1",
        purpose="會後核對", outcome="allowed")
    assert "alice" not in json.dumps(record)
    assert set(record) == {"at", "action", "actor_ref", "meeting_id", "purpose", "outcome"}


def test_viewer_dlp_removes_identity_transcript_and_paths():
    event = {"kind": "minutes", "data": {
        "speaker": "Alice", "text": "秘密", "participants": ["Alice", "Bob"],
        "minutes_md": "全文", "minutes_path": "/tmp/meeting.md"}}
    safe = redact_event_for_viewer(event)
    assert safe["data"]["speaker"] == "[已隱去]"
    assert safe["data"]["text"] == "[已隱去]"
    assert safe["data"]["participants"] == ["P01", "P02"]
    assert "minutes_path" not in safe["data"]
