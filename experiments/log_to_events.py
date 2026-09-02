#!/usr/bin/env python3
"""把舊格式的終端逐字稿 log（`src/meetings/*.log`，T-B 事件匯流排上線前留下的）
轉成 `events.jsonl`，讓沒有真正 events.jsonl 的舊場次也能跑一次 T-C 的
`write_minutes()`（主要是 A：LLM 會議產出）。

只是「盡力還原」，不是精確重建：
- 舊 log 是純文字，沒有結構化的 `target` 欄位——轉出來的事件一律 `target: null`。
- 快路（`├─`）排入時的文字是規則的 `detail`（人看的描述），跟最終出聲的話術不是
  同一份字串；慢路（`└─`）排入時完全沒有話術文字。兩者都靠「同一個 kind 之後第一次
  出現的 🗣 主席…／⚠️ 開口失敗…」回填成同一份文字，讓 `minutes.py` 的
  `_pair_interventions`（比對 kind／target／text）配對得上；真的作廢（無 🗣／⚠️ 行）
  的候選，text 就留空字串——這剛好等於它配對的 queued 也是空字串，一樣配對得上。
- 慢路被壓掉的三種情況（type=無／無話術／冷卻）沒有對應的 queued 事件，跟
  `live.py._run_slow_score` 的行為一致（不 admissible 就不會 emit queued）。
- pros／cons 恆為空 list（舊 log 沒印）。

用法:
    python experiments/log_to_events.py <log 檔路徑> <輸出 jsonl 路徑>
"""
import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from meeting_host.events import Event  # noqa: E402

HEADER_RE = re.compile(r"^議題：(?P<topic>.+?)（預計 (?P<duration>\d+) 分鐘，階段：(?P<phase>.+?)）$")
UTTERANCE_RE = re.compile(r"^\[(?P<mm>\d{2}):(?P<ss>\d{2})\] (?P<speaker>[^：]+)：(?P<text>.*)$")
MIC_RE = re.compile(r"音訊持續中：\d+ 個封包（約 (?P<sec>[\d.]+) 秒）")
SPOKEN_RE = re.compile(r"🗣\s+主席【(?P<kind>[^】]+)】「(?P<text>.*)」$")
QUEUED_FAST_RE = re.compile(
    r"├─\s*(?:🔔|💬)\s*(?:硬打斷|軟插入)\s*【(?P<kind>[^】]+)】(?P<detail>.+?) → 排入$")
QUEUED_SLOW_RE = re.compile(
    r"└─\s*🤔\s*慢路【(?P<type>[^】]+)】P(?P<p>\d+)/N(?P<n>\d+)/None(?P<none>\d+) → 排入$")
DROPPED_RE = re.compile(r"\(主席作廢【(?P<kind>[^】]+)】(?P<reason>.+?)\)$")
FAILED_RE = re.compile(r"⚠️ 主席開口失敗【(?P<kind>[^】]+)】(?P<reason>.+?)；本該說：「(?P<text>.*)」$")
SLOW_NONE_RE = re.compile(
    r"\(慢路被 type=無 壓掉\) P(?P<p>\d+)/N(?P<n>\d+)/None(?P<none>\d+)「(?P<utterance>.*)」")
SLOW_NO_UTTERANCE_RE = re.compile(r"\(慢路判介入但無話術\)")
SLOW_COOLDOWN_RE = re.compile(r"\(慢路結果在冷卻期內作廢\)【(?P<type>[^】]+)】")


def parse_log(text: str) -> list[Event]:
    events: list[Event] = []
    t = 0.0
    participants: list[str] = []
    topic, duration, phase = "會議", 30, "發散期"
    # kind → 尚未回填文字的 queued 事件（只認最近一筆，同一 kind 同時只會有一個未結案
    # 候選——真實系統靠 30 秒退避與 done 集合保證這點，短場次回放也大致成立）
    pending_by_kind: dict[str, Event] = {}

    for raw in text.splitlines():
        line = raw.rstrip("\n")

        m = HEADER_RE.match(line)
        if m:
            topic, duration, phase = m["topic"], int(m["duration"]), m["phase"]
            continue

        m = MIC_RE.search(line)
        if m:
            t = max(t, float(m["sec"]))
            continue

        m = UTTERANCE_RE.match(line)
        if m:
            start = float(int(m["mm"]) * 60 + int(m["ss"]))
            t = max(t, start)
            speaker = m["speaker"]
            if speaker not in participants:
                participants.append(speaker)
            events.append(Event("utterance", t, {
                "speaker": speaker, "text": m["text"], "start": start, "end": start}))
            continue

        m = SPOKEN_RE.search(line)
        if m:
            kind, text_ = m["kind"], m["text"]
            queued = pending_by_kind.pop(kind, None)
            if queued is not None and not queued.data["text"]:
                queued.data["text"] = text_
            events.append(Event("spoken", t, {
                "kind": kind, "target": None, "text": text_, "hard": None, "at": t}))
            continue

        m = QUEUED_FAST_RE.search(line)
        if m:
            kind = m["kind"]
            queued = Event("queued", t, {"kind": kind, "target": None, "text": "", "hard": None})
            events.append(queued)
            pending_by_kind[kind] = queued
            continue

        m = QUEUED_SLOW_RE.search(line)
        if m:
            kind = m["type"]
            events.append(Event("slow_score", t, {
                "positive": int(m["p"]), "negative": int(m["n"]), "none": int(m["none"]),
                "type": kind, "verdict": "正向介入", "utterance": "",
                "pros": [], "cons": [], "admissible": True, "reason": ""}))
            queued = Event("queued", t, {"kind": kind, "target": None, "text": "", "hard": False})
            events.append(queued)
            pending_by_kind[kind] = queued
            continue

        m = DROPPED_RE.search(line)
        if m:
            kind = m["kind"]
            pending_by_kind.pop(kind, None)  # 沒有話術可回填，維持雙方皆空字串以配對
            events.append(Event("dropped", t, {
                "kind": kind, "target": None, "text": "", "reason": m["reason"]}))
            continue

        m = FAILED_RE.search(line)
        if m:
            kind, text_ = m["kind"], m["text"]
            queued = pending_by_kind.pop(kind, None)
            if queued is not None and not queued.data["text"]:
                queued.data["text"] = text_
            events.append(Event("failed", t, {
                "kind": kind, "target": None, "text": text_, "reason": m["reason"]}))
            continue

        m = SLOW_NONE_RE.search(line)
        if m:
            events.append(Event("slow_score", t, {
                "positive": int(m["p"]), "negative": int(m["n"]), "none": int(m["none"]),
                "type": "無", "verdict": "不介入", "utterance": m["utterance"],
                "pros": [], "cons": [], "admissible": False, "reason": "type=無"}))
            continue

        if SLOW_NO_UTTERANCE_RE.search(line):
            events.append(Event("slow_score", t, {
                "positive": None, "negative": None, "none": None, "type": None, "verdict": None,
                "utterance": "", "pros": [], "cons": [], "admissible": False, "reason": "無話術"}))
            continue

        m = SLOW_COOLDOWN_RE.search(line)
        if m:
            events.append(Event("slow_score", t, {
                "positive": None, "negative": None, "none": None, "type": m["type"], "verdict": None,
                "utterance": "", "pros": [], "cons": [], "admissible": False, "reason": "冷卻"}))
            continue

    events.insert(0, Event("meeting", 0.0, {
        "topic": topic, "duration_min": duration, "phase": phase, "participants": participants}))
    return events


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log_path", type=Path)
    ap.add_argument("out_path", type=Path)
    args = ap.parse_args()

    events = parse_log(args.log_path.read_text(encoding="utf-8"))
    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    with args.out_path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(dataclasses.asdict(event), ensure_ascii=False) + "\n")
    print(f"{len(events)} 筆事件 → {args.out_path}")


if __name__ == "__main__":
    main()
