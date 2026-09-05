"""T18：`experiments/score_run.py` 評分程式。

涵蓋交付單列的 13 條驗收標準（逐條在各測試 docstring 標號對照）：
1.  對 holdout 案例跑得出完整報表，數字與 labels.json 標註一致
2.  scored:false 的窗口不影響任何分數，但出現在報表裡並標明排除原因
3.  no_intervention 區間內的介入被算成 FP
4.  同一 opportunity 窗口內第二次以後的介入算 FP，不是第二次 TP
5.  慢路命中要求類型正確；同義詞正規化有作用（自造合成資料，holdout 慢路開口 0 次驗不到）
6.  快路與慢路的統計分開呈現
7.  問候不計入介入
8.  算不出來的指標輸出 N/A 並附原因，沒有任何估算值
9.  Provenance 完整輸出，缺的欄位標 N/A 並註明原因
10. --json 輸出可被 json.load 讀回，欄位與表格一致
11. 落在窗口外的介入，處理方式明確且可辨識
12. （全專案測試數 > 303，由 CI／pytest 收尾時整體驗證，不是單一測試函式）
13. 本檔案本身：每條驗收都有對應測試

除了 holdout 案例本身（唯一允許使用的真實資料）之外，全部用手工組的合成
events／labels，不依賴任何其他真實會議資料。
"""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "experiments"))
import score_run  # noqa: E402
from meeting_host.events import Event  # noqa: E402

HOLDOUT_DIR = Path(__file__).parent.parent / "experiments" / "holdout" / "2026-08-29-two-person"


# ── 小工具：手工組事件／標註 ───────────────────────────────────────────


def ev(event_kind, t, **data) -> Event:
    return Event(kind=event_kind, t=t, data=data)


def spoken(t, kind, target=None, hard=True, at=None):
    return ev("spoken", t, kind=kind, target=target, text="（測試話術）", hard=hard, at=at if at is not None else t)


def queued(t, kind, target=None, hard=True):
    return ev("queued", t, kind=kind, target=target, text="（測試話術）", hard=hard)


def failed(t, kind, target=None, reason="測試失敗"):
    return ev("failed", t, kind=kind, target=target, text="（測試話術）", reason=reason)


def dropped(t, kind, target=None, reason="測試作廢"):
    return ev("dropped", t, kind=kind, target=target, text="（測試話術）", reason=reason)


def opportunity(id_, lo, hi, expect_type=None, scored=True, excluded_reason=None):
    w = {"id": id_, "kind": "opportunity", "range_seconds": [lo, hi],
         "expect_type": expect_type, "why": "測試窗口", "scored": scored}
    if excluded_reason:
        w["excluded_reason"] = excluded_reason
    return w


def no_intervention(id_, lo, hi):
    return {"id": id_, "kind": "no_intervention", "range_seconds": [lo, hi],
            "why": "測試窗口", "scored": True}


def labels_of(windows, *, duration_seconds=1000.0, known_false_positives=None, provenance=None):
    d = {"case_id": "synthetic-test", "duration_seconds": duration_seconds,
         "participants": ["甲", "乙"], "windows": windows}
    if known_false_positives is not None:
        d["known_false_positives"] = known_false_positives
    if provenance is not None:
        d["provenance"] = provenance
    return d


# ── 1. holdout 案例：數字與 labels.json 標註一致 ─────────────────────────


@pytest.mark.skipif(not (HOLDOUT_DIR / "meeting.events.jsonl").exists(), reason="需要 experiments/holdout/2026-08-29-two-person 的真實會議資料（不在公開 repo，見 experiments/holdout/README.md）")
def test_holdout_case_matches_labels():
    events = score_run.load_events(HOLDOUT_DIR / "meeting.events.jsonl")
    labels = score_run.load_labels(HOLDOUT_DIR / "labels.json")
    report = score_run.build_report(events, labels, HOLDOUT_DIR / "meeting.events.jsonl", HOLDOUT_DIR / "labels.json")

    c = report["intervention_counts"]
    assert c["total_interventions_excl_greeting"] == 11  # 11 次「發言超時」，問候不算
    assert c["fast"] == 11 and c["slow"] == 0
    assert c["tp"] == 0  # bug #3：慢路整場沉默，A1/A2 從未被正確命中
    assert c["excluded_scored_false"] == 2  # 467、499 落在窗口 B（scored=false）
    assert c["fp_total"] == 9  # 11 - 2（排除）= 9，其餘全部算 FP

    assert report["metrics"]["overall"]["opportunity_recall"]["value"] == 0.0
    assert report["metrics"]["overall"]["opportunity_recall"]["total"] == 2  # A1、A2

    kfp = report["known_false_positive_crossref"]
    assert kfp["matched_as_fp"] == 9
    excluded_statuses = [r["status"] for r in kfp["rows"] if "scored=false" in r["status"]]
    assert len(excluded_statuses) == 2

    q = report["queued_pipeline"]
    assert q["total_queued"] == 11 and q["spoken"] == 11 and q["failed"] == 0 and q["dropped"] == 0
    assert q["success_rate"]["value"] == 1.0


# ── 2. scored:false 窗口不影響分數，但列在報表並標明排除原因 ─────────────


def test_scored_false_window_excluded_from_scoring():
    windows = [opportunity("B", 100, 200, expect_type="離題", scored=False,
                            excluded_reason="測試：情境由已修缺陷造成")]
    events = [spoken(150, "離題")]  # type 正確、時間點也在窗口內，若有計分本該是 TP
    labels = labels_of(windows)
    report = score_run.build_report(events, labels, Path("e.jsonl"), Path("l.json"))

    c = report["intervention_counts"]
    assert c["tp"] == 0 and c["fp_total"] == 0
    assert c["excluded_scored_false"] == 1

    detail = report["windows"]["detail"]["B"]
    assert detail["scored"] is False
    assert detail["excluded_reason"] == "測試：情境由已修缺陷造成"
    assert detail["hit"] is None  # 排除的窗口不會被標記命中
    assert report["windows"]["all"][0]["excluded_reason"] == "測試：情境由已修缺陷造成"


# ── 3. no_intervention 區間內的介入算 FP ─────────────────────────────────


def test_no_intervention_window_intervention_counts_as_fp():
    windows = [no_intervention("C", 500, 600)]
    events = [spoken(550, "發言超時", target="甲")]
    labels = labels_of(windows)
    report = score_run.build_report(events, labels, Path("e.jsonl"), Path("l.json"))

    c = report["intervention_counts"]
    assert c["tp"] == 0
    assert c["fp_in_window"] == 1
    assert c["fp_total"] == 1
    assert "no_intervention" in report["windows"]["detail"]["C"]["fp_events"][0]["fp_reason"]


# ── 4. 同一 opportunity 窗口第二次以後算 FP，不是第二次 TP ───────────────


def test_second_intervention_in_same_window_is_fp_not_tp():
    windows = [opportunity("A1", 100, 300, expect_type="離題")]
    events = [spoken(120, "離題"), spoken(200, "離題"), spoken(250, "離題")]
    labels = labels_of(windows)
    report = score_run.build_report(events, labels, Path("e.jsonl"), Path("l.json"))

    c = report["intervention_counts"]
    assert c["tp"] == 1
    assert c["fp_in_window"] == 2
    fp_reasons = [f["fp_reason"] for f in report["windows"]["detail"]["A1"]["fp_events"]]
    assert all(r.startswith("重複命中") for r in fp_reasons)
    assert report["metrics"]["overall"]["repeat_hits"]["value"] == 2
    assert report["metrics"]["overall"]["opportunity_recall"]["value"] == 1.0


# ── 5. 慢路命中要求類型正確；同義詞正規化有作用 ──────────────────────────


def test_slow_path_requires_correct_type_with_synonym_normalization():
    # 5a：實際 kind 是同義詞「偏離主題」，expect_type 是「離題」——正規化後應視為同一類 → TP
    windows_a = [opportunity("A1", 100, 300, expect_type="離題")]
    events_a = [spoken(150, "偏離主題")]
    report_a = score_run.build_report(events_a, labels_of(windows_a), Path("e"), Path("l"))
    assert report_a["intervention_counts"]["tp"] == 1
    assert report_a["intervention_counts"]["fp_total"] == 0

    # 5b：型別真的不對（僵局 vs 離題）→ 不算 TP，仍算 FP（不是被忽略）
    windows_b = [opportunity("A1", 100, 300, expect_type="離題")]
    events_b = [spoken(150, "僵局")]
    report_b = score_run.build_report(events_b, labels_of(windows_b), Path("e"), Path("l"))
    assert report_b["intervention_counts"]["tp"] == 0
    assert report_b["intervention_counts"]["fp_in_window"] == 1
    assert "type 不符" in report_b["windows"]["detail"]["A1"]["fp_events"][0]["fp_reason"]
    assert report_b["metrics"]["overall"]["opportunity_recall"]["value"] == 0.0


# ── 6. 快路／慢路統計分開呈現 ─────────────────────────────────────────────


def test_fast_and_slow_stats_reported_separately():
    windows = [
        opportunity("F1", 0, 100, expect_type="有人被冷落"),   # 快路型別的 opportunity
        opportunity("S1", 200, 300, expect_type="離題"),        # 慢路型別的 opportunity
    ]
    events = [
        spoken(50, "有人被冷落", target="甲"),   # 快路 TP
        spoken(250, "離題"),                      # 慢路 TP
        spoken(400, "發言超時", target="乙"),     # 快路 FP（窗口外）
    ]
    labels = labels_of(windows, duration_seconds=3600.0)
    report = score_run.build_report(events, labels, Path("e"), Path("l"))

    assert report["intervention_counts"]["fast"] == 2 and report["intervention_counts"]["slow"] == 1
    m = report["metrics"]
    assert m["fast"]["opportunity_recall"]["value"] == 1.0
    assert m["fast"]["opportunity_recall"]["total"] == 1
    assert m["slow"]["opportunity_recall"]["value"] == 1.0
    assert m["slow"]["opportunity_recall"]["total"] == 1
    # FP/hour 應該分開算：快路 1 筆 FP，慢路 0 筆
    assert m["fast"]["fp_per_meeting_hour"]["count"] == 1
    assert m["slow"]["fp_per_meeting_hour"]["count"] == 0
    assert m["overall"]["fp_per_meeting_hour"]["count"] == 1


def test_untyped_opportunity_window_counts_for_both_paths():
    """`expect_type: null`（不限型別）的窗口屬於快路也屬於慢路，命中歸給實際接住它的那條。

    2026-09-05 的回歸：原本 `opportunity_recall` 用 `window_path(w) == path` 過濾，
    而不限型別的窗口 `window_path` 回傳 None，於是它同時從 fast 與 slow 的分母裡
    消失。8/31 holdout 的 O1 正是這種窗口（標註者刻意不限型別），結果
    `metrics.slow.opportunity_recall` 從來沒把它算進去——用那個數字當比較主指標時，
    一個真的把 O1 接住的改動會顯示成「沒有改善」。
    """
    windows = [opportunity("U1", 0, 100), opportunity("U2", 200, 300)]
    events = [spoken(50, "離題"),                       # 慢路接住 U1
              spoken(250, "發言超時", target="甲")]      # 快路接住 U2
    report = score_run.build_report(events, labels_of(windows, duration_seconds=3600.0),
                                    Path("e"), Path("l"))
    m = report["metrics"]
    # 兩個窗口都進兩邊的分母
    assert m["slow"]["opportunity_recall"]["total"] == 2
    assert m["fast"]["opportunity_recall"]["total"] == 2
    # 命中只歸給實際接住的那條路徑
    assert m["slow"]["opportunity_recall"]["hits"] == 1
    assert m["fast"]["opportunity_recall"]["hits"] == 1
    assert m["overall"]["opportunity_recall"]["hits"] == 2


# ── 7. 問候不計入介入 ─────────────────────────────────────────────────────


def test_greeting_not_counted_as_intervention():
    windows = [no_intervention("D", 0, 100)]
    events = [spoken(10, "問候", target=None)]
    labels = labels_of(windows)
    report = score_run.build_report(events, labels, Path("e"), Path("l"))

    assert report["intervention_counts"]["total_interventions_excl_greeting"] == 0
    assert report["intervention_counts"]["fp_total"] == 0
    assert report["windows"]["detail"]["D"]["fp_events"] == []


# ── 8. 算不出來的指標輸出 N/A 並附原因，不估算 ───────────────────────────


def test_unavailable_metrics_are_na_with_reason_never_estimated():
    labels = labels_of([])
    report = score_run.build_report([], labels, Path("e"), Path("l"))

    for name in ("pcm_duplicate_frames", "state_invariant_violations"):
        m = report["unavailable_metrics"][name]
        assert m["value"] is None
        assert m["reason"]  # 必須附理由，不能只是空的 None

    # 沒有任何 opportunity 窗口／soft 事件時也一律是 N/A，不是 0 或猜測值
    assert report["metrics"]["overall"]["opportunity_recall"]["value"] is None
    assert report["soft_intervention_metrics"]["wait_time_seconds"]["value"] is None
    assert report["soft_intervention_metrics"]["escalation_rate"]["value"] is None


# ── 9. Provenance 完整輸出，缺欄位標 N/A 並註明原因 ──────────────────────


def test_provenance_complete_and_missing_fields_marked_na():
    provenance = {
        "code": "abc123", "slow_path": {"model": "gpt-x", "effort": "none"},
        "fast_path": {"OVERTIME_SECONDS": 180.0}, "tts": {"voice": "v1"},
    }
    labels = labels_of([], provenance=provenance)
    report = score_run.build_report([], labels, Path("events.jsonl"), Path("labels.json"))
    p = report["provenance"]

    assert p["scored_at_git_sha"] and "N/A" not in p["scored_at_git_sha"]  # 在真的 git repo 裡跑
    assert p["case_id"] == "synthetic-test"
    assert p["recorded_at_capture_time"]["code_sha"] == "abc123"
    assert p["recorded_at_capture_time"]["slow_path"] == {"model": "gpt-x", "effort": "none"}
    for name in ("prompt_hash", "cache_hit_miss", "run_index"):
        assert p["not_applicable"][name]["value"] is None
        assert p["not_applicable"][name]["reason"]

    # 缺 provenance 欄位時要標 N/A 並附原因，不能整段消失或拋例外
    labels_missing = labels_of([])  # 完全沒有 "provenance" 這個 key
    report_missing = score_run.build_report([], labels_missing, Path("e"), Path("l"))
    rp = report_missing["provenance"]["recorded_at_capture_time"]
    assert rp["code_sha"]["value"] is None and rp["code_sha"]["reason"]
    assert rp["slow_path"]["value"] is None and rp["slow_path"]["reason"]


# ── 10. --json 輸出可被 json.load 讀回，欄位與表格一致 ───────────────────


def test_json_output_roundtrip_matches_stdout_report(tmp_path, capsys):
    windows = [opportunity("A1", 100, 300, expect_type="離題")]
    events_path = tmp_path / "e.jsonl"
    labels_path = tmp_path / "l.json"
    out_path = tmp_path / "out.json"
    events_path.write_text(
        json.dumps({"kind": "spoken", "t": 150.0,
                    "data": {"kind": "離題", "target": None, "text": "x", "hard": False, "at": 150.0}},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    labels_path.write_text(json.dumps(labels_of(windows), ensure_ascii=False), encoding="utf-8")

    rc = score_run.main([str(events_path), str(labels_path), "--json", str(out_path)])
    assert rc == 0
    assert out_path.exists()

    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["intervention_counts"]["tp"] == 1
    assert loaded["meeting"]["case_id"] == "synthetic-test"
    printed = capsys.readouterr().out
    assert "case_id: synthetic-test" in printed
    assert "TP=1" in printed


# ── 11. 落在窗口外的介入：算 FP，且在報表中可辨識 ─────────────────────────


def test_intervention_outside_all_windows_counts_as_fp_and_is_identifiable():
    windows = [opportunity("A1", 100, 200, expect_type="離題")]
    events = [spoken(9999, "發言超時", target="甲")]  # 遠在任何窗口之外
    labels = labels_of(windows)
    report = score_run.build_report(events, labels, Path("e"), Path("l"))

    assert report["intervention_counts"]["fp_outside_windows"] == 1
    assert report["intervention_counts"]["fp_in_window"] == 0
    outside = report["outside_window_interventions"]
    assert len(outside) == 1
    assert outside[0]["t"] == 9999
    assert "窗口之外" in outside[0]["fp_reason"]


# ── queued → spoken pipeline（延遲、成功率）＋ soft 等待/升級/作廢 ────────


def test_queued_pipeline_matches_spoken_via_fifo_and_computes_latency():
    events = [
        queued(10.0, "發言超時", target="甲", hard=True),
        spoken(10.2, "發言超時", target="甲", hard=True, at=10.15),
        queued(20.0, "發言超時", target="甲", hard=True),
        failed(20.5, "發言超時", target="甲"),
    ]
    labels = labels_of([])
    report = score_run.build_report(events, labels, Path("e"), Path("l"))
    q = report["queued_pipeline"]
    assert q["total_queued"] == 2
    assert q["spoken"] == 1 and q["failed"] == 1
    assert q["success_rate"]["value"] == 0.5
    assert q["spoken_latency_seconds"]["value"] == round(10.2 - 10.0, 3)


def test_soft_intervention_escalation_and_void_rate():
    events = [
        # soft 直接說出口，沒升級
        queued(0.0, "有人被冷落", target="甲", hard=False),
        spoken(0.5, "有人被冷落", target="甲", hard=False, at=0.5),
        # soft 等超過 ESCALATE_SECONDS 沒等到停頓 → 升級成 hard 才說出口
        queued(10.0, "有人被冷落", target="乙", hard=False),
        spoken(26.0, "有人被冷落", target="乙", hard=True, at=26.0),
        # soft 被取代作廢
        queued(40.0, "議程超時", hard=False),
        dropped(41.0, "議程超時", reason="被硬打斷取代"),
    ]
    labels = labels_of([])
    report = score_run.build_report(events, labels, Path("e"), Path("l"))
    s = report["soft_intervention_metrics"]
    assert s["n_soft"] == 3
    assert s["escalation_rate"]["value"] == round(1 / 3, 3)
    assert s["escalation_rate"]["escalated"] == 1
    assert s["void_rate"]["value"] == round(1 / 3, 3)
    assert s["void_rate"]["dropped"] == 1


def test_known_false_positive_crossref_flags_mismatch():
    """labels.json 記的誤報時間點，若跑出來的事件流裡找不到對應介入，
    要能被看出來（人工核對用），不能悄悄吞掉。"""
    windows = [no_intervention("C", 0, 1000)]
    events = [spoken(100, "發言超時", target="甲")]  # 真實只有一筆
    kfp = {"kind": "發言超時", "at_seconds": [100, 500], "count": 2}  # labels 宣稱有兩筆
    labels = labels_of(windows, known_false_positives=kfp)
    report = score_run.build_report(events, labels, Path("e"), Path("l"))

    rows = {r["at_seconds"]: r["status"] for r in report["known_false_positive_crossref"]["rows"]}
    assert rows[100] == "算作 FP"
    assert "不一致" in rows[500]
