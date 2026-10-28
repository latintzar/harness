"""Editing handoffs use actual bounded cuts, private ownership and measured media."""
import asyncio
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from media_exports import ExportStore


@pytest.fixture(autouse=True)
def fixture_export_allowance(monkeypatch):
    # Three-second 96x64 clips fit this explicit fixture allowance. Production
    # retains its full package quota and two-copy disk reserve.
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',8*1024**2)


class Index:
    def __init__(self, source):
        self.clips = [dict(id=f"clip-{i}", video_id="asset-a", dataset="cafe", camera="Counter",
            path=str(source), start=float(i), end=float(i+1),
            source_reference="https://example.org/public-source", provenance="Recorded test footage")
            for i in range(2)]

    def get_clip(self, key):
        return next((c for c in self.clips if c['id'] == key), None)


@pytest.fixture
def prepared(tmp_path):
    state = tmp_path/'company'; media = state/'media'; media.mkdir(parents=True)
    source = media/'source.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
        'testsrc2=size=96x64:rate=10:duration=3','-c:v','libx264','-threads','1',str(source)],check=True)
    return state, Index(source)


def document():
    return {'title':'Counter selects','summary':'Two moments for the editor.', 'output_fps':'30000/1001',
        'items':[{'clip_id':'clip-1','speed_multiplier':'2','framing':'Preserve source','reason':'Second moment'},
                 {'clip_id':'clip-0','speed_multiplier':'1','reason':'Opening moment'}]}


def test_package_has_real_ordered_cuts_hashes_and_rational_edit_plan(prepared):
    state,index = prepared; store = ExportStore(state,index)
    result = asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    assert result['status']=='ready'
    archive = store.download_path(result['id'],owner_principal_id='peter',dataset='cafe')
    with zipfile.ZipFile(archive) as package:
        assert {'manifest.json','report.md','README.txt','edit-decisions.csv'} <= set(package.namelist())
        manifest = json.loads(package.read('manifest.json'))
        assert [i['clip_id'] for i in manifest['items']]==['clip-1','clip-0']
        assert manifest['timeline_duration']=={'numerator':3,'denominator':2}
        assert manifest['output_fps']=={'numerator':30000,'denominator':1001}
        rows = list(csv.DictReader(io.StringIO(package.read('edit-decisions.csv').decode())))
        assert rows[0]['speed_multiplier']=='2/1' and rows[1]['timeline_in']=='1/2'
        for item in manifest['items']:
            assert len(item['source']['sha256'])==64 and len(item['media']['sha256'])==64
            assert item['media']['width']==96 and item['media']['height']==64
            assert item['media']['duration_seconds']==pytest.approx(1,abs=.05)
            assert item['media']['speed_multiplier_applied']==1
            assert package.read(item['filename'])
        text = b'\n'.join(package.read(name) for name in package.namelist() if not name.endswith('.mp4'))
        assert str(state).encode() not in text
        assert b'editor-specific import' in package.read('README.txt')
    reopened = ExportStore(state,index)
    assert reopened.get(result['id'],owner_principal_id='peter')['sha256']==result['sha256']
    with pytest.raises(ValueError): reopened.get(result['id'],owner_principal_id='alice')
    with pytest.raises(ValueError): reopened.get(result['id'],owner_principal_id='peter',dataset='warehouse')


def test_scope_source_paths_and_bounds_fail_before_media_work(prepared,tmp_path):
    state,index = prepared; store = ExportStore(state,index)
    with pytest.raises(ValueError):
        asyncio.run(store.prepare(document(),dataset='warehouse',owner_principal_id='peter'))
    index.clips[0]['path']=str(tmp_path/'outside.mp4')
    with pytest.raises(ValueError):
        asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    index.clips[0]['path']=index.clips[1]['path']
    link=state/'media'/'link.mp4';link.symlink_to(Path(index.clips[0]['path']))
    index.clips[0]['path']=str(link)
    with pytest.raises(ValueError):
        asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    with pytest.raises(ValueError): store.get('../payloads',owner_principal_id='peter')
    body=document();body['items'][0]['speed_multiplier']=0
    with pytest.raises(ValueError):
        asyncio.run(store.prepare(body,dataset='cafe',owner_principal_id='peter'))
    assert store.list(owner_principal_id='peter')==[]


def test_cancellation_cleans_partial_files_and_preserves_terminal_record(prepared):
    state,index = prepared
    class WaitingMedia:
        async def clip(self,*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
    async def scenario():
        nonlocal entered
        entered=asyncio.Event()
        store=ExportStore(state,index,WaitingMedia())
        task=asyncio.create_task(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
        await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        records=store.list(owner_principal_id='peter')
        assert len(records)==1 and records[0]['status']=='interrupted'
        assert not list((state/'payloads').glob('export-*'))
    entered=None
    asyncio.run(scenario())


def test_failure_and_retained_quota_keep_jobs_bounded(prepared,monkeypatch):
    state,index=prepared
    class BrokenMedia:
        async def clip(self,*args,**kwargs): raise OSError('/private/credential-source.mp4 failed')
    store=ExportStore(state,index,BrokenMedia())
    result=asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    assert result['status']=='failed' and '/private' not in json.dumps(result)
    monkeypatch.setattr('media_exports.MAX_RETAINED_EXPORTS',1)
    store=ExportStore(state,index)
    ready=asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    assert ready['status']=='ready'
    with pytest.raises(ValueError):
        asyncio.run(ExportStore(state,index).prepare(document(),dataset='cafe',owner_principal_id='alice'))


def test_export_slot_is_shared_across_store_instances(prepared):
    state,index=prepared
    async def scenario():
        entered=asyncio.Event()
        class WaitingMedia:
            async def clip(self,*args,**kwargs):
                entered.set();await asyncio.Event().wait()
        one=ExportStore(state,index,WaitingMedia());two=ExportStore(state,index)
        task=asyncio.create_task(one.prepare(document(),dataset='cafe',owner_principal_id='peter'))
        await asyncio.wait_for(entered.wait(),5)
        with pytest.raises(ValueError):
            await two.prepare(document(),dataset='cafe',owner_principal_id='alice')
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())


def test_restart_marks_unfinished_package_interrupted_without_retry(prepared):
    state,index=prepared;store=ExportStore(state,index)
    normalized,records,duration=store._selection(document(),'cafe','peter')
    record,fd,allowance=store._admit(normalized,'cafe','peter',duration)
    import os
    os.close(fd)
    work=state/'payloads'/('.'+record['id']+'.work');work.mkdir()
    (work/'unfinished.mp4').write_bytes(b'partial')
    store.recover()
    assert store.get(record['id'],owner_principal_id='peter')['status']=='interrupted'
    assert not work.exists()
    with pytest.raises(ValueError):store.download_path(record['id'],owner_principal_id='peter')


def test_byte_limits_and_private_report_notes_are_enforced(prepared,monkeypatch):
    state,index=prepared;store=ExportStore(state,index)
    body=document();body['summary']=f'Internal source at {state}/media/source.mp4'
    with pytest.raises(ValueError):asyncio.run(store.prepare(body,dataset='cafe',owner_principal_id='peter'))
    monkeypatch.setattr('media_exports.MAX_OUTPUT_BYTES',1024*1024)
    result=asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    assert result['status']=='failed'
    assert not list((state/'payloads').glob('export-*'))


def test_real_free_space_gate_refuses_before_media_and_saved_admission(prepared,monkeypatch):
    import media_exports
    from types import SimpleNamespace
    state,index=prepared
    class ForbiddenMedia:
        async def clip(self,*args,**kwargs):pytest.fail('Low disk admission reached a decoder')
    store=ExportStore(state,index,ForbiddenMedia())
    minimum=2*media_exports.MAX_OUTPUT_BYTES+128*1024**2
    monkeypatch.setattr(media_exports.shutil,'disk_usage',lambda _:SimpleNamespace(free=minimum-1))
    with pytest.raises(ValueError,match='available storage'):
        asyncio.run(store.prepare(document(),dataset='cafe',owner_principal_id='peter'))
    assert store.list(owner_principal_id='peter')==[]
    assert not list((state/'payloads').glob('export-*'))
