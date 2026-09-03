# FUTURE 黑客松安全 Demo Runbook

## 展示前

- 使用獨立 AP、有線網路或同機投影。
- 確認 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 只顯示 `127.0.0.1`。
- Viewer／Operator Token 由秘密管理器注入，彼此不同且至少 32 字元。
- 確認 Keychain KEK 存在；設定 `AHEM_DISABLE_WEB_SEARCH=1`。
- 朗讀隱私告知並取得所有參與者同意。
- 執行預檢；結果必須為 `READY`：

```bash
PYTHONPATH=src .venv/bin/python -m meeting_host.preflight --mode local
```

## 啟動

```bash
PYTHONPATH=src python -m meeting_host.live \
  --topic "FUTURE 黑客松 Demo" --duration 10 --say-hello \
  --spectator-port 8765 --privacy-mode strict --consent
```

## 驗收

- 未帶 Token 的 `/events` 回 401。
- Viewer 呼叫 `/phase`、`/end` 回 401。
- 非信任 Origin 的控制請求回 403。
- 網址 fragment 的 Token 交換成短效 HttpOnly Cookie 後會立即消失；`/events?token=...` 回 401。
- Viewer Cookie 無法呼叫 `/phase`、`/end`；竄改或過期 Cookie 回 401。
- `meetings/` 為 0700；檔案為 0600，嚴格模式只有 `.ahem` 密文。
- 修改密文任一位元後無法解密。

## 展示後

```bash
PYTHONPATH=src python -m meeting_host.retention --directory meetings --ttl-hours 24
# 核對預覽清單後，才加入 --apply 執行
```
