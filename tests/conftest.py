"""讓 tests 直接 import meeting_host，不用設 PYTHONPATH。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
