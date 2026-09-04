"""受限範圍的會議產物到期清除工具。"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

ALLOWED_SUFFIXES = (
    ".log", ".events.jsonl", ".host.md", ".minutes.md", ".ahem",
)


def expired_artifacts(directory: Path, *, ttl_hours: int = 24,
                      now: float | None = None) -> list[Path]:
    if ttl_hours <= 0:
        raise ValueError("ttl_hours 必須大於 0")
    directory = Path(directory).resolve()
    if not directory.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - ttl_hours * 3600
    return sorted(
        path for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith("meeting-")
        and path.name.endswith(ALLOWED_SUFFIXES)
        and path.stat().st_mtime < cutoff
    )


def purge_expired(directory: Path, *, ttl_hours: int = 24,
                  now: float | None = None, dry_run: bool = True) -> list[Path]:
    targets = expired_artifacts(directory, ttl_hours=ttl_hours, now=now)
    if not dry_run:
        for path in targets:
            path.unlink()
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description="清除到期的 Ahem 會議產物")
    parser.add_argument("--directory", type=Path, default=Path("meetings"))
    parser.add_argument("--ttl-hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true", help="實際刪除；未指定時只預覽")
    args = parser.parse_args()
    targets = purge_expired(
        args.directory, ttl_hours=args.ttl_hours, dry_run=not args.apply)
    for target in targets:
        print(target)
    print(f"{'已刪除' if args.apply else '預計刪除'} {len(targets)} 個檔案")


if __name__ == "__main__":
    main()
