from scripts.render_pr_evidence import render


def test_pr_evidence_contains_reproducible_results_without_secrets():
    report = {
        "pytest": {"passed": 10, "skipped": 2, "xfailed": 1},
        "secret_scan": "0 findings",
        "pip_check": "No broken requirements found",
        "pip_audit": "No known vulnerabilities found",
        "bandit": "0 High findings",
    }
    evidence = render(
        report, sha="abc123", system="Linux aarch64", python="3.12.1",
        bypass_matches=0, run_url="https://example.test/actions/runs/1")
    assert "10 passed, 2 skipped, 1 xfailed; exit 0" in evidence
    assert "PYTEST_CURRENT_TEST search | 0 matches; PASS" in evidence
    assert "Linux aarch64" in evidence
    assert "abc123" in evidence
    assert "credentials=" not in evidence
    assert "token=" not in evidence.lower()


def test_pr_evidence_marks_bypass_search_failure():
    report = {
        "pytest": {"passed": 1, "skipped": 0, "xfailed": 0},
        "secret_scan": "0 findings", "pip_check": "pass",
        "pip_audit": "pass", "bandit": "pass",
    }
    evidence = render(
        report, sha="abc", system="Linux", python="3.12",
        bypass_matches=2)
    assert "2 matches; FAIL" in evidence
