#!/usr/bin/env python3
"""Q1：proactive_audio 到底能不能「一直聽但大部分時間不出聲」。

用法:
    python q1_proactive_audio.py                 # 完整跑一輪（4 情境 x REPEATS 次，proactive_audio）
    python q1_proactive_audio.py --baseline       # 跑 baseline：gemini-3.1-flash-live-preview，不開 proactive_audio

每次試驗都開全新 session（避免會話歷史互相污染），量測：
- 是否有任何 model_turn（audio 或 text part）在音檔送完後的靜默觀察窗內出現
- 若有，output_audio_transcription 抓到的文字是什麼
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    AUDIO_DIR, MODEL_BASELINE, MODEL_PROACTIVE, client, stream_pcm,
)
from google.genai import types  # noqa: E402

SILENCE_WAIT = 10.0  # 音檔送完後觀察窗（秒）
REPEATS = 3

SYSTEM_INSTRUCTION = (
    "你正在旁聽一場會議，扮演會議主席。你大部分時間都應該保持完全沉默，"
    "只有在下列情況才開口：討論陷入僵局（同樣的爭執反覆循環、沒有進展）、"
    "話題明顯偏離會議主旨太久、或有人長時間被忽略。"
    "如果目前聽到的內容是正常、有進展的討論，你什麼都不用說，也不要用語音回應。"
    "如果你判斷需要介入，用一句簡短的繁體中文提醒大家拉回正題或做出決定。"
)

TRIALS = ["quiet_1", "quiet_2", "speak_deadlock_1", "speak_offtopic_1"]


async def run_trial(c, model: str, name: str, use_proactive: bool) -> dict:
    config_kwargs = dict(
        response_modalities=["AUDIO"],
        system_instruction=SYSTEM_INSTRUCTION,
        output_audio_transcription={},
        input_audio_transcription={},
    )
    if use_proactive:
        config_kwargs["proactivity"] = types.ProactivityConfig(proactive_audio=True)
    config = types.LiveConnectConfig(**config_kwargs)

    events = []
    t0 = time.perf_counter()
    async with c.aio.live.connect(model=model, config=config) as session:
        async def receiver():
            # ⚠️ session.receive() 是「per-turn」的 async generator——伺服器送出
            # turn_complete=True 那則訊息後，這個 for 迴圈就會自然結束（見 SDK 原始碼
            # google.genai.live.AsyncSession.receive）。我們的音檔有好幾秒長、中間有
            # 自然停頓，VAD 完全可能在音檔講完前就先判斷「這個 turn 講完了」並送一次
            # turn_complete（即使是空回應）。如果只包一層 for，後面真正的介入判斷
            # （下一個 turn）就會被漏掉，導致「沒開口」的統計是假的。
            # 所以外層要再包一次迴圈，讓 receive() 可以重新開始收下一個 turn。
            while True:
                got_any = False
                async for msg in session.receive():
                    got_any = True
                    events.append((time.perf_counter() - t0, msg))
                if not got_any:
                    return  # 連線已關閉，沒有更多訊息可收

        recv_task = asyncio.create_task(receiver())
        send_secs = await stream_pcm(session, AUDIO_DIR / f"{name}.pcm")
        # 送完音檔後，再等一段靜默觀察窗，看模型會不會自己開口
        await asyncio.sleep(SILENCE_WAIT)
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    spoke = False
    output_text = ""
    input_text = ""
    n_turns = 0
    for t, msg in events:
        sc = msg.server_content
        if sc is None:
            continue
        if sc.turn_complete:
            n_turns += 1
        if sc.input_transcription and sc.input_transcription.text:
            input_text += sc.input_transcription.text
        if sc.model_turn and sc.model_turn.parts:
            for p in sc.model_turn.parts:
                if p.inline_data and p.inline_data.data:
                    spoke = True
                if p.text:
                    spoke = True
        if sc.output_transcription and sc.output_transcription.text:
            output_text += sc.output_transcription.text

    return {
        "name": name,
        "spoke": spoke or bool(output_text.strip()),
        "output_text": output_text.strip(),
        "input_text_echo": input_text.strip(),
        "n_events": len(events),
        "n_turns": n_turns,  # 有幾個 turn_complete——用來看音檔中途是否被 VAD 切成多個 turn
        "send_secs": round(send_secs, 2),
    }


async def main(baseline: bool) -> None:
    # proactivity 欄位只有 v1alpha 吃得進去（v1beta 會被伺服器拒絕，見 common.py 註解）
    c = client(api_version="v1beta" if baseline else "v1alpha")
    model = MODEL_BASELINE if baseline else MODEL_PROACTIVE
    use_proactive = not baseline
    print(f"model={model} proactive_audio={use_proactive} repeats={REPEATS}\n")

    results = []
    for name in TRIALS:
        for i in range(REPEATS):
            print(f"--- {name} run {i + 1}/{REPEATS} ---")
            try:
                r = await run_trial(c, model, name, use_proactive)
            except Exception as e:  # noqa: BLE001 -- spike 腳本，錯誤直接印出來看
                print(f"  ERROR: {type(e).__name__}: {e}")
                r = {"name": name, "spoke": None, "error": str(e)}
            print(f"  spoke={r.get('spoke')} n_turns={r.get('n_turns')} output_text={r.get('output_text', '')!r}")
            results.append(r)

    print("\n=== 統計 ===")
    for name in TRIALS:
        rs = [r for r in results if r["name"] == name]
        spoke_count = sum(1 for r in rs if r.get("spoke") is True)
        err_count = sum(1 for r in rs if r.get("spoke") is None)
        expect = "該開口" if name.startswith("speak") else "不該開口"
        print(f"{name} ({expect}): 開口 {spoke_count}/{len(rs)} 次，錯誤 {err_count} 次")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.baseline))
