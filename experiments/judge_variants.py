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

以上三個都沒通過（validation-results.md #8）。第二批變體的假設不一樣——不是尺度也不是指令，
是**類型清單少一格**：

- imbalance       type 清單加「發言權失衡」，並定義什麼算、什麼不算。
                  依據（2026-09-05 從既有 5 輪快取重新盤點）：8/31 那場三軸判「要介入」共 66 次，
                  其中 **42 次被 `is_intervention()` 的 type=無 擋掉**；O1 窗口內 12 次「要介入」
                  **12 次全部**是 type=無。那 24 個被擋的點，pros 幾乎全是同一句話——
                  「Alex 連續主導發言／Jax 沒有機會完整表達」。模型看見了、也決定要講，
                  但六個類型（離題／重複／假共識／僵局／事實錯誤／無）沒有一格裝得下「發言權失衡」，
                  只好選「無」，然後被自己的類型閘門滅掉。這不是靈敏度問題，是詞彙問題。
- imbalance_facts imbalance ＋ 程式算的結構訊號（最近 3 分鐘每人發言秒數／句數／最長應聲長度、
                  目前這一輪連續發言多久）。假設：光給名字會讓模型把「某人講得多」也叫失衡，
                  補上結構事實才分得出「主述」與「另一個人已經不在討論裡」。

基準線：`experiments/out/rescore-logistics-guard-<場次>/stability.json`（現行 prompt 5 輪）。

用法：
    PYTHONPATH=src python experiments/judge_variants.py --variants coarse3,explicit,two_stage --rounds 5
    PYTHONPATH=src python experiments/judge_variants.py --variants coarse3 --rounds 1 --limit 3   # 煙霧測試
    PYTHONPATH=src python experiments/judge_variants.py --report-only                            # 只印比較表

採用規則（先寫下來，避免看到數字才決定）：

第一批（coarse3／explicit／two_stage）用的規則：一個變體要**同時**在兩場上，該講窗口的慢路命中不低於基準、
誤報不高於基準、逐點翻面數不高於基準，才算贏。任一場退步就不採用。

第二批（imbalance／imbalance_facts）改用**不對稱**規則，理由寫在前面：docs 已認定
「該講卻不講」比「不該講卻講」嚴重（validation-results.md #6-3 第 3 點），要求誤報零成長等於
禁止任何提升召回的改動。所以這批明說接受一定幅度的誤報上升，但設上限：

1. 目標（8/31）：慢路對 O1／O3／O4 的命中，五輪中至少 3 輪 ≥1 個窗口命中（基準 2/5 輪），
   且 O1 單獨至少 3/5 輪命中（基準 0/5）。
2. 回歸防線（8/29）：A1／A2 兩個閒聊窗口維持每輪 2/2 命中（基準五輪全 2/2），不得下降。
3. 誤報上限：8/31 慢路誤報中位數 ≤ 5（基準中位數 3），8/29 ≤ 4（基準 2）。
   5 是「43 分鐘裡最多錯 5 次」≈ 每 8.6 分鐘一次，已是主席還能被忍受的密度上限。
4. 穩定度：逐點翻面數不得超過基準的 1.5 倍（8/31 ≤ 10、8/29 ≤ 13）。

四條全過才採用。只過 1、2 而踩破 3 的，記錄為「召回有解、代價未定」，不進 production。

## 結果（2026-09-05）

`imbalance_facts` 四條全過，**已併入 production**（`slow_path.py`）；`imbalance` 兩條沒過。
兩者都把 8/31 的 O1 從 0/5 輪拉到 5/5 輪，命中類型正是新加的「發言權失衡」。
差別在 8/29 回歸防線：`imbalance` 補跑到 10 輪之後 A1／A2 各 9/10（`imbalance_facts` 各
10/10），且慢路誤報較低——但那兩次漏接在 n=10 上仍在，不是抽樣雜訊，所以不採用它。

⚠️ **這兩個變體已經是現行 production**，不能再套在現行 `slow_path` 上（會變成套兩次），
`apply()` 會擋下來。要重跑它們得先把 `slow_path.py` 退回 2026-09-05 之前的版本。

順帶修掉的量測缺陷：`score_run.opportunity_recall(path=...)` 原本把 `expect_type: null`
的窗口從 fast 與 slow 兩邊的分母同時排除（O1 正是這種窗口），導致 `slow_opportunity_hits`
永遠看不到 O1——第一次跑完時這張表顯示「兩個變體都沒改善」，差點把結論下反。
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

_ORIG = {k: getattr(slow_path, k) for k in ("TEMPLATE", "KEY_RULES", "decide", "build_prompt")}

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

# ── 第二批：類型清單少一格 ──────────────────────────────────────────────
#
# `slow_path.is_intervention()` 規定 type=無 一律不算介入。這條有實測依據
#（validation-results.md #3、#3b），不動它。動的是清單本身：加一格裝得下
# 「有人被實質排除在討論外」。定義的後半段（什麼**不算**）跟前半段一樣重要——
# 被擋掉的 24 個點裡，模型的 pros 大量引用「佔 73%」這種比例數字，若不明講
# 比例本身不構成失衡，這一格會退化成「誰講得多就唸誰」。

IMBALANCE_TYPE_OLD = '"type": "<離題/重複/假共識/僵局/事實錯誤/無>"'
IMBALANCE_TYPE_NEW = '"type": "<離題/重複/假共識/僵局/事實錯誤/發言權失衡/無>"'

IMBALANCE_BLOCK = """

關於「發言權失衡」這一類（其他類型看的是「說了什麼」，只有這一類看的是「誰還在參與」）：
算的樣子——有人被實質排除在討論之外：他被接走話之後始終沒能把那句講完，
或已經連續幾分鐘只剩「嗯」「對」「OK」這種應聲，討論實際上只剩一個人在推。
不算的樣子——只是某人講得比較多、或這一段本來就由某人主述而其他人在聽。
兩個人的發言時間本來就不會一樣，**佔比數字本身不構成失衡**，不要拿百分比當理由。
要看的是對方還有沒有真的在參與。"""

# 結構訊號：全部由程式從 MeetingState 算，模型不必自己數。
# 只用 `Replay.state_at()` 真的重建得出來的欄位（utterances／speaking／participants），
# 不碰 voice_active／silence_since——那兩個離線重建不出來，見 Replay 的 docstring。
STRUCTURE_WINDOW = 180.0

def _structure_block(st, now: float) -> str:
    lo = now - STRUCTURE_WINDOW
    win = [u for u in st.utterances if u.end >= lo]
    lines = []
    for p in st.participants:
        mine = [u for u in win if u.speaker == p]
        secs = sum(u.end - u.start for u in mine)
        longest = max((len(u.text) for u in mine), default=0)
        lines.append(f"- 最近 3 分鐘 {p}：說了 {secs:.0f} 秒／{len(mine)} 句，"
                     f"最長的一句 {longest} 字")
    switches = sum(1 for a, b in zip(win, win[1:]) if a.speaker != b.speaker)
    lines.append(f"- 最近 3 分鐘發言權易手 {switches} 次")
    who, run = st.current_run_seconds(now)
    lines.append(f"- 目前這一輪：{who} 已連續講 {run / 60:.1f} 分鐘，中間沒有人插話"
                 if who else "- 目前這一輪：沒有人正在連續發言")
    return "\n".join(lines)

def _imbalance_template() -> str:
    t = _ORIG["TEMPLATE"]
    assert t.count(IMBALANCE_TYPE_OLD) == 1
    return t.replace(IMBALANCE_TYPE_OLD, IMBALANCE_TYPE_NEW)

def _facts_template() -> str:
    t = _imbalance_template()
    a = "\n## 最近的對話"
    assert t.count(a) == 1
    return t.replace(a, "\n## 結構訊號（程式量到的，不是你要推測的）\n{structure}\n\n## 最近的對話")

def _facts_build_prompt():
    """`slow_path.build_prompt` 的替身：多算一段結構訊號填進去。

    照抄原版的 stats／transcript 組法（不是呼叫原版再字串拼接——TEMPLATE 已經
    換成帶 {structure} 的版本，原版 format 會缺鍵）。
    """
    def build_prompt(st, now: float, phase: str | None = None) -> str:
        stats = "\n".join(
            f"- {p}：發言 {st.spoke_seconds(p) / 60:.1f} 分鐘（佔 {st.share(p, now):.0%}），"
            f"已 {st.silent_seconds(p, now) / 60:.1f} 分鐘沒發言"
            for p in st.participants)
        transcript = "\n".join(
            f"[{int(u.start) // 60:02d}:{int(u.start) % 60:02d}] {u.speaker}：{u.text}"
            for u in st.recent())
        return slow_path.TEMPLATE.format(
            topic=st.topic, duration=st.duration_min, elapsed=now / 60,
            participants="、".join(st.participants),
            stats=stats, structure=_structure_block(st, now),
            transcript=transcript, rules=slow_path.KEY_RULES,
            phase_block=f"\n{slow_path.PHASE_RULES.format(phase=phase)}\n" if phase else "")
    return build_prompt

VARIANTS = {
    "coarse3":   {"TEMPLATE": _coarse3_template},
    "explicit":  {"KEY_RULES": lambda: _ORIG["KEY_RULES"] + EXPLICIT_BLOCK},
    "two_stage": {"TEMPLATE": _two_stage_template, "decide": lambda: _two_stage_decide},
    "imbalance": {"TEMPLATE": _imbalance_template,
                  "KEY_RULES": lambda: _ORIG["KEY_RULES"] + IMBALANCE_BLOCK},
    "imbalance_facts": {"TEMPLATE": _facts_template,
                        "KEY_RULES": lambda: _ORIG["KEY_RULES"] + IMBALANCE_BLOCK,
                        "build_prompt": _facts_build_prompt},
}

ADOPTED = {"imbalance", "imbalance_facts"}
"""已併入 production 的變體。套在現行 `slow_path` 上會變成套兩次（TEMPLATE 裡已經
有「發言權失衡」與 {structure}），各個 `_*_template()` 的 assert 會先炸掉，但那個
AssertionError 讀不出原因，所以在這裡先擋，訊息說清楚要怎麼做。"""


def apply(name: str) -> None:
    if name in ADOPTED and "發言權失衡" in _ORIG["TEMPLATE"]:
        raise SystemExit(
            f"變體 {name} 已經是現行 production（2026-09-05 併入 slow_path.py），"
            "不能再套一次。要重跑它，先把 slow_path.py 退回併入前的版本。")
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
