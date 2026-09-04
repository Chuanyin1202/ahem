"""主持風格檔位：既有快路門檻參數的組合（development-plan P1#8）。

不做「人格」——主席的權威來自中立與一致。檔位改的是它**怎麼行使權力**：
多快打斷、多久算被冷落、介入之間隔多久。快路四個鍵是 `fast_path` 的模組常數，
`apply()` 在啟動時覆寫一次；不給 `--style` 就一個值都不動。

三組數字是**未調校的起點**：以預設值為中線，嚴格檔收緊、溫和檔放寬、效率檔只加快
議程提醒與冷卻。哪一組適合哪種會議要靠真實會議實測，目前沒有那個資料。

`demo` 檔位是唯一會動到慢路的：關掉 `slow_path.NONE_VETO`（見該常數的
docstring）。2026-09-03 兩場真實三人會議實測：全場只有開場問候一句話，慢路
連續判出「離題」卻被否決權擋下。`NONE_VETO` 是唯一有驗證數據支持的設定
（validation-results.md #3b：真實會議誤報 60-80% → 0%），**只有 demo 現場、
且已經口頭跟評審／觀眾說明這是刻意調鬆的靈敏度時才用**——關掉它換來的是
demo 看得到主席開口，代價是誤報率沒有實測數據、可能比正式模式明顯更高。
"""
from __future__ import annotations

from . import fast_path, slow_path

STYLES: dict[str, dict[str, float]] = {
    "strict":    {"OVERTIME_SECONDS": 120.0, "NEGLECTED_SECONDS": 240.0, "COOLDOWN_SECONDS": 20.0, "SILENCE_SECONDS": 60.0},
    "gentle":    {"OVERTIME_SECONDS": 240.0, "NEGLECTED_SECONDS": 420.0, "COOLDOWN_SECONDS": 45.0, "SILENCE_SECONDS": 120.0},
    "efficient": {"OVERTIME_SECONDS": 150.0, "NEGLECTED_SECONDS": 300.0, "COOLDOWN_SECONDS": 20.0, "SILENCE_SECONDS": 60.0,
                  "AGENDA_WARN_RATIO": 1.0 / 4.0},
    "demo":      {"OVERTIME_SECONDS": 120.0, "NEGLECTED_SECONDS": 240.0, "COOLDOWN_SECONDS": 20.0, "SILENCE_SECONDS": 60.0,
                  "NONE_VETO": False},
}
LABELS = {"strict": "嚴格主席", "gentle": "溫和引導", "efficient": "效率優先", "demo": "Demo（誤報率未實測）"}

# 每個鍵覆寫哪個模組——目前只有 demo 檔位的 NONE_VETO 落在 slow_path，其餘沿用
# fast_path。新增跨模組的鍵時把它加進這裡，不要在 apply() 裡用 try/except 亂猜。
_MODULE = {"NONE_VETO": slow_path}

_STYLE_KEYS = {key for values in STYLES.values() for key in values}
_BASE_DEFAULTS = {key: getattr(_MODULE.get(key, fast_path), key) for key in _STYLE_KEYS}


def defaults() -> dict[str, float]:
    """回傳目前實際生效的門檻，供狀態頁與測試檢查。"""
    return {key: getattr(_MODULE.get(key, fast_path), key) for key in _STYLE_KEYS}


def apply(name: str | None) -> dict[str, float]:
    """套用檔位並回傳其設定值；每次先回復基準值，避免前一檔位殘留。"""
    if name is None:
        return {}
    if name not in STYLES:
        raise ValueError(f"未知的風格檔位：{name!r}，可用：{sorted(STYLES)}")
    for key, value in _BASE_DEFAULTS.items():
        setattr(_MODULE.get(key, fast_path), key, value)
    applied = {}
    for k, v in STYLES[name].items():
        mod = _MODULE.get(k, fast_path)
        assert hasattr(mod, k), k
        setattr(mod, k, v)
        applied[k] = v
    return applied
