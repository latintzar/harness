"""Read sealed, company-scoped imported media as bytes, never decode it here.

The private intake database and successful preparation receipt authorize files.
Index rows are references to those records, not filesystem capabilities. Each
consumer receives a descriptor verified against the receipt, including on reopen.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import stat

PROFILE = 'source-media-prepare-v2'
RECIPE = 'rocoto-cfr24-h264-segments-v2'
IMAGE = 'sha256:5511c79cf8319967542048d9adcc77950f690ab0a54dc8f4ae74a17dc00315db'
SOURCE_BYTES = 256 * 1024**2
CLIP_BYTES = 8 * 1024**2
MANIFEST_BYTES = 512 * 1024
SHA = re.compile(r'[a-f0-9]{64}')
ASSET = re.compile(r'asset-[a-f0-9]{32}')


class ImportedMediaError(ValueError):
    """Stable sanitized errors: paths and private database values stay private."""


def imported_path(path):
    # Also protects legacy decoder entrypoints if an imported row loses its tag.
    try:
        candidate = Path(path)
        return 'source-intake' in candidate.parts or 'source-intake' in candidate.resolve().parts
    except (TypeError, ValueError, OSError, RuntimeError):
        return False


def imported_record(record):
    return record.get('storage_kind') == 'imported_source' or imported_path(record.get('path', ''))


def require_sandbox_derivation(record, purpose):
    if imported_record(record):
        raise ImportedMediaError('imported_' + purpose + '_requires_sandbox_profile')


def _number(value, low=0, high=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ImportedMediaError('invalid_imported_metadata')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ImportedMediaError('invalid_imported_metadata')
    return value


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class VerifiedFile:
    resolver: 'ImportedMedia'
    relative: tuple[str, ...]
    sha256: str
    size: int
    filename: str
    media_type: str
    maximum: int

    @property
    def path(self):
        """Compatibility reference only; HTTP/vision consumers use open/read_bytes."""
        return self.resolver.root.joinpath(*self.relative)

    def open(self):
        """Hash the held FD, check its identity, then rewind it for byte serving."""
        if not SHA.fullmatch(self.sha256) or not 0 < self.size <= self.maximum:
            raise ImportedMediaError('invalid_imported_file')
        stream = None
        try:
            fd = self.resolver._open(self.relative, sealed=True)
            stream = os.fdopen(fd, 'rb')
            before = os.fstat(stream.fileno())
            if before.st_size != self.size:
                raise ImportedMediaError('imported_file_integrity_failed')
            cache_key = (self.relative, self.sha256, _identity(before))
            if cache_key in self.resolver._verified:
                return stream
            digest = hashlib.sha256()
            remaining = self.size
            while remaining:
                data = stream.read(min(65536, remaining))
                if not data:
                    raise ImportedMediaError('imported_file_integrity_failed')
                digest.update(data); remaining -= len(data)
            if (stream.read(1) or digest.hexdigest() != self.sha256
                    or _identity(before) != _identity(os.fstat(stream.fileno()))):
                raise ImportedMediaError('imported_file_integrity_failed')
            self.resolver._verified[cache_key] = True
            while len(self.resolver._verified) > 256:
                self.resolver._verified.popitem(last=False)
            stream.seek(0)
            return stream
        except (OSError, ValueError) as exc:
            if stream is not None:
                stream.close()
            if isinstance(exc, ImportedMediaError):
                raise
            raise ImportedMediaError('imported_file_unavailable') from None

    def read_bytes(self, *, maximum=CLIP_BYTES):
        if self.size > maximum:
            raise ImportedMediaError('imported_read_limit')
        with self.open() as stream:
            return stream.read(self.size)

    def verify(self):
        with self.open():
            pass
        return self


@dataclass(frozen=True)
class ResolvedMedia:
    original: VerifiedFile
    prepared: VerifiedFile
    thumbnail: VerifiedFile
    metadata: dict
    clip: dict


class ImportedMedia:
    def __init__(self, state):
        # Trusted host configuration supplies state, never a request parameter.
        self.state = Path(state).absolute()
        self.root = self.state / 'source-intake'
        self._verified = OrderedDict()

    def _open(self, relative, *, sealed):
        if not relative or any(not isinstance(p, str) or p in {'', '.', '..'} or '/' in p or '\\' in p for p in relative):
            raise ImportedMediaError('invalid_imported_reference')
        # O_NOFOLLOW on every component, including the configured root's ancestry.
        parts = self.root.parts[1:] + tuple(relative)
        private_from = len(self.state.parts) - 1
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                opened = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if last else os.O_DIRECTORY), dir_fd=fd)
                os.close(fd); fd = opened
                info = os.fstat(fd)
                if index >= private_from and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                    raise ImportedMediaError('invalid_imported_permissions')
                if last and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                             or (sealed and info.st_mode & 0o222)):
                    raise ImportedMediaError('invalid_imported_file')
            result = fd; fd = None
            return result
        except OSError:
            raise ImportedMediaError('imported_file_unavailable') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _authority(self, asset_id):
        fd = self._open(('intake.sqlite3',), sealed=False)
        try:
            # The private store is host-owned; an open FD checks its identity
            # around the consistent read transaction, while SQLite reads its WAL.
            before = os.fstat(fd)
            with sqlite3.connect(self.root.joinpath('intake.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN')
                asset = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
                if asset is None or asset['status'] != 'ready':
                    raise ImportedMediaError('imported_asset_unavailable')
                source = db.execute('SELECT * FROM sources WHERE id=?', (asset['source_id'],)).fetchone()
                job = db.execute("SELECT result FROM jobs WHERE asset_id=? AND stage='prepare' AND status='succeeded'", (asset_id,)).fetchone()
                if source is None or source['owner'] != asset['owner'] or job is None:
                    raise ImportedMediaError('imported_preparation_unavailable')
                if not isinstance(job['result'], str) or len(job['result']) > MANIFEST_BYTES:
                    raise ImportedMediaError('invalid_imported_receipt')
                receipt = json.loads(job['result'])
            current = os.stat(self.root/'intake.sqlite3', follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise ImportedMediaError('imported_store_changed')
            return dict(asset), dict(source), receipt
        except (sqlite3.Error, OSError, json.JSONDecodeError):
            raise ImportedMediaError('imported_store_unavailable') from None
        finally:
            os.close(fd)

    def resolve(self, record):
        try:
            return self._resolve(record)
        except (KeyError, TypeError, OverflowError, json.JSONDecodeError):
            raise ImportedMediaError('invalid_imported_reference') from None

    def _resolve(self, record):
        asset_id = record.get('video_id', '')
        if record.get('storage_kind') != 'imported_source' or not ASSET.fullmatch(asset_id):
            raise ImportedMediaError('invalid_imported_reference')
        asset, source, receipt = self._authority(asset_id)
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in
                [('profile', PROFILE), ('recipe', RECIPE), ('image_digest', IMAGE), ('original_sha256', asset['sha256'])]):
            raise ImportedMediaError('invalid_imported_receipt')
        artifact = receipt.get('artifact_id', '')
        if not re.fullmatch(r'prepare-[a-f0-9]{32}', artifact):
            raise ImportedMediaError('invalid_imported_receipt')
        base = ('worker-artifacts', asset_id, artifact)
        fd = self._open(base+('manifest.json',), sealed=True)
        try:
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        marker = VerifiedFile(self, base+('manifest.json',), receipt['manifest_sha256'], size,
                              'manifest.json', 'application/json', MANIFEST_BYTES)
        manifest = json.loads(marker.read_bytes(maximum=MANIFEST_BYTES))
        if (manifest.get('schema_version') != 1 or manifest.get('recipe') != RECIPE
                or manifest.get('asset_id') != asset_id or manifest.get('collection') != source['collection']
                or manifest.get('original') != {'sha256': asset['sha256'], 'size_bytes': asset['bytes']}
                or record.get('dataset') != source['collection']
                or record.get('camera') != source['name']
                or manifest.get('normalization', {}).get('fps') != 24
                or manifest.get('normalization', {}).get('window_seconds') != 8):
            raise ImportedMediaError('imported_scope_mismatch')
        clips = manifest.get('clips')
        if not isinstance(clips, list) or not 1 <= len(clips) <= 180 or receipt.get('clip_count') != len(clips):
            raise ImportedMediaError('invalid_imported_manifest')
        candidates = [(i,c) for i,c in enumerate(clips) if isinstance(c,dict) and c.get('id') == record.get('id')]
        if len(candidates) != 1:
            raise ImportedMediaError('invalid_imported_reference')
        number, clip = candidates[0]
        stem = f'clip-{number:06d}'
        thumb = clip['thumbnail']
        if (clip.get('id') != asset_id+'-'+stem or clip.get('asset_id') != asset_id
                or clip.get('collection') != source['collection'] or clip.get('file') != stem+'.mp4'
                or thumb.get('file') != stem+'.jpg'):
            raise ImportedMediaError('invalid_imported_manifest')
        source_meta = manifest['source']
        duration = _number(source_meta['duration_seconds'], .001, 1800)
        start = _number(clip['source_elapsed_start_seconds'], 0, duration)
        end = _number(clip['source_elapsed_end_seconds'], 0, duration)
        length = _number(clip['duration_seconds'], .001, 8.25)
        origin = _number(source_meta['origin_seconds'], -1e12)
        if (not start < end or abs(start - number * 8) > .001 or abs(end-start-length) > .05
                or abs(_number(clip['original_pts_start_seconds'], -1e12)-origin-start) > .001
                or abs(_number(clip['original_pts_end_seconds'], -1e12)-origin-end) > .001
                or source_meta.get('capture_utc') is not None or source_meta.get('capture_clock') != 'unknown'):
            raise ImportedMediaError('invalid_imported_timing')
        filename = asset['filename']
        if not isinstance(filename, str) or not filename or len(filename) > 240 or any(c in filename for c in '/\\\x00\r\n'):
            raise ImportedMediaError('invalid_imported_filename')
        original = VerifiedFile(self, ('originals',asset['sha256']), asset['sha256'],
            _integer(asset['bytes'],1,SOURCE_BYTES), filename,
            mimetypes.guess_type(filename)[0] or 'application/octet-stream', SOURCE_BYTES)
        prepared = VerifiedFile(self, base+(clip['file'],), clip['sha256'],
            _integer(clip['size_bytes'],1,CLIP_BYTES), clip['file'],'video/mp4',CLIP_BYTES)
        thumbnail = VerifiedFile(self, base+(thumb['file'],), thumb['sha256'],
            _integer(thumb['size_bytes'],1,CLIP_BYTES),thumb['file'],'image/jpeg',CLIP_BYTES)
        expected = dict(path=str(original.path), preparation_profile=PROFILE, preparation_recipe=RECIPE,
            preparation_image_digest=IMAGE, prepared_manifest_sha256=marker.sha256,
            prepared_path=str(prepared.path), prepared_sha256=prepared.sha256, prepared_size_bytes=prepared.size,
            prepared_width=_integer(clip['width'],2,640), prepared_height=_integer(clip['height'],2,640),
            prepared_start=0., prepared_end=length, start=start, end=end, duration=duration,
            original_pts_start_seconds=clip['original_pts_start_seconds'],original_pts_end_seconds=clip['original_pts_end_seconds'],
            thumbnail_path=str(thumbnail.path), thumbnail_sha256=thumbnail.sha256, thumbnail_size_bytes=thumbnail.size)
        if any(record.get(key) != value for key, value in expected.items()):
            raise ImportedMediaError('imported_record_mismatch')
        for key,value in dict(asset_id=asset_id,source_id=source['id'],source_object_id=asset['object_id'],original_sha256=asset['sha256']).items():
            if key in record and record[key] != value:
                raise ImportedMediaError('imported_scope_mismatch')
        # Validate all references before making any available. Reads reopen and
        # recheck permissions/identity; checksum caching includes inode and ctime.
        original.verify(); prepared.verify(); thumbnail.verify()
        width = _integer(source_meta['coded_width'],2,4096)
        height = _integer(source_meta['coded_height'],2,4096)
        if width*height > 4096*2160:
            raise ImportedMediaError('invalid_imported_metadata')
        frames = _integer(source_meta['frame_count'],1,108000)
        rotation = _number(source_meta.get('rotation_degrees'), -360, 360)
        if rotation % 90 or type(source_meta.get('variable_frame_intervals')) is not bool:
            raise ImportedMediaError('invalid_imported_metadata')
        aspect = source_meta.get('sample_aspect_ratio')
        if not isinstance(aspect, str) or not re.fullmatch(r'[0-9]{1,8}:[0-9]{1,8}', aspect):
            raise ImportedMediaError('invalid_imported_metadata')
        metadata = dict(width=width,height=height,frame_count=frames,fps=frames/duration,
            fps_basis='average_frames_per_source_second',duration=duration,bytes=original.size,
            sha256=original.sha256,metadata_basis='sandbox_source_probe',
            rotation_degrees=source_meta.get('rotation_degrees'),sample_aspect_ratio=source_meta.get('sample_aspect_ratio'),
            variable_frame_intervals=source_meta.get('variable_frame_intervals'),capture_clock='unknown',capture_utc=None)
        return ResolvedMedia(original,prepared,thumbnail,metadata,dict(clip))

    def frame_input(self, record):
        """The available image is the first 640px playback frame, not a video verdict."""
        resolved = self.resolve(record)
        return {'clip':record, 'frames':[{'time':record['start'],'frame':None,
            'image_bytes':resolved.thumbnail.read_bytes(), 'basis':'first_prepared_frame',
            'width':record['prepared_width'],'height':record['prepared_height']}],
            'verification':'One prepared preview frame; detailed video inspection requires its sandbox profile.'}


def byte_response(file, *, range_header=None, attachment=False):
    """Range-capable HTTP response over a verified held FD, with bounded chunks.

    Call from a worker thread for large original checksums. Never hand path to a
    FileResponse: that would reopen it after this module's integrity check.
    """
    from starlette.background import BackgroundTask
    from starlette.responses import Response, StreamingResponse
    from urllib.parse import quote
    start,end = 0,file.size-1
    if range_header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
        try:
            if not match or not any(match.groups()): raise ValueError()
            first,last = match.groups()
            if first:
                start=int(first);end=min(int(last),end) if last else end
            else:
                length=int(last)
                if length<=0: raise ValueError()
                start=max(0,file.size-length)
            if not 0<=start<=end<file.size: raise ValueError()
        except ValueError:
            return Response(status_code=416,headers={'Content-Range':f'bytes */{file.size}'})
    stream = file.open()
    before = _identity(os.fstat(stream.fileno()))
    stream.seek(start)
    def chunks():
        remaining=end-start+1
        try:
            while remaining:
                data=stream.read(min(65536,remaining))
                if not data or _identity(os.fstat(stream.fileno()))!=before:
                    raise ImportedMediaError('imported_file_changed')
                remaining-=len(data)
                yield data
        finally:
            stream.close()
    headers={'Content-Length':str(end-start+1),'Accept-Ranges':'bytes','ETag':'"'+file.sha256+'"',
             'X-Content-Type-Options':'nosniff', 'Cache-Control':'private, no-cache',
             'Content-Disposition':('attachment' if attachment else 'inline')+"; filename*=UTF-8''"+quote(file.filename,safe='')}
    if range_header: headers['Content-Range']=f'bytes {start}-{end}/{file.size}'
    return StreamingResponse(chunks(),status_code=206 if range_header else 200,
        media_type=file.media_type,headers=headers,background=BackgroundTask(stream.close))
