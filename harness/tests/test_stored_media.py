"""Owned generated media preserve bytes while supporting private seek playback."""
import hashlib
from io import BytesIO
from pathlib import Path
import sys
import wave

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from chat_files import ChatFiles
from stored_media import media_format, media_response
from test_server_access import fixture_app
from test_workflow_routes import login


def wav_bytes():
    stream = BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\x00\x00' * 800)
    return stream.getvalue()


def test_real_audio_preview_preserves_bytes_authorization_and_range(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    raw = wav_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    store = ChatFiles(tmp_path / 'state')
    record = store.import_native(BytesIO(raw), name='sample.wav', title='Sample', size_bytes=len(raw),
        sha256=digest, publication_key=digest, provenance={'kind': 'native_output'},
        owner_principal_id='alice', chat_id='chat-a', dataset='cafe')
    assert record['inline_preview_type'] == record['display_type'] == 'audio'
    assert not record['copyable']
    path = record['inline_preview_url']
    with TestClient(app) as alice, TestClient(app) as bob:
        login(alice, app, 'alice'); login(bob, app, 'bob')
        assert bob.get(path).status_code == 404
        assert bob.get(path, headers={'Range': 'bytes=0-43'}).status_code == 404
        full = alice.get(path)
        assert full.status_code == 200 and full.content == raw
        assert full.headers['content-type'] == 'audio/wav'
        assert full.headers['cache-control'] == 'private, no-store'
        assert full.headers['x-content-type-options'] == 'nosniff'
        assert full.headers['accept-ranges'] == 'bytes'
        assert full.headers['content-security-policy'] == "default-src 'none'; sandbox; frame-ancestors 'self'"
        for header, start, end in [('bytes=0-43', 0, 43), ('bytes=44-', 44, len(raw)-1),
                                   ('bytes=-20', len(raw)-20, len(raw)-1)]:
            response = alice.get(path, headers={'Range': header})
            assert response.status_code == 206 and response.content == raw[start:end+1]
            assert response.headers['content-range'] == f'bytes {start}-{end}/{len(raw)}'
        assert alice.get(record['download_url']).content == raw


@pytest.mark.parametrize('header', ['bytes=9000-', 'bytes=5-1', 'bytes=-0', 'bytes=-',
                                   'bytes=0-2,4-6', 'items=0-3', 'bytes=' + '9'*200 + '-'])
def test_invalid_ranges_return_bounded_failure(header):
    response = media_response(b'0123456789', 'video/mp4', header)
    assert response.status_code == 416 and response.body == b''
    assert response.headers['content-range'] == 'bytes */10'


def test_media_classification_requires_matching_extension_and_signature():
    assert media_format('Sample.WAV', wav_bytes()[:128]) == ('audio/wav', 'audio')
    assert media_format('clip.mp4', b'\x00\x00\x00\x18ftypmp42') == ('video/mp4', 'video')
    assert media_format('clip.webm', b'\x1aE\xdf\xa3test') == ('video/webm', 'video')
    for name, prefix in [('sample.mp3', b'<html>'), ('sample.wav', b'RIFFfakeAVI '),
                         ('sample.txt', wav_bytes()), ('sample.mp4', b'not a video')]:
        assert media_format(name, prefix) is None
    with pytest.raises(ValueError):
        media_response(b'<script>', 'text/html')
