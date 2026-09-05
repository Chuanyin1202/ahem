# 已知但延後處理的缺陷／跳過項目

> 依 CLAUDE.md「已知但延後處理的缺陷一律進追蹤清單」規則建立。每次開新一批施工前先掃一眼這裡有沒有能順手處理的。

欄位：發現時間／發現於哪一批／內容／根因（若已知）／影響範圍／狀態。

---

## 1. 【已結案，判斷相反】Track C 的 Enter 鍵補丁不是多餘的，審計那次的判斷是錯的

- **發現時間**：2026-09-05（獨立審計標記待刪）→ 同日再次查證（前台）判定不能刪
- **經過**：Track C 獨立審計（agent `a7a39b09ef2807203`）宣稱「用零JS對照組實測，Chrome原生`<summary>`本來就支援Enter鍵，這8行是解一個不存在的問題」，Zeal 指示直接刪掉。前台刪掉後，**用不受沙盒干擾的方式**重新對照一個完全空白、零JS的`<details>/<summary>`測試 Enter 鍵：連續兩種CDP事件寫法皆為 `[False, False, False, False]`（Space則正常切換 `[False, True, False, True]`）——**證實這台機器這個版本的Chrome，原生`<summary>`確實不支援Enter鍵**，跟審計當時的結論相反。已把這8行加回來，並重新驗證加回後Enter與Space皆正常交替（`[True, False, True, False, True]`）。
- **根因（回頭看兩邊為什麼會兜不起來）**：懷疑審計當時的「零JS對照組已支援Enter」測試本身也踩到類似的環境干擾（本次前台第一次測試在有沙盒限制的環境下執行，結果詭異地連Space都失效；改用不受限的方式跑，結果才穩定重現）——**這是一個「兩次獨立測試同一件事，其中一次的測試環境本身不可靠」的案例**，不是誰粗心，而是這類鍵盤模擬測試對執行環境很敏感，同一個判斷最好在正式合併前多重驗證一次，不能只憑一次對照組結果就下定論。
- **影響範圍**：`src/meeting_host/spectator/index.html` 的收合抽屜鍵盤操作。
- **狀態**：**已結案，但程式碼已隨 Track D 合併一起移除**——2026-09-05 Track D
  rebase 進 main 時，那顆 `<details class="drawer" id="group-dynamics">` 收合面板
  整個被 Track D 的滑入式覆疊抽屜（`.dyn-handle` / `.dyn-drawer`）取代，掛在
  `<summary>` 上的那 8 行 Enter 補丁也就跟著它一起消失了。**這不是把上面的結論
  推翻**：新的把手用的是真正的 `<button>` 元素，Enter／Space 都是瀏覽器原生行為，
  本來就不需要補丁。這一項留著是為了保存「原生 `<summary>` 不吃 Enter」這個實測
  結論——**未來若有人在這個專案裡再用 `<details>/<summary>` 做可鍵盤操作的開關，
  要記得補 Enter，不要重蹈那次審計的判斷**。

## 2. Track C 背景圖畫框用 `background: transparent`，建議改回 `var(--bg)`

- **發現時間**：2026-09-05
- **發現於哪一批**：Track C 獨立審計（agent `a7a39b09ef2807203`）
- **內容**：`body`的背景設成`transparent`讓水彩圖透出來，效果跟設成`var(--bg)`完全相同（審計實測對照），但`transparent`在圖片萬一沒渲染成功時會退化成純白，`var(--bg)`則會退回沙色色票，更安全。
- **影響範圍**：`src/meeting_host/spectator/index.html`，一行CSS。
- **狀態**：待處理。demo之後再改，風險極低但不急。

## 3. Track C 鍵盤Tab到收合按鈕時焦點框偏硬

- **發現時間**：2026-09-05
- **發現於哪一批**：Track C 獨立審計（agent `a7a39b09ef2807203`）
- **內容**：瀏覽器預設的黑色`:focus-visible`外框在沙色版面上視覺略突兀，只有鍵盤操作（非滑鼠）才會出現。
- **影響範圍**：視覺細節，不影響功能。
- **狀態**：**部分過期**。原本指的那顆 `<summary>` 已隨 Track D 合併移除（見第 1 項）。
  Track D 的 `.dyn-handle` / `.dyn-close` 自己有寫
  `:focus-visible { outline: 2px solid var(--accent); }`，用的是既有的沙色系
  accent 色票、不是瀏覽器預設黑框，所以這個問題在新抽屜上不成立。優先度最低。

## 4. 【已解決】Kaner 菱形節奏視覺化沒做

- **發現時間**：2026-09-05
- **發現於哪一批**：Track D（觀戰畫面補完整版）
- **內容**：設計提案 `MainWithSidebar.dc.html` 第 156-169 行有一個 Kaner 參與式
  決策模型的菱形 SVG（發想/拉鋸/決定三段式進度圖，含虛線分隔與目前位置指針），
  放在「群體動力」抽屜裡的「會議節奏」子區塊。工作單 D3 明講這是加分項、時間
  不夠可以跳過。這批確實時間不夠，跳過了。
- **根因**：純粹是優先序取捨，不是技術卡關——SVG 座標是寫死的（`M 6 48 L 160 10
  L 314 48 L 160 86 Z` 這類），要把它對應到目前階段狀態變數（發想/拉鋸/決定）
  需要重新設計座標插值邏輯，比直接搬既有四塊資料多一截工。
- **狀態**：**2026-09-05 Track F 已補做並驗證**。新增 `renderKanerDiamond()`
  （`src/meeting_host/spectator/index.html`），插在「群體動力」抽屜標題列之後、
  既有四塊（KPI／時間軸／主席的思考／發言分佈）之前。跟提案圖的差異是**改用
  真實資料算座標**，不是照抄提案圖的寫死值：
  - 三個階段區的分界點預設把 `duration_min*60` 均分三等份，一旦
    `state.phaseTransitions` 出現第一筆 `to==="呻吟區"`／`to==="收斂期"`，
    改用那筆真實 `t`。
  - 目前位置的圓點與線用 `state.serverNow` 對總時長的比例即時算，掛在
    `handleEvent()` 的 `state.serverNow = ev.t;` 之後呼叫，不另開計時器；
    "meeting"／"phase" 兩個 case 各自推進 `phaseTransitions` 之後，多呼叫一次
    `renderKanerDiamond()` 撿最新分界點（否則會晚一個事件才更新，`serverNow`
    本身的位置不受影響）。
  - 填色路徑、兩條分隔虛線都用同一組 `upperY(x)`/`lowerY(x)` 直線內插公式現算，
    貼齊菱形邊緣，不是寫死 y 座標。
  - 三欄文字標籤的 `grid-template-columns` 三個 fr 值跟著真實區段時長成比例。
  - 說明文字取 `phaseTransitions` 最後一筆真實紀錄；完全沒發生過轉換時改講
    真話「尚未切換階段，目前處於「X」」，沒有照抄提案圖的示範假句。
  - 顏色收進 `:root` 的 `--kaner-fill`(`#EAE1D2`)／`--kaner-track`(`#C9BEAD`)
    兩個新變數；另外三色（外框白底／描邊／目前位置標記）直接重用既有
    `--card-bg`／`--dim`／`--text`，沒有新增第二份定義。
  - **驗證**：Playwright 灌 `examples/synthetic-phases.events.jsonl`（真實回放，
    在 t=180 切「呻吟區」、t=300 切「收斂期」，duration 900 秒，均分點應為
    300/600，跟這兩筆真實時間明顯不同）——DOM 讀出的分界線 x 座標
    67.6/108.7、目前位置圓點 108.7，跟純數學公式算出的期望值 67.60/108.67/108.67
    完全對上；`window.__spectator.handleEvent(...)` 灌一筆假 `phase` 事件
    （t=345，收斂期→發散期）後，圓點移到 124.1（期望 124.07）、粗體欄位換成
    「發想」、說明文字換成「05:45 由決定進入發想」——分界線與三欄寬度比例
    因為這筆事件的 `to` 不是「呻吟區」或「收斂期」而保持不變，這是設計上的
    正確行為（分界點只認第一次切進拉鋸/決定的那一筆）。全程 `page.on
    ("pageerror")` 零筆例外。`pytest tests/` 564 passed / 21 skipped / 2 xfailed，
    跟施工前基準一致。截圖見
    `scratchpad/track_f_screens/`（同批交付對話的暫存檔，未入庫）。

## 5. 【已修復】AI 觀察面板「留意類」用僵局介入次數代理「未解決事項數」

- **發現時間**：2026-09-05
- **發現於哪一批**：Track D
- **內容**：工作單 D2 第 3 條原文舉例的「留意類」規則是讀「未解決事項數」（Track
  A 做的即時決議/待辦/未解決面板 `#minutes-live` 裡的資料）。但施工當下這個
  worktree 裡沒有 Track A 的成果（見下一項的根因說明），沒有這份資料結構可讀。
  改用既有的 `state.spokenKinds["僵局"]`（僵局介入次數）當代理值。
- **根因**：Track A/B/C/D 各自在獨立的 git worktree 施工，這個 worktree
  （`agent-a01c2165924ad3f87`）是從 main 的一個較早快照（`f025fef`）切出去的，
  Track A 對 `index.html` 的改動（新增 `#minutes-live` 面板）當時還沒進到那個
  快照裡，所以這裡看不到。這是專案的 worktree 隔離架構本身的限制。
- **實際暴露出來的問題（合併時覆核才發現，比原記錄更嚴重）**：代理值
  `state.spokenKinds["僵局"]` 是**只增不減**的累計次數——僵局後來被談攏了它也
  不會回頭扣。所以它會跟同一塊面板裡 `computeJudgment()` 的「僵局已解除，對話
  進入收斂節奏」**同時出現、字面互相矛盾**（例：第 2 分鐘有過一次僵局，到第 8
  分鐘判斷類說「僵局已解除」，留意類同時說「仍有 1 項僵局介入未見後續共識」）。
  這不只是「代理值不夠精確」，是畫面上看得出來的錯。
- **狀態**：**2026-09-05 Track D rebase 進 main 時已修復**。`computeNotice()`
  （`src/meeting_host/spectator/index.html`）改讀
  `state.minutesLive.unresolved.length`——Track A 那份 LLM 每
  `MINUTES_PREVIEW_INTERVAL_S`（90 秒）重算一次的當下快照，收掉的項目會真的消失，
  不會只增不減。文案同步改成「仍有 N 項未解決事項，剩餘 M 分鐘」。
  🔴 **已知的行為改變**：`state.minutesLive` 在 `--no-llm` 模式與開場前幾次預覽
  之前是 `null`（`watch_minutes` 掛在 `not args.no_llm` 底下，且要求
  `MINUTES_PREVIEW_MIN_UTTERANCES` = 6 筆發言才開始送），這時候留意類不提醒。
  這是刻意的：沒有資料就不講話，比拿一個語意不同的數字硬湊一句誠實（同一情況下
  Track A 的 `#minutes-live` 面板本來也是顯示「尚無資料」）。留意類只在會議剩餘
  時間低於 30% 時才可能觸發，那個時間點預覽通常已經送過好幾輪。

## 6. 【已結案】Track A/C 在本 worktree 未合併，D2/D3 對照物不存在

- **發現時間**：2026-09-05（開工前棕地探勘階段）
- **發現於哪一批**：Track D
- **內容**：工作單背景段提到「Track A 做的 `#minutes-live`」與「Track C 做的
  inline `<details>`」，開工前逐行檢查 `src/meeting_host/spectator/index.html`
  （1336 行全文）確認兩者都不存在。
- **根因**：同第 5 項——worktree 隔離，Track D 從 main 的較早快照分岔出去。
- **狀態**：**已結案**。2026-09-05 Track D 以 `git rebase main` 併回主線，四處
  衝突全部人工合併完成：Track A 的 `#minutes-live` 與 Track D 的 AI 即時觀察
  面板兩塊獨立並存（前者是結果／待辦，後者是過程／動態訊號），Track C 的 inline
  `<details>` 抽屜連同它的 Enter 補丁 `<script>` 與四組只有它在用的 CSS
  （`.drawer`、`.drawer > summary`、`.drawer-body`、`.section-secondary`）一併
  移除，由 Track D 的滑入式覆疊抽屜取代。
