"""Discord 語音頻道音訊來源。

每位使用者是獨立音軌，所以「誰在說話」由平台直接提供——
不需要 speaker diarization，這是選 Discord 而非單麥克風的主因。

⚠️ 官方 discord.py 至今未合併語音接收（PR #6507），這裡用的是第三方
   experimental 套件 discord-ext-voice-recv。
"""
import asyncio
import ctypes.util
import time
import weakref
from collections.abc import Callable

import discord
from discord.ext import voice_recv

try:
    import davey  # DAVE(E2EE) 解密；沒裝的話 Discord 不會啟用 E2EE
except ImportError:  # pragma: no cover
    davey = None

from .speaker import Output
from .stt import STTPool

# 壞封包頻率限制：同一 ssrc 10 秒內超過這個次數，只印一次彙總訊息，不再逐筆印。
_BAD_PACKET_WINDOW_SECONDS = 10.0
_BAD_PACKET_SUMMARY_THRESHOLD = 50

# 按 PacketRouter 實例分開計數（每場會議一個 router）；用 weak key 避免持有已結束
# 會議的 router 不放。
_BAD_PACKET_STATE: "weakref.WeakKeyDictionary[object, dict[int, dict]]" = weakref.WeakKeyDictionary()


def _note_bad_packet(state: dict[int, dict], ssrc: int, now: float, detail: str) -> str | None:
    """更新單一 ssrc 的壞封包計數狀態，回傳這次要印的訊息（None＝不印）。

    抽成純函式方便測試直接注入時鐘驗證頻率限制，不必真的等 10 秒。
    """
    entry = state.setdefault(ssrc, {"count": 0, "window_start": now, "summarized": False})
    if now - entry["window_start"] >= _BAD_PACKET_WINDOW_SECONDS:
        entry["count"] = 0
        entry["window_start"] = now
        entry["summarized"] = False

    entry["count"] += 1
    if entry["count"] <= _BAD_PACKET_SUMMARY_THRESHOLD:
        return f"⚠️ 壞封包（ssrc={ssrc}）：{detail}"
    if not entry["summarized"]:
        entry["summarized"] = True
        return (
            f"⚠️ ssrc={ssrc} 持續壞封包，可能是連線問題"
            f"（10 秒內已超過 {_BAD_PACKET_SUMMARY_THRESHOLD} 次，不再逐筆印）"
        )
    return None


def patch_keepalive_for_macos() -> None:
    """修掉 discord-ext-voice-recv 在 macOS 上的 keepalive 忙迴圈。

    上游 UDPKeepAlive.run 用 `sendto(packet, addr)` 送 keepalive，但那個 socket
    已經 connected——BSD socket 會拋 OSError 56（Linux 允許，所以上游沒發現）。
    而例外分支裡沒有 sleep，於是每秒重試上萬次、吃滿一顆核心，
    接收執行緒被餓死 → 症狀是「連得上、看得到成員、但一個音訊封包都收不到」。

    已連線的 socket 正確用法是 send()。順便補上失敗時的退避。
    """
    import time as _time

    from discord.ext.voice_recv import reader

    def run(self) -> None:  # noqa: ANN001
        self.voice_client.wait_until_connected()
        while not self._end_thread.is_set():
            vc = self.voice_client
            try:
                packet = self.counter.to_bytes(8, "big")
            except OverflowError:
                self.counter = 0
                continue
            try:
                vc._connection.socket.send(packet)  # 不帶位址
            except Exception:  # noqa: BLE001
                vc.wait_until_connected()
                if not vc.is_connected():
                    break
                _time.sleep(1)  # 上游少了這行，才會變忙迴圈
                continue
            self.counter += 1
            _time.sleep(self.delay / 1000)  # delay 單位是毫秒

    reader.UDPKeepAlive.run = run


def patch_dave_receive() -> None:
    """讓 voice_recv 看得懂 Discord 的 DAVE 端對端加密音訊。

    Discord 的語音已預設啟用 DAVE（E2EE，MLS）。只要環境裡裝了 `davey`，
    discord.py 就會在 voice IDENTIFY 送出 `max_dave_protocol_version=1`，
    Discord 於是回 `dave_protocol_version: 1`——RTP payload 在傳輸層
    （aead_xchacha20_poly1305_rtpsize）解密後**仍是 MLS 密文**。

    discord-ext-voice-recv 不知道 DAVE 的存在，把密文直接餵給 opus 解碼器，
    第一個真實語音封包就炸出 OpusError('corrupted stream')；
    例外從 PacketRouter._do_run 冒上來，run() 的 finally 呼叫 stop_listening()，
    接收器被永久拆掉——症狀是「收到十幾個封包後完全斷流，但 connected=True
    且沒有任何錯誤訊息」。

    這裡在傳輸層解密之後、交給解碼器之前，補上 DAVE 解密那一層。
    MLS session 的建立與 epoch 轉換由 discord.py 的 voice gateway 自行維護，
    我們只借用它已經備妥的 `dave_session`。
    """
    from discord.ext.voice_recv import reader

    _orig_init = reader.AudioReader.__init__

    def __init__(self, sink, voice_client, *, after=None):  # noqa: ANN001
        _orig_init(self, sink, voice_client, after=after)

        transport_decrypt = self.decryptor.decrypt_rtp

        def decrypt_rtp(packet):  # noqa: ANN001
            data = transport_decrypt(packet)  # 第一層：傳輸層解密
            state = voice_client._connection
            session = getattr(state, "dave_session", None)
            if session is None or not getattr(state, "dave_protocol_version", 0):
                return data  # 這通連線沒開 E2EE，維持原行為
            user_id = voice_client._get_id_from_ssrc(packet.ssrc)
            if user_id is None:
                return data  # ssrc 還沒對到人，交給上游既有的略過邏輯
            # 第二層：DAVE/MLS 解密。passthrough（轉換期的未加密封包）
            # 由 davey 自己判斷，不需要我們區分。
            return bytes(session.decrypt(user_id, davey.MediaType.audio, data))

        self.decryptor.decrypt_rtp = decrypt_rtp

    reader.AudioReader.__init__ = __init__


def patch_packet_resilience() -> None:
    """讓一個壞封包不再拆掉整個接收器。

    discord-ext-voice-recv 把任何解碼／路由例外都當致命：
    - `PacketRouter._do_run`（router.py:106-113）在迴圈裡呼叫
      `decoder.pop_data()`／`sink.write()`，任何例外都會冒出迴圈，
      被 `run()` 的 except 接住、`finally` **無條件**呼叫
      `reader.voice_client.stop_listening()`——一個 ssrc 的壞封包
      就拆掉整場的接收器。
    - `AudioReader.callback`（reader.py:181-186）呼叫
      `packet_router.feed_rtp()` 時若拋例外，直接
      `self.error = e` 再 `self.stop()`，同樣是全場拆除。

    今早 DAVE 那次就是走前者這條路徑（解密出來的密文直接餵給 opus
    解碼器炸開）；`patch_dave_receive()` 解掉了密文問題本身，但「一個
    壞封包不該弄死整個接收器」是與 DAVE 無關的一般性防護（現場網路
    一抖、任何一個壞封包都會觸發同樣的靜默斷流），仍要獨立補上。

    修法：兩處各自 try/except，壞掉的那一路 `decoder.reset()` 後繼續，
    不影響其他 ssrc、不拆接收器。同一 ssrc 10 秒內超過
    `_BAD_PACKET_SUMMARY_THRESHOLD` 次才會被判定為連線問題，印一次
    彙總訊息後不再逐筆印（見 `_note_bad_packet`）。
    """
    from nacl.exceptions import CryptoError

    from discord.ext.voice_recv import reader as reader_mod
    from discord.ext.voice_recv import router as router_mod
    from discord.ext.voice_recv import rtp

    def _do_run(self) -> None:  # noqa: ANN001
        state = _BAD_PACKET_STATE.setdefault(self, {})
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    try:
                        data = decoder.pop_data()
                    except Exception as e:  # noqa: BLE001
                        decoder.reset()
                        msg = _note_bad_packet(
                            state, decoder.ssrc, time.monotonic(),
                            f"{type(e).__name__}，已重置該路解碼器",
                        )
                        if msg:
                            print(f"    {msg}")
                        continue

                    if data is None:
                        continue

                    try:
                        self.sink.write(data.source, data)
                    except Exception as e:  # noqa: BLE001
                        decoder.reset()
                        msg = _note_bad_packet(
                            state, decoder.ssrc, time.monotonic(),
                            f"{type(e).__name__}，已重置該路解碼器",
                        )
                        if msg:
                            print(f"    {msg}")

    router_mod.PacketRouter._do_run = _do_run

    def callback(self, packet_data: bytes) -> None:  # noqa: ANN001
        packet = rtp_packet = rtcp_packet = None
        try:
            if not rtp.is_rtcp(packet_data):
                packet = rtp_packet = rtp.decode_rtp(packet_data)
                packet.decrypted_data = self.decryptor.decrypt_rtp(packet)
            else:
                packet = rtcp_packet = rtp.decode_rtcp(self.decryptor.decrypt_rtcp(packet_data))

                if not isinstance(packet, rtp.ReceiverReportPacket):
                    reader_mod.log.info(
                        "Received unexpected rtcp packet: type=%s, %s", packet.type, type(packet)
                    )
                    reader_mod.log.debug(
                        "Packet info:\n  packet=%s\n  data=%s", packet, packet_data
                    )
        except CryptoError:
            reader_mod.log.error("CryptoError decoding packet data")
            # 加密封包與 voice secret 不得進入任何層級的日誌。
            reader_mod.log.debug("CryptoError details: packet_len=%s", len(packet_data))
            return
        except Exception:
            if self._is_ip_discovery_packet(packet_data):
                reader_mod.log.debug("Ignoring ip discovery packet")
                return

            reader_mod.log.exception("Error unpacking packet")
            reader_mod.log.debug("Packet data rejected: len=%s", len(packet_data))
        finally:
            if self.error:
                self.stop()
                return
            if not packet:
                return

        if rtcp_packet:
            self.packet_router.feed_rtcp(rtcp_packet)
        elif rtp_packet:
            ssrc = rtp_packet.ssrc

            if ssrc not in self.voice_client._ssrc_to_id:
                if rtp_packet.is_silence():
                    reader_mod.log.debug("Skipping silence packet for unknown ssrc %s", ssrc)
                    return
                else:
                    reader_mod.log.info("Received packet for unknown ssrc %s:\n%s", ssrc, rtp_packet)

            self.speaking_timer.notify(ssrc)
            try:
                self.packet_router.feed_rtp(rtp_packet)
            except Exception as e:  # noqa: BLE001
                # 上游原行為：self.error = e; self.stop()——一個壞封包就拆掉整場
                # 接收器。這裡改成記錄並丟掉該封包，不設 error、不 stop()。
                state = _BAD_PACKET_STATE.setdefault(self.packet_router, {})
                msg = _note_bad_packet(
                    state, ssrc, time.monotonic(), f"{type(e).__name__}，已捨棄該封包",
                )
                if msg:
                    print(f"    {msg}")

    reader_mod.AudioReader.callback = callback


def ensure_opus() -> None:
    """discord.py 在 macOS 上找不到 Homebrew 的 libopus，得手動指路。

    沒載入的話 sink 收到 opus 封包卻解不開，callback 完全不會被呼叫——
    現象是「bot 進得了頻道但一句都收不到」，而且沒有任何錯誤訊息。
    """
    if discord.opus.is_loaded():
        return
    candidates = [ctypes.util.find_library("opus"),
                  "/opt/homebrew/lib/libopus.0.dylib",
                  "/usr/local/lib/libopus.0.dylib"]
    for path in filter(None, candidates):
        try:
            discord.opus.load_opus(path)
            return
        except Exception:  # noqa: BLE001,PERF203
            continue
    raise RuntimeError("找不到 libopus，請先 brew install opus")


class MeetingSink(voice_recv.BasicSink):
    """BasicSink ＋ 聲學 speaking 事件。
    ⚠️ voice_recv 的 speaking_start/stop 只派發給 AudioSink 的 listener，
    掛在 Client 上的 on_voice_member_speaking_start 永遠不會被呼叫（先前那個是死碼）。"""

    def __init__(self, cb, on_start, on_stop):
        super().__init__(cb)
        self._on_start, self._on_stop = on_start, on_stop

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member) -> None:
        self._on_start(member)

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member) -> None:
        self._on_stop(member)


class MeetingBot(discord.Client):
    """進語音頻道聽，把每個人的 PCM 餵給對應的 STT 連線。"""

    # 播放器重建後換上新 Output 時通知呼叫端（live.py 用它把 Chair 換過去）。
    # 宣告在類別上：測試會用 __new__ 略過 __init__，實例屬性不保證存在
    on_output_replaced: "Callable[[Output], None] | None" = None
    # 收到在場真人的第一個真實音訊封包時通知呼叫端（live.py 用它判斷 --say-hello
    # 的問候時機——確認音訊路徑真的通了，而不只是「人在頻道名單裡」）
    on_human_audio: "Callable[[str], None] | None" = None
    # RTP 層「這個人的麥克風正在／不在傳送」訊號（來源見 _voice_start/_voice_stop）。
    # 通知 live.py 把它發成 events.py 的 "voice" 事件——跟同樣叫法相近的 "speaking"
    # kind（來自 STT partial_transcript，見 stt.py 的 Speaking）是完全獨立的來源，
    # 不要混用：這裡量到的是「麥克風有沒有在送封包」，"speaking" 量到的是「STT
    # 有沒有正在辨識出內容」，兩者時間點與觸發條件都不同。
    on_voice_activity: "Callable[[str, bool], None] | None" = None

    def __init__(self, pool: STTPool, channel_id: int | None = None, state=None):
        # 只用非特權 intent：語音接收靠 voice_states 就夠，
        # 不需要在 Developer Portal 開啟 privileged intents（實測確認）
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.pool = pool
        self.channel_id = channel_id
        self.state = state
        self.loop_ref: asyncio.AbstractEventLoop | None = None

    async def on_ready(self) -> None:
        ensure_opus()
        patch_keepalive_for_macos()
        patch_dave_receive()
        patch_packet_resilience()
        self.loop_ref = asyncio.get_running_loop()
        print(f"已登入：{self.user}")

        channel = await self._pick_channel()
        if channel is None:
            print("找不到可加入的語音頻道——請先進一個語音頻道，或指定 --channel")
            return

        print(f"加入語音頻道：{channel.guild.name} / {channel.name}")
        print(f"    opus 解碼器：{'已載入' if discord.opus.is_loaded() else '❌ 未載入'}")
        self.state_sync(channel)  # 名單：從未開口的人也要在，否則「冷落」永遠不觸發
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        self.output = Output()
        vc.play(self.output, after=self._player_died)  # 取代 _Silence：閒置時行為相同
        vc.listen(MeetingSink(self._on_audio, self._voice_start, self._voice_stop))
        self.vc = vc
        print(f"    監聽中：{vc.is_listening()}｜頻道內："
              f"{[m.display_name for m in channel.members if not m.bot]}")

    async def _pick_channel(self):
        if self.channel_id:
            return self.get_channel(self.channel_id)
        # 沒指定就找第一個有人的語音頻道
        for guild in self.guilds:
            for ch in guild.voice_channels:
                if [m for m in ch.members if not m.bot]:
                    return ch
        return None

    def state_sync(self, channel) -> None:
        if self.state is None:
            return
        now = time.perf_counter()
        for m in channel.members:
            if not m.bot:
                self.state.ensure_participant(m.display_name, now)

    def _voice_start(self, member) -> None:  # 音訊執行緒
        # ⚠️ 這裡由 voice_recv 的 SpeakingTimer 呼叫（見該套件 reader.py），已經是
        # 「一段語音」層級的去重——同一 ssrc 在 speaking_timeout_delay（0.2 秒）內
        # 持續有封包不會重複觸發，不是每個 RTP 封包都呼叫一次，不需要在這裡再加防抖。
        if member is None or member.bot or self.loop_ref is None or self.state is None:
            return
        now = time.perf_counter()
        self.loop_ref.call_soon_threadsafe(self.state.voice_started, member.display_name, now)
        if self.on_voice_activity is not None:
            self.loop_ref.call_soon_threadsafe(self.on_voice_activity, member.display_name, True)

    def _voice_stop(self, member) -> None:
        # 觸發頻率同上——由同一顆 SpeakingTimer 在 0.2 秒無封包後判定逾時才呼叫一次。
        if member is None or member.bot or self.loop_ref is None or self.state is None:
            return
        now = time.perf_counter()
        self.loop_ref.call_soon_threadsafe(self.state.voice_stopped, member.display_name, now)
        if self.on_voice_activity is not None:
            self.loop_ref.call_soon_threadsafe(self.on_voice_activity, member.display_name, False)

    def _player_died(self, err, now: float | None = None) -> None:
        """長存活播放器不該停；停了就重建，不然主席從此啞掉。

        ⚠️ 若編碼／送出持續失敗，每次 after 回呼都無條件重建會形成緊迫的
        非同步重啟迴圈——60 秒內超過 3 次就放棄，不再嘗試。
        """
        print(f"    ⚠️ 播放器停止（{err!r}），重建")
        if not (self.loop_ref and getattr(self, "vc", None) and self.vc.is_connected()):
            return
        now = time.perf_counter() if now is None else now
        restarts = [t for t in getattr(self, "_restarts", []) if now - t < 60]
        if len(restarts) >= 3:
            print("    ⚠️ 播放器 60 秒內重啟超過 3 次，放棄重建（主席將無法出聲）")
            self._restarts = restarts
            return
        restarts.append(now)
        self._restarts = restarts
        self.output = Output()
        # 先換 Chair 再排 _replay：Chair 與播放執行緒必須看同一個 Output，
        # 否則主席會繼續往沒人消費的舊佇列寫音訊，從此啞掉（C1）
        if self.on_output_replaced is not None:
            self.on_output_replaced(self.output)
        self.loop_ref.call_soon_threadsafe(self._replay)

    def _replay(self) -> None:
        """在 event loop 執行緒上重新 play；連線可能在排程後、回呼前斷掉。"""
        try:
            self.vc.play(self.output, after=self._player_died)
        except discord.ClientException as e:
            print(f"    ⚠️ 重建播放器失敗：{e}")

    def _on_audio(self, user, data) -> None:
        """由音訊接收執行緒呼叫——不是 event loop 執行緒，
        所以要透過 call_soon_threadsafe 才能安全碰 asyncio.Queue。"""
        if user is None or user.bot or not data.pcm:
            return  # 主席自己或其他 bot 的聲音不進 STT
        name = user.display_name
        self._pkt = getattr(self, "_pkt", 0) + 1
        # 每 100 個封包（約 2 秒）回報一次，用來確認音訊是否「持續」進來——
        # 先前只在第 1、51 個印，導致無法分辨「斷流」與「還沒到 51 個」
        if self._pkt == 1 or self._pkt % 100 == 0:
            print(f"    🎤 {name} 音訊持續中：{self._pkt} 個封包"
                  f"（約 {self._pkt * 0.02:.1f} 秒）")
        if self.loop_ref:
            self.loop_ref.call_soon_threadsafe(self.pool.feed, name, data.pcm)
            # --say-hello 問候時機用的訊號：只需要「第一次」確認音訊路徑通了，
            # 通知一次即可，不必每個封包都排一次 call_soon_threadsafe
            if self.on_human_audio is not None and not getattr(self, "_human_audio_notified", False):
                self._human_audio_notified = True
                self.loop_ref.call_soon_threadsafe(self.on_human_audio, name)

    async def on_voice_state_update(self, member, before, after) -> None:
        if member.bot:
            return
        # 只在「加入的是我們所在的會議頻道」時同步——加入同 guild 其他頻道不算，
        # 而從別的頻道移入會議頻道（before.channel 不是 None）也要算
        target = getattr(getattr(self, "vc", None), "channel", None)
        joined = (after.channel is not None and target is not None
                  and after.channel.id == target.id
                  and (before.channel is None or before.channel.id != target.id))
        # 離開會議頻道（含移到同 guild 其他頻道）：名單不刪——會後統計還要算他的
        # 發言佔比——但要標記成不在場，否則主席會點名一個已經離線的人（I5）
        left = (before.channel is not None and target is not None
                and before.channel.id == target.id
                and (after.channel is None or after.channel.id != target.id))
        if joined:
            print(f"→ {member.display_name} 加入了語音頻道")
            if self.state:
                self.state.ensure_participant(member.display_name, time.perf_counter())
                self.state.absent.discard(member.display_name)
        elif left:
            print(f"← {member.display_name} 離開了語音頻道")
            if self.state:
                self.state.absent.add(member.display_name)
