#!/usr/bin/env python3
"""用本機 TTS 產生 16kHz mono WAV，供 STT 資料生成與重疊壓力測試。

只呼叫本機 `say`（macOS）或 `espeak-ng`／`espeak`，不把文字送到雲端。
輸出的 manifest 不保存逐句文字，只記情境雜湊、引擎與音軌統計。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2


def load_scenario(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    lines = raw.get("lines") if isinstance(raw, dict) else None
    if not isinstance(lines, list) or not lines:
        raise ValueError("scenario 必須包含非空的 lines 陣列")
    cleaned = []
    cursor = 0.0
    for index, item in enumerate(lines):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            raise ValueError(f"lines[{index}] 缺少 text")
        start = float(item.get("t", cursor))
        pause = float(item.get("pause_after", 0.4))
        if start < 0 or pause < 0:
            raise ValueError(f"lines[{index}] 的 t／pause_after 不可為負")
        cleaned.append({
            "speaker": str(item.get("speaker", f"S{index + 1}")),
            "text": str(item["text"]).strip(),
            "t": start,
            "pause_after": pause,
        })
        cursor = start + pause
    return cleaned


def find_engine() -> str:
    for name in ("say", "espeak-ng", "espeak"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("找不到本機 TTS；請安裝 espeak-ng，macOS 可直接使用 say")


def synth_pcm(engine: str, text: str, voice: str | None = None) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，無法正規化成本專案使用的 16kHz mono PCM")
    with tempfile.TemporaryDirectory(prefix="ahem-synth-") as temp:
        source = Path(temp) / ("line.aiff" if Path(engine).name == "say" else "line.wav")
        if Path(engine).name == "say":
            command = [engine, *( ["-v", voice] if voice else []), "-o", str(source), text]
        else:
            command = [engine, *( ["-v", voice] if voice else []), "-w", str(source), text]
        generated = subprocess.run(command, capture_output=True)
        if generated.returncode != 0:
            raise RuntimeError(f"本機 TTS 失敗：{generated.stderr.decode(errors='replace')[:300]}")
        converted = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(source), "-f", "s16le", "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True,
        )
        if converted.returncode != 0:
            raise RuntimeError(f"ffmpeg 轉檔失敗：{converted.stderr.decode(errors='replace')[:300]}")
        return converted.stdout


def mix_tracks(tracks: list[tuple[float, bytes]]) -> bytes:
    """依開始秒數混合 little-endian PCM16；重疊時飽和，不發生整數溢位。"""
    if not tracks:
        return b""
    decoded = []
    total = 0
    for start, pcm in tracks:
        if len(pcm) % SAMPLE_WIDTH:
            raise ValueError("PCM16 長度必須是 2 的倍數")
        offset = round(start * SAMPLE_RATE)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        decoded.append((offset, samples))
        total = max(total, offset + len(samples))
    mixed = [0] * total
    for offset, samples in decoded:
        for index, sample in enumerate(samples):
            value = mixed[offset + index] + sample
            mixed[offset + index] = max(-32768, min(32767, value))
    return struct.pack(f"<{len(mixed)}h", *mixed)


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(SAMPLE_WIDTH)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(pcm)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", action="append", default=[], metavar="SPEAKER=VOICE")
    args = parser.parse_args()
    voices = dict(value.split("=", 1) for value in args.voice)
    lines = load_scenario(args.scenario)
    engine = find_engine()
    tracks = [(line["t"], synth_pcm(engine, line["text"], voices.get(line["speaker"])))
              for line in lines]
    pcm = mix_tracks(tracks)
    write_wav(args.output, pcm)
    scenario_hash = hashlib.sha256(args.scenario.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "synthetic": True,
        "quality_claim": "STT 壓力工具；不可代替真人會議品質驗收",
        "scenario_sha256": scenario_hash,
        "engine": Path(engine).name,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "duration_seconds": round(len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH), 3),
        "line_count": len(lines),
        "speaker_count": len({line["speaker"] for line in lines}),
    }
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已產生 {args.output} 與 {manifest_path}（synthetic，不能當真人品質證據）")


if __name__ == "__main__":
    main()
