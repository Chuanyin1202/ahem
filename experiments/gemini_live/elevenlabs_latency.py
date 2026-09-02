#!/usr/bin/env python3
"""Q2 對照組：同機同網路量測 ElevenLabs TTS 首位元組延遲，作為 Gemini Live 的基準對照。

請求參數複製自 src/meeting_host/speaker.py 的 Voice 類（僅參考，本檔不 import、不修改該檔）：
- voice_id: EXAVITQu4vr4xnSDxMaL (Sarah)
- model_id: eleven_flash_v2_5
- output_format: pcm_24000

用法: python elevenlabs_latency.py [--n 10]
"""
import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
MODEL_ID = "eleven_flash_v2_5"
RATE = 24000
URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream?output_format=pcm_{RATE}"
TEXT = "請用一句話跟我打招呼，並且簡單自我介紹。"


async def one_run(api_key: str) -> float:
    body = {"text": TEXT, "model_id": MODEL_ID, "language_code": "zh"}
    t0 = time.perf_counter()
    async with aiohttp.ClientSession() as s:
        async with s.post(URL, json=body, headers={"xi-api-key": api_key}) as r:
            if r.status != 200:
                raise RuntimeError(f"TTS HTTP {r.status}: {(await r.text())[:200]}")
            async for chunk in r.content.iter_chunked(4096):
                if chunk:
                    return time.perf_counter() - t0
    raise RuntimeError("沒有收到任何 chunk")


async def main(n: int) -> None:
    api_key = os.environ["ELEVENLABS_API_KEY"]
    xs = []
    for i in range(n):
        try:
            dt = await one_run(api_key)
            print(f"  run {i + 1}/{n}: {dt:.3f}s")
            xs.append(dt)
        except Exception as e:  # noqa: BLE001
            print(f"  run {i + 1}/{n}: ERROR {type(e).__name__}: {e}")
    if xs:
        xs.sort()
        print(f"\nElevenLabs TTS 首位元組: n={len(xs)}/{n}  min={xs[0]:.3f}s  "
              f"median={statistics.median(xs):.3f}s  max={xs[-1]:.3f}s  mean={statistics.mean(xs):.3f}s")
    else:
        print("UNVERIFIED：全部失敗")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()
    asyncio.run(main(args.n))
