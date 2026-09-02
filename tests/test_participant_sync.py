import asyncio

from meeting_host.discord_source import MeetingBot
from meeting_host.state import MeetingState


class FakeChannel:
    def __init__(self, id):  # noqa: A002
        self.id = id


class FakeVC:
    def __init__(self, channel):
        self.channel = channel


class FakeMember:
    def __init__(self, display_name, bot=False):
        self.display_name = display_name
        self.bot = bot


class FakeVoiceState:
    def __init__(self, channel):
        self.channel = channel


def make_bot():
    bot = MeetingBot.__new__(MeetingBot)  # 不跑 discord.Client.__init__
    bot.state = MeetingState(topic="t", duration_min=30, participants=[])
    bot.vc = FakeVC(FakeChannel(1))  # 會議所在頻道
    return bot


def test_join_meeting_channel_syncs():
    bot = make_bot()
    member = FakeMember("Alex")
    before = FakeVoiceState(None)
    after = FakeVoiceState(FakeChannel(1))  # 加入的是會議頻道
    asyncio.run(bot.on_voice_state_update(member, before, after))
    assert bot.state.participants == ["Alex"]


def test_join_other_channel_does_not_sync():
    bot = make_bot()
    member = FakeMember("Alex")
    before = FakeVoiceState(None)
    after = FakeVoiceState(FakeChannel(2))  # 加入的是同 guild 其他頻道
    asyncio.run(bot.on_voice_state_update(member, before, after))
    assert bot.state.participants == []


def test_move_from_other_channel_into_meeting_channel_syncs():
    bot = make_bot()
    member = FakeMember("Alex")
    before = FakeVoiceState(FakeChannel(2))  # 原本在別的頻道
    after = FakeVoiceState(FakeChannel(1))  # 移入會議頻道
    asyncio.run(bot.on_voice_state_update(member, before, after))
    assert bot.state.participants == ["Alex"]


def test_leave_marks_absent_and_rejoin_clears():
    """I5：離開會議頻道 → 標記 absent（不能再被點名），統計仍保留；回來就解除。"""
    bot = make_bot()
    member = FakeMember("Alex")
    asyncio.run(bot.on_voice_state_update(member, FakeVoiceState(None), FakeVoiceState(FakeChannel(1))))
    assert bot.state.participants == ["Alex"]
    assert bot.state.absent == set()

    asyncio.run(bot.on_voice_state_update(member, FakeVoiceState(FakeChannel(1)), FakeVoiceState(None)))
    assert bot.state.participants == ["Alex"]  # 名單不刪：會後統計還要算他的發言佔比
    assert bot.state.absent == {"Alex"}

    asyncio.run(bot.on_voice_state_update(member, FakeVoiceState(None), FakeVoiceState(FakeChannel(1))))
    assert bot.state.absent == set()


def test_move_out_to_other_channel_marks_absent():
    """移到同 guild 的其他頻道也是離開會議——主席一樣不該點名他。"""
    bot = make_bot()
    member = FakeMember("Alex")
    asyncio.run(bot.on_voice_state_update(member, FakeVoiceState(None), FakeVoiceState(FakeChannel(1))))
    asyncio.run(bot.on_voice_state_update(member, FakeVoiceState(FakeChannel(1)), FakeVoiceState(FakeChannel(2))))
    assert bot.state.absent == {"Alex"}


# ── T13 缺陷 A：ensure_participant() 必須帶 now，否則 joined_at 記不到 ──


def test_join_meeting_channel_records_joined_at():
    """會議進行中才加入的人，joined_at 要被記下來——不然 silent_seconds() 又會
    退回「從會議開始算」的舊行為，一進來就被判定全場沉默／被冷落（T13）。"""
    bot = make_bot()
    member = FakeMember("Alex")
    asyncio.run(bot.on_voice_state_update(member, FakeVoiceState(None), FakeVoiceState(FakeChannel(1))))
    assert "Alex" in bot.state.joined_at


class FakeChannelWithMembers(FakeChannel):
    def __init__(self, id, members):  # noqa: A002
        super().__init__(id)
        self.members = members


def test_state_sync_records_joined_at_for_initial_roster():
    """on_ready() 進頻道時同步既有成員（state_sync）也要記 joined_at——
    這條路徑跟 on_voice_state_update 共用同一個沉默起點機制。"""
    bot = MeetingBot.__new__(MeetingBot)
    bot.state = MeetingState(topic="t", duration_min=30, participants=[])
    channel = FakeChannelWithMembers(1, [FakeMember("Alex"), FakeMember("HostBot", bot=True)])
    bot.state_sync(channel)
    assert bot.state.participants == ["Alex"]  # bot 自己不算參與者
    assert "Alex" in bot.state.joined_at


# ── T11 缺陷 B：_on_audio 收到真人音訊封包時通知 on_human_audio ──────────
#
# --say-hello 問候時機用的訊號：確認「真人的音訊路徑真的通了」，而不只是
# 「人在頻道名單裡」。用 FakeLoop 讓 call_soon_threadsafe 同步執行，不必真的
# 起執行緒或連 Discord。


class FakeLoop:
    def call_soon_threadsafe(self, cb, *args, **kw):
        assert not kw, "call_soon_threadsafe 不接受關鍵字參數"
        cb(*args)


class FakePool:
    def feed(self, name, pcm):
        pass


class FakeAudioData:
    def __init__(self, pcm=b"\x01\x02"):
        self.pcm = pcm


def make_audio_bot():
    bot = MeetingBot.__new__(MeetingBot)  # 不跑 discord.Client.__init__
    bot.loop_ref = FakeLoop()
    bot.pool = FakePool()
    return bot


def test_on_audio_notifies_human_audio_once():
    """驗收 3（discord_source 端）：真人的音訊封包一到就通知一次；
    之後同一個人繼續講話不必每個封包都重複通知（避免熱路徑上無謂的排程）。"""
    bot = make_audio_bot()
    calls = []
    bot.on_human_audio = lambda name: calls.append(name)
    member = FakeMember("Alex")
    bot._on_audio(member, FakeAudioData())
    bot._on_audio(member, FakeAudioData())
    bot._on_audio(member, FakeAudioData())
    assert calls == ["Alex"]


def test_on_audio_ignores_bot_and_empty_pcm():
    """主席自己（bot）或空 pcm 的封包不算「真人音訊」，不該觸發問候訊號。"""
    bot = make_audio_bot()
    calls = []
    bot.on_human_audio = lambda name: calls.append(name)
    bot._on_audio(FakeMember("OtherBot", bot=True), FakeAudioData())
    bot._on_audio(FakeMember("Alex"), FakeAudioData(pcm=b""))
    assert calls == []


def test_on_audio_noop_when_no_hello_gate_wired():
    """沒開 --say-hello 時 main_async 不會設定 on_human_audio（維持類別預設 None），
    _on_audio 收到音訊也不能因此炸掉——驗收 5 的 discord_source 端保證。"""
    bot = make_audio_bot()
    assert bot.on_human_audio is None
    bot._on_audio(FakeMember("Alex"), FakeAudioData())  # 不應拋出
