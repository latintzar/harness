"""Only trusted, generated fixtures are decoded on the development host."""
from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import time

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from media_prepare import Limits, PreparationError, prepare, embedding_batches, _run

TEST_LIMITS = Limits(model_side=192, subprocess_seconds=30, job_seconds=90)


def ffmpeg(*args):
    subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-v','error','-threads','1',*map(str,args)],check=True,timeout=30)


@pytest.fixture(scope='session')
def media(tmp_path_factory):
    root=tmp_path_factory.mktemp('trusted-generated-media')
    normal=root/'normal.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=160x96:rate=12','-t','21.25','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',normal)
    vfr=root/'vfr.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=160x96:rate=30','-t','3.9','-vf',"select='not(mod(n,3))+not(mod(n,5))'",'-fps_mode','vfr','-c:v','libx264','-threads','1','-output_ts_offset','7',vfr)
    rotated=root/'rotated.mp4'
    # Set the generated file's track matrix directly: this fixture is portable
    # across FFmpeg 5 and 6, whose rotation-override CLI switches differ.
    payload=bytearray(normal.read_bytes())
    position=payload.index(b'tkhd')
    assert payload[position+4]==0  # Synthetic MP4 track header version zero.
    matrix=(0,65536,0,-65536,0,0,0,0,1073741824)
    payload[position+44:position+80]=struct.pack('>9i',*matrix)
    rotated.write_bytes(payload)
    audio=root/'audio.m4a'
    ffmpeg('-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','aac',audio)
    parts=[]
    for number,width in enumerate([160,192]):
        part=root/f'part-{number}.ts';ffmpeg('-f','lavfi','-i',f'testsrc2=size={width}x96:rate=12','-t','1','-c:v','libx264','-threads','1','-bf','0','-f','mpegts',part);parts.append(part)
    concat=root/'concat.txt';concat.write_text(''.join(f"file '{part}'\n" for part in parts))
    varying=root/'varying.mkv';ffmpeg('-f','concat','-safe','0','-i',concat,'-c','copy',varying)
    return dict(normal=normal,vfr=vfr,rotated=rotated,audio=audio,varying=varying)


def run(media,tmp_path,**kwargs):
    original=tmp_path/'original';original.write_bytes(media.read_bytes())
    output=tmp_path/'out';output.mkdir()
    return prepare('asset-host-123','cafe',source=original,output=output,limits=kwargs.get('limits',TEST_LIMITS)), original, output


def test_original_preserved_bounded_clips_and_hashes(media,tmp_path):
    result,original,output=run(media['normal'],tmp_path)
    assert original.read_bytes()==media['normal'].read_bytes()
    assert result['original']['sha256']==hashlib.sha256(original.read_bytes()).hexdigest()
    assert len(result['clips'])==3
    assert [clip['source_elapsed_start_seconds'] for clip in result['clips']]==[0,8,16]
    assert result['source']['capture_utc'] is None
    for clip in result['clips']:
        assert 0<clip['duration_seconds']<=8.25
        assert max(clip['width'],clip['height'])<=192
        assert clip['sha256']==hashlib.sha256((output/'prepared'/clip['file']).read_bytes()).hexdigest()
        assert (output/'prepared'/clip['thumbnail']['file']).read_bytes().startswith(b'\xff\xd8')
    assert json.loads((output/'prepared/manifest.json').read_text())==result
    assert all(Path(c['file']).name==c['file'] for c in result['clips'])
    assert str(original) not in json.dumps(result)


def test_vfr_and_nonzero_start_keep_original_pts_and_unknown_utc(media,tmp_path):
    result,_,output=run(media['vfr'],tmp_path)
    assert result['source']['variable_frame_intervals']
    assert result['source']['origin_seconds']==pytest.approx(7)
    assert result['clips'][0]['original_pts_start_seconds']==pytest.approx(7)
    timing=json.loads((output/'prepared/frame-timing.json').read_text())
    assert float(timing['frames'][0]['pts']*Fraction(timing['time_base']))==pytest.approx(7)
    assert result['source']['capture_clock']=='unknown'
    assert 'repeats or drops' in result['normalization']['mapping']
    assert result['source']['duration_seconds']<4.1


def test_rotation_becomes_display_orientation(media,tmp_path):
    result,_,_=run(media['rotated'],tmp_path)
    assert abs(result['source']['rotation_degrees'])==90
    assert result['clips'][0]['height']>result['clips'][0]['width']


def test_variable_dimensions_rejected(media,tmp_path):
    with pytest.raises(PreparationError,match='changing_dimensions'):
        run(media['varying'],tmp_path)
    assert not any((tmp_path/'out').iterdir())


@pytest.mark.parametrize('overrides,code',[
    ({'source_bytes':10},'source_size_limit'),({'dimension':64},'dimension_limit'),
    ({'source_seconds':1},'duration_limit'),({'frames':2},'frame_limit'),
    ({'clip_bytes':32},'clip_size_limit'),({'output_bytes':100},'output_limit'),
    ({'clips':1},'clip_count_limit')])
def test_limits_fail_closed_and_remove_partial_outputs(media,tmp_path,overrides,code):
    with pytest.raises(PreparationError,match=code):
        run(media['normal'],tmp_path,limits=replace(TEST_LIMITS,**overrides))
    assert not any((tmp_path/'out').iterdir())


def test_audio_only_rejected(media,tmp_path):
    with pytest.raises(PreparationError,match='video_required'):
        run(media['audio'],tmp_path)


@pytest.mark.parametrize('payload,code',[(b'#EXTM3U\nhttps://example.invalid/x.ts\n','external_reference_format'),(b'ffconcat version 1.0\nfile /etc/passwd\n','external_reference_format'),(b'invalid video bytes','decode_failed')])
def test_playlist_and_invalid_sources_rejected(tmp_path,payload,code):
    source=tmp_path/'fixture';source.write_bytes(payload)
    with pytest.raises(PreparationError,match=code):run(source,tmp_path)
    assert not any((tmp_path/'out').iterdir())


def test_identity_and_symlink_rejected_before_decode(media,tmp_path):
    out=tmp_path/'out';out.mkdir()
    with pytest.raises(PreparationError,match='invalid_identity'):
        prepare('../file','cafe',source=media['normal'],output=out)
    source=tmp_path/'original';source.symlink_to(media['normal'])
    with pytest.raises(PreparationError,match='invalid_source'):
        prepare('asset-1','cafe',source=source,output=out)
    source.unlink();os.link(media['normal'],source)
    try:
        with pytest.raises(PreparationError,match='invalid_source'):
            prepare('asset-1','cafe',source=source,output=out)
    finally:source.unlink()


def test_timeout_kills_and_reaps_actual_child(tmp_path):
    pidfile=tmp_path/'pid'
    program=f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(20)"
    with pytest.raises(PreparationError,match='timeout'):
        _run([sys.executable,'-c',program],tmp_path,replace(TEST_LIMITS,subprocess_seconds=.15),time.monotonic()+3)
    pid=int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


def test_decoder_output_is_bounded(tmp_path):
    with pytest.raises(PreparationError,match='decoder_output_limit'):
        _run([sys.executable,'-c',"print('x'*100000)"],tmp_path,TEST_LIMITS,time.monotonic()+3,stdout_limit=64)


def test_batches_obey_cosmos_transport_limits():
    clips=[{'id':str(n),'size_bytes':5*1024**2,'duration_seconds':10} for n in range(30)]
    assert list(map(len,embedding_batches(clips)))==[12,12,6]
    tiny=[{'id':str(n),'size_bytes':1,'duration_seconds':1} for n in range(161)]
    assert list(map(len,embedding_batches(tiny)))==[160,1]
    with pytest.raises(PreparationError,match='embedding_limit'):
        embedding_batches([{'id':'bad','size_bytes':1,'duration_seconds':11}])
