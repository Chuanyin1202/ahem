"""Seed synthetic events; credentials and KEK stay in a private local directory."""
import argparse
import base64
import json
import secrets
from pathlib import Path
from datetime import datetime, timedelta, timezone
from meeting_host.enterprise import Workspace
from meeting_host.enterprise_security import secure_write_text


def seed(root, days=5, port=8910):
    if type(days) is not int or not 1 <= days <= 30 or not 1024 <= port <= 65535:
        raise ValueError('Invalid credential duration or port')
    root=Path(root).resolve()
    if root.exists():
        raise SystemExit('Use a new directory to avoid overwriting credentials')
    key=secrets.token_bytes(32)
    identities=[dict(id=role,tenant='示範組織',role=role,token=secrets.token_urlsafe(32))
                for role in ['operator','viewer','manager','observer','support']]
    identities.append(dict(id='content-officer',tenant='示範組織',role='operator',regulated_content=True,token=secrets.token_urlsafe(32)))
    secure_write_text(root/'identities.json',json.dumps(identities))
    secure_write_text(root/'kek',base64.b64encode(key).decode())
    ws=Workspace(root/'enterprise.db',key,identities)
    source=Path(__file__).resolve().parents[1]/'examples/synthetic-meeting.events.jsonl'
    events=[json.loads(l) for l in source.read_text().splitlines() if l.strip()]
    for policy in ['team','regulated']:
        mid=ws.ingest(identities[0],events,policy,7)
        if policy=='team':
            ws.db.execute('INSERT INTO grants VALUES (?,?)',(mid,'viewer'));ws.db.commit()
    issued=[]
    for identity in identities:
        profile={k:v for k,v in identity.items() if k!='token'}
        credential=ws.issue_credential(profile,days)
        identity['token']=credential['token']
        issued.append({'actor':identity['id'],'role':identity['role'],
                       'expires_at':credential['expires_at']})
    secure_write_text(root/'identities.json',json.dumps(identities))
    url=f'http://127.0.0.1:{port}/'
    cards=['# Ahem demo 私有登入卡', '', f'登入頁：{url}', '',
           f'憑證自建立起 {days} 天有效；到期後伺服器拒絕登入及既有工作階段。',
           '只供本機合成資料 demo。不要上傳 GitHub／貼到公開 PR。', '']
    for identity, item in zip(identities, issued):
        expires=datetime.fromtimestamp(item['expires_at'],timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        cards.extend([f"## {identity['id']}", '', f"到期（台灣）：{expires}", '',
                      f"存取憑證：`{identity['token']}`", ''])
    secure_write_text(root/'demo-login-cards.md','\n'.join(cards))
    secure_write_text(root/'credential-manifest.json',json.dumps({'days':days,'url':url,'roles':issued},indent=2))
    ws.db.close()
    print('Synthetic workspace ready. Private files:',root)
    print('Service health remains unknown until an explicit health report is received.')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--days',type=int,choices=range(1,31),default=5)
    p.add_argument('--port',type=int,default=8910,help='Login card URL port; start server separately')
    args=p.parse_args()
    seed(args.directory,args.days,args.port)


if __name__=='__main__':main()
