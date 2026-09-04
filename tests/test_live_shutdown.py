"""T-G：Ctrl-C（SIGINT）時 live.py 必須保證寫出會議摘要／逐字稿／事件紀錄。

真根因（實測結果見 task-g-report.md 的重現輸出，不是純讀 code 猜的）：
asyncio.Runner 對 SIGINT 的優雅取消，只讓等在 `await asyncio.gather(*tasks)`
上的這一次 await 拋一次 CancelledError；`tasks` 列表裡的各別 task
（bot.start()／start_chair()／consume()／watch_fast()／watch_slow()）雖然被
gather 級聯 cancel()，但這時還沒真正收尾完成。若在它們收尾「前」就呼叫
`bot.close()`，discord.py 會因為 gateway／語音的讀取迴圈被腰斬而卡死等不到
ack（實測：直接呼叫 15 秒不回；先讓 tasks 收尾完再呼叫則正常在數秒內完成）。

以下用 FakeBot 模擬這個真實卡死語意：close() 要等所有背景 task 真的
收尾（cancel 被遞送到、except 區塊跑完）才會回傳——這正是實測卡住的那個依賴。
"""
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from meeting_host import live
from meeting_host.live import Session, shutdown
from meeting_host.state import MeetingState


class FakeBot:
    """close() 要等 tasks_done 被 set 才回傳——對應實測到的
    『bot.close() 在背景 task 真正收尾前呼叫就會卡住』。"""

    def __init__(self, tasks_done: asyncio.Event):
        self.tasks_done = tasks_done
        self.close_called = False
        self.closed = False

    async def close(self) -> None:
        self.close_called = True
        await self.tasks_done.wait()
        self.closed = True


async def _bg_task(counter: list[int], tasks_done: asyncio.Event) -> None:
    """模擬 bot.start()／consume() 等背景 task：收到 cancel() 才算收尾，
    全部收尾完才讓 tasks_done 事件亮起（FakeBot.close() 卡的就是這個）。"""
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        counter[0] -= 1
        if counter[0] == 0:
            tasks_done.set()
        raise


def _make_session() -> Session:
    st = MeetingState(topic="t", duration_min=30, participants=[])
    return Session(st)


async def _make_tasks_and_bot(n: int) -> tuple[list[asyncio.Task], "FakeBot"]:
    tasks_done = asyncio.Event()
    counter = [n]
    tasks = [asyncio.create_task(_bg_task(counter, tasks_done)) for _ in range(n)]
    # 讓每個 task 真的先跑到 await asyncio.sleep(10) 卡住——cancel() 打在「已經在等
    # 某個 future」的 task 上才會遞送進 except 區塊；打在「剛建立、迴圈還沒排程過」
    # 的 task 上，coroutine 連 try 都不會進去就直接被判定 cancelled（已用最小腳本驗證）。
    await asyncio.sleep(0)
    return tasks, FakeBot(tasks_done)


def test_shutdown_writes_summary_cancels_tasks_and_closes_bot(monkeypatch):
    """GREEN：live.shutdown() 依序完成 summary → 取消並等 tasks 收尾 → bot.close()。"""
    calls = []
    monkeypatch.setattr(live, "summary", lambda s: calls.append("summary"))

    async def go():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(4)
        await shutdown(session, bot, tasks)
        return tasks, bot

    tasks, bot = asyncio.run(go())
    assert calls == ["summary"]
    assert bot.close_called is True
    assert bot.closed is True
    assert all(t.done() for t in tasks)


def test_main_task_cancel_triggers_shutdown_end_to_end(monkeypatch):
    """端到端：main task 被 cancel()（模擬 SIGINT 的優雅取消路徑）。
    finally 呼叫 shutdown()，最終 summary 有跑、bot.close 有跑完、所有背景 task 都 done。"""
    calls = []
    monkeypatch.setattr(live, "summary", lambda s: calls.append("summary"))

    async def fake_main():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(2)
        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await shutdown(session, bot, tasks)
        return tasks, bot

    async def go():
        main_task = asyncio.ensure_future(fake_main())
        await asyncio.sleep(0.1)
        main_task.cancel()
        return await main_task

    tasks, bot = asyncio.run(go())
    assert calls == ["summary"]
    assert bot.closed is True
    assert all(t.done() for t in tasks)


def test_old_pattern_deadlocks_bot_close_before_tasks_settle(monkeypatch):
    """RED（修正前的真實行為）：舊版 `finally: await bot.close(); summary(session)`
    完全沒有先取消／等待 tasks，直接呼叫 bot.close()——對照 FakeBot 卡死語意，
    這一步會永遠等不到 tasks_done，summary 也就永遠跑不到。
    用 wait_for 包一個時間上限，逾時本身就是「舊流程會卡住」的證據
    （不用真的讓測試無限期掛住）。
    """
    calls = []
    monkeypatch.setattr(live, "summary", lambda s: calls.append("summary"))

    async def old_flow():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(4)
        # 舊版 live.py:347-353 的寫法：tasks 完全沒被取消，直接 await bot.close()
        await bot.close()
        live.summary(session)

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(old_flow(), timeout=0.3)

    asyncio.run(go())
    assert calls == []  # summary 從未被呼叫到——這就是回報症狀「沒印會議摘要」的根因


# ── T-I／T20：SIGTERM／繼承 SIG_IGN 的 SIGINT，真實 subprocess 重現 ──────────
#
# 部署端（Pi5）用 `setsid nohup … &` 啟動：非互動 shell 對背景工作會把 SIGINT
# 自動設成 SIG_IGN 並讓子行程繼承（T-G 實測，見 task-g-report.md），kill -INT
# 因此完全沒反應；而 SIGTERM 過去沒有 handler，預設行為是直接砍死進程，
# shutdown() 根本跑不到（RED 已用「stash 掉 T-I 這次修正、跑同一支 driver」
# 驗證：returncode -15、meetings/ 不存在——見 task-i-report.md）。
#
# T20：以下四個真實 subprocess 情境（SIGTERM、繼承 SIG_IGN 的 SIGINT、連按兩次
# POST /end、收尾中第二次 SIGTERM）預設一律不連 Discord——一律透過
# `tests/harness/live_shutdown_driver.py` 起真實子行程，只是把 `bot`／背景 task
# 換成假的（跟本檔開頭的 FakeBot／_bg_task 同一種卡死語意），`live.shutdown()`／
# `live.summary()`／訊號接管（`live.install_shutdown_signal_handlers`）／
# `Session.request_end()` 全部是原封不動的 production code，只是不需要
# DISCORD_BOT_TOKEN／ELEVENLABS_API_KEY 就能跑，也因此不必再用
# `time.sleep(5)` 賭真實 Discord 登入的時間（改用 driver 印的 READY 行或輪詢
# `/health`，更快也更確定）。
#
# 如果要在 9/4 現場前額外對「真的接上 Discord bot」跑一次這四個情境當信心檢查，
# 每個情境都留了一份 `_real_discord` 結尾、內容完全相同的對照組（就是本檔改版
# 前的原始版本），預設一律跳過，opt-in 方式：
#
#     MEETING_HOST_RUN_REAL_DISCORD=1 PYTHONPATH=src .venv/bin/pytest \
#         tests/test_live_shutdown.py -k real_discord -q
#
# 這幾條除了上面的環境變數，還需要 repo root 有 `.env`（DISCORD_BOT_TOKEN／
# ELEVENLABS_API_KEY）；兩個條件缺一都會跳過並在 skip reason 說明原因。

_REPO_ROOT = Path(__file__).parent.parent
_ENV_FILE = _REPO_ROOT / ".env"
_DRIVER = _REPO_ROOT / "tests" / "harness" / "live_shutdown_driver.py"

_RUN_REAL_DISCORD_ENV = "MEETING_HOST_RUN_REAL_DISCORD"


def _real_discord_skip_reason() -> str | None:
    if os.environ.get(_RUN_REAL_DISCORD_ENV) != "1":
        return (f"預設不連真實 Discord；設 {_RUN_REAL_DISCORD_ENV}=1 才會跑"
                "（見本檔 T-I／T20 註解區塊）")
    if not _ENV_FILE.exists():
        return f"{_RUN_REAL_DISCORD_ENV}=1 但找不到 .env（需要 DISCORD_BOT_TOKEN／ELEVENLABS_API_KEY）"
    return None


requires_real_discord = pytest.mark.skipif(
    _real_discord_skip_reason() is not None,
    reason=_real_discord_skip_reason() or "",
)


def _spawn_live(extra_args: list[str] | None = None, **popen_kwargs) -> subprocess.Popen:
    """起一個真的 `python -m meeting_host.live`——只給 `_real_discord` 那組
    opt-in 測試用，會真的連 Discord gateway。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "meeting_host.live", "--topic", "測試", "--no-llm",
         *(extra_args or [])],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=str(_REPO_ROOT), env=env,
        **popen_kwargs,
    )


# POST /phase、/end 要帶的操作權杖（`serve()` 沒指定就隨機產生，測試端釘一組才知道要帶什麼）
_TOKEN = "shutdown-test-token"
# 讀取端（GET /events）現在也要權杖，走真實 serve()／driver 的測試得自己釘一組
_VIEW_TOKEN = "shutdown-test-view-token"


def _spawn_driver(extra_args: list[str] | None = None, **popen_kwargs) -> subprocess.Popen:
    """起 `tests/harness/live_shutdown_driver.py`——本檔預設會跑的那組用這個，
    不連 Discord、不打 LLM／TTS。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    return subprocess.Popen(
        [sys.executable, "-u", str(_DRIVER), *(extra_args or [])],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=str(_REPO_ROOT), env=env,
        **popen_kwargs,
    )


def _wait_ready(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """等驅動腳本印出 READY（訊號 handler 已註冊完成）才送訊號——不用固定
    `time.sleep(5)` 猜時機（那是用來等真實 Discord 登入的，driver 沒有這個
    依賴，可以用更快、更確定的方式）。"""
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


def _wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise AssertionError(f"觀戰 UI 在 {timeout} 秒內沒起來（port={port}）")


def _assert_meeting_files_written_and_cleanup(stdout: str, root: Path = _REPO_ROOT) -> None:
    """從 stdout 找出這次 run 寫出的事件紀錄路徑、斷言檔案真的存在，
    然後把這組測試產物（log／events.jsonl／host.md／minutes.md）連同
    meetings/ 目錄一起清掉——這幾個檔案不在 .gitignore 覆蓋範圍內
    （.gitignore 只蓋 src/meetings/*.log｜*.jsonl，這裡的 cwd 預設是 repo root，
    driver 版本的測試會把 `root` 換成各自的 `tmp_path`）。
    """
    m = re.search(r"事件紀錄：(meetings/meeting-\d+\.events\.jsonl)", stdout)
    assert m, f"stdout 沒有印出「事件紀錄：…」，代表 shutdown() 沒跑到。完整輸出：\n{stdout}"
    events_path = root / m.group(1)
    assert events_path.exists(), f"{events_path} 應該存在但沒有"
    stem = events_path.name.removesuffix(".events.jsonl")
    meetings_dir = events_path.parent
    for sibling in meetings_dir.glob(f"{stem}.*"):
        sibling.unlink()
    if meetings_dir.exists() and not any(meetings_dir.iterdir()):
        meetings_dir.rmdir()


# ── P7：shutdown 時要把 chair.pending／candidate 收成 dropped ────────────


def test_shutdown_emits_dropped_for_pending_before_summary(monkeypatch):
    """B1/B4：chair.pending 非 None 時，shutdown 要在 summary() 寫檔前，
    對它跑既有 on_dropped 路徑（reason=shutdown），events.jsonl 才留得下配對。
    用 fake_summary 側錄「summary 被呼叫那一刻」session.events 的快照，
    斷言 dropped 事件已經在裡面——代表確實排在 summary() 之前發出。"""
    from meeting_host.speaker import Chair, Intervention

    events_seen_by_summary = []

    def fake_summary(s):
        events_seen_by_summary.extend(s.events)

    monkeypatch.setattr(live, "summary", fake_summary)

    async def go():
        session = _make_session()
        chair = Chair(session.st, output=None, voice=None, earcon=None,
                      on_dropped=lambda iv, reason: session.emit(
                          "dropped", {"kind": iv.kind, "target": iv.target,
                                      "text": iv.text, "reason": reason}))
        chair.pending = Intervention(kind="發言超時", target="Alex", text="請簡短一點",
                                      hard=False, revision=0, created_at=0.0)
        session.chair = chair
        tasks, bot = await _make_tasks_and_bot(1)
        await shutdown(session, bot, tasks)
        return chair

    chair = asyncio.run(go())
    dropped = [e for e in events_seen_by_summary if e.kind == "dropped"]
    assert len(dropped) == 1
    assert dropped[0].data == {"kind": "發言超時", "target": "Alex",
                                "text": "請簡短一點", "reason": "shutdown"}
    assert chair.pending is None  # claim 收尾後要清掉，不留給下一次誤判


def test_shutdown_emits_dropped_for_candidate(monkeypatch):
    """B2：chair.candidate（硬打斷候選，等 playing 播完才輪到它）一樣要在
    shutdown 時作廢——它跟 pending 一樣還沒真的出聲。"""
    from meeting_host.speaker import Chair, Intervention

    monkeypatch.setattr(live, "summary", lambda s: None)

    async def go():
        session = _make_session()
        chair = Chair(session.st, output=None, voice=None, earcon=None,
                      on_dropped=lambda iv, reason: session.emit(
                          "dropped", {"kind": iv.kind, "target": iv.target,
                                      "text": iv.text, "reason": reason}))
        chair.candidate = Intervention(kind="議程超時", target=None, text="時間到了",
                                        hard=True, revision=0, created_at=0.0)
        session.chair = chair
        tasks, bot = await _make_tasks_and_bot(1)
        await shutdown(session, bot, tasks)
        return session, chair

    session, chair = asyncio.run(go())
    dropped = [e for e in session.events if e.kind == "dropped"]
    assert len(dropped) == 1
    assert dropped[0].data["reason"] == "shutdown"
    assert chair.candidate is None


def test_shutdown_no_dropped_when_pending_is_none(monkeypatch):
    """pending／candidate 都是 None（沒有卡住的介入）→ 不該無中生有發 dropped。"""
    from meeting_host.speaker import Chair

    monkeypatch.setattr(live, "summary", lambda s: None)

    async def go():
        session = _make_session()
        chair = Chair(session.st, output=None, voice=None, earcon=None,
                      on_dropped=lambda iv, reason: session.emit(
                          "dropped", {"kind": iv.kind, "target": iv.target,
                                      "text": iv.text, "reason": reason}))
        session.chair = chair
        tasks, bot = await _make_tasks_and_bot(1)
        await shutdown(session, bot, tasks)
        return session

    session = asyncio.run(go())
    assert [e for e in session.events if e.kind == "dropped"] == []


def test_shutdown_no_chair_is_noop(monkeypatch):
    """session.chair 仍是 None（例如 bot 從未進頻道就被打斷）→ shutdown 不能炸掉。"""
    monkeypatch.setattr(live, "summary", lambda s: None)

    async def go():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(1)
        await shutdown(session, bot, tasks)
        return session

    session = asyncio.run(go())
    assert session.chair is None


def test_sigterm_triggers_graceful_shutdown():
    """不連 Discord：5 秒內送 SIGTERM，應優雅結束（有 summary／事件紀錄），
    而不是被直接砍死（RED：修正前 returncode -15，meetings/ 不存在）。
    走 tests/harness/live_shutdown_driver.py，`live.shutdown()`／`live.summary()`／
    訊號接管都是原封不動的 production code，只有 bot／背景 task 是假的。"""
    proc = _spawn_driver()
    out = ""
    try:
        _wait_ready(proc)
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"SIGTERM 後 10 秒仍未結束，代表沒有優雅關閉。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0
    assert "會議摘要" in out
    assert "事件紀錄" in out
    _assert_meeting_files_written_and_cleanup(out)


def test_sigint_triggers_graceful_shutdown_when_inherited_as_ignored():
    """不連 Discord：重現部署情境——子行程 exec 前 SIGINT 就已經是 SIG_IGN（用
    preexec_fn 精確重現「非互動 shell 背景工作繼承 SIG_IGN」這個前提），外加
    start_new_session=True 重現 setsid 的新 session。驗證
    `live.install_shutdown_signal_handlers` 真的能蓋掉繼承來的 SIG_IGN，
    kill -INT 一樣能優雅關閉。"""
    def _inherit_sig_ign():
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    proc = _spawn_driver(start_new_session=True, preexec_fn=_inherit_sig_ign)
    out = ""
    try:
        _wait_ready(proc)
        os.kill(proc.pid, signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"SIGINT（繼承 SIG_IGN 前提下）10 秒仍未結束。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0
    assert "會議摘要" in out
    assert "事件紀錄" in out
    _assert_meeting_files_written_and_cleanup(out)


# ── 上面兩條的真實 Discord 對照組：opt-in，預設不跑（見本檔 T-I／T20 註解） ──


@requires_real_discord
def test_sigterm_triggers_graceful_shutdown_real_discord():
    """真實重現：5 秒後送 SIGTERM，應在 10 秒內優雅結束（有 summary／事件紀錄），
    而不是被直接砍死（RED：修正前 returncode -15，meetings/ 不存在）。"""
    proc = _spawn_live()
    try:
        time.sleep(5)
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"SIGTERM 後 10 秒仍未結束，代表沒有優雅關閉。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0
    assert "會議摘要" in out
    assert "事件紀錄" in out
    _assert_meeting_files_written_and_cleanup(out)


@requires_real_discord
def test_sigint_triggers_graceful_shutdown_when_inherited_as_ignored_real_discord():
    """真實重現部署情境：子行程 exec 前 SIGINT 就已經是 SIG_IGN（用 preexec_fn
    精確重現「非互動 shell 背景工作繼承 SIG_IGN」這個前提），外加
    start_new_session=True 重現 setsid 的新 session。驗證 add_signal_handler
    真的能蓋掉繼承來的 SIG_IGN，5 秒後 kill -INT 一樣能優雅關閉。"""
    def _inherit_sig_ign():
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    proc = _spawn_live(start_new_session=True, preexec_fn=_inherit_sig_ign)
    try:
        time.sleep(5)
        os.kill(proc.pid, signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"SIGINT（繼承 SIG_IGN 前提下）10 秒仍未結束。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0
    assert "會議摘要" in out
    assert "事件紀錄" in out
    _assert_meeting_files_written_and_cleanup(out)


# ── T3a：minutes 事件、SSE 推送 grace、第二次 drain ──────────────────
#
# 順序契約（見 live.shutdown 的 docstring）：
#   drain#1 → summary（寫檔＋emit minutes）→ flush SSE → cancel/gather
#   → drain#2 → 寫 events.jsonl → bot.close
# 這一段測的就是後三步為什麼非這個順序不可。


def _stub_minutes_llm(monkeypatch) -> None:
    """把 minutes A 檔的 LLM 呼叫換成固定回應——不打真實 API，也讓內容可預期。"""
    monkeypatch.setattr("meeting_host.minutes._call_minutes_llm",
                        lambda events: {"decisions": [{"who": "A", "what": "收工", "by": "今天"}],
                                        "todos": [], "unresolved": [], "stances": {}})


def _make_chair_with_emit(session):
    """真的 Chair，但 on_dropped 只把事件打進 session（不需要 output/voice/earcon）。"""
    from meeting_host.speaker import Chair
    return Chair(session.st, output=None, voice=None, earcon=None,
                 on_dropped=lambda iv, reason: session.emit(
                     "dropped", {"kind": iv.kind, "target": iv.target,
                                 "text": iv.text, "reason": reason}))


def test_shutdown_writes_minutes_event_as_last_line_of_events_jsonl(tmp_path, monkeypatch):
    """B4：shutdown 跑完後 events.jsonl 的最後一筆是 `minutes`，且事件裡的
    `minutes_md` 與磁碟上的 `.minutes.md` 逐字一致（回放模式只讀 events.jsonl
    也看得到總結，就靠這個）。"""
    monkeypatch.chdir(tmp_path)
    _stub_minutes_llm(monkeypatch)

    async def go():
        session = _make_session()
        session.emit("utterance", {"speaker": "A", "text": "hi", "start": 0.0, "end": 1.0})
        tasks, bot = await _make_tasks_and_bot(2)
        await shutdown(session, bot, tasks)

    asyncio.run(go())

    files = sorted((tmp_path / "meetings").glob("*.events.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    assert last["kind"] == "minutes"
    data = last["data"]
    assert "error" not in data
    assert data["events_path"] == f"meetings/{files[0].name}"
    on_disk = (tmp_path / data["minutes_path"]).read_text(encoding="utf-8")
    assert data["minutes_md"] == on_disk
    assert data["host_md"] == (tmp_path / data["host_path"]).read_text(encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_shutdown_delivers_minutes_event_to_connected_sse_client(tmp_path, monkeypatch):
    """C3：一個觀戰分頁正連著時 shutdown——它必須真的收到 `minutes`。

    接線與 production 一致：`serve()` 是 `tasks` 之一（`live.main_async` 就是這樣掛的），
    所以它會被 shutdown 的 cancel 掃到；client 走真實 socket，不是 aiohttp 的
    測試用 transport。`_flush_spectator()` 的必要性另由
    `test_spectator.py::test_flush_streams_writes_pending_event_before_returning`
    確定性地驗（emit 只是入佇列，flush 才是寫出去）。

    同時是那個 121 秒的迴歸守門：`serve()` 的 `AppRunner` 若用預設
    `shutdown_timeout=60`，SSE 連線會讓 `runner.cleanup()` 等到天荒地老
    （實測 cancel → task 收尾 121 秒），整個收尾就吊在 bot.close() 之前。
    """
    monkeypatch.chdir(tmp_path)
    _stub_minutes_llm(monkeypatch)
    import aiohttp

    from meeting_host import spectator

    async def go():
        session = _make_session()
        port = _free_port()
        serve_task = asyncio.create_task(
            spectator.serve(session, port, _TOKEN, _VIEW_TOKEN))
        async with aiohttp.ClientSession() as cs:
            for _ in range(100):  # 等伺服器起來
                try:
                    health = await cs.get(f"http://127.0.0.1:{port}/health")
                    await health.read()
                    break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.05)
            resp = await cs.get(f"http://127.0.0.1:{port}/events?k={_VIEW_TOKEN}")
            assert resp.status == 200
            lines: list[bytes] = []

            async def reader() -> None:
                try:
                    while True:
                        lines.append(await resp.content.readline())
                except Exception:  # noqa: BLE001 — 連線被 shutdown 砍掉是預期結局
                    pass

            reader_task = asyncio.create_task(reader())
            for _ in range(100):  # 等 snapshot 收完、subscriber 註冊好
                if session.subscribers:
                    break
                await asyncio.sleep(0.05)
            assert len(session.subscribers) == 1

            bg_tasks, bot = await _make_tasks_and_bot(1)
            tasks = [serve_task, *bg_tasks]
            started = time.perf_counter()
            await shutdown(session, bot, tasks)
            elapsed = time.perf_counter() - started
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
            return b"".join(lines), elapsed

    payload, elapsed = asyncio.run(go())
    assert b"event: minutes" in payload, f"SSE client 沒收到 minutes。收到的是：\n{payload!r}"
    sse_minutes = json.loads(
        payload.split(b"event: minutes\ndata: ", 1)[1].split(b"\n", 1)[0].decode("utf-8"))
    assert sse_minutes["kind"] == "minutes"
    assert sse_minutes["data"]["minutes_md"].strip() != ""
    assert sse_minutes["data"]["host_path"].endswith(".host.md")
    assert elapsed < 15, (f"有 SSE 連線時 shutdown 花了 {elapsed:.1f} 秒——"
                          "AppRunner 的 shutdown_timeout 又回到預設 60 了？")


def test_shutdown_drops_pending_that_only_appears_after_cancel(tmp_path, monkeypatch):
    """C3：關掉 T4 留下的殘餘窗口。

    慢路的 `to_thread` 若在第一次 drain 之後才回來、`chair.request()` 塞進一個新的
    pending，第一次 drain 撈不到它——沒有第二次 drain 的話，events.jsonl 會留下一筆
    配不到結果的 `queued`。這裡用「背景 task 在收到 cancel 時才塞 pending」精確重現
    那個時間點（drain#1 之後、gather 回來之前）。
    """
    monkeypatch.chdir(tmp_path)
    _stub_minutes_llm(monkeypatch)
    from meeting_host.speaker import Intervention

    async def go():
        session = _make_session()
        chair = _make_chair_with_emit(session)
        session.chair = chair

        late = Intervention(kind="離題", target=None, text="慢路剛回來的那句",
                            hard=False, revision=0, created_at=0.0)

        async def slow_path_returns_at_cancel():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                chair.pending = late  # ← 第一次 drain 已經跑過了才出現
                raise

        tasks = [asyncio.create_task(slow_path_returns_at_cancel())]
        await asyncio.sleep(0)
        bot = FakeBot(asyncio.Event())
        bot.tasks_done.set()  # 這個測試不驗 close 的等待語意
        await shutdown(session, bot, tasks)
        return chair

    chair = asyncio.run(go())
    assert chair.pending is None  # 第二次 drain 收乾淨了

    files = sorted((tmp_path / "meetings").glob("*.events.jsonl"))
    assert len(files) == 1
    kinds_and_data = [json.loads(line) for line in
                      files[0].read_text(encoding="utf-8").splitlines()]
    dropped = [e for e in kinds_and_data if e["kind"] == "dropped"]
    assert len(dropped) == 1, "cancel 後才出現的 pending 必須被標成 dropped 並寫進 events.jsonl"
    assert dropped[0]["data"] == {"kind": "離題", "target": None,
                                  "text": "慢路剛回來的那句", "reason": "shutdown"}
    # 順序契約：dropped 排在 minutes 之後（minutes 由 summary 先發），
    # 但兩者都在檔案裡——events.jsonl 是最後才寫的，所以撈得到
    assert [e["kind"] for e in kinds_and_data][-2:] == ["minutes", "dropped"]


def test_shutdown_still_writes_events_jsonl_when_flush_is_cancelled(tmp_path, monkeypatch):
    """收尾保證：flush／cancel 階段被外部取消時，events.jsonl 仍要寫得出去
    （shutdown 把第 3–6 步包在 try/finally 就是為了這個）。"""
    monkeypatch.chdir(tmp_path)
    _stub_minutes_llm(monkeypatch)

    async def boom(session, timeout=3.0):
        raise asyncio.CancelledError

    monkeypatch.setattr(live, "_flush_spectator", boom)

    async def go():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(1)
        with pytest.raises(asyncio.CancelledError):
            await shutdown(session, bot, tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return bot

    bot = asyncio.run(go())
    # review round 1：被二次取消打斷時 bot.close() 也不能被跳過——跳過就是把
    # Discord 連線丟著讓對方逾時。它現在跟寫檔在同一個 finally 裡。
    assert bot.close_called is True
    assert bot.closed is True
    files = sorted((tmp_path / "meetings").glob("*.events.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])["kind"] == "minutes"


# ── review round 1：收尾期間再收到一次取消 ─────────────────────────
#
# 反例：shutdown 在 `_flush_spectator()`（最長 3 秒）或 `gather(*tasks)` 上等的
# 時候，第二次 POST /end／第二次 SIGTERM 會在那個 await 重新拋 CancelledError。
# 修正前 `bot.close()` 在 try/finally 之外，整段被跳過，例外再逃出 main_async
# 的 finally，`main()` 只接 KeyboardInterrupt → 未捕捉例外、非 0 退出。


def test_real_second_cancel_during_flush_still_closes_bot(tmp_path, monkeypatch):
    """真的對跑 shutdown 的 task 送第二次 cancel（不是合成 raise），
    斷言 bot.close() 仍被呼叫且跑完，檔案也照寫。"""
    monkeypatch.chdir(tmp_path)
    _stub_minutes_llm(monkeypatch)

    flush_entered = asyncio.Event()

    async def slow_flush(session, timeout=3.0):
        flush_entered.set()
        await asyncio.sleep(10)  # 模擬「等 SSE client」的那 3 秒窗口

    monkeypatch.setattr(live, "_flush_spectator", slow_flush)

    async def go():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(1)
        shutdown_task = asyncio.create_task(shutdown(session, bot, tasks))
        await asyncio.wait_for(flush_entered.wait(), timeout=2)
        shutdown_task.cancel()  # ← 第二次取消，正好打在 flush 的 await 上
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task
        return bot

    bot = asyncio.run(go())
    assert bot.close_called is True, "二次取消時 bot.close() 被跳過了"
    assert bot.closed is True
    files = sorted((tmp_path / "meetings").glob("*.events.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])["kind"] == "minutes"


def test_request_end_cancels_only_once(monkeypatch):
    """來源防重入：連呼兩次 request_end()，注入的 cancel 只被呼叫一次，
    但兩次都回 True（POST /end 兩次都是 200）。"""
    calls = []
    session = Session(MeetingState(topic="t", duration_min=30, participants=[]),
                      cancel=lambda: calls.append("cancel"))

    assert session.request_end() is True
    assert session.request_end() is True
    assert session.request_end() is True
    assert calls == ["cancel"]
    assert session.ending is True


def test_request_end_does_not_cancel_once_shutdown_started(monkeypatch):
    """SIGTERM 先來、使用者再按「結束會議」：那條路徑不經過 request_end()，
    所以 `ending` 必須由 shutdown() 入口也設一次，否則這一按就會打斷收尾。"""
    monkeypatch.setattr(live, "summary", lambda s: None)
    calls = []

    async def go():
        session = Session(MeetingState(topic="t", duration_min=30, participants=[]),
                          cancel=lambda: calls.append("cancel"))
        assert session.ending is False
        tasks, bot = await _make_tasks_and_bot(1)
        await shutdown(session, bot, tasks)
        return session

    session = asyncio.run(go())
    assert session.ending is True
    assert session.request_end() is True  # 仍回報受理
    assert calls == []                     # 但一次 cancel 都沒送出去


def test_main_prints_message_and_exits_1_on_second_cancel(monkeypatch, capsys):
    """訊號路徑保留「按第二次就強制退出」的語意，但 main() 要接住 CancelledError，
    印一行說明並用明確離開碼結束，不噴 traceback。"""
    def fake_run(coro):
        coro.close()  # 不讓 "coroutine was never awaited" 警告冒出來
        raise asyncio.CancelledError

    monkeypatch.setattr("asyncio.run", fake_run)
    monkeypatch.setattr(sys, "argv", ["live"])

    with pytest.raises(SystemExit) as excinfo:
        live.main()

    assert excinfo.value.code == 1
    assert "收到第二次結束訊號，強制退出" in capsys.readouterr().out


def test_double_post_end_still_exits_cleanly():
    """不連 Discord：連按「結束會議」不能把收尾打斷。POST /end 之後 0.5 秒內再送
    幾次，進程仍要以 0 退出且把事件紀錄寫出去。走 driver（`--spectator-port`
    開著時跟 production main_async 一樣真的掛 `spectator.serve()`，`session`
    也真的接了 `cancel=main_task.cancel`），`_end_handler`／`request_end()`／
    `shutdown()` 全是原封不動的 production code。

    註：伺服器隨 shutdown 一起收掉，所以第二次之後的 POST 通常拿到「連線被拒」
    而不是 200——真正的防重入證據是上面三條單元測試；這一條是進程層級的
    冒煙守門（returncode 與檔案），確認整條路徑沒有回歸。
    """
    port = _free_port()
    proc = _spawn_driver(extra_args=["--spectator-port", str(port),
                                     "--spectator-token", _TOKEN,
                                    "--view-token", _VIEW_TOKEN])
    statuses = []
    out = ""
    try:
        _wait_health(port)

        deadline = time.perf_counter() + 0.5
        while True:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/end", data=b"",
                                              method="POST",
                                              headers={"X-Ahem-Token": _TOKEN})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    statuses.append(resp.status)
            except (urllib.error.URLError, OSError) as e:
                statuses.append(type(e).__name__)  # 伺服器已隨收尾關掉
            if time.perf_counter() >= deadline:
                break
            time.sleep(0.15)

        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"連按 /end 後 30 秒仍未結束。statuses={statuses}\nstdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert statuses[0] == 200, f"第一次 POST /end 應該回 200，實際 {statuses}"
    assert proc.returncode == 0, f"連按 /end 後 returncode={proc.returncode}，statuses={statuses}"
    assert "事件紀錄：" in out
    _assert_meeting_files_written_and_cleanup(out)


def test_double_sigterm_during_flush_exits_without_traceback():
    """不連 Discord：真的打進「shutdown 收尾中途再收到一次取消」這個窗口，
    送 SIGTERM、0.5 秒後再送一次。要求：不噴 traceback、印出強制退出說明、
    用明確離開碼結束，而且事件紀錄照樣寫得出去。

    真實進程（原版）用一條連著的觀戰連線讓 `serve()` 的 aiohttp cleanup 自然
    拖長收尾，賭第二次訊號落在那個窗口裡——這個時序在假 bot／無真實網路 I/O
    下量測到通常不到 0.1 秒就跑完，賭不到窗口（實測過，見交付訊息）。改用
    driver 的 `--slow-flush-seconds`：把 `live._flush_spectator`（`shutdown()`
    本來就會呼叫的同一個 module-level 名稱）換成固定睡 2 秒的版本，
    第二次 SIGTERM 必定落在這個 await 上——`shutdown()`／訊號接管／`main()`
    的第二次取消處理全部原封不動，只是換了一種方式讓「還在 await」這件事
    變成確定而非賭時序，跟本檔另一條 in-process 單元測試
    `test_real_second_cancel_during_flush_still_closes_bot`（monkeypatch 同一個
    名稱）是同一招，只是這裡是跨行程版本。
    """
    proc = _spawn_driver(extra_args=["--slow-flush-seconds", "2"])
    out = ""
    try:
        _wait_ready(proc)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)  # ← 打在 shutdown 還在等 _flush_spectator 的地方
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"第二次 SIGTERM 後 30 秒仍未結束。stdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert "Traceback (most recent call last)" not in out, f"噴了 traceback：\n{out}"
    assert "收到第二次結束訊號" in out, f"沒印強制退出說明：\n{out}"
    assert proc.returncode == 1, f"第二次訊號應以明確離開碼 1 結束，實際 {proc.returncode}"
    assert "事件紀錄：" in out
    _assert_meeting_files_written_and_cleanup(out)


# ── 上面兩條的真實 Discord 對照組：opt-in，預設不跑（見本檔 T-I／T20 註解） ──


@requires_real_discord
def test_double_post_end_still_exits_cleanly_real_discord():
    """真實進程：連按「結束會議」不能把收尾打斷。POST /end 之後 0.5 秒內再送
    幾次，進程仍要以 0 退出且把事件紀錄寫出去。

    註：伺服器隨 shutdown 一起收掉，所以第二次之後的 POST 通常拿到「連線被拒」
    而不是 200——真正的防重入證據是上面三條單元測試；這一條是進程層級的
    冒煙守門（returncode 與檔案），確認整條路徑沒有回歸。
    """
    port = _free_port()
    proc = _spawn_live(extra_args=["--spectator-port", str(port),
                                   "--spectator-token", _TOKEN,
                                    "--view-token", _VIEW_TOKEN])
    statuses = []
    try:
        for _ in range(300):  # 等觀戰 UI 起來（bot 登入要幾秒）
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        else:
            pytest.fail("觀戰 UI 沒起來")

        deadline = time.perf_counter() + 0.5
        while True:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/end", data=b"",
                                              method="POST",
                                              headers={"X-Ahem-Token": _TOKEN})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    statuses.append(resp.status)
            except (urllib.error.URLError, OSError) as e:
                statuses.append(type(e).__name__)  # 伺服器已隨收尾關掉
            if time.perf_counter() >= deadline:
                break
            time.sleep(0.15)

        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"連按 /end 後 30 秒仍未結束。statuses={statuses}\nstdout：\n{out}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert statuses[0] == 200, f"第一次 POST /end 應該回 200，實際 {statuses}"
    assert proc.returncode == 0, f"連按 /end 後 returncode={proc.returncode}，statuses={statuses}"
    assert "事件紀錄：" in out
    _assert_meeting_files_written_and_cleanup(out)


@requires_real_discord
def test_double_sigterm_during_flush_exits_without_traceback_real_discord():
    """真實進程、真的打進窗口：帶著一條連著的觀戰連線送 SIGTERM，0.5 秒後再送一次。

    有 SSE 連線時 `serve()` 的 cleanup 會讓收尾多花一秒多，第二次訊號因此真的落在
    `shutdown()` 還在 await 的地方（沒有連線時收尾太快，第二次訊號打不到——所以這
    條測試一定要先接上 /events）。要求：不噴 traceback、印出強制退出說明、用明確
    離開碼結束，而且事件紀錄照樣寫得出去。
    """
    port = _free_port()
    proc = _spawn_live(extra_args=["--spectator-port", str(port),
                                   "--spectator-token", _TOKEN,
                                    "--view-token", _VIEW_TOKEN])
    sock = None
    try:
        for _ in range(300):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        else:
            pytest.fail("觀戰 UI 沒起來")

        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(f"GET /events?k={_VIEW_TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                     "Accept: text/event-stream\r\n\r\n".encode())
        time.sleep(1)  # 讓 SSE handler 註冊成 subscriber

        proc.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)  # ← 打在 shutdown 還在 await 的地方
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"第二次 SIGTERM 後 30 秒仍未結束。stdout：\n{out}")
    finally:
        if sock is not None:
            sock.close()
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert "Traceback (most recent call last)" not in out, f"噴了 traceback：\n{out}"
    assert "收到第二次結束訊號" in out, f"沒印強制退出說明：\n{out}"
    assert proc.returncode == 1, f"第二次訊號應以明確離開碼 1 結束，實際 {proc.returncode}"
    assert "事件紀錄：" in out
    _assert_meeting_files_written_and_cleanup(out)


def test_second_cancel_during_the_discord_post_still_writes_events_and_closes_bot(
        tmp_path, monkeypatch):
    """迴歸：貼會議記錄到 Discord 的那個 await 不能站在 `shutdown()` 的 try 外面。

    2026-09-04 引進「收尾時把記錄貼回頻道」時，那個 `await` 一度放在
    `try:` 之前。第二次中斷（第二個 SIGTERM／SIGINT）若正好打在它身上，
    `CancelledError` 會整個逃出 `shutdown()`，於是第 6 步的 `events.jsonl`
    與第 7 步的 `bot.close()` 全部被跳過——正是 `shutdown()` docstring
    第 3–7 步「全部在同一個 finally 裡」要防的那件事。實測確認過會賠掉。

    這條測試把中斷精準打在那個 await 上，要求兩件產出都仍然成立。
    """
    monkeypatch.chdir(tmp_path)
    _stub_minutes_llm(monkeypatch)

    async def go():
        session = _make_session()
        tasks, bot = await _make_tasks_and_bot(1)
        bot.channel = object()  # 有頻道才會真的走到貼訊息那條路

        async def cancelled_mid_post(minutes_md, filename):
            raise asyncio.CancelledError  # ← 第二次中斷打在這裡

        bot.post_minutes = cancelled_mid_post
        try:
            await shutdown(session, bot, tasks)
        except asyncio.CancelledError:
            pass  # 逃出來本身是允許的；不允許的是產出被賠掉
        return bot

    bot = asyncio.run(go())
    written = sorted((tmp_path / "meetings").glob("*.events.jsonl"))
    assert written, "第二次中斷賠掉了 events.jsonl"
    assert written[0].stat().st_size > 0, "events.jsonl 是空的"
    assert bot.close_called, "第二次中斷跳過了 bot.close()"
