"""Crash/cold-start and additive import guarantees without model execution."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import copy
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import cosmos_retrieval as cosmos


def incoming(number=1):
    asset = 'asset-' + f'{number:032x}'
    records = [{'id': asset+'-clip-000', 'video_id': asset, 'dataset': 'cafe', 'camera': 'Front',
        'start': 0., 'end': 2., 'duration': 20., 'sample_time': 1.,
        'path': '/private/originals/' + f'{number:064x}',
        'embedded_clip_sha256': f'{number+100:064x}', 'model': cosmos.MODEL_ID, 'model_revision': cosmos.MODEL_REVISION}]
    vectors = np.zeros((1, cosmos.DIMENSIONS), dtype=np.float32)
    vectors[0, number % cosmos.DIMENSIONS] = 1
    assets = {asset: {'asset_id': asset, 'source_id': 'source-'+f'{number:032x}',
        'object_id': 'object-'+f'{number:032x}', 'collection': 'cafe', 'original_sha256': f'{number:064x}'}}
    return records, vectors, assets


def merge(state, number, **kwargs):
    records, vectors, assets = incoming(number)
    return cosmos.merge_index(state, records, vectors, asset_metadata=assets, **kwargs)


def legacy(state, marked=True):
    state.mkdir(exist_ok=True)
    records, vectors, _ = incoming(90)
    report = {'model': cosmos.MODEL_ID, 'model_revision': cosmos.MODEL_REVISION,
        'embedding_dimensions': 256, 'clip_count': 1, 'video_count': 1}
    output = io.BytesIO(); np.save(output, vectors, allow_pickle=False)
    files = {'clips.json': json.dumps(records).encode(), 'embeddings.npy': output.getvalue(),
        'index-report.json': json.dumps(report).encode()}
    for name, data in files.items(): (state/name).write_bytes(data)
    if marked:
        marker = {'schema_version': 1, 'generation_id': 'legacy-fixture', 'model': cosmos.MODEL_ID,
            'model_revision': cosmos.MODEL_REVISION, 'dimensions': 256, 'clip_count': 1,
            'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name,data in files.items()}}
        (state/cosmos.GENERATION_FILE).write_text(json.dumps(marker))
    return files


@pytest.mark.parametrize('failure', [*cosmos.GENERATION_FILES, cosmos.GENERATION_FILE])
def test_interrupted_publication_preserves_old_index_on_cold_start(tmp_path, monkeypatch, failure):
    old = merge(tmp_path, 1)
    paths = cosmos.generation_paths(tmp_path)
    old_bytes = {name: path.read_bytes() for name,path in paths.items()}
    original = cosmos._atomic_write
    def interrupted(path, content):
        if path.name == failure: raise OSError('fixture crash')
        original(path, content)
    monkeypatch.setattr(cosmos, '_atomic_write', interrupted)
    with pytest.raises(OSError): merge(tmp_path, 2)
    fresh = cosmos.CosmosRetrievalIndex(tmp_path)
    assert len(fresh.clips) == 1 and fresh.embeddings[0, 1] == 1
    assert json.loads((tmp_path/cosmos.GENERATION_FILE).read_text())['generation_id'] == old['generation_id']
    assert {name:path.read_bytes() for name,path in paths.items()} == old_bytes
    monkeypatch.setattr(cosmos, '_atomic_write', original)
    assert merge(tmp_path, 2)['clip_count'] == 2


@pytest.mark.parametrize('marked', [True, False])
def test_additive_migration_keeps_legacy_root_bytes_and_all_old_records(tmp_path, marked):
    originals = legacy(tmp_path, marked)
    index = cosmos.CosmosRetrievalIndex(tmp_path)
    old_record = copy.deepcopy(index.clips[0])
    result = merge(tmp_path, 1)
    assert result['clip_count'] == 2
    assert index.refresh()
    assert index.clips[0] == old_record
    assert {name:(tmp_path/name).read_bytes() for name in originals} == originals
    marker = json.loads((tmp_path/cosmos.GENERATION_FILE).read_text())
    assert marker['schema_version'] == 2
    assert cosmos.generation_paths(tmp_path)['clips.json'].parent.name == result['generation_id']


def test_exact_retry_preserves_generation_and_conflicting_rows_or_vectors_reject(tmp_path):
    first = merge(tmp_path, 1)
    marker = (tmp_path/cosmos.GENERATION_FILE).read_bytes()
    assert merge(tmp_path, 1) == {**first, 'added': 0, 'changed': False}
    assert (tmp_path/cosmos.GENERATION_FILE).read_bytes() == marker
    records, vectors, assets = incoming(1)
    vectors[0] = np.roll(vectors[0], 1)
    with pytest.raises(ValueError, match='replacement'):
        cosmos.merge_index(tmp_path, records, vectors, asset_metadata=assets)
    records[0]['camera'] = 'Changed label'
    with pytest.raises(ValueError, match='replacement'):
        cosmos.merge_index(tmp_path, records, vectors, asset_metadata=assets)
    assert (tmp_path/cosmos.GENERATION_FILE).read_bytes() == marker


def test_explicit_replacement_requires_current_generation_and_preserves_source_identity(tmp_path):
    first = merge(tmp_path, 1)
    records, vectors, assets = incoming(1)
    vectors[0] = np.roll(vectors[0], 1)
    args = dict(asset_metadata=assets, replace_clip_ids=[records[0]['id']])
    with pytest.raises(ValueError, match='expected generation'):
        cosmos.merge_index(tmp_path, records, vectors, **args)
    newer = merge(tmp_path, 2)
    with pytest.raises(ValueError, match='changed'):
        cosmos.merge_index(tmp_path, records, vectors, expected_generation=first['generation_id'], **args)
    changed = cosmos.merge_index(tmp_path, records, vectors, expected_generation=newer['generation_id'], **args)
    assert changed['added'] == 0 and changed['replaced'] == 1 and changed['clip_count'] == 2
    assert cosmos.CosmosRetrievalIndex(tmp_path).embeddings[0, 2] == 1
    assets[records[0]['video_id']]['original_sha256'] = 'f'*64
    with pytest.raises(ValueError, match='immutable'):
        cosmos.merge_index(tmp_path, records, vectors, expected_generation=changed['generation_id'], **args)


def test_new_interval_cannot_change_existing_original_provenance(tmp_path):
    merge(tmp_path, 1)
    records, vectors, assets = incoming(1)
    records[0].update(id=records[0]['id']+'-next', start=2., end=4.)
    assets[records[0]['video_id']]['object_id'] = 'object-'+'f'*32
    with pytest.raises(ValueError, match='immutable'):
        cosmos.merge_index(tmp_path, records, vectors, asset_metadata=assets)
    assert len(cosmos.CosmosRetrievalIndex(tmp_path).clips) == 1


def test_concurrent_additive_imports_keep_old_a_b_and_exact_retries(tmp_path):
    merge(tmp_path, 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: merge(tmp_path, n), [2, 3, 2, 3]))
    index = cosmos.CosmosRetrievalIndex(tmp_path)
    assert {clip['video_id'] for clip in index.clips} == {incoming(n)[0][0]['video_id'] for n in (1, 2, 3)}
    assert sum(result['added'] for result in results) == 2
    assert len(index.clips) == 3


def test_separate_process_publishers_preserve_all_imports(tmp_path):
    merge(tmp_path, 1)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('fork')) as pool:
        results = list(pool.map(merge, [tmp_path, tmp_path], [2, 3]))
    assert sorted(result['clip_count'] for result in results) == [2, 3]
    assert len(cosmos.CosmosRetrievalIndex(tmp_path).clips) == 3


@pytest.mark.parametrize('change', ['foreign_collection', 'invented_asset', 'wrong_source', 'missing_hash', 'zero_vector', 'nan_vector'])
def test_invalid_worker_or_host_provenance_never_creates_marker(tmp_path, change):
    records, vectors, assets = incoming(1)
    metadata = assets[records[0]['video_id']]
    if change == 'foreign_collection': metadata['collection'] = 'warehouse'
    elif change == 'invented_asset': metadata['asset_id'] = 'model-invented'
    elif change == 'wrong_source': metadata['source_id'] = '../../other'
    elif change == 'missing_hash': records[0].pop('embedded_clip_sha256')
    elif change == 'zero_vector': vectors[:] = 0
    elif change == 'nan_vector': vectors[0, 0] = np.nan
    with pytest.raises(ValueError): cosmos.merge_index(tmp_path, records, vectors, asset_metadata=assets)
    assert not (tmp_path/cosmos.GENERATION_FILE).exists()


def test_pointer_path_cannot_escape_immutable_generation_root(tmp_path):
    merge(tmp_path, 1)
    marker = json.loads((tmp_path/cosmos.GENERATION_FILE).read_text())
    marker['generation_id'] = '../../elsewhere'
    (tmp_path/cosmos.GENERATION_FILE).write_text(json.dumps(marker))
    with pytest.raises(ValueError, match='generation ID'): cosmos.CosmosRetrievalIndex(tmp_path)


def test_pinned_arrays_and_files_remain_valid_after_successful_addition(tmp_path):
    merge(tmp_path, 1)
    index = cosmos.CosmosRetrievalIndex(tmp_path)
    pinned = copy.copy(index)
    paths = cosmos.generation_paths(tmp_path)
    matrix = pinned.embeddings
    merge(tmp_path, 2)
    assert index.refresh()
    assert len(index.clips) == 2 and len(pinned.clips) == 1
    assert pinned.embeddings is matrix and pinned.embeddings[0, 1] == 1
    assert all(path.is_file() for path in paths.values())


@pytest.mark.parametrize('phase', ['generation_sync', 'parent_sync', 'state_before_pointer', 'state_after_pointer'])
def test_directory_sync_interruption_always_leaves_a_complete_committed_view(tmp_path, monkeypatch, phase):
    merge(tmp_path, 1)
    original = cosmos._sync_directory
    state_syncs = 0
    def interrupted(path):
        nonlocal state_syncs
        if path == tmp_path: state_syncs += 1
        if ((phase == 'generation_sync' and path.parent.name == cosmos.GENERATIONS_DIRECTORY)
                or (phase == 'parent_sync' and path.name == cosmos.GENERATIONS_DIRECTORY)
                or (phase == 'state_before_pointer' and path == tmp_path and state_syncs == 1)
                or (phase == 'state_after_pointer' and path == tmp_path and state_syncs == 2)):
            raise OSError('fixture interrupted directory synchronization')
        original(path)
    monkeypatch.setattr(cosmos, '_sync_directory', interrupted)
    with pytest.raises(OSError): merge(tmp_path, 2)
    fresh = cosmos.CosmosRetrievalIndex(tmp_path)
    assert len(fresh.clips) == (2 if phase == 'state_after_pointer' else 1)
    monkeypatch.setattr(cosmos, '_sync_directory', original)
    resumed = merge(tmp_path, 2)
    assert resumed['clip_count'] == 2
    assert resumed['changed'] is (phase != 'state_after_pointer')


def test_qdrant_pinned_collection_still_serves_old_view_after_additive_refresh(tmp_path):
    from qdrant_retrieval import QdrantRetrievalIndex
    from test_qdrant_retrieval import FakeQdrant
    merge(tmp_path, 1)
    api = FakeQdrant()
    index = QdrantRetrievalIndex(tmp_path, client=api)
    pinned = copy.copy(index)
    old_collection = pinned.collection
    merge(tmp_path, 2)
    assert index.refresh()
    assert index.collection != old_collection
    assert pinned.collection == old_collection
    vector = incoming(1)[1][0]
    assert len(pinned.search_vector(vector, dataset='cafe')) == 1
    assert len(index.search_vector(vector, dataset='cafe')) == 2
