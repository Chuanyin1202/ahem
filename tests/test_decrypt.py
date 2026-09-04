import json
import stat

from meeting_host.decrypt import append_audit_record


def test_append_audit_record_is_private_and_preserves_existing_lines(tmp_path):
    path = tmp_path / "audit" / "security-audit.jsonl"
    append_audit_record(path, {"sequence": 1})
    append_audit_record(path, {"sequence": 2})

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records == [{"sequence": 1}, {"sequence": 2}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
