"""Fixed offline installer operations inside the existing privileged broker.

The preview UID is authenticated by BrokerServer. A commissioned source allowlist
and immutable worker script determine what may be installed; no caller commands,
host paths, environment variables, provider credentials or account homes enter.
"""
from __future__ import annotations
import asyncio
from contextlib import suppress
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

import native_executor as native
import native_install_worker as worker
import native_package_mount as packages

INSTALL_VOLUME=512*1024*1024
MAX_RETAINED=4
MAX_JOBS=2048
INSTALL_SECONDS=150
TRANSFER_SECONDS=60

async def drain(task):
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:continue
    return task.result()

class Installer:
    def __init__(self,broker,*,allowed_sources,worker_path=None,backend=None):
        self.broker=broker;self.root=broker.root/'installer';self.root.mkdir(mode=0o700,exist_ok=True)
        self.sources={value['plugin_id']:value for value in allowed_sources}
        for value in self.sources.values():worker.request({'source':value,'archive_sha256':'0'*64,'archive_bytes':1})
        self.worker_path=Path(worker_path or Path(__file__).with_name('native_install_worker.py'))
        info=self.worker_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o022:raise native.ExecutorError('invalid_installer_script')
        self.worker_sha256=hashlib.sha256(self.worker_path.read_bytes()).hexdigest()
        self.backend=backend or InstallDocker(self.root/'volumes',self.worker_path)
        self.db=sqlite3.connect(self.root/'jobs.sqlite3',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA max_page_count=32768')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,request_id TEXT NOT NULL,token TEXT NOT NULL,
            request TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,result TEXT,UNIQUE(scope_key,request_id))''')
        self.uploads={};self.runs={};self.readers={};self.cleanup_locks={}
        broker.installer=self

    def live_count(self):
        return self.db.execute("SELECT count(*) FROM jobs WHERE status NOT IN ('succeeded','failed','stopped','released')").fetchone()[0]

    def _row(self,identifier):return self.db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
    def _status(self,key,status,*,result=None):
        self.db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',(status,json.dumps(result) if result else None,key))
    def _lease(self,row):return {'id':row['id'],'token':row['token'],'scope_key':row['scope_key']}
    def _validate(self,scope,lease):
        if not isinstance(lease,dict) or set(lease)!={'id','token','scope_key'}:raise native.ExecutorError('invalid_install_lease')
        row=self._row(lease['id']) if isinstance(lease['id'],str) else None
        if not row or scope.key!=row['scope_key'] or lease['scope_key']!=scope.key or not secrets.compare_digest(str(lease['token']),row['token']):raise native.ExecutorError('install_scope_mismatch')
        return row
    def _public(self,row):
        # Inventory uses framed readback (up to 2 MiB), never the 64 KiB
        # control channel used to poll and stop every valid package size.
        return {'status':row['status'],'cleanup_confirmed':row['status'] in {'succeeded','failed','stopped','released'}}

    async def prepare(self,scope,request_id,document):
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-f0-9]{32}',request_id):raise native.ExecutorError('invalid_install_request')
        try:worker.request(document)
        except (ValueError,TypeError):raise native.ExecutorError('invalid_install_request') from None
        if self.sources.get(document['source']['plugin_id'])!=document['source']:raise native.ExecutorError('uncommissioned_plugin_source')
        async with self.broker.lock:
            old=self.db.execute('SELECT * FROM jobs WHERE scope_key=? AND request_id=?',(scope.key,request_id)).fetchone()
            if old:
                if json.loads(old['request'])!=document:raise native.ExecutorError('install_request_changed')
                return self._lease(old)
            if self.broker.live_count()>=native.MAX_WORKERS:raise native.ExecutorError('capacity_reached')
            if self.db.execute("SELECT count(*) FROM jobs WHERE status!='released'").fetchone()[0]>=MAX_RETAINED:raise native.ExecutorError('installer_output_capacity')
            if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0]>=MAX_JOBS:raise native.ExecutorError('installer_history_capacity')
            identifier=secrets.token_hex(24);token=secrets.token_hex(32)
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(identifier,scope.key,request_id,token,json.dumps(document),'prepared',time.time(),None))
            # Intent is durable before directories or a decoder/CLI exists.
            directory=self.root/'jobs'/identifier;directory.mkdir(mode=0o700,parents=True);(directory/'input').mkdir(mode=0o700)
            return self._lease(self._row(identifier))

    async def upload(self,scope,lease,reader,writer):
        async with self.broker.lock:
            row=self._validate(scope,lease)
            if row['status']!='prepared':raise native.ExecutorError('installer_not_prepared')
            self._status(row['id'],'receiving');self.uploads[row['id']]=asyncio.current_task()
        key=row['id'];document=json.loads(row['request']);directory=self.root/'jobs'/key/'input'
        try:
            async with asyncio.timeout(TRANSFER_SECONDS):
                await native.BrokerServer._reply(writer,{'status':'receiving'})
                remaining=document['archive_bytes'];hasher=hashlib.sha256()
                with (directory/'source.tar.gz').open('xb') as output:
                    while remaining:
                        kind,data=await native._read_package_frame(reader,min(65536,remaining))
                        if kind!=6 or not data:raise native.ExecutorError('invalid_install_frame')
                        output.write(data);hasher.update(data);remaining-=len(data)
                    output.flush();os.fsync(output.fileno());os.fchmod(output.fileno(),0o444)
                kind,data=await native._read_package_frame(reader,0)
                if kind!=7 or data or hasher.hexdigest()!=document['archive_sha256']:raise native.ExecutorError('install_archive_mismatch')
                (directory/'request.json').write_bytes(worker.canonical(document));(directory/'request.json').chmod(0o444);directory.chmod(0o555)
                async with self.broker.lock:
                    if self._validate(scope,lease)['status']!='receiving':raise native.ExecutorError('installation_stopped')
                    self._status(key,'running');self.runs[key]=asyncio.create_task(self._run(key))
                    self.runs[key].add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                return {'status':'running'}
        except BaseException:
            if self._row(key)['status']=='receiving':self._status(key,'uncertain')
            raise
        finally:self.uploads.pop(key,None)

    async def _run(self,key):
        successful=False
        try:
            if hashlib.sha256(self.worker_path.read_bytes()).hexdigest()!=self.worker_sha256:raise native.ExecutorError('installer_script_changed')
            await self.backend.run(key,self.root/'jobs'/key/'input')
            successful=True
        except BaseException:pass
        finally:
            try:await drain(asyncio.create_task(self._finish(key,successful)))
            finally:self.runs.pop(key,None)

    async def _finish(self,key,successful=False):
        async with self.cleanup_locks.setdefault(key,asyncio.Lock()):
            row=self._row(key)
            if row['status'] in {'succeeded','failed','stopped','released'}:return
            requested_stop=row['status']=='stopping'
            try:
                await self.backend.stop(key)
                result=None
                if successful and not requested_stop:
                    # Immutable file verification/copy/fsync must not block the
                    # shared control socket. Keep the volume and cleanup lock
                    # until the thread finishes, even through repeated Stop.
                    result=await drain(asyncio.create_task(asyncio.to_thread(self._freeze,key,json.loads(row['request']))))
                    if self._row(key)['status']=='stopping':
                        requested_stop=True;result=None
                await self.backend.unmount(key)
                directory=self.root/'jobs'/key/'input'
                if directory.exists():
                    directory.chmod(0o700)
                    for name in ('source.tar.gz','request.json'):(directory/name).unlink(missing_ok=True)
                    directory.rmdir()
                # Stop may also arrive during the awaited unmount operation.
                if self._row(key)['status']=='stopping':requested_stop=True;result=None
                self._status(key,'succeeded' if result else 'stopped' if requested_stop else 'failed',result=result)
            except BaseException:
                self._status(key,'uncertain')
                raise native.ExecutorError('installer_cleanup_uncertain') from None

    def _freeze(self,key,document):
        output=self.backend.volume_path(key)/'workspace/output'
        with packages.open_source(output,'result.json') as stream:
            raw=stream.read(packages.METADATA_BYTES+1)
        if len(raw)>packages.METADATA_BYTES:raise native.ExecutorError('installer_output_limit')
        result=json.loads(raw)
        if (result.get('schema_version')!=1 or result.get('source')!=document['source'] or result.get('archive_sha256')!=document['archive_sha256']
                or result.get('archive_bytes')!=document['archive_bytes'] or result.get('native')!={'pluginId':document['source']['plugin_id']+'@'+packages.MARKETPLACE,'installed':True,'enabled':True}):raise native.ExecutorError('installer_result_mismatch')
        package={'id':document['source']['plugin_id'],'version':result.get('version'),'tree_sha256':result.get('tree_sha256'),'files':result.get('files'),'directories':result.get('directories')}
        company='offline-installer';owner='offline-installer';ownerhash=hashlib.sha256(owner.encode()).hexdigest()
        metadata={'company':company,'owner':ownerhash,'generation':1,'packages':[package]}
        metadata['selection_sha256']=packages.digest({'company':company,'owner':ownerhash,'generation':1,'packages':[(package['id'],package['version'],package['tree_sha256'])]})
        packages.validate_metadata(metadata,company,owner)
        destination=self.root/'jobs'/key/'package';destination.mkdir(mode=0o700)
        for name in package['directories']:(destination/name).mkdir(mode=0o700)
        for item in package['files']:
            hasher=hashlib.sha256();size=0
            with packages.open_source(output/'package',item['path']) as source,(destination/item['path']).open('xb') as target:
                info=os.fstat(source.fileno())
                if info.st_size!=item['size_bytes'] or bool(info.st_mode&0o111)!=item['executable']:raise native.ExecutorError('installer_file_mismatch')
                while data:=source.read(65536):
                    size+=len(data)
                    if size>item['size_bytes']:raise native.ExecutorError('installer_file_mismatch')
                    target.write(data);hasher.update(data)
                if size!=item['size_bytes'] or hasher.hexdigest()!=item['sha256']:raise native.ExecutorError('installer_file_mismatch')
                target.flush();os.fsync(target.fileno());os.fchmod(target.fileno(),0o555 if item['executable'] else 0o444)
        packages.seal(destination)
        return result

    async def stop(self,scope,lease):
        async with self.broker.lock:
            row=self._validate(scope,lease);key=row['id']
            active=[t for t in [self.uploads.get(key),self.runs.get(key),*self.readers.get(key,set())] if t and t is not asyncio.current_task()]
            # Close read admission before awaiting existing readers. Successful
            # output already has confirmed worker cleanup; Stop consumes it.
            if row['status']=='succeeded':self._status(key,'stopped')
            elif row['status'] not in {'failed','stopped','released'}:self._status(key,'stopping')
            for task in active:task.cancel()
        if active:await drain(asyncio.ensure_future(asyncio.gather(*active,return_exceptions=True)))
        await drain(asyncio.create_task(self._finish(key)))
        return self._public(self._row(key))

    async def release(self,scope,lease):
        await self.stop(scope,lease)
        async with self.broker.lock:
            row=self._validate(scope,lease)
            if row['status']=='uncertain':raise native.ExecutorError('installer_cleanup_uncertain')
            self._discard(row['id'])
            return {'status':'released','cleanup_confirmed':True}

    def _discard(self,key):
        if self._row(key)['status'] not in {'succeeded','failed','stopped','released'}:
            raise native.ExecutorError('installer_cleanup_uncertain')
        directory=self.root/'jobs'/key
        if directory.exists():
            for path,_,_ in os.walk(directory):os.chmod(path,0o700)
            shutil.rmtree(directory)
        self._status(key,'released')

    async def reconcile(self):
        # Called before accepting clients / after all existing clients are drained.
        active=list(self.runs.values())+list(self.uploads.values())+[task for tasks in self.readers.values() for task in tasks]
        for task in active:task.cancel()
        if active:await drain(asyncio.ensure_future(asyncio.gather(*active,return_exceptions=True)))
        for row in self.db.execute("SELECT * FROM jobs WHERE status!='released'").fetchall():
            try:
                if row['status'] not in {'succeeded','failed','stopped'}:self._status(row['id'],'stopping')
                await self._finish(row['id'])
                self._discard(row['id'])
            except native.ExecutorError:pass

    async def expire(self):
        rows=self.db.execute("SELECT * FROM jobs WHERE (status='prepared' AND created<?) OR status='uncertain'",(time.time()-60,)).fetchall()
        for row in rows:
            key=row['id']
            # A failed stop remains an admission fence. Retry it on later ticks;
            # one daemon outage must not terminate expiry for all other workers.
            if key in self.uploads or key in self.runs or self.readers.get(key):continue
            try:
                self._status(key,'stopping');await self._finish(key);self._discard(key)
            except (native.ExecutorError,OSError):pass
        # Delivery buffers survive an active read, which has its own finite
        # transfer deadline. A following tick releases the completed buffer.
        for row in self.db.execute("SELECT * FROM jobs WHERE status IN ('succeeded','failed','stopped') AND created<?",(time.time()-300,)).fetchall():
            if self.readers.get(row['id']):continue
            try:self._discard(row['id'])
            except (native.ExecutorError,OSError):pass

    async def read(self,scope,lease,writer):
        row=self._validate(scope,lease)
        if row['status']!='succeeded':raise native.ExecutorError('installer_output_unavailable')
        key=row['id'];task=asyncio.current_task();self.readers.setdefault(key,set()).add(task)
        try:
            async with asyncio.timeout(TRANSFER_SECONDS):
                result=json.loads(row['result']);await native.BrokerServer._reply(writer,{'status':'ready'})
                await native._write_frame(writer,5,worker.canonical(result))
                for item in result['files']:
                    with packages.open_source(self.root/'jobs'/key/'package',item['path']) as stream:
                        while data:=stream.read(65536):await native._write_frame(writer,6,data)
                await native._write_frame(writer,7,b'')
        finally:self.readers[key].discard(task)

    async def handle(self,operation,scope,request,reader,writer):
        fields={'operation','scope'}|(set() if operation=='plugin_capabilities' else {'request_id','document'} if operation=='plugin_prepare' else {'lease'})
        native.BrokerServer._fields(request,fields)
        if operation=='plugin_capabilities':
            value={'schema_version':1,'available':True,'profile':'native-plugin-install-v1','image':native.IMAGE,
                   'worker_sha256':self.worker_sha256,'supported_plugin_ids':sorted(self.sources),
                   'catalogue_revisions':sorted({s['catalogue_revision'] for s in self.sources.values()}),
                   'limits':{'archive_bytes':worker.ARCHIVE_BYTES,'package_bytes':worker.PACKAGE_BYTES,'volume_bytes':INSTALL_VOLUME,
                             'max_workers_shared':native.MAX_WORKERS,'max_retained_outputs':MAX_RETAINED,'wall_seconds':INSTALL_SECONDS}}
        elif operation=='plugin_prepare':value=await self.prepare(scope,request['request_id'],request['document'])
        elif operation=='plugin_upload':value=await self.upload(scope,request['lease'],reader,writer)
        elif operation=='plugin_status':value=self._public(self._validate(scope,request['lease']))
        elif operation=='plugin_stop':value=await self.stop(scope,request['lease'])
        elif operation=='plugin_release':value=await self.release(scope,request['lease'])
        elif operation=='plugin_read':await self.read(scope,request['lease'],writer);return
        else:raise native.ExecutorError('unknown_operation')
        await native.BrokerServer._reply(writer,value)

class InstallDocker:
    def __init__(self,root,script):
        self.volumes=native.Ext4Volumes(root,size_bytes=INSTALL_VOLUME);self.script=Path(script)
        self.docker=('/opt/rocoto-native-executor/docker-29.8.2/docker','--host','unix:///run/rocoto-native-docker/docker.sock')
    @staticmethod
    def volume_key(key):return hashlib.sha256(('installer:'+key).encode()).hexdigest()
    @staticmethod
    def name(key):return 'rocoto-native-install-'+key
    def volume_path(self,key):return self.volumes.root/self.volume_key(key)/'mount'
    async def run(self,key,incoming):
        volume=await self.volumes.mount(self.volume_key(key))
        await native._command(*self.docker,'run','--name',self.name(key),'--label','com.rocoto.native-installer=true','--runtime',native.RUNTIME,'--network','none',
            '--memory','512m','--memory-swap','512m','--cpus','1','--pids-limit','64','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--log-driver','none',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=32m,mode=1777','--mount',f'type=bind,src={volume}/workspace,dst=/workspace',
            '--mount',f'type=bind,src={incoming},dst=/input,readonly','--mount',f'type=bind,src={self.script},dst=/installer/worker.py,readonly',
            '--entrypoint','python3',native.IMAGE,'/installer/worker.py',timeout=INSTALL_SECONDS)
    async def stop(self,key):
        async def present():return bool((await native._command(*self.docker,'ps','--all','--quiet','--filter','name=^/'+self.name(key)+'$')).strip())
        if await present():await native._command(*self.docker,'rm','--force',self.name(key))
        if await present():raise native.ExecutorError('installer_container_present')
    async def unmount(self,key):
        volume=self.volume_path(key)
        if volume.exists():await self.volumes.unmount(volume)
        parent=volume.parent
        if parent.exists():shutil.rmtree(parent)
