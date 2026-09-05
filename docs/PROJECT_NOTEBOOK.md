# ahem 專案小本本

索引：一批一行，日期＋一句話摘要。

- 2026-09-05：會議產出即時預覽（decisions/todos/unresolved）→ 見下方詳細記錄。

---

## 2026-09-05：會議產出即時預覽（`minutes` 事件加 `final` 欄位）

- **時間**：2026-09-05（決賽 Demo Day 前一晚施工）
- **誰做的**：Claude（builder 角色，在 git worktree
  `.claude/worktrees/agent-a70f83c763ec1b3fd`、分支
  `worktree-agent-a70f83c763ec1b3fd` 裡施工，未合併回 main）
- **做了什麼**：
  - `src/meeting_host/live.py`：新增 `Session.watch_minutes()` 背景迴圈
    （比照 `watch_glossary` 的形狀），每 `MINUTES_PREVIEW_INTERVAL_S`（預設
    90 秒）重問一次 `minutes.py` 的 `_call_minutes_llm`，逐字稿累積到
    `MINUTES_PREVIEW_MIN_UTTERANCES`（預設 6 句發言）以上才跑；發出
    `minutes` 事件、`final: False`，只帶 `decisions/todos/unresolved/stances`
    四個清單，不寫檔。`_emit_minutes`（正式收尾）加 `final: True`。
    `main_async` 在 `not args.no_llm` 底下掛上這個新 task。
  - `src/meeting_host/spectator/index.html`：右欄新增獨立區塊「會議產出
    （預覽）」（`#minutes-live`），前端 `case "minutes":` 依 `data.final`
    分流——`true` 才鎖畫面／鎖匯出鈕（行為不變），`false` 只更新
    `state.minutesLive` 並呼叫新的 `renderMinutesLive()`。空狀態顯示
    「尚無資料」。只用既有 CSS 變數，沒有動到既有 4 組（KPI／時間軸／
    主席的思考／發言分佈）。
  - `docs/design/README.md`：加一行「已知偏離」註記（這塊不在設計稿範圍）。
  - 新測試 `tests/test_minutes_preview.py`（4 項，全部 mock 掉 LLM）。
- **實測證據**：
  - 全域回歸：`.venv/bin/python -m pytest tests/ -q` → 施工前
    535 passed/26 skipped/2 xfailed；施工後 **539 passed/26 skipped/2
    xfailed**（多出的 4 項正是新測試，其餘數字完全沒變，無回歸）。
  - 前端邏輯：用 Node 直接執行從 `index.html` 逐字抽出的
    `renderMinutesGroup`／`renderMinutesLive`／`case "minutes":` 原始碼
    （非重寫版本），驗證 final=false 不呼叫 `setEnded()`、final=true 會
    呼叫且不清空剛才的預覽內容、空狀態印出「尚無資料」——三案例皆過。
    這不是這個 repo 既有的自動化框架（前端本來就沒有），是這次交付的
    手動驗證腳本，跑在 `/tmp` 底下，沒有寫進 repo。
- **卡住或未完的**：無阻塞。已知的刻意簡化——
  - 預覽不寫檔（只有正式收尾 `write_minutes` 才寫 host/minutes md），避免
    demo 期間在 `meetings/` 堆檔案；這是刻意偏離「跟正式版共用完全一樣的
    payload 形狀」的最小侵入判斷，理由見交付報告。
  - `watch_minutes` 沒有等 `self.chair` 就緒才跑（比照 `watch_glossary`
    的做法，不是疏漏）。
  - 前端沒有真的跑瀏覽器測試——這個 repo 有 playwright 測試檔
    （`test_spectator_phase.py` 等 3 支）但環境沒裝 `playwright` 套件，
    全部 skip（施工前就是如此，非本批造成）；沒有安裝它，因為那是環境
    層的缺口，超出這批工單範圍。
- **下一關該知道什麼**：
  - 這批東西在 worktree 分支上，**沒有合併回 main**，Zeal 看過再決定。
  - `MINUTES_PREVIEW_INTERVAL_S`（90 秒）與
    `MINUTES_PREVIEW_MIN_UTTERANCES`（6 句）都是 `live.py` 頂部的模組常數，
    demo 現場如果這個功能不穩，把前者調成一個很大的數字（例如
    `999999`）就等於直接關掉，不影響任何其他背景迴圈。
  - 若之後要幫這塊補設計稿，回頭看 `docs/design/README.md` 的偏離註記。
