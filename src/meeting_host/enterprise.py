"""Local enterprise workspace: scoped access, encrypted records and content-free views."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from .enterprise_security import EnvelopeStore, load_kek, prepare_private_dir

ROLES = {"viewer", "operator", "manager", "observer", "support"}
PURPOSES = {"meeting_review", "incident_review"}
SERVICE_STATES = {"ok", "degraded", "unavailable", "unknown"}
COMPONENTS = {"discord", "stt", "tts", "chair"}
ACTOR = web.RequestKey("enterprise_actor", dict)


def metrics(events):
    """Allowlisted numeric projection. No topic/name/text/reason/path survives."""
    duration = 0.0
    speakers = set()
    utterances = interventions = 0
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise ValueError("Invalid event")
        t = event.get("t", 0)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= 86400:
            raise ValueError("Invalid event time")
        duration = max(duration, t)
        data = event["data"]
        if not isinstance(event.get("kind"), str):
            raise ValueError("Invalid event kind")
        for field in ("text", "speaker", "topic"):
            if field in data and not isinstance(data[field], str):
                raise ValueError("Invalid event text")
        if event.get("kind") == "meeting":
            people = data.get("participants", [])
            if not isinstance(people, list) or any(not isinstance(p, str) for p in people):
                raise ValueError("Invalid participants")
            speakers.update(people)
        if event.get("kind") == "utterance":
            utterances += 1
        if event.get("kind") == "spoken":
            interventions += 1
    return dict(duration_seconds=round(duration, 1), participants=len(speakers),
                utterances=utterances, interventions=interventions)


class Workspace:
    def __init__(self, path: Path, kek: bytes, identities: list[dict]):
        self.identities = {}
        for item in identities:
            item = dict(item)
            token = item.pop("token")
            if not isinstance(token, str) or not 32 <= len(token) <= 4096 or item.get("role") not in ROLES:
                raise ValueError("Invalid identity token or role")
            if "regulated_content" in item and type(item["regulated_content"]) is not bool:
                raise ValueError("Content clearance must be boolean")
            if not all(isinstance(item.get(k), str) and item[k] for k in ("id", "tenant")):
                raise ValueError("Identity needs id and tenant")
            digest = hashlib.sha256(token.encode()).hexdigest()
            if digest in self.identities or any(i["id"] == item["id"] for i in self.identities.values()):
                raise ValueError("Duplicate identity")
            self.identities[digest] = item
        prepare_private_dir(path.parent)
        if path.is_symlink():
            raise ValueError("Database must not be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meetings (
          id TEXT PRIMARY KEY, tenant TEXT NOT NULL, policy TEXT NOT NULL,
          expires REAL NOT NULL, aggregate TEXT NOT NULL, blob BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (meeting TEXT, actor TEXT, PRIMARY KEY(meeting, actor));
        CREATE TABLE IF NOT EXISTS audit (at REAL, tenant TEXT, actor TEXT, action TEXT, outcome TEXT);
        CREATE TABLE IF NOT EXISTS health (tenant TEXT, component TEXT, state TEXT, at REAL,
          PRIMARY KEY(tenant,component));
        CREATE TABLE IF NOT EXISTS health_history (tenant TEXT, component TEXT, state TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS disabled_members (actor TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS member_credentials (actor TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, profile TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS alert_rules (tenant TEXT, component TEXT, enabled INTEGER NOT NULL, PRIMARY KEY(tenant,component));
        CREATE TABLE IF NOT EXISTS alert_state (tenant TEXT, component TEXT, state TEXT, PRIMARY KEY(tenant,component));
        CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, tenant TEXT, component TEXT, kind TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS notification_reads (notification TEXT, actor TEXT, PRIMARY KEY(notification,actor));
        CREATE INDEX IF NOT EXISTS notifications_tenant_time ON notifications(tenant,at);
        CREATE TABLE IF NOT EXISTS meeting_imports (meeting TEXT PRIMARY KEY, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS import_receipts (tenant TEXT, source TEXT, meeting TEXT NOT NULL,
          PRIMARY KEY(tenant,source));
        CREATE TABLE IF NOT EXISTS meeting_dates (meeting TEXT PRIMARY KEY, day TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
          component TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS incidents_tenant_updated ON incidents(tenant,updated);
        CREATE INDEX IF NOT EXISTS meetings_tenant_expiry ON meetings(tenant,expires,id);
        CREATE INDEX IF NOT EXISTS meetings_expiry ON meetings(expires);
        CREATE INDEX IF NOT EXISTS audit_tenant_time ON audit(tenant,at);
        CREATE INDEX IF NOT EXISTS health_history_tenant_time ON health_history(tenant,at);
        ''')
        # Database credentials supersede static bootstrap keys after rotation.
        for row in self.db.execute('SELECT actor,digest,profile FROM member_credentials'):
            profile=json.loads(row['profile'])
            if profile['id'] != row['actor'] or profile['role'] not in ROLES:
                raise ValueError('Invalid persisted identity')
            self.identities={k:v for k,v in self.identities.items() if v['id'] != row['actor']}
            if row['digest'] in self.identities:
                raise ValueError('Duplicate credential digest')
            self.identities[row['digest']]=profile
        self.store = EnvelopeStore(kek)
        self.sessions = {}
        self.attempts = {}

    def identify(self, token):
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError("Invalid token")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for expected, identity in self.identities.items():
            if hmac.compare_digest(expected, digest):
                actor = dict(identity, _credential_digest=expected)
                if self.disabled(actor):
                    return None
                return actor
        return None

    def disabled(self, actor):
        expired=self.db.execute('SELECT expires,digest FROM member_credentials WHERE actor=?',(actor['id'],)).fetchone()
        rotated = (expired is not None and '_credential_digest' in actor
                   and not hmac.compare_digest(actor['_credential_digest'], expired['digest']))
        return rotated or (expired is not None and expired[0] <= time.time()) or self.db.execute('SELECT 1 FROM disabled_members WHERE actor=?', (actor['id'],)).fetchone() is not None

    def issue_credential(self, profile, days):
        token=secrets.token_urlsafe(32)
        digest=hashlib.sha256(token.encode()).hexdigest()
        expires=time.time()+days*86400
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO member_credentials VALUES (?,?,?,?)',
                (profile['id'],digest,json.dumps(profile),expires))
        self.identities={k:v for k,v in self.identities.items() if v['id']!=profile['id']}
        self.identities[digest]=dict(profile)
        self.sessions={k:v for k,v in self.sessions.items() if v[0]['id']!=profile['id']}
        return {'token':token,'expires_at':expires,'actor':profile['id']}

    def audit(self, actor, action, outcome):
        self.db.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (time.time(), actor["tenant"],
            hashlib.sha256(actor["id"].encode()).hexdigest()[:16], action, outcome))
        self.db.commit()

    def expire(self):
        self.evaluate_alerts()
        with self.db:
            self.db.execute("DELETE FROM grants WHERE meeting IN (SELECT id FROM meetings WHERE expires<=?)", (time.time(),))
            self.db.execute("DELETE FROM meetings WHERE expires<=?", (time.time(),))
            self.db.execute("DELETE FROM audit WHERE at<?", (time.time()-90*86400,))
            self.db.execute("DELETE FROM health_history WHERE at<?", (time.time()-30*86400,))
            self.db.execute("DELETE FROM meeting_imports WHERE meeting NOT IN (SELECT id FROM meetings)")
            self.db.execute("DELETE FROM meeting_dates WHERE meeting NOT IN (SELECT id FROM meetings)")
            self.db.execute("DELETE FROM incidents WHERE status='resolved' AND updated<?", (time.time()-90*86400,))
            self.db.execute('DELETE FROM notifications WHERE at<?',(time.time()-30*86400,))
            self.db.execute('DELETE FROM notification_reads WHERE notification NOT IN (SELECT id FROM notifications)')

    def evaluate_alerts(self):
        """Allowlisted health only; never include raw errors or meeting content."""
        now=time.time()
        rows=self.db.execute('SELECT h.* FROM health h JOIN alert_rules r ON h.tenant=r.tenant AND h.component=r.component WHERE r.enabled=1').fetchall()
        with self.db:
            for row in rows:
                state=row['state'] if now-row['at']<300 else 'unknown'
                old=self.db.execute('SELECT state FROM alert_state WHERE tenant=? AND component=?',(row['tenant'],row['component'])).fetchone()
                if old and old[0]==state:
                    continue
                self.db.execute('INSERT OR REPLACE INTO alert_state VALUES (?,?,?)',(row['tenant'],row['component'],state))
                if state=='ok' and (old is None or old[0]=='ok'):
                    continue
                kind='recovered' if state=='ok' else state
                self.db.execute('INSERT INTO notifications VALUES (?,?,?,?,?)',(secrets.token_hex(16),row['tenant'],row['component'],kind,now))
                if state!='ok':
                    incident=self.db.execute("SELECT id FROM incidents WHERE tenant=? AND component=? AND status!='resolved'",(row['tenant'],row['component'])).fetchone()
                    severity='critical' if state=='unavailable' else 'warning'
                    if not incident:
                        self.db.execute('INSERT INTO incidents VALUES (?,?,?,?,?,?,?)',(secrets.token_hex(16),row['tenant'],row['component'],severity,'open',now,now))
                    elif severity=='critical':
                        self.db.execute('UPDATE incidents SET severity=?,updated=? WHERE id=?',(severity,now,incident[0]))

    def ingest(self, actor, events, policy, days, *, source_id=None):
        if actor["role"] != "operator":
            raise web.HTTPForbidden()
        if policy not in {"team", "regulated"} or type(days) is not int or not 1 <= days <= 30:
            raise ValueError("Invalid policy or retention")
        if not isinstance(events, list) or not 1 <= len(events) <= 10000:
            raise ValueError("Expected 1..10000 events")
        aggregate = metrics(events)
        if source_id is not None and (not isinstance(source_id, str) or len(source_id) != 64
                                     or any(c not in '0123456789abcdef' for c in source_id)):
            raise ValueError('Invalid source identifier')
        mid = secrets.token_hex(16)
        blob = self.store.encrypt_text(json.dumps(events, ensure_ascii=False), meeting_id=mid, artifact_type="events")
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if source_id is not None:
                receipt = self.db.execute('SELECT meeting FROM import_receipts WHERE tenant=? AND source=?',
                                          (actor['tenant'], source_id)).fetchone()
                if receipt:
                    return receipt[0]
            self.db.execute("INSERT INTO meetings VALUES (?,?,?,?,?,?)", (mid, actor["tenant"], policy,
                time.time()+days*86400, json.dumps(aggregate), blob))
            self.db.execute("INSERT INTO meeting_imports VALUES (?,?)", (mid,time.time()))
            if source_id is not None:
                self.db.execute('INSERT INTO import_receipts VALUES (?,?,?)', (actor['tenant'],source_id,mid))
        self.audit(actor, "import", "ok")
        return mid


def create_app(ws: Workspace, origin: str, *, demo_mode: bool = False):
    if not (origin.startswith("https://") or origin.startswith("http://127.0.0.1:")):
        raise ValueError("Use HTTPS or loopback")

    async def object_body(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected object")
        if request.path != "/api/login":
            session = ws.sessions.get(request.cookies.get("enterprise", ""))
            if not session or session[1] <= time.time() or ws.disabled(session[0]):
                raise web.HTTPUnauthorized()
        return body

    def paging(request):
        limit = int(request.query.get("limit", "20"))
        offset = int(request.query.get("offset", "0"))
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000000:
            raise ValueError("Invalid page")
        return limit, offset

    @web.middleware
    async def boundary(request, handler):
        try:
            if request.method not in {"GET", "HEAD"} and request.headers.get("Origin") != origin:
                raise web.HTTPForbidden(reason="Origin rejected")
            if request.path.startswith("/api/") and request.path != "/api/login":
                session = ws.sessions.get(request.cookies.get("enterprise", ""))
                if not session or session[1] <= time.time() or ws.disabled(session[0]):
                    raise web.HTTPUnauthorized()
                request[ACTOR] = session[0]
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason}, status=exc.status)
        except (ValueError, KeyError, TypeError):
            response = web.json_response({"error": "Invalid request"}, status=400)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy":
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def login(request):
        now = time.time()
        ws.attempts = {k: v for k, v in ws.attempts.items() if v[1] > now}
        key = request.remote
        count, until = ws.attempts.get(key, (0, now+60))
        if count >= 10 or len(ws.attempts) >= 10000:
            raise web.HTTPTooManyRequests()
        ws.attempts[key] = (count+1, until)
        body = await object_body(request)
        actor = ws.identify(body["token"])
        if not actor:
            raise web.HTTPUnauthorized()
        ws.sessions = {k: v for k, v in ws.sessions.items() if v[1] > now}
        if len(ws.sessions) >= 10000:
            raise web.HTTPServiceUnavailable()
        sid = secrets.token_urlsafe(32)
        ws.sessions[sid] = (actor, now+1800)
        response = web.json_response({"role": actor["role"]})
        response.set_cookie("enterprise", sid, httponly=True, samesite="Strict",
                            secure=origin.startswith("https://"), max_age=1800, path="/")
        ws.audit(actor, "login", "ok")
        return response

    async def logout(request):
        ws.sessions.pop(request.cookies.get("enterprise"), None)
        response = web.json_response({"ok": True})
        response.del_cookie("enterprise", path="/")
        return response

    async def logout_all(request):
        actor = request[ACTOR]
        ws.sessions = {k: v for k, v in ws.sessions.items() if v[0]["id"] != actor["id"]}
        ws.audit(actor, "logout_all", "ok")
        response = web.json_response({"ok": True})
        response.del_cookie("enterprise", path="/")
        return response

    def require(request, roles):
        actor = request[ACTOR]
        if actor["role"] not in roles:
            ws.audit(actor, "access", "denied")
            raise web.HTTPForbidden()
        return actor

    def meeting(request):
        row = ws.db.execute("SELECT * FROM meetings WHERE id=? AND tenant=? AND expires>?",
            (request.match_info["mid"], request[ACTOR]["tenant"], time.time())).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        return row

    async def me(request):
        a = request[ACTOR]
        return web.json_response({"role": a["role"], "tenant": a["tenant"],
                                  "demo_mode": demo_mode,
                                  "regulated_content": a.get("regulated_content") is True,
                                  "expires_at": ws.sessions[request.cookies["enterprise"]][1]})

    async def analytics(request):
        actor = require(request, {"operator", "manager", "observer", "viewer"})
        limit, offset = paging(request)
        policy = request.query.get("policy", "all")
        query = request.query.get("q", "").strip().lower()
        if policy not in {"all", "team", "regulated"} or len(query) > 32 or any(c not in "0123456789abcdef" for c in query):
            raise ValueError("Invalid filter")
        where = "tenant=? AND expires>?"
        params = [actor["tenant"], time.time()]
        if policy != "all":
            where += " AND policy=?"; params.append(policy)
        if query:
            where += " AND id LIKE ?"; params.append(query + "%")
        if actor["role"] == "viewer":
            where += " AND id IN (SELECT meeting FROM grants WHERE actor=?)"
            params.append(actor["id"])
        summary = ws.db.execute("SELECT count(*), coalesce(sum(json_extract(aggregate,'$.duration_seconds')),0), "
            "coalesce(sum(json_extract(aggregate,'$.interventions')),0) FROM meetings WHERE " + where, params).fetchone()
        rows = ws.db.execute("SELECT id,policy,expires,aggregate FROM meetings WHERE " + where +
            " ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        items = [{"id": r["id"], "policy": r["policy"], "expires_at": r["expires"], **json.loads(r["aggregate"])} for r in rows]
        return web.json_response({"meetings": items, "count": len(items),
            "total_count": summary[0], "limit": limit, "offset": offset,
            "total_interventions": summary[2], "total_minutes": round(summary[1]/60, 1)})

    async def content(request):
        actor = require(request, {"operator", "viewer"})
        purpose = (await object_body(request)).get("purpose")
        # Read the grant and policy AFTER awaiting the body: no stale authorization.
        row = meeting(request)
        granted = actor["role"] == "operator" or ws.db.execute(
            "SELECT 1 FROM grants WHERE meeting=? AND actor=?", (row["id"], actor["id"])).fetchone()
        if not granted or purpose not in PURPOSES or (row["policy"] == "regulated" and actor.get("regulated_content") is not True):
            ws.audit(actor, "content", "denied")
            raise web.HTTPForbidden()
        text = ws.store.decrypt_text(row["blob"], meeting_id=row["id"], artifact_type="events", purpose=purpose, operator=True)
        ws.audit(actor, "content:"+purpose, "ok")
        return web.json_response({"events": json.loads(text)})

    async def access(request):
        actor = require(request, {"operator", "viewer"})
        row = meeting(request)
        granted = actor["role"] == "operator" or ws.db.execute(
            "SELECT 1 FROM grants WHERE meeting=? AND actor=?", (row["id"], actor["id"])).fetchone()
        if not granted or (row["policy"] == "regulated" and actor.get("regulated_content") is not True):
            raise web.HTTPForbidden()
        return web.json_response({"allowed": True})

    async def ingest(request):
        actor = require(request, {"operator"})
        body = await object_body(request)
        mid = ws.ingest(actor, body["events"], body.get("policy", "team"), body.get("days", 7))
        return web.json_response({"id": mid}, status=201)

    async def grants(request):
        actor = require(request, {"operator"})
        body = await object_body(request) if request.method == "POST" else None
        row = meeting(request)
        if row["policy"] == "regulated" and actor.get("regulated_content") is not True:
            raise web.HTTPForbidden()
        if request.method == "GET":
            candidates = [dict(id=a["id"], granted=bool(ws.db.execute("SELECT 1 FROM grants WHERE meeting=? AND actor=?",
                (row["id"], a["id"])).fetchone())) for a in ws.identities.values()
                if a["tenant"] == actor["tenant"] and a["role"] == "viewer"]
            return web.json_response({"viewers": candidates})
        target = next((a for a in ws.identities.values() if a["id"] == body.get("actor") and
                       a["tenant"] == actor["tenant"] and a["role"] == "viewer"), None)
        if target is None or type(body.get("allow")) is not bool:
            raise ValueError("Invalid grant")
        with ws.db:
            if body["allow"]:
                ws.db.execute("INSERT OR IGNORE INTO grants VALUES (?,?)", (row["id"], target["id"]))
            else:
                ws.db.execute("DELETE FROM grants WHERE meeting=? AND actor=?", (row["id"], target["id"]))
        ws.audit(actor, "grant" if body["allow"] else "revoke", "ok")
        return web.json_response({"ok": True})

    async def health(request):
        actor = require(request, {"operator", "support"})
        if request.method == "POST":
            require(request, {"operator"})
            body = await object_body(request)
            if body.get("component") not in COMPONENTS or body.get("state") not in SERVICE_STATES:
                raise ValueError("Use allowlisted component/state")
            with ws.db:
                previous = ws.db.execute("SELECT state FROM health WHERE tenant=? AND component=?",
                    (actor["tenant"], body["component"])).fetchone()
                if not previous or previous[0] != body["state"]:
                    ws.db.execute("INSERT INTO health_history VALUES (?,?,?,?)", (actor["tenant"], body["component"], body["state"], time.time()))
                ws.db.execute("INSERT OR REPLACE INTO health VALUES (?,?,?,?)", (actor["tenant"], body["component"], body["state"], time.time()))
            ws.evaluate_alerts()
        rows = {r["component"]: r for r in ws.db.execute("SELECT * FROM health WHERE tenant=?", (actor["tenant"],))}
        return web.json_response({"components": [dict(component=c,
            state=rows[c]["state"] if c in rows and time.time()-rows[c]["at"] < 300 else "unknown",
            updated_at=rows[c]["at"] if c in rows else None) for c in sorted(COMPONENTS)]})

    async def audit(request):
        actor = require(request, {"operator"})
        limit, offset = paging(request)
        outcome = request.query.get("outcome", "all")
        if outcome not in {"all", "ok", "denied"}:
            raise ValueError("Invalid outcome")
        where, params = "tenant=? AND at>?", [actor["tenant"], time.time()-90*86400]
        if outcome != "all":
            where += " AND outcome=?"; params.append(outcome)
        count = ws.db.execute("SELECT count(*) FROM audit WHERE " + where, params).fetchone()[0]
        return web.json_response({"entries": [dict(r) for r in ws.db.execute(
            "SELECT at,actor,action,outcome FROM audit WHERE " + where + " ORDER BY at DESC,rowid DESC LIMIT ? OFFSET ?",
            [*params, limit, offset])], "total_count": count, "limit": limit, "offset": offset})

    async def history(request):
        actor = require(request, {"operator", "support"})
        return web.json_response({"entries": [dict(r) for r in ws.db.execute(
            "SELECT component,state,at FROM health_history WHERE tenant=? AND at>? ORDER BY at DESC,rowid DESC LIMIT 100",
            (actor["tenant"], time.time()-30*86400))]})

    async def policy(request):
        actor = require(request, {"operator"})
        body = await object_body(request)
        row = meeting(request)
        target = body.get("policy", row["policy"])
        days = body.get("days")
        if target not in {"team", "regulated"} or type(days) is not int or not 1 <= days <= 30:
            raise ValueError("Invalid retention policy")
        if (row["policy"] == "regulated" or target == "regulated") and actor.get("regulated_content") is not True:
            raise web.HTTPForbidden()
        if row["policy"] == "regulated" and target != "regulated":
            raise ValueError("Restricted content cannot be downgraded")
        expires = min(row["expires"], time.time()+days*86400)
        with ws.db:
            ws.db.execute("UPDATE meetings SET policy=?,expires=? WHERE id=?", (target, expires, row["id"]))
        ws.audit(actor, "policy", "ok")
        return web.json_response({"policy": target, "expires_at": expires})

    async def members(request):
        actor = require(request, {"operator"})
        if request.method == "POST":
            body = await object_body(request)
            target = next((i for i in ws.identities.values() if i["id"] == body.get("actor") and i["tenant"] == actor["tenant"]), None)
            if not target:
                raise web.HTTPNotFound()
            if target.get("regulated_content") is True and actor.get("regulated_content") is not True:
                raise web.HTTPForbidden()
            if request.path.endswith('/status'):
                enabled = body.get('enabled')
                if type(enabled) is not bool or target['id'] == actor['id']:
                    raise ValueError('Cannot change own status')
                with ws.db:
                    if enabled:
                        ws.db.execute('DELETE FROM disabled_members WHERE actor=?',(target['id'],))
                    else:
                        ws.db.execute('INSERT OR IGNORE INTO disabled_members VALUES (?)',(target['id'],))
                ws.audit(actor, 'member_enable' if enabled else 'member_disable', 'ok')
            ws.sessions = {k: v for k, v in ws.sessions.items() if v[0]["id"] != target["id"]}
            ws.audit(actor, "revoke_sessions", "ok")
            return web.json_response({"ok": True})
        return web.json_response({"members": [dict(id=i["id"],role=i["role"],regulated_content=i.get("regulated_content") is True,
            suspended=ws.db.execute('SELECT 1 FROM disabled_members WHERE actor=?',(i['id'],)).fetchone() is not None,
            credential_expires_at=next((r[0] for r in ws.db.execute('SELECT expires FROM member_credentials WHERE actor=?',(i['id'],))),None),
            enabled=not ws.disabled(i), can_manage=i['id']!=actor['id'] and (not i.get('regulated_content') or actor.get('regulated_content') is True),
            sessions=sum(v[0]["id"] == i["id"] and v[1] > time.time() for v in ws.sessions.values()))
            for i in ws.identities.values() if i["tenant"] == actor["tenant"]]})

    async def credentials(request):
        actor=require(request, {'operator'})
        body=await object_body(request)
        days=body.get('days',7)
        if type(days) is not int or not 1<=days<=30:
            raise ValueError('Expected 1..30 days')
        if request.path.endswith('/create'):
            name=body.get('actor')
            if not isinstance(name,str) or not 3<=len(name)<=48 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in name):
                raise ValueError('Use a non-sensitive lowercase member code')
            if body.get('role') not in {'viewer','manager','observer','support'} or body.get('regulated_content'):
                raise web.HTTPForbidden()
            if any(i['id']==name for i in ws.identities.values()):
                raise web.HTTPConflict(reason='Member code unavailable')
            if len(ws.identities)>=1000:
                raise web.HTTPConflict(reason='Local workspace identity limit reached')
            target={'id':name,'tenant':actor['tenant'],'role':body['role'],'regulated_content':False}
            action='member_create'
        else:
            target=next((i for i in ws.identities.values() if i['id']==body.get('actor') and i['tenant']==actor['tenant']),None)
            if target is None:
                raise web.HTTPNotFound()
            if target['id']==actor['id'] or (target.get('regulated_content') and not actor.get('regulated_content')):
                raise web.HTTPForbidden()
            action='credential_rotate'
        result=ws.issue_credential(target,days)
        ws.audit(actor,action,'ok')
        # Plaintext returned once; only a digest is persisted. Never log this response.
        return web.json_response(result)

    async def trends(request):
        actor = require(request, {'operator','manager','observer'})
        rows=ws.db.execute("""SELECT date(i.at,'unixepoch') AS day,count(*) AS meetings,
          round(sum(json_extract(m.aggregate,'$.duration_seconds'))/60.0,1) AS minutes,
          sum(json_extract(m.aggregate,'$.interventions')) AS interventions
          FROM meetings m JOIN meeting_imports i ON i.meeting=m.id
          WHERE m.tenant=? AND m.expires>? AND i.at>? GROUP BY day ORDER BY day""",
          (actor['tenant'],time.time(),time.time()-30*86400))
        return web.json_response({'days':[dict(r) for r in rows], 'basis':'import_time_utc',
          'unknown_import_dates':ws.db.execute('SELECT count(*) FROM meetings m LEFT JOIN meeting_imports i ON i.meeting=m.id WHERE m.tenant=? AND m.expires>? AND i.meeting IS NULL', (actor['tenant'],time.time())).fetchone()[0]})

    async def meeting_date(request):
        actor=require(request, {'operator'})
        body=await object_body(request)
        row=meeting(request)
        if row['policy']=='regulated' and not actor.get('regulated_content'):
            raise web.HTTPForbidden()
        day=body.get('day')
        if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day or date.fromisoformat(day)>datetime.now(timezone.utc).date():
            raise ValueError('Expected a non-future YYYY-MM-DD meeting date')
        with ws.db:
            ws.db.execute('INSERT OR REPLACE INTO meeting_dates VALUES (?,?)',(row['id'],day))
        ws.audit(actor,'meeting_date_update','ok')
        return web.json_response({'day':day,'source':'operator_entered'})

    async def meeting_trends(request):
        actor=require(request, {'operator','manager','observer'})
        days=int(request.query.get('days','30'))
        if days not in {30,90,365}:
            raise ValueError('Unsupported date window')
        cutoff=(datetime.now(timezone.utc).date()-timedelta(days=days-1)).isoformat()
        rows=ws.db.execute("""SELECT d.day,count(*) AS meetings,
          round(avg(json_extract(m.aggregate,'$.duration_seconds'))/60.0,2) AS average_minutes,
          sum(json_extract(m.aggregate,'$.interventions')) AS interventions,
          round(600.0*sum(json_extract(m.aggregate,'$.interventions'))/nullif(sum(json_extract(m.aggregate,'$.duration_seconds')),0),2) AS interventions_per_10_minutes
          FROM meetings m JOIN meeting_dates d ON d.meeting=m.id WHERE m.tenant=? AND m.expires>? AND d.day>=? GROUP BY d.day ORDER BY d.day""",(actor['tenant'],time.time(),cutoff))
        missing=ws.db.execute('SELECT count(*) FROM meetings m LEFT JOIN meeting_dates d ON d.meeting=m.id WHERE m.tenant=? AND m.expires>? AND d.meeting IS NULL',(actor['tenant'],time.time())).fetchone()[0]
        return web.json_response({'days':[dict(r) for r in rows],'window_days':days,'missing_dates':missing,'source':'operator_entered','retained_only':True})

    async def incidents(request):
        actor=require(request, {'operator','support'})
        if request.method=='POST':
            body=await object_body(request)
            now=time.time()
            if 'iid' in request.match_info:
                row=ws.db.execute('SELECT * FROM incidents WHERE id=? AND tenant=?',(request.match_info['iid'],actor['tenant'])).fetchone()
                if row is None:
                    raise web.HTTPNotFound()
                target=body.get('status')
                expected={'open':'acknowledged','acknowledged':'resolved'}.get(row['status'])
                if target != expected or expected is None:
                    raise ValueError('Invalid incident transition')
                with ws.db:
                    ws.db.execute('UPDATE incidents SET status=?,updated=? WHERE id=?',(target,now,row['id']))
                ws.audit(actor,'incident_'+target,'ok')
            else:
                if body.get('component') not in COMPONENTS or body.get('severity') not in {'warning','critical'}:
                    raise ValueError('Invalid incident')
                # No free text: support must never receive raw customer content.
                with ws.db:
                    if ws.db.execute("SELECT 1 FROM incidents WHERE tenant=? AND component=? AND status!='resolved'",(actor['tenant'],body['component'])).fetchone():
                        raise web.HTTPConflict(reason='Component already has an open incident')
                    ws.db.execute('INSERT INTO incidents VALUES (?,?,?,?,?,?,?)', (secrets.token_hex(16),actor['tenant'],body['component'],body['severity'],'open',now,now))
                ws.audit(actor,'incident_create','ok')
        limit,offset=paging(request)
        rows=ws.db.execute('SELECT id,component,severity,status,created,updated FROM incidents WHERE tenant=? ORDER BY updated DESC,id LIMIT ? OFFSET ?', (actor['tenant'],limit,offset))
        return web.json_response({'entries':[dict(r) for r in rows], 'limit':limit,'offset':offset,
          'total_count':ws.db.execute('SELECT count(*) FROM incidents WHERE tenant=?',(actor['tenant'],)).fetchone()[0]})

    async def alert_rules(request):
        actor=require(request, {'operator'})
        if request.method=='POST':
            body=await object_body(request)
            if body.get('component') not in COMPONENTS or type(body.get('enabled')) is not bool:
                raise ValueError('Invalid alert rule')
            with ws.db:
                ws.db.execute('INSERT OR REPLACE INTO alert_rules VALUES (?,?,?)',(actor['tenant'],body['component'],int(body['enabled'])))
                if not body['enabled']:
                    ws.db.execute('DELETE FROM alert_state WHERE tenant=? AND component=?',(actor['tenant'],body['component']))
            ws.audit(actor,'alert_rule_update','ok')
            ws.evaluate_alerts()
        rules={r['component']:bool(r['enabled']) for r in ws.db.execute('SELECT * FROM alert_rules WHERE tenant=?',(actor['tenant'],))}
        return web.json_response({'rules':[{'component':c,'enabled':rules.get(c,False)} for c in sorted(COMPONENTS)]})

    async def notifications(request):
        actor=require(request, {'operator','support'})
        if request.method=='POST':
            await object_body(request)
            row=ws.db.execute('SELECT id FROM notifications WHERE id=? AND tenant=?',(request.match_info['nid'],actor['tenant'])).fetchone()
            if row is None:
                raise web.HTTPNotFound()
            with ws.db:
                ws.db.execute('INSERT OR IGNORE INTO notification_reads VALUES (?,?)',(row[0],actor['id']))
            return web.json_response({'ok':True})
        limit,offset=paging(request)
        cutoff=time.time()-30*86400
        rows=ws.db.execute('SELECT n.id,n.component,n.kind,n.at,r.actor IS NOT NULL AS is_read FROM notifications n LEFT JOIN notification_reads r ON r.notification=n.id AND r.actor=? WHERE n.tenant=? AND n.at>? ORDER BY n.at DESC,n.id LIMIT ? OFFSET ?', (actor['id'],actor['tenant'],cutoff,limit,offset))
        return web.json_response({'entries':[dict(r) for r in rows],'limit':limit,'offset':offset,
            'total_count':ws.db.execute('SELECT count(*) FROM notifications WHERE tenant=? AND at>?',(actor['tenant'],cutoff)).fetchone()[0]})

    async def purge(request):
        actor = require(request, {"operator"})
        row = meeting(request)
        with ws.db:
            ws.db.execute("DELETE FROM grants WHERE meeting=?", (row["id"],))
            ws.db.execute("DELETE FROM meetings WHERE id=?", (row["id"],))
        ws.audit(actor, "delete", "ok")
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[boundary], client_max_size=4*1024*1024)
    app.add_routes([web.post('/api/login', login), web.post('/api/logout', logout), web.post('/api/logout-all', logout_all), web.get('/api/me', me),
        web.get('/api/analytics', analytics), web.post('/api/meetings', ingest),
        web.post('/api/meetings/{mid}/content', content), web.get('/api/meetings/{mid}/grants', grants),
        web.get('/api/meetings/{mid}/access', access),
        web.post('/api/meetings/{mid}/grants', grants), web.delete('/api/meetings/{mid}', purge),
        web.post('/api/meetings/{mid}/policy', policy), web.get('/api/members', members), web.post('/api/members/revoke-sessions', members),
        web.get('/api/health/history', history),
        web.post('/api/members/status', members), web.get('/api/trends',trends),
        web.post('/api/members/create',credentials), web.post('/api/members/rotate',credentials),
        web.get('/api/alert-rules',alert_rules), web.post('/api/alert-rules',alert_rules),
        web.post('/api/meetings/{mid}/date',meeting_date), web.get('/api/meeting-trends',meeting_trends),
        web.get('/api/notifications',notifications), web.post('/api/notifications/{nid}/read',notifications),
        web.get('/api/incidents',incidents), web.post('/api/incidents',incidents), web.post('/api/incidents/{iid}',incidents),
        web.get('/api/health', health), web.post('/api/health', health), web.get('/api/audit', audit)])
    async def index(request):
        return web.FileResponse(Path(__file__).parent/'enterprise_ui'/'index.html')
    app.router.add_get('/', index)
    app.router.add_static('/ui/', Path(__file__).parent/'enterprise_ui')
    async def retention(app):
        import asyncio
        async def sweep():
            while True:
                ws.expire()
                await asyncio.sleep(60)
        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ws.db.close()
    app.cleanup_ctx.append(retention)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--identities', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8890)
    parser.add_argument('--demo-mode',action='store_true',help='Show explicit synthetic health-report controls; authorization is unchanged')
    args = parser.parse_args()
    if args.identities.is_symlink() or args.identities.stat().st_mode & 0o077:
        raise ValueError('Identities file must be private (0600)')
    ws = Workspace(args.database, load_kek(), json.loads(args.identities.read_text()))
    web.run_app(create_app(ws, f'http://127.0.0.1:{args.port}',demo_mode=args.demo_mode), host='127.0.0.1', port=args.port,
                access_log=None)


if __name__ == '__main__':
    main()
