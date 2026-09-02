"""主席的「耳朵還在不在」——失聯偵測。

2026-08-31 那場 42 分鐘的真實會議，STT 在第 36.7 分鐘因為 ElevenLabs 額度耗盡
整批斷線。主席**不知道自己聾了**：`silent_seconds` 靠逐字稿的 `Utterance.end`
起算，逐字稿停了它就一路往上加，於是接下來五分鐘輪流對每個人喊「你怎麼都不
說話」（41:19 Jax、41:50 全場沉默、41:51 Alex、42:22 Alex、42:53 Jax）。

`silent_seconds` 對這兩件事給出**一模一樣**的數字，但正確反應完全相反：

    大家真的安靜下來了  → 該介入
    我的耳朵壞了        → 絕對不能介入，而且該讓人知道

分辨它們需要一條**不經過 STT** 的參照訊號。專案裡已經有：Discord RTP 層的
`voice` 事件（`discord_source.MeetingBot._voice_start/_voice_stop` → `events.py`
的 `kind="voice"`），量的是「這個人的麥克風有沒有在送封包」，跟 ElevenLabs
完全無關。那場事故當下 `voice` 還在正常跳動——證據就在手上，只是沒有人用它。

── 判準：兩條獨立的臂，OR ───────────────────────────────────────────────

**(A) STT 連線層自己說它連不上**（`stt.STTPool.offline()`）。
這是直接證據，也是那場事故的實際形狀：額度耗盡讓 WebSocket 握手回 401，
`STTPool._guard` 的錯誤分支每次重連失敗就把 `_fails` 加一。既有的連線層本來就
分得清「閒置關閉（正常，`_fails` 歸零）」與「真的連不上（`_fails` 累加）」，
所以直接沿用它，不另造一套推論。優點是**快**——兩次失敗約 2 秒就成立，遠早於
任何規則的門檻。缺點是它只看得見「連不上」，看不見「連上了卻不吐字」。

**(B) 有人正在對麥克風說話，但逐字稿完全沒有新內容**（本模組）。
間接證據，比 (A) 慢，但它涵蓋 (A) 看不見的那一整類故障（連線正常、伺服器收音
卻不回 transcript）。量的是「距上一則逐字稿以來，**累積**了多少秒真的有人在
出聲」——不是牆鐘時間。這個區別是整條規則成立的關鍵：沒有人出聲時累積不會長，
所以「大家真的安靜」永遠不會被誤判成失聰；那正是 (A) 與 (B) 都必須存在的理由。

兩條臂互不涵蓋，所以用 OR 而不是 AND。用 AND 的話「連上了卻不吐字」永遠偵測
不到，而那在 demo 現場跟額度耗盡一樣難堪。

── 門檻 DEAF_VOICED_SECONDS = 45.0 的依據 ──────────────────────────────

量自 `experiments/holdout/2026-08-29-two-person/meeting.events.jsonl`（雙人、
14.5 分鐘、繁中口語、STT 全程健康的真實會議，1626 筆 `voice` 事件）。把上面
(B) 的累積器套上去、每收到一則 `utterance` 就歸零，125 次歸零之間量到的分佈是：

    p50 = 3.8s   p90 = 10.0s   p99 = 16.1s   max = 19.8s

健康會議的上界是 19.8 秒（一個人講得久、STT 依自然停頓切 commit 的正常延遲）。
45.0 是它的 2.3 倍，同一場資料重跑零次誤判。

另一側的預算：門檻不能大到「來不及」。把同一場資料在 t=200/300/400/500/600/700
六個點截斷（模擬 STT 從那一刻死掉、`voice` 照常跳動），閘門啟動時刻與第一次可能
的錯誤介入（全場沉默＝最後一則逐字稿 +90s）相比：

    門檻 30s → 啟動 +28～31s，餘裕約 55s（但只有健康上界的 1.5 倍，太貼）
    門檻 45s → 啟動 +44～47s，餘裕約 40s（六個切點全部成立）
    門檻 60s → 啟動 +60～65s，且 t=700 那個切點到散會都沒累積夠，來不及

45.0 是「離健康上界夠遠」與「趕在第一次錯誤介入之前」兩個約束交集裡的值。

⚠️ 跟 `fast_path.SILENCE_SECONDS` 一樣，這是**單一場、單一組參與者**的樣本。
45.0 是有依據的起點，不是統計定論；之後有更多真實會議資料應該重新核算。

已知會誤判的情況（代價有上限，刻意不加特例規則）：麥克風一直開著的環境噪音會
讓 (B) 的累積器持續長大，而噪音的 committed transcript 會被 `stt.is_substantive`
丟掉、不產生 `Utterance`——於是 45 秒後被判成失聰。代價是那段時間主席對四條
規則裡的三條閉嘴並在觀戰 UI 顯示警示；**任何一則真的逐字稿進來就立刻解除**
（`heard()`），不需要重開會議。跟它擋掉的失敗形態（輪流對每個人喊「你怎麼都
不說話」）相比，這個交換是值得的。
"""

# 「距上一則逐字稿以來累積了這麼多秒的出聲」就判定失聰。依據見模組 docstring。
DEAF_VOICED_SECONDS = 45.0

# 判定理由字串。進 `kind="hearing"` 事件的 `reason` 欄位，觀戰 UI 直接顯示。
REASON_STT_OFFLINE = "STT 連線中斷"
REASON_NO_TRANSCRIPT = "有人出聲但沒有逐字稿"


class HearingMonitor:
    """累積「出聲但沒有逐字稿」的秒數，並綁上 STT 連線層自己的健康狀態。

    純狀態機，時間一律由呼叫端注入（`now`），不自己讀時鐘——跟 `MeetingState`
    同一個作法，回放與真實會議因此共用同一套邏輯。

    ⚠️ **座標**：這裡的 `now` 是**會議相對秒**（`live.Session.now`），不是
    `MeetingState.voice_started/voice_stopped` 用的裸 perf_counter。兩邊都由
    同一顆 RTP 訊號驅動，但走不同的呼叫端：`state` 那條由 `discord_source`
    直接呼叫（裸 perf_counter），這裡由 `live.Session.note_voice` 呼叫
    （`Session.now`）。刻意不共用，才不會把兩種座標混在同一個物件裡。
    """

    __slots__ = ("_active", "_since", "_voiced", "_stt_offline")

    def __init__(self) -> None:
        self._active: set[str] = set()      # 目前正在送封包的人
        self._since: float | None = None    # 目前這段「有人在出聲」的起點
        self._voiced: float = 0.0           # 已結束的出聲段累積秒數
        self._stt_offline: bool = False     # 臂 (A)：STT 連線層自己回報連不上

    # ── 輸入 ────────────────────────────────────────────
    def voice(self, speaker: str, active: bool, now: float) -> None:
        """RTP 層的「這個人的麥克風開始／停止送封包」。

        用「集合非空的區間」積分，不是每人各積一份：兩個人同時講的那一秒
        只能算一秒。這跟 `MeetingState.voice_stopped` 判斷 `silence_since`
        的作法一致（都是「還有沒有任何人在出聲」）。
        """
        if active:
            if not self._active:
                self._since = now
            self._active.add(speaker)
            return
        self._active.discard(speaker)
        if not self._active and self._since is not None:
            self._voiced += max(0.0, now - self._since)
            self._since = None

    def heard(self, now: float) -> None:
        """STT 真的吐出一則逐字稿——耳朵是好的，累積歸零。

        歸零之後若還有人正在出聲，這一段要**從現在**重新起算，不能沿用舊的
        `_since`：那一則 commit 已經證明 STT 活到此刻，之前那段出聲不算「沒被
        聽見」。這也是失聰狀態的**唯一解除條件**（臂 A 另外由 `note_stt_offline`
        每一 tick 刷新），所以恢復是自動的，不需要重開會議。
        """
        self._voiced = 0.0
        self._since = now if self._active else None

    def note_stt_offline(self, offline: bool) -> None:
        """臂 (A)：由呼叫端（`live.Session`）每一 tick 從 `STTPool.offline()` 刷新。

        不在這裡直接讀 pool——這個模組不 import `stt`，才能在沒有任何連線的
        回放／單元測試裡單獨使用。
        """
        self._stt_offline = offline

    # ── 查詢 ────────────────────────────────────────────
    def voiced_seconds(self, now: float) -> float:
        """距上一則逐字稿以來，累積有多少秒真的有人在出聲（含還沒結束的那一段）。"""
        if self._since is None:
            return self._voiced
        return self._voiced + max(0.0, now - self._since)

    def deaf(self, now: float) -> bool:
        """主席的耳朵是不是壞了。兩條臂 OR，理由見模組 docstring。"""
        return bool(self.reason(now))

    def reason(self, now: float) -> str:
        """失聰的理由；耳朵正常時回空字串。

        STT 連線層的直接證據優先——它比累積量更明確，也是 2026-08-31 那場事故
        的實際形狀；兩條臂同時成立時報它，讀 log 的人才知道要去看額度而不是
        去猜麥克風。
        """
        if self._stt_offline:
            return REASON_STT_OFFLINE
        if self.voiced_seconds(now) >= DEAF_VOICED_SECONDS:
            return REASON_NO_TRANSCRIPT
        return ""
