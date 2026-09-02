"""即時語音辨識：每位說話者一條獨立的 ElevenLabs Scribe Realtime 連線。

為什麼一人一條：Scribe v2 Realtime 為了低延遲不做 speaker diarization。
我們不需要它——Discord 每位使用者本來就是獨立音軌，「誰在說話」由音訊層提供。
（validation-results.md #1：實測尾段延遲 0.34 秒）

上游餵 PCM，下游收到 Utterance，與 replay.load() 產出的是同一種東西，
所以 state / fast_path / slow_path 完全不需要區分資料來自回放還是真實會議。
"""
import asyncio
import audioop
import base64
import json
import re
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import websockets

from .state import Utterance


class Speaking:
    """「某人正在說話」的即時訊號，與已完成的 Utterance 區分。"""

    __slots__ = ("speaker", "since")

    def __init__(self, speaker: str, since: float):
        self.speaker = speaker
        self.since = since


class SpeakingStopped:
    """「這條連線結束了，別再把他算成正在說話」。

    STT 只有在說話者停頓後才 commit；連線在那之前斷掉的話，那句話永遠不會 commit，
    speaking[他] 就永遠清不掉——軟插入從此等不到停頓，只能走升級硬打斷（I4）。
    """

    __slots__ = ("speaker",)

    def __init__(self, speaker: str):
        self.speaker = speaker


import os

DEBUG = bool(os.environ.get("STT_DEBUG"))
SILENCE_COMMIT_SECONDS = 0.6  # 多久沒收到封包視為這句講完
# 一條連線連續失敗幾次才算「這條連線真的斷了」（見 SpeakerStream.is_offline）。
# 2 而不是 1：`_guard` 的退避是 2→4→8…秒，所以 `_fails >= 2` 代表至少兩次
# 獨立的重連嘗試都失敗、時間跨度 ≥2 秒，單次網路抖動不會成立。
OFFLINE_FAILS = 2
WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
TARGET_RATE = 16000
DISCORD_RATE = 48000
DISCORD_CHANNELS = 2


class _STTProbe:
    """T9 取證探針：把每一筆 committed_transcript 的結構化資料寫成一行 JSON。

    只記錄，不判斷、不過濾、不影響 Utterance 產出——見 stt.py 頂端與
    `SpeakerStream` 各呼叫點的註解。多位 speaker 共用同一個檔案，靠 `speaker`
    欄位事後區分（見 `_get_probe()`）。
    """

    def __init__(self, path: Path):
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()


_shared_probe: "_STTProbe | None" = None


def _env_flag_enabled(name: str) -> bool:
    """環境變數旗標判斷：空字串／`"0"`／`"false"`（不分大小寫）視為關閉。

    `bool(os.environ.get(name))` 對 `"0"` 會誤判成 True——字串 "0" 本身是
    truthy，設成 0 想關閉的人反而把它打開了。
    """
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false")


def _get_probe() -> "_STTProbe | None":
    """`MEETING_STT_PROBE=1` 才建立；未設定（或設為 0/false）時回傳 None，
    呼叫端必須整段跳過。

    全程序共用一個檔案（一場會議一個檔），跨 speaker 交錯寫入的行靠各自的
    `speaker` 欄位事後篩選，不用檔案切分。
    """
    global _shared_probe
    if not _env_flag_enabled("MEETING_STT_PROBE"):
        return None
    if _shared_probe is None:
        out_dir = Path("meetings")
        out_dir.mkdir(exist_ok=True)
        _shared_probe = _STTProbe(out_dir / f"stt-probe-{int(time.time())}.jsonl")
    return _shared_probe


def _probe_audio_stats(rms_values: list[int]) -> dict:
    """一段 commit 期間、每包 PCM 的 RMS 分佈。

    事後用來畫「靜音包 vs 語音包」的分界、訂本地能量閘門的閾值。
    docs/validation-results.md 麥克風路徑量過的門檻（0.02）是不同訊號特性
    （麥克風原始訊號 vs. Discord 封包）量出來的，這裡只負責收集分佈，
    不假設同一組常數可以套用。
    """
    if not rms_values:
        return {"packet_count": 0}
    ordered = sorted(rms_values)
    n = len(ordered)

    def pct(p: float) -> int:
        return ordered[min(n - 1, int(p * n))]

    mean = sum(ordered) / n
    return {
        "packet_count": n,
        "rms_max": ordered[-1],
        "rms_mean": mean,
        "rms_p10": pct(0.10),
        "rms_p50": pct(0.50),
        "rms_p90": pct(0.90),
        # 除以 16-bit PCM 滿幅，只為了跟既有量測的量級對照，不代表閾值可以互通
        "rms_norm_max": ordered[-1] / 32768.0,
        "rms_norm_mean": mean / 32768.0,
    }


try:
    import opencc

    _s2tw = opencc.OpenCC("s2twp")
except ImportError:  # pragma: no cover
    _s2tw = None


def to_traditional(text: str) -> str:
    """ElevenLabs Scribe 的中文輸出一律是簡體，`language_code=zho` 也一樣。

    用 OpenCC 的 s2twp 轉台灣正體——`p` 是片語轉換，會一併處理用詞差異
    （軟件→軟體、鼠標→滑鼠、信息→資訊），不是單純的字元對映。
    副作用：專有名詞也會被在地化（黑客松→駭客松）。
    """
    return _s2tw.convert(text) if _s2tw else text


_SUBSTANTIVE_RE = re.compile(r"[^\W_]")  # 任何 Unicode 字母或數字，排除底線與其他標點/符號


def is_substantive(text: str) -> bool:
    """判斷 committed transcript 是否含有實質內容。

    使用者沉默或喘氣時，STT 會 commit 出純標點／省略號的雜訊（如「......」），
    這些不是發言，不該進 utterances：慢路 LLM 會把它們讀成「持續沉默顯示討論
    陷入僵局」而誤判局勢（真實會議實測抓到）。
    判準是「有沒有至少一個 Unicode 字母或數字」——不限半形英數／CJK，全形數字
    （１２３）、全形字母（ＯＫ）、CJK 擴充區（㐀）、帶圈數字（①）都算數；
    語助詞（嗯、哎）本身是中文字，仍是合法發言，不能被一起擋掉。
    """
    return bool(_SUBSTANTIVE_RE.search(text))


def discord_pcm_to_16k_mono(pcm: bytes, state: object | None = None
                            ) -> tuple[bytes, object | None]:
    """Discord 給的是 48kHz 立體聲，ElevenLabs 要 16kHz 單聲道。
    ratecv 需要跨呼叫保留狀態，否則每個 chunk 邊界會有雜音。"""
    mono = audioop.tomono(pcm, 2, 0.5, 0.5)
    out, state = audioop.ratecv(mono, 2, 1, DISCORD_RATE, TARGET_RATE, state)
    return out, state


class SpeakerStream:
    """一位說話者的 STT 連線。"""

    def __init__(self, speaker: str, api_key: str, out: asyncio.Queue,
                 keyterms: list[str] | None = None, t0: float | None = None,
                 probe: "_STTProbe | None" = None):
        self.speaker = speaker
        self.api_key = api_key
        self.out = out
        self.keyterms = keyterms or []
        self.t0 = t0 or time.perf_counter()
        # 有界佇列：STT 斷線期間 Discord 仍在灌音訊，無上限會累積成一大包過期資料
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=250)  # 約 5 秒
        self._resample_state = None
        # 連線健康：這兩個欄位原本只在 run()／_guard 裡用 getattr 存取（建構後、
        # 第一次連線前不存在）。`is_offline` 要在任何時間點都讀得到，所以在這裡
        # 明確初始化——行為與 getattr 的預設值逐字相同，既有路徑不受影響。
        self._fails = 0                 # 連續失敗次數；握手成功或正常結束即歸零
        self._session_started = False   # 這條連線有沒有真的握手成功
        self._speech_start: float | None = None
        self._committed_start: float | None = None
        # T9 取證探針：`probe` 參數只給測試直接注入假探針用；正式路徑一律走
        # `_get_probe()` 讀 MEETING_STT_PROBE。None 時以下欄位不會被用到，
        # 所有讀寫都包在 `if self._probe is not None:` 之後（見 feed/_send/_receive）
        self._probe = probe if probe is not None else _get_probe()
        if self._probe is not None:
            self._probe_lock = threading.Lock()
            self._probe_packet_rms: list[int] = []
            # 這批累積裡第一包／最後一包音訊的實際時間戳——記錄裡直接輸出這兩個
            # 值，不讓讀者得靠 segment_start／segment_end 去猜這批 audio 涵蓋
            # 哪一段（`_committed_start` 會被 `_send()` 的逾時分支覆寫，不是
            # 穩定的錨點，見 `_record_probe` docstring）
            self._probe_audio_window_start: float | None = None
            self._probe_audio_window_end: float | None = None
            self._probe_partial_count = 0
            self._probe_partial_window_start: float | None = None
            self._probe_partial_window_end: float | None = None
            self._probe_pending_forced = 0

    @property
    def is_offline(self) -> bool:
        """這條連線現在是不是「真的連不上」，而不只是在正常重連。

        兩個既有欄位就足夠，不新增任何推論：

        - `_session_started`：`run()` 每次嘗試連線**之前**就先設回 False，所以
          它為 True 只代表「這條 session 現在活著」。
        - `_fails`：`_guard` 只在真正的錯誤路徑累加（連線拋例外、或 close 1000
          發生在 session 建立之前＝認證被拒）。ElevenLabs 靜音 16 秒的閒置關閉
          走的是 `_session_started=True` 的分支，`_fails` 歸零後立刻重連——
          所以正常停頓造成的短暫斷線在這裡恆為 False（見 `_guard` docstring）。

        額度耗盡（2026-08-31 那場事故）走的是握手 HTTP 401：`websockets.connect`
        拋例外 → `except Exception` → `_fails` 每次重連再加一，同時
        `_session_started` 停在 False。兩個條件同時成立，正是這個 property。
        """
        return self._fails >= OFFLINE_FAILS and not self._session_started

    def feed(self, pcm_48k_stereo: bytes) -> None:
        """從 Discord 的 sink callback 呼叫（不同執行緒，故用 put_nowait）。"""
        pcm, self._resample_state = discord_pcm_to_16k_mono(
            pcm_48k_stereo, self._resample_state)
        if self._probe is not None:
            # audioop.rms 是 C 實作，不逐 sample 跑 Python 迴圈；feed() 從 Discord
            # sink callback 的另一條執行緒呼叫，用 lock 保護這個極短的 append
            rms = audioop.rms(pcm, 2)
            t = time.perf_counter() - self.t0
            with self._probe_lock:
                self._probe_packet_rms.append(rms)
                if self._probe_audio_window_start is None:
                    self._probe_audio_window_start = t
                self._probe_audio_window_end = t
        try:
            self._audio.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # 寧可丟掉過期音訊，也不要送出一大包遲到的內容

    async def run(self) -> None:
        params = [
            "model_id=scribe_v2_realtime",
            f"audio_format=pcm_{TARGET_RATE}",
            "language_code=zho",
            "include_timestamps=true",
            "commit_strategy=vad",
        ]
        # 會前設定檔的人名與專有名詞餵進來，中英夾雜的辨識率差很多
        # （validation-results.md #1：「OS」不給提示會被聽成「always」）
        params += [f"keyterms={quote(k)}" for k in self.keyterms]
        url = f"{WS_URL}?{'&'.join(params)}"

        # 重連時必須重置：舊的 _speech_start 若殘留，會讓 _send 以為還在同一句話，
        # 於是既不 commit 也不重新計時 → 症狀是「封包一直進來但永遠不出字」
        self._speech_start = None
        self._committed_start = None
        self._resample_state = None
        self._session_started = False  # 收到 session_started 才算真的連上
        if self._probe is not None:
            # 跟上面同理：舊連線的探針累加器不重置，重連後第一筆記錄會帶著
            # 已死連線的殘留計數，commit_trigger 也可能被殘留的
            # _probe_pending_forced 誤標
            with self._probe_lock:
                self._probe_packet_rms = []
                self._probe_audio_window_start = None
                self._probe_audio_window_end = None
            self._probe_partial_count = 0
            self._probe_partial_window_start = None
            self._probe_partial_window_end = None
            self._probe_pending_forced = 0
        async with websockets.connect(
                url, additional_headers={"xi-api-key": self.api_key}) as ws:
            await asyncio.gather(self._send(ws), self._receive(ws))

    async def _send(self, ws) -> None:
        while True:
            try:
                pcm = await asyncio.wait_for(self._audio.get(),
                                             timeout=SILENCE_COMMIT_SECONDS)
            except asyncio.TimeoutError:
                # Discord 在人停止說話時直接停止送封包，伺服器端 VAD 看不到靜音，
                # 不會自己 commit。這裡代替它判斷「這句講完了」。
                if self._speech_start is not None:
                    await ws.send(json.dumps({
                        "message_type": "input_audio_chunk",
                        "audio_base_64": "",
                        "commit": True,
                        "sample_rate": TARGET_RATE,
                    }))
                    # 立刻交棒，不能等 _receive 來重置——若這次 commit 沒有回轉錄
                    # （例如只收到雜音），就會每次逾時都重送 commit 而被伺服器限流踢掉
                    self._committed_start = self._speech_start
                    self._speech_start = None
                    if self._probe is not None:
                        # 記一筆「我方剛送出強制 commit」，供 _receive 判斷
                        # commit_trigger。這是計數不是佇列——見 _record_probe
                        # 對這個簡化的已知限制說明
                        self._probe_pending_forced += 1
                continue

            if self._speech_start is None:
                self._speech_start = time.perf_counter() - self.t0
            await ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(pcm).decode(),
                "commit": False,
                "sample_rate": TARGET_RATE,
            }))

    async def _receive(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if DEBUG:
                print(f"    ← {str(msg)[:180]}")
            mt = msg.get("message_type")
            if mt == "session_started":
                self._session_started = True
                self._fails = 0  # 握手成功，先前的退避計數不該延續到這條 session
                continue
            if mt == "partial_transcript":
                if self._probe is not None:
                    t = time.perf_counter() - self.t0
                    self._probe_partial_count += 1
                    if self._probe_partial_window_start is None:
                        self._probe_partial_window_start = t
                    self._probe_partial_window_end = t
                # 送出「正在說話」訊號——超時規則靠這個，不能等 commit
                if self._speech_start is not None:
                    await self.out.put(Speaking(self.speaker, self._speech_start))
                continue
            if mt != "committed_transcript":
                continue
            text = to_traditional(msg.get("text", "").strip())
            if self._probe is not None:
                # 在丟棄判斷之前記錄——丟棄的 commit 正是要收的資料，不能只記錄
                # 有進 Utterance 的那些，否則 is_substantive／emitted 兩欄位就沒意義
                self._record_probe(text)
            # 空字串與純標點／省略號雜訊都不是發言，丟棄路徑與空字串一致（不重置
            # _committed_start／_speech_start）——沿用既有行為，不在此改動 speaking 狀態機
            if not text or not is_substantive(text):
                continue
            end = time.perf_counter() - self.t0
            start = self._committed_start
            if start is None:
                start = self._speech_start if self._speech_start is not None else end
            await self.out.put(Utterance(self.speaker, text, start, end))
            self._committed_start = None
            self._speech_start = None

    def _record_probe(self, text: str) -> None:
        """T9 取證：記錄這一筆 committed_transcript 的結構化資料。

        純記錄，不改動任何狀態（`_committed_start`／`_speech_start`／
        `_audio` 佇列都不碰）、不影響 Utterance 產出——呼叫者已用
        `self._probe is not None` 把整段擋在探針關閉時的路徑之外。

        `end`／`start` 在此獨立算一次（跟下面 emit Utterance 用的是同一個公式），
        不共用同一次呼叫結果：探針關閉時完全不執行這段，兩邊互不依賴，
        才能保證探針開關不改變 Utterance 的 start/end。

        commit_trigger 的判定：`_send()` 逾時送出強制 commit 時會把
        `_probe_pending_forced` 加一，這裡消費它（跟這筆是否實質內容無關——
        只要有 committed_transcript 回來，就對應到一次真實的 commit 往返）。
        這是「計數」不是「配對佇列」——已知限制：若同一使用者在還沒收到
        上一次強制 commit 的回覆前，又觸發第二次強制 commit，兩者無法保證
        與各自的 committed_transcript 一一對應；若某次強制 commit 從未得到
        回覆（例如整段被判為雜音，伺服器完全不回 committed_transcript），
        這個計數會多算，讓後面一筆其實是 server 觸發的 committed_transcript
        被誤標成 client_timeout。真實會議此類重疊在短暫的
        SILENCE_COMMIT_SECONDS 視窗內才會發生，資料量少，能否忽略留給讀
        資料的人自行判斷，這裡如實記錄，不假裝精準。

        累加器（`_probe_packet_rms`／`_probe_partial_count`）只在這筆 commit
        真的結束一個段落時才清空——判準跟下面 `_receive()` 決定要不要重置
        `_committed_start`／`_speech_start` 用同一個（`is_substantive(text)`）。
        丟棄路徑（雜訊 commit）刻意不清空：那正是既有邏輯「不打斷累積」的
        地方，探針的生命週期必須跟著段落走（真實會議出現過的「......」雜訊
        commit 就是這個情境）。

        重要：`segment_start`／`segment_end`（來自 `_committed_start`／
        `_speech_start`）**不是**這批累積 audio／partial 的可靠邊界。
        `_committed_start` 有兩個獨立寫入端——這裡（emit 路徑）跟 `_send()`
        逾時分支——後者每次逾時都會用當下的 `_speech_start` 覆寫它，不管
        上一筆 commit 是不是雜訊。於是「雜訊 commit → 新音訊 → 再次逾時」
        這個真實情境會讓 `_committed_start` 指向**後段**音訊的起點，但累加器
        （因為雜訊 commit 沒清空）其實還留著**前段**音訊。用 `segment_start`
        描述 audio／partial 涵蓋範圍在這裡一定會錯。
        因此 audio／partial 各自的涵蓋區間改成直接記錄「這批累積裡第一包
        （或第一次 partial）／最後一包（或最後一次）」的實際時間戳
        （`audio.window_start`／`window_end`、`partial_window_start`／
        `partial_window_end`），不依賴、也不強求等於 `segment_start`／
        `segment_end`——讀資料的人看這幾個欄位本身就能確定涵蓋哪一段，
        不需要推理狀態機怎麼跳。
        """
        end = time.perf_counter() - self.t0
        start = self._committed_start
        if start is None:
            start = self._speech_start if self._speech_start is not None else end
        segment_ended = is_substantive(text)
        if segment_ended:
            with self._probe_lock:
                packets = self._probe_packet_rms
                audio_window_start = self._probe_audio_window_start
                audio_window_end = self._probe_audio_window_end
                self._probe_packet_rms = []
                self._probe_audio_window_start = None
                self._probe_audio_window_end = None
            partial_count = self._probe_partial_count
            partial_window_start = self._probe_partial_window_start
            partial_window_end = self._probe_partial_window_end
            self._probe_partial_count = 0
            self._probe_partial_window_start = None
            self._probe_partial_window_end = None
        else:
            # 段落還沒結束，只拍照（copy）不清空——下一筆還要繼續累積
            with self._probe_lock:
                packets = list(self._probe_packet_rms)
                audio_window_start = self._probe_audio_window_start
                audio_window_end = self._probe_audio_window_end
            partial_count = self._probe_partial_count
            partial_window_start = self._probe_partial_window_start
            partial_window_end = self._probe_partial_window_end
        if self._probe_pending_forced > 0:
            self._probe_pending_forced -= 1
            trigger = "client_timeout"
        else:
            trigger = "server"
        audio_stats = _probe_audio_stats(packets)
        audio_stats["window_start"] = audio_window_start
        audio_stats["window_end"] = audio_window_end
        self._probe.write({
            "speaker": self.speaker,
            "segment_start": start,
            "segment_end": end,
            "duration": end - start,
            "partial_count": partial_count,
            "partial_window_start": partial_window_start,
            "partial_window_end": partial_window_end,
            "commit_trigger": trigger,
            "text": text,
            "is_substantive": segment_ended,
            "emitted": segment_ended,
            "segment_ended": segment_ended,
            "audio": audio_stats,
        })


class STTPool:
    """多位說話者。每人一條連線，輸出匯流成單一 Utterance 串流。"""

    def __init__(self, api_key: str, keyterms: list[str] | None = None):
        self.api_key = api_key
        self.keyterms = keyterms
        self.streams: dict[str, SpeakerStream] = {}
        self.out: asyncio.Queue[Utterance] = asyncio.Queue()
        self.t0 = time.perf_counter()
        self._tasks: list[asyncio.Task] = []

    def ensure(self, speaker: str) -> SpeakerStream:
        if speaker not in self.streams:
            s = SpeakerStream(speaker, self.api_key, self.out, self.keyterms, self.t0)
            self.streams[speaker] = s
            task = asyncio.create_task(self._guard(s))
            self._tasks.append(task)
        return self.streams[speaker]

    @staticmethod
    async def _guard(stream: "SpeakerStream") -> None:
        """背景 task 若拋例外會被靜默吞掉，包一層把它印出來。

        ElevenLabs 在靜音約 16 秒後會主動關連線（close 1000，不帶任何錯誤訊息），
        這是正常行為，每次會議停頓都會發生。它不能走錯誤退避——
        run() 永遠不會正常返回，所以 _fails 永遠不歸零，
        退避會隨停頓次數翻倍 2→4→8→16→30 秒，超過 5 秒的音訊佇列後就真的掉字。
        閒置關閉 → 立即重連（握手實測 0.2 秒），真正的錯誤才退避。
        """
        while True:
            try:
                try:
                    await stream.run()
                finally:
                    # 不論正常結束、閒置關閉還是拋錯，都要在重連前清掉 speaking：
                    # 這條連線不會再 commit 那句話了（I4）
                    await STTPool._on_stream_ended(stream)
            except websockets.ConnectionClosedOK as e:
                # 認證失敗也是 close 1000，但發生在 session_started 之前、不帶任何訊息。
                # 只有「曾經真的連上」的關閉才是閒置關閉，否則照錯誤退避，避免緊迫重連
                if getattr(stream, "_session_started", False):
                    stream._fails = 0
                    continue
                delay = min(2 * 2 ** getattr(stream, "_fails", 0), 30)
                stream._fails = getattr(stream, "_fails", 0) + 1
                print(f"    ⚠️ {stream.speaker} STT 連線被拒（session 未建立），{delay}s 後重連")
                await asyncio.sleep(delay)
            except Exception as e:  # noqa: BLE001
                delay = min(2 * 2 ** getattr(stream, "_fails", 0), 30)
                stream._fails = getattr(stream, "_fails", 0) + 1
                print(f"    ⚠️ {stream.speaker} STT 斷線（{type(e).__name__}），{delay}s 後重連")
                await asyncio.sleep(delay)
            else:
                stream._fails = 0

    @staticmethod
    async def _on_stream_ended(stream: "SpeakerStream") -> None:
        """連線結束（含閒置關閉）→ 通知下游把這個人移出「正在說話」。"""
        await stream.out.put(SpeakingStopped(stream.speaker))

    def offline(self) -> bool:
        """整個池子的耳朵是不是斷了——**已建立的每一條連線**都連不上才算。

        為什麼要求「全部」而不是「任一」：一個人的連線出問題（他自己的網路、
        或某條 session 剛好卡住）不代表主席聾了，其他人的話照樣聽得到，這時候
        壓住規則是誤鎖。額度耗盡／金鑰失效那種真正的失聯，本來就會讓每一條
        連線同時失敗——那場事故就是這個形狀。

        還沒有任何連線（會議剛開始、沒有人出過聲）回 False：沒有證據就不下
        判斷，跟 `HearingMonitor` 另一條臂（有人出聲卻沒有逐字稿）的原則一致。

        `streams` 只增不減，離開頻道的人留在裡面也不影響：沒有音訊餵進去的
        連線走的是閒置關閉→立即重連，`is_offline` 恆為 False。
        """
        return bool(self.streams) and all(s.is_offline for s in self.streams.values())

    def feed(self, speaker: str, pcm_48k_stereo: bytes) -> None:
        self.ensure(speaker).feed(pcm_48k_stereo)

    async def utterances(self) -> AsyncIterator[Utterance]:
        while True:
            yield await self.out.get()
