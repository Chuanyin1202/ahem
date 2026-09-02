"""T9：STT 取證探針——只記錄，不判斷、不過濾、不影響 Utterance 產出。

背景：真實會議出現使用者確認「沒說過」的兩筆「OK。」幻覺 commit。分析 66 筆真實
發言後發現「零 partial」很可能只是「短（<2 秒）」的代理變數，不是真假判準——直接拿
它當丟棄規則會誤殺常見的短附和詞（好／對／OK／嗯）。這批測試驗的是探針本身的正確性
與零副作用，不驗證任何丟棄或閘門邏輯（那些本來就沒有實作，見 stt.py 的「不做什麼」）。
"""
import asyncio
import audioop
import json
import struct

import pytest

from meeting_host import stt as stt_mod
from meeting_host.stt import SpeakerStream, Utterance


def _pcm48k_stereo_silence(n_samples: int = 960) -> bytes:
    """n_samples 為每聲道樣本數（960 ≈ 20ms @ 48kHz）。"""
    return b"\x00\x00" * n_samples * 2  # 2 bytes/sample * 2 channels


def _pcm48k_stereo_tone(n_samples: int = 960, amplitude: int = 12000) -> bytes:
    import math
    frames = []
    for i in range(n_samples):
        v = int(amplitude * math.sin(2 * math.pi * 440 * i / 48000))
        frames.append(v)
        frames.append(v)  # 立體聲雙聲道複製同一取樣
    return struct.pack(f"<{len(frames)}h", *frames)


class Recorder:
    """假探針：只把 write() 的內容收進 list，不碰檔案系統。"""

    def __init__(self):
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class ListWS:
    """`_receive` 用的假 websocket：固定訊息清單，跑完自然結束（不需要真的斷線）。
    `.send()` 只記錄，給 `_send()` 測試用。"""

    def __init__(self, messages: list[str]):
        self._messages = list(messages)
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        self._iter = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _committed(text: str) -> str:
    return json.dumps({"message_type": "committed_transcript", "text": text})


def _partial() -> str:
    return json.dumps({"message_type": "partial_transcript"})


# ── 驗收 1：探針關閉時零額外工作 ──────────────────────────────────────

def test_probe_none_by_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("MEETING_STT_PROBE", raising=False)
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0)
    assert stream._probe is None
    # 探針專用狀態完全不建立，不只是「值是 None」
    assert not hasattr(stream, "_probe_lock")
    assert not hasattr(stream, "_probe_packet_rms")
    assert not hasattr(stream, "_probe_partial_count")
    assert not hasattr(stream, "_probe_pending_forced")


def test_feed_never_calls_audioop_rms_when_probe_disabled(monkeypatch):
    monkeypatch.delenv("MEETING_STT_PROBE", raising=False)
    calls = []
    monkeypatch.setattr(stt_mod.audioop, "rms",
                         lambda *a, **kw: calls.append(a) or 0)
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0)
    stream.feed(_pcm48k_stereo_tone())
    stream.feed(_pcm48k_stereo_silence())
    assert calls == []  # 探針關閉時，計算 RMS 的函式完全不被呼叫


def test_receive_never_calls_probe_write_when_disabled(monkeypatch):
    monkeypatch.delenv("MEETING_STT_PROBE", raising=False)
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0)
    stream._speech_start = 0.0
    ws = ListWS([_partial(), _committed("嗯。"), _committed("......")])
    # 探針關閉時 _receive 必須完全按舊行為跑，不因為缺少探針狀態而炸掉
    asyncio.run(stream._receive(ws))
    out_items = []
    while not stream.out.empty():
        out_items.append(stream.out.get_nowait())
    utterances = [i for i in out_items if isinstance(i, Utterance)]
    assert len(utterances) == 1
    assert utterances[0].text == "嗯。"


# ── 驗收 4：RMS 用便宜演算法，一包一次呼叫 ────────────────────────────

def test_feed_calls_audioop_rms_exactly_once_per_packet(monkeypatch):
    calls = []
    real_rms = audioop.rms

    def spy(pcm, width):
        calls.append(len(pcm))
        return real_rms(pcm, width)

    monkeypatch.setattr(stt_mod.audioop, "rms", spy)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream.feed(_pcm48k_stereo_tone())
    stream.feed(_pcm48k_stereo_silence())
    stream.feed(_pcm48k_stereo_tone(n_samples=480))
    assert len(calls) == 3  # 每個 feed() 呼叫的整包只算一次 rms，不逐 sample 跑迴圈


# ── 驗收 2：每個 committed_transcript 都產生一筆，含全部欄位 ─────────

def test_record_probe_contains_all_required_fields(monkeypatch):
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 3.0)
    recorder = Recorder()
    stream = SpeakerStream("Bob", "key", asyncio.Queue(), t0=1.0, probe=recorder)
    stream._speech_start = 0.5
    ws = ListWS([_committed("OK。")])
    asyncio.run(stream._receive(ws))
    assert len(recorder.records) == 1
    rec = recorder.records[0]
    for field in ("speaker", "segment_start", "segment_end", "duration",
                  "partial_count", "partial_window_start", "partial_window_end",
                  "commit_trigger", "text", "is_substantive", "emitted",
                  "segment_ended", "audio"):
        assert field in rec, f"缺少欄位 {field}"
    assert "window_start" in rec["audio"] and "window_end" in rec["audio"]
    assert rec["speaker"] == "Bob"
    assert rec["text"] == "OK。"
    assert rec["segment_start"] == 0.5
    assert rec["segment_end"] == pytest.approx(2.0)  # perf_counter(3.0) - t0(1.0)
    assert rec["duration"] == pytest.approx(rec["segment_end"] - rec["segment_start"])
    assert rec["is_substantive"] is True
    assert rec["emitted"] is True
    assert rec["segment_ended"] is True
    assert rec["commit_trigger"] == "server"


def test_record_probe_written_even_for_non_substantive_text(monkeypatch):
    """探針必須連被丟棄的 commit 都記——is_substantive/emitted 兩欄位才有意義。"""
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 1.0)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream._speech_start = 0.0
    ws = ListWS([_committed("......")])
    asyncio.run(stream._receive(ws))
    assert len(recorder.records) == 1
    rec = recorder.records[0]
    assert rec["text"] == "......"
    assert rec["is_substantive"] is False
    assert rec["emitted"] is False
    assert rec["segment_ended"] is False
    # 主路徑仍然沒有把它變成 Utterance
    assert stream.out.empty()


# ── 音訊能量分佈 ──────────────────────────────────────────────────

def test_audio_stats_reflect_fed_packet_energy(monkeypatch):
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 1.0)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream.feed(_pcm48k_stereo_silence())
    stream.feed(_pcm48k_stereo_tone())
    stream._speech_start = 0.0
    ws = ListWS([_committed("測試")])
    asyncio.run(stream._receive(ws))
    audio = recorder.records[0]["audio"]
    assert audio["packet_count"] == 2
    assert audio["rms_max"] > 0  # tone 那包能量明顯不是 0
    assert audio["rms_p10"] <= audio["rms_p50"] <= audio["rms_p90"] <= audio["rms_max"]
    assert audio["rms_norm_max"] == pytest.approx(audio["rms_max"] / 32768.0)
    assert audio["window_start"] is not None
    assert audio["window_end"] is not None
    assert audio["window_start"] <= audio["window_end"]
    # 下一段 commit 開始，累積的封包必須已經被清空（不跨 segment 汙染）
    assert stream._probe_packet_rms == []
    assert stream._probe_audio_window_start is None
    assert stream._probe_audio_window_end is None


def test_audio_stats_empty_when_no_packets_fed():
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    ws = ListWS([_committed("測試")])
    asyncio.run(stream._receive(ws))
    assert recorder.records[0]["audio"] == {
        "packet_count": 0, "window_start": None, "window_end": None}


# ── partial_count ────────────────────────────────────────────────

def test_partial_count_resets_per_segment(monkeypatch):
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 1.0)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream._speech_start = 0.0
    ws = ListWS([_partial(), _partial(), _committed("好，我懂了"),
                 _committed("嗯")])  # 第二筆 commit 前沒有任何 partial
    asyncio.run(stream._receive(ws))
    assert [r["partial_count"] for r in recorder.records] == [2, 0]


# ── 驗收 5：commit_trigger 區分 client_timeout / server ──────────────

def test_commit_trigger_is_server_when_no_forced_commit_pending(monkeypatch):
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 1.0)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream._speech_start = 0.0
    ws = ListWS([_committed("好")])
    asyncio.run(stream._receive(ws))
    assert recorder.records[0]["commit_trigger"] == "server"
    assert stream._probe_pending_forced == 0


def test_commit_trigger_is_client_timeout_when_forced_commit_was_pending(monkeypatch):
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 1.0)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream._committed_start = 0.0
    stream._probe_pending_forced = 1  # 模擬 _send() 剛送出過一次強制 commit
    ws = ListWS([_committed("好")])
    asyncio.run(stream._receive(ws))
    assert recorder.records[0]["commit_trigger"] == "client_timeout"
    assert stream._probe_pending_forced == 0  # 消費後歸零，下一筆預設回到 server


def test_send_timeout_increments_pending_forced_and_sends_commit_true(monkeypatch):
    """整合測試：真的跑 `_send()` 的逾時路徑，不用手動塞 `_probe_pending_forced`。"""
    monkeypatch.setattr(stt_mod, "SILENCE_COMMIT_SECONDS", 0.03)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    ws = ListWS([])

    async def go():
        stream.feed(_pcm48k_stereo_tone())
        task = asyncio.create_task(stream._send(ws))
        await asyncio.sleep(0.15)  # 讓它拉到那包音訊、逾時一次、之後不再重送
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert stream._probe_pending_forced == 1
    commits_sent = [m for m in ws.sent if m.get("commit") is True]
    assert len(commits_sent) == 1  # 交棒邏輯：逾時後不再重複送 commit=True


# ── 驗收 3：探針開關不改變 Utterance 產出 ────────────────────────────

def test_probe_on_off_produce_identical_utterance_sequence(monkeypatch):
    fixed_time = {"v": 1.0}
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: fixed_time["v"])

    messages = [
        _partial(),
        _committed("嗯。"),
        _committed("......"),  # 非實質內容，兩邊都該被丟棄
        _committed("好的，我們開始。"),
    ]

    async def drive(probe):
        out: asyncio.Queue = asyncio.Queue()
        stream = SpeakerStream("A", "key", out, t0=0.0, probe=probe)
        stream._speech_start = 0.5  # 模擬 _send() 已經在講話中設定過
        ws = ListWS(list(messages))
        await stream._receive(ws)
        results = []
        while not out.empty():
            item = out.get_nowait()
            if isinstance(item, Utterance):
                results.append((item.speaker, item.text, item.start, item.end))
        return results

    off = asyncio.run(drive(None))
    recorder = Recorder()
    on = asyncio.run(drive(recorder))

    assert off == on
    assert len(off) == 2  # 「嗯。」與「好的，我們開始。」，「......」被兩邊一致丟棄
    # 探針開啟時額外記了 3 筆（含被丟棄那筆），但不影響上面 Utterance 序列
    assert len(recorder.records) == 3


# ── MEETING_STT_PROBE 環境變數 → 實際落檔 ────────────────────────────

def test_get_probe_writes_jsonl_under_meetings_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEETING_STT_PROBE", "1")
    monkeypatch.setattr(stt_mod, "_shared_probe", None)
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 2.0)

    out: asyncio.Queue = asyncio.Queue()
    stream = SpeakerStream("A", "key", out, t0=0.0)  # probe=None → 走 _get_probe()
    assert stream._probe is not None
    stream._speech_start = 0.0
    ws = ListWS([_committed("好")])
    asyncio.run(stream._receive(ws))

    files = list((tmp_path / "meetings").glob("stt-probe-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["speaker"] == "A"
    assert record["text"] == "好"


def test_env_flag_zero_does_not_enable_probe(monkeypatch):
    """Review Finding 3：`bool("0")` 是 True，字串 "0" 想關閉卻誤開啟。"""
    monkeypatch.setenv("MEETING_STT_PROBE", "0")
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0)
    assert stream._probe is None


def test_env_flag_false_does_not_enable_probe(monkeypatch):
    monkeypatch.setenv("MEETING_STT_PROBE", "false")
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0)
    assert stream._probe is None


# ── Review Finding 1：雜訊 commit 不能截斷下一筆記錄的累積區間 ────────

def test_noise_commit_does_not_truncate_next_records_window(monkeypatch):
    """真實會議出現過的情境：一句話中間夾雜一筆「......」雜訊 commit。

    既有邏輯（丟棄路徑不重置 `_committed_start`／`_speech_start`）讓雜訊
    commit 之後的真正段落，起點還是同一個——探針的累加器必須跟著這個
    生命週期走，否則雜訊 commit 之後那一筆的 `partial_count`／`audio` 只會
    看到雜訊 commit「之後」那一小截，跟涵蓋整段的 `duration` 對不上，
    進而讓一筆真實發言被誤記成 `partial_count=0`。
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: clock["t"])
    recorder = Recorder()
    # 注意：`t0=0.0` 在建構子的 `t0 or time.perf_counter()` 裡是 falsy，
    # 所以先把 clock 釘在 0.0 建構，讓 self.t0 落在 0.0（既有行為，非本次改動範圍）
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    stream._speech_start = 0.5  # 段落起點固定，不受 perf_counter 影響

    # 段落前段：一個 partial ＋ 一包音訊
    stream.feed(_pcm48k_stereo_tone())
    asyncio.run(stream._receive(ListWS([_partial()])))

    # t=2.0：雜訊 commit（段落還沒結束）
    clock["t"] = 2.0
    asyncio.run(stream._receive(ListWS([_committed("......")])))

    # 雜訊 commit 之後，段落繼續：再一個 partial ＋ 一包音訊
    stream.feed(_pcm48k_stereo_tone())
    asyncio.run(stream._receive(ListWS([_partial()])))

    # t=3.0：真正結束這個段落
    clock["t"] = 3.0
    asyncio.run(stream._receive(ListWS([_committed("好，我們開始討論")])))

    assert len(recorder.records) == 2
    noise_rec, real_rec = recorder.records

    # 兩筆記錄共用同一個段落起點——證明雜訊 commit 沒有打斷累積
    assert noise_rec["segment_start"] == 0.5
    assert real_rec["segment_start"] == 0.5
    assert noise_rec["segment_end"] == pytest.approx(2.0)
    assert real_rec["segment_end"] == pytest.approx(3.0)
    assert noise_rec["segment_ended"] is False
    assert real_rec["segment_ended"] is True

    # 雜訊 commit 這筆：duration／partial_count／audio 三者涵蓋同一個區間 [0.5, 2.0)
    assert noise_rec["duration"] == pytest.approx(
        noise_rec["segment_end"] - noise_rec["segment_start"])
    assert noise_rec["partial_count"] == 1
    assert noise_rec["audio"]["packet_count"] == 1

    # 真正結束段落這筆：涵蓋整個 [0.5, 3.0)，含雜訊 commit 前後兩次 partial／
    # 兩包音訊——沒有被雜訊 commit 截斷成只看到後半段
    assert real_rec["duration"] == pytest.approx(
        real_rec["segment_end"] - real_rec["segment_start"])
    assert real_rec["partial_count"] == 2
    assert real_rec["audio"]["packet_count"] == 2

    # 段落真正結束後，累加器才清空，供下一段落使用
    assert stream._probe_packet_rms == []
    assert stream._probe_partial_count == 0


# ── Review round 2，Finding 1：`_send()` 覆寫 `_committed_start` 不能
# 汙染 audio／partial 的涵蓋區間欄位 ──────────────────────────────────

def test_committed_start_overwrite_does_not_corrupt_audio_window(monkeypatch):
    """`_committed_start` 有兩個獨立寫入端：`_receive()` 的 emit 路徑（上面那個
    測試）跟 `_send()` 的逾時分支。後者每次逾時都會用當下的 `_speech_start`
    覆寫它，不管上一筆 commit 是不是雜訊——所以不能拿 `segment_start` 描述
    audio／partial 涵蓋哪一段，得看 audio/partial 自己記錄的時間戳。

    重現步驟（真的跑 `_send()`，不是手動塞狀態）：
    1. 音訊進來 → `_send()` 設 `_speech_start=t1`
    2. `_send()` 逾時 → 強制 commit，`_committed_start=t1`
    3. `_receive()` 收到「......」雜訊 commit → 不清空累加器
    4. 新音訊進來 → `_send()` 設新的 `_speech_start=t2`，音訊繼續累積在同一包裡
    5. `_send()` 再次逾時 → 強制 commit，`_committed_start=t2`（覆寫掉 t1）
    6. `_receive()` 收到真正的 commit → `segment_start` 回報 t2，
       但 `audio.window_start`／`partial_window_start` 必須反映真正最早的
       t1，不能等於（更晚的）`segment_start`
    """
    monkeypatch.setattr(stt_mod, "SILENCE_COMMIT_SECONDS", 0.03)
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    ws = ListWS([])

    async def go():
        # 步驟 1：第一段音訊，讓 _send 設定 _speech_start=t1
        stream.feed(_pcm48k_stereo_tone())
        send_task = asyncio.create_task(stream._send(ws))
        await asyncio.sleep(0.01)
        await stream._receive(ListWS([_partial()]))  # 段落前段的一次 partial
        await asyncio.sleep(0.08)  # 讓它逾時，送出第一次強制 commit（步驟 2）

        # 步驟 3：雜訊 commit，不清空累加器
        await stream._receive(ListWS([_committed("......")]))

        # 步驟 4：第二段音訊，_send 設定新的 _speech_start=t2
        stream.feed(_pcm48k_stereo_tone())
        await asyncio.sleep(0.01)
        await stream._receive(ListWS([_partial()]))  # 段落後段的一次 partial
        await asyncio.sleep(0.08)  # 再次逾時，覆寫 _committed_start（步驟 5）

        # 步驟 6：真正結束這個段落
        await stream._receive(ListWS([_committed("好，我們開始討論")]))

        send_task.cancel()
        try:
            await send_task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())

    assert len(recorder.records) == 2
    noise_rec, real_rec = recorder.records
    assert noise_rec["segment_ended"] is False
    assert real_rec["segment_ended"] is True

    # `_committed_start` 真的被 `_send()` 的第二次逾時覆寫——兩筆的
    # segment_start 不一樣，這正是問題根源
    assert real_rec["segment_start"] != noise_rec["segment_start"]

    # audio_window 是自己觀察到的時間戳：真正涵蓋兩段音訊，起點跟雜訊 commit
    # 那筆一樣（因為沒被清空），不等於（更早於）real_rec 自己回報的 segment_start
    assert real_rec["audio"]["window_start"] == pytest.approx(
        noise_rec["audio"]["window_start"])
    assert real_rec["audio"]["window_start"] < real_rec["segment_start"]
    assert real_rec["audio"]["packet_count"] == 2  # 兩段各一包，都算進來
    assert noise_rec["audio"]["packet_count"] == 1  # 這筆寫出當下只看到第一段

    # partial_window 同理：不等於（更早於）segment_start
    assert real_rec["partial_count"] == 2
    assert real_rec["partial_window_start"] == pytest.approx(
        noise_rec["partial_window_start"])
    assert real_rec["partial_window_start"] < real_rec["segment_start"]


# ── Review Finding 2：重連必須歸零探針累加器 ─────────────────────────

def test_run_reconnect_resets_probe_accumulators(monkeypatch):
    """`run()` 重連時重置 `_speech_start`／`_committed_start` 的同一段邏輯，
    也要一併歸零探針累加器，否則重連後第一筆記錄會帶著上一條死連線的
    殘留計數，`commit_trigger` 也可能被殘留的 `_probe_pending_forced` 誤標。
    """
    recorder = Recorder()
    stream = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0, probe=recorder)
    # 模擬上一條連線留下的殘留狀態
    stream.feed(_pcm48k_stereo_tone())
    stream._probe_partial_count = 3
    stream._probe_partial_window_start = 1.0
    stream._probe_partial_window_end = 2.0
    stream._probe_pending_forced = 2

    def boom(*a, **kw):
        raise RuntimeError("測試不建立真的網路連線")

    monkeypatch.setattr(stt_mod.websockets, "connect", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(stream.run())

    assert stream._probe_packet_rms == []
    assert stream._probe_audio_window_start is None
    assert stream._probe_audio_window_end is None
    assert stream._probe_partial_count == 0
    assert stream._probe_partial_window_start is None
    assert stream._probe_partial_window_end is None
    assert stream._probe_pending_forced == 0


def test_get_probe_shared_across_streams_in_same_process(tmp_path, monkeypatch):
    """同一場會議多位 speaker 共用同一個檔案（靠 speaker 欄位事後區分）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEETING_STT_PROBE", "1")
    monkeypatch.setattr(stt_mod, "_shared_probe", None)
    monkeypatch.setattr(stt_mod.time, "perf_counter", lambda: 1.0)

    a = SpeakerStream("A", "key", asyncio.Queue(), t0=0.0)
    b = SpeakerStream("B", "key", asyncio.Queue(), t0=0.0)
    assert a._probe is b._probe  # 同一個探針物件

    a._speech_start = 0.0
    b._speech_start = 0.0
    asyncio.run(a._receive(ListWS([_committed("A說話")])))
    asyncio.run(b._receive(ListWS([_committed("B說話")])))

    files = list((tmp_path / "meetings").glob("stt-probe-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    speakers = {json.loads(line)["speaker"] for line in lines}
    assert speakers == {"A", "B"}
