#!/usr/bin/env python3
"""話術指令對照實驗：同一批真實評分點，只換 `utterance` 那一行指令，看主席講出什麼。

要回答的問題不是「介入次數會不會變」（那已經量過了），而是：
**主席講的罐頭話，是 prompt 的約束逼出來的，還是模型的極限？**

現行 `slow_path.TEMPLATE` 對話術的指令是

    "utterance": "<若要介入，你會說的話。≤2句，要給出可執行的下一步。不介入則留空>"

假說：「≤2 句」＋「要給出可執行的下一步」這兩個約束本身就會長出
「回到 X；接下來各自提出 Y」這個形狀。

四個條件（V1／V2／V3 互為消融，只差 utterance 那一行）：

    v0_baseline   現行指令，一個字都沒動（對照組）
    v1_unbound    只拿掉兩個約束 —— 直接驗假說
    v2_only_here  v1 ＋「這句話要只有在這場會議說得出口」的內容門檻
    v3_notice     v1 ＋ 在 utterance 前面多一個 `notice` 欄位，
                  先寫下逐字稿裡真正看到的一件具體事，話術要接著它講
                  （＝把「先看再說」塞進同一次呼叫，兩次呼叫架構的窮人版）

重建邏輯全部複用 `rescore_slow_path.py`（`Replay` / `solve_score_times` / `verify`），
這裡不另寫一套 —— 那份重建已對過 858 筆 `fast_timer.silent` 與 137 筆 `share`，零誤差。

`src/meeting_host/` 一個字都不改：變體只在這支腳本裡，靠暫時覆寫模組全域
`slow_path.TEMPLATE` 產生 prompt，覆寫範圍鎖在 `build_prompt()` 那一瞬間。

用法：
    python experiments/utterance_variants.py experiments/holdout/2026-08-29-two-person/meeting.events.jsonl
    python experiments/utterance_variants.py <events.jsonl> --report-only   # 只從快取重印
"""
import argparse
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
import sys  # noqa: E402

sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

import rescore_slow_path as R  # noqa: E402  重建邏輯的唯一來源
from meeting_host import slow_path  # noqa: E402
from meeting_host.events import Event  # noqa: E402
from meeting_host.replay import CHARS_PER_SECOND  # noqa: E402  中文口語語速 4.5 字/秒

# ── 變體定義 ──────────────────────────────────────────────────────────────

BASE_LINE = '  "utterance": "<若要介入，你會說的話。≤2句，要給出可執行的下一步。不介入則留空>"'

V1_LINE = '  "utterance": "<若要介入，你會說的話。不介入則留空>"'

V2_LINE = (
    '  "utterance": "<若要介入，你會說的話。不介入則留空。'
    '這句話要通得過一個檢查：把它原封不動貼到任何一場別的會議也一樣成立的話，就不要說。'
    '裡面必須有這場會議剛剛真的出現的東西——某人講過的原話、兩個人各自實際主張了什麼、'
    '哪件事被當成前提卻從頭到尾沒講明。>"'
)

V3_LINES = (
    '  "notice": "<先寫下你在上面那份逐字稿裡真正看到的一件事。要指得到具體的一句話、'
    '或某個人實際主張了什麼，不是「大家在討論 X」這種概括。看不到就寫「無」>",\n'
    '  "utterance": "<若要介入，你會說的話：把上面 notice 那件事講給他們聽，'
    '讓他們知道你聽見了什麼。不介入則留空>"'
)


def _swap(new_line: str) -> str:
    """把 TEMPLATE 裡 utterance 那一行換掉。找不到原句就炸，不要默默產生沒換到的變體。"""
    tpl = slow_path.TEMPLATE
    if tpl.count(BASE_LINE) != 1:
        raise RuntimeError(
            "slow_path.TEMPLATE 裡找不到唯一的 utterance 指令行，"
            "production prompt 已改動——先確認變體定義還對得上，不要跑。")
    return tpl.replace(BASE_LINE, new_line)


def variants() -> dict[str, dict]:
    return {
        "v0_baseline": {"desc": "現行指令（≤2句＋可執行的下一步）", "template": slow_path.TEMPLATE},
        "v1_unbound": {"desc": "只拿掉兩個約束", "template": _swap(V1_LINE)},
        "v2_only_here": {"desc": "v1 ＋「只有這場會議說得出口」內容門檻", "template": _swap(V2_LINE)},
        "v3_notice": {"desc": "v1 ＋ 先寫 notice 再說話", "template": _swap(V3_LINES)},
    }


# ── 呼叫 ──────────────────────────────────────────────────────────────────

_TPL_LOCK = threading.Lock()


def build_prompt_with(template: str, st, now: float, phase: str | None) -> str:
    """用指定 template 產 prompt。

    `slow_path.build_prompt()` 讀的是模組全域 `TEMPLATE`，所以覆寫是唯一不動
    production code 的做法。鎖只圈住這段純字串運算（無 I/O），HTTP 在鎖外面，
    多變體可以平行跑而不會互相拿到對方的 template。
    """
    with _TPL_LOCK:
        orig = slow_path.TEMPLATE
        slow_path.TEMPLATE = template
        try:
            return slow_path.build_prompt(st, now, phase)
        finally:
            slow_path.TEMPLATE = orig


def call_llm(prompt: str) -> dict:
    """與 `slow_path.score()` 的 request body 逐欄相同，差別只在 prompt 由外面給。

    不直接呼叫 `slow_path.score()`：那支把「產 prompt」與「送請求」綁在一起，
    要平行跑不同變體就得把全域覆寫撐過整個 HTTP 往返。
    """
    body = {
        "model": slow_path.MODEL,
        "reasoning_effort": slow_path.EFFORT,
        "messages": [{"role": "system", "content": slow_path.SYSTEM},
                     {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        slow_path.API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read())
    result = json.loads(payload["choices"][0]["message"]["content"])
    result["verdict"] = slow_path.decide(result)
    return result


def run_one(template: str, st, t_score: float, phase: str | None, retries: int = 3) -> dict:
    prompt = build_prompt_with(template, st, t_score, phase)
    for attempt in range(retries):
        try:
            r = call_llm(prompt)
            r["is_intervention"] = slow_path.is_intervention(r)
            return r
        except Exception as exc:  # noqa: BLE001  單點失敗不能讓整份跑掉
            err = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
    return {"error": err}


# ── 主流程 ────────────────────────────────────────────────────────────────

def collect(replay: R.Replay, solved: list[dict], vs: dict[str, dict],
            workers: int = 6) -> dict:
    R.load_api_key()
    states = [replay.state_at(s["t_score"]) for s in solved]
    jobs = [(vn, i) for vn in vs for i in range(len(solved))]
    out: dict[str, list] = {vn: [None] * len(solved) for vn in vs}
    done = 0
    lock = threading.Lock()

    def work(job):
        nonlocal done
        vn, i = job
        r = run_one(vs[vn]["template"], states[i], solved[i]["t_score"], replay.phase)
        with lock:
            out[vn][i] = r
            done += 1
            print(f"  [{done:>3}/{len(jobs)}] {vn:<13} #{i + 1:>2} "
                  f"{r.get('type', 'ERR')}/{r.get('verdict', '')} "
                  f"{len(r.get('utterance') or '')}字", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, jobs))

    return {
        "source_events": None,  # 由 main 填
        "model": slow_path.MODEL, "effort": slow_path.EFFORT, "phase": replay.phase,
        "rounds": 1,
        "variants": {vn: {"desc": v["desc"], "template": v["template"]} for vn, v in vs.items()},
        "points": [
            {"index": i, "t_score": s["t_score"], "emit_t": s["emit_t"],
             "n_utterances": s["n_utterances"],
             "results": {vn: out[vn][i] for vn in vs}}
            for i, s in enumerate(solved)
        ],
    }


def summarize(blob: dict) -> None:
    vs = list(blob["variants"])
    pts = blob["points"]
    print("\n" + "=" * 110)
    print("副作用：介入次數／type 分佈／話術長度")
    print("=" * 110)
    print(f"{'變體':<14} {'介入':>5} {'離題':>5} {'僵局':>5} {'重複':>5} {'假共識':>6} "
          f"{'事實錯誤':>7} {'無':>4} | {'中位字數':>8} {'最長':>5} {'中位秒':>7} {'最長秒':>7}")
    print("-" * 110)
    for vn in vs:
        rs = [p["results"][vn] for p in pts]
        types = [r.get("type") for r in rs]
        lens = sorted(len(r.get("utterance") or "") for r in rs if (r.get("utterance") or ""))
        med = lens[len(lens) // 2] if lens else 0
        mx = lens[-1] if lens else 0
        n_int = sum(1 for r in rs if r.get("is_intervention"))
        c = lambda k: types.count(k)  # noqa: E731
        print(f"{vn:<14} {n_int:>5} {c('離題'):>5} {c('僵局'):>5} {c('重複'):>5} "
              f"{c('假共識'):>6} {c('事實錯誤'):>7} {c('無'):>4} | "
              f"{med:>8} {mx:>5} {med / CHARS_PER_SECOND:>7.1f} {mx / CHARS_PER_SECOND:>7.1f}")
    print(f"\n（秒數＝字數 ÷ {CHARS_PER_SECOND}，語速常數取自 src/meeting_host/replay.py）")

    # 模板指紋：開頭 6 字重複率，粗略量「同一個形狀」出現多少次
    print("\n開頭 6 字重複率（同一個開頭出現 ≥2 次的句子佔比）")
    for vn in vs:
        us = [(p["results"][vn].get("utterance") or "") for p in pts]
        us = [u for u in us if u]
        heads: dict[str, int] = {}
        for u in us:
            heads[u[:6]] = heads.get(u[:6], 0) + 1
        dup = sum(n for n in heads.values() if n >= 2)
        top = sorted(heads.items(), key=lambda kv: -kv[1])[:3]
        print(f"  {vn:<14} {dup}/{len(us)}  最常見開頭：" +
              "、".join(f"「{h}」×{n}" for h, n in top))


def print_sentences(blob: dict, idxs: list[int] | None = None) -> None:
    vs = list(blob["variants"])
    pts = blob["points"]
    sel = idxs if idxs else range(len(pts))
    print("\n" + "=" * 110)
    print("逐點原文對照")
    print("=" * 110)
    for i in sel:
        p = pts[i]
        print(f"\n── #{p['index'] + 1}  t={p['t_score']:.1f}s ({p['t_score'] / 60:.1f} 分) "
              f"utt={p['n_utterances']} " + "─" * 50)
        for vn in vs:
            r = p["results"][vn]
            u = r.get("utterance") or "（空）"
            flag = "★介入" if r.get("is_intervention") else "  ----"
            print(f"  {vn:<13} {flag} [{r.get('type')}/{r.get('verdict')} "
                  f"P{r.get('positive')}/N{r.get('negative')}/No{r.get('none')}]")
            if r.get("notice"):
                print(f"      notice: {r['notice']}")
            print(f"      {u}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sentences", type=str, default=None,
                    help="逗號分隔的 1-based 點號；不給就全印")
    args = ap.parse_args(argv)

    raw = [json.loads(l) for l in args.events.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = sorted((Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw), key=lambda e: e.t)
    replay = R.Replay(events)
    slow_events = [e for e in events if e.kind == "slow_score"]
    out_dir = args.out or (_HERE / "out" / f"utterance-variants-{args.events.resolve().parent.name}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / "variants.json"

    solved_all = R.solve_score_times(replay, slow_events, drift=R.tick_drift(events))
    solved = solved_all[:args.limit] if args.limit else solved_all

    if args.report_only:
        if not cache.is_file():
            print(f"找不到 {cache}")
            return 2
        blob = json.loads(cache.read_text(encoding="utf-8"))
    else:
        v = R.verify(replay, events, solved_all)
        hard = (v["fast_timer"]["silent_mismatch"] or v["fast_timer"]["participant_order_mismatch"]
                or v["share"]["mismatch"])
        print(f"重建對帳：fast_timer.silent {v['fast_timer']['checked']} 筆／"
              f"share {v['share']['checked']} 筆，"
              f"mismatch={v['fast_timer']['silent_mismatch']}/{v['share']['mismatch']}")
        if hard:
            print("重建與事件檔對不上，停。")
            return 2
        vs = variants()
        print(f"\n{len(vs)} 個變體 × {len(solved)} 點 × 1 輪 = {len(vs) * len(solved)} 次呼叫")
        blob = collect(replay, solved, vs, workers=args.workers)
        blob["source_events"] = str(args.events)
        cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n原始輸出已寫入 {cache}")

    idxs = [int(x) - 1 for x in args.sentences.split(",")] if args.sentences else None
    print_sentences(blob, idxs)
    summarize(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
