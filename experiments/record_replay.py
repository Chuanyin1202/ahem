#!/usr/bin/env python3
"""把任何一場開過的會議錄成 MP4。

不是 demo 專用——**每一場會議（真實或腳本）都會寫出 `events.jsonl`**，而觀戰畫面
本來就有回放模式（`python -m meeting_host.spectator --replay`）。這支把兩者接起來，
再用無頭瀏覽器把畫面錄下來：任何一場開過的會，事後都能重播成影片。

    PYTHONPATH=src python experiments/record_replay.py --latest demo
    PYTHONPATH=src python experiments/record_replay.py meetings/meeting-1788.events.jsonl
    PYTHONPATH=src python experiments/record_replay.py --latest demo --speed 4   # 四倍速快覽

用途不只做 demo：
- 客戶或評審看不到現場 → 事後把那一場重播成影片
- 主席某次判斷有爭議 → 錄下那段畫面，比貼逐字稿好討論
- 同一份事件檔可以錄很多次，內容一模一樣（錄壞了重錄，不用重開會）

## 為什麼走「回放 ＋ 無頭瀏覽器」而不是螢幕錄影

螢幕錄影要有人開著視窗、要授權螢幕擷取、而且錄的是那台機器當下的畫面——
部署在 Pi5 上的會議根本錄不到。回放模式沒有這些限制：事件檔在哪台機器都能重播，
無頭瀏覽器不需要顯示器，所以這支在 headless 機器上也跑得起來。

## 已知限制

- 影片沒有聲音。主席的 TTS 不經過觀戰畫面（那是 Discord／本機喇叭那條路），
  所以要聲音得另外配——時間軸用 `experiments/dub_script.py` 導出。
- 回放速度不是嚴格等時：`spectator --replay` 依事件時間戳推進，`--speed` 只是
  把等待時間等比縮短。快轉倍率太高時瀏覽器的動畫（逐字浮現、淡入）跟不上。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src")); sys.path.insert(0, str(_HERE))

REPO = _HERE.parent
# runbook 要求投影用 1440×900 以上；版面最小寬度 1200px，低於它會變成橫向捲動
WIDTH, HEIGHT = 1440, 900
SETTLE_TAIL_SECONDS = 6.0
"""回放結束後多錄幾秒。最後一批事件（會議記錄、統計）要有時間畫上去，
畫面也要停在完整狀態上，剪接的人才有一格可以定住。"""


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def events_for(name: str) -> Path:
    from score_script_run import latest_run_for
    p = latest_run_for(name)
    if p is None:
        raise SystemExit(f"找不到劇本 {name} 的錄影（meetings/ 裡沒有對得上的事件檔）")
    return p


def start_spectator(events: Path, port: int, speed: float) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": "src"}
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "meeting_host.spectator",
         "--replay", str(events), "--port", str(port), "--speed", str(speed),
         "--public-read"],                       # 錄影不需要權杖，網址乾淨
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):                          # 等它印出網址才算起來
        line = proc.stdout.readline()
        if not line:
            break
        if "觀戰 UI" in line:
            return proc
    proc.kill()
    raise SystemExit("觀戰畫面沒有起來")


def record(url: str, seconds: float, out_dir: Path) -> Path:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT},
                                   record_video_dir=str(out_dir),
                                   record_video_size={"width": WIDTH, "height": HEIGHT})
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        time.sleep(seconds)
        ctx.close()                               # close 才會把影片寫完整
        browser.close()
    webm = next(iter(sorted(out_dir.glob("*.webm"))), None)
    if webm is None:
        raise SystemExit("playwright 沒有寫出影片")
    return webm


def to_mp4(webm: Path, out: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("找不到 ffmpeg，無法轉成 mp4（webm 檔留在 " + str(webm) + "）")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(webm),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p",                   # 沒有這個，很多播放器與剪輯軟體吃不下
         "-movflags", "+faststart", str(out)],
        check=True, capture_output=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events", type=Path, nargs="?", help="會議的 events.jsonl")
    ap.add_argument("--latest", metavar="劇本名", help="用該劇本最新一場錄影")
    ap.add_argument("--speed", type=float, default=1.0, help="回放倍速（預設 1＝等時）")
    ap.add_argument("--out", type=Path, default=None, help="輸出的 mp4（預設放 meetings/）")
    ap.add_argument("--keep-webm", action="store_true")
    a = ap.parse_args(argv)

    events = events_for(a.latest) if a.latest else a.events
    if events is None:
        ap.error("要給 events.jsonl，或用 --latest <劇本名>")

    import json
    last = max(json.loads(l)["t"] for l in events.read_text(encoding="utf-8").splitlines() if l.strip())
    seconds = last / a.speed + SETTLE_TAIL_SECONDS
    out = a.out or (REPO / "meetings" / f"{events.stem.replace('.events','')}.mp4")
    tmp = REPO / "meetings" / "_video_tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    port = free_port()
    print(f"事件檔：{events.name}（{last:.0f} 秒）")
    print(f"回放：localhost:{port}　倍速 {a.speed:g}　預計錄 {seconds / 60:.1f} 分鐘")
    proc = start_spectator(events, port, a.speed)
    try:
        webm = record(f"http://localhost:{port}", seconds, tmp)
        print(f"轉檔中…（{webm.stat().st_size / 1e6:.1f} MB webm）")
        to_mp4(webm, out)
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
    if not a.keep_webm:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"完成：{out}（{out.stat().st_size / 1e6:.1f} MB）")
    print("⚠️ 影片沒有聲音——主席的 TTS 不經過觀戰畫面。"
          "配音時間軸用 experiments/dub_script.py 導出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
