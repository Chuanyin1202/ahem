import discord

from meeting_host.discord_source import MeetingBot
from meeting_host.speaker import Chair, Output
from meeting_host.state import MeetingState


class FakeLoop:
    def call_soon_threadsafe(self, cb, *args, **kw):
        assert not kw, "call_soon_threadsafe 不接受關鍵字參數"
        cb(*args)


class FakeVC:
    def __init__(self, raise_on_play: bool = False):
        self.calls = []
        self._raise_on_play = raise_on_play

    def is_connected(self):
        return True

    def play(self, source, *, after=None):
        if self._raise_on_play:
            raise discord.ClientException("Not connected to voice.")
        self.calls.append((source, after))


def test_player_died_replays_with_new_output_and_after_callback():
    bot = MeetingBot.__new__(MeetingBot)  # 不跑 discord.Client.__init__
    bot.loop_ref = FakeLoop()
    bot.vc = FakeVC()
    old = Output()
    bot.output = old
    bot._player_died(RuntimeError("boom"))
    assert len(bot.vc.calls) == 1
    source, after = bot.vc.calls[0]
    assert isinstance(source, Output) and source is not old
    assert after == bot._player_died


def test_player_restart_gives_up_after_three_in_a_minute():
    bot = MeetingBot.__new__(MeetingBot)
    bot.loop_ref = FakeLoop()
    bot.vc = FakeVC()
    bot.output = Output()
    for i in range(4):
        bot._player_died(RuntimeError("boom"), now=100.0 + i)  # 全在同一分鐘內
    assert len(bot.vc.calls) == 3


def test_player_restart_swallows_client_exception():
    bot = MeetingBot.__new__(MeetingBot)
    bot.loop_ref = FakeLoop()
    bot.vc = FakeVC(raise_on_play=True)
    bot.output = Output()
    bot._player_died(RuntimeError("boom"), now=100.0)  # 不應拋出


class FakeEarcon:
    pcm = b"\x01" * 100
    seconds = 0.1


class FakeVoice:
    async def synth(self, text):
        return
        yield b""  # pragma: no cover — 只是讓它成為 async generator


def test_chair_follows_the_new_output_after_player_restart():
    """C1：播放器重建後 Chair 必須跟著換到新的 Output。

    否則 discord 播放執行緒只讀新 Output，主席卻繼續把音訊寫進舊的，
    從此永遠沒人消費——症狀是「連著、沒錯誤，但主席再也不出聲」。
    """
    bot = MeetingBot.__new__(MeetingBot)
    bot.loop_ref = FakeLoop()
    bot.vc = FakeVC()
    old = Output()
    bot.output = old
    chair = Chair(MeetingState(topic="t", duration_min=30, participants=[]),
                  old, FakeVoice(), FakeEarcon())
    bot.on_output_replaced = chair.replace_output  # 與 live.py start_chair 同一種接法

    bot._player_died(RuntimeError("boom"))

    assert chair.output is not old
    assert chair.output is bot.output
    assert bot.vc.calls[-1][0] is chair.output


def test_restart_before_hook_registration_is_caught_by_resync():
    """R1：start_chair 是「讀 bot.output → 建 Chair → 註冊 hook」三步。

    播放執行緒若在這三步中間重建 Output，hook 還沒註冊、通知就丟了，Chair 會永久
    停在舊 Output。註冊完再呼叫一次 replace_output 做 re-sync 才追得上。
    """
    bot = MeetingBot.__new__(MeetingBot)
    bot.loop_ref = FakeLoop()
    bot.vc = FakeVC()
    o1 = Output()
    bot.output = o1
    chair = Chair(MeetingState(topic="t", duration_min=30, participants=[]),
                  o1, FakeVoice(), FakeEarcon())

    bot._player_died(RuntimeError("boom"))  # hook 還沒註冊
    assert bot.output is not o1
    assert chair.output is o1  # 通知丟了

    bot.on_output_replaced = chair.replace_output
    chair.replace_output(bot.output)  # re-sync 追上註冊前發生的重建

    assert chair.output is bot.output
    assert bot.vc.calls[-1][0] is chair.output
