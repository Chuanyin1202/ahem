"""迴歸 4（提案 §3 第四列，`suite: realtime`）：關閉不寫記錄。

真根因見 tests/test_live_shutdown.py 開頭的說明（T-G／T-I）：
`asyncio.Runner` 對 SIGINT 的優雅取消只讓 `gather(*tasks)` 那次 await 拋
一次 `CancelledError`；`tasks` 裡的個別背景 task 這時還沒真正收尾，若在
它們收尾前就呼叫 `bot.close()`，discord.py 會因為 gateway／語音的讀取
迴圈被腰斬而卡死等不到 ack；SIGTERM 預設行為是直接砍死行程，`shutdown()`
根本跑不到；非互動 shell 背景工作會讓子行程繼承 SIG_IGN 的 SIGINT。

這條回歸依提案 §1 的 suite 表格本來就該用真實時鐘（`suite: realtime`）——
跟迴歸 1–3 不同，不需要也不該套 VirtualClock：程序生命週期、真實訊號時序，
本質上就是要跑真實時間，這裡沒有時鐘契約缺口可言。

對應既有覆蓋（本檔不重寫，改用不連 Discord 的驅動腳本重現同一段收尾骨架）：

- tests/test_live_shutdown.py::test_sigterm_triggers_graceful_shutdown
- tests/test_live_shutdown.py::test_sigint_triggers_graceful_shutdown_when_inherited_as_ignored

這兩條原本的測試標了 `@requires_discord_env`：需要 `.env`（真的
DISCORD_BOT_TOKEN／ELEVENLABS_API_KEY）才會跑，且會真的呼叫 `bot.start()`
連線 Discord gateway、`write_minutes()` 也會真的打 LLM API——這張工單
明確禁止連線 Discord、禁止打任何 LLM／TTS API，所以另外寫一支不需要真實
bot／STT／TTS 的驅動腳本（tests/harness/live_shutdown_driver.py），只重現
「訊號 → shutdown()」這段骨架：拿掉 Discord／LLM 依賴後，其餘行為與原測試
一致——真實 subprocess、真實訊號、限時優雅退出、檢查
summary／events.jsonl／minutes 三種產物是否落地。
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

_DRIVER = Path(__file__).parent / "live_shutdown_driver.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _spawn_driver(tmp_path: Path, **popen_kwargs) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    return subprocess.Popen(
        [sys.executable, "-u", str(_DRIVER)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=str(tmp_path), env=env,
        **popen_kwargs,
    )


def _wait_ready(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """等驅動腳本印出 READY（訊號 handler 已註冊完成）才送訊號——不用固定
    `time.sleep(5)` 猜時機（既有 `_spawn_live` 那組測試要猜是因為要等真的
    Discord 連線與登入，這裡沒有那個依賴，可以用更快、更確定的方式）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break  # 行程提早結束（掛了），跳出去讓下面的斷言噴出實際輸出
        if line.strip() == "READY":
            return
    proc.kill()
    out, _ = proc.communicate()
    raise AssertionError(f"驅動腳本 {timeout} 秒內沒印出 READY。輸出：\n{out}")


def _assert_records_written(tmp_path: Path, out: str) -> None:
    m = re.search(r"事件紀錄：(meetings/meeting-\d+\.events\.jsonl)", out)
    assert m, f"stdout 沒有印出「事件紀錄：…」，代表 shutdown() 沒跑到。完整輸出：\n{out}"
    events_path = tmp_path / m.group(1)
    assert events_path.exists(), f"{events_path} 應該存在但沒有"
    meetings_dir = events_path.parent
    # host.md／minutes.md 的檔名時間戳可能跟 events.jsonl 差 1 秒（見
    # meeting_host.minutes.write_minutes 的 docstring：兩邊各自取
    # int(time.time())，不保證同一秒），不能假設 stem 完全相同，用萬用字元
    # 確認「這種檔案至少寫出一份」即可。
    existing = sorted(p.name for p in meetings_dir.iterdir())
    assert list(meetings_dir.glob("meeting-*.log")), f"缺少逐字稿檔：{existing}"
    assert list(meetings_dir.glob("meeting-*.host.md")), f"缺少主持記錄（B 檔）：{existing}"
    assert list(meetings_dir.glob("meeting-*.minutes.md")), f"缺少會議產出（A 檔）：{existing}"


def test_sigterm_triggers_graceful_shutdown_without_discord(tmp_path):
    proc = _spawn_driver(tmp_path)
    out = ""
    try:
        _wait_ready(proc)
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            raise AssertionError(f"SIGTERM 後 10 秒仍未結束，代表沒有優雅關閉。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, f"returncode={proc.returncode}\n{out}"
    assert "會議摘要" in out
    _assert_records_written(tmp_path, out)


def test_sigint_triggers_graceful_shutdown_when_inherited_as_ignored(tmp_path):
    """真實重現部署情境：子行程 exec 前 SIGINT 就已經是 SIG_IGN（用
    preexec_fn 精確重現『非互動 shell 背景工作繼承 SIG_IGN』這個前提），
    外加 start_new_session=True 重現 setsid 的新 session。驗證
    `add_signal_handler` 真的能蓋掉繼承來的 SIG_IGN，kill -INT 一樣能優雅
    關閉。"""
    def _inherit_sig_ign():
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    proc = _spawn_driver(tmp_path, start_new_session=True, preexec_fn=_inherit_sig_ign)
    out = ""
    try:
        _wait_ready(proc)
        os.kill(proc.pid, signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            raise AssertionError(f"SIGINT（繼承 SIG_IGN 前提下）10 秒仍未結束。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, f"returncode={proc.returncode}\n{out}"
    assert "會議摘要" in out
    _assert_records_written(tmp_path, out)
