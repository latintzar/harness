"""Durable owner control and worker health for fixed SFTP acquisition."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import stat
import sqlite3
import time

from custom_execution import encoded, digest
from custom_coordinator import offload, drain
from source_intake import IntakeActor, IntakeError
from source_service import SourceHost, private_dir, read, require
from storage_connections import StorageConnections, StorageSnapshot
from workspace_identity import workspace_scope

PROFILE='sftp-recordings-v1'
HEARTBEAT_TTL=15
MAX_RUNS=256


def code_identity():
    # Hash files without importing optional acquisition dependencies in the app.
    import importlib.util
    names=('sftp_intake','sftp_transport','storage_connections','storage_runtime','storage_service',
        'source_service','source_intake','source_embedding','compute_guard','custom_connectors',
        'custom_execution','custom_coordinator','workspace_identity','custom_http','access')
    files=[]
    for name in names:
        local=Path(__file__).with_name(name+'.py')
        files.append(local if local.is_file() else Path(importlib.util.find_spec(name).origin))
    return hashlib.sha256(b''.join(hashlib.sha256(path.read_bytes()).digest() for path in files)).hexdigest()


@dataclass(frozen=True)
class Configuration:
    enabled:bool
    company_id:str
    profile:str
    access_state:str
    environment:str

    @classmethod
    def load(cls,state):
        try:value=read(Path(state)/'compute/storage-service.json')
        except FileNotFoundError:return None
        require(value.pop('schema_version',None)==1,'storage_service_configuration')
        try:result=cls(**value)
        except TypeError:raise IntakeError('storage_service_configuration') from None
        require(type(result.enabled)is bool and result.company_id==workspace_scope(state)
            and result.profile==PROFILE and isinstance(result.access_state,str)
            and isinstance(result.environment,str)and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',result.environment),
            'storage_service_configuration')
        access_root=Path(result.access_state)
        require(access_root.is_absolute()and access_root==access_root.resolve(),'storage_service_configuration')
        company_root=access_root if result.environment=='demo'else access_root/'environments'/result.environment
        require(company_root.resolve()==Path(state).resolve(),'storage_service_configuration')
        return result

    @property
    def sha256(self):return hashlib.sha256(encoded(self.__dict__)).hexdigest()


class RunStore:
    def __init__(self,state):
        self.root=private_dir(Path(state)/'storage-service');self.path=self.root/'runs.sqlite3'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.close(fd)
        with self.db()as db:
            db.execute('''CREATE TABLE IF NOT EXISTS runs(connector TEXT PRIMARY KEY, owner TEXT NOT NULL,
                collection TEXT NOT NULL, epoch INTEGER NOT NULL, document TEXT NOT NULL,
                desired TEXT NOT NULL, status TEXT NOT NULL, cleanup INTEGER NOT NULL,
                updated REAL NOT NULL,error TEXT)''')
    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.path,timeout=5);connection.row_factory=sqlite3.Row
        try:
            with connection:yield connection
        finally:connection.close()
    def get(self,key,owner,collection):
        with self.db()as db:
            row=db.execute('SELECT * FROM runs WHERE connector=? AND owner=? AND collection=?',(key,owner,collection)).fetchone()
            return dict(row)if row else None
    def records(self):
        with self.db()as db:return [dict(row)for row in db.execute('SELECT * FROM runs ORDER BY updated,connector LIMIT ?',(MAX_RUNS,))]
    def request(self,snapshot):
        value=snapshot.metadata;key=value['connector_id']
        with self.db()as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM runs WHERE connector=?',(key,)).fetchone()
            if old:
                require(old['owner']==value['owner_id']and old['collection']==value['collection'],'record_not_found')
                if old['desired']=='run'and old['document']==snapshot.document.decode():return
                require(old['cleanup']==1,'storage_cleanup_pending')
            else:require(db.execute('SELECT count(*) FROM runs').fetchone()[0]<MAX_RUNS,'storage_run_limit')
            db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,NULL)',(key,value['owner_id'],value['collection'],
                (old['epoch']+1)if old else 1,snapshot.document.decode(),'run','queued',1,time.time()))
    def stop_intent(self,key,owner,collection):
        with self.db()as db:
            db.execute("UPDATE runs SET desired='pause',status=CASE WHEN cleanup=1 THEN 'paused' ELSE 'stopping' END,updated=? WHERE connector=? AND owner=? AND collection=?",
                (time.time(),key,owner,collection))
    def lock(self,key):
        path=self.root/('run-'+hashlib.sha256(key.encode()).hexdigest()+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:os.close(fd);return None
        return fd
    def current(self,row):
        try:
            db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
            try:
                value=db.execute('SELECT epoch,desired,document FROM runs WHERE connector=? AND owner=? AND collection=?',
                    (row['connector'],row['owner'],row['collection'])).fetchone()
                return value==(row['epoch'],'run',row['document'])
            finally:db.close()
        except (OSError,sqlite3.Error):return False
    def update(self,row,status,*,cleanup,error=None,retire=False):
        with self.db()as db:
            db.execute('''UPDATE runs SET status=CASE WHEN desired='pause' THEN 'paused' ELSE ? END,
                cleanup=?,error=?,updated=?,desired=CASE WHEN ? THEN 'idle' ELSE desired END
                WHERE connector=? AND owner=? AND collection=? AND epoch=?''',
                (status,int(cleanup),error,time.time(),int(retire),row['connector'],row['owner'],row['collection'],row['epoch']))
    def reconcile(self,row):
        fd=self.lock(row['connector'])
        if fd is None:return False
        try:
            # This lease spans the entire network task and its drained cleanup.
            # Its absence also proves a crashed process has closed its sockets.
            current=self.get(row['connector'],row['owner'],row['collection'])
            if current is None or current['epoch']!=row['epoch']:return True
            row=current
            if not row['cleanup']:
                self.update(row,'needs_attention',cleanup=True,error='storage_worker_interrupted',retire=True)
            elif row['desired']=='pause':self.update(row,'paused',cleanup=True)
            return True
        finally:os.close(fd)


class StorageRuntime:
    def __init__(self,authority:StorageConnections,*,source_host=None,clock=time.time,access_state=None,environment=None):
        self.authority=authority;self.state=authority.store.root.parent;self.company=authority.company
        self.host=source_host or SourceHost(self.state);self.runs=RunStore(self.state);self.clock=clock
        self.code_sha256=code_identity()
        self.access_state=Path(access_state).resolve()if access_state is not None else None
        self.environment=environment
    def healthy(self):
        try:
            config=Configuration.load(self.state)
            if(not config or not config.enabled or self.access_state is None
                or config.access_state!=str(self.access_state)or config.environment!=self.environment):return False
            value=read(self.runs.root/'heartbeat.json');now=self.clock()
            return(value.get('schema_version')==1 and value.get('ready')is True and value.get('company_id')==self.company
                and value.get('config_sha256')==config.sha256 and value.get('code_sha256')==self.code_sha256
                and type(value.get('checked_at'))in(float,int)and 0<=now-value['checked_at']<=HEARTBEAT_TTL
                and value.get('expires_at')==value['checked_at']+HEARTBEAT_TTL)
        except (OSError,ValueError,TypeError):return False
    def member_current(self,owner):
        # Company membership is durable authority; browser sessions are irrelevant.
        try:
            if self.access_state is None or not isinstance(self.environment,str):return False
            company_root=self.access_state if self.environment=='demo'else self.access_state/'environments'/self.environment
            if company_root.resolve()!=self.state.resolve():return False
            path=self.access_state/'access.sqlite';info=path.lstat()
            if not stat.S_ISREG(info.st_mode)or info.st_uid!=os.geteuid()or info.st_mode&0o077:return False
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)
            try:
                return db.execute("SELECT 1 FROM principals p JOIN principal_environments m ON m.principal_id=p.id JOIN environments e ON e.id=m.environment WHERE p.id=? AND e.id=? AND m.role IN ('owner','operator')",(owner,self.environment)).fetchone() is not None
            finally:db.close()
        except (OSError,ValueError,sqlite3.Error):return False
    def current(self,snapshot):
        return self.member_current(snapshot.metadata['owner_id'])and self.authority.current(snapshot)
    def _source(self,key,value,owner,collection):
        target=value.get('target')
        if not target:return None
        checksum=digest({k:target[k]for k in ('host','port','root','host_key')})
        return self.authority.sources.lookup_connection_source(IntakeActor(owner),collection=collection,
            connection_id=key,target_sha256=checksum)
    def funded(self,owner,collection,source=None):
        try:
            if not self.member_current(owner):return False
            actor=IntakeActor(owner);config=self.host.configuration()
            if(config and config.principal_id==owner and config.collection==collection and self.host.capabilities(actor).get('can_upload')):return True
            if source:
                for row in self.host.records():
                    if(row['principal_id']==owner and row['binding']['collection']==collection
                        and row['binding']['source_id']==source['id'] and row.get('upload_id')):
                        upload=self.authority.sources.get_upload(actor,row['upload_id'])
                        if upload['status']in {'uploading','sealing'}:return True
            return False
        except (OSError,ValueError,KeyError):return False
    def describe(self,key,*,owner,collection):
        value=self.authority.describe(key,owner=owner,collection=collection)
        record=self.authority.store.get(key,owner=owner)
        target=value.get('target');configured={f['name']for f in record['secret_fields']if f['configured']}
        value['credentials_ready']=bool(target and set(target['authentication'].values())<=configured)
        import base64
        def fingerprint(key):return 'SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(key.split()[1],validate=True)).digest()).decode().rstrip('=')
        value['host_key_fingerprint']=fingerprint(target['host_key'])if target else None
        if value.get('proposal'):
            value['proposal']['host_key_fingerprint']=fingerprint(value['proposal']['target']['host_key'])
        source=self._source(key,value,owner,collection);row=self.runs.get(key,owner,collection)
        acquisition={'status':'idle','cleanup_confirmed':True,'last_check':None,'last_import':None,'next_check':None,'error':None}
        if source:acquisition['source_id']=source['id']
        if row:
            self.runs.reconcile(row);row=self.runs.get(key,owner,collection)
            acquisition.update(status=row['status'],cleanup_confirmed=bool(row['cleanup']),error=row['error'])
            snapshot=StorageSnapshot(row['document'].encode())
            source_id=snapshot.metadata['config']['source_id']
            journal=self.authority.sources.root/'sftp.sqlite3'
            if journal.exists():
                db=sqlite3.connect(journal.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
                try:
                    detail=db.execute('SELECT last_check,last_import,next_check FROM bindings WHERE source=? AND owner=?',(source_id,owner)).fetchone()
                    if detail:acquisition.update(dict(zip(('last_check','last_import','next_check'),detail)))
                finally:db.close()
            acquisition.setdefault('source_id',source_id)
        if row and row['cleanup'] and StorageSnapshot(row['document'].encode()).metadata['generation']!=value['generation'] and value['status']=='configured':
            acquisition={'status':'idle','cleanup_confirmed':True,'last_check':None,'last_import':None,'next_check':None,'error':None}
            if source:acquisition['source_id']=source['id']
        value.update(runtime_available=self.healthy(),funded_ready=self.funded(owner,collection,source),acquisition=acquisition)
        value['can_run']=bool(value['runtime_available']and value['funded_ready']and value['credentials_ready']
            and value['status']=='configured'and acquisition['cleanup_confirmed']and acquisition['status']not in {'queued','running','waiting','complete'})
        value['can_pause']=bool(row and(not row['cleanup']or(value['status']=='configured'and row['desired']=='run')))
        return value
    async def approve(self,key,proposal_id,*,owner,collection):
        require(self.member_current(owner),'workspace_access_required')
        await offload(self.authority.approve,key,proposal_id,owner=owner,collection=collection)
        return await offload(self.describe,key,owner=owner,collection=collection)
    async def run(self,key,*,owner,collection,expected_generation):
        require(type(expected_generation)is int,'storage_generation_required')
        def request():
            require(self.healthy(),'storage_worker_unavailable')
            snapshot=self.authority.snapshot(key,owner=owner,collection=collection)
            require(snapshot.metadata['generation']==expected_generation,'storage_connection_changed')
            source=self.authority.sources.get_source(IntakeActor(owner),snapshot.config.source_id)
            require(self.funded(owner,collection,source),'embedding_allowance_required')
            # Same source lock as authority Pause closes queue-admission races.
            with self.authority.sources._write(lambda:self.current(snapshot)):
                self.runs.request(snapshot)
        await offload(request)
        return await offload(self.describe,key,owner=owner,collection=collection)
    async def pause(self,key,*,owner,collection,expected_generation,timeout=10):
        def revoke():
            value=self.authority.describe(key,owner=owner,collection=collection)
            require(type(expected_generation)is int and value['generation']==expected_generation,'storage_connection_changed')
            if value['status']!='paused':
                self.authority.pause(key,expected_generation=expected_generation,owner=owner,collection=collection)
        await offload(revoke)
        await offload(self.runs.stop_intent,key,owner,collection)
        deadline=asyncio.get_running_loop().time()+timeout
        while True:
            row=await offload(self.runs.get,key,owner,collection)
            if row is None or await offload(self.runs.reconcile,row):break
            if asyncio.get_running_loop().time()>=deadline:break
            await asyncio.sleep(.05)
        return await offload(self.describe,key,owner=owner,collection=collection)
