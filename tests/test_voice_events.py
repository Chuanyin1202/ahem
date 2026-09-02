"""T12：RTP 層 voice_* 訊號進事件流。

`_voice_start`/`_voice_stop` 原本只更新 `MeetingState`（見 test_participant_sync.py
以外沒有專門的測試檔）；這裡驗證新增的 `on_voice_activity` 回呼——
既有的 `state.voice_started`/`voice_stopped` 呼叫必須維持不變，
新回呼是「附加」而非「取代」。
"""
from meeting_host.discord_source import MeetingBot
from meeting_host.live import Session
from meeting_host.state import MeetingState


class FakeMember:
    def __init__(self, display_name, bot=False):
        self.display_name = display_name
        self.bot = bot


class FakeLoop:
    """跟 test_player_restart.py 同一種假 loop——同步立刻執行，但驗證呼叫端
    確實是透過 call_soon_threadsafe（音訊執行緒不得直接碰 event loop）。"""

    def call_soon_threadsafe(self, cb, *args, **kw):
        assert not kw, "call_soon_threadsafe 不接受關鍵字參數"
        cb(*args)


def make_bot():
    bot = MeetingBot.__new__(MeetingBot)  # 不跑 discord.Client.__init__
    bot.state = MeetingState(topic="t", duration_min=30, participants=[])
    bot.loop_ref = FakeLoop()
    return bot


# ── 既有行為不變：state.voice_started/voice_stopped 仍被呼叫 ──────────────


def test_voice_start_still_updates_state():
    bot = make_bot()
    bot._voice_start(FakeMember("Alice"))
    assert "Alice" in bot.state.voice_active


def test_voice_stop_still_updates_state():
    bot = make_bot()
    bot._voice_start(FakeMember("Alice"))
    bot._voice_stop(FakeMember("Alice"))
    assert "Alice" not in bot.state.voice_active


# ── AC1/AC2：on_voice_activity 帶正確的 speaker 與開始/停止語意 ───────────


def test_voice_start_fires_on_voice_activity_with_active_true():
    bot = make_bot()
    calls = []
    bot.on_voice_activity = lambda speaker, active: calls.append((speaker, active))
    bot._voice_start(FakeMember("Alice"))
    assert calls == [("Alice", True)]


def test_voice_stop_fires_on_voice_activity_with_active_false():
    bot = make_bot()
    calls = []
    bot.on_voice_activity = lambda speaker, active: calls.append((speaker, active))
    bot._voice_stop(FakeMember("Alice"))
    assert calls == [("Alice", False)]


# ── AC3：bot 的音訊不產生事件 ─────────────────────────────────────────


def test_bot_member_does_not_fire_on_voice_activity():
    bot = make_bot()
    calls = []
    bot.on_voice_activity = lambda speaker, active: calls.append((speaker, active))
    bot._voice_start(FakeMember("BotMember", bot=True))
    bot._voice_stop(FakeMember("BotMember", bot=True))
    assert calls == []
    assert bot.state.voice_active == set()  # 既有的 bot 過濾也沒被動到


# ── AC4：兩位不同說話者同時進出，各自的事件帶各自的 speaker，不互相污染 ────


def test_two_speakers_interleaved_do_not_cross_contaminate():
    bot = make_bot()
    calls = []
    bot.on_voice_activity = lambda speaker, active: calls.append((speaker, active))
    bot._voice_start(FakeMember("Alice"))
    bot._voice_start(FakeMember("Bob"))
    bot._voice_stop(FakeMember("Alice"))
    bot._voice_stop(FakeMember("Bob"))
    assert calls == [
        ("Alice", True),
        ("Bob", True),
        ("Alice", False),
        ("Bob", False),
    ]
    # 兩人各自的聲學狀態互不影響
    assert bot.state.voice_active == set()


# ── AC5：發送經過 call_soon_threadsafe，不在音訊執行緒直接碰 event loop ────


class RecordingLoop:
    def __init__(self):
        self.scheduled = []

    def call_soon_threadsafe(self, cb, *args):
        self.scheduled.append((cb, args))
        cb(*args)  # 測試裡立刻執行，驗證的重點是「有沒有經過這條路徑」


def test_voice_activity_dispatched_via_call_soon_threadsafe():
    bot = MeetingBot.__new__(MeetingBot)
    bot.state = MeetingState(topic="t", duration_min=30, participants=[])
    bot.loop_ref = RecordingLoop()
    calls = []
    bot.on_voice_activity = lambda speaker, active: calls.append((speaker, active))
    bot._voice_start(FakeMember("Alice"))
    assert calls == [("Alice", True)]
    # state.voice_started 與 on_voice_activity 都各自排了一次 call_soon_threadsafe
    scheduled_callables = [cb for cb, _ in bot.loop_ref.scheduled]
    assert bot.state.voice_started in scheduled_callables
    assert bot.on_voice_activity in scheduled_callables


# ── on_voice_activity 未設定時不炸（例如尚未接線的舊測試／情境）────────────


def test_missing_on_voice_activity_hook_does_not_raise():
    bot = make_bot()
    bot._voice_start(FakeMember("Alice"))  # on_voice_activity 是類別預設 None
    bot._voice_stop(FakeMember("Alice"))


# ── live.py 接線：Session.emit("voice", ...) 產出正確事件（AC1/2/8 的另一半）──


def test_live_wiring_pattern_emits_voice_event():
    """跟 live.py main_async 裡實際寫的那行 lambda 完全同構——不跑 main_async
    （需要真的 Discord token），但驗證同一段接線邏輯的行為。"""
    session = Session(MeetingState(topic="t", duration_min=30, participants=["Alice"]))
    bot = make_bot()
    bot.on_voice_activity = lambda speaker, active: session.emit(
        "voice", {"speaker": speaker, "active": active})

    bot._voice_start(FakeMember("Alice"))
    bot._voice_stop(FakeMember("Alice"))

    voice_events = [e for e in session.events if e.kind == "voice"]
    assert [e.data for e in voice_events] == [
        {"speaker": "Alice", "active": True},
        {"speaker": "Alice", "active": False},
    ]
