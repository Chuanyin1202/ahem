# PR #4 交易一致性修正驗證（2026-09-06）

本次變更基於 b37dde56f4a3cd8752ab9d0cf969f548ad722ef1；只修改選用後台的交易邊界，不修改主席、音訊或主線觀戰介面。

## 問題、修正與證據

| 問題 | 修正 | 回歸證據 |
| --- | --- | --- |
| 獨立稽核 commit 遇 SQLite read lock，503 後仍留下 transaction | 外層 BEGIN IMMEDIATE、commit 失敗 rollback；內層 savepoint 不提前提交 | 真實第二連線 read lock；503 後 in_transaction=False，DB 不變，解除鎖定後第一個匯入成功 |
| 資料先 commit、稽核後失敗，使用者看到失敗但已修改 | 業務資料與成功 audit 同一 transaction | 13 個 HTTP 案例注入失敗，逐一比較整份 DB dump、identities、sessions；重試成功 |
| 輪替失敗遺失原憑證與 session | DB 和 audit 提交後才更新記憶體 | 真實 commit lock 保留舊 token、sessions；成功輪替的新 token 可辨識 |
| 多次 audit 或 alert evaluation 提前提交 | savepoint 支援巢狀呼叫；health／rules 與 evaluation 一起提交 | 第二次 member audit 失敗回復第一筆；兩個 alert evaluation 失敗測試 |
| 匯入稽核失敗留下 receipt | events、receipt、audit 同交易 | 失敗全回復；重試成功；再次匯入只回傳原 ID、audit 僅一筆 |

程式：[enterprise.py](../../../src/meeting_host/enterprise.py)。
可重現測試：[test_enterprise_transactions.py](../../../tests/test_enterprise_transactions.py)。

## 本次實測

環境：macOS arm64、Python 3.13.5、既有 venv、Chrome／Playwright。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ../ahem/.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_enterprise_transactions.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:scripts ../ahem/.venv/bin/python -m pytest -p no:cacheprovider -p browser_test_profile -q -rs tests
git diff --check
```

- [交易回歸 log](transaction-tests.log)：19 passed，0.55 秒，退出碼 0。
- [完整套件 log](full-tests.log)：736 passed、21 skipped、2 xfailed、0 failed，44.73 秒，退出碼 0。
- git diff --check：無輸出，退出碼 0。
- 相對上一版 717 passed 增加 19 個測試；21 skipped 仍為 17 個私有 holdout 與 4 個真實 Discord opt-in；2 個既有 xfail。
- 本機 browser_test_profile 只替代 Google Fonts CSS，不攔截應用／安全 API，不驗證外部字型外觀。
- 本輪為交易修正，未新增 UI 截圖；舊截圖僅為歷史示意，不當成本次視覺驗收證據。故障由真實 SQLite 鎖定及注入測試證明。

## 部署與限制

- 不新增 schema、環境變數、套件或公開憑證；停止後台及 bridge、備份後更新並重啟。既有 audit.target 遷移注意事項仍適用。
- 必須維持 transaction 區塊內無 await；SQLite 連線仍供同一事件迴圈使用。憑證 helper 不允許外部交易包住，以免未提交就改記憶體。
- 這是資料庫交易一致性，不是網路 exactly-once。提交後 HTTP 回應遺失、程序崩潰時仍可能不知道操作是否成功；憑證可由另一 Operator 重新輪替，手動匯入沒有請求 idempotency key。
- 跨程序授權競爭、真正磁碟故障／斷電、Raspberry Pi 實機、長時間負載及真實語音服務仍未驗證；未消耗語音點數。
- 本修正無 DB 格式變更，可停服務回退至 b37dde5，但會重新帶回上述缺陷；回復資料備份可能恢復舊憑證，須另外評估。
- 僅更新既有 PR #4，不合併、不部署到正式主機。
