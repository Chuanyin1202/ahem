#!/usr/bin/env python3
"""慢路「判斷」prompt 的變體實驗：判準不穩（同批點 5 輪 admissible 1–5、該講的窗口大多漏掉）
是不是尺度或指令的問題。**不動 production**——每個變體以覆寫 `slow_path` 模組屬性的方式套用，
跑完還原；重用 `rescore_slow_path` 的多輪與穩定度計算，輸出與基準線同一種 `stability.json`。

變體：
- coarse3   三軸 1–5 改 1–3。假設：差一分就翻面是尺度太細，粗一點會穩。
- explicit  判斷原則後面加一段「具體可介入情況」（從人工標註時的判準寫成文字）。
            假設：模型不是看不到，是沒被告知什麼算「該講」。
- two_stage 先答「此刻有沒有任何值得介入的事」（true/false），有才評分；needs=false 直接不介入。
            假設：把「要不要」跟「多強」分開，能減少三軸互相拉扯造成的翻面。

基準線：`experiments/out/rescore-logistics-guard-<場次>/stability.json`（現行 prompt 5 輪）。

用法：
    PYTHONPATH=src python experiments/judge_variants.py --variants coarse3,explicit,two_stage --rounds 5
    PYTHONPATH=src python experiments/judge_variants.py --variants coarse3 --rounds 1 --limit 3   # 煙霧測試
    PYTHONPATH=src python experiments/judge_variants.py --report-only                            # 只印比較表

採用規則（先寫下來，避免看到數字才決定）：一個變體要**同時**在兩場上，該講窗口的慢路命中不低於基準、
誤報不高於基準、逐點翻面數不高於基準，才算贏。任一場退步就不採用。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src")); sys.path.insert(0, str(_HERE))
import rescore_slow_path as R  # noqa: E402
from meeting_host import slow_path  # noqa: E402
from meeting_host.events import Event  # noqa: E402

CASES = ["2026-08-29-two-person", "2026-08-31-two-person"]
OUT = _HERE / "out" / "judge-variants"
BASELINE = _HERE / "out" / "rescore-logistics-guard-{case}"

_ORIG = {k: getattr(slow_path, k) for k in ("TEMPLATE", "KEY_RULES", "decide")}

EXPLICIT_BLOCK = """
以下是「該介入」的具體情況（人工標註真實會議時用的判準），符合就要給出高於「不介入」的分數並選最貼近的類型：
- 某人切斷別人的話之後連續獨白超過 3 分鐘，其他人只有「嗯」「對」這類應聲 → 依內容選 離題 或 僵局
- 同一個論點第三次以上重申、沒有新資訊 → 重複
- 兩個人各自重申立場兩輪以上、沒有人提出新資訊或讓步 → 僵局
- 話題離開議題本身超過 1 分鐘（閒聊、與議題無關的往事）→ 離題
- 有人說「大家都同意」或直接往下走，但有人明顯還沒表態 → 假共識
以下**不算**該介入：會議雜務（調設備、找檔案、確認聽不聽得到）；發散期內在議題範圍裡舉例、繞遠路；
剛介入過還不到 30 秒；針對主席或工具的抱怨。"""

def _coarse3_template() -> str:
    t = _ORIG["TEMPLATE"]
    assert t.count("<1-5，") == 3, t.count("<1-5，")
    t = t.replace("<1-5，", "<1-3，")
    return t.replace("然後才評分。", "然後才評分。分數只有三級：1＝沒有、2＝有一點、3＝明確。")

def _two_stage_template() -> str:
    t = _ORIG["TEMPLATE"]
    a = '{{\n  "pros": ["理由1", "理由2"],'
    assert t.count(a) == 1
    t = t.replace(a, '{{\n  "needs": <true 或 false：此刻有沒有任何值得主席介入的事。false 就不用細評，其餘欄位照填即可>,\n  "pros": ["理由1", "理由2"],')
    return t.replace("先列出兩個「現在該介入」的理由", "先判斷此刻有沒有任何值得介入的事（needs），再列出兩個「現在該介入」的理由")

def _two_stage_decide(r: dict) -> str:
    if not r.get("needs"):
        return "不介入"
    return _ORIG["decide"](r)

VARIANTS = {
    "coarse3":   {"TEMPLATE": _coarse3_template},
    "explicit":  {"KEY_RULES": lambda: _ORIG["KEY_RULES"] + EXPLICIT_BLOCK},
    "two_stage": {"TEMPLATE": _two_stage_template, "decide": lambda: _two_stage_decide},
}

def apply(name: str) -> None:
    for k, make in VARIANTS[name].items():
        setattr(slow_path, k, make())

def restore() -> None:
    for k, v in _ORIG.items():
        setattr(slow_path, k, v)

def run_case(variant: str, case: str, rounds: int, limit: int | None, retries: int) -> Path:
    events_path = _HERE / "holdout" / case / "meeting.events.jsonl"
    labels_path = _HERE / "holdout" / case / "labels.json"
    raw = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = sorted([Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw], key=lambda e: e.t)
    replay = R.Replay(events)
    slow = [e for e in events if e.kind == "slow_score"]
    solved = R.solve_score_times(replay, slow, drift=R.tick_drift(events))
    todo = slow[:limit] if limit else slow
    out_dir = OUT / case / variant; out_dir.mkdir(parents=True, exist_ok=True)
    apply(variant)
    try:
        blob = R.collect_rounds(replay, todo, solved[:len(todo)], rounds=rounds, cache=out_dir / R.ROUNDS_CACHE,
                                retries=retries, report_only=False, refresh=False)
        blob["source_events"] = str(events_path); blob["variant"] = variant
        rep = R.build_stability(replay, raw, blob, labels_path, out_dir)
        (out_dir / R.ROUNDS_CACHE).write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "stability.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        restore()
    return out_dir

def summarize(stab: dict) -> dict:
    adm = [r["admissible"] for r in stab["per_round_totals"]]
    swing = sum(1 for p in stab["per_point"] if not p.get("stable_is_intervention"))
    hits, fps = [], []
    for rw in stab["per_round_windows"]:
        v = rw["variants"].get("slow_prompt_only", {})
        hits.append(v.get("slow_opportunity_hits"))
        c = v.get("counts", {})
        # 慢路自己的誤報：慢路介入數減去慢路命中（同窗口第二次起算誤報，已含在內）。
        # 不用 counts 的 fp_*——那些含錄到的快路誤報（8/29 那 11 次發言超時），跟變體無關。
        fps.append((c.get("slow") or 0) - (v.get("slow_opportunity_hits") or 0))
    return {"admissible": adm, "swing_points": swing, "n_points": stab["n_points"], "slow_hits": hits, "fp": fps}

def report(variants: list[str]) -> None:
    for case in CASES:
        print(f"\n████ {case} ████")
        rows = []
        b = BASELINE.with_name(BASELINE.name.format(case=case)) / "stability.json"
        if b.is_file():
            rows.append(("baseline", summarize(json.loads(b.read_text(encoding="utf-8")))))
        for v in variants:
            s = OUT / case / v / "stability.json"
            if s.is_file():
                rows.append((v, summarize(json.loads(s.read_text(encoding="utf-8")))))
        print(f"  {'變體':<10} {'admissible 各輪':<22} {'翻面點':>8} {'慢路命中各輪':<18} {'慢路誤報各輪'}")
        for name, s in rows:
            print(f"  {name:<10} {str(s['admissible']):<22} {s['swing_points']:>4}/{s['n_points']:<3} {str(s['slow_hits']):<18} {s['fp']}")

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="coarse3,explicit,two_stage")
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args(argv)
    variants = [v for v in a.variants.split(",") if v]
    for v in variants:
        assert v in VARIANTS, f"未知變體 {v}"
    if not a.report_only:
        R.load_api_key()
        for v in variants:
            for case in a.cases.split(","):
                print(f"\n=== 變體 {v} × {case}（{a.rounds} 輪）===")
                run_case(v, case, a.rounds, a.limit, a.retries)
    report(variants)
    return 0

if __name__ == "__main__":
    sys.exit(main())
