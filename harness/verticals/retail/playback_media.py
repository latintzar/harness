"""Private host media catalog joined to maintained source and camera grants.

The catalog supplies bytes and display metadata only. PostgreSQL supplies source
clocks and tracker identity; snapshots supply visitors, chapters and baskets. The
company's own business scope and location store supply the location record.

The media endpoint owns its authority: one complete decision admits the request,
and the same decision runs again before each body chunk leaves. A host middleware
adds its own checks around this; the endpoint's guarantee stands by itself.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from imported_media import ImportedMedia, ImportedMediaError, VerifiedFile, byte_response

SCHEMA = 'rocoto.player-bootstrap.v1'
# Bootstrap, media admission and each in-stream authority check share this bound.
DEADLINE_S = 5
# The location record is display data read from the company's local stores. It has this much
# of the bootstrap deadline; a slower store leaves the record out and the sources playable.
LOCATION_S = 2
MANIFEST_BYTES = 256 * 1024
ARTIFACTS = 64
SOURCES = 64
LIMITS = {'media': 256 * 1024**2, 'poster': 8 * 1024**2, 'geometry': 4 * 1024**2}
TYPES = {'media': ('.mp4', 'video/mp4'), 'poster': ('.jpg', 'image/jpeg'),
         'geometry': ('.json', 'application/json')}
ADDRESS = re.compile(r'maintained/([a-f0-9]{64})/(0|[1-9][0-9]?)/(0|[1-9][0-9]?)/(media|poster|geometry)')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)


class MediaFile(Strict):
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    size_bytes: int = Field(gt=0, le=LIMITS['media'])

    @model_validator(mode='after')
    def safe_relative(self):
        parts = self.path.split('/')
        if any(p in {'', '.', '..'} or '\\' in p or '\x00' in p for p in parts):
            raise ValueError('Use a sealed media reference')
        return self


class Poster(MediaFile):
    size_bytes: int = Field(gt=0, le=LIMITS['poster'])


class Geometry(MediaFile):
    size_bytes: int = Field(gt=0, le=LIMITS['geometry'])
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    tracker_namespace: str = Field(min_length=1, max_length=256)


class Artifact(Strict):
    mapping_id: str = Field(min_length=1, max_length=256)
    media: MediaFile
    poster: Poster | None = None
    geometry: Geometry | None = None
    width: int = Field(ge=2, le=4096)
    height: int = Field(ge=2, le=2160)
    fps: float = Field(gt=0, le=120)
    duration_s: float = Field(gt=0, le=3600)

    def files(self):
        return [spec for spec in (self.media, self.poster, self.geometry) if spec is not None]


class Source(Strict):
    source_id: str = Field(min_length=1, max_length=256)
    epoch_id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=160)
    artifacts: list[Artifact] = Field(min_length=1, max_length=ARTIFACTS)


class Catalog(Strict):
    schema_version: Literal[1]
    owner_principal_id: str = Field(min_length=1, max_length=256)
    company_id: str = Field(min_length=1, max_length=256)
    face_redaction: Literal['source_anonymized', 'verified_redacted']
    sources: list[Source] = Field(min_length=1, max_length=SOURCES)

    @model_validator(mode='after')
    def unique_mappings(self):
        keys = [(s.source_id, s.epoch_id, a.mapping_id) for s in self.sources for a in s.artifacts]
        if len(keys) > ARTIFACTS or len(set(keys)) != len(keys):
            raise ValueError('Use unique bounded playback mappings')
        if len({(s.source_id, s.epoch_id) for s in self.sources}) != len(self.sources):
            raise ValueError('Group artifacts by their source and epoch')
        return self


class SealedPlaybackFiles(ImportedMedia):
    """Reuse the intake's held-FD integrity and private path validation."""
    def __init__(self, state, owner):
        self.state = Path(state).absolute()
        self.root = self.state / 'playback-media' / hashlib.sha256(owner.encode()).hexdigest()
        self._verified = OrderedDict()

    def catalog(self):
        fd = self._open(('manifest.json',), sealed=True)
        with os.fdopen(fd, 'rb') as stream:
            content = stream.read(MANIFEST_BYTES + 1)
        if len(content) > MANIFEST_BYTES:
            raise ValueError('Playback manifest exceeds its budget')
        return Catalog.model_validate_json(content), hashlib.sha256(content).hexdigest()

    def file(self, spec, kind):
        suffix, media_type = TYPES[kind]
        return VerifiedFile(self, tuple(spec.path.split('/')), spec.sha256,
                            spec.size_bytes, kind + suffix, media_type, LIMITS[kind])

    def sealed(self, specs):
        """Each reference is a private, read-only regular file of its declared length.

        Bootstrap publishes addresses for these files; the checksum belongs to the
        media admission, which holds the descriptor it serves from.
        """
        for spec in specs:
            fd = self._open(tuple(spec.path.split('/')), sealed=True)
            try:
                if os.fstat(fd).st_size != spec.size_bytes:
                    raise ImportedMediaError('imported_file_integrity_failed')
            finally:
                os.close(fd)


def resolver(request, context):
    selected = request.state.workspace
    # One bounded checksum cache per current owner, retained for this service.
    cache = selected.setdefault('playback_files', OrderedDict())
    key = context.principal_id
    if key not in cache:
        cache[key] = SealedPlaybackFiles(selected['state'], key)
        while len(cache) > 16:
            cache.popitem(last=False)
    return cache[key]


async def _catalog(request, context):
    files = resolver(request, context)
    catalog, revision = await asyncio.to_thread(files.catalog)
    if catalog.owner_principal_id != context.principal_id or catalog.company_id != context.company_id:
        raise ValueError('Preserve the commissioned workspace identity')
    return files, catalog, revision


def _match(artifact, binding):
    if artifact.media.sha256 != binding['source_sha256']:
        raise ValueError('Playback bytes must match their source mapping')
    if float(binding['source_window']['end_s']) > artifact.duration_s:
        raise ValueError('Playback mapping exceeds its media duration')
    if artifact.geometry and (artifact.geometry.source_sha256 != binding['source_sha256']
            or artifact.geometry.tracker_namespace != binding['tracker_namespace']):
        raise ValueError('Geometry must match its exact source and tracker namespace')


def _text(value, maximum):
    return value if isinstance(value, str) and value.strip() and len(value) <= maximum else None


def _location(selected, context, catalog):
    """The location record of this session's cameras, read through the company's own stores.

    The business scope owns geography, cameras and grants; the location store owns the
    principal's coordinates. Both belong to the workspace of this request and must carry
    the session's company. The record appears when one location in the principal's current
    access holds a camera bound to a catalog source. It is display data: playback authority
    stays with PostgreSQL, so an absent record leaves the sources playable.
    """
    scope, store = selected.get('business_scope'), selected.get('location_intelligence')
    if scope is None or store is None or getattr(store, 'scope', None) is not scope:
        return None, None
    if scope.company_id != context.company_id:
        return None, None
    wanted = {source.source_id for source in catalog.sources}
    found = {}
    try:
        for config in store._rows(context.principal_id, 'location'):
            try:
                view = store.authorized(context.principal_id, config['collection'], config['location_id'])
            except ValueError:
                continue
            if any(camera['location_id'] == config['location_id'] and wanted.intersection(camera['source_ids'])
                   for camera in view['cameras']):
                found[config['collection'], config['location_id']] = (config, view)
        if len(found) != 1:
            return None, None
        (config, view), = found.values()
        place = next(row for row in view['locations'] if row['id'] == config['location_id'])
        name, address = _text(place['name'], 200), _text(place.get('address'), 500) or ''
        if name is None:
            return None, None
        point = config.get('coordinates') or None
        coordinates = weather = None
        if point:
            coordinates = {key: point[key] for key in ('latitude', 'longitude', 'timezone', 'source')}
            # The weather place the page's header reads, in the shape the bundle reader carried.
            weather = {'name': name, 'address': address, 'latitude': point['latitude'],
                       'longitude': point['longitude'], 'timezone': point['timezone'],
                       'coordinate_source': point['source']}
            precision = _text((point.get('provenance') or {}).get('coordinate_precision'), 100)
            if precision:
                weather['coordinate_precision'] = precision
        record = {'id': place['id'], 'name': name, 'address': address,
                  'coordinates': coordinates, 'weather': weather}
        effective = view['effective']
        # Loaded with the scope store itself; the packaged read closure imports this module without it.
        from business_scope import ScopeActor
        actor = ScopeActor(scope.company_id, context.principal_id)
    except (ValueError, LookupError, StopIteration, TypeError, OSError, sqlite3.Error):
        return None, None
    # The host re-checks this before the answer leaves: a changed scope withdraws the record.
    return record, lambda: scope.current(actor, effective['collection'], effective['revision'],
                                         account_role=effective['account_role'])


def _error(exc, *, refused=503):
    """Stable sanitized outcomes; the selected authority stays selected in every one."""
    from realtime.store import AccessDenied, DeadlineExceeded
    if isinstance(exc, (TimeoutError, DeadlineExceeded)):
        return HTTPException(504, 'playback_deadline_exceeded')
    if isinstance(exc, AccessDenied):
        return HTTPException(404, 'playback_unavailable')
    if isinstance(exc, (ValueError, LookupError)):
        return HTTPException(refused, 'playback_unavailable')
    return HTTPException(503, 'playback_unavailable')


async def maintained_bootstrap(request):
    from realtime.media import PlaybackMediaStore
    from realtime_read_routes import _context
    try:
        async with asyncio.timeout(DEADLINE_S):
            context = await _context(request)
            files, catalog, revision = await _catalog(request, context)
            refs = [{'source_id': s.source_id, 'epoch_id': s.epoch_id, 'mapping_id': a.mapping_id}
                    for s in catalog.sources for a in s.artifacts]
            bindings = iter(await PlaybackMediaStore().bindings(context, refs))
            sources = []
            for si, source in enumerate(catalog.sources):
                artifacts = []
                for ai, artifact in enumerate(source.artifacts):
                    binding = next(bindings)
                    _match(artifact, binding)
                    base = f'/control-room/maintained/{revision}/{si}/{ai}/'
                    row = {k: binding[k] for k in ('mapping_id', 'source_asset_id', 'source_sha256',
                                                   'source_window', 'stream_window')}
                    row.update({k: getattr(artifact, k) for k in ('width', 'height', 'fps', 'duration_s')})
                    row['media_url'] = base + 'media'
                    for kind in ('poster', 'geometry'):
                        if getattr(artifact, kind):
                            row[kind + '_url'] = base + kind
                    if artifact.geometry:
                        row['geometry_binding'] = {'source_sha256': binding['source_sha256'],
                                                   'tracker_namespace': binding['tracker_namespace']}
                    artifacts.append(row)
                sources.append({'source_id': source.source_id, 'epoch_id': source.epoch_id,
                                'label': source.label, 'artifacts': artifacts})
            await asyncio.to_thread(
                files.sealed, [spec for s in catalog.sources for a in s.artifacts for spec in a.files()])
            try:
                async with asyncio.timeout(LOCATION_S):
                    location, current = await asyncio.to_thread(
                        _location, request.state.workspace, context, catalog)
            except TimeoutError:
                location = current = None
            guards = getattr(request.state, 'scope_guards', None)
            if current is not None and guards is not None:
                guards.append(current)
            return JSONResponse({'schema': SCHEMA, 'read_authority': 'maintained', 'sources': sources,
                                 'location': location},
                                headers={'Cache-Control': 'private, no-store'})
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc


async def _admit(request, address):
    """One complete decision: session identity, sealed catalog and current PostgreSQL lineage."""
    from realtime.media import PlaybackMediaStore
    from realtime_read_routes import _context
    revision, source_index, artifact_index, kind = address
    context = await _context(request)
    files, catalog, current = await _catalog(request, context)
    if current != revision:
        raise ValueError('Refresh the playback catalog')
    source = catalog.sources[source_index]
    artifact = source.artifacts[artifact_index]
    spec = getattr(artifact, kind)
    if spec is None:
        raise ValueError('Choose a published playback asset')
    ref = {'source_id': source.source_id, 'epoch_id': source.epoch_id, 'mapping_id': artifact.mapping_id}
    binding, = await PlaybackMediaStore().bindings(context, [ref])
    _match(artifact, binding)
    return files.file(spec, kind)


async def maintained_media(request, asset):
    try:
        match = ADDRESS.fullmatch(asset)
        if not match:
            raise ValueError('Choose a playback asset')
        address = (match[1], int(match[2]), int(match[3]), match[4])
        async with asyncio.timeout(DEADLINE_S):
            file = await _admit(request, address)
            response = await asyncio.to_thread(byte_response, file,
                                               range_header=request.headers.get('range'))
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc, refused=404) from exc
    response.headers['Cache-Control'] = 'private, no-store'
    body = getattr(response, 'body_iterator', None)
    if body is None:
        # An unsatisfiable range carries no bytes and holds no descriptor.
        return response
    release = response.background

    async def current():
        try:
            async with asyncio.timeout(DEADLINE_S):
                await _admit(request, address)
            return True
        except Exception:
            return False

    async def guarded():
        try:
            async for chunk in body:
                # Revoked or unavailable authority ends the body before this chunk leaves.
                if not await current():
                    return
                yield chunk
        finally:
            # Completion, revocation and disconnect all close the held descriptor here,
            # synchronously, so a cancelled request releases it as well.
            if release is not None and not release.is_async:
                release.func(*release.args, **release.kwargs)

    response.body_iterator = guarded()
    return response
