# 安全回報 / Security

**中文**：請勿在公開 issue 揭露安全問題。請透過 GitHub 的私密漏洞回報（Security → Report a vulnerability）聯繫維護者。

觀戰服務預設只綁定 `127.0.0.1`，且要求 Viewer／Operator 短效 Token。Token 必須存放於 Keychain、KMS 或執行環境的秘密管理器，不得提交到 Git 或寫入日誌。對外展示時只開放經驗證的 HTTPS 反向代理，不得公開原始服務埠。

**English**: Please do not disclose security issues in public issues. Use GitHub's private vulnerability reporting (Security → Report a vulnerability).

The spectator server binds to `127.0.0.1` by default and requires short-lived Viewer/Operator tokens. Store tokens in Keychain, KMS, or the runtime secret manager. When remote display is required, expose only an authenticated HTTPS reverse proxy and never the raw service port.
