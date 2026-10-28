"""Seal playback media and the catalog that the maintained camera player reads.

From a host-selected set of camera views this command writes, below
`<state>/playback-media/<sha256(principal)>/`: one real copy of each recording, an optional
poster, one geometry sidecar per recording and `manifest.json` in the Catalog schema of
`playback_media.py`. Directories are 0700; every file is 0400 with a single link.

The dry run is the default and writes nothing:

    RUNTIME_DATABASE_URL=<restricted connection> python -B seal_playback_media.py \\
        --state /var/lib/rocoto-video-preview/environments/retail-demo \\
        --selection selection.json --evidence-root /path/to/retail-action-nvidia

The real run names the revision the dry run printed and a new private receipt file:

    ... --apply --expect-revision <revision> --receipt /path/to/playback-seal.json

Every recording is checked before the first write. Its SHA-256 equals the artifact mapping
PostgreSQL holds. Its frame clock equals the mapped source window. Its tracker sidecar holds
one file per frame and reproduces every reported birth identity PostgreSQL holds for that
tracker epoch. PostgreSQL is read through the maintained SDK as the restricted runtime role,
under the principal's current membership and source grants. The command runs as the service
identity, which owns the sealed files.

Selection file (paths are relative to `--evidence-root`, or absolute):

    {"schema_version": 1, "owner_principal_id": "peter", "company_id": "<64 hex>",
     "face_redaction": "source_anonymized",
     "views": [{"source_id": "...", "epoch_id": "...", "mapping_id": "...", "label": "...",
                "media": "<clip>/clean.mp4", "tracker_kitti": "<clip>/tracker-kitti",
                "poster": "<clip>/poster.jpg"}]}

`tracker_kitti` and `poster` are optional. A recording without a tracker sidecar plays without
boxes.

The receipt of `python -m realtime.import_tracker_run --apply` can stand in for the selection
file. It selects every imported view of that run, labelled by its clip, with `<clip>/clean.mp4`
and `<clip>/tracker-kitti` below `--evidence-root`; the host states the privacy basis itself:

    ... --import-receipt tracker-import.json --face-redaction source_anonymized \\
        --evidence-root <run root>/retail-action-nvidia
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
from types import SimpleNamespace
from typing import Literal
import uuid

from pydantic import Field, ValidationError, model_validator

from imported_media import ImportedMediaError
from playback_media import ARTIFACTS, LIMITS, MANIFEST_BYTES, Catalog, SealedPlaybackFiles, Strict, _match

SCHEMA = 'rocoto.playback-seal-receipt.v1'
GEOMETRY_SCHEMA = 'rocoto.playback-geometry.v1'
SIDECAR_LISTING = 'rocoto.tracker-kitti-directory.v1'
IMPORT_RECEIPT = 'rocoto.tracker-run-import-receipt.v1'
SELECTION_BYTES = 1024 * 1024
IMPORT_RECEIPT_BYTES = 16 * 1024**2
MOOV_BYTES = 16 * 1024**2
KITTI_FRAMES = 100_000
KITTI_FILE_BYTES = 1024 * 1024
KITTI_COLUMNS = 17
# The host's stop rule: sealing leaves at least this much free on the state filesystem.
MIN_FREE_BYTES = 1_500_000_000
FOLDERS = {'media': ('media', '.mp4'), 'poster': ('posters', '.jpg'), 'geometry': ('geometry', '.json')}
KITTI_NAME = re.compile(r'([0-9]{2}_[0-9]{3}_)([0-9]{6})\.txt')
TRACKER_ID = re.compile(r'0|[1-9][0-9]{0,19}')
SHA = re.compile(r'[0-9a-f]{64}')
CLIP = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}')
SEALED_NAME = re.compile(r'[0-9a-f]{64}\.(mp4|jpg|json)|\..+\.partial')


class Refused(ValueError):
    """A refusal with a stable operator-readable code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class View(Strict):
    source_id: str = Field(min_length=1, max_length=256)
    epoch_id: str = Field(min_length=1, max_length=256)
    mapping_id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=160)
    media: str = Field(min_length=1, max_length=1024)
    tracker_kitti: str | None = Field(default=None, min_length=1, max_length=1024)
    poster: str | None = Field(default=None, min_length=1, max_length=1024)


class Selection(Strict):
    schema_version: Literal[1]
    owner_principal_id: str = Field(min_length=1, max_length=256)
    company_id: str = Field(min_length=1, max_length=256)
    face_redaction: Literal['source_anonymized', 'verified_redacted']
    views: list[View] = Field(min_length=1, max_length=ARTIFACTS)

    @model_validator(mode='after')
    def unique_views(self):
        keys = [(view.source_id, view.epoch_id, view.mapping_id) for view in self.views]
        if len(set(keys)) != len(keys):
            raise ValueError('Select each recording once')
        return self


def _sha(content):
    return hashlib.sha256(content).hexdigest()


# ── Evidence is read, never changed ─────────────────────────────────────────────────────────────

def _open_evidence(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Refused('evidence_missing', f'Stage {Path(path).name} where this command reads it.') from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise Refused('evidence_missing', f'Select {Path(path).name} as a regular file.')
    return descriptor


def _chunks(source):
    if isinstance(source, bytes):
        yield source
        return
    with os.fdopen(_open_evidence(source), 'rb') as stream:
        while data := stream.read(1024 * 1024):
            yield data


def _measure(path, maximum, code):
    """SHA-256 and length of one file, read once within the bound of its kind."""
    digest, size = hashlib.sha256(), 0
    for data in _chunks(path):
        size += len(data)
        if size > maximum:
            raise Refused(code, f'{Path(path).name} exceeds the bound of its kind.')
        digest.update(data)
    if not size:
        raise Refused(code, f'{Path(path).name} is empty.')
    return digest.hexdigest(), size


def _content(path, maximum, code):
    content = b''
    for data in _chunks(path):
        content += data
        if len(content) > maximum:
            raise Refused(code, f'{Path(path).name} exceeds the bound of its kind.')
    return content


def _resolve(root, value):
    path = Path(value)
    if path.is_absolute():
        return path
    if root is None or '..' in path.parts:
        raise Refused('evidence_root_required', 'Supply --evidence-root for the relative selection paths.')
    return Path(root).absolute() / path


def load_selection(path):
    try:
        content = _content(path, SELECTION_BYTES, 'selection_invalid')
        return Selection.model_validate_json(content), _sha(content)
    except (Refused, ValidationError, ValueError):
        raise Refused('selection_invalid', 'Supply the host selection in the documented schema.') from None


def selection_from_import(path, face_redaction):
    """Every view of one completed historical import, in the importer's own identifiers."""
    refusal = Refused('import_receipt_invalid',
                      'Supply the receipt of a completed tracker-run import and the privacy basis of its footage.')
    try:
        content = _content(path, IMPORT_RECEIPT_BYTES, 'import_receipt_invalid')
        receipt = json.loads(content)
        if (receipt.get('schema') != IMPORT_RECEIPT or receipt.get('status') != 'imported'
                or receipt.get('dry_run') is not False or receipt.get('error')):
            raise refusal
        views = []
        for view in receipt['views']:
            clip, ids = view['clip'], view['ids']
            if not isinstance(clip, str) or not CLIP.fullmatch(clip):
                raise refusal
            views.append({'source_id': ids['source_id'], 'epoch_id': ids['epoch_id'],
                          'mapping_id': ids['mapping_id'], 'label': clip,
                          'media': f'{clip}/clean.mp4', 'tracker_kitti': f'{clip}/tracker-kitti'})
        selection = Selection.model_validate({
            'schema_version': 1, 'owner_principal_id': receipt['principal_id'],
            'company_id': receipt['company_id'], 'face_redaction': face_redaction, 'views': views})
        return selection, _sha(content)
    except (Refused, ValidationError, ValueError, KeyError, TypeError, AttributeError):
        raise refusal from None


# ── The recording's own frame clock ─────────────────────────────────────────────────────────────

def _boxes(data, start, end):
    position = start
    while position + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, position)
        head = 8
        if size == 1:
            size, head = struct.unpack_from('>Q', data, position + 8)[0], 16
        elif size == 0:
            size = end - position
        if size < head or position + size > end:
            raise ValueError('box')
        yield kind, position + head, position + size
        position += size


def _one(data, bounds, kind):
    found = [(start, end) for name, start, end in _boxes(data, *bounds) if name == kind]
    if len(found) != 1:
        raise ValueError(kind)
    return found[0]


def _movie_header(path):
    with os.fdopen(_open_evidence(path), 'rb') as stream:
        total, position = os.fstat(stream.fileno()).st_size, 0
        while position + 8 <= total:
            stream.seek(position)
            head = stream.read(16)
            size, kind = struct.unpack_from('>I4s', head)
            length = 8
            if size == 1:
                size, length = struct.unpack_from('>Q', head, 8)[0], 16
            elif size == 0:
                size = total - position
            if size < length or position + size > total:
                raise ValueError('box')
            if kind == b'moov':
                if size - length > MOOV_BYTES:
                    raise ValueError('moov')
                stream.seek(position + length)
                return stream.read(size - length)
            position += size
    raise ValueError('moov')


def probe_recording(path):
    """Width, height, frame count and constant frame rate of the one video track of an MP4."""
    try:
        moov = _movie_header(path)
        tracks = []
        for name, start, end in _boxes(moov, 0, len(moov)):
            if name != b'trak':
                continue
            media = _one(moov, (start, end), b'mdia')
            handler = _one(moov, media, b'hdlr')
            if moov[handler[0] + 8:handler[0] + 12] != b'vide':
                continue
            header = _one(moov, media, b'mdhd')
            timescale = struct.unpack_from('>I', moov, header[0] + (20 if moov[header[0]] else 12))[0]
            table = _one(moov, _one(moov, media, b'minf'), b'stbl')
            sizes, times, entry = (_one(moov, table, kind) for kind in (b'stsz', b'stts', b'stsd'))
            frames = struct.unpack_from('>I', moov, sizes[0] + 8)[0]
            count = struct.unpack_from('>I', moov, times[0] + 4)[0]
            if times[0] + 8 + 8 * count > times[1]:
                raise ValueError('stts')
            runs = [struct.unpack_from('>II', moov, times[0] + 8 + 8 * index) for index in range(count)]
            width, height = struct.unpack_from('>HH', moov, entry[0] + 40)
            tracks.append((width, height, frames, timescale, runs))
        (width, height, frames, timescale, runs), = tracks
        deltas = {delta for _, delta in runs}
        if len(deltas) != 1 or sum(total for total, _ in runs) != frames or not frames or not timescale:
            raise ValueError('clock')
        fps = Fraction(timescale, deltas.pop())
    except (ValueError, struct.error, IndexError, ZeroDivisionError):
        raise Refused('recording_unreadable',
                      f'{Path(path).name}: select an MP4 with one video track on a constant frame clock.') from None
    return {'width': width, 'height': height, 'frames': frames, 'fps': fps}


# ── The tracker sidecar ─────────────────────────────────────────────────────────────────────────

def sidecar_digest(items):
    """One digest for a per-frame KITTI directory: the sorted listing of each file's hash and name.

    It is the text `sha256sum` prints over the sorted directory, under one versioned first line.
    The historical importer seals this digest as the run's tracker sidecar, so the reported birth
    identities derived here are the ones PostgreSQL holds.
    """
    lines = sorted((name, _sha(content)) for name, content in items)
    return _sha((SIDECAR_LISTING + '\n' + ''.join(f'{digest}  {name}\n' for name, digest in lines)).encode())


def read_tracker_sidecar(directory, frame_count):
    """Every reported box of every frame, and each raw tracker ID's first reported frame."""
    try:
        with os.scandir(directory) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
            regular = all(entry.is_file(follow_symlinks=False) for entry in entries)
            sizes = [entry.stat(follow_symlinks=False).st_size for entry in entries]
    except OSError:
        raise Refused('evidence_missing', 'Stage the tracker sidecar directory of each selected view.') from None
    matches = [KITTI_NAME.fullmatch(entry.name) for entry in entries]
    if (not regular or not all(matches) or len({match[1] for match in matches}) > 1
            or [int(match[2]) for match in matches] != list(range(frame_count))
            or frame_count > KITTI_FRAMES):
        raise Refused('frame_count_mismatch',
                      'The tracker sidecar holds one file for every frame of its recording.')
    malformed = Refused('sidecar_format', 'The tracker sidecar holds 17-column KITTI rows with finite numbers.')
    files, frames, first = [], [], {}
    for index, (entry, size) in enumerate(zip(entries, sizes, strict=True)):
        # A frame with no reported box is an empty file; it stays part of the sealed listing.
        content = _content(Path(entry.path), KITTI_FILE_BYTES, 'sidecar_format') if size else b''
        files.append((entry.name, content))
        rows, seen = [], set()
        try:
            text = content.decode('ascii')
        except UnicodeError:
            raise malformed from None
        for line in text.splitlines():
            columns = line.split()
            if len(columns) != KITTI_COLUMNS or not TRACKER_ID.fullmatch(columns[1]) or columns[1] in seen:
                raise malformed
            try:
                numbers = [float(value) for value in columns[2:]]
            except ValueError:
                raise malformed from None
            if not all(map(math.isfinite, numbers)):
                raise malformed
            box = numbers[3:7]
            seen.add(columns[1])
            first.setdefault(columns[1], index)
            rows.append((columns[1], box))
        frames.append(rows)
    return frames, first, sidecar_digest(files)


def geometry_document(recording, source_sha256, tracker_namespace, frames, births):
    """What the player paints from: one entry per frame, each box with its ID and birth identity."""
    fps = recording['fps']
    document = {
        'schema': GEOMETRY_SCHEMA,
        'source_sha256': source_sha256,
        'tracker_namespace': tracker_namespace,
        'width': recording['width'],
        'height': recording['height'],
        'fps': fps.numerator if fps.denominator == 1 else float(fps),
        'frame_count': len(frames),
        'frames': {
            str(index): [{'id': tracker_id, 'birth_id': births[tracker_id], 'box': box}
                         for tracker_id, box in rows]
            for index, rows in enumerate(frames)
        },
    }
    return (json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


# ── Verification against PostgreSQL ─────────────────────────────────────────────────────────────

def _context(company_id, principal_id):
    from realtime.contracts import ExecutionBounds, HostExecutionContext
    from realtime.read_contract import MAX_OUTPUT_BYTES

    request = uuid.uuid4().hex
    return HostExecutionContext(
        company_id=company_id, principal_id=principal_id, request_id=request,
        conversation_id='playback-seal:' + request, capabilities=frozenset({'realtime:read'}),
        deadline_utc=datetime.now(timezone.utc) + timedelta(seconds=30),
        bounds=ExecutionBounds(max_events=1000, max_output_bytes=MAX_OUTPUT_BYTES))


async def _lineage(selection):
    """The current binding of every selected recording, and each tracker epoch's visitors."""
    from realtime.continuity import ContinuityStore
    from realtime.media import PlaybackMediaStore
    from realtime.store import RealtimeError

    company, principal = selection.company_id, selection.owner_principal_id
    references = [{'source_id': view.source_id, 'epoch_id': view.epoch_id, 'mapping_id': view.mapping_id}
                  for view in selection.views]
    try:
        bindings = await PlaybackMediaStore().bindings(_context(company, principal), references)
        visitors = {}
        for view in selection.views:
            key = (view.source_id, view.epoch_id)
            if view.tracker_kitti and key not in visitors:
                playback = await ContinuityStore().playback(_context(company, principal), *key)
                visitors[key] = playback['visitors']
    except RealtimeError as error:
        raise Refused('authority_unavailable',
                      'PostgreSQL holds no current binding for a selected recording under this principal ('
                      + type(error).__name__ + ').') from None
    return bindings, visitors


def reported_births(view, binding, recording, first, sidecar_sha256, visitors):
    """Each raw tracker ID's reported birth identity, proven equal to the one PostgreSQL holds."""
    from realtime.continuity import reported_birth_id, reported_frame_s

    fps = recording['fps']
    clock = SimpleNamespace(source_window=SimpleNamespace(start_s=Decimal(binding['source_window']['start_s'])),
                            fps=Decimal(fps.numerator) / Decimal(fps.denominator))
    births = {tracker_id: reported_birth_id(binding['tracker_namespace'], sidecar_sha256, tracker_id, frame)
              for tracker_id, frame in first.items()}
    held = {person['tracker_id']: person for person in visitors}
    if len(held) != len(visitors) or set(held) != set(births):
        raise Refused('visitor_mismatch',
                      f'{view.source_id}: the tracker sidecar and PostgreSQL name different tracker IDs.')
    for tracker_id, person in held.items():
        reported = person.get('first_reported') or {}
        if (person['birth_id'] != births[tracker_id] or person.get('basis') != 'first_reported'
                or reported.get('source_asset_id') != binding['source_asset_id']
                or Decimal(reported.get('source_s', 'NaN')) != reported_frame_s(clock, first[tracker_id])):
            raise Refused('birth_identity_mismatch',
                          f'{view.source_id}: tracker {tracker_id} differs from its reported birth in PostgreSQL.')
    return births


def _service_identity(state):
    """The service identity owns the private state and runs this command; the reader requires both."""
    try:
        info = os.stat(state, follow_symlinks=False)
    except OSError:
        info = None
    if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise Refused('service_identity_required',
                      'Run as the service identity that owns this private environment state.')


def _private_directory(path):
    """True for a private real directory of this identity, False for an absent one."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise Refused('sealed_path_unsafe', f'Keep {Path(path).name} as a private real directory.')
    return True


def _sealed(path, sha256, size):
    """True for an intact sealed file, False for an absent one; any other state is refused."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o400 and info.st_size == size
            and _measure(path, size, 'sealed_file_conflict')[0] == sha256):
        return True
    raise Refused('sealed_file_conflict', f'{Path(path).name} differs from its sealed name; inspect the folder.')


async def plan(options):
    """Verify the whole selection and describe the sealed catalog. This writes nothing."""
    from workspace_identity import IDENTITY_FILE, _read as saved_identity

    state = Path(options.state).absolute()
    _service_identity(state)
    if options.import_receipt:
        selection, selection_sha256 = selection_from_import(options.import_receipt, options.face_redaction)
    else:
        selection, selection_sha256 = load_selection(options.selection)
    try:
        identity = saved_identity(state / IDENTITY_FILE)
    except ValueError:
        identity = None
    if identity is None or identity['company_scope'] != selection.company_id:
        raise Refused('company_mismatch', "The selection names another company than this state's saved identity.")
    root = state / 'playback-media' / _sha(selection.owner_principal_id.encode())
    folders = (root.parent, root, *(root / folder for folder, _ in FOLDERS.values()))
    for folder in folders:
        _private_directory(folder)
    bindings, visitors = await _lineage(selection)
    placed, grouped, matched, views = {}, {}, {}, []

    def place(kind, sha256, size, source):
        folder, suffix = FOLDERS[kind]
        relative = f'{folder}/{sha256}{suffix}'
        placed[relative] = (sha256, size, source)
        return {'path': relative, 'sha256': sha256, 'size_bytes': size}

    for view, binding in zip(selection.views, bindings, strict=True):
        media = _resolve(options.media_root or options.evidence_root, view.media)
        sha256, size = await asyncio.to_thread(_measure, media, LIMITS['media'], 'media_bound')
        if sha256 != binding['source_sha256']:
            raise Refused('media_hash_mismatch',
                          f'{view.source_id}: the recording differs from its artifact mapping in PostgreSQL.')
        recording = await asyncio.to_thread(probe_recording, media)
        length = recording['frames'] / recording['fps']
        start, end = (Fraction(binding['source_window'][name]) for name in ('start_s', 'end_s'))
        # A mapped window lies inside its recording; a complete tracker run spans all of it.
        if end > length or (view.tracker_kitti and end - start != length):
            raise Refused('frame_clock_mismatch',
                          f'{view.source_id}: the recording and its mapped source window differ in length.')
        artifact = {'mapping_id': view.mapping_id, 'media': place('media', sha256, size, media),
                    'width': recording['width'], 'height': recording['height'],
                    'fps': float(recording['fps']), 'duration_s': float(length)}
        report = {'source_id': view.source_id, 'epoch_id': view.epoch_id, 'mapping_id': view.mapping_id,
                  'label': view.label, 'frames': recording['frames']}
        if view.poster:
            poster = _resolve(options.evidence_root, view.poster)
            poster_sha256, poster_size = await asyncio.to_thread(_measure, poster, LIMITS['poster'], 'poster_bound')
            with os.fdopen(_open_evidence(poster), 'rb') as stream:
                if stream.read(3) != b'\xff\xd8\xff':
                    raise Refused('poster_format', f'{view.source_id}: supply a JPEG poster.')
            artifact['poster'] = place('poster', poster_sha256, poster_size, poster)
        if view.tracker_kitti:
            frames, first, sidecar_sha256 = await asyncio.to_thread(
                read_tracker_sidecar, _resolve(options.evidence_root, view.tracker_kitti), recording['frames'])
            births = reported_births(view, binding, recording, first, sidecar_sha256,
                                     visitors[view.source_id, view.epoch_id])
            content = geometry_document(recording, sha256, binding['tracker_namespace'], frames, births)
            if len(content) > LIMITS['geometry']:
                raise Refused('geometry_bound', f'{view.source_id}: the geometry sidecar exceeds its bound.')
            artifact['geometry'] = place('geometry', _sha(content), len(content), content) | {
                'source_sha256': sha256, 'tracker_namespace': binding['tracker_namespace']}
            report |= {'tracker_sidecar_sha256': sidecar_sha256, 'visitors': len(births),
                       'boxes': sum(len(rows) for rows in frames)}
        key = (view.source_id, view.epoch_id)
        source = grouped.setdefault(key, {'source_id': view.source_id, 'epoch_id': view.epoch_id,
                                          'label': view.label, 'artifacts': []})
        if source['label'] != view.label:
            raise Refused('selection_invalid', 'Recordings of one source and epoch share one label.')
        source['artifacts'].append(artifact)
        matched.setdefault(key, []).append(binding)
        views.append(report | {name: artifact[name] for name in (
            'media', 'poster', 'geometry', 'width', 'height', 'fps', 'duration_s') if name in artifact})
    try:
        catalog = Catalog.model_validate({
            'schema_version': 1, 'owner_principal_id': selection.owner_principal_id,
            'company_id': selection.company_id, 'face_redaction': selection.face_redaction,
            'sources': list(grouped.values())})
        # The comparison the bootstrap makes for every artifact, against the same bindings.
        for source in catalog.sources:
            for artifact, binding in zip(source.artifacts, matched[source.source_id, source.epoch_id], strict=True):
                _match(artifact, binding)
    except ValueError as error:
        raise Refused('catalog_invalid', 'The selection leaves the catalog the player reads ('
                      + type(error).__name__ + ').') from None
    manifest = (json.dumps(catalog.model_dump(mode='json', exclude_none=True), sort_keys=True,
                           separators=(',', ':')) + '\n').encode()
    if len(manifest) > MANIFEST_BYTES:
        raise Refused('catalog_invalid', 'The catalog exceeds its 256 KiB bound.')
    revision = _sha(manifest)
    kept = sorted([path for path, (sha256, size, _) in placed.items()
                   if await asyncio.to_thread(_sealed, root / path, sha256, size)])
    writes = sorted(set(placed) - set(kept))
    try:
        current = _content(root / 'manifest.json', MANIFEST_BYTES, 'catalog_invalid')
    except Refused:
        current = None
    # An identical catalog that is itself intact stays in place, with its modification time.
    unchanged = False
    if not writes and current == manifest:
        with suppress(Refused):
            unchanged = await asyncio.to_thread(_sealed, root / 'manifest.json', revision, len(manifest))
    orphans = []
    for folder, _ in FOLDERS.values():
        with suppress(OSError):
            orphans += [f'{folder}/{name}' for name in sorted(os.listdir(root / folder))
                        if f'{folder}/{name}' not in placed]
    existing = root
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    to_write = sum(placed[path][1] for path in writes) + (0 if unchanged else len(manifest))
    if to_write and free - to_write < options.min_free_bytes:
        raise Refused('free_disk_below_stop_rule',
                      f'Sealing leaves {free - to_write} bytes free; the stop rule keeps {options.min_free_bytes}.')
    receipt = {
        'schema': SCHEMA, 'revision': revision,
        'previous_revision': _sha(current) if current is not None else None,
        'owner_principal_id': selection.owner_principal_id, 'company_id': selection.company_id,
        'face_redaction': selection.face_redaction, 'root': str(root), 'database_role': 'rocoto_rt',
        'selection_sha256': selection_sha256,
        'selected_from': 'import_receipt' if options.import_receipt else 'selection',
        'counts': {'sources': len(catalog.sources), 'artifacts': len(views),
                   'geometry': sum('geometry' in view for view in views),
                   'posters': sum('poster' in view for view in views),
                   'visitors': sum(view.get('visitors', 0) for view in views),
                   'boxes': sum(view.get('boxes', 0) for view in views)},
        'bytes': {'media': sum(view['media']['size_bytes'] for view in views),
                  'geometry': sum(view['geometry']['size_bytes'] for view in views if 'geometry' in view),
                  'posters': sum(view['poster']['size_bytes'] for view in views if 'poster' in view),
                  'manifest': len(manifest), 'to_write': to_write, 'free_before': free,
                  'min_free': options.min_free_bytes},
        'views': views, 'writes': writes, 'kept': kept, 'orphans': orphans,
        'tool': {'sha256': {name: _sha(Path(__file__).with_name(name).read_bytes())
                            for name in ('seal_playback_media.py', 'playback_media.py')}},
    }
    return SimpleNamespace(state=state, root=root, folders=folders, selection=selection, catalog=catalog,
                           manifest=manifest, revision=revision, placed=placed, writes=writes,
                           orphans=orphans, unchanged=unchanged, receipt=receipt)


# ── Writing ─────────────────────────────────────────────────────────────────────────────────────

def _write(target, sha256, size, source, *, replace=False):
    """One sealed regular file of its own bytes: written beside its name, checked, then named."""
    partial = target.with_name(f'.{target.name}.{os.getpid()}.partial')
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        digest, written = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'wb') as stream:
            for data in _chunks(source):
                stream.write(data)
                digest.update(data)
                written += len(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        if digest.hexdigest() != sha256 or written != size:
            raise Refused('evidence_changed', 'Evidence changed after its verification; run the dry run again.')
        if not replace and os.path.lexists(target):
            raise Refused('sealed_file_conflict', f'{target.name} appeared during the write.')
        os.replace(partial, target)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(partial)


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def seal(planned, *, prune=False):
    """Copy, check through the reader's own code, then replace the catalog in one rename."""
    root = planned.root
    for folder in planned.folders:
        if not _private_directory(folder):
            os.mkdir(folder, 0o700)
            os.chmod(folder, 0o700)
    # The lock is the folder itself, so the sealed tree holds read-only files alone.
    lock = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused('seal_in_progress', 'Another seal of this catalog is running.') from None
        for relative in planned.writes:
            sha256, size, source = planned.placed[relative]
            if not _sealed(root / relative, sha256, size):
                _write(root / relative, sha256, size, source)
        for folder, _ in FOLDERS.values():
            _sync(root / folder)
        files = SealedPlaybackFiles(planned.state, planned.selection.owner_principal_id)
        try:
            # The reader's held-descriptor path and checksum checks, before the catalog names a file.
            for source in planned.catalog.sources:
                for artifact in source.artifacts:
                    for kind in FOLDERS:
                        if getattr(artifact, kind) is not None:
                            files.file(getattr(artifact, kind), kind).verify()
            if not planned.unchanged:
                _write(root / 'manifest.json', planned.revision, len(planned.manifest), planned.manifest,
                       replace=True)
                _sync(root)
            catalog, revision = files.catalog()
            files.sealed([spec for s in catalog.sources for a in s.artifacts for spec in a.files()])
        except Refused:
            raise
        except (ImportedMediaError, ValueError) as error:
            raise Refused('sealed_read_back_failed',
                          "The sealed files fail the reader's own checks: " + str(error)) from None
        if revision != planned.revision or catalog != planned.catalog:
            raise Refused('sealed_read_back_failed', 'The catalog on disk differs from the verified plan.')
        pruned = []
        if prune:
            for relative in planned.orphans:
                folder, name = relative.split('/')
                if SEALED_NAME.fullmatch(name) and not (root / folder / name).is_dir():
                    os.unlink(root / folder / name)
                    pruned.append(relative)
        return pruned
    finally:
        os.close(lock)


def _reserve(path):
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Refused('receipt_unavailable', 'Name a new receipt file in an existing private directory.') from None


async def run(options):
    """The dry run or the real seal over an open restricted pool; returns the receipt."""
    if options.apply != bool(options.expect_revision) or options.apply != bool(options.receipt):
        raise Refused('apply_requires_dry_run',
                      "Apply with the dry run's revision and a new receipt file; the dry run takes neither.")
    if options.apply and not SHA.fullmatch(options.expect_revision):
        raise Refused('apply_requires_dry_run', 'Supply the 64-character revision of the dry run.')
    if bool(options.selection) == bool(options.import_receipt) or bool(options.import_receipt) != bool(
            options.face_redaction):
        raise Refused('selection_required',
                      'Supply --selection, or --import-receipt together with --face-redaction.')
    planned = await plan(options)
    receipt = planned.receipt | {'mode': 'apply' if options.apply else 'dry_run', 'pruned': []}
    receipt['status'] = 'already_sealed' if planned.unchanged else 'sealed' if options.apply else 'would_seal'
    if not options.apply:
        return receipt | {'recorded_at': datetime.now(timezone.utc).isoformat()}
    if planned.revision != options.expect_revision:
        raise Refused('revision_mismatch', 'The inputs differ from the reviewed dry run; run it again.')
    reserved, sealed = _reserve(options.receipt), False
    try:
        receipt['pruned'] = await asyncio.to_thread(seal, planned, prune=options.prune)
        sealed = True
        receipt['recorded_at'] = datetime.now(timezone.utc).isoformat()
        with os.fdopen(reserved, 'wb') as stream:
            reserved = None
            stream.write((json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if reserved is not None:
            os.close(reserved)
        if not sealed:
            # The catalog stayed as it was, so the reserved receipt name is released.
            Path(options.receipt).unlink(missing_ok=True)
    return receipt


async def run_with_database(options, dsn):
    """Open the one restricted pool this command reads through, seal, and close the pool."""
    from realtime_read_routes import _load_backend

    if not isinstance(dsn, str) or not dsn.strip():
        raise Refused('connection_required', 'Supply the restricted connection in RUNTIME_DATABASE_URL.')
    db = _load_backend()
    try:
        await db.init_runtime_only(dsn, timeout_s=10, max_size=1)
    except RuntimeError as error:
        # The role and schema guards answer with fixed sentences.
        raise Refused('runtime_connection_refused', str(error)) from None
    try:
        return await run(options)
    finally:
        await db.close_runtime_only()


def parser():
    command = argparse.ArgumentParser(
        prog='seal_playback_media.py',
        description='Seal playback media and the maintained catalog; the dry run is the default.')
    command.add_argument('--state', type=Path, required=True, help="The environment's private state directory")
    command.add_argument('--selection', type=Path, help='Host selection of camera views')
    command.add_argument('--import-receipt', type=Path,
                         help='Receipt of realtime.import_tracker_run; selects every imported view')
    command.add_argument('--face-redaction', choices=('source_anonymized', 'verified_redacted'),
                         help='Privacy basis of the footage, stated with --import-receipt')
    command.add_argument('--evidence-root', type=Path, help='Folder the relative selection paths start from')
    command.add_argument('--media-root', type=Path, help='Folder for relative recording paths, when it differs')
    command.add_argument('--min-free-bytes', type=int, default=MIN_FREE_BYTES,
                         help='Free bytes the state filesystem keeps after sealing')
    command.add_argument('--prune', action='store_true', help='Remove sealed files the new catalog leaves out')
    command.add_argument('--apply', action='store_true', help="Write; requires the dry run's revision")
    command.add_argument('--expect-revision', help='revision printed by the dry run')
    command.add_argument('--receipt', help='New private file that receives the receipt')
    return command


def main(argv=None, environ=None):
    options = parser().parse_args(argv)
    source = os.environ if environ is None else environ
    try:
        receipt = asyncio.run(run_with_database(options, source.get('RUNTIME_DATABASE_URL', '')))
    except Refused as refusal:
        print(f'REFUSING: {refusal.code}: {refusal}', file=sys.stderr)
        return 2
    except Exception as failure:
        # Driver errors can quote connection details; the class identifies the failure.
        print(f'REFUSING: database_unavailable: {type(failure).__name__}', file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
