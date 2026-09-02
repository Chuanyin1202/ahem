"""迴歸：「發言超時」不再對同一個人連續重複觸發（T24 任務二）。

症狀（真實會議，非構造場景）：2026-08-29 20:40 那場 14 分 27 秒的雙人 Discord
會議，主席全場出聲 12 次，其中 11 次是「發言超時」，時間點
401/431/467/499/530/561/591/622/657/698/730 秒——幾乎每 30 秒一次，貼著
`COOLDOWN_SECONDS = 30` 的上限，對同一個人（MiMi）連唸 11 次。
事件檔與標註見 `experiments/holdout/2026-08-29-two-person/`（labels.json 的
`known_false_positives`）。那場是 T15 修正之前錄的（provenance.code = 69d1535）。

兩個機制合起來才產生這個症狀，缺一不可：

1. `state._chain_start()`（T15 之前）遇到不同 speaker 只 `continue` 不中斷，
   兩人快速交替時鏈會一路往前串穿別人的發言，量到的是「這個人整場散落的
   發言總和」而不是「他不間斷佔著發言權的那一段」——run 從 3.5 分一路長到
   5.2 分，永遠不歸零。
2. `live.py:422` 的 `done.discard(("發言超時", previous))`：換人講話時解除
   前一位的 claim。這行是「重複觸發」的閘門，**T15 沒有動它，現在也還在**。

所以「T15 修好了」不是理所當然的。這條測試不做推論，直接把那場真實會議的
發言序列餵進現在的程式碼，數出來幾次。

`(2)` 單獨存在是正確的：一個人被提醒之後別人接了話，他之後再連講三分鐘
還是該被提醒。有 `(1)` 修正之後，換人就會讓他的 run 從別人那句之後重新
起算，`(2)` 解除 claim 也不會導致重複——這條測試同時把這個交互釘住。
"""
import json
from pathlib import Path

import pytest

from meeting_host import fast_path, live
from meeting_host.state import RUN_GAP_SECONDS, MeetingState, Utterance

EVENTS = (Path(__file__).parents[2] / "experiments" / "holdout"
          / "2026-08-29-two-person" / "meeting.events.jsonl")

pytestmark = pytest.mark.skipif(not EVENTS.exists(), reason="需要 experiments/holdout/2026-08-29-two-person 的真實會議資料（不在公開 repo，見 experiments/holdout/README.md）")

# labels.json 記錄的誤報時間點，範圍取 401–730 秒
FALSE_POSITIVE_WINDOW = (401.0, 730.0)
RECORDED_FALSE_POSITIVES = 11

TICK = 1.0          # 與 live.py 的 watch_fast 同節奏
REPLAY_END = 780.0  # 蓋過最後一次誤報（730s）之後仍有餘裕


def _load_utterances() -> list[Utterance]:
    """從事件檔取出實際發言序列（只讀，不改動 ground truth）。"""
    us = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["kind"] == "utterance":
            d = e["data"]
            us.append(Utterance(d["speaker"], d["text"], d["start"], d["end"]))
    us.sort(key=lambda u: u.start)
    return us


def _load_joined_at() -> dict[str, float]:
    """從事件檔的 `meeting` 事件取出每個人第一次出現在 participants 的時刻。

    真實會議中 bot 先進空頻道等人，`live.py` 靠 `ensure_participant(name, now)`
    記下 `joined_at`，`silent_seconds()` 才知道「還沒開口的人」該從何時起算沉默
    （state.py T13）。回放台的 participants 是建構參數、沒走過那條路徑，
    不補這份資料的話兩位參與者的沉默都會從 t=0 起算，「全場沉默」會在
    第一個人進頻道之前（t=90）就誤觸發一次——那一次會吃掉一格退避
    （`SILENCE_BACKOFF_FACTOR`），把後面的門檻從 90s 拉到 135s，讓收尾段
    的量測失真。實際事件檔：MiMi t=118.7、Alex Huang t=127.0。
    """
    joined: dict[str, float] = {}
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["kind"] == "meeting":
            for name in e["data"]["participants"]:
                joined.setdefault(name, e["t"])
    return joined


def _replay(utterances: list[Utterance], end: float = REPLAY_END, *,
            closing_gate: bool = False, joined_at: dict[str, float] | None = None
            ) -> tuple[list[tuple[float, str, str | None]], float]:
    """把那場會議重放一遍，回傳 (快路觸發清單, 全程量到的最大 run 秒數)。

    刻意複製 live.py 的資料流，不是簡化版，否則量到的不是產品：
    - 講話中只有「正在說話」訊號（partial → `speaking_now`），講完（commit）
      才進逐字稿；兩人的 utterance 大量重疊，commit 依 `end` 先後到達
    - `st.utterances.sort(key=start)`（live.py:409，多人各自連線，到達順序
      不等於發生順序）
    - `done` 的三條解除規則（live.py:410/413/422）
    - 一 tick 只排一個介入（fast_path 的仲裁規則，live.py:462 的 `break`）

    closing_gate: True 時把 `Session._fast_tick` 的收尾閘門一起複製進來
        （`fast_path.check(..., closing=live.meeting_is_closing_for_rules(...))`）。
        預設 False＝閘門加入之前的行為，用來做「有閘門／沒閘門」的對照。
    joined_at: 補上 `MeetingState.joined_at`（見 `_load_joined_at`）。預設不補，
        維持這個回放台原本的行為。
    """
    st = MeetingState(topic="黑客松籌備", duration_min=30,
                      participants=["Alex Huang", "MiMi"])
    if joined_at:
        st.joined_at.update(joined_at)
    pending = list(utterances)
    done: set[tuple[str, str | None]] = set()
    previous: str | None = None
    fired: list[tuple[float, str, str | None]] = []
    max_run = 0.0
    now = 0.0
    while now <= end:
        for u in pending:
            if u.start <= now < u.end:
                st.speaking_now(u.speaker, u.start)
        while True:
            ready = [u for u in pending if u.end <= now]
            if not ready:
                break
            u = min(ready, key=lambda x: x.end)
            pending.remove(u)
            st.stopped_speaking(u.speaker)
            st.add(u)
            st.utterances.sort(key=lambda x: x.start)
            done.discard(("有人被冷落", u.speaker))
            done.discard(("全場沉默", None))
            if previous is not None and previous != u.speaker:
                done.discard(("發言超時", previous))  # live.py:422
            previous = u.speaker

        _, run = st.current_run_seconds(now)
        max_run = max(max_run, run)

        closing = live.meeting_is_closing_for_rules(st, now) if closing_gate else False
        for t in fast_path.check(st, now, done, closing=closing):
            fired.append((now, t.kind, t.target))
            st.interventions.append(now)  # 出聲即記錄，冷卻期由此起算
            done.add((t.kind, t.target))
            if t.kind == "全場沉默":
                st.note_room_silence_fired()
            break
        now += TICK
    return fired, max_run


def _pre_t15_chain_start(self, who: str, seg_start: float) -> float:
    """T15 之前的 `_chain_start`（`git show 69d1535:src/meeting_host/state.py`）。

    差別只有一行：不同 speaker 的句子 `continue`（跳過）而不是 `break`（中斷）。
    """
    start = seg_start
    for u in reversed(self.utterances):
        if u.speaker != who or u.end > seg_start:
            continue
        if start - u.end > RUN_GAP_SECONDS:
            break
        start = u.start
    return start


@pytest.fixture(scope="module")
def utterances() -> list[Utterance]:
    return _load_utterances()


def test_harness_reproduces_the_recorded_false_positives_with_pre_t15_code(
        utterances, monkeypatch):
    """先證明這個回放台真的能重現那 11 次連唸——否則下面「0 次」是空的。

    把 `_chain_start` 換回 T15 之前的版本，其餘程式碼不動：同一份發言序列
    應該重新長出 labels.json 記下的那串誤報。
    """
    monkeypatch.setattr(MeetingState, "_chain_start", _pre_t15_chain_start)
    fired, max_run = _replay(utterances)

    overtime = [(t, target) for t, kind, target in fired if kind == "發言超時"]
    in_window = [t for t, _ in overtime
                 if FALSE_POSITIVE_WINDOW[0] <= t <= FALSE_POSITIVE_WINDOW[1]]
    assert len(in_window) == RECORDED_FALSE_POSITIVES, in_window
    # 幾乎每 30 秒一次，貼著 COOLDOWN_SECONDS(30) 的上限——這正是「連唸」的形狀
    # （回放實測最大間隔 36 秒，留一點餘裕不把巧合值釘死）
    gaps = [b - a for a, b in zip(in_window, in_window[1:])]
    assert max(gaps) <= 40.0, gaps
    # 而且連在同一個人身上
    assert sum(1 for _, target in overtime if target == "MiMi") >= 10
    # run 遠超過門檻：跨人累加把 MiMi 的 run 灌到 5 分鐘以上
    assert max_run > 300.0


def test_no_repeated_overtime_on_the_real_two_person_meeting(utterances):
    """現在的程式碼跑同一場會議：「發言超時」一次都不觸發。

    數字的來由：T15 讓 `_chain_start` 遇到別人的發言就中斷鏈，全場量到的
    最長連續發言從 5.2 分鐘掉到 52.7 秒（MiMi）／48.7 秒（Alex Huang），
    連 `OVERTIME_SECONDS = 180.0` 的一半都不到，規則根本不成立——
    這與 labels.json 的判定一致：「MiMi 實際不間斷佔用發言權從未超過 180 秒」。
    所以 11 → 0 不是「重複被壓掉」，是「那 11 次本來就都是誤報」。
    """
    fired, max_run = _replay(utterances)

    overtime = [(t, target) for t, kind, target in fired if kind == "發言超時"]
    assert overtime == []
    assert max_run < fast_path.OVERTIME_SECONDS
    assert max_run == pytest.approx(52.7, abs=0.5)


def test_a_genuine_long_run_still_fires_but_only_once_per_turn(utterances):
    """反向保險：0 次不是因為規則被關掉。

    同一份序列後面接一段真的連續發言（同一人、句間 gap 在
    RUN_GAP_SECONDS 內、總長超過 OVERTIME_SECONDS），規則仍會觸發；
    而且在他這一輪裡只觸發一次——冷卻期過了也不會第二次，因為
    `("發言超時", MiMi)` 的 claim 只有換人講話才會被解除（live.py:422）。
    """
    tail = max(u.end for u in utterances)
    extra = list(utterances)
    t = tail + 10.0
    for _ in range(30):  # 30 段 × 10 秒 = 300 秒連續發言，句間 gap 2 秒
        extra.append(Utterance("MiMi", "我再講一段", t, t + 10.0))
        t += 12.0

    fired, _ = _replay(extra, end=t + 60.0)

    overtime = [(ts, target) for ts, kind, target in fired if kind == "發言超時"]
    assert [target for _, target in overtime] == ["MiMi"]
    assert overtime[0][0] > tail  # 觸發點在後面接上的那段連續發言裡
