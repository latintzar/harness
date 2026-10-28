"""One-use admitted source embedding. Input media is opaque to this host.

Private durable attempt receipts are independent of live-query budgets. Modal
runs in the commissioned isolated interpreter, using its company-owned account.
"""
from __future__ import annotations
from retail_settings import value as setting
import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time

import compute_guard
import modal_embed

MODEL = modal_embed.MODEL
REVISION = modal_embed.MODEL_REVISION
WORKSPACE = modal_embed.WORKSPACE
SDK_VERSION = '1.6.1'
PURPOSE = 'rocoto-video-embedding-pilot'
MAX_BYTES = 64 * 1024**2
RESULT_BYTES = 4 * 1024**2
PROFILES = {
    'small': {'max_clips':48,'gross_usd':'1.00','function_seconds':300,'startup_seconds':180},
    'standard': {'max_clips':160,'gross_usd':'1.50','function_seconds':600,'startup_seconds':300},
}


class EmbeddingError(RuntimeError):
    pass


def require(ok, code):
    if not ok: raise EmbeddingError(code)


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def private_directory(path):
    path=Path(path)
    require(path.absolute()==path.resolve(),'invalid_private_directory')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid()
            and not info.st_mode & 0o077,'invalid_private_directory')
    return path


def fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def atomic_json(path,value):
    data=encode(value);require(len(data)<=RESULT_BYTES,'receipt_limit')
    temporary=path.with_name(path.name+'.'+os.urandom(8).hex()+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path);fsync_directory(path.parent)
    finally: temporary.unlink(missing_ok=True)


def read_private(path,maximum=RESULT_BYTES):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
                and not info.st_mode & 0o077 and 0<info.st_size<=maximum,'invalid_private_file')
        data=source.read(maximum+1)
        require(len(data)==info.st_size,'private_file_changed')
        return data


def read_json(path):
    value=json.loads(read_private(path));encode(value);return value


@contextlib.contextmanager
def locked(path):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise EmbeddingError('embedding_attempt_busy') from None
        yield
    finally: os.close(fd)


def utc():
    return datetime.now(timezone.utc).isoformat()


def runtime(python=None,config=None):
    python=python or setting('ROCOTO_RETAIL_MODAL_PYTHON')
    config=config or setting('ROCOTO_RETAIL_MODAL_CONFIG_PATH')
    require(bool(python) and bool(config),'embedding_runtime_unconfigured')
    interpreter=Path(python).expanduser().absolute()
    require(interpreter.is_file() and os.access(interpreter,os.X_OK),'embedding_runtime_unconfigured')
    account=Path(config).expanduser().absolute()
    require(account==account.resolve(),'invalid_modal_account')
    read_private(account,64*1024)
    # An allowlist prevents carrying application/other-company credentials into
    # the Modal interpreter. These module paths are host-selected source code.
    environment={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8',
        'MODAL_CONFIG_PATH':str(account),'PYTHONPATH':os.pathsep.join(
            dict.fromkeys([str(Path(__file__).resolve().parent),str(Path(modal_embed.__file__).resolve().parent)]))}
    return interpreter,environment


def implementation_identity():
    return {'modal_sdk':SDK_VERSION, 'source_embedding_sha256':checksum(Path(__file__).read_bytes()),
        'modal_embed_sha256':checksum(Path(modal_embed.__file__).read_bytes()),
        'compute_guard_sha256':checksum(Path(compute_guard.__file__).read_bytes())}


def batch_identity(context,batch,company,idempotency_key,profile):
    asset=context['asset'];source=context['source']
    prepared=context.get('completed_stages',{}).get('prepare',{})
    require(isinstance(prepared,dict) and prepared.get('profile')=='source-media-prepare-v2'
        and prepared.get('recipe')=='rocoto-cfr24-h264-segments-v2'
        and re.fullmatch(r'[a-f0-9]{64}',prepared.get('manifest_sha256',''))
        and re.fullmatch(r'sha256:[a-f0-9]{64}',prepared.get('image_digest',''))
        and re.fullmatch(r'[a-f0-9]{64}',asset.get('sha256','')), 'verified_preparation_required')
    require(isinstance(company,str) and 0<len(company)<=512 and isinstance(asset.get('owner'),str)
            and re.fullmatch(r'asset-[a-f0-9]{32}',asset.get('id','')),'invalid_embedding_scope')
    match=re.fullmatch(r'(job-[a-f0-9]{32}):([1-9][0-9]{0,17}):([0-9]{1,4})',idempotency_key)
    require(match is not None,'invalid_embedding_generation')
    require(isinstance(batch,list) and 0<len(batch)<=PROFILES[profile]['max_clips'],'embedding_batch_limit')
    rows=[];total=0;seen=set()
    for clip in batch:
        identity=clip.get('id')
        require(isinstance(identity,str) and re.fullmatch(re.escape(asset['id'])+r'-clip-[0-9]{6}',identity)
            and identity not in seen and re.fullmatch(r'[a-f0-9]{64}',clip.get('sha256',''))
            and type(clip.get('size_bytes')) is int and 0<clip['size_bytes']<=8*1024**2
            and type(clip.get('duration_seconds')) in (int,float) and math.isfinite(clip['duration_seconds'])
            and 0<clip['duration_seconds']<=8.25,'invalid_embedding_clip')
        seen.add(identity);total+=clip['size_bytes']
        rows.append({k:clip[k] for k in ('id','sha256','size_bytes','duration_seconds')})
    require(total<=MAX_BYTES,'embedding_batch_limit')
    scope={'company_id':company,'owner_id':asset['owner'],'asset_id':asset['id'],
           'job_id':match[1],'generation':int(match[2]),'batch':int(match[3]),'collection':source['collection']}
    binding={'scope':scope,'clips':rows,'profile':profile,'model':MODEL,'model_revision':REVISION,'frames_per_clip':8,
        'implementation':implementation_identity(),'original_sha256':asset['sha256'],'preparation':{key:prepared[key] for key in
            ('manifest_sha256','image_digest','profile','recipe')}}
    attempt_id='embedding-'+checksum(encode(scope))
    return attempt_id,binding,checksum(encode(binding))


def public(record):
    return {key:record[key] for key in ('attempt_id','reservation_id','binding_sha256','profile','planned_gross_usd')}


async def drain(task):
    while not task.done():
        try: await asyncio.shield(task)
        except asyncio.CancelledError: continue
        except Exception: break
    return task.result()


async def owned_thread(function,*args):
    task=asyncio.create_task(asyncio.to_thread(function,*args))
    try: return await asyncio.shield(task)
    except BaseException:
        try: await drain(task)
        except BaseException: pass
        raise


class SourceEmbedding:
    def __init__(self,state,*,company_id,approval_path=None,modal_python=None,modal_config=None,profile='small',runner=None):
        require(profile in PROFILES,'invalid_embedding_profile')
        self.root=private_directory(Path(state)/'source-embedding')
        self.company,self.approval,self.python,self.config,self.profile=company_id,approval_path,modal_python,modal_config,profile
        self.runner=runner or SubprocessRunner(modal_python,modal_config)

    async def admit(self,context,batch,*,idempotency_key):
        return await owned_thread(self._admit,context,batch,idempotency_key)

    def _admit(self,context,batch,idempotency_key):
        attempt,binding,fingerprint=batch_identity(context,batch,self.company,idempotency_key,self.profile)
        directory=self.root/attempt
        with locked(self.root/'admission.lock'):
            if directory.exists():
                record=read_json(directory/'attempt.json')
                require(record['binding_sha256']==fingerprint,'embedding_binding_changed')
                require(record['status'] in {'admitted','launching','completed','needs_reconciliation'},'embedding_admission_incomplete')
                return public(record)
            require(self.approval is not None,'embedding_admission_required')
            if isinstance(self.runner,SubprocessRunner): runtime(self.python,self.config)
            approval=compute_guard.validate_approval(self.approval,workspace=WORKSPACE,
                planned_gross_usd=PROFILES[self.profile]['gross_usd'],purpose=PURPOSE)
            directory=private_directory(directory);fsync_directory(self.root)
            record={'schema_version':1,'attempt_id':attempt,'reservation_id':'reservation-'+attempt,
                'binding':binding,'binding_sha256':fingerprint,'profile':self.profile,
                'planned_gross_usd':PROFILES[self.profile]['gross_usd'],'created_at':utc(),
                'approval_path':str(approval.path),'approval_sha256':approval.digest,'approval_id':approval.approval_id,
                'status':'claiming','budget_status':'held_pending_provider_evidence','actual_cost_usd':None}
            atomic_json(directory/'attempt.json',record)
            try:
                receipt=compute_guard.claim_approval(approval)
                fsync_directory(receipt.parent)
            except BaseException:
                record['status']='admission_failed';atomic_json(directory/'attempt.json',record);raise
            record.update(status='admitted',claimed_receipt=str(receipt),claimed_at=utc())
            atomic_json(directory/'attempt.json',record)
            return public(record)

    def _directory(self,admission):
        identity=admission.get('attempt_id') if isinstance(admission,dict) else None
        require(isinstance(identity,str) and re.fullmatch(r'embedding-[a-f0-9]{64}',identity),'invalid_embedding_attempt')
        directory=self.root/identity
        record=read_json(directory/'attempt.json')
        require(public(record)==admission and record['binding']['scope']['company_id']==self.company,'embedding_scope_mismatch')
        return directory,record

    def _stage(self,directory,record,inputs):
        require(isinstance(inputs,list) and len(inputs)==len(record['binding']['clips']),'embedding_input_mismatch')
        for index,(expected,actual) in enumerate(zip(record['binding']['clips'],inputs)):
            require(all(actual.get(k)==expected[k] for k in ('id','sha256','duration_seconds'))
                and isinstance(actual.get('content'),bytes) and len(actual['content'])==expected['size_bytes']
                and checksum(actual['content'])==expected['sha256'],'embedding_input_mismatch')
            path=directory/f'{index:04d}.mp4'
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as output:
                output.write(actual['content']);output.flush();os.fsync(output.fileno())
        fsync_directory(directory)

    async def run(self,admission,inputs,*,cancelled):
        directory,record=self._directory(admission)
        with locked(directory/'run.lock'):
            # Re-read under the attempt lock; a second caller never launches again.
            directory,record=self._directory(admission)
            if record['status']=='completed':
                return validate_result(read_json(directory/'result.json'),record)
            require(record['status']=='admitted','embedding_attempt_needs_reconciliation')
            if cancelled.is_set(): raise EmbeddingError('embedding_cancelled_before_launch')
            try:
                await owned_thread(self._stage,directory,record,inputs)
                if cancelled.is_set(): raise EmbeddingError('embedding_cancelled_before_launch')
                record.update(status='launching',launch_intent_at=utc())
                atomic_json(directory/'attempt.json',record)
                result=await self.runner.run(directory,record,cancelled=cancelled)
                if cancelled.is_set(): raise EmbeddingError('embedding_cancelled')
                result=validate_result(result,record)
                atomic_json(directory/'result.json',result)
                record.update(status='completed',completed_at=utc())
                atomic_json(directory/'attempt.json',record)
                return result
            except BaseException:
                # Both interrupted allocation and completed-but-unreceived calls
                # retain the full hold. An operator reconciles provider evidence.
                record.update(status='needs_reconciliation',failure_at=utc())
                atomic_json(directory/'attempt.json',record)
                raise
            finally:
                for path in directory.glob('[0-9][0-9][0-9][0-9].mp4'): path.unlink(missing_ok=True)
                fsync_directory(directory)

    async def stop(self,admission):
        """Host-only crash reconciliation; leaves the full budget hold intact."""
        directory,record=self._directory(admission)
        require(hasattr(self.runner,'stop_attempt'),'embedding_stop_unconfigured')
        return await self.runner.stop_attempt(directory,record)

    def receipt(self,admission):
        """Host-only audit; approval paths/identities are never an HTTP response."""
        directory,record=self._directory(admission)
        if (directory/'provider.json').exists(): record['provider']=read_json(directory/'provider.json')
        return record


def validate_result(result,record):
    require(isinstance(result,dict) and len(encode(result))<=RESULT_BYTES and result.get('model')==MODEL
        and result.get('model_revision')==REVISION and result.get('dimensions')==256
        and result.get('frames_per_clip')==8 and result.get('attempt_id')==record['attempt_id'],
        'embedding_result_provenance')
    rows=result.get('clips')
    require(isinstance(rows,list) and len(rows)==len(record['binding']['clips']),'embedding_result_coverage')
    for expected,actual in zip(record['binding']['clips'],rows):
        require(actual.get('id')==expected['id'] and actual.get('sha256')==expected['sha256'],'embedding_result_provenance')
        vector=actual.get('embedding')
        require(isinstance(vector,list) and len(vector)==256 and all(type(v) in (int,float)
            and math.isfinite(v) for v in vector) and 1e-16<sum(v*v for v in vector)<float('inf'),'invalid_embedding_vector')
    return result


class SubprocessRunner:
    def __init__(self,python=None,config=None):
        self.python,self.config=python,config

    async def _stop(self,directory,interpreter,environment):
        process=await asyncio.create_subprocess_exec(str(interpreter),str(Path(__file__).resolve()),
            '--stop-attempt',str(directory),env=environment,stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
        try:
            await asyncio.wait_for(process.wait(),35)
        except (TimeoutError,asyncio.CancelledError):
            if process.returncode is None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            await asyncio.shield(process.wait())

    async def stop_attempt(self,directory,record):
        interpreter,environment=runtime(self.python,self.config)
        await drain(asyncio.create_task(self._stop(directory,interpreter,environment)))
        provider=read_json(directory/'provider.json')
        require(provider.get('attempt_id')==record['attempt_id'] and provider.get('binding_sha256')==record['binding_sha256']
                and provider.get('stop_confirmed') is True,'provider_stop_unconfirmed')
        return {'attempt_id':record['attempt_id'],'app_id':provider['app_id'],'stop_confirmed':True,
                'budget_status':'held_pending_provider_evidence'}

    async def run(self,directory,record,*,cancelled):
        interpreter,environment=runtime(self.python,self.config)
        process=None;completed=None;stop_wait=None
        try:
            launching=asyncio.create_task(asyncio.create_subprocess_exec(str(interpreter),str(Path(__file__).resolve()),
                '--run-attempt',str(directory),env=environment,stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,start_new_session=True))
            try:
                process=await asyncio.shield(launching)
            except BaseException:
                process=await drain(launching)
                raise
            completed=asyncio.create_task(process.wait());stop_wait=asyncio.create_task(cancelled.wait())
            profile=PROFILES[record['profile']]
            timeout=profile['function_seconds']+profile['startup_seconds']+90
            done,_=await asyncio.wait({completed,stop_wait},timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            require(completed in done and process.returncode==0 and not cancelled.is_set(),'embedding_attempt_interrupted')
            value=read_json(directory/'worker-result.json')
        finally:
            async def cleanup():
                if stop_wait: stop_wait.cancel();await asyncio.gather(stop_wait,return_exceptions=True)
                if process and process.returncode is None:
                    try: os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: await asyncio.wait_for(asyncio.shield(process.wait()),5)
                    except TimeoutError:
                        if process.returncode is None:
                            try: os.killpg(process.pid,signal.SIGKILL)
                            except ProcessLookupError: pass
                        await process.wait()
                if completed: await asyncio.gather(completed,return_exceptions=True)
                if process: await self._stop(directory,interpreter,environment)
            await drain(asyncio.create_task(cleanup()))
        provider=read_json(directory/'provider.json')
        require(provider.get('attempt_id')==record['attempt_id'] and provider.get('binding_sha256')==record['binding_sha256']
                and provider.get('stop_confirmed') is True,'provider_stop_unconfirmed')
        return value


def verify_workspace(modal):
    workspace=modal.Workspace.from_context();workspace.hydrate()
    require(workspace.name==WORKSPACE and compute_guard.OWNER in
            {member.email for member in workspace.members.list() if member.role=='owner'},'modal_workspace_mismatch')


def app_name(record):
    return 'rocoto-source-'+record['attempt_id']


def provider_update(directory,record,**values):
    path=directory/'provider.json'
    with locked(directory/'provider.lock'):
        current=read_json(path) if path.exists() else {
            'schema_version':1,'attempt_id':record['attempt_id'],'binding_sha256':record['binding_sha256'],
            'workspace':WORKSPACE,'app_name':app_name(record),'environment':'main','stop_confirmed':False}
        require(current['attempt_id']==record['attempt_id'] and current['binding_sha256']==record['binding_sha256'],
                'provider_receipt_mismatch')
        for field in ('app_id','function_call_id'):
            if current.get(field) and values.get(field): require(current[field]==values[field],'provider_identity_changed')
        current.update(values,updated_at=utc());atomic_json(path,current)
        return current


def stop_own_app(modal,directory,record,*,timeout_seconds=20):
    """Verify this exact attempt's app and wait for provider-confirmed absence."""
    verify_workspace(modal)
    receipt=read_json(directory/'provider.json') if (directory/'provider.json').exists() else {}
    require(receipt.get('attempt_id')==record['attempt_id'] and receipt.get('binding_sha256')==record['binding_sha256'],
            'provider_receipt_mismatch')
    app_id=receipt.get('app_id')
    require(isinstance(app_id,str) and re.fullmatch(r'ap-[A-Za-z0-9]+',app_id),'provider_identity_unavailable')
    from modal.client import _Client
    from modal._utils.async_utils import synchronizer
    from modal_proto import api_pb2
    async def stop():
        client=await _Client.from_env()
        async def verified_info():
            info=(await client._stub.AppGetInfo(api_pb2.AppGetInfoRequest(app_id=app_id))).info
            require(info.description==app_name(record) and info.environment_name=='main','provider_app_scope_mismatch')
            return info
        info=await verified_info()
        # The receipt is private and written immediately from spawn's returned
        # handle. App identity is verified before cancelling that call.
        call_id=receipt.get('function_call_id')
        if call_id:
            require(isinstance(call_id,str) and re.fullmatch(r'fc-[A-Za-z0-9]+',call_id),'invalid_provider_call')
            call=modal.FunctionCall.from_id(call_id)
            # A completed call can reject cancellation; whole-app Stop and
            # absence of its tasks are the authoritative cleanup proof.
            try: await call.cancel.aio(terminate_containers=True)
            except Exception: pass
        if info.lifecycle.app_state!=api_pb2.APP_STATE_STOPPED:
            await client._stub.AppStop(api_pb2.AppStopRequest(app_id=app_id))
        deadline=time.monotonic()+timeout_seconds
        while True:
            info=await verified_info()
            tasks=await client._stub.TaskList(api_pb2.TaskListRequest(environment_name='main',app_id=app_id))
            if info.lifecycle.app_state==api_pb2.APP_STATE_STOPPED and not tasks.tasks: return
            require(time.monotonic()<deadline,'provider_stop_unconfirmed')
            await asyncio.sleep(.25)
    synchronizer.create_blocking(stop)()
    provider_update(directory,record,stop_confirmed=True,stopped_at=utc())


def child_record(directory):
    directory=private_directory(Path(directory).absolute())
    record=read_json(directory/'attempt.json')
    require(record.get('schema_version')==1 and record.get('profile') in PROFILES
        and re.fullmatch(r'embedding-[a-f0-9]{64}',record.get('attempt_id',''))
        and directory.name==record['attempt_id'] and record['binding_sha256']==checksum(encode(record['binding'])),
        'invalid_embedding_attempt')
    return directory,record


def child_run(directory,*,modal_module=None,worker_factory=None,stop_function=None):
    directory,record=child_record(directory)
    require(record['status']=='launching','embedding_attempt_not_launched')
    profile=PROFILES[record['profile']]
    require(record['binding'].get('implementation')==implementation_identity(),'embedding_implementation_changed')
    approval=compute_guard.validate_approval(record['approval_path'],workspace=WORKSPACE,
        planned_gross_usd=profile['gross_usd'],purpose=PURPOSE)
    require(approval.digest==record['approval_sha256'],'embedding_approval_changed')
    claimed=read_json(Path(record['claimed_receipt']))
    require(claimed.get('approval_id')==approval.approval_id and claimed.get('approval_sha256')==approval.digest,
            'embedding_approval_unclaimed')
    clips=[]
    for index,expected in enumerate(record['binding']['clips']):
        data=read_private(directory/f'{index:04d}.mp4',8*1024**2)
        require(len(data)==expected['size_bytes'] and checksum(data)==expected['sha256'],'embedding_input_mismatch')
        clips.append({'path':expected['id'],'sha256':expected['sha256'],
                      'duration_seconds':expected['duration_seconds'],'content':data})
    if modal_module is None:
        import modal as modal_module
    require(getattr(modal_module,'__version__',None)==SDK_VERSION,'modal_sdk_revision_changed')
    verify_workspace(modal_module)
    factory=worker_factory or modal_embed._create_worker
    stop_function=stop_function or stop_own_app
    app,worker=factory(modal_module,small_batch=record['profile']=='small')
    provider_update(directory,record,phase='app_launch_intent',launch_started_at=utc())
    watching=threading.Event();watch_errors=[]
    def watch_app():
        while not watching.wait(.01):
            try:
                if app.app_id:
                    provider_update(directory,record,app_id=app.app_id,phase='app_created')
                    return
            except BaseException as error:
                watch_errors.append(type(error).__name__);return
    watcher=threading.Thread(target=watch_app,name='embedding-app-receipt',daemon=True)
    watcher.start();call=None
    try:
        # app.run may create/build before its context yields. The watcher records
        # app_id as soon as the SDK exposes it; persisted launch intent retains a
        # full hold through the remaining create-to-receipt ambiguity.
        with app.run(name=app_name(record),environment_name='main'):
            watching.set();watcher.join()
            require(not watch_errors,'provider_receipt_unavailable')
            provider_update(directory,record,app_id=app.app_id,phase='spawn_intent')
            call=worker.spawn(clips,[],False)
            provider_update(directory,record,function_call_id=call.object_id,phase='waiting')
            raw=call.get(timeout=profile['function_seconds']+profile['startup_seconds']+30)
        result={'model':raw['model'],'model_revision':raw['model_revision'],'dimensions':raw['dimensions'],
            'frames_per_clip':raw['frames_per_clip'],'attempt_id':record['attempt_id'],
            'clips':[{'id':row['path'],'sha256':row['sha256'],'embedding':row['embedding']} for row in raw['clips']],
            'timing':raw.get('timing',{})}
        validate_result(result,record)
        atomic_json(directory/'worker-result.json',result)
        provider_update(directory,record,phase='result_received',result_sha256=checksum(encode(result)))
    finally:
        watching.set();watcher.join(timeout=2)
        try:
            if getattr(app,'app_id',None): provider_update(directory,record,app_id=app.app_id)
            stop_function(modal_module,directory,record)
        except BaseException:
            # The durable reservation remains held; a parent cleanup process can
            # retry only this app's Stop, never start another embedding attempt.
            provider_update(directory,record,stop_confirmed=False,stop_reason='provider_stop_unconfirmed')


def child_stop(directory):
    directory,record=child_record(directory)
    import modal
    require(modal.__version__==SDK_VERSION,'modal_sdk_revision_changed')
    stop_own_app(modal,directory,record)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run-attempt');mode.add_argument('--stop-attempt')
    args=parser.parse_args()
    os.umask(0o077)
    def interrupted(signum,frame): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    try:
        if args.run_attempt: child_run(args.run_attempt)
        else: child_stop(args.stop_attempt)
    except BaseException:
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
