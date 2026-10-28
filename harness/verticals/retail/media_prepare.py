"""Bounded media preparation. Invoke ONLY inside the commissioned media sandbox.

The CLI accepts host-issued identifiers, never an input/output path. Python path
parameters exist for trusted fixture tests and the sandbox launcher only.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time

INPUT = Path('/input/original')
OUTPUT = Path('/output')
RECIPE = 'rocoto-cfr24-h264-segments-v2'
FORMATS = 'mov,matroska,webm,avi,mpegts,flv,ogg'
CODECS = {'h264', 'hevc', 'mpeg4', 'mpeg2video', 'mjpeg', 'vp8', 'vp9', 'av1', 'theora', 'ffv1'}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')


class PreparationError(Exception):
    """Safe, stable error code; decoder stderr is intentionally not exposed."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    source_bytes: int = 2 * 1024**3
    source_seconds: float = 1800
    dimension: int = 4096
    pixels: int = 4096 * 2160
    frames: int = 108_000
    output_bytes: int = 512 * 1024**2
    clip_bytes: int = 8 * 1024**2
    clips: int = 180
    probe_bytes: int = 32 * 1024**2
    stderr_bytes: int = 256 * 1024
    subprocess_seconds: float = 180
    job_seconds: float = 600
    model_side: int = 640
    fps: int = 24
    window_seconds: int = 8

    def validate(self):
        for key, value in asdict(self).items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise PreparationError('invalid_limits')
        if self.window_seconds > 10 or self.fps > 60 or self.model_side > 640 or self.clips > 180:
            raise PreparationError('invalid_limits')


def _tree_bytes(root: Path) -> int:
    size = 0
    for item in root.iterdir():
        info = item.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PreparationError('unexpected_output')
        size += info.st_size
    return size


def _kill_reap(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run(argv, work: Path, limits: Limits, deadline: float, *, stdout_limit=4096):
    """Bound pipes using files, enforce wall/output budgets, kill the whole group."""
    remaining = min(limits.subprocess_seconds, deadline - time.monotonic())
    if remaining <= 0:
        raise PreparationError('timeout')
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            shell=False, start_new_session=True, close_fds=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': '/nonexistent'})
        end = time.monotonic() + remaining
        try:
            while True:
                status = process.poll()
                if os.fstat(stdout.fileno()).st_size > stdout_limit or os.fstat(stderr.fileno()).st_size > limits.stderr_bytes:
                    raise PreparationError('decoder_output_limit')
                if _tree_bytes(work) > limits.output_bytes:
                    raise PreparationError('output_limit')
                if time.monotonic() > end:
                    raise PreparationError('timeout')
                if status is not None:
                    break
                time.sleep(.02)
            stderr.seek(0)
            if status != 0 or stderr.read(1):
                raise PreparationError('decode_failed')
            stdout.seek(0)
            return stdout.read(stdout_limit + 1)
        finally:
            if process.poll() is None:
                _kill_reap(process)
            else:
                process.wait()


def _probe(source, work, limits, deadline, *, frames=False):
    argv = ['/usr/bin/ffprobe', '-v', 'error', '-max_alloc', '67108864',
        '-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMATS,
        '-threads', '1', '-select_streams', 'v:0']
    if frames:
        argv += ['-show_frames', '-show_entries',
            'frame=pts,best_effort_timestamp,pkt_duration,duration,width,height']
    else:
        argv += ['-show_streams', '-show_format']
    argv += ['-of', 'json', str(source)]
    raw = _run(argv, work, limits, deadline, stdout_limit=limits.probe_bytes)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise PreparationError('invalid_probe') from None


def _regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise PreparationError('invalid_source') from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise PreparationError('invalid_source')
    return fd, info


def _hash_fd(fd):
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while data := os.read(fd, 1024 * 1024):
        digest.update(data)
    return digest.hexdigest()


def _hash(path):
    fd, _ = _regular(path)
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _number(value):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass
    raise PreparationError('invalid_timing')


def _dimensions(width, height, limits):
    if not isinstance(width, int) or not isinstance(height, int) or min(width, height) < 2:
        raise PreparationError('invalid_dimensions')
    if max(width, height) > limits.dimension or width * height > limits.pixels:
        raise PreparationError('dimension_limit')


def _source_metadata(probe, frame_probe, limits):
    streams = probe.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'video':
        raise PreparationError('video_required')
    stream = streams[0]
    if stream.get('disposition', {}).get('attached_pic') or stream.get('codec_name') not in CODECS:
        raise PreparationError('unsupported_video')
    width, height = stream.get('width'), stream.get('height')
    _dimensions(width, height, limits)
    try:
        time_base = Fraction(stream['time_base'])
        if time_base <= 0:
            raise ValueError()
    except (KeyError, ValueError, ZeroDivisionError):
        raise PreparationError('invalid_timing') from None
    frames = frame_probe.get('frames', [])
    if not frames:
        raise PreparationError('video_required')
    if len(frames) > limits.frames:
        raise PreparationError('frame_limit')
    timing, kinds = [], set()
    for frame in frames:
        if (frame.get('width'), frame.get('height')) != (width, height):
            raise PreparationError('changing_dimensions')
        kind = 'pts' if 'pts' in frame else 'best_effort_timestamp'
        pts = frame.get(kind)
        if not isinstance(pts, int) or isinstance(pts, bool) or (timing and pts <= timing[-1]['pts']):
            raise PreparationError('invalid_timing')
        kinds.add(kind)
        duration = frame.get('duration', frame.get('pkt_duration'))
        timing.append({'pts': pts, 'duration_ticks': duration if isinstance(duration, int) and duration > 0 else None})
    last_duration = timing[-1]['duration_ticks']
    if last_duration is None:
        declared_end = stream.get('start_pts', timing[0]['pts']) + stream.get('duration_ts', 0)
        last_duration = declared_end - timing[-1]['pts']
        if not isinstance(last_duration, int) or last_duration <= 0:
            raise PreparationError('unknown_frame_duration')
        timing[-1]['duration_ticks'] = last_duration
    duration = (timing[-1]['pts'] + last_duration - timing[0]['pts']) * time_base
    if duration <= 0 or duration > limits.source_seconds:
        raise PreparationError('duration_limit')
    rotation = 0
    for side_data in stream.get('side_data_list', []):
        if 'rotation' in side_data:
            rotation = _number(side_data['rotation'])
    if rotation % 90:
        raise PreparationError('unsupported_rotation')
    sar = stream.get('sample_aspect_ratio', '1:1')
    if sar in ('N/A', '0:1'):
        sar = '1:1'
    try:
        aspect = Fraction(sar.replace(':', '/'))
        if not Fraction(1, 16) <= aspect <= 16:
            raise ValueError()
    except (ValueError, ZeroDivisionError):
        raise PreparationError('invalid_aspect_ratio') from None
    return {'codec': stream['codec_name'], 'coded_width': width, 'coded_height': height,
        'sample_aspect_ratio': sar, 'rotation_degrees': rotation,
        'time_base': str(time_base), 'origin_pts': timing[0]['pts'],
        'origin_seconds': float(timing[0]['pts'] * time_base),
        'duration_seconds': float(duration), 'frame_count': len(timing),
        'timestamp_fields': sorted(kinds),
        'variable_frame_intervals': len({b['pts']-a['pts'] for a, b in zip(timing,timing[1:])}) > 1,
        'capture_utc': None, 'capture_clock': 'unknown'}, timing


def embedding_batches(clips):
    """Match the existing Cosmos admission transport: 160 clips / 64 MiB."""
    batches, batch, size = [], [], 0
    for clip in clips:
        if clip['duration_seconds'] > 10.25 or clip['size_bytes'] > 64 * 1024**2:
            raise PreparationError('embedding_limit')
        if batch and (len(batch) >= 160 or size + clip['size_bytes'] > 64 * 1024**2):
            batches.append(batch)
            batch, size = [], 0
        batch.append(clip['id'])
        size += clip['size_bytes']
    if batch:
        batches.append(batch)
    return batches


def prepare(asset_id: str, collection: str, *, source=INPUT, output=OUTPUT, limits=Limits()):
    """Return manifest after atomic publish to output/prepared. Caller owns tenancy.

    source/output arguments are trusted sandbox paths; never bind them to HTTP or
    model arguments. Original bytes are read-only and kept outside this output.
    """
    limits.validate()
    if not isinstance(asset_id, str) or not IDENTIFIER.fullmatch(asset_id) or not isinstance(collection, str) or not IDENTIFIER.fullmatch(collection):
        raise PreparationError('invalid_identity')
    source, output = Path(source), Path(output)
    if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
        raise PreparationError('output_must_be_empty')
    fd, source_info = _regular(source)
    if not 0 < source_info.st_size <= limits.source_bytes:
        os.close(fd)
        raise PreparationError('source_size_limit')
    work = Path(tempfile.mkdtemp(prefix='.prepare-', dir=output))
    deadline = time.monotonic() + limits.job_seconds
    try:
        head = os.read(fd, 1024).lstrip()
        if head.startswith((b'#EXTM3U', b'ffconcat', b'<?xml', b'<MPD')):
            raise PreparationError('external_reference_format')
        original_sha = _hash_fd(fd)
        probe = _probe(source, work, limits, deadline)
        streams = probe.get('streams', [])
        if not streams:
            raise PreparationError('video_required')
        _dimensions(streams[0].get('width'), streams[0].get('height'), limits)
        declared_duration = streams[0].get('duration', probe.get('format', {}).get('duration'))
        if declared_duration is not None and _number(declared_duration) > limits.source_seconds:
            raise PreparationError('duration_limit')
        frame_probe = _probe(source, work, limits, deadline, frames=True)
        metadata, timing = _source_metadata(probe, frame_probe, limits)
        expected_clips = math.ceil(metadata['duration_seconds'] / limits.window_seconds)
        if expected_clips > limits.clips:
            raise PreparationError('clip_count_limit')
        timing_path = work / 'frame-timing.json'
        timing_path.write_text(json.dumps({'time_base':metadata['time_base'], 'frames':timing},separators=(',',':')))
        # The FPS filter repeats/drops frames on the original elapsed timeline.
        # Autorotation happens before this bounded display-aspect-aware scaling.
        side = limits.model_side
        scale = f"scale=w='max(2,trunc(min({side},{side}*dar)/2)*2)':h='max(2,trunc(min({side},{side}/dar)/2)*2)'"
        filters = f'setpts=PTS-STARTPTS,fps={limits.fps}:round=near,{scale},setsar=1'
        argv = ['/usr/bin/ffmpeg','-hide_banner','-nostdin','-v','error','-xerror','-max_alloc','67108864',
            '-protocol_whitelist','file,pipe','-format_whitelist',FORMATS,'-threads','1',
            '-i',str(source),'-map','0:v:0','-an','-sn','-dn','-map_metadata','-1','-map_chapters','-1',
            '-filter_threads','1','-vf',filters,'-t',str(metadata['duration_seconds']),
            '-c:v','libx264','-threads','2','-preset','veryfast','-pix_fmt','yuv420p','-crf','23',
            '-maxrate','2M','-bufsize','4M','-bf','0','-g',str(limits.fps*limits.window_seconds),
            '-keyint_min',str(limits.fps*limits.window_seconds),'-sc_threshold','0',
            '-force_key_frames',f'expr:gte(t,n_forced*{limits.window_seconds})',
            '-f','segment','-segment_time',str(limits.window_seconds),'-reset_timestamps','1',
            '-segment_format','mp4',str(work/'clip-%06d.mp4')]
        _run(argv,work,limits,deadline)
        paths = sorted(work.glob('clip-*.mp4'))
        if not paths or len(paths) > expected_clips or len(paths) > limits.clips:
            raise PreparationError('clip_count_mismatch')
        clips = []
        for index, path in enumerate(paths):
            size = path.stat().st_size
            if not 0 < size <= limits.clip_bytes:
                raise PreparationError('clip_size_limit')
            prepared = _probe(path,work,limits,deadline)
            stream = prepared['streams'][0]
            duration = _number(stream.get('duration',prepared['format'].get('duration')))
            if not 0 < duration <= 10.25 or max(stream['width'],stream['height']) > limits.model_side:
                raise PreparationError('prepared_clip_limit')
            start = index * limits.window_seconds
            end = min(metadata['duration_seconds'], start + duration)
            thumb = path.with_suffix('.jpg')
            _run(['/usr/bin/ffmpeg','-hide_banner','-nostdin','-v','error','-xerror',
                '-max_alloc','67108864','-protocol_whitelist','file,pipe','-format_whitelist','mov',
                '-threads','1','-i',str(path),'-an','-sn','-dn','-frames:v','1','-threads','1',
                '-q:v','3','-update','1',str(thumb)],work,limits,deadline)
            clips.append({'id':f'{asset_id}-clip-{index:06d}','asset_id':asset_id,'collection':collection,
                'file':path.name,'sha256':_hash(path),'size_bytes':size,'duration_seconds':duration,
                'width':stream['width'],'height':stream['height'],
                'source_elapsed_start_seconds':start,'source_elapsed_end_seconds':end,
                'original_pts_start_seconds':metadata['origin_seconds']+start,
                'original_pts_end_seconds':metadata['origin_seconds']+end,
                'thumbnail':{'file':thumb.name,'sha256':_hash(thumb),'size_bytes':thumb.stat().st_size}})
        if metadata['duration_seconds'] - clips[-1]['source_elapsed_end_seconds'] > 1/limits.fps + .001:
            raise PreparationError('incomplete_coverage')
        final_info = os.fstat(fd)
        path_info = source.lstat()
        identity_fields = ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if _hash_fd(fd) != original_sha or any(getattr(info,key) != getattr(source_info,key) for info in (final_info,path_info) for key in identity_fields):
            raise PreparationError('source_changed')
        manifest = {'schema_version':1,'recipe':RECIPE,'asset_id':asset_id,'collection':collection,
            'original':{'sha256':original_sha,'size_bytes':source_info.st_size},'source':metadata,
            'frame_timing':{'file':timing_path.name,'sha256':_hash(timing_path)},
            'normalization':{'fps':limits.fps,'window_seconds':limits.window_seconds,
                'mapping':'Prepared elapsed time maps to source elapsed time from the first decoded presentation timestamp. CFR resampling repeats or drops frames; it does not establish new capture timestamps.',
                'rounding_bound_seconds':1/limits.fps,'rotation_applied':True},
            'clips':clips,'embedding_batches':embedding_batches(clips),
            'toolchain':{'ffmpeg':_run(['/usr/bin/ffmpeg','-version'],work,limits,deadline).decode().splitlines()[0],
                         'ffprobe':_run(['/usr/bin/ffprobe','-version'],work,limits,deadline).decode().splitlines()[0]}}
        manifest_path = work/'manifest.json'
        manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
        if _tree_bytes(work) > limits.output_bytes:
            raise PreparationError('output_limit')
        for child in work.iterdir():
            child.chmod(0o600)
            with child.open('rb') as handle:
                os.fsync(handle.fileno())
        directory = os.open(work,os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        work.rename(output/'prepared')
        directory = os.open(output,os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return manifest
    except BaseException:
        if work.exists():
            shutil.rmtree(work)
        raise
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--asset-id',required=True)
    parser.add_argument('--collection',required=True)
    args = parser.parse_args()
    try:
        manifest = prepare(args.asset_id,args.collection)
        print(json.dumps({'asset_id':manifest['asset_id'],'status':'prepared','manifest':'prepared/manifest.json'}))
    except PreparationError as error:
        print(json.dumps({'status':'failed','error':error.code}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
