#!/usr/bin/env python3
"""第一階段探測：`reasoning_effort` 這個模型到底吃哪些值，以及各值的往返延遲。

只做兩件事，不做結論：
1. 對同一個代表性評分點，每個候選 effort 送 N 次（預設 3）呼叫
2. 記錄「API 收不收」與「每次呼叫的往返延遲」

被拒絕時記下伺服器回的原始訊息（`HTTPError` 的 response body），不做任何解釋
——猜測拒絕原因是第二階段選值出錯的來源。

不改 production code：只在呼叫前設 `slow_path.EFFORT`（模組全域是呼叫時解析），
影響範圍侷限在這支腳本的行程內。

用法：
    python experiments/effort_probe.py experiments/holdout/<場次>/meeting.events.jsonl \
        --efforts none,minimal,low,medium,high --calls 3 --point 17 \
        --out experiments/out/effort-sweep/probe.json
"""
import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

import rescore_slow_path as R  # noqa: E402  重建／反推評分時刻的唯一依據
from meeting_host import slow_path  # noqa: E402


def describe_error(exc: BaseException) -> str:
    """把例外轉成可貼回報告的字串。HTTPError 的 body 才是 API 真正說的話。"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = "<讀不到 response body>"
        return f"HTTP {exc.code} {exc.reason} | body={body.strip()}"
    return f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events", type=Path)
    ap.add_argument("--efforts", default="none,minimal,low,medium,high",
                    help="逗號分隔的候選值")
    ap.add_argument("--calls", type=int, default=3, help="每個候選值呼叫幾次")
    ap.add_argument("--point", type=int, default=None,
                    help="用第幾個慢路評分點（1-based），預設取中間那個")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    R.load_api_key()
    raw = [json.loads(l) for l in args.events.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = [R.Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw]
    events.sort(key=lambda e: e.t)
    replay = R.Replay(events)
    slow_events = [e for e in events if e.kind == "slow_score"]
    solved = R.solve_score_times(replay, slow_events, drift=R.tick_drift(events))

    idx = (args.point - 1) if args.point else len(solved) // 2
    s = solved[idx]
    st = replay.state_at(s["t_score"])
    prompt_chars = len(slow_path.build_prompt(st, s["t_score"], replay.phase))
    print(f"代表點：第 {idx + 1}/{len(solved)} 點  t_score={s['t_score']:.1f}s  "
          f"utterances={s['n_utterances']}  prompt {prompt_chars} 字元")
    print(f"模型：{slow_path.MODEL}    候選 effort：{args.efforts}    每值 {args.calls} 次\n")

    original_effort = slow_path.EFFORT
    report: dict = {
        "model": slow_path.MODEL,
        "events": str(args.events),
        "point_index_1based": idx + 1,
        "t_score": s["t_score"],
        "n_utterances": s["n_utterances"],
        "prompt_chars": prompt_chars,
        "calls_per_effort": args.calls,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": [],
    }
    try:
        for effort in [e.strip() for e in args.efforts.split(",") if e.strip()]:
            slow_path.EFFORT = effort
            rec: dict = {"effort": effort, "calls": []}
            for i in range(args.calls):
                t0 = time.perf_counter()
                try:
                    r = slow_path.score(st, s["t_score"], replay.phase)
                    dt = time.perf_counter() - t0
                    call = {
                        "ok": True, "latency_seconds": round(dt, 3),
                        "positive": r.get("positive"), "negative": r.get("negative"),
                        "none": r.get("none"), "type": r.get("type"),
                        "verdict": r.get("verdict"),
                        "margin": R.margin_of(r),
                        "is_intervention": slow_path.is_intervention(r),
                    }
                    print(f"  [{effort:>8}] #{i + 1}  {dt:6.2f}s  "
                          f"p/n/none={r.get('positive')}/{r.get('negative')}/{r.get('none')}  "
                          f"margin={R.margin_of(r)}  {r.get('verdict')}/{r.get('type')}")
                except Exception as exc:  # noqa: BLE001  被拒也是結果
                    dt = time.perf_counter() - t0
                    msg = describe_error(exc)
                    call = {"ok": False, "latency_seconds": round(dt, 3), "error": msg}
                    print(f"  [{effort:>8}] #{i + 1}  {dt:6.2f}s  拒絕：{msg}")
                rec["calls"].append(call)
                (args.out.parent).mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(report | {"results": report["results"] + [rec]},
                                               ensure_ascii=False, indent=2), encoding="utf-8")
            oks = [c for c in rec["calls"] if c["ok"]]
            rec["accepted"] = len(oks) == len(rec["calls"])
            rec["n_ok"] = len(oks)
            rec["latencies"] = [c["latency_seconds"] for c in oks]
            report["results"].append(rec)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print()
    finally:
        slow_path.EFFORT = original_effort

    print("=" * 88)
    for rec in report["results"]:
        lats = rec["latencies"]
        if lats:
            print(f"  {rec['effort']:>8}  接受 {rec['n_ok']}/{len(rec['calls'])}  "
                  f"延遲 min={min(lats):.2f}s med={sorted(lats)[len(lats) // 2]:.2f}s "
                  f"max={max(lats):.2f}s")
        else:
            print(f"  {rec['effort']:>8}  接受 0/{len(rec['calls'])}  （全被拒）")
    print(f"\n輸出：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
