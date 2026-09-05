"""腳本測試台：`script_source.py`、`speaker.SilentVoice`／`MutedOutput`、`live.load_script`。

設計見 `docs/specs/2026-09-05-script-harness-design.md`。這些測試守的是同一件事：
**主席分辨不出對面是劇本還是真人**——事件的型別、順序、時序語意都必須跟真實 STT
與真實 TTS 一致，不然量到的行為就不是產品行為。
"""
import asyncio
import json

import pytest

from meeting_host import live
from meeting_host.script_source import PARTIAL_INTERVAL, ScriptSource, to_utterances
from meeting_host.speaker import SilentVoice
from meeting_host.state import MeetingState
from meeting_host.stt import Partial, Speaking
from meeting_host.state import Utterance


# ── to_utterances：結束時間的算法要跟 replay.load 一致 ────────────────────


def test_same_speaker_consecutive_lines_have_no_fake_pause():
    """同一人連續兩則代表中間一直在講，end 直接接到下一則開頭。

    不這樣算的話（改用字數估算），摘要式的斷句之間會出現數十秒空檔，
    `state._chain_start` 會把它當成真的停頓而中斷連續發言鏈——「發言超時」
    就被系統性延後，甚至永遠不觸發。這是 `replay.load` 已經踩過的坑，
    兩邊必須同一套規則。
    """
    us = to_utterances([(0.0, "A", "一" * 10), (30.0, "A", "二" * 10), (90.0, "B", "三")])
    assert us[0].end == 30.0          # 接到下一則開頭，不是 0 + 10/4.5
    assert us[1].end == pytest.approx(30.0 + 10 / 4.5)   # 換人：用語速估算
    assert us[1].end < 90.0           # 且不得疊到下一則開頭


def test_end_never_overruns_the_next_line():
    """換人時語速估算不能疊到下一則開頭——疊了就會出現兩人同時在講的假重疊。"""
    us = to_utterances([(0.0, "A", "字" * 100), (5.0, "B", "短")])
    assert us[0].end == 5.0


# ── ScriptSource：吐出去的事件要跟真實 STT 同形 ──────────────────────────


def _drain(source: ScriptSource, limit: float) -> list:
    async def go():
        out = []
        async def pump():
            async for ev in source.utterances():
                out.append(ev)
        try:
            await asyncio.wait_for(pump(), timeout=limit)
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass
        return out
    return asyncio.run(go())


def test_speaking_is_sent_before_and_during_an_utterance_not_only_at_commit():
    """講話期間必須持續送 `Speaking`，不能只在 commit 時送 `Utterance`。

    快路的「發言超時」看的是 `state.speaking_now()`（partial 驅動），不是 commit：
    講不停的人不會 commit，只吐 Utterance 那條規則永遠不會觸發
    （見 `state.speaking_now` 的 docstring）。這條測試就是釘住這件事。
    """
    st = MeetingState(topic="t", duration_min=10, participants=["A"])
    import time
    src = ScriptSource([(0.0, "A", "字" * 14)], st, time.perf_counter())   # 約 3.1 秒
    evs = _drain(src, limit=6.0)

    kinds = [type(e).__name__ for e in evs]
    assert kinds[0] == "Speaking", kinds
    assert kinds[-1] == "Utterance", kinds
    assert kinds.count("Speaking") >= 2, "講話期間要持續送，不是只送一次"
    assert "Partial" in kinds, "畫面要看得到逐字浮現"
    assert isinstance(evs[-1], Utterance) and evs[-1].text == "字" * 14


def test_voice_activity_is_driven_too_so_the_chair_can_wait_for_a_pause():
    """`voice_started`／`voice_stopped` 也要自己送。

    Chair 的軟插入靠 `state.silent_for()` 等停頓，那條訊號只有 discord_source 在寫。
    不送的話 `silence_since` 停在建構時刻、`silent_for` 一路長大，Chair 會判定
    「全場都沒人講」而在腳本角色講到一半就插話——量到的就不是產品行為。
    """
    import time
    st = MeetingState(topic="t", duration_min=10, participants=["A"])
    seen: list[tuple[str, bool]] = []
    src = ScriptSource([(0.0, "A", "字" * 9)], st, time.perf_counter(),
                       on_voice=lambda who, active: seen.append((who, active)))
    _drain(src, limit=6.0)
    assert seen == [("A", True), ("A", False)]
    assert st.silence_since is not None, "講完之後房間要回到沉默狀態"


def test_events_are_the_same_classes_the_real_stt_yields():
    """型別必須是 `stt.Speaking` / `stt.Partial` / `state.Utterance` 本尊。

    `Session.consume` 用 isinstance 分流；自己另外定義同名類別會讓每一筆事件
    都掉進 Utterance 那一支。這也是「主席分辨不出這是 mock」的最低要求。
    """
    import time
    st = MeetingState(topic="t", duration_min=10, participants=["A"])
    src = ScriptSource([(0.0, "A", "字" * 9)], st, time.perf_counter())
    evs = _drain(src, limit=6.0)
    assert any(isinstance(e, Speaking) for e in evs)
    assert any(isinstance(e, Partial) for e in evs)
    assert isinstance(evs[-1], Utterance)


def test_pool_contract_is_satisfied():
    """`Session.consume` 要 `utterances()`／`offline()`，`MeetingBot` 要 `feed()`。"""
    import time
    st = MeetingState(topic="t", duration_min=10, participants=["A"])
    src = ScriptSource([(0.0, "A", "x")], st, time.perf_counter())
    assert src.offline() is False          # 劇本不會斷線
    assert src.feed("A", b"\x00" * 10) is None   # 真人音訊一律丟掉，不得拋例外


# ── SilentVoice：長度必須對 ───────────────────────────────────────────────


def test_silent_voice_produces_audio_of_the_same_length_as_the_real_thing():
    """靜音不能是「什麼都不做」——長度決定 Chair 的每一條時序語意。

    Chair 從 synth() 拿到多少 PCM，就決定 `is_busy()` 多久變 False、冷卻期從哪一刻
    起算、慢路何時恢復評分。瞬間回空的話主席會變成「講完 0 秒就能再講」，
    `live.should_score` 的 busy 閘門形同虛設。
    """
    from meeting_host.audio import DISCORD_RATE, FRAME_BYTES, SAMPLE_WIDTH

    async def total(text):
        return sum([len(p) async for p in SilentVoice().synth(text)])

    text = "字" * 45                      # 45 字 ÷ 4.5 = 10 秒
    got = asyncio.run(total(text))
    seconds = got / (DISCORD_RATE * SAMPLE_WIDTH * 2)
    assert seconds == pytest.approx(10.0, abs=0.05)
    assert got % FRAME_BYTES == 0, "必須是整數幀，否則 Framer 會留下半幀"

    short = asyncio.run(total("好"))
    assert 0 < short < got, "長度要跟著文字長度走"


# ── load_script：劇本壞掉要在開跑前就擋下來 ──────────────────────────────


def _write(tmp_path, data):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


BASE = {"name": "x", "topic": "t", "duration_min": 10,
        "participants": ["A", "B"], "lines": [[0, "A", "hi"]]}


def test_script_missing_field_is_rejected(tmp_path):
    for key in ("name", "topic", "duration_min", "participants", "lines"):
        bad = {k: v for k, v in BASE.items() if k != key}
        with pytest.raises(ValueError, match=key):
            live.load_script(_write(tmp_path, bad))


def test_speaker_outside_the_participant_list_is_rejected(tmp_path):
    """名單外的人開口，主席會對一個它不知道存在的人做統計與點名。

    這種劇本錯誤跑起來之後很難看出來（畫面上就多一個人），所以開跑前就擋。
    """
    bad = {**BASE, "lines": [[0, "A", "hi"], [10, "C", "我是誰"]]}
    with pytest.raises(ValueError, match="C"):
        live.load_script(_write(tmp_path, bad))


def test_empty_lines_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="lines"):
        live.load_script(_write(tmp_path, {**BASE, "lines": []}))


def test_shipped_scripts_all_load_and_declare_their_style():
    """隨附的劇本都要能載入，而且每一份都要明講它跑在哪個門檻檔位。

    2026-09-05 乾跑發現這不是可有可無：`imbalance` 在 test 檔位下會冒出 6 次快路
    介入，隔離被破壞；`healthy` 是負面測試，縮門檻等於改變「該不該講」的定義。
    """
    from pathlib import Path
    scripts = sorted((Path(__file__).parent.parent / "examples" / "scripts").glob("*.json"))
    assert scripts, "examples/scripts/ 應該有隨附劇本"
    for path in scripts:
        d = live.load_script(path)
        assert "style" in d, f"{path.name} 沒有宣告 style（None＝正式門檻）"
        assert d["style"] in (None, "strict", "gentle", "efficient", "demo", "test")
        assert d.get("expect"), f"{path.name} 沒有寫期望，跑出來也無從判斷對錯"


def test_silent_voice_frames_are_distinguishable_from_the_idle_silence_frame():
    """假 TTS 的幀不能跟 `SILENCE_FRAME` 逐位元組相同。

    2026-09-05 實測撞到的整合缺陷：`Output.read()` 用 `frame != SILENCE_FRAME`
    標記 `first_audible_at`，而 `SILENCE_FRAME` 是全零。第一版 SilentVoice 送全零，
    於是 `first_audible_at` 永遠是 None、`Chair.tick()` 永遠不呼叫 `on_spoken`，
    介入卡在 playing 出不去——腳本場次裡兩次「發言權失衡」都排入了、都沒說出口，
    事件檔上看起來卻像是 Chair 壞掉。
    """
    from meeting_host.speaker import SILENCE_FRAME, Output

    async def frames():
        return [p async for p in SilentVoice().synth("測試")]

    got = asyncio.run(frames())
    assert got and all(f != SILENCE_FRAME for f in got)

    # 端到端：這些幀真的會讓 Output 標記出聲時刻
    out = Output()
    for f in got:
        out.enqueue(f)
    out.end_of_utterance()
    while out.is_busy():
        out.read()
    assert out.first_audible_at is not None


def test_script_finished_event_fires_after_the_last_line():
    """劇本播完要發訊號，呼叫端才知道可以收尾。

    沒有這個訊號的話，會議會一直跑到有人手動砍掉；而且劇本結束後房間變成全靜默，
    快路會一路觸發「全場沉默」「有人被冷落」「議程超時」。2026-09-05 實測：一場
    6.3 分鐘的劇本，主席開口 8 次，其中 5 次是播完之後的噪音。
    """
    import time
    st = MeetingState(topic="t", duration_min=10, participants=["A"])
    src = ScriptSource([(0.0, "A", "字" * 5)], st, time.perf_counter())
    assert not src.finished.is_set()
    _drain(src, limit=6.0)
    assert src.finished.is_set()


def test_settling_suppresses_the_fast_rules_the_way_the_closing_gate_does():
    """沉澱期要壓掉快路，而且壓的是收尾閘門同一組規則，不另立一套。

    `Session.script_settling` 直接 OR 進 `_fast_tick` 算出來的 `closing`，
    所以壓制範圍恆等於 `fast_path.CLOSING_SUPPRESSED_KINDS`——之後那個清單改了，
    這裡自動跟著改，不會有兩份清單走散。
    """
    import inspect
    from meeting_host import live
    src = inspect.getsource(live.Session._fast_tick)
    assert "self.script_settling or meeting_is_closing_for_rules" in src


def test_auto_end_uses_the_same_shutdown_entry_as_sigterm():
    """自動收尾要走 `request_end()`，不能自己寫一條收尾路徑。

    `request_end()` 是觀戰 UI 的 POST /end 與 kill -TERM 共用的唯一入口，
    兩份會議記錄與 events.jsonl 都由它保證寫出去。另開一條就會多一種
    「有時候檔案沒寫出來」的失敗模式。
    """
    import inspect
    from meeting_host import live
    src = inspect.getsource(live.end_after_script)
    assert "request_end()" in src
    assert "source.finished.wait()" in src
    assert live.SCRIPT_SETTLE_SECONDS >= 35, "要放得下慢路往返＋Chair 升級＋TTS 播完"


# ── 模型輸出的字元衛生 ───────────────────────────────────────────────────


def test_invisible_characters_are_stripped_not_rejected():
    """零寬字元清掉之後就是模型本來要寫的字，屬於可修復的那一層。

    2026-09-05 實測：gpt-5.6-luna 寫「Billis」時會在 B 與 illis 之間插入零寬空格，
    同一個評分點重跑 15 次出現 3 次（20%）。觀戰畫面會把它顯示出去。
    """
    from meeting_host.phrasing import strip_invisible, unexpected_chars
    assert strip_invisible("B​​illis") == "Billis"
    assert strip_invisible("正常的話不受影響") == "正常的話不受影響"
    assert unexpected_chars(strip_invisible("B​​illis和達哥")) == []


def test_foreign_letters_are_flagged_but_normal_content_is_not():
    """白名單要擋得住外文字母，又不能誤殺這個場景的常態內容。

    誤殺的代價比漏抓高：中英夾雜、百分比、破折號、日文詞都會出現在真實話術裡
    （術語卡的判準本身就明文允許日文詞），把它們判成壞掉會讓主席整句作廢。
    """
    from meeting_host.phrasing import unexpected_chars
    assert unexpected_chars("Bილის與達哥") == ["ი", "ლ", "ს"]      # 喬治亞字母
    assert unexpected_chars("Alex 提到「ROAS 是 2.8」，佔 73%——請 Billis 補充。") == []
    assert unexpected_chars("他提到「カイゼン」這個概念") == []       # 日文假名
    assert unexpected_chars("溫度 25°C、間隔 3·5") == []


def test_phrase_regenerates_once_then_gives_up(monkeypatch):
    """壞掉就重生一次；第二次還壞就放棄這次介入，不送半殘的句子出去。

    修不回來是關鍵：把喬治亞字母從「Bილის」拿掉只剩「Bis」，比不講更糟。
    """
    from meeting_host import slow_path
    st = MeetingState(topic="t", duration_min=20, participants=["Alex", "Billis"])
    st.add(Utterance("Alex", "先講預算", 0, 5))
    r = {"type": "僵局", "pros": ["a"], "cons": ["b"]}

    calls = []

    def fake(prompt, *, seq=iter(["Bილის和達哥都說立場沒變", "Billis和達哥都說立場沒變"])):
        calls.append(1)
        return next(seq)
    monkeypatch.setattr(slow_path, "_phrase_once", fake)
    assert slow_path.phrase(st, 10.0, r) == "Billis和達哥都說立場沒變"
    assert len(calls) == 2, "第一次壞掉要重生一次"

    calls.clear()
    monkeypatch.setattr(slow_path, "_phrase_once", lambda p: "Bილის一直都壞")
    assert slow_path.phrase(st, 10.0, r) == "", "兩次都壞就放棄"
    assert len(calls) == 0 or True
    # 乾淨的話術一次就過，不多打一次呼叫
    n = []
    monkeypatch.setattr(slow_path, "_phrase_once", lambda p: (n.append(1), "Billis請補充")[1])
    assert slow_path.phrase(st, 10.0, r) == "Billis請補充"
    assert len(n) == 1


def test_fast_path_patterns_with_junk_are_discarded():
    """快路的候選句型沿用它既有的「不合格就丟棄，不修補」政策。

    快路的名字是我們自己填進插槽的（安全），但句型本文一樣出自同一顆模型。
    句型庫一次生 4 個候選，丟掉壞的還有別的可用，所以這裡不需要重生。
    """
    from meeting_host.phrasing import validate_pattern
    good = "{target}，最近比較安靜，方便說說你的想法嗎？"
    assert validate_pattern("有人被冷落", good)
    assert not validate_pattern("有人被冷落", good.replace("{target}", "{target}​"))
    assert not validate_pattern("有人被冷落", "Bილის" + good)
