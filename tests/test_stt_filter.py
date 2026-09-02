"""T2：純標點／省略號的 committed transcript 不該進入 Utterance 流。

真實會議實測：使用者沉默或喘氣時，STT 用 commit_strategy=vad 會連續 commit 出
「......」這種純標點雜訊（如 [13:03][13:06][13:09] 三筆連發），這些進了 utterances
後，慢路 LLM 把它們當成證據，寫出「持續沉默與省略號顯示討論陷入僵局」的介入理由——
垃圾進垃圾出。

is_substantive() 是唯一的過濾邏輯：只要含至少一個 Unicode 字母／數字就放行，
不限半形英數／CJK——全形數字、全形字母、CJK 擴充區、帶圈數字都算數。
語助詞（嗯、哎）本身就是合法發言，不能被一起擋掉。
"""
import pytest

from meeting_host.stt import is_substantive, to_traditional


NON_SUBSTANTIVE_CASES = [
    "......",
    "……",
    "...",
    "。。。",
    "—",
    "--",
    "... 。",
    "",
    "   ",
    "、、、",
    "_",
    "_ _",
]

SUBSTANTIVE_CASES = [
    "嗯。",
    "哎。",
    "OK...",
    "A",
    "1",
    "討論一下企劃",
    "...A",
    "1...",
    "１２３",  # 全形數字
    "ＯＫ",  # 全形字母
    "㐀",  # CJK 擴充區 A
]


@pytest.mark.parametrize("text", NON_SUBSTANTIVE_CASES)
def test_non_substantive_text_is_filtered(text):
    """純標點／省略號／空白／底線，不含任何 Unicode 字母或數字 → 判為非實質內容。"""
    assert is_substantive(text) is False


@pytest.mark.parametrize("text", SUBSTANTIVE_CASES)
def test_substantive_text_passes(text):
    """含至少一個 Unicode 字母或數字（半形／全形／CJK／語助詞皆算）→ 判為合法發言，不得被擋。"""
    assert is_substantive(text) is True


def test_simplified_to_traditional_still_substantive():
    """ElevenLabs zho 輸出一律簡體，經 to_traditional 轉繁體後仍要判定為合法發言。

    簡體字與繁體字都落在同一個中文字判準區間，轉換是否實際發生（取決於這台機器
    是否裝了 opencc，見 stt.py 的 ImportError fallback）不影響本測試的判定。
    """
    converted = to_traditional("讨论一下软件")
    assert is_substantive(converted) is True
