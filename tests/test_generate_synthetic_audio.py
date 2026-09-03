import json
import struct

import pytest

from experiments.generate_synthetic_audio import SAMPLE_RATE, load_scenario, mix_tracks


def _pcm(*samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def test_mix_tracks_places_and_saturates_overlapping_pcm():
    one_sample_later = 1 / SAMPLE_RATE
    mixed = mix_tracks([(0.0, _pcm(30_000, -30_000)),
                        (one_sample_later, _pcm(10_000, -10_000))])
    assert struct.unpack("<3h", mixed) == (30_000, -20_000, -10_000)
    assert struct.unpack("<2h", mix_tracks([(0.0, _pcm(30_000, -30_000)),
                                            (0.0, _pcm(10_000, -10_000))])) == (32767, -32768)


def test_load_scenario_supports_explicit_timeline_and_rejects_missing_text(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"lines": [{"t": 1.5, "speaker": "甲", "text": "測試"}]}),
                     encoding="utf-8")
    assert load_scenario(valid)[0]["t"] == 1.5
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"lines": [{"speaker": "甲"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少 text"):
        load_scenario(invalid)
