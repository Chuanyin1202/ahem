from pathlib import Path

from scripts.check_no_secrets import scan


def test_secret_scanner_flags_key_material_without_echoing_value(tmp_path):
    secret = "sk-" + "A" * 40
    path = tmp_path / "sample.txt"
    path.write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    findings = scan([path], tmp_path)
    assert findings == ["sample.txt:1: 疑似 OpenAI key"]
    assert secret not in findings[0]


def test_secret_scanner_allows_placeholder_documentation(tmp_path):
    path = tmp_path / ".env.example"
    path.write_text("OPENAI_API_KEY=<由秘密管理器注入>\n", encoding="utf-8")
    assert scan([path], tmp_path) == []


def test_secret_scanner_rejects_tracked_private_key_filename(tmp_path):
    path = tmp_path / "service.key"
    path.write_text("placeholder", encoding="utf-8")
    assert scan([path], tmp_path) == ["service.key: 禁止追蹤的秘密檔名"]
