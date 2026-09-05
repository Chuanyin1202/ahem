"""T7：live.py 接線的純函式測試——不連 Discord。

涵蓋：fast_path.utterance_for 的三種快路話術模板、slow_path.should_score 的
busy 參數、live.escalate_with_current_facts 的三種升級情境、
live.slow_result_admissible 的三種可送/擋下情境（含冷卻期回歸修正）、
Session.note_speaker 只在發言者換人時遞增 revision（T7b 回歸修正）。
"""
import asyncio
import time

import pytest

from meeting_host import fast_path, live
from meeting_host.fast_path import FAST_KINDS, Trigger, utterance_for
from meeting_host.live import (
    HelloGate,
    Session,
    channel_has_human,
    escalate_with_current_facts,
    resurrect_room_level,
    slow_result_admissible,
)
from meeting_host.phrasing import PHRASE_KINDS, PhraseBank
from meeting_host.slow_path import should_score
from meeting_host.speaker import ESCALATE_SECONDS, Intervention
from meeting_host.state import MeetingState, Utterance


# ── fast_path.utterance_for ─────────────────────────────────────────


def test_utterance_for_overtime_uses_integer_minutes():
    t = Trigger(kind="發言超時", target="Alice", detail="Alice 已連續發言 3.0 分鐘", hard=True)
    assert utterance_for(t) == "Alice，你已經講了3分鐘，先讓其他人接一下。"


def test_utterance_for_neglected():
    t = Trigger(kind="有人被冷落", target="Bob", detail="Bob 已 5.0 分鐘沒有發言", hard=False)
    assert utterance_for(t) == "Bob，你有一陣子沒說話了，想聽聽你的看法。"


def test_utterance_for_agenda_overtime_uses_integer_minutes():
    t = Trigger(kind="議程超時", target=None, detail="議程只剩 4.0 分鐘", hard=False)
    assert utterance_for(t) == "只剩4分鐘，我們往結論收。"


# ── slow_path.should_score(busy=...) ────────────────────────────────


def _scored_state() -> MeetingState:
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "hello", 0.0, 2.0))
    st.add(Utterance("B", "hi", 2.0, 4.0))
    return st


def test_should_score_busy_blocks():
    st = _scored_state()
    assert should_score(st, 10.0, 0, busy=True) is False


def test_should_score_not_busy_unaffected():
    st = _scored_state()
    assert should_score(st, 10.0, 0, busy=False) is True
    assert should_score(st, 10.0, 0) is True  # 預設值不變


# ── live.escalate_with_current_facts ────────────────────────────────


def test_escalate_rule_still_holds_generates_fresh_text():
    """規則現在還成立（A 仍在連續發言超過門檻）→ 用當下事實重生文字＋hard。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.speaking_now("A", 50.0)  # 從 50.0 起持續講到 now
    iv = Intervention(kind="發言超時", target="A", text="stale", hard=False, revision=0, created_at=50.0)
    fresh = escalate_with_current_facts(st, 250.0, 5, iv)
    assert fresh is not None
    assert fresh.hard is True
    assert fresh.revision == 5
    assert fresh.created_at == 250.0
    assert fresh.text != "stale"
    assert "A" in fresh.text and "3" in fresh.text  # 200s ≈ 3.3 分鐘 → round → 3


def test_escalate_rule_no_longer_holds_drops():
    """快路 kind，但規則現在已不成立（沒人在講、也沒超時）→ 作廢。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    assert "發言超時" in FAST_KINDS
    iv = Intervention(kind="發言超時", target="A", text="stale", hard=False, revision=0, created_at=50.0)
    result = escalate_with_current_facts(st, 250.0, 5, iv)
    assert result is None


def test_escalate_slow_path_kind_keeps_utterance():
    """慢路 kind（不在 FAST_KINDS 裡）→ 沿用原話術，只升級成 hard，revision 更新。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    assert "離題" not in FAST_KINDS
    iv = Intervention(kind="離題", target=None, text="回到主題吧", hard=False, revision=0, created_at=50.0)
    result = escalate_with_current_facts(st, 250.0, 7, iv)
    assert result is not None
    assert result.hard is True
    assert result.revision == 7
    assert result.text == "回到主題吧"


# ── live.slow_result_admissible（修正回合 1：冷卻期回歸）────────────


def test_slow_result_admissible_type_wu_is_blocked():
    """type=無 即使三軸分數過了門檻也不算數——擋下，原因 type=無。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "無", "verdict": "正向介入", "positive": 3, "negative": 1, "none": 2, "utterance": ""}
    assert slow_result_admissible(st, 100.0, r) == (False, "type=無")


def test_slow_result_admissible_within_cooldown_is_blocked():
    """LLM 評分期間快路已經開口（interventions 已更新）——回來仍在冷卻期內要擋下，
    不能讓 Chair.request() 接受一個 30 秒內的第二次介入。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.interventions = [90.0]  # now=100 → since_last_intervention=10s < COOLDOWN_SECONDS(30s)
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "utterance": "請回到主題"}
    assert slow_result_admissible(st, 100.0, r) == (False, "冷卻")


def test_slow_result_admissible_normal_case_passes():
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "utterance": "請回到主題"}
    assert slow_result_admissible(st, 100.0, r) == (True, "")


# ── live.meeting_is_closing（收尾閘門）──────────────────────────────
# 正例的逐字稿取自 experiments/holdout/2026-08-29-two-person 收尾段真的講出來的話
# （t=641.0／645.7／688.8／692.7），不是編出來的中文收尾語。


def _closing_state() -> MeetingState:
    """2026-08-29 雙人會議收尾段的四句（原話），時間軸照原始事件檔。"""
    st = MeetingState(topic="黑客松籌備", duration_min=30, participants=["Alex Huang", "MiMi"])
    st.add(Utterance("Alex Huang", "好，那我來結束，我要加那個。反正到記錄，我要渣了，各位，拜拜。",
                     638.0, 641.0))
    st.add(Utterance("Alex Huang", "嗯......OK，那錄下來囉，拜拜，再見。", 643.0, 645.7))
    st.add(Utterance("Alex Huang", "好，拜拜。", 687.0, 688.8))
    st.add(Utterance("MiMi", "那我要離開聊天室嗎？好，那我去下線囉！哎呀！", 690.0, 692.7))
    return st


def test_meeting_is_closing_detects_real_farewell_sequence():
    """實測誤報點 t≈701.3s：往回 90 秒有四句道別／下線 → 判定收尾。"""
    assert live.meeting_is_closing(_closing_state(), 701.3) is True


def test_meeting_is_closing_ignores_stale_farewell_outside_lookback():
    """同一批道別，但已經滑出 90 秒回看窗——閘門自動過期，不會鎖住整場。"""
    st = _closing_state()
    assert live.meeting_is_closing(st, 692.7 + live.CLOSING_LOOKBACK_SECONDS + 1.0) is False


def test_meeting_is_closing_single_farewell_is_not_enough():
    """只有一句道別（例如中途有人先走，其他人回一句「拜拜」）不算收尾。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "那我先走囉，拜拜。", 300.0, 302.0))
    st.add(Utterance("B", "所以攤位那邊我們要準備幾張桌子？", 305.0, 309.0))
    assert live.meeting_is_closing(st, 310.0) is False


def test_meeting_is_closing_rejects_ambiguous_wrapup_words_mid_meeting():
    """像收尾但其實不是：會議中段的「先這樣」「這件事到這邊」「離開這個話題」——
    這些詞刻意不進詞表，閘門必須放行，慢路照常可以出聲。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "好，那這件事先這樣，我們往下一題。", 300.0, 303.0))
    st.add(Utterance("B", "對，先離開這個話題，等資料回來再談。", 304.0, 308.0))
    st.add(Utterance("A", "那攤位的部分呢？", 309.0, 311.0))
    assert live.meeting_is_closing(st, 312.0) is False


def test_meeting_is_closing_empty_transcript():
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    assert live.meeting_is_closing(st, 100.0) is False


def test_slow_result_admissible_blocked_while_closing():
    """收尾段的「拉回議題」——實測誤報的話術，必須被擋下，原因「收尾」。"""
    st = _closing_state()
    r = {"type": "離題", "verdict": "正向介入", "positive": 3, "negative": 2, "none": 2,
         "utterance": "先確認一下：我們是否要回到黑客松籌備並列出下一步？"}
    assert slow_result_admissible(st, 701.3, r) == (False, "收尾")


def test_slow_result_admissible_closing_gate_precedes_cooldown():
    """收尾段常常同時落在快路剛出聲的冷卻期內；reason 要指出收尾，不能被冷卻遮住。"""
    st = _closing_state()
    st.interventions = [698.7]  # 快路在 t=698.7s 講過話（原始事件檔的第 10 次「發言超時」）
    r = {"type": "離題", "verdict": "正向介入", "positive": 3, "negative": 2, "none": 2,
         "utterance": "先確認一下：我們是否要回到黑客松籌備並列出下一步？"}
    assert slow_result_admissible(st, 701.3, r) == (False, "收尾")


def test_slow_result_admissible_not_blocked_during_offtopic_chat():
    """A1／A2 那段真正該介入的離題閒聊裡沒有任何道別詞——閘門不能擋。"""
    st = MeetingState(topic="黑客松籌備", duration_min=30, participants=["Alex Huang", "MiMi"])
    st.add(Utterance("MiMi", "你要不要幫我帶便當？", 240.0, 242.0))
    st.add(Utterance("Alex Huang", "好啊，那你先借我一千塊。", 244.0, 247.0))
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "utterance": "我們回到黑客松籌備吧。"}
    assert slow_result_admissible(st, 252.0, r) == (True, "")


# ── Session.note_speaker（T7b：revision 只在發言者換人時遞增）─────────


def test_revision_only_bumps_on_speaker_change():
    """同一人連講多句（每句都是一次 STT commit）不該讓 revision 變——
    否則軟插入等到的那個停頓本身就會帶來一次 commit，介入永遠來不及開口就被作廢。"""
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A", "B"]))
    session.note_speaker("A")
    session.note_speaker("A")
    session.note_speaker("A")
    assert session.revision == 1
    session.note_speaker("B")
    assert session.revision == 2
    session.note_speaker("B")
    assert session.revision == 2


# ── I1／I2：claim 生命週期與目標開口作廢（consume 走假 pool，不連 STT）──────


class FakePool:
    """只吐固定幾個事件就結束的假 STT pool——consume 的 async for 會自然收尾。"""

    def __init__(self, events):
        self.events = events

    async def utterances(self):
        for ev in self.events:
            yield ev


def _session(participants=("A", "B")) -> Session:
    return Session(MeetingState(topic="t", duration_min=30, participants=list(participants)))


def test_note_speaker_returns_previous_speaker():
    """I1c：換人時要解除上一位的「發言超時」claim，所以呼叫端得知道上一位是誰。"""
    session = _session()
    assert session.note_speaker("A") is None
    assert session.note_speaker("A") == "A"
    assert session.note_speaker("B") == "A"


def test_speaker_change_rearms_overtime_claim():
    """I1c：A 超時被提醒過、B 接著發言 → A 的「發言超時」claim 要解除。

    不解除的話，A 隔一段時間再度連講三分鐘，live 路徑完全不會提醒——
    跟 run.py 回放路徑（prev_speaker 換人就 discard）行為不一致。
    """
    session = _session()
    session.done.add(("發言超時", "A"))
    asyncio.run(session.consume(FakePool([
        Utterance("A", "一", 0.0, 1.0),
        Utterance("B", "二", 1.0, 2.0),
    ])))
    assert ("發言超時", "A") not in session.done


def test_failed_intervention_releases_claim():
    """I1a：TTS 失敗（沒出聲）後同一個觸發要能重試——claim 必須解除。
    節流由 Chair 的 30 秒退避負責，不是靠 claim 卡著。"""
    session = _session()
    session.done.add(("發言超時", "A"))
    session.release_claim(Intervention(kind="發言超時", target="A", text="x", hard=True,
                                        revision=0, created_at=0.0))
    assert ("發言超時", "A") not in session.done


# ── I2：被點名的人自己開口了 → 候選作廢 ──────────────────────────────


class FakeChair:
    """只提供 Chair 的兩個槽位——note_target_spoke 只看這兩個。"""

    def __init__(self, pending=None, candidate=None):
        self.pending = pending
        self.candidate = candidate


def _iv(target, kind="有人被冷落"):
    return Intervention(kind=kind, target=target, text="x", hard=False, revision=0, created_at=0.0)


def test_pending_target_spoke_bumps_revision():
    session = _session()
    session.chair = FakeChair(pending=_iv("A"))
    before = session.revision
    assert session.note_target_spoke("A") is True
    assert session.revision == before + 1


def test_candidate_target_spoke_bumps_revision():
    session = _session()
    session.chair = FakeChair(candidate=_iv("A"))
    before = session.revision
    assert session.note_target_spoke("A") is True
    assert session.revision == before + 1


# ── T21：live.resurrect_room_level／Session.on_dropped ──────────────────
# room-level（target=None）介入不該因為單純換人講話就被 Chair 的 revision
# 機制作廢——換人講話不代表話題自己回到正軌了；只有 target 綁著特定對象的
# 介入（發言超時／有人被冷落）才該在換人講話後仍然作廢。


def _room_iv(kind="離題", text="請回到主題", rev=0, t=100.0):
    return Intervention(kind=kind, target=None, text=text, hard=False, revision=rev, created_at=t)


def test_resurrect_room_level_exempts_target_none_from_speaker_change():
    """target=None＋revision 過期＋還沒超過存活上限 → 重生（換上新 revision）。"""
    iv = _room_iv(rev=0, t=100.0)
    fresh = resurrect_room_level(iv, "revision 過期", now=105.0, revision=3)
    assert fresh is not None
    assert fresh.revision == 3
    assert fresh.kind == "離題" and fresh.text == "請回到主題" and fresh.hard is False
    assert fresh.created_at == 100.0  # 存活時鐘跨重生不變，用來累計年齡


def test_resurrect_room_level_keeps_dropping_target_specific():
    """target 是某人（發言超時／有人被冷落）→ 換人講話後仍然作廢，不重生（驗收 2）。"""
    iv = Intervention(kind="有人被冷落", target="A", text="A，你怎麼看？",
                       hard=False, revision=0, created_at=100.0)
    assert resurrect_room_level(iv, "revision 過期", now=105.0, revision=3) is None


def test_resurrect_room_level_ignores_non_revision_drop_reasons():
    """「被硬打斷取代」「播放器逾時，候選作廢」是呼叫端／Chair 自己判定真的該收掉，
    不是 revision 過期造成的誤傷——不能救。"""
    iv = _room_iv()
    assert resurrect_room_level(iv, "被硬打斷取代", now=105.0, revision=3) is None
    assert resurrect_room_level(iv, "播放器逾時，候選作廢", now=105.0, revision=3) is None


def test_resurrect_room_level_keeps_genuine_rule_no_longer_holds_dropped():
    """「升級時已不成立」是規則重驗後確認話題真的回正軌了——這才是真正的
    「介入太老／已經不成立」保護，不能被重生蓋掉（驗收 3）。"""
    iv = _room_iv()
    assert resurrect_room_level(iv, "升級時已不成立", now=105.0, revision=3) is None


def test_resurrect_room_level_handles_escalate_stale_revision_reason():
    """升級路徑（15 秒後）重生的 fresh 若 revision 也過期，一樣要用同一套判準
    （驗收 5：escalate 路徑與新判準互動正確）。"""
    iv = _room_iv()
    fresh = resurrect_room_level(iv, "升級重生的介入 revision 已過期", now=105.0, revision=9)
    assert fresh is not None and fresh.revision == 9


def test_resurrect_room_level_caps_by_age():
    """換人換不停、一直等不到停頓——存活時間一旦達到 ESCALATE_SECONDS（沿用
    既有門檻，不另外發明新數字）就升級成硬打斷重生，不再是軟插入等安靜
    （驗收 3，2026-09-03 修正：原本這裡是回傳 None 真的作廢，但那條路根本
    不會經過 Chair 自己的硬打斷判斷，三人快速交替會議實測會讓介入永遠沒機會
    講出口，見 `resurrect_room_level` docstring）。"""
    iv = _room_iv(t=100.0)
    still_fresh = resurrect_room_level(iv, "revision 過期",
                                        now=100.0 + ESCALATE_SECONDS - 0.01, revision=1)
    assert still_fresh is not None and still_fresh.hard is False
    escalated = resurrect_room_level(iv, "revision 過期", now=100.0 + ESCALATE_SECONDS, revision=1)
    assert escalated is not None and escalated.hard is True


class _RecordingChair:
    """只提供 on_dropped 測試需要的介面：`request()` 記錄呼叫，不跑真的狀態機。"""

    def __init__(self):
        self.requested: list[Intervention] = []

    def request(self, iv: Intervention) -> bool:
        self.requested.append(iv)
        return True


def test_session_on_dropped_resurrects_room_level_without_releasing_claim():
    session = _session()
    session.revision = 3
    session.chair = _RecordingChair()
    session.done.add(("離題", None))
    iv = _room_iv(rev=0, t=session.now)

    session.on_dropped(iv, "revision 過期")

    assert len(session.chair.requested) == 1
    assert session.chair.requested[0].revision == 3
    assert ("離題", None) in session.done  # 沒被真的放棄，claim 不能解除
    assert not any(e.kind == "dropped" for e in session.events)  # 也不該回報「已作廢」


def test_session_on_dropped_falls_through_for_target_specific():
    session = _session()
    session.revision = 3
    session.chair = _RecordingChair()
    session.done.add(("有人被冷落", "A"))
    iv = Intervention(kind="有人被冷落", target="A", text="A，你怎麼看？",
                       hard=False, revision=0, created_at=session.now)

    session.on_dropped(iv, "revision 過期")

    assert session.chair.requested == []
    assert ("有人被冷落", "A") not in session.done  # 真的放棄，claim 要解除
    dropped = [e for e in session.events if e.kind == "dropped"]
    assert dropped and dropped[0].data["reason"] == "revision 過期"


class _BackoffBlockedChair:
    """模擬 `chair.request()` 被 `speaker.py` 的退避（`_backoff_until`／
    `FAIL_BACKOFF`）擋下、回 False——重生「想排」不等於「排得進去」。"""

    def __init__(self):
        self.requested: list[Intervention] = []

    def request(self, iv: Intervention) -> bool:
        self.requested.append(iv)
        return False


def test_session_on_dropped_falls_through_when_resurrect_request_is_backed_off():
    """T21 review finding：`resurrect_room_level` 判斷可以重生，但
    `chair.request()` 可能因為同 kind 的硬打斷剛因 TTS 失敗退避過（見
    speaker.py `_backoff_until`）而回 False——這句話根本沒被排入，
    不能沿用「revision 過期」這個舊理由蒙混過去（那會讓這個介入卡在「評估中」
    永遠沒有下文，log 也謊報「重生」成功）。要 release claim、要 emit
    「dropped」、log 不能出現「重生」字樣，理由要能看出是「重生時撞上退避」。
    """
    session = _session()
    session.revision = 3
    session.chair = _BackoffBlockedChair()
    session.done.add(("離題", None))
    iv = _room_iv(rev=0, t=session.now)

    session.on_dropped(iv, "revision 過期")

    assert len(session.chair.requested) == 1  # 有嘗試重生
    assert ("離題", None) not in session.done  # 真的沒排入，claim 要解除
    dropped = [e for e in session.events if e.kind == "dropped"]
    assert dropped  # 有 emit「dropped」，不會卡在「評估中」
    assert dropped[0].data["reason"] != "revision 過期"  # 理由要講實話，不能蒙混
    # log 要說「作廢」，不能沿用成功分支的「主席重生【...】」——那句話根本沒被排入
    assert not any("主席重生" in line for line in session.log)
    assert any("主席作廢" in line for line in session.log)


def test_other_speaker_leaves_revision_alone():
    session = _session()
    session.chair = FakeChair(pending=_iv("A"))
    before = session.revision
    assert session.note_target_spoke("B") is False
    assert session.revision == before


def test_note_target_spoke_without_chair_is_noop():
    session = _session()
    before = session.revision
    assert session.note_target_spoke("A") is False
    assert session.revision == before


def test_consume_invalidates_pending_when_its_target_speaks():
    """A 沉默五分鐘 → 排了「有人被冷落：A」；A 自己開口後主席不該再問他為什麼不說話。

    _last_speaker 先設成 A，讓「換人」那條路徑不介入，單獨驗這條規則。
    """
    session = _session()
    session.note_speaker("A")
    session.chair = FakeChair(pending=_iv("A"))
    before = session.revision
    asyncio.run(session.consume(FakePool([Utterance("A", "我在", 0.0, 1.0)])))
    assert session.revision == before + 1


# ── I5：離會的人不能被點名 ──────────────────────────────────────────


def test_neglected_skips_absent():
    """已離開語音頻道的人不算「被冷落」——主席不該對著一個不在的人喊話。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last = {"A": 0.0}  # A 上次發言在 0 秒，now=400 → 已沉默 400 秒
    assert "有人被冷落" in [t.kind for t in fast_path.check(st, 400.0, set())]
    st.absent.add("A")
    assert "有人被冷落" not in [t.kind for t in fast_path.check(st, 400.0, set())]


# ── T11 缺陷 A：全場沉默（fast_path.check 新規則）───────────────────
#
# 跟「有人被冷落」問的是不同問題：那條看「某一個人」是不是被晾在一旁，
# 門檻是 NEGLECTED_SECONDS（5 分鐘）；這條看「整個房間」是不是都停了，
# 門檻是 SILENCE_SECONDS（45 秒），取所有在場者 silent_seconds 的最小值。


def test_room_silence_below_threshold_no_trigger():
    """驗收 1：全場沉默未達門檻（SILENCE_SECONDS）→ 不觸發。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.prior_last = {"A": 0.0, "B": 0.0}
    now = fast_path.SILENCE_SECONDS - 1
    assert "全場沉默" not in [t.kind for t in fast_path.check(st, now, set())]


def test_room_silence_reaches_threshold_triggers():
    """驗收 2：全場沉默達到門檻 → 觸發一次，target 為 None（不是對著特定某個人講）。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.prior_last = {"A": 0.0, "B": 0.0}
    now = fast_path.SILENCE_SECONDS
    triggers = [t for t in fast_path.check(st, now, set()) if t.kind == "全場沉默"]
    assert len(triggers) == 1
    assert triggers[0].target is None


def test_room_silence_not_repeated_while_still_silent():
    """驗收 3：觸發過一次後（已經在 done 裡），持續冷場不再重複觸發——避免碎念。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last = {"A": 0.0}
    now = fast_path.SILENCE_SECONDS + 100  # 遠超過門檻，持續沉默
    done = {("全場沉默", None)}
    assert "全場沉默" not in [t.kind for t in fast_path.check(st, now, done)]


def test_room_silence_needs_only_one_present_speaker_within_threshold():
    """驗收 5：多人情境——只要還有一位在場者在門檻內講過話，最小值就被壓低，不算全場沉默。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    now = 100.0
    st.add(Utterance("A", "剛剛才講完", now - 5.0, now - 4.0))  # A 4 秒前才講完，遠低於門檻
    st.prior_last = {"B": 0.0}  # B 已經沉默 100 秒，遠超門檻
    assert "全場沉默" not in [t.kind for t in fast_path.check(st, now, set())]


def test_room_silence_skips_absent_participants():
    """驗收 6：已離會的人不列入全場沉默的計算——跟「有人被冷落」的既有處理一致。

    A 剛講完話（若算進「全場」會把 min 拉低，不算全場沉默）；B 已沉默 100 秒。
    A 仍在場時，他的「剛講完話」正確地代表房間還沒全靜；A 離會後，房間只剩 B，
    B 早已沉默超過門檻，這時才該算全場沉默——這正是「排除離會者」要保護的情境。
    """
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    now = 100.0
    st.add(Utterance("A", "剛講完", now - 1.0, now - 0.5))  # A 在場、剛講完話
    st.prior_last = {"B": 0.0}  # B 沉默 100 秒
    assert "全場沉默" not in [t.kind for t in fast_path.check(st, now, set())]  # A 在場、剛講完話，房間不算靜
    st.absent.add("A")  # A 離會 → 房間只剩 B，B 早已沉默超過門檻
    assert "全場沉默" in [t.kind for t in fast_path.check(st, now, set())]


def test_utterance_for_room_silence():
    t = Trigger(kind="全場沉默", target=None, detail="全場已 1.0 分鐘沒有人發言", hard=False)
    assert utterance_for(t) == "現場安靜了一陣子，要不要有人先分享一下目前的想法？"


def test_room_silence_claim_released_when_anyone_speaks():
    """驗收 4：全場沉默的 claim（target 為 None）一旦被任何人的發言解除，
    之後再度冷場達門檻要能再次觸發——不然整場只會提醒一次（陷阱：target=None
    不會被「有人被冷落」那行按 speaker 名字 discard 的邏輯連帶解除，須另外處理）。
    """
    session = _session(["A"])
    session.done.add(("全場沉默", None))
    asyncio.run(session.consume(FakePool([Utterance("A", "打破沉默", 0.0, 1.0)])))
    assert ("全場沉默", None) not in session.done


# ── T13 缺陷 B：門檻依實測間隔分佈調到 90s ───────────────────────────


def test_silence_seconds_threshold_updated_to_90():
    """驗收 6：2026-08-28 晚實測的 19 段間隔分佈裡，90s 只抓到那段真冷場
    （128.6s），不誤觸發任何一段正常思考停頓（最長 52.4s）。"""
    assert fast_path.SILENCE_SECONDS == 90.0


# ── T13 缺陷 C：話術輪替 + 退避 ──────────────────────────────────────


def test_room_silence_utterance_varies_by_trigger_count():
    """驗收 7：同一場裡連續兩次全場沉默的話術不相同。第一次（variant=0）
    的文字必須維持跟修正前逐字相同——使用者已經聽過、線上既有測試也鎖著它。"""
    t0 = Trigger(kind="全場沉默", target=None, detail="d", hard=False, variant=0)
    t1 = Trigger(kind="全場沉默", target=None, detail="d", hard=False, variant=1)
    assert utterance_for(t0) == "現場安靜了一陣子，要不要有人先分享一下目前的想法？"
    assert utterance_for(t1) != utterance_for(t0)


def test_room_silence_trigger_carries_current_hit_count_as_variant():
    """check() 產生的 Trigger.variant 要對應目前這場會議已經觸發過幾次，
    utterance_for() 才能選到正確的輪替版本。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last["A"] = 0.0
    st.room_silence_hits = 2
    now = fast_path.SILENCE_SECONDS * fast_path.SILENCE_BACKOFF_FACTOR ** 2
    triggers = [t for t in fast_path.check(st, now, set()) if t.kind == "全場沉默"]
    assert len(triggers) == 1
    assert triggers[0].variant == 2


def test_room_silence_threshold_increases_after_each_hit():
    """驗收 8：同一場裡第二次全場沉默需要的沉默時間大於第一次，第三次又大於
    第二次——退避讓重複提醒愈來愈稀疏，不是固定門檻。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last["A"] = 0.0

    st.room_silence_hits = 0
    first_threshold = fast_path.SILENCE_SECONDS
    assert "全場沉默" in [t.kind for t in fast_path.check(st, first_threshold, set())]

    # 第一次觸發過後（hits=1），同樣的沉默秒數不該再算數——門檻變長了
    st.room_silence_hits = 1
    assert "全場沉默" not in [t.kind for t in fast_path.check(st, first_threshold, set())]
    second_threshold = fast_path.SILENCE_SECONDS * fast_path.SILENCE_BACKOFF_FACTOR
    assert second_threshold > first_threshold
    assert "全場沉默" in [t.kind for t in fast_path.check(st, second_threshold, set())]

    # 第二次觸發過後（hits=2），第二次的門檻也不該再算數——要再拉長
    st.room_silence_hits = 2
    assert "全場沉默" not in [t.kind for t in fast_path.check(st, second_threshold, set())]


def test_silence_backoff_factor_is_a_documented_module_constant():
    """驗收 9：退避參數是模組常數，且必須 > 1（門檻真的會被拉高，不是原地踏步）。"""
    assert fast_path.SILENCE_BACKOFF_FACTOR > 1.0


def test_session_fast_tick_backs_off_and_varies_wording_across_repeats():
    """整合：Session._fast_tick 真正跑兩次全場沉默——第二次排入時話術要不同、
    退避次數已經遞增（用遠超門檻的 now 差值，避開邊界時序的計時誤差）。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last["A"] = 0.0
    session = Session(st)
    session.chair = RecordingChair()
    session.t0 = time.perf_counter() - 10 * fast_path.SILENCE_SECONDS  # 遠超第一次門檻

    session._fast_tick(None)
    assert len(session.chair.requests) == 1
    first_text = session.chair.requests[0].text
    assert st.room_silence_hits == 1

    session.done.discard(("全場沉默", None))  # 模擬有人講過話，claim 被釋放
    session.t0 -= 10 * fast_path.SILENCE_SECONDS  # 讓 now 遠超退避後的第二次門檻
    session._fast_tick(None)
    assert len(session.chair.requests) == 2
    second_text = session.chair.requests[1].text
    assert second_text != first_text
    assert st.room_silence_hits == 2


# ── T13 缺陷 D：--say-hello 問候尚未送出前，快路不能搶話 ──────────────


def test_fast_tick_blocks_all_intervention_while_greeting_pending():
    """驗收 10：--say-hello 開啟且問候尚未送出時，快路不送任何介入——但
    fast_timer 心跳要照常 emit，觀戰 UI 的計時器才不會在等真人進場時卡在
    00:00（T13 review 發現：曾經連 emit 都被一起擋掉，是過度實作）。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last["A"] = 0.0
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    session.t0 = time.perf_counter() - 10 * fast_path.SILENCE_SECONDS  # 全場沉默門檻早就過了
    session._fast_tick(gate)
    assert session.chair.requests == []
    assert "queued" not in [e.kind for e in session.events]
    assert "fast_timer" in [e.kind for e in session.events]


def test_fast_tick_resumes_after_greeting_sent():
    """驗收 11：問候送出之後（gate.greeted=True），快路恢復正常。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last["A"] = 0.0
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    gate.greeted = True
    session.t0 = time.perf_counter() - 10 * fast_path.SILENCE_SECONDS
    session._fast_tick(gate)
    assert len(session.chair.requests) == 1


def test_fast_tick_unaffected_when_no_say_hello():
    """驗收 12：沒有 --say-hello（hello_gate=None）時，快路行為完全不變——
    不能因為這個修正而永遠被擋住。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.prior_last["A"] = 0.0
    session = Session(st)
    session.chair = RecordingChair()
    session.t0 = time.perf_counter() - 10 * fast_path.SILENCE_SECONDS
    session._fast_tick(None)
    assert len(session.chair.requests) == 1


# ── P4：emit_share 統一分母，加總必須等於 1.0 ──────────────────────────


def test_emit_share_totals_to_one_with_chair():
    """兩位參與者各講 30s、主席 10 次介入 × 3.0 秒估算 = 30s → 分母統一為
    30+30+30=90，比例應為 {A:1/3, B:1/3, 主席:1/3}，加總=1.0。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "x", 0.0, 30.0))
    st.add(Utterance("B", "y", 30.0, 60.0))
    st.interventions = [float(i) for i in range(10)]  # 10 次 × 3.0 秒估算 = 30s
    session = Session(st)
    session.emit_share()
    data = session.events[-1].data
    assert data["A"] == pytest.approx(1 / 3)
    assert data["B"] == pytest.approx(1 / 3)
    assert data["主席"] == pytest.approx(1 / 3)
    assert sum(data.values()) == pytest.approx(1.0, abs=1e-9)


def test_emit_share_all_zero_when_nobody_spoke():
    """無人發言（也沒有介入）→ 分母為 0，所有值都是 0.0，不能除以零炸掉。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    session = Session(st)
    session.emit_share()
    data = session.events[-1].data
    assert data == {"A": 0.0, "B": 0.0, "主席": 0.0}


def test_emit_share_matches_state_share_when_chair_silent():
    """主席 0 次介入時，參與者的比例要跟 state.share()（不含主席的分母）一致——
    因為分母裡 chair_seconds=0，兩邊分母其實相等，這是驗收 A3 的第二個情境。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "x", 0.0, 40.0))
    st.add(Utterance("B", "y", 40.0, 60.0))
    session = Session(st)
    session.emit_share()
    data = session.events[-1].data
    now = session.now
    assert data["A"] == pytest.approx(st.share("A", now))
    assert data["B"] == pytest.approx(st.share("B", now))
    assert data["主席"] == 0.0


# ── T8：--say-hello 問候時機（頻道空的時候不問候，等第一個真人才問候一次）──


class RecordingChair:
    """只記錄 request() 呼叫，不做任何節流／排程——驗證問候有沒有送出、送了幾次。"""

    def __init__(self):
        self.requests: list[Intervention] = []

    def request(self, iv: Intervention) -> bool:
        self.requests.append(iv)
        return True


# channel_has_human：純函式，只看 MeetingState.participants／absent


def test_channel_has_human_false_when_no_participants():
    st = MeetingState(topic="t", duration_min=30, participants=[])
    assert channel_has_human(st) is False


def test_channel_has_human_true_when_present():
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    assert channel_has_human(st) is True


def test_channel_has_human_false_when_all_absent():
    """曾經在場但已經離開（discord_source.on_voice_state_update 的 left 分支）不算在場。"""
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    st.absent.add("Alex")
    assert channel_has_human(st) is False


# HelloGate：獨立狀態機，不碰 Chair／MeetingState
#
# T11 缺陷 B：真人剛加入語音頻道時，Discord 客戶端往往還在建立語音連線，
# 這時候問候使用者只會聽到半句（真實回報）。should_greet 因此多了 has_audio／now
# 兩個參數：真人在場只是先決條件，還要等音訊訊號確認（note_audio）或逾時
# （HELLO_AUDIO_TIMEOUT_SECONDS）才真正放行。


def test_hello_gate_never_fires_without_human():
    gate = HelloGate()
    assert gate.should_greet(False, 0.0) is False
    assert gate.should_greet(False, 100.0) is False
    assert gate.greeted is False


def test_hello_gate_waits_for_audio_before_greeting():
    """驗收 2：真人在場但音訊還沒到——即使已經等了一段時間（只要沒超過逾時）都不問候。"""
    gate = HelloGate()
    assert gate.should_greet(True, 0.0) is False  # 剛出現，還沒音訊
    assert gate.should_greet(True, 1.0) is False  # 過了 1 秒，音訊還沒到，也還沒逾時
    assert gate.greeted is False


def test_hello_gate_greets_once_audio_confirmed():
    """驗收 3：音訊訊號一到，立刻問候，且只問候一次。"""
    gate = HelloGate()
    gate.should_greet(True, 0.0)  # 真人出現
    gate.note_audio()  # 收到音訊
    assert gate.should_greet(True, 0.5) is True
    assert gate.greeted is True
    assert gate.should_greet(True, 1.0) is False  # 已問候過，之後恆為 False


def test_hello_gate_greets_on_timeout_without_audio():
    """驗收 4：真人一直沒有音訊，超過 HELLO_AUDIO_TIMEOUT_SECONDS 逾時後照樣問候一次。"""
    gate = HelloGate()
    gate.should_greet(True, 0.0)  # 真人出現，計時起點
    assert gate.should_greet(True, live.HELLO_AUDIO_TIMEOUT_SECONDS - 0.01) is False  # 還沒逾時
    assert gate.should_greet(True, live.HELLO_AUDIO_TIMEOUT_SECONDS) is True  # 逾時，照樣問候
    assert gate.greeted is True


def test_hello_gate_resets_wait_when_human_leaves_before_greeting():
    """真人短暫出現又離開（問候前）→ 逾時計時要重置，不能讓下一次進場直接被判定逾時。"""
    gate = HelloGate()
    gate.should_greet(True, 0.0)
    gate.should_greet(False, 100.0)  # 離開，尚未問候
    # 若沒重置計時起點，now=100 早已超過 8 秒逾時，會誤判成該問候
    assert gate.should_greet(True, 100.5) is False  # 重新出現，剛過 0.5 秒


# 驗收 5：沒開 --say-hello → main_async 不建 gate，兩處問候路徑靠 None 直接短路


def test_build_hello_gate_disabled_without_flag():
    assert live.build_hello_gate(False) is None


def test_build_hello_gate_enabled_with_flag():
    gate = live.build_hello_gate(True)
    assert isinstance(gate, HelloGate)
    assert gate.greeted is False


# Session.maybe_greet_hello：main_async 實際呼叫的那個決策點


def test_maybe_greet_hello_noop_without_chair():
    """bot 還沒進頻道、Chair 還沒建好時被呼叫到——不能炸，也不能消耗掉 gate。"""
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    session = Session(st)
    gate = HelloGate()
    session.maybe_greet_hello(gate)
    assert gate.greeted is False


def test_maybe_greet_hello_skips_when_channel_empty():
    """驗收 1：bot 加入空頻道（沒有任何非 bot 成員）時不發出問候。"""
    st = MeetingState(topic="t", duration_min=30, participants=[])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    session.maybe_greet_hello(gate)
    assert session.chair.requests == []
    assert gate.greeted is False


def test_maybe_greet_hello_waits_for_audio_when_human_already_present():
    """驗收 2：bot 加入時頻道裡已經有真人，但音訊還沒進來 → 還不問候
    （T8 的「立刻問候」行為在 T11 被改成要等音訊確認，見下一則測試）。"""
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    session.maybe_greet_hello(gate)
    assert session.chair.requests == []
    assert gate.greeted is False


def test_maybe_greet_hello_fires_once_audio_confirmed():
    """驗收 3：收到該真人的第一個音訊封包 → 問候，恰好一次。"""
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    session.maybe_greet_hello(gate)  # 音訊還沒到，不問候
    assert session.chair.requests == []
    session.note_human_audio(gate)  # 模擬 MeetingBot._on_audio 收到 Alex 的音訊
    assert len(session.chair.requests) == 1
    iv = session.chair.requests[0]
    assert iv.kind == "問候"
    assert iv.text == "大家好，我是今天的主席，會議開始。"
    assert gate.greeted is True
    session.note_human_audio(gate)  # 再收到一次音訊不能再問候第二次
    assert len(session.chair.requests) == 1


def test_maybe_greet_hello_fires_once_when_human_joins_and_speaks_later():
    """頻道一開始沒人，之後第一個真人加入且確認有音訊才問候，恰好一次。"""
    st = MeetingState(topic="t", duration_min=30, participants=[])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    session.maybe_greet_hello(gate)  # bot 剛進頻道時檢查一次，頻道還是空的
    assert session.chair.requests == []
    st.ensure_participant("Alex")  # 對應 discord_source.on_voice_state_update 的 joined 分支
    session.maybe_greet_hello(gate)  # 下一次輪詢：人在了，但還沒有音訊
    assert session.chair.requests == []
    session.note_human_audio(gate)  # 收到 Alex 的音訊
    assert len(session.chair.requests) == 1
    session.note_human_audio(gate)  # 再收到一次不重複問候
    assert len(session.chair.requests) == 1


def test_maybe_greet_hello_survives_join_leave_join_greeting_only_once():
    """驗收 6：真人反覆進出（進→出→進），中途確認過音訊 → 全程只問候一次，不重複。"""
    st = MeetingState(topic="t", duration_min=30, participants=[])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    st.ensure_participant("Alex")  # 進
    session.note_human_audio(gate)  # 確認音訊 → 問候
    assert len(session.chair.requests) == 1
    st.absent.add("Alex")  # 出
    session.maybe_greet_hello(gate)
    assert len(session.chair.requests) == 1
    st.absent.discard("Alex")  # 再進
    session.maybe_greet_hello(gate)
    assert len(session.chair.requests) == 1


# Session.watch_hello：main_async 排入 tasks 的輪詢迴圈本身


def test_watch_hello_returns_immediately_when_already_greeted():
    """bot 進頻道當下音訊已經確認過（驗收 3 的情境）→ watch_hello 一啟動就該收工，不用等待。"""
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()
    session.note_human_audio(gate)  # 模擬 start_chair 建 Chair 前就已經收過音訊
    assert gate.greeted is True
    asyncio.run(asyncio.wait_for(session.watch_hello(gate), timeout=0.2))
    assert len(session.chair.requests) == 1  # watch_hello 沒有再問候一次


def test_watch_hello_polls_until_human_joins_and_audio_confirmed(monkeypatch):
    """驗收 3 的接線版本：頻道一開始沒人，真人加入且音訊確認後 watch_hello 才問候一次。"""
    monkeypatch.setattr(live, "HELLO_POLL_SECONDS", 0.02)
    st = MeetingState(topic="t", duration_min=30, participants=[])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()

    async def join_then_speak():
        await asyncio.sleep(0.06)  # 落在第 2、3 次輪詢之間，確保先看過一次「還是空的」
        st.ensure_participant("Alex")
        await asyncio.sleep(0.06)  # 讓「人在了但還沒音訊」的狀態先被輪詢看到至少一次
        session.note_human_audio(gate)  # 模擬 MeetingBot._on_audio 收到音訊

    async def run():
        await asyncio.gather(session.watch_hello(gate), join_then_speak())

    asyncio.run(asyncio.wait_for(run(), timeout=2.0))
    assert len(session.chair.requests) == 1
    assert gate.greeted is True


# ── T14：快路／問候的話術句型庫（phrasing.py）接線到 live.py ──────────
#
# 純模組層的驗證邏輯（validate_pattern、PhraseBank 生命週期）在
# tests/test_phrasing.py；這裡驗證接線：Session._fast_tick／
# maybe_greet_hello／escalate_with_current_facts 真的把 phrase_bank
# 傳下去、取用路徑不呼叫生成器、watch_phrasing 不擋住其他任何東西。


def test_session_default_phrase_bank_has_no_generator():
    """驗收 12 的地基：不傳 phrase_bank（既有所有測試、也就是等同沒有這個
    功能的呼叫方式）→ can_generate 恆為 False，行為與 T14 之前完全一致。"""
    session = _session()
    assert session.phrase_bank.can_generate() is False
    assert session.phrase_bank.take("全場沉默") is None


def test_fast_tick_uses_phrase_bank_pattern_with_correct_facts():
    """驗收 7：快路真正排入的話術用了句型庫裡的變體，且名字／分鐘數與
    Trigger 的事實完全一致。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.speaking_now("A", 0.0)
    bank = PhraseBank()
    bank._queues["發言超時"].append("{target}，你已經連續講了{mins}分鐘，可以先停一下嗎？")
    session = Session(st, phrase_bank=bank)
    session.chair = RecordingChair()
    session.t0 = time.perf_counter() - (fast_path.OVERTIME_SECONDS + 1)

    session._fast_tick(None)

    assert len(session.chair.requests) == 1
    assert session.chair.requests[0].text == "A，你已經連續講了3分鐘，可以先停一下嗎？"


def test_fast_tick_falls_back_to_static_template_when_bank_empty():
    """驗收 2：佇列為空（預設的 Session()）→ 主席照常用寫死模板開口，不受影響。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.speaking_now("A", 0.0)
    session = Session(st)
    session.chair = RecordingChair()
    session.t0 = time.perf_counter() - (fast_path.OVERTIME_SECONDS + 1)

    session._fast_tick(None)

    assert len(session.chair.requests) == 1
    assert session.chair.requests[0].text == "A，你已經講了3分鐘，先讓其他人接一下。"


def test_escalate_with_current_facts_uses_bank_pattern():
    """升級路徑（軟插入等超過 15s）一樣可以取到句型庫裡的變體，不必因為是
    升級就退回寫死模板。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.speaking_now("A", 50.0)
    bank = PhraseBank()
    bank._queues["發言超時"].append("{target} 已經講了{mins}分鐘，換個人說說看吧。")
    iv = Intervention(kind="發言超時", target="A", text="stale", hard=False, revision=0, created_at=50.0)

    fresh = escalate_with_current_facts(st, 250.0, 5, iv, bank)

    assert fresh is not None
    assert fresh.text == "A 已經講了3分鐘，換個人說說看吧。"


def test_escalate_with_current_facts_without_bank_is_unaffected():
    """不傳 bank（既有呼叫方式，4 個位置參數）行為完全不變——回歸測試。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.speaking_now("A", 50.0)
    iv = Intervention(kind="發言超時", target="A", text="stale", hard=False, revision=0, created_at=50.0)
    fresh = escalate_with_current_facts(st, 250.0, 5, iv)
    assert fresh is not None
    assert fresh.text == "A，你已經講了3分鐘，先讓其他人接一下。"


def test_maybe_greet_hello_never_calls_generator():
    """驗收 1／9：問候的送出路徑完全不呼叫生成器——就算生成器一被呼叫就炸，
    問候也要能照常準時送出（用寫死模板，因為佇列本來就是空的）。"""
    def boom(kind, topic):
        raise AssertionError("問候不該在送出當下呼叫生成器")

    bank = PhraseBank(generator=boom)
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    session = Session(st, phrase_bank=bank)
    session.chair = RecordingChair()
    gate = HelloGate()

    session.note_human_audio(gate)  # 觸發問候判斷

    assert len(session.chair.requests) == 1
    assert session.chair.requests[0].text == "大家好，我是今天的主席，會議開始。"
    assert gate.greeted is True


def test_maybe_greet_hello_uses_bank_pattern_when_available():
    """驗收 10：問候句型已經生成好時，實際採用生成的版本，且帶入議題。"""
    bank = PhraseBank()
    bank._queues["問候"].append("哈囉大家，今天要聊「{topic}」，開始吧！")
    st = MeetingState(topic="黑客松籌備", duration_min=30, participants=["Alex"])
    session = Session(st, phrase_bank=bank)
    session.chair = RecordingChair()
    gate = HelloGate()

    session.note_human_audio(gate)

    assert len(session.chair.requests) == 1
    assert session.chair.requests[0].text == "哈囉大家，今天要聊「黑客松籌備」，開始吧！"


def _valid_candidate(kind: str) -> str:
    """每個 kind 各給一句合格的句型，供 watch_phrasing 測試用的假生成器共用。"""
    return {
        "發言超時": "{target}，已經講了{mins}分鐘，先緩一緩吧。",
        "有人被冷落": "{target}，要不要也分享一下你的想法？",
        "議程超時": "剩下{mins}分鐘，我們開始收斂吧。",
        "全場沉默": "現場靜了一下，要不要有人先開口？",
        "問候": "大家好，我們開始今天的討論吧。",
    }[kind]


def test_watch_phrasing_fills_every_kind_once_at_start():
    """驗收：會議開始後背景幫每一種 kind 都補一次句型——不用等某個 kind
    第一次觸發才第一次生成。用短逾時把迴圈在進入補充輪詢前掐斷，只驗開場
    那一輪的結果（後面是無限迴圈，等的是真實會議時長）。
    """
    bank = PhraseBank(generator=lambda kind, topic: [_valid_candidate(kind)])
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]),
                       phrase_bank=bank)

    async def run():
        try:
            await asyncio.wait_for(session.watch_phrasing(), timeout=0.3)
        except asyncio.TimeoutError:
            pass  # 開場輪跑完後進了背景輪詢的 sleep，逾時屬預期

    asyncio.run(run())

    for kind in PHRASE_KINDS:
        assert bank.take(kind) == _valid_candidate(kind)


def test_watch_phrasing_stops_at_generation_cap(monkeypatch):
    """驗收 11：同一場會議的生成次數不超過模組常數上限——上限調低到 2 之後，
    5 個 kind 只有前 2 個補得到，第 3 個開始 can_generate() 已經是 False。"""
    import meeting_host.phrasing as phrasing_module
    monkeypatch.setattr(phrasing_module, "MAX_GENERATIONS_PER_MEETING", 2)
    calls: list[str] = []

    def counting_generator(kind, topic):
        calls.append(kind)
        return [_valid_candidate(kind)]

    bank = PhraseBank(generator=counting_generator)
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]),
                       phrase_bank=bank)

    asyncio.run(asyncio.wait_for(session.watch_phrasing(), timeout=1.0))

    assert len(calls) == 2
    assert bank.generations == 2


def test_watch_phrasing_noop_without_generator():
    """驗收 12：沒有生成器（等同 --no-llm）→ watch_phrasing 什麼都不做就結束，
    不會卡住 gather（main_async 也因此完全不排入這個 task，見該處 if 判斷）。"""
    bank = PhraseBank(generator=None)
    session = Session(MeetingState(topic="t", duration_min=30, participants=["A"]),
                       phrase_bank=bank)
    asyncio.run(asyncio.wait_for(session.watch_phrasing(), timeout=0.5))
    assert bank.generations == 0


def test_watch_phrasing_runs_concurrently_without_blocking_hello():
    """驗收「生成不得阻塞任何東西」的直接證明：一個模擬「LLM 很慢」的生成器
    （用真執行緒 sleep，模擬網路延遲）在背景跑著的同時，問候（同步方法，
    走 HelloGate 的時機）依然立刻送出，不必等生成器那頭回來。
    """
    import threading
    import time as time_module

    release = threading.Event()

    def slow_generator(kind, topic):
        release.wait(timeout=2.0)  # 模擬還沒回應的網路呼叫
        return [_valid_candidate(kind)]

    bank = PhraseBank(generator=slow_generator)
    st = MeetingState(topic="t", duration_min=30, participants=["Alex"])
    session = Session(st, phrase_bank=bank)
    session.chair = RecordingChair()
    gate = HelloGate()

    async def run():
        phrasing_task = asyncio.create_task(session.watch_phrasing())
        await asyncio.sleep(0.05)  # 讓 watch_phrasing 先卡進 to_thread(slow_generator)
        start = time_module.perf_counter()
        session.note_human_audio(gate)  # 問候：不能被卡住的生成器拖住
        elapsed = time_module.perf_counter() - start
        release.set()  # 收尾：放行卡住的執行緒，避免測試結束後還有殘留執行緒等待
        phrasing_task.cancel()
        try:
            await phrasing_task
        except asyncio.CancelledError:
            pass
        return elapsed

    elapsed = asyncio.run(asyncio.wait_for(run(), timeout=2.0))

    # 問候是純同步呼叫，遠快於卡住的生成器（2 秒逾時）——門檻抓寬鬆一點
    # 避免測試機忙碌時的排程雜訊誤判，但仍足以證明沒有等到生成器回來。
    assert elapsed < 0.5
    assert len(session.chair.requests) == 1
    assert session.chair.requests[0].text == "大家好，我是今天的主席，會議開始。"


def test_watch_hello_greets_on_timeout_without_audio(monkeypatch):
    """驗收 4 的接線版本：真人加入後一直沒有音訊，watch_hello 逾時後仍要問候一次。"""
    monkeypatch.setattr(live, "HELLO_POLL_SECONDS", 0.02)
    monkeypatch.setattr(live, "HELLO_AUDIO_TIMEOUT_SECONDS", 0.05)
    st = MeetingState(topic="t", duration_min=30, participants=[])
    session = Session(st)
    session.chair = RecordingChair()
    gate = HelloGate()

    async def join_after_a_tick():
        await asyncio.sleep(0.03)
        st.ensure_participant("Alex")  # 一直不呼叫 note_human_audio，模擬對方進來就靜音

    async def run():
        await asyncio.gather(session.watch_hello(gate), join_after_a_tick())

    asyncio.run(asyncio.wait_for(run(), timeout=2.0))
    assert len(session.chair.requests) == 1
    assert gate.greeted is True


# ── T29：慢路拆成兩次呼叫（判斷／話術）的兩道閘門 ────────────────────
#
# `slow_gate` 是第一關（判斷結果本身要不要進到「產話術」這一步），
# `slow_recheck_admissible` 是話術回來之後的第二關（TOCTOU 重驗）。
# 拆之前這兩件事綁在 `slow_result_admissible` 一支裡；那一支現在只留給離線
# 工具與既有回歸測試，前面那批測試就是它的契約，這裡不重複。


def test_slow_gate_passes_without_utterance():
    """第一關的重點：話術這時候還不存在，不能因為 r 裡沒有 utterance 就擋下——
    擋下的話第二次呼叫永遠不會發生，整個拆呼叫的架構就死在這裡。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1}
    assert live.slow_gate(st, 100.0, r) == (True, "")


def test_slow_gate_blocks_type_wu_before_spending_a_call():
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "無", "verdict": "正向介入", "positive": 3, "negative": 1, "none": 2}
    assert live.slow_gate(st, 100.0, r) == (False, "type=無")


def test_slow_gate_blocks_within_cooldown():
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    st.interventions = [90.0]
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1}
    assert live.slow_gate(st, 100.0, r) == (False, "冷卻")


def test_slow_gate_blocks_while_closing_before_cooldown():
    """收尾仍排在冷卻之前——理由跟 slow_result_admissible 那批一樣，
    不能讓系統性的收尾誤報被偶然的冷卻遮住。"""
    st = _closing_state()
    st.interventions = [698.7]
    r = {"type": "離題", "verdict": "正向介入", "positive": 3, "negative": 2, "none": 2}
    assert live.slow_gate(st, 701.3, r) == (False, "收尾")


def test_slow_recheck_rejects_empty_utterance_with_its_own_reason():
    """話術呼叫失敗／回空 → 放棄這次介入，理由必須是「話術失敗」而不是舊的
    「無話術」：那兩個是不一樣的事（沒問過 vs 問了但生不出來），事件檔要分得出來。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "utterance": "   "}
    assert live.slow_recheck_admissible(st, 100.0, r) == (False, "話術失敗")


def test_slow_recheck_rejects_over_long_utterance():
    """超過 UTTERANCE_HARD_CAP 就整句作廢，不截斷——半句話比不講更糟。"""
    from meeting_host.slow_path import UTTERANCE_HARD_CAP
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "utterance": "太" * (UTTERANCE_HARD_CAP + 1)}
    assert live.slow_recheck_admissible(st, 100.0, r) == (False, "話術過長")
    r["utterance"] = "剛" * UTTERANCE_HARD_CAP
    assert live.slow_recheck_admissible(st, 100.0, r) == (True, "")


def test_slow_recheck_catches_cooldown_that_started_during_the_call():
    """TOCTOU 正例：第一關通過時還沒有人講話，話術跑了幾秒期間快路開口了。
    reason 要指出是「話術後」才發生的，跟第一關的冷卻分開。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A"])
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1}
    assert live.slow_gate(st, 100.0, r) == (True, "")
    st.interventions = [101.0]          # 話術生成期間快路出聲
    r["utterance"] = "剛剛「便當」那段先放著，回到黑客松籌備。"
    assert live.slow_recheck_admissible(st, 103.0, r) == (False, "冷卻(話術後)")


def test_slow_recheck_catches_closing_that_started_during_the_call():
    st = _closing_state()
    r = {"type": "離題", "verdict": "正向介入", "positive": 3, "negative": 2, "none": 2,
         "utterance": "剛剛「拜拜」那句是要結束了嗎？"}
    assert live.slow_recheck_admissible(st, 701.3, r) == (False, "收尾(話術後)")


def test_blocked_after_decision_reasons_are_exactly_the_second_gate_reasons():
    """`SLOW_BLOCKED_AFTER_DECISION` 是給觀戰 UI 用的清單（那邊算「受阻」不是
    「忍住」）。它跟第二關真的會回的理由必須一字不差，否則 UI 會把某一種失敗
    悄悄顯示成「主席選擇忍住」。"""
    from meeting_host.slow_path import UTTERANCE_HARD_CAP
    st_ok = MeetingState(topic="t", duration_min=30, participants=["A"])
    st_cool = MeetingState(topic="t", duration_min=30, participants=["A"])
    st_cool.interventions = [99.0]
    base = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1}
    seen = {
        live.slow_recheck_admissible(st_ok, 100.0, {**base, "utterance": ""})[1],
        live.slow_recheck_admissible(st_ok, 100.0,
                                     {**base, "utterance": "長" * (UTTERANCE_HARD_CAP + 1)})[1],
        live.slow_recheck_admissible(st_cool, 100.0, {**base, "utterance": "短句"})[1],
        live.slow_recheck_admissible(_closing_state(), 701.3, {**base, "utterance": "短句"})[1],
        # 失聰：話術那幾秒之間 STT 才死掉（見 hearing.py）。跟上面四種一樣是
        # 「已經決定要開口才被擋下」，所以也必須出現在這份清單裡。
        live.slow_recheck_admissible(st_ok, 100.0, {**base, "utterance": "短句"}, deaf=True)[1],
    }
    assert seen == set(live.SLOW_BLOCKED_AFTER_DECISION)


# ── T29：兩支 prompt 各自的不變量 ────────────────────────────────────


def test_utterance_prompt_carries_transcript_and_the_two_rules_that_earned_their_place():
    """話術 prompt 必須帶三樣東西，少一樣就退回罐頭話（見 slow_path 模組 docstring
    的 34 點實測）：那個時點的逐字稿原文、v2 那條「只有這場說得出口」的門檻、
    以及長度上限。判斷結果（type／pros）也要帶，否則第二次呼叫會自己重判一次。"""
    from meeting_host import slow_path
    st = MeetingState(topic="黑客松籌備", duration_min=30, participants=["Alex", "MiMi"])
    st.add(Utterance("Alex", "那明天晚上要吃什麼？", 240.0, 243.0))
    st.add(Utterance("MiMi", "八方雲集吧。", 244.0, 246.0))
    r = {"type": "離題", "verdict": "負向介入", "positive": 1, "negative": 4, "none": 1,
         "pros": ["話題已離開黑客松"], "cons": ["發散期"]}
    p = slow_path.build_utterance_prompt(st, 250.0, r, "發散期")
    assert "那明天晚上要吃什麼？" in p and "八方雲集吧。" in p
    assert "話題已離開黑客松" in p and "發散期" in p and "離題" in p
    assert "貼到任何一場別的會議也" in p          # v2 的內容門檻
    assert "用「」框起來" in p                    # 逐字引用要求
    assert str(slow_path.MAX_UTTERANCE_CHARS) in p


def test_structure_block_separates_backchannel_from_participation():
    """結構訊號要把「對方只剩應聲」量成數字——那是 6 則逐字稿看不出來的形狀。

    2026-09-05 的依據：8/31 那場 O1 窗口內，模型的三軸每次都判「要介入」、pros 直接
    寫「Alex 連續主導、Jax 已近 3 分鐘未發言」，但六個類型沒有一格裝得下，只好選
    「無」，被 is_intervention() 的 type 閘門滅掉 12/12 次。加上「發言權失衡」這一格
    之後，該窗口五輪全中——而區分「主述」與「已經不在討論裡」靠的就是這裡的
    最長句字數（Jax 三分鐘內唯一一句是「OK。」）。
    """
    from meeting_host import slow_path
    st = MeetingState(topic="t", duration_min=30, participants=["Alex", "Jax"])
    st.add(Utterance("Alex", "很久以前的一段話" * 5, 100.0, 160.0))   # 3 分鐘窗口外
    st.add(Utterance("Jax", "OK。", 400.0, 402.0))
    st.add(Utterance("Alex", "我覺得這件事" * 40, 405.0, 465.0))
    st.add(Utterance("Alex", "而且還有一點" * 40, 466.0, 526.0))
    block = slow_path.build_structure(st, 530.0)

    assert "Alex：說了 120 秒／2 句，最長的一句 240 字" in block   # 窗口外那句沒被算進來
    assert "Jax：說了 2 秒／1 句，最長的一句 3 字" in block         # 三分鐘內只剩應聲
    assert "發言權易手 1 次" in block
    assert "Alex 已連續講 2.0 分鐘" in block   # 405→526，中間 1 秒的停頓不斷鏈

    # 這一段必須進到判斷 prompt，且類型清單有那一格，否則模型判得出來也講不出口
    p = slow_path.build_prompt(st, 530.0)
    assert "## 結構訊號" in p and "最長的一句 3 字" in p
    assert "發言權失衡" in p


def test_judge_prompt_no_longer_asks_for_an_utterance():
    """拆呼叫的前提：判斷那一次完全不提話術。v2 實測只改話術指令就讓介入次數
    從 9 跳到 17——只要 utterance 還在同一份 JSON 裡，兩件事就分不開。"""
    from meeting_host import slow_path
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "hi", 0.0, 1.0))
    p = slow_path.build_prompt(st, 10.0)
    assert "utterance" not in p
    assert "你會說的話" not in p
    assert '"type"' in p and '"pros"' in p


def test_channel_id_comes_from_env_and_bad_values_are_ignored(monkeypatch):
    """`--channel` 的預設值讀 AHEM_CHANNEL_ID；壞值當作沒設，不拿去 join。

    頻道 ID 屬於部署環境，不是產品的一部分：寫死在文件裡的指令會被複製到錯的
    機器上，而這個 repo 是公開的，把 ID 留在版控裡等於公開一個可被騷擾的目標。
    argparse 的 `type=int` 不套用在 default 上，所以轉換必須自己做——轉不動時
    回 None（照原本的路徑等頻道），不能讓一個壞掉的 default 去 join 不存在的頻道。
    """
    from meeting_host import live
    monkeypatch.delenv("AHEM_CHANNEL_ID", raising=False)
    assert live._env_channel_id() is None
    monkeypatch.setenv("AHEM_CHANNEL_ID", " 1234567890 ")
    assert live._env_channel_id() == 1234567890
    monkeypatch.setenv("AHEM_CHANNEL_ID", "not-a-number")
    assert live._env_channel_id() is None
