"""Opt-in local file sidecar. No network calls or imports from the live chair."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from .enterprise import Workspace
from .enterprise_security import load_kek

MAX_BYTES = 4 * 1024 * 1024


def read_private(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Private regular file required')
        if info.st_uid != os.geteuid() or info.st_size > limit:
            raise ValueError('Unexpected file owner or size')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('File too large')
    return data


def sync_once(ws, source, token, policy='team', days=7):
    """Scan only the explicitly assigned private directory; never delete source data."""
    source = Path(source)
    if source.is_symlink() or not source.is_dir() or source.stat().st_mode & 0o077:
        raise ValueError('Private source directory required')
    actor = ws.identify(token)
    if actor is None or actor['role'] != 'operator':
        raise PermissionError('Valid operator credential required')
    report = {'imported': 0, 'duplicates': 0, 'rejected': 0}
    for path in sorted(source.glob('*.events.jsonl')):
        try:
            raw = read_private(path)
            # Content identity survives rename/retry; names and paths are not stored.
            digest = hashlib.sha256(raw).hexdigest()
            exists = ws.db.execute('SELECT 1 FROM import_receipts WHERE tenant=? AND source=?',
                                   (actor['tenant'], digest)).fetchone()
            if exists:
                report['duplicates'] += 1
                continue
            events = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
            ws.ingest(actor, events, policy, days, source_id=digest)
            report['imported'] += 1
        except (OSError, ValueError, TypeError):
            # Do not log paths, transcript, malformed JSON, or credentials.
            report['rejected'] += 1
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--policy', choices=['team', 'regulated'], required=True)
    parser.add_argument('--days', type=int, choices=range(1, 31), default=7)
    parser.add_argument('--interval', type=int, default=0, help='0: one scan; otherwise >= 5 seconds')
    args = parser.parse_args()
    if args.interval != 0 and args.interval < 5:
        parser.error('interval must be zero or at least 5')
    while True:
        ws = None
        try:
            # Refresh persisted credential revocation/rotation on every scan.
            ws = Workspace(args.database, load_kek(), json.loads(read_private(args.identities)))
            report = sync_once(ws, args.source, read_private(args.token_file, 4096).decode().strip(),
                               args.policy, args.days)
            print(json.dumps(report), flush=True)
        except Exception:
            print('{"status":"scan_failed"}', flush=True)
            if not args.interval:
                raise SystemExit(1)
        else:
            if not args.interval:
                raise SystemExit(1 if report['rejected'] else 0)
        finally:
            if ws is not None:
                ws.db.close()
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
