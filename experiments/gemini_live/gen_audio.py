#!/usr/bin/env python3
"""用 macOS `say`（zh_TW / Meijia）把 scripts.py 裡的文本合成 16kHz mono PCM 測試音檔。

用法: python gen_audio.py
輸出: experiments/gemini_live/audio/<name>.pcm （raw s16le, 16kHz, mono）
      同時保留 <name>.wav 方便人耳確認來源音檔本身沒問題
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scripts import SCENARIOS, ZH_QUALITY_TEXT  # noqa: E402

AUDIO_DIR = Path(__file__).parent / "audio"
VOICE = "Meijia"  # zh_TW


def synth(name: str, text: str) -> None:
    aiff = AUDIO_DIR / f"{name}.aiff"
    wav = AUDIO_DIR / f"{name}.wav"
    pcm = AUDIO_DIR / f"{name}.pcm"
    subprocess.run(["say", "-v", VOICE, "-o", str(aiff), text], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ar", "16000", "-ac", "1", str(wav)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(aiff),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(pcm)],
        check=True,
    )
    aiff.unlink()
    secs = pcm.stat().st_size / (16000 * 2)
    print(f"{name}: {secs:.2f}s -> {pcm}")


def main() -> None:
    AUDIO_DIR.mkdir(exist_ok=True)
    for name, text in SCENARIOS.items():
        synth(name, text)
    synth("zh_quality", ZH_QUALITY_TEXT)


if __name__ == "__main__":
    main()
