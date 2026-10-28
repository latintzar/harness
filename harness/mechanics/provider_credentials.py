"""Encrypted provider credentials and explicit quiescent legacy migration.

Run migrate_connections on a stopped workspace, off the application event loop.
Historical filesystem snapshots/backups retain their separate protection policy.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from cryptography.fernet import Fernet,InvalidToken
from workspace_identity import workspace_scope
from provider_policy import CATALOGUE,POLICY_REVISION,selected_scopes,scopes

TABLE='connections_v2'
MAX_LEGACY_DATABASE_BYTES=32*1024*1024
# Accepted plaintext can expand in JSON and Fernet. Migration also retains old
# pages until VACUUM; WAL/rollback recovery can retain a second database image.
MAX_ENCRYPTED_DATABASE_BYTES=128*1024*1024
MAX_ENCRYPTED_STORAGE_BYTES=3*MAX_ENCRYPTED_DATABASE_BYTES
MAX_CONNECTIONS=4096
MAX_SECRET_BYTES=64*1024
MAX_ENVELOPE_BYTES=2*MAX_SECRET_BYTES+4096
# Fernet: 57 bytes framing/MAC + PKCS7-padded AES blocks, then base64.
MAX_CIPHERTEXT_BYTES=len('fernet:v1:')+4*((57+16*(MAX_ENVELOPE_BYTES//16+1)+2)//3)
SCHEMA='''CREATE TABLE IF NOT EXISTS connections_v2 (
 principal TEXT NOT NULL,provider TEXT NOT NULL,generation INTEGER NOT NULL,
 status TEXT NOT NULL,tokens TEXT,client_info TEXT,expires_at REAL,tools TEXT NOT NULL DEFAULT '[]',updated_at TEXT NOT NULL,
 access_mode TEXT NOT NULL,requested_scopes TEXT NOT NULL,oauth_scopes TEXT NOT NULL,
 policy_revision TEXT NOT NULL,inventory_sha256 TEXT,
 PRIMARY KEY(principal,provider));
 CREATE TABLE IF NOT EXISTS credential_format (id INTEGER PRIMARY KEY CHECK(id=1),company_scope TEXT NOT NULL,phase TEXT NOT NULL);
'''

class CredentialError(ValueError):pass


def private_file(fd):
    s=os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or s.st_mode&0o077:
        raise CredentialError('Use the private provider credential store.')
    return s


def directory(state):
    state=Path(state).resolve();workspace_scope(state)
    path=state/'mcp-connections'
    if path.is_symlink():raise CredentialError('Use the private provider credential store.')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.stat()
    if info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise CredentialError('Use the private provider credential store.')
    return path


@contextmanager
def locked(root,*,exclusive=False):
    fd=os.open(root/'.credentials.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        private_file(fd);fcntl.flock(fd,fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:os.close(fd)


class Vault:
    def __init__(self,root,company):self.root=Path(root);self.company=company;self.path=self.root/'credentials.key'
    def key(self,*,create=False):
        if create and not self.path.exists():
            fd,name=tempfile.mkstemp(prefix='.provider-key-',dir=self.root)
            try:
                with os.fdopen(fd,'wb')as stream:stream.write(Fernet.generate_key());stream.flush();os.fsync(stream.fileno())
                try:os.link(name,self.path)
                except FileExistsError:pass
            finally:Path(name).unlink(missing_ok=True)
            d=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(d)
            finally:os.close(d)
        try:
            fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                if private_file(fd).st_size!=44:raise CredentialError('Restore the provider credential key.')
                key=os.read(fd,45)
            finally:os.close(fd)
            return Fernet(key)
        except (OSError,ValueError):raise CredentialError('Restore the private provider credential key.') from None
    def binding(self,principal,provider,generation,field):
        if field not in {'tokens','client_info'} or type(generation)is not int or generation<1:
            raise CredentialError('Use the current provider credential identity.')
        return [1,self.company,principal,provider,generation,field]
    def seal(self,value,*,principal,provider,generation,field):
        if value is None:return None
        if not isinstance(value,str)or len(value.encode())>MAX_SECRET_BYTES:
            raise CredentialError('Use a bounded provider credential.')
        envelope={'binding':self.binding(principal,provider,generation,field),'value':value}
        encoded=json.dumps(envelope,separators=(',',':'),ensure_ascii=False).encode()
        if len(encoded)>MAX_ENVELOPE_BYTES:raise CredentialError('Use a bounded encoded provider credential.')
        return 'fernet:v1:'+self.key().encrypt(encoded).decode()
    def open(self,value,*,principal,provider,generation,field):
        if value is None:return None
        try:
            if not isinstance(value,str)or not value.startswith('fernet:v1:')or len(value)>MAX_CIPHERTEXT_BYTES:raise ValueError
            encoded=self.key().decrypt(value[len('fernet:v1:'):].encode())
            if len(encoded)>MAX_ENVELOPE_BYTES:raise ValueError
            envelope=json.loads(encoded)
            if envelope['binding']!=self.binding(principal,provider,generation,field)or not isinstance(envelope['value'],str)or len(envelope['value'].encode())>MAX_SECRET_BYTES:raise ValueError
            return envelope['value']
        except (InvalidToken,KeyError,ValueError,TypeError):raise CredentialError('Restore the provider credential bound to this account.')from None


def _bounded_database(path,*,legacy=False):
    main_limit=MAX_LEGACY_DATABASE_BYTES if legacy else MAX_ENCRYPTED_DATABASE_BYTES
    total_limit=MAX_LEGACY_DATABASE_BYTES if legacy else MAX_ENCRYPTED_STORAGE_BYTES
    total=0
    for suffix in ('','-wal','-shm','-journal'):
        try:fd=os.open(str(path)+suffix,os.O_RDONLY|os.O_NOFOLLOW)
        except FileNotFoundError:continue
        try:
            size=private_file(fd).st_size
            if suffix=='' and size>main_limit:raise CredentialError('Review the bounded provider credential store.')
            total+=size
        finally:os.close(fd)
    if total>total_limit:raise CredentialError('Review the bounded provider credential store.')


def configure_database(db):
    """SQLite's pager limit belongs to this connection, not to the DB file."""
    page_size=db.execute('PRAGMA page_size').fetchone()[0]
    limit=MAX_ENCRYPTED_DATABASE_BYTES//page_size
    if limit<1 or db.execute('PRAGMA page_count').fetchone()[0]>limit:
        raise CredentialError('Review the bounded provider credential store.')
    actual=db.execute('PRAGMA max_page_count='+str(limit)).fetchone()[0]
    if actual>limit:raise CredentialError('Review the bounded provider credential store.')


def _tables(db):return {r[0]for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _format(db,company):
    tables=_tables(db)
    if 'credential_format'not in tables:return False
    row=db.execute('SELECT company_scope,phase FROM credential_format WHERE id=1').fetchone()
    if not row or row[0]!=company:raise CredentialError('Open the provider store for its owning company.')
    if row[1]!='ready':raise CredentialError('Finish the private provider credential migration.')
    if 'connections'in tables and db.execute('SELECT 1 FROM connections WHERE tokens IS NOT NULL OR client_info IS NOT NULL LIMIT 1').fetchone():
        raise CredentialError('Reconcile credentials created by the earlier runtime.')
    return True


def initialize(root,company):
    """Fast fresh initialization; historical migration is an explicit host operation."""
    root=Path(root);path=root/'connections.sqlite';vault=Vault(root,company)
    with locked(root,exclusive=True):
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            if private_file(fd).st_size>MAX_ENCRYPTED_DATABASE_BYTES:raise CredentialError('Review the bounded provider credential store.')
        finally:os.close(fd)
        _bounded_database(path)
        with sqlite3.connect(path,timeout=5)as db:
            configure_database(db)
            if _format(db,company):vault.key();return vault
            _bounded_database(path,legacy=True)
            if 'connections'in _tables(db) and db.execute('SELECT 1 FROM connections LIMIT 1').fetchone():
                raise CredentialError('Migrate the private provider credentials before starting this workspace.')
            vault.key(create=True);db.executescript(SCHEMA)
            db.execute("INSERT INTO credential_format VALUES(1,?,'ready')",(company,))
    return vault


def migrate_connections(state):
    """Explicit, synchronous migration; stop all old/new users of this DB first.

    secure_delete + checkpoint/truncate + VACUUM scrub SQLite logical residue.
    Copies/snapshots and lower-level storage recovery need their own protection.
    A cleanup_pending receipt prevents startup after an interrupted scrub.
    """
    root=directory(state);company=workspace_scope(state);path=root/'connections.sqlite';vault=Vault(root,company)
    with locked(root,exclusive=True):
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
        try:
            if private_file(fd).st_size>MAX_ENCRYPTED_DATABASE_BYTES:raise CredentialError('Review the bounded provider credential store.')
        finally:os.close(fd)
        _bounded_database(path)
        db=sqlite3.connect(path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            fmt=db.execute('SELECT * FROM credential_format WHERE id=1').fetchone() if 'credential_format'in _tables(db) else None
            if fmt and fmt['company_scope']!=company:raise CredentialError('Open the provider store for its owning company.')
            if fmt and fmt['phase']not in {'ready','cleanup_pending'}:raise CredentialError('Review the provider credential migration phase.')
            if not fmt:_bounded_database(path,legacy=True)
            configure_database(db)
            db.execute('PRAGMA secure_delete=ON')
            db.executescript(SCHEMA)
            if fmt and fmt['phase']=='ready':_format(db,company);vault.key();return {'status':'ready','migrated':0}
            if not fmt:
                vault.key(create=True);db.execute('BEGIN IMMEDIATE')
                if 'connections'in _tables(db) and db.execute('SELECT 1 FROM connections WHERE length(CAST(tokens AS BLOB))>? OR length(CAST(client_info AS BLOB))>? LIMIT 1',(MAX_SECRET_BYTES,MAX_SECRET_BYTES)).fetchone():
                    raise CredentialError('Review the bounded legacy provider credential.')
                rows=db.execute('SELECT * FROM connections LIMIT ?',(MAX_CONNECTIONS+1,)).fetchall()if 'connections'in _tables(db)else[]
                if len(rows)>MAX_CONNECTIONS:raise CredentialError('Review the bounded provider credential store.')
                for row in rows:
                    config=CATALOGUE.get(row['provider'])
                    if config is None and(row['tokens']or row['client_info']):raise CredentialError('Review the legacy provider before migrating its credentials.')
                    if config is None:continue
                    generation=row['generation']+1;requested=selected_scopes(config,'read');granted=()
                    token=row['tokens'];client=row['client_info']
                    if token:
                        from mcp.shared.auth import OAuthToken
                        value=OAuthToken.model_validate_json(token);granted=scopes(value.scope)if value.scope is not None else requested
                        if not set(granted)<=set(requested):raise CredentialError('Review the legacy provider consent before migration.')
                        token=value.model_copy(update={'scope':' '.join(granted)or None}).model_dump_json()
                    binding=dict(principal=row['principal'],provider=row['provider'],generation=generation)
                    db.execute('INSERT INTO connections_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (row['principal'],row['provider'],generation,row['status'],
                         vault.seal(token,field='tokens',**binding),vault.seal(client,field='client_info',**binding),
                         row['expires_at'],row['tools']if'tools'in row.keys()else'[]',row['updated_at'],'read',
                         json.dumps(requested),json.dumps(granted),POLICY_REVISION,None))
                if 'connections'in _tables(db):
                    db.execute("UPDATE connections SET tokens=NULL,client_info=NULL,status='reconnect_required',generation=generation+1")
                if 'flows'in _tables(db):
                    db.execute("UPDATE flows SET status='interrupted' WHERE status IN ('connecting','awaiting_authorization','completing')")
                db.execute("INSERT INTO credential_format VALUES(1,?,'cleanup_pending')",(company,));db.commit()
                count=len(rows)
            else:vault.key();count=0
            # Old binaries cannot use migrated credentials. Clear every SQLite
            # recovery/free-page location before marking the new store usable.
            checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint and checkpoint[0]:raise CredentialError('Stop every provider store reader before completing migration.')
            db.execute('VACUUM')
            _bounded_database(path)
            db.execute("UPDATE credential_format SET phase='ready' WHERE id=1");db.commit()
            checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint and checkpoint[0]:raise CredentialError('Finish the provider store checkpoint.')
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
            return {'status':'ready','migrated':count}
        except (sqlite3.Error,ValueError,TypeError,KeyError):
            db.rollback();raise CredentialError('Finish the private provider credential migration with the runtime stopped.')from None
        finally:db.close()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Migrate provider credentials with all workspace consumers stopped.')
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--migrate',action='store_true',required=True)
    args=parser.parse_args()
    try:print(json.dumps(migrate_connections(args.state),sort_keys=True))
    except (CredentialError,OSError,sqlite3.Error):parser.exit(1,'Provider migration requires the complete private state and stopped runtime.\n')
