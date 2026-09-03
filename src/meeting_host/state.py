"""會議狀態：快路規則的資料來源。

所有時間單位為秒。時間由外部注入（`now`），不自己讀時鐘——
這樣逐字稿回放與真實會議可以用同一套邏輯。
"""
import time
from dataclasses import dataclass, field

# 同一人前後兩句發言之間，間隔超過此值即視為這一輪連續發言已經結束
# （即使之後又是同一人開口，也算新的一輪，run 重新起算）。
RUN_GAP_SECONDS = 5.0


@dataclass
class Utterance:
    speaker: str
    text: str
    start: float
    end: float


@dataclass
class MeetingState:
    topic: str
    duration_min: int
    participants: list[str]
    utterances: list[Utterance] = field(default_factory=list)
    # 主席已介入的時刻，供冷卻期判斷
    interventions: list[float] = field(default_factory=list)
    # 逐字稿涵蓋範圍之前就發生過的事。真實會議中恆為空——
    # 只有回放測試場景（逐字稿僅保留最後幾則）才需要補這段看不見的歷史
    prior_spoke: dict[str, float] = field(default_factory=dict)
    prior_last: dict[str, float] = field(default_factory=dict)
    # 正在說話的人 → 起始時刻。由 partial 結果驅動，不等 commit
    speaking: dict[str, float] = field(default_factory=dict)
    # 聲學層「誰正在出聲」——來自 voice_recv 的封包 0.2s 逾時事件，
    # 跟 STT partial 驅動的 speaking 不同：那個在空 commit 時會卡住，只能給超時規則用
    voice_active: set[str] = field(default_factory=set)
    # 已離開語音頻道的人。仍留在 participants（會後統計要算他的發言），
    # 但主席不能點名一個不在場的人（I5）
    absent: set[str] = field(default_factory=set)
    # None = 有人在講。預設值必須與 voice_started/stopped、Chair 同座標（裸 perf_counter）：
    # 寫死 0.0 的話，silent_for(perf_counter()) 得到的是「程序啟動至今」，
    # 第一個 voice event 之前的軟插入會被當成早就達到停頓門檻（I3）
    silence_since: float | None = field(default_factory=time.perf_counter)
    # 建構當下的裸 perf_counter，只給 ensure_participant() 換算 joined_at 用。
    # 跟 Session.t0／STTPool.t0 是各自對同一個「會議開始」時刻拍的快照——
    # main_async 開頭三行幾乎背靠背建構，誤差在微秒等級，足夠對齊
    # （沿用 silence_since 同一套「建構時間戳當基準」的作法）。
    _t0: float = field(default_factory=time.perf_counter, repr=False)
    # 從未發言過的人，第一次被 ensure_participant() 記下的「成為在場參與者」
    # 時刻——座標跟 Utterance.start/end、silent_seconds() 的 now 相同（會議相對時間）。
    # T13：會議進行到一半才加入、還沒開口的人，沉默要從這裡起算，不能從會議開始算，
    # 否則一進頻道就會被判定「已經沉默了整場會議」，立刻誤觸發「全場沉默」／
    # 「有人被冷落」（實測：使用者剛加入語音頻道那一刻，主席就催了一次全場沉默）。
    joined_at: dict[str, float] = field(default_factory=dict)
    # 「全場沉默」規則已經在這場會議裡實際觸發過幾次——給 fast_path.check() 算
    # 退避門檻、給 utterance_for() 選話術輪替版本用。只在真的排入 Chair 時才遞增
    # （見 Session._fast_tick 呼叫 note_room_silence_fired()），呼叫端負責時機，
    # 跟 done 的管理方式一致。
    room_silence_hits: int = 0

    def add(self, u: Utterance) -> None:
        self.utterances.append(u)

    # ── 查詢 ────────────────────────────────────────────
    def spoke_seconds(self, who: str) -> float:
        return (self.prior_spoke.get(who, 0.0)
                + sum(u.end - u.start for u in self.utterances if u.speaker == who))

    def silent_seconds(self, who: str, now: float) -> float:
        """距離此人上次發言結束多久。

        從未發言過的人，起點是他成為在場參與者的時刻（ensure_participant()
        記下的 joined_at）——不能是會議開始的時刻，否則會議進行到一半才加入、
        還沒開口的人一進來就會被算成「已經沉默了整場會議」（T13）。
        沒有 joined_at 記錄（例如回放路徑：一開始就在建構參數的 participants
        裡，從未走過 ensure_participant()）則維持舊行為，回傳會議已進行的時間。
        """
        if who in self.speaking:
            return 0.0  # 正在講話的人不算沉默
        ends = [u.end for u in self.utterances if u.speaker == who]
        if who in self.prior_last:
            ends.append(self.prior_last[who])
        if ends:
            # 發言結束時間是估算的，可能落在 now 之後，夾住避免負值
            return max(0.0, now - max(ends))
        joined = self.joined_at.get(who)
        return max(0.0, now - joined) if joined is not None else now

    def speaking_now(self, who: str, since: float) -> None:
        """某人「正在說話」——由 STT 的 partial 結果驅動，每秒更新。

        ⚠️ 這是「發言超時」規則能運作的關鍵：
        STT 只有在說話者停頓後才產生 Utterance，但超時規則要抓的正是
        「講不停的人」——他不停就不會有 Utterance，規則就永遠不會觸發。
        所以必須有一條不依賴 commit 的即時訊號。
        """
        if who not in self.speaking:
            self.speaking[who] = since

    def stopped_speaking(self, who: str) -> None:
        self.speaking.pop(who, None)

    def voice_started(self, who: str, now: float) -> None:
        self.voice_active.add(who)
        self.silence_since = None

    def voice_stopped(self, who: str, now: float) -> None:
        self.voice_active.discard(who)
        if not self.voice_active and self.silence_since is None:
            self.silence_since = now

    def silent_for(self, now: float) -> float:
        """沒有任何人在講的秒數；有人在講回 0。"""
        return 0.0 if self.silence_since is None else max(0.0, now - self.silence_since)

    def ensure_participant(self, name: str, now: float | None = None) -> None:
        """把 name 加進在場名單；第一次加入時若給了 `now`，記下 joined_at
        供 silent_seconds() 當「從未發言」時的沉默起點用（見該方法說明）。

        `now` 的座標是裸 perf_counter——跟 voice_started/voice_stopped 一樣，
        呼叫端（discord_source.py）直接傳 time.perf_counter() 即可，不需要
        知道會議的相對時間座標；這裡用 `_t0` 換算成跟 Utterance.start/end
        同座標存進 joined_at。不給 `now`（例如回放路徑、單元測試）就不記錄，
        維持舊行為。
        """
        if name in self.participants:
            return
        self.participants.append(name)
        if now is not None:
            # perf_counter 的絕對值通常不小，兩個相近 float 相減會留下約
            # 1e-14 的抵銷誤差；會議狀態只需要微秒精度，先正規化可避免
            # 相同事件在不同 runner 上落在門檻兩側。
            self.joined_at[name] = round(max(0.0, now - self._t0), 6)

    def note_room_silence_fired(self) -> None:
        """「全場沉默」規則實際排入 Chair 一次後呼叫，遞增退避次數。

        呼叫端（Session._fast_tick）只在 chair.request() 成功時才呼叫這裡，
        跟 done 的管理方式一致——check() 本身保持唯讀，不自己碰這個計數器。
        """
        self.room_silence_hits += 1

    def _chain_start(self, who: str, seg_start: float) -> float:
        """從 seg_start 往前串同一人、句間 gap ≤ RUN_GAP_SECONDS 的 utterances，
        回傳這一輪 run 真正的起點。

        current_run_seconds 的兩種情境都要靠這個鏈：
        - 有人正在講（speaking）：seg_start 是這次 partial 的 since。只看
          這次 since 會漏算他前面已經 commit、屬於同一輪的部分——真人連續
          講很久，STT 通常會依自然停頓切成多個 commit，若不往前鏈，
          「發言超時」會被系統性延後（見 task-f-report.md 第二輪 tick-by-
          tick 實測）。
        - 沒人在講：seg_start 是最後一句的 start。

        跳過 `u.end > seg_start` 的句子——partial 有時比對應那句的 commit
        早到，此時那句還沒真正「結束」，不該被當成 seg_start 之前的歷史。

        遇到不同 speaker 已結束的句子立刻中斷鏈（T15）：多人會議快速交替時，
        同一人前後兩句自己的 gap 可能一直落在 RUN_GAP_SECONDS 內，即使中間
        有人插話——純比時間距離抓不到「換人講過」這件事，鏈會一路往前串穿
        別人的發言，導致「連續發言」量到的其實是這個人整場散落的發言總和，
        而不是他不間斷佔著發言權的那一段（實測：雙人會議 14 分鐘內同一人
        誤觸發「發言超時」11 次，run 從 3.5 分一路累加到 5.2 分未歸零）。
        """
        start = seg_start
        for u in reversed(self.utterances):
            if u.end > seg_start:
                continue
            if u.speaker != who:
                break  # 別人插過話：這一輪到此為止，不再往前串
            if start - u.end > RUN_GAP_SECONDS:
                break
            start = u.start
        return start

    def current_run_seconds(self, now: float) -> tuple[str | None, float]:
        """目前這位發言者「連續講了多久」——中間被別人插話，或沉默超過
        RUN_GAP_SECONDS 就重新計算。

        優先看「正在說話」的即時狀態；沒有的話才回頭看已完成的發言——
        且只算到最後一句結束為止，不能用 now 繼續往後灌水，否則講完話
        沉默下來，run 仍會隨著時間一直長大，最終誤觸發「發言超時」硬打斷。
        兩種情境都透過 `_chain_start` 往前串同一輪的句子。
        """
        if self.speaking:
            who = min(self.speaking, key=lambda k: self.speaking[k])
            since = self.speaking[who]
            return who, now - self._chain_start(who, since)

        if not self.utterances:
            return None, 0.0
        last = self.utterances[-1]
        if now - last.end > RUN_GAP_SECONDS:
            return None, 0.0  # 已沉默夠久，這一輪 run 算結束了

        start = self._chain_start(last.speaker, last.start)
        return last.speaker, last.end - start

    def share(self, who: str, now: float) -> float:
        """發言時間佔比。分母用實際說話總時長，不是會議時長——
        會議大半時間可能是沉默，用會議時長當分母會讓所有人的佔比都失真。"""
        total = sum(self.spoke_seconds(p) for p in self.participants)
        return self.spoke_seconds(who) / total if total else 0.0

    def remaining_seconds(self, now: float) -> float:
        return self.duration_min * 60 - now

    def since_last_intervention(self, now: float) -> float:
        return now - self.interventions[-1] if self.interventions else float("inf")

    def recent(self, n: int = 6) -> list[Utterance]:
        """近因假設：引導者只依最近幾則發言判斷（見 interruption-design.md）。"""
        return self.utterances[-n:]
