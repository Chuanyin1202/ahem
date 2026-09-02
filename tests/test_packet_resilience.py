"""驗證 T-A：一個壞封包不該拆掉整個語音接收器。

discord-ext-voice-recv 原生行為：`PacketRouter._do_run`（router.py:106-113）
或 `AudioReader.callback`（reader.py:181-186）任何解碼／路由例外都會冒到頂層，
導致 `reader.voice_client.stop_listening()` 被呼叫，整場接收器被拆掉。

這裡直接操作真的 `PacketRouter` 與 `AudioReader.callback`，不連 Discord：
- 這台測試機沒裝 libopus，`PacketDecoder` 建構時的 `discord.opus.Decoder()`
  換成假的——測試只驗證「例外處理路徑」，不驗證真的音訊解碼。
- 用 monkeypatch 讓特定封包的解碼／路由拋例外，驗證
  `patch_packet_resilience()` 套用後其餘封包／ssrc 不受影響。

`patch_packet_resilience()` 是直接改 `PacketRouter._do_run` /
`AudioReader.callback` 這兩個 class attribute（跟 `patch_dave_receive` 等
既有 patch 同一風格），不透過 `monkeypatch.setattr` 就不會在測試結束時自動
還原，會污染同一 process 裡的其他測試。所以這裡一律透過 `apply_patch` /
`maybe_patch` fixture 套用——fixture 先用 `monkeypatch.setattr(cls, name,
cls.name)` 記住 patch 前的原方法（monkeypatch 只記錄呼叫當下的值，之後
不管誰用什麼方式改了這個屬性，teardown 時都會照樣還原成這個記住的值），
測試結束就自動還原。
"""
from __future__ import annotations

import struct
import time
from types import SimpleNamespace

import discord.opus as discord_opus_root
import pytest
from discord.ext.voice_recv import opus as voice_recv_opus
from discord.ext.voice_recv import reader as reader_mod
from discord.ext.voice_recv import router as router_mod
from discord.ext.voice_recv import rtp
from discord.ext.voice_recv.reader import AudioReader
from discord.ext.voice_recv.router import PacketRouter
from discord.ext.voice_recv.sinks import BasicSink

from meeting_host.discord_source import _note_bad_packet, patch_packet_resilience

SSRC_A = SSRC = 123456789
SSRC_B = 223456789
USER_ID_A = USER_ID = 987654321
USER_ID_B = 887654321


def raw_rtp_bytes(seq: int, ts: int, payload: bytes, *, ssrc: int = SSRC) -> bytes:
    header = struct.pack(">BBHII", 0x80, 0x78, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    return header + payload


def make_rtp(seq: int, ts: int, payload: bytes, *, ssrc: int = SSRC) -> rtp.RTPPacket:
    packet = rtp.RTPPacket(raw_rtp_bytes(seq, ts, payload, ssrc=ssrc))
    packet.decrypted_data = payload
    return packet


class _FakeOpusDecoder:
    """真的 libopus 這台測試機沒裝；這裡只驗證例外處理，不驗證音訊內容，
    換一個永遠成功的假解碼器就好。"""

    def decode(self, data, *, fec=False):
        return b"\x00" * 10


class FakeMember:
    def __init__(self, uid: int):
        self.id = uid
        self.display_name = f"測試者-{uid}"


class FakeGuild:
    def __init__(self, known_ids):
        self._known_ids = set(known_ids)

    def get_member(self, uid):
        return FakeMember(uid) if uid in self._known_ids else None


class FakeClient:
    def __init__(self, known_ids):
        self._known_ids = set(known_ids)

    def get_user(self, uid):
        return FakeMember(uid) if uid in self._known_ids else None


class FakeVoiceClient:
    """支援多 ssrc 對多 user 的對照表，讓測試能驗證「一個 ssrc 壞掉不影響
    另一個 ssrc」。"""

    def __init__(self, ssrc_to_id: dict[int, int] | None = None):
        self._ssrc_to_id = dict(ssrc_to_id or {SSRC: USER_ID})
        self.guild = FakeGuild(self._ssrc_to_id.values())
        self.client = FakeClient(self._ssrc_to_id.values())
        self.stop_listening_called = 0

    def _get_id_from_ssrc(self, ssrc):
        return self._ssrc_to_id.get(ssrc)

    def stop_listening(self):
        self.stop_listening_called += 1


class FakeEventRouter:
    def dispatch(self, *a, **k):
        pass


class FakeReader:
    """PacketRouter 只需要 reader 提供 .error / .voice_client / .event_router。"""

    def __init__(self, vc):
        self.error = None
        self.voice_client = vc
        self.event_router = FakeEventRouter()


def _wait_until(predicate, timeout=2.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _fake_opus_decoder(monkeypatch):
    monkeypatch.setattr(voice_recv_opus, "Decoder", _FakeOpusDecoder)
    fake_lib = SimpleNamespace(opus_strerror=lambda code: b"mock opus error")
    monkeypatch.setattr(discord_opus_root, "_lib", fake_lib)


def _remember_originals(monkeypatch):
    """讓 monkeypatch 記住 patch 前的 `_do_run` / `callback`，測試結束自動還原。"""
    monkeypatch.setattr(router_mod.PacketRouter, "_do_run", router_mod.PacketRouter._do_run)
    monkeypatch.setattr(reader_mod.AudioReader, "callback", reader_mod.AudioReader.callback)


@pytest.fixture
def apply_patch(monkeypatch):
    """套用 `patch_packet_resilience()`，測試結束自動還原（見檔案開頭說明）。"""
    _remember_originals(monkeypatch)
    patch_packet_resilience()


PATCHED = pytest.param(True, id="patched")
UNPATCHED = pytest.param(
    False,
    id="unpatched",
    marks=pytest.mark.xfail(
        strict=True,
        reason="RED 證據：沒套 patch_packet_resilience() 時，一個壞封包會拆掉整個接收器",
    ),
)


@pytest.fixture
def maybe_patch(request, monkeypatch):
    """依 `request.param`（indirect parametrize）決定要不要套用
    `patch_packet_resilience()`；不管套不套用，`_do_run`/`callback`都會在
    測試結束後自動還原。

    搭配 `UNPATCHED`（`xfail(strict=True)`）使用時，`patched=False` 的那組
    參數化案例就是自動化的 RED 證據：斷言在沒套 patch 的原生行為下必須
    失敗，若哪天不小心通過了（例如上游自己修掉了這個問題），`strict=True`
    會讓 xpass 也算測試失敗，逼人回頭確認。
    """
    _remember_originals(monkeypatch)
    if request.param:
        patch_packet_resilience()


@pytest.mark.parametrize("maybe_patch", [PATCHED, UNPATCHED], indirect=True)
def test_router_survives_bad_packet_and_keeps_routing(maybe_patch, monkeypatch):
    """T-A 驗收 1（router 路徑）：執行緒仍存活、stop_listening 未被呼叫，
    壞封包只丟失自己那一筆，其餘封包仍送到 sink。"""
    bad_seq = 5  # 第 6 個封包解碼失敗
    orig_decode_packet = voice_recv_opus.PacketDecoder._decode_packet

    def flaky_decode_packet(self, packet):
        if getattr(packet, "sequence", None) == bad_seq:
            raise discord_opus_root.OpusError(1)
        return orig_decode_packet(self, packet)

    monkeypatch.setattr(voice_recv_opus.PacketDecoder, "_decode_packet", flaky_decode_packet)

    received = []

    def on_audio(user, data):
        if data.packet.sequence < n_packets:  # 過濾掉下面的 flush 封包
            received.append(data)

    sink = BasicSink(on_audio, decode=True)
    vc = FakeVoiceClient({SSRC: USER_ID})
    sink._voice_client = vc
    reader = FakeReader(vc)

    router = PacketRouter(sink, reader)
    router.start()
    router.set_user_id(SSRC, USER_ID)

    n_packets = 12
    for i in range(n_packets):
        router.feed_rtp(make_rtp(i, i * 960, b"\x01\x02\x03"))
        time.sleep(0.03)  # 貼近真實 opus frame 間隔（20ms），讓 router 有機會逐筆處理
    # jitter buffer 的 prefsize=1 設計會扣住最後一筆，等下一筆進來才確認
    # 可以釋出——不然「最後一筆」永遠卡在 buffer 裡，跟壞封包無關。餵一筆
    # 不列入計算的 flush 封包把它推出來。
    router.feed_rtp(make_rtp(n_packets, n_packets * 960, b"\x01\x02\x03"))

    assert _wait_until(lambda: len(received) >= n_packets - 3)

    assert router.is_alive() is True  # 執行緒沒有被壞封包弄死
    assert vc.stop_listening_called == 0
    assert reader.error is None
    # decoder.reset() 會連 jitter buffer 一起清空，壞封包附近可能連帶丟一兩筆
    # 還沒處理到的緩衝封包，但「後續正常封包仍送到 sink」才是驗收重點——
    # 不是精確的 N-1（那是沒有任何 buffering 副作用時的理想值）。
    assert len(received) >= n_packets - 3
    # 壞封包之後的封包確實有送達，證明不是整場斷流
    assert any(data.packet.sequence > bad_seq for data in received)

    router.stop()
    router.join(timeout=2.0)


def test_bad_packet_on_one_ssrc_does_not_affect_another(apply_patch, monkeypatch):
    """T-A 補充：一個 ssrc 的壞封包不該波及其他 ssrc——ssrc B 要精確收到
    全部封包，ssrc A 只丟自己那一路的（含 buffer reset 的少量連帶損失）。"""
    bad_seq = 5
    orig_decode_packet = voice_recv_opus.PacketDecoder._decode_packet

    def flaky_decode_packet(self, packet):
        if packet.ssrc == SSRC_A and getattr(packet, "sequence", None) == bad_seq:
            raise discord_opus_root.OpusError(1)
        return orig_decode_packet(self, packet)

    monkeypatch.setattr(voice_recv_opus.PacketDecoder, "_decode_packet", flaky_decode_packet)

    received_a: list = []
    received_b: list = []

    def on_audio(user, data):
        if data.packet.sequence >= n_packets:  # 過濾掉下面的 flush 封包
            return
        (received_a if data.packet.ssrc == SSRC_A else received_b).append(data)

    sink = BasicSink(on_audio, decode=True)
    vc = FakeVoiceClient({SSRC_A: USER_ID_A, SSRC_B: USER_ID_B})
    sink._voice_client = vc
    reader = FakeReader(vc)

    router = PacketRouter(sink, reader)
    router.start()
    router.set_user_id(SSRC_A, USER_ID_A)
    router.set_user_id(SSRC_B, USER_ID_B)

    n_packets = 12
    for i in range(n_packets):
        router.feed_rtp(make_rtp(i, i * 960, b"\x01\x02\x03", ssrc=SSRC_A))
        router.feed_rtp(make_rtp(i, i * 960, b"\x04\x05\x06", ssrc=SSRC_B))
        time.sleep(0.03)
    # jitter buffer 的 prefsize=1 設計會扣住每個 ssrc 最後一筆，餵一筆不列入
    # 計算的 flush 封包把它推出來（見同檔另一則測試的說明）。
    router.feed_rtp(make_rtp(n_packets, n_packets * 960, b"\x01\x02\x03", ssrc=SSRC_A))
    router.feed_rtp(make_rtp(n_packets, n_packets * 960, b"\x04\x05\x06", ssrc=SSRC_B))

    assert _wait_until(lambda: len(received_b) >= n_packets and len(received_a) >= n_packets - 3)

    assert router.is_alive() is True
    assert vc.stop_listening_called == 0
    assert reader.error is None
    # ssrc B 完全不受 ssrc A 的壞封包影響：精確收到全部
    assert len(received_b) == n_packets
    assert len(received_a) >= n_packets - 3
    assert any(data.packet.sequence > bad_seq for data in received_a)

    router.stop()
    router.join(timeout=2.0)


def test_sink_write_exception_is_isolated(apply_patch):
    """T-A：`sink.write()` 拋例外（不是解碼失敗，是下游 callback 本身出錯）
    也要被隔離，不能拆掉整個接收器。"""
    bad_seq = 5
    received: list = []

    def on_audio(user, data):
        if data.packet.sequence >= n_packets:  # 過濾掉下面的 flush 封包
            return
        if data.packet.sequence == bad_seq:
            raise RuntimeError("boom: simulated sink.write failure")
        received.append(data)

    sink = BasicSink(on_audio, decode=True)
    vc = FakeVoiceClient({SSRC: USER_ID})
    sink._voice_client = vc
    reader = FakeReader(vc)

    router = PacketRouter(sink, reader)
    router.start()
    router.set_user_id(SSRC, USER_ID)

    n_packets = 12
    for i in range(n_packets):
        router.feed_rtp(make_rtp(i, i * 960, b"\x01\x02\x03"))
        time.sleep(0.03)
    # jitter buffer 的 prefsize=1 設計會扣住最後一筆，餵一筆不列入計算的
    # flush 封包把它推出來（見同檔第一則測試的說明）。
    router.feed_rtp(make_rtp(n_packets, n_packets * 960, b"\x01\x02\x03"))

    assert _wait_until(lambda: len(received) >= n_packets - 3)

    assert router.is_alive() is True
    assert vc.stop_listening_called == 0
    assert reader.error is None
    assert len(received) >= n_packets - 3
    assert any(data.packet.sequence > bad_seq for data in received)

    router.stop()
    router.join(timeout=2.0)


class FakePacketRouterForCallback:
    def __init__(self, fail_calls: int = 0):
        """前 `fail_calls` 次 `feed_rtp()` 呼叫都拋例外，之後恢復正常。"""
        self._fail_calls = fail_calls
        self.feed_rtp_calls = 0
        self.delivered: list = []

    def feed_rtp(self, packet):
        self.feed_rtp_calls += 1
        if self.feed_rtp_calls <= self._fail_calls:
            raise RuntimeError(f"boom: simulated feed_rtp failure #{self.feed_rtp_calls}")
        self.delivered.append(packet)

    def feed_rtcp(self, packet):
        pass


class FakeSpeakingTimer:
    def notify(self, ssrc):
        pass


class FakeReaderForCallback:
    def __init__(self, fail_calls: int = 0):
        self.decryptor = SimpleNamespace(
            decrypt_rtp=lambda packet: b"\x01\x02\x03",
            decrypt_rtcp=lambda data: data,
        )
        self.voice_client = SimpleNamespace(_ssrc_to_id={SSRC: USER_ID}, secret_key=b"x" * 32)
        self.packet_router = FakePacketRouterForCallback(fail_calls)
        self.speaking_timer = FakeSpeakingTimer()
        self.error = None
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1

    def _is_ip_discovery_packet(self, data):
        return False


@pytest.mark.parametrize("maybe_patch", [PATCHED, UNPATCHED], indirect=True)
def test_callback_survives_feed_rtp_exception(maybe_patch):
    """T-A 驗收 1（callback 路徑）：feed_rtp 拋例外時不呼叫 stop()，
    error 維持 None，封包直接丟掉。"""
    fake_self = FakeReaderForCallback(fail_calls=1)
    packet_bytes = raw_rtp_bytes(0, 0, b"\x01\x02\x03")

    AudioReader.callback(fake_self, packet_bytes)

    assert fake_self.packet_router.feed_rtp_calls == 1
    assert fake_self.stop_calls == 0
    assert fake_self.error is None


def test_callback_still_routes_good_packets(apply_patch):
    """patch 不影響正常封包：feed_rtp 不拋例外時照常往下派送。"""
    fake_self = FakeReaderForCallback(fail_calls=0)
    packet_bytes = raw_rtp_bytes(0, 0, b"\x01\x02\x03")

    AudioReader.callback(fake_self, packet_bytes)

    assert fake_self.packet_router.feed_rtp_calls == 1
    assert fake_self.stop_calls == 0
    assert fake_self.error is None


def test_same_reader_recovers_after_feed_rtp_failure(apply_patch):
    """T-A：同一個 reader 先遇到一次 `feed_rtp` 失敗，之後正常封包仍要能
    送達——不是靠「整個測試換一個全新 reader」矇混過關，驗證的是同一個
    reader 實例能不能從一次失敗中恢復。"""
    fake_self = FakeReaderForCallback(fail_calls=1)

    bad_packet_bytes = raw_rtp_bytes(0, 0, b"\x01\x02\x03")
    good_packet_bytes = raw_rtp_bytes(1, 960, b"\x04\x05\x06")

    AudioReader.callback(fake_self, bad_packet_bytes)
    assert fake_self.stop_calls == 0
    assert fake_self.error is None

    AudioReader.callback(fake_self, good_packet_bytes)

    assert fake_self.packet_router.feed_rtp_calls == 2
    assert len(fake_self.packet_router.delivered) == 1
    assert fake_self.packet_router.delivered[0].sequence == 1
    assert fake_self.stop_calls == 0
    assert fake_self.error is None


def test_bad_packet_rate_limit_summarizes_after_threshold():
    """T-A 驗收 4：同一 ssrc 10 秒內超過 50 次壞封包只印一次彙總訊息。

    直接呼叫 `_note_bad_packet` 並注入時鐘，不必真的等 10 秒。
    """
    state: dict[int, dict] = {}
    ssrc = 111
    base = 1000.0

    messages = [
        _note_bad_packet(state, ssrc, base + i * 0.05, "OpusError，已重置該路解碼器")
        for i in range(60)
    ]

    # 前 50 次逐筆印
    assert all(m is not None for m in messages[:50])
    assert all("持續壞封包" not in m for m in messages[:50])
    # 第 51 次是彙總訊息
    assert messages[50] is not None
    assert "持續壞封包" in messages[50]
    # 51 次之後（同一個 10 秒窗口內）不再印
    assert all(m is None for m in messages[51:60])

    # 視窗過期（超過 10 秒）後重新計數，會再印一次逐筆訊息
    resumed = _note_bad_packet(state, ssrc, base + 11.0, "OpusError，已重置該路解碼器")
    assert resumed is not None
    assert "持續壞封包" not in resumed


def test_bad_packet_rate_limit_resets_exactly_at_window_edge():
    """T-A 修正回合 1 F1：視窗邊界要用 `>=`——恰好 10.0 秒也算過期。

    原本用 `>`，恰好 10.0 秒時 `10.0 > 10.0` 為 False，不會重置，殘留的
    彙總狀態會讓這一刻本該是新視窗第 1 筆的壞封包被誤判為「已經彙總過，
    不再印」。改成 `>=` 後，恰好 10.0 秒就視為新視窗，回到逐筆印。
    """
    state: dict[int, dict] = {}
    ssrc = 222
    base = 5000.0

    for _ in range(51):
        _note_bad_packet(state, ssrc, base, "x")  # 51 次：已進入彙總狀態

    msg = _note_bad_packet(state, ssrc, base + 10.0, "x")  # 恰好 10.0 秒
    assert msg is not None
    assert "持續壞封包" not in msg


def test_bad_packet_rate_limit_is_per_ssrc():
    """不同 ssrc 的計數互不影響。"""
    state: dict[int, dict] = {}
    now = 2000.0

    for _ in range(50):
        _note_bad_packet(state, 1, now, "x")

    # ssrc=1 已經到門檻，第 51 次應該是彙總訊息
    msg_ssrc1 = _note_bad_packet(state, 1, now, "x")
    assert "持續壞封包" in msg_ssrc1

    # ssrc=2 是全新的 ssrc，應該照常逐筆印
    msg_ssrc2 = _note_bad_packet(state, 2, now, "x")
    assert msg_ssrc2 is not None
    assert "持續壞封包" not in msg_ssrc2
