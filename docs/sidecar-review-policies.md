# Sidecar review policies

These policies apply only to the optional enterprise sidecar, not to the live
chair's authentication, voice providers or viewer defaults.

| Finding | Policy and regression |
| --- | --- |
| Retention stops after a lock | SQLite busy wait is bounded to 100 ms per operation. Retention retries with capped exponential delay (2..60 seconds), logs a content-free failure and resets its failure counter after recovery. HTTP database failures return 503. `test_retention_recovers_after_database_lock` |
| Restricted deletion | Deleting regulated content requires operator role AND explicit content clearance. The UI hides unavailable deletion actions; the server independently denies them. `test_restricted_delete_and_audit_target` |
| Audit target missing | Import, content access, grants, policy/date changes and deletion include a random meeting ID, never transcript or topic. Old audit rows keep a null target. `test_legacy_audit_schema_migrates` |
| Successful-login throttling | At most 10 failed credential attempts per IP per 60-second window; successful attempts do not consume this quota. All attempts still share a 120/minute/IP cap. Counters are rechecked after asynchronous body receipt. `test_login_failure_and_volume_limits`, `test_successful_login_total_cap`, `test_concurrent_failed_logins_are_counted` |
| Local calendar mismatch | User-entered meeting dates and their trend windows use Asia/Taipei. Import timestamps remain UTC. `test_taipei_midnight_date` |
| KEK special file blocking | Open nonblocking without following symlinks, validate regular-file ownership/permissions, read no more than 4097 characters and reject oversized values. `test_kek_rejects_fifo_and_large_file` |
| Incomplete backup publication | Write/fsync a private temporary file; publish via a same-filesystem hard link that cannot overwrite an existing destination, then remove the temporary name. Failure cannot leave a newly published partial backup. `test_backup_failed_write_leaves_no_final` |
| Misleading session expiry | Session expiry, cookie lifetime and API expiry cannot exceed the underlying credential expiry. `test_effective_session_expiry` |

## Deployment and recovery

- Stop the workbench and bridge before upgrading; make a verified backup of the
  private database and preserve the existing KEK and identities. Do not publish them.
- Startup adds a nullable `audit.target` column to old databases. Existing rows
  are preserved but their historical target cannot be reconstructed.
- The previous writer used positional five-column audit inserts. After migration,
  do not run that old writer against the migrated DB. Roll back using the verified
  pre-upgrade DB with its matching configuration, while reviewing whether that
  snapshot restores expired permissions or old credentials.
- Use one web process and a separate bridge on a local filesystem. This change
  does not introduce shared sessions, external SSO or multi-worker support.
- Backup destinations must support same-filesystem hard links. Directory fsync,
  abrupt power-loss recovery and Raspberry Pi deployment remain unverified.
- Previously damaged managed backup files still stop verification/deletion.
  Investigate and explicitly quarantine them; do not silently skip or delete them.
- Source JSONL remains governed by a separate retention policy. No new cloud API
  calls, paid services or regulatory compliance claims are introduced.

Tests live in `tests/test_review_regressions.py`; run the full suite as well as
the local six-role browser demo before proposing a PR update.
