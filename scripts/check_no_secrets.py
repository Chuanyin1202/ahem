#!/usr/bin/env python3
"""掃描 Git 追蹤文字檔中的高可信秘密格式；零網路、fail closed。"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "Discord bot token": re.compile(r"\b[MN][A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
}

FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pfx"}


def tracked_paths(repo: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    )
    return [repo / item.decode() for item in result.stdout.split(b"\0") if item]


def scan(paths: list[Path], repo: Path) -> list[str]:
    findings = []
    for path in paths:
        relative = path.relative_to(repo)
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"{relative}: 禁止追蹤的秘密檔名")
            continue
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        if b"\0" in raw or len(raw) > 5_000_000:
            continue
        text = raw.decode("utf-8", errors="ignore")
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: 疑似 {label}")
    return findings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path, help="留空時掃描所有 Git 追蹤檔")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    paths = [path.resolve() for path in args.paths] if args.paths else tracked_paths(repo)
    findings = scan(paths, repo)
    if findings:
        raise SystemExit("秘密掃描失敗：\n" + "\n".join(f"- {item}" for item in findings))
    print(f"秘密掃描通過：{len(paths)} 個追蹤檔，0 個高可信命中。")


if __name__ == "__main__":
    main()
