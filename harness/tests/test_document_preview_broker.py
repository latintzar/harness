import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).parents[1]))
import pytest
import source_media as media
import document_preview_contract as contract
from test_source_media import FakeBackend,terminal,sha

@pytest.fixture
def anyio_backend():return 'asyncio'

class DocumentBackend(FakeBackend):
    async def wait(self,spec):
        await self.gate.wait();self.spec=spec
        root=spec.output/'preview';root.mkdir()
        page=b'\x89PNG\r\n\x1a\n'+b'\0\0\0\rIHDR'+(100).to_bytes(4,'big')+(150).to_bytes(4,'big')+b'opaque-fixture'
        text=b'Fixture document'
        for name,body in {'page-0001.png':page,'text.txt':text}.items():(root/name).write_bytes(body)
        original=spec.original.read_bytes()
        m={'schema_version':1,'profile':contract.PROFILE,'recipe':contract.RECIPE,'file_id':spec.document.file_id,
            'format':spec.document.format,'original_sha256':sha(original),'original_size_bytes':len(original),
            'page_count':1,'pages':[{'page':1,'path':'page-0001.png','width':100,'height':150,'size_bytes':len(page),'sha256':sha(page)}],
            'text':{'path':'text.txt','size_bytes':len(text),'sha256':sha(text),'truncated':False}}
        if self.mutate:self.mutate(root,m)
        (root/'manifest.json').write_text(json.dumps(m));return 0

@pytest.fixture
def context(tmp_path):
    backend=DocumentBackend(tmp_path/'volumes')
    broker=media.MediaBroker(tmp_path/'broker',backend,app_uid=os.getuid(),app_gid=os.getgid(),worker_uid=os.getuid(),document_previews=True)
    scope=media.MediaScope('company','alice','asset-'+'a'*32,'job-'+'b'*32)
    original=tmp_path/'original';original.write_bytes(b'opaque-native-document');original.chmod(0o400)
    values={'generation':1,'collection':'company','original_bytes':original.stat().st_size,
        'original_sha256':sha(original.read_bytes()),'expires_at':time.time()+60,
        'document':{'purpose':contract.PURPOSE,'file_id':'file-'+'a'*32,'format':'docx'}}
    return backend,broker,scope,original,values

@pytest.mark.anyio
async def test_profile_admission_closed_by_default(context):
    backend,broker,scope,original,values=context;broker.document_previews=False
    with original.open('rb') as stream:
        with pytest.raises(media.MediaError,match='document_preview_unavailable'):
            await broker.preview(scope,original_fd=stream.fileno(),**values)
    assert backend.start_count==0
    await broker.close()

@pytest.mark.anyio
async def test_actual_descriptor_protocol_and_sealed_pages(context,tmp_path):
    backend,broker,scope,original,values=context
    server=media.MediaServer(broker,tmp_path/'socket/control.sock');await server.start()
    client=media.MediaClient(server.path,server_uid=os.getuid())
    try:
        with original.open('rb') as stream:lease=await client.preview(scope,original_fd=stream.fileno(),**values)
        result=await terminal(client,scope,lease)
        assert result['status']=='succeeded' and result['cleanup_confirmed']
        assert result['result']['profile']==contract.PROFILE
        assert backend.spec.image==contract.IMAGE and backend.spec.arguments==['--file-id','file-'+'a'*32,'--format','docx']
        assert backend.spec.wall_seconds==150 and backend.start_count==1
        with original.open('rb') as stream:again=await client.preview(scope,original_fd=stream.fileno(),**values)
        assert again==lease and backend.start_count==1
        file=next(f for f in result['result']['files'] if f['path']=='text.txt')
        raw=b''.join([part async for part in client.read_output(scope,lease,file['index'])]);assert raw==b'Fixture document'
        await client.release(scope,lease)
    finally:await server.close()

@pytest.mark.anyio
@pytest.mark.parametrize('mutation',['original','pagehash','dimensions','extra','format'])
async def test_worker_claim_mismatch_rejects_snapshot(context,mutation):
    backend,broker,scope,original,values=context
    def mutate(root,m):
        if mutation=='original':m['original_sha256']='0'*64
        if mutation=='pagehash':m['pages'][0]['sha256']='0'*64
        if mutation=='dimensions':m['pages'][0]['width']=99
        if mutation=='extra':(root/'unexpected.html').write_text('active')
        if mutation=='format':m['format']='pdf'
    backend.mutate=mutate
    with original.open('rb') as stream:lease=await broker.preview(scope,original_fd=stream.fileno(),**values)
    result=await terminal(broker,scope,lease)
    assert result['status']=='failed' and result['cleanup_confirmed'] and result['result'] is None
    await broker.release(scope,lease);await broker.close()

@pytest.mark.anyio
async def test_stop_confirms_whole_worker_and_no_page_output(context):
    backend,broker,scope,original,values=context;backend.gate.clear()
    with original.open('rb') as stream:lease=await broker.preview(scope,original_fd=stream.fileno(),**values)
    await backend.started.wait();result=await broker.stop(scope,lease)
    assert result['status']=='stopped' and result['cleanup_confirmed'] and result['result'] is None and not backend.alive
    await broker.release(scope,lease);await broker.close()

@pytest.mark.anyio
async def test_empty_extracted_text_is_a_valid_bounded_protocol_file(context,tmp_path):
    backend,broker,scope,original,values=context
    def mutate(root,m):
        (root/'text.txt').write_bytes(b'');m['text'].update(size_bytes=0,sha256=sha(b''))
    backend.mutate=mutate;server=media.MediaServer(broker,tmp_path/'socket/control.sock');await server.start()
    client=media.MediaClient(server.path,server_uid=os.getuid())
    try:
        with original.open('rb') as stream:lease=await client.preview(scope,original_fd=stream.fileno(),**values)
        result=await terminal(client,scope,lease);assert result['status']=='succeeded'
        entry=next(x for x in result['result']['files'] if x['path']=='text.txt')
        assert b''.join([part async for part in client.read_output(scope,lease,entry['index'])])==b''
        await client.release(scope,lease)
    finally:await server.close()
