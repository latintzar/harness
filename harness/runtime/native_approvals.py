"""Host-bound native MCP approval intents; this module performs no provider I/O.

Native event admission supplies actor identity. Browser decisions and MCP metadata
alone cannot create dispatch authority. Every dispatch claim is consumed durably;
uncertain outcomes remain fenced until host reconciliation records a receipt.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

MAX_ARGUMENT_BYTES=32768
MAX_RECORDS=10000
MAX_PENDING=16
PENDING={'pending','delivering','approved'}
TERMINAL={'declined','cancelled','expired','invalidated','delivery_uncertain','succeeded','rejected','uncertain'}

class ApprovalError(ValueError):pass

def identity(value):
    if not isinstance(value,str) or not 1<=len(value)<=256 or '\0' in value:raise ApprovalError('Use the current native approval.')
    return value

def canonical(value):
    try:return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    except (ValueError,TypeError,RecursionError):raise ApprovalError('Use bounded approval data.') from None

def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def native_key(value):
    if type(value) not in (str,int) or isinstance(value,str) and not 1<=len(value)<=256 or isinstance(value,int) and not -(2**63)<=value<2**63:raise ApprovalError('Use a valid native request.')
    return canonical(value)
def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()
def arguments(value):
    if not isinstance(value,dict) or len(canonical(value).encode())>MAX_ARGUMENT_BYTES:raise ApprovalError('Use bounded tool arguments.')
    return json.loads(canonical(value))

@dataclass(frozen=True)
class ApprovalScope:
    company_id:str
    owner:str
    root_chat_id:str
    generation:str
    root_thread_id:str
    def __post_init__(self):
        for value in asdict(self).values():identity(value)

@dataclass(frozen=True)
class ApprovalBinding:
    provider:str
    server_name:str
    tool:str
    endpoint:str
    connection_generation:int
    package_generation:int|None
    package_sha256:str|None
    schema_sha256:str
    access_mode:str
    oauth_scopes:tuple[str,...]
    write_enabled:bool=False
    authority_kind:str='native_package'
    policy_revision:str|None=None
    policy_sha256:str|None=None
    def __post_init__(self):
        for name in ('provider','server_name','tool','endpoint'):identity(getattr(self,name))
        for name in ('connection_generation',):
            if type(getattr(self,name)) is not int or getattr(self,name)<0:raise ApprovalError('Use the current provider generation.')
        if self.authority_kind=='native_package':
            if type(self.package_generation) is not int or self.package_generation<0:raise ApprovalError('Use the current provider generation.')
            hashes=('package_sha256','schema_sha256')
            if self.policy_revision is not None or self.policy_sha256 is not None:raise ApprovalError('Use the verified package authority.')
        elif self.authority_kind=='direct_provider':
            if self.package_generation is not None or self.package_sha256 is not None:raise ApprovalError('Use the verified direct provider authority.')
            identity(self.policy_revision)
            hashes=('schema_sha256','policy_sha256')
        else:raise ApprovalError('Use a host-owned provider authority.')
        for name in hashes:
            if not isinstance(getattr(self,name),str) or not re.fullmatch('[a-f0-9]{64}',getattr(self,name)):raise ApprovalError('Use the verified provider contract.')
        if self.access_mode not in {'read','read_write'} or type(self.write_enabled) is not bool:raise ApprovalError('Use the configured connection access.')
        if not isinstance(self.oauth_scopes,tuple) or len(self.oauth_scopes)>64 or any(not isinstance(s,str) or not 1<=len(s)<=512 for s in self.oauth_scopes):raise ApprovalError('Use the granted OAuth scopes.')
    @property
    def permitted(self):return self.write_enabled and self.access_mode=='read_write'

class NativeApprovals:
    def __init__(self,state,*,clock=time.time):
        self.clock=clock
        directory=Path(state)/'native-approvals'
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ApprovalError('Use private approval storage.')
        self.database=directory/'approvals.sqlite'
        if self.database.exists() or self.database.is_symlink():
            info=self.database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode&0o077:raise ApprovalError('Use private approval storage.')
        else:
            fd=os.open(self.database,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS intents (
                id TEXT PRIMARY KEY,company_id TEXT NOT NULL,owner TEXT NOT NULL,root_chat_id TEXT NOT NULL,
                generation TEXT NOT NULL,native_key TEXT NOT NULL,thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,
                call_id TEXT NOT NULL,actor_chat_id TEXT NOT NULL,binding TEXT NOT NULL,binding_hash TEXT NOT NULL,
                arguments TEXT NOT NULL,arguments_hash TEXT NOT NULL,fingerprint TEXT NOT NULL,effect_hash TEXT NOT NULL,
                status TEXT NOT NULL,decision TEXT,resolution_reason TEXT,delivery TEXT,
                created REAL NOT NULL,expires REAL NOT NULL,resolved REAL,receipt TEXT,
                UNIQUE(company_id,root_chat_id,generation,native_key),
                UNIQUE(company_id,root_chat_id,generation,thread_id,turn_id,call_id))''')

    @contextmanager
    def _db(self):
        db=sqlite3.connect(self.database,timeout=5,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA max_page_count=65536');db.execute('BEGIN IMMEDIATE')
            yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    @staticmethod
    def _public(row):
        b=json.loads(row['binding'])
        return {'id':row['id'],'kind':'provider_write','status':row['status'],'root_chat_id':row['root_chat_id'],
            'actor_chat_id':row['actor_chat_id'],'provider':b['provider'],'server_name':b['server_name'],'tool':b['tool'],
            'arguments_preview':json.loads(row['arguments']),'arguments_sha256':row['arguments_hash'],
            'created_at':stamp(row['created']),'expires_at':stamp(row['expires']),
            'resolved_at':stamp(row['resolved']) if row['resolved'] is not None else None,
            'resolution_reason':row['resolution_reason'],'receipt':json.loads(row['receipt']) if row['receipt'] else None}

    @staticmethod
    def _row(db,request_id,scope):
        row=db.execute('SELECT * FROM intents WHERE id=? AND company_id=? AND owner=? AND root_chat_id=?',
            (identity(request_id),scope.company_id,scope.owner,scope.root_chat_id)).fetchone()
        if row is None:raise ApprovalError('Use an approval from this parent chat.')
        return row

    def _change(self,db,row,status,reason=None,**changes):
        changes.update(status=status,resolution_reason=reason,resolved=self.clock() if status in TERMINAL else None)
        db.execute('UPDATE intents SET '+','.join(k+'=?' for k in changes)+' WHERE id=?',(*changes.values(),row['id']))
        return db.execute('SELECT * FROM intents WHERE id=?',(row['id'],)).fetchone()

    def _fresh(self,db,row,scope,binding,cancelled=False,*,refusing=False):
        if row['status'] not in PENDING:return row
        reason=('stopped' if cancelled else 'runtime_changed' if row['generation']!=scope.generation or row['thread_id']!=scope.root_thread_id
            else 'connection_changed' if not refusing and (not isinstance(binding,ApprovalBinding) or not binding.permitted or row['binding_hash']!=digest(asdict(binding)))
            else 'expired' if self.clock()>=row['expires'] else None)
        if reason:return self._change(db,row,'expired' if reason=='expired' else 'cancelled' if reason=='stopped' else 'invalidated',reason)
        return row

    def open(self,*,scope,actor_chat_id,native_request_id,turn_id,call_id,binding,tool_arguments,ttl_seconds=300):
        if not isinstance(scope,ApprovalScope) or actor_chat_id!=scope.root_chat_id:raise ApprovalError('The parent approves provider changes.')
        if not isinstance(binding,ApprovalBinding) or not binding.permitted:raise ApprovalError('Enable this connection’s write access first.')
        if type(ttl_seconds) not in (int,float) or not 0<ttl_seconds<=900:raise ApprovalError('Use a bounded approval lifetime.')
        identity(turn_id);identity(call_id);key=native_key(native_request_id);args=arguments(tool_arguments)
        b=canonical(asdict(binding));ah=digest(args)
        fingerprint=digest([asdict(scope),turn_id,call_id,b,ah])
        effect=digest([scope.company_id,scope.owner,binding.provider,binding.endpoint,binding.tool,ah])
        with self._db() as db:
            old=db.execute('SELECT * FROM intents WHERE company_id=? AND root_chat_id=? AND generation=? AND native_key=?',
                (scope.company_id,scope.root_chat_id,scope.generation,key)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise ApprovalError('This native approval has changed.')
                return self._public(old)
            if db.execute("SELECT 1 FROM intents WHERE effect_hash=? AND status IN ('dispatching','uncertain')",(effect,)).fetchone():raise ApprovalError('Resolve the previous provider action before repeating it.')
            if db.execute('SELECT count(*) FROM intents').fetchone()[0]>=MAX_RECORDS:raise ApprovalError('The approval history allowance is full.')
            if db.execute("SELECT count(*) FROM intents WHERE company_id=? AND owner=? AND status IN ('pending','delivering','approved')",(scope.company_id,scope.owner)).fetchone()[0]>=MAX_PENDING:raise ApprovalError('Finish a pending approval first.')
            now=self.clock();rid='approval-'+uuid.uuid4().hex
            try:
                db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (rid,scope.company_id,scope.owner,scope.root_chat_id,scope.generation,key,scope.root_thread_id,turn_id,call_id,
                    actor_chat_id,b,digest(asdict(binding)),canonical(args),ah,fingerprint,effect,'pending',None,None,None,now,now+ttl_seconds,None,None))
            except sqlite3.IntegrityError:raise ApprovalError('This native tool call already has an approval.') from None
            return self._public(db.execute('SELECT * FROM intents WHERE id=?',(rid,)).fetchone())

    def get(self,request_id,*,scope):
        with self._db() as db:return self._public(self._row(db,request_id,scope))

    def interrupt(self,request_id,*,scope,reason='runtime_ended'):
        """Retire one owned saved request after its live root connection is gone."""
        identity(reason)
        with self._db() as db:
            row=self._row(db,request_id,scope)
            if row['status'] in PENDING or row['status']=='dispatching':
                unanswered=row['status']=='pending' or row['status']=='delivering' and row['delivery']=='pending'
                row=self._change(db,row,'uncertain' if row['status']=='dispatching' else 'cancelled',reason,
                    delivery='cancel_pending' if unanswered else row['delivery'])
            return self._public(row)

    def for_chat(self,*,scope):
        with self._db() as db:
            return [self._public(r) for r in db.execute('SELECT * FROM intents WHERE company_id=? AND owner=? AND root_chat_id=? ORDER BY created,id',(scope.company_id,scope.owner,scope.root_chat_id))]

    def delivery_guard(self,request_id,*,scope,binding,cancelled):
        """Synchronous predicate executed under the native connection write lock."""
        with self._db() as db:
            row=self._row(db,request_id,scope)
            row=self._fresh(db,row,scope,binding,cancelled.is_set(),refusing=row['decision']=='decline')
            if row['status']!='delivering':return False
            # Permit exists before native can issue tools/call; dispatch rechecks
            # every binding and consumes it independently of this transport.
            self._change(db,row,'approved' if row['decision']=='allow_once' else 'declined',delivery='attempted')
            return True

    async def answer(self,request_id,*,scope,binding,action,deliver,cancelled):
        if action not in {'allow_once','decline'}:raise ApprovalError('Allow this action once or decline it.')
        if not isinstance(cancelled,asyncio.Event) or not callable(deliver):raise ApprovalError('Use the active native connection.')
        with self._db() as db:
            row=self._fresh(db,self._row(db,request_id,scope),scope,binding,cancelled.is_set(),refusing=action=='decline')
            if row['decision'] is not None:
                if row['decision']!=action:raise ApprovalError('This approval already has a decision.')
                return self._public(row)
            if row['status']!='pending':return self._public(row)
            self._change(db,row,'delivering',decision=action,delivery='pending')
            request=json.loads(row['native_key'])
        payload={'action':'accept' if action=='allow_once' else 'decline','content':{} if action=='allow_once' else None}
        guard=lambda:self.delivery_guard(request_id,scope=scope,binding=binding,cancelled=cancelled)
        try:
            await deliver(request,payload,guard)
        except BaseException as error:
            with self._db() as db:
                row=self._row(db,request_id,scope)
                if row['status'] in {'delivering','approved'}:row=self._change(db,row,'delivery_uncertain','native_delivery_ended',delivery='uncertain')
                elif row['status']=='dispatching':row=self._change(db,row,'uncertain','native_delivery_ended',delivery='uncertain')
                result=self._public(row)
            if isinstance(error,asyncio.CancelledError):raise
            return result
        with self._db() as db:
            row=self._row(db,request_id,scope)
            if row['delivery']=='attempted':
                db.execute("UPDATE intents SET delivery='submitted' WHERE id=?",(request_id,))
            # A transport callback which skipped guard created no authority.
            elif row['status']=='delivering':self._change(db,row,'invalidated','native_guard_required')
            return self._public(db.execute('SELECT * FROM intents WHERE id=?',(request_id,)).fetchone())

    def resolve_native(self,scope,native_request_id):
        with self._db() as db:
            row=db.execute('SELECT * FROM intents WHERE company_id=? AND root_chat_id=? AND generation=? AND native_key=?',
                (scope.company_id,scope.root_chat_id,scope.generation,native_key(native_request_id))).fetchone()
            if row is None:return None
            if row['owner']!=scope.owner:raise ApprovalError('Use the owning parent chat.')
            if row['delivery']=='cancel_pending':
                db.execute("UPDATE intents SET delivery='native_resolved' WHERE id=?",(row['id'],))
                row=db.execute('SELECT * FROM intents WHERE id=?',(row['id'],)).fetchone()
            if row['status']=='pending' or row['status']=='delivering' and row['delivery']=='pending':
                row=self._change(db,row,'cancelled','native_resolved')
            return self._public(row)

    def invalidate(self,*,scope=None,provider=None,company_id=None,owner=None,reason='runtime_ended'):
        """Host Stop/disconnect/reconfiguration hook; dispatched writes stay uncertain."""
        identity(reason)
        if (company_id is None)!=(owner is None):raise ApprovalError('Use the exact connection owner and company.')
        if company_id is not None:
            identity(company_id);identity(owner)
            if scope is not None and (scope.company_id!=company_id or scope.owner!=owner):
                raise ApprovalError('Use one owning workspace scope.')
        with self._db() as db:
            rows=list(db.execute("SELECT * FROM intents WHERE status IN ('pending','delivering','approved','dispatching')"));out=[]
            for row in rows:
                if company_id is not None and (row['company_id']!=company_id or row['owner']!=owner):continue
                if scope is not None and (any(row[k]!=getattr(scope,k) for k in ('company_id','owner','root_chat_id','generation')) or row['thread_id']!=scope.root_thread_id):continue
                if provider is not None and json.loads(row['binding'])['provider']!=provider:continue
                unanswered=row['status']=='pending' or row['status']=='delivering' and row['delivery']=='pending'
                row=self._change(db,row,'uncertain' if row['status']=='dispatching' else 'cancelled',reason,
                    delivery='cancel_pending' if unanswered else row['delivery'])
                out.append(self._public(row))
            return out

    def provider_cancellations(self,*,scope,provider):
        """Private outstanding reply IDs, including a prior durable invalidation."""
        identity(provider)
        with self._db() as db:
            rows=db.execute("SELECT * FROM intents WHERE company_id=? AND owner=? AND root_chat_id=? AND generation=? AND thread_id=? AND status='cancelled' AND delivery='cancel_pending'",
                (scope.company_id,scope.owner,scope.root_chat_id,scope.generation,scope.root_thread_id))
            return [(json.loads(row['native_key']),self._public(row)) for row in rows
                if json.loads(row['binding'])['provider']==provider]

    def preflight_failed(self,request_id,*,scope):
        """Close failed host preflight; return a private reply ID only if unanswered."""
        with self._db() as db:
            row=self._row(db,request_id,scope)
            same_run=row['generation']==scope.generation and row['thread_id']==scope.root_thread_id
            reply=(json.loads(row['native_key']) if same_run and
                (row['status']=='pending' or row['status']=='delivering' and row['delivery']=='pending') else None)
            if row['status'] in PENDING:
                row=self._change(db,row,'invalidated','provider_unavailable' if same_run else 'runtime_changed',
                    delivery='cancel_pending' if reply is not None else row['delivery'])
            elif row['status']=='dispatching':
                row=self._change(db,row,'uncertain','provider_unavailable')
            return reply,self._public(row)

    def cancel_guard(self,request_id,*,scope,cancelled):
        """Close one still-unanswered native request under its transport lock."""
        with self._db() as db:
            row=self._row(db,request_id,scope)
            if (cancelled.is_set() or row['generation']!=scope.generation or row['thread_id']!=scope.root_thread_id
                    or row['delivery']!='cancel_pending' or row['status'] not in {'invalidated','expired','cancelled'}):return False
            db.execute("UPDATE intents SET delivery='cancel_attempted' WHERE id=?",(request_id,))
            return True

    def finish_call(self,*,scope,thread_id,turn_id,call_id):
        with self._db() as db:
            row=db.execute('SELECT * FROM intents WHERE company_id=? AND owner=? AND root_chat_id=? AND generation=? AND thread_id=? AND turn_id=? AND call_id=?',
                (scope.company_id,scope.owner,scope.root_chat_id,scope.generation,thread_id,turn_id,call_id)).fetchone()
            if row is None:return None
            if row['delivery']=='cancel_pending':
                db.execute("UPDATE intents SET delivery='native_call_completed' WHERE id=?",(row['id'],))
                row=db.execute('SELECT * FROM intents WHERE id=?',(row['id'],)).fetchone()
            if row['status'] in PENDING:row=self._change(db,row,'cancelled','native_call_completed')
            elif row['status']=='dispatching':row=self._change(db,row,'uncertain','native_call_completed_without_receipt')
            return self._public(row)

    def expire(self,*,scope):
        """Return private native reply IDs alongside public records for one host run."""
        with self._db() as db:
            rows=list(db.execute("SELECT * FROM intents WHERE company_id=? AND owner=? AND root_chat_id=? AND generation=? AND status IN ('pending','delivering','approved') AND expires<=?",
                (scope.company_id,scope.owner,scope.root_chat_id,scope.generation,self.clock())))
            return [(json.loads(row['native_key']) if row['status']=='pending' or row['status']=='delivering' and row['delivery']=='pending' else None,
                self._public(self._change(db,row,'expired','expired',delivery='cancel_pending' if row['status']=='pending' or row['status']=='delivering' and row['delivery']=='pending' else row['delivery']))) for row in rows]

    def recover(self):return self.invalidate(reason='runtime_restarted')

    def claim_dispatch(self,*,scope,binding,metadata,tool_arguments,cancelled):
        """Host-only future transport hook. No caller in this increment sends writes."""
        if not isinstance(metadata,dict) or not isinstance(binding,ApprovalBinding) or not isinstance(cancelled,asyncio.Event) or not isinstance(metadata.get('x-codex-turn-metadata'),dict):raise ApprovalError('Use an approved native call.')
        call=identity(metadata.get('callId'));thread=identity(metadata.get('threadId'))
        turn=identity((metadata.get('x-codex-turn-metadata') or {}).get('turn_id'))
        args=arguments(tool_arguments)
        with self._db() as db:
            row=db.execute('SELECT * FROM intents WHERE company_id=? AND owner=? AND root_chat_id=? AND generation=? AND thread_id=? AND turn_id=? AND call_id=?',
                (scope.company_id,scope.owner,scope.root_chat_id,scope.generation,thread,turn,call)).fetchone()
            if row is None or row['thread_id']!=scope.root_thread_id or row['arguments_hash']!=digest(args):
                raise ApprovalError('This call has no exact approved intent.')
            if row['status'] not in PENDING and row['binding_hash']!=digest(asdict(binding)):
                raise ApprovalError('This call has no exact approved intent.')
            row=self._fresh(db,row,scope,binding,cancelled.is_set())
            if row['status'] in {'dispatching','succeeded','rejected','uncertain'}:
                return {'dispatch':False,'request':self._public(row)}
            if row['status']=='approved' and row['delivery'] in {'attempted','submitted'}:
                if db.execute("SELECT 1 FROM intents WHERE effect_hash=? AND id!=? AND status IN ('dispatching','uncertain')",(row['effect_hash'],row['id'])).fetchone():raise ApprovalError('Resolve the previous provider action before repeating it.')
                row=self._change(db,row,'dispatching')
                return {'dispatch':True,'request':self._public(row),'arguments':json.loads(row['arguments'])}
        # Commit freshness transitions before returning the admission error.
        raise ApprovalError('This call needs a current parent approval.')

    def provider_has_unresolved(self,*,scope,provider,metadata):
        """Host admission across this owner's chats; exact-call replay is exempt.

        The caller holds the provider's cross-process admission fence through
        this check and claim_dispatch. This is an unresolved-outcome policy,
        separate from how a user/task authorizes an otherwise valid operation.
        """
        identity(provider)
        if not isinstance(metadata,dict) or not isinstance(metadata.get('x-codex-turn-metadata'),dict):
            raise ApprovalError('Use an approved native call.')
        call=identity(metadata.get('callId'));thread=identity(metadata.get('threadId'))
        turn=identity(metadata['x-codex-turn-metadata'].get('turn_id'))
        with self._db() as db:
            rows=db.execute("SELECT * FROM intents WHERE company_id=? AND owner=? AND status IN ('dispatching','uncertain')",(scope.company_id,scope.owner))
            for row in rows:
                if json.loads(row['binding'])['provider']!=provider:continue
                if (row['root_chat_id']==scope.root_chat_id and row['generation']==scope.generation
                        and row['thread_id']==thread and row['turn_id']==turn and row['call_id']==call):continue
                return True
            return False

    def outcome(self,request_id,*,scope,status,receipt=None):
        """Host reconciliation only; never infer remote success from native completion."""
        if status not in {'succeeded','rejected','uncertain'}:raise ApprovalError('Record the provider outcome.')
        if receipt is not None:
            if not isinstance(receipt,dict) or set(receipt)-{'resource_id','url','response_sha256'} or len(canonical(receipt).encode())>8192 or any(not isinstance(v,str) or len(v)>4096 for v in receipt.values()):raise ApprovalError('Use a bounded provider receipt.')
        if status=='succeeded' and not receipt:raise ApprovalError('A successful write needs a provider receipt.')
        with self._db() as db:
            row=self._row(db,request_id,scope)
            if row['generation']!=scope.generation or row['thread_id']!=scope.root_thread_id:
                raise ApprovalError('Reconcile using this action’s original runtime scope.')
            if row['status'] not in {'dispatching','uncertain'}:raise ApprovalError('Reconcile a dispatched provider action.')
            row=self._change(db,row,status,receipt=canonical(receipt) if receipt else None)
            return self._public(row)

class NativeApprovalRouter:
    """Feed only independently admitted native events from one immutable root run.

    resolve_binding is a host callback, never a model or browser argument. Its
    default absence denies writes. The write callback must enforce guard while
    holding the same transport lock used by ordinary native question responses.
    """
    def __init__(self,store,scope,*,resolve_binding,cancelled,write):
        if not isinstance(store,NativeApprovals) or not isinstance(scope,ApprovalScope) or not isinstance(cancelled,asyncio.Event):raise ApprovalError('Use an admitted native root.')
        self.store,self.scope,self.resolve_binding,self.cancelled,self.write=store,scope,resolve_binding,cancelled,write
        self.items={}

    async def _deny(self,native_id):
        await self.write({'id':native_id,'result':{'action':'decline','content':None}})

    async def event(self,item,*,actor_chat_id):
        method=item.get('method');params=item.get('params') or {};native=params.get('item') or {}
        if method=='item/started' and native.get('type')=='mcpToolCall':
            try:
                key=(identity(params.get('threadId')),identity(params.get('turnId')),identity(native.get('id')))
                value={'actor':identity(actor_chat_id),'server':identity(native.get('server')),'tool':identity(native.get('tool')),
                    'arguments':arguments(native.get('arguments'))}
                if key in self.items and self.items[key]!=value:raise ApprovalError('The native call changed.')
                if len(self.items)>=128 and key not in self.items:raise ApprovalError('Finish an active provider tool first.')
                self.items[key]=value
            except ApprovalError:
                self.items.pop((params.get('threadId'),params.get('turnId'),native.get('id')),None)
            return []
        if method=='item/completed' and native.get('type')=='mcpToolCall':
            self.items.pop((params.get('threadId'),params.get('turnId'),native.get('id')),None)
            row=self.store.finish_call(scope=self.scope,thread_id=params.get('threadId'),turn_id=params.get('turnId'),call_id=native.get('id'))
            return [{'type':'approval_resolved','request':row}] if row else []
        if method=='serverRequest/resolved':
            if params.get('threadId')!=self.scope.root_thread_id or actor_chat_id!=self.scope.root_chat_id:return []
            row=self.store.resolve_native(self.scope,params.get('requestId'))
            return [{'type':'approval_resolved','request':row}] if row else []
        if method!='mcpServer/elicitation/request' or 'id' not in item:return []
        native_id=item['id']
        try:
            native_key(native_id)
            if (self.cancelled.is_set() or actor_chat_id!=self.scope.root_chat_id or params.get('threadId')!=self.scope.root_thread_id
                    or params.get('mode')!='form' or (params.get('_meta') or {}).get('codex_approval_kind')!='mcp_tool_call'):
                raise ApprovalError('Use the parent provider approval flow.')
            request_schema=params.get('requestedSchema') or {}
            if request_schema.get('type')!='object' or request_schema.get('properties')!={}:raise ApprovalError('Use the native tool approval.')
            proposed=arguments((params.get('_meta') or {}).get('tool_params'))
            matches=[(key,value) for key,value in self.items.items() if key[:2]==(params.get('threadId'),params.get('turnId'))
                and value['actor']==actor_chat_id and value['server']==params.get('serverName') and digest(value['arguments'])==digest(proposed)]
            if len(matches)!=1:raise ApprovalError('Use one unambiguous native tool call.')
            key,value=matches[0]
            binding=await self.resolve_binding(value['server'],value['tool']) if callable(self.resolve_binding) else None
            if (not isinstance(binding,ApprovalBinding) or binding.server_name!=value['server'] or binding.tool!=value['tool']
                    or self.cancelled.is_set() or self.items.get(key)!=value):raise ApprovalError('Use an enabled provider write connection.')
            record=self.store.open(scope=self.scope,actor_chat_id=actor_chat_id,native_request_id=native_id,
                turn_id=key[1],call_id=key[2],binding=binding,tool_arguments=value['arguments'])
            return [{'type':'approval_request','request':record}]
        except (ApprovalError,ValueError):
            await self._deny(native_id)
            return []

    async def answer(self,request_id,*,owner,action):
        if owner!=self.scope.owner:raise ApprovalError('Use the owning parent chat.')
        record=self.store.get(request_id,scope=self.scope)
        try:
            binding=await self.resolve_binding(record['server_name'],record['tool']) if action!='decline' and callable(self.resolve_binding) else None
        except Exception:
            native_id,failed=self.store.preflight_failed(request_id,scope=self.scope)
            if native_id is not None and not self.cancelled.is_set():
                try:await self.write({'id':native_id,'result':{'action':'cancel','content':None}},
                    guard=lambda:self.store.cancel_guard(request_id,scope=self.scope,cancelled=self.cancelled))
                except (OSError,RuntimeError):pass
            return {'type':'approval_resolved','request':failed}
        async def deliver(native_id,payload,guard):
            await self.write({'id':native_id,'result':payload},guard=guard)
        result=await self.store.answer(request_id,scope=self.scope,binding=binding,action=action,
            deliver=deliver,cancelled=self.cancelled)
        return {'type':'approval_resolved','request':result}

    def retire(self,reason='runtime_ended'):
        self.items.clear()
        return [{'type':'approval_resolved','request':row} for row in self.store.invalidate(scope=self.scope,reason=reason)]

    async def revoke_provider(self,provider):
        """Close this run's provider waits after host connection revocation begins."""
        events={row['id']:{'type':'approval_resolved','request':row}
            for row in self.store.invalidate(scope=self.scope,provider=provider,reason='disconnected')}
        for native_id,row in self.store.provider_cancellations(scope=self.scope,provider=provider):
            if not self.cancelled.is_set():
                try:await self.write({'id':native_id,'result':{'action':'cancel','content':None}},
                    guard=lambda:self.store.cancel_guard(row['id'],scope=self.scope,cancelled=self.cancelled))
                except (OSError,RuntimeError):pass
            events[row['id']]={'type':'approval_resolved','request':self.store.get(row['id'],scope=self.scope)}
        return list(events.values())

    async def expire(self):
        events=[]
        for native_id,row in self.store.expire(scope=self.scope):
            if native_id is not None and not self.cancelled.is_set():
                try:await self.write({'id':native_id,'result':{'action':'cancel','content':None}},
                    guard=lambda:self.store.cancel_guard(row['id'],scope=self.scope,cancelled=self.cancelled))
                except (OSError,RuntimeError):pass
            events.append({'type':'approval_resolved','request':row})
        return events
