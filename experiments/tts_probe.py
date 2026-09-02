"""真的叫一次 ElevenLabs，把「earcon + 一句話」寫成 wav 聽拼接與音質。用法：
    python experiments/tts_probe.py "阿凱，你已經講了三分鐘，先讓其他人接一下。" out.wav

延遲分解／對照實驗（T10 根因調查用，見 docs/validation-results.md）：
    python experiments/tts_probe.py --bench conn -n 10                  # 純 DNS/TCP/TLS 握手（不叫 TTS，不花額度）
    python experiments/tts_probe.py --bench fresh -n 10 --text "..."    # 現況：每次新開 ClientSession
    python experiments/tts_probe.py --bench reuse -n 10 --text "..."    # 對照：重用同一個 ClientSession
    python experiments/tts_probe.py --bench osl -n 10 --text "..."      # 對照：optimize_streaming_latency 開/關
輸出皆為 JSON lines（一行一次量測），用 jq 或自己寫個 python one-liner 算 min/median/max。
"""
import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
import time
import wave
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent.parent / ".env")
from meeting_host.audio import DISCORD_RATE  # noqa: E402
from meeting_host.speaker import TTS_MODEL, TTS_RATE, TTS_URL, VOICE_ID, Earcon, Voice  # noqa: E402

HOST = "api.elevenlabs.io"


async def main(text: str, out: str) -> None:
    v = Voice(os.environ["ELEVENLABS_API_KEY"])
    t0 = time.perf_counter()
    first = None
    chunks = []
    async for pcm in v.synth(text):
        first = first or time.perf_counter() - t0
        chunks.append(pcm)
    pcm = Earcon().pcm + b"\x00" * (DISCORD_RATE * 4 * 7 // 10) + b"".join(chunks)  # earcon + 0.7s 靜音 + 語音
    with wave.open(out, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(DISCORD_RATE); w.writeframes(pcm)
    print(f"首位元組 {first:.2f}s，總 {time.perf_counter() - t0:.2f}s，語音 {len(b''.join(chunks)) / (DISCORD_RATE * 4):.1f}s → {out}")


# ── Phase 1：純連線握手分解（不打 TTS，量 DNS / TCP / TLS 各自成本）──

async def handshake_once(host: str = HOST, port: int = 443) -> dict:
    loop = asyncio.get_running_loop()
    ctx = ssl.create_default_context()

    t0 = time.perf_counter()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    t_dns = time.perf_counter()

    ip = infos[0][4][0]
    reader, writer = await asyncio.open_connection(ip, port)  # 純 TCP，先不帶 ssl
    t_tcp = time.perf_counter()

    transport = writer.transport
    protocol = transport.get_protocol()
    new_transport = await loop.start_tls(transport, protocol, ctx, server_hostname=host)  # 在既有 TCP 上升級 TLS
    t_tls = time.perf_counter()

    new_transport.close()
    return {
        "dns_ms": (t_dns - t0) * 1000,
        "tcp_ms": (t_tcp - t_dns) * 1000,
        "tls_ms": (t_tls - t_tcp) * 1000,
        "total_ms": (t_tls - t0) * 1000,
    }


# ── Phase 2：實際打 TTS stream，用 aiohttp TraceConfig 分解 ──

def _make_trace_config(record: dict) -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()

    async def on_dns_start(session, ctx, params):
        record["dns_start"] = time.perf_counter()

    async def on_dns_end(session, ctx, params):
        record["dns_end"] = time.perf_counter()

    async def on_conn_create_start(session, ctx, params):
        record["conn_create_start"] = time.perf_counter()
        record["reused"] = False

    async def on_conn_create_end(session, ctx, params):
        record["conn_create_end"] = time.perf_counter()

    async def on_conn_reuse(session, ctx, params):
        record["reused"] = True
        record["conn_reuse_at"] = time.perf_counter()

    async def on_request_start(session, ctx, params):
        record.setdefault("request_start", time.perf_counter())

    async def on_response_chunk_received(session, ctx, params):
        record.setdefault("first_chunk_at", time.perf_counter())  # 只記第一次

    tc.on_dns_resolvehost_start.append(on_dns_start)
    tc.on_dns_resolvehost_end.append(on_dns_end)
    tc.on_connection_create_start.append(on_conn_create_start)
    tc.on_connection_create_end.append(on_conn_create_end)
    tc.on_connection_reuseconn.append(on_conn_reuse)
    tc.on_request_start.append(on_request_start)
    tc.on_response_chunk_received.append(on_response_chunk_received)
    tc.freeze()  # Signal.send() 要求 frozen，見 aiosignal.Signal.send
    return tc


async def tts_once(session: aiohttp.ClientSession, text: str, api_key: str, osl: int | None, record: dict) -> dict:
    """打一次 stream TTS，回傳這次的分解時間點。record 是呼叫端已經掛在 session._trace_configs
    上那個 TraceConfig 綁定的同一個 dict——這裡不能自己另開一個，否則收不到任何 trace 事件（T10 踩過）。"""
    url = TTS_URL.format(voice_id=VOICE_ID, rate=TTS_RATE)
    if osl is not None:
        url += f"&optimize_streaming_latency={osl}"
    body = {"text": text, "model_id": TTS_MODEL, "language_code": "zh"}

    t0 = time.perf_counter()
    # trace_configs 綁在 session 上（見 bench_tts/bench_osl 呼叫端的 session._trace_configs），
    # 這裡只負責發request、收第一個 chunk，時間點都靠 record 這個共享 dict 收集
    async with session.post(url, json=body, headers={"xi-api-key": api_key}) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:200]}")
        # 一定要把整個 stream 讀完（跟 speaker.py 的 synth() 一樣）——只讀第一個 chunk 就跳出
        # async with 會讓 aiohttp 判定連線沒讀完不安全重用，直接關掉，reuse 模式會被測試方法本身
        # 污染成「每次都是新連線」（T10 踩過：一開始漏了這段，reuse 組看起來完全沒在重用）。
        async for _chunk in r.content.iter_chunked(4096):
            record.setdefault("first_chunk_observed", time.perf_counter())

    def ms(key_end, key_start):
        if key_end in record and key_start in record:
            return (record[key_end] - record[key_start]) * 1000
        return None

    dns_ms = ms("dns_end", "dns_start")
    connect_ms = ms("conn_create_end", "conn_create_start")
    first_chunk = record.get("first_chunk_at") or record.get("first_chunk_observed")
    ttfb_total_ms = (first_chunk - t0) * 1000 if first_chunk else None
    req_to_first_ms = None
    if "request_start" in record and first_chunk:
        req_to_first_ms = (first_chunk - record["request_start"]) * 1000
    return {
        "reused": record.get("reused", None),
        "dns_ms": dns_ms,
        "connect_ms": connect_ms,  # None 代表重用連線，沒有新握手
        "request_to_first_byte_ms": req_to_first_ms,  # 送出請求 → 收到第一個 chunk（含伺服器合成時間）
        "ttfb_total_ms": ttfb_total_ms,  # 呼叫端整體：從決定要打這通開始 → 收到第一個 chunk
    }


async def bench_conn(n: int) -> None:
    for i in range(n):
        r = await handshake_once()
        print(json.dumps({"i": i, **r}))


async def bench_tts(mode: str, n: int, text: str) -> None:
    api_key = os.environ["ELEVENLABS_API_KEY"]
    if mode == "reuse":
        async with aiohttp.ClientSession() as session:
            # trace_configs 要在 session 建立時給，但每次呼叫要換一個新的 record dict 才能各自收集時間點，
            # session._trace_configs 是私有屬性，但 client.py 的 _request() 每次呼叫都重讀（見 3.14 原始碼），
            # 所以可以在同一個 session 上逐次替換，不影響底層連線池（重用連線與否才是這裡要測的變因）。
            for i in range(n):
                record: dict = {}
                tc = _make_trace_config(record)
                session._trace_configs = [tc]
                r = await tts_once(session, text, api_key, None, record)
                print(json.dumps({"i": i, "mode": "reuse", **r}))
    else:
        for i in range(n):
            record: dict = {}
            tc = _make_trace_config(record)
            async with aiohttp.ClientSession(trace_configs=[tc]) as session:
                r = await tts_once(session, text, api_key, None, record)
            print(json.dumps({"i": i, "mode": "fresh", **r}))


async def bench_osl(n: int, text: str) -> None:
    """交錯 osl=None 與 osl=4，同一顆重用連線下對照，排除「連線冷熱」造成的混淆。"""
    api_key = os.environ["ELEVENLABS_API_KEY"]
    async with aiohttp.ClientSession() as session:
        for i in range(n):
            for osl in (None, 4):
                record: dict = {}
                tc = _make_trace_config(record)
                session._trace_configs = [tc]
                r = await tts_once(session, text, api_key, osl, record)
                print(json.dumps({"i": i, "mode": "osl", "osl": osl, **r}))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--bench":
        p = argparse.ArgumentParser()
        p.add_argument("--bench", required=True, choices=["conn", "fresh", "reuse", "osl"])
        p.add_argument("-n", type=int, default=10)
        p.add_argument("--text", default="好，我們繼續下一位。")
        args = p.parse_args()
        if args.bench == "conn":
            asyncio.run(bench_conn(args.n))
        elif args.bench == "osl":
            asyncio.run(bench_osl(args.n, args.text))
        else:
            asyncio.run(bench_tts(args.bench, args.n, args.text))
    else:
        asyncio.run(main(sys.argv[1], sys.argv[2]))
