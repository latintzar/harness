"""Host-owned intake coordinator. Providers and activation are explicitly injected.

Imported bytes are opaque here: decoding stays in the fixed media sandbox. The
HTTP process never calls this module. Durable uncertain attempts require actual
worker/provider reconciliation before SourceStore permits another attempt.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

import numpy as np

from cosmos_retrieval import MODEL_ID, MODEL_REVISION, DIMENSIONS, merge_index, load_generation
from qdrant_retrieval import LocalQdrant, QdrantRetrievalIndex
from source_intake import IntakeError, SourceStore

PROFILE = 'source-media-prepare-v2'
RECIPE = 'rocoto-cfr24-h264-segments-v2'
WINDOW_SECONDS = 8
SHA = re.compile(r'[a-f0-9]{64}\Z')


class WorkerError(ValueError):
    """Stable failure code. Provider stderr, secrets and paths stay private."""


@dataclass(frozen=True)
class WorkerLimits:
    lease_seconds: int = 60
    heartbeat_seconds: float = 15
    poll_seconds: float = .5
    stage_seconds: float = 900
    prepared_bytes: int = 512 * 1024**2
    artifact_storage_bytes: int = 2 * 1024**3
    manifest_bytes: int = 512 * 1024
    embedding_bytes: int = 4 * 1024 * 1024
    artifact_entries: int = 10000
    publication_seconds: float = 120
    embedding_batch_clips: int = 160
    embedding_import_clips: int = 10000
    embedding_import_bytes: int = 512 * 1024**2

    def __post_init__(self):
        if (any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v<=0
                for v in asdict(self).values()) or not 1<=self.lease_seconds<=90
                or any(type(getattr(self,k)) is not int for k in ('lease_seconds','prepared_bytes',
                    'artifact_storage_bytes','manifest_bytes','embedding_bytes','artifact_entries','embedding_batch_clips','embedding_import_clips','embedding_import_bytes'))
                or self.heartbeat_seconds>=self.lease_seconds/2
                or self.publication_seconds>=900 or self.prepared_bytes>512*1024**2
                or self.embedding_batch_clips>160 or self.embedding_import_clips>10000
                or self.embedding_import_bytes>512*1024**2):
            raise WorkerError('invalid_worker_limits')


async def _thread(function,*args,**kwargs):
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try: return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if task.done() and not task.cancelled(): task.exception()
        raise


async def _settle(awaitable):
    """Finish finite cleanup despite another cancellation of its owner."""
    task=asyncio.ensure_future(awaitable)
    while True:
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled(): raise


@asynccontextmanager
async def _original(store,lease):
    manager=store.open_job_original(lease)
    opening=asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try: stream=await asyncio.shield(opening)
    except asyncio.CancelledError:
        try:
            await _settle(opening)
        except Exception: pass
        else: manager.__exit__(None,None,None)
        raise
    try: yield stream
    finally: manager.__exit__(None,None,None)


def _hash(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def _finite(value,*,low=0,high=1e12):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise WorkerError('invalid_prepared_manifest')
    return float(value)


def _regular(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode&0o077:
        raise WorkerError('invalid_artifact')
    return info


def _directory(path):
    if path.is_symlink(): raise WorkerError('invalid_artifact')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise WorkerError('invalid_artifact')


def _fsync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def validate_manifest(value,context,*,recipe=RECIPE):
    """Verify source binding, bounded numeric claims and the exact file catalogue."""
    asset,source=context['asset'],context['source']
    if (not isinstance(value,dict) or value.get('schema_version')!=1 or value.get('recipe')!=recipe
            or value.get('asset_id')!=asset['id'] or value.get('collection')!=source['collection']
            or value.get('original')!={'sha256':asset['sha256'],'size_bytes':asset['bytes']}):
        raise WorkerError('prepared_provenance_mismatch')
    metadata=value.get('source',{});normal=value.get('normalization',{})
    if (not isinstance(metadata,dict) or not isinstance(normal,dict)
            or normal.get('fps')!=24 or normal.get('window_seconds')!=WINDOW_SECONDS
            or metadata.get('capture_utc') is not None or metadata.get('capture_clock')!='unknown'):
        raise WorkerError('invalid_preparation_profile')
    duration=_finite(metadata.get('duration_seconds'),low=.001,high=1800)
    origin=_finite(metadata.get('origin_seconds'),low=-1e12)
    clips=value.get('clips')
    if not isinstance(clips,list) or not 1<=len(clips)<=180:
        raise WorkerError('invalid_prepared_manifest')
    expected={'manifest.json':None}
    timing=value.get('frame_timing',{})
    if not isinstance(timing,dict) or timing.get('file')!='frame-timing.json' or not SHA.fullmatch(str(timing.get('sha256',''))):
        raise WorkerError('invalid_prepared_manifest')
    expected['frame-timing.json']=timing['sha256']
    previous=0
    for number,clip in enumerate(clips):
        stem=f'clip-{number:06d}'
        if (not isinstance(clip,dict) or clip.get('id')!=f"{asset['id']}-{stem}"
                or clip.get('asset_id')!=asset['id'] or clip.get('collection')!=source['collection']
                or clip.get('file')!=stem+'.mp4' or not SHA.fullmatch(str(clip.get('sha256','')))):
            raise WorkerError('invalid_prepared_manifest')
        length=_finite(clip.get('duration_seconds'),low=.001,high=8.25)
        start=_finite(clip.get('source_elapsed_start_seconds'),high=duration)
        end=_finite(clip.get('source_elapsed_end_seconds'),high=duration)
        if (abs(start-number*WINDOW_SECONDS)>.001 or abs(start-previous)>.05 or not start<end
                or abs((end-start)-length)>.05
                or abs(_finite(clip.get('original_pts_start_seconds'),low=-1e12)-(origin+start))>.001
                or abs(_finite(clip.get('original_pts_end_seconds'),low=-1e12)-(origin+end))>.001):
            raise WorkerError('invalid_prepared_timing')
        previous=end
        for key in ('width','height'):
            if type(clip.get(key)) is not int or not 2<=clip[key]<=640: raise WorkerError('invalid_prepared_manifest')
        if type(clip.get('size_bytes')) is not int or not 0<clip['size_bytes']<=8*1024**2:
            raise WorkerError('invalid_prepared_manifest')
        thumb=clip.get('thumbnail',{})
        if (not isinstance(thumb,dict) or thumb.get('file')!=stem+'.jpg'
                or not SHA.fullmatch(str(thumb.get('sha256','')))
                or type(thumb.get('size_bytes')) is not int or not 0<thumb['size_bytes']<=8*1024**2):
            raise WorkerError('invalid_prepared_manifest')
        expected[clip['file']]=clip['sha256'];expected[thumb['file']]=thumb['sha256']
    if abs(previous-duration)>.05: raise WorkerError('incomplete_prepared_coverage')
    return expected


class Artifacts:
    """Private immutable snapshots, with a separate bounded staging reservation."""
    def __init__(self,store,limits):
        self.root=store.root/'worker-artifacts';self.limits=limits
        _directory(self.root)

    def acquire(self,size,entries=1):
        fd=os.open(self.root/'allocation.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        try:
            _regular(self.root/'allocation.lock')
            used=0;count=0
            for path in self.root.rglob('*'):
                if path.is_symlink(): raise WorkerError('invalid_artifact')
                count+=1
                if path.is_file():
                    info=_regular(path)
                    used+=max(info.st_size,info.st_blocks*512)+4096
                else: used+=4096
                if count+entries+2>self.limits.artifact_entries: raise WorkerError('artifact_entry_quota')
            # Conservative metadata/allocation allowance in addition to content.
            if used+size+(entries+2)*8192>self.limits.artifact_storage_bytes:
                raise WorkerError('artifact_storage_quota')
            return fd
        except BaseException:
            os.close(fd);raise

    def stage(self,asset_id,label):
        if not re.fullmatch(r'asset-[a-f0-9]{32}',asset_id) or label not in {'prepare','embed','derive'}:
            raise WorkerError('invalid_artifact_scope')
        parent=self.root/asset_id;_directory(parent)
        identity=label+'-'+secrets.token_hex(16)
        path=parent/identity;path.mkdir(mode=0o700)
        return identity,path

    @asynccontextmanager
    async def allocation(self,size,entries=1):
        opening=asyncio.create_task(asyncio.to_thread(self.acquire,size,entries))
        try: fd=await asyncio.shield(opening)
        except asyncio.CancelledError:
            try: fd=await _settle(opening)
            except Exception: pass
            else: os.close(fd)
            raise
        try: yield
        finally: os.close(fd)

    def load(self,asset_id,receipt):
        identity=receipt.get('artifact_id','')
        if not re.fullmatch(r'(prepare|embed|derive)-[a-f0-9]{32}',identity): raise WorkerError('invalid_artifact_scope')
        path=self.root/asset_id/identity
        if path.is_symlink() or not path.is_dir() or path.resolve()!=path.absolute(): raise WorkerError('invalid_artifact_scope')
        marker=path/'manifest.json';info=_regular(marker)
        maximum=self.limits.embedding_bytes if identity.startswith('embed-') else self.limits.manifest_bytes
        if info.st_size>maximum or _hash(marker)!=receipt.get('manifest_sha256'):
            raise WorkerError('artifact_integrity_failed')
        return path,json.loads(marker.read_bytes())

    @staticmethod
    def seal(identity,path):
        for file in path.iterdir():
            _regular(file);file.chmod(0o400)
            with file.open('rb') as handle: os.fsync(handle.fileno())
        _fsync_dir(path);path.chmod(0o500);_fsync_dir(path.parent)
        return {'artifact_id':identity,'manifest_sha256':_hash(path/'manifest.json')}


class _DeadlineQdrant:
    def __init__(self,client,deadline): self.client,self.deadline=client,deadline
    def request(self,*args,**kwargs):
        if time.monotonic()>=self.deadline: raise WorkerError('publication_timeout')
        result=self.client.request(*args,**kwargs)
        if time.monotonic()>=self.deadline: raise WorkerError('publication_timeout')
        return result


class IndexPublisher:
    """Lock order is SourceStore -> Cosmos generation -> Qdrant publication."""
    def __init__(self,state,*,resolver_ready,client=None,seconds=120):
        self.state=Path(state);self.resolver_ready=resolver_ready
        self.client=client or LocalQdrant(timeout=5);self.seconds=seconds

    def publish(self,records,vectors,metadata):
        if self.resolver_ready() is not True: raise WorkerError('media_resolver_unavailable')
        proof={};deadline=time.monotonic()+self.seconds
        def before_commit(snapshot,promote):
            actual={row['id']:row for row in snapshot['records']}
            # merge_index adds immutable source provenance to each row.
            for row in records:
                if row['id'] not in actual or any(actual[row['id']].get(key)!=value for key,value in row.items()):
                    raise WorkerError('published_source_mismatch')
            proof.update(QdrantRetrievalIndex.publish_snapshot(self.state,snapshot,
                client=_DeadlineQdrant(self.client,deadline),promote=promote))
            proof['clip_count']=len(records)
        merge_index(self.state,records,vectors,asset_metadata=metadata,before_commit=before_commit)
        return proof


class SourceWorker:
    def __init__(self,store: SourceStore,*,company_id,media_client,media_image,embedding_adapter,
                 publisher,enabled=False,limits=None,scope_factory=None):
        if not isinstance(company_id,str) or not company_id or not re.fullmatch(r'sha256:[a-f0-9]{64}',media_image):
            raise WorkerError('invalid_worker_configuration')
        self.store,self.company_id,self.media,self.media_image=store,company_id,media_client,media_image
        self.embedding,self.publisher=embedding_adapter,publisher
        self.enabled,self.limits=enabled,limits or WorkerLimits()
        self.artifacts=Artifacts(store,self.limits)
        self.scope_factory=scope_factory

    async def checkpoint(self,lease,**body):
        await _thread(self.store.checkpoint_job,lease,checkpoint={
            'schema_version':1,'job_id':lease.job_id,'generation':lease.generation,'stage':lease.stage,**body})

    async def run_once(self,*,worker_id='source-worker',stages=('prepare','embed','publish'),asset_id=None):
        if self.enabled is not True: raise WorkerError('intake_worker_unavailable')
        if self.media is None or self.embedding is None or self.publisher is None:
            raise WorkerError('intake_worker_unavailable')
        lease=await _thread(self.store.claim_job,worker_id=worker_id,
            lease_seconds=self.limits.lease_seconds,stages=stages,asset_id=asset_id)
        if lease is None: return None
        context=await _thread(self.store.job_context,lease)
        stop=asyncio.Event();failure=[]
        async def heartbeat():
            while not stop.is_set():
                try: await asyncio.wait_for(stop.wait(),self.limits.heartbeat_seconds)
                except TimeoutError:
                    try: await _thread(self.store.heartbeat_job,lease,lease_seconds=self.limits.lease_seconds)
                    except Exception as error: failure.append(error);stop.set();return
        pulse=asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(self.limits.stage_seconds):
                if lease.stage=='prepare': result=await self.prepare(lease,context,stop)
                elif lease.stage=='embed': result=await self.embed(lease,context,stop)
                else:
                    # Publication is a finite synchronous critical section. Stop
                    # heartbeats before acquiring its SourceStore transaction.
                    stop.set();await pulse
                    lease=await _thread(self.store.heartbeat_job,lease,lease_seconds=900)
                    result=await self.publish(lease,context)
            if failure: raise failure[0]
            if lease.stage!='publish': await _thread(self.store.finish_job,lease,result=result)
            return {'job_id':lease.job_id,'stage':lease.stage,'status':'succeeded'}
        except BaseException as error:
            # Every launched stage can have external/local durable effects. Keep
            # uncertain attempts fenced; an operator/adapter must reconcile them.
            try: await _thread(self.store.fail_job,lease,error_code='stage_requires_reconciliation',uncertain=True)
            except IntakeError: pass  # Cancellation/expiry already changed authority.
            if isinstance(error,asyncio.CancelledError): raise
            code=str(error)
            return {'job_id':lease.job_id,'stage':lease.stage,'status':'needs_reconciliation',
                'error':code if isinstance(error,WorkerError) and re.fullmatch(r'[a-z0-9_]{1,80}',code)
                    else 'stage_requires_reconciliation'}
        finally:
            stop.set();await pulse

    def _scope(self,context,lease):
        factory=self.scope_factory
        if factory is None:
            from source_media import MediaScope
            factory=MediaScope
        return factory(company_id=self.company_id,owner_id=context['asset']['owner'],
            asset_id=lease.asset_id,job_id=lease.job_id)

    async def prepare(self,lease,context,stop):
        from source_media import MediaError
        scope=self._scope(context,lease);handle=None;released=False
        await self.checkpoint(lease,phase='media_submit_intent',scope=asdict(scope),
            original_sha256=context['asset']['sha256'],original_bytes=context['asset']['bytes'])
        try:
            # Admission validates this exact immutable descriptor; its source
            # acquisition record never comes from browser/model path arguments.
            async with _original(self.store,lease) as original:
                handle=await self.media.submit(scope,generation=lease.generation,collection=context['source']['collection'],
                    original_sha256=context['asset']['sha256'],original_bytes=context['asset']['bytes'],
                    expires_at=lease.expires_at,original_fd=original.fileno())
            await self.checkpoint(lease,phase='media_running',scope=asdict(scope),media_lease=asdict(handle))
            last_heartbeat=time.monotonic()
            while True:
                if stop.is_set(): raise WorkerError('job_cancelled')
                await _thread(self.store.job_context,lease)
                current=await self.media.status(scope,handle)
                if current['status']=='succeeded':
                    if current.get('cleanup_confirmed') is not True: raise WorkerError('media_cleanup_unconfirmed')
                    result=await self.receive_prepared(lease,context,scope,handle,current['result'])
                    await self.media.release(scope,handle);released=True
                    return result
                if current['status'] in {'failed','stopped','uncertain'}: raise WorkerError('media_preparation_failed')
                if time.monotonic()-last_heartbeat>=self.limits.heartbeat_seconds:
                    renewed=await _thread(self.store.heartbeat_job,lease,lease_seconds=self.limits.lease_seconds)
                    try:await self.media.heartbeat(scope,handle,expires_at=renewed.expires_at)
                    except MediaError:
                        # A completed worker rejects renewal; consume its exact
                        # terminal result rather than misclassify it as expired.
                        terminal=await self.media.status(scope,handle)
                        if terminal['status'] in {'starting','running'}:raise
                        continue
                    last_heartbeat=time.monotonic()
                try: await asyncio.wait_for(stop.wait(),self.limits.poll_seconds)
                except TimeoutError: pass
        finally:
            if not released:
                # The client drains an accepted admission on cancellation, then
                # re-raises before returning its handle. Recover that exact
                # private generation so cancellation still confirms whole Stop.
                if handle is None:
                    handle=await _settle(self.media.recover(scope,lease.generation))
                if handle is not None:
                    status=await _settle(self.media.stop(scope,handle))
                    if status.get('cleanup_confirmed') is not True: raise WorkerError('media_cleanup_unconfirmed')

    async def receive_prepared(self,lease,context,scope,handle,result):
        if (not isinstance(result,dict) or result.get('profile')!=PROFILE
                or result.get('image_digest')!=self.media_image):
            raise WorkerError('media_image_mismatch')
        if (result.get('scope')!=asdict(scope) or result.get('generation')!=lease.generation
                or result.get('purpose')!='prepare' or result.get('schema_version')!=1):
            raise WorkerError('media_scope_mismatch')
        manifest=result.get('manifest');expected=validate_manifest(manifest,context)
        files=result.get('files',[])
        if (not isinstance(files,list) or not all(isinstance(item,dict) and isinstance(item.get('path'),str) for item in files)
                or {item.get('path') for item in files}!=set(expected) or len(files)!=len(expected)):
            raise WorkerError('invalid_prepared_files')
        sizes={entry['file']:entry['size_bytes'] for clip in manifest['clips'] for entry in (clip,clip['thumbnail'])}
        total=0;indices=set()
        for item in files:
            if (type(item.get('index')) is not int or item['index']<0 or item['index'] in indices
                    or type(item.get('size_bytes')) is not int or not 0<item['size_bytes']<=32*1024**2
                    or not SHA.fullmatch(str(item.get('sha256','')))
                    or item.get('size_bytes')!=sizes.get(item['path'],item['size_bytes'])
                    or (expected[item['path']] is not None and expected[item['path']]!=item['sha256'])):
                raise WorkerError('invalid_prepared_files')
            if item['path']=='manifest.json' and item['size_bytes']>self.limits.manifest_bytes:
                raise WorkerError('manifest_size_limit')
            indices.add(item['index']);total+=item['size_bytes']
        if total>self.limits.prepared_bytes: raise WorkerError('prepared_size_limit')
        if result.get('total_bytes')!=total: raise WorkerError('prepared_size_mismatch')
        async with self.artifacts.allocation(total,len(files)):
            identity,path=self.artifacts.stage(lease.asset_id,'prepare')
            for item in files:
                await _thread(self.store.job_context,lease)
                digest=hashlib.sha256();received=0
                fd=os.open(path/item['path'],os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                try:
                    async for chunk in self.media.read_output(scope,handle,item['index']):
                        if not isinstance(chunk,bytes) or len(chunk)>1024*1024: raise WorkerError('invalid_output_stream')
                        received+=len(chunk)
                        if received>item['size_bytes']: raise WorkerError('prepared_size_mismatch')
                        digest.update(chunk)
                        view=memoryview(chunk)
                        while view:
                            written=await _thread(os.write,fd,view)
                            if written<=0: raise WorkerError('artifact_write_failed')
                            view=view[written:]
                    if received!=item['size_bytes'] or digest.hexdigest()!=item['sha256']:
                        raise WorkerError('prepared_hash_mismatch')
                    await _thread(os.fsync,fd)
                finally: os.close(fd)
            if json.loads((path/'manifest.json').read_bytes())!=manifest: raise WorkerError('prepared_manifest_mismatch')
            await _thread(self.store.job_context,lease)
            receipt=await _thread(self.artifacts.seal,identity,path)
            return {**receipt,'profile':PROFILE,'recipe':RECIPE,'image_digest':self.media_image,
                'original_sha256':context['asset']['sha256'],'clip_count':len(manifest['clips'])}

    def prepared(self,context):
        receipt=context['completed_stages'].get('prepare')
        if not receipt or receipt.get('image_digest')!=self.media_image: raise WorkerError('prepared_stage_required')
        path,manifest=self.artifacts.load(context['asset']['id'],receipt)
        expected=validate_manifest(manifest,context)
        for name,checksum in expected.items():
            file=path/name;_regular(file)
            if checksum is not None and _hash(file)!=checksum: raise WorkerError('artifact_integrity_failed')
        return path,manifest,receipt

    async def embed(self,lease,context,stop):
        path,manifest,prepared=await _thread(self.prepared,context)
        clips=manifest['clips'];batches=[];batch=[];size=0
        if len(clips)>self.limits.embedding_import_clips or sum(clip['size_bytes'] for clip in clips)>self.limits.embedding_import_bytes:
            raise WorkerError('embedding_authorized_scope_exceeded')
        for clip in clips:
            if batch and (len(batch)==self.limits.embedding_batch_clips or size+clip['size_bytes']>64*1024**2):
                batches.append(batch);batch=[];size=0
            batch.append(clip);size+=clip['size_bytes']
        if batch:batches.append(batch)
        rows=[];admissions=[]
        for number,batch in enumerate(batches):
            await _thread(self.store.job_context,lease)
            admission=await self.embedding.admit(context,batch,
                idempotency_key=f'{lease.job_id}:{lease.generation}:{number}')
            if not isinstance(admission,dict) or not admission.get('reservation_id') or not admission.get('attempt_id'):
                raise WorkerError('embedding_admission_required')
            admissions.append(admission)
            await self.checkpoint(lease,phase='embedding_admitted',batch=number,admissions=admissions,
                prepared_manifest_sha256=prepared['manifest_sha256'])
            if stop.is_set(): raise WorkerError('job_cancelled')
            inputs=[]
            for clip in batch:
                data=await _thread((path/clip['file']).read_bytes)
                if len(data)!=clip['size_bytes'] or hashlib.sha256(data).hexdigest()!=clip['sha256']:
                    raise WorkerError('artifact_integrity_failed')
                inputs.append({'id':clip['id'],'sha256':clip['sha256'],
                    'duration_seconds':clip['duration_seconds'],'content':data})
            await _thread(self.store.job_context,lease)
            result=await self.embedding.run(admission,inputs,cancelled=stop)
            if (result.get('model')!=MODEL_ID or result.get('model_revision')!=MODEL_REVISION
                    or result.get('dimensions')!=DIMENSIONS or result.get('frames_per_clip')!=8
                    or result.get('attempt_id')!=admission['attempt_id']): raise WorkerError('embedding_provenance_mismatch')
            returned=result.get('clips',[])
            if len(returned)!=len(batch): raise WorkerError('embedding_coverage_mismatch')
            mapping={row.get('id'):row for row in returned}
            if set(mapping)!={clip['id'] for clip in batch}: raise WorkerError('embedding_coverage_mismatch')
            for clip in batch:
                row=mapping[clip['id']]
                if row.get('sha256')!=clip['sha256']: raise WorkerError('embedding_hash_mismatch')
                vector=np.asarray(row.get('embedding'),dtype=np.float32)
                if vector.shape!=(DIMENSIONS,) or not np.isfinite(vector).all() or np.linalg.norm(vector)<1e-8:
                    raise WorkerError('invalid_embedding')
                rows.append({'id':clip['id'],'sha256':clip['sha256'],'embedding':(vector/np.linalg.norm(vector)).tolist()})
            await _thread(self.store.job_context,lease)
        data={'model':MODEL_ID,'model_revision':MODEL_REVISION,'dimensions':DIMENSIONS,'frames_per_clip':8,
            'prepared_manifest_sha256':prepared['manifest_sha256'],'rows':rows,
            'reservation_ids':[item['reservation_id'] for item in admissions]}
        encoded=json.dumps(data,allow_nan=False,separators=(',',':')).encode()
        if len(encoded)>self.limits.embedding_bytes: raise WorkerError('embedding_result_limit')
        async with self.artifacts.allocation(len(encoded)):
            identity,out=self.artifacts.stage(lease.asset_id,'embed')
            fd=os.open(out/'manifest.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream: stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            receipt=await _thread(self.artifacts.seal,identity,out)
            return {**receipt,'model':MODEL_ID,'model_revision':MODEL_REVISION,
                'prepared_manifest_sha256':prepared['manifest_sha256'],'clip_count':len(rows)}

    def records(self,context):
        path,manifest,prepared=self.prepared(context)
        embedding=context['completed_stages'].get('embed')
        if not embedding: raise WorkerError('embedding_stage_required')
        _,encoded=self.artifacts.load(context['asset']['id'],embedding)
        if (encoded.get('model')!=MODEL_ID or encoded.get('model_revision')!=MODEL_REVISION
                or encoded.get('dimensions')!=DIMENSIONS or encoded.get('frames_per_clip')!=8
                or encoded.get('prepared_manifest_sha256')!=prepared['manifest_sha256']):
            raise WorkerError('embedding_provenance_mismatch')
        rows=encoded['rows']
        if [row['id'] for row in rows]!=[clip['id'] for clip in manifest['clips']]:
            raise WorkerError('embedding_coverage_mismatch')
        asset=context['asset'];source=context['source'];records=[];vectors=[]
        for clip,row in zip(manifest['clips'],rows):
            if row['sha256']!=clip['sha256']: raise WorkerError('embedding_hash_mismatch')
            records.append({'id':clip['id'],'video_id':asset['id'],'dataset':source['collection'],
                'camera':source['name'],'path':str(self.store.blobs/asset['sha256']),
                'start':clip['source_elapsed_start_seconds'],'end':clip['source_elapsed_end_seconds'],
                'duration':manifest['source']['duration_seconds'],
                'sample_time':clip['source_elapsed_start_seconds'],'capture_clock':'unknown','capture_utc':None,
                'original_pts_start_seconds':clip['original_pts_start_seconds'],
                'original_pts_end_seconds':clip['original_pts_end_seconds'],
                'storage_kind':'imported_source','preparation_profile':PROFILE,'preparation_recipe':RECIPE,
                'preparation_image_digest':self.media_image,'prepared_path':str(path/clip['file']),
                'prepared_manifest_sha256':prepared['manifest_sha256'],
                'prepared_size_bytes':clip['size_bytes'],'prepared_width':clip['width'],'prepared_height':clip['height'],
                'prepared_sha256':clip['sha256'],'prepared_start':0.,'prepared_end':clip['duration_seconds'],
                'thumbnail_path':str(path/clip['thumbnail']['file']),'thumbnail_sha256':clip['thumbnail']['sha256'],
                'thumbnail_size_bytes':clip['thumbnail']['size_bytes'],
                'embedded_clip_sha256':clip['sha256'],'model':MODEL_ID,'model_revision':MODEL_REVISION,
                'frames_embedded':8})
            vectors.append(row['embedding'])
        metadata={asset['id']:{'asset_id':asset['id'],'source_id':source['id'],'object_id':asset['object_id'],
            'original_sha256':asset['sha256'],'collection':source['collection']}}
        return records,np.asarray(vectors,dtype=np.float32),metadata

    async def publish(self,lease,context):
        if self.publisher.resolver_ready() is not True: raise WorkerError('media_resolver_unavailable')
        async with _original(self.store,lease): pass
        records,vectors,metadata=await _thread(self.records,context)
        await self.checkpoint(lease,phase='publication_intent',clip_ids=[row['id'] for row in records],
            prepared_manifest_sha256=context['completed_stages']['prepare']['manifest_sha256'])
        return await _thread(self.store.finish_publication,lease,
            publish=lambda:self.publisher.publish(records,vectors,metadata))
