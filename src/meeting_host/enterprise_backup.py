"""Local encrypted snapshots. Restore only to a new file, never a live DB."""
import argparse
import os
from pathlib import Path
import sqlite3
import time
import secrets
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .enterprise_security import EnvelopeStore, load_kek

MAGIC = b'AHEM-BACKUP-1\n'
MAX_BYTES = 64 * 1024 * 1024


def write_new(path, data):
    path = Path(path)
    if not path.parent.is_dir() or path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError('Destination directory must already exist and be private (0700)')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def validate(db, key):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite integrity check failed')
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'meetings','grants','disabled_members','audit'} <= tables:
        raise ValueError('Not a supported enterprise database')
    store = EnvelopeStore(key)
    count = 0
    for mid, blob in db.execute('SELECT id,blob FROM meetings'):
        # This offline local-admin CLI requires possession of the private KEK.
        store.decrypt_text(blob, meeting_id=mid, artifact_type='events', purpose='backup integrity verification', operator=True)
        count += 1
    return count


def snapshot(source, destination, key):
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
        raise ValueError('Expected a regular database under 64 MiB')
    db = sqlite3.connect(':memory:')
    try:
        origin = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
        try:
            origin.backup(db)
        finally:
            origin.close()
        count = validate(db,key)
        raw = db.serialize()
        if len(raw) > MAX_BYTES:
            raise ValueError('Snapshot exceeds 64 MiB')
        nonce = os.urandom(12)
        write_new(destination, MAGIC+nonce+AESGCM(key).encrypt(nonce,raw,MAGIC))
        return count
    finally:
        db.close()


def restore(source, key, destination=None):
    source = Path(source)
    if source.is_symlink() or source.stat().st_size > MAX_BYTES+128:
        raise ValueError('Invalid backup file')
    data = source.read_bytes()
    if not data.startswith(MAGIC):
        raise ValueError('Unknown backup format')
    nonce = data[len(MAGIC):len(MAGIC)+12]
    raw = AESGCM(key).decrypt(nonce,data[len(MAGIC)+12:],MAGIC)
    db = sqlite3.connect(':memory:')
    try:
        db.deserialize(raw)
        count = validate(db,key)
        if destination is not None:
            write_new(destination, db.serialize())
        return count
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','verify','restore','maintain'])
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--retention-days',type=int,default=7)
    parser.add_argument('--keep-at-least',type=int,default=2)
    parser.add_argument('--apply',action='store_true',help='Actually remove expired managed backups after verification')
    parser.add_argument('--interval',type=int,default=0,help='Maintain worker interval seconds, 0 for one cycle, otherwise >=60')
    args=parser.parse_args()
    if args.action != 'verify' and args.destination is None:
        parser.error('--destination is required; it must not exist')
    key=load_kek()
    if args.action=='maintain':
        if args.interval and args.interval<60:
            parser.error('--interval must be 0 or >=60')
        while True:
            result=maintain(args.source,args.destination,key,days=args.retention_days,keep=args.keep_at_least,apply=args.apply)
            print(result,flush=True)
            if not args.interval:
                return
            time.sleep(args.interval)
    count = snapshot(args.source,args.destination,key) if args.action=='backup' else restore(args.source,key,args.destination if args.action=='restore' else None)
    print(f'{args.action}: PASS; integrity and {count} encrypted meeting records verified')


def maintain(source, directory, key, *, days=7, keep=2, apply=False):
    """Create a verified snapshot then prune only verified managed-name files.

    Retention uses filesystem mtime, not meeting dates. Default is dry-run.
    Any invalid managed archive aborts before deletion; never follows symlinks.
    """
    directory=Path(directory)
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError('Managed directory must be private (0700)')
    if type(days) is not int or not 1<=days<=365 or type(keep) is not int or not 2<=keep<=100:
        raise ValueError('Expected 1..365 days and 2..100 minimum backups')
    new=directory/f'ahem-snapshot-{time.time_ns()}-{secrets.token_hex(4)}.enc'
    snapshot(source,new,key)
    candidates=[]
    for path in directory.iterdir():
        if not re.fullmatch(r'ahem-snapshot-\d+-[a-f0-9]{8}\.enc',path.name):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('Managed entry must be a regular file')
        restore(path,key)  # Verify every candidate before considering deletion.
        stat=path.stat()
        candidates.append((stat.st_mtime_ns,path,stat.st_ino,stat.st_size))
    candidates.sort(key=lambda x:(x[0],x[1].name),reverse=True)
    expired=[x for x in candidates[keep:] if x[0]<time.time_ns()-days*86400*10**9]
    for modified,path,inode,size in expired:
        current=path.lstat()
        if path.is_symlink() or (current.st_ino,current.st_size,current.st_mtime_ns)!=(inode,size,modified):
            raise ValueError('Backup changed during verification')
    if apply:
        for _,path,_,_ in expired:
            path.unlink()
    return {'created':new.name,'verified':len(candidates),'expired_candidates':len(expired),'deleted':len(expired) if apply else 0,'dry_run':not apply,'keep_at_least':keep}


if __name__=='__main__':
    main()
