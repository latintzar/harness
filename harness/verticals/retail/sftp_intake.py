"""Durable, bounded stored-recording acquisition; preparation stays in SourceStore."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from source_intake import SourceStore,IntakeActor,IntakeError
from sftp_transport import SFTPConfig,SFTPCredentials,SFTPError,RemoteFile,CHUNK_BYTES,connect,digest,require
from workspace_identity import workspace_scope

async def drain(task):
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

async def callback(fn,*args,**kwargs):
    value=await fn(*args,**kwargs)if inspect.iscoroutinefunction(fn)else await offload(fn,*args,**kwargs)
    return await value if inspect.isawaitable(value)else value

class Journal:
    def __init__(self,store):
        self.store=store;self.path=store.root/'sftp.sqlite3'
        os.close(store._open(self.path,os.O_CREAT|os.O_RDWR))
        with self.db()as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS bindings(
                source TEXT PRIMARY KEY,owner TEXT NOT NULL,config TEXT NOT NULL,cursor TEXT NOT NULL,
                sweep INTEGER NOT NULL,status TEXT NOT NULL,last_check REAL,last_import REAL,next_check REAL NOT NULL,error TEXT);
                CREATE TABLE IF NOT EXISTS observations(
                source TEXT NOT NULL,name TEXT NOT NULL,version TEXT NOT NULL,record TEXT NOT NULL,
                first_seen REAL NOT NULL,last_seen REAL NOT NULL,observations INTEGER NOT NULL,sweep INTEGER NOT NULL,
                state TEXT NOT NULL,attempt TEXT NOT NULL,upload_id TEXT,sha256 TEXT,
                PRIMARY KEY(source,name));''')
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def bind(self,config):
        with self.db()as db:
            old=db.execute('SELECT * FROM bindings WHERE source=?',(config.source_id,)).fetchone()
            if old:
                require(old['owner']==config.owner_id,'sftp_owner_mismatch')
                original=SFTPConfig(**json.loads(old['config']))
                require(original.target_sha256==config.target_sha256 and original.collection==config.collection
                    and original.company_id==config.company_id,'sftp_source_target_changed')
                require(config.generation>=original.generation,'sftp_stale_configuration')
                if config.sha256!=original.sha256:
                    require(config.generation>original.generation,'sftp_stale_configuration')
                    db.execute("UPDATE bindings SET config=?,status='waiting',next_check=0 WHERE source=?",(json.dumps(asdict(config)),config.source_id))
            else:db.execute("INSERT INTO bindings VALUES(?,?,?,'',1,'waiting',NULL,NULL,0,NULL)",(config.source_id,config.owner_id,json.dumps(asdict(config))))
    def status(self,config):
        with self.db()as db:
            row=db.execute('SELECT * FROM bindings WHERE source=? AND owner=?',(config.source_id,config.owner_id)).fetchone()
            if row is None:return {'source_id':config.source_id,'status':'configured','last_check':None,'last_import':None,'next_check':0,'error':None}
            return {k:row[k]for k in ('status','last_check','last_import','next_check','error')}|{'source_id':config.source_id}
    def scan(self,config,page,cursor,now):
        with self.db()as db:
            binding=db.execute('SELECT * FROM bindings WHERE source=?',(config.source_id,)).fetchone();sweep=binding['sweep']
            for item in page:
                old=db.execute('SELECT * FROM observations WHERE source=? AND name=?',(config.source_id,item.name)).fetchone()
                if old is None:
                    require(db.execute('SELECT count(*) FROM observations').fetchone()[0]<10000,'sftp_observation_limit')
                    db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,1,?,'observing',?,NULL,NULL)",
                        (config.source_id,item.name,item.version,json.dumps(asdict(item)),now,now,sweep,secrets.token_hex(16)))
                elif old['version']!=item.version or old['state']=='missing':
                    # Keep a prior uploading occurrence reachable until it is
                    # explicitly retired by acquisition below.
                    db.execute("UPDATE observations SET version=?,record=?,first_seen=?,last_seen=?,observations=1,sweep=?,state='changed' WHERE source=? AND name=?",
                        (item.version,json.dumps(asdict(item)),now,now,sweep,config.source_id,item.name))
                else:db.execute('UPDATE observations SET last_seen=?,observations=observations+1,sweep=? WHERE source=? AND name=?',(now,sweep,config.source_id,item.name))
            if not cursor:
                db.execute("UPDATE observations SET state='missing' WHERE source=? AND sweep!=? AND state!='sealed'",(config.source_id,sweep))
            db.execute("UPDATE bindings SET cursor=?,sweep=?,last_check=?,error=NULL WHERE source=?",(cursor,sweep if cursor else sweep+1,now,config.source_id))
    def rows(self,config):
        with self.db()as db:return [dict(row)for row in db.execute("SELECT * FROM observations WHERE source=? AND state NOT IN ('sealed','blocked') AND(state!='missing' OR upload_id IS NOT NULL) ORDER BY first_seen,name LIMIT 64",(config.source_id,))]
    def cursor(self,config):
        with self.db()as db:return db.execute('SELECT cursor FROM bindings WHERE source=?',(config.source_id,)).fetchone()[0]
    def block(self,config,name):
        with self.db()as db:db.execute("UPDATE observations SET state='blocked' WHERE source=? AND name=?",(config.source_id,name))
    def missing(self,config,name):
        with self.db()as db:db.execute("UPDATE observations SET upload_id=NULL WHERE source=? AND name=?",(config.source_id,name))
    def reset(self,config,name,now):
        with self.db()as db:db.execute("UPDATE observations SET state='observing',attempt=?,upload_id=NULL,sha256=NULL,first_seen=?,observations=1 WHERE source=? AND name=?",(secrets.token_hex(16),now,config.source_id,name))
    def upload(self,config,name,upload):
        with self.db()as db:db.execute("UPDATE observations SET state='uploading',upload_id=? WHERE source=? AND name=?",(upload,config.source_id,name))
    def sealed(self,config,name,upload,now):
        with self.db()as db:
            db.execute("UPDATE observations SET state='sealed',sha256=? WHERE source=? AND name=?",(upload['sha256'],config.source_id,name))
            db.execute('UPDATE bindings SET last_import=? WHERE source=?',(now,config.source_id))
    def finish(self,config,now,error=None):
        with self.db()as db:
            cursor=db.execute('SELECT cursor FROM bindings WHERE source=?',(config.source_id,)).fetchone()[0]
            pending=db.execute("SELECT count(*) FROM observations WHERE source=? AND state NOT IN ('sealed','missing')",(config.source_id,)).fetchone()[0]
            if error is None and db.execute("SELECT 1 FROM observations WHERE source=? AND state='blocked' LIMIT 1",(config.source_id,)).fetchone():error='sftp_file_limit'
            status='needs_attention'if error else'complete'if config.mode=='one_time'and not cursor and not pending else'waiting'
            db.execute('UPDATE bindings SET status=?,next_check=?,error=? WHERE source=?',(status,now+config.poll_seconds,error,config.source_id))

class SFTPAcquirer:
    """One bounded pull per scheduler tick, with private durable scan/acquire state."""
    def __init__(self,store:SourceStore,*,connector=connect,clock=time.time,max_active=2,begin_upload=None):
        require(isinstance(store,SourceStore)and type(max_active)is int and 1<=max_active<=2)
        self.store,self.connector,self.clock=store,connector,clock
        self.company=workspace_scope(store.root.parent);self.journal=Journal(store)
        self.max_active=max_active;self.active={};self.closed=False
        self.begin_upload=begin_upload or store.begin_upload
        # Keep WAL open while this reader is alive. Last-connection teardown can
        # briefly return SQLITE_BUSY to the fresh zero-wait authority probe.
        # This idle read-only connection holds no transaction or authority cache.
        anchor=sqlite3.connect(store.database.resolve().as_uri()+'?mode=ro',uri=True,check_same_thread=False)
        try:anchor.execute('SELECT count(*) FROM sources').fetchone()
        except BaseException:
            anchor.close();raise
        self._source_anchor=anchor
    def _owned(self,config,actor):
        require(isinstance(config,SFTPConfig)and isinstance(actor,IntakeActor)and config.company_id==self.company
            and config.owner_id==actor.principal_id,'sftp_owner_mismatch')
        source=self.store.get_source(actor,config.source_id)
        require(source['kind']=='sftp'and source['collection']==config.collection,'sftp_source_mismatch')
    async def status(self,config,*,actor):
        await offload(self._owned,config,actor)
        return await offload(self.journal.status,config)
    async def pull(self,config,*,actor,credentials,current,cancelled,max_seconds=120):
        require(isinstance(cancelled,asyncio.Event)and callable(credentials)and callable(current))
        require(type(max_seconds)in(int,float)and 0<max_seconds<=180)
        require(not self.closed and len(self.active)<self.max_active,'sftp_capacity');actor.require_write()
        key=config.source_id
        require(key not in self.active,'sftp_busy')
        stop=asyncio.Event()
        def alive():
            try:return not self.closed and not stop.is_set()and not cancelled.is_set()and current(config)is True
            except Exception:return False
        require(alive(),'sftp_authority_changed')
        task=asyncio.create_task(self._pull(config,actor,credentials,alive,max_seconds));self.active[key]=(task,stop)
        async def watch():
            while not task.done():
                if not alive():stop.set();task.cancel();return
                await asyncio.sleep(.1)
        watcher=asyncio.create_task(watch())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set();task.cancel()
            try:await drain(task)
            except Exception:pass
            raise
        finally:watcher.cancel();await drain(watcher);self.active.pop(key,None)
    async def _pull(self,config,actor,credentials,current,seconds):
        await offload(self._owned,config,actor)
        lock=self.store._open(self.store.root/('sftp-'+digest(config.source_id)+'.lock'),os.O_CREAT|os.O_WRONLY)
        error=None;performed=False;slot=None
        try:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise SFTPError('sftp_busy')from None
            require(current(),'sftp_authority_changed');await offload(self.journal.bind,config)
            status=await offload(self.journal.status,config)
            if status['status']=='complete'or status['next_check']>self.clock():return status
            # Fixed company-wide slots also bound independent worker processes.
            for number in range(self.max_active):
                candidate=self.store._open(self.store.root/('sftp-slot-'+str(number)+'.lock'),os.O_CREAT|os.O_WRONLY)
                try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB);slot=candidate;break
                except BlockingIOError:os.close(candidate)
            require(slot is not None,'sftp_capacity')
            performed=True
            async with asyncio.timeout(seconds):
                bundle=await callback(credentials,config)
                require(isinstance(bundle,SFTPCredentials)and bundle.config_sha256==config.sha256,'sftp_credentials_changed')
                require(current(),'sftp_authority_changed')
                async with self.connector(config,bundle,current)as reader:
                    cursor=await offload(self.journal.cursor,config)
                    page,cursor=await reader.list_page(cursor)
                    require(current(),'sftp_authority_changed');await offload(self.journal.scan,config,page,cursor,self.clock())
                    acquired=0;blocked=False
                    for row in await offload(self.journal.rows,config):
                        require(current(),'sftp_authority_changed')
                        if row['state']in {'changed','missing'}:
                            if row['upload_id']:
                                previous=await offload(self.store.get_upload,actor,row['upload_id'])
                                if previous['status']=='uploading':await offload(self.store.cancel_upload,actor,row['upload_id'])
                            if row['state']=='changed':await offload(self.journal.reset,config,row['name'],self.clock())
                            else:await offload(self.journal.missing,config,row['name'])
                            continue
                        if row['observations']<2 or self.clock()-row['first_seen']<config.stable_seconds:continue
                        item=RemoteFile(**json.loads(row['record']))
                        if item.size>self.store.limits.max_file_bytes:
                            await offload(self.journal.block,config,item.name);blocked=True;continue
                        try:await self._acquire(config,actor,row,item,reader,current)
                        except (SFTPError,IntakeError)as exc:
                            if str(exc)in {'sftp_version_changed','checksum_mismatch','sftp_short_read'}:
                                if row['upload_id']:
                                    upload=await offload(self.store.get_upload,actor,row['upload_id'])
                                    if upload['status']=='uploading':await offload(self.store.cancel_upload,actor,row['upload_id'])
                                await offload(self.journal.reset,config,row['name'],self.clock())
                            raise
                        acquired+=1
                        if acquired==4:break
                    if blocked:raise SFTPError('sftp_file_limit')
        except asyncio.CancelledError:error='sftp_interrupted';raise
        except (SFTPError,IntakeError)as exc:error=str(exc);raise
        except TimeoutError:error='sftp_timeout';raise SFTPError(error)from None
        except Exception:error='sftp_acquisition_failed';raise SFTPError(error)from None
        finally:
            # Accepted store operations drain before this lease is released.
            try:
                if performed:await drain(asyncio.create_task(offload(self.journal.finish,config,self.clock(),error)))
            finally:
                if slot is not None:os.close(slot)
                os.close(lock)
        return await offload(self.journal.status,config)
    async def _acquire(self,config,actor,row,item,reader,current):
        version=digest([item.version,row['attempt']]);request=digest([config.source_id,item.name,version])
        upload=await callback(self.begin_upload,actor,config.source_id,filename=item.name,size=item.size,
            idempotency_key=request,object_key=item.name,source_version=version,current=current)
        row['upload_id']=upload['id'];await offload(self.journal.upload,config,item.name,upload['id'])
        if upload['status']=='sealing':upload=await offload(self.store.seal_upload,actor,upload['id'],expected_sha256=upload['sha256'],current=current)
        if upload['status']=='sealed':await offload(self.journal.sealed,config,item.name,upload,self.clock());return
        if upload['status']=='uploading'and upload['expires']<=self.store.clock():
            await offload(self.store.cancel_upload,actor,upload['id'])
            await offload(self.journal.reset,config,item.name,self.clock());raise SFTPError('sftp_transfer_restart_required')
        if upload['status']!='uploading':
            await offload(self.journal.reset,config,item.name,self.clock());raise SFTPError('sftp_transfer_restart_required')
        async with reader.open(item)as handle:
            offset=upload['committed_bytes']
            while offset<item.size:
                data=await reader.read(handle,offset,min(CHUNK_BYTES,self.store.limits.max_chunk_bytes,item.size-offset))
                upload=await offload(self.store.append_chunk,actor,upload['id'],offset=offset,data=data,current=current)
                offset=upload['committed_bytes']
            # An SFTP stat tuple is not an immutable version. Verify the entire
            # assembled file against a fresh read, including a resumed prefix.
            checksum=hashlib.sha256();offset=0
            while offset<item.size:
                data=await reader.read(handle,offset,min(CHUNK_BYTES,item.size-offset));checksum.update(data);offset+=len(data)
        require(current(),'sftp_authority_changed')
        upload=await offload(self.store.seal_upload,actor,upload['id'],expected_sha256=checksum.hexdigest(),current=current)
        await offload(self.journal.sealed,config,item.name,upload,self.clock())
    async def stop(self,config,*,actor):
        actor.require_write();await offload(self._owned,config,actor)
        value=self.active.get(config.source_id)
        if value:
            task,event=value;event.set();task.cancel()
            try:await drain(task)
            except Exception:pass
        return await self.status(config,actor=actor)
    async def close(self):
        self.closed=True
        for task,event in tuple(self.active.values()):event.set();task.cancel()
        for task,_ in tuple(self.active.values()):
            try:await drain(task)
            except Exception:pass
        # All accepted source operations and SSH cleanup finish before closing.
        anchor,self._source_anchor=self._source_anchor,None
        if anchor is not None:anchor.close()
