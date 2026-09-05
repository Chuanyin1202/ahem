# 已知但延後處理的缺陷

> 依 CLAUDE.md「已知但延後處理的缺陷一律進追蹤清單」規則建立。每次開新一批施工前先掃一眼這裡有沒有能順手處理的。

## 1. Track C 的 Enter 鍵補丁是解一個不存在的問題，可刪

- **發現時間**：2026-09-05
- **發現於哪一批**：Track C 獨立審計（agent `a7a39b09ef2807203`）
- **內容**：`src/meeting_host/spectator/index.html` 為了「補Chrome原生`<summary>`不支援Enter鍵」加了約8行JS（含keydown/keyup與preventDefault）。審計用零JS對照組實測，Chrome原生`<summary>`本來就支援Enter鍵切換，這個前提不成立。
- **根因**：施工者的鍵盤測試方法本身有瑕疵，誤判成「不支援」。
- **影響範圍**：`src/meeting_host/spectator/index.html`，收合抽屜的鍵盤操作。目前行為正確（4次Enter/4次Space交替測試通過、無雙重觸發），只是多餘。
- **狀態**：待處理。**demo（2026-09-06）之後再刪**，現在動它是不必要的風險。馬尾判斷：`net -19行`。

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
- **狀態**：待處理。優先度最低，demo若全程用滑鼠操作不受影響。
