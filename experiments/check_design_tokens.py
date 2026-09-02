#!/usr/bin/env python3
"""T-D 驗收 (3)：比對 index.html 的 CSS variables 與設計稿 `Main.dc.html` 的色票 token。

設計稿定案在 `docs/design/spectator/Main.dc.html` 的 `renderVals()`：`dark`／`light`
兩個物件逐 key 給出色票。實作把它們原樣抄成 `:root` 與 `[data-theme="light"]` 的
CSS variables（`--<key>`），本腳本從兩邊各自抓出 key→hex 值，逐 key 比對是否一致。

用法：
    python experiments/check_design_tokens.py
exit 0 = 兩組 token（dark、light）逐 key 完全一致；非 0 = 有差異，印出詳細列表。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN_PATH = ROOT / "docs" / "design" / "spectator" / "Main.dc.html"
INDEX_PATH = ROOT / "src" / "meeting_host" / "spectator" / "index.html"

# 設計稿裡 `key: '#xxxxxx'`（JS 物件字面量）
DESIGN_TOKEN_RE = re.compile(r"(\w+):\s*'(#[0-9a-fA-F]{3,8})'")
# index.html 裡 `--key: #xxxxxx;`（CSS custom property）
CSS_TOKEN_RE = re.compile(r"--(\w+):\s*(#[0-9a-fA-F]{3,8})\s*;")


def _extract_js_object(text: str, var_name: str) -> dict[str, str]:
    """抓 `const <var_name> = { ... };` 這段區塊內的 key/hex 值。"""
    marker = f"const {var_name} = {{"
    start = text.index(marker) + len(marker)
    end = text.index("};", start)
    block = text[start:end]
    return {k: v.lower() for k, v in DESIGN_TOKEN_RE.findall(block)}


def _extract_css_block(text: str, selector: str) -> dict[str, str]:
    """抓 `<selector> { ... }` 這段區塊內的 --key: #hex；"""
    start = text.index(selector)
    brace_start = text.index("{", start)
    brace_end = text.index("}", brace_start)
    block = text[brace_start:brace_end]
    return {k: v.lower() for k, v in CSS_TOKEN_RE.findall(block)}


def _diff(label: str, design: dict[str, str], impl: dict[str, str]) -> list[str]:
    lines = []
    design_keys, impl_keys = set(design), set(impl)
    for key in sorted(design_keys - impl_keys):
        lines.append(f"  [{label}] index.html 缺少 token：{key} (設計稿={design[key]})")
    for key in sorted(impl_keys - design_keys):
        lines.append(f"  [{label}] index.html 多出未定義 token：{key} (值={impl[key]})")
    for key in sorted(design_keys & impl_keys):
        if design[key] != impl[key]:
            lines.append(f"  [{label}] {key} 值不一致：設計稿={design[key]} index.html={impl[key]}")
    return lines


def main() -> int:
    design_text = DESIGN_PATH.read_text(encoding="utf-8")
    index_text = INDEX_PATH.read_text(encoding="utf-8")

    design_dark = _extract_js_object(design_text, "dark")
    design_light = _extract_js_object(design_text, "light")
    impl_dark = _extract_css_block(index_text, ":root {")
    impl_light = _extract_css_block(index_text, ':root[data-theme="light"] {')

    print(f"設計稿 dark：{len(design_dark)} 個 token；light：{len(design_light)} 個 token")
    print(f"index.html dark：{len(impl_dark)} 個 token；light：{len(impl_light)} 個 token")

    problems = _diff("dark", design_dark, impl_dark) + _diff("light", design_light, impl_light)

    if problems:
        print("\n差異：")
        for line in problems:
            print(line)
        print(f"\n結果：不一致（共 {len(problems)} 處）")
        return 1

    print(f"\n結果：一致（dark {len(design_dark)} 個、light {len(design_light)} 個 token 逐 key 相符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
