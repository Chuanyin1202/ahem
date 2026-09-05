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
from meeting_host.critique import build_critique_prompt
from meeting_host.live import Session
from meeting_host.state import MeetingState


def _session(participants=("A", "B")) -> Session:
    return Session(MeetingState(topic="t", duration_min=30, participants=list(participants)))


def _add_utterances(session, n):
    for i in range(n):
        speaker = "A" if i % 2 == 0 else "B"
        session.emit("utterance", {"speaker": speaker, "text": f"發言{i}",
                                    "start": float(i), "end": float(i) + 0.5})


# ── (a) build_critique_prompt：純函式 ──────────────────────────────────


def test_build_critique_prompt_includes_roster_and_transcript():
    from meeting_host.events import Event

    events = [Event("utterance", 5.0, {"speaker": "Alex", "text": "先講一下時程",
                                        "start": 4.0, "end": 5.0})]
    prompt = build_critique_prompt(events, ["Alex", "Bob"])
    assert "## 與會者" in prompt
    assert "Alex、Bob" in prompt
    assert "## 逐字稿" in prompt
    assert "先講一下時程" in prompt


def test_build_critique_prompt_handles_empty_events_and_participants():
    prompt = build_critique_prompt([], [])
    assert "（無）" in prompt


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

    def fake_call(events, participants):
        call_log.append((len(events), list(participants)))
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


def test_watch_critique_skips_llm_call_when_transcript_too_short(monkeypatch):
    call_log = []
    monkeypatch.setattr("meeting_host.critique._call_critique_llm",
                        lambda events, participants: call_log.append(1) or {})

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
                        lambda events, participants: {"meeting": "", "participants": {}})
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

    def slow_call(events, participants):
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
