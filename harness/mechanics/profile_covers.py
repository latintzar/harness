"""Private profile artwork published from an owned, immutable chat artifact.

The caller supplies current company/member authority. Publication intentionally
shares a sanitized image with members allowed to view that profile; the original
chat file and its private provenance retain their original ACL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import re
import sqlite3
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from chat_files import ChatFiles
from workspace_identity import workspace_scope

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_DISPLAY_BYTES = 16 * 1024 * 1024
MAX_STORED_BYTES = 128 * 1024 * 1024
MAX_COVERS = 200
SPEC = {'type': 'function', 'name': 'publish_profile_cover',
    'description': 'Set the business cover or your own member profile cover from a generated image already published in this chat with publish_file. Use a light, dreamlike Monet-inspired image suited to the business or person. Supply its file_id. For business covers, include the current profile_revision from get_business_profile. This publishes existing artwork; it does not generate an image.',
    'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'file_id': {'type': 'string', 'pattern': '^file-[a-f0-9]{32}$'},
            'target': {'type': 'string', 'enum': ['business', 'member']},
            'expected_profile_revision': {'type': 'integer', 'minimum': 0}},
        'required': ['file_id', 'target']}}


class CoverError(ValueError):
    pass


def image_bytes(raw):
    """Decode bounded raster bytes, orient them, and strip embedded metadata."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise CoverError('Choose a PNG, JPEG or WebP image up to 8 MiB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if (source.format not in {'PNG', 'JPEG', 'WEBP'} or getattr(source, 'n_frames', 1) != 1
                        or source.width * source.height > MAX_PIXELS
                        or max(source.size) > 8192):
                    raise CoverError('Choose a still image up to 20 megapixels and 8,192 pixels per side.')
                source.load()
                oriented = ImageOps.exif_transpose(source)
                # A fresh raster excludes EXIF, comments, ICC profiles and other
                # metadata, including private GPS tags. Keep the original file.
                mode = 'RGBA' if 'A' in oriented.getbands() else 'RGB'
                pixels = oriented.convert(mode)
                pixels.thumbnail((2560, 1600), Image.Resampling.LANCZOS)
                clean = Image.new(mode, pixels.size)
                clean.paste(pixels)
                output = io.BytesIO()
                clean.save(output, format='PNG')
                data = output.getvalue()
                if len(data) > MAX_DISPLAY_BYTES:
                    raise CoverError('Choose a smaller profile image.')
                return data, clean.width, clean.height
    except CoverError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise CoverError('Choose a complete PNG, JPEG or WebP image.') from error


class ProfileCovers:
    def __init__(self, state, *, authorize=None):
        self.state = Path(state)
        self.company_scope = workspace_scope(self.state)
        self.path = self.state / 'profile-covers.sqlite'
        self.authorize = authorize or (lambda *args, **kwargs: False)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS covers (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                company_scope TEXT NOT NULL, target TEXT NOT NULL, target_id TEXT NOT NULL,
                collection TEXT NOT NULL, owner TEXT NOT NULL, actor_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                profile_revision INTEGER, source_file_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                created_at TEXT NOT NULL, data BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS covers_target ON covers(company_scope,target,target_id,sequence);
                CREATE TRIGGER IF NOT EXISTS immutable_cover_update BEFORE UPDATE ON covers
                BEGIN SELECT RAISE(ABORT,'Published profile artwork is immutable'); END;''')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _access(self, actor_id, target, target_id, collection, *, write):
        ChatFiles._scope(actor_id, dataset=collection)
        if target not in {'business', 'member'}:
            raise CoverError('Choose a business or member profile.')
        if (not isinstance(target_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', target_id)
                or (target == 'business' and target_id != collection)
                or (write and target == 'member' and target_id != actor_id)
                or self.authorize(actor_id, target=target, target_id=target_id,
                    collection=collection, write=write) is not True):
            raise CoverError('Open a profile available to your current membership.')

    @staticmethod
    def metadata(row):
        # Private source IDs, chat identity and author remain server-side.
        return {'id': row['id'], 'url': '/api/profile-covers/' + row['id'],
            'target': row['target'], 'target_id': row['target_id'],
            'profile_revision': row['profile_revision'], 'revision': row['sequence'],
            'width': row['width'], 'height': row['height'], 'created_at': row['created_at']}

    def publish(self, files, business, *, file_id, target, owner, actor_id, chat_id, collection,
                current, created, expected_profile_revision=None):
        ChatFiles._scope(owner, chat_id, collection)
        target_id = actor_id if target == 'member' else collection
        self._access(actor_id, target, target_id, collection, write=True)
        if (not isinstance(file_id, str) or not re.fullmatch(r'file-[a-f0-9]{32}', file_id)
                or not callable(current) or not current()):
            raise CoverError('Publish artwork from the active owning chat.')
        if (target == 'business' and (type(expected_profile_revision) is not int
                or expected_profile_revision < 0)):
            raise CoverError('Refresh the business profile revision before publishing its cover.')
        if (Path(files.path).parent.resolve() != self.state.resolve()
                or Path(business.state).resolve() != self.state.resolve()
                or business.company_scope != self.company_scope):
            raise CoverError('Choose artwork from the current company workspace.')
        authority = dict(owner_principal_id=owner, chat_id=chat_id, dataset=collection)
        record = files.get(file_id, **authority)
        if record.get('representation') != 'native_original' or record.get('size_bytes', MAX_SOURCE_BYTES + 1) > MAX_SOURCE_BYTES:
            raise CoverError('Publish a generated raster image with publish_file first.')
        record, raw = files.download(file_id, **authority)
        source_sha = hashlib.sha256(raw).hexdigest()
        if source_sha != record.get('sha256'):
            raise CoverError('Refresh the original artwork file.')
        data, width, height = image_bytes(raw)
        profile_revision = expected_profile_revision if target == 'business' else None
        # Lock business revision and cover insertion together. Authority and turn
        # liveness are rechecked immediately before and after insertion.
        with business.connect() as db:
            db.execute('ATTACH DATABASE ? AS cover_store', (str(self.path),))
            db.execute('BEGIN IMMEDIATE')
            business._scope(db, collection)
            if target == 'business' and business._current(db, collection)['profile_revision'] != profile_revision:
                raise CoverError('Refresh the business profile revision before publishing its cover.')
            self._access(actor_id, target, target_id, collection, write=True)
            if not current():
                raise CoverError('The artwork publication request has finished.')
            prior = db.execute('''SELECT * FROM cover_store.covers WHERE company_scope=? AND target=?
                AND target_id=? AND owner=? AND chat_id=? AND collection=? AND source_file_id=?
                AND source_sha256=? AND profile_revision IS ? ORDER BY sequence DESC LIMIT 1''',
                (self.company_scope, target, target_id, owner, chat_id, collection, file_id, source_sha, profile_revision)).fetchone()
            if prior:
                return self.metadata(prior)
            count, used = db.execute('SELECT COUNT(*),COALESCE(SUM(length(data)),0) FROM cover_store.covers').fetchone()
            if count >= MAX_COVERS or used + len(data) > MAX_STORED_BYTES:
                raise CoverError('This workspace’s saved artwork allowance is full.')
            key = 'cover-' + uuid.uuid4().hex
            db.execute('''INSERT INTO cover_store.covers(id,company_scope,target,target_id,collection,owner,actor_id,chat_id,
                profile_revision,source_file_id,source_sha256,sha256,width,height,created_at,data)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key,self.company_scope,target,target_id,collection,owner,actor_id,chat_id,profile_revision,file_id,
                 source_sha,hashlib.sha256(data).hexdigest(),width,height,datetime.now(timezone.utc).isoformat(),data))
            self._access(actor_id, target, target_id, collection, write=True)
            if not current():
                raise CoverError('The artwork publication request has finished.')
            row = db.execute('SELECT * FROM cover_store.covers WHERE id=?', (key,)).fetchone()
        created.append(key)
        return self.metadata(row)

    def get(self, *, actor_id, target, target_id, collection):
        self._access(actor_id, target, target_id, collection, write=False)
        with self.connect() as db:
            row = db.execute('''SELECT * FROM covers WHERE company_scope=? AND target=? AND target_id=?
                ORDER BY sequence DESC LIMIT 1''', (self.company_scope,target,target_id)).fetchone()
        return self.metadata(row) if row else None

    def download(self, cover_id, *, actor_id, collection=None):
        if not isinstance(cover_id, str) or not re.fullmatch(r'cover-[a-f0-9]{32}', cover_id):
            raise CoverError('Open a saved profile image.')
        with self.connect() as db:
            row = db.execute('SELECT * FROM covers WHERE company_scope=? AND id=?', (self.company_scope,cover_id)).fetchone()
        if row is None:
            raise CoverError('Open a saved profile image.')
        self._access(actor_id,row['target'],row['target_id'],row['collection'] if collection is None else collection,write=False)
        data = row['data']
        if not isinstance(data,bytes) or len(data)>MAX_DISPLAY_BYTES or hashlib.sha256(data).hexdigest()!=row['sha256']:
            raise CoverError('Refresh the saved artwork record.')
        return self.metadata(row), data

    def discard(self, ids, *, owner, chat_id, collection):
        """Host cleanup for newly published artifacts in an interrupted turn."""
        with self.connect() as db:
            db.executemany('DELETE FROM covers WHERE company_scope=? AND id=? AND owner=? AND chat_id=? AND collection=?',
                [(self.company_scope,key,owner,chat_id,collection) for key in ids])
