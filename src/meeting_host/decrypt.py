"""Operator 專用解密入口；明文只輸出到目前終端，不建立第二份檔案。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .security import EnvelopeStore, audit_record, load_kek, secure_write_text


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
        existing = args.audit_file.read_text() if args.audit_file.exists() else ""
        secure_write_text(args.audit_file, existing + json.dumps(record) + "\n")


if __name__ == "__main__":
    main()
