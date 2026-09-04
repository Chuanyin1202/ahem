"""Operator 專用解密入口；明文只輸出到目前終端，不建立第二份檔案。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
from pathlib import Path

from .security import EnvelopeStore, audit_record, load_kek, prepare_private_dir


def append_audit_record(path: Path, record: dict) -> None:
    """Append one durable audit record without a read-modify-write race."""
    path = Path(path)
    prepare_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("稽核路徑必須是一般檔案")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description="解密 Ahem 會議產物")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--purpose", required=True, help="本次解密的明確用途")
    parser.add_argument("--operator", action="store_true", help="確認目前身分已取得 Operator 授權")
    parser.add_argument("--audit-file", type=Path, default=Path("meetings/security-audit.jsonl"))
    args = parser.parse_args()

    payload = json.loads(args.artifact.read_text())
    meeting_id = payload["meeting_id"]
    artifact_type = payload["artifact_type"]
    outcome = "denied"
    try:
        plaintext = EnvelopeStore(load_kek()).decrypt_text(
            args.artifact.read_bytes(), meeting_id=meeting_id, artifact_type=artifact_type,
            purpose=args.purpose, operator=args.operator)
        outcome = "allowed"
        print(plaintext)
    finally:
        record = audit_record(
            "decrypt", actor="local-operator" if args.operator else "unknown",
            meeting_id=meeting_id, purpose=args.purpose, outcome=outcome)
        append_audit_record(args.audit_file, record)


if __name__ == "__main__":
    main()
