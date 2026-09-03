"""會議記錄 A（LLM 會議產出）＋ B（純程式主持記錄），輸入 `Session.events`（T-B 事件匯流排）。

B 純程式產生，不用 LLM：介入清單、作廢／失敗候選、被壓掉的慢路評分數、發言分佈、階段軌跡。
A 一次 LLM 呼叫（沿用 `slow_path` 的 API 設定與 model）：決議事項、待辦、未解決事項、
每人立場摘要，JSON schema 回傳再渲染成 md。

`write_minutes(session, out_dir)` 是唯一的 IO／LLM 呼叫進入點；其餘函式皆為純函式，
方便單元測試不打真實 API。LLM 失敗時 A 檔寫「生成失敗」、不拋例外，B 檔照寫。
"""
import json
import time
import urllib.request
from pathlib import Path

from .events import Event
from .security import prepare_private_dir, write_protected_text
from .slow_path import API_URL, EFFORT, MODEL, _api_key


def fmt(t: float) -> str:
    """`MM:SS`。與 live.py 的 fmt 邏輯相同，這裡獨立一份以避免 import live.py 拖進
    discord/speaker 等重依賴（T-C 只消費事件，不需要那些模組）。"""
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


MINUTES_SYSTEM = """你是會議記錄祕書，根據完整逐字稿與主席介入紀錄整理一份會議產出。
只根據逐字稿與介入紀錄裡實際出現的內容作答，不要杜撰未出現的事實或人名。
若某一節沒有內容，回傳空陣列或空物件，不要編造。

用以下 JSON 格式回覆，不要有其他內容：
{
  "decisions": [{"who": "...", "what": "...", "by": "..."}],
  "todos": [{"owner": "...", "task": "..."}],
  "unresolved": [{"topic": "...", "chair_ruling": "..."}],
  "stances": {"某人": "立場摘要"}
}"""


# ── B：純程式主持記錄 ─────────────────────────────────────────────────


# 快路介入的「理由」欄要回答的是「為什麼這個時候觸發」，不是「主席說了什麼」。
# 規則型介入的具體事實在 `fast_path.Trigger.detail`（例如「Alex Huang 已連續發言
# 4.2 分鐘」），但那個欄位目前只進 `live.py` 的文字 log——`emit("queued", ...)`
# 只帶 kind/target/text/hard，detail 沒有進事件流。
#
# 不改 live.py 也拿得到，因為事件檔裡已經有同一組數字：`Session._fast_tick` 是一段
# 沒有 await 的同步程式，順序固定是
#     emit("fast_timer", {run, silent, remaining}) → fast_path.check(st, now, ...) → emit("queued", ...)
# 兩個 emit 之間沒有別的 emit，而 `check()` 產生 detail 用的 `st.current_run_seconds`／
# `silent_seconds`／`remaining_seconds` 就是 fast_timer 那三個欄位、且用同一個 `now`。
# 所以「queued 的前一筆 fast_timer」可以逐條規則反推出 detail，而且連舊的
# events.jsonl（含已經錄完、無法重跑的真實會議）都能重算。
#
# 反推的精度，逐條規則：
# - 發言超時／議程超時／有人被冷落：與 detail 逐字相同（同一個 now、同一個取值函式）。
# - 全場沉默：`check()` 取的是**在場**參與者 silent_seconds 的最小值（排除 st.absent），
#   fast_timer 的 silent 則含所有 participants。absent 不在事件流裡，無法還原，而
#   「超集合的 min ≤ 子集合的 min」，所以反推值恆為真值的**下界**——因此那一條輸出
#   用 `≥` 標示，不假裝是精確值。要精確值只能由 live.py 把 detail 帶進 queued 事件
#   （那是另一張工單的檔案，見交付說明）。
#
# 若日後 `queued` 事件真的帶了 `detail`，`_pair_interventions` 會優先採用它，
# 這裡的反推自動退居備援。
FAST_DETAIL_UNKNOWN = "（事件檔缺當下的規則數值）"


def _fast_trigger_detail(kind: str, target: str | None, prev: Event | None) -> str:
    """從 queued 前一筆 fast_timer 反推快路規則觸發當下的具體事實。

    拿不到（前一筆不是 fast_timer、欄位缺、型別不對）就回 `FAST_DETAIL_UNKNOWN`，
    不退回顯示話術——重複顯示話術正是這一欄原本沒有資訊量的原因。
    """
    if prev is None or prev.kind != "fast_timer":
        return FAST_DETAIL_UNKNOWN
    data = prev.data or {}
    if kind == "發言超時":
        run = data.get("run") or {}
        seconds = run.get("seconds")
        if run.get("speaker") == target and isinstance(seconds, (int, float)):
            return f"{target} 已連續發言 {seconds / 60:.1f} 分鐘"
    elif kind == "有人被冷落":
        seconds = (data.get("silent") or {}).get(target)
        if isinstance(seconds, (int, float)):
            return f"{target} 已 {seconds / 60:.1f} 分鐘沒有發言"
    elif kind == "議程超時":
        remaining = data.get("remaining")
        if isinstance(remaining, (int, float)):
            return f"議程只剩 {remaining / 60:.1f} 分鐘"
    elif kind == "全場沉默":
        values = [v for v in (data.get("silent") or {}).values()
                  if isinstance(v, (int, float))]
        if values:
            # `≥`：事件流沒有 absent 名單，這是真值的下界（理由見上方模組註解）
            return f"全場已 ≥{min(values) / 60:.1f} 分鐘沒有人發言"
    return FAST_DETAIL_UNKNOWN


def _pair_interventions(events: list[Event]) -> list[dict]:
    """把 `queued` 事件依序配對到它的結果（`spoken`／`failed`／`dropped`），並判斷
    來源是快路還是慢路。

    判斷依據：`live.py` 的 `_run_slow_score` 對慢路介入固定在 `emit("slow_score", ...)`
    後緊接 `emit("queued", ...)`（同一次同步呼叫，中間不會插入其他事件）；`watch_fast`
    的快路介入前一筆事件則是 `fast_timer`，不是 `slow_score`。故用「`queued` 前一筆
    事件是否為 `admissible=True` 的 `slow_score`」判斷快慢路徑。

    只回傳有等到結果的介入；`queued` 之後整場都沒有對應結果（回放資料手工組不完整、
    或會議中途中斷）的候選直接略過，不硬湊。
    """
    results = []
    used_outcome_idx: set[int] = set()
    for qi, qe in enumerate(events):
        if qe.kind != "queued":
            continue
        key = (qe.data["kind"], qe.data.get("target"), qe.data["text"])
        slow = None
        if qi > 0 and events[qi - 1].kind == "slow_score" and events[qi - 1].data.get("admissible"):
            slow = events[qi - 1]

        outcome_event = None
        for oi in range(qi + 1, len(events)):
            if oi in used_outcome_idx:
                continue
            oe = events[oi]
            if oe.kind not in ("spoken", "failed", "dropped"):
                continue
            okey = (oe.data["kind"], oe.data.get("target"), oe.data["text"])
            if okey == key:
                outcome_event = oe
                used_outcome_idx.add(oi)
                break
        if outcome_event is None:
            continue

        source = "慢路" if slow is not None else "快路"
        # 慢路的理由取對應 slow_score 的 pros；快路沒有 pros，取規則觸發當下的具體
        # 事實（`Trigger.detail` 的等價值），優先用 queued 自帶的 detail，沒有就從
        # 前一筆 fast_timer 反推——都不用話術當理由，那只會讓兩欄一模一樣。
        if slow is not None:
            reason = "；".join(slow.data.get("pros", []))
        else:
            reason = (qe.data.get("detail")
                      or _fast_trigger_detail(qe.data["kind"], qe.data.get("target"),
                                              events[qi - 1] if qi > 0 else None))

        results.append({
            "t": outcome_event.t,
            "kind": qe.data["kind"],
            "target": qe.data.get("target"),
            "hard": qe.data["hard"],
            "text": qe.data["text"],
            "reason": reason,
            "source": source,
            "outcome": outcome_event.kind,
            "outcome_reason": outcome_event.data.get("reason"),
        })
    return results


# 「被壓掉的慢路評分」裡最常見的一種，在事件檔裡沒有名字。
#
# `live.slow_gate` 只在「三軸判要介入、但 type=無 否決」時寫理由 "type=無"，其餘
# `not is_intervention(r)` 的情況一律回空字串（該函式最後一段的 `return False, ""`），
# 渲染成「（無原因）」。2026-08-31 那場真實會議 104 筆被壓掉的評分裡有 95 筆是這一種，
# 等於九成沒有解釋。
#
# 空字串其實只有一種來源：`slow_path.is_intervention` 是
#     verdict != "不介入" and type not in ("無", "", None)
# 它為 False 而又不落在 "type=無" 那一支（該支要求 verdict != "不介入"），只可能是
# `verdict == "不介入"`，也就是 `slow_path.decide()` 的 `max(positive, negative) <= none`。
# 所以空字串 ＝「三軸分數自己投票不介入」，再依 type 是不是真的類型分成兩種狀態：
#   - 模型判了一個類型（離題／僵局…）卻自己投票不介入 → 模型認出了問題但忍住
#   - 模型連類型都給「無」 → 模型本來就覺得沒事
# 兩者混在同一格「（無原因）」裡，看記錄的人分不出來。
#
# ⚠️ 這裡純粹是顯示層的重新命名，不碰 `decide()`／`is_intervention()`／
# `slow_gate()`／`slow_result_admissible()` 的任何判斷，也不改事件裡的 reason 欄位。
SUPPRESSED_SCORE_BELOW = "三軸分數不足（模型已判類型，自己投票不介入）"
SUPPRESSED_NO_TYPE = "模型與三軸都判不需介入（type=無）"
SUPPRESSED_UNKNOWN = "（無原因）"


def _suppressed_reason(data: dict) -> str:
    """被壓掉的慢路評分要顯示的原因。事件有寫 reason 就照用，空的才分類。"""
    reason = data.get("reason") or ""
    if reason:
        return reason
    if data.get("verdict") != "不介入":
        return SUPPRESSED_UNKNOWN  # 不該發生（見上方推導），真出現就誠實留白
    if data.get("type") in ("無", "", None):
        return SUPPRESSED_NO_TYPE
    return SUPPRESSED_SCORE_BELOW


def _score_cell(value) -> str:
    return str(value) if isinstance(value, (int, float)) else "-"


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["（無）"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render_host_record(events: list[Event], participants: list[str]) -> str:
    """B：純程式主持記錄。介入清單、作廢／失敗候選、被壓掉的慢路評分數、
    發言分佈、階段軌跡。"""
    paired = _pair_interventions(events)
    spoken = [p for p in paired if p["outcome"] == "spoken"]
    voided = [p for p in paired if p["outcome"] in ("failed", "dropped")]

    lines = ["# 主持記錄", "", "## 介入清單"]
    lines += _render_table(
        ["時間", "類型", "對象", "硬/軟", "話術", "理由"],
        [[fmt(p["t"]), p["kind"], p["target"] or "-", "硬" if p["hard"] else "軟",
          p["text"], p["reason"]] for p in spoken])

    lines += ["", "## 作廢／失敗候選"]
    lines += _render_table(
        ["時間", "類型", "對象", "結果", "話術", "原因"],
        [[fmt(p["t"]), p["kind"], p["target"] or "-",
          "失敗" if p["outcome"] == "failed" else "作廢",
          p["text"], p["outcome_reason"] or ""] for p in voided])

    slow_scores = [e for e in events if e.kind == "slow_score"]
    suppressed = [e for e in slow_scores if not e.data.get("admissible")]
    reason_counts: dict[str, int] = {}
    for e in suppressed:
        reason = _suppressed_reason(e.data)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    lines += ["", "## 被壓掉的慢路評分", f"共 {len(suppressed)} 筆"]
    lines += _render_table(["原因", "次數"],
                            [[reason, str(count)] for reason, count in reason_counts.items()])

    # 「模型認出了問題但自己投票不介入」是調判準時最需要逐筆看的一種——只給一個
    # 次數看不出離門檻多遠，所以把三軸分數攤開。
    # 「差距」＝ 不介入 − max(正向, 負向)，也就是 `slow_path.decide()` 比的那個量：
    # `max(positive, negative) <= none` 成立就判不介入，所以 0 代表平手（差一分就
    # 會開口），數字愈大代表模型愈確定該忍住。
    below = [e for e in suppressed if _suppressed_reason(e.data) == SUPPRESSED_SCORE_BELOW]
    if below:
        lines += ["", f"### {SUPPRESSED_SCORE_BELOW}",
                  "模型判了類型、也寫了正反理由，但三軸分數比不過「不介入」那一軸，"
                  "所以主席沒有開口。差距 ＝ 不介入 − max(正向, 負向)，0 代表平手。"]
        rows = []
        for e in below:
            p, n, none = e.data.get("positive"), e.data.get("negative"), e.data.get("none")
            if all(isinstance(v, (int, float)) for v in (p, n, none)):
                gap = _score_cell(none - max(p, n))
            else:
                gap = "-"
            rows.append([fmt(e.t), e.data.get("type") or "-",
                         _score_cell(p), _score_cell(n), _score_cell(none), gap])
        lines += _render_table(["時間", "類型", "正向", "負向", "不介入", "差距"], rows)

    share_events = [e for e in events if e.kind == "share"]
    names = list(participants) + (["主席"] if "主席" not in participants else [])
    share_rows = []
    if share_events:
        data = share_events[-1].data
        for name in names:
            if name in data:
                share_rows.append([name, f"{data[name]:.0%}"])
        for name, pct in data.items():  # 防禦：data 裡有 names 沒涵蓋到的人
            if name not in names:
                share_rows.append([name, f"{pct:.0%}"])
    lines += ["", "## 發言分佈"]
    lines += _render_table(["對象", "佔比"], share_rows)

    meeting_events = [e for e in events if e.kind == "meeting"]
    phase_rows = []
    last_phase = None
    for e in meeting_events:
        phase = e.data.get("phase")
        if phase != last_phase:
            phase_rows.append([fmt(e.t), phase])
            last_phase = phase
    lines += ["", "## 階段軌跡"]
    lines += _render_table(["時間", "階段"], phase_rows)

    return "\n".join(lines) + "\n"


# ── A：LLM 會議產出 ────────────────────────────────────────────────────


def build_minutes_prompt(events: list[Event]) -> str:
    """完整逐字稿＋介入清單，餵給 LLM 產生會議產出。"""
    transcript = "\n".join(
        f"[{fmt(e.t)}] {e.data['speaker']}：{e.data['text']}"
        for e in events if e.kind == "utterance")
    intervention_lines = [
        f"[{fmt(p['t'])}]（{'硬打斷' if p['hard'] else '軟插入'}／{p['kind']}）{p['text']}"
        for p in _pair_interventions(events) if p["outcome"] == "spoken"]
    interventions = "\n".join(intervention_lines) if intervention_lines else "（無）"
    return (f"## 逐字稿\n{transcript or '（無）'}\n\n"
            f"## 主席介入紀錄\n{interventions}")


def _call_minutes_llm(events: list[Event]) -> dict:
    body = {
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "messages": [{"role": "system", "content": MINUTES_SYSTEM},
                     {"role": "user", "content": build_minutes_prompt(events)}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_api_key()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read())
    return json.loads(payload["choices"][0]["message"]["content"])


def render_minutes(payload: dict) -> str:
    decisions = payload.get("decisions") or []
    todos = payload.get("todos") or []
    unresolved = payload.get("unresolved") or []
    stances = payload.get("stances") or {}

    lines = ["# 會議產出", "", "## 決議事項"]
    lines += _render_table(
        ["誰", "做什麼", "何時前"],
        [[d.get("who", ""), d.get("what", ""), d.get("by", "")] for d in decisions])

    lines += ["", "## 待辦事項"]
    lines += _render_table(
        ["負責人", "任務"],
        [[t.get("owner", ""), t.get("task", "")] for t in todos])

    lines += ["", "## 未解決事項"]
    lines += _render_table(
        ["議題", "主席裁決理由"],
        [[u.get("topic", ""), u.get("chair_ruling", "")] for u in unresolved])

    lines += ["", "## 每人立場摘要"]
    if stances:
        lines += [f"- **{name}**：{summary}" for name, summary in stances.items()]
    else:
        lines.append("（無）")

    return "\n".join(lines) + "\n"


# ── IO 入口 ─────────────────────────────────────────────────────────


def write_minutes(session, out_dir: Path) -> tuple[Path, Path]:
    """寫出 B（主持記錄）與 A（會議產出）。回傳 (host_path, minutes_path)。

    `ts` 理想上該與 `summary()` 寫 log／events.jsonl 用的同一個 `int(time.time())`，
    但 `live.py` 不歸這張工單改，`summary()` 目前沒有把 ts 傳進來——這裡只能各自取
    當下時間，實務上與 summary() 那次呼叫相差不到一秒，檔名時間戳可能差 1 秒。
    """
    out_dir = Path(out_dir)
    prepare_private_dir(out_dir)
    ts = int(time.time())
    host_path = out_dir / f"meeting-{ts}.host.md"
    minutes_path = out_dir / f"meeting-{ts}.minutes.md"

    host_path = write_protected_text(
        host_path, render_host_record(session.events, session.st.participants),
        artifact_type="host_record")

    try:
        payload = _call_minutes_llm(session.events)
        minutes_text = render_minutes(payload)
    except Exception as e:  # noqa: BLE001 — A 失敗不能拖垮 B，也不能讓 summary() 拋例外
        minutes_text = f"# 會議產出\n\n生成失敗：{type(e).__name__}: {e}"
    minutes_path = write_protected_text(minutes_path, minutes_text, artifact_type="minutes")

    return host_path, minutes_path
