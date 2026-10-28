"""Host lifecycle for fixed custom adapters, observed HTTPS and durable cleanup.

One company instance; trusted owner/collection come from the authenticated host.
Source, worker output and model-authored receipts never grant execution authority.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager,contextmanager
from dataclasses import asdict,dataclass,field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import time

from jsonschema import Draft202012Validator
from custom_execution import CustomExecution,TestLease,ExecutionError,encoded,digest
from native_executor import NativeScope

MAX_BATCH_SECONDS=180
MAX_INVOKE_SECONDS=60
MAX_RESULT_BYTES=512*1024
TERMINAL={'succeeded','failed','stopped','released'}

class CoordinatorError(ValueError):pass

def require(value,code='custom_execution_failed'):
    if not value:raise CoordinatorError(code)

async def drain(task):
    """Cleanup/admission remain owned through repeated caller cancellation."""
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:continue
        except Exception:break
    if not task.cancelled():return task.result()

async def offload(fn,*args,**kwargs):
    task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:await drain(task)
        finally:raise


def _private_dir(path):
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode)and info.st_uid==os.getuid()and not info.st_mode&0o077,'custom_execution_private_state')

class _Journal:
    def __init__(self,root):
        self.root=Path(root);_private_dir(self.root);self.path=self.root/'coordinator.sqlite3'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            info=os.fstat(fd);require(stat.S_ISREG(info.st_mode)and info.st_uid==os.getuid()and not info.st_mode&0o077,'custom_execution_private_state')
        finally:os.close(fd)
        with self.db()as db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
            db.executescript('''CREATE TABLE IF NOT EXISTS batches(
                id TEXT PRIMARY KEY,connector TEXT NOT NULL,owner TEXT NOT NULL,collection TEXT NOT NULL,
                snapshot TEXT NOT NULL,kind TEXT NOT NULL,test_lease TEXT,state TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,batch TEXT NOT NULL,state TEXT NOT NULL,broker_run_id TEXT,receipt TEXT,check_json TEXT);
                CREATE INDEX IF NOT EXISTS batch_scope ON batches(connector,owner,collection,state);''')
        # Keep the WAL open while wire guards use short readonly connections.
        # Closing the last SQLite connection otherwise races another reader's
        # recovery/open, which can report BUSY_RECOVERY despite unchanged scope.
        self.anchor=sqlite3.connect(self.path.absolute().as_uri()+'?mode=ro',uri=True,check_same_thread=False)
        try:self.anchor.execute('SELECT count(*) FROM batches').fetchone()
        except BaseException:
            self.anchor.close();raise
    def close(self):
        if self.anchor is not None:
            self.anchor.close();self.anchor=None
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def insert_batch(self,batch):
        with self.db()as db:
            db.execute('BEGIN IMMEDIATE')
            require(db.execute('SELECT count(*)FROM batches').fetchone()[0]<1024,'custom_execution_history_full')
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?)',(batch.id,batch.key,batch.owner,batch.collection,
                batch.snapshot.sha256,batch.kind,json.dumps(asdict(batch.test_lease))if batch.test_lease else None,'running',time.time()))
    def pending(self,key=None,owner=None,collection=None):
        with self.db()as db:
            rows=db.execute("SELECT * FROM batches WHERE state IN ('running','stopping','cleanup_uncertain') ORDER BY created").fetchall()
            return [dict(r)for r in rows if(key is None or(r['connector'],r['owner'],r['collection'])==(key,owner,collection))]
    def request_stop(self,key,owner,collection):
        with self.db()as db:db.execute("UPDATE batches SET state='stopping' WHERE connector=? AND owner=? AND collection=? AND state='running'",(key,owner,collection))
    def allows_current(self,batch):
        db=None
        try:
            db=sqlite3.connect(self.path.absolute().as_uri()+'?mode=ro',uri=True,timeout=0)
            row=db.execute('SELECT state FROM batches WHERE id=?',(batch,)).fetchone()
            return bool(row and row[0]in {'running','passed','succeeded'})
        except sqlite3.OperationalError as exc:
            if (getattr(exc,'sqlite_errorcode',0)&0xff)in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED}:return None
            return False
        finally:
            if db is not None:db.close()
    def state(self,batch,state):
        with self.db()as db:db.execute('UPDATE batches SET state=? WHERE id=?',(state,batch))
    def finish(self,batch,state,current):
        with self.db()as db:
            db.execute('BEGIN IMMEDIATE');require(current(),'custom_execution_stopped')
            changed=db.execute("UPDATE batches SET state=? WHERE id=? AND state='running'",(state,batch)).rowcount
            require(changed==1,'custom_execution_stopped')
    def add_job(self,batch,job):
        with self.db()as db:db.execute('INSERT INTO jobs VALUES(?,?,?,NULL,NULL,NULL)',(job,batch,'admitting'))
    def jobs(self,batch):
        with self.db()as db:return [dict(r)for r in db.execute('SELECT * FROM jobs WHERE batch=? ORDER BY rowid',(batch,))]
    def job_state(self,job,state,*,broker=None,receipt=None,check=None):
        with self.db()as db:db.execute('UPDATE jobs SET state=?,broker_run_id=coalesce(?,broker_run_id),receipt=coalesce(?,receipt),check_json=coalesce(?,check_json) WHERE id=?',
            (state,broker,json.dumps(receipt)if receipt else None,json.dumps(check)if check else None,job))

class GatewaySocket:
    """Per-job UDS; gateway lifespan belongs to the company coordinator."""
    def __init__(self,gateway,directory):self.gateway=gateway;self.directory=Path(directory);self.task=None;self.sock=None;self.path=None;self.inode=None
    async def start(self):
        import uvicorn
        info=self.directory.lstat()
        require(self.directory.is_absolute()and stat.S_ISDIR(info.st_mode)and info.st_uid==os.getuid()and not info.st_mode&0o077,'custom_execution_socket_directory')
        self.path=self.directory/'custom-http.sock'
        require(len(os.fsencode(self.path))<108,'custom_execution_socket_path_limit')
        require(not self.path.exists()and not self.path.is_symlink(),'custom_execution_socket_exists')
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            self.sock.bind(str(self.path));os.chmod(self.path,0o600);self.inode=self.path.lstat().st_ino
            self.sock.listen(8);self.sock.setblocking(False)
            class SocketServer(uvicorn.Server):
                @contextmanager
                def capture_signals(self):yield # The owning application controls process signals.
            self.server=SocketServer(uvicorn.Config(self.gateway,log_config=None,access_log=False,lifespan='off',
                limit_concurrency=4,timeout_keep_alive=2,timeout_graceful_shutdown=2))
            self.task=asyncio.create_task(self.server.serve(sockets=[self.sock]))
            async with asyncio.timeout(5):
                while not self.server.started:
                    if self.task.done():await self.task;raise CoordinatorError('custom_execution_socket_failed')
                    await asyncio.sleep(.01)
        except BaseException:
            await drain(asyncio.create_task(self.close()));raise
        return self
    async def close(self):
        if self.task:
            self.server.should_exit=True
            try:await asyncio.wait_for(asyncio.shield(self.task),3)
            except (TimeoutError,asyncio.CancelledError):self.task.cancel();await drain(self.task)
        if self.sock:self.sock.close()
        if self.path:
            try:
                info=self.path.lstat()
                if stat.S_ISSOCK(info.st_mode)and info.st_ino==self.inode:self.path.unlink()
            except (FileNotFoundError,PermissionError):pass # Broker owns the frozen mount/path after start.

@dataclass
class _Batch:
    id:str
    key:str
    owner:str
    collection:str
    kind:str
    cancelled:asyncio.Event
    parent_cancelled:asyncio.Event
    snapshot:object=None
    test_lease:object=field(default=None,repr=False)
    task:object=field(default=None,repr=False)
    ready:bool=True
    external_current:object=field(default=None,repr=False)
    def ensure_current(self):
        if not self.current():self.cancelled.set();raise asyncio.CancelledError
    def current(self):return self.ready and not self.cancelled.is_set()and not self.parent_cancelled.is_set()and (self.task is None or not self.task.cancelling())and (self.external_current is None or self.external_current()is True)

class CustomCoordinator:
    def __init__(self,manager:CustomExecution,gateway,client,*,root=None,max_batches=2,batch_seconds=MAX_BATCH_SECONDS,
                 invoke_seconds=MAX_INVOKE_SECONDS,poll_seconds=.1,socket_factory=GatewaySocket):
        require(isinstance(manager,CustomExecution)and type(max_batches)is int and 1<=max_batches<=2)
        require(0<batch_seconds<=MAX_BATCH_SECONDS and 0<invoke_seconds<=MAX_INVOKE_SECONDS and .01<=poll_seconds<=1)
        self.manager,self.gateway,self.client=manager,gateway,client
        self.journal=_Journal(root or manager.store.root/'runtime')
        self.max_batches,self.batch_seconds,self.invoke_seconds,self.poll_seconds=max_batches,batch_seconds,invoke_seconds,poll_seconds
        self.socket_factory=socket_factory;self.active={};self.maintenance=set();self.closed=False;self.reconciling=False
    def _scope(self,batch):return NativeScope(self.manager.company,batch.owner,batch.id)
    @asynccontextmanager
    async def _lock(self,key,owner,collection,*,wait=False):
        name=digest([self.manager.company,key,owner,collection])+'.lock'
        fd=os.open(self.journal.root/name,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            info=os.fstat(fd);require(stat.S_ISREG(info.st_mode)and info.st_uid==os.getuid()and not info.st_mode&0o077,'custom_execution_private_state')
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if not wait:raise CoordinatorError('custom_execution_busy')from None
                    await asyncio.sleep(self.poll_seconds)
            yield
        finally:os.close(fd)
    async def status(self,key,*,owner,collection):
        value=await offload(self.manager.status,key,owner=owner,collection=collection)
        pending=await offload(self.journal.pending,key,owner,collection)
        return {**value,'coordinator_available':not self.closed and self.manager.runtime is not None,
            'execution_status':'cleanup_uncertain'if any(p['state']=='cleanup_uncertain'for p in pending)else'running'if pending else'idle'}
    async def test(self,key,*,owner,collection,expected_revision,cancelled):
        return await self._entry(key,owner,collection,'test',cancelled,expected_revision=expected_revision)
    async def invoke(self,key,tool,arguments,*,owner,collection,cancelled):
        return await self._entry(key,owner,collection,'invoke',cancelled,tool=tool,arguments=arguments)
    async def _entry(self,key,owner,collection,kind,parent_cancelled,**options):
        require(isinstance(parent_cancelled,asyncio.Event),'custom_execution_cancellation_required')
        require(not self.closed and self.manager.runtime is not None,'custom_execution_unavailable')
        require(not self.reconciling and len(self.active)<self.max_batches,'custom_execution_busy')
        if parent_cancelled.is_set():raise asyncio.CancelledError
        batch=_Batch('batch-'+secrets.token_hex(16),key,owner,collection,kind,asyncio.Event(),parent_cancelled)
        self.active[batch.id]=batch
        batch.task=asyncio.create_task(self._execute(batch,options))
        watcher=asyncio.create_task(self._watch(batch))
        try:return await asyncio.shield(batch.task)
        except asyncio.CancelledError:
            batch.cancelled.set();batch.task.cancel()
            try:await drain(batch.task)
            except Exception:pass
            raise
        finally:
            watcher.cancel();await drain(watcher);self.active.pop(batch.id,None)
    async def _watch(self,batch):
        while not batch.task.done():
            if batch.cancelled.is_set()or batch.parent_cancelled.is_set()or(batch.external_current is not None and batch.external_current()is False)or(batch.snapshot and self.manager.authority_probe(batch.snapshot,owner=batch.owner,collection=batch.collection)is False):
                batch.cancelled.set();batch.task.cancel();return
            await asyncio.sleep(self.poll_seconds)
    def _begin(self,batch,options):
        # Run as one drained host operation, so accepted test leases survive a
        # cancellation while SQLite is waiting before the journal insertion.
        if not batch.current():raise CoordinatorError('custom_execution_stopped')
        if batch.kind=='test':
            batch.test_lease=self.manager.begin_test(batch.key,owner=batch.owner,collection=batch.collection,expected_revision=options['expected_revision'])
            batch.snapshot=self.manager.test_snapshot(batch.test_lease,owner=batch.owner,collection=batch.collection)
        else:batch.snapshot=self.manager.active_snapshot(batch.key,owner=batch.owner,collection=batch.collection)
        self.journal.insert_batch(batch)
        batch.external_current=lambda:self.journal.allows_current(batch.id)
    async def _execute(self,batch,options):
        begun=False;completed=False
        try:
            async with self._lock(batch.key,batch.owner,batch.collection):
                require(not await offload(self.journal.pending,batch.key,batch.owner,batch.collection),'custom_execution_cleanup_required')
                try:
                    async with asyncio.timeout(self.batch_seconds if batch.kind=='test'else self.invoke_seconds):
                        await offload(self._begin,batch,options);begun=True
                        data=batch.snapshot.metadata
                        capabilities=await self.client.capabilities(self._scope(batch))
                        require(capabilities.get('available')is True and all(capabilities.get(k)==v for k,v in data['runtime'].items()),'custom_execution_runtime_mismatch')
                        tests=data['manifest']['tests']if batch.kind=='test'else [{'tool':options['tool'],'arguments':options['arguments']}]
                        checks=[];output=None
                        for test in tests:
                            batch.ensure_current()
                            check,output=await self._job(batch,test['tool'],test['arguments']);checks.append(check)
                        batch.ensure_current()
                        await offload(self.manager.validate_snapshot,batch.snapshot,owner=batch.owner,collection=batch.collection)
                        if batch.kind=='test':
                            evidence={'test_id':batch.test_lease.id,'snapshot_sha256':batch.snapshot.sha256,**data['runtime'],
                                'broker_run_id':batch.id,'cleanup_confirmed':True,'checks':checks}
                            value=await offload(self.manager.complete_test,batch.test_lease,evidence,owner=batch.owner,collection=batch.collection,current=batch.current)
                        else:value={'connector_id':batch.key,'tool':options['tool'],'result':output,'snapshot_sha256':batch.snapshot.sha256,'batch_id':batch.id}
                        batch.ensure_current()
                        await offload(self.journal.finish,batch.id,'passed'if batch.kind=='test'else'succeeded',batch.current)
                        batch.ensure_current()
                        completed=True;return value
                finally:
                    # _begin may have returned after cancellation while its
                    # thread was drained. Read its accepted state explicitly.
                    if (begun or batch.snapshot)and not completed:
                        await drain(asyncio.create_task(self._retire_batch(batch,'stopped'if batch.cancelled.is_set()or batch.parent_cancelled.is_set()else'failed')))
        except asyncio.CancelledError:raise
        except (CoordinatorError,ExecutionError):raise
        except Exception:raise CoordinatorError('custom_execution_failed')from None
    async def _job(self,batch,tool_name,arguments):
        data=batch.snapshot.metadata
        tool=next((t for t in data['manifest']['tools']if t['name']==tool_name),None)
        require(tool is not None and isinstance(arguments,dict)and len(encoded(arguments))<=16384 and Draft202012Validator(tool['input_schema']).is_valid(arguments),'custom_execution_input_schema')
        arguments=json.loads(encoded(arguments));job=secrets.token_hex(16);scope=self._scope(batch)
        lease=None;grant=None;listener=None;observations=();result=None;clean=False
        # This durable intent precedes broker prepare and provides recovery when
        # a prepare was accepted but its reply never reached this process.
        await offload(self.journal.add_job,batch.id,job)
        try:
            await offload(self.manager.admit_job,job,batch.snapshot,owner=batch.owner,collection=batch.collection)
            batch.ensure_current()
            preparing=asyncio.create_task(self.client.prepare(scope,job,batch.snapshot,tool_name,arguments))
            try:lease=await asyncio.shield(preparing)
            except asyncio.CancelledError:
                try:lease=await drain(preparing)
                except Exception:pass
                raise
            require(lease.scope_key==scope.key,'custom_execution_broker_scope')
            await offload(self.journal.job_state,job,'prepared',broker='custom-'+lease.id)
            await self.client.upload(scope,lease,batch.snapshot,tool_name,arguments)
            issuing=asyncio.create_task(self.gateway.issue(batch.snapshot,tool_name,
                validate=lambda s:self.manager.validate_snapshot(s,owner=batch.owner,collection=batch.collection),
                credentials=lambda s:self.manager.host_credentials(s,owner=batch.owner,collection=batch.collection),
                current=lambda s:batch.current()and self.manager.authority_current(s,owner=batch.owner,collection=batch.collection),
                cancelled=batch.cancelled,ttl_seconds=60))
            try:grant=await asyncio.shield(issuing)
            except asyncio.CancelledError:
                try:grant=await drain(issuing)
                except Exception:pass
                raise
            listener=self.socket_factory(self.gateway,lease.socket_dir)
            await listener.start();batch.ensure_current()
            await self.client.start(scope,lease,grant.token)
            await offload(self.journal.job_state,job,'running')
            while True:
                batch.ensure_current()
                state=await self.client.status(scope,lease)
                if state.get('status')in TERMINAL:
                    require(state.get('status')=='succeeded'and state.get('cleanup_confirmed')is True,'custom_execution_worker_failed');break
                require(state.get('status')in {'prepared','receiving','sealed','starting','running','stopping'},'custom_execution_cleanup_uncertain')
                await asyncio.sleep(self.poll_seconds)
            result=await self.client.result(scope,lease,batch.snapshot,tool_name,arguments)
        finally:
            # A separately shielded cleanup owns gateway tasks, socket and the
            # accepted broker request even under repeated task cancellation.
            observations,clean=await drain(asyncio.create_task(self._cleanup_job(batch,job,lease,grant,listener)))
        require(clean,'custom_execution_cleanup_uncertain')
        batch.ensure_current()
        require(isinstance(result,dict)and result.get('schema_version')==1 and result.get('snapshot_sha256')==batch.snapshot.sha256
            and result.get('tool')==tool_name and result.get('arguments_sha256')==digest(arguments)
            and all(result.get(k)==v for k,v in data['runtime'].items())and result.get('cleanup_confirmed')is True
            and result.get('broker_run_id')=='custom-'+lease.id,'custom_execution_result_identity')
        value=result.get('result');raw=encoded(value)
        require(isinstance(value,dict)and len(raw)<=MAX_RESULT_BYTES and type(result.get('result_bytes'))is int and result['result_bytes']==len(raw)
            and result.get('result_sha256')==hashlib.sha256(raw).hexdigest()and Draft202012Validator(tool['output_schema']).is_valid(value),'custom_execution_output_schema')
        receipts={}
        for record in observations:
            if(record.get('snapshot_sha256')==batch.snapshot.sha256 and record.get('operation')in tool['operations']
               and type(record.get('status'))is int and 200<=record['status']<300):
                receipts[record['operation']]=record['receipt_sha256']
        require(set(receipts)==set(tool['operations']),'custom_execution_operation_unverified')
        check={'tool':tool_name,'arguments_sha256':digest(arguments),'result_sha256':hashlib.sha256(raw).hexdigest(),'schema_valid':True,'operation_receipts':receipts}
        await offload(self.journal.job_state,job,'confirmed',check=check)
        return check,value
    async def _cleanup_job(self,batch,job,lease,grant,listener):
        receipts=();ok=True;outcome=None
        if grant:
            try:receipts=await self.gateway.revoke(grant.id)
            except Exception:ok=False
        # Close the socket before broker Stop, so the mount exposes no callable
        # gateway even when the broker reports uncertain container cleanup.
        if listener:
            try:await listener.close()
            except Exception:ok=False
        try:
            if lease is None:lease=await self.client.recover(self._scope(batch),job,retire=True)
            if lease is not None:
                require(lease.scope_key==self._scope(batch).key,'custom_execution_broker_scope')
                stopped=await self.client.stop(self._scope(batch),lease)
                require(stopped.get('cleanup_confirmed')is True and stopped.get('status')in TERMINAL,'custom_execution_cleanup_uncertain')
                outcome=await self.client.release(self._scope(batch),lease)
                require(outcome.get('cleanup_confirmed')is True and outcome.get('status')=='released','custom_execution_cleanup_uncertain')
            else:outcome={'status':'absent','cleanup_confirmed':True}
        except Exception:ok=False
        broker_id='custom-'+lease.id if lease else'custom-request-'+job
        receipt={'snapshot_sha256':batch.snapshot.sha256,'cleanup_confirmed':True,'broker_run_id':broker_id,
            'receipt_sha256':digest({'request_id':job,'scope':asdict(self._scope(batch)),'outcome':outcome})}
        try:
            if ok:
                known=await offload(self.manager.job_cleanup_state,job,owner=batch.owner,collection=batch.collection)
                require(known is not None or lease is None,'custom_execution_admission_missing')
                if known is not None:await offload(self.manager.confirm_job_cleanup,job,receipt,owner=batch.owner,collection=batch.collection)
                await offload(self.journal.job_state,job,'confirmed',broker=broker_id,receipt=receipt)
            else:
                batch.ready=False
                await offload(self.manager.mark_job_uncertain,job,owner=batch.owner,collection=batch.collection)
                await offload(self.journal.job_state,job,'cleanup_uncertain',broker=broker_id)
        except Exception:
            ok=False
            # Foundation's pre-admission row is still blocking even if the
            # status write fails; journal admission is also durable beforehand.
            await offload(self.journal.job_state,job,'cleanup_uncertain',broker=broker_id)
        return receipts,ok
    async def _retire_batch(self,batch,reason):
        jobs=await offload(self.journal.jobs,batch.id)
        uncertain=any(j['state']!='confirmed'for j in jobs)
        state='cleanup_uncertain'if uncertain else reason
        if batch.test_lease:
            try:await offload(self.manager.fail_test,batch.test_lease,owner=batch.owner,collection=batch.collection,reason=state)
            except ExecutionError:pass # A final committed passed receipt remains historical.
        await offload(self.journal.state,batch.id,state)
    async def _maintenance(self,operation):
        task=asyncio.create_task(operation);self.maintenance.add(task)
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:await drain(task)
            except Exception:pass
            raise
        finally:self.maintenance.discard(task)
    async def stop(self,key,*,owner,collection):
        return await self._maintenance(self._stop(key,owner=owner,collection=collection))
    async def _stop(self,key,*,owner,collection):
        await offload(self.manager.status,key,owner=owner,collection=collection)
        await offload(self.journal.request_stop,key,owner,collection)
        for batch in tuple(self.active.values()):
            if(batch.key,batch.owner,batch.collection)==(key,owner,collection):batch.cancelled.set();batch.task.cancel()
        for batch in tuple(self.active.values()):
            if(batch.key,batch.owner,batch.collection)==(key,owner,collection):
                try:await drain(batch.task)
                except Exception:pass
        # Other web/coordinator processes retain their flock through cleanup;
        # the durable intent above closes their callback/watch authority too.
        async with self._lock(key,owner,collection,wait=True):
            for row in await offload(self.journal.pending,key,owner,collection):await self._reconcile_row(row)
        return await self.status(key,owner=owner,collection=collection)
    async def revoke(self,key,*,owner,collection):
        return await self._maintenance(self._revoke(key,owner=owner,collection=collection))
    async def _revoke(self,key,*,owner,collection):
        value=await offload(self.manager.revoke,key,owner=owner,collection=collection)
        status=await self._stop(key,owner=owner,collection=collection)
        return {**value,**status}
    async def reconcile(self,*,key=None,owner=None,collection=None):
        return await self._maintenance(self._reconcile(key=key,owner=owner,collection=collection))
    async def _reconcile(self,*,key=None,owner=None,collection=None):
        require((key is None and owner is None and collection is None)or all(isinstance(v,str)and v for v in(key,owner,collection)))
        if key is not None:await offload(self.manager.status,key,owner=owner,collection=collection)
        require(not self.reconciling,'custom_execution_busy');self.reconciling=True;summary=[]
        try:
            for row in await offload(self.journal.pending,key,owner,collection):
                if row['id']in self.active:continue
                try:
                    async with self._lock(row['connector'],row['owner'],row['collection']):
                        summary.append(await self._reconcile_row(row))
                except CoordinatorError:continue # Another live process owns its lock.
            return summary
        finally:self.reconciling=False
    async def _reconcile_row(self,row):
        scope=NativeScope(self.manager.company,row['owner'],row['id']);ok=True;last_receipt=None
        for job in await offload(self.journal.jobs,row['id']):
            if job['state']=='confirmed':last_receipt=json.loads(job['receipt']);continue
            try:
                lease=await self.client.recover(scope,job['id'],retire=True)
                if lease:
                    require(lease.scope_key==scope.key,'custom_execution_broker_scope')
                    stopped=await self.client.stop(scope,lease)
                    require(stopped.get('cleanup_confirmed')is True and stopped.get('status')in TERMINAL)
                    outcome=await self.client.release(scope,lease)
                    require(outcome.get('status')=='released'and outcome.get('cleanup_confirmed')is True)
                else:outcome={'status':'absent','cleanup_confirmed':True}
                receipt={'snapshot_sha256':row['snapshot'],'cleanup_confirmed':True,'broker_run_id':'custom-'+lease.id if lease else'custom-request-'+job['id'],
                    'receipt_sha256':digest({'request_id':job['id'],'scope':asdict(scope),'outcome':outcome})}
                known=await offload(self.manager.job_cleanup_state,job['id'],owner=row['owner'],collection=row['collection'])
                require(known is not None or lease is None,'custom_execution_admission_missing')
                if known is not None:await offload(self.manager.confirm_job_cleanup,job['id'],receipt,owner=row['owner'],collection=row['collection'])
                await offload(self.journal.job_state,job['id'],'confirmed',receipt=receipt,broker=receipt['broker_run_id']);last_receipt=receipt
            except Exception:
                ok=False
                await offload(self.journal.job_state,job['id'],'cleanup_uncertain')
        if row['test_lease']:
            lease=TestLease(**json.loads(row['test_lease']))
            if ok:
                saved=await offload(self.manager.test_result,lease.id,owner=row['owner'],collection=row['collection'])
                if saved['status']=='cleanup_uncertain':
                    require(last_receipt is not None)
                    await offload(self.manager.confirm_test_cleanup,lease,last_receipt,owner=row['owner'],collection=row['collection'])
                elif saved['status']not in {'passed','stopped','failed','runtime_restarted'}:
                    await offload(self.manager.fail_test,lease,owner=row['owner'],collection=row['collection'],reason='runtime_restarted')
            else:
                try:await offload(self.manager.fail_test,lease,owner=row['owner'],collection=row['collection'],reason='cleanup_uncertain')
                except ExecutionError:pass
        await offload(self.journal.state,row['id'],'runtime_restarted'if ok else'cleanup_uncertain')
        return {'batch_id':row['id'],'cleanup_confirmed':ok}
    async def close(self):
        self.closed=True
        for batch in tuple(self.active.values()):batch.cancelled.set();batch.task.cancel()
        for batch in tuple(self.active.values()):
            try:await drain(batch.task)
            except Exception:pass
        for task in tuple(self.maintenance):
            try:await drain(task)
            except Exception:pass
        try:await drain(asyncio.create_task(self.gateway.close()))
        finally:await offload(self.journal.close)
