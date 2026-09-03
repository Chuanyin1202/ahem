# FUTURE 黑客松 × Ahem 執行狀態

最後更新：2026-09-03（Asia/Taipei）

工作分支：`codex/future-hackathon-security`

GitHub fork：<https://github.com/billiswen-png/ahem>

這份文件是團隊的單一進度入口。每次執行、測試或交付後都必須更新日期、證據與未完成項目；沒有證據的項目不得標示完成。

## 已完成（11 項，等待團隊／上游驗收）

- [x] 移除語音密鑰與原始封包的日誌輸出。
- [x] Viewer／Operator Token 分權、Origin 檢查與安全回應標頭。
- [x] Spectator 預設只監聽 `127.0.0.1`，並提供 Caddy HTTPS 與 pf 防火牆範例。
- [x] 會議檔案採私有目錄與 `0600` 權限，避免一般使用者誤讀。
- [x] 個資告知、參與者同意閘門與 production 安全模式。
- [x] 每場會議獨立資料金鑰、AES-256-GCM Envelope Encryption 與 Keychain KEK。
- [x] 24 小時保存期限與白名單範圍自動刪除工具。
- [x] 稽核紀錄去識別、Viewer 最小揭露、DLP 與 Prompt Injection 邊界。
- [x] 資安架構、Demo 安全流程與團隊交付文件。
- [x] SBOM、CI 測試、Bandit 高風險門檻與 pip-audit。
- [x] 可向 Ahem 上游提交的 Issue／PR 拆分提案。

## 本次追加完成

- [x] Spectator Token 不再出現在 SSE query string；網址 fragment 只用於一次交換一小時短效、帶簽章的 HttpOnly／SameSite Cookie，交換後立即清除。
- [x] 短效 session 的角色、到期、竄改與不安全 trusted origin 均採 fail closed，並有自動測試。
- [x] 新增 `meeting_host.preflight`：啟動 Demo 前檢查角色 Token、Keychain KEK、加密儲存、loopback／LAN 邊界、Secure Cookie、資料夾權限與監聽埠，全程不輸出秘密值。
- [x] 以臨時隨機 Token 與測試 KEK 實跑本機預檢邏輯，所有檢查通過並得到 `READY`；真實 CLI 會因主機尚未建立 KEK／Token 而正確顯示 `BLOCKED`。
- [x] `scripts/security-check.sh` 現在每次都重新產生 CycloneDX SBOM，再更新本文件的雜湊。
- [x] Playwright／Chromium 已納入 `requirements-dev.txt` 與 CI；原本略過的 4 組觀戰 UI 模組已實跑，另新增 Viewer／Operator、短效 Cookie 與網址清理的瀏覽器端到端測試。
- [x] 新增 `make eval-regression`、`eval-ui`、`eval-quality`、`eval-realtime`；需要私人標註或真實 Discord 的入口會在缺少明確條件時拒絕執行。
- [x] 主持風格每次切換前會回復基準門檻，避免同一程序沿用上一檔位設定；三檔門檻順序已有回歸測試。
- [x] 以 IAF 官方核心能力與 Liberating Structures 官方方法／原則補齊產品研究，並標明 Ahem 只協助流程、不可冒充認證引導師或繞過 Operator 決策。
- [x] 更新 Demo 操作手冊，移除會輸出秘密的 `.env`／API Key 檢查方式，改為 fail-closed 預檢、fragment 一次性交換與本機／HTTPS 安全邊界。
- [x] 同步原始 Ahem `237e945` 的 Spectator 未授權控制修正；保留其 `/phase`、`/end` 風險回歸，同時維持本分支更嚴格的雙角色、讀取端保護、loopback 與短效 session 設計。
- [x] 修正回放模式手動切換階段後畫面不立即更新的限制；現在會沿用原會議 metadata 重送狀態，可在無 Discord 情況下展示階段操作。
- [x] `Chair`、`Session`、背景輪詢、提示音等待與 `Voice` timeout 已支援同一個 `VirtualClock`；核心回歸不再因牆鐘等待而產生不穩定或拖慢。
- [x] 新增本機 TTS 合成音軌工具、三人三階段虛構情境、重疊 PCM 飽和混音測試與 `make eval-audio`；實際產物已驗證為 16kHz／mono／PCM16 並附不含逐句文字的 manifest。
- [x] Regression 明確採固定 stub 而不保存 LLM 回應快取，避免模型輸出引用逐字稿後落地；真模型 `eval-quality` 維持禁快取且至少五輪。

## 尚未完成（4 項，需要真實環境或私人資料）

- [ ] **Discord 真實語音回歸**：需要在授權的 Discord 測試伺服器，以實際 Bot、STT/TTS 帳號執行收音、發聲、斷線重連與安全日誌檢查。密鑰不得貼入 Issue、PR 或本文件。
- [ ] **共用 Wi-Fi／跨裝置邊界驗收**：需要在比賽現場或等價網路，驗證未授權裝置無法連線、Viewer 無法操作、Operator 才能切換階段／結束會議，以及 HTTPS 憑證鏈正常。
- [ ] **慢路判斷穩定度**：需要經同意且完成匿名化標註的真實會議 holdout，至少五輪重評；目前不能用公開合成資料取代後宣稱產品品質完成。
- [ ] **三階段與主持風格正面驗證**：需要一場實際走過發散期、呻吟區、收斂期的會議，並由人工記錄階段真值及三種風格的可接受度。

本機已完成上述項目所需的程式、離線測試、預檢與 runbook；目前電腦沒有 Ahem Keychain KEK、Viewer／Operator Token、Discord／ElevenLabs／OpenAI 憑證，也沒有可提交 repo 的私人 holdout，因此不能誠實宣稱已完成真實服務整合或模型品質驗收。建立或輸入秘密必須由持有人在受控終端操作；真實資料須先取得同意、去識別並依資料政策保存，不應寫入公開 repo、Project 或對話。

## 最新自動化證據

<!-- AUTO-SECURITY-RESULTS:START -->
- 執行時間：`2026-09-03 15:33:16 CST`。
- Git commit：`ca2b9a6`。
- `pytest`：557 passed、21 skipped、2 xfailed。
- `pip check`：No broken requirements found。
- `pip-audit --local`：No known vulnerabilities found。
- `bandit -lll -r src`：0 個 High severity finding。
- `sbom.cdx.json` SHA-256：`360ae8b21fb5c368c677302441da0917c6819428e28f888e6c930d3b0763135c`。
<!-- AUTO-SECURITY-RESULTS:END -->

## 最新簡報交付

- 已依最新資安補強內容，使用內建 Imagegen 逐頁生成 8 張完整 16:9 圖文投影片。
- 每一頁的標題、說明、數據與圖像都包含在生成圖片內；PPTX 僅負責依序承載八張滿版圖片。
- 已輸出 `Ahem_FUTURE黑客松_資安補強_全圖文版.pptx`。
- 已用同一組八張原圖輸出 `output/pdf/Ahem_FUTURE黑客松_資安補強_全圖文版.pdf`，並完成逐頁渲染檢查。
- 第 7 頁初稿曾出現完成清單文字縮寫錯誤，已重新生成並改用修正版。

## 每次執行後的更新規則

1. 先執行與本次修改相符的測試與掃描。
2. 把實際結果、日期及必要的 commit SHA 寫回本文件。
3. 完成必須附可重現證據；人工環境尚未驗證時只能標示「尚未完成」或「待驗收」。
4. 不記錄 Token、語音密鑰、原始音訊、完整逐字稿或可識別個人的內容。
5. GitHub Project 狀態必須與本文件一致。

執行 `scripts/security-check.sh` 可一次完成本機安全檢查並自動更新上方證據區塊。這個流程只保存統計與雜湊，不保存測試輸出中的秘密或會議內容。

## 下一個行動

1. 由 Operator 在本機 Keychain 建立 KEK，並在安全測試伺服器設定環境變數。
2. 依 `docs/demo-security-runbook.md` 完成 Discord 回歸並記錄非敏感結果。
3. 到共用 Wi-Fi 執行網路與越權矩陣。
4. 通過後把上述兩項勾選完成，再將 GitHub Project 對應項目改為「已完成」。

## GitHub Project 同步狀態

- 2026-09-03 已補齊全部 13 張 Project 任務卡的完整說明；每張均包含目的、執行內容、完成定義、驗收方式與 Repo 證據，並逐張確認不再顯示 `No description provided`。
- 11 個已有程式／文件證據的項目：`待驗收`。
- Discord 真實語音回歸：`進行中`。
- 共用 Wi-Fi／越權／日誌與加密現場測試：`進行中`。
- fork 與 `codex/future-hackathon-security` 分支已建立；遠端安全程式 commit：`70bd707`。
- 受 Git Data API 單次傳輸限制，1.7 MB 的生成式架構 PNG 未包含在遠端 commit；原圖與完整簡報保留於本機交付目錄。
