"""Persistent local Qdrant search over the verified Cosmos embedding space.

The original matrix remains available for complete-coverage scoring and discovery.
Search uses Qdrant; a provider failure is surfaced instead of changing backends.
"""
from __future__ import annotations
from retail_settings import value as setting

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

import numpy as np

from cosmos_retrieval import (CosmosRetrievalIndex, DIMENSIONS, GENERATION_FILE, MODEL_ID,
                              MODEL_REVISION, _atomic_write, _file_identity)
from retrieval import DEFAULT_STATE
from workspace_identity import workspace_scope

DEFAULT_URL = 'http://127.0.0.1:8769'
POINT_NAMESPACE = uuid.UUID('0d55c19f-5739-45bf-a92d-f0806e7345f9')


def point_id(clip_id):
    return str(uuid.uuid5(POINT_NAMESPACE, clip_id))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Local Qdrant must respond directly on its configured loopback address')


class LocalQdrant:
    """Small standard-library REST transport, pinned to an explicit loopback service."""
    def __init__(self, url=DEFAULT_URL, timeout=15):
        parts = urlsplit(url)
        if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', '::1'} or parts.username
                or parts.password or parts.path not in {'', '/'} or parts.query or parts.fragment):
            raise ValueError('Qdrant requires an explicit HTTP loopback address without credentials or a path')
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('Qdrant request timeout must be between zero and sixty seconds')
        self.url, self.timeout = url.rstrip('/'), timeout
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method, path, body=None, allow_missing=False):
        data = None if body is None else json.dumps(body, allow_nan=False, separators=(',', ':')).encode()
        request = Request(self.url + path, data=data, method=method,
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            raise ValueError(f'Local Qdrant returned HTTP {exc.code} for {method} {path}') from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ValueError('Local Qdrant is unavailable; start the documented development service') from exc
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Qdrant response exceeded the bounded response size')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Qdrant returned an invalid JSON response') from exc
        if payload.get('status') != 'ok':
            raise ValueError('Qdrant did not confirm successful completion')
        return payload.get('result')


class QdrantRetrievalIndex(CosmosRetrievalIndex):
    """One tenant/model alias with immutable generation collections and scoped queries.

    Canonical state identity is selected by the host, never supplied by an agent tool.
    A shallow copied index keeps its concrete generation and always filters to its own
    current clip IDs, including narrowed investigation and incremental-watch views.
    """
    def __init__(self, state=DEFAULT_STATE / 'cosmos', *, url=None, client=None):
        self.tenant_id = workspace_scope(state)
        model_key = hashlib.sha256(f'{MODEL_ID}:{MODEL_REVISION}:{DIMENSIONS}'.encode()).hexdigest()[:12]
        self.alias = f'rocoto_video_{self.tenant_id[:20]}_{model_key}'
        self.client = client or LocalQdrant(url or setting('ROCOTO_RETAIL_QDRANT_URL', DEFAULT_URL))
        self.collection = None
        self.fingerprint = None
        self._qdrant_initializing = True
        super().__init__(state)
        self._qdrant_initializing = False
        with self._lock:
            self._publish_loaded()

    @classmethod
    def publish_snapshot(cls, state, snapshot, *, client, promote):
        """Publish a HOST-staged immutable snapshot without changing reader state.

        Caller holds SourceStore and Cosmos publication locks in that order. This
        short-lived publisher never loads a model or becomes a query index. Shared
        publication code verifies its concrete collection before alias + promote.
        """
        if not isinstance(snapshot,dict) or not callable(promote):
            raise ValueError('A staged snapshot and host promotion callback are required')
        publisher=cls.__new__(cls)
        publisher.state=Path(state)
        publisher.tenant_id=workspace_scope(state)
        model_key=hashlib.sha256(f'{MODEL_ID}:{MODEL_REVISION}:{DIMENSIONS}'.encode()).hexdigest()[:12]
        publisher.alias=f'rocoto_video_{publisher.tenant_id[:20]}_{model_key}'
        publisher.client=client
        publisher.clips=copy.deepcopy(snapshot['records'])
        publisher.embeddings=np.asarray(snapshot['vectors'],dtype=np.float32).copy()
        publisher.report=copy.deepcopy(snapshot['report'])
        marker=publisher.state/GENERATION_FILE
        publisher._generation_identity=_file_identity(marker) if marker.exists() else None
        publisher.collection=publisher.fingerprint=None
        publisher._publish_loaded(promote=promote)
        return {'generation_id':snapshot['marker']['generation_id'],
                'qdrant_collection':publisher.collection,'qdrant_fingerprint':publisher.fingerprint,
                'clip_count':len(publisher.clips),'profile':'verified_cosmos_qdrant'}

    def _loaded_fingerprint(self):
        if len(self.clips) != len(self.embeddings) or self.embeddings.shape != (len(self.clips), DIMENSIONS):
            raise ValueError('Qdrant needs matching Cosmos clip rows and 256-dimensional vectors')
        if len({c.get('id') for c in self.clips}) != len(self.clips):
            raise ValueError('Qdrant clip IDs must be unique')
        if any(not isinstance(c.get('id'), str) or not c['id'] or c.get('model') != MODEL_ID
               or c.get('model_revision') != MODEL_REVISION for c in self.clips):
            raise ValueError('Qdrant source metadata must match the pinned Cosmos checkpoint')
        norms = np.linalg.norm(self.embeddings, axis=1)
        if not np.isfinite(self.embeddings).all() or np.any(np.abs(norms - 1) > .001):
            raise ValueError('Qdrant accepts finite normalized Cosmos vectors only')
        digest = hashlib.sha256()
        digest.update(json.dumps({'model': MODEL_ID, 'revision': MODEL_REVISION, 'clips': self.clips,
                                  'report': self.report}, sort_keys=True, allow_nan=False,
                                 separators=(',', ':')).encode())
        digest.update(np.asarray(self.embeddings, dtype='<f4').tobytes())
        return digest.hexdigest()

    def _base_filter(self, fingerprint=None):
        return [
            {'key': 'tenant_id', 'match': {'value': self.tenant_id}},
            {'key': 'model_revision', 'match': {'value': MODEL_REVISION}},
            {'key': 'source_index_fingerprint', 'match': {'value': fingerprint or self.fingerprint}},
        ]

    def _publish_loaded(self, *, promote=None):
        if not self.clips:
            self.embeddings = np.empty((0, DIMENSIONS), dtype=np.float32)
            self.embeddings.flags.writeable = False
            return
        fingerprint = self._loaded_fingerprint()
        collection = f'{self.alias}_{fingerprint[:32]}'
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state / '.qdrant-publish.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._check_source_generation()
            info = self.client.request('GET', f'/collections/{collection}', allow_missing=True)
            if info is None:
                self.client.request('PUT', f'/collections/{collection}', {
                    'vectors': {'size': DIMENSIONS, 'distance': 'Cosine'},
                    'on_disk_payload': True,
                })
                for field in ['tenant_id', 'model_revision', 'source_index_fingerprint', 'dataset', 'camera', 'video_id']:
                    self.client.request('PUT', f'/collections/{collection}/index?wait=true',
                                        {'field_name': field, 'field_schema': 'keyword'})
            else:
                vectors = info.get('config', {}).get('params', {}).get('vectors', {})
                if vectors.get('size') != DIMENSIONS or vectors.get('distance') != 'Cosine':
                    raise ValueError('Existing Qdrant generation uses a different vector configuration')
            # Resume an interrupted staging import idempotently. Existing published generations
            # contain the same verified input, so these upserts preserve their point identities.
            for start in range(0, len(self.clips), 128):
                points = []
                for position in range(start, min(start + 128, len(self.clips))):
                    clip = self.clips[position]
                    payload = {k: clip.get(k) for k in ('id', 'dataset', 'camera', 'video_id', 'start', 'end',
                                                       'embedded_clip_sha256')}
                    payload.update(tenant_id=self.tenant_id, model=MODEL_ID, model_revision=MODEL_REVISION,
                                   source_index_fingerprint=fingerprint, source_row=position)
                    points.append({'id': point_id(clip['id']), 'vector': self.embeddings[position].tolist(),
                                   'payload': payload})
                result = self.client.request('PUT', f'/collections/{collection}/points?wait=true', {'points': points})
                if result.get('status') != 'completed':
                    raise ValueError('Qdrant import must complete before publication')
            total = self.client.request('POST', f'/collections/{collection}/points/count', {'exact': True})
            scoped = self.client.request('POST', f'/collections/{collection}/points/count', {
                'exact': True, 'filter': {'must': self._base_filter(fingerprint)}})
            if total.get('count') != len(self.clips) or scoped.get('count') != len(self.clips):
                raise ValueError('Qdrant generation coverage differs from the verified source index')
            self._check_source_generation()
            aliases = self.client.request('GET', '/aliases')['aliases']
            previous = next((item['collection_name'] for item in aliases if item['alias_name'] == self.alias), None)
            if previous != collection:
                actions = [] if previous is None else [{'delete_alias': {'alias_name': self.alias}}]
                actions.append({'create_alias': {'alias_name': self.alias, 'collection_name': collection}})
                self.client.request('POST', '/collections/aliases', {'actions': actions})
            confirmed=self.client.request('GET','/aliases')['aliases']
            if not any(item['alias_name']==self.alias and item['collection_name']==collection for item in confirmed):
                raise ValueError('Qdrant did not confirm the verified generation alias')
            if promote is not None: promote()
            self.collection, self.fingerprint = collection, fingerprint
            self.embeddings.flags.writeable = False
            _atomic_write(self.state / 'qdrant-index.json', json.dumps({
                'schema_version': 1, 'tenant_id': self.tenant_id, 'alias': self.alias, 'collection': collection,
                'source_index_fingerprint': fingerprint, 'model': MODEL_ID, 'model_revision': MODEL_REVISION,
                'dimensions': DIMENSIONS, 'clip_count': len(self.clips), 'distance': 'Cosine',
                'search_mode': 'exact', 'publication': 'verified_count_then_atomic_alias',
            }, indent=2).encode())

    def _check_source_generation(self):
        marker = self.state / GENERATION_FILE
        current=_file_identity(marker) if marker.exists() else None
        if current != self._generation_identity:
            raise ValueError('Source index changed during Qdrant publication; refresh the complete generation')

    def refresh(self):
        if self._qdrant_initializing:
            return super().refresh()
        with self._lock:
            old = self.clips, self.embeddings, self.report, self._generation_identity, self.collection, self.fingerprint
            try:
                changed = super().refresh()
                if changed:
                    self._publish_loaded()
                return changed
            except Exception:
                (self.clips, self.embeddings, self.report, self._generation_identity,
                 self.collection, self.fingerprint) = old
                raise

    def search_vector(self, vector, limit=6, dataset=None):
        """Search a matching text vector; also allows isolated lookup benchmarking."""
        with self._lock:
            eligible = [c for c in self.clips if dataset is None or c['dataset'] == dataset]
            if not eligible or limit <= 0:
                return []
            limit = min(int(limit), 50)
            vector = np.asarray(vector, dtype=np.float32)
            if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
                raise ValueError('Query must be a finite 256-dimensional Cosmos text vector')
            filters = self._base_filter()
            if dataset is not None:
                filters.append({'key': 'dataset', 'match': {'value': dataset}})
            filters.append({'has_id': [point_id(c['id']) for c in eligible]})
            result = self.client.request('POST', f'/collections/{self.collection}/points/query', {
                'query': vector.tolist(), 'filter': {'must': filters}, 'params': {'exact': True},
                'limit': len(eligible) if len(eligible) <= 256 else limit,
                'with_payload': True, 'with_vector': False,
            })
            known = {point_id(c['id']): (position, c) for position, c in enumerate(eligible)}
            found = []
            for hit in result['points']:
                if hit.get('id') not in known:
                    raise ValueError('Qdrant returned a point outside the host-selected source scope')
                payload = hit.get('payload', {})
                position, clip = known[hit['id']]
                if (payload.get('tenant_id') != self.tenant_id or payload.get('id') != clip['id']
                        or payload.get('model_revision') != MODEL_REVISION
                        or payload.get('source_index_fingerprint') != self.fingerprint):
                    raise ValueError('Qdrant result provenance differs from the loaded source index')
                score = float(hit['score'])
                if not math.isfinite(score):
                    raise ValueError('Qdrant returned a nonfinite score')
                found.append((score, position, clip))
            found.sort(key=lambda row: (-row[0], row[1]))
            results = []
            for score, _, clip in found[:limit]:
                item = {k: clip[k] for k in ('id', 'video_id', 'dataset', 'camera', 'start', 'end', 'sample_time')}
                item.update(score=round(score, 6), model=MODEL_ID, verification='candidate',
                            thumbnail=f"/thumbnails/{clip['id']}.jpg", thumbnail_url=f"/thumbnails/{clip['id']}.jpg",
                            source_url=f"/?clip={clip['id']}", video_url=f"/clips/{clip['id']}.mp4", playback_start=0)
                results.append(item)
            return results

    def search(self, query, limit=6, dataset=None):
        query = query.strip()
        with self._lock:
            if not query or limit <= 0 or not any(dataset is None or c['dataset'] == dataset for c in self.clips):
                return []
            return self.search_vector(self._encode_text(query), limit, dataset)

    def status(self):
        with self._lock:
            return {**super().status(), 'vector_backend': 'qdrant', 'vector_distance': 'Cosine',
                    'vector_search_mode': 'exact', 'qdrant_collection': self.collection,
                    'source_index_fingerprint': self.fingerprint,
                    'full_coverage_scoring_backend': 'numpy', 'query_provider_spend_usd': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE / 'cosmos')
    parser.add_argument('--url', default=None)
    parser.add_argument('--query')
    parser.add_argument('--dataset')
    parser.add_argument('--limit', type=int, default=6)
    args = parser.parse_args()
    index = QdrantRetrievalIndex(args.state, url=args.url)
    print(json.dumps(index.search(args.query, args.limit, args.dataset) if args.query else index.status(), indent=2))


if __name__ == '__main__':
    main()
