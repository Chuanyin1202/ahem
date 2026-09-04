"""語音輸出：主席怎麼開口。

四個單位（spec §1）：
- Output  ：長存活的 discord.AudioSource，取代 _Silence。閒置送靜音撐開 RTP 雙向通道
- Earcon  ：預生成的提示音
- Voice   ：文字 → 48k 立體聲 PCM 串流（ElevenLabs）
- Chair   ：pending/playing 兩槽狀態機，決定說什麼、怎麼說、何時說
"""
import asyncio
import dataclasses
import fcntl
import html
import json
import logging
import os
import queue
import re
import time
import wave
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import discord

from .audio import DISCORD_RATE, FRAME_BYTES, SAMPLE_WIDTH, Framer, Upsampler

_EOS = object()  # 一句講完的哨兵
SILENCE_FRAME = b"\x00" * FRAME_BYTES
MAX_QUEUED_FRAMES = 1500  # ≈ 30 秒
EARCON_PATH = Path(__file__).parent.parent.parent / "assets" / "earcon.wav"

PAUSE_SECONDS = 1.0       # 軟插入：沒人講多久算停頓
ESCALATE_SECONDS = 15.0   # 軟插入等不到停頓 → 升級硬打斷
EARCON_GATE = 0.7         # 硬打斷：提示音後最早開口
PREBUFFER_SECONDS = 0.2   # 開口前至少累積的語音
FAIL_BACKOFF = 30.0       # TTS 失敗後同類型退避
PLAYING_MAX_SECONDS = 45.0  # 播放執行緒完全沒消費（player 死了）的逾時上限
TICK = 0.1

VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah：verified zh、成熟穩定。之後要換只改這裡
TTS_MODEL = "eleven_v3_conversational"  # 為對話代理的自然對話最佳化，中文聽感明顯較佳（使用者實聽選定）
# 換自 eleven_flash_v2_5。當初選 flash 是為了延遲，但那個理由不成立——同音色同句實測
# 首位元組 0.23s vs flash 的 0.19s，差 40ms 聽不出來；總生成 1.43s 產出 9.4s 語音
# （即時的 6.6 倍），串流播放不會追不上。v3 不支援 SSML <break>，但主席話術全是純文字，
# 沒有影響（已 grep 確認）。量測見 docs/validation-results.md「主席聲音與模型」。
TTS_RATE = 24000
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=pcm_{rate}"

AZURE_TTS_VOICE = "zh-TW-HsiaoChenNeural"
AZURE_TTS_VOICES = {
    "female": "zh-TW-HsiaoChenNeural",
    "male": "zh-TW-YunJheNeural",
}
AZURE_TTS_RATE = "+12%"
AZURE_TTS_MONTHLY_LIMIT = 500_000
AZURE_TTS_HARD_STOP_PERCENT = 95
AZURE_TTS_WARNING_PERCENTS = (80, 90, 95)
AZURE_TTS_USAGE_FILE = Path("meetings/azure_tts_usage.json")
_AZURE_REGION_RE = re.compile(r"^[a-z0-9-]+$")
_AZURE_VOICE_RE = re.compile(r"^[A-Za-z0-9:-]+$")
_AZURE_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")


class VoiceError(RuntimeError):
    """開口失敗（TTS 逾時／HTTP 錯誤／播放佇列滿）。Chair 只處理這一種例外。"""


class AzureUsageBudget:
    """本機保守配額閘門：在送出 Azure 請求前先記帳，跨程序以 flock 同步。"""

    def __init__(self, path: Path = AZURE_TTS_USAGE_FILE, *,
                 monthly_limit: int = AZURE_TTS_MONTHLY_LIMIT,
                 hard_stop_percent: int = AZURE_TTS_HARD_STOP_PERCENT,
                 warning_percents: tuple[int, ...] = AZURE_TTS_WARNING_PERCENTS):
        if monthly_limit <= 0:
            raise ValueError("AZURE_TTS_MONTHLY_LIMIT 必須大於 0")
        if not 1 <= hard_stop_percent <= 100:
            raise ValueError("AZURE_TTS_HARD_STOP_PERCENT 必須介於 1 到 100")
        if any(not 1 <= value <= hard_stop_percent for value in warning_percents):
            raise ValueError("AZURE_TTS_WARNING_PERCENTS 必須介於 1 到硬停百分比")
        self.path = path
        self.monthly_limit = monthly_limit
        self.hard_stop_percent = hard_stop_percent
        self.warning_percents = tuple(sorted(set(warning_percents)))

    @property
    def hard_limit(self) -> int:
        return self.monthly_limit * self.hard_stop_percent // 100

    def reserve(self, text: str) -> int:
        """預留本次字元；失敗也不回補，以免低估 Azure 實際計量。"""
        month = time.strftime("%Y-%m", time.gmtime())
        amount = len(text)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(self.path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    state = {}
                if state.get("month") != month:
                    state = {"month": month, "characters": 0, "warned": []}
                used = int(state.get("characters", 0))
                projected = used + amount
                if projected > self.hard_limit:
                    raise VoiceError(
                        "Azure TTS 已達本機免費額度安全上限 "
                        f"({used:,}/{self.hard_limit:,} 字元，本月上限的 "
                        f"{self.hard_stop_percent}%)；已阻止本次請求")
                warned = {int(value) for value in state.get("warned", [])}
                percent = projected * 100 / self.monthly_limit
                for threshold in self.warning_percents:
                    if percent >= threshold and threshold not in warned:
                        logging.warning(
                            "Azure TTS 免費額度提醒：本月已使用約 %s/%s 字元 (%.1f%%)",
                            f"{projected:,}", f"{self.monthly_limit:,}", percent)
                        warned.add(threshold)
                state.update(characters=projected, warned=sorted(warned))
                temp = self.path.with_suffix(self.path.suffix + ".tmp")
                temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
                temp.replace(self.path)
                return projected
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Output(discord.AudioSource):
    """佇列有東西就播，沒有就送靜音。read() 由 discord 播放執行緒每 20ms 呼叫，
    enqueue() 由 asyncio 側呼叫——跨執行緒只靠這一個 queue.Queue。

    ⚠️ read() 永遠不能回空 bytes：對 discord.py 那代表「播放結束」，播放器會停掉。
    """

    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=MAX_QUEUED_FRAMES)
        self._framer = Framer()
        self._producing = False  # enqueue 過、還沒看到 EOS
        self.first_audible_at: float | None = None

    # ── asyncio 側 ──
    def enqueue(self, pcm: bytes) -> None:
        self._producing = True
        try:
            self._q.put_nowait(pcm)  # 不能阻塞 event loop
        except queue.Full as e:
            # 30 秒沒被消費＝播放器死了。丟給 Chair 當失敗處理，不要卡住整個 asyncio
            raise VoiceError("播放佇列已滿，播放器沒在消費") from e

    def end_of_utterance(self) -> None:
        try:
            self._q.put_nowait(_EOS)
        except queue.Full:
            # 佇列滿代表播放器死了、沒人在消費——阻塞 put() 會卡死 event loop（見 speaker Chair F3）。
            # 沒人會再讀走這些幀，直接視為「這句話已經結束」。
            self._producing = False

    def is_busy(self) -> bool:
        # len(_framer) 只在 >= FRAME_BYTES 才算忙：EOS 走 Full 分支被丟掉時，
        # framer 裡不足一幀的尾段永遠沒有 EOS 觸發 flush() 把它清出來——
        # 那段音訊已經連同 EOS 一起遺失了，不能讓它讓 is_busy() 卡成永遠 True（R4）。
        return self._producing or not self._q.empty() or len(self._framer) >= FRAME_BYTES

    def reset_marker(self) -> None:
        self.first_audible_at = None

    # ── 播放執行緒側 ──
    def read(self) -> bytes:
        frame = self._framer.pop()
        while frame is None:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                return SILENCE_FRAME  # 閒置或 underflow：都送靜音
            if item is _EOS:
                self._producing = False
                frame = self._framer.flush()
                if frame is None:
                    continue
                break
            self._framer.push(item)
            frame = self._framer.pop()
        if self.first_audible_at is None and frame != SILENCE_FRAME:
            self.first_audible_at = time.perf_counter()
        return frame

    def is_opus(self) -> bool:
        return False


class Earcon:
    """主席的提示音。啟動時載入並驗格式——「本地檔不會失敗」是錯的，缺檔、壞檔都要 fail fast。"""

    def __init__(self, path: Path = EARCON_PATH):
        try:
            with wave.open(str(path), "rb") as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (2, SAMPLE_WIDTH, DISCORD_RATE):
                    raise ValueError(
                        f"earcon 必須是 48kHz 16-bit 立體聲，實際 "
                        f"{w.getframerate()}Hz/{w.getsampwidth() * 8}-bit/{w.getnchannels()}ch：{path}")
                pcm = w.readframes(w.getnframes())
        except (OSError, wave.Error) as e:
            raise ValueError(f"讀不到 earcon：{path}（{e}）") from e
        pad = (-len(pcm)) % FRAME_BYTES
        self.pcm = pcm + b"\x00" * pad
        self.seconds = len(pcm) / (DISCORD_RATE * SAMPLE_WIDTH * 2)


class Voice:
    """文字 → 48k 立體聲 PCM 串流。逾時是必要的：TTS 卡住時主席不能無限期沉默。"""

    def __init__(self, api_key: str, voice_id: str = VOICE_ID, *,
                 first_byte_timeout: float = 3.0, total_timeout: float = 15.0):
        self.api_key = api_key
        self.voice_id = voice_id
        self.first_byte_timeout = first_byte_timeout
        self.total_timeout = total_timeout

    async def _raw_stream(self, text: str) -> AsyncIterator[bytes]:
        """ElevenLabs HTTP stream：raw s16le mono @ TTS_RATE。測試以假的覆蓋。"""
        url = TTS_URL.format(voice_id=self.voice_id, rate=TTS_RATE)
        body = {"text": text, "model_id": TTS_MODEL, "language_code": "zh"}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=body, headers={"xi-api-key": self.api_key}) as r:
                if r.status != 200:
                    raise VoiceError(f"TTS HTTP {r.status}: {(await r.text())[:200]}")
                async for chunk in r.content.iter_chunked(4096):
                    yield chunk

    async def synth(self, text: str) -> AsyncIterator[bytes]:
        up = Upsampler(TTS_RATE)
        t0 = time.perf_counter()
        it = self._raw_stream(text).__aiter__()
        first = True
        while True:
            budget = self.first_byte_timeout if first else max(0.0, self.total_timeout - (time.perf_counter() - t0))
            try:
                chunk = await asyncio.wait_for(it.__anext__(), timeout=budget)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as e:
                raise VoiceError("TTS 首位元組逾時" if first else "TTS 總時長逾時") from e
            first = False
            pcm = up.feed(chunk)
            if pcm:
                yield pcm


def azure_spoken_text(text: str) -> str:
    """顯示文字不動，只改送進 Azure 的口說形式。

    這份對照來自 zh-TW-HsiaoChenNeural 的實聽確認：API 要連續念成
    「誒批哀」，「收斂」的「斂」要固定為台灣華語四聲。替換只發生在
    TTS 邊界，事件、逐字稿與會議記錄仍保留原字。
    """
    text = re.sub(r"(?<![A-Za-z])API(?![A-Za-z])", "誒批哀", text,
                  flags=re.IGNORECASE)
    return text.replace("收斂", "收練")


class AzureVoice(Voice):
    """Azure Speech zh-TW：raw s16le mono @ 24kHz，再沿用既有 Discord 轉換。"""

    def __init__(self, api_key: str, *, region: str,
                 voice_id: str = AZURE_TTS_VOICE, rate: str = AZURE_TTS_RATE,
                 usage_budget: AzureUsageBudget | None = None,
                 first_byte_timeout: float = 3.0, total_timeout: float = 15.0):
        if not _AZURE_REGION_RE.fullmatch(region):
            raise ValueError("AZURE_SPEECH_REGION 格式不正確")
        if not _AZURE_VOICE_RE.fullmatch(voice_id):
            raise ValueError("AZURE_TTS_VOICE 格式不正確")
        if not _AZURE_RATE_RE.fullmatch(rate):
            raise ValueError("AZURE_TTS_RATE 必須是例如 +12% 或 -5%")
        super().__init__(api_key, voice_id, first_byte_timeout=first_byte_timeout,
                         total_timeout=total_timeout)
        self.region = region
        self.rate = rate
        self.usage_budget = usage_budget or AzureUsageBudget()

    async def _raw_stream(self, text: str) -> AsyncIterator[bytes]:
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        spoken_text = azure_spoken_text(text)
        self.usage_budget.reserve(spoken_text)
        spoken = html.escape(spoken_text, quote=False)
        ssml = (
            "<speak version='1.0' xml:lang='zh-TW'>"
            f"<voice name='{self.voice_id}'><prosody rate='{self.rate}'>"
            f"{spoken}</prosody></voice></speak>"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm",
            "User-Agent": "ahem-meeting-chair",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=ssml.encode("utf-8"), headers=headers) as response:
                if response.status != 200:
                    detail = (await response.text())[:200]
                    raise VoiceError(f"Azure TTS HTTP {response.status}: {detail}")
                async for chunk in response.content.iter_chunked(4096):
                    yield chunk


def build_voice(environ: Mapping[str, str] | None = None) -> Voice:
    """依環境變數建立主席 TTS；預設行為保持 ElevenLabs 不變。"""
    env = os.environ if environ is None else environ
    provider = env.get("AHEM_TTS_PROVIDER", "elevenlabs").strip().lower()
    if provider == "elevenlabs":
        return Voice(env["ELEVENLABS_API_KEY"])
    if provider == "azure":
        gender = env.get("AZURE_TTS_GENDER", "female").strip().lower()
        if gender not in AZURE_TTS_VOICES:
            raise ValueError("AZURE_TTS_GENDER 只支援 female 或 male")
        voice_id = env.get("AZURE_TTS_VOICE", "").strip() or AZURE_TTS_VOICES[gender]
        warning_percents = tuple(
            int(value.strip())
            for value in env.get("AZURE_TTS_WARNING_PERCENTS", "80,90,95").split(",")
            if value.strip()
        )
        usage_budget = AzureUsageBudget(
            Path(env.get("AZURE_TTS_USAGE_FILE", str(AZURE_TTS_USAGE_FILE))),
            monthly_limit=int(env.get("AZURE_TTS_MONTHLY_LIMIT", AZURE_TTS_MONTHLY_LIMIT)),
            hard_stop_percent=int(env.get(
                "AZURE_TTS_HARD_STOP_PERCENT", AZURE_TTS_HARD_STOP_PERCENT)),
            warning_percents=warning_percents,
        )
        return AzureVoice(
            env["AZURE_SPEECH_KEY"],
            region=env["AZURE_SPEECH_REGION"],
            voice_id=voice_id,
            rate=env.get("AZURE_TTS_RATE", AZURE_TTS_RATE),
            usage_budget=usage_budget,
        )
    raise ValueError("AHEM_TTS_PROVIDER 只支援 elevenlabs 或 azure")


@dataclass
class Intervention:
    kind: str
    target: str | None
    text: str
    hard: bool
    revision: int   # 呼叫端的世界版本；開口前不符即作廢
    created_at: float


class Chair:
    """兩槽狀態機：pending（等待中）、playing（播放中，最多留一個更高優先候選）。

    只有真的出聲（第一個可聽幀）才算介入——on_spoken 在那一刻回呼，
    live.py 用它寫 interventions；claimed（防重送）由呼叫端自己記。

    ⚠️ clock 必須與 state.voice_* 同座標（裸 perf_counter），不得傳會議相對時間，否則 silent_for 永遠是 0。
    """

    def __init__(self, state, output: Output, voice: Voice, earcon: Earcon, *,
                 clock: Callable[[], float] = time.perf_counter,
                 revision: Callable[[], int] = lambda: 0,
                 on_spoken: Callable | None = None, on_failed: Callable | None = None,
                 on_escalate: Callable[["Intervention"], "Intervention | None"] | None = None,
                 on_dropped: Callable[["Intervention", str], None] | None = None):
        self.state, self.output, self.voice, self.earcon = state, output, voice, earcon
        self.clock, self.revision = clock, revision
        self.on_spoken = on_spoken or (lambda iv, at: None)
        self.on_failed = on_failed or (lambda iv, reason: None)
        self.on_escalate = on_escalate or (lambda iv: iv)  # 預設：升級時文字原封不動重播
        self.on_dropped = on_dropped or (lambda iv, reason: None)
        self.pending: Intervention | None = None
        self.playing: Intervention | None = None
        self.candidate: Intervention | None = None
        self.escalated = 0
        self._pending_since: float | None = None
        self._playing_since: float | None = None
        self._spoken_reported = False  # 本次 playing 是否已經回報過 on_spoken
        self._failed_reported = False  # 本次 playing 是否已經回報過 on_failed
        self._backoff_until: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._gen = 0  # 每次 _start 遞增；_speak 靠這個世代號判斷自己是不是還算數（R2）

    # ── 呼叫端 ──
    def replace_output(self, new: Output) -> None:
        """播放器重建 → 把主席接到新的 Output 上。

        交接前要先把「已經出聲」這件事補報掉：舊 Output 可能已經播出第一個可聽幀、
        但 tick 還沒觀察到，marker 隨舊物件消失後，下一 tick 會誤判「沒有任何音訊出聲」
        → 報失敗、呼叫端釋放 claim，同一件事稍後又被提醒一次（R2）。

        可能由 discord 的播放執行緒（`vc.play(after=...)` 回呼）呼叫：
        只做屬性讀寫與最多一次 on_spoken 回呼。
        """
        if (self.playing is not None and not self._spoken_reported
                and self.output.first_audible_at is not None):
            self.on_spoken(self.playing, self.output.first_audible_at)
            self._spoken_reported = True
        self.output = new

    @staticmethod
    def _backoff_key(iv: Intervention) -> str:
        """退避 key 帶 hard/soft：兩者互不吃。

        規則型硬打斷（發言超時等）不能被同 kind 的舊 soft 失敗擋掉；但 hard 自己失敗後
        也要退避同 kind 的 hard——不然播放器死掉期間，快路每秒重送一次，提示音一秒響一次（R3）。
        """
        return f"{'hard' if iv.hard else 'soft'}:{iv.kind.strip()}"

    def request(self, iv: Intervention) -> bool:
        now = self.clock()
        if self._backoff_until.get(self._backoff_key(iv), 0.0) > now:
            return False
        if self.playing is not None:
            if iv.hard and not self.playing.hard and (self.candidate is None or not self.candidate.hard):
                self.candidate = iv  # 播完重驗再播
                return True
            return False  # 同級以下：5 秒後的世界已不同，丟掉
        if self.pending is not None:
            if iv.hard and not self.pending.hard:
                # 被取代的 soft 要通知呼叫端，否則它記的 claimed 沒人清，
                # 那個觸發從此不會再排入（I1b）
                old = self.pending
                self.pending, self._pending_since = iv, now
                self.on_dropped(old, "被硬打斷取代")
                return True
            return False
        self.pending, self._pending_since = iv, now
        return True

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(TICK)

    # ── 狀態機 ──
    async def tick(self) -> None:
        now = self.clock()
        if self.playing is not None:
            iv = self.playing
            # 「真的出聲」只看播放執行緒實際讀到的第一個可聽幀——跟 _speak 內部
            # synth 有沒有跑完是兩回事：半句失敗也可能已經出聲，0 chunk 也可能從沒出聲。
            if not self._spoken_reported and self.output.first_audible_at is not None:
                self.on_spoken(iv, self.output.first_audible_at)
                self._spoken_reported = True
            if now - self._playing_since > PLAYING_MAX_SECONDS:
                # 播放執行緒完全沒消費（player 死了）：_speak 可能還卡在 await，強制收尾
                if self._task is not None and not self._task.done():
                    self._task.cancel()
                if not self._failed_reported:
                    self.on_failed(iv, "播放逾時")
                    self._failed_reported = True
                # R1：逾時代表播放器沒在消費，誰都播不了——不交接。candidate 直接作廢，
                # 不能移到 pending：舊 frame／EOS 還留在共用 Output 裡，candidate 重設
                # marker 後播放執行緒讀到的舊資料會讓它被誤記一次 on_spoken。
                if self.candidate is not None:
                    self.on_dropped(self.candidate, "播放器逾時，候選作廢")
                    self.candidate = None
                self.playing = None
                return
            if self._task is None or self._task.done():
                if not self.output.is_busy():
                    if not self._spoken_reported and not self._failed_reported:
                        self.on_failed(iv, "沒有任何音訊出聲")  # 0 chunk：synth 完成但沒吐半個 frame
                        self._failed_reported = True
                    self.playing = None
                    if self.candidate is not None:
                        self.pending, self._pending_since = self.candidate, now
                        self.candidate = None
            return
        iv = self.pending
        if iv is None:
            return
        if iv.revision != self.revision():
            self.pending = None  # 世界變了：發言者換人、目標開口、慢路重評
            self.on_dropped(iv, "revision 過期")
            return
        waited = now - self._pending_since
        if iv.hard:
            self._start(iv, hard=True)
        elif self.state.silent_for(now) >= PAUSE_SECONDS and not self.state.speaking:
            self._start(iv, hard=False)
        elif waited >= ESCALATE_SECONDS:
            self.escalated += 1
            fresh = self.on_escalate(iv)  # spec §2：升級要用當下事實重生文字，不是 15 秒前的舊句
            if fresh is None:
                self.pending = None
                self.on_dropped(iv, "升級時已不成立")
            else:
                # R5：呼叫端重生的 fresh 可能忘記把 hard 設回 True（狀態機會誤當 soft
                # 處理，另一個 hard 還會被收成 candidate），也可能沒重驗 revision——
                # 兩件事都不能信任呼叫端，這裡強制修正。
                fresh = dataclasses.replace(fresh, hard=True)
                if fresh.revision != self.revision():
                    self.pending = None
                    self.on_dropped(fresh, "升級重生的介入 revision 已過期")
                else:
                    self._start(fresh, hard=True)

    def _start(self, iv: Intervention, *, hard: bool) -> None:
        self.pending, self.playing = None, iv
        self.output.reset_marker()
        self._spoken_reported = False
        self._failed_reported = False
        self._playing_since = self.clock()
        self._gen += 1  # 新的一代——舊 task（若還沒收尾，例如被 cancel 後恢復）不再算數
        self._task = asyncio.get_running_loop().create_task(self._speak(iv, hard, self._gen))

    async def _speak(self, iv: Intervention, hard: bool, gen: int) -> None:
        t0 = self.clock()
        frames: list[bytes] = []
        prebuffered = False
        try:
            if hard:
                # R3：搬進 try——Output 滿時 enqueue() 會拋 VoiceError，搬進來才能
                # 讓 except／finally 接住，不然 task 直接異常結束、沒 EOS、playing 卡死
                self.output.enqueue(self.earcon.pcm)  # 提示音立即出去，TTS 同時開跑
            async for pcm in self.voice.synth(iv.text):
                # T6b：prebuffer 達標後不能再 append 進 frames——不然這個 chunk 會
                # 同時被下面的 enqueue(pcm) 送一次，又被迴圈結束後的尾段
                # `for f in frames: enqueue(f)` 再送一次，句子整段（少前 200ms）重播。
                if prebuffered:
                    self.output.enqueue(pcm)
                    continue
                frames.append(pcm)
                if sum(map(len, frames)) < PREBUFFER_SECONDS * FRAME_BYTES * 50:
                    continue
                if hard:
                    gap = EARCON_GATE - (self.clock() - t0)
                    if gap > 0:
                        await asyncio.sleep(gap)
                prebuffered = True
                for f in frames:
                    self.output.enqueue(f)
                frames.clear()
            for f in frames:  # 短句：整句都沒到 prebuffer 門檻
                if hard and not prebuffered:
                    gap = EARCON_GATE - (self.clock() - t0)
                    if gap > 0:
                        await asyncio.sleep(gap)
                    prebuffered = True
                self.output.enqueue(f)
            # 「有沒有出聲」交給 tick() 看 output.first_audible_at 判斷（見上）——
            # 這裡只負責把資料送完，不猜播放執行緒有沒有真的讀到。
        except VoiceError as e:
            # R2：兩層防重複回報——
            # (1) gen != self._gen：這一代已經不是目前的 playing（新的 _start 蓋過去了），
            #     舊世代的回報／旗標／退避全部略過。
            # (2) 就算 gen 還算數，_failed_reported 也可能已經被別的路徑設過True
            #     （典型案例：逾時分支已經同步報過一次「播放逾時」，被 cancel 的 task
            #     稍後才恢復執行、synth 把 CancelledError 轉成 VoiceError 跑到這裡）——
            #     此時不能再報第二次。
            # 兩種情況都只有 finally 的 EOS 照送，不然這段資料的 producing 永遠卡 True。
            if gen == self._gen and not self._failed_reported:
                self._backoff_until[self._backoff_key(iv)] = self.clock() + FAIL_BACKOFF
                self.on_failed(iv, f"TTS 失敗：{e}")
                self._failed_reported = True
        except Exception as e:
            # 非 VoiceError 也不能讓 playing 卡死——同樣退避＋回報失敗，finally 照樣送 EOS
            if gen == self._gen and not self._failed_reported:
                self._backoff_until[self._backoff_key(iv)] = self.clock() + FAIL_BACKOFF
                self.on_failed(iv, f"開口例外：{type(e).__name__}: {e}")
                self._failed_reported = True
        finally:
            self.output.end_of_utterance()  # 恰好一次：無論成功、TTS 失敗、其他例外都要送 EOS
