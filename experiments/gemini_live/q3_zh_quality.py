#!/usr/bin/env python3
"""Q3：繁體中文品質。

輸入面：把 zh_quality.pcm（含簡繁字形差異字＋台灣慣用詞）送進
        input_audio_transcription，看轉錄結果是簡體還是繁體、正確率如何。
輸出面：叫模型講一段繁體中文，把原始輸出音訊存成 wav（給人耳確認），
        同時用 output_audio_transcription 做客觀回讀比對——
        我們沒辦法「聽」，所以主觀音質一律標記「需要人耳確認」。

用法: python q3_zh_quality.py
輸出: experiments/gemini_live/out/q3_output_<n>.wav
"""
import asyncio
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import AUDIO_DIR, MODEL_BASELINE, OUT_DIR, OUTPUT_RATE, client, stream_pcm  # noqa: E402
from google.genai import types  # noqa: E402
from scripts import ZH_QUALITY_TEXT  # noqa: E402

MODEL = MODEL_BASELINE  # 這題跟 proactive_audio 無關，用主線候選模型測
OUTPUT_TEXTS = [
    "今天的會議討論了下一季的產品規劃，讓大家更清楚接下來的方向。",
    "請問軟體專案的網路連線測試結果如何？滑鼠點擊的反應速度是否正常？",
]


async def test_input_transcription() -> None:
    print("=== 輸入面：input_audio_transcription 轉錄品質 ===")
    print(f"原始文本: {ZH_QUALITY_TEXT}")
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction="使用者只是在測試語音輸入，不用回應任何內容，保持沉默即可。",
        input_audio_transcription={},
    )
    async with client("v1beta").aio.live.connect(model=MODEL, config=config) as session:
        text_acc = ""

        async def receiver():
            # session.receive() 在 turn_complete 後就結束該次 async generator（SDK 行為，
            # 見 q1_proactive_audio.py 的註解）。這段音檔中間有逗號停頓，VAD 可能把它切成
            # 好幾個 turn，所以要包外層迴圈持續重新收下一個 turn，否則後半句轉錄會被漏掉。
            nonlocal text_acc
            while True:
                got_any = False
                async for msg in session.receive():
                    got_any = True
                    sc = msg.server_content
                    if sc and sc.input_transcription and sc.input_transcription.text:
                        text_acc += sc.input_transcription.text
                if not got_any:
                    return

        recv_task = asyncio.create_task(receiver())
        await stream_pcm(session, AUDIO_DIR / "zh_quality.pcm")
        await asyncio.sleep(5)
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass
    print(f"轉錄結果: {text_acc.strip()!r}")
    has_simplified = any(c in text_acc for c in "这让软软软团团沟这们")
    print(f"是否含常見簡體字判定字元: {'是' if has_simplified else '否（看起來是繁體）'}")


async def test_output_quality(text: str, idx: int) -> None:
    print(f"\n=== 輸出面 #{idx}：叫模型講「{text}」===")
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=f"請你原封不動、用自然的語氣，直接朗讀以下這句繁體中文，不要加任何其他內容：「{text}」",
        output_audio_transcription={},
    )
    async with client("v1beta").aio.live.connect(model=MODEL, config=config) as session:
        pcm_chunks: list[bytes] = []
        out_text = ""

        async def receiver():
            nonlocal out_text
            async for msg in session.receive():
                sc = msg.server_content
                if sc and sc.model_turn and sc.model_turn.parts:
                    for p in sc.model_turn.parts:
                        if p.inline_data and p.inline_data.data:
                            pcm_chunks.append(p.inline_data.data)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    out_text += sc.output_transcription.text
                if sc and sc.turn_complete:
                    return

        # 用文字觸發即可，不需要真的講話
        await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text="請開始朗讀。")]))
        try:
            await asyncio.wait_for(receiver(), timeout=20)
        except asyncio.TimeoutError:
            pass

    OUT_DIR.mkdir(exist_ok=True)
    wav_path = OUT_DIR / f"q3_output_{idx}.wav"
    pcm = b"".join(pcm_chunks)
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUTPUT_RATE)
        w.writeframes(pcm)
    print(f"輸出音訊: {wav_path}（{len(pcm) / (OUTPUT_RATE * 2):.2f}s）［主觀音質需要人耳確認］")
    print(f"output_audio_transcription 回讀: {out_text.strip()!r}")
    print(f"與預期文字是否逐字相符: {'是' if out_text.strip() == text else '否（有差異，見上）'}")


async def main() -> None:
    await test_input_transcription()
    for i, t in enumerate(OUTPUT_TEXTS, 1):
        await test_output_quality(t, i)


if __name__ == "__main__":
    asyncio.run(main())
