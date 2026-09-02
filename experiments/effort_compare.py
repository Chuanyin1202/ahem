#!/usr/bin/env python3
"""把幾個 effort 的多輪結果排成同一張表，跟 effort=none 的基準直接對照。

所有數字都從既有檔案讀出來，這裡不重算任何指標、不呼叫 LLM：
  - 逐輪介入數／穩定點數／margin 分佈 ← `stability.json`（`rescore_slow_path.py` 產）
  - 窗格命中／FP／首次命中延遲       ← `rounds/rNN/score.*.json`（`score_run.py` 產）
  - 往返延遲                          ← `latency.<effort>.json`（`effort_rescore.py` 產）

用法：
    python experiments/effort_compare.py \
        none=experiments/out/effort-sweep/baseline-none \
        low=experiments/out/effort-sweep/effort-low \
        high=experiments/out/effort-sweep/effort-high
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

TICK = 5.0
VARIANT = "slow_only_t15fixed"  # 慢路單獨看的變體（快路 T15 誤報已假設修掉）


def load(dirpath: Path) -> dict:
    st = json.loads((dirpath / "stability.json").read_text(encoding="utf-8"))
    rows = []
    for r in st["per_round_totals"]:
        rd = dirpath / "rounds" / f"r{r['round']:02d}"
        rec = {"round": r["round"], "is_intervention": r["is_intervention"],
               "admissible": r["admissible"], "errors": r["errors"]}
        for name in (VARIANT, "slow_prompt_only"):
            f = rd / f"score.{name}.json"
            if not f.is_file():
                continue
            rep = json.loads(f.read_text(encoding="utf-8"))
            m = rep["metrics"]["slow"]
            c = rep["intervention_counts"]
            rec[name] = {
                "hits": m["opportunity_recall"].get("hits"),
                "total": m["opportunity_recall"].get("total"),
                "first_hit_median": m["first_hit_latency_seconds"].get("value"),
                "first_hit_min": m["first_hit_latency_seconds"].get("min"),
                "first_hit_max": m["first_hit_latency_seconds"].get("max"),
                "fp_in_window": c["fp_in_window"],
                "fp_outside": c["fp_outside_windows"],
                "slow": c["slow"],
            }
        rows.append(rec)
    lat = None
    for f in dirpath.glob("latency.*.json"):
        lat = json.loads(f.read_text(encoding="utf-8"))
    return {"stability": st, "rounds": rows, "latency": lat}


def report(name: str, d: dict) -> None:
    st, rows, lat = d["stability"], d["rounds"], d["latency"]
    pp = st["per_point"]
    n = st["n_points"]
    R = st["n_rounds"]
    bar = "─" * 92
    print(f"\n{bar}\n■ effort={st['effort']}  model={st['model']}  {n} 點 × {R} 輪  (dir 標籤 {name})\n{bar}")

    print(f"  1) 逐輪 is_intervention : {[r['is_intervention'] for r in rows]}")
    print(f"     逐輪 admissible      : {[r['admissible'] for r in rows]}")
    print(f"     逐輪失敗點           : {[r['errors'] for r in rows]}")

    s_iv = sum(1 for p in pp if p["stable_is_intervention"])
    s_vd = sum(1 for p in pp if p["stable_verdict"])
    s_ty = sum(1 for p in pp if p["stable_type"])
    print(f"  2) 穩定點數 /{n}        : is_intervention {s_iv}  verdict {s_vd}  type {s_ty}"
          f"   （翻面 is_intervention {n - s_iv}）")

    near = sum(1 for p in pp if p["rounds_within_one_point"] > 0)
    always = sum(1 for p in pp if p["rounds_within_one_point"] == p["n_rounds"] - p["n_error"]
                 and p["n_rounds"] - p["n_error"] > 0)
    allm = [v for p in pp for v in (p.get("margin") or {}).get("values", []) if v is not None]
    dist: dict = {}
    for v in allm:
        dist[v] = dist.get(v, 0) + 1
    print(f"  3) margin ∈ {{0,1}}       : 至少一輪 {near}/{n}   每一輪都是 {always}/{n}")
    print(f"     全部 {len(allm)} 個 margin 值分佈："
          f"{ {k: dist[k] for k in sorted(dist)} }   中位數 {statistics.median(allm):g}")

    print(f"  4) 窗格（score_run.py，變體 {VARIANT}）：")
    for r in rows:
        v = r.get(VARIANT)
        if not v:
            continue
        print(f"     r{r['round']}  opportunity 命中 {v['hits']}/{v['total']}"
              f"   FP(窗內) {v['fp_in_window']}   FP(窗外) {v['fp_outside']}"
              f"   慢路介入 {v['slow']}")

    if lat and lat.get("summary"):
        s = lat["summary"]
        print(f"  5) 往返延遲（n={s['n_ok']}，失敗 {s['n_failed']}）："
              f"min {s['min']}s  p50 {s['p50']}s  p90 {s['p90']}s  max {s['max']}s")
        print(f"     推算慢路週期 ≈ {TICK} + 往返：p50 {s['slow_cycle_p50']}s  "
              f"max {s['slow_cycle_max']}s")
    else:
        print("  5) 往返延遲：這一份沒有 latency.*.json（基準是既有入庫資料，沒量新延遲）")

    fh = [r[VARIANT]["first_hit_median"] for r in rows if r.get(VARIANT)
          and r[VARIANT]["first_hit_median"] is not None]
    if fh:
        print(f"  6) 首次命中延遲（{VARIANT}，每輪 2 個窗格的中位數）：{fh}")
        print(f"     5 輪中位數 {statistics.median(fh):.1f}s"
              f"（原始 emit_t，未計入 effort 造成的延遲增量）")
        if lat and lat.get("summary"):
            # 基準場 effort=none 實際錄下來的往返中位數（34 點，範圍 2.51–4.88s），
            # 由 rescored.rounds.json 的 points_meta[].latency 算出。
            base = 3.311
            add = lat["summary"]["p50"] - base
            print(f"     一階修正（每個評分點的 emit 都晚 {add:+.2f}s）："
                  f"{statistics.median(fh) + add:.1f}s（不含 tick 網格漂移，見報告限制）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="+", help="標籤=目錄")
    args = ap.parse_args(argv)
    for spec in args.specs:
        name, _, p = spec.partition("=")
        d = Path(p)
        if not (d / "stability.json").is_file():
            print(f"跳過 {name}：{d}/stability.json 不存在", file=sys.stderr)
            continue
        report(name, load(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
