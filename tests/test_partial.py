"""即時逐字（partial）：STT 的累積全文只走到畫面，不進狀態、不餵規則。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meeting_host.live import Session
from meeting_host.state import MeetingState, Utterance
from meeting_host.stt import Partial


def test_on_partial_emits_display_event_and_touches_nothing_else():
    st = MeetingState(topic="t", duration_min=30, participants=["甲"])
    s = Session(st)
    got = []; s.subscribers.append(lambda e: got.append(e))
    s.on_partial(Partial("甲", "今天的會議討論了", 12.0))
    assert [(e.kind, e.data) for e in got] == [("partial", {"speaker": "甲", "text": "今天的會議討論了"})]
    assert st.utterances == [] and st.speaking == {} and s.st.interventions == []


def test_partial_class_is_not_an_utterance():
    p = Partial("甲", "x", 1.0)
    assert not isinstance(p, Utterance) and p.text == "x" and p.since == 1.0


# ── 真瀏覽器：活的那一行顯示 partial，定稿後被正式逐字稿取代 ──
pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from meeting_host import spectator  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "synthetic-meeting.events.jsonl"


def test_live_line_shows_partial_then_is_replaced_by_utterance():
    async def body():
        session = spectator.ReplaySession(spectator._load_events(SAMPLE))
        server = TestServer(spectator._build_app(session))
        await server.start_server()
        try:
            await session.replay(speed=1_000_000)
            url = str(server.make_url("/"))

            def check() -> dict:
                with sync_playwright() as p:
                    browser = p.chromium.launch(); page = browser.new_page()
                    errors: list[str] = []; page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(url); page.wait_for_timeout(500)
                    # 回放已結束會把畫面標成 ended；為了驗活的那行，先把 ended 關掉
                    page.evaluate("__spectator.state.ended = false")
                    page.evaluate('__spectator.handleEvent({kind:"speaking", t: 3000, data:{speaker:"林同", active:true}})')
                    before = page.eval_on_selector("#transcript .u-line.now", "el => el.textContent")
                    page.evaluate('__spectator.handleEvent({kind:"partial", t: 3001, data:{speaker:"林同", text:"今天的會"}})')
                    page.evaluate('document.querySelector("#transcript .u-line.now").dataset.mark = "same-node"')
                    page.evaluate('__spectator.handleEvent({kind:"partial", t: 3002, data:{speaker:"林同", text:"今天的會議討論了"}})')
                    same_node = page.evaluate('document.querySelector("#transcript .u-line.now").dataset.mark === "same-node"')
                    live = page.eval_on_selector("#transcript .u-line.now", "el => el.textContent")
                    n_live = page.evaluate('document.querySelectorAll("#transcript .u-line.now").length')
                    page.evaluate('__spectator.handleEvent({kind:"utterance", t: 3005, data:{speaker:"林同", text:"今天的會議討論了下一季。", start: 3000, end: 3004}})')
                    page.evaluate('__spectator.handleEvent({kind:"speaking", t: 3005, data:{speaker:"林同", active:false}})')
                    after_live = page.evaluate('document.querySelectorAll("#transcript .u-line.now").length')
                    last = page.evaluate('(() => { const ls = document.querySelectorAll("#transcript .u-line:not(.now)"); return ls[ls.length-1].textContent; })()')
                    # 過期：有人 speaking 但 partial 停了——6 秒後淡出、10 秒後移除
                    page.evaluate('__spectator.handleEvent({kind:"speaking", t: 3100, data:{speaker:"沈禾", active:true}})')
                    page.evaluate('__spectator.handleEvent({kind:"partial", t: 3100.5, data:{speaker:"沈禾", text:"哇！"}})')
                    page.evaluate('__spectator.handleEvent({kind:"fast_timer", t: 3107, data:{run:null, silent:{}, remaining:100}})')
                    stale = page.evaluate('document.querySelector("#transcript .u-line.now").classList.contains("stale")')
                    page.evaluate('__spectator.handleEvent({kind:"fast_timer", t: 3111, data:{run:null, silent:{}, remaining:100}})')
                    dropped = page.evaluate('document.querySelectorAll("#transcript .u-line.now").length')
                    settled = page.evaluate('(() => { const ls = document.querySelectorAll("#transcript .u-line:not(.now)"); return ls[ls.length-1].classList.contains("settle"); })()')
                    browser.close()
                    assert not errors, f"頁面 JS 例外：{errors}"
                    return {"before": before, "live": live, "n_live": n_live, "after_live": after_live, "last": last, "same_node": same_node, "settled": settled, "stale": stale, "dropped": dropped}
            return await asyncio.to_thread(check)
        finally:
            await server.close()
    out = asyncio.run(body())
    assert "正在說話" in out["before"]
    assert "今天的會議討論了" in out["live"] and "今天的會" in out["live"] and out["n_live"] == 1   # 覆蓋不是追加
    assert out["after_live"] == 0                                    # 定稿後活的那行消失
    assert "今天的會議討論了下一季。" in out["last"]                    # 正式逐字稿接上
    assert out["same_node"] is True                                  # partial 是就地改字，不是重建節點
    assert out["settled"] is True                                    # 定稿是在原位換成正式行（帶過場）
    assert out["stale"] is True and out["dropped"] == 0                # 沒定稿的短句：6 秒淡出、10 秒移除
