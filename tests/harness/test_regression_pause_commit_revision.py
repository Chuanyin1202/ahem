"""迴歸 1（提案 §3 第一列）：停頓觸發軟插入排入 pending 後，同一人接連幾次
STT commit（每句都會經過 `Session.note_speaker`）不能讓 revision 變、進而
讓 `Chair.tick()` 誤判 revision 過期把 pending 丟掉——最終要恰好開口一次。

對應既有覆蓋（本檔不重寫任何一條斷言，只是把兩段既有覆蓋串成一條端到端
劇本，同時保留一條對照組驗證「真的換人時 revision 該變、該丟」沒有被
這個組合破壞）：

- tests/test_live_wiring.py::test_revision_only_bumps_on_speaker_change
  （T7b：`Session.note_speaker` 只在換人時遞增 revision）
- tests/test_chair.py::test_stale_revision_is_dropped_before_speaking
  （revision 真的變了時，`Chair.tick()` 要丟掉過期 pending）
- tests/test_chair.py::test_soft_waits_for_one_second_pause
  （軟插入等 1 秒停頓才開口的基本節奏，這裡疊上 commit／revision 這條軸）
"""
import asyncio

from meeting_host.live import Session
from meeting_host.speaker import Intervention
from meeting_host.state import MeetingState

from .chair_runner import ChairHarness
from .clock import VirtualClock
from .fake_voice import FakeVoice


def _iv(rev: int, kind: str = "離題", hard: bool = False, text: str = "請回到主題"):
    return Intervention(kind=kind, target=None, text=text, hard=hard, revision=rev, created_at=100.0)


def test_pending_survives_same_speaker_commits_and_speaks_exactly_once():
    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]))
        clock = VirtualClock(start=100.0)
        st = session.st
        st.silence_since = 0.0  # 跟 test_chair.py 的 make() 一樣：場景一開始就已沉默
        h = ChairHarness(st, FakeVoice(n_frames=3), clock=clock, revision=lambda: session.revision)

        st.voice_started("A", now=clock())
        session.note_speaker("A")  # 先建立「目前發言者是 A」（首次一定會遞增一次 revision）
        assert h.request(_iv(session.revision)) is True

        # 停頓期間，A 又連講兩句（每句都是一次 STT commit）——同一人，
        # revision 不該再變（T7b），pending 不該被 Chair.tick() 判定過期丟掉
        session.note_speaker("A")
        session.note_speaker("A")
        await h.run_ticks(5)
        assert h.chair.pending is not None
        assert not h.spoken

        st.voice_stopped("A", now=clock())
        await h.run_ticks(9)              # 0.9s 沉默：還不到 PAUSE_SECONDS
        assert h.chair.pending is not None

        await h.run_ticks(5, drain=True)  # 跨過 1.0s，開口
        assert len(h.spoken) == 1
        assert h.spoken[0][0].kind == "離題"
        assert not h.dropped
    asyncio.run(go())


def test_speaker_change_bumps_revision_and_drops_stale_pending():
    """對照組：真的換人講話 → revision 變 → 過期 pending 要被丟掉，不能誤播。
    跟上一條測試共用同一個 harness 寫法，只是換一種劇本走向，確保「同一人
    commit 不影響 revision」這個修正沒有連帶破壞「換人就該丟」的既有行為。
    """
    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
        clock = VirtualClock(start=100.0)
        st = session.st
        st.silence_since = 0.0
        h = ChairHarness(st, FakeVoice(n_frames=3), clock=clock, revision=lambda: session.revision)

        assert h.request(_iv(session.revision)) is True
        session.note_speaker("B")  # 換人 → revision 遞增
        await h.run_ticks(15, drain=True)

        assert not h.spoken
        assert h.chair.pending is None
        assert h.dropped and h.dropped[0][1] == "revision 過期"
    asyncio.run(go())
