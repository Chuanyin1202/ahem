"""進頻道貼觀戰網址、收尾貼會議記錄——兩件事都不能影響會議本身。

設計背景：參與者權杖貼在該場會議的 Discord 文字聊天裡，看得到的人就等於進得了
那個語音頻道的人，存取範圍直接借用 Discord 既有的成員資格。但 Discord 那一側
隨時可能失敗（缺權限、頻道不支援文字聊天、API 出錯），所以這兩條路徑一律是
best-effort：失敗只印一行，不能讓會議或收尾跟著壞掉。
"""
import asyncio

import pytest

from meeting_host import live
from meeting_host.discord_source import MeetingBot
from meeting_host.events import Event


class FakeChannel:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    async def send(self, content=None, file=None):
        if self.fail:
            raise RuntimeError("Missing Permissions")
        self.sent.append({"content": content, "file": file})


def _bot(channel):
    bot = MeetingBot.__new__(MeetingBot)   # 略過 discord.Client.__init__
    bot.channel = channel
    return bot


def test_post_returns_false_without_a_channel():
    """還沒進頻道就要貼 → 安靜跳過，不炸。"""
    assert asyncio.run(_bot(None)._post("hi")) is False


def test_join_notice_reaches_the_channel():
    ch = FakeChannel()
    assert asyncio.run(_bot(ch)._post("觀戰畫面：http://x/?k=abc")) is True
    assert "?k=abc" in ch.sent[0]["content"]


def test_a_failing_channel_never_raises():
    """缺權限是最可能的真實失敗（bot 角色沒開發訊息權限）——吞掉，回 False。"""
    ch = FakeChannel(fail=True)
    assert asyncio.run(_bot(ch)._post("hi")) is False
    assert ch.sent == []


def test_minutes_go_out_as_a_file_attachment():
    """Discord 單則訊息 2000 字元上限，實測總結常常超過，所以走附件。"""
    ch = FakeChannel()
    md = "# 會議記錄\n" + "決議：" * 900
    assert asyncio.run(_bot(ch).post_minutes(md, "meeting-1.minutes.md")) is True
    sent = ch.sent[0]
    assert sent["file"] is not None
    assert sent["file"].filename == "meeting-1.minutes.md"


def test_empty_minutes_are_not_posted():
    ch = FakeChannel()
    assert asyncio.run(_bot(ch).post_minutes("   ", "x.md")) is False
    assert ch.sent == []


class _Session:
    def __init__(self, events):
        self.events = events


def test_shutdown_helper_reads_the_minutes_event_not_the_file():
    """內容取自剛 emit 的 minutes 事件——收尾當下不重讀檔案。"""
    ch = FakeChannel()
    session = _Session([Event("minutes", 1.0, {
        "minutes_md": "# 決議\n- 上線排程確定",
        "minutes_path": "meetings/meeting-42.minutes.md"})])
    asyncio.run(live._post_minutes_to_channel(session, _bot(ch)))
    assert ch.sent[0]["file"].filename == "meeting-42.minutes.md"


def test_shutdown_helper_survives_a_bot_that_blows_up():
    """關鍵不變式：貼記錄失敗不能讓 shutdown 拋例外——檔案已經寫出去了。"""
    class Exploding:
        async def post_minutes(self, md, name):
            raise RuntimeError("Discord is down")
    session = _Session([Event("minutes", 1.0, {"minutes_md": "x", "minutes_path": "a.md"})])
    asyncio.run(live._post_minutes_to_channel(session, Exploding()))  # 不該拋


def test_shutdown_helper_is_a_noop_without_a_minutes_event():
    ch = FakeChannel()
    asyncio.run(live._post_minutes_to_channel(_Session([]), _bot(ch)))
    assert ch.sent == []
