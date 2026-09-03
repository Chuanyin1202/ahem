# FUTURE 黑客松資安驗收紀錄

驗收日期：2026-09-03
基準 commit：`b527ec685c9d9b22ff76135f2d379a4afe32cf58`
實作分支：`codex/future-hackathon-security`

## 自動化結果

- pytest：521 passed、25 skipped、2 xfailed。
- pip check：No broken requirements found。
- pip-audit：No known vulnerabilities found。
- Bandit（High gate）：0 High。
- CycloneDX SBOM：`sbom.cdx.json`。
- SBOM SHA-256：`428dabd22a34571bc657e7dd9f474d0011269575ce464f00391f18146a95ef50`。

Skipped 項目需要真實 Discord／API／Playwright 環境；本次未使用真實 Token，
也不把這些結果描述為現場整合測試。兩個 xfail 為上游既有的預期失敗。

## 已驗證控制

- CryptoError 日誌不含 voice secret 或封包內容。
- Spectator production 預設綁定 loopback，缺少強 Token 時拒絕啟動。
- Viewer／Operator 權限分離，未授權回 401、非信任 Origin 回 403。
- Viewer 事件會隱去姓名、逐字稿、會議文件與本機路徑。
- CSP、no-referrer、nosniff、frame deny 及 Permissions-Policy 已加入。
- 會議目錄與檔案權限分別為 0700／0600。
- AES-256-GCM 每物件獨立 DEK／Nonce；竄改密文會拒絕解密。
- 解密要求 Operator 與用途，稽核不記錄明文身分。
- 嚴格模式未取得同意時，外部 AI 資料流 fail closed。
- 保存工具預設 dry-run，僅處理白名單內的到期會議產物。

## 現場仍需人工驗證

- 真實 Discord 語音收發與 ElevenLabs/OpenAI 帳號額度。
- 競賽網路上的反向代理 TLS 憑證與投影機來源 IP。
- macOS Keychain KEK 存取權限。
- 主持人完成隱私告知，所有新加入者均明確同意。
- Demo 後核對保存期限與備份清除。
