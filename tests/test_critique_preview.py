"""T-G：AI 主席的「心聲」（`critique.py` ＋ `Session.watch_critique`）。

面板顯示文字是「心聲」，內部模組名／event kind／旗標仍叫 critique
（2026-09-05 中途拍板，理由見 critique.py 模組 docstring）。

仿照 `tests/test_minutes_preview.py` 的寫法，涵蓋：
(a) `build_critique_prompt()` 純函式輸出格式正確（不打真實 API）；
(b) 逐字稿夠長時，`watch_critique` 真的呼叫了 critique 的 LLM（mock 掉），
    發出 `ai_critique` 事件，帶 `meeting`／`participants`（物件，人名→評語）；
(c) 逐字稿太短（低於 `CRITIQUE_MIN_UTTERANCES`）不呼叫、不 emit；
(d) `CancelledError` 是收尾路徑，不能被自己的 except Exception 吃掉；
(e) 會議已進入收尾（`session.ending=True`）時飛行中的呼叫不能再補發，比照
    `watch_minutes` 的同一條回歸測試；
(f) **保險栓（這批最重要的驗收項）**：`--no-critique` 或 `--no-llm` 任一開啟時，
    `main_async` 真正組出來的 `tasks` 清單裡沒有 `watch_critique` 這個協程；
    對照組驗證兩個旗標都沒開時它確實有被排進去（防止判斷式寫反、假綠燈）。
"""
import argparse
import asyncio

import pytest

from meeting_host import live
from meeting_host.critique import (
    CRITIQUE_TAIL_WINDOW_EVENTS,
    CritiqueStats,
    ParticipantSpeechStat,
    _compact_transcript,
    build_critique_prompt,
)
from meeting_host.events import Event
from meeting_host.live import Session
from meeting_host.state import MeetingState


def _session(participants=("A", "B")) -> Session:
    return Session(MeetingState(topic="t", duration_min=30, participants=list(participants)))


def _add_utterances(session, n):
    for i in range(n):
        speaker = "A" if i % 2 == 0 else "B"
        session.emit("utterance", {"speaker": speaker, "text": f"發言{i}",
                                    "start": float(i), "end": float(i) + 0.5})


def _stats(participants=(), now=0.0, remaining=0.0, chair_seconds=0.0,
           chair_interventions=0):
    """組一份 `CritiqueStats` 給純函式測試用。`participants` 是
    `(name, spoke_seconds, silent_seconds, absent)` 四元組的序列；只給名字
    （字串）時其餘欄位補 0.0／False。"""
    stats = []
    for p in participants:
        if isinstance(p, str):
            stats.append(ParticipantSpeechStat(name=p, spoke_seconds=0.0,
                                                silent_seconds=0.0, absent=False))
        else:
            name, spoke, silent, absent = p
            stats.append(ParticipantSpeechStat(name=name, spoke_seconds=spoke,
                                                silent_seconds=silent, absent=absent))
    return CritiqueStats(now=now, remaining_seconds=remaining, participants=stats,
                          chair_seconds=chair_seconds, chair_interventions=chair_interventions)


# ── (a) build_critique_prompt：純函式 ──────────────────────────────────


def test_build_critique_prompt_includes_roster_and_transcript():
    events = [Event("utterance", 5.0, {"speaker": "Alex", "text": "先講一下時程",
                                        "start": 4.0, "end": 5.0})]
    prompt = build_critique_prompt(events, _stats(["Alex", "Bob"], now=5.0))
    assert "## 與會者" in prompt
    assert "Alex、Bob" in prompt
    assert "## 逐字稿" in prompt
    assert "先講一下時程" in prompt


def test_build_critique_prompt_handles_empty_events_and_participants():
    prompt = build_critique_prompt([], _stats([]))
    assert "（無）" in prompt


# ── (a') 交付2：發言統計／主席介入紀錄兩節格式 ──────────────────────────


def test_build_critique_prompt_stats_table_and_no_intervention_placeholder():
    """沒有任何介入時，「## 主席介入紀錄」節不能省略，要印固定占位句——
    否則 LLM 分不清「沒介入」跟「沒餵資料」。"""
    events = [
        Event("utterance", 10.0, {"speaker": "周葵", "text": "先講一下時程",
                                   "start": 9.0, "end": 10.0}),
        Event("utterance", 20.0, {"speaker": "林同", "text": "我覺得可以",
                                   "start": 19.0, "end": 20.0}),
    ]
    stats = _stats(
        [("周葵", 100.0, 5.0, False), ("林同", 50.0, 2.0, False)],
        now=200.0, remaining=1600.0, chair_seconds=9.0, chair_interventions=3,
    )
    prompt = build_critique_prompt(events, stats)

    assert "## 發言統計" in prompt
    assert "會議已進行 03:20，議程剩 26:40" in prompt
    assert "| 周葵 | 01:40 | 63% | 1 | 00:05 |" in prompt
    assert "| 林同 | 00:50 | 31% | 1 | 00:02 |" in prompt
    assert "| 主席 | 00:09 | 6% | 3 次介入 | — |" in prompt

    assert "## 主席介入紀錄" in prompt
    assert "（目前為止主席沒有介入）" in prompt


def test_build_critique_prompt_marks_absent_participant_and_overtime():
    """有已離會的人：名字加「（已離會）」，距上次發言欄寫「—」，但發言時長／
    佔比／則數照列不省略。議程超時（remaining 為負）要顯示「已超時」。"""
    stats = _stats(
        [("沈禾", 30.0, 999.0, True)],
        now=100.0, remaining=-20.0, chair_seconds=3.0, chair_interventions=1,
    )
    prompt = build_critique_prompt([], stats)
    assert "議程剩 已超時 00:20" in prompt
    assert "| 沈禾（已離會） | 00:30 | 91% | 0 | — |" in prompt


def test_build_critique_prompt_intervention_lines_hard_and_soft():
    """[時間] 硬打斷/軟插入【kind→target】「原文」；target 為 None 只印【kind】；
    只列 outcome=="spoken"，作廢/失敗的不列。"""
    events = [
        Event("queued", 8.0, {"kind": "有人被冷落", "target": "沈禾",
                               "text": "沈禾好像還沒說到話，想聽聽你的看法。", "hard": False}),
        Event("spoken", 8.5, {"kind": "有人被冷落", "target": "沈禾",
                               "text": "沈禾好像還沒說到話，想聽聽你的看法。"}),
        Event("queued", 15.0, {"kind": "議程超時", "target": None,
                                "text": "時間差不多了，我們加快一點。", "hard": True}),
        Event("dropped", 15.2, {"kind": "議程超時", "target": None,
                                 "text": "時間差不多了，我們加快一點。", "reason": "收尾"}),
        Event("queued", 19.0, {"kind": "離題", "target": None,
                                "text": "我們先回到報表匯出這一題。", "hard": True}),
        Event("spoken", 19.3, {"kind": "離題", "target": None,
                                "text": "我們先回到報表匯出這一題。"}),
    ]
    prompt = build_critique_prompt(events, _stats([], now=20.0))
    assert '[00:08] 軟插入【有人被冷落→沈禾】「沈禾好像還沒說到話，想聽聽你的看法。」' in prompt
    assert '[00:19] 硬打斷【離題】「我們先回到報表匯出這一題。」' in prompt
    # 被作廢（dropped）的那筆不該出現在輸出裡
    assert "時間差不多了" not in prompt


# ── (b)(c)(d)(e) watch_critique 背景迴圈 ────────────────────────────────


def _run_watch_critique_until(session, monkeypatch, min_events):
    monkeypatch.setattr(live, "CRITIQUE_INTERVAL_S", 0.001)
    got = []
    session.subscribers.append(lambda e: got.append(e))

    async def go():
        task = asyncio.create_task(session.watch_critique())
        deadline = asyncio.get_running_loop().time() + 3.0
        while sum(1 for e in got if e.kind == "ai_critique") < min_events:
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.002)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    return got


def test_watch_critique_calls_llm_and_emits_ai_critique(monkeypatch):
    """`participants` 是物件（人名→評語），不是陣列——2026-09-05 中途拍板的
    schema（見 critique.CRITIQUE_SYSTEM），不是工作單原始草稿那個陣列形狀。"""
    call_log = []

    def fake_call(events, stats):
        call_log.append((len(events), [p.name for p in stats.participants]))
        return {
            "meeting": "討論在原地打轉，還沒有人願意先讓步",
            "participants": {"A": "一直重複同一個論點，沒有回應對方的疑慮"},
        }

    monkeypatch.setattr("meeting_host.critique._call_critique_llm", fake_call)

    session = _session()
    _add_utterances(session, live.CRITIQUE_MIN_UTTERANCES)

    got = _run_watch_critique_until(session, monkeypatch, min_events=1)
    critique_events = [e for e in got if e.kind == "ai_critique"]

    assert call_log, "watch_critique 沒有真的呼叫 critique 的 LLM"
    assert call_log[0][1] == ["A", "B"], "participants 名單沒有正確傳進去"
    assert len(critique_events) >= 1
    data = critique_events[0].data
    assert data["meeting"] == "討論在原地打轉，還沒有人願意先讓步"
    assert data["participants"] == {"A": "一直重複同一個論點，沒有回應對方的疑慮"}


def test_watch_critique_passes_correct_stats_content(monkeypatch):
    """(交付2/live.py 驗收項) watch_critique() 傳給 `_call_critique_llm` 的
    `CritiqueStats` 內容要跟 `self.st` 對得上——不是只斷言「有呼叫」，而是斷言
    呼叫參數本身：發言秒數、距上次發言、離會旗標、主席估算秒數／介入次數、
    剩餘時間全部來自同一份 `MeetingState`。"""
    captured = {}

    def fake_call(events, stats):
        captured["stats"] = stats
        captured["events"] = events
        return {"meeting": "", "participants": {}}

    monkeypatch.setattr("meeting_host.critique._call_critique_llm", fake_call)

    session = _session(("A", "B"))
    _add_utterances(session, live.CRITIQUE_MIN_UTTERANCES)
    session.st.absent.add("B")
    session.st.interventions.extend([1.0, 2.0])  # 2 次介入 → chair_seconds = 6.0

    _run_watch_critique_until(session, monkeypatch, min_events=1)

    assert "stats" in captured, "watch_critique 沒有真的呼叫 critique 的 LLM"
    stats = captured["stats"]
    now_at_call = session.now  # 呼叫發生在飛行途中，容忍些微時間誤差重新核對邏輯關係
    assert stats.chair_seconds == 6.0
    assert stats.chair_interventions == 2
    assert stats.remaining_seconds == pytest.approx(session.st.duration_min * 60 - stats.now, abs=1.0)

    by_name = {p.name: p for p in stats.participants}
    assert set(by_name) == {"A", "B"}
    assert by_name["A"].spoke_seconds == pytest.approx(session.st.spoke_seconds("A"))
    assert by_name["B"].spoke_seconds == pytest.approx(session.st.spoke_seconds("B"))
    assert by_name["A"].absent is False
    assert by_name["B"].absent is True, "session.st.absent 裡的人，stats 沒有標記 absent"


def test_watch_critique_skips_llm_call_when_transcript_too_short(monkeypatch):
    call_log = []
    monkeypatch.setattr("meeting_host.critique._call_critique_llm",
                        lambda events, stats: call_log.append(1) or {})

    session = _session()
    _add_utterances(session, live.CRITIQUE_MIN_UTTERANCES - 1)
    monkeypatch.setattr(live, "CRITIQUE_INTERVAL_S", 0.001)
    got = []
    session.subscribers.append(lambda e: got.append(e))

    async def go():
        task = asyncio.create_task(session.watch_critique())
        await asyncio.sleep(0.05)  # 讓好幾個 tick 都有機會跑
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert not call_log, "逐字稿太短，不該呼叫 LLM"
    assert not [e for e in got if e.kind == "ai_critique"]


def test_watch_critique_cancellation_is_not_swallowed(monkeypatch):
    monkeypatch.setattr("meeting_host.critique._call_critique_llm",
                        lambda events, stats: {"meeting": "", "participants": {}})
    monkeypatch.setattr(live, "CRITIQUE_INTERVAL_S", 0.001)
    session = _session()
    _add_utterances(session, live.CRITIQUE_MIN_UTTERANCES)

    async def drive():
        task = asyncio.create_task(session.watch_critique())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())


def test_watch_critique_suppressed_once_session_is_ending(monkeypatch):
    """比照 watch_minutes 的同一條回歸測試：LLM 呼叫飛行中若會議已經進入收尾，
    這筆批判觀察絕對不能再補發。"""
    import time as _time

    def slow_call(events, stats):
        _time.sleep(0.05)
        return {"meeting": "不該出現", "participants": {}}

    monkeypatch.setattr("meeting_host.critique._call_critique_llm", slow_call)
    monkeypatch.setattr(live, "CRITIQUE_INTERVAL_S", 0.001)

    session = _session()
    _add_utterances(session, live.CRITIQUE_MIN_UTTERANCES)
    got = []
    session.subscribers.append(lambda e: got.append(e))

    async def go():
        task = asyncio.create_task(session.watch_critique())
        await asyncio.sleep(0.01)
        session.ending = True
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert not [e for e in got if e.kind == "ai_critique"], (
        "session.ending=True 之後，批判觀察仍然發出了 ai_critique 事件"
    )


# ── (f) 保險栓：main_async 真正組出來的 tasks 清單 ──────────────────────────
#
# main_async 本身重到不能直接跑（要連 Discord／ElevenLabs）——這個 repo 既有的
# 慣例是完全不在測試裡建構真的 MeetingBot（tests/ 底下零筆），連專門重現收尾
# 行為的 tests/harness/live_shutdown_driver.py 都是另起一支不連 Discord 的骨架，
# 不直接呼叫 production 的 main_async。
#
# 這裡改用最小侵入的方式直接驗證 production 的 main_async：
#   - 只換掉 MeetingBot（唯一真的會連網路的建構子），STTPool／build_voice／
#     Earcon／build_hello_gate 都是真正的 production 物件（皆已確認建構時
#     不連網路，也不需要真的環境變數值——見開工前棕地探勘）。
#   - 換掉 live.shutdown 來攔截 main_async 在 finally 裡真正組出來的 tasks
#     清單，同時避免真的跑 summary()/bot.close() 等收尾邏輯（那些已有
#     tests/test_live_shutdown.py 專門覆蓋，不是這批要驗的東西）。
#   - 用 asyncio.wait_for 給一個很短的逾時，模擬「demo 現場按 Ctrl-C」的
#     取消路徑——cancel 會級聯到 asyncio.gather(*tasks)，跟正式收尾走的是
#     同一條程式碼路徑。


def _live_args(**overrides):
    base = dict(topic="t", duration=1, phase="發散期", style=None, auto_phase=None,
                channel=None, keyterms=None, no_llm=False, no_critique=False,
                say_hello=False, spectator_port=0, spectator_token="", view_token="",
                public_read=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeMeetingBot:
    """唯一被替換掉的建構子：真的 MeetingBot.start() 會連 Discord，不能在測試裡跑。"""

    def __init__(self, pool, channel_id=None, state=None):
        self.pool = pool
        self.channel_id = channel_id
        self.state = state

    async def start(self, token):
        await asyncio.Event().wait()  # 永遠不回來，等外部 cancel

    async def close(self):
        return


def _task_names(tasks):
    """回傳每個 Task 底層 coroutine 的函式名稱，用來檢查有沒有排進 watch_critique。"""
    names = []
    for t in tasks:
        coro = t.get_coro()
        names.append(getattr(getattr(coro, "cr_code", None), "co_name", repr(coro)))
    return names


def _drive_main_async_and_capture_tasks(monkeypatch, args):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.setattr(live, "MeetingBot", _FakeMeetingBot)

    captured = {}

    async def fake_shutdown(session, bot, tasks):
        captured["tasks"] = tasks
        for t in tasks:
            if not t.done():
                t.cancel()

    monkeypatch.setattr(live, "shutdown", fake_shutdown)

    async def drive():
        try:
            await asyncio.wait_for(live.main_async(args), timeout=0.3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    asyncio.run(drive())
    assert "tasks" in captured, "main_async 沒有走到 shutdown()，測試沒有真的驗到 tasks 清單"
    return captured["tasks"]


@pytest.mark.parametrize("overrides", [
    {"no_critique": True},
    {"no_llm": True},
    {"no_llm": True, "no_critique": True},
], ids=["no-critique-only", "no-llm-only", "both"])
def test_insurance_switch_keeps_watch_critique_out_of_task_list(monkeypatch, overrides):
    """保險栓最重要的驗收項：--no-critique 或 --no-llm 任一開啟時，watch_critique
    絕對不能出現在 main_async 真正組出來的 tasks 清單裡。"""
    tasks = _drive_main_async_and_capture_tasks(monkeypatch, _live_args(**overrides))
    names = _task_names(tasks)
    assert "watch_critique" not in names, f"保險栓沒生效，watch_critique 仍在 tasks 裡：{names}"


def test_watch_critique_is_scheduled_when_neither_flag_is_set(monkeypatch):
    """對照組：兩個旗標都沒開時 watch_critique 真的有被排進去——防止保險栓的
    判斷式寫反、或整支任務悄悄消失變成假綠燈（例如上面那條測試永遠通過但
    其實 watch_critique 從來沒被排過）。"""
    tasks = _drive_main_async_and_capture_tasks(monkeypatch, _live_args())
    names = _task_names(tasks)
    assert "watch_critique" in names, f"對照組：兩個旗標都沒開，watch_critique 應該在 tasks 裡：{names}"
    # 順手核對其他既有 LLM 背景迴圈沒有被這批改動波及（棕地紀律：只加不改）。
    for expected in ("watch_slow", "watch_glossary", "watch_minutes"):
        assert expected in names, f"{expected} 不見了，這批不該動到既有的 --no-llm 任務清單"


# ── 交付3：`_compact_transcript()` 長會議逐字稿壓縮（純函式，直接單元測試）──
#
# 用假資料直接構造超過門檻的情境，不用真的餵 12,000 字——用事件則數門檻
# （`CRITIQUE_COMPACT_EVENT_THRESHOLD` = 300）比較好構造。


def _utt(i, speaker="A", text=None):
    text = text if text is not None else f"發言{i}"
    return Event("utterance", float(i), {"speaker": speaker, "text": text,
                                          "start": float(i), "end": float(i) + 0.5})


def test_compact_transcript_returns_identical_output_below_threshold():
    """未達門檻：逐字比對，輸出跟原樣完全一致（demo 的 5 分鐘會議走這一支）。"""
    events = [_utt(i, "A" if i % 2 == 0 else "B") for i in range(10)]
    result = _compact_transcript(events, now=9.0)
    assert result == [e for e in events if e.kind == "utterance"]


def test_compact_transcript_keeps_tail_window_and_marks_dropped_range():
    """超過事件則數門檻（純用短文字避免誤觸發錨點1）：最後
    `CRITIQUE_TAIL_WINDOW_EVENTS` 則逐字保留，更早的整段拿掉、換一行標記。"""
    n = 320
    events = [_utt(i, "A" if i % 2 == 0 else "B", text="嗯") for i in range(n)]
    result = _compact_transcript(events, now=float(n - 1))

    tail_start = n - CRITIQUE_TAIL_WINDOW_EVENTS  # = 200
    kinds = [e.kind for e in result]
    assert kinds.count("critique_gap") == 1, "沒有錨點時，被拿掉的段落應該合成一筆標記"
    assert kinds[0] == "critique_gap"
    marker = result[0]
    assert f"共 {tail_start} 則發言略去" in marker.data["text"]
    assert "見上方發言統計" in marker.data["text"]

    tail = result[1:]
    assert len(tail) == CRITIQUE_TAIL_WINDOW_EVENTS
    assert tail == events[tail_start:], "尾窗必須逐字保留、順序不變"


def test_compact_transcript_restores_both_anchor_types():
    """兩類錨點：①每人第一則長度足夠的發言 ②已說出口的介入前緊鄰兩則發言，
    即使落在被拿掉的舊段裡也要插回原位置。"""
    n = 305
    events = [_utt(i, "A" if i % 2 == 0 else "B", text="嗯") for i in range(n)]
    # 錨點類 1：A、B 各自第一則「夠長」的發言（預設文字太短不會觸發）。
    events[0] = _utt(0, "A", text="這是甲說的第一句真心話比較長")
    events[1] = _utt(1, "B", text="這是乙說的第一句真心話比較長")
    # 錨點類 2 的候選：緊鄰在介入之前的兩則發言，用好認的文字覆蓋。
    events[48] = _utt(48, "A", text="錨點二之前一")
    events[49] = _utt(49, "B", text="錨點二之前二")
    # 一筆已說出口的介入，時間點在第 48/49 則發言之後、尾窗之前。
    events.append(Event("queued", 49.5, {"kind": "離題", "target": None,
                                          "text": "我們先回到主題。", "hard": False}))
    events.append(Event("spoken", 50.0, {"kind": "離題", "target": None,
                                          "text": "我們先回到主題。"}))

    result = _compact_transcript(events, now=float(n - 1))
    texts = [e.data.get("text") for e in result]

    assert "這是甲說的第一句真心話比較長" in texts, "A 的錨點1沒有被保留"
    assert "這是乙說的第一句真心話比較長" in texts, "B 的錨點1沒有被保留"
    assert "錨點二之前一" in texts, "介入前緊鄰第一則（錨點2）沒有被保留"
    assert "錨點二之前二" in texts, "介入前緊鄰第二則（錨點2）沒有被保留"

    # 尾窗（最後 120 則）仍然逐字保留。
    tail_start = n - CRITIQUE_TAIL_WINDOW_EVENTS
    utterances_only = [e for e in events if e.kind == "utterance"]
    assert result[-CRITIQUE_TAIL_WINDOW_EVENTS:] == utterances_only[tail_start:]

    # 被拿掉的部分仍然留下至少一筆標記行（舊段沒有被錨點完全填滿）。
    assert any(e.kind == "critique_gap" for e in result)


def test_compact_transcript_dedupes_consecutive_identical_utterances():
    """同一人連續兩則內容逐字相同 → 只留一則；相似但不同的不能被誤刪。"""
    n = 301
    events = [_utt(i, "A" if i % 2 == 0 else "B", text="嗯") for i in range(n)]
    # 尾窗（最後120則）裡製造一組完全相同的連續重複，跟緊接著一組「相似但不同」的對照。
    dup_at = n - 5
    events[dup_at] = _utt(dup_at, "A", text="這句話重複了")
    events[dup_at + 1] = _utt(dup_at + 1, "A", text="這句話重複了")
    events[dup_at + 2] = _utt(dup_at + 2, "A", text="這句話重複了嗎")  # 相似但不同，不可刪

    result = _compact_transcript(events, now=float(n - 1))
    texts = [e.data.get("text") for e in result if e.kind == "utterance"]

    assert texts.count("這句話重複了") == 1, "完全相同的連續發言只該留一則"
    assert texts.count("這句話重複了嗎") == 1, "相似但不同的發言不可以被誤刪"
