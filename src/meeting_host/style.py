"""主持風格檔位：既有快路門檻參數的組合（development-plan P1#8）。

不做「人格」——主席的權威來自中立與一致。檔位改的是它**怎麼行使權力**：
多快打斷、多久算被冷落、介入之間隔多久。全部是 `fast_path` 既有的模組常數，
`apply()` 在啟動時覆寫一次；不給 `--style` 就一個值都不動。

三組數字是**未調校的起點**：以預設值為中線，嚴格檔收緊、溫和檔放寬、效率檔只加快
議程提醒與冷卻。哪一組適合哪種會議要靠真實會議實測，目前沒有那個資料。
設計文件裡的階段乘數（interruption-design 改動 2）尚未實作，所以檔位不涉及慢路。
"""
from __future__ import annotations

from . import fast_path

STYLES: dict[str, dict[str, float]] = {
    "strict":    {"OVERTIME_SECONDS": 120.0, "NEGLECTED_SECONDS": 240.0, "COOLDOWN_SECONDS": 20.0, "SILENCE_SECONDS": 60.0},
    "gentle":    {"OVERTIME_SECONDS": 240.0, "NEGLECTED_SECONDS": 420.0, "COOLDOWN_SECONDS": 45.0, "SILENCE_SECONDS": 120.0},
    "efficient": {"OVERTIME_SECONDS": 150.0, "NEGLECTED_SECONDS": 300.0, "COOLDOWN_SECONDS": 20.0, "SILENCE_SECONDS": 60.0,
                  "AGENDA_WARN_RATIO": 1.0 / 4.0},
}
LABELS = {"strict": "嚴格主席", "gentle": "溫和引導", "efficient": "效率優先"}


def defaults() -> dict[str, float]:
    keys = {k for v in STYLES.values() for k in v}
    return {k: getattr(fast_path, k) for k in keys}


def apply(name: str | None) -> dict[str, float]:
    """套用檔位到 `fast_path` 的模組常數，回傳實際生效的值。`None` 不動任何東西。"""
    if name is None:
        return {}
    if name not in STYLES:
        raise ValueError(f"未知的風格檔位：{name!r}，可用：{sorted(STYLES)}")
    applied = {}
    for k, v in STYLES[name].items():
        assert hasattr(fast_path, k), k
        setattr(fast_path, k, v)
        applied[k] = v
    return applied
