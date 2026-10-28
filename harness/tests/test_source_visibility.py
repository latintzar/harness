"""Ready/owner filtering preserves concrete Qdrant generations and vector rows."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pytest

HERE=Path(__file__).parents[1]
BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',HERE))
WORKER=Path(os.environ.get('ROCOTO_WORKER_TEST_BASE',BASE))
sys.path.insert(0,str(BASE));sys.path.insert(0,str(WORKER));sys.path.insert(0,str(HERE))
from source_visibility import ready_index_view,VisibilityError
from source_intake import SourceStore,IntakeActor
from qdrant_retrieval import QdrantRetrievalIndex,point_id
import cosmos_retrieval as cosmos

spec=importlib.util.spec_from_file_location('source_worker_fixture',WORKER/'tests/test_source_worker.py')
fixture=importlib.util.module_from_spec(spec);sys.modules[spec.name]=fixture;spec.loader.exec_module(fixture)


@pytest.fixture
def anyio_backend():return 'asyncio'


async def complete(instance):
    await fixture.through_embed(instance)
    assert (await instance.run_once(stages=('publish',)))['status']=='succeeded'


@pytest.mark.anyio
async def test_marker_ready_crash_gap_hidden_from_search_status_corpus_and_cold_restart(tmp_path,monkeypatch):
    instance,store,media,embedding,api=fixture.setup(tmp_path)
    old=fixture.new_asset(store,key='ready');await complete(instance)
    pending=fixture.new_asset(store,key='pending');await fixture.through_embed(instance)
    finish=store._finish_job_locked
    def fail_ready(db,row,result):
        if row['stage']=='publish':raise OSError('synthetic marker to Ready interruption')
        return finish(db,row,result)
    monkeypatch.setattr(store,'_finish_job_locked',fail_ready)
    assert (await instance.run_once())['status']=='needs_reconciliation'
    raw=QdrantRetrievalIndex(instance.publisher.state,client=api)
    assert len(raw.clips)==4
    view=ready_index_view(raw,store,principal_id='alice')
    assert len(view.clips)==2 and {row['video_id'] for row in view.clips}=={old['asset_id']}
    assert view.collection==raw.collection and view.fingerprint==raw.fingerprint and view.tenant_id==raw.tenant_id
    assert np.array_equal(view.embeddings,raw.embeddings[:2]) and not view.embeddings.flags.writeable
    status=view.status()
    assert status['clip_count']==2 and status['video_count']==1 and status['footage_seconds']==10
    assert pending['asset_id'] not in json.dumps(status)
    from server import public_corpus
    assert {row['video_id'] for row in public_corpus(view)}=={old['asset_id']}
    hits=view.search_vector(np.eye(1,256,dtype=np.float32)[0],limit=50)
    assert len(hits)==2 and {row['video_id'] for row in hits}=={old['asset_id']}
    query=next(body for method,path,body in reversed(api.calls) if path.endswith('/query'))
    assert next(item['has_id'] for item in query['filter']['must'] if 'has_id' in item)==[point_id(row['id']) for row in view.clips]
    # A cold reader may load the complete marker but still exposes only Ready rows.
    reopened=SourceStore(tmp_path/'company')
    cold=ready_index_view(QdrantRetrievalIndex(instance.publisher.state,client=api),reopened,principal_id='alice')
    assert cold.clips==view.clips and cold.report==view.report
    # Local-only reconciliation exposes the pending import without repeating media or provider work.
    monkeypatch.setattr(store,'_finish_job_locked',finish)
    job=next(row for row in store.get_asset(fixture.OWNER,pending['asset_id'])['jobs'] if row['stage']=='publish')
    generation=cosmos.load_generation(instance.publisher.state)[3]['generation_id']
    store.reconcile_job(job['id'],expected_generation=job['generation'],outcome='retry',evidence_reference=generation)
    assert (await instance.run_once())['status']=='succeeded'
    fresh=ready_index_view(raw,store,principal_id='alice')
    assert len(fresh.clips)==4 and len(view.clips)==2 and len(embedding.called)==2
    assert fresh.fingerprint==raw.fingerprint and cosmos.load_generation(instance.publisher.state)[3]['generation_id']==generation


@pytest.mark.anyio
async def test_explicit_principal_and_observer_selected_owner_scope(tmp_path):
    instance,store,media,embedding,api=fixture.setup(tmp_path)
    alice=fixture.new_asset(store,key='a');await complete(instance)
    bob=fixture.new_asset(store,actor=IntakeActor('bob'),key='b');await complete(instance)
    raw=QdrantRetrievalIndex(instance.publisher.state,client=api)
    first=ready_index_view(raw,store,principal_id='alice')
    second=ready_index_view(raw,store,principal_id='bob')
    assert {row['video_id'] for row in first.clips}=={alice['asset_id']}
    assert {row['video_id'] for row in second.clips}=={bob['asset_id']}
    assert np.array_equal(second.embeddings,raw.embeddings[2:])
    assert first.collection==second.collection==raw.collection
    assert not ready_index_view(raw,store,principal_id='observer-login-id').clips
    # Host ChatPermissions.principal returns the selected owner for an authorized observer.
    assert ready_index_view(raw,store,principal_id='alice').clips==first.clips
    other_store=SourceStore(tmp_path/'other-company')
    assert not ready_index_view(raw,other_store,principal_id='alice').clips
    assert not ready_index_view(raw,store,principal_id=None).clips
    with pytest.raises(VisibilityError):ready_index_view(raw,store,principal_id='')


@pytest.mark.anyio
async def test_view_cannot_publish_refresh_or_mutate_the_raw_index(tmp_path):
    instance,store,media,embedding,api=fixture.setup(tmp_path);fixture.new_asset(store);await complete(instance)
    raw=QdrantRetrievalIndex(instance.publisher.state,client=api)
    before=copy.deepcopy(raw.clips);marker=(raw.state/cosmos.GENERATION_FILE).read_bytes();calls=len(api.calls)
    view=ready_index_view(raw,store,principal_id='alice')
    assert view.refresh() is False
    for name in ('_reload','_publish_loaded','build'):
        with pytest.raises(VisibilityError,match='read_only'):getattr(view,name)()
    view.clips[0]['camera']='Local selection';view.report['videos'].clear()
    assert raw.clips==before and len(raw.report['videos'])==1
    assert (raw.state/cosmos.GENERATION_FILE).read_bytes()==marker and len(api.calls)==calls


@pytest.mark.anyio
@pytest.mark.parametrize('mutation',[
    lambda row:row.update(source_id='source-'+'0'*32),
    lambda row:row.update(source_object_id='object-'+'0'*32),
    lambda row:row.update(original_sha256='0'*64),
    lambda row:row.update(prepared_manifest_sha256='0'*64),
    lambda row:row.update(dataset='another-private-collection'),
    lambda row:row.update(storage_kind='legacy',path='/elsewhere/movie.mp4'),
    lambda row:row.update(asset_id='asset-'+'0'*32),
])
async def test_forged_import_row_authority_is_excluded(tmp_path,mutation):
    instance,store,media,embedding,api=fixture.setup(tmp_path);fixture.new_asset(store);await complete(instance)
    raw=QdrantRetrievalIndex(instance.publisher.state,client=api)
    mutation(raw.clips[0])
    view=ready_index_view(raw,store,principal_id='alice')
    assert len(view.clips)==1 and view.clips[0]['id']==raw.clips[1]['id']
    assert np.array_equal(view.embeddings,raw.embeddings[[1]])


@pytest.mark.anyio
async def test_missing_success_receipt_hides_corrupt_ready_authority(tmp_path):
    instance,store,media,embedding,api=fixture.setup(tmp_path);upload=fixture.new_asset(store);await complete(instance)
    raw=QdrantRetrievalIndex(instance.publisher.state,client=api)
    with store._write() as db:db.execute("UPDATE jobs SET result=? WHERE stage='publish'",(json.dumps({'generation_id':'claimed'}),))
    assert not ready_index_view(raw,store,principal_id='alice').clips


def test_legacy_corpus_remains_available_when_source_store_is_unavailable(tmp_path,monkeypatch):
    api=fixture.FakeQdrant();fixture.fixture.publish(tmp_path/'legacy')
    raw=QdrantRetrievalIndex(tmp_path/'legacy',client=api);store=SourceStore(tmp_path/'company')
    # An imported-looking reference is rejected when there is no source authority.
    raw.clips.append({**raw.clips[0],'id':'pending','video_id':'asset-'+'1'*32,'storage_kind':'imported_source'})
    raw.embeddings=np.concatenate((raw.embeddings,raw.embeddings[:1]))
    def failed():raise sqlite3.OperationalError('synthetic source database unavailable')
    monkeypatch.setattr(store,'_read',failed)
    view=ready_index_view(raw,store,principal_id='alice')
    assert len(view.clips)==3 and view.source_visibility_unavailable is True
    assert view.report['video_count']==3 and 'pending' not in json.dumps(view.status())
    assert len(ready_index_view(raw,store,principal_id=None).clips)==3
