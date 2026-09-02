# 貢獻指南 / Contributing

**中文**

- 提交前跑 `.venv/bin/python -m pytest tests/ -q`。沒有真實會議資料時，需要錄音的回歸測試會自動 skip，這是正常的。
- 觀戰 UI 的測試需要 Playwright：`.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium`。沒裝會 skip（6 個），不算失敗。
- 改動判斷邏輯或 prompt 時，請用 `experiments/rescore_slow_path.py --rounds 5` 在你自己的會議資料上量測，並在 PR 描述附上前後對照。單次結果不足以支持結論。
- 不要提交任何真實會議的逐字稿、事件檔或由它們衍生的量測產物。`experiments/holdout/` 與 `experiments/out/` 已在 `.gitignore`。
- commit 訊息使用英文，格式 `type(scope): description`。

**English**

- Run `.venv/bin/python -m pytest tests/ -q` before submitting. Regression tests that replay a real recording skip themselves when no data is present; that is expected.
- The spectator UI tests need Playwright: `.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium`. Without it they skip (6 tests); that is not a failure.
- When changing judgement logic or prompts, measure with `experiments/rescore_slow_path.py --rounds 5` on your own meeting data and include the before/after in the PR. A single run does not support a conclusion.
- Never commit real meeting transcripts, event logs, or measurements derived from them. `experiments/holdout/` and `experiments/out/` are git-ignored.
- Commit messages in English, `type(scope): description`.
