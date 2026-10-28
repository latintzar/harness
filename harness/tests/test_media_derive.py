"""Development-host decodes are limited to these generated trusted fixtures."""
from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import media_derive as derivation
from media_derive import derive,validate_request,DEFAULT_LIMITS,PreparationError

LIMITS=replace(DEFAULT_LIMITS,subprocess_seconds=45,job_seconds=120)


def ffmpeg(*args):
    subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-v','error','-threads','1',*map(str,args)],check=True,timeout=45)


@pytest.fixture(scope='session')
def media(tmp_path_factory):
    root=tmp_path_factory.mktemp('trusted-derive-fixtures')
    normal=root/'normal.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=320x192:rate=12','-t','10','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',normal)
    vfr=root/'vfr.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=320x192:rate=30','-t','4','-vf',"select='not(mod(n,3))+not(mod(n,5))'",'-fps_mode','vfr','-c:v','libx264','-threads','1','-output_ts_offset','7',vfr)
    rotated=root/'rotated.mp4';payload=bytearray(normal.read_bytes());at=payload.index(b'tkhd')
    payload[at+44:at+80]=struct.pack('>9i',0,65536,0,-65536,0,0,0,0,1073741824);rotated.write_bytes(payload)
    audio=root/'audio.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=320x192:rate=12','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
        '-t','4','-c:v','libx264','-threads','1','-c:a','aac','-output_ts_offset','7',audio)
    delayed=root/'delayed.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=320x192:rate=12','-itsoffset','1','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
        '-t','4','-c:v','libx264','-threads','1','-c:a','aac','-output_ts_offset','7',delayed)
    large=root/'4k.mp4'
    ffmpeg('-f','lavfi','-i','testsrc2=size=3840x2160:rate=4','-t','1.5','-c:v','libx264','-threads','1','-preset','ultrafast',large)
    return dict(normal=normal,vfr=vfr,rotated=rotated,audio=audio,large=large,delayed=delayed)


def run(path,tmp_path,purpose,start=1.,end=3.,samples=(),limits=LIMITS):
    source=tmp_path/'original';source.write_bytes(path.read_bytes())
    output=tmp_path/'out';output.mkdir()
    result=derive('asset-test','warehouse','clip-test',purpose,start,end,frame_seconds=samples,source=source,output=output,limits=limits)
    assert source.read_bytes()==path.read_bytes()
    assert result['original']['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    assert json.loads((output/'derived/manifest.json').read_text())==result
    assert str(source) not in json.dumps(result)
    for file in result['files']:
        data=(output/'derived'/file['file']).read_bytes()
        assert len(data)==file['size_bytes'] and hashlib.sha256(data).hexdigest()==file['sha256']
    return result,output/'derived'


@pytest.mark.parametrize('purpose',['detail_video_v1','source_cut_v1','inspect_frames_v1'])
def test_each_purpose_reads_original_and_preserves_receipt(media,tmp_path,purpose):
    result,directory=run(media['normal'],tmp_path,purpose,samples=[.2,1.,1.8] if purpose=='inspect_frames_v1' else [])
    assert result['request']['source_elapsed_start_seconds']==1
    assert result['request']['source_elapsed_end_seconds']==3
    assert result['requested_original_pts_start_seconds']==1
    assert result['source']['capture_clock']=='unknown' and result['capture_utc'] is None
    if purpose=='detail_video_v1':
        file=result['files'][0]
        assert (file['width'],file['height'],file['average_frame_rate'],file['frame_count'])==(320,192,'4/1',8)
        assert file['mapping_basis']=='nominal_source_elapsed_4fps_resampling'
    elif purpose=='source_cut_v1':
        file=result['files'][0]
        assert file['frame_count']==24 and file['mapping_basis']=='preserved_source_frame_pts'
        assert file['audio_codecs']==[]
    else:
        assert len(result['files'])==3
        for file in result['files']:
            assert (directory/file['file']).read_bytes().startswith(b'\xff\xd8')
            assert file['source_elapsed_seconds']<=file['requested_source_seconds']
            assert file['requested_source_seconds']-file['source_elapsed_seconds']<1/12+1e-6


@pytest.mark.parametrize('purpose',['detail_video_v1','source_cut_v1','inspect_frames_v1'])
def test_vfr_nonzero_pts_and_actual_frame_mapping(media,tmp_path,purpose):
    result,_=run(media['vfr'],tmp_path,purpose,start=.43,end=2.27,samples=[.05,.5,1.7] if purpose=='inspect_frames_v1' else [])
    assert result['source']['origin_seconds']==pytest.approx(7)
    assert result['source']['variable_frame_intervals']
    assert result['requested_original_pts_start_seconds']==pytest.approx(7.43)
    assert result['requested_original_pts_end_seconds']==pytest.approx(9.27)
    if purpose=='source_cut_v1':
        file=result['files'][0]
        assert file['actual_source_first_frame_seconds']>=.43
        for stamp,frame in zip(file['frame_pts_seconds'],file['source_frames']):
            assert frame['original_pts_seconds']==pytest.approx(7+file['actual_source_first_frame_seconds']+stamp)
    elif purpose=='inspect_frames_v1':
        for frame in result['files']:
            assert frame['original_pts_seconds']==pytest.approx(7+frame['source_elapsed_seconds'])


@pytest.mark.parametrize('purpose',['detail_video_v1','source_cut_v1','inspect_frames_v1'])
def test_rotation_is_physical_and_dimensions_truthful(media,tmp_path,purpose):
    result,_=run(media['rotated'],tmp_path,purpose,samples=[.5] if purpose=='inspect_frames_v1' else [])
    assert abs(result['source']['rotation_degrees'])==90
    assert (result['files'][0]['width'],result['files'][0]['height'])==(192,320)


def test_source_cut_keeps_audio_with_nonzero_source_clock(media,tmp_path):
    result,_=run(media['audio'],tmp_path,'source_cut_v1',start=1,end=3)
    assert result['files'][0]['audio_codecs']==['aac']
    assert abs(result['files'][0]['audio_start_seconds'][0])<.03
    assert result['files'][0]['actual_source_first_frame_seconds']==pytest.approx(1)
    assert result['requested_original_pts_start_seconds']==pytest.approx(8)


@pytest.mark.parametrize('purpose,dimensions',[('detail_video_v1',(1920,1080)),('source_cut_v1',(3840,2160)),('inspect_frames_v1',(1920,1080))])
def test_4k_original_uses_purpose_resolution(media,tmp_path,purpose,dimensions):
    result,_=run(media['large'],tmp_path,purpose,start=0,end=1,samples=[.2] if purpose=='inspect_frames_v1' else [])
    assert (result['files'][0]['width'],result['files'][0]['height'])==dimensions


@pytest.mark.parametrize('changes,code',[
    ({'purpose':'shell'},'unsupported_derivation_purpose'),({'start':-1},'derivation_interval_limit'),
    ({'end':20},'derivation_interval_limit'),({'start':float('nan')},'invalid_derivation_request'),
    ({'start':True},'invalid_derivation_request'),({'frame_seconds':[0]},'invalid_frame_selection'),
    ({'purpose':'inspect_frames_v1','frame_seconds':[0,1,2,3]},'invalid_frame_selection'),
    ({'purpose':'inspect_frames_v1','frame_seconds':[1,0]},'invalid_frame_selection'),
    ({'purpose':'inspect_frames_v1','frame_seconds':[2]},'invalid_frame_selection'),
    ({'asset_id':'../../etc'},'invalid_identity')])
def test_request_validation_before_decode(changes,code):
    args=dict(asset_id='asset-test',collection='warehouse',clip_id='clip-test',purpose='detail_video_v1',start=0,end=2,frame_seconds=[])
    with pytest.raises(PreparationError,match=code):validate_request(**(args|changes))


@pytest.mark.parametrize('changes,code',[
    ({'source_bytes':1},'source_size_limit'),({'dimension':64},'dimension_limit'),
    ({'source_seconds':1},'duration_limit'),({'frames':2},'frame_limit'),
    ({'output_bytes':100},'output_limit')])
def test_limits_remove_partial_derivatives(media,tmp_path,changes,code):
    with pytest.raises(PreparationError,match=code):run(media['normal'],tmp_path,'detail_video_v1',limits=replace(LIMITS,**changes))
    assert not any((tmp_path/'out').iterdir())


def test_interval_outside_original_is_rejected(media,tmp_path):
    with pytest.raises(PreparationError,match='derivation_outside_source'):
        run(media['normal'],tmp_path,'detail_video_v1',start=9,end=11)
    assert not any((tmp_path/'out').iterdir())


def test_cli_has_no_path_or_command_flags():
    import layout
    result=subprocess.run([sys.executable,str(layout.ROOT/'run.py'),'media_derive','--help'],capture_output=True,text=True,check=True)
    assert '--purpose' in result.stdout and '--frame-seconds' in result.stdout
    assert '--source' not in result.stdout and '--command' not in result.stdout and '--output' not in result.stdout


def test_delayed_audio_retains_offset_from_video_clock(media,tmp_path):
    result,_=run(media['delayed'],tmp_path,'source_cut_v1',start=0,end=2)
    file=result['files'][0]
    assert file['audio_codecs']==['aac']
    assert file['audio_start_seconds'][0]==pytest.approx(1,abs=.05)
    assert file['actual_source_end_seconds']==pytest.approx(2)


def test_actual_frame_boundary_is_reported_separately(media,tmp_path):
    result,_=run(media['normal'],tmp_path,'source_cut_v1',start=1.01,end=2.1)
    file=result['files'][0]
    assert result['request']['source_elapsed_start_seconds']==1.01
    assert file['actual_source_first_frame_seconds']==pytest.approx(13/12)
    assert file['actual_source_end_seconds']>2.1
    assert file['requested_interval_is_frame_aligned'] is False
