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

## 為什麼用 crop 而不是 zoompan

`zoompan` 是為靜態圖片設計的，套在影片上每一格會重新取樣，畫面會抖。這裡改用
時間變化的 `crop` 再 `scale` 回原尺寸——等效於推鏡，而且每一格都是原始像素的
單純裁切，沒有抖動。推近目標保持 16:10（與來源同比例），避免變形。
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
TARGETS = {
    "none":     None,
    "chair":    (40, 240, 1000, 625),      # 左欄中下段，主席的介入出現在這裡
    "thinking": (940, 380, 480, 300),      # 右欄「AI 即時觀察」與心聲
    "minutes":  (1060, 20, 380, 238),      # 右上「會議產出（預覽）」
}


def seg_filter(zoom: str, dur: float) -> str:
    """單一段落的 filter chain：推近 → 縮回輸出尺寸 → 頭尾淡入淡出。"""
    t = TARGETS.get(zoom)
    if t is None:
        vf = f"scale={W}:{H}"
    else:
        tx, ty, tw, th = t
        # 線性內插：t=0 是整張畫面，t=dur 是目標區域
        cw = f"'{W}+({tw}-{W})*min(1,t/{dur:.3f})'"
        ch = f"'{H}+({th}-{H})*min(1,t/{dur:.3f})'"
        cx = f"'({tx})*min(1,t/{dur:.3f})'"
        cy = f"'({ty})*min(1,t/{dur:.3f})'"
        vf = f"crop=w={cw}:h={ch}:x={cx}:y={cy},scale={W}:{H}"
    return (f"{vf},fade=t=in:st=0:d={FADE},"
            f"fade=t=out:st={max(0.0, dur - FADE):.3f}:d={FADE},setsar=1")


def cut(source: Path, seg: dict, out: Path) -> float:
    start, end = float(seg["start"]), float(seg["end"])
    dur = end - start
    # -ss 放在 -i 之前是快速搜尋（關鍵影格），放在之後是精確搜尋。這裡要精確，
    # 因為段落起點是照事件時間挑的，差一秒就會切掉主席那一句。
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-ss", f"{start}", "-t", f"{dur}",
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
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart", str(out)],
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
