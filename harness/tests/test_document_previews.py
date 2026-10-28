import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
import pytest
from chat_files import ChatFiles
from document_previews import DocumentPreviews,DocumentPreviewError
from source_media import MediaBroker,MediaServer,MediaClient
from test_document_preview_broker import DocumentBackend

@pytest.fixture
def anyio_backend():return 'asyncio'

@asynccontextmanager
async def setup(tmp_path,*,enabled=True,cache_bytes=128*1024**2):
    state=tmp_path/'state';state.mkdir(mode=0o700,parents=True)
    files=ChatFiles(state);body=b'opaque-native-docx'
    record=files.import_native(io.BytesIO(body),name='Private report.docx',size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),publication_key='a'*64,provenance={},
        owner_principal_id='alice',chat_id='root',dataset='company')
    backend=DocumentBackend(tmp_path/'volumes')
    broker=MediaBroker(tmp_path/'broker',backend,app_uid=os.getuid(),app_gid=os.getgid(),worker_uid=os.getuid(),document_previews=True)
    server=MediaServer(broker,tmp_path/'socket/control.sock');await server.start()
    client=MediaClient(server.path,server_uid=os.getuid())
    previews=DocumentPreviews(files,client=client,enabled=enabled,cache_bytes=cache_bytes)
    try:yield files,record,backend,broker,previews
    finally:await server.close()

@pytest.mark.anyio
async def test_full_fd_snapshot_cache_preserves_original_and_scope(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        before=files.download(record['id'],owner_principal_id='alice')
        result=await previews.prepare(record['id'],owner='alice')
        assert result['preview_status']=='ready' and result['file']['inline_preview_type']=='pages'
        assert result['content']=='Fixture document' and len(result['pages'])==1
        assert previews.page(record['id'],1,owner='alice').startswith(b'\x89PNG')
        assert files.download(record['id'],owner_principal_id='alice')==before
        assert result['file']['sha256']==before[0]['sha256']
        assert (await previews.prepare(record['id'],owner='alice'))==result and backend.start_count==1
        with pytest.raises(ValueError):previews.content(record['id'],owner='bob')
        with pytest.raises(ValueError):previews.page(record['id'],1,owner='bob')
        assert not backend.alive and broker.db.execute('SELECT status FROM jobs').fetchone()[0]=='released'

@pytest.mark.anyio
async def test_default_closed_and_owner_change_during_worker(tmp_path):
    async with setup(tmp_path,enabled=False) as (files,record,backend,broker,previews):
        with pytest.raises(DocumentPreviewError,match='unavailable'):await previews.prepare(record['id'],owner='alice')
        assert backend.start_count==0
        previews.enabled=True;backend.gate.clear();task=asyncio.create_task(previews.prepare(record['id'],owner='alice'))
        await backend.started.wait()
        with files.connect() as db:db.execute('UPDATE files SET owner=? WHERE id=?',('bob',record['id']))
        backend.gate.set()
        with pytest.raises(ValueError):await task
        with previews.db() as db:assert db.execute('SELECT count(*) FROM pages').fetchone()[0]==0
        assert not backend.alive

@pytest.mark.anyio
async def test_changed_or_deleted_source_and_corrupt_cache_are_unavailable(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        await previews.prepare(record['id'],owner='alice')
        with previews.db(write=True) as db:db.execute('UPDATE pages SET body=? WHERE name=?',(b'changed','page-0001.png'))
        with pytest.raises(DocumentPreviewError,match='integrity'):previews.page(record['id'],1,owner='alice')
        with files.connect() as db:db.execute('UPDATE files SET content=? WHERE id=?',(b'changed',record['id']))
        with pytest.raises(DocumentPreviewError,match='original_changed'):previews.content(record['id'],owner='alice')
        with files.connect() as db:db.execute('DELETE FROM files WHERE id=?',(record['id'],))
        with pytest.raises(ValueError):previews.page(record['id'],1,owner='alice')

@pytest.mark.anyio
async def test_duplicate_requests_share_one_worker_and_cancel_cleans(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        backend.gate.clear();first=asyncio.create_task(previews.prepare(record['id'],owner='alice'))
        await backend.started.wait();second=asyncio.create_task(previews.prepare(record['id'],owner='alice'))
        backend.gate.set();a,b=await asyncio.gather(first,second);assert a==b and backend.start_count==1
    async with setup(tmp_path/'cancel') as (files,record,backend,broker,previews):
        backend.gate.clear();task=asyncio.create_task(previews.prepare(record['id'],owner='alice'))
        await backend.started.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert not backend.alive and broker.db.execute('SELECT status FROM jobs').fetchone()[0]=='released'
        assert previews.content(record['id'],owner='alice')['preview_status']=='failed'

@pytest.mark.anyio
async def test_quota_denies_preview_without_changing_download(tmp_path):
    async with setup(tmp_path,cache_bytes=1) as (files,record,backend,broker,previews):
        with pytest.raises(DocumentPreviewError,match='allowance'):await previews.prepare(record['id'],owner='alice')
        assert files.download(record['id'],owner_principal_id='alice')[1]==b'opaque-native-docx'
        with previews.db() as db:assert db.execute('SELECT count(*) FROM pages').fetchone()[0]==0
        assert not backend.alive

@pytest.mark.anyio
async def test_lost_admission_reply_reconciles_and_explicit_retry_uses_new_generation(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        original=previews.client.preview
        async def lost(*args,**kwargs):
            await original(*args,**kwargs);raise RuntimeError('synthetic reply lost')
        previews.client.preview=lost
        with pytest.raises(RuntimeError,match='reply lost'):await previews.prepare(record['id'],owner='alice')
        assert not backend.alive and broker.db.execute('SELECT status FROM jobs').fetchone()[0]=='released'
        previews.client.preview=original
        result=await previews.prepare(record['id'],owner='alice');assert result['preview_status']=='ready'
        assert broker.db.execute('SELECT generation FROM jobs').fetchone()[0]==2

@pytest.mark.anyio
async def test_failed_stop_never_exposes_prepared_pages_or_advances_generation(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        real_stop=previews.client.stop
        async def unconfirmed(*args,**kwargs):return {'cleanup_confirmed':False}
        previews.client.stop=unconfirmed
        for attempt in range(2):
            with pytest.raises(DocumentPreviewError,match='cleanup_pending'):await previews.prepare(record['id'],owner='alice')
            assert previews.content(record['id'],owner='alice')['preview_status']=='cleanup_pending'
            with pytest.raises(DocumentPreviewError,match='page_unavailable'):previews.page(record['id'],1,owner='alice')
            assert broker.db.execute('SELECT generation FROM jobs').fetchone()[0]==1
            assert backend.start_count==1
        previews.client.stop=real_stop
        result=await previews.prepare(record['id'],owner='alice')
        assert result['preview_status']=='ready' and backend.start_count==2
        assert broker.db.execute('SELECT generation FROM jobs').fetchone()[0]==2

@pytest.mark.anyio
async def test_ready_published_only_after_cleanup_and_formatted_source_copy_preserved(tmp_path):
    async with setup(tmp_path) as (files,record,backend,broker,previews):
        source='# Generated source\n\nOriginal **Markdown** content.'
        record=files.create({'name':'Generated.docx','title':'Generated','content':source},owner_principal_id='alice',chat_id='root',dataset='company')
        before=files.download(record['id'],owner_principal_id='alice')
        stopping=asyncio.Event();gate=asyncio.Event();real_stop=previews.client.stop
        async def held(*args,**kwargs):stopping.set();await gate.wait();return await real_stop(*args,**kwargs)
        previews.client.stop=held
        task=asyncio.create_task(previews.prepare(record['id'],owner='alice'))
        try:
            await asyncio.wait_for(stopping.wait(),2)
            assert previews.content(record['id'],owner='alice')['preview_status']=='prepared'
            with pytest.raises(DocumentPreviewError):previews.page(record['id'],1,owner='alice')
        finally:gate.set()
        result=await task
        assert result['preview_status']=='ready' and result['content']==source
        assert result['file']['copy_source']=='original_source'
        assert files.download(record['id'],owner_principal_id='alice')==before
