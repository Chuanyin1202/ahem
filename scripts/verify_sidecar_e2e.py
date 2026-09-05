"""Synthetic only: live writer -> separate bridge process -> running UI, plus retry."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from playwright.sync_api import sync_playwright, expect
from meeting_host.events import Event
from meeting_host.live import _write_events_jsonl
from meeting_host.enterprise_security import secure_write_text

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--runtime', type=Path, required=True)
p.add_argument('--url', required=True)
p.add_argument('--output', type=Path, required=True)
args = p.parse_args()
source = Path(tempfile.mkdtemp(prefix='bridge-fixture-',dir=args.runtime))
ids = json.loads((args.runtime/'identities.json').read_text())
operator = next(i for i in ids if i['id']=='operator')
secure_write_text(source/'operator-key',operator['token'])
events = [Event('meeting',0,{'topic':'Sidecar 整合驗證（合成資料） '+source.name,'participants':['測試與會者']}),
          Event('utterance',1,{'speaker':'測試與會者','text':'這段內容由 Ahem 寫檔，再由獨立程序自動匯入。'}),
          Event('spoken',2,{'text':'主席確認同步流程。'})]
_write_events_jsonl(SimpleNamespace(events=events),source/'meeting-integration.events.jsonl')
cmd = [sys.executable,'-m','meeting_host.enterprise_bridge','--source',str(source),
       '--database',str(args.runtime/'enterprise.db'),'--identities',str(args.runtime/'identities.json'),
       '--token-file',str(source/'operator-key'),'--policy','team']
env = dict(os.environ,AHEM_KEK_FILE=str(args.runtime/'kek'))
reports = [json.loads(subprocess.check_output(cmd,env=env,text=True)) for _ in range(2)]
assert reports == [dict(imported=1,duplicates=0,rejected=0),dict(imported=0,duplicates=1,rejected=0)]
args.output.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome')
    page = browser.new_page(viewport={'width':1536,'height':1024})
    errors = []
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(args.url)
    assert page.title() == 'Ahem 企業工作台'
    page.get_by_label('存取憑證').fill(operator['token'])
    page.get_by_role('button',name='進入工作台').click()
    page.locator('#workspace').wait_for(state='visible')
    expect(page.locator('#panel')).to_have_attribute('aria-busy','false')
    # Source receipt is deliberately not exposed through public UI/API.
    import sqlite3
    with sqlite3.connect(args.runtime/'enterprise.db') as db:
        import hashlib
        digest = hashlib.sha256((source/'meeting-integration.events.jsonl').read_bytes()).hexdigest()
        mid = db.execute('SELECT meeting FROM import_receipts WHERE source=?',(digest,)).fetchone()[0]
    page.get_by_role('button',name='內容與授權',exact=True).click()
    page.locator('tbody tr').filter(has_text=mid[:8]).get_by_role('button',name='查看內容').click()
    page.get_by_role('button',name='確認讀取').click()
    expect(page.locator('.transcript')).to_contain_text('獨立程序自動匯入')
    assert not errors
    page.evaluate('window.scrollTo(0,0)')
    page.screenshot(path=str(args.output/'bridge-content.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(args.output/'bridge-mobile.png'),full_page=True)
    browser.close()
report = {'scans':reports,'page_errors':errors,'rendered_imported_content':True,
          'fixture':'synthetic, using real live._write_events_jsonl and two fresh CLI processes'}
(args.output/'bridge-results.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
