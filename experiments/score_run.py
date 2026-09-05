#!/usr/bin/env python3
"""評分程式：`events.jsonl` ＋ `labels.json` → 指標表 ＋ provenance。

提案（`docs/specs/2026-08-28-eval-harness-proposal.md`）第七節第 2
步：「先有裁判，再有選手」——這支程式先落地，之後的 harness／劇本／錄放器都要
餵它算分，這裡的規則是唯一的計分依據。

規則對照提案第五節，逐條在下面函式的 docstring 標明。純讀檔算數，不連線、不
呼叫任何 LLM、不 import 任何跑會議用的模組（只借 `meeting_host.fast_path` 的
`FAST_KINDS` 常數與 `meeting_host.events.Event` 的型別，兩者都是穩定的資料契約）。

用法：
    python experiments/score_run.py <events.jsonl> <labels.json> [--json out.json]
"""
import argparse
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
from meeting_host.events import Event  # noqa: E402
from meeting_host.fast_path import FAST_KINDS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

GREETING_KIND = "問候"  # 跟現有 UI／is_intervention 一致：問候不算一次介入

# ── 型別同義詞正規化（提案第五節：「離題↔偏離主題 先正規化」）───────────
# 只收錄提案點名、且已在標註／模型輸出中實際出現過的同義詞。之後如果再發現
# 新的同義詞，加進這個 dict 就好，不要動比對邏輯（normalize_type 之後的比較
# 永遠是精確字串相等）。
TYPE_SYNONYMS: dict[str, str] = {
    "偏離主題": "離題",
}


def normalize_type(t: str | None) -> str | None:
    if t is None:
        return None
    return TYPE_SYNONYMS.get(t, t)


def na(reason: str) -> dict:
    """算不出來的指標一律長這個形狀：{"value": None, "reason": "..."}。
    禁止用猜測值頂替——找不到資料就是 None，理由寫清楚。"""
    return {"value": None, "reason": f"N/A —— {reason}"}


# ── 讀檔 ─────────────────────────────────────────────────────────────────


def load_events(path: Path) -> list[Event]:
    events: list[Event] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            events.append(Event(kind=raw["kind"], t=raw["t"], data=raw["data"]))
    events.sort(key=lambda e: e.t)
    return events


def load_labels(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 從事件流萃取「介入」與「queued pipeline」─────────────────────────────


def extract_interventions(events: list[Event]) -> list[dict]:
    """只取 `spoken`：`queued` 之後沒真的說出口（failed／dropped）的候選從未
    發生過，不該影響 TP/FP——评分的是「主席实际做了什么」，不是「曾经想做什么」。
    問候不計入介入，跟現有 UI（`is_intervention`／`spectator/index.html`）一致。
    """
    out = []
    for e in events:
        if e.kind != "spoken":
            continue
        ikind = e.data.get("kind")
        if ikind == GREETING_KIND:
            continue
        path = "fast" if ikind in FAST_KINDS else "slow"
        out.append({
            "t": e.t, "kind": ikind, "target": e.data.get("target"),
            "hard": e.data.get("hard"), "path": path,
        })
    out.sort(key=lambda x: x["t"])
    return out


def match_queued_pipeline(events: list[Event]) -> dict:
    """把 `queued` 依 `(kind, target)` FIFO 配對到之後最早出現、尚未配對的
    `spoken`／`failed`／`dropped`。

    為什麼 FIFO 夠用：Chair（`speaker.py`）是兩槽狀態機，同一 `(kind, target)`
    在同一時間最多一個在 pending／playing，先進先出足以還原真實配對，不需要
    比對文字——升級（soft 等超過 15 秒仍未出現停頓）不會另外送一次 `queued`，
    只會讓最終 `spoken` 的 `hard` 從 False 變 True，話術也可能因為
    `escalate_with_current_facts` 重生而跟 queued 當下的文字不同（見
    `speaker.py` Chair.tick 的升級分支）。用 (kind, target) 配對、用
    `queued.hard != terminal.hard` 偵測升級，是唯一在兩邊資料形狀不對稱時
    還站得住腳的辦法。

    問候不走一般排隊路徑（`live.py` 的 --say-hello 問候從未 emit `queued`），
    這裡整段略過，不列入 queued pipeline 統計。
    """
    pending: dict[tuple, list[dict]] = defaultdict(list)
    pairs: list[dict] = []
    unmatched_terminal: list[dict] = []

    for e in events:
        data = e.data
        if data.get("kind") == GREETING_KIND:
            continue
        key = (data.get("kind"), data.get("target"))
        if e.kind == "queued":
            pending[key].append({
                "kind": data.get("kind"), "target": data.get("target"),
                "queued_t": e.t, "queued_hard": data.get("hard"),
                "terminal": None, "terminal_t": None, "terminal_hard": None,
                "reason": None, "escalated": False,
            })
        elif e.kind in ("spoken", "failed", "dropped"):
            bucket = pending.get(key)
            if not bucket:
                unmatched_terminal.append({
                    "kind": data.get("kind"), "target": data.get("target"),
                    "terminal": e.kind, "terminal_t": e.t,
                })
                continue
            rec = bucket.pop(0)
            rec["terminal"] = e.kind
            rec["terminal_t"] = e.t
            rec["terminal_hard"] = data.get("hard")  # 只有 spoken 帶這欄
            rec["reason"] = data.get("reason")
            rec["escalated"] = (
                (e.kind == "spoken" and rec["queued_hard"] is False and data.get("hard") is True)
                or (e.kind == "dropped" and rec["reason"] and "升級" in rec["reason"])
            )
            pairs.append(rec)

    still_pending = [rec for bucket in pending.values() for rec in bucket]
    return {"pairs": pairs, "unmatched_terminal": unmatched_terminal, "still_pending": still_pending}


# ── 窗口比對（提案第五節）───────────────────────────────────────────────


def find_window(t: float, windows: list[dict]) -> dict | None:
    for w in windows:
        lo, hi = w["range_seconds"]
        if lo <= t <= hi:
            return w
    return None


def window_path(w: dict) -> str | None:
    """從 `expect_type` 推斷這個 opportunity 窗口屬於快路還是慢路，
    `expect_type is None`（如 holdout 的窗口 B）表示不限型別，回傳 None。"""
    et = w.get("expect_type")
    if et is None:
        return None
    return "fast" if normalize_type(et) in FAST_KINDS else "slow"


def score_windows(interventions: list[dict], windows: list[dict]) -> dict:
    """一對一窗口匹配（提案第五節）：
    - 每個 `opportunity` 窗口是一次機會，介入依時間配對，一窗口最多一次 TP，
      同窗口第二次起算 FP。
    - `no_intervention` 區間內任何介入都算 FP。
    - 慢路（以及有指定 `expect_type` 的快路）命中要求 `type` 屬於正確類型
      （先過 `normalize_type` 同義詞正規化），型別不符不算 TP，仍算 FP。
    - `scored: false` 的窗口：落在裡面的介入不計分，只記錄在 `excluded_events`。
    - 落在所有窗口之外：算 FP（`fp_outside`）——見本檔案模組 docstring／
      交付報告裡的說明，這是本工單自行裁定的一條規則，提案沒有明說。
    """
    window_report = {
        w["id"]: {
            "id": w["id"], "kind": w["kind"], "range_seconds": w["range_seconds"],
            "expect_type": w.get("expect_type"), "scored": w.get("scored", True),
            "excluded_reason": w.get("excluded_reason"), "why": w.get("why"),
            "hit": None, "fp_events": [],
        }
        for w in windows
    }

    tp_list: list[dict] = []
    fp_in_window: list[dict] = []
    fp_outside: list[dict] = []
    excluded_events: list[dict] = []

    for iv in interventions:
        w = find_window(iv["t"], windows)
        if w is None:
            fp_outside.append({**iv, "fp_reason": "落在所有已標註窗口之外"})
            continue
        wr = window_report[w["id"]]
        if not w.get("scored", True):
            excluded_events.append({**iv, "window_id": w["id"]})
            continue
        if w["kind"] == "no_intervention":
            rec = {**iv, "fp_reason": f"落在 no_intervention 窗口 {w['id']} 內"}
            wr["fp_events"].append(rec)
            fp_in_window.append(rec)
            continue
        # opportunity
        expect = normalize_type(w.get("expect_type"))
        ok = expect is None or normalize_type(iv["kind"]) == expect
        if ok and wr["hit"] is None:
            wr["hit"] = iv
            tp_list.append({**iv, "window_id": w["id"]})
        elif ok:
            rec = {**iv, "fp_reason": f"重複命中：窗口 {w['id']} 已有 TP，type 正確但算第二次起 FP"}
            wr["fp_events"].append(rec)
            fp_in_window.append(rec)
        else:
            rec = {**iv, "fp_reason": f"type 不符：窗口 {w['id']} 要求 {w.get('expect_type')!r}，實際 {iv['kind']!r}"}
            wr["fp_events"].append(rec)
            fp_in_window.append(rec)

    scored_windows = [w for w in windows if w.get("scored", True)]
    excluded_windows = [w for w in windows if not w.get("scored", True)]
    return {
        "window_report": window_report,
        "scored_windows": scored_windows,
        "excluded_windows": excluded_windows,
        "tp": tp_list,
        "fp_in_window": fp_in_window,
        "fp_outside": fp_outside,
        "excluded_events": excluded_events,
    }


# ── 指標 ─────────────────────────────────────────────────────────────────


def _stats(values: list[float]) -> dict:
    if not values:
        return na("這個桶裡沒有任何事件")
    return {
        "value": round(statistics.median(values), 3),
        "n": len(values),
        "mean": round(statistics.mean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def opportunity_recall(scored: dict, path: str | None) -> dict:
    """某條路徑接住了多少個「它有機會處理」的窗口。

    ⚠️ `expect_type: null`（不限型別）的窗口屬於**兩條路徑**，不是不屬於任何一條。
    這裡曾經寫成 `window_path(w) == path`，而 `window_path` 對不限型別的窗口回傳
    `None`——結果那種窗口同時從 `metrics.fast` 與 `metrics.slow` 的分母裡消失，
    只留在 `metrics.overall`。2026-09-05 撞到：8/31 的 O1（標註者刻意不限型別，
    見 labels.json 的 why）正是「該講卻不講」的主要素材，卻從來沒有被
    `metrics.slow.opportunity_recall` 算進去過——judge_variants 的比較表以它當
    主指標，於是一個把 O1 從 0/5 拉到 5/5 的變體，在表上顯示為「沒有改善」。

    正確的口徑：不限型別的窗口進兩邊的分母，但**只有當實際命中它的那次介入
    來自這條路徑時**才算這條路徑的命中（`hit["path"]`，由 `extract_interventions`
    依 `FAST_KINDS` 標好）。有指定 `expect_type` 的窗口行為完全不變。
    """
    opp_windows = [w for w in scored["scored_windows"] if w["kind"] == "opportunity"
                   and (path is None or window_path(w) in (path, None))]
    if not opp_windows:
        return na(f"沒有屬於「{path or '任何'}」路徑、且 scored=true 的 opportunity 窗口")
    hits = 0
    for w in opp_windows:
        hit = scored["window_report"][w["id"]]["hit"]
        if hit is None:
            continue
        if path is None or hit["path"] == path:
            hits += 1
    return {"value": round(hits / len(opp_windows), 3), "hits": hits, "total": len(opp_windows)}


def fp_per_hour(scored: dict, duration_seconds: float, path: str | None) -> dict:
    if duration_seconds <= 0:
        return na("會議時長未知或為 0")
    fps = [f for f in scored["fp_in_window"] + scored["fp_outside"]
           if path is None or f["path"] == path]
    hours = duration_seconds / 3600.0
    return {"value": round(len(fps) / hours, 3), "count": len(fps), "hours": round(hours, 4)}


def first_hit_latency(scored: dict, path: str | None) -> dict:
    latencies = []
    for w in scored["scored_windows"]:
        if w["kind"] != "opportunity":
            continue
        if path is not None and window_path(w) != path:
            continue
        hit = scored["window_report"][w["id"]]["hit"]
        if hit is not None:
            latencies.append(hit["t"] - w["range_seconds"][0])
    return _stats(latencies)


def repeat_hits(scored: dict, path: str | None) -> dict:
    reps = [f for f in scored["fp_in_window"]
            if f["fp_reason"].startswith("重複命中") and (path is None or f["path"] == path)]
    return {"value": len(reps)}


def queued_pipeline_metrics(pipeline: dict) -> dict:
    pairs = pipeline["pairs"]
    resolved = pairs  # terminal 一定已知（pairs 只收有 terminal 的紀錄）
    total_queued = len(pairs) + len(pipeline["still_pending"])
    spoken = [p for p in resolved if p["terminal"] == "spoken"]
    failed = [p for p in resolved if p["terminal"] == "failed"]
    dropped = [p for p in resolved if p["terminal"] == "dropped"]
    success_rate = (
        na("沒有任何 queued 事件解析出結果")
        if not resolved else
        {"value": round(len(spoken) / len(resolved), 3), "spoken": len(spoken), "resolved": len(resolved)}
    )
    latency = _stats([p["terminal_t"] - p["queued_t"] for p in spoken])
    return {
        "total_queued": total_queued,
        "resolved": len(resolved),
        "still_pending_at_log_end": len(pipeline["still_pending"]),
        "unmatched_terminal_events": len(pipeline["unmatched_terminal"]),
        "spoken": len(spoken), "failed": len(failed), "dropped": len(dropped),
        "success_rate": success_rate,
        "spoken_latency_seconds": latency,
    }


def soft_metrics(pipeline: dict) -> dict:
    """`hard=false`（軟插入）的等待時間／升級率／作廢率。"""
    soft = [p for p in pipeline["pairs"] if p["queued_hard"] is False]
    if not soft:
        return {
            "wait_time_seconds": na("本場沒有任何 soft（hard=false）介入請求"),
            "escalation_rate": na("本場沒有任何 soft（hard=false）介入請求"),
            "void_rate": na("本場沒有任何 soft（hard=false）介入請求"),
            "n_soft": 0,
        }
    wait = _stats([p["terminal_t"] - p["queued_t"] for p in soft])
    escalated = sum(1 for p in soft if p["escalated"])
    dropped = sum(1 for p in soft if p["terminal"] == "dropped")
    return {
        "wait_time_seconds": wait,
        "escalation_rate": {"value": round(escalated / len(soft), 3), "escalated": escalated, "n_soft": len(soft)},
        "void_rate": {"value": round(dropped / len(soft), 3), "dropped": dropped, "n_soft": len(soft)},
        "n_soft": len(soft),
    }


def known_false_positive_crossref(labels: dict, scored: dict, tolerance: float = 2.0) -> dict | None:
    """對照 `labels.json` 的 `known_false_positives`：這批本來就標成誤報的
    時間點，這次跑出來的 FP 是不是就是它們——不是重新判定它們對不對。"""
    kfp = labels.get("known_false_positives")
    if not kfp:
        return None
    kind = kfp["kind"]
    all_fp = scored["fp_in_window"] + scored["fp_outside"]
    excluded = scored["excluded_events"]

    rows = []
    for at in kfp["at_seconds"]:
        fp_hit = next((f for f in all_fp if f["kind"] == kind and abs(f["t"] - at) <= tolerance), None)
        excl_hit = next((e for e in excluded if e["kind"] == kind and abs(e["t"] - at) <= tolerance), None)
        if fp_hit is not None:
            status = "算作 FP"
        elif excl_hit is not None:
            status = f"落在 scored=false 窗口 {excl_hit['window_id']}，已排除不計分"
        else:
            status = "找不到對應介入——與 labels.json 不一致，需要人工核對"
        rows.append({"at_seconds": at, "status": status})

    matched_as_fp = sum(1 for r in rows if r["status"] == "算作 FP")
    return {
        "kind": kind, "why": kfp.get("why"), "expected_count": kfp.get("count", len(kfp["at_seconds"])),
        "matched_as_fp": matched_as_fp, "rows": rows,
    }


# ── Provenance ───────────────────────────────────────────────────────────


def get_scorer_git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.STDOUT,
        ).strip()
    except Exception as e:  # noqa: BLE001
        return f"N/A —— git rev-parse 失敗：{type(e).__name__}: {e}"


def build_provenance(labels: dict, events_path: Path, labels_path: Path) -> dict:
    recorded = labels.get("provenance", {})
    return {
        "scored_at_git_sha": get_scorer_git_sha(),
        "case_id": labels.get("case_id") or na("labels.json 缺 case_id"),
        "events_file": str(events_path), "labels_file": str(labels_path),
        "recorded_at_capture_time": {
            "code_sha": recorded.get("code") or na("labels.json 的 provenance 未記錄"),
            "slow_path": recorded.get("slow_path") or na("labels.json 的 provenance 未記錄"),
            "fast_path_constants": recorded.get("fast_path") or na("labels.json 的 provenance 未記錄"),
            "tts": recorded.get("tts") or na("labels.json 的 provenance 未記錄"),
        },
        "not_applicable": {
            "prompt_hash": na("real-holdout 沒有合成 harness 的請求正規化層，這場真實會議沒有 prompt hash 可記"),
            "cache_hit_miss": na("real-holdout 不讀 LLM 回應快取（提案第六節只給 eval-regression）"),
            "run_index": na("real-holdout 是單次真實會議紀錄，沒有『同一劇本第 N 次跑』的概念"),
        },
    }


# ── 組報表 ───────────────────────────────────────────────────────────────


def build_report(events: list[Event], labels: dict, events_path: Path, labels_path: Path) -> dict:
    interventions = extract_interventions(events)
    windows = labels.get("windows", [])
    scored = score_windows(interventions, windows)
    pipeline = match_queued_pipeline(events)

    duration_seconds = labels.get("duration_seconds")
    if duration_seconds is None:
        duration_seconds = max((e.t for e in events), default=0.0)
        duration_source = "N/A —— labels.json 沒有 duration_seconds，改用事件流最後一筆的 t 估計"
    else:
        duration_source = "labels.json.duration_seconds"

    fast_iv = [iv for iv in interventions if iv["path"] == "fast"]
    slow_iv = [iv for iv in interventions if iv["path"] == "slow"]

    counts = {
        "total_interventions_excl_greeting": len(interventions),
        "fast": len(fast_iv), "slow": len(slow_iv),
        "tp": len(scored["tp"]), "fp_in_window": len(scored["fp_in_window"]),
        "fp_outside_windows": len(scored["fp_outside"]),
        "fp_total": len(scored["fp_in_window"]) + len(scored["fp_outside"]),
        "excluded_scored_false": len(scored["excluded_events"]),
    }

    metrics = {"overall": {}, "fast": {}, "slow": {}}
    for path in (None, "fast", "slow"):
        key = path or "overall"
        metrics[key]["opportunity_recall"] = opportunity_recall(scored, path)
        metrics[key]["fp_per_meeting_hour"] = fp_per_hour(scored, duration_seconds, path)
        metrics[key]["first_hit_latency_seconds"] = first_hit_latency(scored, path)
        metrics[key]["repeat_hits"] = repeat_hits(scored, path)

    unclassified_opp_windows = [
        w["id"] for w in scored["scored_windows"] if w["kind"] == "opportunity" and window_path(w) is None
    ]

    report = {
        "provenance": build_provenance(labels, events_path, labels_path),
        "meeting": {
            "case_id": labels.get("case_id"), "duration_seconds": duration_seconds,
            "duration_source": duration_source, "participants": labels.get("participants"),
        },
        "windows": {
            "all": [
                {"id": w["id"], "kind": w["kind"], "range_seconds": w["range_seconds"],
                 "expect_type": w.get("expect_type"), "scored": w.get("scored", True),
                 "excluded_reason": w.get("excluded_reason")}
                for w in windows
            ],
            "scored_count": len(scored["scored_windows"]),
            "excluded_count": len(scored["excluded_windows"]),
            "unclassified_opportunity_windows": unclassified_opp_windows,
            "detail": {wid: {k: v for k, v in wr.items()} for wid, wr in scored["window_report"].items()},
        },
        "intervention_counts": counts,
        "outside_window_interventions": scored["fp_outside"],
        "excluded_events": scored["excluded_events"],
        "known_false_positive_crossref": known_false_positive_crossref(labels, scored),
        "metrics": metrics,
        "queued_pipeline": queued_pipeline_metrics(pipeline),
        "soft_intervention_metrics": soft_metrics(pipeline),
        "unavailable_metrics": {
            "pcm_duplicate_frames": na(
                "需要合成 harness 的 FakePlayer frame ledger（逐幀序號記錄）；"
                "真實會議的 events.jsonl 不含 PCM 幀層級資訊，算不出來"
            ),
            "state_invariant_violations": na(
                "需要合成 harness 逐 tick 檢查 pending/playing 狀態機不變量；"
                "events.jsonl 只有離散的 emit 快照，重建不出每個 tick 的完整內部狀態"
            ),
        },
        "not_in_scope": {
            "post_intervention_behavior_metrics": "提案第二階段（點名後是否開口、超時介入後原講者是否停），本工單不做",
        },
    }
    return report


# ── 輸出 ─────────────────────────────────────────────────────────────────


def _fmt_metric(m: dict) -> str:
    # na() 的 reason 已經是「N/A —— ...」的完整句子，這裡不再疊一層 N/A（）。
    if m.get("value") is None:
        return m.get("reason", "N/A（無理由）")
    return str(m["value"])


def print_report(report: dict) -> None:
    p = report["provenance"]
    m = report["meeting"]
    print("=" * 72)
    print(f"case_id: {m['case_id']}    duration: {m['duration_seconds']}s"
          f"（{report['meeting']['duration_source']}）")
    print(f"scored_at_git_sha: {p['scored_at_git_sha']}")
    print(f"recorded code_sha: {p['recorded_at_capture_time']['code_sha']}")
    print("=" * 72)

    print("\n-- 窗口 --")
    for w in report["windows"]["all"]:
        tag = "計分" if w["scored"] else f"排除（{w['excluded_reason']}）"
        print(f"  [{w['id']:>3}] {w['kind']:<15} {w['range_seconds']} "
              f"expect_type={w['expect_type']!r}  {tag}")

    print("\n-- 介入計數（已排除問候）--")
    c = report["intervention_counts"]
    print(f"  total={c['total_interventions_excl_greeting']}  fast={c['fast']}  slow={c['slow']}")
    print(f"  TP={c['tp']}  FP(窗口內)={c['fp_in_window']}  FP(窗口外)={c['fp_outside_windows']}"
          f"  FP合計={c['fp_total']}  排除(scored=false)={c['excluded_scored_false']}")

    print("\n-- 指標（overall / fast / slow）--")
    for name in ("opportunity_recall", "fp_per_meeting_hour", "first_hit_latency_seconds", "repeat_hits"):
        row = " / ".join(_fmt_metric(report["metrics"][k][name]) for k in ("overall", "fast", "slow"))
        print(f"  {name:<28} {row}")

    print("\n-- queued → spoken --")
    q = report["queued_pipeline"]
    print(f"  total_queued={q['total_queued']}  resolved={q['resolved']}"
          f"  spoken={q['spoken']}  failed={q['failed']}  dropped={q['dropped']}"
          f"  still_pending={q['still_pending_at_log_end']}  unmatched_terminal={q['unmatched_terminal_events']}")
    print(f"  success_rate={_fmt_metric(q['success_rate'])}"
          f"  latency_seconds(median)={_fmt_metric(q['spoken_latency_seconds'])}")

    print("\n-- soft（hard=false）--")
    s = report["soft_intervention_metrics"]
    print(f"  n_soft={s['n_soft']}"
          f"  wait_time(median)={_fmt_metric(s['wait_time_seconds'])}"
          f"  escalation_rate={_fmt_metric(s['escalation_rate'])}"
          f"  void_rate={_fmt_metric(s['void_rate'])}")

    kfp = report["known_false_positive_crossref"]
    if kfp:
        print(f"\n-- known_false_positives 對照（kind={kfp['kind']}，共 {kfp['expected_count']} 筆）--")
        print(f"  這次跑出來算作 FP：{kfp['matched_as_fp']} / {kfp['expected_count']}")
        for r in kfp["rows"]:
            print(f"    t≈{r['at_seconds']:>4}s  {r['status']}")

    print("\n-- 算不出來的指標 --")
    for name, val in report["unavailable_metrics"].items():
        print(f"  {name}: {_fmt_metric(val)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("events", type=Path, help="events.jsonl")
    ap.add_argument("labels", type=Path, help="labels.json")
    ap.add_argument("--json", type=Path, default=None, help="另存機器可讀的 JSON 報表")
    args = ap.parse_args(argv)

    events = load_events(args.events)
    labels = load_labels(args.labels)
    report = build_report(events, labels, args.events, args.labels)
    print_report(report)

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n(已寫入 {args.json})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
