"""T22：觀戰 UI「主席思考」三態顯示（`spectator/index.html`）的守恆不變量。

背景：`slow_score` 的 `admissible=True` 只代表「這次評分通過閘門、排進佇列」，
不代表真的講出來了——真正結果要等後續的 `spoken`／`dropped`／`failed` 才定案，
可能被 revision 過期作廢、播放失敗，或者**連佇列都排不進去**（`Chair.request()`
回 False：忙碌中／退避中／已有 pending，見 `speaker.py`）。這第三種是 review
抓到的 TOCTOU 缺口——`live.py::_run_slow_score` 的 `busy` 守衛只在評分**之前**
檢查，評分本身要跑好幾秒，這幾秒內快路完全可能先搶進去讓 Chair 變忙；這種情況
後端根本不會發 `queued` 事件，若前端只靠「等 queued」配對，判斷會永遠卡在
「評估中」，讓「開口＋受阻＋忍住」小於總評分次數。

`tests/fixtures/slow_path_three_state.events.jsonl` 是自己造的合成事件（8/29 那晚
的真實回放檔剛好 9 次 admissible 全部順利排入，沒有這個情境，驗不到這條），涵蓋
六種組合：admissible→queued→spoken（開口）、→queued→failed（受阻）、
admissible 但下一筆不是 queued（受阻·主席忙碌中）、→queued→dropped（受阻·
revision 過期）、非 admissible（忍住）、以及 admissible 後直接被會議收尾的
`minutes` 事件截斷（受阻·主席忙碌中——驗證這條規則不看下一筆事件的種類）。

本測試需要 Playwright（含瀏覽器）才能真的執行前端 JS；這個專案目前沒有把
Playwright 列為相依套件（沒有 pyproject/requirements 條目，也沒有 CI 設定檔），
所以在沒裝的環境下用 `importorskip` 直接跳過，不會讓 `pytest tests/ -q` 的
passed 計數變動或失敗——只會多一筆 skipped。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from aiohttp.test_utils import TestServer  # noqa: E402

from meeting_host import spectator  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "slow_path_three_state.events.jsonl"
# T29：慢路拆成兩次呼叫之後多出來的第四種「受阻」——已經決定要開口，但話術那一步
# 失手（`話術失敗`／`話術過長`）或那幾秒內世界變了（`冷卻(話術後)`／`收尾(話術後)`）。
# 這幾筆的 admissible 是 false，卻**不是**「忍住」：主席不是選擇克制。獨立一份合成
# 事件檔，不動上面那份——那份守的是原本三種受阻的配對邏輯，兩件事分開驗才看得出
# 哪一條壞了。
PHRASE_FIXTURE = Path(__file__).parent / "fixtures" / "slow_path_phrase_failure.events.jsonl"


def _render(fixture: Path):
    """把一份事件檔灌進 ReplaySession、開瀏覽器跑真的前端 JS，回傳三個畫面值。"""
    async def body():
        events = spectator._load_events(fixture)
        session = spectator.ReplaySession(events)
        server = TestServer(spectator._build_app(session))
        await server.start_server()
        try:
            await session.replay(speed=1_000_000)
            url = str(server.make_url("/"))

            def check() -> tuple[str, str, str]:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(url)
                    page.wait_for_timeout(500)
                    slow_total = page.eval_on_selector("#s-slow", "el => el.textContent")
                    slow_sub = page.eval_on_selector("#s-slow-sub", "el => el.textContent")
                    judge_html = page.eval_on_selector("#judge-list", "el => el.innerHTML")
                    browser.close()
                    assert not errors, f"頁面 JS 例外：{errors}"
                    return slow_total, slow_sub, judge_html

            return await asyncio.to_thread(check)
        finally:
            await server.close()

    return asyncio.run(body())


def test_phrase_call_failures_count_as_blocked_not_withheld():
    """T29 第四種受阻。四筆評分：開口 1（話術成功→queued→spoken）、
    受阻 2（`話術失敗`＋`冷卻(話術後)`）、忍住 1（LLM 自己判不需要開口）。

    這裡守兩件事：
    1. 守恆不變式在多出一種受阻之後仍然成立（三格加總 ＝ 總評分次數）。
    2. 話術失敗顯示成「受阻」而不是「忍住」——顯示成忍住等於把失敗說成克制，
       觀戰的人會以為主席是自己決定不講的。
    """
    slow_total, slow_sub, judge_html = _render(PHRASE_FIXTURE)

    assert slow_total == "4 次"
    assert slow_sub == "開口 1 · 受阻 2 · 忍住 1"
    parts = dict(p.split(" ", 1) for p in slow_sub.split(" · "))
    assert sum(int(v) for v in parts.values()) == 4

    # 最近三次判斷（新→舊）：忍住 → 受阻(冷卻後) → 受阻(話術失敗)。
    # 兩種新受阻的理由要各自寫得出來，不能被同一句話蓋掉。
    assert judge_html.count("<b>受阻</b>") == 2
    assert "決定開口但話術生成失敗" in judge_html
    assert "想好話時已在冷卻期內" in judge_html
    assert "<b>忍住</b>" in judge_html


def test_slow_path_three_state_invariant_including_busy_rejection():
    async def body():
        events = spectator._load_events(FIXTURE)
        session = spectator.ReplaySession(events)
        server = TestServer(spectator._build_app(session))
        await server.start_server()
        try:
            # 灌到接近瞬間完成——事件全部進 session.events 後才開瀏覽器，走
            # snapshot 一次重播的路徑（見 index.html resetForSnapshot() 的說明：
            # 這條路徑跟即時逐筆收事件必須產生相同結果，重播本來就要可重入）。
            await session.replay(speed=1_000_000)
            url = str(server.make_url("/"))

            def check() -> tuple[str, str, str]:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(url)
                    page.wait_for_timeout(500)
                    slow_total = page.eval_on_selector("#s-slow", "el => el.textContent")
                    slow_sub = page.eval_on_selector("#s-slow-sub", "el => el.textContent")
                    judge_html = page.eval_on_selector("#judge-list", "el => el.innerHTML")
                    browser.close()
                    assert not errors, f"頁面 JS 例外：{errors}"
                    return slow_total, slow_sub, judge_html

            return await asyncio.to_thread(check)
        finally:
            await server.close()

    slow_total, slow_sub, judge_html = asyncio.run(body())

    assert slow_total == "6 次"
    assert slow_sub == "開口 1 · 受阻 4 · 忍住 1"

    # 守恆不變量：三格加總必須等於總評分次數（這是這次 review 要求改成的形式，
    # 不是只驗證今晚那份剛好全部排入的資料）。
    parts = dict(p.split(" ", 1) for p in slow_sub.split(" · "))
    assert sum(int(v) for v in parts.values()) == 6

    # 最近三次判斷（新→舊）依合成檔的事件順序應該是：
    # revision 過期受阻 → 忍住 → 主席忙碌中受阻（會議被 minutes 收尾那筆）。
    # 兩種「受阻」理由要能區分開，不能被同一句話蓋掉。
    assert "主席忙碌中" in judge_html
    assert "revision 過期" in judge_html
    assert "忍住" in judge_html
    # 沒排進佇列（忙碌被拒）跟已經排進佇列又被回收（revision 過期），是畫面上
    # 兩筆不同的「受阻」，各自帶著自己的理由，不能被同一句話蓋掉。
    assert judge_html.count("<b>受阻</b>") == 2
