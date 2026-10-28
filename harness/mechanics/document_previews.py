"""Private derived pages over exact ChatFiles originals; decoding stays in gVisor."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager,contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import time

from chat_files import ChatFiles
import document_preview_contract as contract
from source_media import MediaClient,MediaScope,MediaLease,_drain
from workspace_identity import workspace_scope

class DocumentPreviewError(ValueError):pass

def require(value,code='document_preview_unavailable'):
    if not value:raise DocumentPreviewError(code)

def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

async def offload(function,*args,**kwargs):
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:return await asyncio.shield(task)
    except BaseException:
        try:await _drain(task)
        except BaseException:pass
        raise

class DocumentPreviews:
    def __init__(self,files,*,client=None,enabled=False,cache_bytes=128*1024**2):
        if not isinstance(files,ChatFiles):raise TypeError('Use the owned ChatFiles store.')
        self.files=files;self.state=files.path.parent;self.company=workspace_scope(self.state)
        self.client=client or MediaClient();self.enabled=enabled is True
        require(type(cache_bytes) is int and 1<=cache_bytes<=512*1024**2)
        self.cache_bytes=cache_bytes;self.root=self.state/'document-previews';self.database=self.root/'cache.sqlite'
        self.root.mkdir(mode=0o700,exist_ok=True)
        info=self.root.lstat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077)
        fd=os.open(self.database,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:require(stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_nlink==1)
        finally:os.close(fd)
        with self.db(write=True) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY,owner TEXT NOT NULL,file_id TEXT NOT NULL,original_sha TEXT NOT NULL,
                generation INTEGER NOT NULL,status TEXT NOT NULL,manifest TEXT,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pages (key TEXT NOT NULL,name TEXT NOT NULL,sha TEXT NOT NULL,
                body BLOB NOT NULL,PRIMARY KEY(key,name));''')

    @contextmanager
    def db(self,*,write=False):
        fd=os.open(self.database,os.O_RDONLY|os.O_NOFOLLOW)
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid==os.getuid() and before.st_nlink==1 and not before.st_mode&0o077)
        db=sqlite3.connect(self.database,timeout=5);db.row_factory=sqlite3.Row
        try:
            if write:
                db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA max_page_count=135168');db.execute('BEGIN IMMEDIATE')
            else:db.execute('BEGIN')
            yield db
            after=self.database.lstat();require((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino))
            if write:db.commit()
        except BaseException:db.rollback();raise
        finally:db.close();os.close(fd)

    def original(self,file_id,owner):
        fd=os.open(self.files.path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            before=os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_uid==os.getuid() and before.st_nlink==1 and not before.st_mode&0o077)
            record,raw=self.files.download(file_id,owner_principal_id=owner)
            after=self.files.path.lstat()
            require((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'document_original_changed')
        finally:os.close(fd)
        format=Path(record['name']).suffix.lower().removeprefix('.')
        mimes={'docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','pdf':'application/pdf','xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
        require(record.get('kind')=='document' and format in mimes and (record.get('representation')=='native_original' or (not record.get('representation') and record.get('media_type')==mimes[format])),'document_preview_format')
        require(len(raw)==record['size_bytes'] and 0<len(raw)<=contract.MAX_ORIGINAL
            and digest(raw)==record['sha256'],'document_original_changed')
        key=digest(canonical([self.company,owner,file_id,record['sha256'],contract.IMAGE,contract.RECIPE]))
        return record,raw,format,key

    def row(self,key):
        with self.db() as db:return db.execute('SELECT * FROM jobs WHERE key=?',(key,)).fetchone()

    def _status(self,key,status):
        with self.db(write=True) as db:db.execute('UPDATE jobs SET status=?,updated=? WHERE key=?',(status,time.time(),key))

    def content(self,file_id,*,owner):
        record,raw,format,key=self.original(file_id,owner);row=self.row(key)
        file={**record,'page_preview_available':self.enabled,'page_preview_status':row['status'] if row else 'available' if self.enabled else 'unavailable'}
        result={'file':file,'content':None,'pages':[],'preview_status':file['page_preview_status']}
        if row and row['status']=='ready':
            manifest=json.loads(row['manifest'])
            text=self._body(key,'text.txt').decode('utf-8');copyable=not manifest['text']['truncated']
            copy_source='extracted_text'
            if record.get('representation')!='native_original':
                source=self.files.content(file_id,owner_principal_id=owner)['content']
                require(isinstance(source,str) and digest(source.encode())==record.get('source_sha256'),'document_original_changed')
                require(self.original(file_id,owner)[3]==key,'document_original_changed')
                text=source;copyable=record.get('copyable') is True;copy_source='original_source'
            result.update(content=text,pages=[{'page':p['page'],'width':p['width'],'height':p['height'],
                'url':f'/api/files/{file_id}/pages/{p["page"]}'} for p in manifest['pages']],preview_status='ready')
            file.update(inline_preview_type='pages',preview_format='pages',copyable=copyable,copy_source=copy_source,
                page_preview_status='ready',page_count=manifest['page_count'],preview_renderer='Poppler' if format=='pdf' else 'LibreOffice and Poppler')
        return result

    def _body(self,key,name):
        with self.db() as db:row=db.execute('SELECT sha,body FROM pages WHERE key=? AND name=?',(key,name)).fetchone()
        require(row is not None and digest(row['body'])==row['sha'],'document_preview_integrity')
        return row['body']

    def page(self,file_id,page,*,owner):
        record,raw,format,key=self.original(file_id,owner);row=self.row(key)
        require(row is not None and row['status']=='ready' and type(page) is int,'document_page_unavailable')
        manifest=json.loads(row['manifest']);require(1<=page<=manifest['page_count'],'document_page_unavailable')
        item=manifest['pages'][page-1];body=self._body(key,item['path'])
        require(len(body)==item['size_bytes'] and digest(body)==item['sha256'],'document_preview_integrity')
        return body

    @asynccontextmanager
    async def lock(self,key):
        fd=os.open(self.root/(key+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd);require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid())
            async with asyncio.timeout(170):
                while True:
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:await asyncio.sleep(.05)
            yield
        finally:os.close(fd)

    def _begin(self,key,record,owner,generation):
        with self.db(write=True) as db:
            if db.execute('SELECT count(*) FROM jobs').fetchone()[0]>=1000 and not db.execute('SELECT 1 FROM jobs WHERE key=?',(key,)).fetchone():
                raise DocumentPreviewError('document_preview_allowance')
            db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,NULL,?)',
                (key,owner,record['id'],record['sha256'],generation,'preparing',time.time()))

    def _save(self,key,record,owner,manifest,contents):
        current,_,_,current_key=self.original(record['id'],owner)
        require(current_key==key and current['sha256']==manifest['original_sha256'],'document_original_changed')
        with self.db(write=True) as db:
            used=db.execute('SELECT coalesce(sum(length(body)),0) FROM pages WHERE key!=?',(key,)).fetchone()[0]
            require(used+sum(map(len,contents.values()))<=self.cache_bytes,'document_preview_allowance')
            db.execute('DELETE FROM pages WHERE key=?',(key,))
            for name,raw in contents.items():db.execute('INSERT INTO pages VALUES(?,?,?,?)',(key,name,digest(raw),raw))
            db.execute('UPDATE jobs SET status=?,manifest=?,updated=? WHERE key=?',('prepared',json.dumps(manifest),time.time(),key))

    async def prepare(self,file_id,*,owner):
        require(self.enabled,'document_preview_unavailable')
        record,raw,format,key=await offload(self.original,file_id,owner)
        async with self.lock(key):
            existing=await offload(self.row,key)
            if existing and existing['status']=='ready':return await offload(self.content,file_id,owner=owner)
            scope=MediaScope(self.company,owner,'asset-'+file_id[5:],'job-'+key[:32])
            generation=existing['generation'] if existing else 1
            lease=None;success=False
            try:
                if existing:lease=await self.client.recover(scope,generation)
                if lease:
                    status=await self.client.status(scope,lease)
                    if existing['status'] in {'cleanup_pending','prepared'} or status['status'] not in {'starting','running','succeeded'}:
                        stopped=await self.client.stop(scope,lease)
                        require(stopped['cleanup_confirmed'] is True,'document_cleanup_pending')
                        await self.client.release(scope,lease)
                        lease=None;generation+=1
                await offload(self._begin,key,record,owner,generation)
                if lease is None:
                    # The private stream only supplies a descriptor; broker verifies UID/hash/size.
                    with tempfile.NamedTemporaryFile(dir=self.root,delete=False) as temporary:
                        temporary.write(raw);temporary.flush();os.fsync(temporary.fileno())
                        os.chmod(temporary.name,0o400)
                        readonly=os.open(temporary.name,os.O_RDONLY|os.O_NOFOLLOW)
                        try:
                            lease=await self.client.preview(scope,generation=generation,collection=record['dataset'],
                                original_sha256=record['sha256'],original_bytes=len(raw),expires_at=time.time()+60,
                                original_fd=readonly,document={'purpose':contract.PURPOSE,'file_id':file_id,'format':format})
                        finally:os.close(readonly);os.unlink(temporary.name)
                async with asyncio.timeout(160):
                    refreshed=time.monotonic()
                    while True:
                        status=await self.client.status(scope,lease)
                        if status['status'] not in {'starting','running'}:break
                        if time.monotonic()-refreshed>15:
                            try:await self.client.heartbeat(scope,lease,expires_at=time.time()+60)
                            except Exception:
                                status=await self.client.status(scope,lease)
                                if status['status'] in {'starting','running'}:raise
                            refreshed=time.monotonic()
                        await asyncio.sleep(.05)
                    require(status['status']=='succeeded' and status['cleanup_confirmed'] is True,'document_conversion_failed')
                    result=status['result'];request=contract.DocumentRequest(contract.PURPOSE,file_id,format)
                    require(result.get('scope')==asdict(scope) and result.get('generation')==generation
                        and result.get('profile')==contract.PROFILE and result.get('image_digest')==contract.IMAGE)
                    manifest=contract.validate_manifest(result['manifest'],result['files'],request=request,
                        original_sha256=record['sha256'],original_bytes=len(raw))
                    contents={};total=0
                    for entry in result['files']:
                        chunks=[];received=0
                        async for chunk in self.client.read_output(scope,lease,entry['index']):
                            received+=len(chunk);total+=len(chunk)
                            require(received<=entry['size_bytes'] and total<=contract.MAX_OUTPUT,'document_preview_integrity')
                            chunks.append(chunk)
                        body=b''.join(chunks)
                        require(len(body)==entry['size_bytes'] and digest(body)==entry['sha256'],'document_preview_integrity')
                        contents[entry['path']]=body
                    require(json.loads(contents.pop('manifest.json'))==manifest,'document_preview_integrity')
                    for page in manifest['pages']:
                        header=contents[page['path']][:24]
                        require(len(header)==24 and header[:8]==b'\x89PNG\r\n\x1a\n' and header[12:16]==b'IHDR'
                            and struct.unpack('>II',header[16:24])==(page['width'],page['height']),'document_preview_integrity')
                    contents['text.txt'].decode('utf-8')
                    await offload(self._save,key,record,owner,manifest,contents);success=True
            finally:
                async def cleanup():
                    nonlocal lease
                    if lease is None:lease=await self.client.recover(scope,generation)
                    if lease:
                        stopped=await self.client.stop(scope,lease)
                        require(stopped['cleanup_confirmed'] is True,'document_cleanup_pending')
                        await self.client.release(scope,lease)
                    await offload(self._status,key,'ready' if success else 'failed')
                try:await _drain(asyncio.create_task(cleanup()))
                except BaseException:
                    await offload(self._status,key,'cleanup_pending');raise
            return await offload(self.content,file_id,owner=owner)
