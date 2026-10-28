"""Evidence playback must serve only the cited interval, including range requests."""
import json
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).parents[1]))
from server import create_app

class SourceIndex:
    def __init__(self, path):
        self.clips = [dict(id='evidence', video_id='recording',dataset='test', camera='A',start=2.,end=3.,sample_time=2.5,path=str(path),thumbnail_path=str(path))]
    def get_clip(self, key): return self.clips[0] if key == 'evidence' else None
    def get_video(self,key): return self.clips[0] if key == 'recording' else None
    def status(self): return {'ready':True,'datasets':['test']}

def test_clip_is_physically_bounded_and_original_is_private(tmp_path):
    source = tmp_path / 'source.mp4'
    writer = cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'mp4v'),10,(64,48))
    for i in range(50): writer.write(np.full((48,64,3),i*4,np.uint8))
    writer.release()
    with TestClient(create_app(tmp_path/'state',SourceIndex(source))) as client:
        assert client.post('/api/session/demo',headers={'X-Video-Demo':'1'}).status_code==200
        response=client.get('/clips/evidence.mp4')
        assert response.status_code == 200
        clip=tmp_path/'served.mp4'; clip.write_bytes(response.content)
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-of','json',str(clip)]))
        assert abs(float(probe['format']['duration'])-1)<.15
        ranged=client.get('/clips/evidence.mp4',headers={'Range':'bytes=0-63'})
        assert ranged.status_code==206 and len(ranged.content)==64
        assert client.get('/media/recording').status_code==404
        assert client.get('/clips/missing.mp4').status_code==404

def test_attachments_validate_images_and_origin(tmp_path):
    import io
    from PIL import Image
    img=io.BytesIO(); Image.new('RGB',(8,8)).save(img,format='PNG')
    app=create_app(tmp_path,SourceIndex(tmp_path/'source'))
    with TestClient(app) as client:
        assert client.post('/api/session',json={'code':app.state.access.issue('demo')},headers={'X-Video-Demo':'1'}).status_code==200
        assert client.post('/api/attachments',files={'file':('x.png',img.getvalue(),'image/png')}).status_code==403
        assert client.post('/api/attachments',files={'file':('x.png',b'bad','image/png')},headers={'X-Video-Demo':'1'}).status_code==400
        response=client.post('/api/attachments',files={'file':('x.png',img.getvalue(),'image/png')},headers={'X-Video-Demo':'1'})
        assert response.status_code==200
        assert client.get(response.json()['url']).headers['content-type']=='image/png'


def test_download_retains_source_resolution_and_exports_source_timing(tmp_path):
    source = tmp_path / 'source.mp4'
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'mp4v'), 4, (1600,900))
    for _ in range(20):
        writer.write(np.full((900,1600,3),100,np.uint8))
    writer.release()
    with TestClient(create_app(tmp_path/'state',SourceIndex(source))) as client:
        client.post('/api/session/demo',headers={'X-Video-Demo':'1'})
        result = client.get('/downloads/evidence.mp4')
        assert result.status_code == 200
        assert 'attachment' in result.headers['content-disposition']
        path=tmp_path/'download.mp4';path.write_bytes(result.content)
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))
        assert (probe['streams'][0]['width'],probe['streams'][0]['height']) == (1600,900)
        assert abs(float(probe['format']['duration'])-1)<.15
        manifest=client.get('/api/clip/evidence/manifest')
        assert manifest.status_code == 200
        data=manifest.json()
        assert data['source_interval_seconds'] == {'start':2.,'end':3.}
        assert data['asset_id']=='recording' and data['clip_id']=='evidence'
        assert data['source_filename']=='source.mp4'
        assert str(tmp_path) not in manifest.text


def test_playback_preserves_available_source_frame_timestamps(tmp_path):
    """A variable-rate source keeps its real frames rather than CFR duplicates."""
    source = tmp_path/'variable.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=64x48:rate=30:duration=1',
                    '-vf',"select='not(mod(n,3))+eq(n,4)'",'-fps_mode','vfr','-c:v','libx264',
                    '-threads','1',str(source)], check=True)

    def times(path):
        data=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','frame=best_effort_timestamp_time','-of','json',str(path)]))
        return [float(frame['best_effort_timestamp_time']) for frame in data['frames']]

    index=SourceIndex(source)
    index.clips[0].update(start=0.,end=1.)
    with TestClient(create_app(tmp_path/'state',index)) as client:
        client.post('/api/session/demo',headers={'X-Video-Demo':'1'})
        for route in ['/clips/evidence.mp4','/downloads/evidence.mp4']:
            response=client.get(route)
            assert response.status_code==200
            path=tmp_path/(route.split('/')[1]+'.mp4');path.write_bytes(response.content)
            assert np.allclose(times(path),times(source),atol=.001)
