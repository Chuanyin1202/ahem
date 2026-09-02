"""迴歸：STT 死掉時，主席不再對著在場的人喊「你怎麼都不說話」。

症狀（真實會議，非構造場景）：2026-08-31 那場 42 分鐘的 Discord 會議，
進行到 36.7 分時 ElevenLabs 額度耗盡，STT 與 TTS 共用同一把 key、兩邊同時死掉。
逐字稿從此停止更新，但 `silent_seconds()` 是從 `Utterance.end` 起算的，於是
每個人的沉默秒數一路往上爬，接下來五分鐘主席連續四次錯誤介入：
41:19 Jax、41:50 全場沉默、41:51 Alex、42:22 Alex、42:53 Jax。
當時 Discord RTP 層的 `voice` 事件**還在正常跳動**——證據在手上，只是沒人用。

── 為什麼用 2026-08-29 那場的事件檔 ────────────────────────────────────

08-31 那場的 events.jsonl 只在 Pi5 上（`~/meeting-host-agent/meetings/`），本機
取不到。所以這裡用 `experiments/holdout/2026-08-29-two-person/meeting.events.jsonl`
自行構造等價情境——構造的只有「STT 在第 N 秒死掉」這一件事，其餘全部是那場
真實會議錄下來的資料：

- **125 則真實 utterance**（含真實的 start／end／commit 到達時刻）
- **1626 筆真實 `voice` 事件**（Discord RTP 層，跟 STT 完全獨立的那條訊號）

`_replay_deaf(stt_dead_at=X)` 的意思是：commit 到達時刻 ≥ X 的逐字稿永遠不會
到（STT 死了，既不吐 partial 也不吐 commit），但 `voice` 時間軸**原封不動照放**。
這正是 08-31 那場的形狀，而且 voice 的密度、停頓分佈都是真的，不是我編的。

── 這個檔案的兩半 ──────────────────────────────────────────────────────

1. **沒有閘門會觸發**（`test_..._without_gate`）：不裝閘門重放，證明同一份資料
   真的會長出那串錯誤介入。沒有這一半，下面的「0 次」可能只是資料沒踩到。
2. **有閘門不會觸發**（`test_..._with_gate`）：其餘一個字都不改，只把
   `fast_path.check(..., deaf=…)` 接上，錯誤介入歸零。

門檻與判準的依據見 `src/meeting_host/hearing.py`；哪三條規則被壓住、
「議程超時」為什麼不壓，見 `fast_path.DEAF_SUPPRESSED_KINDS`。
"""
import json
from pathlib import Path

import pytest

from pathlib import Path as _P
_EVENTS_PATH = _P(__file__).parents[2] / "experiments" / "holdout" / "2026-08-29-two-person" / "meeting.events.jsonl"
pytestmark = pytest.mark.skipif(not _EVENTS_PATH.exists(), reason="需要 experiments/holdout/2026-08-29-two-person 的真實會議資料（不在公開 repo，見 experiments/holdout/README.md）")

from meeting_host import fast_path, live
from meeting_host.hearing import DEAF_VOICED_SECONDS, HearingMonitor
from meeting_host.state import MeetingState, Utterance

from .test_regression_overtime_no_repeat import EVENTS, _load_joined_at

TICK = 1.0            # 與 live.py 的 watch_fast 同節奏
STT_DEAD_AT = 400.0   # 構造的斷線時刻（會議中段，兩人正在來回討論）
REPLAY_END = 820.0    # 事件檔最後一筆 voice 在 861.9；留在 voice 還有資料的範圍內

# 失聰期間絕不該出現的三條（見 fast_path.DEAF_SUPPRESSED_KINDS）
SUPPRESSED = ("發言超時", "有人被冷落", "全場沉默")


def _load_voice() -> list[tuple[float, str, bool]]:
    """事件檔裡的 RTP 層 `voice` 時間軸（只讀，不改動 ground truth）。"""
    out = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["kind"] == "voice":
            out.append((e["t"], e["data"]["speaker"], bool(e["data"]["active"])))
    out.sort(key=lambda x: x[0])
    return out


def _load_utterances_with_arrival() -> list[tuple[float, Utterance]]:
    """(commit 到達時刻, Utterance)。

    到達時刻用事件的 `t`（`Session.emit` 當下的會議相對秒），不是 `u.end`——
    「STT 死掉」切的是**逐字稿什麼時候進到系統**，不是那句話什麼時候講完。
    """
    out = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["kind"] == "utterance":
            d = e["data"]
            out.append((e["t"], Utterance(d["speaker"], d["text"], d["start"], d["end"])))
    out.sort(key=lambda x: x[0])
    return out


def _replay_deaf(*, stt_dead_at: float | None, deaf_gate: bool,
                 end: float = REPLAY_END, joined_at: dict[str, float] | None = None
                 ) -> tuple[list[tuple[float, str, str | None]], list[tuple[float, bool]]]:
    """重放那場會議，可選擇在 `stt_dead_at` 讓 STT 死掉。

    回傳 (快路觸發清單, 失聰狀態變化清單)。

    資料流刻意複製 `live.py`，不是簡化版（跟
    `test_regression_overtime_no_repeat._replay` 同一套，只多兩件事）：
    - `voice` 事件依真實時間軸餵進 `HearingMonitor`（`live.Session.note_voice`）
    - 每一則到達的逐字稿呼叫 `hearing.heard()`（`live.Session.consume`）

    `stt_dead_at=None`＝STT 全程健康（對照組，用來驗證不誤鎖）。
    `deaf_gate=False`＝閘門加入之前的行為，用來做有／沒有閘門的對照。
    """
    st = MeetingState(topic="黑客松籌備", duration_min=30,
                      participants=["Alex Huang", "MiMi"])
    if joined_at:
        st.joined_at.update(joined_at)
    hearing = HearingMonitor()

    voice = _load_voice()
    arrivals = _load_utterances_with_arrival()
    if stt_dead_at is not None:
        # STT 死了：那之後到達的 commit 永遠不會來，partial 也不會來（所以整則
        # 從 pending 拿掉——`speaking_now` 也不該再被餵）
        arrivals = [(t, u) for t, u in arrivals if t < stt_dead_at]
    pending = list(arrivals)

    done: set[tuple[str, str | None]] = set()
    previous: str | None = None
    fired: list[tuple[float, str, str | None]] = []
    transitions: list[tuple[float, bool]] = []
    was_deaf = False
    vi = 0
    now = 0.0
    while now <= end:
        # ── RTP 層訊號（跟 STT 完全獨立，死了也照跳）─────────────────
        while vi < len(voice) and voice[vi][0] <= now:
            _, speaker, active = voice[vi]
            hearing.voice(speaker, active, voice[vi][0])
            vi += 1
        # ── STT 訊號 ────────────────────────────────────────────────
        for at, u in pending:
            if u.start <= now < u.end:
                st.speaking_now(u.speaker, u.start)
        while True:
            ready = [(at, u) for at, u in pending if at <= now]
            if not ready:
                break
            at, u = min(ready, key=lambda x: x[0])
            pending.remove((at, u))
            hearing.heard(now)              # live.py Session.consume
            st.stopped_speaking(u.speaker)
            st.add(u)
            st.utterances.sort(key=lambda x: x.start)
            done.discard(("有人被冷落", u.speaker))
            done.discard(("全場沉默", None))
            if previous is not None and previous != u.speaker:
                done.discard(("發言超時", previous))
            previous = u.speaker

        deaf = hearing.deaf(now)
        if deaf != was_deaf:
            transitions.append((now, deaf))
            was_deaf = deaf

        closing = live.meeting_is_closing_for_rules(st, now)
        for t in fast_path.check(st, now, done, closing=closing,
                                 deaf=deaf if deaf_gate else False):
            fired.append((now, t.kind, t.target))
            st.interventions.append(now)
            done.add((t.kind, t.target))
            if t.kind == "全場沉默":
                st.note_room_silence_fired()
            break
        now += TICK
    return fired, transitions


@pytest.fixture(scope="module")
def joined_at():
    return _load_joined_at()


def _after_death(fired):
    return [f for f in fired if f[0] >= STT_DEAD_AT]


# ── 反向驗證上半：沒有閘門，同一份資料真的會長出那串錯誤介入 ──────────


def test_deaf_meeting_fires_the_false_interventions_without_the_gate(joined_at):
    """先證明這個情境會踩雷，否則下面的「0 次」是空的。

    STT 在 t=400 死掉、`voice` 照跳，不裝閘門重放：主席會開始輪流催在場的人。
    這重現的是 08-31 那場的形狀（「有人被冷落」對著兩個人各喊、外加「全場沉默」）。
    """
    fired, _ = _replay_deaf(stt_dead_at=STT_DEAD_AT, deaf_gate=False,
                             joined_at=joined_at)
    late = _after_death(fired)
    bad = [f for f in late if f[1] in SUPPRESSED]
    assert bad, "情境沒踩到雷，這條測試就沒有意義"
    # 「有人被冷落」對著在場的兩個人都喊過——正是 08-31 那場「輪流對每個人喊」
    neglected = {target for _, kind, target in bad if kind == "有人被冷落"}
    assert neglected == {"Alex Huang", "MiMi"}, bad
    # 「全場沉默」也踩到了：房間其實有人在出聲，聾的是主席
    assert any(kind == "全場沉默" for _, kind, _ in bad), bad


# ── 反向驗證下半：接上閘門，同一份資料零觸發 ──────────────────────────


def test_deaf_meeting_fires_nothing_transcript_dependent_with_the_gate(joined_at):
    """其餘一個字都不改，只把 `deaf=` 接上：三條規則在失聰期間零觸發。"""
    fired, transitions = _replay_deaf(stt_dead_at=STT_DEAD_AT, deaf_gate=True,
                                       joined_at=joined_at)
    late = _after_death(fired)
    assert [f for f in late if f[1] in SUPPRESSED] == []
    # 閘門真的鎖上過（不是「因為別的原因剛好沒觸發」）
    assert transitions and transitions[0][1] is True


def test_gate_locks_in_time_before_the_first_false_intervention(joined_at):
    """閘門必須**趕在**第一次錯誤介入之前鎖上，不然擋不到。

    量的是同一份資料的兩個時刻：沒有閘門時第一次錯誤介入的 t，與閘門鎖上的 t。
    """
    fired_open, _ = _replay_deaf(stt_dead_at=STT_DEAD_AT, deaf_gate=False,
                                  joined_at=joined_at)
    first_bad = min(t for t, kind, _ in _after_death(fired_open) if kind in SUPPRESSED)
    _, transitions = _replay_deaf(stt_dead_at=STT_DEAD_AT, deaf_gate=True,
                                   joined_at=joined_at)
    locked_at = transitions[0][0]
    assert transitions[0][1] is True
    assert locked_at < first_bad, (locked_at, first_bad)
    # 而且鎖上的時刻確實由「累積出聲量」決定，不是別的巧合：
    # 斷線後累積滿 DEAF_VOICED_SECONDS 秒的出聲才成立，所以一定晚於斷線本身
    assert locked_at >= STT_DEAD_AT + DEAF_VOICED_SECONDS


# ── 不誤鎖：STT 全程健康的同一場會議，閘門一次都不鎖 ────────────────


def test_healthy_meeting_never_locks_the_gate(joined_at):
    """對照組：同一場會議、STT 全程健康（逐字稿照常到達），閘門完全不啟動。

    這是 `DEAF_VOICED_SECONDS = 45.0` 的直接驗收——那場健康會議 125 次歸零之間
    量到的最大累積出聲量是 19.8 秒（依據見 hearing.py），離 45 秒還有一倍餘裕。
    """
    _, transitions = _replay_deaf(stt_dead_at=None, deaf_gate=True, joined_at=joined_at)
    assert transitions == []


def test_healthy_meeting_fires_exactly_the_same_triggers_with_and_without_the_gate(joined_at):
    """更強的一條：健康會議接上閘門之後，快路的觸發序列**逐筆相同**。

    「閘門沒鎖」只說明狀態沒變，這條直接比對輸出——失聰閘門對健康會議是完全
    透明的，一次都沒有偷偷改變任何一條規則的行為。
    """
    with_gate, _ = _replay_deaf(stt_dead_at=None, deaf_gate=True, joined_at=joined_at)
    without_gate, _ = _replay_deaf(stt_dead_at=None, deaf_gate=False, joined_at=joined_at)
    assert with_gate == without_gate


# ── 恢復：STT 活過來之後規則照常 ────────────────────────────────────


def test_gate_releases_when_transcripts_come_back(joined_at):
    """STT 只死一段時間（t=400–620）就活過來：閘門鎖上又自動解除，不需重開會議。

    解除條件是「有任何一則逐字稿真的進來」（`HearingMonitor.heard`），
    所以復活之後的第一則 commit 就會讓狀態翻回來。
    """
    # 手動組一個「死了又活」的序列：把 400–620 之間到達的 commit 拿掉，其餘照舊
    st = MeetingState(topic="黑客松籌備", duration_min=30,
                      participants=["Alex Huang", "MiMi"])
    st.joined_at.update(joined_at)
    hearing = HearingMonitor()
    voice = _load_voice()
    arrivals = [(t, u) for t, u in _load_utterances_with_arrival()
                if not (400.0 <= t < 620.0)]
    pending = list(arrivals)
    transitions: list[tuple[float, bool]] = []
    was_deaf = False
    vi = 0
    now = 0.0
    while now <= REPLAY_END:
        while vi < len(voice) and voice[vi][0] <= now:
            _, speaker, active = voice[vi]
            hearing.voice(speaker, active, voice[vi][0])
            vi += 1
        while True:
            ready = [(at, u) for at, u in pending if at <= now]
            if not ready:
                break
            at, u = min(ready, key=lambda x: x[0])
            pending.remove((at, u))
            hearing.heard(now)
            st.add(u)
            st.utterances.sort(key=lambda x: x.start)
        deaf = hearing.deaf(now)
        if deaf != was_deaf:
            transitions.append((now, deaf))
            was_deaf = deaf
        now += TICK

    assert [flag for _, flag in transitions] == [True, False], transitions
    locked_at, released_at = transitions[0][0], transitions[1][0]
    assert 400.0 < locked_at < 620.0
    # 復活後的第一則 commit 一到就解除
    first_back = min(t for t, _ in arrivals if t >= 620.0)
    assert released_at == pytest.approx(first_back, abs=TICK + 0.001), (released_at, first_back)
