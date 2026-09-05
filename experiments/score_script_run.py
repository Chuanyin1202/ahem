#!/usr/bin/env python3
"""把腳本場次的錄影對它自己的期望窗口計分。

劇本場景有真實會議沒有的優勢：**真值是寫出來的，不是標出來的**。劇本說
「Alex 從 70 秒起獨白到 381 秒」，那個窗口就是 ground truth，不需要人工標註。
所以這裡不寫第二套計分器——把劇本的 `windows` 轉成 `labels.json` 的格式，
直接餵 `experiments/score_run.py`（見 docs/specs/2026-09-05-script-harness-design.md 決定五）。

用法：
    PYTHONPATH=src python experiments/score_script_run.py <events.jsonl> [--script <path>]
    PYTHONPATH=src python experiments/score_script_run.py --all      # 掃 meetings/ 裡最新的每個劇本場次
    PYTHONPATH=src python experiments/score_script_run.py --check    # 只驗窗口與門檻是否仍然一致

## `--check` 在檢查什麼

規則型的窗口（發言超時／全場沉默／有人被冷落／議程超時）是從**當時生效的門檻**推出來的。
門檻一改（例如 `--style test` 把 OVERTIME 從 180 縮到 60），觸發時刻就跟著移動，
寫死的窗口就不再對應同一件事——那時候比較兩次的分數是在比不同的東西。

所以這裡在計分之前先乾跑一次快路：用劇本宣告的 `style` 套上門檻，算出每條規則
會在第幾秒觸發，然後確認那個時刻**落在對應的機會窗口裡**。對不上就停下來，
不要拿對不上的窗口去計分。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src")); sys.path.insert(0, str(_HERE))
import score_run  # noqa: E402
from meeting_host import fast_path, style  # noqa: E402
from meeting_host.events import Event  # noqa: E402
from meeting_host.live import load_script  # noqa: E402
from meeting_host.script_source import to_utterances  # noqa: E402
from meeting_host.state import MeetingState  # noqa: E402

SCRIPTS = _HERE.parent / "examples" / "scripts"
MEETINGS = _HERE.parent / "meetings"
_BASE = style.defaults()


def _restore() -> None:
    for k, v in _BASE.items():
        setattr(style._MODULE.get(k, fast_path), k, v)


def labels_for(script: dict, duration: float) -> dict:
    """劇本的 `windows` → `score_run` 吃的 labels。欄位名一致，不做轉換。"""
    return {
        "case_id": f"script:{script['name']}",
        "duration_seconds": duration,
        "participants": script["participants"],
        "topic": script["topic"],
        "windows": script["windows"],
        "notes": script.get("expect", ""),
    }


def rule_fire_times(script: dict) -> list[tuple[float, str, str | None]]:
    """在劇本宣告的門檻檔位下乾跑快路，回傳每條規則會在第幾秒觸發。"""
    _restore()
    style.apply(script.get("style"))
    try:
        us = to_utterances([tuple(r) for r in script["lines"]])
        st = MeetingState(topic=script["topic"], duration_min=script["duration_min"],
                          participants=list(script["participants"]))
        for p in script["participants"]:
            st.joined_at[p] = 0.0
        pending, done, fired, prev, now = list(us), set(), [], None, 0.0
        while now <= us[-1].end + 30:
            for u in pending:
                if u.start <= now < u.end:
                    st.speaking_now(u.speaker, u.start)
            while pending and pending[0].end <= now:
                u = pending.pop(0)
                st.stopped_speaking(u.speaker); st.add(u)
                done.discard(("有人被冷落", u.speaker)); done.discard(("全場沉默", None))
                if prev and prev != u.speaker:
                    done.discard(("發言超時", prev))
                prev = u.speaker
            for t in fast_path.check(st, now, done):
                fired.append((now, t.kind, t.target))
                st.interventions.append(now); done.add((t.kind, t.target))
                if t.kind == "全場沉默":
                    st.note_room_silence_fired()
                break
            now += 5.0
        return fired
    finally:
        _restore()


def check_windows(script: dict) -> list[str]:
    """規則型窗口與當下門檻是否仍然一致。回傳問題清單，空的代表沒問題。"""
    fired = rule_fire_times(script)
    problems = []
    for w in script["windows"]:
        et = w.get("expect_type")
        if w["kind"] != "opportunity" or et not in fast_path.FAST_KINDS:
            continue          # 慢路型的窗口是內容範圍，不由門檻決定
        lo, hi = w["range_seconds"]
        hits = [t for t, k, _ in fired if k == et and lo <= t <= hi]
        if not hits:
            allt = [f"{t:.0f}s" for t, k, _ in fired if k == et] or ["從未觸發"]
            problems.append(
                f"{script['name']}／{w['id']}：期望 {et} 落在 [{lo}, {hi}]，"
                f"但在檔位 {script.get('style')!r} 下乾跑得到 {allt}")
    return problems


def score_one(events_path: Path, script: dict) -> dict:
    raw = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = sorted([Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw], key=lambda e: e.t)
    duration = max((e.t for e in events), default=0.0)
    labels = labels_for(script, duration)
    return score_run.build_report(events, labels, events_path, Path(f"<{script['name']}.windows>"))


def summarize(name: str, rep: dict) -> str:
    m, c = rep["metrics"], rep["intervention_counts"]
    r = m["overall"]["opportunity_recall"]
    hits, total = r.get("hits"), r.get("total")
    return (f"  {name:<17} 命中 {hits}/{total}　"
            f"介入 {c['total_interventions_excl_greeting']}　"
            f"TP {c['tp']}　FP {c['fp_total']}（窗內 {c['fp_in_window']}／窗外 {c['fp_outside_windows']}）")


def latest_run_for(name: str) -> Path | None:
    """meetings/ 裡最新一個屬於這個劇本的事件檔（用議題與參與者比對）。"""
    script = load_script(SCRIPTS / f"{name}.json")
    best = None
    for f in sorted(MEETINGS.glob("*.events.jsonl"), key=lambda p: p.stat().st_mtime):
        try:
            first = next(l for l in f.read_text(encoding="utf-8").splitlines() if '"meeting"' in l)
        except StopIteration:
            continue
        d = json.loads(first)["data"]
        if d.get("topic") == script["topic"] and set(d.get("participants") or []) <= set(script["participants"]):
            # 用逐字稿第一句比對——同一個議題底下有好幾個劇本，光看 topic 分不開。
            # ⚠️ 不能用 `'"utterance"' in line` 過濾：`slow_score` 事件本身就有一個
            # `utterance` 欄位（話術），那樣會撈到沒有 `text` 的行。要解析出 kind。
            first_text = None
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                if e["kind"] == "utterance":
                    first_text = e["data"]["text"]
                    break
            if first_text == script["lines"][0][2]:
                best = f
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events", type=Path, nargs="?")
    ap.add_argument("--script", type=Path, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)

    names = sorted(p.stem for p in SCRIPTS.glob("*.json"))

    if a.check or a.all:
        print("── 窗口與當下門檻的一致性 ──")
        bad = []
        for n in names:
            bad += check_windows(load_script(SCRIPTS / f"{n}.json"))
        print("  全部一致" if not bad else "\n".join(f"  ⚠️ {b}" for b in bad))
        if a.check:
            return 1 if bad else 0
        if bad:
            print("\n窗口與門檻對不上，停在這裡——不要拿對不上的窗口去計分。")
            return 2

    if a.all:
        print("\n── 各劇本最新一次錄影的計分 ──")
        for n in names:
            f = latest_run_for(n)
            if f is None:
                print(f"  {n:<17} （沒有找到錄影）"); continue
            print(summarize(n, score_one(f, load_script(SCRIPTS / f"{n}.json"))))
        return 0

    if not a.events:
        ap.error("要給 events.jsonl，或用 --all／--check")
    script = load_script(a.script) if a.script else None
    if script is None:
        ap.error("單檔計分要用 --script 指定劇本")
    print(summarize(script["name"], score_one(a.events, script)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
