"""Synthetic broker streams only: publication does not execute or decode files."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import chat_files
from chat_files import ChatFiles
from native_executor import NativeScope, NativeLease
from deploy.migration.legacy.native_files import NativeFilePublisher, PublicationError

@pytest.fixture
def anyio_backend():return 'asyncio'

SCOPE=NativeScope('company-a','alice','chat-a')
LEASE=NativeLease(SCOPE.key,1,'a'*48,'/private/sockets')
BINDING=dict(company_id='company-a',owner_principal_id='alice',root_chat_id='chat-a',dataset='cafe')

class Broker:
    def __init__(self,raw=b'original\x00binary',path='report.docx'):
        self.raw,self.path=raw,path;self.calls=[];self.closed=False;self.block=None;self.manifest_change={}
    async def snapshot(self,scope,lease,paths):
        self.calls.append(('snapshot',scope,lease,paths))
        return {'snapshot_id':'b'*48,'generation':lease.generation,'total_bytes':len(self.raw),
                'files':[dict(path=self.path,size=len(self.raw),sha256=hashlib.sha256(self.raw).hexdigest(),index=0,**self.manifest_change)]}
    async def read_output(self,scope,lease,snapshot_id,index):
        self.calls.append(('read_output',scope,lease,snapshot_id,index))
        try:
            yield self.raw[:3]
            if self.block:await self.block.wait()
            yield self.raw[3:]
        finally:self.closed=True


def fixture(tmp_path,broker=None):
    store=ChatFiles(tmp_path);broker=broker or Broker()
    session=SimpleNamespace(scope=SCOPE,lease=LEASE,client=broker,stopped=False,_closing=False)
    publisher=NativeFilePublisher(store,**BINDING)
    return store,broker,session,publisher

@pytest.mark.anyio
async def test_exact_binary_owner_binding_and_idempotent_new_snapshot(tmp_path):
    store,broker,session,publisher=fixture(tmp_path)
    file=await publisher.publish(session,actor_chat_id='chat-a',path='report.docx',title='Quarterly report')
    assert store.download(file['id'],owner_principal_id='alice')[1]==broker.raw
    assert store.content(file['id'],owner_principal_id='alice')['content'] is None
    again=await publisher.publish(session,actor_chat_id='chat-a',path='report.docx',title='Retry title')
    assert again==file and len(store.list(owner_principal_id='alice'))==1
    assert ChatFiles(tmp_path).download(file['id'],owner_principal_id='alice')[1]==broker.raw
    for wrong in ({'owner_principal_id':'bob'},{'owner_principal_id':'alice','chat_id':'child'},
                  {'owner_principal_id':'alice','dataset':'warehouse'}):
        with pytest.raises(ValueError):store.download(file['id'],**wrong)
    assert 'private/sockets' not in json.dumps(file)
    assert all(call[1:3]==(SCOPE,LEASE) for call in broker.calls)
    assert broker.closed

@pytest.mark.anyio
@pytest.mark.parametrize('path',['../secret','/workspace/outputs/a.txt','~/.codex/auth.json','.codex/auth.json','dir/.env','a\\b.txt','bad\n.txt','bad\u202etxt.exe','<script>.txt'])
async def test_unsafe_paths_rejected_before_snapshot(tmp_path,path):
    store,broker,session,publisher=fixture(tmp_path)
    with pytest.raises((ValueError,PublicationError)):await publisher.publish(session,actor_chat_id='chat-a',path=path)
    assert broker.calls==[] and store.list(owner_principal_id='alice')==[]

@pytest.mark.anyio
@pytest.mark.parametrize('change',[{'actor_chat_id':'child'},{'company_id':'other'},{'owner_principal_id':'bob'},{'root_chat_id':'other'}])
async def test_parent_scope_required(tmp_path,change):
    store,broker,session,publisher=fixture(tmp_path)
    actor=change.get('actor_chat_id','chat-a')
    if 'actor_chat_id' not in change:publisher=NativeFilePublisher(store,**(BINDING|change))
    with pytest.raises(PublicationError):await publisher.publish(session,actor_chat_id=actor,path='report.docx')
    assert broker.calls==[]

@pytest.mark.anyio
async def test_cancelled_stream_leaves_no_file_and_closes_broker_stream(tmp_path):
    store,broker,session,publisher=fixture(tmp_path);broker.block=asyncio.Event()
    task=asyncio.create_task(publisher.publish(session,actor_chat_id='chat-a',path='report.docx'))
    while len(broker.calls)<2:await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert broker.closed and store.list(owner_principal_id='alice')==[]

@pytest.mark.anyio
async def test_stop_during_stream_fences_commit(tmp_path):
    store,broker,session,publisher=fixture(tmp_path);broker.block=asyncio.Event()
    task=asyncio.create_task(publisher.publish(session,actor_chat_id='chat-a',path='report.docx'))
    while len(broker.calls)<2:await asyncio.sleep(0)
    session._closing=True;broker.block.set()
    with pytest.raises(PublicationError):await task
    assert store.list(owner_principal_id='alice')==[]

@pytest.mark.anyio
@pytest.mark.parametrize('fault',['truncated','digest','generation','path','size'])
async def test_broker_mismatch_never_commits(tmp_path,fault):
    store,broker,session,publisher=fixture(tmp_path)
    snapshot=broker.snapshot
    async def altered(*args,**kwargs):
        data=await snapshot(*args,**kwargs)
        if fault=='truncated':broker.raw=broker.raw[:-1]
        elif fault=='digest':data['files'][0]['sha256']='c'*64
        elif fault=='generation':data['generation']=2
        elif fault=='path':data['files'][0]['path']='other.docx'
        else:data['files'][0]['size']=17*1024**2;data['total_bytes']=17*1024**2
        return data
    broker.snapshot=altered
    with pytest.raises(PublicationError):await publisher.publish(session,actor_chat_id='chat-a',path='report.docx')
    assert store.list(owner_principal_id='alice')==[]

@pytest.mark.anyio
async def test_company_quota_and_retry_at_capacity(tmp_path,monkeypatch):
    store,broker,session,publisher=fixture(tmp_path)
    monkeypatch.setattr(chat_files,'MAX_COMPANY_BYTES',len(broker.raw))
    file=await publisher.publish(session,actor_chat_id='chat-a',path='report.docx')
    assert (await publisher.publish(session,actor_chat_id='chat-a',path='report.docx'))==file
    broker.raw=b'changed bytes'
    with pytest.raises(ValueError,match='allowance'):await publisher.publish(session,actor_chat_id='chat-a',path='report.docx')
    assert len(store.list(owner_principal_id='alice'))==1

@pytest.mark.anyio
@pytest.mark.parametrize('name,raw,preview,inline',[
    ('report.html',b'<script>window.bad=true</script>','text',None),
    ('drawing.svg',b'<svg onload="bad()"></svg>','text',None),
    ('disguised.pdf',b'<html>not a PDF</html>',None,None),
    ('notes.md',b'# Report\n<script>bad()</script>','markdown',None),
    ('photo.png',b'\x89PNG\r\n\x1a\n' + b'\x00'*24,None,'image'),
    ('document.pdf',b'%PDF-1.7\noriginal bytes\n%%EOF',None,None),
    ('sheet.csv',b'Name,Value\nOriginal,=1+2\n','table',None),
])
async def test_preview_allowlist_preserves_exact_original(tmp_path,name,raw,preview,inline):
    store,broker,session,publisher=fixture(tmp_path,Broker(raw,name))
    file=await publisher.publish(session,actor_chat_id='chat-a',path=name)
    assert file['preview_format']==preview and file.get('inline_preview_type')==inline
    assert (store.content(file['id'],owner_principal_id='alice')['content'] is not None)==bool(preview)
    assert ChatFiles(tmp_path).download(file['id'],owner_principal_id='alice')[1]==raw
    if name.endswith(('.html','.svg')):assert file['media_type']=='application/octet-stream'

@pytest.mark.anyio
async def test_bounded_wait_closes_stream(tmp_path):
    store,broker,session,publisher=fixture(tmp_path);broker.block=asyncio.Event();publisher.timeout_seconds=.02
    with pytest.raises(PublicationError,match='publication_timeout'):
        await publisher.publish(session,actor_chat_id='chat-a',path='report.docx')
    assert broker.closed and store.list(owner_principal_id='alice')==[]


def import_bytes(store,raw=b'report',identity='a'*64,**changes):
    values=dict(name='report.docx',size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),
        publication_key=identity,provenance={'kind':'native_output'},owner_principal_id='alice',chat_id='chat-a',dataset='cafe')
    return store.import_native(io.BytesIO(raw),**(values|changes))


def test_store_rechecks_hash_and_truncation_transactionally(tmp_path):
    store=ChatFiles(tmp_path)
    for change in ({'sha256':'a'*64},{'size_bytes':20},{'size_bytes':1}):
        with pytest.raises(ValueError,match='checksum'):import_bytes(store,**change)
        assert store.list(owner_principal_id='alice')==[]


def test_concurrent_company_quota_is_enforced(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store=ChatFiles(tmp_path);monkeypatch.setattr(chat_files,'MAX_COMPANY_BYTES',6)
    def attempt(number):
        try:return ('saved',import_bytes(store,identity=hashlib.sha256(str(number).encode()).hexdigest()))
        except chat_files.sqlite3.OperationalError as error:
            assert error.sqlite_errorcode in {chat_files.sqlite3.SQLITE_BUSY,chat_files.sqlite3.SQLITE_LOCKED}
            return ('busy',None)
        except ValueError as error:
            assert 'company file allowance is full' in str(error)
            return ('quota',None)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(attempt,range(4)))
    saved=sum(status=='saved' for status,_ in results)
    assert saved<=1 and len(store.list(owner_principal_id='alice'))==saved
    # Immediate busy admission deliberately leaves the event loop available for
    # Stop. Opening readers can also make the winning writer's commit return
    # busy, so all contenders may roll back; a quiescent retry must then succeed.
    if saved==0:
        assert all(status=='busy' for status,_ in results)
        import_bytes(store,identity='b'*64)
    with pytest.raises(ValueError,match='company file allowance is full'):
        import_bytes(store,identity='c'*64)
    assert len(store.list(owner_principal_id='alice'))==1


def test_empty_binary_and_large_text_fallback(tmp_path):
    store=ChatFiles(tmp_path)
    empty=import_bytes(store,b'',name='empty.txt')
    assert store.content(empty['id'],owner_principal_id='alice')['content']==''
    raw=b'x'*(chat_files.MAX_FILE_BYTES+1)
    large=import_bytes(store,raw,identity='b'*64,name='large.txt')
    assert large['copyable'] is False and large['preview_format'] is None
    assert store.content(large['id'],owner_principal_id='alice')['content'] is None
    assert store.download(large['id'],owner_principal_id='alice')[1]==raw
