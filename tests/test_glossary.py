"""提示卡（`glossary.py` ＋ `live.Session.watch_glossary`）。

對應工單的五條硬性要求，每條至少一個測試：
1. **完全靜默**：不經過 Chair、不產生 Intervention、不寫 st.interventions、
   不影響冷卻期、不發 TTS（`test_silent_*`）
2. **不得捏造**：卡片要嘛帶逐字稿時間戳＋原話，要嘛帶 URL；沒來源就不印
   （`test_fabricated_*`、`test_gloss_without_sources_*`、`test_every_card_*`）
3. **不干擾既有兩條路**：抽取／查詢失敗被隔離在自己的 task 裡（`test_failure_*`）
4. **成本可控**：批次節奏與網路查詢預算（`test_batch_due_*`、`test_web_lookup_budget`）
5. **回放可重現**：走既有事件匯流排，`events.jsonl` 來回一趟等值（`test_replay_*`）

所有測試都注入假的 extractor／lookup，完全不碰網路。
"""
import asyncio
import dataclasses
import json

import pytest

from meeting_host import glossary as g
from meeting_host.events import Event
from meeting_host.live import GLOSSARY_POLL_SECONDS, Session
from meeting_host.state import MeetingState, Utterance


def test_source_url_allows_public_https_and_blocks_local_networks():
    assert g._safe_source_url("https://example.com/reference")
    assert not g._safe_source_url("http://example.com/reference")
    assert not g._safe_source_url("https://localhost/private")
    assert not g._safe_source_url("https://127.0.0.1/private")
    assert not g._safe_source_url("https://169.254.169.254/latest/meta-data")


# ── 共用素材 ────────────────────────────────────────────────────────────
def u(speaker, text, start, end=None):
    return Utterance(speaker, text, start, end if end is not None else start + 3.0)


TRANSCRIPT = [
    u("Alex", "我們用 RTP 收封包，這樣延遲比較低。", 10.0),
    u("MiMi", "RTP 就是即時傳輸協定，負責把聲音切成小封包送出去。", 20.0),
    u("Alex", "對，然後 STT 那邊接。", 35.0),
    u("MiMi", "那個八方雲集的案子呢？", 50.0),
]


def state_with(utterances, participants=("Alex", "MiMi")):
    st = MeetingState(topic="測試", duration_min=30, participants=list(participants))
    for x in utterances:
        st.add(x)
    return st


class FakeChair:
    """只記錄有沒有被要求開口——提示卡的驗收就是這個清單永遠是空的。"""

    pending = None
    playing = None

    def __init__(self):
        self.requested = []

    def request(self, iv):
        self.requested.append(iv)
        return True


# ── 1. 純函式：出處與驗證 ────────────────────────────────────────────────
def test_find_mentions_is_case_insensitive_and_sorted():
    utts = [u("A", "先講 rtp", 30.0), u("B", "RTP 很重要", 10.0), u("C", "無關", 20.0)]
    hits = g.find_mentions("RTP", utts)
    assert [h.start for h in hits] == [10.0, 30.0]


def test_find_mentions_counts_utterances_not_string_occurrences():
    """同一則發言講兩次只算一則——口語重複不該把「提到次數」灌水。"""
    utts = [u("A", "你看一下漏梗，你看一下漏梗", 10.0)]
    assert len(g.find_mentions("漏梗", utts)) == 1


def test_find_explanation_requires_marker_right_after_term():
    st = state_with(TRANSCRIPT)
    found = g.find_explanation("RTP", st.utterances)
    assert found is not None and found.start == 20.0
    assert "即時傳輸協定" in found.text


def test_find_explanation_ignores_filler_just_because_sentence_has_marker():
    """「就是」是中文口語超高頻語助詞。整句掃描的話這句會被誤判成「有人解釋過」。"""
    utts = [u("A", "就是，就是那個 RTP 啦，我不知道怎麼講", 10.0)]
    assert g.find_explanation("RTP", utts) is None


def test_find_explanation_needs_substance_after_marker():
    """「RTP 就是」後面沒有實質內容，那不是解釋。"""
    utts = [u("A", "RTP 就是啊", 10.0)]
    assert g.find_explanation("RTP", utts) is None


def test_looks_like_term_rejects_participants_and_bad_shapes():
    assert g.looks_like_term("RTP", ["Alex", "MiMi"])
    assert not g.looks_like_term("Alex", ["Alex", "MiMi"])       # 在場的人名，零資訊
    assert not g.looks_like_term("alex huang", ["Alex Huang"])   # 大小寫／空白不影響
    assert not g.looks_like_term("我", [])                        # 太短
    assert not g.looks_like_term("這一句話已經長到根本不是一個詞了啦真的", [])  # 太長
    assert not g.looks_like_term("。。。", [])                    # 沒有任何字母數字
    assert not g.looks_like_term("2026", [])                     # 純數字


# ── 2. 不得捏造 ─────────────────────────────────────────────────────────
def test_fabricated_term_never_becomes_a_card():
    """LLM 回一個逐字稿裡根本沒有的詞 → build_card 直接回 None。"""
    st = state_with(TRANSCRIPT)
    assert g.build_card("Kubernetes", st.utterances) is None


def test_fabricated_term_dropped_by_run_batch_and_costs_no_web_lookup():
    st = state_with(TRANSCRIPT)
    calls = []

    def lookup(term, ctx):
        calls.append(term)
        return "不該被呼叫", [g.Source("x", "https://example.com")]

    book = g.Glossary(extractor=lambda *_: ["Kubernetes"], lookup=lookup)
    assert book.run_batch(st.utterances, st.utterances, st.participants) == []
    assert calls == []          # 連查都不查——先驗證出處，再花錢
    assert book.web_lookups == 0


def test_gloss_without_sources_is_discarded():
    """沒有來源連結的一句話說明＝模型自由發揮，整段丟掉，卡片退回逐字稿版本。"""
    st = state_with(TRANSCRIPT)
    card = g.build_card("RTP", st.utterances, gloss="RTP 是一種很棒的協定", sources=[])
    assert card is not None
    assert card.gloss is None and card.sources == ()


def test_no_result_sentinel_is_discarded_even_with_sources():
    st = state_with(TRANSCRIPT)
    card = g.build_card("RTP", st.utterances, gloss="查無資料",
                        sources=[g.Source("t", "https://example.com")])
    assert card.gloss is None and card.sources == ()


def test_every_printable_card_carries_a_source():
    """硬規則：印出來的卡要嘛有 URL，要嘛有會議裡的原話出處。而且 first 恆存在。"""
    st = state_with(TRANSCRIPT)
    cards = [
        g.build_card("RTP", st.utterances),                                    # 有人解釋過
        g.build_card("八方雲集", st.utterances, gloss="鍋貼連鎖店",
                      sources=[g.Source("維基", "https://zh.wikipedia.org/x")]),  # 有 URL
    ]
    for card in cards:
        assert card is not None
        assert card.first.text and card.first.t is not None   # 時間戳＋原話恆在
        assert g.is_printable(card)
        assert bool(card.sources) or card.explained is not None or card.mentions >= 3


def test_unexplained_unsourced_term_is_not_printed_until_repeated():
    """沒人解釋、網路也查不到、只出現一兩次 → 不印。「這個詞出現過」不是資訊。"""
    once = [u("A", "那個八方雲集的案子呢？", 10.0)]
    assert not g.is_printable(g.build_card("八方雲集", once))

    many = once + [u("B", "八方雲集那邊回覆了嗎", 30.0), u("A", "八方雲集說下週", 60.0)]
    card = g.build_card("八方雲集", many)
    assert card.mentions == 3 and g.is_printable(card)


def test_gloss_strips_inline_markdown_links():
    """模型會把來源塞成句中的 markdown 連結；連結另外用 sources 呈現。"""
    cleaned = g._clean_gloss("LINE 是即時通訊軟體。([line.me](https://www.line.me/tw/))")
    assert cleaned == "LINE 是即時通訊軟體"
    assert "http" not in cleaned


# ── 3. 去重與預算 ───────────────────────────────────────────────────────
def test_same_term_is_not_printed_twice():
    st = state_with(TRANSCRIPT)
    book = g.Glossary(extractor=lambda *_: ["RTP"], lookup=None)
    assert [c.term for c in book.run_batch(st.utterances, st.utterances, st.participants)] == ["RTP"]
    assert book.run_batch(st.utterances, st.utterances, st.participants) == []


def test_near_duplicate_terms_are_deduped():
    utts = [u("A", "我們用 RTP 收封包", 10.0), u("B", "RTP 就是即時傳輸協定，負責送封包", 20.0),
            u("C", "RTP 封包 就是那個東西，很常見的東西", 30.0)]
    book = g.Glossary(extractor=lambda *_: ["RTP"], lookup=None)
    book.run_batch(utts, utts, [])
    book2 = book.run_batch(utts, utts, [])   # 第二批模型改回 "RTP 封包"
    assert book2 == []
    book.extractor = lambda *_: ["RTP 封包"]
    assert book.run_batch(utts, utts, []) == []


def test_web_lookup_budget_is_capped():
    utts = [u("A", f"詞{i} 就是一個測試用的東西啦", i * 10.0) for i in range(6)]
    seen = []

    def lookup(term, ctx):
        seen.append(term)
        return None, []

    book = g.Glossary(extractor=lambda b, k, p: [f"詞{i}" for i in range(6)],
                      lookup=lookup, max_web_lookups=2)
    book.run_batch(utts, utts, [])
    assert len(seen) == 2
    assert book.web_lookups == 2


def test_card_budget_is_capped():
    utts = [u("A", f"詞{i} 就是一個測試用的東西啦", i * 10.0) for i in range(6)]
    book = g.Glossary(extractor=lambda b, k, p: [f"詞{i}" for i in range(6)],
                      lookup=None, max_cards=3)
    assert len(book.run_batch(utts, utts, [])) == 3
    assert book.run_batch(utts, utts, []) == []


# ── 4. 成本：批次節奏 ───────────────────────────────────────────────────
@pytest.mark.parametrize("pending, now, last, expected", [
    (0, 999.0, 0.0, False),                                  # 沒有新發言就不跑
    (g.BATCH_MIN_UTTERANCES - 1, 10.0, 0.0, False),           # 沒滿一批、也還沒等夠久
    (g.BATCH_MIN_UTTERANCES, 10.0, 0.0, True),                # 滿一批
    (1, g.BATCH_MAX_WAIT_SECONDS + 1, 0.0, True),             # 沒滿但等夠久了
])
def test_batch_due_rules(pending, now, last, expected):
    s = Session(state_with([]))
    assert s.glossary_batch_due(pending, now, last) is expected


def test_batch_rhythm_is_much_slower_than_the_slow_path():
    """成本可控的關鍵不變量：提示卡的批次節奏必須顯著慢於慢路的 5 秒 tick。"""
    from meeting_host.live import TICK
    assert GLOSSARY_POLL_SECONDS > TICK
    assert g.BATCH_MAX_WAIT_SECONDS > TICK * 10


# ── 5. 完全靜默 ─────────────────────────────────────────────────────────
def _run_one_glossary_pass(session, monkeypatch, extractor, lookup=None):
    """把 watch_glossary 跑到「剛好處理完第一批」就停，回傳它 emit 的事件。

    透過 `watch_glossary(book=…)` 注入假的抽取器／查詢函式，完全不碰網路。
    """
    monkeypatch.setattr("meeting_host.live.GLOSSARY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(g, "BATCH_MIN_UTTERANCES", 1)
    book = g.Glossary(extractor=extractor, lookup=lookup)

    async def drive():
        task = asyncio.create_task(session.watch_glossary(book))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if session.events:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    return [e for e in session.events if e.kind == "glossary"]


def test_silent_never_asks_the_chair_to_speak(monkeypatch):
    """核心驗收：提示卡不會讓主席多開口一次。"""
    st = state_with(TRANSCRIPT)
    session = Session(st)
    chair = FakeChair()
    session.chair = chair

    cards = _run_one_glossary_pass(session, monkeypatch, lambda b, k, p: ["RTP"])

    assert cards, "應該要有卡片，否則這個測試什麼都沒證明"
    assert chair.requested == []          # 沒有任何 Intervention 被排入
    assert st.interventions == []         # 沒有寫進冷卻期的依據
    assert session.done == set()          # 沒有佔用任何 claim
    assert session.revision == 0
    kinds = {e.kind for e in session.events}
    assert kinds == {"glossary"}          # 沒有 queued／spoken／failed／dropped


def test_silent_does_not_shorten_the_cooldown(monkeypatch):
    """冷卻期是 since_last_intervention 算的；提示卡不能碰 st.interventions。"""
    st = state_with(TRANSCRIPT)
    st.interventions.append(100.0)
    session = Session(st)
    session.chair = FakeChair()

    before = list(st.interventions)
    _run_one_glossary_pass(session, monkeypatch, lambda b, k, p: ["RTP"])
    assert st.interventions == before
    assert st.since_last_intervention(130.0) == 30.0   # 與沒有這個功能時完全相同


def test_silent_runs_even_without_a_chair(monkeypatch):
    """提示卡不經過 Chair，所以 bot 還沒進頻道（chair is None）也照印。"""
    session = Session(state_with(TRANSCRIPT))
    assert session.chair is None
    assert _run_one_glossary_pass(session, monkeypatch, lambda b, k, p: ["RTP"])


# ── 6. 失敗隔離 ─────────────────────────────────────────────────────────
def test_failure_in_extractor_is_isolated(monkeypatch, capsys):
    """抽取炸掉不能把 watch_glossary 打死，也不能讓例外逃到 gather 去。"""
    st = state_with(TRANSCRIPT)
    session = Session(st)
    session.chair = FakeChair()
    monkeypatch.setattr("meeting_host.live.GLOSSARY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(g, "BATCH_MIN_UTTERANCES", 1)

    calls = []

    def boom(batch, known, participants):
        calls.append(1)
        raise RuntimeError("LLM 掛了")

    book = g.Glossary(extractor=boom, lookup=None)

    async def drive():
        task = asyncio.create_task(session.watch_glossary(book))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(calls) >= 2:
                break
        assert not task.done(), "task 不該因為一次失敗就結束"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert len(calls) >= 2                       # 失敗後下一批還會再試
    assert not session.events                     # 什麼都沒印
    assert st.interventions == [] and session.chair.requested == []
    assert "提示卡失敗" in capsys.readouterr().out


def test_failure_in_lookup_degrades_to_transcript_only(monkeypatch):
    """搜尋逾時只影響「那一個詞」：卡片退回只有逐字稿出處的版本，不整批重跑、
    不連累同一批其他詞，更不會影響主席。"""
    st = state_with(TRANSCRIPT)
    session = Session(st)
    session.chair = FakeChair()

    def timeout(term, ctx):
        raise TimeoutError("web search 逾時")

    cards = _run_one_glossary_pass(
        session, monkeypatch, lambda b, k, p: ["RTP"], lookup=timeout)

    assert len(cards) == 1
    assert cards[0].data["gloss"] is None and cards[0].data["sources"] == []
    assert cards[0].data["first"]["text"]            # 出處還在，卡片仍然合格
    assert st.interventions == [] and session.chair.requested == []


def test_failed_batch_is_retried_not_swallowed(monkeypatch):
    """抽取失敗的那一批不能被吃掉——否則那些發言裡的術語整場再也不會被看到。
    （與慢路「失敗時 last_n 不推進」同一個原則。）"""
    st = state_with(TRANSCRIPT)
    session = Session(st)
    monkeypatch.setattr("meeting_host.live.GLOSSARY_POLL_SECONDS", 0.001)
    monkeypatch.setattr(g, "BATCH_MIN_UTTERANCES", 1)
    monkeypatch.setattr(g, "BATCH_MAX_WAIT_SECONDS", 0.0)

    batches = []

    def flaky(batch, known, participants):
        batches.append([x.text for x in batch])
        if len(batches) == 1:
            raise RuntimeError("第一次掛掉")
        return ["RTP"]

    book = g.Glossary(extractor=flaky, lookup=None)

    async def drive():
        task = asyncio.create_task(session.watch_glossary(book))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if session.events:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert len(batches) >= 2
    assert batches[0] == batches[1], "重試必須用同一批發言，不能跳過"
    assert [e.kind for e in session.events] == ["glossary"]


def test_cancellation_is_not_swallowed(monkeypatch):
    """CancelledError 是收尾路徑，必須原樣往外拋，不能被 except Exception 吃掉。"""
    session = Session(state_with(TRANSCRIPT))
    monkeypatch.setattr("meeting_host.live.GLOSSARY_POLL_SECONDS", 0.001)

    async def drive():
        task = asyncio.create_task(session.watch_glossary())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())


# ── 7. 回放可重現 ───────────────────────────────────────────────────────
def test_replay_roundtrip_through_events_jsonl(monkeypatch):
    """走既有事件匯流排：emit 出來的卡序列化後再讀回來完全等值。"""
    st = state_with(TRANSCRIPT)
    session = Session(st)

    def lookup(term, ctx):
        return "RTP 是即時傳輸協定", [g.Source("維基百科", "https://zh.wikipedia.org/wiki/RTP")]

    events = _run_one_glossary_pass(
        session, monkeypatch, lambda b, k, p: ["RTP"], lookup=lookup)
    assert len(events) == 1

    line = json.dumps(dataclasses.asdict(events[0]), ensure_ascii=False)
    raw = json.loads(line)
    back = Event(kind=raw["kind"], t=raw["t"], data=raw["data"])
    assert back == events[0]

    d = back.data
    assert d["term"] == "RTP"
    assert d["gloss"] == "RTP 是即時傳輸協定"
    assert d["sources"] == [{"title": "維基百科", "url": "https://zh.wikipedia.org/wiki/RTP"}]
    assert d["first"] == {"speaker": "Alex", "t": 10.0, "text": "我們用 RTP 收封包，這樣延遲比較低。"}
    assert d["explained"]["t"] == 20.0
    assert d["mentions"] == 2


def test_subscriber_sees_glossary_like_any_other_event(monkeypatch):
    """觀戰 UI 是靠 subscribers 收事件的——新種類不必改 spectator.py 就會落到畫面。"""
    session = Session(state_with(TRANSCRIPT))
    seen = []
    session.subscribers.append(seen.append)
    _run_one_glossary_pass(session, monkeypatch, lambda b, k, p: ["RTP"])
    assert [e.kind for e in seen] == ["glossary"]


# ── 8. 事件 payload 契約 ────────────────────────────────────────────────
def test_card_payload_matches_the_documented_schema():
    card = g.Card(term="RTP", mentions=2,
                  first=g.Mention("Alex", 10.0, "我們用 RTP"),
                  explained=g.Mention("MiMi", 20.0, "RTP 就是即時傳輸協定"),
                  gloss="即時傳輸協定", sources=(g.Source("維基", "https://x.test/a"),))
    data = card.as_data()
    assert set(data) == {"term", "mentions", "first", "explained", "gloss", "sources"}
    assert set(data["first"]) == {"speaker", "t", "text"}
    json.dumps(data, ensure_ascii=False)   # 必須可序列化，否則寫不進 events.jsonl


def test_card_payload_when_nothing_was_found_online():
    card = g.build_card("RTP", state_with(TRANSCRIPT).utterances)
    data = card.as_data()
    assert data["gloss"] is None and data["sources"] == []
    assert data["explained"] is not None    # 但會議裡有人解釋過，所以還是印得出來
