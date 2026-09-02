#!/usr/bin/env python3
"""慢路重評：拿現行 prompt 重跑一場已錄會議的每一個慢路評分點，跟當時的結果對照。

回答的問題：「同樣這些評分點，換成現在的 `slow_path.py`，主席會出手幾次？」

做法三步，每一步都可以單獨驗：

1. **重建**（`Replay`）：從 `events.jsonl` 把每個 `slow_score` 事件當下的
   `MeetingState` 還原回來。重建的正確性不靠宣稱，靠兩條事件檔自帶的
   不變量逐筆對帳（`--verify`）：
   - `fast_timer.silent`：每秒一筆，等於 `st.silent_seconds(p, now)`。
     對得上 ⇒ 逐字稿的 end 時間、`speaking` 集合、participants 名單全部正確。
   - `share`：每次 commit／出聲一筆，等於 `live.Session.emit_share()` 的算式。
     對得上 ⇒ `st.spoke_seconds()` 與 `st.interventions` 正確。
   評分時刻 `t_score` 不在事件檔裡（`slow_score` 的 t 是 LLM 回來之後才 emit 的），
   由 `watch_slow` 的 5 秒 tick 網格反推，並用 `should_score()` 的閘門條件驗證唯一性
   ——見 `solve_score_times()`。

2. **重評**：鏡射 production 的**兩次呼叫**——`slow_path.score()` 判斷，過了
   `live.slow_gate()` 才 `slow_path.phrase()` 產話術（MODEL／EFFORT／prompt 全照
   現況），結果寫進 `rescored.json`。之後所有指標都從這個檔重算，不再呼叫 LLM。
   只打 `score()` 是不行的：拆呼叫之後它不回傳話術，閘門會一律判「無話術」。

3. **計分**：把新結果轉成 `events.jsonl` 形狀（`queued`＋`spoken`），交給
   `experiments/score_run.py` 算窗口命中——計分規則只有那一支，這裡不另寫。

用法：
    # 只驗重建，不呼叫 LLM
    python experiments/rescore_slow_path.py <events.jsonl> --labels <labels.json> --verify

    # 重評（會呼叫 LLM；已有 rescored.json 就直接沿用，加 --refresh 才重跑）
    python experiments/rescore_slow_path.py <events.jsonl> --labels <labels.json>

    # 只從快取重算指標
    python experiments/rescore_slow_path.py <events.jsonl> --labels <labels.json> --report-only

    # 多輪：同一批點重跑 5 輪量穩定度（原始輸出存 rescored.rounds.json，不動 rescored.json）
    python experiments/rescore_slow_path.py <events.jsonl> --labels <labels.json> --rounds 5

    # 從多輪快取重算任何指標，不再呼叫 LLM
    python experiments/rescore_slow_path.py <events.jsonl> --labels <labels.json> \
        --rounds 5 --report-only

單次結果只是一個抽樣。`slow_path.EFFORT = "none"` 不等於 temperature=0，同一個
prompt 再問一次仍可能得到不同的三軸分數；要拿命中／誤報當任何門檻的依據之前，
先用 `--rounds` 量它在輪與輪之間到底穩不穩——見下面「多輪重評」一節。
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

import score_run  # noqa: E402  同一個 experiments/ 目錄，計分唯一依據
from meeting_host import live, slow_path  # noqa: E402
from meeting_host.events import Event  # noqa: E402
from meeting_host.fast_path import COOLDOWN_SECONDS  # noqa: E402
from meeting_host.state import MeetingState, Utterance  # noqa: E402

TICK = 5.0  # live.Session.watch_slow 的評分節奏；反推 t_score 用
GREETING_KIND = "問候"


# ── .env ─────────────────────────────────────────────────────────────────


def load_api_key() -> None:
    """把 OPENAI_API_KEY 放進環境變數，讓 slow_path._api_key() 直接取得。

    往上找第一個 `.env`——git worktree 裡沒有 `.env`（不進版控），真正的檔案在
    主 repo 根目錄，`slow_path._api_key()` 的相對路徑在 worktree 下找不到。
    key 只進 os.environ，不落地、不列印。
    """
    if os.environ.get("OPENAI_API_KEY"):
        return
    for d in [_REPO, *_REPO.parents]:
        env = d / ".env"
        if not env.is_file():
            continue
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()
                return
    raise RuntimeError("找不到 .env 裡的 OPENAI_API_KEY")


# ── 重建 ─────────────────────────────────────────────────────────────────


@dataclass
class Join:
    name: str
    at: float
    exact: bool = False   # at 是從 fast_timer.silent 精確反推的，還是退回用事件自己的 t


class Replay:
    """把 `events.jsonl` 重播成任意時刻的 `MeetingState`。

    只重建 `slow_path.build_prompt()` ／ `live.slow_result_admissible()` 真正會讀的欄位：
    topic／duration_min／participants／joined_at／utterances／speaking／interventions。
    `voice_active`／`silence_since`／`absent`／`room_silence_hits` 是快路規則用的，
    這裡不重建——不重建的東西就不要假裝重建出來了。
    """

    def __init__(self, events: list[Event]):
        self.events = sorted(events, key=lambda e: e.t)
        meetings = [e for e in self.events if e.kind == "meeting"]
        if not meetings:
            raise ValueError("事件檔沒有 meeting 事件，重建不出 topic／duration／phase")
        self.topic = meetings[0].data["topic"]
        self.duration_min = meetings[0].data["duration_min"]
        self.phase = meetings[0].data["phase"]
        phases = {e.data.get("phase") for e in meetings}
        if len(phases) > 1:
            raise ValueError(f"這場會議中途換過 phase（{phases}），本工具只處理單一 phase")
        self.joins = self._solve_joins()

    def _solve_joins(self) -> list[Join]:
        """participants 何時進名單、joined_at 是多少——從 `fast_timer.silent` 反推。

        `_fast_tick` 每秒對 `st.participants` 產生一筆 `silent` dict，dict 的鍵序
        就是 `st.participants` 的順序（insertion order）。某人第一次出現在 silent
        裡、且當時他還沒有任何 utterance 時，`silent_seconds` 走的正是
        `now - joined_at` 這條路，所以 `joined_at = t - silent`。

        他第一次出現時已經講過話（silent 走 utterance end 那條路）就推不出
        joined_at——那種情況 joined_at 對 `silent_seconds` 也不再有任何影響
        （有 utterance 就永遠不會用到它），記成 None 即可。

        ⚠️ `meeting` 事件的 `participants` 只說明「這些人此刻已經在場」，不帶
        silent，反推不出 joined_at。所以它給的是**暫定值**（事件自己的 t），
        必須讓後面第一筆帶得出 silent 的 `fast_timer` 修正掉：真正的 joined_at
        比 `meeting` 的 t 早一點——session 登記參與者到 emit 之間的落差，
        2026-08-31 那場實測 0.7275 秒。

        這個誤差只出現在「會議開始時就已經在頻道裡的人」身上，而且在他第一次
        開口後就不再影響任何計算，所以很容易漏掉：2026-08-29 那場開場時頻道是
        空的（`participants=[]`），整份資料完全碰不到這條路徑，`--verify` 全綠。
        """
        first_utt: dict[str, float] = {}
        for e in self.events:
            if e.kind == "utterance":
                first_utt.setdefault(e.data["speaker"], e.t)

        seen: dict[str, Join] = {}
        for e in self.events:
            if e.kind == "fast_timer":
                names = list(e.data.get("silent", {}).keys())
                getter = lambda n: e.data["silent"][n]  # noqa: E731
            elif e.kind == "meeting":
                names = list(e.data.get("participants", []))
                getter = lambda n: None  # noqa: E731
            else:
                continue
            for n in names:
                sil = getter(n)
                fu = first_utt.get(n)
                # 能精確反推的條件：這筆事件帶得出 silent，且該人當時還沒開口過
                #（開口之後 silent_seconds 走的是「上一則發言結束」，反推不出 joined_at）
                exact = sil is not None and (fu is None or fu > e.t)
                if n in seen:
                    if seen[n].exact or not exact:
                        continue
                    seen[n] = Join(n, e.t - sil, True)
                    continue
                if exact:
                    seen[n] = Join(n, e.t - sil, True)
                else:
                    # 暫定值：`meeting` 事件不帶 silent，只知道「這個人此刻已經在場」。
                    # 真正的 joined_at 比這個時刻早一點（session 登記到 emit 之間的落差，
                    # 2026-08-31 那場實測 0.727s）。標成非精確，讓後面第一筆帶得出
                    # silent 的 fast_timer 修正它——不修的話這個誤差會一路留著，
                    # 而且只在「開場就已在場的人」身上出現，換一份資料就消失，很難察覺。
                    seen[n] = Join(n, e.t, False)
        return sorted(seen.values(), key=lambda j: j.at)

    def state_at(self, t: float) -> MeetingState:
        st = MeetingState(topic=self.topic, duration_min=self.duration_min, participants=[])
        for j in self.joins:
            if j.at <= t:
                st.participants.append(j.name)
                st.joined_at[j.name] = max(0.0, j.at)
        for e in self.events:
            if e.t > t:
                break
            d = e.data
            if e.kind == "utterance":
                st.stopped_speaking(d["speaker"])
                st.add(Utterance(d["speaker"], d["text"], d["start"], d["end"]))
                st.utterances.sort(key=lambda x: x.start)
            elif e.kind == "speaking":
                if d.get("active"):
                    st.speaking_now(d["speaker"], e.t)
                else:
                    st.stopped_speaking(d["speaker"])
            elif e.kind == "spoken":
                st.interventions.append(d.get("at", e.t))
        return st


# ── 評分時刻反推 ─────────────────────────────────────────────────────────


def emit_share_values(st: MeetingState) -> dict[str, float]:
    """`live.Session.emit_share()` 的算式（含主席估算秒數）。
    不能用 `state.share()`——分母不含主席，是別的呼叫端在用的。"""
    chair_seconds = len(st.interventions) * 3.0
    per = {p: st.spoke_seconds(p) for p in st.participants}
    total = sum(per.values()) + chair_seconds
    out = {p: (s / total if total else 0.0) for p, s in per.items()}
    out["主席"] = chair_seconds / total if total else 0.0
    return out


def tick_drift(events: list[Event]) -> float:
    """`asyncio.sleep()` 每秒實際多睡多久——從 `fast_timer`（名目 1 秒一筆）量。

    只有第一個評分點需要它：它的 tick 網格錨在 now=0，中間累積的排程誤差沒有任何
    已測量的錨點可以吸收；第二個點以後都錨在上一筆 `slow_score` 的實測 emit t，
    誤差不累積。

    實測 0.00079 s/s，在 t=150 累積約 0.12 秒——本來完全可以忽略，但 prompt 裡的
    `目前進行到第 {elapsed:.0f} 分鐘` 剛好在 150.0 秒（＝2.5 分）踩到 Python
    round-half-even 的分界：150.00 印「第 2 分鐘」，150.12 印「第 3 分鐘」，
    而當時記錄的 cons 寫的是「會議才進行 3 分鐘」。不補這個漂移，第一個點餵給
    模型的 prompt 就跟當時差一個字。
    """
    fts = [e for e in events if e.kind == "fast_timer"]
    if len(fts) < 2:
        return 0.0
    return (fts[-1].t - fts[0].t) / (len(fts) - 1) - 1.0


def solve_score_times(replay: Replay, slow_events: list[Event], max_latency: float = 20.0,
                      drift: float = 0.0) -> list[dict]:
    """反推每個 `slow_score` 事件對應的評分時刻 `t_score`。

    `slow_score` 的 t 是 LLM 回來之後才 emit 的，比真正的評分時刻晚了一個
    LLM 往返。`watch_slow` 的結構是「sleep(TICK) → 評分 → emit → sleep(TICK) → …」，
    所以第 i 次評分的時刻一定落在

        t_score(i) = t_emit(i-1) + TICK * k     （k ≥ 1 的整數）

    這條網格上（中間被跳過的 k-1 個 tick 是 `should_score()` 擋掉的）。k 用兩個
    條件挑：`should_score()` 在 t_score(i) 必須為 True，且被跳過的每個 tick 必須為
    False。剩下不只一個候選時，取延遲最接近全體中位數的那個，並把候選數記在
    `n_candidates` 裡——`--verify` 會把它印出來，不唯一的點看得到。

    第一個點沒有前一次 emit 可以接，改用「session 起點 + TICK * k」的網格。
    """
    def grid(prev_emit: float, k: int) -> float:
        t = prev_emit + TICK * k
        return t * (1.0 + drift) if prev_emit == 0.0 else t  # 只有第一個點吃累積漂移

    out: list[dict] = []
    prev_emit = 0.0        # 第一個點：從 session 起點（now=0）起算的 tick 網格
    prev_n = 0             # last_n
    for e in slow_events:
        cands = []
        k = 1
        while True:
            t_s = grid(prev_emit, k)
            if t_s >= e.t:
                break
            lat = e.t - t_s
            if lat <= max_latency:
                st = replay.state_at(t_s)
                if slow_path.should_score(st, t_s, prev_n):
                    cands.append({"k": k, "t_score": t_s, "latency": lat,
                                  "n_utt": len(st.utterances)})
            k += 1
        out.append({"emit_t": e.t, "candidates": cands, "prev_n": prev_n})
        prev_emit = e.t
        # last_n 推進到「評分當下的 utterance 數」；候選還沒定案，先放最後一個候選，
        # 定案後（下面第二輪）再修正。
        prev_n = cands[-1]["n_utt"] if cands else prev_n

    # 第二輪：有了全體延遲樣本才挑得出中位數，重跑一次讓 last_n 也對齊定案值
    lat_pool = [c["latency"] for r in out for c in r["candidates"]]
    med = sorted(lat_pool)[len(lat_pool) // 2] if lat_pool else 3.0

    solved: list[dict] = []
    prev_emit, prev_n = 0.0, 0
    for e in slow_events:
        cands = []
        k = 1
        while True:
            t_s = grid(prev_emit, k)
            if t_s >= e.t:
                break
            lat = e.t - t_s
            if lat <= max_latency:
                st = replay.state_at(t_s)
                if slow_path.should_score(st, t_s, prev_n):
                    cands.append({"k": k, "t_score": t_s, "latency": lat,
                                  "n_utt": len(st.utterances)})
            k += 1
        if not cands:
            raise RuntimeError(
                f"slow_score@{e.t:.2f} 反推不出評分時刻：5 秒網格上沒有任何一個點"
                f"能通過 should_score()。重建與事件檔不一致，不要用猜的補。")
        pick = min(cands, key=lambda c: abs(c["latency"] - med))
        solved.append({
            "emit_t": e.t, "t_score": pick["t_score"], "latency": pick["latency"],
            "n_utterances": pick["n_utt"], "n_candidates": len(cands),
            "candidates_t": [round(c["t_score"], 2) for c in cands],
        })
        prev_emit, prev_n = e.t, pick["n_utt"]
    return solved


# ── 重建自我驗證 ─────────────────────────────────────────────────────────


def verify(replay: Replay, events: list[Event], solved: list[dict]) -> dict:
    """三組對帳。任何一組對不上，後面的重評結論都不成立。"""
    report: dict = {}

    # 1) fast_timer.silent / remaining：每秒一筆
    # 容差 1e-3：`_fast_tick` 先讀一次 `self.now` 算 silent／remaining，`emit()` 又讀
    # 一次當事件的 t，兩次 perf_counter 之間差了幾微秒——重建只拿得到後面那個 t，
    # 這個微秒級落差不是重建誤差。
    fts = [e for e in events if e.kind == "fast_timer"]
    bad_sil, bad_rem, bad_order = [], [], []
    max_sil_diff = max_rem_diff = 0.0
    for e in fts:
        st = replay.state_at(e.t)
        got = {p: st.silent_seconds(p, e.t) for p in st.participants}
        want = e.data["silent"]
        if list(got) != list(want):
            bad_order.append(e.t)
        for p, v in want.items():
            d = abs(got[p] - v) if p in got else float("inf")
            max_sil_diff = max(max_sil_diff, d)
            if d > 1e-3:
                bad_sil.append({"t": e.t, "who": p, "want": v, "got": got.get(p)})
        d = abs(st.remaining_seconds(e.t) - e.data["remaining"])
        max_rem_diff = max(max_rem_diff, d)
        if d > 1e-3:
            bad_rem.append(e.t)
    report["fast_timer"] = {
        "checked": len(fts), "silent_mismatch": len(bad_sil),
        "participant_order_mismatch": len(bad_order),
        "remaining_mismatch": len(bad_rem),
        "max_silent_diff": max_sil_diff, "max_remaining_diff": max_rem_diff,
        "worst": sorted(bad_sil, key=lambda r: -abs((r["got"] or 0) - r["want"]))[:5],
    }
    # 每秒 tick 的實際漂移——第一個評分點的網格錨在 now=0，會吃到這段累積誤差
    if len(fts) > 1:
        span = fts[-1].t - fts[0].t
        report["fast_timer"]["tick_drift_per_second"] = round(
            (span - (len(fts) - 1)) / (len(fts) - 1), 6)

    # 2) share：每次 commit／出聲一筆
    shs = [e for e in events if e.kind == "share"]
    bad_share = []
    for e in shs:
        st = replay.state_at(e.t)
        got = emit_share_values(st)
        for p, v in e.data.items():
            if abs(got.get(p, -1) - v) > 1e-6:
                bad_share.append({"t": e.t, "who": p, "want": v, "got": got.get(p)})
    report["share"] = {"checked": len(shs), "mismatch": len(bad_share), "worst": bad_share[:5]}

    # 3) 評分時刻的唯一性與敏感度：t_score 與 emit_t 兩端重建的 prompt 是否相同
    amb = [s for s in solved if s["n_candidates"] > 1]
    diff_prompt = []
    for s in solved:
        a = slow_path.build_prompt(replay.state_at(s["t_score"]), s["t_score"], replay.phase)
        b = slow_path.build_prompt(replay.state_at(s["emit_t"]), s["emit_t"], replay.phase)
        if a != b:
            sa = replay.state_at(s["t_score"])
            sb = replay.state_at(s["emit_t"])
            diff_prompt.append({
                "t_score": round(s["t_score"], 2), "emit_t": round(s["emit_t"], 2),
                "transcript_same": [u.text for u in sa.recent()] == [u.text for u in sb.recent()],
            })
    report["score_time"] = {
        "points": len(solved),
        "latency_min": round(min(s["latency"] for s in solved), 2),
        "latency_max": round(max(s["latency"] for s in solved), 2),
        "ambiguous_points": [{"emit_t": round(s["emit_t"], 2), "candidates": s["candidates_t"]} for s in amb],
        "prompt_differs_at_emit_t": len(diff_prompt),
        "of_which_transcript_still_same": sum(1 for d in diff_prompt if d["transcript_same"]),
        "detail": diff_prompt[:10],
    }
    return report


# ── 重評 ─────────────────────────────────────────────────────────────────


def _rephrase(replay: Replay, s: dict, r: dict, *, retries: int,
              sleep_seconds: float) -> tuple[str | None, float | None, str | None]:
    """第二次呼叫：第一關放行才產話術。回傳 (話術, 話術往返秒數, 錯誤)。

    第一關擋下時回 `(None, None, None)`——話術**沒有被嘗試過**，跟「嘗試了但模型
    沒寫出來」（`("", 秒數, None)`）是兩件事。`recompute_gates()` 靠這個差別
    分辨「本來就不該有話術」與「快取跟現行閘門定義對不上」，不可以合併成空字串。

    話術呼叫失敗不退回罐頭句（`live.Session._run_slow_score` 的同一個決定），
    記成空字串＋錯誤訊息，讓第二關判「話術失敗」。
    """
    gate_t = s["emit_t"]           # 舊事件檔的 emit 時刻 ≈ score() 回來的那一刻
    gate_st = replay.state_at(gate_t)
    ok, _reason = live.slow_gate(gate_st, gate_t, r)
    if not ok:
        return None, None, None
    err: str | None = None
    text = ""
    t0 = time.monotonic()
    for attempt in range(retries):
        try:
            text = slow_path.phrase(gate_st, gate_t, r, replay.phase)
            err = None
            break
        except Exception as exc:  # noqa: BLE001  單點失敗不能讓整份跑掉
            err = f"{type(exc).__name__}: {exc}"
            text = ""
            if attempt < retries - 1:
                time.sleep(sleep_seconds * (attempt + 1))
    seconds = round(time.monotonic() - t0, 2)
    return text, seconds, err


def rescore(replay: Replay, slow_events: list[Event], solved: list[dict],
            *, retries: int = 3, sleep_seconds: float = 2.0) -> list[dict]:
    """對每個評分點重跑一次現行慢路，**兩次呼叫都跑**。

    T29 拆呼叫之後 `slow_path.score()` 不再回傳話術，production 走的是
    `slow_gate()` → `phrase()` → `slow_recheck_admissible()`（見
    `live.Session._run_slow_score`）。這裡照同一條路跑，否則量到的是一條
    已經不存在的管線——只打 `score()` 會讓每個點的話術都是 None，閘門
    一律回「無話術」，介入數假性歸零。

    時刻的取法跟 `recompute_gates()` 一致：第一關用 `emit_t`（舊事件檔的 emit
    時刻 ≈ `score()` 回來的那一刻），話術呼叫餵的也是那一刻的 state——production
    的 `phrase()` 拿到的是最新逐字稿，不是評分當下的。

    `phrase_seconds` 是這支工具自己量到的話術往返，寫進快取供
    `recompute_gates()` 定位第二關的時刻。三種組合各自有意義，不可混為一談：

        phrase_seconds is None，utterance is None   第一關就擋下，話術**未嘗試**
        phrase_seconds 有值，utterance == ""        話術跑了但沒寫出來／呼叫失敗
        phrase_seconds 有值，utterance 有內容        正常
    """
    load_api_key()
    points = []
    for i, (old, s) in enumerate(zip(slow_events, solved)):
        st = replay.state_at(s["t_score"])
        new: dict | None = None
        err: str | None = None
        for attempt in range(retries):
            try:
                new = slow_path.score(st, s["t_score"], replay.phase)
                err = None
                break
            except Exception as exc:  # noqa: BLE001  單點失敗不能讓整份跑掉
                err = f"{type(exc).__name__}: {exc}"
                if attempt < retries - 1:
                    time.sleep(sleep_seconds * (attempt + 1))
        rec = {
            "index": i,
            "t_score": s["t_score"], "emit_t": s["emit_t"], "latency": s["latency"],
            "n_utterances": s["n_utterances"], "n_candidates": s["n_candidates"],
            "phase": replay.phase,
            "old": {k: old.data.get(k) for k in
                    ("positive", "negative", "none", "type", "verdict", "utterance",
                     "admissible", "reason")},
            "new": None, "error": err,
        }
        if new is not None:
            utterance, phrase_seconds, phrase_err = _rephrase(
                replay, s, new, retries=retries, sleep_seconds=sleep_seconds)
            rec["new"] = {
                "positive": new.get("positive"), "negative": new.get("negative"),
                "none": new.get("none"), "type": new.get("type"),
                "verdict": new.get("verdict"), "utterance": utterance,
                "pros": new.get("pros", []), "cons": new.get("cons", []),
                "is_intervention": slow_path.is_intervention(new),
                "phrase_seconds": phrase_seconds, "phrase_error": phrase_err,
            }
        status = "ERR" if new is None else (
            "介入" if rec["new"]["is_intervention"] else rec["new"]["verdict"])
        print(f"  [{i + 1:>2}/{len(slow_events)}] t={s['t_score']:7.1f}s  "
              f"舊 {old.data.get('verdict')}/{old.data.get('type')}  →  "
              f"新 {(rec['new'] or {}).get('verdict')}/{(rec['new'] or {}).get('type')}  {status}")
        points.append(rec)
    return points


class StaleCacheError(RuntimeError):
    """快取的形狀跟現行閘門定義對不上，重算會得到假數字——停下來，不要猜。"""


def _gates(replay: Replay, n: dict, gate_t: float) -> tuple[bool, str]:
    """在 `gate_t` 這一刻跑完 production 的兩段閘門。回傳 (可送, 原因)。

    對應 `live.Session._run_slow_score`：第一關 `slow_gate()` 在 `score()` 回來
    時判，通過才打話術呼叫，話術回來後 `slow_recheck_admissible()` 再判一次
    （TOCTOU）。第二關的時刻＝`gate_t + phrase_seconds`。

    `deaf` 兩關都不傳（維持預設 False）：事件檔重建不含 STT 存活狀態，
    `Replay` 也沒重建它——沒重建的東西不假裝重建出來了。
    """
    r = {"type": n["type"], "verdict": n["verdict"],
         "positive": n.get("positive"), "negative": n.get("negative"),
         "none": n.get("none")}
    ok, reason = live.slow_gate(replay.state_at(gate_t), gate_t, r)
    if not ok:
        return ok, reason
    secs = n.get("phrase_seconds")
    if secs is None:
        # 第一關放行，但快取裡沒有話術往返紀錄 ⇒ 這份快取是「只打 score()」的
        # 舊工具產的（或閘門定義變寬了，當初沒打話術的點現在放行）。兩種情況都
        # 不能拿空話術去判第二關——那會回「話術失敗」，跟拆呼叫前那個 bug 一模一樣。
        raise StaleCacheError(
            f"第一關放行但快取沒有 phrase_seconds（index={n.get('_index', '?')}）："
            f"這份快取不是兩次呼叫版本產生的，或閘門定義已改變。"
            f"請加 --refresh 重跑，不要用它算任何指標。")
    r["utterance"] = n["utterance"] or ""
    recheck_t = gate_t + secs
    return live.slow_recheck_admissible(replay.state_at(recheck_t), recheck_t, r)


def recompute_gates(replay: Replay, points: list[dict]) -> None:
    """套 production 的兩段閘門（`slow_gate` → 話術 → `slow_recheck_admissible`），
    就地寫回 `points`。

    ⚠️ 時間點取 `emit_t`，不是 `t_score`。`live._run_slow_score` 是

        r = await asyncio.to_thread(score, self.st, self.now, self.phase)   # ← 評分時刻
        admissible, reason = slow_gate(self.st, self.now, r)                # ← await 回來才讀的另一個 now

    兩個 `self.now` 差了一整個 LLM 往返（本場 2.5–4.9 秒）。差別只影響冷卻判斷，
    但這場正好會踩到：有三個新結果的 `emit_t` 落在快路剛出聲後 1–3 秒內，
    用 `t_score` 判會過、用 `emit_t` 判會被冷卻擋掉。兩個值都記下來，
    `admissible` 以忠於程式的 `emit_t` 為準。

    ⚠️ 舊事件檔（拆呼叫之前錄的）的 `emit_t` 是「一次呼叫、判完就 emit」的時刻，
    所以拿它當第一關的時刻是對的；第二關再往後推 `phrase_seconds`。拆呼叫之後
    錄的事件檔 `emit_t` 是話術之後才 emit，兩者差一個話術往返——這個對應關係是
    推導不是量測，換資料集時要重新確認。

    ⚠️ `admissible_at_score_time` 只跑**第一關**，不是完整的兩段閘門。拆呼叫之後
    這是唯一誠實的做法：話術是在 `emit_t` 那一刻、用那一刻的 state 產生的，
    「如果在 t_score 判定」這個反事實裡根本沒有取樣過話術。硬把 emit_t 產的話術
    搬到 t_score 去判第二關是編造；對第一關就被擋下的點更沒有話術可搬。
    這個欄位的用途本來就是看冷卻／收尾在兩個時刻的差別——那全部發生在第一關。

    這一步不呼叫 LLM，所以 `--report-only` 也會重新算一次——快取只存模型輸出。
    """
    for p in points:
        n = p.get("new")
        if not n:
            continue
        n["_index"] = p.get("index")
        adm, reason = _gates(replay, n, p["emit_t"])
        n.pop("_index", None)
        # 反事實只跑第一關，理由見下面 `admissible_at_score_time` 那段
        r1 = {"type": n["type"], "verdict": n["verdict"],
              "positive": n.get("positive"), "negative": n.get("negative"),
              "none": n.get("none")}
        adm_s, reason_s = live.slow_gate(
            replay.state_at(p["t_score"]), p["t_score"], r1)
        n["admissible"], n["reason"] = adm, reason
        n["admissible_at_score_time"], n["reason_at_score_time"] = adm_s, reason_s


# ── 反事實事件檔（餵給 score_run.py）─────────────────────────────────────


def _ev(kind: str, t: float, data: dict) -> dict:
    return {"kind": kind, "t": t, "data": data}


def build_counterfactuals(raw: list[dict], points: list[dict]) -> dict[str, list[dict]]:
    """把重評結果轉成 `events.jsonl` 的形狀，交給 score_run.py 計分。

    計分規則不在這裡——`score_run.extract_interventions()` 只認 `spoken`，
    所以每個「會出手」的點補一組 `queued`＋`spoken`（同一個 t：新結果是
    hard=False 的軟插入，真實 Chair 會等停頓、最多 15 秒才升級硬打斷，
    這裡不模擬那段等待，見報告的限制一節）。

    三個變體：
      as_recorded        原始事件檔，一個字都沒動（基準）
      slow_prompt_only   只換 prompt：原始事件 ＋ 用實際歷史（含 11 次快路誤報
                         造成的冷卻）判定 admissible 的新慢路介入
      slow_only_t15fixed 假設 T15 已修（11 次「發言超時」誤報不存在）：移掉那批
                         fast 介入，慢路改用只含問候＋慢路自己的 30 秒冷卻鏈
    """
    fast_kinds = {"發言超時", "有人被冷落", "議程超時", "全場沉默"}

    def slow_ev(p: dict, t: float) -> list[dict]:
        n = p["new"]
        d = {"kind": n["type"].strip(), "target": None, "text": n["utterance"], "hard": False}
        return [_ev("queued", t, dict(d)), _ev("spoken", t, {**d, "at": t})]

    # A：原封不動
    variants = {"as_recorded": [dict(e) for e in raw]}

    terminal = ("queued", "spoken", "failed", "dropped")

    def is_slow_terminal(e: dict) -> bool:
        """原始事件檔裡慢路自己產生的介入事件——換 prompt 時必須拿掉。

        不拿掉就是「原始慢路 ＋ 新慢路」疊加，不是替換：2026-08-31 那場原始
        慢路出聲 5 次，疊上新 prompt 的 9 次後 slow=14，O3 的「命中」其實是
        原始那次。8/29 那場原始慢路整場 0 次，所以這個疊加從來沒露餡。
        """
        return (e["kind"] in terminal
                and e["data"].get("kind") not in fast_kinds
                and e["data"].get("kind") != GREETING_KIND)

    # B：只換 prompt——原始慢路介入拿掉，換成新 prompt 的
    b = [dict(e) for e in raw if not is_slow_terminal(e)]
    for p in points:
        if p["new"] and p["new"]["admissible"]:
            b.extend(slow_ev(p, p["emit_t"]))
    variants["slow_prompt_only"] = sorted(b, key=lambda e: e["t"])

    # C：T15 已修 → 快路誤報不存在，冷卻鏈重算；原始慢路同樣拿掉
    c = [dict(e) for e in raw
         if not (e["kind"] in terminal and e["data"].get("kind") in fast_kinds)
         and not is_slow_terminal(e)]
    chain = [e["data"]["at"] for e in raw
             if e["kind"] == "spoken" and e["data"].get("kind") == GREETING_KIND]
    for p in points:
        n = p["new"]
        if not n or not n["is_intervention"] or not n["utterance"]:
            continue
        t = p["emit_t"]
        if chain and t - chain[-1] < COOLDOWN_SECONDS:
            continue
        chain.append(t)
        c.extend(slow_ev(p, t))
    variants["slow_only_t15fixed"] = sorted(c, key=lambda e: e["t"])
    return variants


# ── 報表 ─────────────────────────────────────────────────────────────────


def print_table(points: list[dict]) -> None:
    print("\n" + "=" * 100)
    print("逐點對照（舊＝2026-08-29 錄的當下，新＝現行 slow_path）")
    print("=" * 100)
    hdr = f"{'#':>2} {'t_score':>8}  {'舊 P/N/None':<12} {'舊type':<6} {'舊verdict':<9} | " \
          f"{'新 P/N/None':<12} {'新type':<6} {'新verdict':<9} {'adm':<4} 翻轉"
    print(hdr)
    print("-" * 100)
    for p in points:
        o, n = p["old"], p["new"]
        os_ = f"P{o['positive']}/N{o['negative']}/No{o['none']}"
        if n is None:
            print(f"{p['index'] + 1:>2} {p['t_score']:8.1f}  {os_:<12} {o['type'] or '':<6} "
                  f"{o['verdict'] or '':<9} | 呼叫失敗：{p['error']}")
            continue
        ns = f"P{n['positive']}/N{n['negative']}/No{n['none']}"
        flip = ""
        if o["verdict"] != n["verdict"]:
            flip += "verdict "
        if o["type"] != n["type"]:
            flip += "type "
        if bool(o["admissible"]) != bool(n["admissible"]):
            flip += "ADMISSIBLE"
        print(f"{p['index'] + 1:>2} {p['t_score']:8.1f}  {os_:<12} {(o['type'] or ''):<6} "
              f"{(o['verdict'] or ''):<9} | {ns:<12} {(n['type'] or ''):<6} "
              f"{(n['verdict'] or ''):<9} {'YES' if n['admissible'] else '-':<4} {flip}")


def print_summary(points: list[dict], labels: dict | None) -> None:
    ok = [p for p in points if p["new"]]
    print("\n" + "=" * 100)
    print("總計")
    print("=" * 100)
    print(f"  評分點：{len(points)}（成功重評 {len(ok)}，失敗 {len(points) - len(ok)}）")
    print(f"  舊 admissible：{sum(1 for p in points if p['old']['admissible'])}"
          f"    新 admissible：{sum(1 for p in ok if p['new']['admissible'])}")
    print(f"  舊 is_intervention：{sum(1 for p in points if p['old']['verdict'] != '不介入' and p['old']['type'] not in ('無', '', None))}"
          f"    新 is_intervention：{sum(1 for p in ok if p['new']['is_intervention'])}")
    blocked = [p for p in ok if p["new"]["is_intervention"] and not p["new"]["admissible"]]
    for p in blocked:
        print(f"    - t={p['emit_t']:.1f}s 被兩段閘門擋下：{p['new']['reason']}"
              f"（同一點若在 t_score={p['t_score']:.1f}s 判定，第一關為 "
              f"{'放行' if p['new']['admissible_at_score_time'] else p['new']['reason_at_score_time']}）")
    for label, key in (("verdict", "verdict"), ("type", "type")):
        olds: dict = {}
        news: dict = {}
        for p in ok:
            olds[p["old"][key]] = olds.get(p["old"][key], 0) + 1
            news[p["new"][key]] = news.get(p["new"][key], 0) + 1
        print(f"  舊 {label} 分佈：{olds}")
        print(f"  新 {label} 分佈：{news}")
    if labels:
        print("\n  機會窗格逐格（新結果，未套任何冷卻）：")
        for w in labels.get("windows", []):
            lo, hi = w["range_seconds"]
            inside = [p for p in ok if lo <= p["emit_t"] <= hi]
            fired = [p for p in inside if p["new"]["is_intervention"]]
            adm = [p for p in inside if p["new"]["admissible"]]
            print(f"    [{w['id']:>3}] {w['kind']:<15} {w['range_seconds']} "
                  f"expect={w.get('expect_type')!r} scored={w.get('scored', True)}  "
                  f"評分點 {len(inside)}，is_intervention {len(fired)}，admissible {len(adm)}"
                  f"{'，type=' + ','.join(sorted({p['new']['type'] for p in fired})) if fired else ''}")


def run_scorer(variants: dict[str, list[dict]], labels_path: Path, out_dir: Path,
               *, quiet: bool = False) -> dict:
    """每個變體寫成 events.jsonl，交給 score_run.build_report()。"""
    labels = score_run.load_labels(labels_path)
    summary = {}
    for name, evs in variants.items():
        path = out_dir / f"counterfactual.{name}.events.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        report = score_run.build_report(score_run.load_events(path), labels, path, labels_path)
        (out_dir / f"score.{name}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        summary[name] = report
        if quiet:
            continue
        c = report["intervention_counts"]
        m = report["metrics"]
        print(f"\n--- score_run：{name} ---")
        print(f"  介入 total={c['total_interventions_excl_greeting']} fast={c['fast']} slow={c['slow']}"
              f"  TP={c['tp']} FP(窗內)={c['fp_in_window']} FP(窗外)={c['fp_outside_windows']}"
              f"  排除={c['excluded_scored_false']}")
        for k in ("overall", "slow", "fast"):
            r = m[k]["opportunity_recall"]
            v = f"{r['value']}（{r.get('hits')}/{r.get('total')}）" if r.get("value") is not None else r["reason"]
            print(f"  opportunity_recall[{k}] = {v}")
        for wid, wr in report["windows"]["detail"].items():
            hit = wr["hit"]
            print(f"    窗格 {wid:>3} {wr['kind']:<15} 命中={'t=%.1f kind=%s' % (hit['t'], hit['kind']) if hit else '無'}"
                  f"  FP={len(wr['fp_events'])}")
    return summary


# ── 多輪重評（穩定度量測）───────────────────────────────────────────────
#
# 單次重評回答的是「這一次模型怎麼判」。EFFORT="none" 不等於 temperature=0，
# 同一個 prompt 再問一次仍可能得到不同的三軸分數，所以任何從單次結果算出來的
# 命中／誤報都只是一個抽樣，不是統計結論。這一段把同一批評分點重跑 N 輪，
# 把「判準的邊界在哪裡」變成可以看的數字：
#   - 逐點：哪些點 N 輪一致（判準說得清楚），哪些點在翻（判準的真正邊界）
#   - 總量：每一輪的介入數／可送數，列出每一個值而不是平均——N=5 講平均沒意義
#   - 窗格：每一輪都跑一次 score_run.py（規則唯一依據），看「命中 2/2」出現幾次
#   - 離散：三軸分數在輪間的變動範圍，以及有多少點卡在 decide() 的一分之差上
#
# 原始輸出全部落在 rescored.rounds.json，換指標不必重新呼叫 LLM（--report-only）。

ROUNDS_CACHE = "rescored.rounds.json"

# 快取裡的管線標記。T29 把慢路拆成「判斷」「話術」兩次呼叫，閘門也從單一
# `slow_result_admissible()` 變成 `slow_gate()` ＋ `slow_recheck_admissible()`。
# 拆之前產的快取沒有 `phrase_seconds`，拿現行閘門去重算會得到假數字，
# 所以在檔案裡留下標記，讀的時候擋掉——不是相容，是拒絕。
PIPELINE = "two-call"


def require_pipeline(blob: dict, cache: Path) -> None:
    """快取不是兩次呼叫版本產生的就停下來。"""
    got = blob.get("pipeline")
    if got == PIPELINE:
        return
    raise StaleCacheError(
        f"{cache} 的 pipeline={got!r}，不是 {PIPELINE!r}。\n"
        f"這份快取是慢路拆成兩次呼叫（T29）之前產生的：裡面的 utterance 來自舊版\n"
        f"單次 score()，沒有 phrase_seconds，套現行的兩段閘門會算出假數字。\n"
        f"請加 --refresh 重跑（會呼叫 LLM），或換一個輸出目錄。")


def margin_of(n: dict) -> int | None:
    """`decide()` 的判定餘裕：max(positive, negative) - none。

    `decide()` 的規則是 `max(p, n) <= none → 不介入`，所以：
      margin <= 0 → 不介入；margin >= 1 → 介入。
    margin ∈ {0, 1} 就是「一分之差」——任一軸動一分，介入與否就翻面。
    """
    p, ng, no = n.get("positive"), n.get("negative"), n.get("none")
    if p is None or ng is None or no is None:
        return None
    return max(p, ng) - no


def merge_points(meta: list[dict], round_points: list[dict]) -> list[dict]:
    """把「不隨輪次改變的欄位（meta）」＋「某一輪的模型輸出」拼回單次模式的 point 形狀。

    拼出來的 dict 直接餵給既有的 `recompute_gates()`／`build_counterfactuals()`，
    多輪不另寫一份計分或閘門邏輯。回傳的 `new` 是 round_points 裡的同一個物件
    （不 copy），所以 `recompute_gates()` 寫回的 admissible 會留在快取結構裡。
    """
    by_index = {r["index"]: r for r in round_points}
    out = []
    for m in meta:
        r = by_index.get(m["index"], {"new": None, "error": "缺這一輪的紀錄"})
        out.append({**m, "new": r.get("new"), "error": r.get("error")})
    return out


def point_meta(points: list[dict]) -> list[dict]:
    keys = ("index", "t_score", "emit_t", "latency", "n_utterances",
            "n_candidates", "phase", "old")
    return [{k: p[k] for k in keys if k in p} for p in points]


def collect_rounds(replay: Replay, slow_events: list[Event], solved: list[dict],
                   *, rounds: int, cache: Path, retries: int,
                   report_only: bool, refresh: bool) -> dict:
    """跑到累計 N 輪為止，每一輪的原始輸出即時寫檔。

    - 已有快取且沒下 `--refresh`：只補不足的輪次（第 6 輪不必為了第 5 輪重跑）
    - 每跑完一輪就寫一次檔：中途失敗／中斷，已經花掉的呼叫不會白費
    - `--report-only`：一律不呼叫 LLM，快取有幾輪就報幾輪
    """
    blob: dict
    if cache.is_file() and not refresh:
        blob = json.loads(cache.read_text(encoding="utf-8"))
        require_pipeline(blob, cache)
    else:
        blob = {}
    if not blob:
        blob = {
            "source_events": None, "pipeline": PIPELINE,
            "model": slow_path.MODEL, "effort": slow_path.EFFORT,
            "utterance_effort": slow_path.UTTERANCE_EFFORT,
            "phase": replay.phase, "scorer_git_sha": score_run.get_scorer_git_sha(),
            "points_meta": None, "rounds": [],
        }
    have = len(blob.get("rounds", []))
    if report_only:
        if have == 0:
            raise RuntimeError(f"--report-only 但 {cache} 裡一輪都沒有")
        if have < rounds:
            print(f"\n⚠ --report-only：快取只有 {have} 輪，少於要求的 {rounds} 輪，"
                  f"以下所有數字都是 {have} 輪的。")
        return blob

    for r in range(have, rounds):
        print(f"\n--- 第 {r + 1}/{rounds} 輪：重評 {len(slow_events)} 點（呼叫 LLM）---")
        points = rescore(replay, slow_events, solved, retries=retries)
        recompute_gates(replay, points)
        if blob["points_meta"] is None:
            blob["points_meta"] = point_meta(points)
        blob["rounds"].append({
            "round": r + 1,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "points": [{"index": p["index"], "new": p["new"], "error": p["error"]}
                       for p in points],
        })
        cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        n_int = sum(1 for p in points if p["new"] and p["new"]["is_intervention"])
        n_adm = sum(1 for p in points if p["new"] and p["new"]["admissible"])
        n_err = sum(1 for p in points if not p["new"])
        print(f"  → 第 {r + 1} 輪：is_intervention {n_int}／admissible {n_adm}／失敗 {n_err}"
              f"（已寫入 {cache}）")
    return blob


def _counter(values: list) -> dict:
    out: dict = {}
    for v in values:
        k = "ERROR" if v is _ERR else str(v)
        out[k] = out.get(k, 0) + 1
    return out


class _Err:
    def __repr__(self) -> str:
        return "ERROR"


_ERR = _Err()


def per_point_stability(meta: list[dict], rounds: list[dict]) -> list[dict]:
    """逐點：每個欄位在 N 輪裡各出現幾次，以及三軸分數的變動範圍。

    失敗的點記成 "ERROR"，不併進「不介入」——那會讓介入數偏低而且看不出來。
    """
    by_round = [{p["index"]: p for p in r["points"]} for r in rounds]
    out = []
    for m in meta:
        i = m["index"]
        news = []
        for br in by_round:
            rec = br.get(i)
            news.append(rec.get("new") if rec else None)
        errs = sum(1 for n in news if n is None)
        pick = lambda key: [(_ERR if n is None else n.get(key)) for n in news]  # noqa: E731
        axes = {}
        for ax in ("positive", "negative", "none"):
            vals = [n.get(ax) for n in news if n]
            axes[ax] = {
                "values": [(None if n is None else n.get(ax)) for n in news],
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
                "range": (max(vals) - min(vals)) if vals else None,
            }
        margins = [(None if n is None else margin_of(n)) for n in news]
        mv = [x for x in margins if x is not None]
        verdicts = _counter(pick("verdict"))
        types = _counter(pick("type"))
        interventions = _counter(pick("is_intervention"))
        admissibles = _counter(pick("admissible"))
        # 「decide() 說介入、卻被 type=無 擋掉」的輪數——這是第二個獨立的翻轉來源
        blocked_by_type = sum(
            1 for n in news
            if n and margin_of(n) is not None and margin_of(n) >= 1 and not n["is_intervention"])
        out.append({
            "index": i,
            "t_score": round(m["t_score"], 2),
            "emit_t": round(m["emit_t"], 2),
            "n_rounds": len(news),
            "n_error": errs,
            "verdict": verdicts,
            "type": types,
            "is_intervention": interventions,
            "admissible": admissibles,
            "axes": axes,
            "margin": {"values": margins,
                       "min": min(mv) if mv else None,
                       "max": max(mv) if mv else None},
            "rounds_within_one_point": sum(1 for x in mv if x in (0, 1)),
            "rounds_decide_yes_but_type_none": blocked_by_type,
            "stable_verdict": len(verdicts) == 1 and errs == 0,
            "stable_type": len(types) == 1 and errs == 0,
            "stable_is_intervention": len(interventions) == 1 and errs == 0,
            "stable_admissible": len(admissibles) == 1 and errs == 0,
        })
    return out


def per_round_totals(rounds: list[dict]) -> list[dict]:
    out = []
    for r in rounds:
        ps = r["points"]
        out.append({
            "round": r["round"],
            "n_points": len(ps),
            "is_intervention": sum(1 for p in ps if p["new"] and p["new"]["is_intervention"]),
            "admissible": sum(1 for p in ps if p["new"] and p["new"]["admissible"]),
            "errors": sum(1 for p in ps if not p["new"]),
        })
    return out


def per_round_windows(replay: Replay, raw: list[dict], meta: list[dict],
                      rounds: list[dict], labels_path: Path, out_dir: Path) -> list[dict]:
    """每一輪各跑一次 `score_run.build_report()`，抽出窗格指標。

    算法完全來自 `score_run.py`：這裡只負責把某一輪的結果轉成它吃的事件檔形狀
    （`build_counterfactuals()`，跟單次模式同一支），再讀它算好的欄位。
    """
    out = []
    for r in rounds:
        pts = merge_points(meta, r["points"])
        rd = out_dir / "rounds" / f"r{r['round']:02d}"
        rd.mkdir(parents=True, exist_ok=True)
        reports = run_scorer(build_counterfactuals(raw, [p for p in pts if p["new"]]),
                             labels_path, rd, quiet=True)
        rec: dict = {"round": r["round"],
                     "errors": sum(1 for p in pts if not p["new"]),
                     "variants": {}}
        for name, rep in reports.items():
            recall = rep["metrics"]["overall"]["opportunity_recall"]
            slow_recall = rep["metrics"]["slow"]["opportunity_recall"]
            rec["variants"][name] = {
                "opportunity_hits": recall.get("hits"),
                "opportunity_total": recall.get("total"),
                "opportunity_recall": recall.get("value"),
                "slow_opportunity_hits": slow_recall.get("hits"),
                "counts": rep["intervention_counts"],
                "windows": {wid: {"kind": w["kind"],
                                  "hit_t": (w["hit"] or {}).get("t"),
                                  "hit_kind": (w["hit"] or {}).get("kind"),
                                  "fp": len(w["fp_events"]),
                                  "scored": w.get("scored")}
                            for wid, w in rep["windows"]["detail"].items()},
            }
        out.append(rec)
    return out


def build_stability(replay: Replay, raw: list[dict], blob: dict,
                    labels_path: Path | None, out_dir: Path) -> dict:
    meta = blob["points_meta"]
    rounds = blob["rounds"]
    # 兩段閘門是純計算，每一輪都在報表前重算一次再寫回——理由同單次模式的
    # `recompute_gates()`：快取只該存模型輸出，admissible 會隨
    # `live.slow_result_admissible()` 改動而過期。`merge_points()` 不 copy `new`，
    # 所以寫回的值會留在 blob 裡，下面所有統計看到的都是重算後的值。
    for r in rounds:
        recompute_gates(replay, merge_points(meta, r["points"]))
    rep: dict = {
        "model": blob["model"], "effort": blob["effort"], "phase": blob["phase"],
        "n_rounds": len(rounds), "n_points": len(meta),
        "recorded_at": [r["recorded_at"] for r in rounds],
        "per_round_totals": per_round_totals(rounds),
        "per_point": per_point_stability(meta, rounds),
    }
    if labels_path:
        rep["per_round_windows"] = per_round_windows(
            replay, raw, meta, rounds, labels_path, out_dir)
    return rep


def print_stability(rep: dict) -> None:
    R = rep["n_rounds"]
    bar = "=" * 108
    print("\n" + bar)
    print(f"多輪穩定度：{rep['n_points']} 個評分點 × {R} 輪"
          f"（model={rep['model']} effort={rep['effort']}）")
    print(bar)

    # 1) 總量分佈：把每一輪的值都列出來，不給平均
    print("\n【1】每輪總量（N=5 講平均沒有意義，逐輪列出）")
    print(f"  {'輪':>3} {'is_intervention':>16} {'admissible':>12} {'失敗':>6}")
    for t in rep["per_round_totals"]:
        print(f"  {t['round']:>3} {t['is_intervention']:>16} {t['admissible']:>12} {t['errors']:>6}")
    ints = [t["is_intervention"] for t in rep["per_round_totals"]]
    adms = [t["admissible"] for t in rep["per_round_totals"]]
    errs = sum(t["errors"] for t in rep["per_round_totals"])
    print(f"  is_intervention 分佈：{ints}（{min(ints)}–{max(ints)}）")
    print(f"  admissible      分佈：{adms}（{min(adms)}–{max(adms)}）")
    print(f"  呼叫失敗總數：{errs}"
          + ("（失敗點不併入「不介入」，逐點表以 ERROR 顯示）" if errs else ""))

    # 2) 逐點穩定度
    pp = rep["per_point"]
    flip_int = [p for p in pp if not p["stable_is_intervention"]]
    flip_verd = [p for p in pp if not p["stable_verdict"]]
    flip_type = [p for p in pp if not p["stable_type"]]
    flip_adm = [p for p in pp if not p["stable_admissible"]]
    print(f"\n【2】逐點穩定度（{R} 輪一致＝穩定）")
    print(f"  is_intervention 穩定 {len(pp) - len(flip_int)}／{len(pp)}"
          f"，搖擺 {len(flip_int)}")
    print(f"  verdict         穩定 {len(pp) - len(flip_verd)}／{len(pp)}，搖擺 {len(flip_verd)}")
    print(f"  type            穩定 {len(pp) - len(flip_type)}／{len(pp)}，搖擺 {len(flip_type)}")
    print(f"  admissible      穩定 {len(pp) - len(flip_adm)}／{len(pp)}，搖擺 {len(flip_adm)}")
    print(f"\n  {'#':>3} {'t_score':>8} {'P 範圍':>10} {'N 範圍':>10} {'None 範圍':>11} "
          f"{'margin':>12}  verdict×輪 / type×輪 / 介入×輪")
    print("  " + "-" * 104)
    for p in pp:
        ax = p["axes"]
        f = lambda a: (f"{ax[a]['min']}-{ax[a]['max']}" if ax[a]["min"] != ax[a]["max"]  # noqa: E731
                       else f"{ax[a]['min']}")
        mark = "  ←搖擺" if not p["stable_is_intervention"] else ""
        m = p["margin"]
        ms = f"{m['min']}..{m['max']}" if m["min"] != m["max"] else f"{m['min']}"
        print(f"  {p['index'] + 1:>3} {p['t_score']:>8.1f} {f('positive'):>10} {f('negative'):>10} "
              f"{f('none'):>11} {ms:>12}  "
              f"{_fmt_counter(p['verdict'])} / {_fmt_counter(p['type'])} / "
              f"{_fmt_counter(p['is_intervention'])}{mark}")

    if flip_int:
        print("\n  ── 搖擺點（判準的真正邊界）──")
        for p in flip_int:
            ax = p["axes"]
            print(f"   #{p['index'] + 1:>2} t={p['t_score']:.1f}s  "
                  f"P={ax['positive']['values']} N={ax['negative']['values']} "
                  f"None={ax['none']['values']}  margin={p['margin']['values']}")
            print(f"        verdict={_fmt_counter(p['verdict'])}  type={_fmt_counter(p['type'])}"
                  f"  介入={_fmt_counter(p['is_intervention'])}"
                  f"  admissible={_fmt_counter(p['admissible'])}")

    # 3) 三軸離散程度 ＋ 一分之差
    print("\n【3】三軸離散與「一分之差」")
    for ax in ("positive", "negative", "none"):
        rng = [p["axes"][ax]["range"] for p in pp if p["axes"][ax]["range"] is not None]
        dist: dict = {}
        for r in rng:
            dist[r] = dist.get(r, 0) + 1
        moved = sum(1 for r in rng if r > 0)
        print(f"  {ax:<9} 輪間變動範圍分佈 {dict(sorted(dist.items()))}"
              f"（{moved}／{len(rng)} 個點在動，最大 {max(rng) if rng else 0} 分）")
    near = [p for p in pp if p["rounds_within_one_point"] > 0]
    always_near = [p for p in pp if p["rounds_within_one_point"] == p["n_rounds"] - p["n_error"]
                   and p["n_rounds"] - p["n_error"] > 0]
    print(f"  margin = max(P,N) - None：<=0 不介入、>=1 介入")
    print(f"  有任一輪 margin ∈ {{0,1}}（動一分就翻面）的點：{len(near)}／{len(pp)}")
    print(f"  每一輪都卡在一分之差的點：{len(always_near)}／{len(pp)}")
    print(f"  卡一分之差的點號：{[p['index'] + 1 for p in near]}")
    blocked = [p for p in pp if p["rounds_decide_yes_but_type_none"] > 0]
    print(f"  decide() 判介入但被 type=無 擋掉（第二個翻轉來源）的點："
          f"{[(p['index'] + 1, p['rounds_decide_yes_but_type_none']) for p in blocked]}")

    # 4) 窗格指標分佈
    if "per_round_windows" not in rep:
        print("\n【4】窗格指標：沒給 --labels，跳過")
        return
    print("\n【4】窗格指標（算法來自 experiments/score_run.py，逐輪列出）")
    for variant in ("slow_prompt_only", "slow_only_t15fixed"):
        rows = [(r["round"], r["variants"][variant]) for r in rep["per_round_windows"]
                if variant in r["variants"]]
        if not rows:
            continue
        print(f"\n  --- {variant} ---")
        wids = list(rows[0][1]["windows"].keys())
        print(f"  {'輪':>3} {'命中':>7} {'slow介入':>8} {'TP':>3} {'FP窗內':>6} {'FP窗外':>6}  "
              + "  ".join(f"{w}命中/FP" for w in wids))
        for rnd, v in rows:
            c = v["counts"]
            cells = []
            for w in wids:
                d = v["windows"][w]
                hit = "○" if d["hit_t"] is not None else "×"
                cells.append(f"{hit}/{d['fp']}")
            print(f"  {rnd:>3} {str(v['opportunity_hits']) + '/' + str(v['opportunity_total']):>7} "
                  f"{c['slow']:>8} {c['tp']:>3} {c['fp_in_window']:>6} {c['fp_outside_windows']:>6}  "
                  + "     ".join(f"{c2:>6}" for c2 in cells))
        hits = [v["opportunity_hits"] for _, v in rows]
        total = rows[0][1]["opportunity_total"]
        full = sum(1 for h in hits if h == total)
        print(f"  命中數分佈：{hits}（滿分 {total}）")
        print(f"  → 「命中 {total}/{total}」在 {len(rows)} 輪裡出現 {full} 次")
        for w in wids:
            fps = [v["windows"][w]["fp"] for _, v in rows]
            if any(fps):
                print(f"    窗格 {w}（{rows[0][1]['windows'][w]['kind']}）FP 分佈：{fps}")


def _fmt_counter(c: dict) -> str:
    return ",".join(f"{k}×{v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))


# ── main ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("events", type=Path, help="錄好的 events.jsonl（唯讀）")
    ap.add_argument("--labels", type=Path, default=None, help="labels.json，給 score_run.py 計分用")
    ap.add_argument("--out", type=Path, default=None, help="輸出目錄（預設 experiments/out/rescore-<事件檔父目錄名>）")
    ap.add_argument("--verify", action="store_true", help="只跑重建自我驗證，不呼叫 LLM")
    ap.add_argument("--report-only", action="store_true", help="只從既有 rescored.json 重算指標，不呼叫 LLM")
    ap.add_argument("--refresh", action="store_true", help="即使已有 rescored.json 也重新呼叫 LLM")
    ap.add_argument("--limit", type=int, default=None, help="只重評前 N 個點（除錯用）")
    ap.add_argument("--rounds", type=int, default=1, metavar="N",
                    help="把同一批評分點重跑 N 輪量穩定度（>1 進多輪模式，走 "
                         "rescored.rounds.json，不碰單次模式的 rescored.json）")
    ap.add_argument("--dump-prompt", type=int, default=None, metavar="N",
                    help="印出第 N 個評分點（1-based）重建出來的 prompt 與當時記錄的輸出，人工交叉比對用")
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args(argv)

    raw = [json.loads(l) for l in args.events.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = [Event(kind=r["kind"], t=r["t"], data=r["data"]) for r in raw]
    events.sort(key=lambda e: e.t)
    replay = Replay(events)
    slow_events = [e for e in events if e.kind == "slow_score"]
    out_dir = args.out or (_HERE / "out" / f"rescore-{args.events.resolve().parent.name}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / "rescored.json"

    print(f"事件檔：{args.events}")
    print(f"議題：{replay.topic}／{replay.duration_min} 分鐘／phase={replay.phase}／"
          f"participants={[j.name for j in replay.joins]}")
    print(f"慢路評分點：{len(slow_events)}    輸出目錄：{out_dir}")
    print(f"重評用的模型：{slow_path.MODEL}  effort={slow_path.EFFORT}")

    drift = tick_drift(events)
    solved = solve_score_times(replay, slow_events, drift=drift)

    if args.dump_prompt:
        s = solved[args.dump_prompt - 1]
        old = slow_events[args.dump_prompt - 1]
        print(f"\n--- 第 {args.dump_prompt} 點：t_score={s['t_score']:.2f}s"
              f"（emit_t={s['emit_t']:.2f}s，LLM 延遲 {s['latency']:.2f}s）---")
        print(slow_path.build_prompt(replay.state_at(s["t_score"]), s["t_score"], replay.phase))
        print("\n--- 當時記錄下來的輸出（事件檔，未經重建）---")
        print(json.dumps(old.data, ensure_ascii=False, indent=2))
        return 0

    print("\n--- 重建自我驗證 ---")
    v = verify(replay, events, solved)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    (out_dir / "verify.json").write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    hard_fail = (v["fast_timer"]["silent_mismatch"] or v["fast_timer"]["participant_order_mismatch"]
                 or v["share"]["mismatch"])
    if hard_fail:
        print("\n重建與事件檔對不上，停在這裡——不要拿對不上的 state 去重評。")
        return 2
    if args.verify:
        return 0

    if args.rounds > 1:
        todo = slow_events[:args.limit] if args.limit else slow_events
        rounds_cache = out_dir / ROUNDS_CACHE
        try:
            blob = collect_rounds(replay, todo, solved[:len(todo)],
                                  rounds=args.rounds, cache=rounds_cache,
                                  retries=args.retries, report_only=args.report_only,
                                  refresh=args.refresh)
            blob["source_events"] = str(args.events)
            rep = build_stability(replay, raw, blob, args.labels, out_dir)
        except RuntimeError as exc:
            print(f"\n{exc}")
            return 2
        # build_stability 會就地重算兩段閘門，寫檔放在它後面才存得到重算後的值
        rounds_cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        (out_dir / "stability.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print_stability(rep)
        print(f"\n原始輸出：{rounds_cache}（每輪 {len(todo)} 點的模型輸出全在裡面，"
              f"換指標用 --rounds {args.rounds} --report-only 重算，不會再呼叫 LLM）")
        print(f"穩定度報表：{out_dir / 'stability.json'}")
        return 0

    try:
        if cache.is_file() and not args.refresh:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            require_pipeline(blob, cache)
            points = blob["points"]
            print(f"\n沿用既有結果：{cache}（要重新呼叫 LLM 請加 --refresh）")
        elif args.report_only:
            print(f"\n--report-only 但找不到 {cache}")
            return 2
        else:
            todo = slow_events[:args.limit] if args.limit else slow_events
            print(f"\n--- 重評 {len(todo)} 點（呼叫 LLM）---")
            points = rescore(replay, todo, solved[:len(todo)], retries=args.retries)
            blob = {"source_events": str(args.events), "pipeline": PIPELINE,
                    "model": slow_path.MODEL, "effort": slow_path.EFFORT,
                    "utterance_effort": slow_path.UTTERANCE_EFFORT, "phase": replay.phase,
                    "scorer_git_sha": score_run.get_scorer_git_sha(), "points": points}

        # 兩段閘門是純計算，每次都重算再寫回——快取只該存模型輸出，不該存會隨
        # `live.slow_gate()`／`live.slow_recheck_admissible()` 改動而過期的判定結果。
        recompute_gates(replay, points)
    except StaleCacheError as exc:
        print(f"\n{exc}")
        return 2
    blob["points"] = points
    cache.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已寫入 {cache}")
    print_table(points)
    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else None
    print_summary(points, labels)

    if args.labels:
        print("\n" + "=" * 100)
        print("窗口計分（規則來自 experiments/score_run.py，這裡只負責把結果轉成它吃的格式）")
        print("=" * 100)
        run_scorer(build_counterfactuals(raw, [p for p in points if p["new"]]),
                   args.labels, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
