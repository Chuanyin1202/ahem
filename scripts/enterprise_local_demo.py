"""Seed synthetic events; credentials and KEK stay in a private local directory."""
import argparse
import base64
import json
import secrets
from pathlib import Path
from meeting_host.enterprise import Workspace
from meeting_host.enterprise_security import secure_write_text


def main():
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);args=p.parse_args()
    root=args.directory.resolve()
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
    ws.db.close()
    print('Synthetic workspace ready. Private files:',root)
    print('Service health remains unknown until an explicit health report is received.')


if __name__=='__main__':main()
