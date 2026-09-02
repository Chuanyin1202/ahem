import asyncio

import pytest

from meeting_host.audio import FRAME_BYTES
from meeting_host.speaker import Chair, Intervention, Output, VoiceError
from meeting_host.state import MeetingState


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class FakeEarcon:
    pcm = b"\x01" * FRAME_BYTES * 5
    seconds = 0.1


class FakeVoice:
    def __init__(self, fail=False): self.fail, self.calls = fail, []
    async def synth(self, text):
        self.calls.append(text)
        if self.fail:
            raise VoiceError("boom")
        for _ in range(3):
            yield b"\x02" * FRAME_BYTES


class FakeVoiceZeroChunks:
    """F1：synth 正常結束但一個 chunk 都沒吐——不該被算成「有出聲」。"""
    def __init__(self): self.calls = []
    async def synth(self, text):
        self.calls.append(text)
        return
        yield b""  # pragma: no cover — 讓 Python 把這函式編成 async generator，但永遠不執行


class FakeVoiceFailAfterYield:
    """F1：hard 路徑先讓 earcon 真的被讀到一幀，再讓 synth 失敗——驗證「提示音已響」仍算出聲。"""
    def __init__(self): self.calls = []
    async def synth(self, text):
        self.calls.append(text)
        await asyncio.sleep(0)  # 讓外面有機會先把 earcon drain 掉
        raise VoiceError("boom-after-earcon")
        yield b""  # pragma: no cover


class FakeVoiceFailMidway:
    """F1：soft 路徑先吐夠多 frame 越過 prebuffer 門檻（已入列可聽的音訊），才失敗。"""
    def __init__(self): self.calls = []
    async def synth(self, text):
        self.calls.append(text)
        for _ in range(12):
            yield b"\x02" * FRAME_BYTES
        raise VoiceError("boom-midway")


class FakeVoiceRaisesValueError:
    """F2：非 VoiceError 例外也不能讓 playing 卡住。"""
    def __init__(self): self.calls = []
    async def synth(self, text):
        self.calls.append(text)
        raise ValueError("weird failure")
        yield b""  # pragma: no cover


class FakeVoiceLongSentence:
    """長句路徑：15 個 frame，每個 frame 前真的讓出一次 event loop（模擬串流逐塊到達）。"""
    def __init__(self): self.calls = []
    async def synth(self, text):
        self.calls.append(text)
        for _ in range(15):
            await asyncio.sleep(0)
            yield b"\x02" * FRAME_BYTES


class FakeVoiceHangsThenConvertsCancellation:
    """R2：synth 掛住直到被 Chair 逾時 cancel，並把 CancelledError 轉成 VoiceError
    ——模擬某些 TTS SDK／HTTP client 在收到取消時把它包裝成別的例外類別的行為。"""
    def __init__(self): self.calls = []
    async def synth(self, text):
        self.calls.append(text)
        try:
            await asyncio.sleep(3600)
            yield b"\x02" * FRAME_BYTES  # pragma: no cover — 永遠等不到，會先被 cancel
        except asyncio.CancelledError:
            raise VoiceError("cancel converted")


def drain(out: Output):
    """模擬播放執行緒把佇列讀空。

    ⚠️ 不可用 out.is_busy()：那包含 _producing 旗標——一句話說到一半、
    音訊還在合成中（例如硬打斷 EARCON_GATE 的真實 asyncio.sleep 等待期間）
    佇列會暫時清空但 is_busy() 仍是 True。這個迴圈是同步的，若拿 is_busy()
    當終止條件，在資料還沒補上前會無限自旋，卡死 event loop——
    連讓 EARCON_GATE 那個真實計時器觸發的機會都沒有，永久死結（非時序抖動，
    已用最小重現腳本驗證：is_busy() 版本會讓整個測試行程卡死 99% CPU 不返回）。

    round 1 review 修正：原本只看 `_q.empty()`／`len(_framer)`，在「framer 裡剩不足
    一幀的尾段、佇列已空、但 EOS 還沒送到」時一樣會自旋（framer len 永遠 >0 但
    read() 內部要等到 EOS 才會 flush）。改成先把佇列現有資料讀空，再多讀一次去
    收掉可能的 EOS／flush 尾段——最多多一次 read()，不會自旋。
    """
    frames = []
    while not out._q.empty():
        frames.append(out.read())
    frames.append(out.read())  # 收掉可能的 EOS（flush 尾段或已無資料就回靜音，都只讀一次）
    return frames


def iv(kind="離題", hard=False, rev=0, t=100.0, text="請回到主題"):
    return Intervention(kind=kind, target=None, text=text, hard=hard, revision=rev, created_at=t)


def make(clock, voice=None, rev=lambda: 0):
    st = MeetingState(topic="t", duration_min=30, participants=[])
    # state 的沉默時鐘預設是裸 perf_counter（I3）；這裡的 Clock 是從 100.0 起算的
    # 假時鐘，兩者不同座標。用 0.0 表示「這個測試場景一開始就已經沉默」
    st.silence_since = 0.0
    out = Output()
    spoken, failed = [], []
    c = Chair(st, out, voice or FakeVoice(), FakeEarcon(), clock=clock, revision=rev,
              on_spoken=lambda i, at: spoken.append((i, at)), on_failed=lambda i, r: failed.append((i, r)))
    return st, out, c, spoken, failed


def make_full(clock, voice=None, rev=lambda: 0, on_escalate=None, on_dropped=None):
    """跟 make() 一樣，多回傳 dropped 清單、支援 on_escalate／on_dropped 覆寫（F5 專用）。

    保留獨立於 make() 之外，避免改動 make() 的回傳 tuple 長度動到既有測試的解構。
    """
    st = MeetingState(topic="t", duration_min=30, participants=[])
    # state 的沉默時鐘預設是裸 perf_counter（I3）；這裡的 Clock 是從 100.0 起算的
    # 假時鐘，兩者不同座標。用 0.0 表示「這個測試場景一開始就已經沉默」
    st.silence_since = 0.0
    out = Output()
    spoken, failed, dropped = [], [], []
    c = Chair(st, out, voice or FakeVoice(), FakeEarcon(), clock=clock, revision=rev,
              on_spoken=lambda i, at: spoken.append((i, at)),
              on_failed=lambda i, r: failed.append((i, r)),
              on_escalate=on_escalate,
              on_dropped=on_dropped or (lambda i, r: dropped.append((i, r))))
    return st, out, c, spoken, failed, dropped


async def ticks(c, clock, n, step=0.1, out=None):
    """每個測試情境必須在同一個 asyncio.run 裡跑完——
    asyncio.run 結束會取消 Chair 建的 _speak task，跨兩次 run 狀態會壞掉。"""
    for _ in range(n):
        await c.tick()
        await asyncio.sleep(0)  # 讓 _speak task 前進
        await asyncio.sleep(0)
        clock.advance(step)
        if out is not None:
            drain(out)


def test_soft_waits_for_one_second_pause():
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        st.voice_started("A", now=clock())
        assert c.request(iv()) is True
        await ticks(c, clock, 5)              # A 還在講
        assert c.playing is None and not spoken
        st.voice_stopped("A", now=clock())
        await ticks(c, clock, 9)              # 0.9s 沉默：還不行
        assert c.playing is None
        # 跨過 1.0s；F1 之後 on_spoken 改在 tick() 裡看 first_audible_at 偵測，
        # 需要「觸發播放的那個 tick」之後至少再一輪 tick 才會回報，5 輪留足餘量
        # （浮點數 Clock 累加 += 0.1 精度不保證剛好卡在第幾輪跨過門檻）。
        await ticks(c, clock, 5, out=out)
        assert spoken and spoken[0][0].kind == "離題"
    asyncio.run(go())


def test_soft_escalates_to_hard_after_15s():
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        st.voice_started("A", now=clock())    # 永遠有人講
        c.request(iv())
        await ticks(c, clock, 149)
        assert not spoken
        await ticks(c, clock, 4, out=out)     # 這裡會升級成硬打斷
        # 升級後的硬打斷用真實 asyncio.sleep(EARCON_GATE) 等待——clock 是假的，
        # tick() 迴圈裡的 sleep(0) 不會讓真實時間前進，必須真的等 _speak task 跑完，
        # 否則會在 spoken 還沒被寫入前就檢查斷言（時序問題，非 Chair 行為錯誤）。
        if c._task is not None and not c._task.done():
            await asyncio.wait_for(c._task, timeout=2.0)
        drain(out)                            # 讀走 task 完成後才入列的幀，first_audible_at 才會設
        await ticks(c, clock, 2, out=out)      # 讓 tick() 偵測到 first_audible_at 並回報 on_spoken
        assert spoken and c.escalated == 1
        assert c.playing is None and not out.is_busy()
    asyncio.run(go())


def test_stale_revision_is_dropped_before_speaking():
    async def go():
        clock = Clock()
        current = {"rev": 0}
        st, out, c, spoken, _ = make(clock, rev=lambda: current["rev"])
        c.request(iv(rev=0))
        current["rev"] = 1                    # 發言者換人了
        await ticks(c, clock, 15, out=out)
        assert not spoken and c.pending is None
    asyncio.run(go())


def test_hard_replaces_pending_soft():
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        st.voice_started("A", now=clock())
        c.request(iv(kind="離題"))
        assert c.request(iv(kind="發言超時", hard=True)) is True
        assert c.pending.kind == "發言超時"
    asyncio.run(go())


def test_hard_plays_earcon_immediately_even_while_talking():
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        st.voice_started("A", now=clock())
        c.request(iv(kind="發言超時", hard=True))
        await ticks(c, clock, 1)
        assert out.read() == b"\x01" * FRAME_BYTES   # earcon 先出
        await ticks(c, clock, 10, out=out)
        # 同上：EARCON_GATE 是真實 asyncio.sleep，假 clock 推不動它，要真的等 task 跑完
        if c._task is not None and not c._task.done():
            await asyncio.wait_for(c._task, timeout=2.0)
        drain(out)
        await ticks(c, clock, 2, out=out)
        assert spoken
        assert c.playing is None and not out.is_busy()
    asyncio.run(go())


def test_request_while_playing_keeps_only_higher_priority_candidate():
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        c.request(iv(kind="離題"))
        await ticks(c, clock, 12)             # 進入 playing（佇列未 drain，保持 busy）
        assert c.playing is not None
        assert c.request(iv(kind="假共識")) is False
        assert c.request(iv(kind="發言超時", hard=True)) is True
        assert c.candidate.kind == "發言超時"
    asyncio.run(go())


def test_tts_failure_reports_and_backs_off():
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoice(fail=True))
        c.request(iv(kind="離題"))
        await ticks(c, clock, 12, out=out)
        assert failed and failed[0][1].startswith("TTS")
        assert not spoken
        assert c.request(iv(kind="離題")) is False   # 30s 內同 kind 退避
        clock.advance(31)
        assert c.request(iv(kind="離題")) is True
    asyncio.run(go())


def test_soft_waits_while_stt_still_hears_speech():
    """裁決 1：軟插入除了看聲學層 silent_for，還要看 STT 的 speaking dict。

    聲學層（voice_recv）已經靜音 ≥1s（silent_for 達標），但 STT partial
    （state.speaking）仍認為 A 在講——這代表聲學層可能有 stale stop race，
    不該在人還在講時開口。等 STT 也確認停了（stopped_speaking），下一輪才開口。
    """
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())    # 聲學層立刻靜音，silent_for 從此刻起算
        st.speaking_now("A", clock())         # 但 STT 仍認為 A 在講
        c.request(iv())
        await ticks(c, clock, 15)             # 遠超過 1.0s，但 STT 還卡著 → 不該開口
        assert c.playing is None and not spoken
        st.stopped_speaking("A")              # STT 終於確認停了
        await ticks(c, clock, 3, out=out)     # 下一輪 tick 才開口
        assert spoken and spoken[0][0].kind == "離題"
    asyncio.run(go())


# ── 修正回合 1 ────────────────────────────────────────────────────────


def test_on_spoken_fires_only_after_first_audible_frame():
    """F1：on_spoken 不能在 synth 結束那一刻就報——要等播放執行緒真的讀到可聽幀。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock)
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 12)   # 跨過 1.0s 停頓，_speak 跑完並把幀入列——但沒人 drain
        assert not spoken           # first_audible_at 還是 None，不算「已出聲」
        drain(out)                  # 這時才真的讀到幀，first_audible_at 被設
        await c.tick()              # 下一輪 tick 偵測到才回報
        assert spoken and spoken[0][0].kind == "離題"
    asyncio.run(go())


def test_zero_chunk_synth_reports_failed_not_spoken():
    """F1：synth 正常結束但 0 chunk——沒有任何可聽幀，不該算介入，要回報失敗。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoiceZeroChunks())
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        # 15 輪：跨過 1.0s 門檻後至少再留幾輪給 tick() 的偵測邏輯跑（見 test_soft_waits_for_one_second_pause 的說明）
        await ticks(c, clock, 15, out=out)
        assert not spoken
        assert failed and failed[0][1] == "沒有任何音訊出聲"
        assert c.playing is None
    asyncio.run(go())


def test_hard_failure_after_earcon_read_still_counts_as_spoken():
    """F1：硬打斷的提示音已經被讀到（可聽見），之後 TTS 才失敗——仍要算「已出聲」。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoiceFailAfterYield())
        c.request(iv(kind="發言超時", hard=True))
        await ticks(c, clock, 1)     # _start 觸發，earcon 入列
        drain(out)                   # 讀走 earcon → first_audible_at 被設（真的「出聲」了）
        await ticks(c, clock, 3)     # 讓 synth 的 VoiceError 真正發生、task 結束
        assert spoken and spoken[0][0].kind == "發言超時"
        assert failed and failed[0][1].startswith("TTS")
    asyncio.run(go())


def test_soft_midway_failure_with_audible_frames_counts_as_spoken():
    """F1：soft 已經入列超過 prebuffer 門檻的音訊（半句話已經在播），中途才失敗——仍算已出聲。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoiceFailMidway())
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 15, out=out)
        assert spoken and spoken[0][0].kind == "離題"
        assert failed and failed[0][1].startswith("TTS")
    asyncio.run(go())


def test_non_voice_error_still_sends_eos_and_frees_playing():
    """F2：非 VoiceError 例外（例如 synth 內部真的爆炸）也要送 EOS，playing 不能永久卡住。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoiceRaisesValueError())
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 15, out=out)
        assert c.playing is None
        assert not out.is_busy()
        assert failed and failed[0][1].startswith("開口例外")
    asyncio.run(go())


def test_playing_times_out_when_player_never_consumes():
    """F2：播放執行緒完全沒消費（player 死了）——45 秒後要強制收尾，不能卡死 playing。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock)
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 12)      # 觸發軟插入播放，但不 drain——佇列一直被視為忙碌
        assert c.playing is not None
        await ticks(c, clock, 460)     # 假時鐘推進 46s，跨過 PLAYING_MAX_SECONDS=45.0
        assert c.playing is None
        assert failed and failed[0][1] == "播放逾時"
    asyncio.run(go())


def test_hard_request_ignores_backoff():
    """F4／R3：規則型硬打斷不能被同 kind 的舊 **soft** 失敗退避吃掉。

    退避 key 帶 hard/soft，兩邊互不吃：這裡失敗的是 soft，所以只有 soft 被擋。
    （hard 自己失敗後會退避 hard，見 test_hard_failure_backs_off_hard_of_same_kind）
    """
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoice(fail=True))
        c.request(iv(kind="離題"))
        await ticks(c, clock, 12, out=out)
        assert failed and not spoken
        assert c.request(iv(kind="離題")) is False              # soft 仍被退避擋
        assert c.request(iv(kind="離題", hard=True)) is True     # hard 不吃 soft 的退避
    asyncio.run(go())


def test_escalation_uses_refreshed_text():
    """F5：15 秒升級成硬打斷時，文字要用 on_escalate 重生，不能沿用 15 秒前的舊句。"""
    async def go():
        clock = Clock()
        voice = FakeVoice()
        fresh = iv(kind="離題", text="fresh")
        st, out, c, spoken, failed, dropped = make_full(clock, voice=voice, on_escalate=lambda old: fresh)
        st.voice_started("A", now=clock())    # 永遠有人講，逼軟插入升級
        c.request(iv(text="stale"))
        await ticks(c, clock, 149)
        assert not spoken
        await ticks(c, clock, 4, out=out)
        if c._task is not None and not c._task.done():
            await asyncio.wait_for(c._task, timeout=2.0)
        assert voice.calls == ["fresh"]
    asyncio.run(go())


def test_escalation_callback_returning_none_drops():
    """F5：on_escalate 回傳 None（呼叫端判斷升級時已不成立）——直接作廢，不硬打斷。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed, dropped = make_full(clock, on_escalate=lambda old: None)
        st.voice_started("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 153, out=out)
        assert not spoken
        assert c.pending is None and c.playing is None
        assert dropped and dropped[0][1] == "升級時已不成立"
    asyncio.run(go())


def test_stale_revision_notifies_on_dropped():
    """F5：revision 過期作廢時要通知呼叫端（on_dropped），不能讓 claimed 狀態沒人清。"""
    async def go():
        clock = Clock()
        current = {"rev": 0}
        st, out, c, spoken, failed, dropped = make_full(clock, rev=lambda: current["rev"])
        c.request(iv(rev=0))
        current["rev"] = 1
        await ticks(c, clock, 15, out=out)
        assert not spoken and c.pending is None
        assert dropped and dropped[0][1] == "revision 過期"
    asyncio.run(go())


def test_long_sentence_waits_for_prebuffer_before_enqueue():
    """長句路徑：累積到 prebuffer 門檻（10 frame）前，不該有任何幀真的入列播放佇列。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, _ = make(clock, voice=FakeVoiceLongSentence())
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 10)   # 觸發軟插入，_speak 開始跑，但不 drain
        assert out._q.qsize() == 0  # 累積到 10 frame（38400 bytes）之前，佇列必須是空的
        await ticks(c, clock, 10, out=out)
        assert spoken and spoken[0][0].kind == "離題"
    asyncio.run(go())


# ── 修正回合 2 ────────────────────────────────────────────────────────


def test_playing_timeout_drops_candidate_instead_of_handoff():
    """R1：逾時代表播放器沒在消費，誰都播不了——candidate 不能交接，直接作廢。
    交接的話 candidate 重設 marker 後，播放執行緒讀到共用 Output 裡的舊資料會被誤記成
    candidate 自己的 on_spoken（舊資料、新招牌）。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed, dropped = make_full(clock)
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv(kind="離題"))
        await ticks(c, clock, 12)      # 觸發軟插入進 playing，不 drain——佇列一直忙碌
        assert c.playing is not None
        assert c.request(iv(kind="發言超時", hard=True)) is True
        assert c.candidate is not None and c.candidate.kind == "發言超時"
        await ticks(c, clock, 460)     # 推 46s，跨過 PLAYING_MAX_SECONDS=45.0
        assert c.playing is None
        assert c.candidate is None     # 沒有被交接到 pending
        assert dropped and dropped[0][0].kind == "發言超時"
        assert dropped[0][1] == "播放器逾時，候選作廢"
        assert not spoken
    asyncio.run(go())


def test_cancelled_task_does_not_double_report_failed():
    """R2：逾時 cancel 後，若 synth 把 CancelledError 轉成 VoiceError，恢復執行的舊
    task 不能再報第二次 on_failed——世代計數＋_failed_reported 旗標雙重防護。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoiceHangsThenConvertsCancellation())
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 12)      # 觸發軟插入進 playing，不 drain
        assert c.playing is not None
        await ticks(c, clock, 460)     # 推 46s，跨過逾時：cancel task、報一次「播放逾時」
        assert c.playing is None
        assert len(failed) == 1 and failed[0][1] == "播放逾時"
        # 讓被 cancel 的舊 task 真的跑到 except VoiceError（synth 內部把 CancelledError 轉換完成）
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(failed) == 1        # 沒有變成兩筆
        assert failed[0][1] == "播放逾時"
    asyncio.run(go())


def test_hard_earcon_enqueue_failure_reports_and_frees():
    """R3：hard 的 earcon enqueue 要在 try 裡——Output 滿時 enqueue(earcon) 拋 VoiceError，
    要能被 except／finally 接住，不能讓 task 沒送 EOS 就異常結束、playing 永久卡住。"""
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock)
        for _ in range(1500):
            out.enqueue(b"\x02" * FRAME_BYTES)  # 先把佇列塞滿，enqueue(earcon) 一定會拋 VoiceError
        c.request(iv(kind="發言超時", hard=True))
        await ticks(c, clock, 3)       # enqueue(earcon) 立刻拋錯，同步跑完 except/finally
        assert c._task is not None and c._task.done()
        assert failed and failed[0][1].startswith("TTS")
        drain(out)                     # 把佇列裡塞的資料讀空（EOS 因佇列滿被 R4 的 Full 分支吃掉）
        await c.tick()                 # 讓 tick() 偵測 task done 且 output 不忙
        assert c.playing is None
    asyncio.run(go())


def test_escalation_fresh_with_stale_revision_is_dropped():
    """R5：on_escalate 重生的 fresh 可能忘記重驗 revision——Chair 自己要驗，
    過期就作廢，不能硬打斷一個已經不成立的世界版本。"""
    async def go():
        clock = Clock()
        current = {"rev": 0}
        fresh = iv(kind="離題", text="fresh", rev=1)   # 跟目前 revision(0) 不一致
        st, out, c, spoken, failed, dropped = make_full(
            clock, rev=lambda: current["rev"], on_escalate=lambda old: fresh)
        st.voice_started("A", now=clock())    # 永遠有人講，逼軟插入升級
        c.request(iv(text="stale", rev=0))
        await ticks(c, clock, 153, out=out)   # 跨過 15s 升級門檻
        assert not spoken
        assert c.pending is None and c.playing is None
        assert dropped and dropped[0][1] == "升級重生的介入 revision 已過期"
    asyncio.run(go())


def test_escalation_fresh_is_forced_hard():
    """R5：on_escalate 回傳的 fresh 就算 hard=False，狀態機也要強制當硬打斷處理——
    不然升級後仍被當成 soft，另一個真正的 hard request 會被誤收成 candidate 而不是被擋下。"""
    async def go():
        clock = Clock()
        fresh = iv(kind="離題", text="fresh", hard=False)   # 呼叫端忘記把 hard 設回 True
        st, out, c, spoken, failed, dropped = make_full(clock, on_escalate=lambda old: fresh)
        st.voice_started("A", now=clock())    # 永遠有人講，逼軟插入升級
        c.request(iv(text="stale"))
        await ticks(c, clock, 153)            # 跨過 15s，升級應觸發 _start(fresh, hard=True)（強制）
        assert c.playing is not None
        assert c.playing.hard is True         # 就算 fresh.hard 傳 False，狀態機也要強制修正
        assert c.request(iv(kind="發言超時", hard=True)) is False   # 播放中已是 hard，同級以下擋掉
        assert c.candidate is None            # 不是被誤收成 candidate
    asyncio.run(go())


# ── T6b：問候語播兩次（音訊層重複，非 Chair 重觸發）───────────────────


def test_long_sentence_is_enqueued_exactly_once():
    """T6b 根因：prebuffer 達標後的每個 chunk 同時被 append 進 frames 又直接 enqueue，
    迴圈結束後的尾段 `for f in frames: enqueue(f)` 把「prebuffer 之後整段」再送一次，
    句子（少前 200ms）重播。這裡直接數播放佇列裡總共出現幾個語音幀，抓真正的重複，
    而不是只看有沒有觸發播放（既有 test_long_sentence_waits_for_prebuffer_before_enqueue
    只驗證「門檻前不搶跑」，抓不到這個 bug）。"""
    async def go():
        clock = Clock()
        voice = FakeVoiceLongSentence()
        st, out, c, spoken, _ = make(clock, voice=voice)
        st.voice_started("A", now=clock())
        st.voice_stopped("A", now=clock())
        c.request(iv())
        await ticks(c, clock, 15)      # 觸發軟插入，_start 開始跑
        assert c._task is not None
        await asyncio.wait_for(c._task, timeout=2.0)   # 真的等 15 個 frame 全部跑完（含中途 sleep(0)）
        voice_frames = 0
        while not out._q.empty() or len(out._framer) > 0:
            f = out.read()
            if f == b"\x02" * FRAME_BYTES:
                voice_frames += 1
        assert voice_frames == 15   # 修正前會是 15 + (15-10) = 20
    asyncio.run(go())


def test_long_hard_sentence_is_enqueued_exactly_once():
    """T6b hard 版本：earcon 只該出現一次（5 幀），語音也只該出現一次（15 幀）——
    hard 路徑走同一段 prebuffer 邏輯，同樣的重複 bug 也會發生在 EARCON_GATE 之後。"""
    async def go():
        clock = Clock()
        voice = FakeVoiceLongSentence()
        st, out, c, spoken, _ = make(clock, voice=voice)
        c.request(iv(kind="發言超時", hard=True))
        await ticks(c, clock, 1)       # 觸發 _start（hard 不用等停頓）
        assert c._task is not None
        await asyncio.wait_for(c._task, timeout=2.0)   # 真的等（含 EARCON_GATE 真實延遲）跑完
        earcon_frames = 0
        voice_frames = 0
        while not out._q.empty() or len(out._framer) > 0:
            f = out.read()
            if f == b"\x01" * FRAME_BYTES:
                earcon_frames += 1
            elif f == b"\x02" * FRAME_BYTES:
                voice_frames += 1
        assert earcon_frames == 5   # FakeEarcon.pcm 剛好是 5 幀，只該出現一次
        assert voice_frames == 15
    asyncio.run(go())


def test_hard_replacing_soft_notifies_dropped():
    """I1b：hard 取代 pending soft 時，被取代的那個 soft 必須通知 on_dropped。

    不通知的話，呼叫端記在 claimed（live.py 的 done）裡的舊觸發永遠沒人清，
    那件事從此不會再被排入——症狀是「規則明明還成立，主席卻再也不提」。
    """
    async def go():
        clock = Clock()
        st, out, c, spoken, failed, dropped = make_full(clock)
        st.voice_started("A", now=clock())    # 有人在講，soft 不會馬上開始播
        soft = iv(kind="離題")
        assert c.request(soft) is True
        assert c.request(iv(kind="發言超時", hard=True)) is True
        assert c.pending.kind == "發言超時"
        assert dropped == [(soft, "被硬打斷取代")]
    asyncio.run(go())


def test_replace_output_carries_audible_marker():
    """R2：播放途中換 Output，已出聲的事實不能跟著舊物件一起消失。

    舊 Output 已播出第一個可聽幀、但 tick 還沒觀察到就被換掉的話，新 Output 的
    marker 是 None——下一 tick 會誤報「沒有任何音訊出聲」，呼叫端釋放 claim，
    同一件事稍後又被提醒一次。
    """
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock)
        c.request(iv(kind="離題"))
        await ticks(c, clock, 12)      # 進入 playing；佇列有資料但還沒被讀
        assert c.playing is not None
        drain(out)                      # 播放執行緒讀到第一個可聽幀
        assert out.first_audible_at is not None
        assert not spoken               # tick 還沒觀察到這件事

        new = Output()
        c.replace_output(new)

        assert c.output is new
        assert len(spoken) == 1
        assert spoken[0][1] == out.first_audible_at
        assert not failed
    asyncio.run(go())


def test_hard_failure_backs_off_hard_of_same_kind():
    """R3：hard 失敗後 30 秒內同 kind 的 hard 也要退避。

    on_failed 會釋放 claim，快路每秒重送同一個 hard；退避若只擋 soft，
    播放器死掉期間就變成一秒響一次提示音。
    """
    async def go():
        clock = Clock()
        st, out, c, spoken, failed = make(clock, voice=FakeVoice(fail=True))
        c.request(iv(kind="發言超時", hard=True))
        await ticks(c, clock, 12, out=out)
        assert failed and failed[0][1].startswith("TTS")
        assert c.playing is None  # 已收尾，否則下面的 request 會走 candidate 分支
        assert c.request(iv(kind="發言超時", hard=True)) is False
        clock.advance(31)
        assert c.request(iv(kind="發言超時", hard=True)) is True
    asyncio.run(go())
