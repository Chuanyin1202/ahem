# Prior Art：開源與論文盤點

> **狀態（2026-09-02）**：本文是 2026-08-27 的盤點，第一節狀態表反映當時。其後：Pipecat 經實測評估後未採用（管線自建）；Discord 每人一軌收音已在兩場真實會議驗證。論文部分對現行評分設計的影響仍有效。

> 建立於 2026-08-27。這個領域比想像中熱得多——**「何時介入」是公認未解的核心問題，而我們選中的正是它。**

---

## 一、開源

| 專案 | 是什麼 | 價值 | 狀態 |
|---|---|---|---|
| [**Pipecat**](https://github.com/pipecat-ai/pipecat) | 即時語音 agent 框架（Daily 維護） | ★★★ 語音管線骨架 | 待實測 |
| [**thoughtful-agents**](https://github.com/xybruceliu/thoughtful-agents) | Inner Thoughts 的官方實作 | ★★★ 大腦層基礎 | ✅ 已讀原始碼 |
| **AgentCall** | AI agent 用語音／視訊／螢幕分享加入 Meet／Teams／Zoom | ★★ 平台整合 | ⚠️ 未驗成熟度 |
| [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv) | discord.py 的語音接收擴充 | ★★ 每人一軌＝免費 diarization | 待實測 |
| Rapida、Natively、各家 meeting bot | 助理型（轉錄、筆記、摘要） | ✕ 定位不同 | 不採用 |

### Pipecat：借骨架 ＋ 自搭多人層

> ✅ **2026-08-27 實測驗證**（commit `c2c2429`，含實跑的 pytest，3 passed）。
> 本節先前的判斷有**兩處錯誤**，已依實測改寫，修正說明見下。

#### ✅ 最關鍵的問題已解：AI 主動打斷人類，零框架改動

**打斷不是硬編碼行為，是一個開關。**

```python
# processors/aggregators/llm_response_universal.py:1291
if params.enable_interruptions:
    await self.broadcast_interruption()
```

`enable_interruptions` 是 `BaseUserTurnStartStrategy.__init__` 的一等公民參數
（`turns/user_start/base_user_turn_start_strategy.py:56,78`），還能逐 turn 覆寫（同檔 `:197-228`）。

另一半同樣關鍵：**輸出端沒有「人在講話就不播 bot 音訊」的閘門**——
`transports/base_output.py:371-399` 只對 `InterruptionFrame` 有反應，沒有任何 user-speaking 檢查。

**實測證據**：關掉 `enable_interruptions` 後，在人類講話當下注入 `TTSSpeakFrame`，
該 frame 順利抵達下游 TTS，全程無 `InterruptionFrame`——人類持續發聲沒有反過來砍掉 AI 輸出。

**實作方式**：主席「何時開口」不屬於 turn strategy 的職責，它是 pipeline 外的一條 async loop：

```python
await worker.queue_frames([TTSSpeakFrame("你已經講三分鐘了，讓別人講。")])
```

Turn strategy 只需要做一件事：把 `enable_interruptions` 關掉。

> ⚠️ **修正紀錄 1**：本節先前寫「Pipecat 的打斷邏輯是 AI 讓步給人類，我們要反過來寫」——
> **這個技術障礙不存在**。設一個參數就好，不用自己寫。

#### ⚠️ 真正的風險在多人層（新的第一風險）

先前寫「原生多人支援」**不精確**。準確說法是：**傳輸層原生多人，turn/VAD/STT 層仍是一對一。**

| 問題 | 位置 |
|---|---|
| STT service 只有一個 `self._user_id`，每收到一個 `UserAudioRawFrame` 就覆寫 | `services/stt_service.py:447-452` |
| VAD 完全不看 `user_id` | `audio/vad/vad_controller.py:130,170` |
| **已知效能問題，至今 OPEN**：第二位參與者接入後延遲累積 **100–280ms/s**、agent 回答上一題 | [issue #3218](https://github.com/pipecat-ai/pipecat/issues/3218)（2025-12-10 開） |
| 官方**零**多人範例 | 全 repo 搜尋只有兩個註解字串 |

issue #3218 的社群 root cause：`VADAnalyzer.analyze_audio` 對每個 10ms frame 做一次
`run_in_executor` 執行緒往返——**這行在 HEAD 仍在**（`audio/vad/vad_analyzer.py:191`）。

**這直接威脅我們的四人場景。**

**可行架構（要自己搭）**：`ParallelPipeline` ＋ 每支線
`FunctionFilter(lambda f: f.user_id == "u1", filter_system_frames=True)` → 各自 STT ＋ aggregator。

> ⚠️ **坑**：`InputAudioRawFrame` 是 SystemFrame（`frames.py:1459`），`FunctionFilter` 預設放行所有
> system frame（`function_filter.py:67-69`）。**必須顯式帶 `filter_system_frames=True`**，否則過濾無效。

#### 其他確認事項

| 項目 | 結論 |
|---|---|
| **ElevenLabs** | 官方一等公民。`ElevenLabsRealtimeSTTService`（`stt.py:452`）正是我們驗證 #1 用的那支 API；TTS 有 WebSocket 串流版。**但 ElevenLabs STT 無 diarization**，若改走單流 diarization 路線需換 Speechmatics／Deepgram／AssemblyAI |
| **Krisp Interruption Prediction** | 能單獨用，但需商業授權（帳號＋`.kef` 模型檔＋API key），報價未知。**零成本替代**：`MinWordsUserTurnStartStrategy(min_words=3)` 用字數門檻擋掉「嗯」「對」 |
| Turn Strategies | 三類（Start／Stop／Mute）各自可插拔，繼承基底類別即可。策略是 list 依序評估，回傳 `STOP` 短路後續 |
| 現成事件 | `on_user_turn_started/stopped/idle` 等；`TurnTrackingObserver` 提供每輪時長 → **「講超過三分鐘」直接可用**（但只算 turn 不分人） |

> 💡 **一個反直覺的結論**：主席場景**其實不太需要** Krisp 的打斷預測。
> 那個模型是用來精細判斷「人類是不是真的要打斷 AI」——但我們要做的是**關掉**打斷，不是精修它。

#### 兩個要避開的坑

1. **`user_mute` 系列不要拿來當「不准打斷我」用**。它在 aggregator 層直接丟掉
   `InputAudioRawFrame` / `TranscriptionFrame`（`llm_response_universal.py:1158-1172`），
   **會議逐字稿會斷**。
2. 想讓主席發言「不可被打斷」：`UninterruptibleFrame` 是 mixin（`frames.py:147`），
   `TTSSpeakFrame` 預設沒帶，需自定 `class ChairTTSSpeakFrame(TTSSpeakFrame, UninterruptibleFrame)`。
   （讀碼判斷可行，**未實測**）

#### 定位

**借**：transport、frame pipeline、TTS service、output transport、打斷機制、metrics
**自搭**：多人音訊分流與說話人歸屬層（把 Pipecat 的 aggregator 當「單人 turn 偵測器」用，一人掛一個）

估計省下約 **六到七成**工程量，省在 TTS／打斷／transport 這些細節極多但無差異化價值的部分。

#### 尚未驗證（決策前要補）

1. **4 路並行 VAD 的實際 CPU 成本**——issue #3218 的成因仍在 HEAD，但未實測四路延遲。
   **這是唯一可能推翻「借骨架」結論的因素**，正式決策前應做一次四人實測
2. 主席自己的 TTS 輸出會不會被 transport 回灌成 user 音訊（**自己打斷自己**）——需實測
3. `UninterruptibleFrame` mixin 套在 `TTSSpeakFrame` 上是否可行
4. Krisp IP 實際費用

---

## 二、論文

| 論文 | 相關度 | 為什麼重要 |
|---|---|---|
| [**To Facilitate or not to Facilitate**](https://arxiv.org/abs/2607.28643) | ★★★ | **直球命中「何時介入」**，見第三節 |
| [**PTFA**](https://arxiv.org/pdf/2503.12499) | ★★★ | 實作踩過的坑，可直接避開 |
| [Overhearing LLM Agents: Survey, Taxonomy, Roadmap](https://arxiv.org/pdf/2509.16325) | ★★ | 我們的 agent 屬於 **overhearing agents** 這個已命名的領域，有現成分類法 |
| [ProACT](https://arxiv.org/pdf/2607.03730) | ★★ | breakdown-aware proactive agent，對應僵局偵測 |
| [Bringing Everyone to the Table](https://arxiv.org/html/2508.08242) | ★★ | LLM 引導群體決策實驗，對應「點名沉默者」 |
| [Who Speaks Next?](https://arxiv.org/pdf/2412.04937) | ★★ | 多人 AI 討論的 turn-taking systematics |
| [Toward Agentic Governance](https://arxiv.org/html/2606.00603v1) | ★ | 什麼形塑 agent 在公共論壇的介入 |
| [Shall We Dig Deeper?](https://arxiv.org/html/2509.23327) | ★ | 非同步討論的知識共構策略 |
| [Proactive Conversational Agents with Inner Thoughts](https://arxiv.org/abs/2501.00383) | ★★★ | 機制層基礎，見 `interruption-design.md` |
| [ProMediate](https://www.microsoft.com/en-us/research/articles/evaluating-proactive-ai-mediators-in-multi-party-conversation-with-promediate/) / [CLARA](https://dl.acm.org/doi/10.1145/3786325) | ★★ | 見 `product-definition.md` |

---

## 三、三個對設計有立即影響的發現

### 發現 1：LLM 的介入傾向會**兩極崩壞**，不只是「過度熱衷」

> ⚠️ **2026-08-27 修正**：讀完全文後，先前「LLM 天生過度熱衷插話」這個單一結論**不完整**。
> 真實情況是**兩個方向的失敗同時存在**，而且論文指出過去文獻忽略了反向那一半。

Table 4（預測「該不該介入」，全資料集）的實際數字：

| 模型 | Recall | F1p | F1n | 行為 |
|---|---|---|---|---|
| OLMo7B | **0.986** | 0.476 | **0.007** | **幾乎每則都說要介入** |
| Qwen32B | **0.105** | 0.121 | 0.728 | **幾乎不介入** |
| OLMo32B | 0.149 | 0.162 | 0.716 | 幾乎不介入 |
| LLaMa70B | 0.197 | 0.194 | 0.659 | 幾乎不介入 |
| **ModernBert** | 0.555 | **0.478** | 0.609 | 最均衡 |

**同家族、不同大小的模型行為可能完全相反。**

→ **實務結論：模型選擇會劇烈影響介入頻率，必須實測，不能假設。**
我們原本規劃「高頻評分用 Haiku、階段判斷用 Sonnet」——**這個選擇必須用同一段錄音實測介入頻率**，
不能因為「小模型比較便宜」就直接用。

### 發現 1b：一個可直接搬的崩潰守門規則

論文的做法：**F1p 或 F1n < 0.15 即視為模型行為崩潰**，該結果畫上刪除線、排除於比較之外。
且**刻意分開報正負類 F1，不用單一 macro-F1**——因為單一數字會同時掩蓋
「一律介入」與「一律不介入」兩種崩潰。

→ 我們的評估腳本直接照抄這條守門規則。

### 發現 1c：「不介入」要做成獨立選項，不是殘差

論文 Finding 3：**決定何時「不要」介入很容易，決定何時「該」介入很難**——人類與模型皆然。
（精確版本：人類在「不介入」的判斷上明顯比 LLM 更確定；負向增強則雙方都高度不確定。）

論文的問卷設計據此把三軸做成**三欄必填、彼此不互斥**：

```
Positive Reinforcement : 1-5   （鼓勵某種行為）
Negative Reinforcement : 1-5   （抑制某種行為）
No Reinforcement Needed: 1-5   ← 獨立評分，不是「前兩者不夠高」的殘差
```

→ **我們的評分要照做**：現行設計是「分數過門檻就介入」，等於把「不介入」當成殘差。
改成獨立評分是有實質差異的防呆。

### 發現 2：PTFA 的兩個失敗模式，直接避開

- 有時 **8 分鐘**才做出第一次介入，而參與者早已產出一堆想法
- 有時**每個人講完就插話**，不讓參與者把想法發展完

**這正是 Kaner Diamond 要解的問題**（發散期該閉嘴、呻吟區該忍住、收斂期才推進）。
等於別人替我們驗證了「沒有階段感知會怎麼死」。

### 發現 3：小分類器比 LLM prompting 可靠，但天花板很低

論文建了 **PEFK 語料庫**，並發現 ModernBert 分類器在 5 個資料集中**全部拿下最高 F1p**，勝過所有 LLM。

**但「上限偏低」的具體數字很低：**

| 任務 | 最佳成績 |
|---|---|
| **prediction**（該不該介入） | ModernBert **F1p = 0.478**；所有模型巨集平均 0.300 |
| **detection**（這則是不是引導師說的，用來估天花板） | ModernBert **F1p = 0.594**；巨集平均 0.450 |

論文歸因（Finding 5）：把「引導」定義成「引導師說過的話」這個定義本身注入雜訊，架住了天花板。

> ⚠️ **論文的結論對我們是壞消息，必須誠實面對**：
> 現成 LLM **「不能當自主引導者」**（原文：rendering them unusable as autonomous facilitative agents）。

**為什麼這不是致命打擊——但期待必須調整：**

1. 他們的判準是 Def.2（引導師說過的話＝該介入），論文自己承認這定義有雜訊
2. 他們的場景是**非同步文字討論**，缺乏我們有的確定性訊號：**誰講了多久、誰沒說話、議程剩多少時間**
3. **我們的快路完全不受這個天花板限制**——發言超時、沉默時長、議程時間都是計時器，不需要 LLM 判斷

> **→ 這反而強化了快路／慢路分流的設計決定。**
> 論文證明了「靠 LLM 判斷何時介入」不可靠，那就更該把所有能用規則的都用規則。
> 對 demo 也有意義：**最穩的介入類型是規則型的**（超時、冷落）。

**仍然不做微調**，理由：
1. 天花板已知偏低（F1p ≈ 0.48），投入產出比差
2. PEFK **本身不散布**，只釋出建置程式碼（GPLv3）；Fora 子集不公開、需申請
3. 時間不夠

> ⚠️ **兩則修正紀錄**
> - 先前說「不做 LoRA 是因為訓練資料不存在」——**不正確**，PEFK 存在。
> - 先前說「PEFK 是非同步文字討論的資料」——**不精確**。它有 **3 個同步口語子集**
>   （IQ2 正式辯論、WHoW 廣播電視、Fora 故事分享），是逐字稿，打斷與搶話會轉錄成獨立 turn，
>   約 1/3 發言出自引導師。**這三個才是跟我們最對口的資料**，只是引導師角色
>   （辯論主持／節目主持／故事引導）與「會議主席」不完全等價。

---

## 四、這個盤點對專案的整體意義

1. **選題選對了**：「何時介入」是這領域公認未解的核心問題，不是我們自己想出來的偽命題
2. **定位站得住，但論證要換一個**（見下）
3. **有現成的坑可以避**：PTFA 的兩個失敗模式、LLM 介入傾向的兩極崩壞
4. **有現成的骨架可以站**：Pipecat（管線）＋ thoughtful-agents（大腦）
5. **但天花板是真的**：學界最好的「何時介入」判斷也只有 F1p ≈ 0.5，
   → 能用規則的一律用規則，LLM 判斷當加分不當主力

### 關於第 2 點：一個必須修正的論證

本文先前寫「所有語音框架的打斷邏輯都是 AI 讓步給人，**因為技術上做不到**，所以沒人做主席」。

**前半句作為技術論據不成立**——Pipecat 實測證明 AI 主動打斷人類只需要關一個參數。

> **產品定位沒有動搖，但論證方式要換：**
>
> ❌ 舊：沒有人做主席，**因為框架做不到**
> ✅ 新：技術上一直做得到，**但沒有人做**——因為那是產品選擇，不是技術限制

而這**反而強化了原本的論點**：既然技術障礙不存在，「為什麼市面上全是助理、沒有主席」的答案就純粹是
**沒有人敢讓 AI 得罪使用者**——這正是 `product-definition.md` 從一開始的核心主張。

**對 demo 的實際影響**：被問「這跟 Teams Facilitator 差在哪」時，
不要答「技術上他們做不到」（會被懂的人當場戳破），要答**「他們不敢，我們敢，而且我們有群體過程模型知道什麼時候該敢」**。
