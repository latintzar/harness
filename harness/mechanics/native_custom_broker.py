"""Fixed custom-read jobs inside the privileged native broker.

The authenticated preview UID transfers bounded source bytes, never a command,
image, host path or provider credential. HTTP authority is a separate host grant.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import time

import custom_worker as worker
import native_executor as native
import native_package_mount as packages

VOLUME_BYTES=64*1024*1024
MAX_RETAINED=8
MAX_HISTORY=2048
RELEASED_SECONDS=86400
WALL_SECONDS=45
TRANSFER_SECONDS=30

async def drain(task):
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:continue
    return task.result()

async def silent_command(*argv,timeout):
    """Untrusted container console is discarded; all retained output has quota.

    A child may reach PID1 console descriptors even when the wrapper redirects
    its ordinary stdout. Never capture Docker-run output in host memory.
    """
    try:
        process=await asyncio.create_subprocess_exec(*map(str,argv),stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'})
    except OSError:raise native.ExecutorError('custom_runtime_unavailable')from None
    try:await asyncio.wait_for(process.wait(),timeout)
    except (asyncio.CancelledError,asyncio.TimeoutError)as error:
        if process.returncode is None:
            try:process.kill()
            except ProcessLookupError:pass
        await drain(asyncio.create_task(process.wait()))
        if isinstance(error,asyncio.TimeoutError):raise native.ExecutorError('custom_worker_timeout')from None
        raise
    if process.returncode:raise native.ExecutorError('custom_worker_failed')

class CustomExecutor:
    def __init__(self,broker,*,worker_path=None,backend=None):
        self.broker=broker;self.root=broker.root/'custom';self.root.mkdir(mode=0o700,exist_ok=True)
        self.worker_path=Path(worker_path or Path(__file__).with_name('custom_worker.py'))
        info=self.worker_path.lstat()
        if not stat.S_ISREG(info.st_mode)or info.st_uid!=os.geteuid()or info.st_mode&0o022:raise native.ExecutorError('invalid_custom_worker')
        self.worker_sha256=hashlib.sha256(self.worker_path.read_bytes()).hexdigest()
        self.backend=backend or CustomDocker(self.root/'volumes',self.worker_path)
        self.db=sqlite3.connect(self.root/'jobs.sqlite3',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA max_page_count=8192')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,request_id TEXT NOT NULL,
            token TEXT NOT NULL,request TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,result TEXT,
            UNIQUE(scope_key,request_id))''')
        self.uploads={};self.runs={};self.readers={};self.cleanup_locks={};broker.custom_executor=self

    def live_count(self):return self.db.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('succeeded','failed','stopped','released')").fetchone()[0]
    def _row(self,key):return self.db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
    def _status(self,key,status,result=None):self.db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',(status,json.dumps(result)if result is not None else None,key))
    def _socket_dir(self,key):return self.broker.run_root/('custom-'+key)
    def _lease(self,row):return {'id':row['id'],'token':row['token'],'scope_key':row['scope_key'],'socket_dir':str(self._socket_dir(row['id']))}
    def _validate(self,scope,lease):
        if not isinstance(lease,dict)or set(lease)!={'id','token','scope_key','socket_dir'}:raise native.ExecutorError('invalid_custom_lease')
        row=self._row(lease['id'])if isinstance(lease['id'],str)else None
        if not row or row['scope_key']!=scope.key or lease!=self._lease(row):raise native.ExecutorError('custom_scope_mismatch')
        return row
    def _public(self,row):return {'status':row['status'],'cleanup_confirmed':row['status']in {'succeeded','failed','stopped','released'}}

    async def prepare(self,scope,request_id,document):
        if not isinstance(request_id,str)or not re.fullmatch(r'[a-f0-9]{32}',request_id):raise native.ExecutorError('invalid_custom_request')
        try:worker.request(document)
        except (ValueError,TypeError):raise native.ExecutorError('invalid_custom_request')from None
        async with self.broker.lock:
            old=self.db.execute('SELECT * FROM jobs WHERE scope_key=? AND request_id=?',(scope.key,request_id)).fetchone()
            if old:
                if json.loads(old['request'])!=document:raise native.ExecutorError('custom_request_changed')
                return self._lease(old)
            if self.broker.live_count()>=native.MAX_WORKERS:raise native.ExecutorError('capacity_reached')
            if self.db.execute("SELECT count(*) FROM jobs WHERE status!='released'").fetchone()[0]>=MAX_RETAINED:raise native.ExecutorError('custom_output_capacity')
            if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0]>=MAX_HISTORY:raise native.ExecutorError('custom_history_capacity')
            key=secrets.token_hex(24)
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,NULL)',(key,scope.key,request_id,secrets.token_hex(32),json.dumps(document),'prepared',time.time()))
            # Durable intent precedes paths and any sandbox allocation.
            root=self.root/'jobs'/key;root.mkdir(mode=0o700,parents=True);(root/'source').mkdir(mode=0o700);(root/'input').mkdir(mode=0o700)
            sockets=self._socket_dir(key);sockets.mkdir(mode=0o700);os.chown(sockets,self.broker.app_uid,self.broker.app_gid)
            return self._lease(self._row(key))

    async def upload(self,scope,lease,reader,writer):
        async with self.broker.lock:
            row=self._validate(scope,lease)
            if row['status']!='prepared':raise native.ExecutorError('custom_not_prepared')
            self._status(row['id'],'receiving');self.uploads[row['id']]=asyncio.current_task()
        key=row['id'];root=self.root/'jobs'/key
        try:
            async with asyncio.timeout(TRANSFER_SECONDS):
                await native.BrokerServer._reply(writer,{'status':'receiving'})
                kind,raw=await native._read_package_frame(reader,worker.DOCUMENT_BYTES+worker.ARGUMENT_BYTES+64)
                if kind!=5:raise native.ExecutorError('invalid_custom_frame')
                try:value=worker.bundle(json.loads(raw),json.loads(row['request']),company=scope.company_id,owner=scope.owner_id,image=native.IMAGE,worker_sha256=self.worker_sha256)
                except (ValueError,TypeError,KeyError):raise native.ExecutorError('custom_snapshot_mismatch')from None
                for item in value['snapshot']['files']:
                    target=root/'source'/item['path'];target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    remaining=item['size_bytes'];hasher=hashlib.sha256()
                    with target.open('xb')as stream:
                        while remaining:
                            kind,data=await native._read_package_frame(reader,min(65536,remaining))
                            if kind!=6 or not data:raise native.ExecutorError('invalid_custom_frame')
                            stream.write(data);hasher.update(data);remaining-=len(data)
                        stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o444)
                    if hasher.hexdigest()!=item['sha256']:raise native.ExecutorError('custom_source_mismatch')
                kind,data=await native._read_package_frame(reader,0)
                if kind!=7 or data:raise native.ExecutorError('invalid_custom_frame')
                (root/'bundle.json').write_bytes(worker.canonical(value));(root/'bundle.json').chmod(0o600)
                packages.seal(root/'source')
                async with self.broker.lock:
                    if self._validate(scope,lease)['status']!='receiving':raise native.ExecutorError('custom_stopped')
                    self._status(key,'sealed')
                return {'status':'sealed','snapshot_sha256':json.loads(row['request'])['snapshot_sha256']}
        except BaseException:
            if self._row(key)['status']=='receiving':self._status(key,'uncertain')
            raise
        finally:self.uploads.pop(key,None)

    def _freeze_socket(self,key):
        root=self._socket_dir(key);info=root.lstat()
        if not stat.S_ISDIR(info.st_mode)or info.st_uid!=self.broker.app_uid:raise native.ExecutorError('invalid_custom_socket_directory')
        os.chown(root,os.geteuid(),self.broker.socket_group);root.chmod(0o510)
        if {p.name for p in root.iterdir()}!={'custom-http.sock'}:raise native.ExecutorError('custom_socket_required')
        path=root/'custom-http.sock';info=path.lstat()
        if not stat.S_ISSOCK(info.st_mode)or info.st_uid!=self.broker.app_uid or info.st_nlink!=1:raise native.ExecutorError('invalid_custom_socket')
        os.chown(path,self.broker.socket_owner,self.broker.socket_group,follow_symlinks=False);os.chmod(path,0o600,follow_symlinks=False)

    async def start(self,scope,lease,capability):
        if not isinstance(capability,str)or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',capability):raise native.ExecutorError('invalid_custom_capability')
        async with self.broker.lock:
            row=self._validate(scope,lease);key=row['id']
            if row['status']!='sealed':raise native.ExecutorError('custom_source_required')
            self._status(key,'starting')
            try:
                if hashlib.sha256(self.worker_path.read_bytes()).hexdigest()!=self.worker_sha256:raise native.ExecutorError('custom_worker_changed')
                self._freeze_socket(key);root=self.root/'jobs'/key;value=json.loads((root/'bundle.json').read_bytes());request=json.loads(row['request'])
                config={'snapshot_sha256':request['snapshot_sha256'],'entrypoint':value['snapshot']['manifest']['entrypoint'],
                    'tool':request['tool'],'arguments':value['arguments'],'gateway_token':capability}
                path=root/'input/run.json';path.write_bytes(worker.canonical(config));path.chmod(0o444);(root/'input').chmod(0o555)
                self._status(key,'running');self.runs[key]=asyncio.create_task(self._run(key));self.runs[key].add_done_callback(lambda t:t.exception()if not t.cancelled()else None)
            except BaseException:self._status(key,'uncertain');raise
        return {'status':'running'}

    async def _run(self,key):
        succeeded=False
        try:await self.backend.run(key,self.root/'jobs'/key,self._socket_dir(key));succeeded=True
        except BaseException:pass
        finally:
            try:await drain(asyncio.create_task(self._finish(key,succeeded)))
            finally:self.runs.pop(key,None)

    def _freeze_output(self,key,request):
        raw=worker.read_regular(self.backend.volume_path(key)/'workspace/result.json',worker.RESULT_BYTES)
        result=json.loads(raw);worker.need(isinstance(result,dict));raw=worker.canonical(result);worker.need(len(raw)<=worker.RESULT_BYTES)
        path=self.root/'jobs'/key/'result.json'
        with path.open('xb')as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o600)
        return {'schema_version':1,'snapshot_sha256':request['snapshot_sha256'],'tool':request['tool'],'arguments_sha256':request['arguments_sha256'],
            'result_sha256':hashlib.sha256(raw).hexdigest(),'result_bytes':len(raw),'profile':worker.PROFILE,'image_digest':native.IMAGE,'worker_sha256':self.worker_sha256}

    async def _finish(self,key,succeeded=False):
        async with self.cleanup_locks.setdefault(key,asyncio.Lock()):
            row=self._row(key)
            if row['status']in {'succeeded','failed','stopped','released'}:return
            stopped=row['status']=='stopping';result=None
            try:
                await self.backend.stop(key)
                if succeeded and not stopped:
                    try:result=await drain(asyncio.create_task(asyncio.to_thread(self._freeze_output,key,json.loads(row['request']))))
                    except (ValueError,OSError,KeyError,RecursionError):result=None
                if self._row(key)['status']=='stopping':stopped=True;result=None
                await self.backend.unmount(key)
                root=self.root/'jobs'/key
                for child in ('source','input'):
                    tree=root/child
                    if tree.exists():
                        for name,_,_ in os.walk(tree):os.chmod(name,0o700)
                        shutil.rmtree(tree)
                sockets=self._socket_dir(key)
                if sockets.exists():sockets.chmod(0o700);shutil.rmtree(sockets)
                if self._row(key)['status']=='stopping':stopped=True;result=None
                self._status(key,'succeeded'if result else'stopped'if stopped else'failed',result)
            except BaseException:
                self._status(key,'uncertain');raise native.ExecutorError('custom_cleanup_uncertain')from None

    async def stop(self,scope,lease):
        async with self.broker.lock:
            row=self._validate(scope,lease);key=row['id']
            active=[t for t in [self.uploads.get(key),self.runs.get(key),*self.readers.get(key,set())]if t and t is not asyncio.current_task()]
            if row['status']=='succeeded':self._status(key,'stopped')
            elif row['status']not in {'failed','stopped','released'}:self._status(key,'stopping')
            for task in active:task.cancel()
        if active:await drain(asyncio.ensure_future(asyncio.gather(*active,return_exceptions=True)))
        await drain(asyncio.create_task(self._finish(key)))
        return self._public(self._row(key))

    def _discard(self,key):
        if self._row(key)['status']not in {'succeeded','failed','stopped','released'}:raise native.ExecutorError('custom_cleanup_uncertain')
        root=self.root/'jobs'/key
        if root.exists():
            for name,_,_ in os.walk(root):os.chmod(name,0o700)
            shutil.rmtree(root)
        self._status(key,'released')

    async def release(self,scope,lease):
        await self.stop(scope,lease)
        async with self.broker.lock:self._validate(scope,lease);self._discard(lease['id'])
        return {'status':'released','cleanup_confirmed':True}

    async def read(self,scope,lease,writer):
        row=self._validate(scope,lease)
        if row['status']!='succeeded':raise native.ExecutorError('custom_output_unavailable')
        key=row['id'];task=asyncio.current_task();self.readers.setdefault(key,set()).add(task)
        try:
            async with asyncio.timeout(TRANSFER_SECONDS):
                await native.BrokerServer._reply(writer,{'status':'ready'})
                await native._write_frame(writer,5,worker.canonical(json.loads(row['result'])))
                with packages.open_source(self.root/'jobs'/key,'result.json')as stream:
                    while raw:=stream.read(65536):await native._write_frame(writer,6,raw)
                await native._write_frame(writer,7,b'')
        finally:self.readers[key].discard(task)

    async def reconcile(self):
        tasks=list(self.runs.values())+list(self.uploads.values())+[t for rows in self.readers.values()for t in rows]
        for task in tasks:task.cancel()
        if tasks:await drain(asyncio.ensure_future(asyncio.gather(*tasks,return_exceptions=True)))
        for row in self.db.execute("SELECT * FROM jobs WHERE status!='released'").fetchall():
            try:
                if row['status']not in {'succeeded','failed','stopped'}:self._status(row['id'],'stopping')
                await self._finish(row['id']);self._discard(row['id'])
            except native.ExecutorError:pass

    async def expire(self):
        for row in self.db.execute("SELECT * FROM jobs WHERE(status IN ('prepared','sealed')AND created<?)OR status IN ('uncertain','stopping')",(time.time()-60,)).fetchall():
            key=row['id']
            if key in self.uploads or key in self.runs or self.readers.get(key):continue
            try:self._status(key,'stopping');await self._finish(key);self._discard(key)
            except (native.ExecutorError,OSError):pass
        for row in self.db.execute("SELECT * FROM jobs WHERE status IN ('succeeded','failed','stopped')AND created<?",(time.time()-300,)).fetchall():
            if self.readers.get(row['id']):continue
            try:self._discard(row['id'])
            except (native.ExecutorError,OSError):pass
        # A released row is a request tombstone: it fences a prepare that arrives after its
        # cleanup, a race of seconds. A day later the row goes, so MAX_HISTORY bounds one
        # day of request identities and a host keeps taking jobs.
        self.db.execute("DELETE FROM jobs WHERE status='released' AND created<?",(time.time()-RELEASED_SECONDS,))

    async def handle(self,operation,scope,request,reader,writer):
        fields={'operation','scope'}|(set()if operation=='custom_capabilities'else {'request_id','retire'}if operation=='custom_recover'else {'request_id','document'}if operation=='custom_prepare'else {'lease','capability'}if operation=='custom_start'else {'lease'})
        native.BrokerServer._fields(request,fields)
        if operation=='custom_capabilities':value={'schema_version':1,'available':True,'profile':worker.PROFILE,'image_digest':native.IMAGE,'worker_sha256':self.worker_sha256,
            'limits':{'source_bytes':worker.SOURCE_BYTES,'source_files':worker.SOURCE_FILES,'result_bytes':worker.RESULT_BYTES,'wall_seconds':WALL_SECONDS,'volume_bytes':VOLUME_BYTES,'shared_workers':native.MAX_WORKERS}}
        elif operation=='custom_prepare':value=await self.prepare(scope,request['request_id'],request['document'])
        elif operation=='custom_recover':
            identifier=request['request_id']
            if not isinstance(identifier,str)or not re.fullmatch(r'[a-f0-9]{32}',identifier)or type(request['retire'])is not bool:raise native.ExecutorError('invalid_custom_request')
            async with self.broker.lock:
                row=self.db.execute('SELECT * FROM jobs WHERE scope_key=? AND request_id=?',(scope.key,identifier)).fetchone()
                if request['retire']:
                    if row is None:
                        if self.db.execute('SELECT count(*)FROM jobs').fetchone()[0]>=MAX_HISTORY:raise native.ExecutorError('custom_history_capacity')
                        # Recovery may overtake a delayed prepare handler. A
                        # durable request tombstone prevents later allocation;
                        # no source directory, socket or worker is created.
                        self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,NULL)',
                            (secrets.token_hex(24),scope.key,identifier,secrets.token_hex(32),'null','released',time.time()))
                    elif row['status']not in {'succeeded','failed','stopped','released'}:
                        self._status(row['id'],'stopping')
                value={'lease':self._lease(row)if row and row['request']!='null'else None}
        elif operation=='custom_upload':value=await self.upload(scope,request['lease'],reader,writer)
        elif operation=='custom_start':value=await self.start(scope,request['lease'],request['capability'])
        elif operation=='custom_status':value=self._public(self._validate(scope,request['lease']))
        elif operation=='custom_stop':value=await self.stop(scope,request['lease'])
        elif operation=='custom_release':value=await self.release(scope,request['lease'])
        elif operation=='custom_read':await self.read(scope,request['lease'],writer);return
        else:raise native.ExecutorError('unknown_operation')
        await native.BrokerServer._reply(writer,value)

class CustomDocker:
    def __init__(self,root,script):
        self.volumes=native.Ext4Volumes(root,size_bytes=VOLUME_BYTES);self.script=Path(script)
        self.docker=('/opt/rocoto-native-executor/docker-29.8.2/docker','--host','unix:///run/rocoto-native-docker/docker.sock')
    @staticmethod
    def volume_key(key):return hashlib.sha256(('custom:'+key).encode()).hexdigest()
    @staticmethod
    def name(key):return 'rocoto-native-custom-'+key
    def volume_path(self,key):return self.volumes.root/self.volume_key(key)/'mount'
    async def run(self,key,root,sockets):
        volume=await self.volumes.mount(self.volume_key(key))
        await silent_command(*self.docker,'run','--name',self.name(key),'--label','com.rocoto.native-custom=true',
            '--runtime',native.RUNTIME,'--network','none','--memory','512m','--memory-swap','512m','--cpus','1','--pids-limit','64',
            '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--log-driver','none',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=16m,mode=1777','--mount',f'type=bind,src={volume}/workspace,dst=/workspace',
            '--mount',f'type=bind,src={root}/source,dst=/source,readonly','--mount',f'type=bind,src={root}/input,dst=/input,readonly',
            '--mount',f'type=bind,src={sockets},dst=/run/rocoto,readonly','--mount',f'type=bind,src={self.script},dst=/worker/worker.py,readonly',
            '--entrypoint','python3',native.IMAGE,'/worker/worker.py',timeout=WALL_SECONDS)
    async def stop(self,key):
        async def present():return bool((await native._command(*self.docker,'ps','--all','--quiet','--filter','name=^/'+self.name(key)+'$')).strip())
        if await present():await native._command(*self.docker,'rm','--force',self.name(key))
        if await present():raise native.ExecutorError('custom_container_present')
    async def unmount(self,key):
        volume=self.volume_path(key)
        if volume.exists():await self.volumes.unmount(volume)
        if volume.parent.exists():shutil.rmtree(volume.parent)
