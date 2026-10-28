"""Signature classification and range playback for bounded, owned output bytes."""
from pathlib import Path
import re

from starlette.responses import Response

MEDIA_TYPES = {
    'audio': {'audio/wav', 'audio/mpeg', 'audio/ogg', 'audio/flac', 'audio/mp4'},
    'video': {'video/mp4', 'video/webm'},
}


def media_format(name, prefix):
    suffix = Path(name).suffix.lower()
    checks = {
        '.wav': ('audio/wav', 'audio', prefix[:4] == b'RIFF' and prefix[8:12] == b'WAVE'),
        '.mp3': ('audio/mpeg', 'audio', prefix.startswith(b'ID3') or len(prefix) >= 2 and prefix[0] == 255 and prefix[1] & 0xE0 == 0xE0),
        '.ogg': ('audio/ogg', 'audio', prefix.startswith(b'OggS')),
        '.flac': ('audio/flac', 'audio', prefix.startswith(b'fLaC')),
        '.m4a': ('audio/mp4', 'audio', prefix[4:8] == b'ftyp'),
        '.mp4': ('video/mp4', 'video', prefix[4:8] == b'ftyp'),
        '.webm': ('video/webm', 'video', prefix.startswith(b'\x1aE\xdf\xa3')),
    }
    item = checks.get(suffix)
    return item[:2] if item and item[2] else None


def media_response(raw, media_type, range_header=None):
    if media_type not in MEDIA_TYPES['audio'] | MEDIA_TYPES['video']:
        raise ValueError('Choose a supported audio or video file.')
    headers = {'Cache-Control': 'private, no-store', 'Content-Disposition': 'inline',
               'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'bytes',
               'Content-Security-Policy': "default-src 'none'; sandbox; frame-ancestors 'self'"}
    start, end = 0, len(raw) - 1
    if range_header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header) if len(range_header) <= 128 else None
        try:
            if not match or not any(match.groups()):
                raise ValueError()
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                if int(last) <= 0:
                    raise ValueError()
                start = max(0, len(raw) - int(last))
            if not 0 <= start <= end < len(raw):
                raise ValueError()
        except ValueError:
            return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{len(raw)}'})
        headers['Content-Range'] = f'bytes {start}-{end}/{len(raw)}'
    return Response(raw[start:end + 1], status_code=206 if range_header else 200, media_type=media_type, headers=headers)
