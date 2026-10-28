"""Host-only custom adapter authority and test receipts; executes no source.

Owner-selected targets are separate from model-authored packages. Runtime receipt
acceptance defaults closed. The future gateway/broker supplies actual evidence;
this module supplies durable identity, revocation and activation CAS boundaries.
"""
from __future__ import annotations
from dataclasses import dataclass,field
import hashlib
from functools import wraps
import ipaddress
import json
import math
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from workspace_identity import workspace_scope

PROFILE='https-python-read-v1'
IMAGE='sha256:30fb0c0f17c5cdf33dd0cc883956fec220497343df8003220fcbc3cd1b95608f'
MAX_TESTS=256
MAX_GRANTS=1024
MAX_GRANT_BYTES=16*1024*1024
MAX_DOCUMENT_BYTES=128*1024
_NAME=re.compile(r'[a-z][a-z0-9_]{0,63}')
_HASH=re.compile(r'[a-f0-9]{64}')
_FIELD=re.compile(r'[A-Z][A-Z0-9_]{0,63}')

class ExecutionError(ValueError):pass


def require(value,message='Use the current custom connector execution contract.'):
    if not value:raise ExecutionError(message)


def encoded(value):
    try:return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    except (ValueError,TypeError,RecursionError):raise ExecutionError('Use bounded JSON metadata.')from None


def digest(value):return hashlib.sha256(encoded(value)).hexdigest()


def exact(value,keys):require(isinstance(value,dict)and set(value)==set(keys))

def name(value):require(isinstance(value,str)and _NAME.fullmatch(value));return value


def _validation(function):
    @wraps(function)
    def checked(*args,**kwargs):
        try:return function(*args,**kwargs)
        except (TypeError,KeyError,AttributeError,RecursionError,OverflowError):
            raise ExecutionError('Use the bounded custom connector metadata contract.')from None
    return checked


@_validation
def schema(value):
    """A bounded, fully expanded JSON-schema subset; no regex/remote resolution."""
    require(len(encoded(value))<=16384)
    allowed={'type','properties','required','additionalProperties','items','enum','minimum','maximum',
             'minItems','maxItems','minLength','maxLength','description'}
    def walk(node,depth=0):
        require(isinstance(node,dict)and depth<=8 and set(node)<=allowed)
        kind=node.get('type');require(kind in {'object','array','string','integer','number','boolean','null'})
        if 'description'in node:require(isinstance(node['description'],str)and len(node['description'])<=1024)
        if 'enum'in node:
            require(isinstance(node['enum'],list)and 1<=len(node['enum'])<=64 and
                    all(isinstance(x,(str,int,float,bool))or x is None for x in node['enum']))
        if kind=='object':
            require(node.get('additionalProperties')is False)
            props=node.get('properties',{});require(isinstance(props,dict)and len(props)<=32)
            for key,child in props.items():require(isinstance(key,str)and len(key)<=128);walk(child,depth+1)
        elif kind=='array':
            require(type(node.get('maxItems'))is int and 0<=node['maxItems']<=64)
            walk(node.get('items'),depth+1)
        for key in ('minItems','maxItems','minLength','maxLength'):
            if key in node:require(type(node[key])is int and 0<=node[key]<=16384)
    walk(value)
    try:Draft202012Validator.check_schema(value)
    except Exception:raise ExecutionError('Use the supported bounded tool schema.')from None
    return json.loads(encoded(value))


@_validation
def validate_manifest(value,files):
    exact(value,{'profile','entrypoint','tools','tests'});require(value['profile']==PROFILE)
    path=value['entrypoint']
    require(isinstance(path,str)and re.fullmatch(r'[A-Za-z0-9_/-]+\.py',path)and
            not path.startswith('/')and all(p not in {'','..','.'}for p in path.split('/'))and
            path in {f['path']for f in files},'Choose an existing relative Python entrypoint.')
    tools=value['tools'];require(isinstance(tools,list)and 1<=len(tools)<=16)
    names=set();normalized=[]
    for tool in tools:
        exact(tool,{'name','description','input_schema','output_schema','operations'})
        key=name(tool['name']);require(key not in names);names.add(key)
        require(isinstance(tool['description'],str)and 1<=len(tool['description'])<=1000)
        operations=tool['operations'];require(isinstance(operations,list)and 1<=len(operations)<=16)
        for operation in operations:name(operation)
        require(len(set(operations))==len(operations))
        normalized.append({**tool,'input_schema':schema(tool['input_schema']),'output_schema':schema(tool['output_schema'])})
    tests=value['tests'];require(isinstance(tests,list)and 1<=len(tests)<=32)
    tested=set();seen=set()
    for test in tests:
        exact(test,{'tool','arguments'});require(test['tool']in names and isinstance(test['arguments'],dict)and len(encoded(test['arguments']))<=16384)
        require(digest(test)not in seen);seen.add(digest(test));tested.add(test['tool'])
        tool=next(t for t in normalized if t['name']==test['tool'])
        require(Draft202012Validator(tool['input_schema']).is_valid(test['arguments']),'Use test inputs matching the declared tool.')
    require(tested==names,'Include an observational test for every declared tool.')
    result={**value,'tools':normalized};require(len(encoded(result))<=64*1024)
    return json.loads(encoded(result))


def authentication_fields(auth):
    if auth['kind']in {'bearer','header'}:return (auth['credential'],)
    if auth['kind']=='basic':return tuple(sorted({auth['username_field'],auth['password_field']}))
    return ()


@_validation
def validate_target(value,declared):
    exact(value,{'origin','authentication','operations'})
    origin=value['origin'];require(isinstance(origin,str)and len(origin)<=2048)
    try:parsed=urlsplit(origin);port=parsed.port
    except ValueError:raise ExecutionError('Choose a public HTTPS origin.')from None
    host=parsed.hostname
    require(parsed.scheme=='https'and host and parsed.username is None and parsed.password is None and
        port in(None,443)and parsed.path in('','/')and not parsed.query and not parsed.fragment and
        not any(c.isspace()for c in origin),'Choose a public HTTPS origin without embedded credentials.')
    require(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host)and '.'in host and
        all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label)for label in host.split('.'))and
        not host.endswith(('.local','.localhost','.internal','.home','.test')),
        'Choose the public API hostname.')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ExecutionError('Choose the public API hostname.')
    auth=value['authentication'];require(isinstance(auth,dict))
    kind=auth.get('kind')
    expected={'none':{'kind'},'bearer':{'kind','credential'},'header':{'kind','name','credential'},
              'basic':{'kind','username_field','password_field'}}
    require(kind in expected);exact(auth,expected[kind])
    refs=([auth['credential']]if kind in {'bearer','header'}else[auth['username_field'],auth['password_field']]if kind=='basic'else[])
    require(all(isinstance(ref,str)and _FIELD.fullmatch(ref)and ref in declared for ref in refs),'Choose declared credential references.')
    if kind=='header':
        header=auth['name'];require(isinstance(header,str)and re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}',header)and
            header.lower()not in {'host','authorization','cookie','proxy-authorization','connection','content-length','transfer-encoding','upgrade'}and
            not header.lower().startswith(('proxy-','sec-','x-forwarded-')),'Choose the API credential header.')
    operations=value['operations'];require(isinstance(operations,list)and 1<=len(operations)<=32)
    seen=set();normalized=[]
    for operation in operations:
        exact(operation,{'id','method','path','query_schema'});key=name(operation['id'])
        require(key not in seen);seen.add(key);require(operation['method']in {'GET','HEAD'})
        path=operation['path'];require(isinstance(path,str)and len(path)<=1024 and
            re.fullmatch(r'/[A-Za-z0-9_./-]*',path)and '//'not in path)
        if path!='/':require(all(part not in {'.','..',''}for part in path.strip('/').split('/')))
        normalized.append({**operation,'query_schema':schema(operation['query_schema'])})
    result={'origin':'https://'+host,'authentication':dict(auth),'operations':normalized}
    require(len(encoded(result))<=64*1024);return json.loads(encoded(result))


def initialize_schema(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS custom_execution_authority(
      connector_id TEXT PRIMARY KEY,credential_generation INTEGER NOT NULL,target_generation INTEGER NOT NULL,
      revocation_generation INTEGER NOT NULL,active_generation INTEGER NOT NULL,active_snapshot TEXT,
      active_receipt TEXT,active_status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS custom_execution_grants(
      connector_id TEXT NOT NULL,generation INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(connector_id,generation));
    CREATE TABLE IF NOT EXISTS custom_execution_tests(
      id TEXT PRIMARY KEY,connector_id TEXT NOT NULL,owner TEXT NOT NULL,collection TEXT NOT NULL,
      token_hash TEXT NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,evidence TEXT);
    CREATE TABLE IF NOT EXISTS custom_target_proposals(
      id TEXT PRIMARY KEY,connector_id TEXT NOT NULL,owner TEXT NOT NULL,
      collection TEXT NOT NULL,document TEXT NOT NULL,created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS custom_proposal_connector ON custom_target_proposals(connector_id,created);
    CREATE TABLE IF NOT EXISTS custom_execution_jobs(
      id TEXT PRIMARY KEY,connector_id TEXT NOT NULL,owner TEXT NOT NULL,collection TEXT NOT NULL,
      snapshot_sha256 TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT);
    CREATE INDEX IF NOT EXISTS custom_execution_jobs_scope ON custom_execution_jobs(connector_id,status);
    ''')


def authority(db,key):
    db.execute('INSERT OR IGNORE INTO custom_execution_authority VALUES(?,(SELECT CASE WHEN count(*)>0 THEN 1 ELSE 0 END FROM credentials WHERE connector_id=?),0,1,0,NULL,NULL,\'unconfigured\')',(key,key))
    columns=['connector_id','credential_generation','target_generation','revocation_generation','active_generation','active_snapshot','active_receipt','active_status']
    return dict(zip(columns,db.execute('SELECT * FROM custom_execution_authority WHERE connector_id=?',(key,)).fetchone()))


def retained_fields(db,key):
    row=authority(db,key)
    return json.loads(row['active_snapshot'])['secret_fields']if row['active_snapshot']else[]


def invalidate(db,key,kind):
    row=authority(db,key)
    if kind=='credential':db.execute('UPDATE custom_execution_authority SET credential_generation=credential_generation+1 WHERE connector_id=?',(key,))
    if kind=='revoked':db.execute('UPDATE custom_execution_authority SET revocation_generation=revocation_generation+1 WHERE connector_id=?',(key,))
    if kind=='revoked'or(kind!='source'and row['active_snapshot']):
        db.execute('UPDATE custom_execution_authority SET active_status=? WHERE connector_id=?',('revoked'if kind=='revoked'else'needs_verification',key))
    # Completed evidence remains historical truth; fresh activation rechecks its binding.
    db.execute("UPDATE custom_execution_tests SET status='invalidated' WHERE connector_id=? AND status='pending'",(key,))


def public_projection(db,key):
    row=authority(db,key)
    saved={name:stamp for name,stamp in db.execute('SELECT name,updated_at FROM credentials WHERE connector_id=?',(key,))}
    return {'credential_generation':row['credential_generation'],'target_generation':row['target_generation'],
        'active_generation':row['active_generation'],'active_status':row['active_status'],
        'active_revision':json.loads(row['active_snapshot'])['revision']if row['active_snapshot']else None,
        'active_secret_fields':[{**f,'configured':f['name']in saved,'updated_at':saved.get(f['name'])}for f in retained_fields(db,key)]}


def public_values(db,key,readers=()):
    return [value for read in readers for value in read(db,key)]+[json.loads(raw)for(raw,)in db.execute('SELECT document FROM custom_execution_grants WHERE connector_id=?',(key,))]+[
        json.loads(raw)for(raw,)in db.execute('SELECT snapshot FROM custom_execution_tests WHERE connector_id=?',(key,))]+[
        json.loads(raw)for(raw,)in db.execute('SELECT document FROM custom_target_proposals WHERE connector_id=?',(key,))]


@dataclass(frozen=True)
class RuntimeProfile:
    image_digest:str
    worker_sha256:str
    profile:str=PROFILE
    def __post_init__(self):require(self.image_digest==IMAGE and isinstance(self.worker_sha256,str)and _HASH.fullmatch(self.worker_sha256)and self.profile==PROFILE)
    def document(self):return {'profile':self.profile,'image_digest':self.image_digest,'worker_sha256':self.worker_sha256}

@dataclass(frozen=True)
class SourceFile:
    path:str
    content:bytes
    sha256:str
    def __post_init__(self):
        require(isinstance(self.path,str)and type(self.content)is bytes and len(self.content)<=128*1024 and
            isinstance(self.sha256,str)and self.sha256==hashlib.sha256(self.content).hexdigest())

@dataclass(frozen=True)
class ExecutionSnapshot:
    document:bytes
    files:tuple[SourceFile,...]
    def __post_init__(self):
        require(type(self.document)is bytes and len(self.document)<=MAX_DOCUMENT_BYTES and
            type(self.files)is tuple and 1<=len(self.files)<=32 and all(isinstance(f,SourceFile)for f in self.files)and
            sum(len(f.content)for f in self.files)<=1024*1024)
        try:value=json.loads(self.document)
        except (ValueError,UnicodeError,RecursionError):raise ExecutionError('Use the immutable execution snapshot.')from None
        exact(value,{'schema','company','owner','collection','connector_id','revision','source_sha256','files','secret_fields',
            'manifest','manifest_sha256','target','target_sha256','credential_generation','target_generation','revocation_generation','runtime'})
        require(encoded(value)==self.document,'Use the canonical execution snapshot.')
    @property
    def metadata(self):return json.loads(self.document)
    @property
    def sha256(self):return hashlib.sha256(self.document).hexdigest()

@dataclass(frozen=True)
class CredentialSnapshot:
    snapshot_sha256:str
    company_id:str
    owner_id:str
    connector_id:str
    credential_generation:int
    fields:tuple[tuple[str,str],...]=field(repr=False)
    def as_mapping(self):return dict(self.fields)


@dataclass(frozen=True)
class TestLease:
    id:str
    token:str=field(repr=False)
    snapshot_sha256:str
    def __post_init__(self):
        require(isinstance(self.id,str)and re.fullmatch(r'test-[a-f0-9]{32}',self.id)and
            isinstance(self.token,str)and _HASH.fullmatch(self.token)and
            isinstance(self.snapshot_sha256,str)and _HASH.fullmatch(self.snapshot_sha256))


class CustomExecution:
    def __init__(self,store,*,runtime=None,clock=time.time):
        from custom_connectors import CustomConnectors
        require(isinstance(store,CustomConnectors)and(runtime is None or isinstance(runtime,RuntimeProfile)))
        self.store,self.runtime,self.clock=store,runtime,clock
        self.company=workspace_scope(store.root.parent)

    def _owned(self,db,key,owner,collection):
        record,package=self.store._row(db,key,owner)
        require(record['collection']==collection,'Choose a connector in this business.')
        return record,package

    def status(self,key,*,owner,collection):
        with self.store.db()as db:
            self._owned(db,key,owner,collection)
            return {'id':key,**public_projection(db,key),'runtime_available':self.runtime is not None,'profile':PROFILE}

    def grant_target(self,key,target,*,owner,collection,expected_generation,expected_revision=None):
        """Called only from authenticated owner target selection, never save/tools."""
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');record,package=self._owned(db,key,owner,collection);row=authority(db,key)
            if expected_revision is not None:
                require(type(expected_revision)is int and record['revision']==expected_revision,'Review access for the current connector version.')
            require(type(expected_generation)is int and row['target_generation']==expected_generation,'Review the current target grant.')
            declared={f['name']for f in record['secret_fields']}|{f['name']for f in retained_fields(db,key)}
            document=validate_target(target,declared);self.store._public_text(db,key,document)
            if row['target_generation']:
                previous=db.execute('SELECT document FROM custom_execution_grants WHERE connector_id=? AND generation=?',(key,row['target_generation'])).fetchone()
                if previous and json.loads(previous[0])==document and row['active_status']!='revoked':return {'generation':row['target_generation'],'target':document}
            count,total=db.execute('SELECT count(*),coalesce(sum(length(CAST(document AS BLOB))),0)FROM custom_execution_grants').fetchone()
            require(count<MAX_GRANTS and total+len(encoded(document))<=MAX_GRANT_BYTES,'Review retained target grant history.')
            generation=row['target_generation']+1
            db.execute('INSERT INTO custom_execution_grants VALUES(?,?,?)',(key,generation,encoded(document).decode()))
            db.execute('UPDATE custom_execution_authority SET target_generation=?,active_status=? WHERE connector_id=?',
                (generation,'needs_verification'if row['active_snapshot']else'unconfigured',key))
            invalidate(db,key,'target')
            return {'generation':generation,'target':document}

    def _snapshot(self,db,key,owner,collection,revision):
        current,_=self._owned(db,key,owner,collection);row=authority(db,key)
        require(row['active_status']!='revoked','Approve the owner target again before executing this connector.')
        saved=db.execute('SELECT record,package FROM versions WHERE connector_id=? AND revision=?',(key,revision)).fetchone()
        require(saved is not None,'Prepare the source revision first.')
        record,package=map(json.loads,saved);package=self.store._validate_package(package)
        require('execution'in package,'Add the validated execution manifest to this source revision.')
        manifest=package['execution'];declared={f['name']for f in package['secret_fields']}
        grant=db.execute('SELECT document FROM custom_execution_grants WHERE connector_id=? AND generation=?',(key,row['target_generation'])).fetchone()
        require(grant is not None,'Choose and approve this connector’s target first.')
        target=validate_target(json.loads(grant[0]),declared)
        require(declared==set(authentication_fields(target['authentication'])),'Declare exactly the credential fields used by this auth profile.')
        operations={o['id']for o in target['operations']}
        require(all(set(t['operations'])<=operations for t in manifest['tools']),'Grant every declared operation before testing.')
        configured={name for(name,)in db.execute('SELECT name FROM credentials WHERE connector_id=?',(key,))}
        require(declared<=configured,'Configure the declared credential fields before testing.')
        if declared:
            cipher=self.store._cipher(db)
            try:
                for field,ciphertext in db.execute('SELECT name,ciphertext FROM credentials WHERE connector_id=?',(key,)):
                    if field in declared:cipher.decrypt(ciphertext)
            except Exception:raise ExecutionError('Restore the private connector credentials.')from None
        files=tuple(SourceFile(f['path'],f['content'].encode(),hashlib.sha256(f['content'].encode()).hexdigest())for f in sorted(package['files'],key=lambda f:f['path']))
        expected=[{'path':f.path,'size_bytes':len(f.content),'sha256':f.sha256}for f in files]
        require(sorted(record['files'],key=lambda f:f['path'])==expected,'Restore the immutable source revision.')
        document={'schema':1,'company':self.company,'owner':owner,'collection':collection,'connector_id':key,'revision':revision,
            'source_sha256':digest(package),'files':expected,'secret_fields':package['secret_fields'],
            'manifest':manifest,'manifest_sha256':digest(manifest),'target':target,'target_sha256':digest(target),
            'credential_generation':row['credential_generation'],'target_generation':row['target_generation'],
            'revocation_generation':row['revocation_generation'],'runtime':self.runtime.document()if self.runtime else None}
        require(len(encoded(document))<=MAX_DOCUMENT_BYTES)
        return ExecutionSnapshot(encoded(document),files)

    def snapshot(self,key,*,owner,collection,expected_revision):
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');record,_=self._owned(db,key,owner,collection)
            require(type(expected_revision)is int and record['revision']==expected_revision,'Read the current source revision.')
            return self._snapshot(db,key,owner,collection,expected_revision)

    def _validate(self,db,snapshot,owner,collection,*,draft=False):
        require(isinstance(snapshot,ExecutionSnapshot))
        data=snapshot.metadata
        require(data['company']==self.company and data['owner']==owner and data['collection']==collection,'Use the owning execution snapshot.')
        current,_=self._owned(db,data['connector_id'],owner,collection)
        require(not draft or current['revision']==data['revision'],'Test the current draft before activating it.')
        if not draft and current['revision']!=data['revision']:
            active=authority(db,data['connector_id'])
            require(active['active_status']=='active'and active['active_snapshot']==snapshot.document.decode(),
                'Use the current draft or the active execution version.')
        actual=self._snapshot(db,data['connector_id'],owner,collection,data['revision'])
        require(snapshot==actual,'The connector authority changed; verify the current version.')
        return actual

    def validate_snapshot(self,snapshot,*,owner,collection):
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');return self._validate(db,snapshot,owner,collection)

    def authority_current(self,snapshot,*,owner,collection):
        return self.authority_probe(snapshot,owner=owner,collection=collection)is True

    def authority_probe(self,snapshot,*,owner,collection):
        """Cheap host-only wire fence after full off-thread snapshot validation.

        Read only scalar authority/current-version metadata with zero SQLite
        lock wait. It does not reconstruct source, decrypt credentials or admit
        a job. Busy/missing/corrupt storage closes this operation immediately.
        """
        db=None
        try:
            if not isinstance(snapshot,ExecutionSnapshot)or self.runtime is None:return False
            data=snapshot.metadata
            if(data['company']!=self.company or data['owner']!=owner or data['collection']!=collection
                or data['runtime']!=self.runtime.document()):return False
            db=sqlite3.connect(self.store.database.absolute().as_uri()+'?mode=ro',uri=True,timeout=0)
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA busy_timeout=0')
            row=db.execute('''SELECT 1 FROM connectors c JOIN custom_execution_authority a ON a.connector_id=c.id
                WHERE c.id=? AND c.owner=? AND c.collection=?
                AND json_extract(c.record,'$.collection')=?
                AND a.credential_generation=? AND a.target_generation=? AND a.revocation_generation=?
                AND a.active_status!='revoked'
                AND (json_extract(c.record,'$.revision')=? OR(a.active_status='active'AND a.active_snapshot=?))
                AND NOT EXISTS(SELECT 1 FROM custom_execution_tests t WHERE t.connector_id=c.id AND t.status='cleanup_uncertain')
                AND NOT EXISTS(SELECT 1 FROM custom_execution_jobs j WHERE j.connector_id=c.id AND j.status='cleanup_uncertain')''',
                (data['connector_id'],owner,collection,collection,data['credential_generation'],data['target_generation'],
                 data['revocation_generation'],data['revision'],snapshot.document.decode())).fetchone()
            return row is not None
        except sqlite3.OperationalError as exc:
            if (getattr(exc,'sqlite_errorcode',0)&0xff)in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED}:return None
            return False
        except (ValueError,KeyError,TypeError,OSError,sqlite3.Error):return False
        finally:
            if db is not None:db.close()

    def host_credentials(self,snapshot,*,owner,collection):
        """Host gateway only; hold its admission fence and recheck before I/O.

        This synchronous snapshot neither admits nor performs a network effect.
        Never serialize the returned private fields into tools, events or HTTP.
        """
        require(self.runtime is not None,'Use the commissioned custom executor.')
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');snapshot=self._validate(db,snapshot,owner,collection);data=snapshot.metadata
            fields=authentication_fields(data['target']['authentication']);values=[]
            cipher=self.store._cipher(db)if fields else None
            for name in fields:
                row=db.execute('SELECT ciphertext FROM credentials WHERE connector_id=? AND name=?',(data['connector_id'],name)).fetchone()
                require(row is not None,'Configure the current credential fields.')
                try:values.append((name,cipher.decrypt(row[0]).decode()))
                except Exception:raise ExecutionError('Restore the private connector credentials.')from None
            return CredentialSnapshot(snapshot.sha256,self.company,owner,data['connector_id'],data['credential_generation'],tuple(values))

    def active_snapshot(self,key,*,owner,collection):
        require(self.runtime is not None,'Use the commissioned custom executor.')
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');self._owned(db,key,owner,collection);row=authority(db,key)
            self._cleanup_fence(db,key)
            require(row['active_status']=='active'and row['active_snapshot'],'Verify and activate a connector version first.')
            document=json.loads(row['active_snapshot']);snapshot=self._snapshot(db,key,owner,collection,document['revision'])
            require(snapshot.document==encoded(document),'Verify the current active authority.')
            return snapshot

    def admit_job(self,job_id,snapshot,*,owner,collection):
        """Host coordinator only: persist before any broker admission/network."""
        require(isinstance(job_id,str)and re.fullmatch(r'[a-f0-9]{32}',job_id))
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');self._validate(db,snapshot,owner,collection)
            data=snapshot.metadata
            old=db.execute('SELECT connector_id,owner,collection,snapshot_sha256,status FROM custom_execution_jobs WHERE id=?',(job_id,)).fetchone()
            if old:
                require(old==(data['connector_id'],owner,collection,snapshot.sha256,'admitted'),'Use a new host job identity.')
                return
            self._cleanup_fence(db,data['connector_id'])
            require(db.execute('SELECT count(*)FROM custom_execution_jobs').fetchone()[0]<4096,'Review retained execution history.')
            db.execute('INSERT INTO custom_execution_jobs VALUES(?,?,?,?,?,?,NULL)',
                (job_id,data['connector_id'],owner,collection,snapshot.sha256,'admitted'))

    def _owned_job(self,db,job_id,owner,collection):
        row=db.execute('SELECT connector_id,snapshot_sha256,status,receipt FROM custom_execution_jobs WHERE id=? AND owner=? AND collection=?',
            (job_id,owner,collection)).fetchone()
        require(row is not None,'Choose the owning host job.')
        self._owned(db,row[0],owner,collection)
        return row

    def job_cleanup_state(self,job_id,*,owner,collection):
        with self.store.db()as db:
            if not db.execute('SELECT 1 FROM custom_execution_jobs WHERE id=?',(job_id,)).fetchone():return None
            return self._owned_job(db,job_id,owner,collection)[2]

    def mark_job_uncertain(self,job_id,*,owner,collection):
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');row=self._owned_job(db,job_id,owner,collection)
            require(row[2]in {'admitted','cleanup_uncertain'},'Use the recorded job outcome.')
            db.execute("UPDATE custom_execution_jobs SET status='cleanup_uncertain' WHERE id=?",(job_id,))

    def confirm_job_cleanup(self,job_id,receipt,*,owner,collection):
        """Trusted exact-job recovery; source changes never establish cleanup."""
        exact(receipt,{'snapshot_sha256','cleanup_confirmed','broker_run_id','receipt_sha256'})
        require(receipt['cleanup_confirmed']is True and isinstance(receipt['broker_run_id'],str)and
            re.fullmatch(r'[A-Za-z0-9_-]{16,128}',receipt['broker_run_id'])and
            isinstance(receipt['receipt_sha256'],str)and _HASH.fullmatch(receipt['receipt_sha256']))
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');row=self._owned_job(db,job_id,owner,collection)
            require(row[1]==receipt['snapshot_sha256'],'Use the exact job cleanup receipt.')
            if row[2]=='confirmed':require(row[3]==encoded(receipt).decode(),'Use the recorded job outcome.')
            else:
                require(row[2]in {'admitted','cleanup_uncertain'})
                db.execute("UPDATE custom_execution_jobs SET status='confirmed',receipt=? WHERE id=?",(encoded(receipt).decode(),job_id))

    @staticmethod
    def _cleanup_fence(db,key):
        require(not db.execute("SELECT 1 FROM custom_execution_jobs WHERE connector_id=? AND status!='confirmed' LIMIT 1",(key,)).fetchone(),
            'Confirm the earlier worker cleanup before continuing.')
        require(not db.execute("SELECT 1 FROM custom_execution_tests WHERE connector_id=? AND status='cleanup_uncertain' LIMIT 1",(key,)).fetchone(),
            'Confirm the earlier worker cleanup before continuing.')

    def begin_test(self,key,*,owner,collection,expected_revision):
        require(self.runtime is not None,'Use the commissioned custom executor.')
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');record,_=self._owned(db,key,owner,collection)
            require(record['revision']==expected_revision and type(expected_revision)is int,'Read the current source revision.')
            self._cleanup_fence(db,key)
            require(db.execute('SELECT count(*)FROM custom_execution_tests').fetchone()[0]<MAX_TESTS,'Review retained connector test history.')
            snapshot=self._snapshot(db,key,owner,collection,expected_revision)
            identifier='test-'+secrets.token_hex(16);token=secrets.token_hex(32);created=self.clock()
            db.execute('INSERT INTO custom_execution_tests VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                (identifier,key,owner,collection,hashlib.sha256(token.encode()).hexdigest(),snapshot.document.decode(),'pending',created,created+900))
            return TestLease(identifier,token,snapshot.sha256)

    def _test(self,db,lease,owner,collection):
        require(isinstance(lease,TestLease))
        raw=db.execute('SELECT connector_id,owner,collection,token_hash,snapshot,status,expires,evidence FROM custom_execution_tests WHERE id=?',(lease.id,)).fetchone()
        require(raw is not None and raw[1]==owner and raw[2]==collection and isinstance(lease.token,str)and
            secrets.compare_digest(raw[3],hashlib.sha256(lease.token.encode()).hexdigest()),'Use the owning host test lease.')
        document=json.loads(raw[4]);snapshot=self._snapshot(db,raw[0],owner,collection,document['revision'])
        require(snapshot.document==encoded(document)and snapshot.sha256==lease.snapshot_sha256,'The connector test authority changed.')
        require(self.clock()<raw[6]and raw[5]in {'pending','passed'},'Run a current connector test.')
        self._validate(db,snapshot,owner,collection,draft=True)
        return snapshot,raw[5],raw[7]

    def test_snapshot(self,lease,*,owner,collection):
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');return self._test(db,lease,owner,collection)[0]

    def test_result(self,test_id,*,owner,collection):
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT connector_id,snapshot,status,expires,evidence FROM custom_execution_tests WHERE id=? AND owner=? AND collection=?',(test_id,owner,collection)).fetchone()
            require(row is not None,'Choose a test from this owner and business.')
            self._owned(db,row[0],owner,collection)
            status=row[2]
            if status=='pending'and self.clock()>=row[3]:
                status='expired';db.execute("UPDATE custom_execution_tests SET status='expired' WHERE id=?",(test_id,))
            return {'id':test_id,'connector_id':row[0],'status':status,'snapshot_sha256':hashlib.sha256(row[1].encode()).hexdigest(),
                'revision':json.loads(row[1])['revision'],'expires_at':row[3],
                'evidence':json.loads(row[4])if row[4]else None}

    def fail_test(self,lease,*,owner,collection,reason):
        require(isinstance(lease,TestLease),'Use the owning host test lease.')
        require(reason in {'failed','stopped','runtime_restarted','cleanup_uncertain'},'Use a host test outcome.')
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE')
            raw=db.execute('SELECT connector_id,token_hash,status,snapshot FROM custom_execution_tests WHERE id=? AND owner=? AND collection=?',(lease.id,owner,collection)).fetchone()
            require(raw and isinstance(lease,TestLease)and isinstance(lease.token,str)and
                secrets.compare_digest(raw[1],hashlib.sha256(lease.token.encode()).hexdigest()),'Use the owning host test lease.')
            self._owned(db,raw[0],owner,collection)
            require(hashlib.sha256(raw[3].encode()).hexdigest()==lease.snapshot_sha256,'Use the owning host test lease.')
            # Source/credential revocation and expiry close test authority, but
            # cannot establish that an already admitted worker was removed.
            # Retain its immutable snapshot and let the owning coordinator
            # record failed cleanup even after that authority was invalidated.
            require(raw[2]in {'pending','invalidated','expired',reason},'Use the recorded test outcome.')
            db.execute('UPDATE custom_execution_tests SET status=? WHERE id=?',(reason,lease.id))
            return {'id':lease.id,'status':reason}

    def confirm_test_cleanup(self,lease,receipt,*,owner,collection):
        """Trusted coordinator only, after reconciling that exact broker job."""
        require(isinstance(lease,TestLease))
        exact(receipt,{'snapshot_sha256','cleanup_confirmed','broker_run_id','receipt_sha256'})
        require(receipt['cleanup_confirmed']is True and isinstance(receipt['broker_run_id'],str)and
            re.fullmatch(r'[A-Za-z0-9_-]{16,128}',receipt['broker_run_id'])and
            isinstance(receipt['receipt_sha256'],str)and _HASH.fullmatch(receipt['receipt_sha256']))
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT connector_id,token_hash,snapshot,status,evidence FROM custom_execution_tests WHERE id=? AND owner=? AND collection=?',
                (lease.id,owner,collection)).fetchone()
            require(row and secrets.compare_digest(row[1],hashlib.sha256(lease.token.encode()).hexdigest()),'Use the owning host test lease.')
            self._owned(db,row[0],owner,collection)
            require(hashlib.sha256(row[2].encode()).hexdigest()==lease.snapshot_sha256==receipt['snapshot_sha256'])
            if row[3]=='stopped':require(row[4]is not None and json.loads(row[4])==receipt)
            else:
                require(row[3]=='cleanup_uncertain','Reconcile the recorded uncertain worker.')
                db.execute("UPDATE custom_execution_tests SET status='stopped',evidence=? WHERE id=?",(encoded(receipt).decode(),lease.id))
            return {'id':lease.id,'status':'stopped'}

    def complete_test(self,lease,evidence,*,owner,collection,current=None):
        """Host coordinator only, after verified worker output and actual cleanup."""
        require(self.runtime is not None,'Use the commissioned custom executor.')
        exact(evidence,{'test_id','snapshot_sha256','profile','image_digest','worker_sha256','broker_run_id','cleanup_confirmed','checks'})
        require(len(encoded(evidence))<=65536)
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');snapshot,status,old=self._test(db,lease,owner,collection);data=snapshot.metadata
            require(evidence['test_id']==lease.id and evidence['snapshot_sha256']==snapshot.sha256 and evidence['cleanup_confirmed']is True and
                all(evidence[k]==v for k,v in self.runtime.document().items())and
                isinstance(evidence['broker_run_id'],str)and re.fullmatch(r'[A-Za-z0-9_-]{16,128}',evidence['broker_run_id']),
                'Use matching host runtime and cleanup evidence.')
            checks=evidence['checks'];tests=data['manifest']['tests'];require(isinstance(checks,list)and len(checks)==len(tests))
            for check,test in zip(checks,tests):
                exact(check,{'tool','arguments_sha256','result_sha256','schema_valid','operation_receipts'})
                tool=next(t for t in data['manifest']['tools']if t['name']==test['tool'])
                require(check['tool']==test['tool']and check['arguments_sha256']==digest(test['arguments'])and
                    isinstance(check['result_sha256'],str)and _HASH.fullmatch(check['result_sha256'])and check['schema_valid']is True)
                receipts=check['operation_receipts'];require(isinstance(receipts,dict)and set(receipts)==set(tool['operations'])and
                    all(isinstance(v,str)and _HASH.fullmatch(v)for v in receipts.values()),'Use host receipts for every tested operation.')
            self._cleanup_fence(db,data['connector_id'])
            require(current is None or current(),'The connector test was stopped.')
            if status=='passed':require(json.loads(old)==evidence,'Use the already recorded test outcome.')
            else:db.execute("UPDATE custom_execution_tests SET status='passed',evidence=? WHERE id=?",(encoded(evidence).decode(),lease.id))
            return {'id':lease.id,'status':'passed','snapshot_sha256':snapshot.sha256,'revision':data['revision']}

    def activate(self,test_id,*,owner,collection,expected_active_generation):
        require(self.runtime is not None,'Use the commissioned custom executor.')
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE')
            test=db.execute('SELECT connector_id,snapshot,status,expires,evidence FROM custom_execution_tests WHERE id=? AND owner=? AND collection=?',(test_id,owner,collection)).fetchone()
            require(test and test[2]=='passed'and test[4]and self.clock()<test[3],'Use a current verified host test receipt.')
            data=json.loads(test[1]);snapshot=self._snapshot(db,test[0],owner,collection,data['revision'])
            require(snapshot.document==encoded(data),'The tested authority changed.')
            self._validate(db,snapshot,owner,collection,draft=True);row=authority(db,test[0])
            self._cleanup_fence(db,test[0])
            require(type(expected_active_generation)is int and row['active_generation']==expected_active_generation,'Review the current active connector version.')
            db.execute("UPDATE custom_execution_authority SET active_generation=active_generation+1,active_snapshot=?,active_receipt=?,active_status='active' WHERE connector_id=?",
                (snapshot.document.decode(),test_id,test[0]))
            return {'id':test[0],**public_projection(db,test[0])}

    def revoke(self,key,*,owner,collection):
        """Immediately closes new authority; caller then drains future runtime Stop."""
        with self.store.db()as db:
            db.execute('BEGIN IMMEDIATE');self._owned(db,key,owner,collection);invalidate(db,key,'revoked')
            return {'id':key,**public_projection(db,key),'cleanup_required':True}
