#!/usr/bin/env python3
"""把一場會議的事件檔導成**配音稿**：帶時間軸的逐字稿 ＋ 主席每一刻在畫面上的狀態。

給 demo 影片用。錄製時整場靜音（`live --script --mute`），聲音事後配——所以配音的人
需要知道每一秒畫面上在發生什麼，包括**主席判斷了但沒開口**的時刻（觀戰畫面會顯示
「忍住」，那是產品的一部分，`docs/demo-runbook.md`：「忍住的次數是產品的一部分，
講給評審聽」）。

用法：
    PYTHONPATH=src python experiments/dub_script.py <events.jsonl>
    PYTHONPATH=src python experiments/dub_script.py --latest demo       # 該劇本最新一場
    PYTHONPATH=src python experiments/dub_script.py --latest demo --md  # 輸出 Markdown 表格

時間戳是**會議相對時間**（跟畫面上的計時器同一個座標），精確到 0.1 秒。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src")); sys.path.insert(0, str(_HERE))
from meeting_host.fast_path import FAST_KINDS  # noqa: E402

MEETINGS = _HERE.parent / "meetings"

# 事件 → 配音稿要不要收，以及怎麼描述。
# 刻意**不收** fast_timer／share／speaking／partial：那些每秒好幾筆，是畫面的心跳，
# 不是配音的人需要看的東西。收的是「畫面上會變化、而且值得講一句」的事。
def fmt(t: float) -> str:
    return f"{int(t) // 60:02d}:{t % 60:04.1f}"


def rows(events: list[dict]) -> list[tuple[float, str, str]]:
    """→ [(時間, 標記, 內容)]。標記是給配音的人掃過去用的視覺錨點。"""
    out: list[tuple[float, str, str]] = []
    for e in events:
        t, k, d = e["t"], e["kind"], e["data"]
        if k == "meeting":
            out.append((t, "▶", f"會議開始　議題「{d['topic']}」　"
                                f"預計 {d['duration_min']} 分鐘　"
                                f"與會者 {'、'.join(d.get('participants') or [])}"))
        elif k == "utterance":
            out.append((t, "　", f"{d['speaker']}：{d['text']}"))
        elif k == "phase":
            out.append((t, "◆", f"階段切換 → {d['phase']}（{d.get('source', '')}）"))
        elif k == "glossary":
            term = d.get("term") or d.get("詞") or ""
            out.append((t, "📎", f"術語卡【{term}】出現在畫面右側"))
        elif k == "slow_score":
            adm, reason, typ = d.get("admissible"), d.get("reason") or "", d.get("type")
            if adm:
                continue          # 通過的那一筆由後面的 spoken 代表，不重複列
            if typ in ("無", "", None):
                continue          # 「沒事」不進配音稿，一場有上百筆
            # 判斷了、也選了型別，卻沒有開口——這正是畫面上的「忍住」
            out.append((t, "🤔", f"主席判斷【{typ}】但沒有開口："
                                 f"{reason or '三軸未過門檻'}"
                                 f"　P{d.get('positive')}/N{d.get('negative')}/None{d.get('none')}"))
        elif k == "queued":
            path = "快路" if d.get("kind") in FAST_KINDS else "慢路"
            how = "硬打斷（會先響提示音）" if d.get("hard") else "軟插入（等對方停頓）"
            out.append((t, "⏳", f"{path}【{d['kind']}】排入佇列，{how}"))
        elif k == "spoken":
            out.append((t, "🗣", f"主席開口【{d['kind']}】「{d['text']}」"))
        elif k in ("failed", "dropped"):
            out.append((t, "✖", f"介入作廢【{d.get('kind')}】{d.get('reason', '')}"))
        elif k == "minutes":
            out.append((t, "📄", "會議記錄產出，畫面切到決議／待辦／未解決事項"))
    return sorted(out, key=lambda r: r[0])


NOTE = """> ⚠️ 畫面右欄的「主席思考 N 次｜開口・受阻・忍住」，那個**忍住計數包含每一次
> 沒有開口的判斷**——一場會議每 5 秒判一次，其中絕大多數是「此刻沒事」
> （`type=無`）。這份稿刻意**不列那些**，否則一場八分鐘的會議會有上百行雜訊。
>
> 稿上的 🤔 只列「**判出了型別、卻仍然沒有開口**」的時刻——那才是值得講的克制：
> 它認得出這是離題，但剛提醒過（同型退避）、或證據不足以壓過保留意見（三軸未過門檻）。
> 所以計數器跳動的次數會多於稿上的行數，那是正常的。
"""


def render(rs: list[tuple[float, str, str]], markdown: bool) -> str:
    if not markdown:
        return NOTE.replace("> ", "").replace(">", "") + "\n" + \
            "\n".join(f"[{fmt(t)}] {mark} {text}" for t, mark, text in rs)
    head = "| 時間 | | 畫面上發生什麼 | 旁白 |\n|---|---|---|---|"
    body = "\n".join(f"| {fmt(t)} | {mark} | {text.replace('|', '／')} | |" for t, mark, text in rs)
    return NOTE + "\n" + head + "\n" + body


def latest_for(name: str) -> Path | None:
    from score_script_run import latest_run_for
    return latest_run_for(name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events", type=Path, nargs="?")
    ap.add_argument("--latest", metavar="劇本名", help="用該劇本最新一場錄影")
    ap.add_argument("--md", action="store_true", help="輸出 Markdown 表格（多一欄空的旁白讓你填）")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)

    path = a.events
    if a.latest:
        path = latest_for(a.latest)
        if path is None:
            print(f"找不到劇本 {a.latest} 的錄影", file=sys.stderr)
            return 1
    if path is None:
        ap.error("要給 events.jsonl，或用 --latest <劇本名>")

    events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    text = render(rows(events), a.md)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
        print(f"配音稿寫到 {a.out}（來源 {path.name}）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
