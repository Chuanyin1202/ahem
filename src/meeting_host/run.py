"""觀察模式：回放一場會議，即時顯示主席的判斷。

它不出聲——只把「什麼時候想介入、為什麼」攤在螢幕上，
讓人可以事後逐條檢視。這是 evaluation.md 第 0 層盲標的素材來源。

用法:
    python -m meeting_host.run overtime            # 只跑快路，瞬間完成
    python -m meeting_host.run overtime --llm      # 加上慢路 LLM 評分
    python -m meeting_host.run --list
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "experiments"))
from scenarios import SCENARIOS  # noqa: E402

from . import fast_path, replay  # noqa: E402
from .state import MeetingState  # noqa: E402

TICK = 5.0  # 每隔幾秒檢查一次（真實系統中慢路也是這個節奏）


def fmt(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def stats_block(st: MeetingState, now: float) -> str:
    return "\n".join(
        f"- {p}：發言 {st.spoke_seconds(p) / 60:.1f} 分鐘"
        f"（佔 {st.share(p, now):.0%}），"
        f"已 {st.silent_seconds(p, now) / 60:.1f} 分鐘沒發言"
        for p in st.participants
    )


def load_script_scenario(path: str):
    """讀腳本測試台的劇本 JSON（`live.py --script` 同一份檔案），轉成這裡要的形狀。

    兩邊共用同一份劇本是刻意的：有聲的實跑（`live --script`，真實時間、有 Chair
    與 TTS）與無人值守的量化跑（這裡，模擬時間、瞬間跑完、可連跑 N 次）必須是
    **同一個輸入**，不然「調參前後的比較」比的是兩份不同的東西。
    """
    import json
    from pathlib import Path as _P
    from .live import load_script
    from .script_source import to_utterances
    d = load_script(_P(path))
    st = MeetingState(topic=d["topic"], duration_min=d["duration_min"],
                      participants=list(d["participants"]))
    for who in d["participants"]:
        st.joined_at[who] = 0.0   # 劇本從第 0 秒就全員在場
    utterances = to_utterances([tuple(r) for r in d["lines"]])
    sc = {"note": d.get("note", ""), "topic": d["topic"], "duration": d["duration_min"],
          "expect": d.get("expect", "（劇本未寫期望）"), "phase": d.get("phase"),
          "elapsed": (utterances[-1].end + 30) / 60.0}
    return sc, st, utterances


def run(name: str, use_llm: bool, script: str | None = None) -> None:
    if script:
        sc, st, utterances = load_script_scenario(script)
        name = f"script:{name}"
    else:
        sc = SCENARIOS[name]
        st, utterances = replay.load(sc)

    print(f"\n{'=' * 68}")
    print(f"場景：{name} — {sc['note']}")
    print(f"議題：{sc['topic']}（預計 {sc['duration']} 分鐘）")
    print(f"預期：{sc['expect']}")
    print("=" * 68)

    end = sc["elapsed"] * 60
    pending = list(utterances)
    # 逐字稿開始之前沒有任何資料，在那段區間套規則只會產生假警報。
    # 真實會議不存在這個問題——agent 從第一秒就在聽。
    now = utterances[0].start if utterances else 0.0
    fired: list[tuple[float, str, str | None]] = []
    slow_fired: list[tuple[float, dict]] = []
    done: set[tuple[str, str | None]] = set()
    prev_speaker: str | None = None
    last_scored = 0
    if use_llm:
        from .slow_path import is_intervention, phrase, score, should_score

    while now <= end:
        # 跟真實會議同一種資料形態：講話中只有「正在說話」訊號（partial），
        # 講完（commit）才拿到全文。以 start 揭露會讓回放預知整句內容，
        # 慢路的觸發時點、冷卻、快路後續結果全部失真
        for u in pending:
            if u.start <= now < u.end:
                st.speaking_now(u.speaker, u.start)
        while pending and pending[0].end <= now:
            u = pending.pop(0)
            st.stopped_speaking(u.speaker)
            st.add(u)
            # 他開口了 → 解除對他的「冷落」提醒
            done.discard(("有人被冷落", u.speaker))
            # 換人講話了 → 解除前一位的「超時」提醒（他還在講就不該重複喊）
            if prev_speaker and prev_speaker != u.speaker:
                done.discard(("發言超時", prev_speaker))
            prev_speaker = u.speaker
            print(f"[{fmt(u.start)}] {u.speaker}：{u.text[:52]}")

        for t in fast_path.check(st, now, done):
            fired.append((now, t.kind, t.target))
            st.interventions.append(now)
            done.add((t.kind, t.target))
            mark = "🔔 硬打斷" if t.hard else "💬 軟插入"
            print(f"    ├─ {mark}【{t.kind}】{t.detail}")
            break  # 一次只發一個介入，其餘等下一輪（仲裁規則）

        # 慢路跟產品同一個節奏、同一套冷卻規則——不然回放量到的不是產品
        if use_llm and should_score(st, now, last_scored):
            last_scored = len(st.utterances)
            try:
                r = score(st, now, sc.get("phase"))
            except Exception as e:  # noqa: BLE001
                print(f"    ⚠️ 慢路失敗：{type(e).__name__}")
                last_scored = 0  # 跟 live 一樣：失敗就下一 tick 重試同一批
                continue
            if is_intervention(r):
                # T29：話術是第二次呼叫，只在判定要介入之後才打——跟 live 一樣，
                # 不然這裡印出來的引號會永遠是空的（`score()` 不再回傳 utterance）。
                # 這支是觀察模式，沒有 Chair 也沒有 TOCTOU 問題（回放是單執行緒、
                # 時間由迴圈自己推進），所以只重現「先判斷、再產話術」這一段。
                try:
                    r["utterance"] = phrase(st, now, r, sc.get("phase"))
                except Exception as e:  # noqa: BLE001
                    r["utterance"] = ""
                    print(f"    ⚠️ 慢路話術失敗：{type(e).__name__}")
                slow_fired.append((now, r))
                st.interventions.append(now)
                print(f"    └─ 🤔 慢路【{r.get('type')}】"
                      f"P{r['positive']}/N{r['negative']}/None{r['none']}"
                      f"「{r.get('utterance', '')}」")

        now += TICK

    print(f"\n{'─' * 68}\n會議結束於 {fmt(end)}")
    print(stats_block(st, end))

    if fired:
        print(f"\n快路共觸發 {len(fired)} 次：")
        for t, kind, target in fired:
            print(f"  {fmt(t)}  {kind}" + (f" → {target}" if target else ""))
    else:
        print("\n快路未觸發任何介入")

    if use_llm:
        print(f"\n慢路共觸發 {len(slow_fired)} 次：" if slow_fired else "\n慢路未觸發任何介入")
        for t, r in slow_fired:
            print(f"  {fmt(t)}  {r.get('type')}  P{r['positive']}/N{r['negative']}/None{r['none']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?")
    ap.add_argument("--llm", action="store_true", help="加上慢路 LLM 評分")
    ap.add_argument("--script", default=None, metavar="PATH",
                    help="改吃腳本測試台的劇本 JSON（與 live --script 同一份檔案）")
    ap.add_argument("--rounds", type=int, default=1,
                    help="同一份輸入跑幾輪。LLM 判斷本身不穩定，單次結果只是一個抽樣——"
                         "任何比較都至少 5 輪（docs/evaluation.md）")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.script:
        from pathlib import Path as _P
        for i in range(args.rounds):
            if args.rounds > 1:
                print(f"\n\n{'█' * 20} 第 {i + 1}/{args.rounds} 輪 {'█' * 20}")
            run(_P(args.script).stem, args.llm, script=args.script)
        return
    if args.list or not args.scenario:
        for k, v in SCENARIOS.items():
            print(f"  {k:<18} {v['note']}")
        return
    run(args.scenario, args.llm)


if __name__ == "__main__":
    main()
