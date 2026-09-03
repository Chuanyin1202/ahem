import json
from pathlib import Path

import pytest

from scripts.update_project_status import END, START, render_results, update_status


def sample_report() -> dict[str, object]:
    return {
        "pytest": {"passed": 7, "skipped": 2, "xfailed": 1},
        "secret_scan": "0 個高可信秘密命中",
        "pip_check": "No broken requirements found",
        "pip_audit": "No known vulnerabilities found",
        "bandit": "0 個 High severity finding",
    }


def test_update_status_replaces_only_generated_section(tmp_path: Path) -> None:
    status = tmp_path / "PROJECT_STATUS.md"
    status.write_text(f"before\n{START}\nold\n{END}\nafter\n", encoding="utf-8")
    generated = render_results(sample_report(), "abc1234", "deadbeef")
    update_status(status, generated)
    text = status.read_text(encoding="utf-8")
    assert text.startswith("before\n")
    assert text.endswith("\nafter\n")
    assert "7 passed、2 skipped、1 xfailed" in text
    assert "0 個高可信秘密命中" in text
    assert "abc1234" in text
    assert "deadbeef" in text


def test_update_status_fails_without_markers(tmp_path: Path) -> None:
    status = tmp_path / "PROJECT_STATUS.md"
    status.write_text("team-owned content\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing generated-section markers"):
        update_status(status, "generated")


def test_report_shape_is_json_serializable() -> None:
    assert json.loads(json.dumps(sample_report()))["pytest"]["passed"] == 7
