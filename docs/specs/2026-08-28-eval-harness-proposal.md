# 不靠真人的迭代測試台（v2，依審查修訂）

> **狀態：核心離線工具已實作（2026-09-03 更新）**。已有回歸 harness、窗口計分、重評工具、real-holdout 流程，以及 `make eval-regression`／`eval-ui`／`eval-quality`／`eval-realtime`。Playwright／Chromium 已列入開發與 CI；`Chair`、`Session` 與 `Voice` 的讀時鐘、async 等待及網路 timeout 可由同一個 `VirtualClock` 驅動，Output 以 FakePlayer frame ledger 驗證。L2 已有本機 TTS 多人時間軸／重疊音軌生成器與 synthetic manifest，但仍不可取代真人短句 corpus。Regression 採固定 stub，不另外落地 LLM 回應快取，避免在磁碟保存可能引用逐字稿的模型輸出；`eval-quality` 固定禁用快取並至少跑五輪。

> 2026-08-28。v1 主張「錄 STT 事件重播就能抓到今天四種 bug」經審查**不成立**：四個 bug 分屬三種問題
>（狀態／時序、音訊幀完整性、程序生命週期），要三種 oracle。v2 改成「先有 oracle 與時鐘契約，再談錄放」。
> 目標不變：真人會議只做最後驗證；調參在幾分鐘內可重複。

## 一、三個 suite，不混跑

| suite | 時鐘 | LLM | 用途 | 速度 |
|---|---|---|---|---|
| `eval-regression` | **虛擬時鐘**（注入） | 固定回應（快取或 stub） | 狀態機、接線、invariants；今天那類 bug | 秒級、決定性 |
| `eval-quality` | 虛擬時鐘 | **真模型、禁快取、每組 ≥5 次** | prompt／門檻是否真的變好，報分布 | 分鐘級、花錢 |
| `eval-realtime` | 真實（speed=1） | 真 | TTS 首位元組、播放器、shutdown、STT 延遲 | 實時 |

「可重複但錯誤」比不決定性更危險：倍速重播只能在 `eval-regression`，且**所有**時間來源同一個虛擬時鐘。

## 二、時鐘契約（前置工作，其他都建在上面）

現況有四套時間：`Session.now`（裸 perf_counter − t0）、Chair 的 `clock`（裸 perf_counter）、`Speaking.since`／`Utterance.start`（錄製時相對）、Output 播放執行緒 20ms。

- 引入 `Clock` 介面：`now()`、`sleep(s)`（async）。`Session`、`Chair.run/tick`、`watch_fast/slow` 的 sleep、`Voice` chunk 延遲、`Output` 的消費節奏全部改經 `Clock`。生產用 `WallClock`；測試用 `VirtualClock`（`advance(s)` 後推進 event loop 到靜止）。
- 事件邊界定案：聲學 `voice_started/stopped`（來自 voice_recv）與 STT `Speaking/SpeakingStopped/Utterance` 是**兩條**訊號，fixture 與錄放格式都要各自帶。

## 三、三種 oracle（bug × oracle 覆蓋矩陣）

| 今天的 bug | oracle | suite |
|---|---|---|
| 停頓→commit→revision 作廢 | 狀態機 assertion：pending 存活且最終 `spoken` 恰一次 | regression |
| prebuffer 後 chunk 重複 | **PCM 完整性**：FakeVoice 送編號 frame，假播放器記錄每幀序號，斷言每幀恰好播一次（hard／soft 各一） | regression |
| 單人 run 永不歸零 | `current_run_seconds` 在長沉默後歸零、不觸發超時 | regression |
| 關閉不寫記錄 | **程序生命週期**：subprocess + SIGINT/SIGTERM，限時退出，檢查 summary/events/minutes 檔 | realtime |

前三個當時已各有測試，v2 的工作是把它們納入同一個 suite 與同一套 fixture 格式，而不是重寫。

## 四、劇本與資料集

劇本格式同 v1（`lines` 帶 `t/who/text/pause_after/overlap_prev/continuous`，`expect` 帶標註窗口），**加三樣**：
- `faults`：故障排程（TTS 首 chunk 逾時、中途失敗、播放器不消費／重啟、壞封包、STT 斷線）——這些劇本內容給不出，必須排程注入。
- `phase_truth` 只給 `eval-quality` 的**階段偵測指標**用（偵測器做出來之前不餵給 prompt）；prompt 用的 phase 由系統決定或手動覆寫，兩者分開評。
- 資料集切分：`dev`（可反覆看，含今天八場）／`held-out`（調參時不看）／`adversarial`（近門檻、自我修正、同時多問題）／`real-holdout`（真人會議錄音，只在候選版本晉級時跑）。**八個舊場景已用於選型與過濾器設計，只能當 dev。**

## 五、評分（取代 v1 的 F1p/F1n）

- **一對一窗口匹配**：每個 `expect` 窗口是一個 opportunity；系統介入依時間配對，一窗口最多一 TP，同窗口第二次起算 FP；`no_intervention` 區間內任何介入算 FP。
- 指標：opportunity recall、**FP／會議小時**、首次命中延遲、重複命中數、queued→spoken 成功率與延遲、soft 等待時間／升級率／作廢率、PCM 重複幀數、狀態 invariants 違反數。
- 慢路 TP 要求 `type` 屬於正確類型集合（`離題`↔`偏離主題` 先正規化），不再「只看該不該」。
- 介入後行為指標（給 quality／真人會議用）：點名後 N 秒目標是否開口、超時介入後原講者是否停、離題介入後議題相似度是否回升。
- 每次跑輸出 provenance：git SHA、劇本版本、fast_path 常數、Chair 秒數、STT 設定、model／effort、prompt hash、cache hit/miss、run index。

## 六、LLM 回應快取（只給 regression）

key 用**正規化請求**：`{schema_version, model, effort, system_prompt_hash, rules_hash, scenario_id, scenario_version, checkpoint_id, phase, participants_sorted, recent_utterances:[{speaker,text,relative_start_bucket}], stats:{elapsed_min_bucket, spoke_0_1min, share_pct, silent_0_1min}}`——去掉牆鐘、session id；時間量化到 prompt 顯示精度。prompt 改了就是新條件、全部 miss，這是對的。`eval-quality` 禁讀快取。

## 七、實作順序（改）

1. **四個已知 bug 的 regression 收斂進同一 harness**（半天）：`tests/harness/` 放 `VirtualClock`、`FakeVoice(numbered frames)`、`FakePlayer(frame ledger)`、`scenario runner`；四條序列各一測試。
2. **scorer**（半天）：`experiments/score_run.py events.jsonl scenario.yaml` → 上述指標表＋provenance。先有裁判，再有選手。
3. **時鐘契約落地**（1 天）：`Clock` 注入 Session／Chair／loops／Voice／Output；既有 133 測試仍綠。
4. **手寫第一批 L1 fixture**（半天）：八場 dev 轉新格式＋補 `faults`；不等錄放器。
5. **錄放器**（半天）：從真實會議錄 `voice_*`＋STT 事件時間軸，餵 runner——真人會議從此每場都變成一個 `real-holdout` 案例。
6. **L2 合成音訊**降為「STT 資料生成／壓力工具」，不在關鍵路徑；且要加非 ElevenLabs 的真人短句 corpus 做 STT 三階段評分（原始／OpenCC 後／最終）。
7. **階段偵測指標**與 held-out／adversarial 劇本：與自動階段偵測同一張單（第二場兩人會議之後）。

每次調參：改常數／prompt → `make eval-regression`（秒）→ `make eval-quality`（分鐘、花錢、看分布）→ 通過才排真人會議。

## 八、已知限制

- TTS 合成語音會讓 STT 表現偏樂觀；每人一軌下重疊只有時間並發、沒有聲學干擾。
- 標註窗口仍主觀；兩人標註取交集會丟掉灰區——灰區另存成 adversarial。
- 介入後行為指標需要閉迴路（LLM 扮演與會者）才有意義，屬第二階段。
