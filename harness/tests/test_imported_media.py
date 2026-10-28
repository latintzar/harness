"""Imported footage stays opaque in the web/query process."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from source_intake import IntakeActor, SourceStore


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def imported(tmp_path):
    from imported_media import PROFILE, RECIPE, IMAGE
    state = tmp_path / 'company'; state.mkdir(mode=0o700)
    store = SourceStore(state)
    actor = IntakeActor('owner')
    source = store.create_source(actor, collection='warehouse', name='Loading bay')
    original = b'opaque original video; deliberately undecodable'
    upload = store.begin_upload(actor, source['id'], filename='Loading bay.mov', size=len(original), idempotency_key='one')
    store.append_chunk(actor, upload['id'], offset=0, data=original)
    upload = store.seal_upload(actor, upload['id'])
    asset = store.get_asset(actor, upload['asset_id'])
    path = store.root/'worker-artifacts'/asset['id']/('prepare-'+'a'*32)
    path.mkdir(parents=True, mode=0o700)
    path.parent.chmod(0o700); path.parent.parent.chmod(0o700)
    prepared = b'opaque prepared video'; thumbnail = b'opaque prepared thumbnail'
    clip = dict(id=asset['id']+'-clip-000000', asset_id=asset['id'], collection='warehouse',
        file='clip-000000.mp4', sha256=digest(prepared), size_bytes=len(prepared),
        duration_seconds=4., width=640, height=360, source_elapsed_start_seconds=0.,
        source_elapsed_end_seconds=4., original_pts_start_seconds=12., original_pts_end_seconds=16.,
        thumbnail=dict(file='clip-000000.jpg', sha256=digest(thumbnail), size_bytes=len(thumbnail)))
    manifest = dict(schema_version=1, recipe=RECIPE, asset_id=asset['id'], collection='warehouse',
        original=dict(sha256=digest(original), size_bytes=len(original)),
        source=dict(coded_width=1920, coded_height=1080, duration_seconds=4., frame_count=100,
            origin_seconds=12., rotation_degrees=0, sample_aspect_ratio='1:1',
            capture_utc=None, capture_clock='unknown', variable_frame_intervals=True),
        normalization=dict(fps=24, window_seconds=8), clips=[clip])
    encoded = json.dumps(manifest).encode()
    for name, data in [('manifest.json', encoded), ('clip-000000.mp4',prepared), ('clip-000000.jpg',thumbnail)]:
        (path/name).write_bytes(data); (path/name).chmod(0o400)
    path.chmod(0o500)
    receipt = dict(artifact_id=path.name, manifest_sha256=digest(encoded), profile=PROFILE,
        recipe=RECIPE, image_digest=IMAGE, original_sha256=digest(original), clip_count=1)
    with store._write() as db:
        db.execute("UPDATE jobs SET status='succeeded',result=? WHERE asset_id=? AND stage='prepare'",
            (json.dumps(receipt),asset['id']))
        db.execute("UPDATE assets SET status='ready' WHERE id=?",(asset['id'],))
    row = dict(id=clip['id'], video_id=asset['id'], dataset='warehouse', camera=source['name'],
        path=str(store.blobs/digest(original)), start=0.,end=4.,duration=4.,sample_time=0.,
        storage_kind='imported_source',preparation_profile=PROFILE, preparation_recipe=RECIPE,
        preparation_image_digest=IMAGE, prepared_path=str(path/clip['file']), prepared_sha256=digest(prepared),
        prepared_manifest_sha256=digest(encoded),prepared_start=0.,prepared_end=4.,
        prepared_width=640,prepared_height=360,prepared_size_bytes=len(prepared),
        original_pts_start_seconds=12.,original_pts_end_seconds=16.,
        thumbnail_path=str(path/clip['thumbnail']['file']),thumbnail_sha256=digest(thumbnail),
        thumbnail_size_bytes=len(thumbnail),asset_id=asset['id'],source_id=source['id'],
            source_object_id=asset['object_id'],original_sha256=digest(original))
    return dict(state=state,store=store,row=row,manifest=manifest,receipt=receipt,
        original=original,prepared=prepared,thumbnail=thumbnail,actor=actor)


def test_library_uses_verified_worker_metadata_and_original_name(imported, monkeypatch):
    from library import SourceLibrary
    from types import SimpleNamespace
    def forbidden(*a, **kw): raise AssertionError('host decoder called')
    monkeypatch.setattr('subprocess.check_output',forbidden)
    library = SourceLibrary(imported['state'],SimpleNamespace(clips=[imported['row']]))
    result=library.listing('warehouse')['assets'][0]
    assert result['filename']=='Loading bay.mov'
    assert result['relative_path']=='Loading bay.mov'
    assert (result['width'],result['height'],result['frame_count'],result['fps'])==(1920,1080,100,25.)
    assert result['metadata_basis']=='sandbox_source_probe'
    assert result['sha256']==digest(imported['original'])
    assert str(imported['state']) not in json.dumps(result)


def deny_decoders(monkeypatch):
    def forbidden(*a,**kw): raise AssertionError('host decoder called')
    monkeypatch.setattr('subprocess.run',forbidden)
    monkeypatch.setattr('subprocess.check_output',forbidden)
    monkeypatch.setattr('asyncio.create_subprocess_exec',forbidden)
    monkeypatch.setattr('PIL.Image.open',forbidden)
    monkeypatch.setattr('cv2.VideoCapture',forbidden)


def test_prepared_playback_frames_original_and_http_are_opaque(imported,monkeypatch):
    from imported_media import ImportedMedia, VerifiedFile, byte_response
    from library import SourceLibrary
    from media import EvidenceMedia
    from retrieval import RetrievalIndex
    from types import SimpleNamespace
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    deny_decoders(monkeypatch)
    row=imported['row']; resolver=ImportedMedia(imported['state'])
    resolved=resolver.resolve(row)
    assert resolved.original.read_bytes()==imported['original']
    assert resolved.thumbnail.read_bytes()==imported['thumbnail']
    prepared=asyncio.run(EvidenceMedia(imported['state']).clip(row))
    assert isinstance(prepared,VerifiedFile) and prepared.read_bytes()==imported['prepared']
    index=RetrievalIndex.__new__(RetrievalIndex);index.clips=[row];index.imported_media=resolver
    inspection=index.inspect_clip(row['id'])
    assert inspection['frames']==[dict(time=0.,frame=None,image_bytes=imported['thumbnail'],
        basis='first_prepared_frame',width=640,height=360)]
    original,mime=SourceLibrary(imported['state'],index).original(row['video_id'],'warehouse')
    assert original.filename=='Loading bay.mov' and mime=='video/quicktime'
    async def handler(request):
        return byte_response(original,range_header=request.headers.get('range'),attachment=True)
    with TestClient(Starlette(routes=[Route('/original',handler)])) as client:
        response=client.get('/original')
        assert response.content==imported['original']
        assert "filename*=UTF-8''Loading%20bay.mov" in response.headers['content-disposition']
        assert response.headers['etag']=='"'+digest(imported['original'])+'"'
        for header,expected in [('bytes=3-10',imported['original'][3:11]),('bytes=-8',imported['original'][-8:]),('bytes=3-',imported['original'][3:])]:
            response=client.get('/original',headers={'Range':header})
            assert response.status_code==206 and response.content==expected
        for header in ['bytes=10000-','bytes=0-1,4-5','bytes=-0','bytes=','bytes=10-2']:
            assert client.get('/original',headers={'Range':header}).status_code==416


@pytest.mark.parametrize('field,value',[
    ('dataset','other'),('path','/etc/passwd'),('prepared_path','/etc/passwd'),
    ('thumbnail_path','/etc/passwd'),('prepared_sha256','b'*64),('thumbnail_sha256','b'*64),
    ('prepared_manifest_sha256','b'*64),('preparation_profile','1080p-detail'),
    ('preparation_image_digest','sha256:'+'b'*64),('preparation_recipe','v1'),
    ('prepared_width',1080),('prepared_size_bytes',1),('start',1),('end',5),
    ('prepared_start',1),('prepared_end',5),('original_pts_start_seconds',0),
    ('asset_id','asset-'+'f'*32),('source_id','source-other'),('source_object_id','object-other'),
    ('original_sha256','b'*64),('storage_kind','legacy'),('video_id','asset-'+'f'*32)
])
def test_untrusted_row_fields_cannot_rebind_source_or_quality(imported,field,value):
    from imported_media import ImportedMedia,ImportedMediaError
    with pytest.raises(ImportedMediaError) as error:
        ImportedMedia(imported['state']).resolve({**imported['row'],field:value})
    assert str(imported['state']) not in str(error.value)


@pytest.mark.parametrize('status',['received','preparing','indexing','needs_attention','cancelled'])
def test_pending_or_cancelled_rows_remain_hidden(imported,status):
    from imported_media import ImportedMedia,ImportedMediaError
    from library import SourceLibrary
    from types import SimpleNamespace
    with imported['store']._write() as db:
        db.execute('UPDATE assets SET status=?',(status,))
    with pytest.raises(ImportedMediaError,match='imported_asset_unavailable'):
        ImportedMedia(imported['state']).resolve(imported['row'])
    assert SourceLibrary(imported['state'],SimpleNamespace(clips=[imported['row']])).catalog()=={}


def test_company_root_and_source_owner_are_authoritative(imported,tmp_path):
    from imported_media import ImportedMedia,ImportedMediaError
    other=tmp_path/'other';other.mkdir(mode=0o700);SourceStore(other)
    with pytest.raises(ImportedMediaError):ImportedMedia(other).resolve(imported['row'])
    with imported['store']._write() as db:db.execute("UPDATE sources SET owner='other'")
    with pytest.raises(ImportedMediaError):ImportedMedia(imported['state']).resolve(imported['row'])


@pytest.mark.parametrize('kind',['manifest','original','prepared','thumbnail'])
def test_mutation_after_cached_verification_is_detected(imported,kind):
    from imported_media import ImportedMedia,ImportedMediaError
    resolver=ImportedMedia(imported['state']);resolved=resolver.resolve(imported['row'])
    path=(Path(imported['row']['prepared_path']).parent/'manifest.json') if kind=='manifest' else getattr(resolved,kind).path
    before=path.stat();path.chmod(0o600)
    data=path.read_bytes();path.write_bytes(b'!'+data[1:]);path.chmod(0o400)
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(ImportedMediaError,match='integrity'):
        resolver.resolve(imported['row'])


@pytest.mark.parametrize('kind',['symlink','hardlink','writable','parent_symlink','other_inode'])
def test_files_require_sealed_regular_owned_identity(imported,tmp_path,kind):
    from imported_media import ImportedMedia,ImportedMediaError
    resolver=ImportedMedia(imported['state']);file=resolver.resolve(imported['row']).prepared
    directory=file.path.parent;directory.chmod(0o700)
    if kind=='symlink':
        other=tmp_path/'outside';other.write_bytes(imported['prepared']);other.chmod(0o400)
        file.path.unlink();file.path.symlink_to(other)
    elif kind=='hardlink':os.link(file.path,tmp_path/'hardlink')
    elif kind=='writable':file.path.chmod(0o600)
    elif kind=='parent_symlink':
        renamed=directory.with_name('moved');directory.rename(renamed);directory.symlink_to(renamed,target_is_directory=True)
    else:
        with file.open() as held:
            file.path.unlink();file.path.write_bytes(b'bad'*10);file.path.chmod(0o400)
            assert held.read()==imported['prepared']
    with pytest.raises(ImportedMediaError):file.read_bytes()


def test_read_limit_and_stripped_tag_metadata_still_block_decoders(imported,monkeypatch):
    from imported_media import ImportedMedia,ImportedMediaError
    from library import SourceLibrary
    from types import SimpleNamespace
    deny_decoders(monkeypatch)
    resolver=ImportedMedia(imported['state']);resolved=resolver.resolve(imported['row'])
    with pytest.raises(ImportedMediaError,match='read_limit'):resolved.original.read_bytes(maximum=1)
    library=SourceLibrary(imported['state'],SimpleNamespace(clips=[]))
    for path in [resolved.original.path,resolved.prepared.path,resolved.thumbnail.path]:
        with pytest.raises(ImportedMediaError,match='metadata_requires_verified_record'):library.metadata(path)


def test_detailed_query_focus_and_export_wait_for_sandbox_profile(imported,monkeypatch):
    from imported_media import ImportedMediaError
    from media import EvidenceMedia
    from focus import FocusTracker
    from cosmos_inspect import _prepare_source_clip,_reuse_prepared_source
    from media_exports import ExportStore
    from types import SimpleNamespace
    deny_decoders(monkeypatch)
    # This profile-denial fixture needs only a tiny package reservation.
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',8*1024**2)
    row=imported['row'];state=imported['state']
    operations=[lambda:FocusTracker(state).build(row,[0,0,.5,.5]),
        lambda:_prepare_source_clip(row,state/'detail.mp4'),
        lambda:_reuse_prepared_source(row,state/'detail.mp4',state)]
    for operation in operations:
        with pytest.raises(ImportedMediaError,match='requires_sandbox_profile'):operation()
    with pytest.raises(ValueError,match='source_derivatives_unavailable'):
        asyncio.run(EvidenceMedia(state).clip(row,source_resolution=True,owner_principal_id='owner'))
    store=ExportStore(state,SimpleNamespace(get_clip=lambda _:row))
    result=asyncio.run(store.prepare({'title':'Selected clip','items':[{'clip_id':row['id']}]},
        dataset='warehouse',owner_principal_id='owner'))
    assert result['status']=='failed'
    assert not list(store.directory.rglob('*.mp4'))


def test_missing_resolver_fails_before_frame_decoder(imported,monkeypatch):
    from imported_media import ImportedMediaError
    from retrieval import RetrievalIndex
    deny_decoders(monkeypatch)
    index=RetrievalIndex.__new__(RetrievalIndex);index.clips=[imported['row']]
    with pytest.raises(ImportedMediaError,match='resolver_unavailable'):index.inspect_clip(imported['row']['id'])


def test_imported_and_legacy_assets_keep_legacy_logical_paths(imported,monkeypatch):
    from library import SourceLibrary
    from types import SimpleNamespace
    path=imported['state'].parent/'demo'/'a.mp4';path.parent.mkdir();path.write_bytes(b'legacy')
    legacy={**imported['row'],'id':'demo','video_id':'demo','path':str(path),'storage_kind':'legacy'}
    library=SourceLibrary(imported['state'],SimpleNamespace(clips=[legacy,imported['row']]))
    catalog=library.catalog()
    assert catalog['demo']['relative_path']=='a.mp4'
    assert catalog[imported['row']['video_id']]['relative_path']=='Loading bay.mov'


def test_alias_into_imported_storage_stays_out_of_host_metadata(imported,monkeypatch,tmp_path):
    from imported_media import ImportedMediaError
    from library import SourceLibrary
    from types import SimpleNamespace
    deny_decoders(monkeypatch)
    alias=tmp_path/'looks-like-demo.mp4';alias.symlink_to(imported['row']['path'])
    with pytest.raises(ImportedMediaError):SourceLibrary(imported['state'],SimpleNamespace()).metadata(alias)


def test_non_regular_file_fails_without_blocking(imported):
    from imported_media import ImportedMedia,ImportedMediaError
    file=ImportedMedia(imported['state']).resolve(imported['row']).thumbnail
    file.path.parent.chmod(0o700);file.path.unlink();os.mkfifo(file.path,0o400)
    with pytest.raises(ImportedMediaError,match='invalid_imported_file'):file.read_bytes()


def test_completed_receipt_and_bounded_original_are_required(imported):
    from imported_media import ImportedMedia,ImportedMediaError,SOURCE_BYTES
    with imported['store']._write() as db:db.execute("UPDATE jobs SET status='needs_reconciliation' WHERE stage='prepare'")
    with pytest.raises(ImportedMediaError,match='preparation_unavailable'):ImportedMedia(imported['state']).resolve(imported['row'])
    with imported['store']._write() as db:
        db.execute("UPDATE jobs SET status='succeeded' WHERE stage='prepare'")
        db.execute('UPDATE assets SET bytes=?',(SOURCE_BYTES+1,))
    with pytest.raises(ImportedMediaError):ImportedMedia(imported['state']).resolve(imported['row'])
