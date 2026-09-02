# 產品定義：主席，不是助理

> 建立於 2026-08-23。這份文件記錄「為什麼是這個定位」，避免下次重新爭論。

---

## 一、競品盤點（2026-08-22 查證）

### 這個題目已經被微軟直球佔了

**Microsoft Teams Facilitator Agent** 2026 年已 public preview：

- 會議中自我介紹、宣讀議程、**用計時器追蹤進度**
- 即時記錄、擷取任務、**主動提出建議**
- 官方定位就是「不只是轉錄工具，而是像一個 AI 參與者，聆聽、理解、引導群體」
- Teams Rooms（實體會議室）也在支援範圍

其他：Zoom AI Companion 3.0 有 agentic 功能；Google Meet 從 2026-02 起三人以上會議預設自動產生筆記與待辦。

### 學術這邊也滿了

| 我們想做的 | 已有研究 |
|---|---|
| 適時插話 | CHI 2025《Proactive Conversational Agents with Inner Thoughts》 |
| 引導每個人發言 | 參與度即時監測 + 輕量提示降低發言失衡，已被驗證有效 |
| 沒共識時給建議 | Microsoft Research ProMediate、ACM TOCHI CLARA |
| 共識太快時要挑戰 | CHI 2025 已做過 AI voice agent 當 devil's advocate |

**結論：功能點層面沒有一塊是新的。差異化不能建立在功能清單上。**

---

## 二、那道縫：全都是「助理」，沒有一個是「主席」

看完所有產品與論文，它們有一個共同點：**沒有一個行使權力**。

微軟的 Facilitator 會記筆記、會提醒議程、會建議，但它：

- 不會打斷講了八分鐘的副總
- 不會說「這個離題了，回來」
- 不會在僵持不下時說「今天沒共識，我判定延到下次，理由是這樣」

**企業軟體不敢做這件事，因為得罪客戶。而這恰好是問題的核心。**

### 差異化的完整答案

被問「這跟 Teams Facilitator 差在哪」時，只有這一個答案：

> **它管議程，我們管群體過程。**
> **它有計時器，我們有群體過程模型。**
> **它會建議，我們會裁決。**

外加一層：**華語會議文化**的問題跟西方不同——階級壓抑、不直說反對、「沒人講話就當通過」。針對這個設計的主持人，國際大廠不會做。

---

## 三、專業知識層：Kaner's Diamond

這是讓「什麼時候該插嘴」從直覺變成**有依據的判斷**的關鍵，也是微軟那個沒有的東西。

### Diamond of Participatory Decision-Making（Sam Kaner, 1996）

```
發散 divergent  →  呻吟區 Groan Zone  →  收斂 convergent
```

**Groan Zone 是衝突與不適必然發生的中間階段，而且不能跳過。**
直接從發散跳到收斂，代表想法根本沒被充分理解，那種「共識」是假的。

### 同一個行為在不同階段的對錯完全相反

| 會議當下在哪 | 該做什麼 | 絕對不能做 |
|---|---|---|
| 發散期 | 逼出更多不同意見、點名沉默者 | 收斂、催促、下結論 |
| **呻吟區** | **忍住，讓衝突走完；只維持秩序** | 急著調解、強行推共識 ← **最常見的錯誤** |
| 收斂期 | 推進、裁決、封板 | 再開新議題 |

**微軟那個是議程與計時器導向（線性推進，時間到就催），它不知道什麼叫呻吟區**，所以它會在群體正需要衝突的時候去「幫忙收斂」——那是專業引導師眼中的低級錯誤。

### 待補（尚未查證）

- IAF（International Association of Facilitators）引導師能力框架
- Liberating Structures 等其他流派能不能再疊上去

---

## 四、已知風險（不淡化）

1. **「跟微軟差在哪」只有一個答案**，就是「它是助理、我們是主席」。這是**定位差異不是技術差異**。評審買不買單，是這題最大的不確定性。
2. **現場 demo 是真人即興**，AI 判斷失準（打斷錯人、裁決得莫名其妙）會當場很尷尬。成敗高度依賴那五分鐘。
3. 學術論文那麼多，若評審熟這塊，每個功能點他都見過。

---

## 五、Demo 構想

現場請四位評審坐下來開一場**真的會議**，給一個他們會有分歧的題目。AI 全程主持，五分鐘內結束並產出結論。

**評審不是觀眾，是被管的人**——他們會親身感受到被打斷、被點名、被裁決。

（完整腳本與失敗保險尚未撰寫。）

---

## 參考來源

- [Microsoft Teams Facilitator](https://support.microsoft.com/en-us/teams/copilot/facilitator-in-microsoft-teams-meetings)
- [Facilitator Agent 實測](https://www.preciofishbone.com/knowledge-hub/teams-facilitator-agent-meeting-assistant/)
- [Proactive Conversational Agents with Inner Thoughts (arXiv 2501.00383)](https://arxiv.org/abs/2501.00383)
- [ProMediate (Microsoft Research)](https://www.microsoft.com/en-us/research/articles/evaluating-proactive-ai-mediators-in-multi-party-conversation-with-promediate/)
- [CLARA (ACM TOCHI)](https://dl.acm.org/doi/10.1145/3786325)
- [Kaner's Diamond of Participation](https://www.chriscorrigan.com/parkinglot/the-diamond-of-participation/)
- [Facilitator's Guide to Participatory Decision-Making (Kaner) PDF](https://www.storypikes.com/workshops/PDFs/Facilitators%20Guide%20to%20Participation%20by%20Sam%20Kaner%20with%20Lenny%20Lind-Catherine%20Toldi-Sarah%20Fisk%20and%20Duane%20Berger-2007.pdf)
