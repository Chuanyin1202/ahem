from pathlib import Path


def test_discord_error_logging_never_mentions_voice_secret_or_packet_bytes():
    source = (Path(__file__).parents[1] / "src/meeting_host/discord_source.py").read_text()
    assert "self.voice_client.secret_key" not in source
    assert 'len(packet_data), packet_data' not in source


def test_spectator_frontend_never_puts_token_in_event_stream_url():
    source = (Path(__file__).parents[1] /
              "src/meeting_host/spectator/index.html").read_text()
    assert 'EventSource("/events?token=' not in source
    assert 'EventSource("/events")' in source
    assert 'fetch("/session"' in source
