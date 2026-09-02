"""階段自動判斷在觀戰 UI 的接線（`spectator/index.html` 的 `phase`／`phase_suggestion` 處理）。

回放 `examples/synthetic-phases.events.jsonl`（虛構三階段會議，含 5 筆建議、2 筆自動切換、
以及一筆帶新階段的 `meeting` 重送）。要驗的：
- 前端收到這兩種新事件不拋 JS 例外
- 時間軸的階段標記恰好兩條——`phase` 事件與 `meeting` 重送不會為同一次切換畫兩次
- 狀態列在回放結束時顯示最後的階段（決定）
- 一筆**未套用**的建議會讓狀態列出現「（建議：…）」，套用後提示消失

需要 Playwright；沒裝就 skip，跟其他觀戰 UI 測試一樣。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from meeting_host import spectator  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "synthetic-phases.events.jsonl"


def _run():
    async def body():
        session = spectator.ReplaySession(spectator._load_events(SAMPLE))
        server = TestServer(spectator._build_app(session))
        await server.start_server()
        try:
            await session.replay(speed=1_000_000)
            url = str(server.make_url("/"))

            def check() -> dict:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(url)
                    page.wait_for_timeout(600)
                    out = {
                        "marks": page.evaluate("__spectator.state.phaseMarks.map(m => m.phase)"),
                        "status_end": page.eval_on_selector("#status", "el => el.textContent"),
                        "hint_end": page.evaluate("__spectator.state.phaseHint"),
                    }
                    # 灌一筆未套用的建議（跟現況不同）→ 狀態列要出現括號提示
                    page.evaluate("""__spectator.handleEvent({kind: "phase_suggestion", t: 400,
                        data: {phase: "發散期", confidence: 0.9, reason: "", current: "收斂期", applied: false}})""")
                    out["status_with_hint"] = page.eval_on_selector("#status", "el => el.textContent")
                    # 再灌一筆真的切換 → 提示消失、標記多一條
                    page.evaluate("""__spectator.handleEvent({kind: "phase", t: 410, data: {phase: "發散期", source: "manual"}})""")
                    out["status_after_switch"] = page.eval_on_selector("#status", "el => el.textContent")
                    out["marks_after"] = page.evaluate("__spectator.state.phaseMarks.map(m => m.phase)")
                    browser.close()
                    assert not errors, f"頁面 JS 例外：{errors}"
                    return out
            return await asyncio.to_thread(check)
        finally:
            await server.close()
    return asyncio.run(body())


def test_phase_events_render_without_errors_and_marks_are_deduplicated():
    out = _run()
    assert out["marks"] == ["呻吟區", "收斂期"], out["marks"]          # meeting 重送沒有多畫一條
    assert out["status_end"].startswith("決定階段"), out["status_end"]
    assert out["hint_end"] is None                                    # 最後一筆建議已套用，提示清掉
    assert "（建議：發想）" in out["status_with_hint"], out["status_with_hint"]
    assert "（建議" not in out["status_after_switch"] and out["status_after_switch"].startswith("發想階段")
    assert out["marks_after"] == ["呻吟區", "收斂期", "發散期"]
