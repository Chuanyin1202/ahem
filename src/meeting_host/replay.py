"""把測試場景轉成帶時間軸的發言序列。

用模擬時間而非真實時間，所以一場 30 分鐘的會議可以瞬間跑完。
真實會議接上 STT 之後產出的是同樣的 Utterance 序列——兩邊共用下游全部邏輯。
"""
import re

from .state import MeetingState, Utterance

CHARS_PER_SECOND = 4.5  # 中文口語語速，用來估算發言長度


def _to_seconds(ts: str) -> float:
    m, s = ts.split(":")
    return int(m) * 60 + int(s)


def _duration(s: str) -> float:
    """'8分20秒' / '40秒' / '0秒' → 秒"""
    m = re.search(r"(\d+)\s*分", s)
    sec = re.search(r"(\d+)\s*秒", s)
    return (int(m.group(1)) * 60 if m else 0) + (int(sec.group(1)) if sec else 0)


def _last_spoke_at(s: str, elapsed: float) -> float | None:
    """'6分鐘前' → 絕對時刻；'剛剛'/'進行中' → 現在；'從未發言' → None"""
    if "從未" in s:
        return None
    if "剛剛" in s or "進行中" in s:
        return elapsed
    return max(0.0, elapsed - _duration(s))


def load(scenario: dict) -> tuple[MeetingState, list[Utterance]]:
    st = MeetingState(
        topic=scenario["topic"],
        duration_min=scenario["duration"],
        participants=list(scenario["stats"].keys()),
    )

    raw = [(_to_seconds(ts), who, text) for ts, who, text in scenario["transcript"]]
    utterances = []
    for i, (start, who, text) in enumerate(raw):
        next_start = raw[i + 1][0] if i + 1 < len(raw) else float("inf")
        next_who = raw[i + 1][1] if i + 1 < len(raw) else None
        if next_who == who:
            # 逐字稿只放代表性摘要句（句尾常接「...」）——同一人連續兩則
            # 代表中間一直在講，不是真的停頓。end 直接接到下一句開頭；
            # 若改用字數估算，摘要句字數遠少於真實時間戳間隔，會把「持續
            # 發言」誤建模成一堆有數十秒空檔的短句，讓 state.py 的沉默
            # 判斷（run 中斷）把摘要斷句當成真的沉默（見 task-f-report.md）
            end = next_start
        else:
            # 換人或逐字稿結束：講完的時間用語速估算，但不能疊到下一句開頭
            end = min(start + len(text) / CHARS_PER_SECOND, next_start)
        utterances.append(Utterance(who, text, start, end))

    # 逐字稿只保留最後幾則，前面那段「看不見的歷史」由 stats 補回，
    # 否則發言時長與沉默時長會嚴重失真（真實會議中不需要這步）
    elapsed = scenario["elapsed"] * 60
    first_ts = _to_seconds(scenario["transcript"][0][0])
    for who, d in scenario["stats"].items():
        last = _last_spoke_at(d["last"], elapsed)
        if last is not None:
            # 夾到逐字稿起點：語意是「開始觀察時，此人最後發言不晚於起點」。
            # 不夾的話，逐字稿範圍內才發言的人會被誤判成從開會就沒說過話
            st.prior_last[who] = min(last, first_ts)
        # 用 utterances 的實際長度（上面算好的 end - start），不是字數估算——
        # 同一人連續句現在 end 會接到下一句開頭，字數估算會讓這裡偏小，
        # prior_spoke + in_transcript 就對不上 stats 給的總發言時長
        in_transcript = sum(u.end - u.start for u in utterances if u.speaker == who)
        st.prior_spoke[who] = max(0.0, _duration(d["spoke"]) - in_transcript)

    return st, utterances
