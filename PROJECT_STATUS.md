# FUTURE 黑客松 × Ahem 執行狀態

最後更新：2026-09-03（Asia/Taipei）  
工作分支：`codex/future-hackathon-security`

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

## 尚未完成（2 項，需要真實環境）

- [ ] **Discord 真實語音回歸**：需要在授權的 Discord 測試伺服器，以實際 Bot、STT/TTS 帳號執行收音、發聲、斷線重連與安全日誌檢查。密鑰不得貼入 Issue、PR 或本文件。
- [ ] **共用 Wi-Fi／跨裝置邊界驗收**：需要在比賽現場或等價網路，驗證未授權裝置無法連線、Viewer 無法操作、Operator 才能切換階段／結束會議，以及 HTTPS 憑證鏈正常。

## 最新自動化證據

- `pytest`：521 passed、25 skipped、2 xfailed。
- `pip check`：No broken requirements found。
- `pip-audit --local`：No known vulnerabilities found。
- `bandit -lll -r src`：0 個 High severity finding。
- `sbom.cdx.json` SHA-256：`428dabd22a34571bc657e7dd9f474d0011269575ce464f00391f18146a95ef50`。

## 每次執行後的更新規則

1. 先執行與本次修改相符的測試與掃描。
2. 把實際結果、日期及必要的 commit SHA 寫回本文件。
3. 完成必須附可重現證據；人工環境尚未驗證時只能標示「尚未完成」或「待驗收」。
4. 不記錄 Token、語音密鑰、原始音訊、完整逐字稿或可識別個人的內容。
5. GitHub Project 狀態必須與本文件一致。

## 下一個行動

1. 由 Operator 在本機 Keychain 建立 KEK，並在安全測試伺服器設定環境變數。
2. 依 `docs/demo-security-runbook.md` 完成 Discord 回歸並記錄非敏感結果。
3. 到共用 Wi-Fi 執行網路與越權矩陣。
4. 通過後把上述兩項勾選完成，再將 GitHub Project 對應項目改為「已完成」。
