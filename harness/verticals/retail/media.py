"""Private originals produce cached, physically bounded evidence files."""
import asyncio
import hashlib
import io
from pathlib import Path
import uuid

from PIL import Image, UnidentifiedImageError

from imported_media import ImportedMedia, imported_record

class EvidenceMedia:
    def __init__(self, state):
        self.state = Path(state)
        self.locks = {}
        self.imported_media = ImportedMedia(state)

    def _derivatives(self, owner_principal_id):
        if not isinstance(owner_principal_id, str) or not owner_principal_id:
            raise ValueError('derivative_owner_required')
        service = getattr(self, 'derivatives', None)
        if service is None or not service.enabled:
            raise ValueError('source_derivatives_unavailable')
        return service

    async def source_cut(self, record, *, owner_principal_id):
        return await self._derivatives(owner_principal_id).derive(record, 'source_cut_v1',
            owner_principal_id=owner_principal_id)

    async def inspect(self, record, *, owner_principal_id):
        duration = record['end'] - record['start']
        samples = [round(duration * fraction, 6) for fraction in (.1, .5, .9)]
        receipt = await self._derivatives(owner_principal_id).derive(record, 'inspect_frames_v1',
            owner_principal_id=owner_principal_id, frame_seconds=samples)
        frames = []
        for file, metadata in zip(receipt.files, receipt.manifest['files'], strict=True):
            frames.append({'time': round(metadata['source_elapsed_seconds'], 6),
                'frame': metadata['source_frame_index'], 'original_pts_seconds': metadata['original_pts_seconds'],
                'basis': 'source_frame_pts', 'width': metadata['width'], 'height': metadata['height'],
                'image_bytes': await asyncio.to_thread(file.read_bytes, maximum=8*1024**2)})
        return {'clip': record, 'frames': frames, 'verification': 'Awaiting visual inspection'}

    async def clip(self, record, *, source_resolution=False, owner_principal_id=None):
        if imported_record(record):
            if source_resolution:
                receipt = await self.source_cut(record, owner_principal_id=owner_principal_id)
                return receipt.files[0]
            resolved = await asyncio.to_thread(self.imported_media.resolve, record)
            return resolved.prepared
        source = Path(record['path'])
        start, end = float(record['start']), float(record['end'])
        if start < 0 or not 0 < end-start <= 120:
            raise ValueError('Choose a bounded evidence interval.')
        profile = 'source-resolution-timestamps-v2' if source_resolution else 'preview-timestamps-v2'
        fingerprint = f"{source.resolve()}:{source.stat().st_mtime_ns}:{start}:{end}:{profile}"
        key = hashlib.sha256(fingerprint.encode()).hexdigest()
        directory = self.state / 'playback'
        directory.mkdir(mode=0o700, exist_ok=True)
        output = directory / (key + '.mp4')
        async with self.locks.setdefault(key, asyncio.Lock()):
            if output.exists(): return output
            temp = directory / (key + '.tmp.mp4')
            audio = ['-map','0:a?','-c:a','aac','-b:a','192k'] if source_resolution else ['-an']
            scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2" if source_resolution else "scale='min(1280,iw)':-2"
            process = await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y',
                '-ss',str(start),'-i',str(source),'-t',str(end-start),'-map','0:v:0',
                *audio,'-vf',scale,'-c:v','libx264','-preset','veryfast','-crf','18' if source_resolution else '23',
                '-threads','2','-pix_fmt','yuv420p','-fps_mode','passthrough',
                '-movflags','+faststart',str(temp),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, error = await asyncio.wait_for(process.communicate(), timeout=90)
                if process.returncode: raise ValueError('The evidence clip could not be prepared.')
                temp.chmod(0o600)
                temp.replace(output)
            finally:
                if process.returncode is None:
                    process.kill(); await process.wait()
                temp.unlink(missing_ok=True)
            return output

    def save_attachment(self, data, name):
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError('Choose an image up to 10 MB.')
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError('Choose an image up to 20 megapixels.')
                image.load()
                clean = image.convert('RGB')
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('Choose a valid PNG, JPEG, GIF or WebP image.') from exc
        key = uuid.uuid4().hex
        directory = self.state/'attachments'; directory.mkdir(mode=0o700,exist_ok=True)
        path = directory/(key+'.png'); clean.save(path); path.chmod(0o600)
        return {'id':key,'url':f'/attachments/{key}.png','name':Path(name or 'Image').name[:160],'mime':'image/png'}
