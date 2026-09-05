# 真實會議資料（holdout）

這個目錄放**你自己的**真實會議紀錄，供 `experiments/rescore_slow_path.py` 重評與 `experiments/score_run.py` 計分，
也供 `tests/harness/` 裡三個回歸測試與 `tests/test_score_run.py` 的一個案例使用（資料不在時那些測試自動 skip）。
目錄內容除本檔外不進版控——會議紀錄含與會者真實對話。

## 怎麼取得一份事件檔

用 `meeting_host.live` 主持一場會議，結束（Ctrl-C 或 SIGTERM）後 `meetings/` 會留下：

| 檔案 | 內容 |
|---|---|
| `meeting-<秒>.events.jsonl` | **事件流**，重評與計分的唯一輸入 |
| `meeting-<秒>.host.md` | 主持記錄：每次介入的時間、類型、理由 |
| `meeting-<秒>.minutes.md` | 會議產出：決議、待辦、未解決事項、立場 |

把它們複製到 `experiments/holdout/<案例名>/`，事件檔改名為 `meeting.events.jsonl`。
案例名慣例：`YYYY-MM-DD-<人數>-person`。

## 事件檔格式

每行一個 JSON：`{"kind": ..., "t": <會議相對秒>, "data": {...}}`。schema 定義在 `src/meeting_host/events.py`。重評與計分會讀的：

| kind | data | 用途 |
|---|---|---|
| `meeting` | `topic, duration_min, phase, participants` | 會議設定 |
| `utterance` | `speaker, text, start, end` | 逐字稿（STT 的最終文字） |
| `speaking` | `speaker, active` | STT 層的說話中／停止 |
| `voice` | `speaker, active` | RTP 層的麥克風活動 |
| `fast_timer` | `run, silent{人: 秒}, remaining` | 每秒一筆；**重建自我驗證的不變量** |
| `share` | `{人: 佔比}` | 每次 commit／出聲一筆；**重建自我驗證的不變量** |
| `slow_score` | 三軸分數、`type`、`verdict`、`utterance`、`admissible`、`reason` | 慢路每次判斷 |
| `queued` / `spoken` / `failed` / `dropped` | `kind, target, text, hard, reason` | 介入的生命週期；計分只認 `spoken` |
| `glossary`、`hearing`、`minutes` | — | 術語卡、失聰偵測、會後記錄 |

先跑 `--verify` 確認事件檔能被工具正確重建，對不上就不要拿去重評：

```bash
PYTHONPATH=src python experiments/rescore_slow_path.py experiments/holdout/<案例>/meeting.events.jsonl --verify
```

## 標註：`labels.json`

計分以**窗口**為單位，不逐點標。`experiments/score_run.py` 只讀這些欄位：

```json
{
  "case_id": "<案例名>",
  "duration_seconds": 2605,
  "windows": [
    {"id": "O1", "kind": "opportunity",     "range_seconds": [1009, 1280], "expect_type": null,
     "why": "為什麼主席這段該開口", "scored": true},
    {"id": "O2", "kind": "opportunity",     "range_seconds": [1500, 1620], "expect_type": "議程超時",
     "why": "...", "scored": true},
    {"id": "S1", "kind": "no_intervention", "range_seconds": [743, 825],
     "why": "為什麼這段不該開口", "scored": true},
    {"id": "PRE","kind": "no_intervention", "range_seconds": [0, 738],
     "why": "...", "scored": false, "excluded_reason": "單人時段，不構成會議"}
  ]
}
```

- `opportunity`：主席**該開口**的機會。一個窗口最多算一次命中，同窗口第二次起算誤報。
  `expect_type` 填類型（離題／重複／假共識／僵局／事實錯誤／發言權失衡／發言超時／有人被冷落／議程超時／全場沉默）則要求類型相符才算命中；`null` 為不限——**不限型別的窗口同時屬於快路與慢路**，命中歸給實際接住它的那條路徑（2026-09-05 修正，見 validation-log #9-4）。
- `no_intervention`：**不該開口**的區間，裡面任何介入都算誤報。
- `scored: false`：排除不計分（例如單人時段、STT 已失效的尾段），附 `excluded_reason`。
- ⚠️ **沒有被任何窗口涵蓋的時間，裡面的介入一律算誤報**。所以留白不是中立，等於宣告「這裡不該講」。
- 窗口邊界要從**逐字稿或規則門檻**推，不要貼著主席實際介入的時間畫——貼著畫的窗口永遠推翻不了主席。

### 階段真值：`phase_truth`（選填，給階段偵測用）

如果這場會議真的走過不同階段，在 `labels.json` 加一個 `phase_truth`，由在場者依序標出每段的階段：

```json
"phase_truth": [
  {"phase": "發散期", "range_seconds": [0, 900]},
  {"phase": "呻吟區", "range_seconds": [900, 1500]},
  {"phase": "收斂期", "range_seconds": [1500, 2100]}
]
```

然後對真值計分（每分鐘一次 LLM 讀數，遲滯後看它有沒有在每個窗口內切到對的階段、切換延遲幾秒、有沒有誤切）：

```bash
PYTHONPATH=src python experiments/phase_replay.py experiments/holdout/<案例>/meeting.events.jsonl \
    --truth experiments/holdout/<案例>/labels.json
```

沒有 `phase_truth` 時用 `--expect <階段>` 做反面驗證（全程單一階段的錄音上不得切換）。
標階段時的判準見 `src/meeting_host/phase.py` 的 `CRITERIA`——特別注意：**針對主席、工具或流程的爭執不算呻吟區**，只有在議題上走不出去的衝突才算。

其他欄位（`participants`、`topic`、`provenance` 的程式碼 SHA 與常數、`labelled_by`、`notes`）是給人看的，計分不讀，但建議填：
`provenance.code` 填主持那場會議時的 commit，`fast_path`／`slow_path` 填當時生效的常數，之後才對得回「這個數字是哪一版跑的」。

標註的分工：從事件檔整理出「主席實際開口的每一次」「想開口但沒出聲的」「連續數分鐘沉默的區間」給人逐題判斷，
人只回答判斷題，時段換算與 provenance 由程式端補。

## 重評與計分

```bash
# 用現行 prompt 重跑每個慢路評分點（呼叫 LLM），套窗口計分
PYTHONPATH=src python experiments/rescore_slow_path.py experiments/holdout/<案例>/meeting.events.jsonl \
    --labels experiments/holdout/<案例>/labels.json
# 同一批點跑 5 輪量穩定度——單次結果只是一個抽樣，不要拿單次當門檻依據
PYTHONPATH=src python experiments/rescore_slow_path.py ... --rounds 5
# 只從快取重算指標，不呼叫 LLM
PYTHONPATH=src python experiments/rescore_slow_path.py ... --report-only
```

輸出在 `experiments/out/rescore-<案例>/`（不進版控）：`rescored.json`、`rescored.rounds.json`、`stability.json`、`score.*.json`。
