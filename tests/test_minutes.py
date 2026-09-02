"""T-C：會議記錄 A（LLM 會議產出）＋ B（純程式主持記錄）。

涵蓋（見 T-C 驗收標準）：
(a) render_host_record：介入清單、作廢／失敗候選、被壓掉的慢路評分、發言分佈、階段軌跡；
(b) render_minutes：固定 payload 渲染出四節；
(c) write_minutes：LLM 呼叫拋例外時 A 檔寫「生成失敗」、B 檔正常、函式本身不拋例外。
"""
import json

from meeting_host.events import Event
from meeting_host.minutes import (
    FAST_DETAIL_UNKNOWN,
    SUPPRESSED_NO_TYPE,
    SUPPRESSED_SCORE_BELOW,
    build_minutes_prompt,
    render_host_record,
    render_minutes,
    write_minutes,
)


def _events():
    """手工組一場會議的事件序列：快路介入 1 筆（成功）、慢路介入 1 筆（成功）、
    1 筆失敗、1 筆作廢、3 筆被壓掉的慢路評分（三種 reason 各一次）、
    一筆 meeting（phase 變動）、一筆 share。
    """
    events = []

    # 開場
    events.append(Event("meeting", 0.0, {
        "topic": "黑客松籌備", "duration_min": 30, "phase": "發散期",
        "participants": ["Alex"],
    }))
    events.append(Event("utterance", 5.0, {
        "speaker": "Alex", "text": "先講一下時程", "start": 4.0, "end": 5.0}))

    # 快路介入（成功）：queued 前一筆是 fast_timer，不是 slow_score
    events.append(Event("fast_timer", 10.0, {
        "run": {"speaker": "Alex", "seconds": 95.0}, "silent": {}, "remaining": 1700.0}))
    events.append(Event("queued", 10.0, {
        "kind": "發言超時", "target": "Alex", "text": "Alex 已經連續講了 90 秒，請讓其他人也發言。",
        "hard": True}))
    events.append(Event("spoken", 11.0, {
        "kind": "發言超時", "target": "Alex", "text": "Alex 已經連續講了 90 秒，請讓其他人也發言。",
        "hard": True, "at": 11.0}))

    # 被壓掉的慢路評分：type=無
    events.append(Event("slow_score", 15.0, {
        "positive": 2, "negative": 1, "none": 3, "type": "無", "verdict": "不介入",
        "utterance": "", "pros": ["p"], "cons": ["c"], "admissible": False, "reason": "type=無"}))

    # 被壓掉的慢路評分：無話術
    events.append(Event("slow_score", 20.0, {
        "positive": 4, "negative": 1, "none": 1, "type": "離題", "verdict": "正向介入",
        "utterance": "", "pros": ["p2"], "cons": ["c2"], "admissible": False, "reason": "無話術"}))

    # 被壓掉的慢路評分：冷卻
    events.append(Event("slow_score", 25.0, {
        "positive": 5, "negative": 1, "none": 1, "type": "重複", "verdict": "正向介入",
        "utterance": "先不要打斷", "pros": ["p3"], "cons": ["c3"],
        "admissible": False, "reason": "冷卻"}))

    # 慢路介入（成功）：queued 前一筆是 admissible=True 的 slow_score
    events.append(Event("slow_score", 30.0, {
        "positive": 4, "negative": 1, "none": 1, "type": "離題", "verdict": "正向介入",
        "utterance": "請回到議題", "pros": ["已經偏離議題兩分鐘", "其他人插不上話"], "cons": ["c4"],
        "admissible": True, "reason": ""}))
    events.append(Event("queued", 30.0, {
        "kind": "離題", "target": None, "text": "請回到議題", "hard": False}))
    events.append(Event("spoken", 31.0, {
        "kind": "離題", "target": None, "text": "請回到議題", "hard": False, "at": 31.0}))

    # 快路介入：作廢（dropped）
    events.append(Event("fast_timer", 40.0, {
        "run": None, "silent": {"Alex": 200.0}, "remaining": 1600.0}))
    events.append(Event("queued", 40.0, {
        "kind": "有人被冷落", "target": "Alex", "text": "Alex 好一陣子沒發言了，問問他的看法。",
        "hard": False}))
    events.append(Event("dropped", 42.0, {
        "kind": "有人被冷落", "target": "Alex", "text": "Alex 好一陣子沒發言了，問問他的看法。",
        "reason": "世界已變"}))

    # 快路介入：失敗（failed）
    events.append(Event("fast_timer", 50.0, {
        "run": {"speaker": "Alex", "seconds": 95.0}, "silent": {}, "remaining": 1500.0}))
    events.append(Event("queued", 50.0, {
        "kind": "發言超時", "target": "Alex", "text": "請讓其他人也發言。", "hard": True}))
    events.append(Event("failed", 51.0, {
        "kind": "發言超時", "target": "Alex", "text": "請讓其他人也發言。", "reason": "TTS 逾時"}))

    # phase 變動
    events.append(Event("meeting", 60.0, {
        "topic": "黑客松籌備", "duration_min": 30, "phase": "收斂期",
        "participants": ["Alex"],
    }))

    # 發言分佈（只取最後一筆）
    events.append(Event("share", 5.0, {"Alex": 0.6, "主席": 0.4}))
    events.append(Event("share", 65.0, {"Alex": 0.8, "主席": 0.2}))

    return events


# ── render_host_record ──────────────────────────────────────────────


def test_render_host_record_lists_interventions():
    md = render_host_record(_events(), ["Alex"])
    # 介入清單：快路成功 1 筆 + 慢路成功 1 筆 = 2 筆
    assert md.count("發言超時") >= 1
    intervention_section = md.split("## 作廢／失敗候選")[0]
    assert "離題" in intervention_section
    assert "請回到議題" in intervention_section
    assert "Alex 已經連續講了 90 秒" in intervention_section
    # 慢路的理由取 pros
    assert "已經偏離議題兩分鐘" in intervention_section


def test_render_host_record_lists_dropped_and_failed():
    md = render_host_record(_events(), ["Alex"])
    voided_section = md.split("## 作廢／失敗候選")[1].split("## 被壓掉的慢路評分")[0]
    assert "作廢" in voided_section
    assert "失敗" in voided_section
    assert "有人被冷落" in voided_section
    assert "世界已變" in voided_section
    assert "TTS 逾時" in voided_section


def test_render_host_record_counts_suppressed_slow_scores():
    md = render_host_record(_events(), ["Alex"])
    section = md.split("## 被壓掉的慢路評分")[1].split("## 發言分佈")[0]
    assert "共 3 筆" in section
    assert "type=無" in section
    assert "無話術" in section
    assert "冷卻" in section


def _fast_events(timer_data, queued_data):
    """一組最小的快路介入事件：fast_timer → queued → spoken。"""
    return [
        Event("fast_timer", 10.0, timer_data),
        Event("queued", 10.0, queued_data),
        Event("spoken", 11.0, dict(queued_data, at=11.0)),
    ]


def _intervention_rows(md):
    return md.split("## 介入清單")[1].split("## 作廢／失敗候選")[0]


def _cells(section):
    """把一段 markdown 表格拆成 [[欄, 欄, ...], ...]，跳過表頭與分隔列。"""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-"):
            continue
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows[1:]  # 第一列是表頭


def test_fast_reason_is_trigger_fact_not_the_utterance():
    """缺陷一：快路的理由欄不能是話術的複本，要是規則觸發當下的具體事實。"""
    md = render_host_record(_events(), ["Alex"])
    section = _intervention_rows(md)
    # 話術（第 5 欄）與理由（第 6 欄）不再逐字相同
    row = [line for line in section.splitlines() if "發言超時" in line][0]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[4] == "Alex 已經連續講了 90 秒，請讓其他人也發言。"
    assert cells[5] == "Alex 已連續發言 1.6 分鐘"  # fast_timer run.seconds=95.0


def test_fast_reason_covers_every_fast_rule():
    """四條快路規則各自從 fast_timer 反推出對應的事實。"""
    cases = [
        ({"run": {"speaker": "Alex", "seconds": 240.0}, "silent": {}, "remaining": 900.0},
         {"kind": "發言超時", "target": "Alex", "text": "話術", "hard": True},
         "Alex 已連續發言 4.0 分鐘"),
        ({"run": None, "silent": {"Alex": 318.0, "Bob": 12.0}, "remaining": 900.0},
         {"kind": "有人被冷落", "target": "Alex", "text": "話術", "hard": False},
         "Alex 已 5.3 分鐘沒有發言"),
        ({"run": None, "silent": {}, "remaining": 252.0},
         {"kind": "議程超時", "target": None, "text": "話術", "hard": False},
         "議程只剩 4.2 分鐘"),
        ({"run": None, "silent": {"Alex": 132.0, "Bob": 96.0}, "remaining": 900.0},
         {"kind": "全場沉默", "target": None, "text": "話術", "hard": False},
         # 事件流沒有 absent 名單，取所有人的最小值＝真值的下界，用 ≥ 標示
         "全場已 ≥1.6 分鐘沒有人發言"),
    ]
    for timer_data, queued_data, expected in cases:
        md = render_host_record(_fast_events(timer_data, queued_data), ["Alex", "Bob"])
        row = _cells(_intervention_rows(md))[0]
        assert row[5] == expected, (queued_data["kind"], row)
        assert row[5] != row[4]  # 理由欄沒有再抄一次話術


def test_fast_reason_prefers_detail_carried_on_the_queued_event():
    """若日後 live.py 把 Trigger.detail 帶進 queued 事件，優先採用，不再反推。"""
    events = _fast_events(
        {"run": {"speaker": "Alex", "seconds": 240.0}, "silent": {}, "remaining": 900.0},
        {"kind": "發言超時", "target": "Alex", "text": "話術", "hard": True,
         "detail": "Alex 已連續發言 4.2 分鐘"})
    section = _intervention_rows(render_host_record(events, ["Alex"]))
    assert "Alex 已連續發言 4.2 分鐘" in section
    assert "4.0 分鐘" not in section


def test_fast_reason_marks_unknown_when_event_file_lacks_the_numbers():
    """反推不到就誠實留白，不退回顯示話術（那正是原本的缺陷）。"""
    events = [
        Event("utterance", 9.0, {"speaker": "Alex", "text": "嗨", "start": 8.0, "end": 9.0}),
        Event("queued", 10.0, {"kind": "發言超時", "target": "Alex", "text": "話術", "hard": True}),
        Event("spoken", 11.0, {"kind": "發言超時", "target": "Alex", "text": "話術",
                                "hard": True, "at": 11.0}),
    ]
    row = _cells(_intervention_rows(render_host_record(events, ["Alex"])))[0]
    assert row[5] == FAST_DETAIL_UNKNOWN
    assert row[5] != row[4]

    # 對象對不上（fast_timer 記的是別人在講）也算反推不到，不能張冠李戴
    mismatched = _fast_events(
        {"run": {"speaker": "Bob", "seconds": 240.0}, "silent": {}, "remaining": 900.0},
        {"kind": "發言超時", "target": "Alex", "text": "話術", "hard": True})
    assert _cells(_intervention_rows(
        render_host_record(mismatched, ["Alex"])))[0][5] == FAST_DETAIL_UNKNOWN


def test_slow_reason_still_comes_from_pros():
    """慢路那幾列不受缺陷一的修法影響，理由仍是 slow_score 的 pros。"""
    section = _intervention_rows(render_host_record(_events(), ["Alex"]))
    assert "已經偏離議題兩分鐘；其他人插不上話" in section


def _suppressed_events():
    """三種空 reason 的被壓掉評分：模型判了類型／模型判 type=無／異常（無 verdict）。"""
    return [
        # 模型判了「離題」，但 max(2, 2) <= 3 → decide() 判不介入
        Event("slow_score", 10.0, {
            "positive": 2, "negative": 2, "none": 3, "type": "離題", "verdict": "不介入",
            "utterance": "", "pros": ["p"], "cons": ["c"], "admissible": False, "reason": ""}),
        # 平手（差距 0）：max(2, 3) <= 3
        Event("slow_score", 20.0, {
            "positive": 2, "negative": 3, "none": 3, "type": "僵局", "verdict": "不介入",
            "utterance": "", "pros": ["p"], "cons": ["c"], "admissible": False, "reason": ""}),
        # 模型連類型都給「無」
        Event("slow_score", 30.0, {
            "positive": 1, "negative": 1, "none": 4, "type": "無", "verdict": "不介入",
            "utterance": "", "pros": ["p"], "cons": ["c"], "admissible": False, "reason": ""}),
        # type=無 否決（三軸判要介入）——事件自己寫了 reason，照原樣顯示
        Event("slow_score", 40.0, {
            "positive": 4, "negative": 1, "none": 1, "type": "無", "verdict": "正向介入",
            "utterance": "", "pros": ["p"], "cons": ["c"], "admissible": False,
            "reason": "type=無"}),
    ]


def _suppressed_section(md):
    return md.split("## 被壓掉的慢路評分")[1].split("## 發言分佈")[0]


def test_suppressed_scores_split_model_judged_from_model_silent():
    """缺陷二：空 reason 要分得出「模型判了類型但三軸不足」與其他情況。"""
    section = _suppressed_section(render_host_record(_suppressed_events(), ["Alex"]))
    assert "共 4 筆" in section
    assert "（無原因）" not in section
    summary = section.split("###")[0]
    assert f"| {SUPPRESSED_SCORE_BELOW} | 2 |" in summary
    assert f"| {SUPPRESSED_NO_TYPE} | 1 |" in summary
    assert "| type=無 | 1 |" in summary


def test_suppressed_scores_counts_still_sum_to_total():
    """加總不變：分類只是換名字，不能吃掉或複製任何一筆。"""
    events = _suppressed_events()
    section = _suppressed_section(render_host_record(events, ["Alex"]))
    summary = section.split("###")[0]
    assert sum(int(row[1]) for row in _cells(summary)) == len(events)


def test_suppressed_scores_detail_table_shows_three_axes_and_gap():
    """三軸分數攤開，差距＝不介入 − max(正向, 負向)（decide() 比的那個量）。"""
    section = _suppressed_section(render_host_record(_suppressed_events(), ["Alex"]))
    detail = section.split(f"### {SUPPRESSED_SCORE_BELOW}")[1]
    assert "| 00:10 | 離題 | 2 | 2 | 3 | 1 |" in detail
    assert "| 00:20 | 僵局 | 2 | 3 | 3 | 0 |" in detail
    # 只列這一種：type=無 的那兩筆（00:30／00:40）不該出現在明細表裡
    body = detail.split("## 發言分佈")[0]
    assert "00:30" not in body
    assert "00:40" not in body


def test_suppressed_scores_detail_table_absent_when_no_such_case():
    """沒有這一種就不要多印一個空區塊。"""
    events = [e for e in _suppressed_events() if e.data.get("reason") == "type=無"]
    section = _suppressed_section(render_host_record(events, ["Alex"]))
    assert SUPPRESSED_SCORE_BELOW not in section


def test_render_host_record_share_uses_last_share_event():
    md = render_host_record(_events(), ["Alex"])
    section = md.split("## 發言分佈")[1].split("## 階段軌跡")[0]
    assert "80%" in section
    assert "20%" in section
    assert "60%" not in section  # 只取最後一筆 share


def test_render_host_record_phase_trajectory():
    md = render_host_record(_events(), ["Alex"])
    section = md.split("## 階段軌跡")[1]
    assert "發散期" in section
    assert "收斂期" in section


def test_render_host_record_empty_events_has_no_sections_crash():
    md = render_host_record([], [])
    assert "（無）" in md
    assert "介入清單" in md


# ── render_minutes ───────────────────────────────────────────────────


def _minutes_payload():
    return {
        "decisions": [{"who": "Alex", "what": "先做骨架", "by": "9/5"}],
        "todos": [{"owner": "Alex", "task": "把 pipeline 接起來"}],
        "unresolved": [{"topic": "要不要做觀戰 UI", "chair_ruling": "先做完 A/B 再評估"}],
        "stances": {"Alex": "希望先求有再求好"},
    }


def test_render_minutes_has_four_sections():
    md = render_minutes(_minutes_payload())
    assert "## 決議事項" in md
    assert "## 待辦事項" in md
    assert "## 未解決事項" in md
    assert "## 每人立場摘要" in md
    assert "先做骨架" in md
    assert "把 pipeline 接起來" in md
    assert "先做完 A/B 再評估" in md
    assert "希望先求有再求好" in md


def test_render_minutes_handles_empty_payload():
    md = render_minutes({})
    assert "（無）" in md
    assert "# 會議產出" in md


# ── build_minutes_prompt ────────────────────────────────────────────


def test_build_minutes_prompt_includes_transcript_and_interventions():
    prompt = build_minutes_prompt(_events())
    assert "先講一下時程" in prompt
    assert "請回到議題" in prompt
    assert "Alex 已經連續講了 90 秒" in prompt


# ── write_minutes ────────────────────────────────────────────────────


class _FakeSt:
    participants = ["Alex"]


class _FakeSession:
    def __init__(self, events):
        self.events = events
        self.st = _FakeSt()


def test_write_minutes_llm_failure_keeps_b_writes_failure_message(tmp_path, monkeypatch):
    def boom(events):
        raise RuntimeError("no network")

    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm", boom)

    session = _FakeSession(_events())
    host_path, minutes_path = write_minutes(session, tmp_path)

    assert host_path.exists()
    assert minutes_path.exists()
    host_text = host_path.read_text(encoding="utf-8")
    minutes_text = minutes_path.read_text(encoding="utf-8")
    assert "介入清單" in host_text
    assert "生成失敗" in minutes_text
    assert "RuntimeError" in minutes_text


def test_write_minutes_success_renders_llm_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meeting_host.minutes._call_minutes_llm", lambda events: _minutes_payload())

    session = _FakeSession(_events())
    host_path, minutes_path = write_minutes(session, tmp_path)

    minutes_text = minutes_path.read_text(encoding="utf-8")
    assert "先做骨架" in minutes_text
    assert json.dumps(_minutes_payload(), ensure_ascii=False)  # payload 本身可序列化，防手誤
