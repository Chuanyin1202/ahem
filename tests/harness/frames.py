"""帶編號的 PCM 幀：迴歸 2（prebuffer 後 chunk 重複）的 PCM 完整性 oracle 用。

跟既有 tests/test_chair.py 的做法（用固定內容 `b"\\x02" * FRAME_BYTES` 數
幀的總數）不同——編號幀連「順序對不對」「有沒有哪一幀被播兩次」「有沒有
哪一幀完全沒播到（但總數剛好被另一幀重播抵銷）」都能一次驗完，不只是
總數對不對。
"""
from meeting_host.audio import FRAME_BYTES

_MARKER = b"\xab"
_SEQ_BYTES = 4


def numbered_frame(seq: int) -> bytes:
    """一個完整長度（FRAME_BYTES）的假語音幀，前 4 bytes 是大端序號，其餘填 marker。"""
    if seq < 0 or seq >= 2 ** (8 * _SEQ_BYTES):
        raise ValueError(f"序號超出 4-byte 範圍：{seq}")
    payload = seq.to_bytes(_SEQ_BYTES, "big")
    return payload + _MARKER * (FRAME_BYTES - len(payload))


def frame_seq(frame: bytes) -> int | None:
    """還原序號；靜音幀、earcon 幀或任何不是 `numbered_frame()` 產出的內容回 None。"""
    if len(frame) != FRAME_BYTES:
        return None
    if frame[_SEQ_BYTES:] != _MARKER * (FRAME_BYTES - _SEQ_BYTES):
        return None
    return int.from_bytes(frame[:_SEQ_BYTES], "big")
