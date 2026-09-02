#!/usr/bin/env python3
"""Q4：30 分鐘會議要付出什麼代價——session resumption 與 context window compression 的實際行為。

測試 1：session resumption
    - session A：開啟並要求 resumption handle，送一個「事實」讓模型記住，關閉連線
    - session B：用 A 拿到的 handle 重新連線，量測重連花多久，並問它是否還記得那個事實

測試 2：context window compression
    - 開一個 trigger_tokens 設很低的 session，跑多輪對話，觀察 session 是否還能正常運作
      （這不足以「證明」真的觸發了 compression，只能證明設定被接受、session 沒有因此壞掉——
      如上限所述，未在报告中誇大這部分的確定性）

用法: python q4_session_lifecycle.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import MODEL_BASELINE, client  # noqa: E402
from google.genai import types  # noqa: E402

MODEL = MODEL_BASELINE
FACT_STATEMENT = "請記住：我叫做陳大文，我最喜歡的顏色是深藍色。等一下我會問你這件事，先跟我確認你記住了就好，不用多說。"
RECALL_QUESTION = "我剛剛跟你說我叫什麼名字？我最喜歡的顏色是什麼？"


async def send_text_and_collect(session, text: str, timeout: float = 20.0) -> tuple[str, str | None, bool]:
    """送一句文字，收集：模型回覆文字、若有的 session_resumption new_handle、是否 resumable。"""
    reply = ""
    new_handle = None
    resumable = False

    async def receiver():
        nonlocal reply, new_handle, resumable
        async for msg in session.receive():
            if msg.session_resumption_update:
                u = msg.session_resumption_update
                if u.new_handle:
                    new_handle = u.new_handle
                resumable = bool(u.resumable)
            sc = msg.server_content
            if sc and sc.output_transcription and sc.output_transcription.text:
                reply += sc.output_transcription.text
            if sc and sc.turn_complete:
                return

    await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]))
    try:
        await asyncio.wait_for(receiver(), timeout=timeout)
    except asyncio.TimeoutError:
        print("  [警告] 等回覆逾時")
    return reply.strip(), new_handle, resumable


async def test_session_resumption() -> None:
    print("=== 測試 1：session resumption ===")
    config_a = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        session_resumption=types.SessionResumptionConfig(),  # 空的 = 跟伺服器要新 handle
    )

    handle = None
    t_connect_a0 = time.perf_counter()
    async with client("v1beta").aio.live.connect(model=MODEL, config=config_a) as session_a:
        t_connect_a1 = time.perf_counter()
        print(f"session A 連線+setup 花費: {t_connect_a1 - t_connect_a0:.2f}s")
        reply, h, resumable = await send_text_and_collect(session_a, FACT_STATEMENT)
        print(f"session A 回覆: {reply!r}")
        if h:
            handle = h
        print(f"拿到 resumption handle: {'有' if handle else '沒有'}  resumable={resumable}")
        # 再送一次確保有拿到最新 handle（伺服器可能在幾輪之後才給）
        if not handle:
            reply2, h2, resumable2 = await send_text_and_collect(session_a, "好，收到。")
            handle = h2 or handle
            print(f"第二輪後 handle: {'有' if handle else '沒有'}")

    if not handle:
        print("UNVERIFIED：整個 session A 期間都沒有收到 session_resumption_update.new_handle，"
              "無法繼續測試重連後是否記得上下文。")
        return

    print("\n關閉 session A，準備用 handle 重新連線...")
    t_close = time.perf_counter()
    config_b = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )
    t_connect_b0 = time.perf_counter()
    async with client("v1beta").aio.live.connect(model=MODEL, config=config_b) as session_b:
        t_connect_b1 = time.perf_counter()
        print(f"session B（resumption）連線+setup 花費: {t_connect_b1 - t_connect_b0:.2f}s"
              f"（距離關閉 session A 共 {t_connect_b1 - t_close:.2f}s）")
        reply_b, _, _ = await send_text_and_collect(session_b, RECALL_QUESTION)
        print(f"session B 回覆: {reply_b!r}")
        remembered = ("陳大文" in reply_b or "大文" in reply_b) and ("藍" in reply_b)
        print(f"是否記得先前事實: {'是' if remembered else '否／不完整（見上面實際回覆自行判斷）'}")


async def test_context_compression() -> None:
    print("\n=== 測試 2：context_window_compression（低 trigger_tokens，觀察 session 是否仍正常）===")
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=1000,
            sliding_window=types.SlidingWindow(),
        ),
    )
    try:
        async with client("v1beta").aio.live.connect(model=MODEL, config=config) as session:
            for i in range(4):
                text = f"這是第 {i + 1} 輪測試訊息，請用一句話簡短回覆確認收到，不用重複我說的內容。"
                reply, _, _ = await send_text_and_collect(session, text, timeout=15)
                print(f"  第 {i + 1} 輪回覆: {reply!r}")
            print("結論：session 在設定低 trigger_tokens 的 context_window_compression 下跑完 4 輪，"
                  "沒有連線錯誤或被拒絕連線。"
                  "UNVERIFIED：這 4 輪對話量是否真的觸發了 compression（沒有伺服器端明確信號可以確認），"
                  "只能證明設定本身合法、且不會讓 session 壞掉。")
    except Exception as e:  # noqa: BLE001
        print(f"UNVERIFIED：context_window_compression 設定導致連線/執行錯誤：{type(e).__name__}: {e}")


async def main() -> None:
    await test_session_resumption()
    await test_context_compression()


if __name__ == "__main__":
    asyncio.run(main())
