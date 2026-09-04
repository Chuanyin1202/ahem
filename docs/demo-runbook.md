# Demo 現場流程（Runbook）

> 目標：現場任何人照這份就能開會，出事時知道下一步。本頁只提供操作流程；資安驗收以 [`demo-security-runbook.md`](demo-security-runbook.md) 為準。

## 會前（前一晚 ＋ 當天早上各跑一次）

```bash
ssh pi5
cd ~/meeting-host-agent
PYTHONPATH=src .venv/bin/python -c "from meeting_host import live, phase, style; print('載入 OK')"
PYTHONPATH=src .venv/bin/python -m meeting_host.preflight --mode local
```

- 預檢必須顯示 `READY`；它只檢查秘密是否存在與長度，不會輸出值。Raspberry Pi 上的 KEK 來自 systemd credential 或 0600 Linux secret file。不要用 `grep`、shell trace 或截圖顯示 `.env`。
- Discord：bot 已在授權測試伺服器裡，對指定語音頻道有連線與發言權限；頻道 ID 由 Operator 現場確認。
- 投影：優先在同一台主機開觀戰畫面。跨裝置展示必須先完成 Caddy HTTPS／防火牆設定與現場網路驗收。
- Viewer 與 Operator 使用不同且至少 32 字元的 Token；Token 只能放在網址 fragment 做一次性交換，不能放 query string。
- 給評審的公開連結預設只有去識別狀態。若要展示完整逐字稿，必須使用無真實個資的 demo 腳本，並同時設 `AHEM_VIEWER_CONTENT=full` 與 `AHEM_DEMO_PUBLIC_TRANSCRIPT=1`。

## 開始

```bash
cd ~/meeting-host-agent
PYTHONPATH=src nohup .venv/bin/python -u -m meeting_host.live \
    --topic "<題目>" --duration <分鐘> --say-hello --spectator-port 8765 \
    --channel <授權測試頻道 ID> --auto-phase suggest \
    --privacy-mode strict --consent \
    > live-$(date +%m%d-%H%M).out 2>&1 < /dev/null &
tail -f live-*.out        # 看到「已登入」「加入語音頻道」「觀戰 UI：http://…」才算起來
```

本機 Viewer 開啟 `http://127.0.0.1:8765/#token=<Viewer Token>`；Operator 改用 Operator Token。交換成功後 fragment 會被清除，之後使用短效 HttpOnly Cookie。不可把完整網址貼進聊天、Issue 或投影畫面。

- `--auto-phase suggest`：階段只在狀態列括號提示，切換由人在畫面點選單。不放心就拿掉。
- `--style strict|gentle|efficient`：主持風格檔位，未調校，只在想展示「同一場會議不同風格」時用。
- 與會者先進頻道再起 bot 也可以；bot 會等第一個真人的音訊通了才問候。

## 會中

- 狀態列顯示現在階段與主席狀態（聆聽中／等停頓／發言中）。點時鐘旁的階段選單可手動切換。
- 右欄「主席的思考」每 5 秒一筆：開口／受阻／忍住。**忍住的次數是產品的一部分，講給評審聽。**
- 左欄會出現不出聲的術語補充卡，那不是主席開口。
- 有人說話時左欄底部有一行活的文字跟著長（帶游標），停頓後變成正式的一句——那是 STT 的即時輸出，可以指給評審看「它在聽」。

## 出事時

| 狀況 | 怎麼看 | 怎麼辦 |
|---|---|---|
| 主席不講話、右欄有「主席講不出話」告示 | log 出現 TTS 授權或額度錯誤，但不含秘密 | 停止 Demo；由憑證持有人在不投影的受控終端更新秘密，再重新預檢與啟動。不要在現場聊天或 Issue 傳遞 key |
| 左欄「主席聽不到」告示 | log 有 STT 斷線／`hearing` 事件 | 同上，STT 與 TTS 共用同一把 key |
| 主席重複打斷同一人 | 觀戰畫面連續同型介入 | 手動把階段切到「拉鋸」（該階段主席最克制）；或 `kill -TERM` 重啟並加 `--style gentle` |
| 觀戰畫面不更新 | 頁面「連線中斷」 | 重新整理；bot 還活著就會重送全量 snapshot |
| 整個掛掉 | 行程不在 | 重啟同一指令；不要在現場改程式 |
| 從 ssh 起 bot 後 ssh 不回來 | 指令卡住 | 啟動指令要有 `< /dev/null`（stdin 沒關會把 ssh 通道抓住）；bot 其實已經起了，另開 ssh 查即可 |

## 結束

```bash
# 一定要送給 python 本體，不是外層的 bash（它的參數字串也含 meeting_host.live，粗糙的 pgrep 會先抓到它）
kill -TERM $(pgrep -f '^\.venv/bin/python -u -m meeting_host\.live')
# 或畫面右下「結束會議」；兩者走同一條 shutdown。約 10 秒內結束並寫記錄
ls -t meetings/ | head -4   # meeting-<秒>.events.jsonl / .host.md / .minutes.md / .log
```

`minutes.md` 是會議產出（決議、待辦、未解決、立場），`host.md` 是主持記錄（每次介入的時間、類型、理由）——收尾時把 `host.md` 打出來給評審看。

## 不做的事

- 現場不改 prompt、不改門檻常數、不裝套件。
- 不做本地備援；假設雲端與網路正常，額度靠兩個帳號輪替。
- 不停用 production 安全模式、不共用 Viewer／Operator Token、不用 HTTP 對外公開觀戰服務。
- 不展示 `.env`、Keychain、完整逐字稿、原始音訊或含 Token 的網址。
