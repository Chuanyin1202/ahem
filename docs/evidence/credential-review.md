# PR #4 credential review

## Confirmed defect and fix

Two Workspace processes can share the sidecar database. Previously, after one
rotated a credential, the other could still authenticate the old cached token.
The new regression test failed before the fix (`identify(old_token)` was not None).
Authenticated actors now bind to a credential digest, and every authentication
and session validity check compares it to the persisted current version.
This invalidates old cached credentials/sessions across processes. A different
process may need restart to learn a newly issued token; rejection is fail-closed.

The bridge also validates its credential before each file and after reading it.
A second regression test revokes the operator during file reading and verifies
that no meeting is imported. This is not a claim of atomic authorization against
all possible concurrent DB changes between check and write.

## Evidence

`PYTHONPATH=src python -m pytest -q tests/test_enterprise_credentials.py tests/test_enterprise_bridge.py tests/test_demo_credentials_expiry.py`

18 passed, exit 0 (0.36 seconds). Tests cover cross-process rotation, session
invalidation, revocation during file read, and five-day expiry after restart.
No real credentials were rotated; the existing five-day demo expiry is unchanged.
The 8910 demo was restarted to load the fix; older 8891/8907 deployments were not changed.

Initial local full suite: 3 failed, 653 passed, 21 skipped, 2 xfailed, exit 1;
all three failures were Chrome `Page.goto` 30-second timeouts in existing spectator
tests. This is not counted as a successful full test run and the cause is not
claimed proven. Follow-up full-suite evidence is recorded in the PR checks/logs.

No changes to upstream state, spectator, speaker, or requirements.txt. Linux/Pi
deployment and real Discord voice remain separate acceptance gates.
