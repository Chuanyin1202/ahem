# Demo 現場流程（Runbook）

> 目標：現場任何人照這份就能開會，出事時知道下一步。所有指令以 Pi5 上的路徑為準。

## 會前（前一晚 ＋ 當天早上各跑一次）

```bash
ssh <DEPLOY_HOST>            # 部署主機，見下方「部署環境變數」
cd <DEPLOY_PATH>
grep -c '=' .env                              # 至少要有 ELEVENLABS_API_KEY、OPENAI_API_KEY、DISCORD_BOT_TOKEN、AHEM_PUBLIC_URL、AHEM_CHANNEL_ID
PYTHONPATH=src .venv/bin/python -c "from meeting_host import live, phase, style; print('載入 OK')"
# ElevenLabs：這把 key 還有額度嗎（需要 user_read 權限的 key）
K=$(grep '^ELEVENLABS_API_KEY=' .env | cut -d= -f2-); curl -s -H "xi-api-key: $K" https://api.elevenlabs.io/v1/user/subscription | grep -o '"character_count":[0-9]*,"character_limit":[0-9]*'
```

- Discord：bot 已在伺服器裡、對語音頻道有連線與發言權限。頻道 ID 放在部署主機的 `.env`（`AHEM_CHANNEL_ID`），**不要寫進這份文件或指令**——這個 repo 是公開的。開會前確認 `.env` 裡那個 ID 仍然是要用的頻道。
- 投影：觀戰畫面的對外網址由部署主機 `.env` 的 `AHEM_PUBLIC_URL` 決定（反向代理／tunnel 指到該機的 `localhost:8765`），用 1440×900 以上的解析度；瀏覽器先開好、確認能連。後端沒起時這個網址回 502，那是正常的。
  - **啟動橫幅印出來的網址就是要交出去的那個**。若橫幅底下出現「AHEM_PUBLIC_URL 未設定，上面是本機網址」的警告，代表你跑在沒設定的機器上，那串網址只有那台機器連得到——停下來，不要把它交給任何人。
  - 手機與平板都是單欄流式版面，不會橫向捲動（實測 360–1920px 全段無溢出），掃碼給評審看沒問題。
- 網路：Pi5 與投影電腦同一網段（或走 tunnel 對外）。
- 權杖：啟動會印兩個網址。**操作者**那個給自己（可切階段、可結束會議），**參與者**那個只能讀。
  - 畫面會投影，所以前端讀完 `?k=` 就把它從網址列抹掉，權杖不會出現在螢幕上。
  - 想固定就先 `export AHEM_SPECTATOR_TOKEN=<操作者>` 與 `export AHEM_VIEW_TOKEN=<參與者>` 再啟動，網址才能事先收藏。
  - **預設是私密的**：沒帶權杖的人開網址只會看到「需要權杖才能觀看」。真實會議把參與者網址貼進該場會議的 Discord 文字聊天即可，範圍自然等於進得了那個頻道的人。
  - **demo 現場評審不在 Discord 頻道裡**，要讓他們自己掃碼看就得加 `--public-read`——那等於完整逐字稿對外公開，只在 demo 用，彩排真實內容時不要加。

## 部署環境變數

機器相關的值全部放在**部署主機自己的 `.env`**，不進版控（這個 repo 是公開的）：

| 變數 | 內容 | 沒設會怎樣 |
|---|---|---|
| `AHEM_PUBLIC_URL` | 觀戰畫面對外的 base URL（反向代理／tunnel 的主機名） | 啟動橫幅退回 `http://localhost:<port>` 並印警告——那串只有該機器連得到 |
| `AHEM_CHANNEL_ID` | 預設加入的 Discord 語音頻道 ID | `--channel` 沒帶就不會自動進頻道 |

`<DEPLOY_HOST>` / `<DEPLOY_PATH>` 是你自己的 ssh 目標與部署路徑，記在本機、不要寫進這份文件。

> **2026-09-05 的事故**：真實會議要跑在部署主機上，卻被跑到開發機上；開發機沒有
> `AHEM_PUBLIC_URL`，橫幅安靜地印了 `http://localhost:8765/?k=…`，那串網址被交給
> 外網的人，完全打不開。現在橫幅會在沒設定時明講「這是本機網址」。

## 開始

```bash
cd ~/meeting-host-agent
PYTHONPATH=src nohup .venv/bin/python -u -m meeting_host.live \
    --topic "<題目>" --duration <分鐘> --say-hello --spectator-port 8765 \
    --auto-phase suggest \
    > live-$(date +%m%d-%H%M).out 2>&1 < /dev/null &
tail -f live-*.out        # 看到「已登入」「加入語音頻道」「觀戰 UI：http://…」才算起來
```

- **demo 現場要讓評審自己開連結就加 `--public-read`**：讀取端不設防，寫入端仍要操作者權杖。真實會議不要加。
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
| 主席不講話、右欄有「主席講不出話」告示 | log 出現 `TTS HTTP 401 … quota_exceeded` | ElevenLabs 額度用完。**換另一個帳號的 key**：改 `.env` 的 `ELEVENLABS_API_KEY`，`kill -TERM` 後重新啟動（會議狀態在記憶體，重啟等於新開一場；先前的記錄已寫在 `meetings/`） |
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
