# Optional enterprise sidecar integration

## Scope and architecture

Upstream `Chuanyin1202/ahem` remains the meeting-chair source of truth. This branch
now includes main `ec831d3f29c974b65fa36b036d3d58eea3798737`, including its new
spectator UI, live minutes and AI critique. The workbench was selected from the
contributor's `1cdfea4` rather than merging its old security/core changes.
See [review policies](sidecar-review-policies.md) for the eight fixes and migration.

```text
Discord -> Ahem live -> STT / decisions / TTS / spectator (unchanged)
                |
                | shutdown: complete events JSONL, atomic rename
                v
        explicit private meeting directory (plaintext, owner-only)
                |
                | independent enterprise_bridge process; bounded file size
                v
        SQLite transaction: encrypted meeting + tenant-scoped import receipt
                |
                v
        independent enterprise HTTP process -> role-scoped workbench UI
```

The only existing runtime file changed is `live.py`: write events to a same-directory
0600 temporary file, flush/fsync, then replace the final path. The temporary suffix
is not watched. No database/network calls or enterprise/cryptography imports are
added to the live chair. A file-write failure still propagates as before; this is
not a claim of disk failure tolerance. File fsync is synchronous at shutdown,
not during audio processing; directory fsync/power-loss durability is not proven.

Preserved byte-for-byte: upstream requirements.txt, spectator/auth/UI, speaker,
Discord source, state, and their token semantics. No Viewer redaction default,
short-token rejection, rounding revert reversal, or voice package migration is
introduced by the sidecar. Enterprise crypto is an explicit optional dependency.

## Implemented local workflows

Five-day demo credentials and role screenshots: [demo-access.md](demo-access.md).

- Management: numeric meeting summaries, filters, pagination, date/import trends.
- Enterprise: separate local identities, sessions, grants, revocation, expiry,
  credential rotation and audit. No automatic mapping from Discord participants.
- Restricted content: encrypted stored events, explicit clearance/purpose,
  retention controls, encrypted backup/restore CLI.
- Support: allowlisted health states, incidents, in-app notifications and demo
  simulations. These are **not wired to live service monitoring**.
- Integration: completed-file scanning; restart/retry deduplication; transaction
  rollback; content displayed in the running UI after a separate CLI scan.

## Deployment: opt-in, not installed automatically

1. Keep existing upstream deployment and participant links intact. Back up before
   changing any deployment. Test the integration on synthetic data first.
2. Install the upstream dependencies normally, then the optional sidecar file:

   ```sh
   python -m pip install -r requirements.txt
   python -m pip install -r requirements-enterprise.txt
   ```

3. For a new **synthetic-only** local workspace:

   ```sh
   PYTHONPATH=src python scripts/enterprise_local_demo.py --directory /absolute/new-private-demo --days 5 --port 8907
   PYTHONPATH=src AHEM_KEK_FILE=/absolute/new-private-demo/kek python -m meeting_host.enterprise \
     --identities /absolute/new-private-demo/identities.json \
     --database /absolute/new-private-demo/enterprise.db --port 8907 --demo-mode
   ```

4. Real use requires separately provisioned private identities, a KEK, and an
   operator token file (0600, service-owned). Never reuse the demo identities for
   real meetings. Explicitly assign a **single organization's** source directory;
   do not infer tenant or policy from event text. Only include meetings approved
   for import. The bridge processes all matching existing files as well as new
   ones. No external SSO/MFA or notification account is required for this demo.
5. Ahem writes to `meetings` under its working directory. Use a dedicated 0700
   directory owned by the same OS service account; final files must be 0600.
   Do not point at a shared or unrelated directory. Old non-atomic writers are
   not safe to watch concurrently; stop them before one-time historical import.

   ```sh
   PYTHONPATH=src AHEM_KEK_FILE=/absolute/private/kek python -m meeting_host.enterprise_bridge \
     --source /absolute/ahem-runtime/meetings \
     --database /absolute/private/enterprise.db \
     --identities /absolute/private/identities.json \
     --token-file /absolute/private/bridge-token --policy team --days 7 --interval 15
   ```

   With no interval, one scan exits 0 on success or 1 on a rejected file/failure.
   Interval mode emits only counts or `scan_failed`, retries next scan, and never
   deletes source files. Investigate nonzero rejection counts locally; raw errors
   and file paths are intentionally not logged. Credential state reloads each scan.
   The web service and bridge are separate processes sharing a local SQLite DB;
   no network filesystem, multi-host writer, or production-scale claim is made.
6. Linux example: `deploy/enterprise-sidecar.service.example` uses systemd
   credential loading and a low-priority separate worker. Replace paths/user,
   provision matching permissions, validate with systemd-analyze, and test on
   Raspberry Pi before enabling. This template has **not been run on a Pi**.

## Data and lifecycle contract

- Receipts are `(tenant, SHA256(exact file bytes)) -> meeting ID`, committed in
  the same write transaction as meeting content. Identical bytes count as the
  same import even after rename. Different byte encodings count as different imports.
- Receipts intentionally survive expiry/manual deletion, preventing resurrection
  by the polling worker. They contain no transcript/path, but remain metadata.
  There is not yet a receipt pruning/export/administration UI. Document this
  retention separately; an old DB restore can also restore old receipts/grants.
- UI manual imports remain independent and can duplicate a bridge import.
- Limits: 4 MiB raw JSONL, 10,000 events, numeric relative time 0–86400, current
  event schema. Oversize/malformed files are rejected and kept for intervention;
  automatic chunking is not implemented. Large histories are re-hashed per scan;
  this is suitable for a small local demo, not a tuned large-scale ingestion system.
- Source JSONL and upstream logs/minutes remain plaintext. Database encryption
  does not encrypt/remove those files. Manage source retention separately and
  retain participant consent/access controls. No regulated-industry certification.
- Imported meetings are visible to the operator; participant grants are explicit.
  The current upstream live viewer is unaffected. No unified login or live backfill.

## Rollback

Stop the bridge and optional workbench only; the chair has no dependency on them.
Return to the previous upstream checkout to undo atomic file publication. Do not
run the bridge against an active old writer. Preserve private DB/KEK/receipts and
backups together; do not restore over a live DB or delete the KEK. Restoring an old
backup may restore old credentials/permissions and requires a separate review.

## Validation / PR decision

See [current evidence](evidence/enterprise-sidecar/README.md). Local synthetic
integration remains opt-in, not an instruction to merge before demo. These local
updates have not been published to the PR. Remaining gates: new Linux CI, clean deployment dependencies, Raspberry
Pi real voice shutdown/disk behavior, source lifecycle, and product acceptance.
