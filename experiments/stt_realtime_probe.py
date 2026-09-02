#!/usr/bin/env python3
"""Scribe v2 Realtime 串流探針。

驗證項 #1 的本體：量測中文即時語音辨識的準確度與端到端延遲。

這不是一次性腳本——它是 development-plan.md P1#11「歷史回放評估」的雛形，
之後會反覆拿歷次會議錄音重跑。

用法:
    python experiments/stt_realtime_probe.py <音檔> [--lang zho] [--realtime]

    --realtime  依音訊實際長度節流送出（模擬真實會議），量到的延遲才有意義
                不加此旗標則全速灌入，只驗準確度、不驗延遲
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import websockets
from dotenv import load_dotenv

SAMPLE_RATE = 16000
CHUNK_MS = 250  # 每個 chunk 的音訊長度
BYTES_PER_SAMPLE = 2
CHUNK_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * CHUNK_MS // 1000
WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


def to_pcm16k(path: Path) -> bytes:
    """任意音檔 → 16kHz 單聲道 PCM raw。"""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        sys.exit(f"ffmpeg 轉檔失敗: {result.stderr.decode()[:500]}")
    return result.stdout


async def probe(audio: bytes, api_key: str, lang: str, model: str, realtime: bool,
                keyterms: list[str] | None = None) -> dict:
    params = [
        f"model_id={model}",
        f"audio_format=pcm_{SAMPLE_RATE}",
        "include_timestamps=true",
        "commit_strategy=vad",
    ]
    if lang:
        params.append(f"language_code={lang}")
    # 會議場景的專有名詞（人名、專案名、技術詞）餵進來可提升辨識率，
    # 特別是中文語境裡夾雜的英文詞
    for term in keyterms or []:
        params.append(f"keyterms={quote(term)}")
    url = f"{WS_URL}?{'&'.join(params)}"

    events: list[dict] = []
    # 每個 chunk 送出的時刻，用來回推「說完 → 出字」的延遲
    sent_at: list[float] = []
    t0 = time.perf_counter()

    async with websockets.connect(url, additional_headers={"xi-api-key": api_key}) as ws:

        async def receive():
            async for raw in ws:
                msg = json.loads(raw)
                events.append({"t": time.perf_counter() - t0, "msg": msg})
                mt = msg.get("message_type")
                if mt == "session_started":
                    print(f"  [session] {msg.get('session_id', '?')}")
                elif mt == "partial_transcript":
                    print(f"  [部分 {events[-1]['t']:6.2f}s] {msg.get('text', '')}")
                elif mt and mt.startswith("committed_transcript"):
                    print(f"  [確定 {events[-1]['t']:6.2f}s] {msg.get('text', '')}")
                elif mt == "error" or "detail" in msg:
                    print(f"  [錯誤] {msg}")

        receiver = asyncio.create_task(receive())

        for i in range(0, len(audio), CHUNK_BYTES):
            chunk = audio[i:i + CHUNK_BYTES]
            sent_at.append(time.perf_counter() - t0)
            await ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(chunk).decode(),
                "commit": False,
                "sample_rate": SAMPLE_RATE,
            }))
            if realtime:
                await asyncio.sleep(CHUNK_MS / 1000)

        # 收尾：強制 commit 尾段
        await ws.send(json.dumps({
            "message_type": "input_audio_chunk",
            "audio_base_64": "",
            "commit": True,
            "sample_rate": SAMPLE_RATE,
        }))
        audio_done_at = time.perf_counter() - t0

        try:
            await asyncio.wait_for(receiver, timeout=15)
        except asyncio.TimeoutError:
            receiver.cancel()

    return {"events": events, "audio_done_at": audio_done_at,
            "audio_secs": len(audio) / (SAMPLE_RATE * BYTES_PER_SAMPLE)}


def report(result: dict, realtime: bool) -> None:
    events = result["events"]
    # committed_transcript 與 committed_transcript_with_timestamps 是同一段的兩種表示，
    # 只取前者計入文字，否則轉錄會被串接兩次
    finals = [e for e in events if e["msg"].get("message_type") == "committed_transcript"]
    stamped = [e for e in events
               if e["msg"].get("message_type") == "committed_transcript_with_timestamps"]
    if not finals:  # 只回傳帶時間戳版本時的退路
        finals = stamped
    partials = [e for e in events if e["msg"].get("message_type") == "partial_transcript"]

    print("\n" + "=" * 60)
    print(f"音訊長度      : {result['audio_secs']:.2f}s")
    print(f"送完音訊於    : {result['audio_done_at']:.2f}s")
    print(f"部分/確定筆數 : {len(partials)} / {len(finals)}")

    if partials:
        print(f"首次出字      : {partials[0]['t']:.2f}s")
    if finals:
        print(f"末次確定      : {finals[-1]['t']:.2f}s")
        tail = finals[-1]["t"] - result["audio_done_at"]
        print(f"尾段延遲      : {tail:.2f}s  ← 說完到出最終文字")
        if not realtime:
            print("  ⚠️  未加 --realtime，延遲數字不代表真實會議情境")

    text = "".join(e["msg"].get("text", "") for e in finals)
    print(f"\n完整轉錄:\n{text or '(無)'}")

    # speaker_id 是文件標為 optional 的欄位，實測它到底有沒有回傳
    speakers = {w.get("speaker_id") for e in stamped
                for w in e["msg"].get("words", []) if w.get("speaker_id")}
    print(f"\nspeaker_id    : {speakers or '未回傳（realtime 無 diarization，符合預期）'}")
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--lang", default="zho")
    ap.add_argument("--model", default="scribe_v2_realtime")
    ap.add_argument("--realtime", action="store_true",
                    help="依實際音訊長度節流，量真實延遲")
    ap.add_argument("--keyterms", nargs="*", default=None,
                    help="專有名詞提示（人名、專案名、技術詞）")
    args = ap.parse_args()

    load_dotenv()
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        sys.exit("缺 ELEVENLABS_API_KEY，請確認 .env")
    if not args.audio.exists():
        sys.exit(f"找不到音檔: {args.audio}")

    audio = to_pcm16k(args.audio)
    print(f"檔案: {args.audio.name}  ({len(audio) / (SAMPLE_RATE * BYTES_PER_SAMPLE):.2f}s, "
          f"model={args.model}, lang={args.lang}, realtime={args.realtime})\n")

    result = asyncio.run(probe(audio, api_key, args.lang, args.model, args.realtime,
                               args.keyterms))
    report(result, args.realtime)


if __name__ == "__main__":
    main()
