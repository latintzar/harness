"""Revocable, session-bound MCP reads for OpenAI's managed agent harness.

The credential is stable because stock session tools are immutable. Authority is
an in-memory generation captured from the current root admission, renewed only
after provider root/children reconciliation and the previous host reads drain.
Restart retains the credential binding and withdraws every active generation.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time

from host import Admission
from public_mcp import HTTPMCP, HTTPMCPServices
from tool_families import ToolContext
from workspace_identity import workspace_scope


PATH='/runtime/mcp'
MAX_RECORDS=10000
MAX_SESSIONS=128
MAX_TURN_SECONDS=3600


def _identity(value):
    if not isinstance(value,str) or not 1<=len(value)<=256 or any(ord(char)<32 for char in value):
        raise ValueError('Use the saved managed session identity.')
    return value


def _private_file(path, *, create=False):
    flags=os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK
    if create:flags|=os.O_CREAT
    fd=os.open(path,flags,0o600)
    details=os.fstat(fd)
    if (not stat.S_ISREG(details.st_mode) or details.st_uid!=os.geteuid()
            or details.st_nlink!=1 or details.st_mode&0o077):
        os.close(fd)
        raise ValueError('Use private managed session credential storage.')
    return fd


@dataclass(frozen=True)
class ManagedReadGrant:
    id:str
    company_id:str
    owner_principal_id:str
    dataset:str
    expires_at:float
    client_id:str
    permission:str='workspace:read'


@dataclass(eq=False)
class Generation:
    lease:object=field(repr=False)
    grant:ManagedReadGrant
    source_admission:object=field(repr=False)
    cancel:object=field(repr=False)
    deadline:float
    current_run:object=field(repr=False)
    run:dict=field(repr=False)
    actor:str
    revoked:bool=False
    services:object=field(default=None,repr=False)

    @property
    def id(self):return self.grant.id

    def current(self):
        if self.revoked:return False
        try:
            allowed=(not self.revoked and not self.cancel.is_set()
                and self.lease.host.monotonic()<self.deadline
                and self.current_run() is True and self.source_admission.current()
                and self.lease.record_current() and self.lease.host._current_generation(self))
            if not allowed:self.lease.revoke(self)
            return allowed
        except Exception:
            try:self.lease.revoke(self)
            except Exception:self.revoked=True
            return False

    def close(self):self.lease.end(self)


@dataclass(frozen=True)
class _CapturedServices(HTTPMCPServices):
    generation:Generation

    def admit(self,grant):
        generation=self.generation;lease=generation.lease
        if grant!=generation.grant or not generation.current():
            raise ValueError('Continue with the current managed turn.')
        source=generation.source_admission
        admission=Admission(generation.current,source.bind,
            source.model_context,resource=getattr(source,'_resource',None))
        return ToolContext(state=Path(self.state),company_id=grant.company_id,
            actor=generation.actor,data_owner=lease.data_owner,collection=lease.collection,
            chat_id=lease.root_chat,record=self.agent.children.get(lease.root_chat),run=generation.run,
            agents=getattr(self.agent,'automation_store',None),agent=self.agent,
            admission=admission,extension=self.agent.vertical)


class SessionLease:
    """Server custody of one stock session's stable credential and read grants."""
    def __init__(self,host,row,services):
        self.host=host;self.binding_id=row['binding_id'];self.key=row['key']
        self.company_id=row['company_id'];self.data_owner=row['data_owner']
        self.root_chat=row['root_chat'];self.collection=row['collection']
        self.services=services;self._generation=None;self._retired=set();self._disconnected_request=None

    @property
    def session_id(self):return self.host._row(self.binding_id)['session_id']

    @property
    def tools(self):
        """Send this only in the provider credential field, never a chat or receipt."""
        return [{'type':'mcp','server_label':'rocoto','connection_origin':'service','required':True,
            'allowed_tools':sorted(self.services.registry.read_names('public')),
            'transport':{'type':'http','server_url':self.host.url,
                'authorization':'Bearer '+self.host._token(self.key)}}]

    def record_current(self):
        record=self.services.agent.children.get(self.root_chat)
        return (isinstance(record,dict) and record.get('id')==self.root_chat
            and record.get('owner_principal_id')==self.data_owner
            and record.get('dataset')==self.collection
            and not record.get('parent_id') and not record.get('native_root_id')
            and Path(self.services.agent.state).absolute()==Path(self.services.state).absolute())

    def created(self,provider_session_id):
        """Bind the API-issued session once; a conflicting response cannot adopt it."""
        _identity(provider_session_id)
        with self.host._lock,self.host._db() as db:
            self.host._require_epoch(db)
            row=db.execute('SELECT session_id FROM bindings WHERE binding_id=?',(self.binding_id,)).fetchone()
            if row['session_id'] not in (None,provider_session_id):
                raise ValueError('Preserve the managed session binding.')
            try:db.execute('UPDATE bindings SET session_id=? WHERE binding_id=?',(provider_session_id,self.binding_id))
            except sqlite3.IntegrityError:raise ValueError('Use the session owned by this conversation.') from None

    def begin(self,*,actor,admission,cancel,deadline,current_run,quiesced,run,request_id,recovering=False):
        """Host-only: quiesced certifies reconciled root and inherited children."""
        _identity(actor);_identity(request_id)
        if (not (quiesced is True or recovering is True) or not isinstance(cancel,asyncio.Event)
                or type(deadline) not in {int,float} or not math.isfinite(deadline)
                or not 0<deadline-self.host.monotonic()<=MAX_TURN_SECONDS
                or not callable(current_run) or not isinstance(run,dict)
                or run.get('cancel') is not cancel or self.services.agent.active.get(self.root_chat) is not run
                or not self.record_current() or not isinstance(admission,Admission)):
            raise ValueError('Reconcile the previous session and admit a bounded current turn.')
        with self.host._lock:
            if self._generation is not None or self._pending_reads():
                raise ValueError('Drain the previous managed turn before renewal.')
            if cancel.is_set() or not admission.current() or current_run() is not True:
                raise ValueError('Use the current authorized root turn.')
            with self.host._db() as db:
                self.host._require_epoch(db)
                prior=db.execute('SELECT request_id,actor,request_epoch,recoverable FROM bindings WHERE binding_id=?',(self.binding_id,)).fetchone()
                if recovering and (prior['request_id']!=request_id or prior['actor']!=actor
                        or (prior['request_epoch']==self.host._epoch and self._disconnected_request!=request_id)
                        or not prior['recoverable']):
                    raise ValueError('Recover only the same already admitted request and actor after restart.')
                if not recovering and prior['request_id']==request_id:
                    raise ValueError('Use the next reconciled managed request.')
                db.execute('UPDATE bindings SET generation=generation+1,request_id=?,actor=?,request_epoch=?,recoverable=1 WHERE binding_id=?',
                    (request_id,actor,self.host._epoch,self.binding_id))
                number=db.execute('SELECT generation FROM bindings WHERE binding_id=?',(self.binding_id,)).fetchone()[0]
            grant=ManagedReadGrant('managed-read-'+self.key+'-'+str(number),self.company_id,self.data_owner,self.collection,
                self.host.clock()+deadline-self.host.monotonic(),'rocoto-managed:'+self.key)
            generation=Generation(self,grant,admission,cancel,deadline,current_run,run,actor)
            generation.services=_CapturedServices(self.services.state,self.services.agent,generation)
            self._disconnected_request=None
            self._generation=generation;self.host._generations[grant.id]=generation
            if not generation.current():
                self.end(generation)
                raise ValueError('Refresh the current managed turn admission.')
            return generation

    def current(self):
        generation=self._generation
        return bool(generation and generation.current())

    def end(self,generation=None,*,disconnected=False):
        """Withdraw immediately; closing an old generation cannot stop its successor."""
        with self.host._lock:
            selected=generation or self._generation
            if selected is None:return
            if selected.lease is not self:raise ValueError('Close the owning session lease.')
            selected.revoked=True;self.host._generations.pop(selected.id,None)
            self._retired.add(selected.id)
            if self._generation is selected:
                if disconnected:
                    self._disconnected_request=self.host._row(self.binding_id)['request_id']
                self._generation=None

    close=end

    def revoke(self,generation=None):
        selected=generation or self._generation
        self.end(selected)
        if selected is None:return
        self._disconnected_request=None
        with self.host._lock,self.host._db() as db:
            self.host._require_epoch(db)
            db.execute('UPDATE bindings SET recoverable=0 WHERE binding_id=? AND generation=?',
                (self.binding_id,int(selected.id.rsplit('-',1)[1])))

    def _pending_reads(self):
        keys=self._retired|({self._generation.id} if self._generation else set())
        return any(key in keys for key in self.host.transport.calls.values())

    async def drain(self):
        """Retain captured snapshots while the transport drains actual worker reads."""
        cancelled=False
        while self._pending_reads():
            try:await asyncio.sleep(.01)
            except asyncio.CancelledError:cancelled=True
        self._retired.clear()
        if cancelled:raise asyncio.CancelledError


class ManagedMCPHost:
    """Separate authority from public customer grants, using the shared MCP protocol."""
    def __init__(self,state,*,public_origin='',clock=time.time,monotonic=time.monotonic):
        self.root=Path(state).absolute()/'managed-mcp';self.clock=clock;self.monotonic=monotonic
        self.url=public_origin.rstrip('/')+PATH if public_origin else None
        self.transport=None;self._lock=threading.RLock();self._leases={};self._generations={}
        self._epoch=secrets.token_hex(24)
        if not self.url:return
        self._initialize()
        self.transport=HTTPMCP(self,self.resolve,resource_url=self.url,
            resource_path=PATH,permission='workspace:read')

    def _initialize(self):
        if self.root.is_symlink():raise ValueError('Use private managed session credential storage.')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.root.stat()
        if self.root!=self.root.resolve() or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise ValueError('Use private managed session credential storage.')
        self.path=self.root/'bindings.sqlite3';seed=self.root/'credential-seed'
        if not seed.exists():
            if self.path.exists():raise ValueError('Restore the existing managed session credential seed.')
            try:
                fd=os.open(seed,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(secrets.token_bytes(48));stream.flush();os.fsync(stream.fileno())
            except FileExistsError:pass
        fd=_private_file(seed)
        with os.fdopen(fd,'rb') as stream:self._seed=stream.read(49)
        if len(self._seed)!=48:raise ValueError('Restore the existing managed session credential seed.')
        os.close(_private_file(self.path,create=True))
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS host_epoch (id INTEGER PRIMARY KEY CHECK(id=1),epoch TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS bindings(binding_id TEXT PRIMARY KEY,key TEXT UNIQUE NOT NULL,
                digest TEXT UNIQUE NOT NULL,company_id TEXT NOT NULL,data_owner TEXT NOT NULL,root_chat TEXT NOT NULL,
                collection TEXT NOT NULL,session_id TEXT UNIQUE,generation INTEGER NOT NULL DEFAULT 0,
                request_id TEXT,actor TEXT,request_epoch TEXT,recoverable INTEGER NOT NULL DEFAULT 0)''')
            db.execute('INSERT OR REPLACE INTO host_epoch VALUES(1,?)',(self._epoch,))

    @contextmanager
    def _db(self):
        os.close(_private_file(self.path))
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA max_page_count=16384');db.execute('BEGIN IMMEDIATE')
            yield db;db.commit()
        finally:db.close()

    def _require_epoch(self,db):
        row=db.execute('SELECT epoch FROM host_epoch WHERE id=1').fetchone()
        if row is None or row[0]!=self._epoch:
            raise ValueError('Reconcile managed sessions with the current host process.')

    def _row(self,binding_id):
        with self._db() as db:
            self._require_epoch(db)
            row=db.execute('SELECT * FROM bindings WHERE binding_id=?',(binding_id,)).fetchone()
            if row is None:raise ValueError('Choose a saved managed session binding.')
            return row

    def _token(self,key):
        signature=hmac.new(self._seed,('rocoto-managed-mcp-v1:'+key).encode(),hashlib.sha384).digest()
        return 'rmanaged_'+base64.urlsafe_b64encode(signature).decode().rstrip('=')

    def bind_session(self,binding_id,*,company_id,data_owner,root_chat,collection,services):
        for value in (binding_id,company_id,data_owner,root_chat,collection):_identity(value)
        if (self.transport is None or not isinstance(services,HTTPMCPServices)
                or workspace_scope(services.state)!=company_id
                or Path(services.state).absolute()!=Path(services.agent.state).absolute()):
            raise ValueError('Configure the current workspace managed MCP service.')
        identity=(company_id,data_owner,root_chat,collection)
        with self._lock,self._db() as db:
            self._require_epoch(db)
            row=db.execute('SELECT * FROM bindings WHERE binding_id=?',(binding_id,)).fetchone()
            if row is not None and tuple(row[key] for key in ('company_id','data_owner','root_chat','collection'))!=identity:
                raise ValueError('Preserve the managed session owner, conversation and collection.')
            if row is None:
                if db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0]>=MAX_RECORDS:
                    raise ValueError('Review retained managed session bindings.')
                key=secrets.token_hex(24)
                digest=hashlib.sha256(self._token(key).encode()).hexdigest()
                db.execute('INSERT INTO bindings(binding_id,key,digest,company_id,data_owner,root_chat,collection) VALUES(?,?,?,?,?,?,?)',
                    (binding_id,key,digest,*identity))
                row=db.execute('SELECT * FROM bindings WHERE binding_id=?',(binding_id,)).fetchone()
            prior=self._leases.get(binding_id)
            if prior is not None:
                if prior.services!=services:raise ValueError('Use the installed managed session services.')
                return prior
            if len(self._leases)>=MAX_SESSIONS:raise ValueError('Drain existing managed sessions before admitting more.')
            lease=SessionLease(self,row,services)
            if not lease.record_current():raise ValueError('Use the saved owning root conversation.')
            self._leases[binding_id]=lease
            return lease

    async def revoke_binding(self, binding_id):
        """Withdraw a saved session even when restart has no live generation."""
        _identity(binding_id)
        with self._lock:
            lease = self._leases.get(binding_id)
            if lease is not None:
                lease.revoke()
                lease._disconnected_request = None
            with self._db() as db:
                self._require_epoch(db)
                db.execute('UPDATE bindings SET recoverable=0 WHERE binding_id=?', (binding_id,))
        if lease is not None:
            await lease.drain()

    def _current_generation(self,generation):
        with self._lock:
            if self._generations.get(generation.id) is not generation:return False
            try:
                row=self._row(generation.lease.binding_id)
                return generation.id.endswith('-'+str(row['generation']))
            except (OSError,ValueError,sqlite3.Error):return False

    def authenticate(self,token):
        if not isinstance(token,str) or len(token)!=73 or not token.startswith('rmanaged_'):return None
        digest=hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            try:
                with self._db() as db:
                    self._require_epoch(db)
                    row=db.execute('SELECT binding_id FROM bindings WHERE digest=?',(digest,)).fetchone()
                lease=self._leases.get(row[0]) if row else None
                generation=lease._generation if lease else None
                return generation.grant if generation and generation.current() else None
            except (OSError,ValueError,sqlite3.Error):return None

    def active(self,key):
        with self._lock:
            generation=self._generations.get(key)
            return generation.grant if generation and generation.current() else None

    def resolve(self,grant):
        with self._lock:
            generation=self._generations.get(grant.id)
            return generation.services if generation and generation.grant==grant and generation.current() else None

    @asynccontextmanager
    async def lifespan(self):
        if self.transport is None:
            yield self
            return
        async with self.transport.lifespan():
            try:yield self
            finally:
                for lease in tuple(self._leases.values()):lease.end()
                for lease in tuple(self._leases.values()):await lease.drain()
