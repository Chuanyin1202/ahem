# 專案小本本 — Ahem 觀戰畫面 UI Upgrade

> 索引，一批一行；細節寫在本檔（批次量不大，暫不拆分檔）。

- 2026-09-05 — Track B（水彩背景圖）完工，合併於 `de4d5ef`。細節見下。
- 2026-09-05 — Track A（會議產出即時預覽）完工＋獨立審計抓到一個競態並修復，已合併。細節見下。

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
