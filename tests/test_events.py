"""T-B：事件匯流排。

涵蓋（見 T-B 驗收標準）：
(a) Event 可序列化；(b) subscriber 例外不影響其他 subscriber；
(c) consume 收到 Utterance 後依序含 utterance／speaking(active=False)／share；
(d) slow_score 事件含 pros/cons/admissible/reason；
(e) summary() 決定的 events.jsonl 路徑寫出後每行可還原且與 session.events 一致；
(f) spectator／minutes 模組不存在時對應路徑印略過訊息、不拋例外；
(g) T3a：summary() 會 emit `minutes`（含兩份 md 內容與四個路徑），
    minutes 模組缺席時仍要發、內容給空字串並帶 error。
"""
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from meeting_host.events import Event
from meeting_host.live import (
    Session,
    _try_import_spectator_serve,
    _try_write_minutes,
    _write_events_jsonl,
    summary,
)
from meeting_host.state import MeetingState, Utterance


class FakePool:
    """只吐固定幾個事件就結束的假 STT pool（與 test_live_wiring.py 同款）。"""

    def __init__(self, events):
        self.events = events

    async def utterances(self):
        for ev in self.events:
            yield ev


class FakeChair:
    pending = None
    playing = None

    def __init__(self):
        self.requested = []

    def request(self, iv):
        self.requested.append(iv)
        return True


# ── (a) Event 可序列化 ──────────────────────────────────────────────


def test_event_asdict_is_json_serializable():
    e = Event(kind="utterance", t=1.5, data={"speaker": "A", "text": "hi"})
    dumped = json.dumps(dataclasses.asdict(e), ensure_ascii=False)
    restored = json.loads(dumped)
    assert restored == {"kind": "utterance", "t": 1.5, "data": {"speaker": "A", "text": "hi"}}


# ── (b) subscriber 例外不影響其他 subscriber ────────────────────────


def test_subscriber_exception_does_not_block_others():
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))
    received = []

    def bad(event):
        raise RuntimeError("boom")

    def good(event):
        received.append(event)

    session.subscribers.append(bad)
    session.subscribers.append(good)
    session.emit("utterance", {"speaker": "A", "text": "hi", "start": 0.0, "end": 1.0})

    assert len(received) == 1
    assert received[0].kind == "utterance"


# ── (c) consume(Utterance) → utterance／speaking(active=False)／share ──


def test_consume_utterance_emits_utterance_speaking_share_in_order():
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
    asyncio.run(session.consume(FakePool([Utterance("A", "hi", 0.0, 1.0)])))

    kinds = [e.kind for e in session.events]
    assert kinds.count("utterance") == 1
    assert kinds.count("speaking") == 1
    assert kinds.count("share") == 1
    i_u, i_s, i_sh = kinds.index("utterance"), kinds.index("speaking"), kinds.index("share")
    assert i_u < i_s < i_sh

    assert session.events[i_u].data == {"speaker": "A", "text": "hi", "start": 0.0, "end": 1.0}
    assert session.events[i_s].data == {"speaker": "A", "active": False}
    assert "A" in session.events[i_sh].data and "主席" in session.events[i_sh].data


def test_consume_emits_meeting_when_participant_count_changes():
    """participants 從 0 變成 2（Session 建構時已餵入名單）→ 第一次 consume 要重送 meeting。"""
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
    assert session._last_participant_count == 0
    asyncio.run(session.consume(FakePool([Utterance("A", "hi", 0.0, 1.0)])))

    meeting_events = [e for e in session.events if e.kind == "meeting"]
    assert len(meeting_events) == 1
    data = dict(meeting_events[0].data)
    start_epoch = data.pop("start_epoch")  # T16：wall-clock 值隨測試執行時間變動，不能精確比對
    assert isinstance(start_epoch, float)
    assert data == {
        "topic": "t", "duration_min": 30, "phase": "發散期", "participants": ["A", "B"],
    }
    assert session._last_participant_count == 2


# ── (d) slow_score 含 pros/cons/admissible/reason ───────────────────


def test_run_slow_score_emits_admissible_result_with_pros_cons(monkeypatch):
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "hello", 0.0, 2.0))
    st.add(Utterance("B", "hi", 2.0, 4.0))
    session = Session(st)
    session.chair = FakeChair()

    # T29：慢路是兩次呼叫，判斷不再帶話術——話術由 slow_path.phrase() 產生，
    # 所以這裡要各 stub 一支。
    fake_result = {
        "positive": 4, "negative": 1, "none": 1, "type": "離題", "verdict": "正向介入",
        "pros": ["p1", "p2"], "cons": ["c1", "c2"],
    }
    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: fake_result)
    monkeypatch.setattr("meeting_host.slow_path.phrase", lambda *a, **kw: "請回到主題")

    asyncio.run(session._run_slow_score(0))

    slow_events = [e for e in session.events if e.kind == "slow_score"]
    assert len(slow_events) == 1
    data = slow_events[0].data
    assert data["pros"] == ["p1", "p2"]
    assert data["cons"] == ["c1", "c2"]
    assert data["admissible"] is True
    assert data["reason"] == ""
    assert data["positive"] == 4
    assert data["type"] == "離題"
    assert data["verdict"] == "正向介入"
    assert data["utterance"] == "請回到主題"
    # 被接受的候選也要排入 queued
    assert [e.kind for e in session.events if e.kind == "queued"] == ["queued"]


def test_run_slow_score_emits_even_when_inadmissible(monkeypatch):
    """type=無 即使被壓掉，也要送出 slow_score 事件（admissible=False）。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "hello", 0.0, 2.0))
    st.add(Utterance("B", "hi", 2.0, 4.0))
    session = Session(st)
    session.chair = FakeChair()

    fake_result = {
        "positive": 3, "negative": 1, "none": 2, "type": "無", "verdict": "正向介入",
        "utterance": "", "pros": ["p1"], "cons": ["c1"],
    }
    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: fake_result)

    asyncio.run(session._run_slow_score(0))

    slow_events = [e for e in session.events if e.kind == "slow_score"]
    assert len(slow_events) == 1
    assert slow_events[0].data["admissible"] is False
    assert slow_events[0].data["reason"] == "type=無"
    assert session.chair.requested == []  # 被壓掉，不會排入


# ── (d2) T29：慢路兩次呼叫的接線 ─────────────────────────────────────
#
# 這一節守的是「拆成兩次呼叫」之後 `_run_slow_score` 的四個性質：
# 成本（閘門擋下就不打第二次）、事件數（一次評分仍只一筆 slow_score）、
# 誠實（失敗與 TOCTOU 各有自己的 reason）、以及觀戰 UI 賴以配對的事件相鄰性。


def _two_person_session():
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "hello", 0.0, 2.0))
    st.add(Utterance("B", "hi", 2.0, 4.0))
    session = Session(st)
    session.chair = FakeChair()
    return st, session


_JUDGE_YES = {"positive": 4, "negative": 1, "none": 1, "type": "離題",
              "verdict": "正向介入", "pros": ["p1"], "cons": ["c1"]}


def test_run_slow_score_does_not_spend_a_phrase_call_when_the_first_gate_blocks(monkeypatch):
    """成本性質：第一關擋下就不打第二次呼叫。拆兩次呼叫的整個前提就是
    「話術只花在真的要開口的那 6-12 次上」——這裡破了，一場會議就要多付
    一百多次呼叫。"""
    st, session = _two_person_session()
    calls = []
    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: {
        **_JUDGE_YES, "type": "無"})   # 第一關的 type=無 分支
    monkeypatch.setattr("meeting_host.slow_path.phrase",
                        lambda *a, **kw: calls.append(1) or "不該被呼叫")

    asyncio.run(session._run_slow_score(0))

    assert calls == []
    data = [e for e in session.events if e.kind == "slow_score"][0].data
    assert (data["admissible"], data["reason"]) == (False, "type=無")
    assert data["utterance"] == ""            # 沒問過就是空的，不是「模型寫不出來」
    assert data["utterance_seconds"] is None  # 沒打第二次呼叫
    assert session.chair.requested == []


def test_run_slow_score_gives_up_honestly_when_the_phrase_call_fails(monkeypatch):
    """話術呼叫爆掉：不退回罐頭句（那正是這次要修的東西）、不讓例外打死
    watch_slow、reason 要指出真正發生的事。"""
    st, session = _two_person_session()

    def boom(*a, **kw):
        raise RuntimeError("模擬 API 失敗")

    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: dict(_JUDGE_YES))
    monkeypatch.setattr("meeting_host.slow_path.phrase", boom)

    asyncio.run(session._run_slow_score(0))

    slow_events = [e for e in session.events if e.kind == "slow_score"]
    assert len(slow_events) == 1              # 判斷／話術不是兩筆事件
    data = slow_events[0].data
    assert (data["admissible"], data["reason"]) == (False, "話術失敗")
    assert data["utterance"] == ""
    assert isinstance(data["utterance_seconds"], float)  # 打過了，只是失敗
    assert session.chair.requested == []
    assert not [e for e in session.events if e.kind == "queued"]


def test_run_slow_score_drops_an_over_long_utterance_instead_of_truncating(monkeypatch):
    from meeting_host.slow_path import UTTERANCE_HARD_CAP
    st, session = _two_person_session()
    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: dict(_JUDGE_YES))
    monkeypatch.setattr("meeting_host.slow_path.phrase",
                        lambda *a, **kw: "長" * (UTTERANCE_HARD_CAP + 5))

    asyncio.run(session._run_slow_score(0))

    data = [e for e in session.events if e.kind == "slow_score"][0].data
    assert (data["admissible"], data["reason"]) == (False, "話術過長")
    assert len(data["utterance"]) == UTTERANCE_HARD_CAP + 5  # 原文照留，事件檔要看得到被丟掉的是什麼
    assert session.chair.requested == []


def test_run_slow_score_rechecks_the_world_after_the_phrase_call(monkeypatch):
    """TOCTOU：話術跑的那幾秒快路先開口了。第一關通過不代表現在還能講——
    Chair.request() 不檢查冷卻，這裡不重驗就會在 30 秒內連講兩次。"""
    st, session = _two_person_session()

    def phrase_and_meanwhile_fast_path_speaks(*a, **kw):
        st.interventions.append(session.now)   # 模擬話術生成期間快路出聲
        return "剛剛「便當」那段先放著，回到黑客松籌備。"

    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: dict(_JUDGE_YES))
    monkeypatch.setattr("meeting_host.slow_path.phrase", phrase_and_meanwhile_fast_path_speaks)

    asyncio.run(session._run_slow_score(0))

    data = [e for e in session.events if e.kind == "slow_score"][0].data
    assert (data["admissible"], data["reason"]) == (False, "冷卻(話術後)")
    assert data["utterance"]        # 話術有生出來，事件檔要留著它，才看得出被擋的是什麼
    assert session.chair.requested == []


def test_slow_score_and_its_queued_stay_adjacent_even_though_a_call_sits_between(monkeypatch):
    """觀戰 UI 的三態配對靠「admissible 的 slow_score 後面緊接著就是它的 queued」
    （spectator/index.html handleEvent 開頭）。話術呼叫要跑好幾秒，這段期間
    會議照樣在發言；只要 emit 的順序寫錯（先 emit slow_score 再打話術），
    中間就會插進 utterance／fast_timer，每一筆 admissible 判斷都會被誤判成
    「受阻·主席忙碌中」。這裡讓話術呼叫期間真的 emit 一筆事件來守住這條。"""
    st, session = _two_person_session()

    def phrase_while_someone_talks(*a, **kw):
        session.emit("utterance", {"speaker": "A", "text": "還在講", "start": 5.0, "end": 6.0})
        return "剛剛「便當」那段先放著，回到黑客松籌備。"

    monkeypatch.setattr("meeting_host.slow_path.score", lambda *a, **kw: dict(_JUDGE_YES))
    monkeypatch.setattr("meeting_host.slow_path.phrase", phrase_while_someone_talks)

    asyncio.run(session._run_slow_score(0))

    kinds = [e.kind for e in session.events]
    i = kinds.index("slow_score")
    assert kinds[i + 1] == "queued", f"slow_score 與 queued 之間插了事件：{kinds}"
    assert kinds.index("utterance") < i  # 那筆插進來的事件排在 slow_score 之前


# ── (e) events.jsonl 每行可 json.loads 且 kind 與 session.events 一致 ──


def test_summary_returns_path_and_write_events_jsonl_matches_session_events(tmp_path, monkeypatch):
    """T3a 後 summary() 只回傳 events.jsonl 的目標路徑，內容由 `_write_events_jsonl()`
    在 shutdown 的第二次 drain 之後才寫（見 live.shutdown 的順序說明）。"""
    monkeypatch.chdir(tmp_path)
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    session = Session(st)
    session.emit("utterance", {"speaker": "A", "text": "hi", "start": 0.0, "end": 1.0})
    session.emit("share", {"A": 1.0, "主席": 0.0})

    events_path = summary(session)
    assert not events_path.exists()  # summary() 只決定路徑，不寫內容

    _write_events_jsonl(session, events_path)

    files = sorted((tmp_path / "meetings").glob("*.events.jsonl"))
    assert len(files) == 1 and files[0].name == events_path.name
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(session.events)
    parsed_kinds = [json.loads(line)["kind"] for line in lines]
    assert parsed_kinds == [e.kind for e in session.events]


def test_write_events_jsonl_with_none_path_is_noop(tmp_path, monkeypatch):
    """summary() 被測試 monkeypatch 掉時沒有路徑可寫——不能炸。"""
    monkeypatch.chdir(tmp_path)
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))
    _write_events_jsonl(session, None)
    assert not (tmp_path / "meetings").exists()


# ── (g) T3a：summary() emit 的 minutes 事件 ─────────────────────────


def test_summary_emits_minutes_event_with_md_contents_and_paths(tmp_path, monkeypatch):
    """B1／B2：summary() 寫完 .log 與兩份 md 後 emit `minutes`，帶兩份 md 的完整
    內容與四個相對 cwd 的路徑；且它是 session.events 的最後一筆。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm",
                        lambda events: {"decisions": [], "todos": [],
                                        "unresolved": [], "stances": {}})
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))
    session.emit("utterance", {"speaker": "A", "text": "hi", "start": 0.0, "end": 1.0})

    events_path = summary(session)

    assert session.events[-1].kind == "minutes"
    data = session.events[-1].data
    assert "error" not in data
    assert data["log_path"].startswith("meetings/") and data["log_path"].endswith(".log")
    assert data["events_path"] == str(events_path)
    assert data["host_path"].endswith(".host.md")
    assert data["minutes_path"].endswith(".minutes.md")
    # 內容與磁碟一致
    assert data["host_md"] == Path(data["host_path"]).read_text(encoding="utf-8")
    assert data["minutes_md"] == Path(data["minutes_path"]).read_text(encoding="utf-8")
    assert data["host_md"].strip() != ""


def test_summary_emits_minutes_event_with_error_when_module_missing(tmp_path, monkeypatch):
    """B2：`_try_write_minutes` 因 ImportError 略過時，`minutes` 事件仍要發——
    兩份 md 給空字串並帶 error，UI 才知道「有結束、但沒有總結」。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "meeting_host.minutes", None)
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))

    events_path = summary(session)

    assert session.events[-1].kind == "minutes"
    data = session.events[-1].data
    assert data["error"] == "minutes module unavailable"
    assert data["host_md"] == "" and data["minutes_md"] == ""
    assert data["host_path"] == "" and data["minutes_path"] == ""
    assert data["events_path"] == str(events_path)


# ── (f) spectator／minutes 模組不存在時印略過訊息、不拋例外 ─────────


def test_try_import_spectator_serve_missing_module_skips(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "meeting_host.spectator", None)
    result = _try_import_spectator_serve()
    assert result is None
    assert "略過" in capsys.readouterr().out


def test_try_write_minutes_missing_module_skips_without_raising(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "meeting_host.minutes", None)
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))
    _try_write_minutes(session, tmp_path)  # 不拋例外
    assert "略過" in capsys.readouterr().out
