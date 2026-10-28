"""Reviewed timers and signed webhooks feeding the workspace's one signal ledger."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid

from signal_chain import bounded


class SignalSources:
    def __init__(self,store,*,public_origin='',environment='demo'):
        self.store=store;self.public_origin=public_origin.rstrip('/');self.environment=environment
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS signal_sources (
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS signal_ingress (
                source_id TEXT NOT NULL,event_id TEXT NOT NULL,digest TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(source_id,event_id));''')

    @staticmethod
    def _write(db,record):
        db.execute('UPDATE signal_sources SET status=?,body=? WHERE id=?',
            (record['status'],json.dumps(record),record['id']))

    @staticmethod
    def _get(db,key):
        row=db.execute('SELECT body FROM signal_sources WHERE id=?',(key,)).fetchone()
        if row is None:raise ValueError('Choose a current workspace wake source.')
        return json.loads(row['body'])

    def get(self,key):
        with self.store.connect() as db:return self._get(db,key)

    def current(self,record,*,now=None):
        now=time.time() if now is None else now
        if record['status']!='active' or record['authority_until']<=now:return False
        check=self.store.signal_authority
        view={**record,'target_kind':'chat','source_id':None,'expires_at':record['authority_until']}
        try:return callable(check) and check(view) is True
        except (ValueError,OSError):return False

    def _create(self,kind,*,owner,actor,chat_id,collection,label,chain=None,authority_until=None,now=None,**fields):
        now=time.time() if now is None else now
        if not isinstance(label,str) or not 1<=len(label.strip())<=80:
            raise ValueError('Give this wake a short recognizable label.')
        record={'id':kind+'-'+uuid.uuid4().hex,'kind':kind,'status':'active','owner_principal_id':owner,
            'actor_principal_id':actor,'chat_id':chat_id,'collection':collection,'label':label.strip(),
            'created_at':now,'authority_until':min(authority_until or now+7*86400,now+30*86400),
            'chain':self.store.signals.reviewed_chain(chain),**fields}
        if not self.current(record,now=now):raise ValueError('Create this wake from the current conversation authority.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active=[json.loads(row['body']) for row in db.execute("SELECT body FROM signal_sources WHERE status='active'")]
            active=[row for row in active if row['authority_until']>now]
            if len(active)>=200 or sum(row['chat_id']==chat_id for row in active)>=10:
                raise ValueError('Review this conversation’s existing wake sources first.')
            db.execute('INSERT INTO signal_sources VALUES(?,?,?,?)',(record['id'],kind,'active',json.dumps(record)))
        return self.public(record)

    def create_hook(self,*,days=7,signature='hmac_sha256',authority_until=None,**identity):
        now=identity.get('now') or time.time()
        if type(days) is not int or not 1<=days<=30 or signature not in {'hmac_sha256','bearer'}:
            raise ValueError('Choose a webhook grant of one to thirty days and its signature scheme.')
        return self._create('hook',**identity,authority_until=min(authority_until or now+days*86400,now+days*86400),
            signature=signature,secret=secrets.token_urlsafe(32),revealed_at=None)

    def set_timer(self,*,note,delay_seconds=60,every_seconds=None,fires=1,authority_until=None,**identity):
        now=identity.get('now') or time.time()
        if (not isinstance(note,str) or not note.strip() or len(note)>8000
                or type(delay_seconds) is not int or not 60<=delay_seconds<=604800
                or every_seconds is not None and (type(every_seconds) is not int or not 300<=every_seconds<=604800)
                or type(fires) is not int or not 1<=fires<=50 or every_seconds is None and fires!=1):
            raise ValueError('Set a bounded wake time, note and repeat count.')
        until=min(authority_until or now+7*86400,now+7*86400)
        if now+delay_seconds>=until:raise ValueError('Set the wake inside its original authority period.')
        return self._create('timer',**identity,authority_until=until,note=note,next_at=now+delay_seconds,
            every_seconds=every_seconds,fires_left=fires,fired=0)

    def public(self,record):
        result={key:value for key,value in record.items() if key not in {'secret','revealed_at'}}
        if record['kind']=='hook':
            result.update(secret_available=record['revealed_at'] is None and record['status']=='active' and record['authority_until']>time.time(),
                url=f"{self.public_origin}/hooks/in/{self.environment}/{record['id']}",
                signature_header='X-Hub-Signature-256' if record['signature']=='hmac_sha256' else 'Authorization')
        return result

    def list(self,*,actor,chat_id=None,collection=None):
        with self.store.connect() as db:
            records=[json.loads(row['body']) for row in db.execute('SELECT body FROM signal_sources ORDER BY rowid DESC')]
        return [self.public(record) for record in records if record['actor_principal_id']==actor
            and (chat_id is None or record['chat_id']==chat_id) and (collection is None or record['collection']==collection)]

    def reveal(self,key,*,actor,chat_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self._get(db,key)
            if (record['actor_principal_id']!=actor or record['chat_id']!=chat_id or record['kind']!='hook'
                    or record['revealed_at'] is not None or not self.current(record)):
                raise ValueError('Copy this active conversation webhook secret once.')
            record['revealed_at']=time.time();self._write(db,record)
            return record['secret']

    def cancel(self,key,*,actor,chat_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self._get(db,key)
            if record['actor_principal_id']!=actor or record['chat_id']!=chat_id:
                raise ValueError('Cancel a wake belonging to this conversation’s actor.')
            record['status']='revoked';self._write(db,record)
            for row in db.execute("SELECT body FROM agent_wakes WHERE connection_id=? AND status='pending'",(key,)).fetchall():
                self.store.wakes._retire(db,json.loads(row['body']),'cancelled','source_revoked',time.time())
        return self.public(record)

    def _deliver(self,record,event_id,text,payload,*,now=None):
        now=time.time() if now is None else now
        channel='webhook' if record['kind']=='hook' else 'timer'
        admission_key=channel+':'+hashlib.sha256((record['id']+'\0'+event_id).encode()).hexdigest()
        return self.store.wakes.enqueue_chat(chat_id=record['chat_id'],collection=record['collection'],
            owner=record['owner_principal_id'],actor=record['actor_principal_id'],provider=channel,
            connection_id=record['id'],event_id=event_id,text=text,payload=payload,chain=record['chain'],
            expires_at=min(record['authority_until'],now+86400),now=now,source_id=record['id'],
            sender={'principal_id':record['actor_principal_id'],'channel':channel,'label':record['label'],
                'admission_key':admission_key,'provider_event_id':event_id,'wake_source_id':record['id'],
                'authority_until':record['authority_until']})

    def receive(self,key,raw,signature,*,event_id=None,now=None):
        now=time.time() if now is None else now
        if not isinstance(raw,bytes) or len(raw)>65536:raise ValueError('Use a webhook body within 64 KB.')
        record=self.get(key)
        if record['kind']!='hook' or not self.current(record,now=now):raise ValueError('Use a current workspace webhook.')
        expected=('sha256='+hmac.new(record['secret'].encode(),raw,hashlib.sha256).hexdigest()
            if record['signature']=='hmac_sha256' else 'Bearer '+record['secret'])
        if not isinstance(signature,str) or not hmac.compare_digest(signature,expected):
            raise PermissionError('Verify the webhook signature.')
        # Only a verified bounded body is parsed.
        payload=bounded(json.loads(raw))
        if not isinstance(payload,dict):raise ValueError('Send an object as the webhook event.')
        event_id=event_id or payload.get('id') or payload.get('event_id') or hashlib.sha256(raw).hexdigest()
        if not isinstance(event_id,str) or not 1<=len(event_id)<=200 or any(ord(c)<32 for c in event_id):
            raise ValueError('Use a stable bounded webhook event identity.')
        text=payload.get('text') or json.dumps(payload,ensure_ascii=False)
        if not isinstance(text,str) or not text.strip() or len(text)>8000:raise ValueError('Keep webhook text within 8000 characters.')
        digest=hashlib.sha256(raw).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT digest FROM signal_ingress WHERE source_id=? AND event_id=?',(key,event_id)).fetchone()
            if prior and prior['digest']!=digest:raise ValueError('Preserve the original webhook event payload.')
            if not prior:
                count=db.execute('SELECT COUNT(*) FROM signal_ingress WHERE source_id=? AND created_at>=?',
                    (key,int(now//86400)*86400)).fetchone()[0]
                pending=db.execute("SELECT COUNT(*) FROM agent_wakes WHERE connection_id=? AND status='pending'",(key,)).fetchone()[0]
                if count>=1000 or pending>=100:raise ValueError('Process this webhook’s bounded daily and pending events.')
                db.execute('INSERT INTO signal_ingress VALUES(?,?,?,?)',(key,event_id,digest,now))
        signal=self._deliver(record,event_id,text,payload,now=now)
        return {'id':signal['id'],'status':'duplicate' if prior else 'accepted'}

    def tick(self,*,now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            records=[json.loads(row['body']) for row in db.execute("SELECT body FROM signal_sources WHERE kind='timer' AND status='active'")]
        for record in records:
            if record['next_at']>now:continue
            if not self.current(record,now=now) or record['next_at']<now-86400:
                with self.store.connect() as db:
                    record['status']='expired';self._write(db,record)
                continue
            self._deliver(record,str(record['fired']),record['note'],{'text':record['note'],'scheduled_at':record['next_at']},now=now)
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');latest=self._get(db,record['id'])
                if latest['fired']!=record['fired'] or latest['status']!='active':continue
                latest['fired']+=1;latest['fires_left']-=1
                if latest['fires_left']==0:latest['status']='done'
                else:latest['next_at']=now+latest['every_seconds']
                self._write(db,latest)
