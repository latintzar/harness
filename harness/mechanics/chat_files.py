"""Private generated documents, bound by the host to a company, owner and chat.

Content and metadata commit together in SQLite. File names are labels, never paths.
The native agent receives creation/read tools; browsers receive scoped previews and
downloads. Editing packages remain in ExportStore and are linked by their host ID.
"""
from __future__ import annotations

from datetime import datetime, timezone
import csv
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import sqlite3
import uuid
import unicodedata

MAX_FILE_BYTES = 1024 * 1024
MAX_NATIVE_FILE_BYTES = 16 * 1024 * 1024
MAX_COPY_BYTES = 100 * 1024
MAX_COMPANY_BYTES = 64 * 1024 * 1024
MAX_COMPANY_FILES = 1000
MAX_TABLE_ROWS = 20000
MAX_TABLE_COLUMNS = 1000
MAX_TABLE_CELLS = 100000
csv.field_size_limit(MAX_FILE_BYTES)
FORMATS = {
    '.md': 'text/markdown', '.txt': 'text/plain', '.csv': 'text/csv',
    '.json': 'application/json', '.tsv': 'text/tab-separated-values',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pdf': 'application/pdf',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}
DISPLAY_TYPES = {'.md': 'markdown', '.txt': 'text', '.csv': 'table', '.tsv': 'table',
                 '.json': 'json', '.docx': 'document', '.pdf': 'pdf', '.xlsx': 'spreadsheet'}


def _spreadsheet_cell(value):
    # Spreadsheet applications discard leading whitespace before interpreting
    # formula prefixes. An apostrophe keeps external text literal on opening.
    start = 0
    while start < len(value) and (value[start].isspace() or ord(value[start]) < 32
                                 or value[start] == '\ufeff'):
        start += 1
    stripped = value[start:]
    if (stripped.lower().startswith('sep=') or (stripped[:1] in {'=', '+', '-', '@'}
            and not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', stripped))):
        return "'" + value
    return value


def _spreadsheet_download(content, suffix, *, legacy=False):
    delimiter = '\t' if suffix == '.tsv' else ','
    output = StringIO(newline='')
    # Quote every field so locale-based delimiter detection cannot reinterpret
    # a semicolon inside a data cell as a boundary exposing a hidden formula.
    writer = csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_ALL)
    cells = 0
    try:
        for count, row in enumerate(csv.reader(StringIO(content, newline=''), delimiter=delimiter, strict=True), 1):
            cells += len(row)
            if count > MAX_TABLE_ROWS or len(row) > MAX_TABLE_COLUMNS or cells > MAX_TABLE_CELLS:
                raise ValueError('Split tables into up to 20,000 rows, 1,000 columns and 100,000 cells per file.')
            safe = [_spreadsheet_cell(value) for value in row]
            writer.writerow(safe)
    except (csv.Error, ValueError):
        if not legacy:
            raise ValueError('Use valid CSV or TSV with up to 20,000 rows, 1,000 columns and 100,000 cells.') from None
        # Older files accepted arbitrary text. Keep it available as one literal
        # cell, while the source preview preserves the original text exactly.
        output = StringIO(newline='')
        csv.writer(output, delimiter=delimiter, quoting=csv.QUOTE_ALL).writerow([_spreadsheet_cell(content)])
    return output.getvalue().encode('utf-8')


class ChatFiles:
    def __init__(self, state, exports=None):
        self.path = Path(state) / 'chat-files.sqlite'
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('The file catalogue needs operator review.')
        self.exports = exports
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat_id TEXT NOT NULL,
                    dataset TEXT NOT NULL, metadata TEXT NOT NULL, content BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS files_owner ON files(owner, chat_id, dataset);
                CREATE TABLE IF NOT EXISTS native_publications (
                    publication_key TEXT PRIMARY KEY, file_id TEXT UNIQUE NOT NULL, provenance TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS export_chats (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat_id TEXT NOT NULL,
                    dataset TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS file_preferences (
                    owner TEXT NOT NULL, file_id TEXT NOT NULL, title TEXT,
                    last_opened_at TEXT, PRIMARY KEY(owner,file_id));
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'rendered' not in {row[1] for row in db.execute('PRAGMA table_info(files)')}:
                db.execute('ALTER TABLE files ADD COLUMN rendered BLOB')
            # Upgrade existing CSV/TSV downloads atomically with their metadata.
            # Source bytes remain the authoritative preview/read representation.
            for key, metadata, raw in db.execute('SELECT id,metadata,content FROM files WHERE rendered IS NULL'):
                record = json.loads(metadata)
                suffix = Path(record['name']).suffix.lower()
                if suffix not in {'.csv', '.tsv'} or record.get('representation') in {'native_original','provider_result'}:
                    continue
                rendered = _spreadsheet_download(raw.decode('utf-8'), suffix, legacy=True)
                record.update(size_bytes=len(rendered), sha256=hashlib.sha256(rendered).hexdigest(),
                              source_size_bytes=len(raw), source_sha256=hashlib.sha256(raw).hexdigest())
                db.execute('UPDATE files SET metadata=?,rendered=? WHERE id=?', (json.dumps(record), rendered, key))
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def _scope(owner_principal_id, chat_id=None, dataset=None):
        if not isinstance(owner_principal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', owner_principal_id):
            raise ValueError('Choose an authenticated file owner.')
        for value in (chat_id, dataset):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value)):
                raise ValueError('Choose a saved chat and business collection.')

    def create(self, document, *, owner_principal_id, chat_id, dataset):
        self._scope(owner_principal_id, chat_id, dataset)
        if not chat_id or not dataset:
            raise ValueError('Choose a saved chat and business collection.')
        if not isinstance(document, dict) or set(document) - {'name', 'title', 'content'}:
            raise ValueError('Provide a file name, title and text content.')
        name, title, content = document.get('name'), document.get('title'), document.get('content')
        if (not isinstance(name, str) or not 1 <= len(name) <= 120 or name != name.strip()
                or name.startswith('.') or any(c in name for c in '/\\')
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError('Use a short file name such as report.md or results.csv.')
        suffix = Path(name).suffix.lower()
        if suffix not in FORMATS:
            raise ValueError('Choose DOCX, PDF, XLSX, Markdown, text, CSV, TSV or JSON for this document.')
        if title is None:
            title = Path(name).stem
        if not isinstance(title, str) or not title.strip() or len(title) > 160 or any(ord(c) < 32 for c in title):
            raise ValueError('Use a short title for the file.')
        if not isinstance(content, str) or '\x00' in content:
            raise ValueError('Provide text content for the document.')
        try:
            raw = content.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError('Use valid Unicode text for the document.') from None
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError('Split this document into files of up to 1 MiB each.')
        if suffix == '.json':
            try:
                json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, RecursionError):
                raise ValueError('Provide valid JSON content.') from None
        rendered = None
        if suffix in {'.docx', '.pdf', '.xlsx'}:
            from document_rendering import render
            rendered = render(suffix, title.strip(), content)
        elif suffix in {'.csv', '.tsv'}:
            rendered = _spreadsheet_download(content, suffix)
        download = rendered if rendered is not None else raw
        key = 'file-' + uuid.uuid4().hex
        record = {'id': key, 'chat_id': chat_id, 'dataset': dataset, 'name': name,
            'title': title.strip(), 'media_type': FORMATS[suffix], 'size_bytes': len(download),
            'source_size_bytes': len(raw), 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'display_type': DISPLAY_TYPES[suffix],
            'preview_format': 'markdown' if suffix in {'.md', '.docx', '.pdf'} else ('table' if suffix in {'.csv', '.tsv', '.xlsx'} else 'text'),
            'created_at': datetime.now(timezone.utc).isoformat(), 'sha256': hashlib.sha256(download).hexdigest(),
            'download_url': f'/api/files/{key}/download', 'preview_url': f'/api/files/{key}/content',
            'copyable': len(raw) <= MAX_COPY_BYTES, 'kind': 'document', 'status': 'ready'}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count, used = db.execute('SELECT count(*),coalesce(sum(length(content)+coalesce(length(rendered),0)),0) FROM files').fetchone()
            if count >= MAX_COMPANY_FILES or used + len(raw) + len(rendered or b'') > MAX_COMPANY_BYTES:
                raise ValueError('The company file allowance is full. Ask the operator to archive earlier files.')
            db.execute('INSERT INTO files(id,owner,chat_id,dataset,metadata,content,rendered) VALUES(?,?,?,?,?,?,?)',
                (key, owner_principal_id, chat_id, dataset, json.dumps(record), raw, rendered))
        return record

    @staticmethod
    def native_name(name, title=None):
        if (not isinstance(name,str) or not 1<=len(name)<=120 or name!=name.strip()
                or name.startswith('.') or name.endswith('.')
                or any(c in '/\\<>:"|?*' or unicodedata.category(c).startswith('C') for c in name)):
            raise ValueError('Use a short file name for this output.')
        title=Path(name).stem if title is None else title
        if (not isinstance(title,str) or not title.strip() or len(title)>160
                or any(unicodedata.category(c).startswith('C') for c in title)):
            raise ValueError('Use a short title for the file.')
        return name,title.strip()

    @staticmethod
    def _native_preview(name, prefix, size):
        suffix=Path(name).suffix.lower()
        from stored_media import media_format
        media = media_format(name, prefix)
        if media:
            return media[0], media[1], None, media[1]
        # Signature checks classify originals; they never decode on the host.
        if suffix=='.pdf' and prefix.startswith(b'%PDF-'):
            return 'application/pdf','pdf',None,None
        images={'.png':('image/png',prefix.startswith(b'\x89PNG\r\n\x1a\n')),
                '.jpg':('image/jpeg',prefix.startswith(b'\xff\xd8\xff')),
                '.jpeg':('image/jpeg',prefix.startswith(b'\xff\xd8\xff')),
                '.webp':('image/webp',prefix.startswith(b'RIFF') and prefix[8:12]==b'WEBP')}
        if suffix in images and images[suffix][1]:
            return images[suffix][0],'image',None,'image'
        text_suffixes={'.txt','.md','.json','.csv','.tsv','.log','.py','.js','.css','.sql',
                       '.yaml','.yml','.xml','.html','.htm','.svg','.sh','.toml','.ini','.rst'}
        if suffix in text_suffixes and size<=MAX_FILE_BYTES:
            try:
                text=prefix.decode('utf-8')
                if any(ord(c)<32 and c not in '\t\n\r' for c in text):raise ValueError()
            except (UnicodeError,ValueError):pass
            else:
                preview='markdown' if suffix=='.md' else ('table' if suffix in {'.csv','.tsv'} else 'text')
                # Active formats and exact native CSV originals download as opaque
                # attachments. UTF-8 JSON previews remain inert in the JSON response.
                media='text/markdown' if suffix=='.md' else ('text/plain' if suffix=='.txt' else 'application/octet-stream')
                return media,DISPLAY_TYPES.get(suffix,'text'),preview,None
        return 'application/octet-stream',DISPLAY_TYPES.get(suffix,'file'),None,None

    def import_native(self, stream, *, name, title=None, size_bytes, sha256, publication_key,
                      provenance, owner_principal_id, chat_id, dataset):
        """Host-only exact-byte import from a verified private broker buffer.

        Original binary occupies one SQLite BLOB. Transactional publication and
        quota checks cover concurrency/retries; this never opens a model path.
        """
        self._scope(owner_principal_id,chat_id,dataset)
        if not chat_id or not dataset:raise ValueError('Choose a saved chat and business collection.')
        name,title=self.native_name(name,title)
        if (type(size_bytes) is not int or not 0<=size_bytes<=MAX_NATIVE_FILE_BYTES
                or not isinstance(sha256,str) or not re.fullmatch(r'[a-f0-9]{64}',sha256)
                or not isinstance(publication_key,str) or not re.fullmatch(r'[a-f0-9]{64}',publication_key)):
            raise ValueError('Choose an output of up to 16 MiB with a verified checksum.')
        prefix=stream.read(min(size_bytes,MAX_FILE_BYTES)+1);stream.seek(0)
        media,display,preview,inline=self._native_preview(name,prefix,size_bytes)
        key='file-'+uuid.uuid4().hex
        record={'id':key,'chat_id':chat_id,'dataset':dataset,'name':name,'title':title,
            'media_type':media,'size_bytes':size_bytes,'sha256':sha256,
            'source_size_bytes':size_bytes,'source_sha256':sha256,'representation':'native_original',
            'display_type':display,'preview_format':preview,'inline_preview_type':inline,
            'created_at':datetime.now(timezone.utc).isoformat(),
            'download_url':f'/api/files/{key}/download','preview_url':f'/api/files/{key}/content',
            'inline_preview_url':f'/api/files/{key}/preview' if inline else None,
            'copyable':preview is not None and size_bytes<=MAX_COPY_BYTES,'kind':'document','status':'ready'}
        # Broker identifiers stay in the private publication ledger, not the UI.
        with self.connect() as db:
            db.execute('PRAGMA busy_timeout=0')
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT f.owner,f.chat_id,f.dataset,f.metadata FROM native_publications n JOIN files f ON f.id=n.file_id WHERE n.publication_key=?',(publication_key,)).fetchone()
            if prior:
                metadata=json.loads(prior[3])
                if prior[:3]!=(owner_principal_id,chat_id,dataset) or metadata['sha256']!=sha256 or metadata['name']!=name:
                    raise ValueError('The publication identity needs operator review.')
                return metadata
            count,used=db.execute('SELECT count(*),coalesce(sum(length(content)+coalesce(length(rendered),0)),0) FROM files').fetchone()
            if count>=MAX_COMPANY_FILES or used+size_bytes>MAX_COMPANY_BYTES:
                raise ValueError('The company file allowance is full. Ask the operator to archive earlier files.')
            cursor=db.execute('INSERT INTO files(id,owner,chat_id,dataset,metadata,content,rendered) VALUES(?,?,?,?,?,zeroblob(?),NULL)',
                (key,owner_principal_id,chat_id,dataset,json.dumps(record),size_bytes))
            digest=hashlib.sha256();received=0
            with db.blobopen('files','content',cursor.lastrowid,readonly=False) as blob:
                while chunk:=stream.read(65536):
                    received+=len(chunk)
                    if received>size_bytes:raise ValueError('The output checksum needs another verified snapshot.')
                    digest.update(chunk);blob.write(chunk)
            if received!=size_bytes or digest.hexdigest()!=sha256:
                raise ValueError('The output checksum needs another verified snapshot.')
            db.execute('INSERT INTO native_publications(publication_key,file_id,provenance) VALUES(?,?,?)',(publication_key,key,json.dumps(provenance,separators=(',',':'))))
        return record

    def import_provider_resource(self, resource, *, provider, provider_name, publication_key, provenance,
                                 owner_principal_id, chat_id, dataset, guard, created=None):
        """Host-only provider metadata and returned text; this performs no URL fetch."""
        from provider_outputs import external_url, label, MAX_TEXT_BYTES
        self._scope(owner_principal_id,chat_id,dataset)
        if not chat_id or not dataset or not callable(guard) or not guard():
            raise ValueError('Open the resource in its active workspace.')
        if (not isinstance(resource,dict) or not isinstance(publication_key,str)
                or not re.fullmatch('[a-f0-9]{64}',publication_key)
                or not isinstance(provider,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',provider)):
            raise ValueError('Choose a verified provider resource.')
        text=resource.get('content');url=external_url(resource.get('source_url'))
        if text is not None and not isinstance(text,str):raise ValueError('Use the returned text snapshot.')
        raw=text.encode() if text is not None else b''
        if len(raw)>MAX_TEXT_BYTES or b'\x00' in raw or (not url and text is None):
            raise ValueError('Open this resource with its provider.')
        title=label(resource.get('title'),'Provider resource');name=label(resource.get('name'),title)
        key='file-'+uuid.uuid4().hex
        record={'id':key,'chat_id':chat_id,'dataset':dataset,'name':name,'title':title,
            'kind':'provider_resource','representation':'provider_result','status':'ready',
            'provider':provider,'provider_name':label(provider_name,'Provider'),'source_url':url,
            'description':label(resource.get('description'),limit=1000),
            'media_type':resource.get('media_type','application/octet-stream'),'size_bytes':None,
            'preview_format':'text' if text is not None else None,'display_type':'resource',
            'preview_url':f'/api/files/{key}/content','download_url':None,
            'snapshot_size_bytes':len(raw),'snapshot_sha256':hashlib.sha256(raw).hexdigest() if text is not None else None,
            'copyable':text is not None,'created_at':datetime.now(timezone.utc).isoformat()}
        with self.connect() as db:
            db.execute('PRAGMA busy_timeout=0');db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT f.owner,f.chat_id,f.dataset,f.metadata FROM native_publications n JOIN files f ON f.id=n.file_id WHERE n.publication_key=?',(publication_key,)).fetchone()
            if prior:
                if prior[:3]!=(owner_principal_id,chat_id,dataset):raise ValueError('Choose the owning resource workspace.')
                previous=json.loads(prior[3])
                if any(previous.get(k)!=record.get(k) for k in ('kind','provider','source_url','title','name','media_type','snapshot_sha256','snapshot_size_bytes')):
                    raise ValueError('Choose the original verified provider result.')
                return previous
            count,used=db.execute('SELECT count(*),coalesce(sum(length(content)+coalesce(length(rendered),0)),0) FROM files').fetchone()
            if count>=MAX_COMPANY_FILES or used+len(raw)>MAX_COMPANY_BYTES:
                raise ValueError('The company file allowance is full. Ask the operator to archive earlier files.')
            if not guard():raise ValueError('The resource request has finished.')
            db.execute('INSERT INTO files(id,owner,chat_id,dataset,metadata,content,rendered) VALUES(?,?,?,?,?,?,NULL)',
                (key,owner_principal_id,chat_id,dataset,json.dumps(record),raw))
            db.execute('INSERT INTO native_publications(publication_key,file_id,provenance) VALUES(?,?,?)',
                (publication_key,key,json.dumps(provenance,separators=(',',':'))))
        if created is not None:created.append((publication_key,key))
        return record

    def discard_provider_publications(self, created, *, owner_principal_id, chat_id, dataset):
        """Remove only this admission's new rows when its post-commit fence closes."""
        self._scope(owner_principal_id,chat_id,dataset)
        if not isinstance(created,list) or len(created)>16:raise ValueError('Use the bounded publication receipt.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for publication_key,file_id in created:
                row=db.execute('SELECT f.owner,f.chat_id,f.dataset,f.metadata FROM native_publications n JOIN files f ON f.id=n.file_id WHERE n.publication_key=? AND f.id=?',(publication_key,file_id)).fetchone()
                if row is None:continue
                if row[:3]!=(owner_principal_id,chat_id,dataset) or json.loads(row[3]).get('representation')!='provider_result':
                    raise ValueError('Choose the owning provider publication receipt.')
                db.execute('DELETE FROM native_publications WHERE publication_key=? AND file_id=?',(publication_key,file_id))
                db.execute('DELETE FROM files WHERE id=?',(file_id,))

    def link_export(self, export_id, *, owner_principal_id, chat_id, dataset):
        self._scope(owner_principal_id, chat_id, dataset)
        if not chat_id or not dataset or self.exports is None:
            raise ValueError('Choose a saved chat and editing package.')
        self.exports.get(export_id, owner_principal_id=owner_principal_id, dataset=dataset)
        with self.connect() as db:
            # A package's first host association is immutable, including its owner.
            db.execute('INSERT OR IGNORE INTO export_chats VALUES(?,?,?,?)',
                (export_id, owner_principal_id, chat_id, dataset))
            actual = db.execute('SELECT owner,chat_id,dataset FROM export_chats WHERE id=?', (export_id,)).fetchone()
            if actual != (owner_principal_id, chat_id, dataset):
                raise ValueError('Choose an editing package created in this chat.')
        return self.get(export_id, owner_principal_id=owner_principal_id)

    @staticmethod
    def _export(record, chat_id=None):
        key = record['id']
        return {'id': key, 'chat_id': chat_id, 'dataset': record['dataset'],
            'name': record['title'] + '.zip', 'title': record['title'], 'media_type': 'application/zip',
            'size_bytes': record.get('bytes', 0), 'created_at': record['created_at'],
            'download_url': f'/api/files/{key}/download' if record['status'] == 'ready' else None,
            'preview_url': f'/api/files/{key}/content', 'copyable': False,
            'kind': 'media_export', 'status': record['status'], 'summary': record.get('summary', ''),
            'item_count': record.get('item_count', 0), 'error': record.get('error')}

    def list(self, *, owner_principal_id, chat_id=None, dataset=None):
        self._scope(owner_principal_id, chat_id, dataset)
        sql, args = 'SELECT metadata FROM files WHERE owner=?', [owner_principal_id]
        for field, value in [('chat_id', chat_id), ('dataset', dataset)]:
            if value is not None:
                sql += f' AND {field}=?'
                args.append(value)
        with self.connect() as db:
            documents = [json.loads(r[0]) for r in db.execute(sql, args)]
            associations = {r[0]: r[1] for r in db.execute('SELECT id,chat_id FROM export_chats WHERE owner=?', (owner_principal_id,))}
            preferences = {r[0]: r[1:] for r in db.execute(
                'SELECT file_id,title,last_opened_at FROM file_preferences WHERE owner=?', (owner_principal_id,))}
        packages = self.exports.list(owner_principal_id=owner_principal_id, dataset=dataset) if self.exports else []
        documents.extend(self._export(r, associations.get(r['id'])) for r in packages
            if chat_id is None or associations.get(r['id']) == chat_id)
        return sorted((self._preferences(r, preferences.get(r['id'])) for r in documents),
                      key=lambda r: r['created_at'], reverse=True)

    @staticmethod
    def _preferences(record, preferences):
        # Display preferences are separate from immutable source/provider metadata.
        if preferences:
            title, opened = preferences
            if title is not None: record = {**record, 'title': title}
            if opened is not None: record = {**record, 'last_opened_at': opened}
        return record

    def get(self, file_id, *, owner_principal_id, chat_id=None, dataset=None):
        self._scope(owner_principal_id, chat_id, dataset)
        if not isinstance(file_id, str) or not re.fullmatch(r'(?:file|export)-[0-9a-f]{32}', file_id):
            raise ValueError('Choose a file from this workspace.')
        if file_id.startswith('export-') and self.exports:
            source = self.exports.get(file_id, owner_principal_id=owner_principal_id, dataset=dataset)
            with self.connect() as db:
                row = db.execute('SELECT chat_id FROM export_chats WHERE id=? AND owner=?', (file_id, owner_principal_id)).fetchone()
            record = self._export(source, row[0] if row else None)
        else:
            with self.connect() as db:
                row = db.execute('SELECT metadata FROM files WHERE id=? AND owner=?', (file_id, owner_principal_id)).fetchone()
            if row is None:
                raise ValueError('Choose a file from this workspace.')
            record = json.loads(row[0])
        if (chat_id is not None and record['chat_id'] != chat_id) or (dataset is not None and record['dataset'] != dataset):
            raise ValueError('Choose a file from this chat and business collection.')
        with self.connect() as db:
            preferences = db.execute('SELECT title,last_opened_at FROM file_preferences WHERE owner=? AND file_id=?',
                                     (owner_principal_id, file_id)).fetchone()
        return self._preferences(record, preferences)

    def rename(self, file_id, title, *, owner_principal_id):
        self.get(file_id, owner_principal_id=owner_principal_id)
        if (not isinstance(title, str) or not title.strip() or len(title) > 160
                or any(unicodedata.category(c) in {'Cc', 'Cs'} for c in title)):
            raise ValueError('Use a file title with 1–160 characters.')
        with self.connect() as db:
            db.execute('''INSERT INTO file_preferences(owner,file_id,title) VALUES(?,?,?)
                ON CONFLICT(owner,file_id) DO UPDATE SET title=excluded.title''',
                (owner_principal_id, file_id, title.strip()))
        return self.get(file_id, owner_principal_id=owner_principal_id)

    def opened(self, file_id, *, owner_principal_id):
        self.get(file_id, owner_principal_id=owner_principal_id)
        with self.connect() as db:
            db.execute('''INSERT INTO file_preferences(owner,file_id,last_opened_at) VALUES(?,?,?)
                ON CONFLICT(owner,file_id) DO UPDATE SET last_opened_at=excluded.last_opened_at''',
                (owner_principal_id, file_id, datetime.now(timezone.utc).isoformat()))
        return self.get(file_id, owner_principal_id=owner_principal_id)

    def content(self, file_id, *, owner_principal_id, chat_id=None, dataset=None):
        record = self.get(file_id, owner_principal_id=owner_principal_id, chat_id=chat_id, dataset=dataset)
        if record['kind'] == 'media_export' or (record.get('representation') in {'native_original','provider_result'} and record.get('preview_format') is None):
            return {'file': record, 'content': None}
        with self.connect() as db:
            raw = db.execute('SELECT content FROM files WHERE id=? AND owner=?', (file_id, owner_principal_id)).fetchone()[0]
        return {'file': record, 'content': raw.decode('utf-8')}

    def download(self, file_id, *, owner_principal_id, chat_id=None, dataset=None):
        record = self.get(file_id, owner_principal_id=owner_principal_id, chat_id=chat_id, dataset=dataset)
        if record['kind'] == 'provider_resource':
            raise ValueError('Open this resource with its provider.')
        if record['kind'] != 'document':
            raise ValueError('Download this editing package through its package store.')
        with self.connect() as db:
            source, rendered = db.execute('SELECT content,rendered FROM files WHERE id=? AND owner=?',
                                         (file_id, owner_principal_id)).fetchone()
        return record, rendered if rendered is not None else source
