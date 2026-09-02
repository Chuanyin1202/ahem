#!/usr/bin/env python3
"""把一場已錄會議餵給階段偵測器，看它會不會亂跳。

用法：
    PYTHONPATH=src python experiments/phase_replay.py experiments/holdout/<案例>/meeting.events.jsonl \\
        [--expect 發散期] [--tick 60]

每 `--tick` 秒用 `rescore_slow_path.Replay.state_at()` 重建當時的會議狀態（跟重評工具
同一套重建，`--verify` 過的才可信），把當時慢路已判出的 type 一併餵給 `phase.judge()`，
再交給 `PhaseDetector` 做遲滯。逐筆列出讀數與是否切換。

`--expect` 給定時，任何切換到其他階段都算失敗（exit 1）。
`--truth` 給 labels.json（含 `phase_truth`）時，改對真值計分：每個真值窗口內的讀數多數、
偵測器在窗口內有沒有切到該階段、切換延遲（窗口起點到切換的秒數）、以及真值以外的誤切。兩場既有真實錄音全程是發散期，
這就是目前唯一能做的驗證——反面的：偵測器不得在全程發散的會議上判成別的。
正面驗證（它會不會在該切的時候切）要等一場真的走完三階段的錄音。
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src")); sys.path.insert(0, str(_HERE))
import rescore_slow_path as R  # noqa: E402
from meeting_host import phase as ph  # noqa: E402
from meeting_host.events import Event  # noqa: E402


def score_against_truth(readings: list[dict], switches: list[tuple[float, str]],
                        truth: list[dict], initial: str) -> dict:
    """純函式，供測試。truth：[{"phase", "range_seconds": [lo, hi]}, ...]，依時間排序。

    每個窗口回報：讀數多數（majority）、偵測器在窗口結束時的階段（detected_at_end）、
    切換延遲（latency_s：窗口起點到第一次切到該階段的秒數；沒切到為 None）。
    誤切（false_switches）：切到的階段跟當時所在窗口的真值不同。
    """
    def phase_at(t: float) -> str:
        cur = initial
        for st_, ph_ in switches:
            if st_ <= t:
                cur = ph_
        return cur
    def truth_at(t: float) -> str | None:
        for w in truth:
            lo, hi = w["range_seconds"]
            if lo <= t <= hi:
                return w["phase"]
        return None
    windows = []
    for w in truth:
        lo, hi = w["range_seconds"]
        inside = [r for r in readings if lo <= r["t"] <= hi]
        from collections import Counter
        maj = Counter(r["phase"] for r in inside).most_common(1)[0][0] if inside else None
        first = next((st_ for st_, ph_ in switches if ph_ == w["phase"] and lo <= st_ <= hi), None)
        already = phase_at(lo) == w["phase"]
        windows.append({"phase": w["phase"], "range_seconds": [lo, hi], "readings": len(inside),
                        "majority": maj, "detected_at_end": phase_at(hi),
                        "hit": phase_at(hi) == w["phase"],
                        "latency_s": (0.0 if already else (first - lo if first is not None else None))})
    false_switches = [(st_, ph_) for st_, ph_ in switches if truth_at(st_) not in (None, ph_)]
    return {"windows": windows, "hits": sum(w["hit"] for w in windows), "n_windows": len(windows),
            "false_switches": false_switches}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events", type=Path)
    ap.add_argument("--expect", default=None, choices=list(ph.PHASES))
    ap.add_argument("--tick", type=float, default=ph.PHASE_TICK_SECONDS)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--truth", type=Path, default=None, help="labels.json，讀其中的 phase_truth")
    a = ap.parse_args(argv)
    R.load_api_key()
    raw = [json.loads(l) for l in a.events.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = sorted([Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw], key=lambda e: e.t)
    replay = R.Replay(events)
    slow = [e for e in events if e.kind == "slow_score"]
    end = events[-1].t
    det = ph.PhaseDetector(current=replay.phase)
    rows, switches = [], []
    t = a.tick
    while t <= end:
        st = replay.state_at(t)
        why = ph.judgeable(st, t)
        if why:
            print(f"  t={t:6.0f}s  不判：{why}")
        else:
            types = [e.data.get("type") or "" for e in slow if e.t <= t]
            try:
                r = ph.judge(st, t, det.current, types)
            except Exception as exc:  # noqa: BLE001
                print(f"  t={t:6.0f}s  判斷失敗：{type(exc).__name__}: {exc}"); t += a.tick; continue
            sw = det.observe(r, t)
            rows.append({"t": t, **r, "switched_to": sw})
            mark = f"  → 切換到 {sw}" if sw else ""
            print(f"  t={t:6.0f}s  {r['phase']}  信心 {r['confidence']:.2f}  {r['reason'][:48]}{mark}")
            if sw:
                switches.append((t, sw))
        t += a.tick
    print(f"\n讀數 {len(rows)} 筆；讀數分佈：{ {p: sum(1 for x in rows if x['phase']==p) for p in ph.PHASES} }")
    print(f"切換 {len(switches)} 次：{switches or '無'}")
    if a.out:
        a.out.write_text(json.dumps({"events": str(a.events), "tick": a.tick, "expect": a.expect,
                                     "readings": rows, "switches": switches}, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.truth:
        truth = json.loads(a.truth.read_text(encoding="utf-8")).get("phase_truth") or []
        if not truth:
            print("\n--truth 的 labels.json 沒有 phase_truth"); return 2
        rep = score_against_truth(rows, switches, truth, replay.phase)
        print("\n對真值計分：")
        for w in rep["windows"]:
            lat = "—" if w["latency_s"] is None else f"{w['latency_s']:.0f}s"
            print(f"  {w['phase']} [{w['range_seconds'][0]:.0f}, {w['range_seconds'][1]:.0f}]  讀數 {w['readings']}  多數 {w['majority']}  "
                  f"窗口結束時 {w['detected_at_end']}  {'命中' if w['hit'] else '未命中'}  延遲 {lat}")
        print(f"  命中 {rep['hits']}/{rep['n_windows']}；誤切 {len(rep['false_switches'])}：{rep['false_switches'] or '無'}")
        if a.out:
            blob = json.loads(a.out.read_text(encoding="utf-8")); blob["truth_score"] = rep
            a.out.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if rep["hits"] == rep["n_windows"] and not rep["false_switches"] else 1
    if a.expect and any(sw != a.expect for _, sw in switches):
        print(f"\n✗ 期望全程 {a.expect}，但發生了切換"); return 1
    if a.expect:
        print(f"\n✓ 全程維持 {a.expect}（遲滯後未切換）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
