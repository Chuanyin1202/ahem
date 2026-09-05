"""Exercise the live local workbench and keep screenshots without credentials."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--identities',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8890')
    parser.add_argument('--channel', default=None, help='Optional installed browser channel, e.g. chrome')
    args=parser.parse_args()
    ids={i['id']:i for i in json.loads(args.identities.read_text())}
    args.output.mkdir(parents=True,exist_ok=True)
    results={}
    with sync_playwright() as p:
        browser=p.chromium.launch(channel=args.channel)
        for role in ['manager','operator','observer','support','content-officer','viewer']:
            context=browser.new_context(viewport={'width':1536,'height':1024})
            page=context.new_page();errors=[];console=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('console',lambda m:console.append(m.text) if m.type=='error' and '401' not in m.text else None)
            page.goto(args.url+'/')
            assert page.title()=='Ahem 企業工作台'
            if role=='manager':
                page.screenshot(path=str(args.output/'login.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(args.output/'login-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.set_viewport_size({'width':1536,'height':1024})
            for asset in ['login-background-v2.png','roles-and-states-v2.png']:
                assert page.request.get(args.url+'/ui/assets/'+asset).status==200
            page.get_by_label('存取憑證').fill(ids[role]['token'])
            page.get_by_role('button',name='進入工作台').click()
            page.locator('#workspace').wait_for(state='visible')
            expect(page.locator('#panel')).to_have_attribute('aria-busy','false')
            if role in {'operator','content-officer'}:
                page.get_by_role('button',name='內容與授權',exact=True).click()
                page.get_by_role('button',name='查看內容').first.wait_for()
                if role=='operator':
                    # Import actual public Ahem JSONL through the UI.
                    source=Path(__file__).resolve().parents[1]/'examples/synthetic-meeting.events.jsonl'
                    count=page.locator('tbody tr').count()
                    page.get_by_label('Ahem 事件檔').set_input_files(source)
                    page.get_by_role('button',name='匯入事件檔').click()
                    expect(page.locator('tbody tr')).to_have_count(count+1)
                    page.get_by_role('button',name='管理授權').first.click()
                    expect(page.locator('.detail')).to_contain_text('會議閱覽授權')
                    if page.get_by_role('button',name='允許閱覽').count():
                        page.get_by_role('button',name='允許閱覽').click()
                    page.get_by_role('button',name='撤銷閱覽').click()
                    page.get_by_role('button',name='允許閱覽').wait_for()
                    page.get_by_role('button',name='允許閱覽').click()
                    page.get_by_role('button',name='撤銷閱覽').wait_for()
                else:
                    row=page.locator('tbody tr').filter(has_text='受限內容').first
                    row.get_by_role('button',name='查看內容').click()
                    page.get_by_role('button',name='確認讀取').click()
                    page.locator('.transcript').wait_for()
                    assert '林同' in page.locator('.transcript').inner_text()
                    assert '先回到第二季上線排程' in page.locator('.transcript').inner_text()
            if role in {'manager','observer','support'}:
                page.wait_for_timeout(300)
                body=page.locator('#panel').inner_text()
                assert '林同' not in body and '拉麵' not in body and '已隱去' not in body
            if role=='support':
                page.get_by_text('尚無有效回報').first.wait_for()
                assert page.get_by_role('button',name='內容與授權',exact=True).count()==0
            if role=='viewer':
                assert page.get_by_role('button',name='匯入事件檔',exact=True).count()==0
                assert page.get_by_role('button',name='管理授權',exact=True).count()==0
                page.get_by_role('button',name='查看內容').first.click()
                page.get_by_role('button',name='確認讀取').click()
                expect(page.locator('.transcript')).to_contain_text('先回到第二季上線排程')
            if role=='manager':
                with page.expect_download() as download:
                    page.get_by_role('button',name='匯出本頁統計 JSON').click()
                exported=json.loads(Path(download.value.path()).read_text())
                assert exported['scope']=='current_page' and '林同' not in str(exported)
                page.get_by_label('篩選政策').select_option('regulated')
                page.get_by_role('button',name='套用篩選').click()
                expect(page.locator('tbody tr')).to_have_count(1)
            if role=='operator':
                page.on('dialog',lambda dialog:dialog.accept())
                page.get_by_role('button',name='保存政策',exact=True).first.click()
                page.get_by_label('新的最長保存天數').fill('1')
                page.get_by_role('button',name='確認更新政策').click()
                expect(page.locator('.detail')).to_have_count(0)
                for tab,title in [('成員工作階段','members'),('存取稽核','audit'),('服務狀態','health'),('我的工作階段','account')]:
                    page.get_by_role('button',name=tab,exact=True).click()
                    expect(page.locator('#panel')).to_have_attribute('aria-busy','false')
                    if title=='members':
                        expect(page.locator('tbody tr')).to_have_count(6)
                        row=page.locator('tbody tr').filter(has=page.get_by_text('viewer',exact=True))
                        row.get_by_role('button',name='終止工作階段').click()
                        expect(page.locator('#panel')).to_have_attribute('aria-busy','false')
                    page.screenshot(path=str(args.output/f'{title}.png'),full_page=True)
                page.get_by_role('button',name='內容與授權',exact=True).click()
                expect(page.locator('#panel')).to_have_attribute('aria-busy','false')
            assert errors==[],errors
            assert console==[],console
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(args.output/f'{role}.png'),full_page=True)
            results[role]={'page_errors':len(errors),'console_errors':len(console),'screenshot':f'{role}.png','status':'pass'}
            if role in {'manager','operator'}:
                page.set_viewport_size({'width':390,'height':844})
                page.evaluate('window.scrollTo(0,0)')
                page.screenshot(path=str(args.output/f'{role}-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button',name='登出',exact=True).click()
            page.get_by_label('存取憑證').wait_for()
            expect(page.locator('#sidebar')).to_be_hidden()
            context.close()
        browser.close()
    (args.output/'browser-results.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results,indent=2))


if __name__=='__main__':main()
