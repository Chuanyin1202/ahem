# Ahem 完整系統架構與部署說明

> 適用版本：`codex/future-hackathon-security`，2026-09-04。本文以目前實作為準，不把 roadmap 當成已完成功能。

## 1. 目標與範圍

Ahem 是加入 Discord 語音頻道的即時 AI 會議主席。它使用每人獨立音軌判斷誰在說話，將語音轉成文字，由規則與 LLM 判斷是否介入，最後將主席話術播回頻道。

- 音源正式接入為 Discord。
- STT 固定為 ElevenLabs Scribe Realtime。
- TTS 可選 ElevenLabs 或 Azure Speech 台灣男／女聲。
- `--no-llm` 可停用慢路、話術生成、階段判斷與術語卡。
- Google Meet、單麥 diarization 與本地 STT 不在 demo 主路徑。

## 2. 系統全貌

```text
參與者 → Discord RTP → MeetingBot → 每人 PCM 音軌
                                      │
                                      ├→ ElevenLabs STT → MeetingState
                                      │                        ├→ 快路規則
                                      │                        └→ OpenAI 慢路
                                      │                                  │
                                      │                         安全／靜默／冷卻閘門
                                      │                                  ▼
                                      ← Discord Output ← Chair ← TTS

MeetingState → Event stream → Spectator SSE → loopback → HTTPS proxy
             └→ Minutes / events → AES-256-GCM → Raspberry Pi storage
```

## 3. 元件與責任

| 元件 | 責任 | 失敗原則 |
|---|---|---|
| `discord_source.py` | 頻道連線、使用者分軌、RTP activity | 不記錄封包或密鑰；重連後重建 output |
| `stt.py` | ElevenLabs WebSocket、partial/final 轉錄 | 更新失聰狀態；不無限堆積過期音訊 |
| `state.py` | 參與者、發言、時間、版本與介入狀態 | 過期 speaking 旗標不可永久阻擋主席 |
| `fast_path.py` | 超時、被冷落、沉默、議程超時 | 不依賴 LLM |
| `slow_path.py` | 離題、重複、假共識、僵局、事實錯誤 | 話術無法驗證就不介入 |
| `phase.py` / `style.py` | 階段與主持檔位 | 階段預設只建議；切檔先復原基準值 |
| `speaker.py` | Earcon、TTS、佇列與介入升級 | 通道滿、超時或額度不明時 fail closed |
| `spectator.py` | UI、SSE、Viewer/Operator session | 未授權不可讀 events；Viewer 不可操作 |
| `security.py` | 同意、檔案權限、封套加密、去識別 | 密鑰或密文異常不降級成明文 |
| `minutes.py` | 會議與主持紀錄 | 單一摘要失敗不拖垮其他收尾 |

## 4. 介入時序

1. Discord 將某位使用者的 PCM 音訊送到專屬 STT connection。
2. partial 只用於 UI；final utterance 才進入判斷與會後記錄。
3. 快路持續檢查時間規則；慢路約每 5 秒評分語意風險。
4. 候選介入通過收尾、STT 健康、冷卻、版本與話術驗證門。
5. 硬介入先播 earcon；軟介入等停頓，過久時依當下事實重建為硬介入。
6. TTS 輸出轉成 Discord 48 kHz stereo frame。
7. 第一個可聽 frame 真正被消費後才記為 `spoken`。

## 5. 資料流與外傳

| 資料 | 目的地 | 用途 |
|---|---|---|
| 每人音訊 | Discord → ElevenLabs | 即時 STT |
| final 逐字稿片段 | OpenAI | 慢路判斷與話術 |
| 主席話術 | ElevenLabs 或 Azure | 語音合成 |
| 術語與前後文 | OpenAI/搜尋（可關） | 術語卡 |
| events、摘要與主持紀錄 | Raspberry Pi | AES-256-GCM 密文，預設 24 小時保存 |

「有 TLS」不等於「沒有對外傳送」。真實會議前必須使用 [`privacy-notice.md`](privacy-notice.md) 取得所有人同意。

## 6. 信任邊界與權限

- Viewer 可讀去識別事件，不可呼叫 `/phase` 或 `/end`。
- Operator 可讀完整事件、改階段與結束會議。解密另需本機 KEK 與明確用途。
- Token 放 URL fragment，經 `POST /session` 換成一小時 HttpOnly、SameSite=Strict Cookie，然後清除 fragment。
- 公開入口必須 HTTPS；Ahem 本體只監聽 `127.0.0.1:8765`。
- trusted origin 不接受萬用字元、URL 帳密、路徑或非 loopback HTTP。

Viewer 預設隱去姓名、議題、逐字稿、主席話術、慢路理由、術語卡、會後文件與路徑。完整公開模式需同時設 `AHEM_VIEWER_CONTENT=full` 與 `AHEM_DEMO_PUBLIC_TRANSCRIPT=1`，且只能用合成資料。

## 7. 儲存與密鑰

- 每個產物產生獨立 256-bit DEK 與 nonce。
- 內容用 AES-256-GCM 加密，DEK 再由 KEK 包裝。
- AAD 綁定 schema、meeting ID、產物類型、建立時間與隱私模式；調包或修改會驗證失敗。
- macOS 用 Keychain；Linux 優先用 systemd `LoadCredential`。
- Linux secret 必須是絕對路徑、一般檔案、非 symlink、權限不超過 0600，且由 root 或服務帳號擁有。
- `meetings/` 為 0700；產物與 Azure 額度狀態為 0600，使用原子取代寫入。

## 8. 語音與額度

`AHEM_TTS_PROVIDER=elevenlabs|azure` 只切換 TTS，STT 仍用 ElevenLabs。Azure 可選
`female` 小辰或 `male` 雲哲，預設語速 `+12%`。ElevenLabs 以
`ELEVENLABS_TTS_GENDER=female|male` 選擇聲別，實際 Voice ID 由環境秘密設定提供；
男聲未提供 ID 時會 fail closed。發音替換只在 TTS 邊界，UI 與記錄保留原文。

Azure 本機計量在請求前預留字元，預設 80/90/95% 警告、95% 硬停。狀態檔無法解析時停止 TTS，不歸零後繼續計費。Azure Cost Management 預算通知仍需保留。

## 9. Raspberry Pi 5 部署

1. Repo 放 `/opt/ahem`，建立專用 `ahem` 帳號與 `/var/lib/ahem/meetings`。
2. 建立 venv，執行 `scripts/install-secure.sh`，確認 `pip check`。
3. 複製 [`ahem.env.example`](../deploy/ahem.env.example) 到 `/etc/ahem/ahem.env`，填值後設 0600。
4. 產生 base64 32-byte KEK 到 `/etc/ahem/ahem-kek`，設 0600，不放 env 或 Git。
5. 複製 [`ahem.service.example`](../deploy/ahem.service.example) 到 `/etc/systemd/system/ahem.service`。
6. 複製 `ahem-retention.service.example` 與 `ahem-retention.timer.example`，啟用每小時保存期清理。
7. 用 Cloudflare Tunnel 或 Caddy 將 HTTPS 入口反代到 loopback 8765，並將入口寫入 trusted origins。
8. 執行 `systemctl daemon-reload`，再啟用 Ahem 服務與 retention timer。`ExecStartPre` 未 READY 就不啟動。

systemd 範本使用唯讀系統、專用帳號、`UMask=0077`、限制可寫目錄與必要 network address families。憑證必須在 Pi 上由 Operator 填入，repo 只放空白範本。

## 10. 啟動、關閉與保存

預檢阻擋項：Viewer/Operator Token 不合格、Discord/STT/LLM/TTS 必要憑證缺失、未啟用加密、KEK 不可用、目錄權限錯誤、埠被佔用、對外模式沒有 HTTPS origin 或 Secure Cookie。

SIGINT、SIGTERM 與 Operator `/end` 都走同一優雅關閉路徑。Discord 附件上傳亦位於
關機保證區內，失敗不會跳過事件落盤與 Bot 關閉；systemd 預留 35 秒停止期限。
到期清除預設只預覽，需明確加 `--apply`；TTL 必須大於 0。

## 11. 失敗模式

| 問題 | 系統行為 | 處置 |
|---|---|---|
| ElevenLabs STT 失效 | hearing 變紅，主席不依賴錯誤轉錄開口 | 檢查額度、key 與網路；無法恢復就人工主持 |
| OpenAI 失效 | 慢路隔離，快路仍可工作 | 可用 `--no-llm`，並明示功能降級 |
| TTS 超時／額度滿 | 記錄 failed 並退避，不假裝已發言 | 切換已預檢 provider 或人工代讀 |
| Azure usage 檔損壞 | 停止 Azure TTS | 核對 portal 後由 Operator 修復計數 |
| Viewer Token 失效 | `/events` 401，不降級公開 | 重新交換 Token |
| KEK 不可用 | 預檢阻擋，不寫明文 | 修正 owner/mode/credential |
| Pi 重啟 | systemd 重啟服務，內存狀態不自動還原 | 保留密文，開新會議 |

## 12. 驗收與已知限制

可自動驗證：Python 與 Chromium 測試、秘密掃描、Bandit、dependency audit、SBOM、檔案權限、密文竄改、角色越權與優雅關閉。

必須在 Pi 與真實服務驗收：Discord 分軌與播放器重建、三個雲端服務的真實額度與延遲、HTTPS Host/Origin/Cookie、systemd credential owner/mode、SIGTERM 後的密文記錄，以及一場真正經過發散→挣扎→收旂的會議。

慢路判斷仍有多輪不穩定，不得以單次 demo 宣稱已解決。交付關卡為：`preflight READY` → 離線測試與掃描 → Pi 真實憑證 smoke test → 合成 demo 公開 Viewer → 真實會議維持 redacted Viewer。
