# Demo 就緒三件事：壞封包防護、會議記錄、觀戰 UI

> **狀態：已實作**。壞封包防護、事件匯流排、會議記錄與觀戰 UI 均已完成並在真實會議驗證。本文保留為設計與事件 schema 合約的紀錄；其中的工作波次是當時的排程安排。

> 2026-08-28。前提：語音輸出（`speaker.py`）已在真實會議驗證。本文定義三個子系統的設計與工單合約；
> 設計稿定稿在 `docs/design/spectator/`（字體與 29 個色票 token 以 `Main.dc.html` 的 `renderVals()` 為準，實作不得自行發明）。

## 排程

```
第 1 波（平行）  A 壞封包防護          ‖  B 事件匯流排（接縫合約）
第 2 波（平行）  C 會議記錄 A＋B        ‖  D 觀戰 UI
第 3 波          E 真實會議實測 ＋ 文件
```

第 2 波只能在 B 合併後開；C／D 不得動 `live.py`（B 已把接線做完）。

## A. 壞封包防護

**問題**：`discord-ext-voice-recv` 把任何解碼例外當致命：`PacketRouter.run()` 的 `finally` 呼叫 `stop_listening()`（router.py:96-104），`AudioReader.callback` 的 `feed_rtp` 例外也 `self.stop()`（reader.py:181-186）。當日 DAVE 那次就是這條路徑靜默拆掉接收器。現場網路一抖、一個壞封包，整場無法出聲且無任何訊息。

**修法**（`discord_source.py` 新增 `patch_packet_resilience()`，與既有 `patch_keepalive_for_macos`／`patch_dave_receive` 同一風格，`on_ready` 呼叫）：
- 包 `PacketRouter._do_run`：每個 decoder 的 `pop_data()`／`sink.write()` 各自 try/except；例外時 `decoder.reset()`、計數、印一行 `⚠️ 壞封包（ssrc=…）：{type}，已重置該路解碼器`，**繼續迴圈**。
- 包 `AudioReader.callback` 內 `feed_rtp` 的例外分支：改為記錄並丟掉該封包，不設 `self.error`、不 `stop()`。
- 同一 ssrc 10 秒內超過 50 次壞封包 → 只印一次「持續壞封包，可能是連線問題」，不再逐筆印。

**驗收**：
1. `tests/test_packet_resilience.py`：用真的 `PacketRouter` ＋ 假 sink，注入一個 `pop_data()` 會拋 `OpusError` 的 decoder（或 monkeypatch `PacketDecoder._decode_packet` 對特定 seq 拋錯），斷言 router 執行緒仍存活、`stop_listening` 未被呼叫、後續正常封包仍送到 sink。
2. 對照組：不套 patch 時同一測試會失敗（RED 證據）。
3. 全套測試綠；Pi5 `--say-hello` 實測仍正常。

## B. 事件匯流排（接縫合約）

**目的**：`live.py` 現在把一切印成字串；會議記錄與觀戰 UI 都需要結構化事件。**事件 schema 是 C／D 的合約，定義在 `src/meeting_host/events.py`，不得由第 2 波更動。**

```python
@dataclass(frozen=True)
class Event:
    kind: str          # 見下表
    t: float           # 會議相對秒（session.now）
    data: dict         # 依 kind
```

| kind | data | 來源 |
|---|---|---|
| `meeting` | `{topic, duration_min, phase, participants: [str]}` | 開始、名單變動、phase 變動時重送 |
| `utterance` | `{speaker, text, start, end}` | consume |
| `speaking` | `{speaker, active: bool}` | Speaking／SpeakingStopped／Utterance |
| `fast_timer` | `{run: {speaker, seconds} \| null, silent: {speaker: seconds}, remaining: seconds}` | watch_fast 每秒 |
| `slow_score` | `{positive, negative, none, type, verdict, utterance, pros: [str], cons: [str], admissible: bool, reason: str, utterance_seconds: float\|null}` | watch_slow 每次評分（含被壓掉的） |
| `queued` | `{kind, target, text, hard}` | Chair.request 接受 |
| `spoken` | `{kind, target, text, hard, at}` | on_spoken |
| `failed` | `{kind, target, text, reason}` | on_failed |
| `dropped` | `{kind, target, text, reason}` | on_dropped |
| `share` | `{speaker: pct}` 含 `主席` | 每次 utterance／spoken 後 |
| `minutes` | `{host_md, minutes_md, host_path, minutes_path, log_path, events_path}`（模組缺席時另含 `error`） | shutdown 的 `summary()`，全場最後一筆 |
| `voice` | `{speaker, active: bool}` | `MeetingBot._voice_start`／`_voice_stop`（Discord RTP 層，2026-08-29 新增） |
| `glossary` | `{term, mentions, first: {speaker, t, text}, explained: {speaker, t, text} \| null, gloss: str \| null, sources: [{title, url}]}` | `Session.watch_glossary`（提示卡，2026-08-31 新增） |
| `hearing` | `{ok: bool, reason: str, voiced_seconds: float}` | `Session._note_hearing`（失聯偵測，2026-08-31 新增；**只在狀態改變時**送） |

> ⚠️ `glossary` 是事件表裡**唯一不代表主席行為的事件**。它不經過 `Chair`、不產生
> `Intervention`、不寫 `st.interventions`、不佔冷卻期額度、不發 TTS——性質是「貢獻」
> 而不是「糾正」，跟其他十種介入不可混為一談（判準、批次節奏與成本見
> `src/meeting_host/glossary.py`）。消費端可以依賴兩個不變量：`first` 恆存在（每張卡
> 都指得到逐字稿的時間戳＋原話），`gloss` 非 null 時 `sources` 必定非空（沒有來源連結的
> 說明在 `glossary.build_card` 就已丟棄）。

> ⚠️ **`slow_score` 契約變動（慢路拆成兩次 LLM 呼叫）**。慢路從「一次呼叫同時
> 產出三軸分數與話術」改成兩次獨立呼叫：`slow_path.score()` 只判斷（每 5 秒一次），
> `slow_path.phrase()` 只產話術（只在通過 `live.slow_gate` 之後才打，一場 6-12 次）。
> 理由與 34 點實測依據見 `src/meeting_host/slow_path.py` 模組 docstring。事件形狀影響：
>
> - `utterance` **語意變了**：它現在是第二次呼叫的產物。`reason ∈ {"", "type=無",
>   "冷卻", "收尾"}` 時必定為 `""`——那代表**根本沒問過**，不是「模型判了介入卻寫不
>   出話」。只有 `admissible=true`，或 `reason` 是下面四個「決定要講之後才被擋」的值
>   時才可能有內容。
> - `reason` 新增四個值：`話術失敗`、`話術過長`、`冷卻(話術後)`、`收尾(話術後)`。
>   舊值 `無話術` production 不再產生（定義仍留在 `live.slow_result_admissible`，
>   給離線工具與回歸測試用）。觀戰 UI 把這四個算「受阻」而非「忍住」——主席已經
>   決定要開口，是話術生成或那幾秒的世界變動擋掉的，顯示成忍住會把失敗說成克制。
> - 新增欄位 `utterance_seconds`：第二次呼叫的往返秒數（沒打就是 `null`）。這段延遲
>   卡在「決定要講」與「排進 Chair」之間，直接吃 `ESCALATE_SECONDS=15s` 的預算。
>   舊 events.jsonl 沒有這個欄位，消費端要容錯。
>
> 一次評分仍然只 emit **一筆** `slow_score`，且 emit 與 `Chair.request()` 之間沒有
> `await`——觀戰 UI 的「admissible 的 `slow_score` 緊接著就是它的 `queued`」三態配對
> 與守恆不變式（開口＋受阻＋忍住 ＝ 總評分次數）完全不受影響。

> ⚠️ **`hearing`（失聯偵測，2026-08-31 新增）**。2026-08-31 那場 42 分鐘的真實會議
> 在第 36.7 分鐘因 ElevenLabs 額度耗盡讓 STT 整批斷線，主席不知道自己聾了，靠
> `silent_seconds` 一路灌水的數字連續四次對著在場的人喊「你怎麼都不說話」。這個事件
> 就是「主席現在聽不聽得見」的對外訊號，判準與 45 秒門檻的量測依據見
> `src/meeting_host/hearing.py`。三點契約：
>
> - **只在狀態改變時 emit**（好→壞、壞→好），不是每秒——每秒心跳已經有 `fast_timer`。
>   消費端要當「邊緣」看：`ok=false` 之後一路維持失聰，直到下一筆 `ok=true`。
> - 它跟 `voice`／`glossary` 一樣**不代表主席的介入行為**：不經過 `Chair`、不產生
>   `Intervention`、不佔冷卻期、不發 TTS，因此不進任何一格「主席開口」的計數。
> - 失聰期間被壓住的快路規則是三條（`fast_path.DEAF_SUPPRESSED_KINDS`：發言超時、
>   有人被冷落、全場沉默）；**「議程超時」不在內**——它只看時鐘，主席聽不見不代表
>   議程沒在走。慢路另外多出 `slow_score.reason` 的兩個值 `失聰`／`失聰(話術後)`，
>   後者已加入 `live.SLOW_BLOCKED_AFTER_DECISION`（算「受阻」不是「忍住」）。

> ⚠️ `voice` 與 `speaking` 是**兩個獨立來源，不可混用**。`speaking` 來自 STT 的
> `partial_transcript`（`stt.py`），量的是「STT 有沒有正在辨識出內容」；`voice` 來自
> Discord RTP 層，量的是「這個人的麥克風有沒有在傳送封包」。刻意保留兩條，因為測試台的
> 錄放器需要真實會議的 `voice` 時間軸，而 STT 幻覺調查需要一個與 STT 無關的參照訊號。

**實作**：
- `Session.emit(kind, data)`：append 到 `self.events: list[Event]`，並呼叫所有 `self.subscribers`（同步 callback，例外不得傳播）。
- `_log` 保留；改由各 emit 點同時 `_log` 對應的一行（顯示不變）。
- `slow_path.score()` 回傳值已含 `pros`／`cons`（prompt 要求 LLM 回），`live.py` 只是沒存——帶進 `slow_score`。
- **接線給第 2 波用（B 負責，C／D 不動 live.py）**：
  - `--spectator-port N`（預設 0＝不開）：`main_async` 啟動時 `from .spectator import serve; tasks.append(create_task(serve(session, N)))`——import 失敗（模組尚未存在）印一行警告並略過。
  - `summary()` 末尾：`from .minutes import write_minutes; write_minutes(session, out_dir)`——同樣 import 失敗略過。
  - `Session.events` 在 `summary()` 一併寫成 `meetings/<ts>.events.jsonl`（每行一個 Event）——這就是主持記錄 B 的原料。
- **`minutes` 事件（後續追加）**：`summary()` 先寫 `.log` 與兩份 md、再 emit `minutes`（帶兩份 md 的完整內容與四個檔案路徑），events.jsonl 由 `shutdown()` 最後才寫，所以總結一定在檔案裡的最後一筆——回放模式只餵 events.jsonl 也看得到總結。
- 既有 `src/meetings/*.log` 已 gitignore，`*.jsonl` 也加進去。

**驗收**：
1. `tests/test_events.py`：emit 序列化（`dataclasses.asdict` 後可 `json.dumps`）；subscriber 例外不影響其他 subscriber；`consume` 收到 Utterance 後 `events` 內有 `utterance`、`speaking(active=False)`、`share`。
2. 既有 88 測試全綠（`_log` 輸出不變）。
3. `--spectator-port 0` 與缺 `spectator`／`minutes` 模組時 `live.py` 照常啟動（import 略過路徑有測試）。

## C. 會議記錄 A＋B（`src/meeting_host/minutes.py`）

- 輸入：`Session`（`events`、`st`、`log`）。輸出兩個檔到 `meetings/`：
  - **B 主持記錄** `<ts>.host.md`：純程式產生，不用 LLM——介入清單（時間、類型、對象、硬／軟、話術、理由＝對應 `slow_score` 的 pros 或快路 detail）、被作廢／失敗的候選、發言時間分佈、階段軌跡（目前只有手動 phase 一段）。
  - **A 會議產出** `<ts>.minutes.md`：一次 LLM 呼叫（沿用 `slow_path` 的 API 設定與 model），輸入完整逐字稿＋介入清單，輸出：決議事項（誰、做什麼、何時前）、待辦與負責人、未解決事項（含主席的裁決理由）、每人立場摘要。JSON schema 回傳，再渲染成 md。
- `write_minutes(session, out_dir) -> tuple[Path, Path]`；LLM 失敗時 B 照寫、A 寫成「生成失敗：{原因}」不拋例外。

**驗收**：
1. `tests/test_minutes.py`：用手工組的 `events` 產 B，斷言介入與作廢各列一筆、發言分佈百分比正確；A 用假的 `score`-風格 stub 驗渲染。
2. 對今天 `src/meetings/offtopic-2026-08-28-2.log`（用對應的 events.jsonl，若無則用 B 新產的一場）真跑一次 A，人工看合理。

## D. 觀戰 UI（`src/meeting_host/spectator.py` ＋ `src/meeting_host/spectator/index.html`）

- `serve(session, port)`：aiohttp（已是相依）。`GET /` 回 `index.html`；`GET /events` SSE——連線先送 `snapshot`（`session.events` 全部）再串流後續 emit；`GET /health`。
- `index.html`：vanilla JS，無建置。`<html data-view="engineer|user" data-theme="dark|light">`，右上兩個切換鈕。**字體與色票照設計稿**：Google Fonts `Noto Sans TC`／`Noto Serif TC`／`JetBrains Mono`；CSS variables 從 `docs/design/spectator/Main.dc.html` 的 `dark`／`light` 物件逐 key 抄（29 個），`[data-theme=light]` 覆蓋。版面照 `EngineerDark`／`UserLight` 兩格。
- 區塊 ↔ 事件：逐字稿←`utterance`／`speaking`；快路計時條←`fast_timer`；慢路卡片←`slow_score`（`admissible=false` 用虛線半透明，同設計稿）；介入紀錄←`queued`／`spoken`／`failed`／`dropped`；發言分佈←`share`；頂列←`meeting`。
- 1440×900 為設計基準，寬度縮放可接受（`min-width: 1200px`）。

**驗收**：
1. `tests/test_spectator.py`：aiohttp test client——`/health` 200；`/events` 先收到 snapshot 再收到一筆 emit。
2. 用 `experiments/` 的一場 events.jsonl 回放（`python -m meeting_host.spectator --replay file.jsonl`）在本機開頁面，**截圖**兩視角×兩主題各一張（4 張）附在報告，對設計稿逐格比對。
3. 字體與色票核對：`rg` 檢查 index.html 內 29 個 token 名與值與設計稿一致（腳本比對，不憑肉眼）。

## E. 實測與文件

Pi5 跑 `--spectator-port 8765`，Alex 進頻道，瀏覽器開 `http://pi5:8765`；驗四個區塊即時更新、慢路被壓掉的卡片有出現、會後 `meetings/` 產三個檔。文件：README「下一步」、validation-results 新節。
