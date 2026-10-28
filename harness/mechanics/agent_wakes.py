"""Durable wake requests from authenticated host adapters.

Provider adapters verify signatures and map sender/channel identities to an owning
principal before calling this module. It exposes no HTTP route or model tool.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid


class AgentWakeQueue:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS agent_wakes (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, owner TEXT NOT NULL,
                provider TEXT NOT NULL, connection_id TEXT NOT NULL, event_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, body TEXT NOT NULL,
                UNIQUE(agent_id,owner,provider,connection_id,event_id))''')

    def _input_allowed(self, record, definition):
        return self.store.class_allowed(definition) and self.store.inputs.validate_wake(record, definition)

    def enqueue(self, agent_id, *, owner, expected_revision, provider, connection_id, event_id,
                kind='event', sender_id=None, channel_id=None, text='', now=None, requires_input=True, source_scope=None,
                chain=None, payload=None):
        """Accept an already-authenticated, owner-bound event, never new authority."""
        now = time.time() if now is None else now
        if (not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', provider)
                or not isinstance(connection_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', connection_id)
                or not isinstance(event_id, str) or not 1 <= len(event_id) <= 200
                or any(ord(c) < 32 for c in event_id) or kind not in {'event','channel_reply','message','schedule','data','script','timer','hook'}
                or not isinstance(text, str) or len(text) > 8000 or type(requires_input) is not bool):
            raise ValueError('Use a bounded, stable event identity and message from the verified adapter.')
        for identifier in (sender_id, channel_id):
            if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 200
                    or any(ord(c) < 32 for c in identifier)):
                raise ValueError('Use the verified sender and channel identifiers.')
        if kind == 'channel_reply' and (not sender_id or not channel_id or not text.strip()):
            raise ValueError('Bind the reply text to its verified sender and conversation channel.')
        if source_scope is not None:
            try:
                bounded_scope = json.dumps(source_scope, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError):
                raise ValueError('Bind a bounded source scope to the host event.') from None
            if not isinstance(source_scope, dict) or len(bounded_scope.encode()) > 16000:
                raise ValueError('Bind a bounded source scope to the host event.')
        if chain is None:
            chain=self.store.get(agent_id).get('signal_chain')
        chain=self.store.signals.validate_chain(chain)
        data=self.store.signals.validate_payload(payload) if payload is not None else {'text':text}
        payload = {'agent_id':agent_id, 'owner_principal_id':owner, 'revision':expected_revision,
            'provider':provider, 'connection_id':connection_id, 'event_id':event_id, 'kind':kind, 'sender_id':sender_id,
            'channel_id':channel_id, 'text':text,'chain':chain,'payload':data}
        if source_scope is not None:
            payload['source_scope'] = json.loads(bounded_scope)
        if not requires_input:
            payload['requires_input'] = False
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            definition = self.store._read(db, 'agents', agent_id)
            if not owner or definition.get('owner_principal_id') != owner:
                raise ValueError('Deliver the event to its verified owning principal.')
            if not self._input_allowed(payload,definition):
                raise ValueError('Use a current production observation to wake this agent.')
            prior = db.execute('SELECT body FROM agent_wakes WHERE agent_id=? AND owner=? AND provider=? AND connection_id=? AND event_id=?',
                (agent_id, owner, provider, connection_id, event_id)).fetchone()
            if prior:
                record = json.loads(prior['body'])
                matches=record['payload_sha256']==fingerprint
                if not matches and 'chain' not in record and chain==[{'kind':'deliver'}] and data=={'text':text}:
                    legacy={key:value for key,value in payload.items() if key not in {'chain','payload'}}
                    matches=record['payload_sha256']==hashlib.sha256(json.dumps(legacy,sort_keys=True).encode()).hexdigest()
                if not matches:
                    raise ValueError('Preserve the original payload for this provider event identity.')
                return record
            if (type(expected_revision) is not int or expected_revision != definition['revision']
                    or not self.store.grant_current(definition, now=now)
                    or (not self.store.standing(definition) and definition['runs_started'] >= definition['max_runs'])):
                raise ValueError('Use the active, reviewed agent revision within its execution allowance.')
            if db.execute("SELECT COUNT(*) FROM agent_wakes WHERE status='pending'").fetchone()[0] >= 1000:
                raise ValueError('Process queued workspace events before accepting more.')
            if db.execute("SELECT COUNT(*) FROM agent_wakes WHERE agent_id=? AND status='pending'", (agent_id,)).fetchone()[0] >= 100:
                raise ValueError('Process this agent’s queued events before accepting more.')
            record = {**payload, 'id':'wake-' + uuid.uuid4().hex, 'payload_sha256':fingerprint,
                'status':'pending', 'created_at':now, 'expires_at':min(definition['expires_at'] or now+86400, now+86400),
                'run_id':None, 'completed_at':None,'chain_status':'ready' if chain==[{'kind':'deliver'}] else 'pending'}
            db.execute('INSERT INTO agent_wakes VALUES(?,?,?,?,?,?,?,?,?)',
                (record['id'],agent_id,owner,provider,connection_id,event_id,'pending',now,json.dumps(record)))
            self.store._audit(db,agent_id,'wake_queued',{'wake_id':record['id'],'provider':provider,'kind':kind})
        return record

    @staticmethod
    def _get(db, key):
        row=db.execute('SELECT body FROM agent_wakes WHERE id=?',(key,)).fetchone()
        if row is None:raise ValueError('Choose a saved workspace signal.')
        return json.loads(row['body'])

    @staticmethod
    def _write(db, record):
        db.execute('UPDATE agent_wakes SET status=?,body=? WHERE id=?',
            (record['status'], json.dumps(record), record['id']))

    def get(self, wake_id, *, owner):
        with self.store.connect() as db:
            row = db.execute('SELECT body FROM agent_wakes WHERE id=? AND owner=?', (wake_id, owner)).fetchone()
        if row is None:
            raise ValueError('Read a wake belonging to the verified principal.')
        return json.loads(row['body'])

    def pending(self, agent_id, *, owner, now=None):
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            definition = self.store._read(db, 'agents', agent_id)
            if not owner or definition.get('owner_principal_id') != owner:
                return None
            for row in db.execute("SELECT body FROM agent_wakes WHERE agent_id=? AND status='pending' ORDER BY created_at,rowid", (agent_id,)).fetchall():
                record = json.loads(row['body'])
                if record.get('target_kind')=='job' or record.get('chain_status') in {'held','uncertain'}:continue
                if not self._input_allowed(record,definition):
                    self._retire(db,record,'cancelled','observation_ineligible',now)
                    continue
                if self._eligible(record, definition, now):
                    return record
                self._retire(db, record, 'cancelled', 'execution_grant_ended', now)
        return None

    def _eligible(self, record, definition, now):
        return (record.get('target_kind')!='job' and self.store.signals.producer_current(record)
            and record['status'] == 'pending' and record['expires_at'] > now
            and self.store.grant_current(definition, now=now)
            and (self.store.standing(definition) or definition['runs_started'] < definition['max_runs'])
            and record['revision'] == definition['revision']
            and record['owner_principal_id'] == definition.get('owner_principal_id'))

    def for_claim(self, db, wake_id, definition, now):
        row = db.execute('SELECT body FROM agent_wakes WHERE id=? AND agent_id=?', (wake_id, definition['id'])).fetchone()
        if row is None:
            return None
        record = json.loads(row['body'])
        if not self._input_allowed(record,definition):
            if record['status']=='pending':self._retire(db,record,'cancelled','observation_ineligible',now)
            return None
        return record if self._eligible(record, definition, now) and record.get('chain_status','ready')=='ready' else None

    def claimed(self, db, record, run_id):
        record.update(status='claimed', run_id=run_id)
        self._write(db,record)

    def _retire(self, db, record, status, reason, now):
        record.update(status=status, reason=reason, completed_at=now)
        self._write(db,record)

    def cancel_pending(self, db, agent_id, reason, now=None):
        now = time.time() if now is None else now
        for row in db.execute("SELECT body FROM agent_wakes WHERE agent_id=? AND status='pending'", (agent_id,)).fetchall():
            record=json.loads(row['body'])
            # Producer-owned calculation authority is independent of optional agent delivery.
            if record.get('target_kind')!='job':self._retire(db,record,'cancelled',reason,now)

    def get_for_run(self, db, run_id):
        for row in db.execute("SELECT body FROM agent_wakes WHERE status='claimed'").fetchall():
            record = json.loads(row['body'])
            if record['run_id'] == run_id:
                return record
        return None

    def finish(self, db, run_id, status, now):
        for row in db.execute("SELECT body FROM agent_wakes WHERE status='claimed'").fetchall():
            record=json.loads(row['body'])
            if record['run_id'] == run_id:
                self._retire(db,record,status,'run_'+status,now)

    def enqueue_chat(self, *, chat_id, collection, owner, actor, provider, connection_id, event_id,
                     text, sender, chain=None, payload=None, expires_at=None, now=None, source_id=None):
        """Host-bound ordinary messages enter the same chain without a saved-agent definition."""
        now=time.time() if now is None else now
        for value in (chat_id,collection,owner,actor,connection_id):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',value):
                raise ValueError('Bind a signal to its actual workspace conversation and actor.')
        if (not isinstance(provider,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',provider)
                or not isinstance(event_id,str) or not 1<=len(event_id)<=200 or any(ord(c)<32 for c in event_id)
                or not isinstance(text,str) or not text.strip() or len(text)>8000
                or not isinstance(sender,dict) or sender.get('principal_id')!=actor):
            raise ValueError('Use the verified provider message and real sender.')
        chain=self.store.signals.validate_chain(chain)
        payload=self.store.signals.validate_payload(payload if payload is not None else {'text':text})
        sender=self.store.signals.validate_payload(sender)
        source={'target_kind':'chat','agent_id':chat_id,'chat_id':chat_id,'collection':collection,
            'owner_principal_id':owner,'actor_principal_id':actor,'revision':0,
            'provider':provider,'connection_id':connection_id,'event_id':event_id,'kind':'message',
            'text':text,'sender':sender,'chain':chain,'payload':payload,'source_id':source_id}
        fingerprint=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()
        record={**source,'id':'wake-'+uuid.uuid4().hex,'payload_sha256':fingerprint,'status':'pending',
            'created_at':now,'expires_at':min(expires_at or now+86400,now+86400),'run_id':None,'completed_at':None,
            'chain_status':'ready' if chain==[{'kind':'deliver'}] else 'pending'}
        if record['expires_at']<=now or not self.store.signals._current(record):
            raise ValueError('Admit the message through current conversation authority.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT body FROM agent_wakes WHERE agent_id=? AND owner=? AND provider=? AND connection_id=? AND event_id=?',
                (chat_id,owner,provider,connection_id,event_id)).fetchone()
            if prior:
                prior=json.loads(prior['body'])
                if prior['payload_sha256']!=fingerprint:raise ValueError('Preserve the original provider event payload.')
                return prior
            if db.execute("SELECT COUNT(*) FROM agent_wakes WHERE status IN ('pending','claimed')").fetchone()[0]>=1000:
                raise ValueError('Process queued workspace signals before accepting more.')
            db.execute('INSERT INTO agent_wakes VALUES(?,?,?,?,?,?,?,?,?)',
                (record['id'],chat_id,owner,provider,connection_id,event_id,'pending',now,json.dumps(record)))
        return record

    def enqueue_job(self, *, producer, producer_id, owner, actor, collection, event_id, payload,
                    source_scope, chain, destination=None, chat_id=None, kind='script', expires_at=None, now=None):
        """Persist host-authorized work whose calculation may be the complete job."""
        now=time.time() if now is None else now
        for value in (producer,producer_id,owner,actor,collection,event_id):
            if not isinstance(value,str) or not 1<=len(value)<=200 or any(ord(char)<32 for char in value):
                raise ValueError('Bind the job to its current producer, actor and workspace.')
        if kind not in {'event','channel_reply','message','schedule','data','script','timer','hook'}:
            raise ValueError('Use the reviewed producer event kind.')
        if chat_id is not None and (not isinstance(chat_id,str) or not 1<=len(chat_id)<=200):
            raise ValueError('Bind the optional producer conversation explicitly.')
        payload=self.store.signals.validate_payload(payload)
        source_scope=self.store.signals.validate_payload(source_scope)
        chain=self.store.signals.validate_chain(chain)
        if destination is not None:
            if (not isinstance(destination,dict) or set(destination)!={'agent_id','revision'}
                    or type(destination['revision']) is not int):
                raise ValueError('Select an exact saved agent revision as the job destination.')
            definition=self.store.get(destination['agent_id'])
            if (definition['owner_principal_id']!=owner or definition['dataset']!=collection
                    or definition['revision']!=destination['revision']):
                raise ValueError('Keep the job destination inside its owning workspace and revision.')
        until=now+86400 if expires_at is None else expires_at
        if type(until) not in (int,float) or not now<until<=now+86400:
            raise ValueError('Bound the original job execution authority to one day.')
        record={'target_kind':'job','producer':producer,'producer_id':producer_id,'owner_principal_id':owner,
            'actor_principal_id':actor,'collection':collection,'provider':producer,'connection_id':producer_id,
            'event_id':event_id,'agent_id':destination['agent_id'] if destination else producer_id,
            'destination':destination,'chat_id':chat_id,'revision':destination['revision'] if destination else 0,
            'kind':kind,'requires_input':False,'text':payload.get('text',''),'payload':payload,
            'source_scope':source_scope,'chain':chain}
        if not isinstance(record['text'],str) or len(record['text'])>8000:raise ValueError('Keep job text bounded.')
        fingerprint=hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()
        record.update(id='wake-'+uuid.uuid4().hex,status='pending',created_at=now,expires_at=until,
                      run_id=None,chain_status='pending',payload_sha256=fingerprint)
        if not self.store.signals._current(record):raise ValueError('Use the current host producer authority.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT body FROM agent_wakes WHERE agent_id=? AND owner=? AND provider=? AND connection_id=? AND event_id=?',
                (record['agent_id'],owner,producer,producer_id,event_id)).fetchone()
            if prior:
                saved=json.loads(prior['body'])
                if saved['payload_sha256']!=fingerprint:raise ValueError('Preserve the original producer event.')
                return saved
            if db.execute("SELECT COUNT(*) FROM agent_wakes WHERE status='pending'").fetchone()[0]>=1000:
                raise ValueError('Process the workspace pending signals first.')
            db.execute('INSERT INTO agent_wakes VALUES(?,?,?,?,?,?,?,?,?)',
                (record['id'],record['agent_id'],owner,producer,producer_id,event_id,'pending',now,json.dumps(record)))
        return record

    def signal_current(self, key):
        with self.store.connect() as db:record=self._get(db,key)
        return record['status']=='claimed' and self.store.signals._current(record)

    @staticmethod
    def chat_in_flight(db):
        """A lost admitted turn holds the workspace until provider reconciliation."""
        return any(record.get('target_kind')=='chat' and (
            record['status']=='claimed' or 'message_start' in record)
            for row in db.execute("SELECT body FROM agent_wakes WHERE status IN ('claimed','uncertain')")
            for record in (json.loads(row['body']),))

    def claim_chat(self, wake_id, *, owner, now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(db,wake_id)
            if (record.get('target_kind')!='chat' or record['owner_principal_id']!=owner
                    or record['status']!='pending' or record.get('chain_status')!='ready'
                    or not self.store.signals._current(record)):
                return None
            if (any(json.loads(row['body'])['status'] in {'running','recovering'} for row in db.execute('SELECT body FROM runs'))
                    or self.chat_in_flight(db)):
                return None
            if not self.store.signals.reserve(db,'turn',wake_id,target=record['chat_id'],now=now):return None
            record.update(status='claimed',run_id='signal-turn-'+uuid.uuid4().hex)
            self._write(db,record)
            return record

    def finish_chat(self, wake_id, claim_id, *, owner, status, admitted=True):
        if status not in {'completed','failed','cancelled','uncertain','deferred'}:
            raise ValueError('Record the signal turn outcome.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(db,wake_id)
            if (record.get('target_kind')!='chat' or record['owner_principal_id']!=owner
                    or record['run_id']!=claim_id or record['status']!='claimed'):
                raise ValueError('Finish the original claimed conversation signal.')
            if status=='deferred':
                if admitted:raise ValueError('Defer only a signal that never entered its conversation.')
                self.store.signals.release_unstarted(db,wake_id)
                record.update(status='pending',run_id=None)
                self._write(db,record)
            else:self._retire(db,record,status,'conversation_'+status,time.time())
        return record
