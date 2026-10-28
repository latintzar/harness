"""Durable owner-private shared links, acquired through the existing intake gate.

The host supplies an independent membership/collection guard. Logout does not
revoke company membership; observer sessions cannot mutate this store. No paid
work, decoder, external credentials, or indexing implementation lives here.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
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
from provider_credentials import Vault
from source_intake import IntakeActor,IntakeError,SourceStore
from source_service import SourceHost
from workspace_identity import workspace_scope
from link_download import PublicDownload,LinkError,require,classify,media_magic,ACTIONS,CHUNK

MAX_RECORDS=1000
MAX_ATTEMPTS=5
JOB_SECONDS=300
STOP_WAIT_SECONDS=10
ID=re.compile(r'link-[a-f0-9]{32}\Z')


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

async def drain(task):
 while not task.done():
  try:await asyncio.shield(task)
  except asyncio.CancelledError:continue
  except Exception:break
 if task.done()and not task.cancelled():task.exception()

async def offload(fn,*args,**kwargs):
 task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs))
 try:return await asyncio.shield(task)
 except asyncio.CancelledError:await drain(task);raise


class LinkIntake:
 def __init__(self,state,*,store=None,host=None,authorized,enabled=False,transport=None):
  self.state=Path(state).absolute();self.company=workspace_scope(self.state)
  require(callable(authorized)and type(enabled)is bool)
  self.authorized,self.enabled=authorized,enabled
  self.store=store or SourceStore(self.state);self.host=host or SourceHost(self.state)
  require(self.store.root.parent.resolve()==self.state.resolve()and self.host.state.resolve()==self.state.resolve(),'workspace_access_required')
  self.root=self.state/'source-links';SourceStore._directory(self.root)
  self.database=self.root/'links.sqlite3';fd=SourceStore._open(self.database,os.O_RDWR|os.O_CREAT);os.close(fd)
  self.vault=Vault(self.root,self.company);self.vault.key(create=True)
  self.transport=transport or PublicDownload();self.tasks={};self.closed=False;self.initialized=False;self.initialization=asyncio.Lock()
  with self.db()as db:
   db.execute('CREATE TABLE IF NOT EXISTS links(id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_key TEXT NOT NULL,collection TEXT NOT NULL,url_hash TEXT NOT NULL,url_cipher TEXT NOT NULL,provider TEXT NOT NULL,status TEXT NOT NULL,error TEXT,attempts INTEGER NOT NULL DEFAULT 0,metadata TEXT,upload_id TEXT,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(owner,request_key))')

 @contextmanager
 def db(self,*,write=False):
  db=sqlite3.connect(self.database,timeout=5,isolation_level=None);db.row_factory=sqlite3.Row
  try:
   db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA max_page_count=8192')
   if write:db.execute('BEGIN IMMEDIATE')
   yield db
   if write:db.execute('COMMIT')
  except BaseException:
   if db.in_transaction:db.execute('ROLLBACK')
   raise
  finally:db.close()

 def _row(self,db,actor,identity):
  require(isinstance(actor,IntakeActor)and isinstance(identity,str)and ID.fullmatch(identity),'record_not_found')
  row=db.execute('SELECT * FROM links WHERE id=? AND owner=?',(identity,actor.principal_id)).fetchone()
  require(row is not None,'record_not_found');return dict(row)

 def _authority(self,actor,collection,*,write=False):
  require(isinstance(actor,IntakeActor),'workspace_access_required')
  if write:actor.require_write()
  require(self.authorized(actor.principal_id,collection)is True,'workspace_access_required')

 def _public(self,row):
  value={key:row[key]for key in('id','provider','status','collection','created','updated')}
  value.update(received_bytes=0,size_bytes=None,filename=None,import_id=row['upload_id'])
  if row['metadata']:
   meta=json.loads(row['metadata']);value.update(filename=meta['filename'],size_bytes=meta['size_bytes'])
  if row['upload_id']:
   result=self.store.get_import(IntakeActor(row['owner']),row['upload_id'])
   value['received_bytes']=result['upload']['committed_bytes']
   value['import']=result
   if row['status']in{'cancelling','cancelled'}and result['asset'] and any(j['status']=='needs_reconciliation'and j['error']=='cancel_requested'for j in result['asset']['jobs']):value['status']='cancelling'
   # Internal import projection has no path/token but the HTTP adapter uses the
   # existing source_routes projection before exposing it to the browser.
   if result['upload']['status']=='sealed'and value['status']not in{'cancelled','cancelling'}:value['status']='received'
  if row['error']:value['action']={'code':row['error'],'message':ACTIONS.get(row['error'],ACTIONS['download_interrupted'])}
  return value

 def create(self,actor,*,collection,url,idempotency_key):
  self._authority(actor,collection,write=True);require(self.enabled and not self.closed,'source_service_unavailable')
  require(isinstance(collection,str)and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',collection),'invalid_collection')
  require(isinstance(idempotency_key,str)and 0<len(idempotency_key.encode())<=160 and all(ord(c)>=32 and ord(c)!=127 for c in idempotency_key),'invalid_idempotency_key')
  try:provider,normalized,action=classify(url)
  except (ValueError,UnicodeError):raise LinkError('invalid_link')from None
  url_hash=hashlib.sha256(normalized.encode()).hexdigest();now=time.time()
  with self.db(write=True)as db:
   old=db.execute('SELECT * FROM links WHERE owner=? AND request_key=?',(actor.principal_id,idempotency_key)).fetchone()
   if old:
    require(old['url_hash']==url_hash and old['collection']==collection,'idempotency_conflict');return self._public(dict(old))
   require(db.execute('SELECT count(*) FROM links').fetchone()[0]<MAX_RECORDS,'link_record_limit')
   identity='link-'+uuid.uuid4().hex
   cipher=self.vault.seal(normalized,principal=actor.principal_id,provider=identity,generation=1,field='tokens')
   db.execute('INSERT INTO links(id,owner,request_key,collection,url_hash,url_cipher,provider,status,error,created,updated)VALUES(?,?,?,?,?,?,?,?,?,?,?)',
    (identity,actor.principal_id,idempotency_key,collection,url_hash,cipher,provider,'needs_action'if action else'queued',action,now,now))
   row=self._row(db,actor,identity)
  return self._public(row)

 def get(self,actor,identity):
  with self.db()as db:row=self._row(db,actor,identity)
  self._authority(actor,row['collection']);return self._public(row)

 def list(self,actor,*,collection=None):
  require(isinstance(actor,IntakeActor),'workspace_access_required')
  with self.db()as db:
   rows=db.execute('SELECT * FROM links WHERE owner=? ORDER BY created DESC LIMIT 100',(actor.principal_id,)).fetchall()
  return[self._public(dict(row))for row in rows if(collection is None or row['collection']==collection)and self.authorized(actor.principal_id,row['collection'])is True]

 def _current(self,actor,identity):
  if self.closed:return False
  try:
   db=sqlite3.connect(f'file:{self.database}?mode=ro',uri=True,timeout=0)
   try:row=db.execute('SELECT collection,status FROM links WHERE id=? AND owner=?',(identity,actor.principal_id)).fetchone()
   finally:db.close()
   return bool(row and row[1]=='downloading'and self.authorized(actor.principal_id,row[0])is True)
  except (OSError,sqlite3.Error,ValueError):return False

 @contextmanager
 def _lease(self,name):
  fd=SourceStore._open(self.root/(name+'.lock'),os.O_RDWR|os.O_CREAT)
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
  except BlockingIOError:raise LinkError('download_capacity')from None
  finally:os.close(fd)

 @contextmanager
 def _slot(self):
  selected=None
  for n in range(2):
   lease=self._lease('slot-'+str(n))
   try:lease.__enter__();selected=lease;break
   except LinkError:continue
  require(selected is not None,'download_capacity')
  try:yield
  finally:selected.__exit__(None,None,None)

 def _update(self,actor,identity,**values):
  require(set(values)<= {'status','error','metadata','upload_id'},'invalid_link_state')
  with self.db(write=True)as db:
   row=self._row(db,actor,identity)
   # Stop is monotonic; completed acquisition may still have a durable seal
   # intent. Its authoritative SourceStore receipt survives this presentation.
   if row['status']in{'cancelling','cancelled'}and 'status'in values and values['status']not in{'cancelled','cancelling'}:return
   db.execute('UPDATE links SET '+','.join(key+'=?'for key in values)+',updated=? WHERE id=?',(*values.values(),time.time(),identity))

 async def start(self,actor,identity):
  actor.require_write();value=await offload(self.get,actor,identity)
  require(self.enabled and not self.closed,'source_service_unavailable')
  if value['status']in{'received','needs_action','cancelled','cancelling'}:return value
  key=(actor.principal_id,identity)
  if key not in self.tasks or self.tasks[key].done():
   task=asyncio.create_task(self.run(actor,identity));self.tasks[key]=task
   task.add_done_callback(lambda t:t.exception()if not t.cancelled()else None)
  return value

 async def run(self,actor,identity):
  try:return await self._run(actor,identity)
  except LinkError as error:
   if str(error)=='download_capacity':
    # Capacity rejection must not overwrite an already-running owner of this
    # exact job. An independent queued job remains explicitly retryable.
    def rejected():
     with self.db(write=True)as db:
      row=self._row(db,actor,identity)
      if row['status'] in {'queued','interrupted'}:
       db.execute("UPDATE links SET status='interrupted',error='download_capacity' WHERE id=?",(identity,))
    await offload(rejected)
   raise

 async def _run(self,actor,identity):
  actor.require_write();require(self.enabled and not self.closed,'source_service_unavailable')
  # flock acquisition is nonblocking. Its lifetime spans every offloaded write.
  with self._lease(identity),self._slot():
   def begin():
    with self.db(write=True)as db:
     row=self._row(db,actor,identity);self._authority(actor,row['collection'],write=True)
     if row['status']in{'received','needs_action','cancelled','cancelling'}:return None
     require(row['attempts']<MAX_ATTEMPTS,'download_attempt_limit')
     db.execute("UPDATE links SET status='downloading',error=NULL,attempts=attempts+1,updated=? WHERE id=?",(time.time(),identity))
     return row
   row=await offload(begin)
   if row is None:return await offload(self.get,actor,identity)
   try:
    async with asyncio.timeout(JOB_SECONDS):await self._acquire(actor,identity,row)
   except asyncio.CancelledError:
    await offload(self._update,actor,identity,status='interrupted',error='download_interrupted');raise
   except Exception as error:
    code=str(error)if isinstance(error,(IntakeError,LinkError))else'download_interrupted'
    if code not in ACTIONS:code='download_interrupted'
    status='interrupted'if code in{'download_interrupted','source_service_unavailable','embedding_allowance_required','embedding_allowance_reserved','acquisition_stopped'}else'needs_action'
    await offload(self._update,actor,identity,status=status,error=code)
   with self.db()as db:latest=self._row(db,actor,identity)
   if latest['status']=='cancelling':await offload(self._finish_cancel,actor,identity)
   return await offload(self.get,actor,identity)

 def _recover_upload(self,actor,identity):
  with self.store._read()as db:
   found=db.execute('SELECT id FROM uploads WHERE owner=? AND request_key=?',(actor.principal_id,identity)).fetchone()
  if found:self._update(actor,identity,upload_id=found[0])
  return found[0]if found else None

 async def _acquire(self,actor,identity,row):
  row['upload_id']=await offload(self._recover_upload,actor,identity)or row['upload_id']
  current=lambda:self._current(actor,identity)
  require(current(),'acquisition_stopped')
  if row['upload_id']:
   view=await offload(self.store.get_import,actor,row['upload_id'])
   if view['upload']['status']=='sealed':
    await offload(self._update,actor,identity,status='received',error=None);return
   await offload(self.host.authorize,actor,view,operation='content')
  else:
   capabilities=await offload(self.host.capabilities,actor)
   require(capabilities.get('can_upload')is True,'source_service_unavailable')
  url=await offload(self.vault.open,row['url_cipher'],principal=actor.principal_id,provider=identity,generation=1,field='tokens')
  async with self.transport.open(url,current=current,max_bytes=self.store.limits.max_file_bytes)as response:
   meta=response.metadata
   if row['metadata']:require(json.loads(row['metadata'])==meta,'remote_changed')
   await offload(self._update,actor,identity,metadata=json.dumps(meta,sort_keys=True))
   source=await offload(self.store.ensure_connection_source,actor,collection=row['collection'],connection_id=identity,
    target_sha256=row['url_hash'],name='Shared link',kind='shared_link',current=current)
   upload=None;offset=0;hasher=hashlib.sha256()
   async for chunk in response.chunks():
    require(current(),'acquisition_stopped')
    if offset==0:
     require(media_magic(chunk),'video_file_required')
     upload=await offload(self.host.begin_remote,self.store,actor,source['id'],filename=meta['filename'],size=meta['size_bytes'],
      idempotency_key=identity,object_key=identity,source_version=digest({'url_sha256':row['url_hash'],'metadata':meta}),current=current)
     await offload(self._update,actor,identity,upload_id=upload['id'])
    hasher.update(chunk)
    # Retry replays fixed 1MiB chunks from byte zero. SourceStore verifies every
    # committed chunk, then appends the remainder; final SHA covers all bytes.
    await offload(self.store.append_chunk,actor,upload['id'],offset=offset,data=chunk,current=current)
    offset+=len(chunk)
   require(upload is not None and offset==meta['size_bytes'],'download_interrupted')
   await offload(self.store.seal_upload,actor,upload['id'],expected_sha256=hasher.hexdigest(),current=current)
   await offload(self._update,actor,identity,status='received',error=None)

 def _finish_cancel(self,actor,identity):
  self._recover_upload(actor,identity)
  with self.db()as db:row=self._row(db,actor,identity)
  if row['upload_id']:
   try:self.store.cancel_import(actor,row['upload_id'])
   except IntakeError as error:
    if str(error)!='import_finished':raise
  self._update(actor,identity,status='cancelled',error='acquisition_stopped')

 async def cancel(self,actor,identity):
  cleanup=asyncio.create_task(self._cancel(actor,identity))
  try:return await asyncio.shield(cleanup)
  except asyncio.CancelledError:await drain(cleanup);raise

 async def _cancel(self,actor,identity):
  actor.require_write();await offload(self.get,actor,identity)
  await offload(self._update,actor,identity,status='cancelling',error='acquisition_stopped')
  task=self.tasks.get((actor.principal_id,identity))
  if task and not task.done():task.cancel();await drain(task)
  async def finish():
   deadline=time.monotonic()+STOP_WAIT_SECONDS
   while True:
    try:
     with self._lease(identity):
      await offload(self._finish_cancel,actor,identity)
      return await offload(self.get,actor,identity)
    except LinkError as error:
     if str(error)!='download_capacity':raise
     if time.monotonic()>=deadline:return await offload(self.get,actor,identity)
     await asyncio.sleep(.05)
  cleanup=asyncio.create_task(finish())
  try:return await asyncio.shield(cleanup)
  except asyncio.CancelledError:await drain(cleanup);raise

 async def ready(self):
  async with self.initialization:
   require(not self.closed,'source_service_unavailable')
   if not self.initialized:
    await self.reconcile();self.initialized=True

 async def reconcile(self):
  # Startup only repairs orphan presentation; it never re-fetches or spends.
  with self.db()as db:rows=db.execute("SELECT id,owner FROM links WHERE status IN ('queued','downloading','cancelling')").fetchall()
  for row in rows:
   try:
    with self._lease(row['id']):
     def repair():
      with self.db(write=True)as db:
       fresh=self._row(db,IntakeActor(row['owner']),row['id'])
       if fresh['status']in{'queued','downloading'}:
        db.execute("UPDATE links SET status='interrupted',error='download_interrupted',updated=? WHERE id=?",(time.time(),row['id']))
     await offload(repair)
   except LinkError:continue
   # Cancelling stays fenced until an owner retries Stop; intake cancellation
   # may require the independently running media worker's reconciliation.

 async def close(self):
  self.closed=True
  tasks=[t for t in self.tasks.values()if not t.done()]
  for task in tasks:task.cancel()
  for task in tasks:await drain(task)


def member_current(state,access_state,environment,owner):
 """Cheap host company-membership fence; browser logout is not revocation."""
 try:
  state=Path(state).resolve();access_state=Path(access_state).resolve()
  if not isinstance(environment,str):return False
  company_root=access_state if environment=='demo'else access_state/'environments'/environment
  if company_root.resolve()!=state:return False
  path=access_state/'access.sqlite';info=path.lstat()
  if not stat.S_ISREG(info.st_mode)or info.st_uid!=os.geteuid()or info.st_mode&0o077:return False
  db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)
  try:
   return db.execute("SELECT 1 FROM principals p JOIN principal_environments m ON m.principal_id=p.id JOIN environments e ON e.id=m.environment WHERE p.id=? AND e.id=? AND m.role IN ('owner','operator')",(owner,environment)).fetchone()is not None
  finally:db.close()
 except(OSError,ValueError,sqlite3.Error):return False
