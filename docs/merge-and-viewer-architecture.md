# Demo 與機密會議的觀看／合併架構

## 決策

Ahem 保留同一套 Python 核心與相同授權程式碼，不為 demo 另建一條繞過安全檢查的
執行路徑。差異只由部署設定表達：

| 設定檔 | 使用者 | Viewer 內容 | 使用時機 |
|---|---|---|---|
| Demo participant | 已在 Discord 語音頻道內、持有 Viewer token 的參與者與評審 | full | 合成資料或已明確同意公開的 demo |
| Private observer | 合法登入但不是會議參與者的觀察者 | redacted | 機密會議、營運監控 |
| Operator | 主持與維運人員 | full + control | 兩種部署均適用 |

Demo participant 設定必須同時使用：

```dotenv
AHEM_VIEWER_CONTENT=full
AHEM_DEMO_PUBLIC_TRANSCRIPT=1
```

Private observer 預設為：

```dotenv
AHEM_VIEWER_CONTENT=redacted
AHEM_DEMO_PUBLIC_TRANSCRIPT=0
```

缺少 demo acknowledgement 時，full 模式 fail closed。未持有合法 session 的請求仍在
`/events` 前被拒絕；內容模式不會改變身份驗證與 Operator 寫入權限。

## 分階段合併

1. 基礎層：Linux KEK provider、秘密掃描 CI、style baseline reset。
2. 存取層：Viewer／Operator session、origin、cookie、DLP。
3. 資料層：加密、解密稽核、retention。
4. 部署層：Pi systemd credential、Caddy/Tunnel 與實機 smoke test。

目前大型 PR 保持開啟。若 demo 前不接受 Viewer 內容模式的產品取捨，只拆第 1 層；
不要用關閉測試、測試環境變數或公開 `/events` 作為替代方案。

## PR 證據契約

每次提交由 `scripts/security-check.sh` 產生 `.security-run/pr-evidence.md`，GitHub Actions
同時把它寫入 Job Summary 並連同 pytest log、JSON 摘要和 SBOM 上傳。證據至少包含：

- commit SHA 與執行環境；
- 實際測試數量與 exit 0；
- secrets、pip check、pip-audit、Bandit 結果；
- `PYTEST_CURRENT_TEST` 全 repo 搜尋結果；
- 可定位的安全核心檔案。

真實 Discord、Pi、雲端 API 與私有 holdout 不可由離線測試冒充；沒有執行時必須在
PR 中標為尚未驗證。
