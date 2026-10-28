"""CPU-only contract tests for persistent, scoped Cosmos retrieval."""
import copy
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1]))
from cosmos_retrieval import MODEL_ID, MODEL_REVISION, _publish_generation, generation_paths
from qdrant_retrieval import QdrantRetrievalIndex, LocalQdrant, point_id


class FakeQdrant:
    def __init__(self):
        self.collections = {}
        self.aliases = {}
        self.calls = []
        self.fail_upsert = False
        self.fail_query = False

    def request(self, method, path, body=None, allow_missing=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        parts = path.split('?')[0].strip('/').split('/')
        if path == '/aliases':
            return {'aliases': [{'alias_name': k, 'collection_name': v} for k, v in self.aliases.items()]}
        if path == '/collections/aliases':
            for action in body['actions']:
                if 'delete_alias' in action:
                    self.aliases.pop(action['delete_alias']['alias_name'])
                if 'create_alias' in action:
                    item = action['create_alias']
                    self.aliases[item['alias_name']] = item['collection_name']
            return True
        name = parts[1]
        if len(parts) == 2:
            if method == 'PUT':
                self.collections[name] = {'points': {}, 'config': body}
                return True
            if name not in self.collections:
                if allow_missing:
                    return None
                raise ValueError('missing')
            item = self.collections[name]
            return {'config': {'params': item['config']}, 'points_count': len(item['points'])}
        item = self.collections[name]
        if parts[2] == 'index':
            return {'status': 'completed'}
        if parts[2:] == ['points']:
            if self.fail_upsert:
                raise ValueError('upsert failed')
            for point in body['points']:
                item['points'][point['id']] = point
            return {'status': 'completed'}
        points = list(item['points'].values())
        for condition in body.get('filter', {}).get('must', []):
            if 'has_id' in condition:
                points = [p for p in points if p['id'] in condition['has_id']]
            else:
                points = [p for p in points if p['payload'].get(condition['key']) == condition['match']['value']]
        if parts[-1] == 'count':
            return {'count': len(points)}
        if parts[-1] == 'query':
            if self.fail_query:
                raise ValueError('query unavailable')
            vector = np.asarray(body['query'])
            found = [{'id': p['id'], 'payload': p['payload'], 'score': float(np.dot(p['vector'], vector))}
                     for p in points]
            return {'points': sorted(found, key=lambda p: -p['score'])[:body['limit']]}
        raise AssertionError((method, path))


def publish(root, suffix='', vectors=None):
    root.mkdir(parents=True, exist_ok=True)
    records = [{'id': f'clip-{i}{suffix}', 'video_id': f'video-{i}', 'dataset': 'cafe' if i < 2 else 'warehouse',
                'camera': f'camera-{i}', 'start': 0., 'end': 8., 'sample_time': 4.,
                'model': MODEL_ID, 'model_revision': MODEL_REVISION, 'embedded_clip_sha256': str(i) * 64}
               for i in range(3)]
    if vectors is None:
        vectors = np.zeros((3, 256), dtype=np.float32)
        vectors[0, 0], vectors[1, 1], vectors[2, 2] = 1, 1, 1
    report = {'model': MODEL_ID, 'model_revision': MODEL_REVISION, 'embedding_dimensions': 256, 'clip_count': 3}
    _publish_generation(root, records, vectors, report)


class QdrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = FakeQdrant()

    def tearDown(self):
        self.tmp.cleanup()

    def index(self, directory='a'):
        state = self.root / directory
        publish(state)
        index = QdrantRetrievalIndex(state, client=self.api)
        index._encode_text = lambda _: np.eye(1, 256, dtype=np.float32)[0]
        return index

    def test_stages_points_before_atomic_alias_and_retains_readonly_vectors(self):
        index = self.index()
        paths = [call[1] for call in self.api.calls]
        self.assertLess(next(i for i, p in enumerate(paths) if '/points?' in p), paths.index('/collections/aliases'))
        self.assertEqual(self.api.aliases[index.alias], index.collection)
        self.assertFalse(index.embeddings.flags.writeable)
        self.assertEqual(len(self.api.collections[index.collection]['points']), 3)
        payload = next(iter(self.api.collections[index.collection]['points'].values()))['payload']
        self.assertEqual(payload['source_index_fingerprint'], index.fingerprint)
        self.assertEqual(payload['model_revision'], MODEL_REVISION)

    def test_dataset_filter_and_public_result_match_cosmos_shape(self):
        index = self.index()
        hits = index.search('coffee', dataset='cafe')
        self.assertEqual([hit['id'] for hit in hits], ['clip-0', 'clip-1'])
        self.assertEqual(hits[0]['model'], MODEL_ID)
        self.assertEqual(hits[0]['video_url'], '/clips/clip-0.mp4')
        self.assertNotIn('path', hits[0])
        request = self.api.calls[-1][2]
        self.assertIn({'key': 'dataset', 'match': {'value': 'cafe'}}, request['filter']['must'])

    def test_shallow_copied_scope_cannot_return_other_clip_ids(self):
        index = self.index()
        restricted = copy.copy(index)
        restricted.clips = [index.clips[1]]
        restricted.embeddings = index.embeddings[[1]]
        self.assertEqual([h['id'] for h in restricted.search('coffee')], ['clip-1'])
        filters = self.api.calls[-1][2]['filter']['must']
        self.assertIn({'has_id': [point_id('clip-1')]}, filters)
        self.assertEqual(len(index.clips), 3)

    def test_tenants_have_distinct_collections_even_with_identical_clip_ids(self):
        a, b = self.index('a'), self.index('b')
        self.assertNotEqual(a.alias, b.alias)
        self.assertNotEqual(a.collection, b.collection)
        self.assertNotEqual(a.tenant_id, b.tenant_id)
        a.search('coffee')
        self.assertIn({'key': 'tenant_id', 'match': {'value': a.tenant_id}}, self.api.calls[-1][2]['filter']['must'])

    def test_complete_workspace_move_preserves_scoped_alias_and_results(self):
        original = self.index('source')
        relocated = self.root / 'staging'
        shutil.copytree(original.state, relocated)
        moved = QdrantRetrievalIndex(relocated, client=self.api)
        moved._encode_text = original._encode_text
        self.assertEqual(original.tenant_id, moved.tenant_id)
        self.assertEqual(original.alias, moved.alias)
        self.assertEqual(original.collection, moved.collection)
        self.assertEqual(original.search('coffee'), moved.search('coffee'))

    def test_failed_publication_retains_old_complete_local_and_remote_generation(self):
        index = self.index()
        old = (index.collection, index.fingerprint, index.clips, index.embeddings)
        publish(index.state, '-new')
        self.api.fail_upsert = True
        with self.assertRaisesRegex(ValueError, 'upsert failed'):
            index.refresh()
        self.assertEqual(index.collection, old[0])
        self.assertEqual(index.fingerprint, old[1])
        self.assertIs(index.clips, old[2])
        self.assertIs(index.embeddings, old[3])
        self.assertEqual(self.api.aliases[index.alias], old[0])
        self.api.fail_upsert = False
        self.assertTrue(index.refresh())
        self.assertNotEqual(index.collection, old[0])
        self.assertIn(old[0], self.api.collections)

    def test_snapshot_queries_its_original_generation_after_alias_switch(self):
        index = self.index()
        snapshot = copy.copy(index)
        old = snapshot.collection
        publish(index.state, '-new')
        index.refresh()
        self.assertEqual(snapshot.search('coffee')[0]['id'], 'clip-0')
        self.assertIn(old, self.api.calls[-1][1])
        self.assertEqual(index.search('coffee')[0]['id'], 'clip-0-new')

    def test_qdrant_failure_is_reported_without_numpy_fallback(self):
        index = self.index()
        self.api.fail_query = True
        with self.assertRaisesRegex(ValueError, 'query unavailable'):
            index.search('coffee')

    def test_empty_company_never_reads_another_collection_or_calls_encoder(self):
        index = QdrantRetrievalIndex(self.root / 'empty', client=self.api)
        index._encode_text = lambda _: self.fail('empty company called encoder')
        self.assertEqual(index.search('coffee'), [])
        self.assertFalse(index.status()['ready'])
        self.assertEqual(self.api.calls, [])

    def test_changed_model_revision_and_invalid_vectors_fail_before_publish(self):
        state = self.root / 'bad'
        publish(state)
        report = generation_paths(state)['index-report.json']
        data = json.loads(report.read_text())
        data['model_revision'] = 'wrong'
        report.chmod(0o600)
        report.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            QdrantRetrievalIndex(state, client=self.api)
        self.assertEqual(self.api.calls, [])

    def test_transport_only_accepts_loopback_without_credentials_or_paths(self):
        for url in ['https://cloud.qdrant.io', 'http://localhost:8769', 'http://user:secret@127.0.0.1:8769',
                    'http://127.0.0.1:8769/path']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalQdrant(url)

    def test_stale_loader_cannot_repoint_alias_over_newer_source_generation(self):
        index = self.index()
        old = index.collection
        publish(index.state, '-new')
        with self.assertRaisesRegex(ValueError, 'changed during'):
            index._publish_loaded()
        self.assertEqual(self.api.aliases[index.alias], old)


@unittest.skipUnless(os.environ.get('ROCOTO_QDRANT_TEST_URL'), 'Set ROCOTO_QDRANT_TEST_URL for the local service proof')
class RealQdrantTests(unittest.TestCase):
    def test_real_service_tenant_vectors_scope_and_atomic_generation(self):
        # Unique fixture state means unique owned collections; existing collections are untouched.
        with tempfile.TemporaryDirectory(prefix='rocoto-qdrant-proof-') as tmp:
            root = Path(tmp)
            publish(root / 'tenant-a')
            opposite = np.zeros((3, 256), dtype=np.float32)
            opposite[0, 2], opposite[1, 0], opposite[2, 1] = 1, 1, 1
            publish(root / 'tenant-b', vectors=opposite)
            a = QdrantRetrievalIndex(root / 'tenant-a', url=os.environ['ROCOTO_QDRANT_TEST_URL'])
            b = QdrantRetrievalIndex(root / 'tenant-b', url=os.environ['ROCOTO_QDRANT_TEST_URL'])
            query = np.eye(1, 256, dtype=np.float32)[0]
            self.assertEqual(a.search_vector(query)[0]['id'], 'clip-0')
            self.assertEqual(b.search_vector(query)[0]['id'], 'clip-1')
            self.assertNotEqual(a.collection, b.collection)
            restricted = copy.copy(a)
            restricted.clips, restricted.embeddings = [a.clips[2]], a.embeddings[[2]]
            self.assertEqual([h['id'] for h in restricted.search_vector(query)], ['clip-2'])
            self.assertEqual(restricted.search_vector(query, dataset='cafe'), [])
            old = copy.copy(a)
            publish(a.state, '-new')
            self.assertTrue(a.refresh())
            self.assertEqual(a.search_vector(query)[0]['id'], 'clip-0-new')
            self.assertEqual(old.search_vector(query)[0]['id'], 'clip-0')


if __name__ == '__main__':
    unittest.main()
