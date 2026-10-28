"""Fixed on-demand derivation; execute ONLY inside the pinned media sandbox.

Host-issued IDs, source-relative bounds and at most three frame times are the
entire CLI. Source/output paths exist only as trusted fixture/launcher parameters.
The commissioned prepare-v2 worker remains a separate image and entrypoint.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time

from media_prepare import (INPUT, OUTPUT, FORMATS, IDENTIFIER, Limits, PreparationError,
    _regular, _hash_fd, _hash, _probe, _source_metadata, _run, _number, _tree_bytes)

PROFILE = 'source-media-derive-v1'
RECIPE = 'rocoto-source-derive-v1'
PURPOSES = {'detail_video_v1', 'inspect_frames_v1', 'source_cut_v1'}
DETAIL_BYTES = 8 * 1024**2
FRAME_BYTES = 8 * 1024**2
CUT_BYTES = 64 * 1024**2
MANIFEST_BYTES = 512 * 1024
MAX_SECONDS = 8.25
MAX_CUT_FRAMES = 2048
DEFAULT_LIMITS = Limits(source_bytes=256*1024**2)
# Display aspect is preserved; small sources keep their available detail.
DETAIL_SCALE = ("scale=w='max(2,trunc(min(min(iw*sar,1920),min(ih,1080)*dar)/2)*2)':"
                "h='max(2,trunc(min(min(ih,1080),min(iw*sar,1920)/dar)/2)*2)',setsar=1")


def _valid_number(value):
    if type(value) not in (float,int) or not math.isfinite(value):
        raise PreparationError('invalid_derivation_request')
    return float(value)


def validate_request(asset_id, collection, clip_id, purpose, start, end, frame_seconds):
    if any(not isinstance(x,str) or not IDENTIFIER.fullmatch(x) for x in (asset_id,collection,clip_id)):
        raise PreparationError('invalid_identity')
    if not isinstance(purpose,str) or purpose not in PURPOSES:
        raise PreparationError('unsupported_derivation_purpose')
    start,end = _valid_number(start),_valid_number(end)
    if start<0 or not 0<end-start<=MAX_SECONDS:
        raise PreparationError('derivation_interval_limit')
    if not isinstance(frame_seconds,(list,tuple)):
        raise PreparationError('invalid_frame_selection')
    times=[_valid_number(t) for t in frame_seconds]
    if purpose=='inspect_frames_v1':
        if not 1<=len(times)<=3 or any(not 0<=t<end-start for t in times) or times!=sorted(set(times)):
            raise PreparationError('invalid_frame_selection')
    elif times:
        raise PreparationError('invalid_frame_selection')
    return start,end,times


def _command(source):
    return ['/usr/bin/ffmpeg','-hide_banner','-nostdin','-v','error','-xerror','-max_alloc','67108864',
        '-protocol_whitelist','file,pipe','-format_whitelist',FORMATS,'-threads','1','-copyts','-i',str(source)]


def _audio_probe(source,work,limits,deadline):
    raw=_run(['/usr/bin/ffprobe','-v','error','-max_alloc','67108864','-protocol_whitelist','file,pipe',
        '-format_whitelist',FORMATS,'-threads','1','-select_streams','a:0','-show_streams','-of','json',str(source)],
        work,limits,deadline,stdout_limit=256*1024)
    try: return json.loads(raw).get('streams',[])
    except (ValueError,UnicodeError): raise PreparationError('invalid_probe') from None


def _source_frame(index,timing,timebase,origin):
    frame=timing[index]
    return {'source_frame_index':index,'original_pts':frame['pts'],
        'original_pts_seconds':float(frame['pts']*timebase),
        'source_elapsed_seconds':float(frame['pts']*timebase-origin),
        'duration_seconds':float(frame['duration_ticks']*timebase) if frame['duration_ticks'] else None}


def _file(path,maximum):
    size=path.stat().st_size
    if not 0<size<=maximum: raise PreparationError('derived_file_limit')
    return {'file':path.name,'sha256':_hash(path),'size_bytes':size}


def _video_metadata(path,work,limits,deadline):
    probe=_probe(path,work,limits,deadline)
    frames=_probe(path,work,limits,deadline,frames=True).get('frames',[])
    video=probe['streams'][0]
    duration=_number(video.get('duration',probe.get('format',{}).get('duration')))
    if not 0<duration<=MAX_SECONDS or not 1<=len(frames)<=MAX_CUT_FRAMES:
        raise PreparationError('derived_timing_limit')
    timebase=Fraction(video['time_base'])
    local=[]
    for frame in frames:
        pts=frame.get('pts',frame.get('best_effort_timestamp'))
        if type(pts) is not int: raise PreparationError('invalid_derived_timing')
        local.append(float(pts*timebase))
    if local[0]<0 or local[0]>.001 or any(b<=a for a,b in zip(local,local[1:])):
        raise PreparationError('invalid_derived_timing')
    audio=_audio_probe(path,work,limits,deadline)
    return {'mime_type':'video/mp4','video_codec':video.get('codec_name'),'width':video['width'],
        'height':video['height'],'average_frame_rate':video.get('avg_frame_rate'),
        'nominal_frame_rate':video.get('r_frame_rate'),'duration_seconds':duration,
        'frame_count':len(frames),'start_time_seconds':local[0],
        'audio_codecs':[row.get('codec_name') for row in audio],
        'audio_start_seconds':[_number(row.get('start_time',0)) for row in audio],
        'time_base':str(timebase),'frame_pts_seconds':local}


def derive(asset_id,collection,clip_id,purpose,start,end,*,frame_seconds=(),source=INPUT,output=OUTPUT,
           limits=DEFAULT_LIMITS):
    """Publish output/derived atomically; the caller owns scope and job fencing."""
    limits.validate()
    start,end,samples=validate_request(asset_id,collection,clip_id,purpose,start,end,frame_seconds)
    source,output=Path(source),Path(output)
    if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
        raise PreparationError('output_must_be_empty')
    fd,initial=_regular(source)
    if not 0<initial.st_size<=min(limits.source_bytes,256*1024**2):
        os.close(fd);raise PreparationError('source_size_limit')
    work=Path(tempfile.mkdtemp(prefix='.derive-',dir=output))
    deadline=time.monotonic()+limits.job_seconds
    try:
        if os.read(fd,1024).lstrip().startswith((b'#EXTM3U',b'ffconcat',b'<?xml',b'<MPD')):
            raise PreparationError('external_reference_format')
        checksum=_hash_fd(fd)
        probe=_probe(source,work,limits,deadline)
        frame_probe=_probe(source,work,limits,deadline,frames=True)
        metadata,timing=_source_metadata(probe,frame_probe,limits)
        if end>metadata['duration_seconds']+1e-6:
            raise PreparationError('derivation_outside_source')
        timebase=Fraction(metadata['time_base']);origin=timing[0]['pts']*timebase
        elapsed=[float(frame['pts']*timebase-origin) for frame in timing]
        first,last=bisect_left(elapsed,start),bisect_left(elapsed,end)
        if first>=last or last-first>MAX_CUT_FRAMES:
            raise PreparationError('derivation_frame_limit')
        actual_start=elapsed[first]
        selected=[_source_frame(i,timing,timebase,origin) for i in range(first,last)]
        original={'sha256':checksum,'size_bytes':initial.st_size}
        request={'asset_id':asset_id,'collection':collection,'clip_id':clip_id,'purpose':purpose,
            'source_elapsed_start_seconds':start,'source_elapsed_end_seconds':end,
            'frame_seconds':samples,'original_sha256':checksum}
        files=[]
        if purpose=='inspect_frames_v1':
            for number,offset in enumerate(samples):
                target=start+offset
                index=max(0,bisect_right(elapsed,target)-1)
                # A displayed source frame may begin before the requested sample;
                # retain its actual PTS rather than label it with the request time.
                path=work/f'frame-{number:02d}.jpg'
                filters=f'select=eq(n\\,{index}),{DETAIL_SCALE}'
                argv=_command(source)+['-map','0:v:0','-an','-sn','-dn','-map_metadata','-1','-map_chapters','-1',
                    '-filter_threads','1','-vf',filters,'-frames:v','1','-threads','1','-q:v','2','-update','1',str(path)]
                _run(argv,work,replace(limits,output_bytes=min(limits.output_bytes,3*FRAME_BYTES)),deadline)
                info=_probe_image(path,work,limits,deadline)
                files.append({**_file(path,FRAME_BYTES),'mime_type':'image/jpeg',**info,
                    'requested_clip_seconds':offset,'requested_source_seconds':target,
                    **_source_frame(index,timing,timebase,origin)})
        else:
            path=work/'clip.mp4'
            filters=f'trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS'
            detail=purpose=='detail_video_v1'
            filters+=f',fps=4:round=near,{DETAIL_SCALE}' if detail else ',scale=trunc(iw/2)*2:trunc(ih/2)*2'
            argv=_command(source)+['-map','0:v:0','-sn','-dn','-map_metadata','-1','-map_chapters','-1',
                '-filter_threads','1','-vf',filters,'-c:v','libx264','-threads','2','-preset','veryfast',
                '-crf','18','-pix_fmt','yuv420p','-bf','0']
            if detail:
                argv+=['-an','-maxrate','7M','-bufsize','8M','-fps_mode','cfr']
            else:
                audio=_audio_probe(source,work,limits,deadline)
                if audio:
                    absolute_start=float(origin)+actual_start;absolute_end=float(origin)+end
                    argv+=['-map','0:a:0','-af',f'atrim=start={absolute_start:.9f}:end={absolute_end:.9f},asetpts=PTS-{absolute_start:.9f}/TB',
                        '-c:a','aac','-b:a','192k']
                else: argv+=['-an']
                argv+=['-fps_mode','passthrough','-enc_time_base:v',str(timebase),'-video_track_timescale',str(timebase.denominator)]
            argv+=['-movflags','+faststart',str(path)]
            maximum=DETAIL_BYTES if detail else CUT_BYTES
            _run(argv,work,replace(limits,output_bytes=min(limits.output_bytes,maximum)),deadline)
            output_meta=_video_metadata(path,work,limits,deadline)
            if detail:
                if (output_meta['width']>1920 or output_meta['height']>1080 or output_meta['frame_count']>33
                        or Fraction(output_meta['average_frame_rate'])!=4):
                    raise PreparationError('detail_profile_mismatch')
            else:
                if output_meta['frame_count']!=len(selected):
                    raise PreparationError('source_frame_mapping_mismatch')
                for stamp,frame in zip(output_meta['frame_pts_seconds'],selected):
                    if abs(stamp-(frame['source_elapsed_seconds']-actual_start))>max(float(timebase),1e-6):
                        raise PreparationError('source_frame_mapping_mismatch')
            files.append({**_file(path,maximum),**output_meta,
                'actual_source_first_frame_seconds':actual_start,
                'actual_source_end_seconds':actual_start+output_meta['duration_seconds'],
                'requested_interval_is_frame_aligned':abs(actual_start-start)<1e-6 and abs(actual_start+output_meta['duration_seconds']-end)<1e-6,
                'actual_original_first_frame_pts':timing[first]['pts'],
                'source_frames':selected,
                'mapping_basis':'nominal_source_elapsed_4fps_resampling' if detail else 'preserved_source_frame_pts',
                'resampling_rounding_bound_seconds':.25 if detail else 0,
                'rotation_applied':True,'transformation':'H.264 CRF18; '+('1080p bounding box,4fps,silent' if detail else 'source pixel dimensions and frame timing; AAC audio when present')})
        final=os.fstat(fd);current=source.lstat()
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if _hash_fd(fd)!=checksum or any(getattr(info,key)!=getattr(initial,key) for info in (final,current) for key in fields):
            raise PreparationError('source_changed')
        manifest={'schema_version':1,'profile':PROFILE,'recipe':RECIPE,'purpose':purpose,
            'asset_id':asset_id,'collection':collection,'clip_id':clip_id,'original':original,
            'request':request,'request_sha256':hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'source':metadata,'files':files,
            'requested_original_pts_start_seconds':float(origin)+start,'requested_original_pts_end_seconds':float(origin)+end,
            'clock':'source_relative','capture_utc':None}
        encoded=(json.dumps(manifest,allow_nan=False,indent=2)+'\n').encode()
        if len(encoded)>MANIFEST_BYTES: raise PreparationError('manifest_limit')
        (work/'manifest.json').write_bytes(encoded)
        if _tree_bytes(work)>limits.output_bytes:raise PreparationError('output_limit')
        for child in work.iterdir():
            child.chmod(0o600)
            with child.open('rb') as handle:os.fsync(handle.fileno())
        directory=os.open(work,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        work.rename(output/'derived')
        directory=os.open(output,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return manifest
    except BaseException:
        if work.exists():shutil.rmtree(work)
        raise
    finally:os.close(fd)


def _probe_image(path,work,limits,deadline):
    raw=_run(['/usr/bin/ffprobe','-v','error','-max_alloc','67108864','-protocol_whitelist','file,pipe',
        '-format_whitelist','image2','-threads','1','-select_streams','v:0','-show_entries','stream=width,height',
        '-of','json',str(path)],work,limits,deadline,stdout_limit=4096)
    try:
        stream=json.loads(raw)['streams'][0];width,height=stream['width'],stream['height']
        if type(width) is not int or type(height) is not int or not 2<=width<=1920 or not 2<=height<=1080:
            raise ValueError()
        return {'width':width,'height':height}
    except (KeyError,IndexError,ValueError,TypeError):raise PreparationError('frame_profile_mismatch') from None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--asset-id',required=True);parser.add_argument('--collection',required=True)
    parser.add_argument('--clip-id',required=True);parser.add_argument('--purpose',required=True,choices=sorted(PURPOSES))
    parser.add_argument('--start',required=True,type=float);parser.add_argument('--end',required=True,type=float)
    parser.add_argument('--frame-seconds',action='append',type=float,default=[])
    args=parser.parse_args()
    try:
        result=derive(args.asset_id,args.collection,args.clip_id,args.purpose,args.start,args.end,frame_seconds=args.frame_seconds)
        print(json.dumps({'status':'derived','asset_id':result['asset_id'],'purpose':result['purpose'],'manifest':'derived/manifest.json'}))
        return 0
    except PreparationError as error:
        print(json.dumps({'status':'failed','error':error.code}));return 1


if __name__=='__main__':raise SystemExit(main())
