#!/usr/bin/env python3
"""第二階段：拿指定的 `reasoning_effort` 跑完整的多輪重評，並額外記下每一次呼叫的往返延遲。

這支只做兩件 `rescore_slow_path.py` 沒做的事，其餘一律轉交它：

1. 呼叫前設 `slow_path.EFFORT = <值>`。`slow_path.score()` 讀的是模組全域，
   Python 在呼叫時才解析，所以在這裡設就夠了——**production code 一個字都沒動**，
   `src/meeting_host/slow_path.py` 的 `EFFORT` 仍然是 `"none"`。
2. 包一層 `slow_path.score`，量每一次呼叫的 wall-clock 往返延遲。原本的重評流程
   只保留事件檔裡「當時」的延遲（effort=none 那場錄下來的 2.5–4.9 秒），
   量不到新 effort 的延遲——而延遲正是這次要付的代價。

延遲逐次寫檔（`latency.<effort>.json`），中斷不白費。

用法：
    python experiments/effort_rescore.py --effort low --rounds 5 \
        experiments/holdout/<場次>/meeting.events.jsonl \
        --labels experiments/holdout/<場次>/labels.json \
        --out experiments/out/effort-sweep/effort-low
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

import rescore_slow_path as R  # noqa: E402
from meeting_host import slow_path  # noqa: E402

TICK = R.TICK  # 5.0，watch_slow 的 sleep；週期 ≈ TICK + 往返


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--effort", required=True,
                    help="reasoning_effort 的值（先用 effort_probe.py 確認 API 收）")
    ap.add_argument("--out", type=Path, required=True, help="輸出目錄（不要指到已入庫的基準目錄）")
    args, rest = ap.parse_known_args(argv)

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    lat_path = out_dir / f"latency.{args.effort}.json"
    lat_blob: dict = {
        "model": slow_path.MODEL, "effort": args.effort,
        "tick_seconds": TICK,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "calls": [],
    }
    if lat_path.is_file():  # 續跑：接在既有紀錄後面，不覆蓋
        lat_blob = json.loads(lat_path.read_text(encoding="utf-8"))

    real_score = slow_path.score

    def timed_score(st, now, phase=None):
        t0 = time.perf_counter()
        ok, err = False, "中斷"  # finally 一定讀得到，即使被 KeyboardInterrupt 打斷
        try:
            r = real_score(st, now, phase)
            ok, err = True, None
            return r
        except Exception as exc:  # noqa: BLE001  失敗也要記——重試會多花時間
            ok, err = False, f"{type(exc).__name__}"
            raise
        finally:
            lat_blob["calls"].append({
                "t_score": now, "ok": ok,
                "latency_seconds": round(time.perf_counter() - t0, 3),
                "error": err,
            })
            lat_path.write_text(json.dumps(lat_blob, ensure_ascii=False, indent=2),
                                encoding="utf-8")

    slow_path.EFFORT = args.effort
    slow_path.score = timed_score  # type: ignore[assignment]
    print(f"effort={slow_path.EFFORT}（production 的 src/meeting_host/slow_path.py 未改動）")
    try:
        rc = R.main([*rest, "--out", str(out_dir)])
    finally:
        slow_path.score = real_score  # type: ignore[assignment]
        slow_path.EFFORT = "none"

    ok = [c["latency_seconds"] for c in lat_blob["calls"] if c["ok"]]
    if ok:
        lat_blob["summary"] = {
            "n_ok": len(ok), "n_failed": sum(1 for c in lat_blob["calls"] if not c["ok"]),
            "min": round(min(ok), 3),
            "p50": round(statistics.median(ok), 3),
            "p90": round(sorted(ok)[int(len(ok) * 0.9)], 3) if len(ok) > 1 else round(ok[0], 3),
            "max": round(max(ok), 3),
            "mean": round(statistics.mean(ok), 3),
            "slow_cycle_p50": round(TICK + statistics.median(ok), 3),
            "slow_cycle_max": round(TICK + max(ok), 3),
        }
        lat_path.write_text(json.dumps(lat_blob, ensure_ascii=False, indent=2), encoding="utf-8")
        s = lat_blob["summary"]
        print(f"\n往返延遲（n={s['n_ok']}，失敗 {s['n_failed']}）："
              f"min={s['min']}s p50={s['p50']}s p90={s['p90']}s max={s['max']}s")
        print(f"推算慢路週期 ≈ TICK({TICK}) + 往返：p50={s['slow_cycle_p50']}s "
              f"max={s['slow_cycle_max']}s")
        print(f"延遲原始紀錄：{lat_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
