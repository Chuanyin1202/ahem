# 貢獻指南 / Contributing

**中文**

- 從 `codex/future-hackathon-security` 建立短分支；一個 PR 只處理一個可獨立驗收的問題。
- 提交前跑 `make test`、`make secrets` 與 `make security`。沒有私人資料或真實服務憑證時，對應測試會明確 skip，不得改成假成功。
- 觀戰 UI 測試使用 `requirements-dev.txt` 與 Playwright Chromium；執行 `make eval-ui` 必須真的通過，不再把缺少瀏覽器當成果。
- 改動判斷邏輯或 prompt 時，請用 `experiments/rescore_slow_path.py --rounds 5` 在你自己的會議資料上量測，並在 PR 描述附上前後對照。單次結果不足以支持結論。
- 不要提交任何真實會議的逐字稿、事件檔或由它們衍生的量測產物。`experiments/holdout/` 與 `experiments/out/` 已在 `.gitignore`。
- commit 訊息使用英文，格式 `type(scope): description`。

**English**

- Branch from `codex/future-hackathon-security`; keep each PR independently reviewable.
- Run `make test`, `make secrets`, and `make security` before submitting. Tests that require private data or authorised services must skip explicitly rather than report a synthetic success.
- Install `requirements-dev.txt` and Playwright Chromium, then run `make eval-ui`; a missing browser is not completion evidence.
- When changing judgement logic or prompts, measure with `experiments/rescore_slow_path.py --rounds 5` on your own meeting data and include the before/after in the PR. A single run does not support a conclusion.
- Never commit real meeting transcripts, event logs, or measurements derived from them. `experiments/holdout/` and `experiments/out/` are git-ignored.
- Commit messages in English, `type(scope): description`.
