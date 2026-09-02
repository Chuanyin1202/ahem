"""快路話術只能斷言規則真正知道的事（T33）。

起因是 2026-08-31 的真實會議：主席對一位**佔了全場 69% 發言時間**的參與者說
「Alex Huang 從開會到現在還沒說話，你對這個提案的看法是什麼？」——當場就被戳破。

根因不是判斷失準，是觸發條件與話術的斷言對不上：
- 觸發條件（`fast_path.check`）：`silent_seconds(p) >= NEGLECTED_SECONDS`，
  意思是「**這 5 分鐘沒說話**」
- 話術斷言：「**從開會到現在都沒說過話**」——規則從來沒有檢查過這件事

這個檔案把「每一句話術的每個斷言都要能從 Trigger 帶的事實推出來」變成可執行的
檢查。門檻常數一律不動：這裡驗的是「說錯話」，不是「判錯」。

⚠️ 那場會議的事件檔不在這個 repo 裡，下面的時間軸是依現場回報的兩個數字
（69% 佔比、最後 5 分鐘沒發言）重建的最小場景，不是原始逐字稿。
"""
import pytest

from meeting_host import fast_path
from meeting_host.fast_path import Trigger, utterance_for
from meeting_host.phrasing import PhraseBank, build_prompt, validate_pattern
from meeting_host.state import MeetingState, Utterance

# 主席不可以說出口的斷言——規則沒有檢查過「他整場有沒有開口」這件事。
NEVER_SPOKE_CLAIMS = [
    "還沒說話",
    "沒說過話",
    "從開會到現在",
    "從會議開始",
    "還沒發言",
    "都還沒聽到",
    "第一次",
]


def _the_2026_08_31_meeting() -> tuple[MeetingState, float]:
    """重建那場會議的形狀：Alex 講掉近七成時間，後段換 Jax 一直講，
    Alex 於是安靜滿 5 分鐘，「有人被冷落」對 Alex 成立。

    刻意讓其他三條規則都不成立，斷言才只反映這一條：
    - 發言超時：Jax 最後一句結束於 914，now=936 已超過 RUN_GAP_SECONDS
    - 全場沉默：Jax 只安靜了 22 秒，遠低於 SILENCE_SECONDS
    - 議程超時：30 分鐘的會議還剩 14 分鐘
    """
    st = MeetingState(topic="黑客松分工", duration_min=30,
                      participants=["Alex Huang", "Jax"])
    st.add(Utterance("Alex Huang", "我先講一下整體規劃……", 0.0, 320.0))
    st.add(Utterance("Jax", "嗯，我懂。", 320.0, 325.0))
    st.add(Utterance("Alex Huang", "然後排程的部分是這樣……", 325.0, 636.0))
    st.add(Utterance("Jax", "那我補充一下我這邊的狀況……", 636.0, 914.0))
    return st, 936.0


def test_the_2026_08_31_meeting_reproduces_the_trigger():
    """先確認重建的場景真的踩到那條規則、而且前提數字對得上——
    不然後面那條「話術不得宣稱他沒發言過」會變成空跑。"""
    st, now = _the_2026_08_31_meeting()
    assert st.share("Alex Huang", now) == pytest.approx(0.69, abs=0.005)
    assert st.silent_seconds("Alex Huang", now) >= fast_path.NEGLECTED_SECONDS

    triggers = fast_path.check(st, now, set())
    assert [(t.kind, t.target) for t in triggers] == [("有人被冷落", "Alex Huang")]


def test_neglected_utterance_never_claims_the_target_was_silent_all_meeting():
    """驗收 1：佔比 69%、最後 5 分鐘沒說話的人被點名時，
    話術不得宣稱他沒發言過。"""
    st, now = _the_2026_08_31_meeting()
    (trigger,) = fast_path.check(st, now, set())

    text = utterance_for(trigger)

    assert "Alex Huang" in text
    for claim in NEVER_SPOKE_CLAIMS:
        assert claim not in text, f"話術仍宣稱他沒發言過：{text}"
    assert text == "Alex Huang，你有一陣子沒說話了，想聽聽你的看法。"


def test_neglected_utterance_also_true_for_someone_who_never_spoke():
    """同一句話術對「整場沒開過口」的人一樣成立——這是選擇單一講法
    （而不是拆成兩條分支）的前提：它在兩種情況下都是真的。"""
    st = MeetingState(topic="t", duration_min=30, participants=["A", "B"])
    st.add(Utterance("A", "我先開始……", 0.0, 350.0))  # A 一直講到 350
    now = 400.0
    assert st.spoke_seconds("B") == 0.0  # B 整場沒開口
    assert st.silent_seconds("B", now) >= fast_path.NEGLECTED_SECONDS

    neglected = [t for t in fast_path.check(st, now, set()) if t.kind == "有人被冷落"]
    assert [t.target for t in neglected] == ["B"]
    assert utterance_for(neglected[0]) == "B，你有一陣子沒說話了，想聽聽你的看法。"


def test_neglected_utterance_does_not_presuppose_a_proposal_on_the_table():
    """原句還夾了「你對這個提案的看法是什麼？」——規則不知道桌上有沒有提案。"""
    t = Trigger(kind="有人被冷落", target="B", detail="B 已 5.0 分鐘沒有發言", hard=False)
    text = utterance_for(t)
    for claim in ["提案", "方案", "這個案子"]:
        assert claim not in text


# ── 數字：說出口的分鐘數不能超出規則量到的範圍 ────────────────────────


@pytest.mark.parametrize("detail_mins, spoken", [
    ("3.0", 3),
    ("3.9", 3),   # 四捨五入會講成「4 分鐘」——多算了 54 秒
    ("4.5", 4),
    ("5.0", 5),
])
def test_overtime_minutes_never_overstate_how_long_he_talked(detail_mins, spoken):
    """「你已經講了 N 分鐘」是下界宣告：N 必須是他確實已經講滿的分鐘數。"""
    t = Trigger(kind="發言超時", target="A",
                detail=f"A 已連續發言 {detail_mins} 分鐘", hard=True)
    assert utterance_for(t) == f"A，你已經講了{spoken}分鐘，先讓其他人接一下。"


@pytest.mark.parametrize("detail_mins, left", [
    ("0.3", 1),   # 四捨五入會算出 0 →「只剩0分鐘」，既不是事實也不是人話
    ("0.9", 1),
    ("4.0", 4),
    ("4.2", 5),
])
def test_agenda_minutes_are_an_upper_bound_and_never_zero(detail_mins, left):
    """「只剩 N 分鐘」是上界宣告：剩餘時間一定不超過說出口的數字，且不會是 0。

    短會議的提醒窗口可以只有 20 秒（`agenda_warn_seconds(2)`），這條規則又
    可能被冷卻期／硬打斷擠到很後面才第一次成立，0 是真的講得出來的。
    """
    t = Trigger(kind="議程超時", target=None,
                detail=f"議程只剩 {detail_mins} 分鐘", hard=False)
    assert utterance_for(t) == f"只剩{left}分鐘，我們往結論收。"


def test_agenda_utterance_never_says_zero_minutes_in_the_shortest_window():
    """端到端：2 分鐘的會議、剩 20 秒剛好踩到門檻，主席不會講「只剩0分鐘」。"""
    st = MeetingState(topic="t", duration_min=2, participants=[])
    now = 100.0  # 剩 20 秒 = agenda_warn_seconds(2)
    (trigger,) = fast_path.check(st, now, set())
    assert trigger.kind == "議程超時"
    assert utterance_for(trigger) == "只剩1分鐘，我們往結論收。"


# ── 全場沉默：規則只知道「沒有人在說話」 ──────────────────────────────


@pytest.mark.parametrize("variant", range(len(fast_path._ROOM_SILENCE_UTTERANCES)))
def test_room_silence_utterances_do_not_presuppose_a_prior_discussion(variant):
    """全場沉默完全可能是從第一秒就沒人開口（沒人開麥、大家在讀文件），
    所以話術不能預設「剛剛已經討論過」或「已經有一個方向」。"""
    t = Trigger(kind="全場沉默", target=None,
                detail="全場已 1.5 分鐘沒有人發言", hard=False, variant=variant)
    text = utterance_for(t)
    for claim in ["剛剛講到", "剛剛的", "目前的方向", "剛才討論", "回顧"]:
        assert claim not in text, f"variant {variant} 預設了先前的討論：{text}"


def test_room_silence_first_variant_is_still_word_for_word_unchanged():
    """第一句是使用者已經聽過的話術，T13 的註解明講不能無故換掉——
    它本來就只斷言「安靜了一陣子」，這次不需要動它。"""
    t = Trigger(kind="全場沉默", target=None, detail="d", hard=False, variant=0)
    assert utterance_for(t) == "現場安靜了一陣子，要不要有人先分享一下目前的想法？"


# ── phrasing.py：LLM 生的變體不能帶著同一個假前提 ─────────────────────


def test_neglected_prompt_no_longer_states_the_false_premise():
    """驗收 3：說明本身若還寫著「從會議開始到現在都還沒發言過」，
    LLM 生出來的每個變體都會帶著同一個謊。"""
    prompt = build_prompt("有人被冷落", topic=None)
    assert "還沒發言過" not in prompt
    assert "從會議開始到現在" not in prompt
    # 而且要明講「不知道他之前有沒有發言過」與禁止的講法
    assert "並不知道他之前有沒有發言過" in prompt
    assert "從開會到現在還沒說話" in prompt  # 以「這樣講不行」的形式出現


def test_room_silence_prompt_forbids_referring_to_earlier_discussion():
    prompt = build_prompt("全場沉默", topic=None)
    assert "不知道大家先前討論到哪裡" in prompt
    assert "回顧一下剛剛講到哪" in prompt  # 反例清單


@pytest.mark.parametrize("kind", ["有人被冷落", "全場沉默"])
def test_prompts_still_carry_the_slot_rules_the_validator_enforces(kind):
    """改說明不能把插槽規則講丟——`validate_pattern` 仍會照原規則丟棄候選，
    說明與驗證對不上只會浪費生成額度。"""
    prompt = build_prompt(kind, topic=None)
    if kind == "有人被冷落":
        assert "{target}" in prompt
    else:
        assert "不能包含任何插槽" in prompt
    assert "不要提到任何數字" in prompt or "不能提到任何" in prompt


def test_truthful_generated_variant_passes_validation_and_is_actually_used():
    """驗收 3：照新說明生出來的變體要能通過 `validate_pattern`（插槽、長度、
    禁止插槽外數字都沒被改動），並真的被 `utterance_for` 採用。

    用固定的假回傳，不打 LLM——這裡驗的是驗證規則與新說明相容，
    不是 LLM 這一次剛好生得好。
    """
    candidates = [
        "{target}，有一陣子沒聽到你的聲音了，想聽聽你的想法。",
        "{target}，你安靜好一會兒了，要不要說說你的角度？",
        "{target}，已經5分鐘沒聽到你的聲音了。",   # 不合格：插槽外的數字
        "有人想補充嗎？",                            # 不合格：缺 {target}
    ]
    assert [validate_pattern("有人被冷落", c) for c in candidates] == \
        [True, True, False, False]

    bank = PhraseBank(generator=lambda kind, topic: candidates)
    bank.refill("有人被冷落")
    t = Trigger(kind="有人被冷落", target="Alex Huang",
                detail="Alex Huang 已 5.0 分鐘沒有發言", hard=False)

    text = utterance_for(t, bank)

    assert text == "Alex Huang，有一陣子沒聽到你的聲音了，想聽聽你的想法。"
    for claim in NEVER_SPOKE_CLAIMS:
        assert claim not in text
