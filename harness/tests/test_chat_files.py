"""Generated files are private artifacts; rich text is inert until rendered safely."""
import json
import csv
import hashlib
from io import StringIO, BytesIO
from pathlib import Path
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import chat_files
from chat_files import ChatFiles
from test_server_access import fixture_app
from test_workflow_routes import login, HEADERS


def test_files_survive_restart_and_are_scoped_to_owner_chat_collection(tmp_path):
    store = ChatFiles(tmp_path)
    source = '<script>alert("private")</script>\n# Monthly report'
    file = store.create({'name': 'report.md', 'content': source}, owner_principal_id='alice', chat_id='chat-a', dataset='cafe')
    reopened = ChatFiles(tmp_path)
    assert reopened.content(file['id'], owner_principal_id='alice')['content'] == source
    assert reopened.list(owner_principal_id='bob') == []
    assert reopened.list(owner_principal_id='alice', chat_id='chat-b') == []
    for scope in [{'owner_principal_id': 'bob'}, {'owner_principal_id': 'alice', 'chat_id': 'chat-b'},
                  {'owner_principal_id': 'alice', 'dataset': 'warehouse'}]:
        with pytest.raises(ValueError):
            reopened.content(file['id'], **scope)
    assert 'owner' not in json.dumps(file) and str(tmp_path) not in json.dumps(file)


def test_file_formats_and_transactional_storage_limits(tmp_path, monkeypatch):
    store = ChatFiles(tmp_path)
    scope = {'owner_principal_id': 'alice', 'chat_id': 'chat-a', 'dataset': 'cafe'}
    for name in ['../report.txt', '.hidden.md', 'report.html', 'bad\\name.txt', 'x\n.txt']:
        with pytest.raises(ValueError):
            store.create({'name': name, 'content': 'content'}, **scope)
    with pytest.raises(ValueError, match='valid JSON'):
        store.create({'name': 'invalid.json', 'content': '{"value":NaN}'}, **scope)
    with pytest.raises(ValueError, match='1 MiB'):
        store.create({'name': 'large.txt', 'content': 'x' * (chat_files.MAX_FILE_BYTES + 1)}, **scope)
    record = store.create({'name': 'long.txt', 'content': 'x' * (chat_files.MAX_COPY_BYTES + 1)}, **scope)
    assert record['copyable'] is False
    monkeypatch.setattr(chat_files, 'MAX_COMPANY_FILES', 2)
    def create(_):
        try:
            store.create({'name': 'small.txt', 'content': 'one'}, **scope)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(create, range(4))) == 1


def test_file_display_changes_preserve_source_and_survive_concurrent_open(tmp_path):
    store = ChatFiles(tmp_path)
    scope = {'owner_principal_id': 'alice', 'chat_id': 'chat-a', 'dataset': 'cafe'}
    original = store.create({'name': 'report.csv', 'content': 'item,value\nbox,3'}, **scope)
    before = store.download(original['id'], **scope)[1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        renamed = pool.submit(store.rename, original['id'], 'Delivery report', owner_principal_id='alice')
        opened = pool.submit(store.opened, original['id'], owner_principal_id='alice')
        renamed.result(); opened.result()
    reopened = ChatFiles(tmp_path)
    record, raw = reopened.download(original['id'], **scope)
    assert record['title'] == 'Delivery report' and record['last_opened_at']
    assert raw == before
    assert all(record[key] == original[key] for key in ('name', 'sha256', 'source_sha256', 'created_at'))
    assert reopened.list(owner_principal_id='alice')[0] == record
    assert reopened.content(original['id'], **scope)['content'] == 'item,value\nbox,3'
    with pytest.raises(ValueError): reopened.rename(original['id'], 'Other', owner_principal_id='bob')
    with pytest.raises(ValueError): reopened.opened(original['id'], owner_principal_id='bob')
    for title in ['', '  ', 'a\nb', 'x' * 161, '\ud800']:
        with pytest.raises(ValueError): reopened.rename(original['id'], title, owner_principal_id='alice')


def test_file_display_routes_enforce_owner_observer_csrf_and_exact_body(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    store = ChatFiles(tmp_path / 'state')
    record = store.create({'name': 'report.md', 'content': '# Report'},
        owner_principal_id='alice', chat_id='chat-a', dataset='cafe')
    path = '/api/files/' + record['id']
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as owner:
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(owner, app, 'owner', owner=True)
        assert alice.patch(path, json={'title': 'New'}).status_code == 403
        assert alice.patch(path, json={'title': 'New', 'name': 'changed.txt'}, headers=HEADERS).status_code == 422
        assert alice.post(path + '/opened', json={'owner': 'bob'}, headers=HEADERS).status_code == 422
        assert bob.patch(path, json={'title': 'New'}, headers=HEADERS).status_code == 404
        assert bob.post(path + '/opened', json={}, headers=HEADERS).status_code == 404
        assert alice.patch(path, json={'title': 'Weekly report'}, headers=HEADERS).json()['title'] == 'Weekly report'
        result = alice.post(path + '/opened', json={}, headers=HEADERS)
        assert result.status_code == 200 and result.json()['last_opened_at']
        assert result.json()['name'] == record['name']
        owner.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert owner.get(path).json()['title'] == 'Weekly report'
        assert owner.patch(path, json={'title': 'Other'}, headers=HEADERS).status_code == 403
        assert owner.post(path + '/opened', json={}, headers=HEADERS).status_code == 403


@pytest.mark.parametrize('extension,delimiter', [('csv', ','), ('tsv', '\t')])
def test_spreadsheet_downloads_keep_external_values_inert_and_source_exact(tmp_path, extension, delimiter):
    rows = [['Name', 'Value'], ['Formula', '=1+2'], ['Link', '+HYPERLINK("https://example.com")'],
            ['Minus expression', '-SUM(A1:A2)'], ['At expression', '@SUM(A1:A2)'],
            ['Leading whitespace', ' \t\r\n=1+2'], ['Leading marker', '\ufeff=1+2'],
            ['Negative number', '-3'], ['Decimal', '-0.25'], ['Exponent', '-1.25e+3'],
            ['Positive number', '+3'], ['Plain text', 'ordinary text'],
            ['Multiline', 'First line\nSecond line, quoted "word"']]
    stream = StringIO(newline='')
    csv.writer(stream, delimiter=delimiter).writerows(rows)
    source = stream.getvalue()
    scope = {'owner_principal_id': 'alice', 'chat_id': 'chat-a', 'dataset': 'cafe'}
    store = ChatFiles(tmp_path)
    record = store.create({'name': 'report.' + extension, 'content': source}, **scope)
    metadata, binary = store.download(record['id'], **scope)
    rendered = list(csv.reader(StringIO(binary.decode(), newline=''), delimiter=delimiter))
    assert rendered[:1] == rows[:1]
    assert [row[1] for row in rendered[1:7]] == ["'" + row[1] for row in rows[1:7]]
    assert rendered[7:] == rows[7:]
    assert store.content(record['id'], **scope)['content'] == source
    assert metadata == record
    assert record['size_bytes'] == len(binary)
    assert record['sha256'] == hashlib.sha256(binary).hexdigest()
    assert record['source_size_bytes'] == len(source.encode())
    assert record['source_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert ChatFiles(tmp_path).download(record['id'], **scope) == (metadata, binary)


def test_existing_spreadsheet_downloads_are_migrated_with_exact_source(tmp_path):
    source = b'Name,Value\r\nExternal data,=1+2\r\n'
    identity = 'file-' + 'a' * 32
    metadata = {'id': identity, 'kind': 'document', 'chat_id': 'chat-a', 'dataset': 'cafe',
                'name': 'old.csv', 'media_type': 'text/csv', 'size_bytes': len(source),
                'sha256': hashlib.sha256(source).hexdigest(), 'created_at': '2026-10-04T00:00:00+00:00'}
    with sqlite3.connect(tmp_path / 'chat-files.sqlite') as db:
        db.execute('CREATE TABLE files (id TEXT PRIMARY KEY, owner TEXT, chat_id TEXT, dataset TEXT, metadata TEXT, content BLOB)')
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?)', (identity, 'alice', 'chat-a', 'cafe', json.dumps(metadata), source))
    store = ChatFiles(tmp_path)
    record, binary = store.download(identity, owner_principal_id='alice')
    assert list(csv.reader(StringIO(binary.decode())))[1] == ['External data', "'=1+2"]
    assert store.content(identity, owner_principal_id='alice')['content'] == source.decode()
    assert record['size_bytes'] == len(binary) and record['sha256'] == hashlib.sha256(binary).hexdigest()
    assert record['source_sha256'] == hashlib.sha256(source).hexdigest()
    with pytest.raises(ValueError):
        store.download(identity, owner_principal_id='bob')


def test_spreadsheet_downloads_preserve_cell_boundaries_and_disable_separator_directives(tmp_path):
    store = ChatFiles(tmp_path)
    scope = {'owner_principal_id': 'alice', 'chat_id': 'chat-a', 'dataset': 'cafe'}
    source = 'sep=;\nexternal;=1+2\n'
    file = store.create({'name': 'report.csv', 'content': source}, **scope)
    _, binary = store.download(file['id'], **scope)
    assert binary == b'"\'sep=;"\r\n"external;=1+2"\r\n'
    assert store.content(file['id'], **scope)['content'] == source


def test_spreadsheet_validation_bounds_leave_catalogue_unchanged(tmp_path, monkeypatch):
    store = ChatFiles(tmp_path)
    scope = {'owner_principal_id': 'alice', 'chat_id': 'chat-a', 'dataset': 'cafe'}
    monkeypatch.setattr(chat_files, 'MAX_TABLE_CELLS', 3)
    for source in ['one,two\nthree,four\n', '"unclosed quote']:
        with pytest.raises(ValueError, match='valid CSV or TSV'):
            store.create({'name': 'report.csv', 'content': source}, **scope)
    assert store.list(owner_principal_id='alice') == []


def test_file_routes_private_preview_download_and_observer(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    agent = app.state.agent
    agent.children['chat-a'] = {'id': 'chat-a', 'owner_principal_id': 'alice', 'dataset': 'cafe', 'messages': []}
    store = ChatFiles(tmp_path / 'state')
    record = store.create({'name': 'customer report.md', 'content': '# Report\n<script>private()</script>'},
        owner_principal_id='alice', chat_id='chat-a', dataset='cafe')
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as owner:
        login(alice, app, 'alice'); login(bob, app, 'bob'); login(owner, app, 'owner', owner=True)
        result = alice.get('/api/files?chat_id=chat-a&dataset=cafe')
        assert result.status_code == 200
        assert result.json()['files'] == [record]
        assert alice.get(record['preview_url']).json()['content'].startswith('# Report')
        download = alice.get(record['download_url'])
        assert download.status_code == 200 and download.text.startswith('# Report')
        assert 'attachment;' in download.headers['content-disposition']
        assert download.headers['x-content-type-options'] == 'nosniff'
        assert 'no-store' in download.headers['cache-control']
        assert bob.get(record['preview_url']).status_code == 404
        assert bob.get(record['download_url']).status_code == 404
        assert bob.get('/api/files').json() == {'files': []}
        assert bob.get('/api/files?chat_id=chat-a').status_code == 404
        owner.post('/api/observer', json={'environment_id': 'demo', 'principal_id': 'alice'}, headers=HEADERS)
        assert owner.get(record['download_url']).status_code == 200
        bob.post('/api/session/demo', headers=HEADERS)
        assert bob.get('/api/files').status_code == 403


def test_native_preview_routes_only_inline_rasters_and_preserve_csp(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    store=ChatFiles(tmp_path/'state')
    records=[]
    for name,raw in [('photo.png',b'\x89PNG\r\n\x1a\nimage'),('report.pdf',b'%PDF-1.7\noriginal'),
                     ('drawing.svg',b'<svg><script>private()</script></svg>')]:
        digest=hashlib.sha256(raw).hexdigest()
        records.append((store.import_native(BytesIO(raw),name=name,title=None,size_bytes=len(raw),sha256=digest,
            publication_key=digest,provenance={'kind':'native_output'},owner_principal_id='alice',chat_id='chat-a',dataset='cafe'),raw))
    with TestClient(app) as alice,TestClient(app) as bob:
        login(alice,app,'alice');login(bob,app,'bob')
        for record,raw in records:
            path='/api/files/'+record['id']+'/preview'
            assert bob.get(path).status_code==404
            response=alice.get(path)
            if record['name']=='photo.png':
                assert response.status_code==200 and response.content==raw
                assert response.headers['content-type']=='image/png'
                assert response.headers['content-security-policy']=="default-src 'none'; sandbox; frame-ancestors 'self'"
                assert response.headers['cache-control']=='private, no-store'
                assert response.headers['x-content-type-options']=='nosniff'
            else:assert response.status_code==400
            download=alice.get(record['download_url'])
            assert download.content==raw and download.headers['content-disposition'].startswith('attachment;')
            assert bob.get(record['download_url']).status_code==404
