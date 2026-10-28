"""Explicitly commissioned, company-owned source worker and one-use intake gate.

The HTTP host uses SourceHost only. SourceService runs in a separate process;
constructing the host gate never starts a worker or contacts a provider.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import time
import uuid

import compute_guard
from source_embedding import SourceEmbedding, PURPOSE, WORKSPACE, runtime, atomic_json, public as public_attempt
from source_intake import SourceStore, IntakeActor, IntakeError
from source_media import MediaClient, MediaScope, IMAGE, PROFILE, DERIVE_IMAGE, DERIVE_PROFILE
from source_routes import SourceRoutePolicy
from source_worker import SourceWorker, WorkerLimits, IndexPublisher, _thread
from qdrant_retrieval import LocalQdrant
from workspace_identity import workspace_scope

HEARTBEAT_SECONDS=5
HEARTBEAT_TTL=15
MAX_JSON=128*1024
ROOT_RECEIPT=Path('/opt/rocoto-source-media/readiness-receipt.json')
SHA=re.compile(r'[a-f0-9]{64}\Z')


def code_identity():
    """Bind readiness to the worker's local source closure, including paid admission."""
    import importlib.util
    names=('source_service','source_worker','source_intake','source_embedding','modal_embed',
        'compute_guard','cosmos_retrieval','qdrant_retrieval','retrieval','imported_media',
        'source_media','native_executor','native_package_mount','document_preview_contract',
        'workspace_identity')
    result=hashlib.sha256()
    for name in names:
        local=Path(__file__).with_name(name+'.py')
        path=local if local.is_file() else Path(importlib.util.find_spec(name).origin)
        result.update(name.encode()+b'\0'+hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


# Capture once with the loaded modules. A long-lived worker must never renew its
# identity by hashing a newer checkout while still executing the old imports.
LOADED_CODE_SHA256=code_identity()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def require(ok,code):
    if not ok: raise IntakeError(code)


def private_dir(path):
    path=Path(path)
    require(path.absolute()==path.resolve(),'invalid_service_storage')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077,'invalid_service_storage')
    return path


def read(path,*,uid=None,public=False):
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==(os.geteuid() if uid is None else uid)
                and not info.st_mode&(0o022 if public else 0o077) and info.st_size<=MAX_JSON,'invalid_service_record')
        with os.fdopen(descriptor,'rb',closefd=False) as stream: raw=stream.read(MAX_JSON+1)
        require(len(raw)<=MAX_JSON,'invalid_service_record')
        value=json.loads(raw)
        require(isinstance(value,dict),'invalid_service_record')
        return value
    finally: os.close(descriptor)


@contextmanager
def lock(path,*,exclusive=False):
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid() and not info.st_mode&0o077,'invalid_service_storage')
        fcntl.flock(fd,fcntl.LOCK_EX | (fcntl.LOCK_NB if exclusive else 0))
        yield
    finally: os.close(fd)


@dataclass(frozen=True)
class Configuration:
    enabled:bool
    company_id:str
    principal_id:str
    collection:str
    approval_path:str
    approval_sha256:str
    receipt_sha256:str
    modal_python:str
    modal_config:str
    max_file_bytes:int=256*1024**2

    @classmethod
    def load(cls,state,company):
        try: value=read(Path(state)/'compute/source-intake-service.json')
        except FileNotFoundError: return None
        require(value.pop('schema_version',None)==1,'invalid_service_configuration')
        try: config=cls(**value)
        except TypeError: raise IntakeError('invalid_service_configuration') from None
        require(type(config.enabled) is bool and config.company_id==company
                and isinstance(config.principal_id,str) and 0<len(config.principal_id.encode())<=160
                and all(ord(char)>=32 for char in config.principal_id) and isinstance(config.collection,str)
                and all(isinstance(item,str) for item in [config.approval_sha256,config.receipt_sha256,config.approval_path])
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',config.collection)
                and SHA.fullmatch(config.approval_sha256) and SHA.fullmatch(config.receipt_sha256)
                and type(config.max_file_bytes) is int and 0<config.max_file_bytes<=256*1024**2,'invalid_service_configuration')
        path=Path(config.approval_path)
        require(path.is_absolute() and path==path.resolve() and path.is_relative_to(Path(state)/'compute'),'invalid_service_configuration')
        require(all(isinstance(item,str) and item.startswith('/') for item in [config.modal_python,config.modal_config]),'invalid_service_configuration')
        return config

    @property
    def fingerprint(self): return digest(self.__dict__)


class SourceHost:
    """Read-only readiness projection plus atomic, provider-free HTTP admission."""
    def __init__(self,state,*,company_id=None,clock=time.time,read_only=False):
        self.state=Path(state).absolute();self.company=company_id or workspace_scope(self.state)
        self.read_only=read_only
        self.root=self.state/'source-service';self.admissions=self.root/'admissions';self.clock=clock
        if not read_only:
            private_dir(self.root);private_dir(self.admissions)
        self.code_sha256=LOADED_CODE_SHA256

    def configuration(self): return Configuration.load(self.state,self.company)

    def code_current(self):
        try:return code_identity()==self.code_sha256
        except (OSError,ValueError,AttributeError):return False

    def records(self):
        for folder in (self.root,self.admissions):
            require(folder.absolute()==folder.resolve(),'invalid_service_storage')
            if folder.exists():
                info=folder.stat()
                require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077,'invalid_service_storage')
        paths=sorted(self.admissions.glob('*.json'))
        require(len(paths)<=1000,'source_admission_limit')
        records=[]
        for path in paths:
            require(SHA.fullmatch(path.stem),'invalid_service_record')
            row=read(path)
            require(row.get('schema_version')==1 and row.get('company_id')==self.company
                    and row.get('approval_sha256')==path.stem,'invalid_service_record')
            records.append(row)
        return records

    def save(self,record):
        require(not self.read_only,'read_only_workspace')
        require(SHA.fullmatch(record.get('approval_sha256','')),'invalid_service_record')
        atomic_json(self.admissions/(record['approval_sha256']+'.json'),record)

    def healthy(self,config=None):
        try:
            config=config or self.configuration()
            if config is None or not config.enabled:return False
            current=read(self.root/'heartbeat.json');now=self.clock()
            return (current.get('schema_version')==1 and current.get('company_id')==self.company
                and current.get('config_sha256')==config.fingerprint and current.get('ready') is True
                and current.get('code_sha256')==self.code_sha256 and self.code_current()
                and type(current.get('checked_at')) in (float,int)
                and current['checked_at']<=now+1 and 0<=now-current['checked_at']<=HEARTBEAT_TTL
                and current.get('expires_at')==current['checked_at']+HEARTBEAT_TTL)
        except (OSError,ValueError,TypeError):return False

    def policy(self):
        try: config=self.configuration()
        except (OSError,ValueError): config=None
        return SourceRoutePolicy(intake_enabled=bool(config and config.enabled),worker_ready=self.healthy(config),
            empty_workspace_collection=config.collection if config else None)

    def approval(self,config):
        value=compute_guard.validate_approval(config.approval_path,workspace=WORKSPACE,
            planned_gross_usd='1.00',purpose=PURPOSE)
        require(value.digest==config.approval_sha256,'embedding_allowance_required')
        claimed=value.path.with_name(f'approval-{value.approval_id}.claimed.json')
        require(not claimed.exists(),'embedding_allowance_required')
        return value

    def collections(self,actor):
        # Admission receipts remain authoritative for already owned transfers
        # when the host rotates or removes a new-upload configuration.
        return {row['binding']['collection'] for row in self.records() if row['principal_id']==actor.principal_id}

    def readiness(self,actor,*,collection=None):
        """Safe observed admission state; continuation survives config removal."""
        result={'available':False,'worker_ready':False,'can_upload':False,
            'can_manage_imports':False,'admission':'setup_required','notice':''}
        try:
            records=self.records()
            result['can_manage_imports']=not actor.read_only and any(row['principal_id']==actor.principal_id
                and (collection is None or row['binding']['collection']==collection) for row in records)
            config=self.configuration()
            eligible=bool(config and config.enabled and config.principal_id==actor.principal_id
                and (collection is None or config.collection==collection))
            if not eligible:return result
            reserved=any(row['approval_sha256']==config.approval_sha256 for row in records)
            try: self.approval(config);funded=True
            except (OSError,ValueError):funded=False
            healthy=self.healthy(config)
            result.update(available=healthy,worker_ready=healthy,
                can_upload=not actor.read_only and healthy and funded and not reserved,
                admission='reserved' if reserved else 'allowance_required' if not funded else 'ready' if healthy else 'worker_unavailable',
                notice='The current upload allowance is reserved.' if reserved else
                    'Your workspace owner can add an upload allowance.' if not funded else
                    '' if healthy else 'The import worker is awaiting readiness.')
        except (OSError,ValueError,KeyError,TypeError):
            result.update(admission='status_unavailable',notice='Import status is temporarily unavailable.')
        return result

    def capabilities(self,actor):
        state=self.readiness(actor)
        result={key:state[key] for key in ('can_upload','can_manage_imports')}
        if state['notice']:result['notice']=state['notice']
        return result

    def begin_remote(self,store,actor,source_id,*,filename,size,idempotency_key,
                     object_key,source_version,current):
        """Host callback for fixed storage readers; reserve the ordinary file allowance.

        A recovery pass can resume a remote reservation only when its owning
        adapter supplies a fresh authority guard. The source worker never opens
        a remote server or fabricates that authority from an old admission.
        """
        require(callable(current) and current() is True,'acquisition_stopped')
        require(store.root.parent.resolve()==self.state.resolve(),'workspace_access_required')
        source=store.get_source(actor,source_id)
        require(source['kind'] in {'sftp','shared_link'},'record_not_found')
        require(isinstance(object_key,str) and 0<len(object_key.encode())<=1024
            and all(ord(char)>=32 and ord(char)!=127 for char in object_key)
            and isinstance(source_version,str) and SHA.fullmatch(source_version),'invalid_source_version')
        return self.begin(store,actor,collection=source['collection'],filename=filename,size=size,
            idempotency_key=idempotency_key,source_id=source_id,current=current,
            _remote={'object_key':object_key,'source_version':source_version,'kind':source['kind']})

    def begin(self,store,actor,*,collection,filename,size,idempotency_key,source_id=None,
              current=None,_remote=None):
        require(not self.read_only,'read_only_workspace')
        require(not actor.read_only,'read_only_workspace')
        if _remote is not None:require(callable(current) and current() is True,'acquisition_stopped')
        require(isinstance(filename,str) and filename.strip() and len(filename.encode())<=255 and all(ord(c)>=32 for c in filename)
                and '/' not in filename and '\\' not in filename and filename not in {'.','..'},'invalid_filename')
        require(isinstance(idempotency_key,str) and idempotency_key.strip() and len(idempotency_key.encode())<=160
                and all(ord(c)>=32 for c in idempotency_key),'invalid_idempotency_key')
        binding={'principal_id':actor.principal_id,'collection':collection,'filename':filename,'size':size,
                 'idempotency_key':idempotency_key,'source_id':source_id}
        if _remote is not None:binding['remote']=_remote
        with lock(self.root/'admission.lock'):
            # A late create retry belongs to its original grant, even after a new
            # configuration is installed or the original approval is consumed.
            for record in self.records():
                if record['principal_id']==actor.principal_id and record['binding']['idempotency_key']==idempotency_key:
                    require(record['binding']==binding,'idempotency_conflict')
                    return self._bind(store,record,current=current)
            config=self.configuration()
            require(config and config.enabled and self.healthy(config),'source_service_unavailable')
            require(config.principal_id==actor.principal_id and config.collection==collection,'workspace_access_required')
            require(type(size) is int and 0<size<=config.max_file_bytes,'file_size_limit')
            require(not (self.admissions/(config.approval_sha256+'.json')).exists(),'embedding_allowance_reserved')
            try: approval=self.approval(config)
            except (OSError,ValueError):raise IntakeError('embedding_allowance_required') from None
            record={'schema_version':1,'company_id':self.company,'principal_id':actor.principal_id,
                'approval_sha256':config.approval_sha256,'approval_path':config.approval_path,
                'approval_id':approval.approval_id,'config_sha256':config.fingerprint,
                'binding':binding,'status':'reserving','created_at':self.clock()}
            self.save(record) # Slot is durable BEFORE creating its transfer.
            try:return self._bind(store,record,current=current)
            except IntakeError as error:
                if str(error) in {'record_not_found','invalid_filename','invalid_metadata','upload_capacity','asset_limit','storage_quota','source_limit','invalid_size_or_offset','acquisition_stopped'}:
                    # Prove absence under the store commit lock before releasing
                    # an unused allowance. A committed transfer with a lost reply
                    # retains its reservation for exact idempotent recovery.
                    with store._write() as db:
                        exists=db.execute('SELECT 1 FROM uploads WHERE owner=? AND request_key=?',
                            (actor.principal_id,idempotency_key)).fetchone()
                        if exists is None:
                            path=self.admissions/(record['approval_sha256']+'.json');path.unlink()
                            directory=os.open(self.admissions,os.O_RDONLY|os.O_DIRECTORY)
                            try:os.fsync(directory)
                            finally:os.close(directory)
                raise

    def _bind(self,store,record,*,current=None):
        actor=IntakeActor(record['principal_id']);body=record['binding']
        remote=body.get('remote')
        if remote is not None:require(callable(current) and current() is True,'acquisition_stopped')
        if record.get('upload_id'):return store.get_import(actor,record['upload_id'])['upload']
        if body['source_id']:
            source=store.get_source(actor,body['source_id'])
            require(source['kind']==(remote['kind'] if remote else 'upload') and source['collection']==body['collection'],'record_not_found')
        else: source=store.ensure_upload_source(actor,collection=body['collection'])
        extra={'object_key':remote['object_key'],'source_version':remote['source_version'],'current':current} if remote else {}
        upload=store.begin_upload(actor,source['id'],filename=body['filename'],size=body['size'],idempotency_key=body['idempotency_key'],**extra)
        record.update(status='reserved',upload_id=upload['id'],asset_id=upload['asset_id'])
        self.save(record);return upload

    def authorize(self,actor,view,*,operation):
        require(not actor.read_only,'read_only_workspace')
        # view already passed SourceStore's principal ownership check. Stop needs
        # no remaining budget or live worker; reconciliation confirms cleanup.
        if operation=='cancel':return
        require(operation in {'content','complete'},'invalid_import_operation')
        row=next((record for record in self.records() if record.get('upload_id')==view['upload']['id']
                  and record['principal_id']==actor.principal_id),None)
        require(row is not None and row['binding']['collection']==view['source']['collection']
                and row['binding']['size']==view['upload']['expected_bytes'],'record_not_found')

    def recover_admissions(self,store):
        with lock(self.root/'admission.lock'):
            for record in self.records():
                if record['status']=='reserving' and 'remote' not in record['binding']:self._bind(store,record)


def root_bytes(path,*,expected_uid=0):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==expected_uid and info.st_nlink==1
                and not info.st_mode&0o022 and info.st_size<=1024*1024,'invalid_commission_receipt')
        with os.fdopen(fd,'rb',closefd=False) as stream:return stream.read(1024*1024+1)
    finally:os.close(fd)


class HealthProbe:
    """Verify the installed proof and pinned sources, then test free local peers."""
    def __init__(self,host,config,media,qdrant,*,receipt_path=ROOT_RECEIPT,root_reader=root_bytes):
        self.host,self.config,self.media,self.qdrant=host,config,media,qdrant
        self.receipt_path=Path(receipt_path);self.root_reader=root_reader

    def installed(self):
        raw=self.root_reader(self.receipt_path)
        require(hashlib.sha256(raw).hexdigest()==self.config.receipt_sha256,'commission_receipt_changed')
        receipt=json.loads(raw)
        expected=[{'purpose':'prepare','profile':PROFILE,'recipe':'rocoto-cfr24-h264-segments-v2','image_digest':IMAGE}]
        expected.extend({'purpose':purpose,'profile':DERIVE_PROFILE,'recipe':'rocoto-source-derive-v1','image_digest':DERIVE_IMAGE}
                        for purpose in ['detail_video_v1','inspect_frames_v1','source_cut_v1'])
        require(receipt.get('schema_version')==1 and receipt.get('profiles')==expected
                and receipt.get('preview_uid')==os.geteuid() and receipt.get('preview_gid')==os.getegid()
                and SHA.fullmatch(receipt.get('installed_proof_sha256','')),'invalid_commission_receipt')
        sources=receipt.get('sources',[])
        legacy={'source_media.py','native_executor.py','broker_service.py'}
        current=legacy|{'native_package_mount.py'}
        documents=current|{'document_preview_contract.py'}
        require(isinstance(sources,list) and all(isinstance(entry,dict) for entry in sources)
                and {entry.get('path') for entry in sources} in (legacy,current,documents)
                and len(sources)==len({entry['path'] for entry in sources}),'invalid_commission_receipt')
        for entry in sources:
            require(SHA.fullmatch(entry.get('sha256','')) and hashlib.sha256(self.root_reader(self.receipt_path.parent/entry['path'])).hexdigest()==entry['sha256'],'commission_source_changed')
        runtime(self.config.modal_python,self.config.modal_config)
        return receipt['installed_proof_sha256']

    async def __call__(self):
        proof=await _thread(self.installed)
        seed=hashlib.sha256((self.host.company+':source-service-health').encode()).hexdigest()
        scope=MediaScope(company_id=self.host.company,owner_id=self.config.principal_id,
            asset_id='asset-'+seed[:32],job_id='job-'+seed[32:])
        require(await self.media.recover(scope,1) is None,'unexpected_health_lease')
        result=await _thread(self.qdrant.request,'GET','/collections')
        require(isinstance(result.get('collections'),list),'qdrant_unavailable')
        return {'media':True,'derivatives':True,'qdrant':True,'embedding_runtime':True,
                'commission_proof_sha256':proof}


class SourceService:
    def __init__(self,host,store,worker,probe,*,sleep_seconds=.5):
        self.host,self.store,self.worker,self.probe=host,store,worker,probe
        self.config=host.configuration();require(self.config is not None,'source_service_unconfigured')
        self.sleep_seconds=sleep_seconds;self.instance=uuid.uuid4().hex;self.stopping=asyncio.Event()
        self.ready=False;self.recovering=True;self.components={};self.failure=None

    def write_health(self):
        now=self.host.clock()
        atomic_json(self.host.root/'heartbeat.json',{'schema_version':1,'company_id':self.host.company,
            'instance_id':self.instance,'pid':os.getpid(),'config_sha256':self.config.fingerprint,
            'code_sha256':self.host.code_sha256,
            'checked_at':now,'expires_at':now+HEARTBEAT_TTL,'ready':bool(self.config.enabled and self.ready and not self.recovering and not self.stopping.is_set() and self.host.code_current()),
            'components':self.components,'failure':self.failure})

    async def pulse(self):
        while not self.stopping.is_set():
            try:
                current=await _thread(self.host.configuration)
                require(current and current.fingerprint==self.config.fingerprint,'source_configuration_changed')
                require(await _thread(self.host.code_current),'source_code_changed')
                self.components=await self.probe()
                require(all(self.components.get(key) is True for key in ['media','derivatives','qdrant','embedding_runtime'])
                        and SHA.fullmatch(self.components.get('commission_proof_sha256','')),'source_check_incomplete')
                self.ready=True;self.failure=None
            except Exception:
                self.ready=False;self.components={};self.failure='source_service_check_failed'
            await _thread(self.write_health)
            try:await asyncio.wait_for(self.stopping.wait(),HEARTBEAT_SECONDS)
            except TimeoutError:pass

    def _jobs(self):
        records=self.host.records();known={row.get('asset_id'):row for row in records if row.get('asset_id')}
        with self.store._read() as db:
            rows=[dict(row) for row in db.execute("SELECT j.*,a.owner FROM jobs j JOIN assets a ON a.id=j.asset_id WHERE j.status IN ('running','needs_reconciliation')")]
        result=[]
        for row in rows:
            if row['asset_id'] not in known:continue
            require(row['owner']==known[row['asset_id']]['principal_id'],'invalid_recovery_scope')
            row['checkpoint']=json.loads(row['result']) if row['result'] else None
            result.append(row)
        return result

    def _attempts(self,job):
        root=getattr(self.worker.embedding,'root',None)
        if root is None:return []
        paths=sorted(Path(root).glob('embedding-*/attempt.json'))
        require(len(paths)<=1000,'source_attempt_limit')
        values=[]
        for path in paths:
            record=read(path);scope=record.get('binding',{}).get('scope',{})
            if scope.get('job_id')!=job['id']:continue
            require(scope.get('company_id')==self.host.company and scope.get('asset_id')==job['asset_id']
                and scope.get('owner_id')==job['owner'],'invalid_recovery_scope')
            values.append(record)
        return values

    async def reconcile(self):
        """Stop stale owned effects; retries and financial holds remain explicit."""
        await _thread(self.store.recover)
        jobs=await _thread(self._jobs)
        unresolved=False
        for job in jobs:
            if job['status']=='running':unresolved=True;continue # Await the existing lease fence.
            checkpoint=job['checkpoint'] or {};confirmed=True;evidence=[]
            try:
                if job['stage']=='prepare' and checkpoint:
                    require(checkpoint.get('phase') in {'media_submit_intent','media_running'},'unknown_recovery_phase')
                    scope=MediaScope(**checkpoint['scope'])
                    require(scope.company_id==self.host.company and scope.owner_id==job['owner']
                        and scope.asset_id==job['asset_id'] and scope.job_id==job['id'],'invalid_recovery_scope')
                    handle=await self.worker.media.recover(scope,checkpoint['generation'])
                    if handle:
                        result=await self.worker.media.stop(scope,handle)
                        require(result.get('cleanup_confirmed') is True,'media_cleanup_unconfirmed')
                        await self.worker.media.release(scope,handle)
                    evidence.append('media_stopped')
                elif job['stage']=='embed':
                    attempts=await _thread(self._attempts,job)
                    require(bool(attempts) or not checkpoint.get('admissions'),'embedding_receipt_missing')
                    for record in attempts:
                        if record['status'] in {'claiming','admission_failed','admitted'}:
                            evidence.append('embedding_not_launched');continue
                        result=await self.worker.embedding.stop(public_attempt(record))
                        require(result.get('stop_confirmed') is True,'embedding_cleanup_unconfirmed')
                        evidence.append('embedding_stopped_hold_retained')
                elif job['stage']=='publish':
                    # Publication reconciliation preserves the uncertain index and
                    # never promotes source readiness from a process heartbeat.
                    evidence.append('publication_requires_review')
                elif checkpoint:raise IntakeError('unknown_recovery_phase')
                reference='source-cleanup-'+uuid.uuid4().hex
                atomic_json(self.host.root/(reference+'.json'),{'job_id':job['id'],'generation':job['generation'],
                    'asset_id':job['asset_id'],'confirmed_at':self.host.clock(),'evidence':evidence})
                await _thread(self.store.reconcile_job,job['id'],expected_generation=job['generation'],
                    outcome='cancelled' if job['error']=='cancel_requested' else 'failed',evidence_reference=reference)
            except Exception:confirmed=False
            unresolved |= not confirmed
        self.recovering=unresolved
        return not unresolved

    async def step(self):
        was_recovering=self.recovering
        await self.reconcile()
        current=await _thread(self.host.configuration)
        if current is None or current.fingerprint!=self.config.fingerprint:
            self.ready=False;self.failure='source_configuration_changed';await _thread(self.write_health);return None
        if not await _thread(self.host.code_current):
            self.ready=False;self.failure='source_code_changed';await _thread(self.write_health);return None
        if was_recovering and not self.recovering:await _thread(self.write_health)
        if not self.config.enabled or not self.ready or self.recovering or not self.host.healthy(self.config):return None
        records=await _thread(self.host.records)
        # Publication uses already validated embeddings, including an older
        # admission whose host configuration has since rotated. Only the current
        # unspent approval can admit preparation/embedding; no grant is reused.
        funded=False
        try:await _thread(self.host.approval,self.config);funded=True
        except (OSError,ValueError):pass
        for record in records:
            if not record.get('asset_id'):continue
            stages=('prepare','embed','publish') if funded and record['approval_sha256']==self.config.approval_sha256 else ('publish',)
            result=await self.worker.run_once(worker_id='source-service-'+self.instance,stages=stages,asset_id=record['asset_id'])
            if result is not None:return result
        return None

    async def run(self):
        with lock(self.host.root/'service.lock',exclusive=True):
            await _thread(self.write_health)
            await _thread(self.host.recover_admissions,self.store)
            pulse=asyncio.create_task(self.pulse());active=None
            try:
                while not self.stopping.is_set():
                    active=asyncio.create_task(self.step())
                    stopped=asyncio.create_task(self.stopping.wait())
                    finished,_=await asyncio.wait([active,stopped],return_when=asyncio.FIRST_COMPLETED)
                    if stopped in finished and not active.done():active.cancel()
                    stopped.cancel();await asyncio.gather(stopped,return_exceptions=True)
                    try:await active
                    except asyncio.CancelledError:
                        if not self.stopping.is_set():raise
                    except Exception:self.ready=False;self.failure='source_worker_requires_attention'
                    if not self.stopping.is_set():
                        try:await asyncio.wait_for(self.stopping.wait(),self.sleep_seconds)
                        except TimeoutError:pass
            finally:
                self.stopping.set();self.ready=False;await _thread(self.write_health)
                if active and not active.done():active.cancel();await asyncio.gather(active,return_exceptions=True)
                await pulse
                await self.reconcile()
                self.ready=False;await _thread(self.write_health)


async def main_async(state):
    host=SourceHost(state);config=host.configuration()
    require(config is not None,'source_service_unconfigured')
    store=SourceStore(state);media=MediaClient();qdrant=LocalQdrant(timeout=5)
    embedding=SourceEmbedding(state,company_id=host.company,approval_path=config.approval_path,
        modal_python=config.modal_python,modal_config=config.modal_config,profile='small')
    probe=HealthProbe(host,config,media,qdrant)
    service=None
    publisher=IndexPublisher(Path(state)/'cosmos',resolver_ready=lambda:bool(service and service.ready and not service.recovering),client=qdrant)
    limits=replace(WorkerLimits(),embedding_batch_clips=48,embedding_import_clips=48,embedding_import_bytes=64*1024**2)
    worker=SourceWorker(store,company_id=host.company,media_client=media,media_image=IMAGE,
        embedding_adapter=embedding,publisher=publisher,enabled=config.enabled,limits=limits)
    service=SourceService(host,store,worker,probe)
    loop=asyncio.get_running_loop()
    for event in [signal.SIGINT,signal.SIGTERM]:loop.add_signal_handler(event,service.stopping.set)
    await service.run()


def main():
    parser=argparse.ArgumentParser(description='Run the explicitly commissioned company source worker.')
    parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(main_async(args.state.absolute()))


if __name__=='__main__':main()
