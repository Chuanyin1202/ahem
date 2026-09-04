import base64
import stat

from meeting_host.preflight import run_checks, summary


def _secure_env():
    return {
        "AHEM_VIEWER_TOKEN": "v" * 32,
        "AHEM_OPERATOR_TOKEN": "o" * 32,
        "AHEM_SECURE_STORAGE": "1",
        "AHEM_DISABLE_WEB_SEARCH": "1",
        "DISCORD_BOT_TOKEN": "discord-test",
        "ELEVENLABS_API_KEY": "eleven-test",
        "OPENAI_API_KEY": "openai-test",
    }


def test_local_preflight_ready_without_exposing_secret_values(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    checks = run_checks(mode="local", host="127.0.0.1", port=0,
                        directory=directory, env=_secure_env(),
                        keychain_loader=lambda: b"k" * 32)
    result = summary(checks)
    assert result["ready"] is True
    rendered = repr(result)
    assert "v" * 32 not in rendered
    assert "o" * 32 not in rendered


def test_preflight_fails_closed_for_shared_tokens_and_public_local_bind(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir()
    directory.chmod(0o755)
    env = _secure_env()
    env["AHEM_OPERATOR_TOKEN"] = env["AHEM_VIEWER_TOKEN"]
    result = summary(run_checks(mode="local", host="0.0.0.0", port=0,
                                directory=directory, env=env,
                                keychain_loader=lambda: b"k" * 32))
    assert result["ready"] is False
    names = {item["name"] for item in result["checks"] if item["status"] == "fail"}
    assert {"role_separation", "network_boundary", "private_storage"} <= names


def test_lan_preflight_requires_https_origin_and_secure_cookie(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir()
    directory.chmod(stat.S_IRWXU)
    env = _secure_env()
    blocked = summary(run_checks(mode="lan", host="127.0.0.1", port=0,
                                directory=directory, env=env,
                                keychain_loader=lambda: b"k" * 32))
    assert blocked["ready"] is False
    env.update({"AHEM_TRUSTED_ORIGINS": "https://demo.local",
                "AHEM_COOKIE_SECURE": "1"})
    ready = summary(run_checks(mode="lan", host="127.0.0.1", port=0,
                               directory=directory, env=env,
                               keychain_loader=lambda: b"k" * 32))
    assert ready["ready"] is True


def test_preflight_rejects_missing_service_credentials_and_malformed_origin(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir(mode=0o700)
    env = _secure_env()
    env.pop("DISCORD_BOT_TOKEN")
    env.update({"AHEM_TRUSTED_ORIGINS": "https://user:pass@example.com/path",
                "AHEM_COOKIE_SECURE": "1"})
    result = summary(run_checks(mode="lan", host="127.0.0.1", port=0,
                                directory=directory, env=env,
                                keychain_loader=lambda: b"k" * 32))
    failures = {item["name"] for item in result["checks"] if item["status"] == "fail"}
    assert {"discord_credentials", "network_boundary"} <= failures


def test_preflight_no_llm_does_not_require_openai(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir(mode=0o700)
    env = _secure_env()
    env.pop("OPENAI_API_KEY")
    result = summary(run_checks(mode="local", host="127.0.0.1", port=0,
                                directory=directory, env=env, no_llm=True,
                                keychain_loader=lambda: b"k" * 32))
    assert result["ready"] is True


def test_preflight_rejects_invalid_azure_runtime_settings(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir(mode=0o700)
    env = _secure_env() | {
        "AHEM_TTS_PROVIDER": "azure",
        "AZURE_SPEECH_KEY": "azure-test",
        "AZURE_SPEECH_REGION": "https://wrong.example",
        "AZURE_TTS_GENDER": "robot",
        "AZURE_TTS_RATE": "fast",
        "AZURE_TTS_WARNING_PERCENTS": "90,101",
    }
    result = summary(run_checks(mode="local", host="127.0.0.1", port=0,
                                directory=directory, env=env,
                                keychain_loader=lambda: b"k" * 32))
    assert result["ready"] is False
    assert next(item for item in result["checks"]
                if item["name"] == "text_to_speech_configuration")["status"] == "fail"


def test_preflight_loads_linux_kek_file_without_test_environment_bypass(tmp_path):
    directory = tmp_path / "meetings"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    kek_path = tmp_path / "ahem-kek"
    kek_path.write_text(base64.b64encode(b"k" * 32).decode(), encoding="ascii")
    kek_path.chmod(0o600)
    env = _secure_env() | {"AHEM_KEK_FILE": str(kek_path)}
    result = summary(run_checks(mode="local", host="127.0.0.1", port=0,
                                directory=directory, env=env))
    assert result["ready"] is True
    assert next(item for item in result["checks"]
                if item["name"] == "key_encryption_key")["status"] == "pass"
