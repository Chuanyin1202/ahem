"""觀戰 UI「主席故障告示」：會議進行中就看得見主席聽不到／講不出話。

背景（2026-08-31 那場 42 分鐘的真實會議）：ElevenLabs 額度耗盡讓 STT 與 TTS
（共用同一把 key）同時死掉。四次介入全部 `TTS HTTP 401 quota_exceeded`，但那件事
**只有散會後的主持記錄看得到**——會議進行中，觀戰 UI 上沒有任何地方顯示「主席
現在講不出話」，現場只會覺得它變安靜了，分不出是判斷不介入還是壞了。

告示要回答的就是這個問題：**主席現在是「不想講」還是「講不出來」／「聽不到」。**

兩種故障的資料來源不同（見 index.html 的 `renderAlerts`）：
- 聽不到：後端的 `hearing` 事件（判定在 `hearing.py`，前端不重算）
- 講不出話：從既有的 `failed`／`spoken` 事件自己數連續失敗，後端不必多發事件，
  舊的 events.jsonl 回放也照樣顯示得出來

「連續」與「偶發」必須分得出來：一次 TTS 逾時每場都可能發生，喊出來就是誤報；
`TTS_FAIL_ALERT = 2` 起跳，真的 `spoken` 一次就歸零（`dropped` 不算——那是介入
被作廢，跟發聲能力無關）。

本測試需要 Playwright（含瀏覽器）才能真的執行前端 JS；這個專案沒有把 Playwright
列為相依套件，所以在沒裝的環境下用 `importorskip` 直接跳過——作法與既有的
`tests/test_spectator_three_state.py` 完全一致。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from aiohttp.test_utils import TestServer  # noqa: E402

from meeting_host import spectator  # noqa: E402

BROKEN = Path(__file__).parent / "fixtures" / "chair_broken_alerts.events.jsonl"
RECOVERED = Path(__file__).parent / "fixtures" / "chair_recovered_alerts.events.jsonl"
# 只有一次 TTS 失敗的情境從 BROKEN 那份切前 4 行來（見 `_prefix`）——刻意不另存
# 一份幾乎一樣的檔案，免得兩份之後不同步。
SINGLE_FAILURE_LINES = 4


def _render(events: list):
    """把事件灌進 ReplaySession、開瀏覽器跑真的前端 JS，回傳告示區的狀態。"""
    async def body():
        session = spectator.ReplaySession(events)
        server = TestServer(spectator._build_app(session))
        await server.start_server()
        try:
            await session.replay(speed=1_000_000)
            url = str(server.make_url("/"))

            def check() -> tuple[bool, str, list[str], bool, bool]:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(url)
                    page.wait_for_timeout(500)
                    hidden = page.eval_on_selector("#alerts", "el => el.hidden")
                    text = page.eval_on_selector("#alerts", "el => el.textContent")
                    titles = page.eval_on_selector_all(
                        "#alerts .a-title", "els => els.map(e => e.textContent)")
                    # 版面：右欄兩顆按鈕必須還看得到（`.right` 曾因新增區塊把按鈕
                    # 擠出畫面過，那次的修法是加 overflow-y；告示放在左欄就是為了
                    # 從一開始就不碰右欄，這裡把它釘住）
                    export_visible = page.is_visible("#btn-export")
                    end_visible = page.is_visible("#btn-end")
                    browser.close()
                    assert not errors, f"頁面 JS 例外：{errors}"
                    return hidden, text, titles, export_visible, end_visible

            return await asyncio.to_thread(check)
        finally:
            await server.close()

    return asyncio.run(body())


def _prefix(path: Path, lines: int) -> list:
    """事件檔的前 N 行（`spectator._load_events` 的形狀）。"""
    return spectator._load_events(path)[:lines]


# ── 偵測：連續失敗看得見，偶發單次不誤報 ──────────────────────────────


def test_single_tts_failure_does_not_raise_an_alert():
    """驗收 4 的後半：一次 TTS 逾時跟額度耗盡不是同一件事，單次不能誤報。"""
    hidden, text, titles, _, _ = _render(_prefix(BROKEN, SINGLE_FAILURE_LINES))
    assert hidden is True, text
    assert titles == []


def test_repeated_tts_failure_shows_that_the_chair_cannot_speak():
    """驗收 4 的前半：連續失敗時畫面上明確講出「主席講不出話」，
    並帶出次數與最後一次的原因（額度耗盡與逾時因此分得出來）。"""
    hidden, text, titles, export_visible, end_visible = _render(spectator._load_events(BROKEN))
    assert hidden is False
    assert "主席講不出話" in titles, titles
    assert "連續 2 次" in text, text
    assert "quota_exceeded" in text, text
    # 同一份資料裡 STT 也死了（共用同一把 key）——兩種故障各佔一列，不互相蓋掉
    assert "主席聽不到" in titles, titles
    assert "STT 連線中斷" in text, text
    # 版面：告示不擠掉右欄的兩顆按鈕
    assert export_visible and end_visible


def test_alerts_clear_once_the_chair_recovers():
    """恢復：STT 活過來（`hearing.ok=true`）且主席真的出聲過一次之後，告示消失。

    這同時證明告示不是「一旦出現就永遠賴著」——它描述的是**現在**的狀態。
    """
    hidden, text, titles, _, _ = _render(spectator._load_events(RECOVERED))
    assert hidden is True, text
    assert titles == []


def test_reconnecting_does_not_double_count_the_failure_streak():
    """SSE 重連會重送全量 snapshot；連續失敗次數是前端累加出來的，
    重播前必須先歸零，否則「連續 2 次」會變成「連續 4 次」。

    這條沒辦法靠真的 SSE 連線驗——重連的時機由瀏覽器決定，測試控制不了。
    所以在頁面腳本執行**之前**把 `EventSource` 換成一個把 listener 收起來的
    替身，直接餵兩次同一份 snapshot：驗的正是 `resetForSnapshot()` 這條路徑
    本身可不可重入，而不是連線行為。
    """
    events = [{"kind": e.kind, "t": e.t, "data": e.data}
              for e in spectator._load_events(BROKEN)]

    async def body() -> str:
        session = spectator.ReplaySession([])
        server = TestServer(spectator._build_app(session))
        await server.start_server()
        try:
            url = str(server.make_url("/"))

            def run() -> str:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.add_init_script("""
                        window.__listeners = {};
                        window.EventSource = function () {
                          this.addEventListener = function (k, f) {
                            (window.__listeners[k] = window.__listeners[k] || []).push(f);
                          };
                        };
                    """)
                    page.goto(url)
                    page.wait_for_timeout(300)
                    page.evaluate(
                        """(evs) => {
                            var send = function () {
                              (window.__listeners.snapshot || []).forEach(function (f) {
                                f({ data: JSON.stringify(evs) });
                              });
                            };
                            send();   // 第一次連線
                            send();   // 重連，重送全量
                        }""", events)
                    page.wait_for_timeout(200)
                    text = page.eval_on_selector("#alerts", "el => el.textContent")
                    browser.close()
                    assert not errors, f"頁面 JS 例外：{errors}"
                    return text

            return await asyncio.to_thread(run)
        finally:
            await server.close()

    text = asyncio.run(body())
    assert "連續 2 次" in text, text
