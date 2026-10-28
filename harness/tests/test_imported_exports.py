"""Imported export packages retain source provenance and use only verified bytes."""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest
BASE=Path(os.environ.get('ROCOTO_EXPORT_TEST_BASE',Path(__file__).parents[1]))
sys.path.insert(0,str(BASE/'tests'));sys.path.insert(0,str(BASE));sys.path.insert(0,str(Path(__file__).parents[1]))
from media_exports import ExportStore
from media import EvidenceMedia
from test_source_derivatives import imported,prepared_import,FakeMedia,helper,anyio_backend
from test_imported_media import deny_decoders


@pytest.fixture(autouse=True)
def fixture_export_allowance(monkeypatch):
    # These opaque fixture payloads are tiny. Reserve their bounded test budget
    # rather than requiring the production 1 GiB package headroom on the dev disk.
    # The real disk-admission refusal is exercised in test_media_exports.py.
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',8*1024**2)


class Index:
    def __init__(self,row):self.clips=[row]
    def get_clip(self,key):return next((r for r in self.clips if r['id']==key),None)


def document(row):
    return dict(title='Loading bay selects',summary='A source-resolution editing handoff.',
        items=[dict(clip_id=row['id'],speed_multiplier='2',reason='Review this delivery.')])


def setup(imported,broker=None):
    broker=broker or FakeMedia();media=EvidenceMedia(imported['state'])
    media.derivatives=helper(imported,broker)
    store=ExportStore(imported['state'],Index(imported['row']),media)
    return store,broker


@pytest.mark.anyio
async def test_imported_export_uses_source_cut_receipt_and_original_filename_without_host_decode(imported,monkeypatch):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    result=await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    assert result['status']=='ready',result
    with zipfile.ZipFile(store.download_path(result['id'],owner_principal_id='owner')) as archive:
        manifest=json.loads(archive.read('manifest.json'));item=manifest['items'][0]
        assert archive.read(item['filename'])==b'opaque video content'
        assert archive.read(item['thumbnail']['filename'])==imported['thumbnail']
        assert item['source']['filename']=='Loading bay.mov'
        assert item['source']['sha256']==hashlib.sha256(imported['original']).hexdigest()
        assert (item['media']['width'],item['media']['height'])==(1920,1080)
        assert item['media']['mapping_basis']=='preserved_source_frame_pts'
        assert item['media']['actual_original_first_frame_pts']==300
        assert item['media']['source_frames'][0]['original_pts_seconds']==12
        assert item['media']['derivation_profile']=='source-media-derive-v1'
        assert item['source']['capture_start_utc'] is None
        assert manifest['timeline_duration']=={'numerator':2,'denominator':1}
        assert str(imported['state']) not in json.dumps(manifest)
        assert 'derivative-' not in json.dumps(manifest)
    assert broker.calls==1 and broker.releases==1 and not broker.alive


@pytest.mark.anyio
@pytest.mark.parametrize('owner,dataset',[('foreign','warehouse'),('owner','other')])
async def test_owner_and_collection_rejected_before_admission(imported,monkeypatch,owner,dataset):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    with pytest.raises(ValueError):await store.prepare(document(imported['row']),dataset=dataset,owner_principal_id=owner)
    assert not broker.calls and store.list(owner_principal_id=owner)==[]


@pytest.mark.anyio
@pytest.mark.parametrize('change',[{'path':'/etc/passwd'},{'start':1.},{'storage_kind':'legacy'},
    {'original_sha256':'0'*64},{'prepared_sha256':'1'*64}])
async def test_tampered_index_record_cannot_select_files_or_decode(imported,monkeypatch,change):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    store.index.clips[0]=dict(imported['row'],**change)
    with pytest.raises(ValueError):await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    assert not broker.calls


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['uncommissioned','revoked_publication','revoked_owner'])
async def test_imported_export_requires_live_authorized_derivative_helper(imported,monkeypatch,kind):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    if kind=='uncommissioned':store.media.derivatives.enabled=False
    if kind=='revoked_publication':
        with imported['store']._write() as db:db.execute("UPDATE jobs SET status='queued' WHERE stage='publish'")
    if kind=='revoked_owner':
        with imported['store']._write() as db:db.execute("UPDATE assets SET owner='foreign'")
    if kind=='revoked_owner':
        with pytest.raises(ValueError):await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    else:
        result=await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
        assert result['status']=='failed'
        assert str(imported['state']) not in json.dumps(result)
    assert not broker.calls and not list((imported['state']/'payloads').glob('export-*'))


@pytest.mark.anyio
@pytest.mark.parametrize('target',['original','derivative','thumbnail'])
async def test_replaced_bytes_between_derivation_and_copy_fail_closed(imported,monkeypatch,target):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    source_cut=store.media.source_cut
    async def replace(record,**kwargs):
        receipt=await source_cut(record,**kwargs)
        from imported_media import ImportedMedia
        resolved=ImportedMedia(imported['state']).resolve(record)
        file=receipt.files[0] if target=='derivative' else getattr(resolved,target)
        file.path.chmod(0o600);file.path.write_bytes(b'X'*file.size);file.path.chmod(0o400)
        return receipt
    store.media.source_cut=replace
    result=await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    assert result['status']=='failed' and not list((imported['state']/'payloads').glob('export-*'))
    assert not broker.alive and broker.releases==1


@pytest.mark.anyio
async def test_duplicate_selection_reuses_derivative_but_preserves_edit_order(imported,monkeypatch):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    body=document(imported['row']);body['items'].append(dict(clip_id=imported['row']['id'],speed_multiplier='1'))
    result=await store.prepare(body,dataset='warehouse',owner_principal_id='owner')
    assert result['status']=='ready' and broker.calls==1
    items=result['manifest']['items'];assert [i['filename'] for i in items]==['clips/01.mp4','clips/02.mp4']
    assert result['manifest']['timeline_duration']=={'numerator':6,'denominator':1}


@pytest.mark.anyio
async def test_source_revoked_while_archiving_never_publishes_package(imported,monkeypatch):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    original=store._documents_and_zip
    def revoke(*args):
        original(*args)
        with imported['store']._write() as db:db.execute("UPDATE jobs SET status='queued' WHERE stage='publish'")
    monkeypatch.setattr(store,'_documents_and_zip',revoke)
    result=await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    assert result['status']=='failed' and not list((imported['state']/'payloads').glob('export-*'))


@pytest.mark.anyio
async def test_stop_waits_for_sandbox_cleanup_and_removes_export_work(imported,monkeypatch):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    broker.gate.clear();broker.stop_gate.clear()
    pending=asyncio.create_task(store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner'))
    try:
        await asyncio.wait_for(broker.admitted.wait(),5)
        pending.cancel();await asyncio.sleep(.02);pending.cancel();await asyncio.sleep(.02)
        assert not pending.done() and broker.alive
    finally:
        broker.stop_gate.set()
        if not pending.done():pending.cancel()
        outcome=(await asyncio.gather(pending,return_exceptions=True))[0]
    assert isinstance(outcome,asyncio.CancelledError)
    assert not broker.alive and broker.stops==1 and broker.releases==1
    assert store.list(owner_principal_id='owner')[0]['status']=='interrupted'
    assert not list((imported['state']/'payloads').glob('.*.work'))


@pytest.mark.anyio
async def test_verified_source_copy_never_reopens_a_media_path(imported,monkeypatch):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    await store.media.source_cut(imported['row'],owner_principal_id='owner')
    open_path=Path.open
    def guarded(path,*args,**kwargs):
        mode=args[0] if args else kwargs.get('mode','r')
        if 'source-intake' in path.parts and ('originals' in path.parts or path.suffix in {'.mp4','.jpg'}) and 'r' in mode:
            raise AssertionError('reopened imported media path')
        return open_path(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',guarded)
    result=await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    assert result['status']=='ready' and broker.calls==1


@pytest.mark.anyio
async def test_package_quota_keeps_imported_artifacts_private(imported,monkeypatch):
    deny_decoders(monkeypatch);store,broker=setup(imported)
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',1024*1024)
    result=await store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner')
    assert result['status']=='failed' and not list((imported['state']/'payloads').glob('export-*'))
    assert broker.releases==1 and not broker.alive


@pytest.mark.anyio
async def test_repeated_stop_joins_archive_thread_before_removing_work(imported,monkeypatch):
    import threading
    deny_decoders(monkeypatch);store,broker=setup(imported)
    entered=threading.Event();release=threading.Event();original=store._documents_and_zip
    def waiting(*args):
        entered.set();assert release.wait(5);original(*args)
    monkeypatch.setattr(store,'_documents_and_zip',waiting)
    pending=asyncio.create_task(store.prepare(document(imported['row']),dataset='warehouse',owner_principal_id='owner'))
    assert await asyncio.to_thread(entered.wait,5)
    try:
        pending.cancel();await asyncio.sleep(.02);pending.cancel();await asyncio.sleep(.02)
        assert not pending.done()
    finally:release.set()
    with pytest.raises(asyncio.CancelledError):await pending
    assert store.list(owner_principal_id='owner')[0]['status']=='interrupted'
    assert not list((imported['state']/'payloads').glob('.*.work'))
    assert not list((imported['state']/'payloads').glob('export-*'))
