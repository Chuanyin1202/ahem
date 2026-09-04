import asyncio

import pytest

from meeting_host import speaker
from meeting_host.speaker import (
    AzureUsageBudget,
    AzureVoice,
    Voice,
    VoiceError,
    azure_spoken_text,
    build_voice,
)


def test_azure_spoken_text_keeps_confirmed_taiwan_pronunciations():
    assert azure_spoken_text("API 要收斂，myAPI 不動") == "誒批哀 要收練，myAPI 不動"
    assert azure_spoken_text("api 上線") == "誒批哀 上線"


def test_build_voice_keeps_elevenlabs_as_default():
    voice = build_voice({"ELEVENLABS_API_KEY": "eleven-key"})
    assert type(voice) is Voice
    assert voice.api_key == "eleven-key"


def test_build_voice_selects_confirmed_azure_defaults():
    voice = build_voice({
        "AHEM_TTS_PROVIDER": "azure",
        "AZURE_SPEECH_KEY": "azure-key",
        "AZURE_SPEECH_REGION": "eastasia",
    })
    assert isinstance(voice, AzureVoice)
    assert voice.voice_id == "zh-TW-HsiaoChenNeural"
    assert voice.rate == "+12%"


def test_build_voice_selects_confirmed_azure_male_voice():
    voice = build_voice({
        "AHEM_TTS_PROVIDER": "azure",
        "AZURE_SPEECH_KEY": "azure-key",
        "AZURE_SPEECH_REGION": "eastasia",
        "AZURE_TTS_GENDER": "male",
    })
    assert isinstance(voice, AzureVoice)
    assert voice.voice_id == "zh-TW-YunJheNeural"
    assert voice.rate == "+12%"


def test_build_voice_rejects_unknown_azure_gender():
    with pytest.raises(ValueError, match="female 或 male"):
        build_voice({
            "AHEM_TTS_PROVIDER": "azure",
            "AZURE_SPEECH_KEY": "azure-key",
            "AZURE_SPEECH_REGION": "eastasia",
            "AZURE_TTS_GENDER": "other",
        })


def test_explicit_azure_voice_overrides_gender_preset():
    voice = build_voice({
        "AHEM_TTS_PROVIDER": "azure",
        "AZURE_SPEECH_KEY": "azure-key",
        "AZURE_SPEECH_REGION": "eastasia",
        "AZURE_TTS_GENDER": "male",
        "AZURE_TTS_VOICE": "zh-TW-HsiaoYuNeural",
    })
    assert voice.voice_id == "zh-TW-HsiaoYuNeural"


@pytest.mark.parametrize("region", ["https://example.com", "eastasia/path", "east asia"])
def test_azure_region_rejects_non_region_values(region):
    with pytest.raises(ValueError, match="REGION"):
        AzureVoice("key", region=region)


def test_azure_request_uses_ssml_profile_and_raw_pcm(monkeypatch):
    captured = {}

    class Content:
        async def iter_chunked(self, size):
            assert size == 4096
            yield b"\x01\x02"

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, url, *, data, headers):
            captured.update(url=url, data=data.decode(), headers=headers)
            return Response()

    monkeypatch.setattr(speaker.aiohttp, "ClientSession", Session)
    class Budget:
        def reserve(self, text):
            captured["metered_text"] = text

    voice = AzureVoice("secret", region="eastasia", usage_budget=Budget())

    async def collect():
        return b"".join([chunk async for chunk in voice._raw_stream("API 要收斂 & 決定")])

    assert asyncio.run(collect()) == b"\x01\x02"
    assert captured["url"].startswith("https://eastasia.tts.speech.microsoft.com/")
    assert "誒批哀 要收練 &amp; 決定" in captured["data"]
    assert "zh-TW-HsiaoChenNeural" in captured["data"]
    assert "rate='+12%'" in captured["data"]
    assert captured["headers"]["X-Microsoft-OutputFormat"] == "raw-24khz-16bit-mono-pcm"
    assert captured["headers"]["Ocp-Apim-Subscription-Key"] == "secret"
    assert captured["metered_text"] == "誒批哀 要收練 & 決定"


def test_azure_usage_budget_warns_once_and_persists(tmp_path, caplog):
    budget = AzureUsageBudget(tmp_path / "usage.json", monthly_limit=100,
                              hard_stop_percent=95, warning_percents=(80, 90, 95))
    budget.reserve("字" * 81)
    budget.reserve("字" * 5)

    state = (tmp_path / "usage.json").read_text(encoding="utf-8")
    assert '"characters": 86' in state
    assert '"warned": [' in state
    assert caplog.text.count("免費額度提醒") == 1


def test_azure_usage_budget_blocks_before_safety_limit(tmp_path):
    budget = AzureUsageBudget(tmp_path / "usage.json", monthly_limit=100,
                              hard_stop_percent=95, warning_percents=(80, 90, 95))
    assert budget.reserve("字" * 95) == 95
    with pytest.raises(VoiceError, match="已阻止本次請求"):
        budget.reserve("再")
