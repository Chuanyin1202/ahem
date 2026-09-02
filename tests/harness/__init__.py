"""不靠真人的迭代測試台（v2，T19 第 1 步）。

只做提案 docs/specs/2026-08-28-eval-harness-proposal.md 第七節
第 1 步的範圍：把四個已知 bug 的回歸收斂進同一套 fixture／runner，
不做時鐘契約落地（第 3 步）、不做完整劇本格式（第 4 步）、不做錄放器
（第 5 步）。各模組職責：

- clock.py         ：VirtualClock，regression suite 唯一的時間來源
- frames.py        ：帶編號的假 PCM 幀（PCM 完整性 oracle 用）
- fake_voice.py    ：FakeVoice（吐編號幀）、FakeEarcon
- fake_player.py   ：FakePlayer，播放執行緒的假模擬＋frame ledger
- chair_runner.py  ：ChairHarness，Chair 場景 runner（迴歸 1、2 共用）
- live_shutdown_driver.py ：迴歸 4 的 subprocess 驅動腳本（不連 Discord／LLM）
"""
