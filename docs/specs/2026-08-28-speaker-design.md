# 讓主席開口：語音輸出子系統設計

> **狀態：已實作**（設計於 2026-08-28，實作見 `src/meeting_host/speaker.py`，已在真實會議驗證）。本文保留設計當時的前提：「語音輸出零行數」「參與者為空清單」「會後總結範圍外」描述的是當時，均已由後續實作取代；「已定決策」一節的示例話術其後經稽核修正，不再宣稱規則未檢查的前提。

> 2026-08-28。前提：`docs/interruption-design.md` 第五節已定案「硬打斷＝提示音→停頓→說話、軟插入＝等停頓直接說」，
> `docs/tech-architecture.md` 定案 TTS 走 ElevenLabs streaming。本文只補「怎麼落地」，不重開那些決策。
> 現況：聽（Discord 每人一軌 → STT）與判斷（快路／慢路）已在真實會議驗證；「說」零行數。
> v2：依第三方設計審查修訂（5 Critical、5 Warning、1 Info，10 條採納）。

## 已定決策（本次討論）

| 題目 | 決定 | 理由 |
|---|---|---|
| 主席講到一半被蓋過去 | **A：講完為止**；B（偵測到人聲即停，如 OpenAI Realtime 的 `interrupt_response`）記入待辦 | 介入 ≤2 句；主席有制度性權力不用搶；先看被蓋過的頻率再決定 |
| 軟插入的「停頓」 | 沒有任何人在講 **≥ 1.0 秒**（聲學訊號，見 §2） | 比 STT commit 的 0.6 秒保守，避免搶在換氣點 |
| 等不到停頓 | **等超過 15 秒升級成硬打斷**，升級前重驗觸發仍成立 | 候選發言有存活時間，過期作廢 |
| 模型 | 全遠端；TTS 備案由本地 Qwen3-TTS 改為 OpenAI `gpt-4o-mini-tts`（本 spec 只留 TTS provider 抽象，不動 STT） | 雲端備案切換成本低 |
| 主席的聲音 | 先任選一個支援中文的 ElevenLabs 女聲，`voice_id` 為單一常數，之後再換 | 先能動再調 |

## 1. 架構：新模組 `src/meeting_host/speaker.py`，四個單位

```
Trigger（快路）／評分結果（慢路）
        ↓ Chair.request(Intervention)
   Chair ──── pending / playing 兩槽的狀態機：說什麼、怎麼說、什麼時候說
        ↓
   Voice ──── 文字 → 48kHz s16le 立體聲 PCM 串流（ElevenLabs；provider 可換）
        ↓
   Output ─── discord.AudioSource，取代 _Silence：內建 byte buffer，read() 永遠回精確 3840 bytes
        ↑
   Earcon ─── 預先生好的短提示音（進 repo；啟動時載入並驗證格式，失敗即 fail fast）
```

- `Output` 閒置時行為與 `_Silence` 相同（送靜音撐開 RTP 雙向通道）。
- 介面：
  - `Voice.synth(text) -> AsyncIterator[bytes]`：已是 48k 立體聲 PCM，片段大小任意
  - `Output.enqueue(pcm)`、`Output.end_of_utterance()`（EOS sentinel）、`Output.is_busy()`：
    busy = producer 未結束 **或** buffer 有資料 **或** 幀未送完；不是 `queue.empty()`
  - `Output.first_audible_at`：第一個非靜音幀被 `read()` 的時刻（interventions 用）
  - `Chair.request(iv: Intervention)`；`Intervention = (kind, target, text, hard, revision, created_at)`

### 音訊格式契約（可測）

```
ElevenLabs stream(output_format="pcm_24000")  → raw s16le mono 24k
  → audioop.ratecv 24k→48k（跨 chunk 保留 state，同 stt.py 反向路徑）
  → audioop.tostereo
  → Output buffer → 3840-byte 幀（20ms）；尾段補零
```
若方案允許 `pcm_48000` 則省掉重採樣，但契約不變。奇數 byte 留到下一 chunk。
Earcon wav 啟動時去 header、驗 48k/16-bit/2ch，否則轉換一次後快取。

## 2. Chair 狀態機

### 訊號來源
- **沒人在講**：由 `voice_recv` 的 `voice_member_speaking_start/stop`（封包 0.2s 逾時，聲學層）維護
  `MeetingState.silence_since`。STT partial 驅動的 `speaking` 只給超時規則用，**不當軟插入的 VAD**——
  它在空 commit 時會卡住（`stt.py` 空字串 `continue`，`live.py` 只在有 Utterance 時清）。
- **觸發仍成立**：`revision` 由呼叫端遞增（發言者換人、目標開口、慢路新評分都 +1）；
  開口前 `revision` 不符即作廢。

### 兩槽
| 槽 | 內容 |
|---|---|
| `pending` | 最多一個等待中的介入（含等停頓的軟插入） |
| `playing` | 正在播的介入；播放中最多保留 **一個** 更高優先的候選，播完重驗後執行 |

### 規則
| 情境 | 行為 |
|---|---|
| 硬打斷（發言超時、事實錯誤） | earcon 入佇列 **同時** 啟動 TTS；0.7s 後（且 TTS 已有 prebuffer ≥ 200ms）開始播語音 |
| 軟插入（其他全部） | 等 `silence_since ≥ 1.0s` → 播；等超過 15s → 重驗仍成立則改走硬打斷（文字用當下事實重生） |
| pending 是 soft、來了 hard | hard 取代 soft（規則型 > LLM 型，`interruption-design.md` 優先序） |
| playing 中來了更高優先 | 存為候選，播完重驗再播；同級或更低直接丟 |
| earcon 已播 | 視為不可取消的介入 |
| 主席講話中有人蓋過去 | 講完為止（A） |

### 三種紀錄（拆開）
| 紀錄 | 誰寫 | 用途 |
|---|---|---|
| `claimed`（=現在的 `done`） | 觸發端在 `Chair.request` 被接受時 | 防同一 trigger 每秒重送 |
| `attempts` | Chair | TTS／播放嘗試與失敗原因，供事後檢視；失敗後對同 kind 退避 30s |
| `interventions` | Chair 在 `first_audible_at` | 快路冷卻、統計——只記真的出聲的 |

`live.py` 現在觸發即寫 `interventions`，改為只寫 `claimed`；`interventions` 交給 Chair。
慢路在 pending 存在期間跳過評分（`should_score` 多一個條件）。

## 3. 說什麼
- 快路模板（填 `Trigger` 已算好的事實）：
  - 發言超時：「{who}，你已經講了 {n} 分鐘，先讓其他人接一下。」
  - 有人被冷落：「{who} 從開會到現在還沒說話，你對這個提案的看法是什麼？」
  - 議程超時：「只剩 {n} 分鐘，我們往結論收。」
- 慢路：LLM 的 `utterance`；為空則不介入並記 log
- 被 `type=無` 壓掉的評分（三軸＋話術）寫進會議 log，不出聲

## 4. 資料流與執行緒
- TTS 在 asyncio 側抓串流 → 轉換 → `Output` 的 `queue.Queue`（有界，約 30s）
- Discord 播放執行緒每 20ms `read()`；跨執行緒只有這一個佇列
- 收音路徑不動；`_on_audio` **排除 `user.bot`**（含自己），避免主席或其他 bot 的聲音進 STT
- `live.py` 啟動與 `voice_state_update` 時把頻道內非 bot 成員同步進 `MeetingState.participants`
  ——目前是空清單，「有人被冷落」在真實會議永遠不會觸發（先前紀錄標「已修」是錯的，只修了 replay）

## 5. 失敗處理
| 失敗 | 處理 |
|---|---|
| TTS first-byte > 3s 或總時長 > 15s | 取消、記 `attempts`、印本該說的話；不寫 `interventions`；同 kind 退避 30s |
| 已入佇列半句後斷線 | 送 error sentinel，Output 播完已有資料後回靜音 |
| Output underflow | 回靜音幀，不中斷播放器 |
| `read()`／Opus 拋錯、播放器停止 | `vc.play(..., after=cb)` 偵測，重建 Output 並重新 `play` |
| Discord 重連 | `on_voice_state_update` 後重新 `play(Output)` |
| Earcon 檔缺失／格式錯 | 啟動時 fail fast |
| 關閉 | 取消 producer、清佇列 |

## 6. 測試
1. **自動（pytest，venv）**：Output 任意 chunk → 精確 3840、補零、EOS、underflow；PCM 轉換固定波形驗 48k/2ch/時長；
   Chair 假時鐘：0.99/1.0s 邊界、等待中恢復說話、15s 升級、revision 作廢、hard 取代 soft；
   bookkeeping：claimed/attempts/interventions 各自對冷卻的影響
2. **離線**：假 `Output` 把「earcon＋一句 TTS」寫成 wav 聽拼接
3. **Discord 單人**：`live.py --say-hello`；同時記錄 sink 收到的 user id，確認主席的聲音沒進 STT
4. **真實會議**：第二場兩人以上，驗冷落規則（participants 同步後）與軟插入等停頓

## 7. 不在範圍
B 打斷即停、螢幕視覺預告、會後 summarize、Kaner 階段自動偵測、第二套 STT。
