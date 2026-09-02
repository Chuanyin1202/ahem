"""Q1~Q4 共用的連線/送音工具。"""
import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).parent.parent.parent / ".env")

AUDIO_DIR = Path(__file__).parent / "audio"
OUT_DIR = Path(__file__).parent / "out"
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK_MS = 100
CHUNK_BYTES = INPUT_RATE * 2 * CHUNK_MS // 1000

MODEL_PROACTIVE = "models/gemini-2.5-flash-native-audio-preview-09-2025"  # 支援 proactive_audio / affective_dialog
MODEL_BASELINE = "models/gemini-3.1-flash-live-preview"  # 文件明寫不支援 proactive_audio


def client(api_version: str = "v1beta") -> genai.Client:
    # 實測發現：proactivity / enable_affective_dialog 這兩個 setup 欄位在 v1beta 會被
    # 伺服器直接拒絕（"Unknown name ... at 'setup': Cannot find field."），
    # 必須用 v1alpha 才吃得進去——這點與官方文件寫的「v1beta」不符，以實測為準。
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"api_version": api_version})


async def stream_pcm(session, path: Path, realtime: bool = True) -> float:
    """把一個 pcm 檔用固定節奏餵進 session，回傳送完所花的秒數。"""
    data = path.read_bytes()
    t0 = time.perf_counter()
    for i in range(0, len(data), CHUNK_BYTES):
        chunk = data[i:i + CHUNK_BYTES]
        await session.send_realtime_input(
            audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}")
        )
        if realtime:
            await asyncio.sleep(CHUNK_MS / 1000)
    return time.perf_counter() - t0
