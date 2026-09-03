import os

from meeting_host.retention import purge_expired


def test_retention_only_targets_expired_ahem_artifacts(tmp_path):
    old = tmp_path / "meeting-1.events.jsonl.ahem"
    recent = tmp_path / "meeting-2.log"
    unrelated = tmp_path / "notes.txt"
    for path in (old, recent, unrelated):
        path.write_text("x")
    os.utime(old, (0, 0))
    os.utime(unrelated, (0, 0))

    targets = purge_expired(tmp_path, ttl_hours=24, now=200_000, dry_run=True)
    assert targets == [old]
    assert old.exists()

    purge_expired(tmp_path, ttl_hours=24, now=200_000, dry_run=False)
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()
