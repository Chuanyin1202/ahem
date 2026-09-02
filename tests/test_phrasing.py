"""T14：句型庫（phrasing.py）——取用零延遲、生成失敗不拖垮會議、句型驗證。

不打真實 API：所有測試用假的 generator（普通 Python callable），驗證
PhraseBank／validate_pattern／fill 的行為契約。真實 LLM 呼叫
（phrasing.generate_patterns）只在交付報告裡跑過一次，不進單元測試。
"""
from meeting_host.fast_path import Trigger, utterance_for
from meeting_host.phrasing import (
    MAX_GENERATIONS_PER_MEETING,
    PhraseBank,
    fill,
    greeting_text,
    validate_pattern,
)


# ── 驗收 4／5／6：validate_pattern ────────────────────────────────────


def test_validate_rejects_missing_required_slot():
    """驗收 4：發言超時缺了 {mins}，即使有 {target} 也要丟棄。"""
    assert validate_pattern("發言超時", "{target}，可以先讓大家喘口氣嗎？") is False


def test_validate_accepts_pattern_with_all_required_slots():
    assert validate_pattern("發言超時", "{target}，講了{mins}分鐘了，先緩一緩吧。") is True


def test_validate_rejects_unknown_slot():
    """驗收 5：出現規格之外的插槽（例如「有人被冷落」不該有 {mins}）要丟棄。"""
    assert validate_pattern("有人被冷落", "{target}，已經{mins}分鐘沒聽到你的聲音了。") is False


def test_validate_rejects_extra_unrelated_slot():
    assert validate_pattern("全場沉默", "現場好安靜，{target}要不要先說？") is False


def test_validate_rejects_too_short():
    """驗收 6：長度下限。"""
    assert validate_pattern("全場沉默", "嗯？") is False


def test_validate_rejects_too_long():
    """驗收 6：長度上限。"""
    long_text = "現場安靜了好一陣子" * 10
    assert validate_pattern("全場沉默", long_text) is False


def test_validate_rejects_fabricated_number_outside_slot():
    """插槽以外還出現數字＝可能捏造的事實——例如 LLM 自己寫死了一個分鐘數。"""
    assert validate_pattern("有人被冷落", "{target}，已經5分鐘沒聽到你的聲音了。") is False


def test_validate_rejects_dangling_brace():
    """孤立括號會讓 str.format 直接炸掉，必須在驗證階段就擋下。"""
    assert validate_pattern("有人被冷落", "{target}，你覺得 {怎麼樣？") is False


def test_validate_greeting_allows_zero_or_one_topic_slot():
    assert validate_pattern("問候", "大家好，很高興今天能聚在一起開會，我們開始吧。") is True
    assert validate_pattern("問候", "歡迎大家，今天要來聊聊「{topic}」，我們開始吧。") is True


def test_validate_greeting_rejects_unknown_slot():
    assert validate_pattern("問候", "大家好，{target}我們開始吧。") is False


def test_validate_unknown_kind_rejected():
    assert validate_pattern("不存在的類型", "隨便一句話，長度也夠。") is False


# ── 驗收 1：take() 是純記憶體操作，不呼叫生成器 ──────────────────────


def test_take_never_calls_generator():
    calls = []

    def spy_generator(kind, topic):
        calls.append(kind)
        return ["不應該被呼叫"]

    bank = PhraseBank(generator=spy_generator)
    assert bank.take("發言超時") is None  # 佇列本來就空的
    bank._queues["發言超時"].append("{target}，講了{mins}分鐘囉，換人吧。")
    assert bank.take("發言超時") == "{target}，講了{mins}分鐘囉，換人吧。"
    assert calls == []  # 全程沒有呼叫過生成器


def test_take_pops_in_order_giving_variety():
    """驗收 8 的地基：佇列裡有多個變體時，連續 take 兩次拿到不同句型。"""
    bank = PhraseBank()
    bank._queues["全場沉默"].extend(["安靜了一陣子，聊聊？", "有人要打破沉默嗎？"])
    first = bank.take("全場沉默")
    second = bank.take("全場沉默")
    assert first != second
    assert bank.take("全場沉默") is None  # 用完了


# ── 驗收 2：佇列為空時 utterance_for 退回寫死模板 ────────────────────


def test_utterance_for_falls_back_when_bank_empty():
    empty_bank = PhraseBank()
    t = Trigger(kind="發言超時", target="Alice", detail="Alice 已連續發言 3.0 分鐘", hard=True)
    assert utterance_for(t, empty_bank) == "Alice，你已經講了3分鐘，先讓其他人接一下。"


def test_utterance_for_none_bank_is_unaffected():
    """驗收 12 的地基：不傳 bank（等同 --no-llm 之前的呼叫方式）行為完全不變。"""
    t = Trigger(kind="議程超時", target=None, detail="議程只剩 4.0 分鐘", hard=False)
    assert utterance_for(t) == "只剩4分鐘，我們往結論收。"


# ── 驗收 3：生成器例外／格式錯誤不傳播、不拖垮會議 ───────────────────


def test_refill_swallows_generator_exception():
    def boom(kind, topic):
        raise RuntimeError("LLM 掛了")

    bank = PhraseBank(generator=boom)
    bank.refill("發言超時")  # 不應該拋例外
    assert bank.take("發言超時") is None
    assert bank.generations == 1  # 呼叫嘗試過，仍算一次


def test_refill_swallows_non_list_return():
    bank = PhraseBank(generator=lambda kind, topic: {"phrasings": ["not a list, a dict"]})
    bank.refill("全場沉默")
    assert bank.take("全場沉默") is None


def test_refill_skips_non_string_candidates():
    bank = PhraseBank(generator=lambda kind, topic: [123, None, "現場很安靜，要不要先聊聊？"])
    bank.refill("全場沉默")
    assert bank.take("全場沉默") == "現場很安靜，要不要先聊聊？"


def test_refill_discards_invalid_candidates_without_raising():
    """一批候選裡有合格也有不合格的——合格的入列，不合格的丟棄，不報錯。"""
    candidates = [
        "{target}，講了{mins}分鐘了，要不要先休息一下？",  # 合格
        "{target}，講了{mins}分鐘了，已經5分鐘了。",       # 不合格：插槽外有數字
        "太短",                                              # 不合格：太短
    ]
    bank = PhraseBank(generator=lambda kind, topic: candidates)
    bank.refill("發言超時")
    assert bank.take("發言超時") == "{target}，講了{mins}分鐘了，要不要先休息一下？"
    assert bank.take("發言超時") is None


# ── 驗收 7：填值正確，事實與 Trigger 完全一致 ─────────────────────────


def test_fill_matches_trigger_facts_across_variants():
    variants = [
        "{target}，你已經講了{mins}分鐘，要不要先讓別人接話？",
        "已經過了{mins}分鐘囉，{target}要不要先喘口氣？",
    ]
    bank = PhraseBank()
    bank._queues["發言超時"].extend(variants)
    t = Trigger(kind="發言超時", target="Bob", detail="Bob 已連續發言 5.0 分鐘", hard=True)

    first = utterance_for(t, bank)
    assert "Bob" in first and "5" in first
    assert first == variants[0].format(target="Bob", mins=5)

    second = utterance_for(t, bank)
    assert "Bob" in second and "5" in second
    assert second == variants[1].format(target="Bob", mins=5)
    assert first != second  # 驗收 8：連續兩次措辭不同


def test_fill_returns_none_on_missing_key():
    assert fill("{target} 已經講了{mins}分鐘", target="Bob", mins=3) == "Bob 已經講了3分鐘"
    assert fill("{missing}", target="Bob") is None


# ── 驗收 11：生成次數上限 ─────────────────────────────────────────────


def test_generation_count_capped_per_meeting():
    calls = []

    def counting_generator(kind, topic):
        calls.append(kind)
        return []

    bank = PhraseBank(generator=counting_generator)
    for _ in range(MAX_GENERATIONS_PER_MEETING + 5):
        bank.refill("全場沉默")
    assert bank.generations == MAX_GENERATIONS_PER_MEETING
    assert len(calls) == MAX_GENERATIONS_PER_MEETING


def test_can_generate_false_without_generator():
    """驗收 12 的地基：generator=None（等同 --no-llm）時永遠不能生成。"""
    bank = PhraseBank(generator=None)
    assert bank.can_generate() is False
    bank.refill("全場沉默")  # 應為 no-op
    assert bank.generations == 0
    assert bank.take("全場沉默") is None


# ── 驗收 9／10：問候 ──────────────────────────────────────────────────


def test_greeting_falls_back_when_not_generated_yet():
    """驗收 9：句型還沒生成好（佇列空）→ 退回既有寫死那句，不等待、不阻塞。"""
    bank = PhraseBank()  # 沒有任何生成器呼叫過
    assert greeting_text(bank, "黑客松籌備") == "大家好，我是今天的主席，會議開始。"


def test_greeting_uses_generated_pattern_when_available():
    """驗收 10：句型生成成功時，實際採用生成的版本。"""
    bank = PhraseBank()
    bank._queues["問候"].append("大家好，今天我們要聊「{topic}」，開始吧！")
    assert greeting_text(bank, "黑客松籌備") == "大家好，今天我們要聊「黑客松籌備」，開始吧！"


def test_greeting_pattern_without_topic_slot_still_used():
    bank = PhraseBank()
    bank._queues["問候"].append("嗨大家好，會議正式開始囉。")
    assert greeting_text(bank, "黑客松籌備") == "嗨大家好，會議正式開始囉。"


def test_greeting_falls_back_when_topic_is_none_and_pattern_needs_it():
    """理論上不該生成需要 {topic} 又遇到沒有 topic 的情況（topic 一定有值），
    但防禦性地確保就算發生也不會拋例外——fill 會把 None 轉成空字串代入。"""
    bank = PhraseBank()
    bank._queues["問候"].append("今天要聊「{topic}」，開始吧！")
    assert greeting_text(bank, None) == "今天要聊「」，開始吧！"
