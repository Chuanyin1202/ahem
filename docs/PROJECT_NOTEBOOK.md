# 專案小本本 — Ahem 觀戰畫面 UI Upgrade

> 索引，一批一行；細節寫在本檔（批次量不大，暫不拆分檔）。

- 2026-09-05 — Track B（水彩背景圖）完工，合併於 `de4d5ef`。細節見下。
- 2026-09-05 — Track A（會議產出即時預覽）完工＋獨立審計抓到一個競態並修復，已合併。細節見下。
- 2026-09-05 — Track C（收合抽屜＋配色分層＋背景圖可見度）完工，獨立審計放行（無阻塞性問題），已合併。細節見下。
- 2026-09-05 — Track B 補做獨立審計（原本合併時漏了這一關）。細節見下。
- 2026-09-05 — Track D（主席狀態徽章＋AI 即時觀察面板＋群體動力滑入抽屜）完工。細節見下。
- 2026-09-05 — Track D 以 `git rebase main` 併回主線，4 處衝突人工合併＋覆核，順手修掉 DEFERRED_DEFECTS 第 5 項。細節見下。
- 2026-09-05 — Track E（水彩背景改滿版半透明、會議摘要改白卡片，逐值比對提案圖修正）完工，已合併於 `0cb37c8`。
- 2026-09-05 — Track F（補 Kaner 菱形「會議節奏」視覺化，用真實資料算座標＋核對抽屜排版/圖示）完工。細節見下。
- 2026-09-05 — Track G（AI 即時觀察加第四類「心聲」，真的接 LLM＋保險栓 `--no-critique`）完工，本 worktree 內已 commit，尚未合併主線。細節見下。
- 2026-09-06 — Track H（心聲補真統計/介入紀錄＋長會議逐字稿壓縮，解決 DEFERRED_DEFECTS 第 7 項）完工，本 worktree 內已 commit，尚未合併主線。細節見下。

---

## 2026-09-05 Track B：水彩背景圖

- **時間**：2026-09-05 晚間
- **誰做的**：builder agent（worktree隔離，agent id `aa3bfa1ba2ead952e`），Claude Sonnet 5 前台審核＋合併
- **做了什麼**：把 Zeal 核准的「折衷版」水彩素材加進 `src/meeting_host/spectator/index.html`，作為最底層背景圖

- **實測證據**：
  - 疊層測試（headless Chrome 最小重現案例）：原工作單指示的 `z-index:0` 會讓背景圖蓋住全部內容（`#view`/`.left`/`.right` 都沒設 `position`，一般流內容排在 z-index:0 定位元素之後）；改 `z-index:-1` 後正常。**下次任何人要疊一張全螢幕背景圖，先查頁面容器有沒有設 `position`，沒有就不能用 `z-index:0`，直接用 `-1`。**
  - 相對路徑 `src="assets/..."` 在真實伺服器會 404：`spectator.py` 只註冊 `/`、`/health`、`/events`、`/phase`、`/end` 五條路由，沒有靜態檔案服務。改用 base64 內嵌解決，原始 jpg 仍留在 `assets/` 資料夾備查。
  - 像素級 diff（改動前後 1440×900 截圖）：**逐位元組相同**——因為 `#view` 滿版不透明無縫隙，背景圖在目前正式版面上實測 **0% 可見**，不是「低調透出一點」，是完全看不到。
  - 互動元件（逐字稿欄/KPI卡/時間軸/判斷清單/階段選單/匯出/結束會議按鈕）像素與原始檔案完全相同，確認沒有被蓋住。
  - 既有 pytest 套件在這個環境跑不起來（缺 `aiohttp`／`playwright`），但用不相干的 `test_chair.py` 做對照組，一樣缺依賴 → 判定是既有環境缺口，不是這次改動造成的。

- **卡住或未完的**：
  - 背景圖目前 100% 不可見，是「準備好了但看不到」的狀態，不是完工可驗收的視覺效果。
  - 沒有真伺服器＋真瀏覽器的最後一哩驗證（環境缺依賴），只驗證到 headless 截圖與程式邏輯層級。

- **下一關該知道什麼**：
  - Track C（①收合面板＋②配色分層）動工時，**必須**順便讓 `.left`/`.right`/`#view` 之類的容器留一點縫隙或些微透明，否則背景圖永遠是擺好看的心理安慰，實際 0% 可見。這是 Track C 工作單裡明確要交代的一項，不能漏。
  - 這是 Zeal 已知情的技術判斷（超出原工作單字面指示的兩處偏離：z-index 數值、路徑改 base64），已在 commit message 記錄理由，未經 Zeal 事先核准，屬於施工者當場的合理技術修正，非範圍變更。

---

## 2026-09-05 Track A：會議產出即時預覽（`minutes` 事件加 `final` 欄位）

- **時間**：2026-09-05（決賽 Demo Day 前一晚施工）
- **誰做的**：builder agent（worktree隔離，agent id `a70f83c763ec1b3fd`）施工；delivery-auditor agent（`a0d2e71602ce6e96a`）獨立審計；Claude Sonnet 5 前台套用修復＋合併
- **做了什麼**：
  - `src/meeting_host/live.py`：新增 `Session.watch_minutes()` 背景迴圈（比照 `watch_glossary` 的形狀），每 `MINUTES_PREVIEW_INTERVAL_S`（預設90秒）重問一次 `minutes.py` 的 `_call_minutes_llm`，逐字稿累積到 `MINUTES_PREVIEW_MIN_UTTERANCES`（預設6句發言）以上才跑；發出 `minutes` 事件、`final: False`，只帶 `decisions/todos/unresolved/stances` 四個清單，不寫檔。`_emit_minutes`（正式收尾）加 `final: True`。`main_async` 在 `not args.no_llm` 底下掛上這個新 task。
  - `src/meeting_host/spectator/index.html`：右欄新增獨立區塊「會議產出（預覽）」（`#minutes-live`），前端 `case "minutes":` 依 `data.final` 分流——`true` 才鎖畫面／鎖匯出鈕（行為不變），`false` 只更新 `state.minutesLive` 並呼叫新的 `renderMinutesLive()`。空狀態顯示「尚無資料」。只用既有 CSS 變數，沒有動到既有4組（KPI／時間軸／主席的思考／發言分佈）。
  - `docs/design/README.md`：加一行「已知偏離」註記（這塊不在設計稿範圍）。
  - 新測試 `tests/test_minutes_preview.py`（原4項，加上審計後補的回歸測試共5項，全部 mock 掉 LLM）。

- **獨立審計抓到的真缺陷（阻塞性，已修復）**：
  - **競態**：`watch_minutes` 的 LLM 呼叫最長可能飛行到接近 90 秒；若這段期間會議剛好收尾（`summary()` 已經送出 `final:True`），飛行中的預覽回來後會補發一筆 `final:False`，排在正式版**之後**，導致①貼回 Discord 的會議記錄變空白 ②`--replay` 模式看不到總結 ③「匯出紀錄」按鈕永久顯示「會議結束後才有紀錄」（snapshot重播會照順序重跑，每次重新整理都壞）。審計員實測估計發生機率約每6~8場會撞一次。
  - **修復**（2行，前台套用）：`live.py` 的 `watch_minutes` 在 LLM 呼叫回來、emit 之前加一道 `if self.ending: continue`（`session.ending` 這個旗標在 `shutdown()` 送出正式版之前就會被設成 True）；`index.html` 把 `state.minutes = data` 移進 `if (data.final)` 分支內，做縱深防禦。
  - **驗證**：新增回歸測試 `test_preview_suppressed_once_session_is_ending`，先做拔除演練——把修復註解掉，測試真的紅了（證明測試有在測東西）；還原修復後，全套 **540 passed / 26 skipped / 2 xfailed / 0 failed**。

- **獨立審計逐條核實其餘聲稱**：測試數字535→539屬實、`final`分流真的接在生產路徑非死碼、右欄既有4組完全沒動、`spectator.py`/`minutes.py`皆0行變動、仿照`watch_glossary`寫法屬實（無鎖無共享可變狀態，`await`正確交出控制權不會卡住其他背景迴圈）、kill-switch（`MINUTES_PREVIEW_INTERVAL_S`設超大數字）有效且同時關掉上述競態、馬尾檢查抓到一個真的沒人用的欄位已標記（`stances`欄位全repo零消費者，暫留著給前端未來使用，非本次阻塞項）。

- **卡住或未完的**：無阻塞（原本1個阻塞項已修復並驗證）。已知的刻意簡化——預覽不寫檔（避免demo期間`meetings/`堆檔案）；`watch_minutes`不等`self.chair`就緒（比照`watch_glossary`，非疏漏）；前端沒有可跑的自動化測試框架（這個repo本來就沒有），用從真實原始碼逐字抽出配假DOM的手動驗證腳本代替。

- **下一關該知道什麼**：
  - 已合併回 main。`MINUTES_PREVIEW_INTERVAL_S`（90秒）與`MINUTES_PREVIEW_MIN_UTTERANCES`（6句）都是`live.py`頂部的模組常數，demo現場如果這個功能不穩，把前者調成很大數字（例如`999999`）即可關掉，不影響任何其他背景迴圈，且這個關法同時也是那道競態的另一層防護。
  - 若之後要幫這塊補設計稿，回頭看`docs/design/README.md`的偏離註記。
  - **疲勞計數**：`live.py`/`minutes.py`/`spectator/index.html`目前累計缺陷修復 1 筆（這個競態），離「同一層第3個缺陷要停下來質疑」還很遠，正常記錄即可。

---

## 2026-09-05 Track C：收合抽屜＋配色分層＋背景圖可見度

- **時間**：2026-09-05
- **誰做的**：builder agent（worktree隔離，agent id `ab4c360076fa05455`）施工；delivery-auditor agent（`a7a39b09ef2807203`）獨立審計；Claude Sonnet 5 前台合併
- **做了什麼**：`src/meeting_host/spectator/index.html`（+120/-49行，唯一改動檔案）
  - 用原生 `<details id="group-dynamics">`/`<summary>` 把 KPI 統計卡／SVG 時間軸／主席判斷紀錄／發言佔比四組包成可收合抽屜，`#minutes-live`（Track A）維持在抽屜外的獨立區塊。
  - 配色：KPI數字與部分標題改用既有 `--muted`，`#minutes-live`與判斷紀錄維持 `--text`，零新色碼。
  - 背景圖可見度：`body` 改留 14px padding 當畫框，`#view` 高度用 `calc(100vh - 28px)` 補償，讓 Track B 的水彩背景圖真的從四邊露出一圈水洗邊框。
  - 額外加了8行JS補「Enter鍵開關抽屜」，理由是誤判Chrome原生summary不支援Enter鍵（見下方審計發現）。

- **獨立審計結果：放行，無阻塞性問題**。逐條核實：抽屜結構正確（`#minutes-live`確實在外面，四組確實都在裡面，無遺漏無錯位）、配色零新色碼（掃描全部14個hex值皆為既有變數）、背景圖疊層數學正確（`#view` rect 分毫不差，像素取樣證實四邊真的透出水彩圖，無多餘縫隙無裁切）、540測試全過、既有告警系統與匯出按鈕命中皆未受影響、只改了這一個檔案。

- **審計順便抓到的技術細節（非阻塞，記錄備查）**：
  - 施工者聲稱「Chrome原生summary不支援Enter鍵」**經審計實測不成立**（零JS對照組一樣能用Enter切換）——那8行是在解一個不存在的問題。但審計也證實這8行**沒有壞處**（4次Enter、4次Space交替測試皆正確、無雙重觸發），且明天就要demo，**現況經實測正確，不建議此刻動它**。已記入下方 `DEFERRED_DEFECTS.md`，demo後再清掉這8行（馬尾判斷：net -19行）。
  - `background: transparent` 可以改回 `var(--bg)`，效果完全相同且更安全（萬一圖沒渲染會退回色票而非變成純白）——一個字的改動，同樣留到demo後。
  - 鍵盤Tab到收合按鈕時瀏覽器預設的黑色焦點框在沙色版面上略突兀——只有鍵盤操作才會看到，demo若全程用滑鼠操作不受影響，非阻塞。

- **卡住或未完的**：無阻塞。上述三個小發現均為刻意延後、非疏漏。

- **下一關該知道什麼**：
  - 已合併回 main。
  - **疲勞計數**：`spectator/index.html` 累計缺陷修復仍是1筆（Track A的競態），Track C本身是功能新增、審計未發現真缺陷，不列入計數。

---

## 2026-09-05 Track B 補做獨立審計

- **時間**：2026-09-05
- **誰做的**：delivery-auditor agent（`a67f144fc9f745ccc`）
- **為什麼要補做**：Track B（水彩背景圖）當初合併時，前台只自己複核就直接合併，沒有走完整的獨立審計流程——這跟其他批次的標準不一致，是Zeal當面追問「Track A、B都有審計通過了嗎」才發現的漏做項，當場補派。
- **結果：放行，無阻塞性問題**。七條當初的自稱全數獨立重驗（不是照抄施工報告數字）：base64內嵌的必要性（親自讀`spectator.py`路由確認無靜態檔案服務）、z-index:-1正確、pointer-events:none正確、當時100%不可見、Track C合併後互動元件仍0命中背景圖（3,110個取樣點）、只動了2個檔案皆屬實；540測試自己重跑兩次（Track C合併前後各一次）皆過；用真實aiohttp server打`/`確認圖片data URI完整未截斷、親眼看過解碼後的圖是真的水彩。

- **非阻塞發現**：①原始素材`bg-full-desktop-v3.jpg`在scratchpad已不存在，「這是Zeal核准的折衷版」目前唯一憑據是程式碼註解——**建議Zeal花十秒瞄一眼畫面確認圖對不對**；②Track B的CSS註解在Track C改版後過期（已修正，見上）；③`pointer-events:none`會讓`elementFromPoint`點擊測試對它完全隱形，這類測試永遠證明不了「沒被遮住」，只有像素比對才算數——記錄下來給以後同類驗收參考；④index.html因base64內嵌從69,770→315,501 bytes（+352%），改用靜態路由會更精簡，但demo後再做。

- **審計過程本身的插曲**：審計進行到一半main分支被Track C合併（HEAD從`c05b026`動到`af87934`再到`2030d1b`），一度讓同一組像素比對測試出現前後矛盾的數字；審核員自己多跑一組「同一畫面連拍兩張」的對照才抓到問題出在檔案本身變了、不是量測誤差，沒有把錯的數字報出來。**這是「審計跑的時候不要改稿」這條老毛病第二次發生**，下次派審計要考慮凍結分支或明確告知審計者本批可能被合併。

- **另一個要注意的**：審核員在main分支上沒有現成的`.venv`，是借用旁邊worktree的（Python 3.12.3），而這個repo的CI宣告用Python 3.13——**demo正式機器上機前建議確認一次實際跑的Python版本**，避免版本落差在最後一刻出狀況。

## 2026-09-05 15:19–15:41 — Track D：觀戰畫面補完整版（主席徽章／AI觀察／群體動力抽屜）

**誰做的**：Track D 施工者（worktree `agent-a01c2165924ad3f87`），依
`track_d_work_order.md` 施工，設計提案原始碼來自
`extracted_MainWithSidebar.dc.html`（Zeal 核准的完整版提案）。

**做了什麼**：

1. **開工前棕地探勘**：發現工作單引用的「Track A 的 `#minutes-live` 決議/待辦/
   未解決面板」與「Track C 的 `<details>` 群體動力」在**這個 worktree 裡都不存在**
   ——本 worktree 是從 main 直接切出來的獨立 git worktree，Track A/C 的改動顯然還
   留在它們各自的 worktree 裡未合併回 main。這不是我能修的範圍（worktree 隔離，
   我的沙箱明確拒絕跨 worktree 操作），所以 Track D 直接對著「本 worktree 目前真
   實的 `index.html`」施工，沒有假裝那兩塊東西存在。**合併時要注意**：D2 的
   「留意類」規則原本該讀 Track A 的即時未解決事項數，這裡改用結構性代理值（見
   下方「跳過/替代」）；D3 沒有既有 `<details>` 要拆，直接新建滑入抽屜。
2. **D1 主席狀態徽章**：新增 `#chair-badge`（三態：發言中/忍住中/可能異常），放在
   `.head-clock` 裡、`.status` 之上。決定邏輯：
   - 發言中／忍住中：沿用既有 `currentChairStatus()`；「等停頓」（已排隊等空檔開口）
     歸進「忍住中」——理由是兩者都還沒真的發出聲音，跟工作單「不要另創第四態」的
     要求一致，這是我自己判斷的小幅澄清，寫在這裡明講。
   - 可能異常：沿用 `renderAlerts()` 的兩個真實訊號（`hearing.ok===false`／
     `ttsFailStreak>=TTS_FAIL_ALERT`），**額外加了 `maxSilence>=SILENCE_LIMIT`**
     當第三觸發條件——工作單允許加但要求寫理由：這不是新發明的門檻，`s-silence-sub`
     本來就用同一個 `SILENCE_LIMIT` 顯示「曾觸發超時」，這裡只是把同一個既有事實
     也反映到頂部徽章。
   - 新色票收進 `:root`：`--pill-speaking-bg/--pill-holding-bg/--pill-holding-fg/
     --pill-warning-bg/--pill-warning-fg`（逐值抄自提案圖 `renderVals()` 的
     `table` 物件），沒有裸 hex 散落在標記或規則裡。
   - 為了不讓同一件事在畫面上講兩遍，把舊的 `.status` 文字從「決定階段 · 主席聆聽中」
     簡化成「決定階段」——主席狀態改由新徽章顯示。這是一個小偏離，寫在這裡供人知道。
3. **D2 AI 即時觀察面板**：新增 `#obs-list`，三類規則**全部不呼叫 LLM**：
   - 觀察類：發言分佈某人佔比 ≥ 65%（`SHARE_DOMINANCE_THRESHOLD`，我自己定的門檻，
     可調）就生一則。
   - 判斷類：偵測到「呻吟區→收斂期」的階段轉換（新增 `state.phaseTransitions`
     追蹤，既有的 `phaseMarks` 只記切到哪個階段、不記從哪來），或「僵局介入之後
     60 秒（`DEADLOCK_COOLDOWN_S`）沒有再介入」。
   - 留意類：剩餘時間 < 30%（`NOTICE_TIME_LEFT_RATIO`）且**有僵局介入尚未收斂**。
     工作單原文舉例用「未解決事項數」，但那份資料結構（Track A 的即時決議面板）
     這個 worktree沒有，改用既有的 `spokenKinds["僵局"]` 次數當代理值——一樣是
     後端已經算好的原子事實，語意也貼近「還沒被共識收斂掉的爭點」，但不是完整的
     待辦/未解決清單。**合併後如果 Track A 的面板已經在，這裡應該改回讀真正的
     未解決事項數**（已補一筆進 `docs/DEFERRED_DEFECTS.md`）。
   - 三類各自跟「上一次推播文字」比對，內容沒變就不重推；最新一則帶打字游標
     （複用提案圖的 `ahem-pulse` keyframe，`step-end` timing）。
4. **D3 群體動力滑入抽屜**：新增 `<button id="dyn-handle">`（真正的 `<button>`
   元素，Tab／Enter／Space 天生就會動，沒有重寫 keydown 判斷）+ `#dyn-drawer`
   （`position:fixed`、`transform:translateX`，不推擠版面）。既有的
   KPI（`.stats`）／時間軸（`#timeline`）／主席的思考（`#judge-list`）／
   發言分佈（`#share-list`）四塊**原樣搬進抽屜**，id 與 render 函式完全沒動。
   **`top` 座標刻意沒有照抄提案圖的 `160px`**：提案圖是通版標題列蓋住左右兩欄，
   生產版的 `.head` 只蓋在左欄之上，右欄（含抽屜）從 `top:0` 開始本來就不會蓋到
   左欄的主席徽章，硬抄那個數字反而是套錯前提。
   Kaner 菱形節奏 SVG（提案圖第 156-169 行）**沒有做**——見下方跳過清單。

**實測證據**：
- `.venv/bin/python -m pytest tests/ -q` → **559 passed, 21 skipped, 2 xfailed**
  （改動前後兩次都跑過，數字一致，沒有連坐；`tests/test_spectator_phase.py`／
  `test_spectator_chair_broken.py`／`test_spectator_three_state.py` 這三份會真的
  開 Chromium 執行前端 JS 的測試也全過）。這台環境原本沒裝任何 pip 依賴，我在
  worktree 裡自己建了 `.venv` 並裝了 `requirements.txt` + `playwright`
  （`playwright install chromium`，沒裝系統依賴也能跑，`--with-deps` 需要 sudo
  裝不了，改用純瀏覽器二進位）。
- 用 `PYTHONPATH=src .venv/bin/python -m meeting_host.spectator --replay
  examples/synthetic-meeting.events.jsonl --port 8877 --speed 1000000
  --public-read` 起真的觀戰伺服器，寫了一支 Playwright 腳本
  （`/tmp/.../scratchpad/verify_track_d.py`）用真瀏覽器打開頁面：
  - 三態徽章：用 `window.__spectator.handleEvent(...)` 灌真事件（`hearing`／
    `failed`／`fast_timer`）強制切三態拍照，**不是**用零 JS 對照組推論。截圖見
    `scratchpad/track_d_screens/02~05_badge_*.png`。過程中**用真瀏覽器測試抓到
    一個真的 bug**：`computeObservation/computeJudgment/computeNotice` 回傳字串
    自己帶了「觀察：/判斷：/留意：」前綴，`renderObservations()` 又加了一次，
    畫面上印出「觀察：觀察：林同…」——已修好（拿掉 compute 函式裡的前綴，
    只在 render 端印一次），修完重新截圖確認正常，見
    `07_obs_panel_after_dominant_share.png`／`08_obs_panel_after_deadlock_resolved.png`。
  - AI 觀察面板：灌一筆主導度 82% 的假 `share` 事件、一筆僵局介入+時間推進 61 秒，
    畫面真的各生出一則新的觀察/判斷，不是靜態文字。
  - 群體動力抽屜：`page.keyboard.press("Tab")` 迴圈找到 `#dyn-handle`
    （`found_handle_by_tab: true`），按 `Enter` 開啟（`aria-expanded=true`、
    class 多了 `open`），抽屜內 `.stats`/`#timeline`/`#judge-list`/`#share-list`
    確認都在且有內容（`judgeListHtmlLen:321`／`shareListHtmlLen:499`，不是空殼）。
    開啟時再按 `Tab` 落在 `#dyn-close`，按 `Enter` 關閉；重新 focus 把手後按
    `Space` 也能開——**Tab/Enter/Space 三者都用真瀏覽器測過**，不是推論。量
    `.right` 的 `getBoundingClientRect()` 在抽屜開/關前後完全相同
    （`right_layout_unaffected_by_drawer: true`），證明「蓋在內容上、不推擠」。
    截圖見 `09~12_drawer_*.png`。
  - 全程 `page.on("pageerror")` 收集：**零筆 JS 例外**。
- 沒有新增任何 hex 顏色字面值散落在標記/規則裡：`grep -n "#[0-9A-Fa-f]\{3,6\}"`
  只命中 `:root` 裡新舊變數定義，以及既有、這批完全沒碰過的 `renderTimeline()`
  SVG 字串（那是既有程式碼，不是這批新增的）。

**卡住或未完的**：
- Kaner 菱形節奏 SVG 沒做（時間關係，工作單本來就允許跳過）——見
  `docs/DEFERRED_DEFECTS.md`。
- D2「留意類」目前讀的是「僵局介入次數」代理值，不是 Track A 真正的未解決事項數
  （因為那份資料結構在本 worktree 不存在）——見 `docs/DEFERRED_DEFECTS.md`。
- Track A/C 的成果如何跟這批合併（尤其 D2 面板要不要跟 `#minutes-live` 合併、
  合併後留意類規則要不要換資料源）沒有在這批處理，需要合併時的人決定。

**下一關該知道什麼**：
- 合併分支時，如果 Track A 的 `#minutes-live` 真的併進來，記得把
  `computeNotice()` 的 `state.spokenKinds["僵局"]` 換成真正的未解決事項數。
- `.status` 的文字從這批開始不再包含「主席聆聽中」——只寫階段名（+ 建議提示）；
  如果有別的 Track 依賴那段舊文字格式做斷言，需要知道這個變化（本批跑過的
  `tests/test_spectator_phase.py` 只用 `startswith("決定階段")`，沒斷言完整字串，
  所以沒有測試因此變紅，但別的 Track 若自己寫過類似斷言要注意）。
- 三個新的可調參數都在 `index.html` 裡用具名常數宣告、旁邊有註解寫調整理由：
  `SHARE_DOMINANCE_THRESHOLD`(0.65)／`DEADLOCK_COOLDOWN_S`(60)／
  `NOTICE_TIME_LEFT_RATIO`(0.3)／`CHAIR_ANOMALY_SILENCE` 判斷式裡的 `SILENCE_LIMIT`
  重用（既有值 300）。demo 前如果覺得太靈敏/不夠靈敏，改這幾個數字就好。
- 本批新建的 `.venv`（含 playwright + chromium 瀏覽器二進位）留在這個 worktree
  裡，`.gitignore` 應該已經排除它（沒有另外檢查，但 Python 專案的 venv 目錄
  通常都在忽略清單裡）；合併/部署到正式 demo 機器時仍要照 README 的步驟自己
  建一份，不要以為這個 `.venv` 會被帶過去。

---

## 2026-09-05 — Track D 併回 main（`git rebase main`）：4 處衝突人工合併＋覆核

**誰做的**：合併執行者（general-purpose agent），在 worktree
`agent-a01c2165924ad3f87` 上跑 `git rebase main`（把 `3b88338` 重放到 `440afff`）。
前 2 處衝突由前台 Sonnet 先手動解掉，本次負責解剩下 2 處 ＋ 覆核前 2 處。

**做了什麼**：

1. **覆核前台已解的 2 處，抓到 2 個殘留問題**（不是邏輯錯，是清理沒做乾淨）：
   - **`index.html` 第 1385 行留了一個孤兒 `>>>>>>>` 衝突標記**。前台把 Track A 的
     `renderMinutesLive()` 與 Track D 的觀察函式兩邊都保留、也補上了正確的收尾大括號，
     但忘了刪掉那行標記。`git status` 仍顯示 `UU` 所以不會被誤 commit，但它會讓整支
     `<script>` 語法錯誤、**整個觀戰畫面的 JS 全部不執行**。已刪除。
   - **Track C 的 4 組 CSS 變成孤兒規則**：`.drawer`、`.drawer > summary`（含
     `::marker`／`::-webkit-details-marker`／`::after` 三條）、`.drawer-body`、
     `.section-secondary`。它們唯一的使用者是被 Track D 取代掉的
     `<details class="drawer" id="group-dynamics">`，移除後沒有任何元素會命中。
     已刪除並留一段註解說明為什麼消失。
   - **前台的合併邏輯本身判斷正確**：Track A 的 `#minutes-live`（結果／待辦）與
     Track D 的 AI 即時觀察面板（過程／動態訊號）確實是兩塊獨立區塊、兩邊都完整保留、
     沒有互相蓋掉；Track C 的 inline 抽屜連同它的 Enter 鍵補丁 `<script>` 乾淨移除。
     JS 事件監聽器沒有指向已不存在的 id（`#group-dynamics` 在全檔零命中）。
2. **解掉剩下 2 處衝突**（都在 `wireSource()` 的 snapshot handler 與開機初始渲染）：
   兩側都是「各自呼叫自己那個 render 函式」，彼此獨立，**兩邊都保留**，
   並保留 Track D 那段抽屜開合的 IIFE。
3. **採用了 Track D 施工者留在 `computeNotice()` 上方的建議**（工作單第 4 點）：
   改讀 `state.minutesLive.unresolved.length`。覆核時發現原代理值的問題**比原記錄更嚴重**
   ——`state.spokenKinds["僵局"]` 是只增不減的累計次數，會跟同一塊面板裡
   `computeJudgment()` 的「僵局已解除」**同時出現、字面互相矛盾**。詳見
   `docs/DEFERRED_DEFECTS.md` 第 5 項。
4. **合併兩份小本本與兩份缺陷清單**（add/add 衝突）：兩邊內容都保留，缺陷清單重新編號，
   並把本次已處理掉的項目標成已修復／已結案。

**實測證據**（全部自己重跑，不採信任何先前回報的數字）：

- `git status` 不再有 `UU`／`AA`；
  `/usr/bin/grep -n "<<<<<<<\|=======\|>>>>>>>" src/meeting_host/spectator/index.html`
  → **0 命中**。
- `node --check` 抽出的 inline script（1,240 行）→ **rc=0**，語法正確；全檔只剩 **1 個**
  `<script>` 區塊（Track C 那個 Enter 補丁 script 已隨 `<details>` 一起消失）。
- Python `HTMLParser` 標籤平衡檢查 → **errors: none, unclosed: none**。
- **完整測試套件**：`.venv/bin/python -m pytest tests/ -q`
  → **564 passed, 21 skipped, 2 xfailed in 30.53s**，結束碼 0（Python 3.12.3）。
- **headless Chromium 冒煙測試**（真的開頁面，不是讀程式碼推論）：
  `#minutes-live`／`#obs-list`／`#dyn-handle`／`#dyn-drawer`／`#dyn-close`／
  `#chair-badge`／`#s-iv`／`#timeline`／`#judge-list`／`#share-list` 各 1 個；
  `#group-dynamics` 與 `<details>` 各 **0 個**；兩塊面板都渲染出空狀態文字
  （「尚無資料」／「尚無觀察」）**證明兩個 render 函式在開機時都真的被呼叫到**——
  這正是第 2 處衝突要解對的東西。
  抽屜開合：初始關 → 點把手開（`aria-hidden=false`／`aria-expanded=true`）→
  點關閉鈕關 → **Enter 開** → **Space 關**，`transform` 實測滑到
  `matrix(1,0,0,1,0,0)`。**原生 `<button>` 確實 Enter／Space 都吃，
  證實 Track D「不必自己寫 keydown」的設計成立**，也證實移除 Track C 那 8 行是對的。
- **端到端驗證 `computeNotice()` 的改動**（掛真的 `spectator._build_app`＋
  playwright，不是單元測試造假）：
  ①會議走到第 8 分鐘、僵局早已過去、但預覽還沒到 → 只顯示「判斷：拉鋸結束」，
  **沒有**矛盾的留意訊息（舊代理值在這一格會噴「仍有 1 項僵局介入未見後續共識」）；
  ②預覽帶 2 筆未解決事項 → 「留意：仍有 2 項未解決事項，剩餘 2 分鐘」；
  ③其中一筆被收掉 → **數字真的降成 1**（只增不減的舊代理值做不到這件事）。
  全程 **JS ERRORS: none**。

**卡住或未完的**：無。rebase 已 `--continue` 完成。

**下一關該知道什麼**：

- 🔴 **`computeNotice()` 的行為變了**：`state.minutesLive` 在 `--no-llm` 模式下永遠是
  `null`（`watch_minutes` 掛在 `not args.no_llm` 底下），且要累積
  `MINUTES_PREVIEW_MIN_UTTERANCES`(6) 筆發言、每
  `MINUTES_PREVIEW_INTERVAL_S`(90 秒) 才送一次。**所以 demo 若跑 `--no-llm`，
  「留意」這一類不會出現**（「觀察」與「判斷」兩類不受影響，它們讀的是快路資料）。
  這是刻意的取捨：沒資料就不講話。若 demo 一定要看到留意類，確認不要加 `--no-llm`，
  且會議長度要夠讓預覽至少送出一輪。
- 本 worktree 的 `.venv` 是 **Python 3.12.3**，而 repo CI 宣告 3.13——
  上一批小本本已經提醒過，demo 正式機器上機前再確認一次實際跑的版本。
- `docs/DEFERRED_DEFECTS.md` 還有 3 項待處理：第 2 項（`background: transparent`
  建議改 `var(--bg)`）、第 3 項（已因 Track D 改版大致失效，僅剩最低優先度）、
  第 4 項（Kaner 菱形節奏視覺化未做）。demo 之後再看。

---

## 2026-09-05 Track F：補 Kaner 菱形「會議節奏」視覺化＋核對抽屜排版/圖示

- **時間**：2026-09-05（Demo Day 當天）
- **誰做的**：builder agent（worktree 隔離），Claude Sonnet 5 前台驗收
- **開工前棕地探勘發現的落差**：worktree 建立時分岔自舊的 `f025fef`（另一條
  Discord／participant-token 功能線），main 當時已經前進到 `0cb37c8`
  （含 Track D 的滑入抽屜與 Track E 的水彩/卡片修正），兩者相差 14 個 commit。
  `git merge-base main HEAD` 確認 `f025fef` 是 `0cb37c8` 的**純祖先**（`main..HEAD`
  無任何獨有 commit），於是用 `git merge --ff-only main` 快轉到 `0cb37c8`，
  乾淨、無衝突。這一步如果漏做，`.dyn-drawer`／`#dyn-handle` 等工作單引用的
  id 全部不存在，會整批誤判成「Track D 沒做」。
- **做了什麼**：
  1. `src/meeting_host/spectator/index.html` 新增 `renderKanerDiamond()` 與兩個
     幾何輔助函式 `kanerUpperY(x)`/`kanerLowerY(x)`，插在 `renderTimeline()`
     之後。HTML 插入點在「群體動力」抽屜標題列之後、既有四塊（KPI／時間軸／
     主席的思考／發言分佈）之前，比照提案圖順序（會議節奏在最上面）。
  2. 三個階段分界點：預設 `duration_min*60` 均分三等份；`state.phaseTransitions`
     出現第一筆 `to==="呻吟區"`/`to==="收斂期"` 就改用那筆真實 `t`（用
     `gotB1`/`gotB2` 旗標鎖住「只取第一筆」，不是每次都覆蓋成最後一筆）。
  3. 目前位置圓點／線：`state.serverNow` 對總時長的比例映射到 6~314px，掛在
     `handleEvent()` 開頭 `state.serverNow = ev.t;` 之後呼叫，讓它跟畫面上其他
     即時元素一樣每個事件就更新；另外在 `"meeting"`／`"phase"` 兩個 case 尾端
     各多呼叫一次，因為這兩處會推進 `phaseTransitions`／換 `duration_min`，
     順序上晚於檔頭那次呼叫，不補這一下分界線會晚一個事件才追上（marker
     位置本身不受影響，因為它只依賴 `serverNow`，在檔頭那次呼叫時已經是對的）。
  4. 填色路徑與兩條分隔虛線都用同一組直線內插公式現算 y 座標，不寫死。
  5. 三欄標籤 `grid-template-columns` 的三個 fr 值跟著真實分界點成比例
     （`Math.max(1, ...)` 防止退化寬度把 grid 弄壞）。
  6. 說明文字取 `phaseTransitions` 最後一筆；沒有紀錄時改講真話「尚未切換階段，
     目前處於「X」」，**沒有**照抄提案圖那句「主席取得評估授權後開始收斂」——
     那是示範用的假敘述，沒有對應的真實判斷邏輯，工作單也明講不要抄。
  7. 顏色：新增 `--kaner-fill`(`#EAE1D2`)／`--kaner-track`(`#C9BEAD`) 兩個
     `:root` 變數；另外三色（外框白底／描邊／目前位置標記）分別剛好等於既有
     `--card-bg`／`--dim`／`--text`，直接重用沒有重複定義。
- **一處小澄清（工作單 vs 提案圖原始碼有出入，取後者）**：工作單文字說「其餘
  兩欄用 `--muted`」，但逐字讀提案圖第 166-167 行，未選中欄位的顏色是
  `#A89F93`——這個色碼在本專案已有既有具名變數 `--dim`，不是 `--muted`
  （`#857C72`）。既然工作單本身開宗明義要求「直接讀源碼取精確數值，不要憑
  印象轉述」，這裡採用提案圖的真實色碼、對到既有的 `--dim`，不是工作單文字
  複述時的筆誤版本。
- **排版/圖示核對結果**（Playwright 實測，不是讀程式碼推論）：把手菱形 icon
  14×14、抽屜標題菱形 icon 13×13（皆為 `M12 3 L21 12 L12 21 L3 12 Z`）、關閉
  按鈕 15×15（兩條對角線 X）、關閉按鈕點擊區 34×34、抽屜寬度 380px、抽屜陰影
  `rgba(43,38,34,0.08) -12px 0 32px`、把手陰影 `rgba(43,38,34,0.06) -6px 0 16px`
  ——**全部跟提案圖逐值相符，Track D 已經做對，本批沒有改動這些**。
- **實測證據**：
  - `.venv/bin/python -m pytest tests/ -q` → **564 passed, 21 skipped, 2 xfailed**
    （施工前後各跑一次，數字一致；這批只碰 `index.html`，Python 零改動）。
    這台環境原本沒有 `.venv`，本批自己建的（`requirements.txt` + `playwright
    install chromium`，chromium 二進位命中既有的 `~/.cache/ms-playwright`
    共用快取，沒有重新下載）。
  - `node --check`（抽出真正的 `<script>`…`</script>` 區塊，用 `^<script>$`/
    `^</script>$` 精確定位行號，不是天真的正規表示式——第一次天真抓法被我自己
    寫的 CSS 註解裡出現的字面字串 `<script>` 誤導撞到假陽性語法錯誤）→ **rc=0**。
  - Python `HTMLParser` 標籤平衡檢查（沿用 Track D 那次審計的同一支檢查邏輯）
    → **errors: 0, unclosed: []**。
  - `grep -n "#[0-9A-Fa-f]{3,6}"` 逐行核對：新程式碼（`renderKanerDiamond` 系列
    函式）**零筆**裸 hex，全部走 `var(--...)`；命中的都是既有、這批完全沒碰過的
    `renderTimeline()` SVG 字串或說明文字裡的色碼提及。
  - **真瀏覽器＋真實回放資料驗證**（`PYTHONPATH=src .venv/bin/python -m
    meeting_host.spectator --replay examples/synthetic-phases.events.jsonl
    --port 8879 --speed 1000000 --public-read`，這份範例檔本身就在 t=180
    切「呻吟區」、t=300 切「收斂期」，duration_min=15（900 秒），均分點應為
    300/600，跟這兩筆真實時間明顯不同，足以證明程式真的讀了
    `phaseTransitions` 而不是巧合對上均分值）：
    - `#kaner-diamond` innerHTML 讀出的分界線 x 座標 `67.6`/`108.7`、目前位置
      圓點 `108.7`，跟純數學公式（`px(180,900)=67.60`／`px(300,900)=108.67`）
      獨立算出的期望值完全對上（誤差 <0.15px，浮點捨入範圍內）。
    - `#kaner-labels` 的 `grid-template-columns` 電腦運算後的像素
      `63px/41.98px/210px`，比例 `20%/13.33%/66.67%`，跟 `(180)/(120)/(600)`
      秒數比例完全一致。
    - `#kaner-now` 顯示「目前：決定階段」、`#kaner-caption` 顯示
      「05:00 由拉鋸進入決定」（`fmtClock(300)="05:00"`，取自 `phaseTransitions`
      最後一筆，沒有抄提案圖的假句）。
    - 用 `window.__spectator.handleEvent({kind:'phase', t:345, data:{phase:
      '發散期', source:'manual-test'}})` 在瀏覽器 console 灌一筆假事件（只在
      記憶體裡動，沒有寫回任何檔案）：目前位置圓點即時移到 `124.1`（期望值
      `px(345,900)=124.07`）、粗體欄位換成「發想」、說明文字換成
      「05:45 由決定進入發想」——**證明「隨事件即時移動」與「文字說明真的會換」
      都成立**。分界線本身沒動（這筆事件的 `to` 是「發散期」，不是
      「呻吟區」或「收斂期」，設計上就不該影響分界點），這是正確行為，不是
      沒反應。
    - 全程 `page.on("pageerror")` 收集 → **零筆 JS 例外**。
    - 截圖：`scratchpad/track_f_screens/01_drawer_real_transitions.png`（整頁）、
      `02_drawer_top_kaner_closeup.png`（真實資料狀態特寫）、
      `03/04_after_fake_transition*.png`（灌假事件後的狀態）、
      `05_handle_closed.png`／`06_drawer_header_closeup.png`（把手／標題列
      特寫）——這些截圖存在對話的 scratchpad，**沒有入庫**，僅供這次交付佐證。
- **卡住或未完的**：無阻塞。以下是刻意的簡化，記在這裡讓下一棒知道：
  - `total`（總時長）分母在 `!state.meeting` 時退化用常數 `1` 避免除以 0，這時
    marker 會停在最左端、三欄近似等寬——這是「會議還沒開始」的誠實佔位狀態，
    不是 bug；工作單沒有明講這個邊界情況，是本批新增的合理判斷。
  - 分界點的「第一筆」判斷用兩個布林旗標 `gotB1`/`gotB2` 鎖住，如果同一階段
    重複進出（例如人工把階段切回發散期又切回呻吟區），分界點永遠停在**第一次**
    切過去的那個時間，不會被後續同名切換覆蓋——這是刻意選擇（分界點代表
    「這個階段第一次開始」），工作單原文用詞是「第一個分界點」，這裡讀成
    「第一筆紀錄」而非「最後一筆」，跟 `computeJudgment()` 讀「最後一筆」的
    既有慣例不同，屬於語意不同、刻意分開處理，不是不一致。
- **下一關該知道什麼**：
  - `docs/DEFERRED_DEFECTS.md` 第 4 項已標記解決，其餘第 2、3 項仍待處理
    （優先度低，demo 後再看，不在這批範圍）。
  - `.venv`（含 chromium 二進位）留在這個 worktree，`.gitignore` 已排除，
    正式 demo 機器要照 README 自己建一份。
  - 本批累計缺陷修復 0 筆（純新增功能，沒有動到既有四塊的資料/渲染邏輯），
    疲勞計數不適用。

## 2026-09-05 Track G：AI 即時觀察加第四類「心聲」，真的接 LLM＋保險栓

- **時間**：2026-09-05（Demo Day 前一天）
- **誰做的**：builder agent（worktree 隔離，`agent-a311027fff77f21ef`，分岔自
  main 當時最新的 `5efe758`——開工前已用 `git log -1 main` 對過，不是舊快照，
  這批沒有踩到前幾批連續撞過的 worktree 分岔地雷），Claude Sonnet 5 前台驗收。
- **開工前棕地探勘**：完整讀過 `minutes.py`（`MINUTES_SYSTEM`／
  `build_minutes_prompt()`／`_call_minutes_llm()`／`slow_path` 的 API 設定
  匯入）、`live.py` 的 `watch_minutes()`／`main_async` 組 `tasks` 的地方／
  `--no-llm` 旗標寫法、`index.html` 的 `computeObservation`/`computeJudgment`/
  `computeNotice`/`pushObservation`/`OBS_LABEL`/`renderObservations()`。額外
  發現工作單沒提到的一處接線：`index.html` 的 `KINDS`（SSE 白名單陣列，第
  1862 行附近）沒有 `ai_critique`，若不加，正式連線（非 replay）永遠收不到
  這個事件——`snapshot` 重播路徑不受影響（它是全量重播，不經過 `KINDS`
  過濾），只有即時串流會被擋，這種「replay 測得過、demo 現場測不出來」的
  落差正是最危險的那種，已補上。
- **中途插播（重要）**：施工到一半，Zeal 插播「fable 專門設計了完整版
  system prompt／JSON 格式」，取代工作單原本 G1 段落我自己寫的草稿版本。
  已改用插播版本：JSON schema 從 `{"meeting_note": str, "participants":
  [{"name","note"}]}` 改成 `{"meeting": str, "participants": {名字: 評語}}`
  （`participants` 是物件不是陣列），面板顯示文字從「批判」改成「心聲」——
  **但內部識別名稱（模組名 `critique.py`、event kind `"ai_critique"`、
  `--no-critique` 旗標、方法名 `watch_critique`、CSS class `ok-critique`）
  全部維持不改**，只有 UI 顯示字串與 JSON schema 是這次的更新範圍，插播訊息
  原文「其餘工作單內容（G2 背景迴圈、保險栓、G3 前端接線骨架）不變」。
- **做了什麼**：
  1. **新檔案 `src/meeting_host/critique.py`**：仿 `minutes.py` 結構——
     `CRITIQUE_SYSTEM`（逐字照抄插播的 fable 版本，不是自己轉述）、
     `build_critique_prompt(events, participants)`（「## 與會者」名單＋
     `minutes.build_minutes_prompt()` 同款逐字稿抽取）、`_call_critique_llm()`
     （跟 `_call_minutes_llm()` 完全同款 urllib＋`timeout=90`＋
     `response_format: json_object`，`from .slow_path import API_URL, EFFORT,
     MODEL, _api_key` 沿用同一組既有設定，沒有另開新的）。
  2. **`live.py`**：新增常數 `CRITIQUE_INTERVAL_S=45.0`／
     `CRITIQUE_MIN_UTTERANCES=4`（比 `MINUTES_PREVIEW_*` 快一點、門檻低一點，
     理由見工作單）；新增 `Session.watch_critique()`，完全仿 `watch_minutes()`
     骨架（`while True: sleep → 門檻檢查 → asyncio.to_thread 呼叫 LLM →
     `self.ending` 二次檢查 → `self.emit("ai_critique", …)`，整段包
     `try/except`，`CancelledError` 原樣往外拋）；新增 `--no-critique`
     旗標（`action="store_true"`，跟 `--no-llm` 同款寫法）；`main_async`
     組 `tasks` 的地方在既有 `if not args.no_llm:` 區塊裡、`watch_minutes()`
     之後，加一層 `if not args.no_critique: tasks.append(...watch_critique())`
     ——兩個旗標任一開啟都排不進去。
  3. **`index.html`**：`KINDS` 白名單補 `"ai_critique"`（工作單沒提到，見上）；
     `handleEvent()` 加 `case "ai_critique":`，讀 `data.meeting`／
     `data.participants`（物件，用 `Object.keys` 走訪）；新增 `obsLabelFor()`
     查表函式（`OBS_LABEL` 三類固定物件不動，動態的 `critique_meeting`／
     `critique_person:<name>` 另外查），`renderObservations()` 改呼叫
     `obsLabelFor()` 取代原本直讀 `OBS_LABEL[o.kind]`；新增 CSS
     `.obs-row .ok.ok-critique { color: var(--accent) }`，渲染時判斷
     `isCritique` 動態加這個 class，肉眼可辨（其餘三類是 `--text`）；順手
     更新了三處會讓人誤讀的既有註解／文案（`.obs-note` 腳注、AI 即時觀察區塊
     的 HTML 註解、`computeObservation()` 前的區塊註解）——原文說「三類全部
     不呼叫 LLM」，加了第四類之後這句話變成謊言，不改的話是留一個説明文件
     層級的謊在裡面。
  4. **一個小澄清（工作單 vs 既有程式碼慣例有出入，取後者）**：工作單 G3
     第 1 條給的範例程式碼在 `case "ai_critique":` 裡呼叫了一次
     `renderObservations()`，但讀完整份 `handleEvent()` 才發現：**這個函式
     本來就在 switch 結束後統一呼叫一次**（第 1845 行附近的既有註解「AI 即時
     觀察：全部規則都是從上面已經更新完的 state 算出來的原子事實，所以放在
     switch 之後統一算一次，不用每個 case 各自呼叫一遍」），其餘 13 個既有
     case 沒有任何一個在自己內部重複呼叫它。判斷維持既有慣例（case 裡不呼叫，
     讓後面統一那次負責），不照抄工作單／插播訊息裡那行多餘呼叫——多呼叫一次
     不會壞，但會製造「這裡好像有特殊理由要多算一次」的錯誤印象。
- **實測證據**：
  - `.venv/bin/python -m pytest tests/ -q` → **574 passed, 21 skipped, 2
    xfailed**（基準 564 passed / 21 skipped / 2 xfailed ＋本批新增 10 筆全過，
    數字對得上，其餘既有測試零失敗）。這台環境原本沒有 `.venv`，本批自己建
    （`requirements.txt` + `playwright install chromium`，命中既有共用快取）。
  - 新測試檔 `tests/test_critique_preview.py`（10 個測試）：
    `build_critique_prompt()` 純函式輸出格式；逐字稿夠長時真的呼叫 LLM 並
    emit `ai_critique`；逐字稿太短不呼叫；`CancelledError` 不被吞；
    `session.ending=True` 時飛行中的呼叫不補發（比照 `watch_minutes` 的同款
    回歸測試）；**保險栓本身**（`--no-critique`／`--no-llm`／兩者皆開，三種
    組合各自驗證 `watch_critique` 不在 `main_async` 真正組出來的 `tasks`
    清單裡）＋一個對照組（兩旗標都不開時 `watch_critique` 確實在清單裡，
    順手核對 `watch_slow`/`watch_glossary`/`watch_minutes` 也還在，證明沒有
    動到既有 `--no-llm` 任務清單）。
  - **保險栓測試不是憑讀程式碼保證，是真的驅動了 `live.main_async()`**：
    只換掉 `MeetingBot`（唯一真的會連網路的建構子）跟 `live.shutdown`（攔截
    真正組出來的 `tasks`，避免真的跑收尾邏輯），`STTPool`／`build_voice`／
    `Earcon`／`build_hello_gate` 都是真正的 production 物件（開工前逐一確認
    建構時不連網路：`STTPool.__init__` 純同步賦值、`Voice.__init__` 只存
    欄位不發請求、`Earcon.__init__` 讀本地 `assets/earcon.wav`、
    `build_hello_gate(False)` 直接回 `None`）。用
    `asyncio.wait_for(live.main_async(args), timeout=0.3)` 模擬「demo 現場按
    Ctrl-C」的取消路徑，跟正式收尾走同一條 `except (KeyboardInterrupt,
    asyncio.CancelledError): pass; finally: await shutdown(...)`。
  - **變異測試（mutation testing）證明這條測試真的有牙齒**：故意把
    `if not args.no_critique:` 暫時改成 `if True:`，重跑
    `test_insurance_switch_keeps_watch_critique_out_of_task_list`——
    `no-critique-only` 那個參數化案例如預期**變紅**（`no-llm-only`／`both`
    兩案例因為外層還有 `if not args.no_llm:` 兜著，仍然綠——這正是預期行為，
    證明測試矩陣分得出「哪一層旗標在擋」），改回原樣後重跑全部變綠。
  - **真瀏覽器驗證**（`--replay examples/synthetic-meeting.events.jsonl
    --port 8891 --speed 1000000 --public-read` + Playwright）：用
    `window.__spectator.handleEvent({kind:"ai_critique", ...})` 灌兩筆事件
    （會議整體一句＋ Alex 一句），DOM 讀出「心聲：」「心聲 · Alex：」都正確
    出現；`getComputedStyle` 讀出 `.ok-critique` 是 `rgb(166, 93, 63)`
    （`--accent` #A65D3F），其餘 `.ok` 是 `rgb(43, 38, 34)`（`--text`），
    兩者不同色、肉眼可辨；同一句話沒變時重推 → 清單筆數不變（4→4，dedup
    生效，不會無限疊同一句話）；文字真的變了 → 新一筆真的出現在畫面上
    （這個面板本質是跟著時間走的觀察流，跟既有觀察/判斷/留意三類同一種設計，
    不是「每人一張永遠覆蓋的現況卡」——新舊句都留在捲動歷史裡是設計本身，
    不是 bug）；全程 `page.on("pageerror")` 零筆例外。截圖存在
    `scratchpad/track_g_after_first.png`／`track_g_after_second.png`
    （對話暫存檔，未入庫）。
  - `node --check`（抽出 `<script>`…`</script>`）→ rc=0；Python
    `HTMLParser` 標籤平衡檢查 → errors: 0, unclosed: []；
    `grep "#[0-9A-Fa-f]{3,6}"` 對新增的 critique/心聲相關行 → 0 筆裸 hex。
- **卡住或未完的**：無阻塞。以下是已知但刻意留給下一棒的落差，已記進
  `docs/DEFERRED_DEFECTS.md` 第 7 項：`CRITIQUE_SYSTEM`（插播版本）開場說
  「你會拿到…主席介入紀錄與發言統計」，但 `build_critique_prompt()` 目前
  只組了與會者名單＋逐字稿，沒有真的附上介入紀錄或發言分佈。插播訊息明確只
  要求換 system prompt／JSON schema，明講「其餘不變」，原始工作單對這個函式
  的要求本來就只有「逐字稿抽取方式」，所以判斷這不在這批改動範圍——但這是
  一個真實的 system prompt 與實際輸入內容不一致，不是我編出來的假想問題，
  已詳細記錄根因與影響範圍，風險評估是「demo 前風險低（不會當機或報錯，只是
  判斷力打折）」。
- **下一關該知道什麼**：
  - `docs/DEFERRED_DEFECTS.md` 第 7 項：要做到 system prompt 講的完整版本，
    可以參考 `minutes.build_minutes_prompt()`／`minutes.render_host_record()`
    的既有寫法，把介入紀錄與 `share` 事件的發言分佈也組進
    `build_critique_prompt()`。
  - 本批**沒有**合併回 `main`——只在這個 worktree 裡 commit，跟其他幾條
    Track 一樣是獨立分支，合併時機與方式由 Zeal／協調者決定。
  - `src/meeting_host/events.py`（`Event.kind` 的行內文件，每個新 kind
    都在這裡留一段補充說明，`glossary`/`hearing`/`voice`/`phase` 等既有
    kind 都是這樣記的）跟 `docs/specs/2026-08-28-demo-readiness-design.md`
    （T-B 事件匯流排規格文件）都**沒有更新**——工作單「目標檔案」明確只列
    `critique.py`／`live.py`／`index.html` 三個檔案，一開始順手在
    `events.py` 補了一段 `ai_critique` 的說明文件，覆核時發現它不在
    allowlist 裡，已還原（`git checkout -- src/meeting_host/events.py`）。
    這不是「忘記做」，是「做了又主動撤回」，如實記在這裡：如果之後有人要把
    `events.py` 或那份獨立 spec 文件當 event kind 的唯一正本查，要記得
    它們目前都沒收錄 `ai_critique` 這個新 kind，下一棒如果確認補這段文件
    在允許範圍內，可以參考本批 commit 歷史裡曾經寫過又還原的那版文字。
  - `.venv`（含 chromium）留在這個 worktree，`.gitignore` 已排除。

---

## 2026-09-06 Track H：心聲補真統計/介入紀錄＋長會議逐字稿壓縮

- **時間**：2026-09-06（決賽 Demo Day 當天前）
- **誰做的**：builder agent（worktree 隔離，`agent-a413cae73783fcac8`，分岔自
  main 當時最新的 `fea5ed4`——開工前已用 `git log -1 main` 對過），
  規格由 fable（另一模型）針對這個專案的真實程式碼設計，逐字照抄不自行改寫
  用詞（見 CLAUDE.md「寫進正典/規格要逐字照抄」）；Claude Sonnet 5 前台驗收。
- **開工前棕地探勘**：完整讀過 `critique.py`（91 行全文，`CRITIQUE_SYSTEM`／
  `build_critique_prompt()`／`_call_critique_llm()`）、`minutes.py` 第
  98-157 行 `_pair_interventions(events)`（配對邏輯與回傳欄位）、`live.py`
  第 522-534 行 `emit_share()`（佔比分母公式）與第 1057-1096 行
  `watch_critique()`（現行呼叫方式）、`state.py` 的 `spoke_seconds`／
  `silent_seconds`／`remaining_seconds`／`absent` 四個既有查詢方法——工作單
  點名的統計來源全部現成，沒有另外重算一份。
- **背景**：`CRITIQUE_SYSTEM`（Track G 上線）開宗明義說 LLM 會拿到「逐字稿、
  主席介入紀錄與發言統計」，但 `build_critique_prompt()` 之前只給逐字稿＋
  與會者名單——這個落差記在 `docs/DEFERRED_DEFECTS.md` 第 7 項，是 Track G
  施工時特意標記「留給下一棒決定」的已知缺口。Zeal 同時指出實測會議長度是
  40-60 分鐘（不是只有 demo 那 5 分鐘），要求一併處理逐字稿越餵越長的問題。
- **做了什麼**：
  1. **`src/meeting_host/critique.py`**：新增 `CritiqueStats`／
     `ParticipantSpeechStat` 兩個 dataclass，承載 `watch_critique()` 從
     `self.st`／`self.now` 換算出來的統計資料——`build_critique_prompt()`
     因此仍是不依賴 `Session`／`MeetingState` 的純函式，單元測試不用建一整個
     Session。原本獨立的 `participants: list[str]` 參數併入
     `CritiqueStats.participants`（不再重複傳一份永遠要保持同步的清單）。
     新增 `_render_stats_table()`／`_render_interventions()` 兩個排版函式，
     插在「## 與會者」與「## 逐字稿」之間（順序：先給量、再給主席做過什麼、
     最後才是長逐字稿）。介入紀錄抽取直接呼叫 `minutes._pair_interventions
     (events)`，沒有重寫第二份配對邏輯。新增 `_compact_transcript()`（長會議
     逐字稿壓縮，見下）與 `_dedupe_consecutive()`／`_render_transcript()`
     兩個輔助函式。`_call_critique_llm(events, stats)` 簽章同步更新。
     `CRITIQUE_SYSTEM` 整段替換成 Zeal／fable 這批給的完整版本（逐字照抄，
     已用程式比對驗證跟工作單原文逐字元相同，見下方實測證據）。
  2. **`src/meeting_host/live.py`**：`Session.watch_critique()` 內新增把
     `self.st`／`self.now` 換算成 `CritiqueStats`／`ParticipantSpeechStat`
     的邏輯（`chair_seconds = len(self.st.interventions) * 3.0`，跟
     `emit_share()` 用同一個公式，不是 `state.share()`），再傳給
     `_call_critique_llm`。任務排程（`--no-critique`／`--no-llm` 保險栓）與
     `CRITIQUE_INTERVAL_S`／`CRITIQUE_MIN_UTTERANCES` 兩個常數完全沒動。
  3. **長會議逐字稿壓縮**（`_compact_transcript(events, now)`）：觸發門檻
     為逐字稿超過 12,000 字元或超過 300 則發言（兩者任一），demo 的 5 分鐘
     會議兩個門檻都碰不到，行為與改動前完全相同。觸發後：先做 STT 連續重複
     發言去重；尾窗（最後 15 分鐘或最後 120 則，取範圍較短者）全部逐字保留；
     尾窗之外只留兩類錨點原文（每人第一則長度 ≥10 字的發言／每筆已說出口的
     介入前緊鄰兩則發言），插回原時間位置；其餘整段拿掉的連續區段換成一筆
     `kind="critique_gap"` 的合成事件，`_render_transcript()` 認得這個 kind
     原樣印出標記文字。刻意不做（已寫進程式碼註解）：不開第二個 LLM 呼叫做
     摘要、不做規則式中文縮寫/關鍵詞抽取、不做「決議偵測」錨點；已知天花板
     （跨過尾窗的遠距重複抓不到）也寫進註解，不是這批的缺陷。
  4. `docs/DEFERRED_DEFECTS.md` 第 7 項標記已解決，`docs/PROJECT_NOTEBOOK.md`
     補這一節。
- **實測證據**：
  - `CRITIQUE_SYSTEM` 逐字比對：寫了一段 Python 腳本從工作單原文用正則抽出
    程式碼區塊、跟 `critique.py` 裡的字串逐字元比較 → `MATCH`（見驗收流程，
    不是憑肉眼比對）。
  - `.venv/bin/python -m pytest tests/ -q`（本批自己在這個 worktree 建的
    `.venv`，`requirements.txt` 全裝，沒裝 `playwright`——凡是需要真瀏覽器
    的測試檔用 `pytest.importorskip("playwright.sync_api")` 乾淨跳過，不是
    失敗）→ **558 passed, 26 skipped, 2 xfailed, 0 failed**。skipped 數比
    Track G 紀錄的 21 多 5，是這台環境沒裝 playwright 的環境差異（多跳過
    5 個需要真瀏覽器的測試檔），不是這批改動造成的新跳過或新失敗。
  - `tests/test_critique_preview.py` 從原本 10 個測試項目增加到 18 個
    （新增 8 個），單獨執行 `.venv/bin/python -m pytest
    tests/test_critique_preview.py -q` → **18 passed**：
    - `build_critique_prompt()` 統計表／介入紀錄格式正確（含表頭時間換算、
      佔比分母含主席、發言則數、距上次發言）；
    - 邊界情況：完全沒有介入時印固定占位句「（目前為止主席沒有介入）」；
      有已離會的人時名字加「（已離會）」、距上次發言欄寫「—」但其餘欄位
      照列；議程超時（remaining 為負）顯示「已超時 MM:SS」；
    - 介入紀錄行格式：硬打斷/軟插入＋【kind→target】或【kind】＋原文引號，
      作廢（`outcome != "spoken"`）的候選不出現在輸出裡；
    - `_compact_transcript()`：未達門檻時輸出與原樣逐字相同（`==` 比對，
      不是子字串檢查）；超過門檻時尾窗最後 120 則逐字保留（用 `==`
      比對整段子清單，不是抽樣檢查）、被拿掉的段落合成一筆
      `kind="critique_gap"` 標記且文案含正確的略去則數；兩類錨點（每人
      第一則夠長的發言／介入前緊鄰兩則）即使落在被拿掉的舊段裡也真的
      被插回輸出；同一人連續完全相同的發言只留一則，相似但不同的不受影響；
    - `watch_critique()` 傳給 `_call_critique_llm` 的 `CritiqueStats` 內容
      正確：用 mock 截住呼叫參數本身（不是只斷言「有呼叫」），核對
      `chair_seconds`／`chair_interventions`／`remaining_seconds` 跟
      `session.st` 對得上，且 `absent` 集合裡的人真的被標記
      `ParticipantSpeechStat.absent=True`。
  - 既有保險栓測試（`--no-critique`／`--no-llm`／兩者皆開三種組合＋對照組）
    全部沿用原樣未改動斷言邏輯（只改了 mock 的函式簽章以配合新參數），
    全部通過，證明這批改動沒有動到任務排程那條路徑。
  - `git status --short` 只有三個檔案被改動：`src/meeting_host/critique.py`／
    `src/meeting_host/live.py`／`tests/test_critique_preview.py`，符合工作單
    allowlist，沒有動到 `minutes.py`／`index.html`／既有「觀察/判斷/留意」
    三類規則邏輯。
- **卡住或未完的**：無阻塞，工作單三項交付（system prompt 替換／統計＋介入
  紀錄／長會議壓縮）全部完成，沒有跳過任何一項。已知的刻意簡化都寫進
  `_compact_transcript()` 的程式碼註解裡（見上「做了什麼」第 3 點），不是
  遺漏。
- **下一關該知道什麼**：
  - 本批**沒有**合併回 `main`——只在這個 worktree 裡 commit，跟 Track G
    一樣是獨立分支，合併時機與方式由 Zeal／協調者決定。
  - `.venv`（本批自建，未裝 playwright）留在這個 worktree，`.gitignore`
    已排除；要跑到真瀏覽器那幾個測試檔（`test_spectator_*`／`test_partial`）
    需要另外 `pip install playwright && playwright install chromium`。
  - `_compact_transcript()` 的壓縮門檻（12,000 字元／300 則）與尾窗大小
    （15 分鐘／120 則）都是 `critique.py` 頂部的模組常數
    （`CRITIQUE_COMPACT_CHAR_THRESHOLD`／`CRITIQUE_COMPACT_EVENT_THRESHOLD`／
    `CRITIQUE_TAIL_WINDOW_SECONDS`／`CRITIQUE_TAIL_WINDOW_EVENTS`），demo
    現場如果想確認壓縮邏輯完全不會被觸發，可以直接讀這四個常數對照實際會議
    長度，不用跑統計。
  - 遠距重複偵測（某人 25 分鐘前講過同一論點、又不落在錨點裡）是已知天花板，
    不是這批的缺陷；需要時的升級路徑是 LLM 滾動摘要，明確不在這批範圍內，
    已寫進 `_compact_transcript()` docstring。
  - 本批累計缺陷修復 0 筆（純新增功能），疲勞計數不適用。
