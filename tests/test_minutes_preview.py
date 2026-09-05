"""會議產出預覽（`Session.watch_minutes`）：會議進行中定期發 `minutes` 事件，
`final: False`——跟正式收尾（`summary()` → `_emit_minutes`，`final: True`，
見 test_events.py 的 T3a）共用同一個 event kind，靠 `final` 分辨，不新增管道。

涵蓋：
(a) 逐字稿夠長時，預覽真的呼叫了 minutes 的 LLM（mock 掉，絕不打真 API），
    並發出 final=False、帶 decisions/todos/unresolved 的 minutes 事件；
(b) 逐字稿太短（低於 MINUTES_PREVIEW_MIN_UTTERANCES）不呼叫、不 emit；
(c) CancelledError 是收尾路徑，不能被自己的 except Exception 吃掉（比照
    test_glossary.py 的同名驗收）；
(d) 正常會議結束路徑（summary()）不受這支新迴圈影響，仍然發 final=True。
"""
import asyncio

import pytest

from meeting_host import live
from meeting_host.live import Session, summary
from meeting_host.state import MeetingState


def _session(participants=("A", "B")) -> Session:
    """與 test_live_wiring.py 的 `_session()` 同款寫法，這裡自己開一份小的，
    不跨測試檔互相 import（這個 repo 的既有測試檔一律各自建自己的 helper）。"""
    return Session(MeetingState(topic="t", duration_min=30, participants=list(participants)))


def _add_utterances(session, n):
    for i in range(n):
        speaker = "A" if i % 2 == 0 else "B"
        session.emit("utterance", {"speaker": speaker, "text": f"發言{i}",
                                    "start": float(i), "end": float(i) + 0.5})


def _run_watch_minutes_until(session, monkeypatch, min_events):
    """跑 watch_minutes，等到收滿 `min_events` 筆 minutes 事件或逾時就取消、回傳收到的事件。"""
    monkeypatch.setattr(live, "MINUTES_PREVIEW_INTERVAL_S", 0.001)
    got = []
    session.subscribers.append(lambda e: got.append(e))

    async def go():
        task = asyncio.create_task(session.watch_minutes())
        deadline = asyncio.get_running_loop().time() + 3.0
        while sum(1 for e in got if e.kind == "minutes") < min_events:
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


def test_preview_calls_minutes_llm_and_emits_final_false(monkeypatch):
    call_log = []

    def fake_call(events):
        call_log.append(len(events))
        return {
            "decisions": [{"who": "A", "what": "訂場地", "by": "週五"}],
            "todos": [{"owner": "B", "task": "寄邀請信"}],
            "unresolved": [{"topic": "預算上限", "chair_ruling": "留到下次"}],
            "stances": {"A": "支持辦兩天"},
        }

    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm", fake_call)

    session = _session()
    _add_utterances(session, live.MINUTES_PREVIEW_MIN_UTTERANCES)

    got = _run_watch_minutes_until(session, monkeypatch, min_events=1)
    minutes_events = [e for e in got if e.kind == "minutes"]

    assert call_log, "watch_minutes 沒有真的呼叫 minutes 的 LLM"
    assert len(minutes_events) >= 1
    data = minutes_events[0].data
    assert data["final"] is False
    assert data["decisions"] == [{"who": "A", "what": "訂場地", "by": "週五"}]
    assert data["todos"] == [{"owner": "B", "task": "寄邀請信"}]
    assert data["unresolved"] == [{"topic": "預算上限", "chair_ruling": "留到下次"}]
    # 預覽只帶結構化清單，不寫檔、不帶路徑欄位——跟 final=True 那份形狀不同
    assert "host_md" not in data and "minutes_path" not in data


def test_preview_skips_llm_call_when_transcript_too_short(monkeypatch):
    call_log = []
    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm",
                        lambda events: call_log.append(1) or {})

    session = _session()
    _add_utterances(session, live.MINUTES_PREVIEW_MIN_UTTERANCES - 1)
    monkeypatch.setattr(live, "MINUTES_PREVIEW_INTERVAL_S", 0.001)
    got = []
    session.subscribers.append(lambda e: got.append(e))

    async def go():
        task = asyncio.create_task(session.watch_minutes())
        await asyncio.sleep(0.05)  # 讓好幾個 tick 都有機會跑
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert not call_log, "逐字稿太短，不該呼叫 LLM"
    assert not [e for e in got if e.kind == "minutes"]


def test_cancellation_is_not_swallowed(monkeypatch):
    """比照 test_glossary.py 的同名驗收：CancelledError 是收尾路徑，不能被
    watch_minutes 自己的 except Exception 吃掉。"""
    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm",
                        lambda events: {"decisions": [], "todos": [], "unresolved": []})
    monkeypatch.setattr(live, "MINUTES_PREVIEW_INTERVAL_S", 0.001)
    session = _session()
    _add_utterances(session, live.MINUTES_PREVIEW_MIN_UTTERANCES)

    async def drive():
        task = asyncio.create_task(session.watch_minutes())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())


# ── 正常收尾路徑：final=True 不受這支新的預覽迴圈影響 ──────────────────────


def test_summary_still_emits_final_true(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm",
                        lambda events: {"decisions": [], "todos": [],
                                        "unresolved": [], "stances": {}})
    session = _session(("A",))
    session.emit("utterance", {"speaker": "A", "text": "hi", "start": 0.0, "end": 1.0})

    summary(session)

    assert session.events[-1].kind == "minutes"
    assert session.events[-1].data["final"] is True
