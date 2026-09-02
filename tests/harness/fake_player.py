"""假播放執行緒：讀 Output、把讀到的編號語音幀記進 ledger。"""
from meeting_host.speaker import Output

from .frames import frame_seq


class FakePlayer:
    """模擬 discord 播放執行緒對 `Output.read()` 的消費行為，但把每次讀到的
    『編號語音幀』序號留下來（ledger），供之後斷言完整性用——跟
    tests/test_chair.py 的 `drain()` 做同一件事，但那邊讀完就丟。
    """

    def __init__(self, output: Output):
        self.output = output
        self.ledger: list[int] = []  # 依讀取順序記錄的語音幀序號

    def drain_all(self) -> None:
        """跟 tests/test_chair.py 的 drain() 同一套終止邏輯（見該處註解）：
        不能用 `is_busy()` 當終止條件——framer 裡剩不足一幀的尾段、佇列已空、
        但 EOS 還沒送到時會自旋死鎖。改成先把佇列現有資料讀空，再多讀一次
        收掉可能的 EOS／flush 尾段，最多多一次 read()，不會自旋。
        """
        while not self.output._q.empty():
            self._read_one()
        self._read_one()

    def _read_one(self) -> None:
        seq = frame_seq(self.output.read())
        if seq is not None:
            self.ledger.append(seq)

    def assert_played_exactly_once_in_order(self, n_frames: int) -> None:
        """迴歸 2 的核心斷言：0..n_frames-1 依序恰好各出現一次。

        跟既有測試只數『總幀數對不對』不同——這裡連順序、重複、遺漏都會被
        抓到（T6b 根因：prebuffer 達標後的 chunk 同時被 append 進 frames
        又直接 enqueue，尾段迴圈再送一次，會讓某些序號重複出現、且順序被
        打亂在整串幀的後段）。
        """
        expected = list(range(n_frames))
        assert self.ledger == expected, (
            f"預期依序恰好播放 0..{n_frames - 1} 各一次，實際 ledger={self.ledger}")
