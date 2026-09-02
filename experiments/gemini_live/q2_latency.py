#!/usr/bin/env python3
"""Q2：原生音訊輸出的首位元組延遲。

量測「音檔送完（含 VAD 判斷已經講完）→ 收到第一個輸出音訊 byte」的時間，
對每個模型跑 N 次取分佈（min/median/max），而不是只看平均。

用法: python q2_latency.py [--n 10] [--model proactive|baseline|both]
"""
import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import AUDIO_DIR, MODEL_BASELINE, MODEL_PROACTIVE, client, stream_pcm  # noqa: E402
from google.genai import types  # noqa: E402

PROMPT_AUDIO = AUDIO_DIR / "latency_prompt.pcm"


async def one_run(c, model: str, api_version: str) -> dict | None:
    """回傳兩個時間點：
    - total: 從開始送音訊（含真實語速的串流時間）到第一個輸出音訊 byte
    - post_input: 從音訊送完（VAD 應該已判斷講完）到第一個輸出音訊 byte
      這個數字才是跟 ElevenLabs TTS「首位元組 2.44s」（文字送出到出聲）同基準的比較對象——
      TTS 沒有「使用者講話」這段時間，Gemini Live 有，所以不能拿 total 直接比 2.44s。
    """
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction="你是一個語音助理，請直接用繁體中文簡短回答使用者的話。",
    )
    async with c.aio.live.connect(model=model, config=config) as session:
        first_byte_at = None
        send_done_at = None
        send_done = asyncio.Event()

        async def receiver():
            # session.receive() 在 turn_complete 後就結束該次 async generator，包外層
            # while 迴圈避免 VAD 把輸入切成多個 turn 時漏掉真正有音訊的那個 turn
            # （見 q1_proactive_audio.py 的說明）。
            nonlocal first_byte_at
            while True:
                got_any = False
                async for msg in session.receive():
                    got_any = True
                    if first_byte_at is None:
                        sc = msg.server_content
                        if sc and sc.model_turn and sc.model_turn.parts:
                            for p in sc.model_turn.parts:
                                if p.inline_data and p.inline_data.data:
                                    first_byte_at = time.perf_counter()
                    if first_byte_at is not None and send_done.is_set():
                        return
                if not got_any:
                    return

        recv_task = asyncio.create_task(receiver())
        t0 = time.perf_counter()
        await stream_pcm(session, PROMPT_AUDIO)
        send_done_at = time.perf_counter()
        send_done.set()
        try:
            await asyncio.wait_for(recv_task, timeout=15)
        except asyncio.TimeoutError:
            recv_task.cancel()
        if first_byte_at is None:
            return None
        return {"total": first_byte_at - t0, "post_input": first_byte_at - send_done_at}


async def bench(c, model: str, api_version: str, n: int) -> list[dict]:
    results = []
    for i in range(n):
        dt = await one_run(c, model, api_version)
        if dt is not None:
            print(f"  run {i + 1}/{n}: total={dt['total']:.3f}s  post_input={dt['post_input']:.3f}s")
            results.append(dt)
        else:
            print(f"  run {i + 1}/{n}: NO RESPONSE")
    return results


def report(name: str, xs: list[dict], n_total: int) -> None:
    if not xs:
        print(f"{name}: 全部 {n_total} 次都沒有收到回應（UNVERIFIED）")
        return
    for key, label in (("post_input", "post_input（跟 TTS 2.44s 同基準）"), ("total", "total（含真實語速輸入時間）")):
        vals = sorted(x[key] for x in xs)
        print(f"{name} [{label}]: n={len(vals)}/{n_total}  min={vals[0]:.3f}s  "
              f"median={statistics.median(vals):.3f}s  max={vals[-1]:.3f}s  "
              f"mean={statistics.mean(vals):.3f}s")


async def main(n: int, which: str) -> None:
    c_beta = client("v1beta")
    if which in ("proactive", "both"):
        print(f"=== {MODEL_PROACTIVE} (v1alpha, 從 audio 送完到第一個輸出音訊 byte) ===")
        c_alpha = client("v1alpha")
        xs = await bench(c_alpha, MODEL_PROACTIVE, "v1alpha", n)
        report(MODEL_PROACTIVE, xs, n)
    if which in ("baseline", "both"):
        print(f"\n=== {MODEL_BASELINE} (v1beta) ===")
        xs = await bench(c_beta, MODEL_BASELINE, "v1beta", n)
        report(MODEL_BASELINE, xs, n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--model", choices=["proactive", "baseline", "both"], default="both")
    args = ap.parse_args()
    asyncio.run(main(args.n, args.model))
