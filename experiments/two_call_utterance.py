#!/usr/bin/env python3
"""T29 驗收：慢路拆成兩次呼叫（判斷／話術）之後，主席到底講出什麼。

要回答的問題與 `utterance_variants.py` 那一支是同一個，但對象不同：那支是
**在一次呼叫的架構下**換話術指令的消融實驗（v0/v1/v2/v3，結論是「同一次呼叫
分不開」）；這支跑的是**已經拆成兩次呼叫的 production 程式碼本身**
（`slow_path.score()` ＋ `slow_path.phrase()`），跟那四個變體並排比。

兩種模式：

    # A. 選 effort 與長度上限用的 sweep（子集點 × 多組設定，量延遲與句子）
    python experiments/two_call_utterance.py <events.jsonl> --sweep

    # B. 驗收主表（全部評分點跑一次新架構，與 variants.json 並排）
    python experiments/two_call_utterance.py <events.jsonl>
    python experiments/two_call_utterance.py <events.jsonl> --report-only

重建邏輯全部複用 `rescore_slow_path.py`（`Replay` / `solve_score_times` / `verify`），
這裡不另寫一套——那份重建已對過 858 筆 `fast_timer.silent` 與 137 筆 `share`，零誤差。
`utterance_variants.py`／`rescore_slow_path.py`／`effort_*.py` 一個字都不改，只 import。

## 話術產生的點：為什麼是全部 34 點，不是只有「判定要介入」的那幾點

production 只在通過第一關閘門（`live.slow_gate`）時才打話術呼叫。但那樣一場
只有 9-17 句，樣本太小、也沒辦法跟 v0（31 句）／v2（30 句）在同一批點上並排
比引號率。所以這支在**全部 34 點**都產一次話術（把該點的判斷結果原樣餵進去），
另外標出 production 真的會用到的是哪幾點，兩組數字都報。
副作用要講清楚：`type=無` 的點被餵進一個寫著「你已經決定要開口」的 prompt，
是 off-distribution 的用法，那幾句的品質不能代表 production——所以下面每張表
都同時給「全部 34 點」與「僅 production 會用的點」兩欄。

## 引號的認定

不是只看有沒有「」。每一段被引號框起來的字，都會拿去跟那個時點
`st.recent()` 的逐字稿做**去標點後的子字串比對**——對得上才算「逐字引用」，
對不上就是「引號裡的話沒人說過」（＝捏造），單獨列出來。只數引號不驗內容
的話，模型只要學會加引號就能刷高分。
"""
import argparse
import json
import re
import statistics
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
from meeting_host import live, slow_path  # noqa: E402
from meeting_host.events import Event  # noqa: E402
from meeting_host.replay import CHARS_PER_SECOND  # noqa: E402  中文口語語速 4.5 字/秒

# ── 呼叫（帶延遲量測）─────────────────────────────────────────────────────

_TPL_LOCK = threading.Lock()


def _post(body: dict, timeout: float = 90.0) -> tuple[dict, float]:
    """打一次 chat/completions，回傳 (解析後的 JSON, 往返秒數)。

    request body 逐欄與 `slow_path.score()`／`slow_path.phrase()` 相同，差別只在
    這裡要把往返時間量出來——production 那兩支不回傳延遲。
    金鑰只從 os.environ 取（`R.load_api_key()` 放進去的），不落地、不列印。
    """
    import os
    req = urllib.request.Request(
        slow_path.API_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    return json.loads(payload["choices"][0]["message"]["content"]), time.perf_counter() - t0


def call_judge(st, now: float, phase: str | None, retries: int = 3) -> dict:
    """第一次呼叫：判斷。prompt 直接用 production 的 `slow_path.build_prompt()`。"""
    body = {
        "model": slow_path.MODEL,
        "reasoning_effort": slow_path.EFFORT,
        "messages": [{"role": "system", "content": slow_path.SYSTEM},
                     {"role": "user", "content": slow_path.build_prompt(st, now, phase)}],
        "response_format": {"type": "json_object"},
    }
    for attempt in range(retries):
        try:
            r, sec = _post(body)
            r["verdict"] = slow_path.decide(r)
            r["is_intervention"] = slow_path.is_intervention(r)
            r["latency_seconds"] = round(sec, 3)
            return r
        except Exception as exc:  # noqa: BLE001  單點失敗不能讓整份跑掉
            err = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
    return {"error": err}


def call_phrase(st, now: float, r: dict, phase: str | None, *,
                effort: str | None = None, max_chars: int | None = None,
                template: str | None = None, retries: int = 3) -> dict:
    """第二次呼叫：話術。

    prompt 走 production 的 `slow_path.build_utterance_prompt()`；sweep 要換
    effort／字數上限／模板時，才暫時覆寫模組全域（鎖只圈住純字串運算，HTTP 在
    鎖外面，所以多個設定可以平行跑而不會互相拿到對方的模板）。
    """
    with _TPL_LOCK:
        o_tpl, o_max = slow_path.UTTERANCE_TEMPLATE, slow_path.MAX_UTTERANCE_CHARS
        if template is not None:
            slow_path.UTTERANCE_TEMPLATE = template
        if max_chars is not None:
            slow_path.MAX_UTTERANCE_CHARS = max_chars
        try:
            prompt = slow_path.build_utterance_prompt(st, now, r, phase)
        finally:
            slow_path.UTTERANCE_TEMPLATE, slow_path.MAX_UTTERANCE_CHARS = o_tpl, o_max
    body = {
        "model": slow_path.MODEL,
        "reasoning_effort": effort or slow_path.UTTERANCE_EFFORT,
        "messages": [{"role": "system", "content": slow_path.UTTERANCE_SYSTEM},
                     {"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    for attempt in range(retries):
        try:
            out, sec = _post(body)
            text = out.get("utterance") or ""
            return {"utterance": text.strip() if isinstance(text, str) else "",
                    "latency_seconds": round(sec, 3)}
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
    return {"error": err, "utterance": ""}


# ── 指標 ─────────────────────────────────────────────────────────────────

_QUOTE_RE = re.compile(r"[「『]([^」』]{2,})[」』]")
_PUNCT_RE = re.compile(r"[\s，。、；：！？…—－·,.!?;:~\"'（）()【】\[\]{}]")


def _norm(s: str) -> str:
    return _PUNCT_RE.sub("", s)


def transcript_text(st) -> str:
    """該時點餵給 LLM 的逐字稿本文（`st.recent()`，跟兩支 prompt 用的是同一批）。"""
    return "".join(u.text for u in st.recent())


def _lcs_ratio(q: str, hay: str) -> float:
    """引號內容與逐字稿的最長共同連續子字串佔引號長度的比例。

    用來把兩種「對不上」分開：
    - STT 同音字／語助詞掉一個字（「他已經判斷19次」→「它已經判斷19次」）：比例極高
    - 把分散在好幾句話裡的詞拼成一句引號（「三、四、五去那邊聊天、現場再買」）：比例低
    只數「完全對得上」會把前者跟後者算成同一件事，那對誰都不公平。
    """
    if not q:
        return 0.0
    best = 0
    for i in range(len(q)):
        for j in range(i + best + 1, len(q) + 1):
            if q[i:j] in hay:
                best = j - i
            else:
                break
    return best / len(q)


NEAR_THRESHOLD = 0.7  # 連續對上 7 成以上算「近似逐字」——不是精算，是分開上面那兩類的分界


def quote_check(utterance: str, transcript: str) -> dict:
    """引號認定：框起來的字要真的在逐字稿裡出現過（去標點後子字串比對）。

    給兩個層級的數字，不要只給一個：
    - `grounded`：整段引號逐字對得上（最嚴）
    - `near`：最長共同連續子字串 ≥ NEAR_THRESHOLD（放過同音字／掉語助詞）
    """
    quotes = _QUOTE_RE.findall(utterance or "")
    hay = _norm(transcript)
    grounded = [q for q in quotes if _norm(q) and _norm(q) in hay]
    ratios = {q: _lcs_ratio(_norm(q), hay) for q in quotes}
    near = [q for q in quotes if ratios[q] >= NEAR_THRESHOLD]
    return {"n_quotes": len(quotes), "quotes": quotes,
            "n_grounded": len(grounded),
            "n_near": len(near),
            "ratios": {q: round(v, 2) for q, v in ratios.items()},
            "ungrounded": [q for q in quotes if q not in grounded],
            "far": [q for q in quotes if q not in near],
            "has_quote": bool(quotes),
            "all_grounded": bool(quotes) and len(grounded) == len(quotes),
            "all_near": bool(quotes) and len(near) == len(quotes)}


def has_pullback(utterance: str) -> bool:
    return ("回到" in (utterance or "")) or ("拉回" in (utterance or ""))


def _stats(lens: list[int]) -> tuple[int, int]:
    if not lens:
        return 0, 0
    return int(statistics.median(lens)), max(lens)


def metrics(rows: list[dict]) -> dict:
    """rows: [{utterance, transcript}]。回傳與工單那張表同構的一組數字。"""
    us = [r for r in rows if (r.get("utterance") or "").strip()]
    lens = [len(r["utterance"]) for r in us]
    med, mx = _stats(lens)
    qc = [quote_check(r["utterance"], r["transcript"]) for r in us]
    return {
        "n_points": len(rows),
        "n_with_utterance": len(us),
        "n_has_quote": sum(1 for q in qc if q["has_quote"]),
        "n_grounded": sum(1 for q in qc if q["all_grounded"]),
        "n_near": sum(1 for q in qc if q["all_near"]),
        "n_ungrounded_quote": sum(1 for q in qc if q["has_quote"] and not q["all_near"]),
        "n_pullback": sum(1 for r in us if has_pullback(r["utterance"])),
        "median_chars": med, "max_chars": mx,
        "median_tts": round(med / CHARS_PER_SECOND, 1),
        "max_tts": round(mx / CHARS_PER_SECOND, 1),
    }


def print_metrics_table(title: str, named: list[tuple[str, dict]]) -> None:
    print("\n" + "=" * 118)
    print(title)
    print("=" * 118)
    print(f"{'變體':<22} {'點數':>4} {'有話術':>6} {'含引號':>6} {'全逐字':>6} "
          f"{'全近似':>6} {'拼湊':>5} {'回到/拉回':>9} {'中位字':>6} {'最長字':>6} {'中位秒':>6} {'最長秒':>6}")
    print("-" * 118)
    for name, m in named:
        print(f"{name:<22} {m['n_points']:>4} {m['n_with_utterance']:>6} {m['n_has_quote']:>6} "
              f"{m['n_grounded']:>6} {m['n_near']:>6} {m['n_ungrounded_quote']:>5} "
              f"{m['n_pullback']:>9} {m['median_chars']:>6} {m['max_chars']:>6} "
              f"{m['median_tts']:>6.1f} {m['max_tts']:>6.1f}")
    print(f"\n（秒數＝字數 ÷ {CHARS_PER_SECOND}，語速常數取自 src/meeting_host/replay.py。"
          f"\n 全逐字＝句中每一段「」都能在該時點逐字稿裡逐字找到；"
          f"全近似＝每一段「」的最長連續共同子字串都 ≥{NEAR_THRESHOLD:.0%}"
          f"（放過 STT 同音字／掉語助詞）；\n 拼湊＝有引號但至少一段連 {NEAR_THRESHOLD:.0%} 都對不上"
          f"，也就是把散在好幾句話裡的詞拼成一句引號。）")


# ── 主表：全部評分點跑一次新架構 ─────────────────────────────────────────

def run_full(replay: R.Replay, solved: list[dict], states: list, workers: int) -> dict:
    n = len(solved)
    judged: list[dict] = [None] * n  # type: ignore[list-item]
    phrased: list[dict] = [None] * n  # type: ignore[list-item]
    lock, done = threading.Lock(), 0

    def judge(i: int) -> None:
        nonlocal done
        judged[i] = call_judge(states[i], solved[i]["t_score"], replay.phase)
        with lock:
            done += 1
            print(f"  判斷 [{done:>3}/{n}] #{i + 1:>2} "
                  f"{judged[i].get('type', 'ERR')}/{judged[i].get('verdict', '')} "
                  f"{judged[i].get('latency_seconds')}s", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(judge, range(n)))

    done = 0

    def do_phrase(i: int) -> None:
        nonlocal done
        r = judged[i]
        phrased[i] = ({"utterance": "", "error": "judge failed"} if r.get("error")
                      else call_phrase(states[i], solved[i]["t_score"], r, replay.phase))
        with lock:
            done += 1
            print(f"  話術 [{done:>3}/{n}] #{i + 1:>2} "
                  f"{len(phrased[i].get('utterance') or ''):>3}字 "
                  f"{phrased[i].get('latency_seconds')}s", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(do_phrase, range(n)))

    points = []
    for i, s in enumerate(solved):
        r, p = judged[i], phrased[i]
        st, t = states[i], s["t_score"]
        merged = dict(r)
        merged["utterance"] = p.get("utterance", "")
        gate_ok, gate_reason = live.slow_gate(st, t, r) if not r.get("error") else (False, "判斷失敗")
        if gate_ok:
            ok2, reason2 = live.slow_recheck_admissible(st, t, merged)
        else:
            ok2, reason2 = False, gate_reason
        points.append({
            "index": i, "t_score": t, "emit_t": s["emit_t"], "n_utterances": s["n_utterances"],
            "judge": r, "phrase": p,
            "transcript": transcript_text(st),
            "gate_ok": gate_ok, "gate_reason": gate_reason,
            "admissible": ok2, "reason": reason2,
        })
    return {
        "source_events": None,
        "model": slow_path.MODEL, "judge_effort": slow_path.EFFORT,
        "utterance_effort": slow_path.UTTERANCE_EFFORT,
        "max_utterance_chars": slow_path.MAX_UTTERANCE_CHARS,
        "utterance_hard_cap": slow_path.UTTERANCE_HARD_CAP,
        "phase": replay.phase, "points": points,
    }


# ── 舊變體（variants.json）用同一組指標重算 ───────────────────────────────

def variants_rows(vblob: dict, states: list, name: str) -> list[dict]:
    rows = []
    for p in vblob["points"]:
        i = p["index"]
        r = p["results"].get(name, {})
        rows.append({"utterance": r.get("utterance") or "",
                     "transcript": transcript_text(states[i])})
    return rows


# ── 報表 ─────────────────────────────────────────────────────────────────

def report(blob: dict, states: list, vblob: dict | None, n_sentences: int) -> None:
    pts = blob["points"]
    all_rows = [{"utterance": p["phrase"].get("utterance") or "", "transcript": p["transcript"]}
                for p in pts]
    prod_rows = [r for r, p in zip(all_rows, pts) if p["gate_ok"]]

    named = []
    if vblob:
        for vn in vblob["variants"]:
            named.append((vn, metrics(variants_rows(vblob, states, vn))))
    named.append(("t29_split（全部點）", metrics(all_rows)))
    named.append(("t29_split（僅 production 會用的點）", metrics(prod_rows)))
    print_metrics_table("話術品質：新架構 vs 一次呼叫的四個變體（同一批評分點）", named)

    # 介入次數
    n_int = sum(1 for p in pts if p["judge"].get("is_intervention"))
    n_gate = sum(1 for p in pts if p["gate_ok"])
    n_adm = sum(1 for p in pts if p["admissible"])
    print("\n介入次數（同一批 {} 點）".format(len(pts)))
    if vblob:
        for vn in vblob["variants"]:
            c = sum(1 for p in vblob["points"] if p["results"].get(vn, {}).get("is_intervention"))
            print(f"  {vn:<22} 判定介入 {c}")
    print(f"  {'t29_split':<22} 判定介入 {n_int}／通過第一關閘門 {n_gate}"
          f"／通過話術後重驗 {n_adm}")

    # 閘門理由分佈
    reasons: dict[str, int] = {}
    for p in pts:
        if not p["admissible"]:
            reasons[p["reason"]] = reasons.get(p["reason"], 0) + 1
    print("\n被擋下的理由分佈：" + ("、".join(f"{k or '(判不需介入)'}×{v}"
                                        for k, v in sorted(reasons.items())) or "（無）"))

    # 延遲
    jl = [p["judge"]["latency_seconds"] for p in pts if p["judge"].get("latency_seconds")]
    pl = [p["phrase"]["latency_seconds"] for p in pts if p["phrase"].get("latency_seconds")]
    gl = [p["phrase"]["latency_seconds"] for p in pts
          if p["gate_ok"] and p["phrase"].get("latency_seconds")]

    def q(xs, f):
        return round(sorted(xs)[min(len(xs) - 1, int(len(xs) * f))], 2) if xs else 0.0

    print("\n延遲（秒；n＝樣本數）")
    for label, xs in (("判斷呼叫", jl), ("話術呼叫（全部點）", pl), ("話術呼叫（production 會用的點）", gl)):
        if xs:
            print(f"  {label:<28} n={len(xs):>3} 中位 {statistics.median(xs):.2f} "
                  f"p90 {q(xs, 0.9):.2f} 最長 {max(xs):.2f} 最短 {min(xs):.2f}")
    if jl and gl:
        tot = [a + b for a, b in zip(jl, gl)] if len(jl) == len(gl) else None
        med_total = statistics.median(jl) + statistics.median(gl)
        print(f"  {'評分時刻→排進 Chair（中位）':<28} 現況 {statistics.median(jl):.2f}s "
              f"→ 新架構 {med_total:.2f}s（+{statistics.median(gl):.2f}s）")
        del tot

    # 逐句原文
    print("\n" + "=" * 118)
    print(f"話術原文（前 {n_sentences} 個有話術的點；★＝production 會真的講出來）")
    print("=" * 118)
    shown = 0
    for p in pts:
        u = p["phrase"].get("utterance") or ""
        if not u:
            continue
        shown += 1
        if shown > n_sentences:
            break
        qc = quote_check(u, p["transcript"])
        mark = "★" if p["admissible"] else ("·" if p["gate_ok"] else " ")
        print(f"\n{mark} #{p['index'] + 1}  t={p['t_score']:.0f}s ({p['t_score'] / 60:.1f}分)  "
              f"[{p['judge'].get('type')}/{p['judge'].get('verdict')} "
              f"P{p['judge'].get('positive')}/N{p['judge'].get('negative')}/No{p['judge'].get('none')}]  "
              f"{len(u)}字＝{len(u) / CHARS_PER_SECOND:.1f}s  "
              f"引號{qc['n_quotes']}（可溯源{qc['n_grounded']}）  gate={p['reason'] or 'OK'}")
        print(f"   t29 ： {u}")
        if vblob:
            for vn in ("v0_baseline", "v2_only_here"):
                vr = vblob["points"][p["index"]]["results"].get(vn, {})
                vu = vr.get("utterance") or "（空）"
                print(f"   {vn[:3]}  ： {vu}")


# ── sweep：選 effort 與長度上限 ───────────────────────────────────────────

def _no_rule4_template() -> str:
    """把「不要用『我們先回到』」那條規則整條拿掉的模板（消融）。

    為什麼要有這個消融：主表的「回到/拉回」欄位是被 prompt 直接指示的，
    拿它當「拆呼叫有效」的證據會是自我實現。這條消融量的是拆呼叫本身
    （＝話術獨立於判斷）對那個開頭形狀有沒有影響。
    """
    tpl = slow_path.UTTERANCE_TEMPLATE
    marker = "4. 不要用"
    i = tpl.index(marker)
    j = tpl.index("5. 不要編造", i)
    return (tpl[:i] + tpl[j:]).replace("5. 不要編造", "4. 不要編造")


def run_sweep(replay: R.Replay, solved: list[dict], states: list,
              idxs: list[int], workers: int) -> dict:
    """在子集點上，先各判斷一次，再用多組設定各產一次話術。"""
    judged: dict[int, dict] = {}
    for i in idxs:
        judged[i] = call_judge(states[i], solved[i]["t_score"], replay.phase)
        print(f"  判斷 #{i + 1:>2} {judged[i].get('type')}/{judged[i].get('verdict')} "
              f"{judged[i].get('latency_seconds')}s", flush=True)

    configs = {
        "eff=none  max=50": {"effort": "none", "max_chars": 50},
        "eff=low   max=50": {"effort": "low", "max_chars": 50},
        "eff=medium max=50": {"effort": "medium", "max_chars": 50},
        "eff=none  max=40": {"effort": "none", "max_chars": 40},
        "eff=none  max=70": {"effort": "none", "max_chars": 70},
        "eff=none  max=50 無規則4": {"effort": "none", "max_chars": 50,
                                     "template": _no_rule4_template()},
    }
    out: dict[str, dict] = {c: {} for c in configs}
    jobs = [(c, i) for c in configs for i in idxs]
    lock, done = threading.Lock(), 0

    def work(job):
        nonlocal done
        c, i = job
        res = call_phrase(states[i], solved[i]["t_score"], judged[i], replay.phase, **configs[c])
        with lock:
            out[c][i] = res
            done += 1
            print(f"  [{done:>3}/{len(jobs)}] {c:<24} #{i + 1:>2} "
                  f"{len(res.get('utterance') or ''):>3}字 {res.get('latency_seconds')}s",
                  flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, jobs))

    return {"idxs": idxs, "judged": {str(k): v for k, v in judged.items()},
            "configs": {c: {k: v for k, v in cfg.items() if k != "template"}
                        for c, cfg in configs.items()},
            "results": {c: {str(i): out[c][i] for i in idxs} for c in configs},
            "transcripts": {str(i): transcript_text(states[i]) for i in idxs}}


def run_judge_rounds(replay: R.Replay, solved: list[dict], states: list,
                     rounds: int, workers: int) -> dict:
    """只跑判斷呼叫 N 輪，量「拿掉話術欄位之後介入次數有沒有真的變」。

    需要多輪的理由：`slow_path.EFFORT = "none"` 不是 temperature=0，同一個 prompt
    再問一次就可能得到不同的三軸分數。既有 5 輪基準（一次呼叫、prompt 裡還有
    utterance 那一行，experiments/out/rescore-*/stability.json）的 is_intervention
    是 6/12/8/7/12——單輪拿一個數字跟它比，比不出任何東西。
    """
    n = len(solved)
    out = []
    for k in range(rounds):
        res: list[dict] = [None] * n  # type: ignore[list-item]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda i: res.__setitem__(
                i, call_judge(states[i], solved[i]["t_score"], replay.phase)), range(n)))
        n_int = sum(1 for r in res if r.get("is_intervention"))
        n_gate = sum(1 for i, r in enumerate(res)
                     if not r.get("error") and live.slow_gate(states[i], solved[i]["t_score"], r)[0])
        print(f"  第 {k + 1} 輪：判定介入 {n_int}／通過第一關閘門 {n_gate}", flush=True)
        out.append({"round": k + 1, "is_intervention": n_int, "gate_ok": n_gate,
                    "types": _counter([r.get("type") for r in res]),
                    "points": res})
    return {"rounds": out, "n_points": n}


def _counter(xs: list) -> dict:
    c: dict = {}
    for x in xs:
        c[str(x)] = c.get(str(x), 0) + 1
    return c


def report_judge_rounds(blob: dict) -> None:
    print("\n" + "=" * 90)
    print("判斷呼叫（已拿掉 utterance 欄位）逐輪介入次數")
    print("=" * 90)
    for r in blob["rounds"]:
        print(f"  第 {r['round']} 輪：判定介入 {r['is_intervention']:>2}／"
              f"通過第一關閘門 {r['gate_ok']:>2}  type 分佈 {r['types']}")
    ints = [r["is_intervention"] for r in blob["rounds"]]
    gates = [r["gate_ok"] for r in blob["rounds"]]
    print(f"\n  判定介入 範圍 {min(ints)}-{max(ints)}（中位 {statistics.median(ints):.0f}）")
    print(f"  通過閘門 範圍 {min(gates)}-{max(gates)}（中位 {statistics.median(gates):.0f}）")
    print("\n  對照：一次呼叫（prompt 裡還有 utterance 那一行）的 5 輪基準"
          "\n  experiments/out/rescore-2026-08-29-two-person/stability.json："
          "\n    判定介入 6/12/8/7/12（範圍 6-12）、admissible 4/9/6/6/10（範圍 4-10）")


def report_sweep(blob: dict) -> None:
    idxs = blob["idxs"]
    named = []
    for c in blob["configs"]:
        rows = [{"utterance": blob["results"][c][str(i)].get("utterance") or "",
                 "transcript": blob["transcripts"][str(i)]} for i in idxs]
        named.append((c, metrics(rows)))
    print_metrics_table(f"話術設定 sweep（{len(idxs)} 個評分點 × {len(blob['configs'])} 組設定）",
                        named)
    print("\n往返延遲（秒）")
    for c in blob["configs"]:
        ls = [blob["results"][c][str(i)].get("latency_seconds") for i in idxs
              if blob["results"][c][str(i)].get("latency_seconds")]
        if ls:
            print(f"  {c:<24} n={len(ls):>2} 中位 {statistics.median(ls):.2f} "
                  f"最長 {max(ls):.2f} 最短 {min(ls):.2f}")
    print("\n逐句原文（sweep）")
    for i in idxs:
        print(f"\n── #{i + 1} " + "─" * 60)
        for c in blob["configs"]:
            u = blob["results"][c][str(i)].get("utterance") or "（空）"
            print(f"  {c:<24} {len(u):>3}字 | {u}")


# ── 主流程 ────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--judge-rounds", type=int, default=0,
                    help="只跑判斷呼叫 N 輪，量介入次數的輪間變異")
    ap.add_argument("--sweep-points", type=str, default="5,9,13,17,21,25,29,33",
                    help="1-based 點號，逗號分隔")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sentences", type=int, default=16)
    args = ap.parse_args(argv)

    raw = [json.loads(l) for l in args.events.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = sorted((Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw), key=lambda e: e.t)
    replay = R.Replay(events)
    slow_events = [e for e in events if e.kind == "slow_score"]
    src_dir = args.events.resolve().parent.name
    out_dir = args.out or (_HERE / "out" / f"two-call-{src_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / ("sweep.json" if args.sweep
                       else "judge_rounds.json" if args.judge_rounds
                       else "two_call.json")

    solved = R.solve_score_times(replay, slow_events, drift=R.tick_drift(events))
    states = [replay.state_at(s["t_score"]) for s in solved]

    vpath = _HERE / "out" / f"utterance-variants-{src_dir}" / "variants.json"
    vblob = json.loads(vpath.read_text(encoding="utf-8")) if vpath.is_file() else None
    if vblob and len(vblob["points"]) != len(solved):
        print(f"⚠️ variants.json 的點數（{len(vblob['points'])}）與這次重建（{len(solved)}）不同，"
              f"不並排比較。")
        vblob = None

    if not args.report_only:
        v = R.verify(replay, events, solved)
        hard = (v["fast_timer"]["silent_mismatch"] or v["fast_timer"]["participant_order_mismatch"]
                or v["share"]["mismatch"])
        print(f"重建對帳：fast_timer.silent {v['fast_timer']['checked']} 筆／"
              f"share {v['share']['checked']} 筆，"
              f"mismatch={v['fast_timer']['silent_mismatch']}/{v['share']['mismatch']}")
        if hard:
            print("重建與事件檔對不上，停。")
            return 2
        R.load_api_key()

    if args.sweep:
        if args.report_only:
            blob = json.loads(cache.read_text(encoding="utf-8"))
        else:
            idxs = [int(x) - 1 for x in args.sweep_points.split(",")]
            idxs = [i for i in idxs if 0 <= i < len(solved)]
            blob = run_sweep(replay, solved, states, idxs, args.workers)
            cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n原始輸出已寫入 {cache}")
        report_sweep(blob)
        return 0

    if args.judge_rounds:
        if args.report_only:
            blob = json.loads(cache.read_text(encoding="utf-8"))
        else:
            blob = run_judge_rounds(replay, solved, states, args.judge_rounds, args.workers)
            cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n原始輸出已寫入 {cache}")
        report_judge_rounds(blob)
        return 0

    if args.report_only:
        blob = json.loads(cache.read_text(encoding="utf-8"))
    else:
        print(f"\n{len(solved)} 點 × 2 次呼叫 = {len(solved) * 2} 次")
        blob = run_full(replay, solved, states, args.workers)
        blob["source_events"] = str(args.events)
        cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n原始輸出已寫入 {cache}")
    report(blob, states, vblob, args.sentences)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
