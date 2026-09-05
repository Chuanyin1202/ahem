"""Core startup must not require the optional enterprise dependency."""
import os
from pathlib import Path
import subprocess
import sys


def test_live_import_without_cryptography():
    code = '''
import sys
import importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            raise ImportError('optional dependency deliberately absent')
sys.meta_path.insert(0, Block())
from meeting_host import live
assert not any(k.startswith('meeting_host.enterprise') for k in sys.modules)
print('core import independent')
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable,'-c',code],cwd=root,
                            env=dict(os.environ,PYTHONPATH=str(root/'src')),
                            capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
