#!/usr/bin/env python3
"""把一支完整的會議錄影剪成精華片段，帶緩慢推近。

會議錄影是等時的（`record_replay.py`），一場 5 分鐘的會議就是 5 分鐘的影片——
中間有大量「什麼都沒發生」的時間。評選影片有 2 分鐘上限，所以需要挑段落、
接起來，並且在主席開口的那一刻把畫面推近，讓觀眾知道要看哪裡。

    PYTHONPATH=src python experiments/make_highlight.py --spec highlight.json --out reel.mp4
    PYTHONPATH=src python experiments/make_highlight.py --latest demo --auto   # 從事件檔自動挑段落

## spec 格式

```json
{
  "source": "meetings/ahem-demo-v2.mp4",
  "segments": [
    {"start": 0,  "end": 8,  "zoom": "none",     "note": "開場"},
    {"start": 45, "end": 68, "zoom": "chair",    "note": "離題 → 主席拉回"},
    {"start": 162,"end": 172,"zoom": "thinking", "note": "忍住"}
  ]
}
```

`zoom` 三選一：
- `none`     不推近，整個畫面
- `chair`    推向左欄逐字稿（主席說的話出現在那裡）
- `thinking` 推向右欄「AI 即時觀察／主席的思考」

## 為什麼用 zoompan 而不是 crop

第一版想用時間變化的 `crop` 再 `scale` 回原尺寸，行不通：**`crop` 的 `w`／`h`
只在 filter 初始化時求值一次**，運算式裡放 `t` 會直接報
`Error when evaluating the expression`。只有 `x`／`y` 會每格重算——也就是說
`crop` 做得到平移，做不到推近。

改用 `zoompan`，它就是為這件事設計的：`zoom` 這個變數帶得到上一格的值，
所以可以每格遞增。影片來源要加 `d=1`（一格進、一格出），否則它會把每一格
當成靜態圖重複輸出 `d` 次，長度會爆掉。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src")); sys.path.insert(0, str(_HERE))

W, H = 1440, 900
FADE = 0.35          # 每段頭尾的淡入淡出，讓硬切不刺眼
ZOOM_END = 0.72      # 推到最後保留原畫面的幾成（越小推越近）

# 推近目標（左上角座標與尺寸，維持 16:10）。數值對應 1440×900 的觀戰畫面版面：
# 左欄逐字稿約 0–1060px，右欄面板約 1080–1440px。
# 倍率不要開太大：右欄本身只有 340px 寬，硬推到 3× 會把文字切在畫面外，
# 觀眾看到的是半句話。實測 1.4–1.8× 已經足夠把視線帶過去，字也還讀得完整。
TARGETS = {
    "none":     None,
    "chair":    (40, 240, 1000, 625),      # 左欄中下段，主席的介入出現在這裡（1.44×）
    "thinking": (640, 60, 800, 500),       # 右欄「AI 即時觀察」與心聲（1.80×）
    "minutes":  (640, 0, 800, 500),        # 右欄「會議產出（預覽）」（1.80×）
}


FPS = 30


def seg_filter(zoom: str, dur: float) -> str:
    """單一段落的 filter chain：推近 → 頭尾淡入淡出。"""
    t = TARGETS.get(zoom)
    if t is None:
        vf = f"scale={W}:{H}"
    else:
        tx, ty, tw, th = t
        zmax = W / tw                       # 目標區域佔畫面寬度的倒數 = 放大倍率
        frames = max(1, int(dur * FPS))
        step = (zmax - 1.0) / frames        # 每格遞增，跑滿整段
        # zoompan 的 x/y 是可視區域的**左上角**，範圍 0..(iw-iw/zoom)。所以錨點要用
        # 「目標左上角佔可移動範圍的比例」，不是「目標中心佔全畫面的比例」——後者會
        # 讓偏離畫面中心的目標整個位移掉（實測右欄被推到只剩左半邊在畫面內）。
        fx = tx / (W - tw) if W > tw else 0.0
        fy = ty / (H - th) if H > th else 0.0
        # 用輸出幀號 `on` 直接算倍率，不要用 `zoom+step` 這種累加寫法：`zoom` 讀的是
        # 上一格的值，但在 `d=1`（一格進一格出）底下它每格都被重置回 1，推近永遠不會
        # 發生（2026-09-05 實測：24 秒的段落跑到最後一格仍是原尺寸）。
        vf = (f"zoompan=z='min(1+{step:.8f}*on,{zmax:.4f})'"
              f":x='(iw-iw/zoom)*{fx:.4f}':y='(ih-ih/zoom)*{fy:.4f}'"
              f":d=1:s={W}x{H}:fps={FPS}")
    # `fps` 要放在 zoompan 之前：zoompan 的 `fps=` 只是宣告輸出幀率，不會補幀，
    # 拿 25fps 的來源去餵它，出來的時間軸會被壓成 25/30（實測：一段 24 秒的畫面
    # 只剩 20 秒的內容，尾端整段對不上）。先補到 30fps 再推近就對得上了。
    return (f"fps={FPS},{vf},fade=t=in:st=0:d={FADE},"
            f"fade=t=out:st={max(0.0, dur - FADE):.3f}:d={FADE},setsar=1")


def cut(source: Path, seg: dict, out: Path) -> float:
    start, end = float(seg["start"]), float(seg["end"])
    dur = end - start
    # -ss 必須放在 -i 之前。放在之後是輸出端 seek：解碼整支來源、跑完整條 filter
    # chain，最後才丟掉前面的幀——於是 `fade=t=out:st=dur-0.35` 是從**來源**第 0 秒
    # 起算的，段落還沒開始畫面就黑掉了（2026-09-05 實測：zoompan 段每段只有 45KB
    # 的純黑，而且 274s／318s 兩段因為時間軸被壓縮而落在來源尾端之外，直接產出空檔）。
    # 放在 -i 之前是輸入端 seek，filter chain 拿到的是一條從 0 開始的段落，
    # 且自 ffmpeg 2.1 起輸入端 seek 就是精確到幀的，不會只切在關鍵影格上。
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-i", str(source), "-t", f"{dur}",
         "-vf", seg_filter(seg.get("zoom", "none"), dur),
         "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", "30", str(out)],
        check=True, capture_output=True)
    return dur


def build(spec: dict, out: Path) -> None:
    source = Path(spec["source"])
    if not source.exists():
        raise SystemExit(f"找不到來源影片 {source}")
    tmp = Path(tempfile.mkdtemp(prefix="highlight-"))
    try:
        parts, total = [], 0.0
        for i, seg in enumerate(spec["segments"]):
            p = tmp / f"{i:02d}.mp4"
            d = cut(source, seg, p)
            total += d
            parts.append(p)
            print(f"  [{i + 1}/{len(spec['segments'])}] {seg['start']:>6.1f}–{seg['end']:<6.1f}"
                  f" {d:>5.1f}s  zoom={seg.get('zoom', 'none'):<9} {seg.get('note', '')}")
        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        # 重新編碼而不是 `-c copy`：段落之間的時間基準（tbn/tbc）不一定一致，
        # 串流複製會讓 concat 丟掉時間戳對不上的片段。2026-09-05 實測：七段
        # 共 105 秒，用 `-c copy` 接出來只有 71 秒，而且中段整片空白。
        # 重編一次成本很低（片子只有一兩分鐘），換來的是長度一定對。
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
             "-r", str(FPS), "-fps_mode", "cfr", "-movflags", "+faststart", str(out)],
            check=True, capture_output=True)
        print(f"\n完成：{out}　總長 {total:.1f} 秒（{total / 60:.1f} 分）"
              f"　{out.stat().st_size / 1e6:.1f} MB")
        if total > 120:
            print(f"⚠️ 超過評選影片的 2:00 上限 {total - 120:.0f} 秒——要再砍段落或縮短。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def auto_spec(name: str, source: Path) -> dict:
    """從事件檔自動挑段落：每次主席開口前後各留一段，加上開場與結尾產出。

    偏移量：錄影是瀏覽器載入之後才開始，比回放起點晚一點點（實測約 2 秒）。
    """
    from score_script_run import latest_run_for
    ev_path = latest_run_for(name)
    if ev_path is None:
        raise SystemExit(f"找不到劇本 {name} 的事件檔")
    events = [json.loads(l) for l in ev_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    offset = 2.0
    segs = [{"start": 0, "end": 8, "zoom": "none", "note": "開場：議題與四位與會者"}]
    for e in events:
        if e["kind"] == "spoken" and e["data"].get("kind") != "問候":
            t = e["t"] + offset
            segs.append({"start": max(0, t - 16), "end": t + 12, "zoom": "chair",
                         "note": f"{e['data']['kind']}：{e['data']['text'][:26]}…"})
    last = max(e["t"] for e in events) + offset
    segs.append({"start": max(0, last - 12), "end": last, "zoom": "minutes", "note": "會議產出"})
    return {"source": str(source), "segments": segs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", type=Path)
    ap.add_argument("--latest", metavar="劇本名")
    ap.add_argument("--source", type=Path, default=Path("meetings/ahem-demo-v2.mp4"))
    ap.add_argument("--auto", action="store_true", help="從事件檔自動挑段落")
    ap.add_argument("--out", type=Path, default=Path("meetings/ahem-highlight.mp4"))
    a = ap.parse_args(argv)

    if a.spec:
        spec = json.loads(a.spec.read_text(encoding="utf-8"))
    elif a.latest and a.auto:
        spec = auto_spec(a.latest, a.source)
    else:
        ap.error("要給 --spec，或 --latest <劇本名> --auto")
    build(spec, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
