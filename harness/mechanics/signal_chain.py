"""One workspace signal ledger: bounded chains, daily budgets and effect receipts.

AgentWakeQueue owns ingress rows in the same background-agents.sqlite transaction.
A step receipt is persisted before any external work; an interrupted effect is
held for reconciliation, never assumed to be safe to repeat.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time

from jsonschema import Draft202012Validator

DAILY_LIMITS = {'turn':48, 'screen':500, 'send':200}
MAX_STEPS = 8
MAX_BYTES = 65536

CLAUSE_SCHEMA={'type':'object','properties':{'field':{'type':'string','minLength':1,'maxLength':120},
    'op':{'type':'string','enum':['eq','ne','contains','gt','gte','lt','lte']},'value':{}},
    'required':['field','op','value'],'additionalProperties':False}
CHAIN_SCHEMA={'type':'array','minItems':1,'maxItems':MAX_STEPS,'items':{'anyOf':[
    {'type':'object','properties':{'kind':{'const':'deliver'}},'required':['kind'],'additionalProperties':False},
    {'type':'object','properties':{'kind':{'const':'rules'},
        'all':{'type':'array','maxItems':8,'items':CLAUSE_SCHEMA},'any':{'type':'array','maxItems':8,'items':CLAUSE_SCHEMA}},
        'required':['kind'],'additionalProperties':False},
    {'type':'object','properties':{'kind':{'type':'string','enum':['script','screen','effect']},
        'handler':{'type':'string','pattern':'^[A-Za-z_][A-Za-z0-9_]{0,79}$'},'config':{'type':'object'}},
        'required':['kind','handler'],'additionalProperties':False}]}}


def bounded(value):
    encoded=json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))
    if len(encoded.encode())>MAX_BYTES:
        raise ValueError('Keep each signal and step output within 64 KB.')
    return json.loads(encoded)


class StepHeld(ValueError):
    """A host prerequisite prevented any external request from starting."""


class SignalLedger:
    def __init__(self, store):
        self.store=store
        self.handlers={}
        self.producers={}
        self.running=set()
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS signal_usage (
                    kind TEXT NOT NULL, reservation TEXT NOT NULL, day INTEGER NOT NULL,
                    target TEXT NOT NULL, amount INTEGER NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(kind,reservation));
                CREATE INDEX IF NOT EXISTS signal_usage_day ON signal_usage(day,kind,target);
                CREATE TABLE IF NOT EXISTS signal_steps (
                    signal_id TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(signal_id,position));
            ''')

    def recover(self, db):
        for row in db.execute("SELECT body FROM signal_steps WHERE status='started'").fetchall():
            receipt=json.loads(row['body'])
            receipt.update(status='uncertain',error='Reconcile the original step after restart.')
            db.execute('UPDATE signal_steps SET status=?,body=? WHERE signal_id=? AND position=?',
                ('uncertain',json.dumps(receipt),receipt['signal_id'],receipt['position']))
            signal=self.store.wakes._get(db,receipt['signal_id'])
            signal['chain_status']='uncertain'
            self.store.wakes._write(db,signal)

    def reserve(self, db, kind, reservation, *, target='', amount=1, target_limit=None, now=None):
        now=time.time() if now is None else now
        if kind not in DAILY_LIMITS or type(amount) is not int or not 1<=amount<=DAILY_LIMITS[kind]:
            raise ValueError('Reserve a bounded declared signal resource.')
        prior=db.execute('SELECT * FROM signal_usage WHERE kind=? AND reservation=?',(kind,reservation)).fetchone()
        if prior:
            if prior['target']!=target or prior['amount']!=amount:
                raise ValueError('Preserve the original signal resource reservation.')
            return True
        day=int(now//86400)
        count=db.execute('SELECT COALESCE(SUM(amount),0) FROM signal_usage WHERE kind=? AND day=?',(kind,day)).fetchone()[0]
        if count+amount>DAILY_LIMITS[kind]:return False
        if target_limit is not None:
            count=db.execute('SELECT COALESCE(SUM(amount),0) FROM signal_usage WHERE kind=? AND day=? AND target=?',
                (kind,day,target)).fetchone()[0]
            if count+amount>target_limit:return False
        db.execute('INSERT INTO signal_usage VALUES(?,?,?,?,?,?)',(kind,reservation,day,target,amount,now))
        return True

    def release_unstarted(self, db, reservation):
        db.execute("DELETE FROM signal_usage WHERE kind='turn' AND reservation=?",(reservation,))

    def usage(self, *, now=None):
        day=int((time.time() if now is None else now)//86400)
        with self.store.connect() as db:
            counts={row['kind']:row['amount'] for row in db.execute(
                'SELECT kind,SUM(amount) amount FROM signal_usage WHERE day=? GROUP BY kind',(day,))}
        return {kind:{'used':counts.get(kind,0),'limit':limit} for kind,limit in DAILY_LIMITS.items()}

    @staticmethod
    def validate_payload(payload):
        value=bounded(payload)
        if not isinstance(value,dict):raise ValueError('Supply bounded signal object data.')
        return value

    @staticmethod
    def validate_chain(chain):
        if chain is None:chain=[{'kind':'deliver'}]
        chain=bounded(chain)
        if not isinstance(chain,list) or not 1<=len(chain)<=MAX_STEPS:
            raise ValueError('Use one to eight explicit signal steps.')
        for position,step in enumerate(chain):
            if not isinstance(step,dict) or step.get('kind') not in {'rules','script','screen','effect','deliver'}:
                raise ValueError('Choose a registered signal step.')
            if step['kind']=='deliver':
                if position!=len(chain)-1 or set(step)-{'kind'}:
                    raise ValueError('Finish the signal chain with its reviewed destination.')
            elif step['kind']=='rules':
                if set(step)-{'kind','all','any'} or not (step.get('all') or step.get('any')):
                    raise ValueError('Supply bounded signal rule clauses.')
                for group in ('all','any'):
                    clauses=step.get(group,[])
                    if not isinstance(clauses,list) or len(clauses)>8:
                        raise ValueError('Use at most eight clauses in a rule group.')
                    for clause in clauses:
                        if (not isinstance(clause,dict) or set(clause)!={'field','op','value'}
                                or clause['op'] not in {'eq','ne','contains','gt','gte','lt','lte'}
                                or not isinstance(clause['field'],str) or len(clause['field'])>120
                                or any(not part.isidentifier() for part in clause['field'].split('.'))):
                            raise ValueError('Use explicit bounded signal field comparisons.')
            else:
                if (set(step)-{'kind','handler','config'} or not isinstance(step.get('handler'),str)
                        or not step['handler'].isidentifier() or not isinstance(step.get('config',{}),dict)):
                    raise ValueError('Use a host-registered step and bounded configuration.')
        if chain[-1]['kind']!='deliver':raise ValueError('Finish the signal chain with delivery.')
        return chain

    def register(self, name, handler, *, kind, timeout_seconds=30, cleanup_seconds=30,
                 cancel=None, reconcile=None, metered=True, config_schema=None, description=''):
        """Install a host adapter, including its cleanup and reconciliation contract.

        run(payload, config, context) returns payload/text/deliver. Call
        context['external'] with the stable request identity before external work.
        cancel(context) returns a cleanup receipt, with quiescent=True only after
        provider cleanup. reconcile(context) reads that original identity and
        returns status succeeded/failed/uncertain, optional output and evidence.
        A reconciliation callback never starts a replacement operation.
        """
        if (not isinstance(name,str) or name in self.handlers or not name.isidentifier() or not callable(handler)
                or kind not in {'script','screen','effect'} or type(metered) is not bool
                or any(type(value) not in (int,float) or not math.isfinite(value) or not 0<value<=600
                       for value in (timeout_seconds,cleanup_seconds))
                or any(value is not None and not callable(value) for value in (cancel,reconcile))):
            raise ValueError('Install each bounded host signal handler once.')
        if config_schema is not None:
            if not isinstance(config_schema,dict) or config_schema.get('type')!='object':
                raise ValueError('Declare bounded step configuration fields.')
            Draft202012Validator.check_schema(config_schema)
        if not isinstance(description,str) or len(description)>500:raise ValueError('Describe the registered step concisely.')
        self.handlers[name]={'kind':kind,'run':handler,'timeout':timeout_seconds,'cleanup_seconds':cleanup_seconds,
                             'cancel':cancel,'reconcile':reconcile,'metered':metered,
                             'config_schema':copy.deepcopy(config_schema),'description':description}

    def declarations(self):
        return [{'handler':name,'kind':item['kind'],'description':item['description'],
                 'config_schema':copy.deepcopy(item['config_schema'])}
                for name,item in self.handlers.items() if item['config_schema'] is not None]

    def reviewed_chain(self, chain):
        """Person-facing chain selection uses explicit host declarations only."""
        chain=self.validate_chain(chain)
        for step in chain:
            if step['kind'] in {'rules','deliver'}:continue
            item=self.handlers.get(step['handler'])
            if (item is None or item['kind']!=step['kind'] or item['config_schema'] is None
                    or not Draft202012Validator(item['config_schema']).is_valid(step.get('config',{}))):
                raise ValueError('Choose a declared signal step with its reviewed configuration fields.')
        return chain

    def register_producer(self, name, authorized):
        if not isinstance(name,str) or not name.isidentifier() or name in self.producers or not callable(authorized):
            raise ValueError('Install each explicit signal producer authority once.')
        self.producers[name]=authorized

    @staticmethod
    def _rules(step, payload):
        def matches(clause):
            value=payload
            for part in clause['field'].split('.'):
                if not isinstance(value,dict) or part not in value:return False
                value=value[part]
            other=clause['value'];op=clause['op']
            if op=='eq':return value==other
            if op=='ne':return value!=other
            if op=='contains':return (isinstance(value,str) and isinstance(other,str) or isinstance(value,list)) and other in value
            if type(value) not in (int,float) or type(other) not in (int,float):return False
            return {'gt':value>other,'gte':value>=other,'lt':value<other,'lte':value<=other}[op]
        return all(matches(clause) for clause in step.get('all',[])) and (
            not step.get('any') or any(matches(clause) for clause in step['any']))

    def producer_current(self, record):
        producer=record.get('producer')
        if not producer:return True
        check=self.producers.get(producer)
        try:return check is not None and check(copy.deepcopy(record)) is True
        except (ValueError,OSError):return False

    def _current(self, record):
        if record.get('expires_at',0)<=time.time() or not self.producer_current(record):return False
        producer=record.get('producer')
        if record.get('target_kind')=='job':return producer is not None
        if record.get('target_kind','agent')=='agent':
            definition=self.store.get(record['agent_id'])
            return (self.store.grant_current(definition) and record['revision']==definition['revision']
                and record['owner_principal_id']==definition.get('owner_principal_id')
                and self.store.wakes._input_allowed(record,definition)
                and (record.get('requires_input',True) or self.store.input_optional_wake(definition,record)))
        check=getattr(self.store,'signal_authority',None)
        try:return callable(check) and check(record) is True
        except (ValueError,OSError):return False

    def _context(self,record,receipt,cancelled):
        def external(identity):
            identity=self.validate_payload(identity)
            if len(json.dumps(identity).encode())>4096:raise ValueError('Record a bounded external identity.')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT body FROM signal_steps WHERE signal_id=? AND position=?',
                               (record['id'],receipt['position'])).fetchone()
                current=json.loads(row['body'])
                if current['status']!='started' or ('external_identity' in current and current['external_identity']!=identity):
                    raise ValueError('Keep the original admitted external request identity.')
                current['external_identity']=identity
                db.execute('UPDATE signal_steps SET body=? WHERE signal_id=? AND position=?',
                           (json.dumps(current),record['id'],receipt['position']))
                receipt['external_identity']=identity
                context['external_identity']=copy.deepcopy(identity)
        context={'signal_id':record['id'],'step':receipt['position'],
            'idempotency_key':f"{record['id']}:{receipt['position']}",
            'signal':copy.deepcopy(record),'external_identity':copy.deepcopy(receipt.get('external_identity')),
            'current':lambda:not cancelled.is_set() and self._current(record),
            'cancelled':cancelled,'external':external}
        return context

    @staticmethod
    def _output(output,payload):
        output=bounded(output)
        if not isinstance(output,dict) or set(output)-{'payload','text','deliver'}:
            raise ValueError('Return bounded signal data and a delivery decision.')
        if 'deliver' in output and type(output['deliver']) is not bool:
            raise ValueError('Return an explicit signal delivery decision.')
        result=SignalLedger.validate_payload(output.get('payload',payload))
        if 'text' in output:result['text']=output['text']
        if 'text' in result and (not isinstance(result['text'],str) or len(result['text'])>8000):
            raise ValueError('Keep signal text within its message limit.')
        return result,output.get('deliver') is not False

    async def _invoke(self,registered,payload,config,context,receipt):
        task=asyncio.create_task(registered['run'](copy.deepcopy(payload),copy.deepcopy(config),context))
        self.running.add(task)
        task.add_done_callback(self.running.discard)
        task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        try:
            deadline=time.monotonic()+registered['timeout']
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('The bounded step deadline ended.')
                done,_=await asyncio.wait({task},timeout=min(.1,remaining))
                if done:return task.result()
                if not context['current']():raise ValueError('Current signal authority ended.')
        except BaseException:
            context['cancelled'].set()
            if not task.done():task.cancel()
            async def cleanup():
                evidence={}
                if registered['cancel'] is not None:
                    evidence=bounded(await registered['cancel'](context))
                    if not isinstance(evidence,dict):raise ValueError('Record the provider cleanup receipt.')
                await asyncio.gather(task,return_exceptions=True)
                return {'task_stopped':True,'quiescent':evidence.get('quiescent') is True,**evidence}
            closing=asyncio.create_task(cleanup())
            try:
                done,_=await asyncio.wait({closing},timeout=registered['cleanup_seconds'])
                receipt['cleanup']=closing.result() if done else {'quiescent':False}
            except BaseException:receipt['cleanup']={'quiescent':False}
            finally:
                if not closing.done():
                    closing.cancel()
                    closing.add_done_callback(lambda done:None if done.cancelled() else done.exception())
            raise

    async def process(self, signal):
        """Advance one saved chain; immutable ingress remains its authority evidence."""
        record=self.store.wakes.get(signal['id'],owner=signal['owner_principal_id'])
        if record['status']!='pending' or not self._current(record):return None
        if record.get('chain_status')=='ready':return record
        payload=copy.deepcopy(record.get('payload',{'text':record['text']}))
        chain=self.validate_chain(record.get('chain'))
        for position,step in enumerate(chain):
            if not self._current(record):return None
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                prior=db.execute('SELECT body FROM signal_steps WHERE signal_id=? AND position=?',
                                 (record['id'],position)).fetchone()
                if prior:
                    receipt=json.loads(prior['body'])
                    if receipt['status']=='succeeded':payload=receipt['output'];continue
                    return None
                receipt={'signal_id':record['id'],'position':position,'step':step,'status':'started',
                    'started_at':time.time(),'input_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()}
                if step['kind']=='deliver':receipt.update(status='succeeded',output=payload)
                elif step['kind']=='rules':
                    receipt.update(status='succeeded' if self._rules(step,payload) else 'filtered',output=payload)
                else:
                    registered=self.handlers.get(step['handler'])
                    if registered is None or registered['kind']!=step['kind']:
                        receipt.update(status='held',error='Install the reviewed signal handler.')
                    elif registered['metered'] and step['kind'] in {'screen','effect'} and not self.reserve(db,
                            'screen' if step['kind']=='screen' else 'send',f"{record['id']}:{position}",target=record['agent_id']):
                        receipt.update(status='held',error='Review the daily signal allowance.')
                db.execute('INSERT INTO signal_steps VALUES(?,?,?,?)',
                           (record['id'],position,receipt['status'],json.dumps(receipt)))
            if receipt['status']=='started':
                context=self._context(record,receipt,asyncio.Event())
                try:
                    if not context['current']():raise StepHeld('Current signal authority ended.')
                    output=await self._invoke(registered,payload,step.get('config',{}),context,receipt)
                    payload,deliver=self._output(output,payload)
                    if not context['current']():raise ValueError('Current signal authority ended.')
                    receipt.update(status='succeeded' if deliver else 'filtered',output=payload)
                except BaseException as exc:
                    held=isinstance(exc,StepHeld) and 'external_identity' not in receipt
                    receipt.update(status='held' if held else 'uncertain',error='Review the original step before continuing.')
                    self._save_step(receipt)
                    self._chain_status(record,receipt)
                    if isinstance(exc,asyncio.CancelledError):raise
                    return None
                self._save_step(receipt)
            if receipt['status']!='succeeded':
                self._chain_status(record,receipt)
                return None
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self.store.wakes._get(db,record['id'])
            if record['status']!='pending' or not self._current(record):return None
            record.update(chain_status='ready',processed_payload=payload)
            if 'text' in payload:record['processed_text']=payload['text']
            if record.get('target_kind')=='job':
                destination=record.get('destination')
                if destination:
                    record.update(target_kind='agent',agent_id=destination['agent_id'],revision=destination['revision'])
                    if not self._current(record):record.update(status='cancelled',reason='destination_authority_ended')
                else:record.update(status='completed',completed_at=time.time())
            self.store.wakes._write(db,record)
        return record

    def _chain_status(self,record,receipt):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');latest=self.store.wakes._get(db,record['id'])
            latest['chain_status']=receipt['status']
            if receipt['status']=='filtered':latest.update(status='screened',completed_at=time.time())
            elif receipt['status']=='failed':latest.update(status='failed',completed_at=time.time())
            self.store.wakes._write(db,latest)

    async def reconcile(self,signal_id,*,owner):
        record=self.store.wakes.get(signal_id,owner=owner)
        for receipt in self.receipts(signal_id,owner=owner):
            if receipt['status']!='uncertain':continue
            registered=self.handlers.get(receipt['step'].get('handler'))
            if registered is None or registered['reconcile'] is None:continue
            context=self._context(record,receipt,asyncio.Event())
            result=bounded(await asyncio.wait_for(registered['reconcile'](context),registered['cleanup_seconds']))
            if not isinstance(result,dict) or result.get('status') not in {'succeeded','failed','uncertain'}:
                raise ValueError('Reconcile the original external request with a terminal receipt.')
            if result['status']=='uncertain':continue
            receipt['evidence']=self.validate_payload(result.get('evidence',{}))
            if result['status']=='succeeded':
                payload,deliver=self._output(result.get('output',{}),{})
                receipt.update(status='succeeded' if deliver else 'filtered',output=payload)
            else:receipt.update(status='failed')
            self._save_step(receipt);self._chain_status(record,receipt)
        return self.store.wakes.get(signal_id,owner=owner)

    def _save_step(self, receipt):
        receipt['completed_at']=time.time()
        with self.store.connect() as db:
            db.execute('UPDATE signal_steps SET status=?,body=? WHERE signal_id=? AND position=?',
                       (receipt['status'],json.dumps(receipt),receipt['signal_id'],receipt['position']))

    def receipts(self, signal_id, *, owner):
        self.store.wakes.get(signal_id,owner=owner)
        with self.store.connect() as db:
            return [json.loads(row['body']) for row in db.execute(
                'SELECT body FROM signal_steps WHERE signal_id=? ORDER BY position',(signal_id,))]
