"""Immutable copies of the exact JPEGs supplied to source inspection.

The host supplies company state and scope. Serving a returned frame additionally
requires current chat/source authorization by the HTTP route. This store never
opens a media path, decodes an image, or creates Gallery artifacts. Byte quotas
cover JPEG payloads; a receipt-count cap also bounds metadata growth.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

MAX_FRAMES = 4
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_COMPANY_BYTES = 256 * 1024 * 1024
MAX_RECEIPTS = 4096
MAX_METADATA_BYTES = 64 * 1024
_PUBLIC_KEYS = frozenset({'url', 'time', 'clip_id', 'sha256', 'basis', 'width', 'height'})
_REQUIRED_KEYS = frozenset({'url', 'time', 'clip_id', 'sha256'})
_URL = re.compile(r'/api/inspection-previews/([a-f0-9]{32})/([0-3])\.jpg')
_UNAVAILABLE = 'Inspection previews are temporarily unavailable.'


class PreviewError(ValueError):
    """Bounded validation, admission or private-storage failure."""


class _PrivateFrame(dict):
    def __repr__(self):
        return '<InspectionFrame private JPEG bytes>'


def _identifier(value, *, clip=False):
    pattern = r'[A-Za-z0-9_.:-]{1,256}' if clip else r'[A-Za-z0-9_-]{1,160}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or value in {'.', '..'}:
        raise PreviewError('Choose an authenticated inspection scope.')
    return value


def _metadata(frame):
    value = frame.get('time')
    if type(value) not in (int, float) or not 0 <= value <= 1e12 or not math.isfinite(value):
        raise PreviewError('Use the sampled frame timestamp.')
    result = {'time': float(value)}
    basis = frame.get('basis')
    if basis is not None:
        if (not isinstance(basis, str) or not 1 <= len(basis) <= 128
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in basis)):
            raise PreviewError('Use the sampled frame timing basis.')
        result['basis'] = basis
    for key in ('width', 'height'):
        value = frame.get(key)
        if value is not None:
            if type(value) is not int or not 1 <= value <= 65535:
                raise PreviewError('Use the sampled frame dimensions.')
            result[key] = value
    return result


def _jpeg(raw):
    # Decoder responsibility belongs to the media worker. Marker checks alone
    # deliberately do not claim that these bytes form a fully decoded JPEG.
    if (type(raw) is not bytes or not 5 <= len(raw) <= MAX_FRAME_BYTES
            or not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9')):
        raise PreviewError('Use the bounded JPEG bytes from source inspection.')
    return raw


def _current(guard):
    if guard is not None:
        try:
            if guard():
                return
        except Exception:
            pass
        raise PreviewError('The inspection preview admission has finished.')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Use unique metadata fields.')
        value[key] = item
    return value


class InspectionPreviews:
    def __init__(self, state, *, create=False):
        self.state = Path(state)
        self.path = self.state / 'inspection-previews.sqlite'
        if create:
            self._initialize()

    def __repr__(self):
        return '<InspectionPreviews private company store>'

    def _check_paths(self):
        if self.state.is_symlink():
            raise PreviewError(_UNAVAILABLE)
        for path in (self.path, Path(str(self.path) + '-journal'),
                Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm')):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PreviewError(_UNAVAILABLE)

    def _initialize(self):
        try:
            self._check_paths()
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PreviewError(_UNAVAILABLE)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS receipts (
                        id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        owner TEXT NOT NULL, chat_id TEXT NOT NULL, dataset TEXT NOT NULL,
                        clip_id TEXT NOT NULL, size_bytes INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS frames (
                        receipt_id TEXT NOT NULL REFERENCES receipts(id), position INTEGER NOT NULL,
                        metadata TEXT NOT NULL, sha256 TEXT NOT NULL, image BLOB NOT NULL,
                        PRIMARY KEY(receipt_id, position));
                    CREATE TRIGGER IF NOT EXISTS immutable_receipt_update BEFORE UPDATE ON receipts
                        BEGIN SELECT RAISE(ABORT,'Immutable inspection receipt'); END;
                    CREATE TRIGGER IF NOT EXISTS immutable_frame_update BEFORE UPDATE ON frames
                        BEGIN SELECT RAISE(ABORT,'Immutable inspection frame'); END;
                ''')
        except (OSError, sqlite3.Error):
            raise PreviewError(_UNAVAILABLE) from None

    @staticmethod
    def _descriptor(receipt_id, index, clip_id, metadata, sha256):
        return {'url': f'/api/inspection-previews/{receipt_id}/{index}.jpg',
            'clip_id': clip_id, 'sha256': sha256, **metadata}

    def save(self, *, owner_principal_id, chat_id, dataset, clip_id, frames, guard=None):
        """Atomically admit up to four copied frames; call from drained host offload."""
        owner = _identifier(owner_principal_id)
        chat_id, dataset, clip_id = _identifier(chat_id), _identifier(dataset), _identifier(clip_id, clip=True)
        if not isinstance(frames, (list, tuple)) or not 1 <= len(frames) <= MAX_FRAMES:
            raise PreviewError('Use one to four sampled inspection frames.')
        prepared = []
        total = 0
        for frame in frames:
            if not isinstance(frame, dict):
                raise PreviewError('Use the sampled inspection frames.')
            raw, metadata = _jpeg(frame.get('image_bytes')), _metadata(frame)
            total += len(raw)
            if total > MAX_RECEIPT_BYTES:
                raise PreviewError('The inspection previews reached their storage allowance.')
            prepared.append((raw, metadata, hashlib.sha256(raw).hexdigest()))
        fingerprint = hashlib.sha256(_canonical([owner, chat_id, dataset, clip_id,
            [[metadata, sha] for _, metadata, sha in prepared]]).encode()).hexdigest()
        _current(guard)
        self._initialize()
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                _current(guard)
                prior = db.execute('SELECT id FROM receipts WHERE fingerprint=?', (fingerprint,)).fetchone()
                if prior:
                    receipt_id = prior[0]
                else:
                    count, used = db.execute('SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM receipts').fetchone()
                    if count >= MAX_RECEIPTS or used + total > MAX_COMPANY_BYTES:
                        raise PreviewError('The inspection previews reached their storage allowance.')
                    receipt_id = uuid.uuid4().hex
                    db.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,?)',
                        (receipt_id, fingerprint, owner, chat_id, dataset, clip_id, total))
                    db.executemany('INSERT INTO frames VALUES (?,?,?,?,?)',
                        [(receipt_id, i, _canonical(meta), sha, raw)
                         for i, (raw, meta, sha) in enumerate(prepared)])
                _current(guard)
            return [self._descriptor(receipt_id, i, clip_id, meta, sha)
                for i, (_, meta, sha) in enumerate(prepared)]
        except (OSError, sqlite3.Error):
            raise PreviewError(_UNAVAILABLE) from None

    def get(self, *, owner_principal_id, receipt_id, index, chat_id=None):
        """Read one private frame; absent/foreign scope returns None without writes."""
        owner = _identifier(owner_principal_id)
        if chat_id is not None:
            _identifier(chat_id)
        if (not isinstance(receipt_id, str) or not re.fullmatch(r'[a-f0-9]{32}', receipt_id)
                or type(index) is not int or not 0 <= index < MAX_FRAMES):
            return None
        try:
            self._check_paths()
            if not self.path.exists():
                return None
            with closing(sqlite3.connect(self.path.absolute().as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('''SELECT r.owner,r.chat_id,r.dataset,r.clip_id,f.metadata,f.sha256,
                        CASE WHEN length(f.image)<=? THEN f.image END AS image
                    FROM frames f JOIN receipts r ON r.id=f.receipt_id
                    WHERE r.id=? AND f.position=? AND r.owner=? AND (? IS NULL OR r.chat_id=?)''',
                    (MAX_FRAME_BYTES, receipt_id, index, owner, chat_id, chat_id)).fetchone()
            if row is None:
                return None
            raw = _jpeg(row['image'])
            if hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise PreviewError(_UNAVAILABLE)
            metadata = _metadata(json.loads(row['metadata']))
            return _PrivateFrame(owner_principal_id=row['owner'], chat_id=row['chat_id'],
                dataset=row['dataset'], clip_id=row['clip_id'], sha256=row['sha256'],
                image_bytes=raw, **metadata)
        except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, KeyError, RecursionError):
            raise PreviewError(_UNAVAILABLE) from None


def tool_previews(name, content, success):
    """Project host inspection metadata, never arbitrary URLs or model image data."""
    if name != 'inspect_clip' or success is not True or not isinstance(content, list) or len(content) > 16:
        return []
    first = next((part for part in content if isinstance(part, dict) and part.get('type') == 'inputText'), None)
    if first is None:
        return []
    text = first.get('text')
    if not isinstance(text, str) or len(text) > MAX_METADATA_BYTES:
        return []
    try:
        if len(text.encode()) > MAX_METADATA_BYTES:
            return []
        metadata = json.loads(text, object_pairs_hook=_unique_object)
        frames = metadata.get('frame_previews') if isinstance(metadata, dict) else None
        if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
            return []
        result, receipt_id, clip_id = [], None, None
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict) or not _REQUIRED_KEYS <= frame.keys() <= _PUBLIC_KEYS:
                return []
            match = _URL.fullmatch(frame.get('url', ''))
            if not match or int(match[2]) != index:
                return []
            if index and (receipt_id != match[1] or clip_id != frame['clip_id']):
                return []
            receipt_id, clip_id = match[1], _identifier(frame['clip_id'], clip=True)
            if not isinstance(frame['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', frame['sha256']):
                return []
            result.append({'url': frame['url'], 'clip_id': clip_id, 'sha256': frame['sha256'], **_metadata(frame)})
        return result
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError):
        return []
