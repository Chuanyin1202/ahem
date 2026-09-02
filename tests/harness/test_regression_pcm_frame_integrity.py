"""迴歸 2（提案 §3 第二列）：prebuffer 後 chunk 重複——soft／hard 各一。

根因（T6b）：prebuffer 達標後的每個 chunk 同時被 append 進 `frames` 又
直接 `enqueue()`，迴圈結束後的尾段 `for f in frames: enqueue(f)` 把
「prebuffer 之後整段」再送一次，句子（少前 200ms）重播。

對應既有覆蓋（本檔不重寫，改用編號幀＋frame ledger 這種更嚴格的 PCM 完整性
oracle——既有測試只數總幀數，抓不到「順序錯了」或「某幀從沒播到、另一幀
播了兩次但總數剛好抵銷」這種情況）：

- tests/test_chair.py::test_long_sentence_is_enqueued_exactly_once（soft）
- tests/test_chair.py::test_long_hard_sentence_is_enqueued_exactly_once（hard）
"""
import asyncio

from meeting_host.speaker import Intervention
from meeting_host.state import MeetingState

from .chair_runner import ChairHarness
from .clock import VirtualClock
from .fake_voice import FakeVoice

N_FRAMES = 15  # 超過 PREBUFFER_SECONDS 門檻（10 幀）的長句，跟既有測試同一個長度


def _iv(hard: bool):
    return Intervention(kind="離題" if not hard else "發言超時", target=None,
                         text="一段長句", hard=hard, revision=0, created_at=100.0)


def test_soft_long_sentence_each_frame_played_exactly_once():
    async def go():
        st = MeetingState(topic="t", duration_min=30, participants=["A"])
        st.silence_since = 0.0
        clock = VirtualClock(start=100.0)
        h = ChairHarness(st, FakeVoice(n_frames=N_FRAMES), clock=clock)

        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        assert h.request(_iv(hard=False)) is True
        await h.run_ticks(15)           # 觸發軟插入，_speak 開始跑，不 drain
        await h.wait_task_settled()     # 真的等 15 個 frame（含中途 sleep(0)）全部跑完
        h.player.drain_all()
        h.player.assert_played_exactly_once_in_order(N_FRAMES)
    asyncio.run(go())


def test_hard_long_sentence_each_frame_played_exactly_once():
    async def go():
        st = MeetingState(topic="t", duration_min=30, participants=["A"])
        clock = VirtualClock(start=100.0)
        h = ChairHarness(st, FakeVoice(n_frames=N_FRAMES), clock=clock)

        assert h.request(_iv(hard=True)) is True
        await h.run_ticks(1)            # 觸發 _start（hard 不用等停頓）
        await h.wait_task_settled()     # 真的等（含 EARCON_GATE 真實延遲）跑完
        h.player.drain_all()
        # earcon（固定內容、非編號幀）先出，frame_seq() 對它回 None，不進 ledger；
        # 這裡只驗語音幀本身的完整性
        h.player.assert_played_exactly_once_in_order(N_FRAMES)
    asyncio.run(go())
