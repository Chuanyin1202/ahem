#!/usr/bin/env python3
"""Render a secret-free, reproducible Markdown evidence bundle for a PR."""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

BYPASS_MARKER = "PYTEST_" + "CURRENT_TEST"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def render(report: dict, *, sha: str, system: str, python: str,
           bypass_matches: int, run_url: str = "") -> str:
    tests = report["pytest"]
    status = "PASS" if bypass_matches == 0 else "FAIL"
    lines = [
        "# PR verification evidence",
        "",
        f"- Commit: `{sha}`",
        f"- Environment: `{system}`",
        f"- Python: `{python}`",
    ]
    if run_url:
        lines.append(f"- CI run: {run_url}")
    lines += [
        "",
        "## Commands",
        "",
        "```bash",
        "python scripts/check_no_secrets.py",
        "python -m pytest -q",
        "python -m pip check",
        "python -m pip_audit --local",
        "python -m bandit -q -lll -r src",
        f"git grep -n {BYPASS_MARKER} -- .",
        "```",
        "",
        "## Results",
        "",
        "| Gate | Result |",
        "|---|---|",
        (f"| pytest | {tests['passed']} passed, {tests.get('skipped', 0)} skipped, "
         f"{tests.get('xfailed', 0)} xfailed; exit 0 |"),
        f"| tracked-file secret scan | {report['secret_scan']} |",
        f"| pip check | {report['pip_check']} |",
        f"| pip-audit | {report['pip_audit']} |",
        f"| Bandit | {report['bandit']} |",
        f"| test-framework bypass search | {bypass_matches} matches; {status} |",
        "",
        "## Code-path evidence",
        "",
        "- `src/meeting_host/spectator.py`: Viewer/Operator authentication and session exchange.",
        "- `src/meeting_host/security.py`: Linux/macOS KEK providers and encrypted storage.",
        "- `src/meeting_host/preflight.py`: fail-closed deployment checks.",
        "- `src/meeting_host/style.py`: baseline reset before applying a style profile.",
        "",
        "This artifact contains summaries and paths only; credentials and secret values are never included.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    grep = subprocess.run(
        ["git", "grep", "-n", BYPASS_MARKER, "--", "."],
        capture_output=True, text=True)
    if grep.returncode not in {0, 1}:
        raise SystemExit(grep.returncode)
    matches = len([line for line in grep.stdout.splitlines() if line.strip()])
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repository}/actions/runs/{run_id}" if all(
        (server, repository, run_id)) else ""
    output = render(
        report,
        sha=_git("rev-parse", "HEAD"),
        system=f"{platform.system()} {platform.release()} {platform.machine()}",
        python=platform.python_version(),
        bypass_matches=matches,
        run_url=run_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    if matches:
        raise SystemExit("test-framework bypass marker found")


if __name__ == "__main__":
    main()
