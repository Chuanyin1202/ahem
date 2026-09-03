"""迴歸（T21）：兩人快速交替換人講話時，room-level（target=None）介入不該被
`Session.note_speaker` 遞增的 revision 連續作廢。

背景（今晚一場 18 分 22 秒的真實雙人會議實測）：11 次介入排入佇列，5 次講出來、
6 次被丟棄，丟棄原因全部是「revision 過期」——分佈不是隨機的：兩人快速交替期間
排入的 target=None 軟插入 100% 被丟，全場沉默（沒有人換人講話）期間排入的
100% 成功。根因見 live.py 的 `resurrect_room_level` docstring。

本檔只驗證這張單新增的行為，不重寫既有覆蓋：
- tests/test_chair.py::test_stale_revision_is_dropped_before_speaking
  （Chair 本身「revision 不符即作廢」的通用機制完全不動，見 speaker.py）
- tests/harness/test_regression_pause_commit_revision.py
  （同一人連講不遞增 revision／真的換人要作廢——這兩條既有覆蓋原封不動）
- tests/test_live_wiring.py 的 `resurrect_room_level`／`Session.on_dropped`
  純函式單元測試（本檔驗證的是這些函式接上真正 Chair 之後的端到端行為）
"""
import asyncio

from meeting_host.live import Session, escalate_with_current_facts
from meeting_host.speaker import ESCALATE_SECONDS, Intervention
from meeting_host.state import MeetingState

from .chair_runner import ChairHarness
from .clock import VirtualClock
from .fake_voice import FakeVoice


def _room_iv(session: Session, kind: str = "離題", text: str = "請回到主題", created_at=None):
    return Intervention(kind=kind, target=None, text=text, hard=False,
                         revision=session.revision,
                         created_at=session.now if created_at is None else created_at)


def test_room_level_survives_rapid_speaker_alternation_and_speaks_at_pause():
    """驗收 1／6：兩人每隔一句就換人講話（今晚序列 07:00～09:29 那段），
    room-level 軟插入不再因為換人而作廢；換人結束、真正的停頓出現後照常開口。
    """
    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
        clock = VirtualClock(start=100.0)
        st = session.st
        st.silence_since = 0.0
        h = ChairHarness(st, FakeVoice(n_frames=3), clock=clock,
                          revision=lambda: session.revision, on_dropped=session.on_dropped)
        session.chair = h.chair

        session.note_speaker("A")  # 先建立目前發言者（首次必定遞增一次 revision）
        st.voice_started("A", now=clock())  # 一路有人講：先只看 revision 這條軸，跟停頓解耦
        iv = _room_iv(session)
        assert h.request(iv) is True

        # 兩人快速交替換人講話 6 次，每次都會讓 revision 遞增；舊行為下一個
        # tick 就會被判「revision 過期」丟掉，永遠等不到它要的停頓
        for sp in ["B", "A", "B", "A", "B", "A"]:
            session.note_speaker(sp)
            await h.run_ticks(3)  # 0.3s，遠小於 PAUSE_SECONDS，交替密度比停頓快

        assert h.chair.pending is not None  # 沒有被真的丟掉
        assert not h.spoken                  # 一直有人在講，還不該開口

        st.voice_stopped("A", now=clock())   # 交替結束，真正的停頓開始
        await h.run_ticks(9)
        assert h.chair.pending is not None    # 0.9s：還不到 PAUSE_SECONDS(1.0s)
        await h.run_ticks(5, drain=True)      # 跨過 1.0s
        assert len(h.spoken) == 1
        assert h.spoken[0][0].kind == "離題"
    asyncio.run(go())


def test_target_specific_still_drops_on_speaker_change_with_real_wiring():
    """驗收 2：發言超時／有人被冷落（target=某人）換人講話後仍然作廢——
    這條保護在接上真正的 `Session.on_dropped` 之後也不能被弄掉。
    """
    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
        clock = VirtualClock(start=100.0)
        st = session.st
        st.silence_since = 0.0
        h = ChairHarness(st, FakeVoice(n_frames=3), clock=clock,
                          revision=lambda: session.revision, on_dropped=session.on_dropped)
        session.chair = h.chair

        session.note_speaker("A")
        session.done.add(("有人被冷落", "B"))
        iv = Intervention(kind="有人被冷落", target="B", text="B，你怎麼看？", hard=False,
                           revision=session.revision, created_at=session.now)
        assert h.request(iv) is True

        session.note_speaker("B")  # 換人 → revision 遞增；target 不是 None，仍要作廢
        await h.run_ticks(15, drain=True)

        assert not h.spoken
        assert h.chair.pending is None
        assert ("有人被冷落", "B") not in session.done  # 真的作廢，claim 要解除
        dropped = [e for e in session.events if e.kind == "dropped"]
        assert dropped and dropped[0].data["reason"] == "revision 過期"
    asyncio.run(go())


def test_room_level_too_old_escalates_to_hard_instead_of_dying_silently():
    """驗收 3（2026-09-03 三人真實會議修正後）：換人換不停、真的等不到一次
    停頓——存活超過既有的 ESCALATE_SECONDS 門檻之後**升級成硬打斷**，不再是
    悄悄作廢。

    背景：原本這裡超過年齡上限就放行讓它「照 Chair 原本的行為真的作廢」，
    但那條路根本不會經過 Chair 自己的硬打斷判斷（那個判斷比較的是
    `_pending_since`，每次重生都被 `request()` 重設，快速換人時永遠來不及
    累積滿 ESCALATE_SECONDS）——結果是換人換不停的 room-level 介入永遠沒有
    機會變成硬打斷，只會在這裡放棄。三人真實會議實測：兩次「離題」判定各自
    重生 3～4 次後在這裡被丟掉，全場只有開場問候一句話。

    用倒退 `created_at` 模擬「這句話已經存在 ESCALATE_SECONDS 那麼久」——
    `Session.on_dropped` 用的是真實時鐘（`Session.now`），單元測試裡不會真的
    等 15 秒，直接讓它一開始就已經「太老」。
    """
    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
        clock = VirtualClock(start=100.0)
        st = session.st
        st.silence_since = 0.0
        h = ChairHarness(st, FakeVoice(n_frames=3), clock=clock,
                          revision=lambda: session.revision, on_dropped=session.on_dropped)
        session.chair = h.chair

        session.note_speaker("A")
        st.voice_started("A", now=clock())  # 一路有人講：不讓停頓分支干擾這條年齡上限測試
        old_created_at = session.now - (ESCALATE_SECONDS + 1.0)  # 模擬已經存活超過上限
        iv = _room_iv(session, created_at=old_created_at)
        assert h.request(iv) is True

        session.note_speaker("B")  # 換人 → revision 過期 → 太老，升級成硬打斷重新排入
        await h.run_ticks(2)  # hard 一進 pending 立刻不等安靜開講，這兩輪內就會轉成 playing

        await h.wait_task_settled()
        h.player.drain_all()
        await h.run_ticks(2, drain=True)

        assert len(h.spoken) == 1
        assert h.spoken[0][0].hard is True
        dropped = [e for e in session.events if e.kind == "dropped"]
        assert not dropped  # 這次不是真的作廢——沒有 dropped 事件
    asyncio.run(go())


def test_room_level_escalate_interaction_after_surviving_alternation():
    """驗收 5：撐過幾次換人重生之後，安靜下來不再換人、真的等滿 15 秒——
    Chair 既有的升級路徑（`escalate_with_current_facts`：慢路 kind 沿用原話術、
    只升級成硬打斷）要能正常接手，不受這次「換人不作廢 room-level 介入」的
    修正影響。
    """
    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
        clock = VirtualClock(start=100.0)
        st = session.st
        st.silence_since = 0.0

        def on_escalate(iv):
            return escalate_with_current_facts(st, clock(), session.revision, iv, None)

        h = ChairHarness(st, FakeVoice(n_frames=3), clock=clock,
                          revision=lambda: session.revision, on_dropped=session.on_dropped,
                          on_escalate=on_escalate)
        session.chair = h.chair

        session.note_speaker("A")
        st.voice_started("A", now=clock())  # 一路有人講：擋住停頓分支，逼流程走到 escalate
        iv = _room_iv(session)
        assert h.request(iv) is True

        # 先撐過幾次快速換人（本張單的重生機制），確認跟後面的 escalate 不衝突
        for sp in ["B", "A", "B"]:
            session.note_speaker(sp)
            await h.run_ticks(3)
        assert h.chair.pending is not None
        assert not h.spoken

        # 之後安靜下來不再換人，等滿 15 秒讓 Chair 自然升級成硬打斷
        await h.run_ticks(149)
        assert not h.spoken
        await h.run_ticks(4)
        await h.wait_task_settled()
        h.player.drain_all()
        await h.run_ticks(2, drain=True)

        assert len(h.spoken) == 1
        assert h.spoken[0][0].kind == "離題"
        assert h.spoken[0][0].hard is True  # escalate 強制升級成硬打斷
        assert h.chair.escalated == 1
    asyncio.run(go())
