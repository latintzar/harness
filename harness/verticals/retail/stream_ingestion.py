"""Durable stream windows shared by live adapters and recorded-dataset replay.

The host selects tenant/owner and registers source bytes before admission. Model
output supplies observations only. Windows, events and downstream work retain
independent source clocks and arrival clocks; replay never invents capture dates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from monitoring import EventDefinition

PRODUCT_EVENTS = frozenset({
    'pick_up', 'pickup', 'picked_up_item', 'touch', 'touch_product',
    'put_back', 'put_back_item', 'put_down', 'place_in_basket',
    'examine_object', 'examine_item', 'inspect_product', 'rotate_item',
    'hand_over', 'carry', 'drop_item', 'conceal_item', 'remove_packaging',
    'exit_with_item', 'place_elsewhere',
})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class VisionItemClaim(BaseModel):
    """Untrusted model estimates; authority comes from the host's raw response receipt."""
    model_config = ConfigDict(extra='forbid')
    event_key: str = Field(min_length=1, max_length=160)
    object_ref: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=1000, strict=True)
    relation: Literal['first_observation', 'same_item', 'distinct_item', 'unresolved']
    prior_event_key: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode='after')
    def explicit_association(self):
        if self.relation in {'same_item', 'distinct_item'} and not self.prior_event_key:
            raise ValueError('Reference the earlier observation for this item association.')
        if self.relation in {'first_observation', 'unresolved'} and self.prior_event_key:
            raise ValueError('Use an explicit association type with an earlier observation.')
        return self


class StreamWindow(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    stream_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$')
    source_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$')
    profile_id: str
    profile_revision: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    observation_origin: Literal['pipeline_observation', 'dataset_reference'] = 'pipeline_observation'
    input_kind: Literal['live', 'dataset_replay']
    observations: list[EventDefinition] = Field(default_factory=list, max_length=500)
    item_claims: list[VisionItemClaim] = Field(default_factory=list, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    sampling: str = Field(min_length=1, max_length=200)
    processing_seconds: float = Field(ge=0)

    @model_validator(mode='after')
    def intervals(self):
        if self.end_s <= self.start_s or self.end_s - self.start_s > 60:
            raise ValueError('Supply a source window of at most 60 seconds.')
        keys = set()
        for event in self.observations:
            if (event.source.clip_id != self.source_id or event.profile_id != self.profile_id
                    or event.profile_revision != self.profile_revision
                    or event.source.start_s < self.start_s or event.source.end_s > self.end_s
                    or event.event_key in keys):
                raise ValueError('Keep distinct observations within their source window and profile.')
            keys.add(event.event_key)
        by_key = {event.event_key: event for event in self.observations}
        claimed = set()
        for claim in self.item_claims:
            event = by_key.get(claim.event_key)
            if (not event or not event.track_id or claim.object_ref != event.object_ref
                    or claim.event_key in claimed or self.observation_origin != 'pipeline_observation'):
                raise ValueError('Bind item estimates to distinct primary tracked observations.')
            claimed.add(claim.event_key)
        return self


class StreamIngestion:
    def __init__(self, state, monitoring, *, clock=time.time):
        self.path = Path(state) / 'stream-ingestion.sqlite'
        self.monitoring, self.clock = monitoring, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS stream_windows (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, stream TEXT NOT NULL,
                    source TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL, received REAL NOT NULL,
                    status TEXT NOT NULL, event_ids TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS stream_model_outputs (
                    window_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, response_sha256 TEXT NOT NULL, raw TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stream_response_adapters (
                    window_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS stream_cursor ON stream_windows(owner,stream,source,end);
                CREATE TABLE IF NOT EXISTS stream_work (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, window_id TEXT NOT NULL,
                    event_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, result TEXT);
            ''')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _authorize(self, owner, spec):
        profile = self.monitoring.get_profile(owner, spec.profile_id, revision=spec.profile_revision)
        source = self.monitoring.source(owner, spec.source_id, profile['dataset'])
        if spec.start_s < source.get('start', 0) or spec.end_s > source['end']:
            raise ValueError('Keep the window within its registered source.')
        if profile['camera_ids'] and source.get('camera_id') not in profile['camera_ids']:
            raise ValueError('Use the profile’s authorized camera.')
        vocabulary = {row['type'] for row in profile['vocabulary']}
        if any(event.type not in vocabulary for event in spec.observations):
            raise ValueError('Use the saved profile vocabulary.')
        return source

    def accept_model_response(self, owner, context, raw_response, *, adapter_receipt=None):
        """Host adapter for the actual JSON response, without model-supplied trust flags.

        Context is host-owned source/profile/window/model configuration. Preserve
        the exact response bytes before producing qualified item estimates.
        """
        if not isinstance(raw_response, str) or len(raw_response.encode()) > 2_000_000:
            raise ValueError('Supply a bounded original structured model response.')
        response = json.loads(raw_response)
        if not isinstance(response, dict) or set(response) != {'observations', 'item_claims'}:
            raise ValueError('Return observations and explicit item_claims in the structured response.')
        if 'observations' in context or 'item_claims' in context:
            raise ValueError('Keep observations in their original response, separate from host context.')
        return self.accept(owner, {**context, **response}, _model_response=raw_response, _adapter_receipt=adapter_receipt)

    def model_claim(self, owner, event_id):
        """Read a committed claim with current source authorization and exact raw bytes."""
        event = self.monitoring.get_event(owner, event_id)
        with self.connect() as db:
            rows = db.execute("SELECT w.*,m.source_sha256,m.response_sha256,m.raw FROM stream_windows w JOIN stream_model_outputs m ON m.window_id=w.id WHERE w.owner=? AND w.status='committed' AND w.source=?", (owner, event['source']['clip_id'])).fetchall()
        for row in rows:
            if event_id not in json.loads(row['event_ids']):
                continue
            spec = StreamWindow.model_validate_json(row['body'])
            source = self._authorize(owner, spec)
            if (source.get('sha256') != row['source_sha256']
                    or hashlib.sha256(row['raw'].encode()).hexdigest() != row['response_sha256']):
                raise ValueError('Preserve the original source and model response bytes.')
            claim = next((c for c in spec.item_claims if c.event_key == event['event_key']), None)
            if claim:
                with self.connect() as db:
                    adapter = db.execute('SELECT receipt_sha256 FROM stream_response_adapters WHERE owner=? AND window_id=?', (owner, row['id'])).fetchone()
                return {'adapter_receipt_sha256': adapter['receipt_sha256'] if adapter else None, 'claim': claim.model_dump(), 'window_id': row['id'],
                    'start_s': spec.start_s, 'end_s': spec.end_s, 'model': spec.model,
                    'profile_id': spec.profile_id, 'profile_revision': spec.profile_revision,
                    'source_sha256': row['source_sha256'], 'response_sha256': row['response_sha256']}
        return None

    def accept(self, owner, window, *, _model_response=None, _adapter_receipt=None):
        """Recoverable inbox → immutable events → durable downstream work.

        Repeating identical input after a crash repairs incomplete effects. Each
        effect uses its event identity as the idempotency key. Changed retries
        are rejected rather than silently changing the historical observation.
        """
        owner = self.monitoring.owner(owner)
        spec = StreamWindow.model_validate(window)
        source = self._authorize(owner, spec)
        serialized = spec.model_dump()
        # Keep fingerprints of pre-basket windows stable on an identical retry.
        if not spec.item_claims:
            serialized.pop('item_claims')
        body = canonical(serialized)
        if spec.item_claims and _model_response is None:
            raise ValueError('Admit item estimates through the original model response adapter.')
        if _model_response is not None:
            original = json.loads(_model_response)
            # Schema defaults may be omitted in the original response; validation
            # supplies them identically without rewriting the original receipt.
            parsed = StreamWindow.model_validate({**serialized, **original})
            if parsed != spec or set(original) != {'observations', 'item_claims'}:
                raise ValueError('Bind the exact structured response to this source window.')
            if not source.get('sha256'):
                raise ValueError('Register the source bytes before admitting model item estimates.')
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        key = hashlib.sha256(canonical([owner, spec.stream_id, spec.source_id,
            spec.profile_id, spec.profile_revision, spec.start_s, spec.end_s]).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM stream_windows WHERE id=?', (key,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('Preserve the original stream window on retry.')
            else:
                last = db.execute('SELECT end,status FROM stream_windows WHERE owner=? AND stream=? AND source=? ORDER BY end DESC LIMIT 1',
                    (owner, spec.stream_id, spec.source_id)).fetchone()
                expected = last['end'] if last else source.get('start', 0)
                if (last and last['status'] != 'committed') or abs(spec.start_s - expected) > 1e-6:
                    raise ValueError('Resume this source at its next committed window boundary.')
                db.execute('INSERT INTO stream_windows(id,owner,stream,source,start,end,fingerprint,body,received,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (key, owner, spec.stream_id, spec.source_id, spec.start_s, spec.end_s,
                     fingerprint, body, self.clock(), 'pending'))
            if _model_response is not None:
                raw_sha = hashlib.sha256(_model_response.encode()).hexdigest()
                old = db.execute('SELECT * FROM stream_model_outputs WHERE window_id=?', (key,)).fetchone()
                if old and (old['response_sha256'] != raw_sha or old['source_sha256'] != source['sha256']):
                    raise ValueError('Preserve the original model response on retry.')
                db.execute('INSERT OR IGNORE INTO stream_model_outputs VALUES(?,?,?,?,?)',
                    (key, owner, source['sha256'], raw_sha, _model_response))
            if _adapter_receipt is not None:
                if _model_response is None:
                    raise ValueError('Bind the native adapter receipt to a model response.')
                adapter_body = canonical(_adapter_receipt)
                if len(adapter_body.encode()) > 4_000_000:
                    raise ValueError('Keep the native response receipt bounded.')
                adapter_sha = hashlib.sha256(adapter_body.encode()).hexdigest()
                old = db.execute('SELECT receipt_sha256 FROM stream_response_adapters WHERE window_id=?', (key,)).fetchone()
                if old and old['receipt_sha256'] != adapter_sha:
                    raise ValueError('Preserve the original native response and input trace.')
                db.execute('INSERT OR IGNORE INTO stream_response_adapters VALUES(?,?,?,?)', (key, owner, adapter_sha, adapter_body))
        event_ids = []
        observations = sorted(spec.observations, key=lambda e: (e.source.end_s, e.source.start_s, e.event_key)) if spec.item_claims else spec.observations
        for event in observations:
            saved = self.monitoring.commit_event(owner, event.model_dump(), origin=spec.observation_origin)
            event_ids.append(saved['id'])
            kinds = ['project_event']
            if any(claim.event_key == event.event_key for claim in spec.item_claims):
                kinds.append('project_basket')
            if event.type in PRODUCT_EVENTS and spec.observation_origin == 'pipeline_observation':
                kinds.append('inspect_product')
            for kind in kinds:
                work_id = hashlib.sha256(canonical([owner, saved['id'], kind]).encode()).hexdigest()
                payload = {'event_id': saved['id'], 'source_id': spec.source_id,
                    'stream_id': spec.stream_id, 'source_time_s': event.source.start_s,
                    'object_bbox': event.bbox, 'object_description': event.object_description,
                    'source_sha256': source.get('sha256'), 'input_kind': spec.input_kind, 'observation_origin': spec.observation_origin}
                with self.connect() as db:
                    db.execute('INSERT OR IGNORE INTO stream_work(id,owner,window_id,event_id,kind,body,status) VALUES(?,?,?,?,?,?,?)',
                        (work_id, owner, key, saved['id'], kind, canonical(payload), 'pending'))
        self._authorize(owner, spec)
        with self.connect() as db:
            db.execute('UPDATE stream_windows SET status=?,event_ids=? WHERE id=?',
                ('committed', canonical(event_ids), key))
        return {'window_id': key, 'status': 'committed', 'event_ids': event_ids,
            'source_end_s': spec.end_s, 'input_kind': spec.input_kind}

    def history(self, owner, stream_id, *, limit=200):
        owner = self.monitoring.owner(owner)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Choose a bounded history length.')
        with self.connect() as db:
            rows = db.execute('SELECT * FROM stream_windows WHERE owner=? AND stream=? ORDER BY received,id LIMIT ?',
                (owner, stream_id, limit)).fetchall()
        result = []
        for row in rows:
            try:
                spec = StreamWindow.model_validate_json(row['body'])
                self._authorize(owner, spec)
            except ValueError:
                continue
            result.append({'id': row['id'], 'source_id': row['source'], 'start_s': row['start'],
                'end_s': row['end'], 'received_at': row['received'], 'status': row['status'],
                'input_kind': spec.input_kind, 'observation_origin': spec.observation_origin,
                'event_ids': json.loads(row['event_ids']),
                'model': spec.model, 'sampling': spec.sampling,
                'processing_seconds': spec.processing_seconds})
        return result

    def drain(self, owner, handlers, *, limit=20):
        """Run bounded host handlers; failed remote work stays explicit for review.

        A handler receives the immutable event, payload and stable job ID. Model
        transports must use their own funded/idempotent receipt for that job ID.
        """
        owner = self.monitoring.owner(owner)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Choose a bounded work batch.')
        processed = []
        for _ in range(limit):
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # A lost worker may have reached a paid provider. Preserve that
                # uncertainty for reconciliation instead of silently billing twice.
                db.execute("UPDATE stream_work SET status='interrupted',result=?,lease_until=0 WHERE owner=? AND status='running' AND lease_until<?",
                    (canonical({'reason': 'Review the interrupted processing receipt.'}), owner, self.clock()))
                rows = db.execute("SELECT work.* FROM stream_work work JOIN stream_windows window ON window.id=work.window_id WHERE work.owner=? AND work.status='pending' AND window.status='committed' ORDER BY work.rowid LIMIT 500", (owner,)).fetchall()
                row = next((r for r in rows if r['kind'] in handlers), None)
                if row is None:
                    break
                db.execute("UPDATE stream_work SET status='running',lease_until=?,attempts=attempts+1 WHERE id=?",
                    (self.clock() + 120, row['id']))
            try:
                event = self.monitoring.get_event(owner, row['event_id'])
                if row['kind'] == 'inspect_product' and (event.get('origin') == 'dataset_reference'
                        or event.get('review_state') == 'publisher_annotation'):
                    result = canonical({'reason': 'Evaluation reference retained separately.'})
                    status = 'skipped'
                else:
                    result = handlers[row['kind']](event, json.loads(row['body']), row['id'])
                    self.monitoring.get_event(owner, row['event_id'])
                    result = canonical(result)
                    status = 'done'
            except Exception:
                # Keep errors free of provider payloads, credentials and media.
                result, status = canonical({'reason': 'Processing needs review.'}), 'failed'
            with self.connect() as db:
                db.execute('UPDATE stream_work SET status=?,result=?,lease_until=0 WHERE id=?', (status, result, row['id']))
            processed.append({'id': row['id'], 'kind': row['kind'], 'status': status})
        return processed
