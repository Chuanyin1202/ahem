"""腳本測試台的輸入端：把固定劇本假裝成 STT。

設計與定位見 `docs/specs/2026-09-05-script-harness-design.md`。一句話：
**與會者全是腳本，只有主席是真的**——真的判斷、真的話術、真的 TTS。

⚠️ **這裡產出的資料不能當判斷品質的證據。** 人寫的對話比真人乾淨、離題比真人明顯，
實測 8 個手寫場景 × 5 次是 40/40 零誤報（validation-results #3），同一套判斷在真實
會議上「該講卻不講」嚴重到 2026-09-05 才解掉一半。這個模組是**行為與 TTS 的驗收**，
品質量測一律回到 `experiments/holdout/` 的真實會議重評。

## 它替換的是哪一個接縫

`live.Session.consume(pool)` 只依賴 `pool.utterances()` 這個 async generator，以及
`pool.offline()`（失聰偵測的臂 A）；`discord_source.MeetingBot` 另外會呼叫
`pool.feed()` 把收到的音訊丟進去。三個方法就是全部的介面契約——照著實作，Session
不知道對面不是真的 STT。

## 為什麼要逐秒吐 Speaking，不能只在講完時吐 Utterance

快路的「發言超時」看的是 `state.speaking_now()`（由 partial 驅動），**不是** commit——
「講不停的人」不停就不會 commit，只吐 Utterance 的話那條規則永遠不會觸發
（見 `state.speaking_now` 的 docstring）。所以一則發言期間必須持續送 Speaking，
就像真的 STT 每秒回一次 partial 一樣。

## 為什麼還要自己驅動 voice_started／voice_stopped

Chair 的軟插入靠 `state.silent_for()` 等停頓，那條訊號只有 `discord_source.py` 在寫
（RTP 層的封包活動），跟 STT 是兩個獨立來源。沒有它的話 `silence_since` 停在建構時刻，
`silent_for` 一路長大，Chair 會判定「全場都沒人講」而在腳本角色講到一半就插話——
量到的就不是產品行為。所以這裡照著 `MeetingBot` 的形狀，兩條訊號都送。
"""
import asyncio
import time
from collections.abc import AsyncIterator, Callable

from .replay import CHARS_PER_SECOND
from .state import MeetingState, Utterance
from .stt import Partial, Speaking

PARTIAL_INTERVAL = 1.0
"""多久送一次 partial／Speaking。真實 STT 實測約每秒一筆（validation-results #1）。"""


def to_utterances(lines: list[tuple[float, str, str]]) -> list[Utterance]:
    """`(第幾秒, 誰, 講什麼)` → 帶結束時間的 Utterance 序列。

    結束時間的算法沿用 `replay.load()`：同一人連續兩則代表中間一直在講，
    end 直接接到下一則開頭；換人或劇本結束才用語速估算，且不得疊到下一則開頭。
    不沿用同一套規則的話，「同一人連續發言」會被建模成一堆有空檔的短句，
    `state.py` 的 run 計算會把摘要斷句當成真的停頓（見 replay.load 的說明）。
    """
    rows = sorted(lines, key=lambda r: r[0])
    out: list[Utterance] = []
    for i, (start, who, text) in enumerate(rows):
        next_start = rows[i + 1][0] if i + 1 < len(rows) else float("inf")
        next_who = rows[i + 1][1] if i + 1 < len(rows) else None
        if next_who == who:
            end = next_start
        else:
            end = min(start + len(text) / CHARS_PER_SECOND, next_start)
        out.append(Utterance(who, text, start, float(end)))
    return out


class ScriptSource:
    """假裝自己是 `stt.STTPool`。時間軸是**真實時鐘**，不加速。

    不加速是刻意的：主席的門檻是實際秒數（`fast_path.OVERTIME_SECONDS = 180`），
    壓縮時間量到的就不是產品行為。一個 10 分鐘的劇本就跑 10 分鐘。

    `t0` 必須是 `live.Session.t0`（裸 perf_counter）——劇本的秒數是會議相對時間，
    而 `Utterance.start/end`、`Session.now` 都在那個座標上。傳錯的話逐字稿的時間戳
    會跟主席看到的「現在第幾分鐘」對不起來。
    """

    def __init__(self, lines: list[tuple[float, str, str]], state: MeetingState,
                 t0: float, *, on_voice: Callable[[str, bool], None] | None = None):
        self.utterance_list = to_utterances(lines)
        self.state = state
        self.t0 = t0
        self.on_voice = on_voice or (lambda who, active: None)
        self.finished = asyncio.Event()
        """最後一則發言 commit 之後被設起來。呼叫端據此開始沉澱、然後收尾——
        沒有這個訊號的話會議會一直跑到有人手動砍掉，而且劇本播完後房間變成
        全靜默，快路會一路觸發「全場沉默」「有人被冷落」「議程超時」。
        2026-09-05 實測：一場 6.3 分鐘的劇本，主席開口 8 次，**其中 5 次是
        劇本結束之後的噪音**——判斷都沒錯，但那不是劇本要測的東西。"""

    # ── STTPool 的介面契約 ───────────────────────────────────────────
    def feed(self, speaker: str, pcm_48k_stereo: bytes) -> None:
        """`MeetingBot` 收到真人音訊時會呼叫這裡。腳本模式一律丟掉——

        這正是「環境太吵也沒關係」的原因：bot 還在頻道裡（TTS 要從那裡出去），
        但你講什麼都不會進逐字稿，主席只聽劇本。
        """

    def offline(self) -> bool:
        """失聰偵測的臂 (A)。腳本永遠不會斷線，所以永遠回 False。"""
        return False

    async def utterances(self) -> AsyncIterator[object]:
        """按真實時鐘把劇本吐成 STT 事件。

        一則發言的生命週期，跟真實 STT 對齊：
            voice_started → Speaking（每秒）＋ Partial（每秒，累積全文）
                          → Utterance（commit）→ voice_stopped
        兩則之間什麼都不送，就是真的沉默——Chair 的軟插入等的就是這段。
        """
        for u in self.utterance_list:
            await self._sleep_until(u.start)
            self.state.voice_started(u.speaker, time.perf_counter())
            self.on_voice(u.speaker, True)
            yield Speaking(u.speaker, u.start)

            # 講話期間：每秒一筆，partial 帶「這段到目前為止的整句」（累積全文，
            # 不是新增片段），跟真實 STT 的形狀一致（見 stt.Partial 的 docstring）
            span = max(u.end - u.start, 1e-6)
            while True:
                nxt = min(self._elapsed() + PARTIAL_INTERVAL, u.end)
                if nxt >= u.end:
                    break
                await self._sleep_until(nxt)
                shown = max(1, int(len(u.text) * (self._elapsed() - u.start) / span))
                yield Partial(u.speaker, u.text[:shown], u.start)
                yield Speaking(u.speaker, u.start)

            await self._sleep_until(u.end)
            yield Utterance(u.speaker, u.text, u.start, u.end)
            self.state.voice_stopped(u.speaker, time.perf_counter())
            self.on_voice(u.speaker, False)
        self.finished.set()

    # ── 時鐘 ────────────────────────────────────────────────────────
    def _elapsed(self) -> float:
        return time.perf_counter() - self.t0

    async def _sleep_until(self, when: float) -> None:
        delay = when - self._elapsed()
        if delay > 0:
            await asyncio.sleep(delay)
