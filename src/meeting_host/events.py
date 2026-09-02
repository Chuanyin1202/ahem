"""事件匯流排的資料型別。

`Event` 是 `live.py`（`Session.emit`）與後續兩個工單（會議記錄、觀戰 UI）的接縫合約——
schema 定案於 docs/specs/2026-08-28-demo-readiness-design.md 的
「T-B 事件匯流排」一節，欄位表格為準，不得由消費端（T-C／T-D）更動。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    kind: str   # 見 spec 事件表：meeting/utterance/speaking/fast_timer/slow_score/
                # queued/spoken/failed/dropped/share/minutes/voice/glossary/hearing
    t: float    # 會議相對秒（Session.now）
    data: dict  # 依 kind 而定，見 spec 事件表

    # ── kind="voice" 補充說明（T12，不在原始 spec 事件表裡）─────────────────
    # data 形如 {"speaker": str, "active": bool}，跟 "speaking" 結構對稱，
    # 但來源完全不同、不可混用：
    #   - "speaking"：來自 STT 的 partial_transcript（stt.py 的 Speaking／
    #     SpeakingStopped），量的是「STT 有沒有正在辨識出內容」。
    #   - "voice"：來自 Discord RTP 層（discord_source.py 的
    #     MeetingBot._voice_start/_voice_stop），量的是「這個人的麥克風有沒有
    #     在傳送封包」，跟 STT 完全獨立。兩者觸發時間點與判準都不一樣，
    #     是刻意保留的兩條獨立訊號——用途見該工單：錄放器需要真實會議的
    #     voice_* 時間軸，以及 STT 幻覺調查需要一個跟 STT 無關的參照訊號。

    # ── kind="hearing" 補充說明（失聯偵測，不在原始 spec 事件表裡）─────────
    # data 形如 {"ok": bool, "reason": str, "voiced_seconds": float}：
    #   - "ok"：主席現在聽不聽得見。False＝失聰中。
    #   - "reason"：失聰的理由，取值見 `hearing.REASON_*`（"STT 連線中斷"／
    #     "有人出聲但沒有逐字稿"）；`ok=True` 時恆為空字串。
    #   - "voiced_seconds"：距上一則逐字稿以來，累積量到多少秒真的有人在對麥克風
    #     出聲（RTP 層，跟 STT 無關）。判定門檻與依據見 hearing.py。
    #
    # **只在狀態改變時 emit**（好→壞、壞→好），不是每秒一筆——每秒的心跳已經有
    # `fast_timer`。所以消費端要把它當「邊緣」看：收到 ok=False 之後一路維持
    # 失聰，直到下一筆 ok=True。中途連線的觀戰 UI 不會漏，`spectator` 一律先送
    # 全量 snapshot。
    #
    # 這個事件跟 `voice` 一樣**不代表主席的介入行為**：不經過 Chair、不產生
    # Intervention、不佔冷卻期、不發 TTS。它描述的是主席的感知能力本身。
    # 失聰期間被壓住的是哪幾條規則（三條，「議程超時」不在內）見
    # `fast_path.DEAF_SUPPRESSED_KINDS`；慢路則多出 `slow_score.reason` 的兩個
    # 新值 `失聰`／`失聰(話術後)`（後者已加入 `live.SLOW_BLOCKED_AFTER_DECISION`）。

    # ── kind="meeting" 補充說明（T16，不在原始 spec 事件表裡）─────────────
    # data 新增一個欄位：
    #   - "start_epoch"：會議開始（t=0）當下的 unix epoch 秒（float，不是相對秒）。
    #     觀戰 UI 用它把逐字稿／主席判斷每一列的相對秒 t 換算成真實時鐘時間
    #     （固定 UTC+8，不用瀏覽器本地時區）。回放模式讀到 T16 之前錄的舊
    #     events.jsonl 時這個欄位不存在，前端要能容錯退回顯示相對時間。

    # ── kind="partial" 補充說明（即時逐字顯示）──────────────────────────────
    # data {"speaker": str, "text": str}：這個人**這段目前為止的全文**（ElevenLabs
    # partial_transcript 是累積的，不是新增片段；實測約每秒一筆），已轉繁體。
    # 消費端用「覆蓋」不是「追加」：同一人的下一筆 partial 取代上一筆，該人的下一筆
    # "utterance" 是定稿、取代 partial；"speaking" active=False 時清掉。
    # **只給畫面用**：不進 MeetingState、不進逐字稿、快路慢路都不讀它。

    # ── kind="phase" / "phase_suggestion" 補充說明（階段自動判斷）─────────────
    # "phase"：階段**真的改變了**。data {"phase": str, "source": "manual"|"auto"}。
    #   manual＝觀戰畫面 POST /phase；auto＝--auto-phase apply 由偵測器套用。
    #   只在改變時 emit；開場階段仍在 "meeting" 事件裡。
    # "phase_suggestion"：偵測器的一次讀數。data {"phase", "confidence", "reason",
    #   "current": str, "applied": bool}。`applied` 為 True 代表同一刻已 emit "phase"。
    #   兩者都**不代表主席行為**：不經過 Chair、不佔冷卻、不發 TTS。判準與遲滯見 phase.py。

    # ── kind="glossary" 補充說明（提示卡，不在原始 spec 事件表裡）──────────
    # data 形如：
    #   {"term": str,                       # 術語本身，逐字照抄逐字稿裡的寫法
    #    "mentions": int,                   # 在幾則發言裡被提到（同一則講兩次算一次）
    #    "first": {"speaker", "t", "text"}, # 首次提到的那則發言，t 同 Utterance.start
    #    "explained": {...} | null,         # 會議裡真的有人解釋過的那則發言，同結構
    #    "gloss": str | null,               # 網路查證後的一句話說明
    #    "sources": [{"title", "url"}]}     # gloss 的來源連結
    #
    # 這是**唯一一種不代表主席行為的事件**：它不經過 Chair、不產生
    # Intervention、不寫 st.interventions、不佔冷卻期、不發 TTS。純粹是印給人看的
    # 補充資料，性質是「貢獻」而不是「糾正」（判準與成本見 glossary.py）。
    #
    # 不變量（消費端可以依賴）：`first` 恆存在，所以每一張卡都指得到逐字稿的
    # 具體位置（時間戳＋原話）；`gloss` 非 null 時 `sources` 必定非空——沒有
    # 來源連結的說明在 glossary.build_card 就被丟掉了，不會走到這裡。

    # ── kind="slow_score" 契約變動（T29：慢路拆成兩次 LLM 呼叫）────────────
    # 慢路從「一次呼叫同時產出三軸分數與話術」改成兩次獨立呼叫（判斷／話術），
    # 理由與實測依據見 slow_path.py 模組 docstring。事件形狀的影響有三點：
    #
    #   - `utterance` **語意變了**。舊：第一次呼叫順手寫的話術，不論這次評分
    #     admissible 與否都可能有值（連 type=無、被冷卻壓掉的那些也帶著話術）。
    #     新：話術是第二次呼叫的產物，而第二次呼叫**只在第一關閘門
    #     （live.slow_gate）通過時才發生**。所以現在
    #         reason ∈ {"", "type=無", "冷卻", "收尾"}  → utterance 必定是 ""
    #     （不是「模型沒寫」，是根本沒問過）。只有 admissible=True，或 reason
    #     落在 live.SLOW_BLOCKED_AFTER_DECISION 那幾個「決定要講之後才被擋」
    #     的理由時，這個欄位才可能有內容。消費端不可再把「空 utterance」
    #     讀成「模型判了介入卻寫不出話」。
    #   - `reason` **新增四個值**（全部發生在第二次呼叫之後）：
    #     `話術失敗`（呼叫失敗或回空）、`話術過長`（超過 UTTERANCE_HARD_CAP，
    #     整句作廢不截斷）、`冷卻(話術後)`、`收尾(話術後)`（TOCTOU 重驗擋下）。
    #     舊的 `無話術` 仍在 live.slow_result_admissible 的定義裡，但 production
    #     不再產生這個值——留著是為了離線工具與既有回歸測試。
    #   - **新增欄位 `utterance_seconds`**：第二次呼叫的實際往返秒數（float），
    #     沒打第二次呼叫時是 None。加這個欄位是因為這段延遲卡在「決定要講」與
    #     「排進 Chair」之間，直接吃掉 Chair 軟插入升級（ESCALATE_SECONDS=15s）
    #     的預算，事後只能從事件檔量得到。舊 events.jsonl 沒有這個欄位，
    #     消費端要能容錯。
    #
    # 一次評分仍然只 emit **一筆** slow_score（不是判斷、話術各一筆），而且
    # emit 與後續 Chair.request() 之間沒有 await——觀戰 UI 靠「admissible 的
    # slow_score 緊接著就是它的 queued」做的三態配對因此完全不受影響。
