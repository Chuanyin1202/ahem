#!/usr/bin/env python3
"""介入評分探針：量延遲、比模型、調 prompt。

這是「逐字稿模擬測試」的雛形，也是驗證 #3 的本體。
不依賴 thoughtful-agents 套件——我們本來就要改造它的評分準則，
照抄整個框架沒有意義，真正要量的是「一次評分呼叫要多久、判斷準不準」。

設計依據：
- 三軸評分（Positive/Negative/None 三欄必填不互斥）← prior-art 發現 1c
- 四條 Key Rules ← To Facilitate or not to Facilitate 論文原文
- 正反論證再給分 ← Inner Thoughts §5.4
- 只看最近 N 則 ← 近因假設

用法:
    python experiments/scoring_probe.py                    # 8 場景 × 5 次，exit code ≠ 0 表示有不符
    python experiments/scoring_probe.py --scenario overtime --runs 5
    python experiments/scoring_probe.py --with-phase
"""
import argparse
import statistics
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from scenarios import SCENARIOS  # noqa: E402

from meeting_host import fast_path, replay, slow_path  # noqa: E402

# prompt、模型、判定規則全部直接用產品的（slow_path.py），探針不再另抄一份——
# 先前兩份手抄本連 JSON 的 type 選項都不一樣，測到的不是產品


def expected(sc: dict) -> bool:
    """expect 字串 → 該不該介入。只比對「該／不該」，不比類型：
    類型判斷由快路規則負責，LLM 在這一項實測不可靠（validation-results.md #3）。"""
    e = sc["expect"]
    if e.startswith("不該介入"):
        return False
    if e.startswith("該介入"):
        return True
    raise ValueError(f"expect 無法解析：{e!r}")


def call(sc: dict, with_phase: bool) -> tuple[dict | None, list, float, str]:
    """回傳 (慢路結果, 快路觸發, 耗時秒, 錯誤訊息)。

    量的是整個產品：快路規則 or 慢路評分，任一成立即為系統介入。
    只量慢路會把 overtime／silent 這類快路負責的場景錯記成 FN——
    慢路對它們回「無」正是設計（validation-results.md #3：類型由快路判定）。
    """
    st, utts = replay.load(sc)
    for u in utts:
        st.add(u)
    now = sc["elapsed"] * 60
    fast = fast_path.check(st, now)
    t0 = time.perf_counter()
    try:
        r = slow_path.score(st, now, sc.get("phase") if with_phase else None)
    except urllib.error.HTTPError as e:
        return None, fast, time.perf_counter() - t0, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, fast, time.perf_counter() - t0, f"{type(e).__name__}: {e}"
    return r, fast, time.perf_counter() - t0, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", nargs="*", default=None, help="只跑指定場景（可多個）")
    ap.add_argument("--runs", type=int, default=5, help="每場景跑幾次（evaluation.md 規定至少 5 次）")
    ap.add_argument("--with-phase", action="store_true",
                    help="把場景的 phase 欄位放進 prompt（目前只有 divergent_phase 有）")
    args = ap.parse_args()

    scenarios = {k: v for k, v in SCENARIOS.items()
                 if args.scenario is None or k in args.scenario}
    latencies: list[float] = []
    tp = fp = fn = tn = 0
    mismatches: list[str] = []

    print(f"模型 {slow_path.MODEL}，effort={slow_path.EFFORT}，"
          f"系統介入 = fast_path.check() 任一觸發 or slow_path.is_intervention()")
    for name, sc in scenarios.items():
        want = expected(sc)
        print(f"\n{'=' * 70}\n場景：{name} — {sc['note']}")
        print(f"預期：{sc['expect']}\n{'=' * 70}")
        for run in range(args.runs):
            result, fast, secs, err = call(sc, args.with_phase)
            latencies.append(secs)
            if err:
                print(f"  {secs:5.2f}s  ❌ {err}")
                mismatches.append(f"{name} 第{run + 1}次：呼叫失敗 {err}")
                continue
            slow_got = slow_path.is_intervention(result)
            got = bool(fast) or slow_got
            ok = got == want
            tp += got and want
            fp += got and not want
            fn += (not got) and want
            tn += (not got) and not want
            fast_tag = "快路:" + "+".join(t.kind for t in fast) if fast else "快路:無"
            if not ok:
                mismatches.append(f"{name} 第{run + 1}次：預期{'介入' if want else '不介入'}，"
                                  f"實際{'介入' if got else '不介入'}"
                                  f"（{fast_tag}；慢路 {result['verdict']} [{result.get('type')}]）")
            tag = f"(第{run + 1}次)" if args.runs > 1 else ""
            print(f"  {secs:5.2f}s  {'✅' if ok else '❌'} {fast_tag}  慢路 {result['verdict']}"
                  f"  P{result.get('positive')}/N{result.get('negative')}"
                  f"/None{result.get('none')}  [{result.get('type')}] {tag}")
            if result.get("utterance"):
                print(f"          「{result['utterance']}」")

    total = tp + fp + fn + tn
    print(f"\n{'=' * 70}\n結果（{len(scenarios)} 場景 × {args.runs} 次 = {total} 次）")
    print(f"  TP {tp}  FP {fp}  FN {fn}  TN {tn}   正確 {tp + tn}/{total}")
    if latencies:
        print(f"  延遲 中位 {statistics.median(latencies):.2f}s  "
              f"最快 {min(latencies):.2f}s  最慢 {max(latencies):.2f}s")
    if mismatches:
        print("\n不符：")
        for m in mismatches:
            print(f"  - {m}")
    print("=" * 70)
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
