"""Owner-scoped connector source packages and encrypted, write-only credentials.

Source is an artifact, never code executed by the application process. A prepared
package becomes executable only through the separately commissioned executor or
customer runner. Customer secrets are encrypted under a host-only key; tool output
contains credential field names and readiness, never credential values.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import uuid
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from cryptography.fernet import Fernet

KINDS={'sql','api','sftp','cli','mcp','local','other'}
LABELS={'sql':'Database','api':'API','sftp':'File transfer','cli':'CLI','mcp':'MCP',
        'local':'Local system','other':'Custom'}
SECRET_NAME=re.compile(r'[A-Z][A-Z0-9_]{0,63}')
MAX_SOURCE_BYTES=1024*1024


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, limit, label, *, optional=False):
    if (not isinstance(value,str) or len(value)>limit or '\x00' in value
            or (not value.strip() and not optional)):
        raise ValueError(f'Provide {label} of up to {limit:,} characters.')
    return value.strip()


def contains_secret(value,secret):
    if isinstance(value,str):return secret in value
    if isinstance(value,dict):return any(contains_secret(k,secret) or contains_secret(v,secret) for k,v in value.items())
    if isinstance(value,list):return any(contains_secret(v,secret) for v in value)
    return False


class CustomConnectors:
    def __init__(self,state):
        self._public_value_readers = {}
        self.root=Path(state)/'custom-connectors'
        if self.root.is_symlink():raise ValueError('Review the connector storage location.')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.root.chmod(0o700)
        self.database=self.root/'connectors.sqlite'
        self.key_path=self.root/'credential-key'
        if self.root.is_symlink() or self.database.is_symlink() or self.key_path.is_symlink():
            raise ValueError('Review the connector storage location.')
        # Create with private mode before SQLite opens it, including an empty store.
        fd=os.open(self.database,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
        self.database.chmod(0o600)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS connectors (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, collection TEXT NOT NULL,
                    record TEXT NOT NULL, package TEXT);
                CREATE INDEX IF NOT EXISTS connector_owner ON connectors(owner,collection);
                CREATE TABLE IF NOT EXISTS credentials (
                    connector_id TEXT NOT NULL, name TEXT NOT NULL, ciphertext BLOB NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(connector_id,name));
                CREATE TABLE IF NOT EXISTS credential_requests (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL, record TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS credential_request_chat ON credential_requests(owner,chat_id);
                CREATE TABLE IF NOT EXISTS versions (
                    connector_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    record TEXT NOT NULL, package TEXT NOT NULL,
                    PRIMARY KEY(connector_id,revision));
            ''')
            from custom_execution import initialize_schema
            initialize_schema(db)

    def register_extension(self, name, *, initialize, public_values):
        if name in self._public_value_readers:
            return
        with self.db() as db:
            initialize(db)
        self._public_value_readers[name] = public_values

    def db(self):
        result=sqlite3.connect(self.database,timeout=15)
        result.execute('PRAGMA secure_delete=ON')
        return result

    @staticmethod
    def identity(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',value):
            raise ValueError('Choose a saved company workspace and connector owner.')
        return value

    def _row(self,db,key,owner):
        self.identity(owner)
        row=db.execute('SELECT record,package FROM connectors WHERE id=? AND owner=?',(key,owner)).fetchone()
        if row is None:raise ValueError('Choose a custom connector from your workspace.')
        return json.loads(row[0]),json.loads(row[1]) if row[1] else None

    def _public(self,db,record):
        from custom_execution import public_projection
        saved={r[0]:r[1] for r in db.execute('SELECT name,updated_at FROM credentials WHERE connector_id=?',(record['id'],))}
        return {**record,**public_projection(db,record['id']),'chat_url':f'/?chat={record["chat_id"]}',
                'package_url':f'/api/connections/custom/{record["id"]}/package' if record['revision']>1 else None,
                'secret_fields':[{**field,'configured':field['name'] in saved,
                                  'updated_at':saved.get(field['name'])} for field in record['secret_fields']]}

    def create(self,*,name,description,kind,collection,owner,chat_id=None):
        self.identity(owner);self.identity(collection)
        if chat_id is not None:self.identity(chat_id)
        if kind not in KINDS:raise ValueError('Choose a connection type.')
        description=text(description,8000,'a description')
        name=text(name or f'{LABELS[kind]} connector',100,'a name')
        key='custom-'+uuid.uuid4().hex
        record={'id':key,'name':name,'description':description,'summary':description[:180],
                'kind':kind,'collection':collection,'chat_id':chat_id or 'connector-'+uuid.uuid4().hex,
                'status':'draft','revision':1,'created_at':now(),'updated_at':now(),
                'secret_fields':[],'files':[],'instructions':'Describe the software and the work you want it to do.'}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM connectors').fetchone()[0]>=500:
                raise ValueError('Review existing connectors before adding another.')
            db.execute('INSERT INTO connectors VALUES (?,?,?,?,NULL)',(key,owner,collection,json.dumps(record)))
            return self._public(db,record)

    def get(self,key,*,owner):
        with self.db() as db:return self._public(db,self._row(db,key,owner)[0])

    def list(self,*,owner,collection=None):
        self.identity(owner)
        with self.db() as db:
            rows=db.execute('SELECT record FROM connectors WHERE owner=? ORDER BY rowid DESC',(owner,)).fetchall()
            return [self._public(db,record) for (raw,) in rows if (record:=json.loads(raw))
                    and (collection is None or record['collection']==collection)]

    def _cipher(self,db=None):
        # This directory is host state and is never mounted into generated-code workspaces.
        if not self.key_path.exists():
            if db is None:
                with self.db() as current:established=current.execute('SELECT 1 FROM credentials LIMIT 1').fetchone()
            else:established=db.execute('SELECT 1 FROM credentials LIMIT 1').fetchone()
            if established:raise ValueError('Restore the existing private credential key.')
            fd, temporary=tempfile.mkstemp(prefix='.credential-key-',dir=self.root)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(Fernet.generate_key());stream.flush();os.fsync(stream.fileno())
                try:os.link(temporary,self.key_path)
                except FileExistsError:pass
                directory=os.open(self.root,os.O_DIRECTORY)
                try:os.fsync(directory)
                finally:os.close(directory)
            finally:Path(temporary).unlink(missing_ok=True)
        if self.key_path.is_symlink() or self.key_path.stat().st_mode & 0o077:
            raise ValueError('Review the private credential store permissions.')
        return Fernet(self.key_path.read_bytes())

    @staticmethod
    def _validate_package(package):
        if not isinstance(package,dict) or not {'summary','instructions','secret_fields','files'}<=set(package) or set(package)-{'summary','instructions','secret_fields','files','execution'}:
            raise ValueError('Provide summary, instructions, secret_fields and source files.')
        summary=text(package['summary'],300,'a short summary')
        instructions=text(package['instructions'],16000,'setup instructions')
        fields=package['secret_fields'];files=package['files']
        if not isinstance(fields,list) or len(fields)>16:raise ValueError('Request up to sixteen credential fields.')
        names=set()
        for field in fields:
            if (not isinstance(field,dict) or set(field)!={'name','label','description'}
                    or not isinstance(field['name'],str) or not SECRET_NAME.fullmatch(field['name'])
                    or field['name'] in names):
                raise ValueError('Use distinct uppercase credential names, labels and descriptions.')
            text(field['label'],100,'a credential label');text(field['description'],500,'a credential description')
            names.add(field['name'])
        if not isinstance(files,list) or not 1<=len(files)<=32:raise ValueError('Provide one to thirty-two source files.')
        paths=set();total=0
        for file in files:
            if not isinstance(file,dict) or set(file)!={'path','content'}:raise ValueError('Provide source file paths and content.')
            path=file['path'];content=file['content']
            if (not isinstance(path,str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,160}',path)
                    or path.startswith('/') or any(part in {'','..','.'} for part in path.split('/'))
                    or any(part.startswith('.') for part in path.split('/'))
                    or path.lower()=='rocoto-connector.json' or path in paths):
                raise ValueError('Use distinct relative source paths, such as src/connector.py.')
            if PurePosixPath(path).suffix.lower() not in {'.py','.js','.ts','.mjs','.cjs','.json','.toml','.yaml','.yml','.md','.txt','.sql','.sh','.ps1'}:
                raise ValueError('Use source code, configuration or documentation files.')
            if not isinstance(content,str) or '\x00' in content:raise ValueError('Provide UTF-8 source text.')
            raw=content.encode('utf-8');total+=len(raw)
            if len(raw)>128*1024 or total>MAX_SOURCE_BYTES:raise ValueError('Keep source files within 128 KiB each and 1 MiB together.')
            if any(path.startswith(saved+'/')or saved.startswith(path+'/')for saved in paths):
                raise ValueError('Use source paths with distinct files and directories.')
            paths.add(path)
        result={'summary':summary,'instructions':instructions,'secret_fields':fields,'files':files}
        if 'execution'in package:
            from custom_execution import validate_manifest
            result['execution']=validate_manifest(package['execution'],files)
        return result

    def save_package(self,key,package,*,expected_revision,owner):
        package=self._validate_package(package)
        encoded=json.dumps(package)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record,_=self._row(db,key,owner)
            if type(expected_revision) is not int or record['revision']!=expected_revision:
                raise ValueError('Read the latest connector revision before saving changes.')
            secrets=db.execute('SELECT ciphertext FROM credentials WHERE connector_id=?',(key,)).fetchall()
            for (ciphertext,) in secrets:
                if contains_secret(package,self._cipher(db).decrypt(ciphertext).decode()):
                    raise ValueError('Use credential field references in connector source and instructions.')
            record.update(status='prepared',revision=record['revision']+1,updated_at=now(),
                          summary=package['summary'],instructions=package['instructions'],
                          secret_fields=package['secret_fields'],
                          files=[{'path':f['path'],'size_bytes':len(f['content'].encode('utf-8')),
                                  'sha256':hashlib.sha256(f['content'].encode('utf-8')).hexdigest()} for f in package['files']])
            if 'execution'in package:record['execution']=package['execution']
            else:record.pop('execution',None)
            db.execute('UPDATE connectors SET record=?,package=? WHERE id=?',(json.dumps(record),encoded,key))
            db.execute('INSERT INTO versions VALUES (?,?,?,?)',(key,record['revision'],json.dumps(record),encoded))
            self._cancel_requests(db,connector_id=key,reason='connector_changed')
            # A draft repair retains fields required by the last active version.
            from custom_execution import retained_fields,invalidate
            retained={field['name'] for field in package['secret_fields']+retained_fields(db,key)}
            removed=False
            for (name,) in db.execute('SELECT name FROM credentials WHERE connector_id=?',(key,)).fetchall():
                if name not in retained:
                    db.execute('DELETE FROM credentials WHERE connector_id=? AND name=?',(key,name));removed=True
            invalidate(db,key,'credential'if removed else'source')
            return self._public(db,record)

    def source(self,key,*,owner):
        with self.db() as db:
            record,package=self._row(db,key,owner)
            return {**self._public(db,record),'source_files':(package or {}).get('files',[])}

    def _set_secret(self,db,key,name,value,record,package):
        if not isinstance(name,str) or not SECRET_NAME.fullmatch(name):raise ValueError('Choose a requested credential field.')
        if not isinstance(value,str) or not 1<=len(value)<=16384 or '\x00' in value:raise ValueError('Enter a credential of up to 16,384 characters.')
        from custom_execution import retained_fields,public_values,invalidate
        if name not in {field['name'] for field in record['secret_fields']+retained_fields(db,key)}:raise ValueError('Choose a requested credential field.')
        histories=db.execute('SELECT record,package FROM versions WHERE connector_id=?',(key,)).fetchall()
        requests=db.execute('SELECT record FROM credential_requests WHERE connector_id=?',(key,)).fetchall()
        if (contains_secret(record,value) or contains_secret(package,value)
                or any(contains_secret(json.loads(r),value) or contains_secret(json.loads(p),value) for r,p in histories)
                or any(contains_secret(json.loads(r),value) for (r,) in requests)
                or any(contains_secret(metadata,value)for metadata in public_values(db,key,self._public_value_readers.values()))):
            raise ValueError('Replace credentials in source and descriptions with field references first.')
        stamp=now();ciphertext=self._cipher(db).encrypt(value.encode())
        db.execute('INSERT OR REPLACE INTO credentials VALUES (?,?,?,?)',(key,name,ciphertext,stamp))
        invalidate(db,key,'credential')
        return {'name':name,'configured':True,'updated_at':stamp}

    def set_secret(self,key,name,value,*,owner):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');record,package=self._row(db,key,owner)
            return self._set_secret(db,key,name,value,record,package)

    @staticmethod
    def _request_row(db,request_id,owner,chat_id):
        row=db.execute('SELECT record FROM credential_requests WHERE id=? AND owner=? AND chat_id=?',
                       (request_id,owner,chat_id)).fetchone()
        if row is None:raise ValueError('Choose a credential request in this chat.')
        return json.loads(row[0])

    @staticmethod
    def _save_request(db,record):
        db.execute('UPDATE credential_requests SET record=? WHERE id=?',(json.dumps(record),record['id']))
        return record

    def _cancel_requests(self,db,*,reason,chat_id=None,owner=None,connector_id=None):
        rows=db.execute('SELECT id,owner,chat_id,connector_id,record FROM credential_requests').fetchall()
        result=[]
        for _,saved_owner,saved_chat,saved_connector,raw in rows:
            if ((owner is not None and owner!=saved_owner) or (chat_id is not None and chat_id!=saved_chat)
                    or (connector_id is not None and connector_id!=saved_connector)):continue
            record=json.loads(raw)
            if record['status']!='pending':continue
            record.update(status='cancelled',resolution_reason=reason)
            self._save_request(db,record)
            result.append(record)
        return result

    def cancel_credential_requests(self,*,reason,chat_id=None,owner=None):
        if reason not in {'stopped','runtime_restarted','connector_changed','turn_ended','expired'}:
            raise ValueError('Choose a credential request resolution.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._cancel_requests(db,reason=reason,chat_id=chat_id,owner=owner)

    def _check_request_current(self,db,record,owner):
        if record['status']=='pending':
            current,_=self._row(db,record['connector_id'],owner)
            reason=('connector_changed' if current['revision']!=record['connector_revision'] else
                    'expired' if datetime.fromisoformat(record['expires_at'])<=datetime.now(timezone.utc) else None)
            if reason:
                record.update(status='timed_out' if reason=='expired' else 'cancelled',resolution_reason=reason)
                self._save_request(db,record)
        return record

    def credential_request(self,request_id,*,owner,chat_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._check_request_current(db,self._request_row(db,request_id,owner,chat_id),owner)

    def credential_requests(self,*,owner,chat_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT record FROM credential_requests WHERE owner=? AND chat_id=? ORDER BY rowid',
                            (owner,chat_id)).fetchall()
            return [self._check_request_current(db,json.loads(raw),owner) for (raw,) in rows]

    def _public_text(self,db,key,value):
        for (ciphertext,) in db.execute('SELECT ciphertext FROM credentials WHERE connector_id=?',(key,)):
            if contains_secret(value,self._cipher(db).decrypt(ciphertext).decode()):
                raise ValueError('Use a description of the setup step.')
        return value

    def request_credentials(self,key,*,owner,chat_id,collection,reason,expires_in=30):
        self.identity(owner);self.identity(chat_id);self.identity(collection)
        reason=text(reason,1000,'a short reason')
        if type(expires_in) is not int or not 1<=expires_in<=900:raise ValueError('Use a bounded credential request.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current,_=self._row(db,key,owner)
            if current['collection']!=collection:raise ValueError('Use a connector in this business.')
            if not current['secret_fields']:raise ValueError('Save the requested credential fields first.')
            self._public_text(db,key,reason)
            rows=db.execute('SELECT record FROM credential_requests WHERE owner=? AND chat_id=?',(owner,chat_id)).fetchall()
            for (raw,) in rows:
                existing=self._check_request_current(db,json.loads(raw),owner)
                if existing['status']=='pending':
                    if existing['connector_id']==key:return existing
                    raise ValueError('Complete the current credential request first.')
            if len(rows)>=100:raise ValueError('Continue credential setup in a new chat.')
            fields=[{name:field[name] for name in ('name','label','description','configured')}
                    for field in self._public(db,current)['secret_fields']]
            record={'id':'credential-'+uuid.uuid4().hex,'connector_id':key,
                    'connector_revision':current['revision'],'title':current['name'],'reason':reason,
                    'fields':fields,'status':'pending',
                    'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=expires_in)).isoformat()}
            db.execute('INSERT INTO credential_requests VALUES (?,?,?,?,?)',
                       (record['id'],owner,chat_id,key,json.dumps(record)))
            return record

    def resolve_credential_request(self,request_id,*,owner,chat_id,action,values=None,reason=None):
        if action not in {'submit','skip'}:raise ValueError('Choose Submit or Skip.')
        if action=='submit' and (not isinstance(values,dict) or reason is not None):
            raise ValueError('Provide the requested private credential fields.')
        if action=='skip':
            if values is not None:raise ValueError('Provide a reason for skipping.')
            reason=text(reason,1000,'a short reason for skipping')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._check_request_current(db,self._request_row(db,request_id,owner,chat_id),owner)
            if record['status'] in {'cancelled','timed_out'}:return record
            if record['status']!='pending':
                if record['status']=={'submit':'submitted','skip':'skipped'}[action]:return record
                raise ValueError('This credential request already has a response.')
            current,package=self._row(db,record['connector_id'],owner)
            if action=='skip':
                self._public_text(db,record['connector_id'],reason)
                record.update(status='skipped',skip_reason=reason)
            else:
                names={field['name'] for field in record['fields']}
                configured={row[0] for row in db.execute('SELECT name FROM credentials WHERE connector_id=?',(record['connector_id'],))}
                if set(values)-names or names-set(values)-configured:
                    raise ValueError('Complete the requested credential fields.')
                # The transaction rolls back every field if any field is rejected.
                for name,value in values.items():
                    self._set_secret(db,record['connector_id'],name,value,current,package)
                record.update(status='submitted',fields=[{**field,'configured':True} for field in record['fields']])
            return self._save_request(db,record)

    def remove_secret(self,key,name,*,owner):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self._row(db,key,owner)
            removed=db.execute('DELETE FROM credentials WHERE connector_id=? AND name=?',(key,name)).rowcount
            if removed:
                from custom_execution import invalidate
                invalidate(db,key,'credential')
            return {'name':name,'configured':False}

    def package(self,key,*,owner):
        with self.db() as db:
            record,package=self._row(db,key,owner)
            if package is None:raise ValueError('Prepare this connector in its setup chat first.')
        output=BytesIO()
        with ZipFile(output,'w',ZIP_DEFLATED) as archive:
            def write(name, content):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
            for file in package['files']:write(file['path'],file['content'])
            write('rocoto-connector.json',json.dumps({
                'schema':1,'id':key,'name':record['name'],'revision':record['revision'],
                'kind':record['kind'],'execution':'customer_runner_or_isolated_executor',
                'credential_names':[f['name'] for f in record['secret_fields']],
                'files':record['files'],'instructions':record['instructions'],
                'status':'prepared',**({'execution_manifest':package['execution']}if 'execution'in package else{})},indent=2))
        return output.getvalue()
