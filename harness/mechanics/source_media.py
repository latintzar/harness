"""Fixed, isolated media preparation broker. This module never decodes media.

Only the commissioned preview UID can submit a read-only sealed file descriptor.
The host coordinator supplies authenticated scope and SourceStore lease metadata.
No model/browser path, image, command, URL or mount is part of this protocol.
"""
from __future__ import annotations

import argparse
import array
import asyncio
from dataclasses import asdict, dataclass
import fcntl
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import struct
import time

from native_executor import Ext4Volumes, _command
import document_preview_contract as documents

IMAGE = 'sha256:5511c79cf8319967542048d9adcc77950f690ab0a54dc8f4ae74a17dc00315db'
PROFILE = 'source-media-prepare-v2'
DERIVE_IMAGE = 'sha256:0aea150cbf82b976f602bf34a376c608d361a8c16d5496180dfc22d6c24c4b18'
DERIVE_PROFILE = 'source-media-derive-v1'
DERIVE_RECIPE = 'rocoto-source-derive-v1'
DERIVE_PURPOSES = {'detail_video_v1', 'inspect_frames_v1', 'source_cut_v1'}
WINDOW_SECONDS = 8
PURPOSE = 'prepare'
SCHEMA_VERSION = 1
SOCKET = '/run/rocoto-source-media/control.sock'
ROOT = '/var/lib/rocoto-source-media'
INPUT_BYTES = 256 * 1024**2
OUTPUT_BYTES = 512 * 1024**2
CLIP_BYTES = 8 * 1024**2
CONTROL_BYTES = 32 * 1024
PACKET_BYTES = 64 * 1024
MAX_FILES = 363
WALL_SECONDS = 620
MAX_ACTIVE = 2
MAX_RETAINED = 4
WORKER_UID = 10001
RECIPE = 'rocoto-cfr24-h264-segments-v2'
TERMINAL = {'succeeded', 'failed', 'stopped', 'released'}


class MediaError(RuntimeError):
    pass


def _require(condition, code):
    if not condition:
        raise MediaError(code)


def _identifier(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class MediaScope:
    company_id: str
    owner_id: str
    asset_id: str
    job_id: str

    def __post_init__(self):
        for value in (self.company_id, self.owner_id):
            _require(isinstance(value, str) and 0 < len(value.encode()) <= 512
                     and not any(ord(c) < 32 for c in value), 'invalid_scope')
        _require(_identifier(self.asset_id, r'asset-[a-f0-9]{32}') and
                 _identifier(self.job_id, r'job-[a-f0-9]{32}'), 'invalid_scope')

    @property
    def key(self):
        return hashlib.sha256(_json(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True)
class MediaLease:
    scope_key: str
    generation: int
    token: str

    def __post_init__(self):
        _require(_identifier(self.scope_key, r'[a-f0-9]{64}') and
                 type(self.generation) is int and 0 < self.generation < 2**63 and
                 _identifier(self.token, r'[a-f0-9]{48}'), 'invalid_lease')


@dataclass(frozen=True)
class DerivationRequest:
    purpose: str
    clip_id: str
    start: float
    end: float
    frame_seconds: tuple = ()

    def __post_init__(self):
        _require(isinstance(self.purpose,str) and self.purpose in DERIVE_PURPOSES
            and _identifier(self.clip_id,r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
            and _number(self.start) and _number(self.end) and self.start>=0
            and 0<self.end-self.start<=8.25, 'invalid_derivation_request')
        _require(isinstance(self.frame_seconds,(list,tuple)) and all(_number(t) for t in self.frame_seconds),
                 'invalid_frame_selection')
        frames=tuple(float(t) for t in self.frame_seconds)
        if self.purpose=='inspect_frames_v1':
            _require(1<=len(frames)<=3 and frames==tuple(sorted(set(frames)))
                and all(0<=t<self.end-self.start for t in frames), 'invalid_frame_selection')
        else: _require(not frames, 'invalid_frame_selection')
        object.__setattr__(self,'start',float(self.start));object.__setattr__(self,'end',float(self.end))
        object.__setattr__(self,'frame_seconds',frames)

    @classmethod
    def parse(cls,value):
        _require(isinstance(value,dict) and set(value)=={'purpose','clip_id','start','end','frame_seconds'},
                 'invalid_derivation_request')
        return cls(**value)

    def manifest_request(self,scope,collection,checksum):
        return {'asset_id':scope.asset_id,'collection':collection,'clip_id':self.clip_id,'purpose':self.purpose,
            'source_elapsed_start_seconds':self.start,'source_elapsed_end_seconds':self.end,
            'frame_seconds':list(self.frame_seconds),'original_sha256':checksum}


@dataclass(frozen=True)
class MediaSpec:
    scope_key: str
    generation: int
    asset_id: str
    collection: str
    directory: Path
    volume: Path
    derivation: DerivationRequest | None = None
    document: documents.DocumentRequest | None = None

    @property
    def wall_seconds(self):
        return documents.WALL_SECONDS if self.document else WALL_SECONDS

    @property
    def image(self):
        return documents.IMAGE if self.document else DERIVE_IMAGE if self.derivation else IMAGE

    @property
    def arguments(self):
        if self.document:return ["--file-id",self.document.file_id,"--format",self.document.format]
        args=["--asset-id",self.asset_id,"--collection",self.collection]
        if self.derivation:
            d=self.derivation
            args += ["--clip-id",d.clip_id,"--purpose",d.purpose,"--start",str(d.start),"--end",str(d.end)]
            for stamp in d.frame_seconds: args += ["--frame-seconds",str(stamp)]
        return args

    @property
    def name(self):
        return f'rocoto-source-media-{self.scope_key}-{self.generation}'

    @property
    def original(self):
        return self.directory / 'original'

    @property
    def output(self):
        return self.volume / 'workspace'


class MediaDocker:
    """Fixed image/profile. No request-selected Docker arguments are accepted."""
    def __init__(self, root):
        # Requires the additive size_bytes constructor patch shipped with this module.
        self.volumes = Ext4Volumes(Path(root) / 'volumes', size_bytes=OUTPUT_BYTES)
        self.docker = ('/opt/rocoto-native-executor/docker-29.8.2/docker', '--host',
                       'unix:///run/rocoto-native-docker/docker.sock')

    def volume_path(self, key, generation):
        return self.volumes.root / f'{key}-{generation}' / 'mount'

    async def mount(self, spec):
        path = await self.volumes.mount(f'{spec.scope_key}-{spec.generation}')
        _require(path == spec.volume and not any(spec.output.iterdir()), 'invalid_output_volume')

    def flags(self, spec):
        return ['--name', spec.name, '--label', 'com.rocoto.source-media=true',
            '--runtime', 'rocoto-runsc', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', '10001:10001', '--cpus', '2', '--memory', '1073741824',
            '--memory-swap', '1073741824', '--pids-limit', '64', '--init',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777',
            '--log-driver', 'none',
            '--mount', f'type=bind,src={spec.original},dst=/input/original,readonly',
            '--mount', f'type=bind,src={spec.output},dst=/output']

    async def start(self, spec):
        await _command(*self.docker, 'create', *self.flags(spec), spec.image, *spec.arguments)
        value = json.loads(await _command(*self.docker, 'inspect', spec.name))[0]
        host = value['HostConfig']
        expected_mounts = {('/input/original', str(spec.original), False),
                           ('/output', str(spec.output), True)}
        _require(value['Image'] == spec.image and host['Runtime'] == 'rocoto-runsc'
            and host['NetworkMode'] == 'none' and host['ReadonlyRootfs']
            and host['Memory'] == host['MemorySwap'] == 1073741824
            and host['NanoCpus'] == 2_000_000_000 and host['PidsLimit'] == 64
            and host['CapDrop'] == ['ALL'] and 'no-new-privileges' in host['SecurityOpt']
            and host['Init'] and value['Config']['User'] == '10001:10001'
            and len(value['Mounts']) == 2
            and {(x['Destination'], x['Source'], x['RW']) for x in value['Mounts']} == expected_mounts,
            'media_profile_mismatch')
        await _command(*self.docker, 'start', spec.name)

    async def wait(self, spec):
        output = await _command(*self.docker, 'wait', spec.name, timeout=spec.wall_seconds)
        _require(output.strip().isdigit(), 'invalid_worker_exit')
        return int(output.strip())

    async def stop(self, spec):
        async def present():
            # A successful exact-name list proves absence; inspect errors do not.
            return bool((await _command(*self.docker, 'ps', '--all', '--quiet',
                        '--filter', 'name=^/' + spec.name + '$')).strip())
        if await present():
            await _command(*self.docker, 'rm', '--force', spec.name)
        _require(not await present(), 'worker_stop_unconfirmed')

    async def unmount(self, spec):
        await self.volumes.unmount(spec.volume)
        if spec.volume.parent.exists():
            shutil.rmtree(spec.volume.parent, ignore_errors=False)


def _directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(), 'invalid_broker_directory')
    path.chmod(0o700)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(parent_fd)
    finally: os.close(parent_fd)


def _identity(info):
    return tuple(getattr(info, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _copy_original(fd, destination, *, uid, size, checksum):
    before = os.fstat(fd)
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == uid
        and stat.S_IMODE(before.st_mode) == 0o400
        and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        and before.st_size == size, 'invalid_original_descriptor')
    digest = hashlib.sha256()
    with destination.open('xb') as output:
        position = 0
        while position < size:
            data = os.pread(fd, min(64 * 1024, size - position), position)
            _require(bool(data), 'original_truncated')
            output.write(data); digest.update(data); position += len(data)
        _require(not os.pread(fd, 1, size) and _identity(before) == _identity(os.fstat(fd))
                 and digest.hexdigest() == checksum, 'original_integrity_failed')
        output.flush(); os.fsync(output.fileno())
    destination.chmod(0o444)


def _read_json(path, limit):
    _require(path.stat().st_size <= limit, 'manifest_limit')
    try:
        value = json.loads(path.read_bytes())
        _json(value)  # Reject non-finite numbers, including optional metadata.
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise MediaError('invalid_manifest') from None


def _validate_manifest(manifest, files, *, scope, collection, size, checksum, sealed):
    by_path = {entry['path']: entry for entry in files}
    _require(isinstance(manifest, dict) and manifest.get('schema_version') == 1
        and manifest.get('recipe') == RECIPE and manifest.get('asset_id') == scope.asset_id
        and manifest.get('collection') == collection
        and manifest.get('original') == {'sha256': checksum, 'size_bytes': size}, 'manifest_provenance')
    source = manifest.get('source', {})
    _require(isinstance(source, dict) and source.get('capture_utc') is None
        and source.get('capture_clock') == 'unknown'
        and _number(source.get('duration_seconds')) and 0 < source['duration_seconds'] <= 1800
        and _number(source.get('origin_seconds'))
        and type(source.get('origin_pts')) is int
        and type(source.get('frame_count')) is int and 0 < source['frame_count'] <= 108000,
        'invalid_source_timing')
    normal = manifest.get('normalization', {})
    _require(isinstance(normal, dict) and normal.get('fps') == 24 and normal.get('window_seconds') == WINDOW_SECONDS
        and normal.get('rounding_bound_seconds') == 1/24 and normal.get('rotation_applied') is True
        and isinstance(normal.get('mapping'), str) and len(normal['mapping']) <= 1024, 'invalid_normalization')
    timing = manifest.get('frame_timing', {})
    _require(isinstance(timing, dict) and timing.get('file') == 'frame-timing.json'
             and by_path.get('frame-timing.json', {}).get('sha256') == timing.get('sha256'), 'invalid_frame_timing')
    frame_data = _read_json(sealed / str(by_path['frame-timing.json']['index']), 32 * 1024**2)
    try:
        timebase = Fraction(source['time_base'])
        frames = frame_data['frames']
        _require(timebase > 0 and frame_data['time_base'] == source['time_base']
            and isinstance(frames, list) and len(frames) == source['frame_count'], 'invalid_frame_timing')
        previous = None
        for frame in frames:
            pts, duration = frame['pts'], frame['duration_ticks']
            _require(type(pts) is int and (previous is None or pts > previous)
                     and (duration is None or type(duration) is int and duration > 0), 'invalid_frame_timing')
            previous = pts
        _require(frames[0]['pts'] == source['origin_pts'] and type(frames[-1]['duration_ticks']) is int
            and abs(float(frames[0]['pts'] * timebase) - source['origin_seconds']) < 1e-6
            and abs(float((frames[-1]['pts'] + frames[-1]['duration_ticks'] - frames[0]['pts']) * timebase)
                    - source['duration_seconds']) < 1e-6, 'invalid_frame_timing')
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise MediaError('invalid_frame_timing') from None
    clips = manifest.get('clips')
    _require(isinstance(clips, list) and 0 < len(clips) <= 180, 'invalid_clips')
    expected = {'manifest.json', 'frame-timing.json'}
    ids = []
    for index, clip in enumerate(clips):
        name = f'clip-{index:06d}'
        _require(isinstance(clip, dict) and clip.get('id') == f'{scope.asset_id}-{name}'
            and clip.get('asset_id') == scope.asset_id and clip.get('collection') == collection
            and clip.get('file') == name + '.mp4', 'invalid_clip_identity')
        for field in ('duration_seconds', 'source_elapsed_start_seconds', 'source_elapsed_end_seconds',
                      'original_pts_start_seconds', 'original_pts_end_seconds'):
            _require(_number(clip.get(field)), 'invalid_clip_timing')
        duration, start, end = clip['duration_seconds'], clip['source_elapsed_start_seconds'], clip['source_elapsed_end_seconds']
        _require(0 < duration <= WINDOW_SECONDS + .25 and start == index * WINDOW_SECONDS and start < end <= source['duration_seconds']
            and abs(end - min(source['duration_seconds'], start + duration)) < 1e-6
            and abs(clip['original_pts_start_seconds'] - (source['origin_seconds'] + start)) < 1e-6
            and abs(clip['original_pts_end_seconds'] - (source['origin_seconds'] + end)) < 1e-6
            and all(type(clip.get(k)) is int and 0 < clip[k] <= 640 for k in ('width', 'height')),
            'invalid_clip_timing')
        thumbnail = clip.get('thumbnail', {})
        _require(isinstance(thumbnail, dict) and thumbnail.get('file') == name + '.jpg', 'invalid_thumbnail')
        for record in (clip, thumbnail):
            verified = by_path.get(record.get('file'), {})
            _require(type(record.get('size_bytes')) is int
                     and verified.get('sha256') == record.get('sha256')
                     and verified.get('size_bytes') == record.get('size_bytes'), 'output_integrity_failed')
            expected.add(record['file'])
        ids.append(clip['id'])
    _require(source['duration_seconds'] - clips[-1]['source_elapsed_end_seconds'] <= 1/24 + .001
             and set(by_path) == expected, 'incomplete_output')
    batches = manifest.get('embedding_batches')
    _require(isinstance(batches, list) and batches and all(isinstance(b, list) and 0 < len(b) <= 160 for b in batches)
             and [item for batch in batches for item in batch] == ids, 'invalid_embedding_batches')
    sizes = {clip['id']: clip['size_bytes'] for clip in clips}
    _require(all(sum(sizes[item] for item in batch) <= 64 * 1024**2 for batch in batches), 'invalid_embedding_batches')


def _validate_derived_manifest(manifest, files, *, scope, collection, size, checksum, request):
    """Validate fixed-profile claims and sealed byte hashes without media decoding."""
    expected_request=request.manifest_request(scope,collection,checksum)
    _require(isinstance(manifest,dict) and manifest.get('schema_version')==1
        and manifest.get('profile')==DERIVE_PROFILE and manifest.get('recipe')==DERIVE_RECIPE
        and manifest.get('purpose')==request.purpose and manifest.get('asset_id')==scope.asset_id
        and manifest.get('collection')==collection and manifest.get('clip_id')==request.clip_id
        and manifest.get('original')=={'sha256':checksum,'size_bytes':size}
        and manifest.get('request')==expected_request
        and manifest.get('request_sha256')==hashlib.sha256(_json(expected_request).encode()).hexdigest(),
        'derivation_provenance')
    source=manifest.get('source',{})
    _require(isinstance(source,dict) and _number(source.get('duration_seconds'))
        and 0<request.end<=source['duration_seconds']<=1800
        and _number(source.get('origin_seconds')) and abs(source['origin_seconds'])<=1e12
        and type(source.get('origin_pts')) is int
        and type(source.get('frame_count')) is int and 0<source['frame_count']<=108000
        and source.get('capture_clock')=='unknown' and source.get('capture_utc') is None
        and manifest.get('clock')=='source_relative' and manifest.get('capture_utc') is None,
        'invalid_derivation_source')
    try:
        _require(isinstance(source.get('time_base'),str) and len(source['time_base'])<=80,'invalid_derivation_source')
        timebase=Fraction(source['time_base'])
        _require(timebase>0 and abs(float(timebase*source['origin_pts'])-source['origin_seconds'])<1e-5,
            'invalid_derivation_source')
    except (ValueError,ZeroDivisionError,OverflowError):raise MediaError('invalid_derivation_source') from None
    _require(all(type(source.get(k)) is int and 2<=source[k]<=4096 for k in ('coded_width','coded_height'))
        and source['coded_width']*source['coded_height']<=4096*2160
        and _number(source.get('rotation_degrees')) and source['rotation_degrees']%90==0,'invalid_derivation_source')
    origin=source['origin_seconds']
    for field,value in [('requested_original_pts_start_seconds',origin+request.start),
                        ('requested_original_pts_end_seconds',origin+request.end)]:
        _require(_number(manifest.get(field)) and abs(manifest[field]-value)<1e-5,'invalid_derivation_timing')
    outputs=manifest.get('files')
    _require(isinstance(outputs,list) and len(outputs)==(len(request.frame_seconds) if request.purpose=='inspect_frames_v1' else 1),
        'invalid_derivation_files')
    by_path={entry['path']:entry for entry in files};expected={'manifest.json'}
    def dimensions(row):
        bound=(4096,4096) if request.purpose=='source_cut_v1' else (1920,1080)
        _require(all(type(row.get(k)) is int and 2<=row[k]<=v for k,v in zip(('width','height'),bound))
            and row['width']*row['height']<=4096*2160,'invalid_derived_dimensions')
    def source_frame(frame):
        _require(isinstance(frame,dict) and type(frame.get('source_frame_index')) is int
            and 0<=frame['source_frame_index']<source['frame_count'] and type(frame.get('original_pts')) is int
            and all(_number(frame.get(k)) for k in ('original_pts_seconds','source_elapsed_seconds'))
            and abs(frame['original_pts_seconds']-float(frame['original_pts']*timebase))<1e-5
            and abs(frame['source_elapsed_seconds']-(frame['original_pts_seconds']-origin))<1e-5
            and 0<=frame['source_elapsed_seconds']<source['duration_seconds']
            and (frame.get('duration_seconds') is None or _number(frame['duration_seconds']) and frame['duration_seconds']>0),
            'invalid_derived_frame')
    for number,row in enumerate(outputs):
        _require(isinstance(row,dict),'invalid_derivation_files')
        filename=f'frame-{number:02d}.jpg' if request.purpose=='inspect_frames_v1' else 'clip.mp4'
        expected.add(filename);sealed=by_path.get(filename,{})
        maximum=64*1024**2 if request.purpose=='source_cut_v1' else CLIP_BYTES
        _require(row.get('file')==filename and _identifier(row.get('sha256'),r'[a-f0-9]{64}')
            and type(row.get('size_bytes')) is int and 0<row['size_bytes']<=maximum
            and row['sha256']==sealed.get('sha256') and row['size_bytes']==sealed.get('size_bytes'),
            'derived_file_integrity')
        dimensions(row)
        if request.purpose=='inspect_frames_v1':
            sample=request.frame_seconds[number]
            _require(row.get('mime_type')=='image/jpeg' and row.get('requested_clip_seconds')==sample
                and _number(row.get('requested_source_seconds'))
                and abs(row['requested_source_seconds']-request.start-sample)<1e-5,'invalid_frame_selection')
            source_frame(row)
            _require(row['source_elapsed_seconds']<=row['requested_source_seconds']+1e-5,'invalid_frame_selection')
            continue
        count=row.get('frame_count');duration=row.get('duration_seconds');stamps=row.get('frame_pts_seconds')
        _require(row.get('mime_type')=='video/mp4' and row.get('video_codec')=='h264'
            and type(count) is int and 1<=count<=2048 and _number(duration) and 0<duration<=8.25
            and isinstance(stamps,list) and len(stamps)==count and all(_number(t) and 0<=t<=duration for t in stamps)
            and abs(stamps[0])<.001 and all(b>a for a,b in zip(stamps,stamps[1:])), 'invalid_derived_video')
        _require(isinstance(row.get('audio_codecs'),list) and len(row['audio_codecs'])<=1
            and all(v=='aac' for v in row['audio_codecs']) and isinstance(row.get('audio_start_seconds'),list)
            and len(row['audio_start_seconds'])==len(row['audio_codecs'])
            and all(_number(t) and -.1<=t<=duration for t in row['audio_start_seconds']), 'invalid_derived_audio')
        for field in ('time_base','average_frame_rate','nominal_frame_rate'):
            try:
                _require(isinstance(row.get(field),str) and len(row[field])<=80 and Fraction(row[field])>0,'invalid_derived_video')
            except (ValueError,ZeroDivisionError):raise MediaError('invalid_derived_video') from None
        selected=row.get('source_frames')
        _require(isinstance(selected,list) and 1<=len(selected)<=2048,'invalid_derived_frame')
        for frame in selected:source_frame(frame)
        _require(all(b['source_frame_index']==a['source_frame_index']+1
            and b['original_pts']>a['original_pts'] for a,b in zip(selected,selected[1:])), 'invalid_derived_frame')
        first=selected[0]['source_elapsed_seconds']
        _require(first>=request.start-1e-5 and selected[-1]['source_elapsed_seconds']<request.end
            and _number(row.get('actual_source_first_frame_seconds')) and abs(row['actual_source_first_frame_seconds']-first)<1e-5
            and row.get('actual_original_first_frame_pts')==selected[0]['original_pts']
            and _number(row.get('actual_source_end_seconds')) and abs(row['actual_source_end_seconds']-first-duration)<1e-5
            and type(row.get('requested_interval_is_frame_aligned')) is bool and row.get('rotation_applied') is True,
            'invalid_derivation_timing')
        if request.purpose=='detail_video_v1':
            _require(count<=33 and row.get('average_frame_rate')=='4/1' and not row['audio_codecs']
                and row.get('mapping_basis')=='nominal_source_elapsed_4fps_resampling'
                and row.get('resampling_rounding_bound_seconds')==.25
                and all(abs(stamp-i/4)<1e-5 for i,stamp in enumerate(stamps)), 'invalid_detail_profile')
        else:
            width,height=source['coded_width'],source['coded_height']
            if abs(source['rotation_degrees'])%180:width,height=height,width
            _require((row['width'],row['height'])==(width//2*2,height//2*2),'invalid_source_cut_dimensions')
            _require(count==len(selected) and row.get('mapping_basis')=='preserved_source_frame_pts'
                and row.get('resampling_rounding_bound_seconds')==0
                and all(abs(stamp-(frame['source_elapsed_seconds']-first))<=max(float(timebase),1e-6)
                    for stamp,frame in zip(stamps,selected)), 'invalid_source_cut_profile')
    _require(set(by_path)==expected,'invalid_derivation_files')


def _snapshot(spec, *, scope, size, checksum, worker_uid):
    """Copy ordinary files only after the whole worker is confirmed absent."""
    source = spec.output / ('preview' if spec.document else 'derived' if spec.derivation else 'prepared')
    sealed = spec.directory / 'sealed'
    sealed.mkdir(mode=0o700)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    files = []
    try:
        names = sorted(os.listdir(source_fd))
        _require(0 < len(names) <= (documents.MAX_PAGES+2 if spec.document else 4 if spec.derivation else MAX_FILES), 'output_file_limit')
        total = 0
        for index, name in enumerate(names):
            if spec.document:
                _require(name in {'manifest.json','text.txt'} or _identifier(name,r'page-[0-9]{4}\.png'),'invalid_output_path')
            elif spec.derivation:
                _require(name in {'manifest.json','clip.mp4','frame-00.jpg','frame-01.jpg','frame-02.jpg'},'invalid_output_path')
            else:
                _require(name in {'manifest.json', 'frame-timing.json'} or
                         _identifier(name, r'clip-[0-9]{6}\.(mp4|jpg)'), 'invalid_output_path')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
            try:
                before = os.fstat(fd)
                maximum = 32 * 1024**2 if name == 'frame-timing.json' else 1024**2 if name == 'manifest.json' else CLIP_BYTES
                if spec.derivation:
                    maximum = 512*1024 if name=='manifest.json' else (64*1024**2 if name=='clip.mp4' and spec.derivation.purpose=='source_cut_v1' else CLIP_BYTES)
                if spec.document:
                    maximum=1024**2 if name=='manifest.json' else documents.MAX_TEXT if name=='text.txt' else documents.MAX_PAGE
                _require(stat.S_ISREG(before.st_mode) and before.st_uid == worker_uid and before.st_nlink == 1
                    and (0 <= before.st_size <= maximum if spec.document and name=='text.txt' else 0 < before.st_size <= maximum), 'invalid_output_file')
                total += before.st_size
                _require(total <= (documents.MAX_OUTPUT if spec.document else OUTPUT_BYTES), 'output_size_limit')
                digest = hashlib.sha256()
                with (sealed / str(index)).open('xb') as out:
                    position = 0
                    while position < before.st_size:
                        data = os.read(fd, min(64 * 1024, before.st_size - position))
                        _require(bool(data), 'output_truncated')
                        position += len(data); out.write(data); digest.update(data)
                    _require(not os.read(fd, 1) and _identity(os.fstat(fd)) == _identity(before), 'output_changed')
                    out.flush(); os.fsync(out.fileno())
                (sealed / str(index)).chmod(0o400)
                files.append({'index': index, 'path': name, 'size_bytes': before.st_size, 'sha256': digest.hexdigest()})
            finally:
                os.close(fd)
        entry = next((x for x in files if x['path'] == 'manifest.json'), None)
        _require(entry is not None, 'manifest_missing')
        manifest = _read_json(sealed / str(entry['index']), 1024**2)
        if spec.document:
            try:documents.validate_manifest(manifest,files,request=spec.document,original_sha256=checksum,original_bytes=size)
            except ValueError:raise MediaError('invalid_document_preview') from None
            for page in manifest['pages']:
                entry=next(f for f in files if f['path']==page['path'])
                with (sealed/str(entry['index'])).open('rb') as stream:header=stream.read(24)
                _require(len(header)==24 and header[:8]==b'\x89PNG\r\n\x1a\n' and header[12:16]==b'IHDR'
                    and struct.unpack('>II',header[16:24])==(page['width'],page['height']),'invalid_document_page')
        elif spec.derivation:
            _validate_derived_manifest(manifest,files,scope=scope,collection=spec.collection,
                size=size,checksum=checksum,request=spec.derivation)
        else:
            _validate_manifest(manifest, files, scope=scope, collection=spec.collection,
                               size=size, checksum=checksum, sealed=sealed)
        for directory in (sealed, spec.directory):
            dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
        return {'manifest': manifest, 'files': files, 'total_bytes': total,
                'scope': asdict(scope), 'generation': spec.generation, 'profile': documents.PROFILE if spec.document else DERIVE_PROFILE if spec.derivation else PROFILE, 'image_digest': spec.image,
                'purpose': documents.PURPOSE if spec.document else spec.derivation.purpose if spec.derivation else PURPOSE, 'schema_version': SCHEMA_VERSION}
    finally:
        os.close(source_fd)


async def _drain(task):
    """Finish an owned bounded operation even after repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


class MediaBroker:
    def __init__(self, root, backend, *, app_uid, app_gid, clock=time.time, worker_uid=WORKER_UID, document_previews=False):
        self.root, self.backend = Path(root), backend
        self.document_previews=document_previews is True
        self.app_uid, self.app_gid, self.clock, self.worker_uid = app_uid, app_gid, clock, worker_uid
        _directory(self.root)
        self.db = sqlite3.connect(self.root / 'media.sqlite3', isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, scope TEXT NOT NULL,
            generation INTEGER NOT NULL, token TEXT NOT NULL, collection TEXT NOT NULL,
            checksum TEXT NOT NULL, size INTEGER NOT NULL, expires REAL NOT NULL, started REAL NOT NULL,
            status TEXT NOT NULL, confirmed INTEGER NOT NULL, error TEXT, result TEXT)''')
        self.db.execute('CREATE TABLE IF NOT EXISTS job_derivations (key TEXT PRIMARY KEY, generation INTEGER NOT NULL, body TEXT NOT NULL)')
        self.lock, self.tasks = asyncio.Lock(), {}

    def _row(self, key):
        return self.db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()

    def _derivation_body(self,row):
        item=self.db.execute('SELECT body FROM job_derivations WHERE key=? AND generation=?',(row['key'],row['generation'])).fetchone()
        return item['body'] if item else '{}'

    def _spec(self, row):
        scope = MediaScope(**json.loads(row['scope']))
        derivation=self._derivation_body(row)
        body=json.loads(derivation)
        document=documents.DocumentRequest.parse(body) if body.get('purpose')==documents.PURPOSE else None
        return MediaSpec(row['key'], row['generation'], scope.asset_id, row['collection'],
            self.root / f"{row['key']}-{row['generation']}", self.backend.volume_path(row['key'], row['generation']),
            DerivationRequest.parse(body) if derivation!='{}' and not document else None,document)

    def _valid(self, scope, lease):
        row = self._row(scope.key)
        _require(row is not None and lease.scope_key == scope.key and row['generation'] == lease.generation
                 and secrets.compare_digest(row['token'], lease.token), 'stale_media_lease')
        return row

    def _update(self, key, status, confirmed, error=None, result=None):
        self.db.execute('UPDATE jobs SET status=?,confirmed=?,error=?,result=? WHERE key=?',
            (status, int(confirmed), error, _json(result) if result else None, key))

    async def submit(self, scope, *, generation, collection, original_sha256, original_bytes, expires_at, original_fd):
        return await self._submit(scope,generation=generation,collection=collection,original_sha256=original_sha256,
            original_bytes=original_bytes,expires_at=expires_at,original_fd=original_fd)

    async def derive(self, scope, *, generation, collection, original_sha256, original_bytes, expires_at, original_fd, derivation):
        request=DerivationRequest.parse(derivation)
        _require(_identifier(request.clip_id,re.escape(scope.asset_id)+r'-clip-[0-9]{6}'),'derivation_clip_scope')
        return await self._submit(scope,generation=generation,collection=collection,original_sha256=original_sha256,
            original_bytes=original_bytes,expires_at=expires_at,original_fd=original_fd,derivation=request)

    async def preview(self,scope,*,document,**values):
        _require(self.document_previews,'document_preview_unavailable')
        try:request=documents.DocumentRequest.parse(document)
        except (TypeError,ValueError):raise MediaError('invalid_document_request') from None
        _require(type(values.get('original_bytes')) is int and 0<values['original_bytes']<=documents.MAX_ORIGINAL,'document_size_limit')
        return await self._submit(scope,derivation=request,**values)

    async def _submit(self, scope, *, generation, collection, original_sha256, original_bytes, expires_at, original_fd, derivation=None):
        _require(type(generation) is int and 0 < generation < 2**63
            and _identifier(collection, r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}')
            and _identifier(original_sha256, r'[a-f0-9]{64}')
            and type(original_bytes) is int and 0 < original_bytes <= INPUT_BYTES, 'invalid_submission')
        self._expiry(expires_at)
        async with self.lock:
            old = self._row(scope.key)
            if old:
                _require(generation >= old['generation'], 'stale_media_lease')
                if generation == old['generation']:
                    _require((old['collection'], old['checksum'], old['size'], self._derivation_body(old)) ==
                             (collection, original_sha256, original_bytes, _json(asdict(derivation)) if derivation else '{}'), 'changed_submission')
                    return MediaLease(scope.key, generation, old['token'])
                _require(old['confirmed'] and old['status'] in TERMINAL, 'prior_worker_unconfirmed')
                _require(old['status'] == 'released', 'release_previous_output')
            count = self.db.execute("SELECT count(*) FROM jobs WHERE status!='released'").fetchone()[0]
            _require(count < MAX_RETAINED, 'media_retention_limit')
            active = self.db.execute("SELECT count(*) FROM jobs WHERE confirmed=0").fetchone()[0]
            _require(active < MAX_ACTIVE, 'media_capacity')
            token = secrets.token_hex(24)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)',
                    (scope.key, _json(asdict(scope)), generation, token, collection, original_sha256,
                     original_bytes, expires_at, self.clock(), 'starting'))
                self.db.execute('INSERT OR REPLACE INTO job_derivations VALUES (?,?,?)',
                    (scope.key,generation,_json(asdict(derivation)) if derivation else '{}'))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK');raise
            row = self._row(scope.key); spec = self._spec(row)
            _directory(spec.directory)
            # Shield the bounded copy so cancellation cannot race with removal/reuse.
            copying = asyncio.create_task(asyncio.to_thread(_copy_original, original_fd, spec.original,
                uid=self.app_uid, size=original_bytes, checksum=original_sha256))
            try:
                await asyncio.shield(copying)
                self._expiry(expires_at)
            except BaseException:
                try: await _drain(copying)
                except BaseException: pass
                self._update(scope.key, 'failed', True, 'original_admission_failed')
                spec.original.unlink(missing_ok=True)
                raise
            self.tasks[scope.key] = asyncio.create_task(self._run(scope, row, spec))
            return MediaLease(scope.key, generation, token)

    def _expiry(self, expires):
        _require(_number(expires) and self.clock() < expires <= self.clock() + 90, 'invalid_media_expiry')

    async def heartbeat(self, scope, lease, *, expires_at):
        self._expiry(expires_at)
        async with self.lock:
            row = self._valid(scope, lease)
            _require(row['status'] in {'starting', 'running'} and self.clock() < row['expires']
                     and self.clock() < row['started'] + WALL_SECONDS, 'media_lease_expired')
            self.db.execute('UPDATE jobs SET expires=? WHERE key=?', (expires_at, scope.key))
        return await self.status(scope, lease)

    async def _run(self, scope, row, spec):
        status, error, result, confirmed = 'failed', 'media_worker_failed', None, False
        waiting = None
        current = asyncio.current_task()
        async def enforce_expiry():
            while True:
                await asyncio.sleep(.2)
                if self.clock() >= self._row(scope.key)['expires']:
                    current.cancel()
                    return
        watchdog = asyncio.create_task(enforce_expiry())
        try:
            async with asyncio.timeout(max(.001, row['started'] + spec.wall_seconds - self.clock())):
                await self.backend.mount(spec)
                await self.backend.start(spec)
                self._update(scope.key, 'running', False)
                waiting = asyncio.create_task(self.backend.wait(spec))
                while not waiting.done():
                    _require(self.clock() < self._row(scope.key)['expires'], 'media_lease_expired')
                    await asyncio.wait({waiting}, timeout=.2)
                _require(await waiting == 0, 'media_worker_failed')
                await self.backend.stop(spec)
                confirmed = True
                _require(self.clock() < self._row(scope.key)['expires'], 'media_lease_expired')
                copying = asyncio.create_task(asyncio.to_thread(_snapshot, spec, scope=scope,
                    size=row['size'], checksum=row['checksum'], worker_uid=self.worker_uid))
                try:
                    result = await asyncio.shield(copying)
                except BaseException:
                    try: await _drain(copying)
                    except BaseException: pass
                    raise
                _require(self.clock() < self._row(scope.key)['expires'], 'media_lease_expired')
                status, error = 'succeeded', None
        except asyncio.CancelledError:
            if self.clock() >= self._row(scope.key)['expires']:
                status, error = 'failed', 'media_lease_expired'
            else:
                status, error = 'stopped', 'stop_requested'
        except TimeoutError:
            error = 'media_wall_timeout'
        except Exception as failure:
            # Stable protocol errors describe the failed boundary; never return
            # decoder stderr, arbitrary exception text or a private host path.
            error = str(failure) if isinstance(failure, MediaError) else 'media_preparation_failed'
        finally:
            watchdog.cancel(); await asyncio.gather(watchdog, return_exceptions=True)
            if waiting and not waiting.done():
                waiting.cancel(); await asyncio.gather(waiting, return_exceptions=True)
            async def cleanup():
                await self.backend.stop(spec)
                await self.backend.unmount(spec)
                spec.original.unlink(missing_ok=True)
            try:
                await _drain(asyncio.create_task(cleanup()))
                confirmed = True
            except (OSError, RuntimeError):
                confirmed, status, error, result = False, 'uncertain', 'media_cleanup_unconfirmed', None
            if status != 'succeeded':
                result = None
                shutil.rmtree(spec.directory / 'sealed', ignore_errors=True)
            self._update(scope.key, status, confirmed, error, result)

    async def status(self, scope, lease):
        async with self.lock:
            row = self._valid(scope, lease)
            return {'status': row['status'], 'cleanup_confirmed': bool(row['confirmed']),
                    'generation': row['generation'], 'error': row['error'],
                    'result': json.loads(row['result']) if row['result'] else None}

    async def stop(self, scope, lease):
        async with self.lock:
            row = self._valid(scope, lease)
            task = self.tasks.get(scope.key)
            if task and not task.done():
                task.cancel()
            elif not row['confirmed']:
                await self._reconcile(row)
        if task:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        async with self.lock:
            row = self._valid(scope, lease)
            if not row['confirmed']:
                await self._reconcile(row)
        value = await self.status(scope, lease)
        _require(value['cleanup_confirmed'], 'media_cleanup_unconfirmed')
        return value

    async def _reconcile(self, row):
        spec = self._spec(row)
        try:
            await self.backend.stop(spec)
            await self.backend.unmount(spec)
            spec.original.unlink(missing_ok=True)
            shutil.rmtree(spec.directory / 'sealed', ignore_errors=True)
            self._update(row['key'], 'stopped', True, 'broker_restarted')
        except (OSError, RuntimeError):
            self._update(row['key'], 'uncertain', False, 'media_cleanup_unconfirmed')

    async def recover_lease(self, scope, generation):
        _require(type(generation) is int and 0 < generation < 2**63, 'invalid_lease')
        async with self.lock:
            row = self._row(scope.key)
            if row is None: return None
            _require(row['generation'] == generation, 'stale_media_lease')
            return MediaLease(scope.key, generation, row['token'])

    async def recover(self):
        async with self.lock:
            for row in self.db.execute('SELECT * FROM jobs WHERE confirmed=0').fetchall():
                await self._reconcile(row)

    async def output_file(self, scope, lease, index):
        async with self.lock:
            row = self._valid(scope, lease)
            _require(row['status'] == 'succeeded' and row['confirmed'] and type(index) is int, 'output_unavailable')
            files = json.loads(row['result'])['files']
            _require(0 <= index < len(files), 'invalid_output_index')
            fd = os.open(self._spec(row).directory / 'sealed' / str(index), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                    and info.st_size == files[index]['size_bytes'], 'output_integrity_failed')
                return os.fdopen(fd, 'rb'), files[index]
            except BaseException:
                os.close(fd); raise

    async def release(self, scope, lease):
        async with self.lock:
            row = self._valid(scope, lease)
            _require(row['confirmed'] and row['status'] in TERMINAL, 'worker_stop_required')
            shutil.rmtree(self._spec(row).directory, ignore_errors=True)
            self._update(scope.key, 'released', True)
            self.tasks.pop(scope.key, None)
        return await self.status(scope, lease)

    async def close(self):
        for key, task in list(self.tasks.items()):
            if not task.done(): task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.recover()
        self.db.close()


def _send_packet(sock, payload):
    _require(0 < len(payload) <= PACKET_BYTES, 'invalid_packet')
    _require(sock.send(payload) == len(payload), 'short_packet')


def _receive_packet(sock):
    data, _, flags, _ = sock.recvmsg(PACKET_BYTES)
    _require(data and not flags & socket.MSG_TRUNC, 'truncated_packet')
    return data


def _send_json(sock, value):
    body = _json(value).encode()
    _require(len(body) <= 2 * 1024**2, 'response_limit')
    _send_packet(sock, b'J' + str(len(body)).encode())
    for offset in range(0, len(body), PACKET_BYTES - 1):
        _send_packet(sock, b'D' + body[offset:offset + PACKET_BYTES - 1])


def _receive_json(sock):
    header = _receive_packet(sock)
    _require(header[:1] == b'J' and header[1:].isdigit(), 'invalid_response')
    size = int(header[1:]); _require(0 < size <= 2 * 1024**2, 'response_limit')
    body = bytearray()
    while len(body) < size:
        data = _receive_packet(sock)
        _require(data[:1] == b'D' and len(body) + len(data) - 1 <= size, 'invalid_response')
        body.extend(data[1:])
    value = json.loads(body)
    _require(isinstance(value, dict), 'invalid_response')
    if 'broker_error' in value:
        raise MediaError(value['broker_error'])
    return value


def _receive_request(sock, app_uid):
    credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _require(struct.unpack('3i', credentials)[1] == app_uid, 'media_access_denied')
    fds = []
    try:
        data, ancillary, flags, _ = sock.recvmsg(CONTROL_BYTES, socket.CMSG_SPACE(4 * 4), socket.MSG_CMSG_CLOEXEC)
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received = array.array('i'); received.frombytes(payload[:len(payload) // 4 * 4]); fds.extend(received)
        _require(data and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC), 'invalid_request')
        value = json.loads(data)
        _require(isinstance(value, dict), 'invalid_request')
        _require(len(fds) == (1 if value.get('operation') in {'submit','derive','preview'} else 0), 'invalid_original_descriptor')
        return value, fds
    except BaseException:
        for fd in fds: os.close(fd)
        raise


class MediaServer:
    def __init__(self, broker, path=SOCKET):
        self.broker, self.path = broker, Path(path)
        self.socket, self.accepting, self.lock_fd = None, None, None
        self.connections = set()

    async def start(self):
        self.lock_fd = os.open(self.broker.root / 'broker.lock', os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd); self.lock_fd = None
            raise MediaError('broker_already_running') from None
        try:
            await self.broker.recover()
            self.path.parent.mkdir(mode=0o711, parents=True, exist_ok=True)
            parent = self.path.parent.lstat()
            _require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid()
                     and not parent.st_mode & 0o022, 'invalid_socket_directory')
            self.path.unlink(missing_ok=True)
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.socket.setblocking(False); self.socket.bind(str(self.path)); self.socket.listen(12)
            os.chown(self.path, os.geteuid(), self.broker.app_gid); self.path.chmod(0o660)
            self.accepting = asyncio.create_task(self._accept())
        except BaseException:
            await self.close(); raise

    async def _accept(self):
        loop = asyncio.get_running_loop()
        while True:
            sock, _ = await loop.sock_accept(self.socket)
            if len(self.connections) >= 12:
                sock.close(); continue
            sock.setblocking(True); sock.settimeout(10)
            task = asyncio.create_task(self._connection(sock))
            self.connections.add(task); task.add_done_callback(self.connections.discard)

    async def _connection(self, sock):
        fds = []
        try:
            request, fds = await asyncio.to_thread(_receive_request, sock, self.broker.app_uid)
            _require(request.pop('schema_version', None) == SCHEMA_VERSION and
                     request.pop('purpose', None) == PURPOSE, 'unsupported_media_profile')
            scope = MediaScope(**request['scope'])
            operation = request.get('operation')
            if operation in {'submit','derive','preview'}:
                allowed = {'operation', 'scope', 'generation', 'collection', 'original_sha256', 'original_bytes', 'expires_at'}
                if operation=='derive':allowed.add('derivation')
                if operation=='preview':allowed.add('document')
                _require(set(request) == allowed, 'invalid_request')
                admission=self.broker.preview if operation=='preview' else self.broker.derive if operation=='derive' else self.broker.submit
                value = asdict(await admission(scope, original_fd=fds[0],
                    **{key: request[key] for key in allowed - {'operation', 'scope'}}))
            elif operation == 'recover':
                _require(set(request) == {'operation', 'scope', 'generation'}, 'invalid_request')
                recovered = await self.broker.recover_lease(scope, request['generation'])
                value = {'lease': asdict(recovered) if recovered else None}
            else:
                lease = MediaLease(**request['lease'])
                allowed = {'operation', 'scope', 'lease'}
                if operation == 'heartbeat': allowed.add('expires_at')
                if operation == 'read_output': allowed.add('index')
                _require(set(request) == allowed, 'invalid_request')
                if operation == 'heartbeat':
                    value = await self.broker.heartbeat(scope, lease, expires_at=request['expires_at'])
                elif operation == 'status':
                    value = await self.broker.status(scope, lease)
                elif operation == 'stop':
                    value = await self.broker.stop(scope, lease)
                elif operation == 'release':
                    value = await self.broker.release(scope, lease)
                elif operation == 'read_output':
                    stream, metadata = await self.broker.output_file(scope, lease, request['index'])
                    try:
                        await asyncio.to_thread(_send_json, sock, metadata)
                        while data := await asyncio.to_thread(stream.read, PACKET_BYTES - 1):
                            await asyncio.to_thread(_send_packet, sock, b'D' + data)
                        await asyncio.to_thread(_send_packet, sock, b'E')
                    finally:
                        stream.close()
                    return
                else:
                    raise MediaError('unknown_operation')
            await asyncio.to_thread(_send_json, sock, value)
        except (MediaError, OSError, ValueError, KeyError, TypeError, RecursionError) as error:
            try:
                await asyncio.to_thread(_send_json, sock, {'broker_error': str(error) if isinstance(error, MediaError) else 'invalid_request'})
            except (MediaError, OSError):
                pass
        finally:
            for fd in fds: os.close(fd)
            sock.close()

    async def close(self):
        if self.lock_fd is None: return
        if self.accepting:
            self.accepting.cancel(); await asyncio.gather(self.accepting, return_exceptions=True)
        if self.socket: self.socket.close()
        # Every socket has a10s timeout; let descriptor copies finish before shutdown.
        await asyncio.gather(*list(self.connections), return_exceptions=True)
        await self.broker.close()
        self.path.unlink(missing_ok=True)
        os.close(self.lock_fd); self.lock_fd = None


class MediaClient:
    def __init__(self, socket_path=SOCKET, *, server_uid=0):
        self.path, self.server_uid = str(socket_path), server_uid

    def _connect(self, operation, scope, lease=None, *, original_fd=None, **values):
        request = {'schema_version': SCHEMA_VERSION, 'purpose': PURPOSE, 'operation': operation, 'scope': asdict(scope), **values}
        if lease is not None: request['lease'] = asdict(lease)
        data = _json(request).encode(); _require(len(data) <= CONTROL_BYTES, 'request_limit')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET); sock.settimeout(45)
        try:
            sock.connect(self.path)
            _require(struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                     == self.server_uid, 'media_server_identity')
            ancillary = [] if original_fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [original_fd]))]
            _require(sock.sendmsg([data], ancillary) == len(data), 'short_request')
            return sock
        except BaseException:
            sock.close(); raise

    def _exchange(self, operation, scope, lease=None, **values):
        with self._connect(operation, scope, lease, **values) as sock:
            return _receive_json(sock)

    async def _request(self, operation, scope, lease=None, **values):
        return await asyncio.to_thread(self._exchange, operation, scope, lease, **values)

    async def submit(self, scope, **values):
        return await self._admit('submit',scope,**values)

    async def derive(self, scope, **values):
        values['derivation']=asdict(DerivationRequest.parse(values.get('derivation')))
        return await self._admit('derive',scope,**values)

    async def preview(self,scope,**values):
        values['document']=asdict(documents.DocumentRequest.parse(values.get('document')))
        return await self._admit('preview',scope,**values)

    async def _admit(self, operation, scope, **values):
        # Duplicate the FD before leaving the caller's context; a cancelled
        # thread still owns a valid descriptor until its bounded exchange ends.
        copied_fd = os.dup(values.pop('original_fd'))
        def exchange():
            try: return self._exchange(operation, scope, original_fd=copied_fd, **values)
            finally: os.close(copied_fd)
        task = asyncio.create_task(asyncio.to_thread(exchange))
        try:
            return MediaLease(**await asyncio.shield(task))
        except BaseException:
            try: await _drain(task)
            except BaseException: pass
            raise

    async def recover(self, scope, generation):
        value = await self._request('recover', scope, generation=generation)
        return MediaLease(**value['lease']) if value.get('lease') else None

    async def status(self, scope, lease):
        return await self._request('status', scope, lease)

    async def heartbeat(self, scope, lease, *, expires_at):
        return await self._request('heartbeat', scope, lease, expires_at=expires_at)

    async def stop(self, scope, lease):
        value = await self._request('stop', scope, lease)
        _require(value.get('cleanup_confirmed') is True, 'media_cleanup_unconfirmed')
        return value

    async def release(self, scope, lease):
        return await self._request('release', scope, lease)

    async def read_output(self, scope, lease, index):
        connecting = asyncio.create_task(asyncio.to_thread(self._connect, 'read_output', scope, lease, index=index))
        try:
            sock = await asyncio.shield(connecting)
        except BaseException:
            result = await asyncio.gather(connecting, return_exceptions=True)
            if isinstance(result[0], socket.socket): result[0].close()
            raise
        try:
            metadata = await asyncio.to_thread(_receive_json, sock)
            _require(metadata.get('index') == index and type(metadata.get('size_bytes')) is int
                     and (0 <= metadata['size_bytes'] <= documents.MAX_TEXT if metadata.get('path')=='text.txt'
                          else 0 < metadata['size_bytes'] <= (64 * 1024**2 if metadata.get('path')=='clip.mp4' else 32 * 1024**2))
                     and _identifier(metadata.get('sha256'), r'[a-f0-9]{64}'), 'invalid_output_metadata')
            size, digest = 0, hashlib.sha256()
            while True:
                packet = await asyncio.to_thread(_receive_packet, sock)
                if packet == b'E': break
                _require(packet[:1] == b'D', 'invalid_output_frame')
                data = packet[1:]; size += len(data)
                _require(size <= metadata['size_bytes'], 'output_size_limit')
                digest.update(data); yield data
            _require(size == metadata['size_bytes'] and digest.hexdigest() == metadata['sha256'], 'output_integrity_failed')
        finally:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()


async def _main(args):
    _require(os.geteuid() == 0, 'root_broker_required')
    os.umask(0o077)
    broker = MediaBroker(ROOT, MediaDocker(ROOT), app_uid=args.app_uid, app_gid=args.app_gid,
                         document_previews=getattr(args, "document_previews", False))
    server = MediaServer(broker)
    await server.start()
    ending = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, ending.set)
    try:
        await ending.wait()
    finally:
        await server.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-uid', required=True, type=int)
    parser.add_argument('--app-gid', required=True, type=int)
    arguments = parser.parse_args()
    try:
        asyncio.run(_main(arguments))
    except KeyboardInterrupt:
        pass
