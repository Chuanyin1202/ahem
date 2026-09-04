# Ahem 安全架構

## 安全邊界

```text
投影裝置 ── HTTPS 443 + Viewer Token ──> 反向代理
                                             │ loopback
                                             ▼
                                      Ahem 127.0.0.1:8765
                                        │      │
                               Operator Token  └─ HTTPS egress allowlist
                                        │          Discord / ElevenLabs / OpenAI
                                        ▼
                         AES-256-GCM 會議產物（每場獨立 DEK）
                                        │
                              KEK provider（平台依賴）
                         macOS Keychain / Linux credential file
```

## 信任規則

1. Viewer 只可讀取受保護的即時事件。
2. Operator 才能執行 `/phase`、`/end` 與解密。
3. 應用服務只綁 `127.0.0.1`；遠端流量必須經 HTTPS 反向代理。
4. 真實會議使用 `--privacy-mode strict --consent`；沒有同意便中止。
5. 嚴格模式的會議檔不落明文；KEK 只從平台秘密儲存讀取。
6. 預設保存 24 小時，備份及匯出亦適用同一期限。

## KEK 初始化

### macOS Keychain

在受控終端產生 KEK，再由操作者親自存入 Keychain：

```bash
python -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())'
security add-generic-password -U -s ahem.envelope-kek -a "$USER" -w '<上一行輸出>'
```

不要把輸出貼到聊天室、Issue、`.env` 或 shell history。

### Raspberry Pi / Linux

優先使用 systemd credential；應用程式會從 `$CREDENTIALS_DIRECTORY/ahem-kek`
讀取，不會把 KEK 放入 environment：

```ini
# /etc/systemd/system/ahem.service.d/credentials.conf
[Service]
LoadCredential=ahem-kek:/etc/ahem/ahem-kek
```

`/etc/ahem/ahem-kek` 內容是 base64 編碼的 32-byte KEK，必須是一般檔案、
非符號連結且權限 `0600`。非 systemd 部署可設 `AHEM_KEK_FILE`
指向同等安全的絕對路徑；container 也可使用 `/run/secrets/ahem_kek`。
正式雲端應改用 KMS/HSM。

## Discord voice 相依套件例外

`discord-ext-voice_recv==0.5.2a179` 仍宣告舊的 `PyNaCl<1.6` 上限，但修補版本為
`PyNaCl==1.6.2`。本專案以 `scripts/install-secure.sh` 分階段安裝並執行
`pip check`，且用完整語音回歸測試守門。這是暫時相容性例外；上游更新正式版本後
應移除 `--no-deps`，不得長期忽略其他依賴。

## Token

`AHEM_VIEWER_TOKEN` 與 `AHEM_OPERATOR_TOKEN` 必須不同且至少 32 字元，以秘密管理器
注入。觀看頁以 `https://入口/#token=<token>` 開啟；fragment 不會進入 HTTP access
log。頁面會透過 `POST /session` 將 Token 一次交換成一小時、帶簽章的 HttpOnly／
SameSite Cookie，隨即清除網址與 JavaScript 記憶體中的 Token。`/events` 不再接受
query-string Token，Cookie 遭竄改或過期時會回 401。LAN／HTTPS 模式必須設定
`AHEM_COOKIE_SECURE=1`。

為了舊部署不在合併後直接中斷，只設 `AHEM_SPECTATOR_TOKEN` 時會以單向衍生方式
產生分離的 Viewer Token；新部署仍應明確設定兩個長 Token。若三者都未設定，
服務會產生本次進程專用的隨機 Token 並只在啟動終端顯示一次，不再因缺少新變數而中斷。

Viewer 預設為 `AHEM_VIEWER_CONTENT=redacted`。只有使用事先準備、無真實個資的
demo 資料時，才可同時設 `AHEM_VIEWER_CONTENT=full` 與
`AHEM_DEMO_PUBLIC_TRANSCRIPT=1`；缺任一開關就拒絕啟動。

## Demo 安全預檢

啟動真實服務前先執行：

```bash
PYTHONPATH=src .venv/bin/python -m meeting_host.preflight --mode local
# 經 HTTPS 反向代理提供第二台裝置時：
PYTHONPATH=src .venv/bin/python -m meeting_host.preflight --mode lan --host 127.0.0.1
```

工具只顯示通過、警告或阻擋原因，不會顯示 Token 值。只有結果為 `READY` 才能進入
Demo；`BLOCKED` 時先修正 Token 分離、加密儲存、資料夾權限、監聽位址或 HTTPS Cookie。
