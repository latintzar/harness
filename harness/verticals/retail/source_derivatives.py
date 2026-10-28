"""Private, owner-bound receipts for fixed sandbox media derivations.

Only opaque bytes cross this boundary. The broker owns decoding, image selection
and process cleanup. Cache readers re-authorize the original and verify sealed
output descriptors, including in a separate provider-input process.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
import asyncio
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from imported_media import ImportedMedia, VerifiedFile
from source_intake import SourceStore
from source_worker import Artifacts, WorkerLimits, _directory, _thread, _settle, _fsync_dir
from source_media import (MediaScope, MediaLease, MediaError, DerivationRequest, DERIVE_PROFILE,
    DERIVE_RECIPE, DERIVE_IMAGE, _validate_derived_manifest)
from workspace_identity import workspace_scope

RECEIPT = re.compile(r'derivative-[a-f0-9]{32}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
MANIFEST_BYTES = 512 * 1024
MAX_ROWS = 512


class DerivativeError(ValueError):
    """Stable errors without source paths, broker tokens or provider diagnostics."""


@dataclass(frozen=True)
class DerivativeReceipt:
    receipt_id: str
    manifest: dict
    files: list[VerifiedFile]


def _json(value, maximum=65536):
    try: result=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    except (ValueError,TypeError,RecursionError):raise DerivativeError('invalid_derivative_metadata') from None
    if len(result.encode())>maximum:raise DerivativeError('derivative_metadata_limit')
    return result


def _digest(value):return hashlib.sha256(_json(value).encode()).hexdigest()


def _require(value,code='invalid_derivative_receipt'):
    if not value:raise DerivativeError(code)


class SourceDerivatives:
    def __init__(self,state,*,company_id,media_client=None,enabled=False):
        self.state=Path(state).absolute()
        _require(isinstance(company_id,str) and workspace_scope(self.state)==company_id,'derivative_company_mismatch')
        self.company_id=company_id;self.media=media_client;self.enabled=enabled is True
        self.resolver=ImportedMedia(self.state);self.root=self.resolver.root
        self.path=self.root/'derivatives.sqlite3'
        self.limits=WorkerLimits();self.wait_seconds=650.;self.poll_seconds=.25

    def _authorize(self,record,owner,purpose,frame_seconds=()):
        _require(isinstance(owner,str) and 0<len(owner)<=200,'derivative_owner_required')
        _require(isinstance(record,dict),'invalid_derivative_source')
        record=copy.deepcopy(record);_json(record)
        resolved=self.resolver.resolve(record)
        asset,source,_=self.resolver._authority(record['video_id'])
        _require(asset['owner']==owner and source['owner']==owner,'derivative_owner_mismatch')
        self._published(asset['id'],owner)
        request=DerivationRequest.parse(dict(purpose=purpose,clip_id=record['id'],start=record['start'],
            end=record['end'],frame_seconds=list(frame_seconds)))
        binding=dict(company_id=self.company_id,owner_id=owner,asset_id=asset['id'],source_id=source['id'],
            source_object_id=asset['object_id'],collection=source['collection'],original_sha256=asset['sha256'],
            original_bytes=asset['bytes'],prepared_manifest_sha256=record['prepared_manifest_sha256'],
            request=json.loads(_json(asdict(request))),profile=DERIVE_PROFILE,recipe=DERIVE_RECIPE,image_digest=DERIVE_IMAGE)
        return record,binding,resolved

    def _published(self,asset_id,owner):
        fd=self.resolver._open(('intake.sqlite3',),sealed=False)
        try:
            before=os.fstat(fd)
            with sqlite3.connect((self.root/'intake.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
                row=db.execute("""SELECT p.result FROM assets a JOIN sources s ON s.id=a.source_id
                    JOIN source_objects o ON o.id=a.object_id JOIN jobs p ON p.asset_id=a.id
                    WHERE a.id=? AND a.status='ready' AND a.owner=? AND s.owner=?
                    AND o.source_id=a.source_id AND p.stage='publish' AND p.status='succeeded'""",
                    (asset_id,owner,owner)).fetchone()
            after=os.stat(self.root/'intake.sqlite3',follow_symlinks=False)
            _require((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'derivative_store_changed')
            _require(row and isinstance(row[0],str) and len(row[0].encode())<=65536,'derivative_publication_unavailable')
            receipt=json.loads(row[0])
            _require(isinstance(receipt,dict) and isinstance(receipt.get('generation_id'),str) and receipt['generation_id']
                and isinstance(receipt.get('qdrant_collection'),str) and receipt['qdrant_collection']
                and isinstance(receipt.get('qdrant_fingerprint'),str) and SHA.fullmatch(receipt['qdrant_fingerprint']),
                'derivative_publication_unavailable')
        finally:os.close(fd)

    def _initialize(self):
        # Intake already exists and was independently authorized before writes.
        fd=self.resolver._open(('intake.sqlite3',),sealed=False);os.close(fd)
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid()
                and not info.st_mode&0o077,'invalid_derivative_storage')
        finally:os.close(fd)
        with self._db(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY, cache_key TEXT UNIQUE NOT NULL, binding TEXT NOT NULL,
                record TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL,
                handle TEXT, artifact TEXT, files TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL)''')
        _directory(self.root/'derivative-locks')
        _fsync_dir(self.root)

    @contextmanager
    def _db(self,*,write=False):
        fd=self.resolver._open(('derivatives.sqlite3',),sealed=False)
        db=None
        try:
            before=os.fstat(fd)
            db=sqlite3.connect(self.path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=10)
            db.row_factory=sqlite3.Row
            if write:
                db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA max_page_count=16384')  # 64MiB at SQLite's default page size.
                db.execute('BEGIN IMMEDIATE')
            else:db.execute('BEGIN')
            yield db
            after=os.stat(self.path,follow_symlinks=False)
            _require((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'derivative_store_changed')
            if write:db.commit()
        except sqlite3.Error:raise DerivativeError('derivative_storage_unavailable') from None
        finally:
            if db is not None:db.close()
            os.close(fd)

    def _row(self,identity=None,*,key=None):
        if not self.path.exists():return None
        if identity is not None:_require(isinstance(identity,str) and RECEIPT.fullmatch(identity))
        with self._db() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts'").fetchone():return None
            row=db.execute('SELECT * FROM receipts WHERE '+('id=?' if identity is not None else 'cache_key=?'),
                (identity if identity is not None else key,)).fetchone()
        return dict(row) if row else None

    def _admit(self,record,binding):
        self._initialize();key=_digest(binding)
        with self._db(write=True) as db:
            row=db.execute('SELECT * FROM receipts WHERE cache_key=?',(key,)).fetchone()
            if row:return dict(row)
            _require(db.execute('SELECT count(*) FROM receipts').fetchone()[0]<MAX_ROWS,'derivative_receipt_quota')
            now=time.time();identity='derivative-'+uuid.uuid4().hex
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,'pending',1,NULL,NULL,NULL,NULL,?,?)",
                (identity,key,_json(binding),_json(record),now,now))
            return dict(db.execute('SELECT * FROM receipts WHERE id=?',(identity,)).fetchone())

    def _update(self,identity,expected_generation,**fields):
        _require(fields and set(fields)<={'status','handle','artifact','files','error','generation'})
        with self._db(write=True) as db:
            changed=db.execute('UPDATE receipts SET '+','.join(name+'=?' for name in fields)+',updated=? WHERE id=? AND generation=?',
                (*fields.values(),time.time(),identity,expected_generation)).rowcount
            _require(changed==1,'stale_derivative_generation')

    @asynccontextmanager
    async def _locked(self,identity):
        _require(RECEIPT.fullmatch(identity))
        fd=os.open(self.root/'derivative-locks'/(identity+'.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid()
                and not info.st_mode&0o077,'invalid_derivative_storage')
            deadline=time.monotonic()+self.wait_seconds
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise DerivativeError('derivative_busy')
                    await asyncio.sleep(self.poll_seconds)
            yield
        finally:os.close(fd)

    def _scope(self,row,binding):
        return MediaScope(self.company_id,binding['owner_id'],binding['asset_id'],'job-'+row['id'].split('-')[1])

    def _validate_files(self,manifest,files,binding,scope):
        _require(isinstance(files,list) and 2<=len(files)<=4)
        paths=set();indices=set();total=0
        purpose=binding['request']['purpose']
        for item in files:
            _require(isinstance(item,dict) and set(item)=={'index','path','size_bytes','sha256'})
            path=item['path'];size=item['size_bytes'];index=item['index']
            allowed={'manifest.json','clip.mp4'} if purpose!='inspect_frames_v1' else {'manifest.json','frame-00.jpg','frame-01.jpg','frame-02.jpg'}
            maximum=MANIFEST_BYTES if path=='manifest.json' else 64*1024**2 if purpose=='source_cut_v1' else 8*1024**2
            _require(path in allowed and path not in paths and type(index) is int and index>=0 and index not in indices
                and type(size) is int and 0<size<=maximum and isinstance(item['sha256'],str) and SHA.fullmatch(item['sha256']))
            paths.add(path);indices.add(index);total+=size
        _validate_derived_manifest(manifest,files,scope=scope,collection=binding['collection'],
            size=binding['original_bytes'],checksum=binding['original_sha256'],
            request=DerivationRequest.parse(binding['request']))
        return total

    def read_receipt(self,receipt_id,record=None,*,owner_principal_id,purpose):
        try:return self._read_receipt(receipt_id,record,owner_principal_id=owner_principal_id,purpose=purpose)
        except DerivativeError:raise
        except (ValueError,RuntimeError,OSError,KeyError,TypeError,OverflowError,RecursionError):
            raise DerivativeError('derivative_receipt_unavailable') from None

    def _read_receipt(self,receipt_id,record=None,*,owner_principal_id,purpose):
        row=self._row(receipt_id)
        _require(row is not None and row['status']=='ready','derivative_receipt_unavailable')
        return self._read(row,record,owner_principal_id,purpose)

    def _read(self,row,record,owner,purpose):
        try:
            saved=json.loads(row['record']);binding=json.loads(row['binding'])
            _require(_digest(binding)==row['cache_key'] and binding['owner_id']==owner
                and binding['company_id']==self.company_id and binding['request']['purpose']==purpose,
                'derivative_scope_mismatch')
            _,actual,_=self._authorize(saved if record is None else record,owner,purpose,binding['request']['frame_seconds'])
            _require(actual==binding,'derivative_source_mismatch')
            artifact=json.loads(row['artifact']);files=json.loads(row['files'])
            _require(isinstance(artifact,dict) and set(artifact)=={'artifact_id','manifest_sha256'}
                and re.fullmatch(r'derive-[a-f0-9]{32}',artifact['artifact_id']))
            base=('worker-artifacts',binding['asset_id'],artifact['artifact_id'])
            marker=next(item for item in files if item['path']=='manifest.json')
            _require(marker['sha256']==artifact['manifest_sha256'])
            manifest=json.loads(VerifiedFile(self.resolver,base+('manifest.json',),marker['sha256'],
                marker['size_bytes'],'manifest.json','application/json',MANIFEST_BYTES).read_bytes(maximum=MANIFEST_BYTES))
            self._validate_files(manifest,files,binding,self._scope(row,binding))
            handles=[]
            for entry in manifest['files']:
                maximum=64*1024**2 if purpose=='source_cut_v1' else 8*1024**2
                handle=VerifiedFile(self.resolver,base+(entry['file'],),entry['sha256'],entry['size_bytes'],
                    entry['file'],entry['mime_type'],maximum)
                handle.verify();handles.append(handle)
            return DerivativeReceipt(row['id'],manifest,handles)
        except (KeyError,TypeError,StopIteration,json.JSONDecodeError):
            raise DerivativeError('invalid_derivative_receipt') from None

    async def derive(self,record,purpose,*,owner_principal_id,frame_seconds=()):
        try:return await self._derive(record,purpose,owner_principal_id=owner_principal_id,frame_seconds=frame_seconds)
        except DerivativeError:raise
        except TimeoutError:raise DerivativeError('source_derivation_timeout') from None
        except (ValueError,RuntimeError,OSError,KeyError,TypeError,OverflowError,RecursionError):
            raise DerivativeError('source_derivation_failed') from None

    async def _derive(self,record,purpose,*,owner_principal_id,frame_seconds=()):
        record,binding,resolved=await _thread(self._authorize,record,owner_principal_id,purpose,frame_seconds)
        row=await _thread(self._row,key=_digest(binding))
        if row and row['status']=='ready':
            return await _thread(self.read_receipt,row['id'],record,owner_principal_id=owner_principal_id,purpose=purpose)
        _require(self.enabled and self.media is not None,'source_derivation_unavailable')
        row=await _thread(self._admit,record,binding)
        async with self._locked(row['id']):
            row=await _thread(self._row,row['id'])
            if row['status']=='ready':
                return await _thread(self.read_receipt,row['id'],record,owner_principal_id=owner_principal_id,purpose=purpose)
            if row['status'] not in {'pending','cancelled'}:
                await self._reconcile(row,binding)
                row=await _thread(self._row,row['id'])
                if row['status']=='ready':return await _thread(self._read,row,record,owner_principal_id,purpose)
                raise DerivativeError('derivation_reconciled_retry_required')
            if row['status']=='cancelled':
                await _thread(self._update,row['id'],row['generation'],generation=row['generation']+1,
                    status='pending',handle=None,artifact=None,files=None,error=None)
                row=await _thread(self._row,row['id'])
            _,current,resolved=await _thread(self._authorize,record,owner_principal_id,purpose,frame_seconds)
            _require(current==binding,'derivative_source_mismatch')
            return await self._run(row,record,binding,resolved)

    async def _cleanup(self,row,binding,handle=None):
        scope=self._scope(row,binding)
        if handle is None:
            handle=await self.media.recover(scope,row['generation'])
        if handle is not None:
            status=await self.media.stop(scope,handle)
            _require(status.get('cleanup_confirmed') is True,'derivative_cleanup_unconfirmed')
            result=await self.media.release(scope,handle)
            _require(isinstance(result,dict) and result.get('status')=='released','derivative_release_unconfirmed')

    async def _reconcile(self,row,binding):
        try:
            await _settle(self._cleanup(row,binding))
            if row['artifact'] and row['files']:
                await _thread(self._read,row,None,binding['owner_id'],binding['request']['purpose'])
                await _thread(self._update,row['id'],row['generation'],status='ready',error=None)
            else:await _thread(self._update,row['id'],row['generation'],status='cancelled',error=None)
        except BaseException:
            await _thread(self._update,row['id'],row['generation'],status='needs_reconciliation',error='cleanup_unconfirmed')
            raise

    async def _receive(self,row,binding,handle,result):
        scope=self._scope(row,binding)
        _require(isinstance(result,dict) and result.get('schema_version')==1
            and result.get('scope')==asdict(scope) and result.get('generation')==row['generation']
            and result.get('profile')==DERIVE_PROFILE and result.get('image_digest')==DERIVE_IMAGE
            and result.get('purpose')==binding['request']['purpose'],'derivative_result_scope_mismatch')
        files=result.get('files');manifest=result.get('manifest')
        total=self._validate_files(manifest,files,binding,scope)
        _require(result.get('total_bytes')==total,'derivative_result_size_mismatch')
        artifacts=Artifacts(SourceStore(self.state),self.limits)
        async with artifacts.allocation(total,len(files)):
            identity,path=artifacts.stage(binding['asset_id'],'derive')
            for item in files:
                fd=os.open(path/item['path'],os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                digest=hashlib.sha256();size=0
                try:
                    async for chunk in self.media.read_output(scope,handle,item['index']):
                        _require(isinstance(chunk,bytes) and 0<len(chunk)<=1024*1024,'invalid_derivative_stream')
                        size+=len(chunk);_require(size<=item['size_bytes'],'derivative_output_size')
                        digest.update(chunk);view=memoryview(chunk)
                        while view:
                            count=await _thread(os.write,fd,view)
                            _require(count>0,'derivative_write_failed');view=view[count:]
                    _require(size==item['size_bytes'] and digest.hexdigest()==item['sha256'],'derivative_output_integrity')
                    await _thread(os.fsync,fd)
                finally:os.close(fd)
            _require(json.loads((path/'manifest.json').read_bytes())==manifest,'derivative_manifest_mismatch')
            # Authority can change while a worker runs. Recheck before publishing.
            await _thread(self._authorize,json.loads(row['record']),binding['owner_id'],
                binding['request']['purpose'],binding['request']['frame_seconds'])
            artifact=await _thread(artifacts.seal,identity,path)
            await _thread(self._update,row['id'],row['generation'],status='copied',artifact=_json(artifact),files=_json(files))

    async def _run(self,row,record,binding,resolved):
        scope=self._scope(row,binding);handle=None;finished=False
        await _thread(self._update,row['id'],row['generation'],status='admitting')
        try:
            async with asyncio.timeout(self.limits.stage_seconds):
                # Held validated original FD never becomes a caller-selected path.
                with resolved.original.open() as original:
                    handle=await self.media.derive(scope,generation=row['generation'],collection=binding['collection'],
                        original_sha256=binding['original_sha256'],original_bytes=binding['original_bytes'],
                        expires_at=time.time()+self.limits.lease_seconds,original_fd=original.fileno(),derivation=binding['request'])
                await _thread(self._update,row['id'],row['generation'],status='running',handle=_json(asdict(handle)))
                last_heartbeat=time.monotonic()
                while True:
                    current=await self.media.status(scope,handle)
                    if current['status']=='succeeded':
                        _require(current.get('cleanup_confirmed') is True,'derivative_cleanup_unconfirmed')
                        await self._receive(row,binding,handle,current['result'])
                        await self._cleanup(row,binding,handle)
                        await _thread(self._update,row['id'],row['generation'],status='ready',error=None)
                        finished=True
                        return await _thread(self.read_receipt,row['id'],record,owner_principal_id=binding['owner_id'],
                            purpose=binding['request']['purpose'])
                    _require(current['status'] in {'starting','running'},'source_derivation_failed')
                    if time.monotonic()-last_heartbeat>=self.limits.heartbeat_seconds:
                        try:await self.media.heartbeat(scope,handle,expires_at=time.time()+self.limits.lease_seconds)
                        except MediaError:
                            # Completion may race the status read; a terminal worker
                            # no longer accepts lease renewal. Read its final state.
                            terminal=await self.media.status(scope,handle)
                            if terminal['status'] in {'starting','running'}:raise
                            continue
                        last_heartbeat=time.monotonic()
                    await asyncio.sleep(self.poll_seconds)
        finally:
            if not finished:
                try:
                    await _settle(self._cleanup(row,binding,handle))
                    saved=await _thread(self._row,row['id'])
                    await _thread(self._update,row['id'],row['generation'],
                        status='copied' if saved['artifact'] else 'cancelled',error=None)
                except BaseException:
                    await _thread(self._update,row['id'],row['generation'],status='needs_reconciliation',error='cleanup_unconfirmed')
                    raise
