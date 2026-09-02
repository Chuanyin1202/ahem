# 技術架構與選型

> **狀態（2026-09-02）**：本文記錄 2026-08-23 的架構選型。已被實作取代的部分：第四節「預先生成候選發言、延遲只剩 TTS」——現行慢路是判斷與話術兩次呼叫，評分到排入佇列約 4.9 秒（見 validation-results 5-2）；分層決策中的規則／LLM 分工以現行為準（規則：發言超時、議程超時、有人被冷落、全場沉默；LLM：離題、重複、假共識、僵局、事實錯誤）；第三節本地備案已定案不做。語音管線、Discord 每人一軌、延遲約束的分析仍為現行依據。

> 建立於 2026-08-23。語音查證於 2026-08-22，資產盤點於 2026-08-23。

---

## 一、三層結構

```
定位層  ──  主席，不是助理。有裁決權，而且會用。
             ↓
知識層  ──  Kaner Diamond ＋ 引導方法論
             判斷「現在該不該說、該說什麼類型的話」
             ↓
機制層  ──  Inner Thoughts（thoughtful-agents）
             判斷「這個念頭夠不夠格打斷別人」
```

### 機制層：Inner Thoughts（CHI 2025）

處理「何時開口」，做法跟直覺不同：

**不是**「預測下一個該誰講」——論文明確指出這招在多人會議會失敗，因為真實會議大多是自己搶著講，沒有明確輪次分配。

**而是**：AI 在對話進行的同時，**平行地持續產生自己的內心想法**，每個想法計算一個「參與動機分數」，分數過門檻才開口。

五階段：`trigger → retrieval → thought formation → evaluation → participation`

實測在擬人性、連貫性、**插話時機恰當性**上都顯著勝過既有做法。

開源實作：[`xybruceliu/thoughtful-agents`](https://github.com/xybruceliu/thoughtful-agents)（Python，2639 行）

> ✅ **賭注 #2 已驗（2026-08-24）：讀過原始碼，框架能用，但要動手術。**
> 它是為「AI 當平等參與者」設計的，不是為主席。三個必要改動與完整的介入類型設計
> 見 **[`interruption-design.md`](./interruption-design.md)** ——那是本專案的技術核心。

---

## 二、語音管線：耳朵 / 大腦 / 嘴巴

> **決策（2026-08-24）：全部用遠端模型，語音走 ElevenLabs。** 12 天的專案不花時間在本地模型環境上。

```
音訊層  ──  誰在講話（由平台／麥克風聲道決定，不靠聲紋辨識）
            ↓ 每人一條獨立音訊流
耳朵    ──  ElevenLabs Scribe v2 Realtime（WebSocket STT，150ms）
            只做「這條聲道講了什麼」，不做「這是誰」
            ↓
大腦    ──  自己做 ← 全部的價值所在
            Inner Thoughts 機制（該不該開口）
            ＋ Kaner Diamond（現在哪個階段、該說什麼類型的話）
            ↓
嘴巴    ──  ElevenLabs streaming TTS（純發聲）
```

### ElevenLabs 即時 STT 不做 speaker diarization

這是官方明確的設計取捨：**Scribe v2 Realtime 為了低延遲省略了 diarization，diarization 只在批次版 Scribe v2 提供**（最多 32 人）。且官方表示即時版的非英語 diarization 目前不是優先項目。

**這不改變路徑，反而證實了它**：第五節已把 diarization 定為「可繞過」——Discord 每人一軌、實體會議每人一支麥。ElevenLabs 只負責把**單一人的聲道轉文字**，「誰在講」由音訊層告訴它。這正是 Scribe v2 Realtime 擅長的事（90+ 語言含中文）。

### 關鍵判斷：把 ElevenLabs 當耳朵和嘴巴用，不要當大腦用

ElevenLabs Agents（Conversational AI）有 barge-in 偵測和自訂 turn-taking 模型，first-turn latency 500ms 以內。聽起來完美，**但它不開放 VAD 閾值、不開放「禁止打斷的時間窗」、不開放狀態轉換規則，重疊語音處理也不是可調子系統。**

→ **直接用它當整個 agent，「何時開口」的決策就在別人的黑盒裡，而那正是唯一的差異化。** 而且它的 turn-taking 是為一對一對話設計，不是為四個人搶著講話的會議室。

~~**待驗**：Conversational AI 的 turn-taking 能否關閉、改由我們的決策層控制它何時開口。若可以，STT + TTS + 串流基礎建設可以整包借用，語音管線工程量少一大半。~~
→ **已不需要驗證（2026-08-28）**：決策層已自建完成（`speaker.py` 的 `Chair`／`Intervention`），語音管線走 Scribe Realtime WebSocket＋TTS streaming 自組，不借用 Conversational AI。

### 額度規劃

| 項目 | 數字 |
|---|---|
| ElevenLabs 免費方案 | 10,000 credits／月 ≈ 10 分鐘 TTS——**只夠驗證接通** |
| 黑客松贊助 | 110,000 credits／人，四人合計 440k——**但活動前 48 小時才開通（約 9/2）** |
| 即時 STT | $0.39／小時 |
| TTS | $0.10／千字（v2）、$0.05／千字（Flash） |

**建議**：開發期付一個月的 Starter/Creator 級方案，把贊助額度留給現場。中途卡額度比這幾百塊貴得多。（待 Alex 拍板）

---

## 三、既有資產（2026-08-23 盤點）

> **2026-08-24 降級為備案。** 決策改為全遠端模型（第二節），以下資產**不主動用**，只在 ElevenLabs 出問題或額度耗盡時啟用。保留紀錄是為了讓備案可以在一天內切換。

`Personal/ai-tools/sherpa-onnx` — 本地已有可跑的中文即時語音辨識

| 資產 | 內容 | 對本專案的意義 |
|---|---|---|
| 中文即時辨識腳本 | `scripts/realtime-chinese/realtime_chinese_recognition.py` | 賭注 #1 的一半已經有底 |
| Zipformer 串流模型 | `sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20` | 中英雙語、即時、低延遲——正是需要的 |
| VAD | `silero_vad.onnx` | barge-in 需要 |
| SenseVoice | 中/日/英/韓/粵離線 | 備用 |
| Fire Red | 中英高精度離線 | 備用（非串流） |
| HTTP API 服務 | `production/` | 可直接包成服務 |

~~`Personal/ai-tools/Qwen3-TTS` — 本地 TTS（1.2G），ElevenLabs 的備案。~~
→ **2026-08-28 改為 OpenAI `gpt-4o-mini-tts`（雲端）當 TTS 備案**，取代原訂的本地 Qwen3-TTS；STT 備案（下方）不受影響。

**缺口：sherpa-onnx 本地 `models/` 沒有 speaker diarization 模型。** 上游 sherpa-onnx 支援 speaker segmentation，需另外下載驗證。

### 備案切換順序

> **2026-09-02 定案：不做本地備案。** demo 預設遠端服務與網路正常。下列切換順序保留為紀錄，
> **本地 STT（sherpa-onnx）不會啟用**；雲端 TTS 備案（`gpt-4o-mini-tts`）**從未實作**，`src/` 裡沒有對應程式碼。
> 實際發生過的失效是 ElevenLabs 額度耗盡（2026-08-31，STT 與 TTS 同一把 key 同時死），
> 處理方式是兩個帳號輪替，不是切備案。理由見 `development-plan.md` 失敗保險表。


主線是 ElevenLabs（第二節）。若需切換：

1. **STT 備案**：AssemblyAI streaming——若之後需要單麥 diarization，它是唯一現成的（串流 diarization 與轉錄同一條 WebSocket，即時標記最多 10 人，會隨上下文回頭修正標籤）
2. **STT 本地備案**：sherpa-onnx Zipformer 串流模型（上表）
3. **TTS 備案**：OpenAI `gpt-4o-mini-tts`（雲端，2026-08-28 起取代原訂的本地 Qwen3-TTS）

參考數字：ElevenLabs Scribe v2 Realtime 150ms；Deepgram Flux 與其並列 end-of-speech 延遲最低；Speechmatics 建議 voice agent 從 1.5 秒起跳（非 plug-and-play 的超低延遲）。

---

## 四、最硬的約束：延遲

整條鏈加起來：

```
STT 出字 150–300ms
  + LLM 判斷該不該插話  0.3–2 秒  ← 最大的一塊
  + TTS 首字 150–500ms
  ────────────────────────────
  ≈ 1–3 秒
```

**插話晚三秒，話題已經過去了，那個介入就是錯的。**
這是這個題目真正的技術核心，比 diarization 難得多。

### 三個解法，都要用上

1. **預先生成，只等放行。** 這正是 Inner Thoughts 的精神——想法是平行持續產生的，不是等到要說才生。候選發言隨時備著，決策只做「要不要放出去」，延遲就只剩 TTS 的 150ms。（此項已被兩次呼叫的實作取代，見文首狀態。）
2. **分層決策。**
   - 發言超時、議程超時、有人被冷落、全場沉默 → 規則判斷，零延遲
   - 離題、重複、假共識、僵局、事實錯誤 → 才走 LLM
3. **快慢兩個模型。** 小模型判斷「值不值得開口」，大模型準備「開口要說什麼」。

---

## 五、音訊來源與平台整合

> 2026-08-24 查證後改寫。**Discord 的發現改變了賭注 #1 的性質。**

### 核心設計：把音訊來源抽象成介面

核心 agent 完全不知道自己在哪個平台。

```
音訊 I/O 適配層（可抽換）
  ├─ 檔案       ← 開發測試，可重複跑同一段錄音   ★ 最先做這個
  ├─ 本地麥克風  ← 實體會議、demo 主線
  ├─ Discord     ← 每人一軌，免費 diarization
  └─ Meet bot    ← roadmap，不做
       ↕ 統一介面：音訊流入 ／ 音訊流出 ／ 誰在講話
核心 agent
```

**「檔案」那條一定要先做。** 調插話門檻時需要拿同一段錄音反覆跑、比較不同參數的結果。
每次都真的開一場會來測是不可能的——這條做好了，開發速度差好幾倍。

### 三個平台的結論

| 平台 | 結論 | 理由 |
|---|---|---|
| **實體會議** | **demo 主線** | 一台筆電＋麥克風。最簡單、延遲最低、零平台限制 |
| **Discord** | **最佳整合路徑** | 見下方——**免費解掉 diarization** |
| **Google Meet** | **放棄** | 官方 API 對 demo 不可行，見下方 |

### Discord：每人一軌，diarization 問題直接消失

[`discord-ext-voice-recv`](https://github.com/imayhaveborkedit/discord-ext-voice-recv) 提供 `VoiceRecvClient.get_speaking()`，
**直接告訴你哪個成員正在講話**——因為 Discord 語音本來就是每個使用者一條獨立音訊流。

→ **不需要做聲紋辨識，平台就給你答案。**

⚠️ **風險**：官方 `discord.py` 至今未合併語音接收（[PR #6507](https://github.com/Rapptz/discord.py/pull/6507)），
必須用這個第三方 experimental 套件。版本推進到 `0.3.1a128`，持續維護中。**第一天實測。**

### Google Meet：官方 API 對 demo 不可行

Meet Media API 有一條致命限制：

> **Google Cloud 專案、OAuth 主體、以及「會議中的所有參與者」都必須加入 Developer Preview Program。**

現場評審不可能全部先去申請 Google 開發者預覽。其他限制：

- 虛擬音訊流有數量上限（要開剛好三個 audio transceiver），參與者可能超過
- 會議有加密或浮水印就不能連
- 有未成年帳號在場會被拒

**替代路徑**：browser bot（Playwright 開瀏覽器用一個帳號加入 Meet ＋ 虛擬音訊裝置）——
Otter、Fireflies 那類服務的做法，可用瀏覽器自動化實作。
但脆弱、要處理虛擬音效卡、會議裡會多一個機器人參與者。
**留在簡報 roadmap，不要花 12 天的力氣在這。**

### 實體會議的採集

- **每人一支麥／一台裝置**：每個聲道就是一個人，diarization 問題同樣消失
- **單麥 + diarization**：技術上漂亮，但風險高

**建議：多麥為主，單麥辨識當加分項，不要當必要條件。** demo 現場配麥很正常。

---

## 六、第一天必須驗掉的三件事

沒驗這三件事，後面全是空的。

1. ~~**中文即時 diarization 準不準**~~ → 🔽 **降級（08-24）：可繞過，不再是賭注。**
   Discord 每人一軌、實體會議每人一支麥，兩條路都不需要聲紋辨識。
   單麥 diarization 改列為加分項。**改成要驗**：`discord-ext-voice-recv` 這個 experimental 套件能不能穩定收音並回報 speaking 狀態。
2. ~~**`thoughtful-agents` 能不能用**~~ → ✅ **已驗（08-24）：能用但要改**，見 [`interruption-design.md`](./interruption-design.md)
3. **端到端延遲的真實數字**——拿自己的會議錄一段，把整條鏈跑一次，量出來。
   ⚠️ 但注意：earcon 策略（見 `interruption-design.md` 第五節）已經大幅降低延遲的殺傷力——
   提示音零延遲播放，人停下來的 0.5–1 秒剛好夠 TTS 生成。**這條從致命降為重要。**
4. ~~**（08-24 新增）ElevenLabs Conversational AI 的 turn-taking 能否關閉**，改由我們的決策層控制何時開口。
   可以 → 借用它整套 STT+TTS+串流基礎建設；不行 → 自己用 Scribe Realtime WebSocket + TTS streaming 組。**這個答案決定語音管線的工程量差一倍。**~~
   → **已不需要驗證（2026-08-28）**：決策層自建完成，語音管線確定走 Scribe Realtime WebSocket + TTS streaming 自組。

**新的第一順位風險**：不再是「聽不聽得清楚」，而是 **`interruption-design.md` 那套評分準不準**。
打斷的方式已經有解，判斷失準沒有解。

---

## 七、尚未決定 / 未撰寫

- 權力邊界：AI 能裁決到什麼程度，哪些事必須交回人類（不定清楚，demo 會失控）
- 12 天里程碑排程
- 四人分工
- Demo 五分鐘腳本與失敗保險

---

## 參考來源

- [AssemblyAI 即時 STT 與 diarization](https://www.assemblyai.com/blog/best-api-models-for-real-time-speech-recognition-and-transcription)
- [Speaker diarization 方案比較](https://www.assemblyai.com/blog/top-speaker-diarization-libraries-and-apis)
- [STT 供應商 2026 獨立比較](https://www.coval.ai/blog/best-speech-to-text-providers-in-2026-independent-benchmarks-and-how-to-choose/)
- [ElevenLabs barge-in 與 turn-taking](https://deepgram.com/learn/elevenlabs-barge-in-interruptions-turn-taking)
- [ElevenLabs Agents 2026](https://aividpipeline.com/blog/elevenlabs-agents-guide-2026)
- [thoughtful-agents](https://github.com/xybruceliu/thoughtful-agents)
