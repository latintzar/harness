"""Owner grants for the fixed SFTP acquisition driver.

The chat proposes an immutable destination. Its owner approves it; a host worker
then takes a generation-bound snapshot. Credentials stay in CustomConnectors'
encrypted store. This module neither launches a worker nor admits indexing spend.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import secrets
import sqlite3
import time

from custom_execution import authority, digest, encoded, require
from source_intake import IntakeActor
from workspace_identity import workspace_scope


def initialize_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS storage_connection_proposals(
            id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, owner TEXT NOT NULL,
            collection TEXT NOT NULL, document TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS storage_proposal_connector
            ON storage_connection_proposals(connector_id,created);
        CREATE TABLE IF NOT EXISTS storage_connection_grants(
            connector_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
            revision INTEGER NOT NULL, document TEXT NOT NULL, status TEXT NOT NULL);
    ''')


def public_values(db, key):
    return [json.loads(row[0]) for row in db.execute(
        'SELECT document FROM storage_connection_proposals WHERE connector_id=?', (key,))]


def _grant(db, key):
    row = db.execute('SELECT generation,revision,document,status FROM storage_connection_grants WHERE connector_id=?',
        (key,)).fetchone()
    return dict(zip(('generation','revision','document','status'),row)) if row else None


def validate_target(target, declared):
    from sftp_transport import SFTPConfig
    fields = {'host','port','root','host_key','mode','stable_seconds','poll_seconds','authentication'}
    require(isinstance(target,dict) and set(target)==fields, 'Review the SFTP folder configuration.')
    auth = target['authentication']
    require(isinstance(auth,dict) and set(auth) in (
        {'username_field','password_field'}, {'username_field','private_key_field'},
        {'username_field','private_key_field','passphrase_field'}), 'Choose the SFTP login fields.')
    require(all(isinstance(value,str) and value in declared for value in auth.values())
        and len(set(auth.values()))==len(auth), 'Declare each SFTP login field in the private form.')
    config = SFTPConfig(company_id='validation',owner_id='validation',collection='validation',
        source_id='validation',generation=1,**{k:v for k,v in target.items() if k!='authentication'})
    host = config.host.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        require('.' in host and not host.endswith(('.localhost','.local','.internal','.test','.invalid')),
            'Use the public SFTP hostname assigned to this server.')
    else:
        underlying = (address.ipv4_mapped or address) if isinstance(address,ipaddress.IPv6Address) else address
        require(underlying.is_global and not underlying.is_multicast,
            'Use the commissioned public SFTP endpoint.')
    import asyncssh
    host_key = asyncssh.import_public_key(config.host_key).export_public_key().decode().strip()
    return {**target,'host':host,'host_key':host_key,'authentication':dict(auth)}


@dataclass(frozen=True)
class StorageSnapshot:
    document: bytes

    @property
    def metadata(self):
        return json.loads(self.document)

    @property
    def config(self):
        from sftp_transport import SFTPConfig
        return SFTPConfig(**self.metadata['config'])

    @property
    def sha256(self):
        return digest(self.metadata)


class StorageConnections:
    def __init__(self, store, sources):
        self.store, self.sources = store, sources
        store.register_extension("retail_storage", initialize=initialize_schema, public_values=public_values)
        require(store.root.parent.resolve()==sources.root.parent.resolve(), 'Use the owning company storage.')
        self.company = workspace_scope(store.root.parent)

    def _owned(self, db, key, owner, collection):
        record,package = self.store._row(db,key,owner)
        require(record['collection']==collection and record['kind']=='sftp',
            'Choose a file-transfer connector in this business.')
        return record,package

    def propose(self, key, target, reason, *, expected_revision, owner, collection):
        require(isinstance(reason,str) and 1<=len(reason.strip())<=1000, 'Explain this folder connection.')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record,package = self._owned(db,key,owner,collection)
            require(type(expected_revision) is int and record['revision']==expected_revision and package
                and 'execution' not in package, 'Prepare the current SFTP setup and its private login fields.')
            value = validate_target(target,{f['name'] for f in record['secret_fields']})
            grant = _grant(db,key)
            document = {'revision':expected_revision,'generation':grant['generation'] if grant else 0,
                'target':value,'reason':reason.strip()}
            self.store._public_text(db,key,document)
            serialized = encoded(document).decode()
            existing = db.execute('SELECT id FROM storage_connection_proposals WHERE connector_id=? AND document=?',
                (key,serialized)).fetchone()
            if existing: return {'id':existing[0],**document,'status':'proposed'}
            count = db.execute('SELECT count(*) FROM storage_connection_proposals WHERE connector_id=?',(key,)).fetchone()[0]
            total = db.execute('SELECT coalesce(sum(length(CAST(document AS BLOB))),0) FROM storage_connection_proposals').fetchone()[0]
            require(count<32 and total+len(serialized.encode())<=16*1024*1024, 'Review retained folder proposals.')
            identifier = 'storage-target-'+secrets.token_hex(16)
            db.execute('INSERT INTO storage_connection_proposals VALUES(?,?,?,?,?,?)',
                (identifier,key,owner,collection,serialized,time.time()))
            return {'id':identifier,**document,'status':'proposed'}

    def approve(self, key, proposal_id, *, owner, collection):
        # Same lock order as intake's commit guard: source -> connector authority.
        with self.sources._write(), self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            record,_ = self._owned(db,key,owner,collection)
            row = db.execute('SELECT document FROM storage_connection_proposals WHERE id=? AND connector_id=? AND owner=? AND collection=?',
                (proposal_id,key,owner,collection)).fetchone()
            require(row is not None, 'Review this connector’s folder proposal.')
            proposal = json.loads(row[0]);grant = _grant(db,key)
            require(proposal['revision']==record['revision'], 'Review the current folder setup.')
            target = validate_target(proposal['target'],{f['name'] for f in record['secret_fields']})
            generation = grant['generation'] if grant else 0
            if grant and grant['status']=='configured' and grant['revision']==record['revision'] and json.loads(grant['document'])==target:
                return {'generation':generation,'target':target,'status':'configured'}
            require(proposal['generation']==generation, 'Review the current folder access proposal.')
            generation += 1
            db.execute('INSERT OR REPLACE INTO storage_connection_grants VALUES(?,?,?,?,?)',
                (key,generation,record['revision'],encoded(target).decode(),'configured'))
            return {'generation':generation,'target':target,'status':'configured'}

    def pause(self, key, *, expected_generation, owner, collection):
        with self.sources._write(), self.store.db() as db:
            db.execute('BEGIN IMMEDIATE');self._owned(db,key,owner,collection)
            grant = _grant(db,key)
            require(grant and type(expected_generation) is int and grant['generation']==expected_generation,
                'Read the current folder connection before pausing.')
            db.execute("UPDATE storage_connection_grants SET generation=generation+1,status='paused' WHERE connector_id=?",(key,))
            return {'generation':grant['generation']+1,'status':'paused'}

    def describe(self, key, *, owner, collection):
        with self.store.db() as db:
            record,_ = self._owned(db,key,owner,collection);grant = _grant(db,key)
            latest = db.execute('SELECT id,document FROM storage_connection_proposals WHERE connector_id=? ORDER BY created DESC,id DESC LIMIT 1',(key,)).fetchone()
            generation = grant['generation'] if grant else 0
            proposal = None
            if latest:
                value = json.loads(latest[1])
                proposal = {'id':latest[0],**value,'current':value['revision']==record['revision'] and value['generation']==generation}
            status = grant['status'] if grant else 'setup'
            if grant and grant['revision']!=record['revision']: status='review_required'
            return {'generation':generation,'status':status,'proposal':proposal,
                'target':json.loads(grant['document']) if grant else None,
                'source_type':'incremental_folder' if grant and json.loads(grant['document'])['mode']=='watch' else 'stored_recordings'}

    def _binding(self, db, key, owner, collection):
        record,package = self._owned(db,key,owner,collection);grant = _grant(db,key)
        require(grant and grant['status']=='configured' and grant['revision']==record['revision']
            and package and 'execution' not in package, 'Approve the current folder setup before importing.')
        target = validate_target(json.loads(grant['document']),{f['name'] for f in record['secret_fields']})
        configured = {row[0] for row in db.execute('SELECT name FROM credentials WHERE connector_id=?',(key,))}
        require(set(target['authentication'].values())<=configured, 'Complete this connector’s private login form.')
        row = authority(db,key)
        return {'connector_id':key,'company_id':self.company,'owner_id':owner,'collection':collection,
            'revision':record['revision'],'generation':grant['generation'],
            'credential_generation':row['credential_generation'],'target':target}

    def snapshot(self, key, *, owner, collection):
        from dataclasses import asdict
        from sftp_transport import SFTPConfig
        with self.store.db() as db:
            binding = self._binding(db,key,owner,collection)
            record,_ = self._owned(db,key,owner,collection)
        target = binding['target']
        target_sha = digest({k:target[k] for k in ('host','port','root','host_key')})
        source = self.sources.ensure_connection_source(IntakeActor(owner),collection=collection,
            connection_id=key,target_sha256=target_sha,name=record['name'],kind='sftp')
        config = SFTPConfig(company_id=self.company,owner_id=owner,collection=collection,
            source_id=source['id'],generation=binding['generation'],**{k:v for k,v in target.items() if k!='authentication'})
        snapshot = StorageSnapshot(encoded({**binding,'config':asdict(config)}))
        require(self.current(snapshot), 'Read the current folder connection before importing.')
        return snapshot

    def _current(self, db, snapshot):
        require(isinstance(snapshot,StorageSnapshot), 'Use a host-owned folder snapshot.')
        value = snapshot.metadata
        require(value['company_id']==self.company, 'Use this company’s folder snapshot.')
        binding = self._binding(db,value['connector_id'],value['owner_id'],value['collection'])
        require({k:v for k,v in value.items() if k!='config'}==binding, 'The folder connection has changed.')
        config = snapshot.config
        require(config.company_id==self.company and config.owner_id==value['owner_id']
            and config.collection==value['collection'] and config.generation==value['generation']
            and {k:getattr(config,k) for k in value['target'] if k!='authentication'}==
                {k:v for k,v in value['target'].items() if k!='authentication'}, 'Use the exact folder snapshot.')
        # The read owns no write lock, so this is safe inside SourceStore's commit guard.
        source = self.sources.lookup_connection_source(IntakeActor(value['owner_id']),
            collection=value['collection'],connection_id=value['connector_id'],target_sha256=config.target_sha256)
        require(source and source['id']==config.source_id and source['kind']=='sftp', 'Use the owning source.')
        return value

    def current(self, snapshot):
        """Read-only, zero-busy probe for the transport's event-loop guard.

        Full configuration and credential validation happens in the drained host
        snapshot/credential calls. This probe performs bounded scalar reads and
        fails closed on lock contention; it never runs schema setup or a write.
        """
        connections = []
        try:
            if not isinstance(snapshot,StorageSnapshot) or len(snapshot.document)>32768:return False
            value=snapshot.metadata
            if value['company_id']!=self.company:return False
            config=value['config'];target=value['target']
            if any(type(value[k]) is not int for k in ('revision','generation','credential_generation')):return False
            expected={k:v for k,v in target.items() if k!='authentication'}|{
                'company_id':self.company,'owner_id':value['owner_id'],'collection':value['collection'],
                'generation':value['generation'],'source_id':config['source_id']}
            if config!=expected:return False
            db=sqlite3.connect(self.store.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
            connections.append(db)
            row=db.execute('''SELECT c.record,g.generation,g.revision,g.document,g.status,a.credential_generation
                FROM connectors c JOIN storage_connection_grants g ON g.connector_id=c.id
                JOIN custom_execution_authority a ON a.connector_id=c.id
                WHERE c.id=? AND c.owner=? AND c.collection=?''',
                (value['connector_id'],value['owner_id'],value['collection'])).fetchone()
            if row is None:return False
            record=json.loads(row[0])
            if (record['kind']!='sftp' or record['revision']!=value['revision'] or
                    row[1:]!=(value['generation'],value['revision'],encoded(target).decode(),
                        'configured',value['credential_generation'])):return False
            source=sqlite3.connect(self.sources.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
            connections.append(source)
            target_sha=digest({k:target[k] for k in ('host','port','root','host_key')})
            match=source.execute('''SELECT s.id FROM connection_sources b JOIN sources s ON s.id=b.source_id
                WHERE b.owner=? AND b.collection=? AND b.connection_id=? AND b.target_sha256=?
                AND s.owner=b.owner AND s.collection=b.collection AND s.kind='sftp' AND s.id=?''',
                (value['owner_id'],value['collection'],value['connector_id'],target_sha,config['source_id'])).fetchone()
            if match is None:return False
            return True
        except (ValueError,KeyError,TypeError,OSError,sqlite3.Error):
            return False
        finally:
            for connection in connections:connection.close()

    def credentials(self, snapshot):
        from sftp_transport import SFTPCredentials
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            value = self._current(db,snapshot)
            fields = value['target']['authentication'];cipher = self.store._cipher(db)
            result = {}
            for field,name in fields.items():
                row = db.execute('SELECT ciphertext FROM credentials WHERE connector_id=? AND name=?',
                    (value['connector_id'],name)).fetchone()
                require(row is not None, 'Complete the current private login form.')
                result[field.removesuffix('_field')] = cipher.decrypt(row[0]).decode()
            return SFTPCredentials(config_sha256=snapshot.config.sha256,**result)
