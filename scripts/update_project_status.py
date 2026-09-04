"""Safely update the generated security evidence in PROJECT_STATUS.md."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

START = "<!-- AUTO-SECURITY-RESULTS:START -->"
END = "<!-- AUTO-SECURITY-RESULTS:END -->"


def git_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_results(report: dict[str, object], commit: str, sbom_hash: str) -> str:
    now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S %Z")
    pytest = report["pytest"]
    assert isinstance(pytest, dict)
    return "\n".join(
        [
            START,
            f"- 執行時間：`{now}`。",
            f"- Git commit：`{commit}`。",
            (
                "- `pytest`："
                f"{pytest['passed']} passed、{pytest['skipped']} skipped、"
                f"{pytest['xfailed']} xfailed。"
            ),
            f"- 秘密掃描：{report['secret_scan']}。",
            f"- `pip check`：{report['pip_check']}。",
            f"- `pip-audit --local`：{report['pip_audit']}。",
            f"- `bandit -lll -r src`：{report['bandit']}。",
            f"- `sbom.cdx.json` SHA-256：`{sbom_hash}`。",
            END,
        ]
    )


def update_status(status_path: Path, generated: str) -> None:
    text = status_path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise ValueError("PROJECT_STATUS.md is missing generated-section markers")
    updated = pattern.sub(generated, text, count=1)
    status_path.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--status", type=Path, default=Path("PROJECT_STATUS.md"))
    parser.add_argument("--sbom", type=Path, default=Path("sbom.cdx.json"))
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    repo = args.status.resolve().parent
    update_status(args.status, render_results(report, git_commit(repo), sha256(args.sbom)))


if __name__ == "__main__":
    main()
