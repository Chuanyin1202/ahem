# 安全回報 / Security

**中文**：請勿在公開 issue 揭露安全問題。請透過 GitHub 的私密漏洞回報（Security → Report a vulnerability）聯繫維護者。

已知的設計限制：觀戰服務綁定 `0.0.0.0`，`/events` 會送出完整逐字稿，`POST /phase` 與 `POST /end` 沒有認證。請只在可信網路上執行，或以防火牆限制該埠。

**English**: Please do not disclose security issues in public issues. Use GitHub's private vulnerability reporting (Security → Report a vulnerability).

Known design limitation: the spectator server binds `0.0.0.0`, `/events` streams the full transcript, and `POST /phase` and `POST /end` are unauthenticated. Run it on a trusted network only, or firewall the port.
