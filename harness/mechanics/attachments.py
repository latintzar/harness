"""Validated private image attachments for every workspace."""
import io
from pathlib import Path
import uuid
from PIL import Image, UnidentifiedImageError

class AttachmentStore:
    def __init__(self, state):
        self.state=Path(state)

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

