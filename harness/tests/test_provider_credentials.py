"""Real SQLite/file migration and ciphertext bindings; no account credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest
from mcp.shared.auth import OAuthToken,OAuthClientInformationFull
sys.path.insert(0,str(Path(__file__).parents[1]))
from mcp_connections import MCPConnections,ConnectionError,_Storage
from provider_credentials import CredentialError,Vault,migrate_connections
from workspace_identity import workspace_scope


def legacy(state,*,wal=False,scope='default'):
    state.mkdir(exist_ok=True);workspace_scope(state)
    directory=state/'mcp-connections';directory.mkdir(mode=0o700)
    path=directory/'connections.sqlite'
    connection=sqlite3.connect(path);path.chmod(0o600)
    if wal:connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript('''CREATE TABLE connections (principal TEXT,provider TEXT,generation INTEGER,status TEXT,tokens TEXT,client_info TEXT,expires_at REAL,tools TEXT,updated_at TEXT,PRIMARY KEY(principal,provider));
     CREATE TABLE flows (id TEXT,principal TEXT,provider TEXT,binding TEXT,status TEXT,expires_at REAL,created_at TEXT,updated_at TEXT);''')
    tokens=OAuthToken(access_token='legacy-private-access',refresh_token='legacy-private-refresh',scope=scope,expires_in=600).model_dump_json()
    client=OAuthClientInformationFull(redirect_uris=['https://rocoto.example/api/connections/oauth/callback'],client_id='public-client',client_secret='legacy-private-client',token_endpoint_auth_method='none').model_dump_json()
    connection.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,?,?,?)',('alice','notion',4,'connected',tokens,client,None,'["notion-fetch"]','stamp'))
    connection.execute('INSERT INTO flows VALUES(?,?,?,?,?,?,?,?)',('flow','alice','notion','binding','awaiting_authorization',1,'stamp','stamp'))
    connection.commit()
    return path,connection


@pytest.mark.parametrize('wal',[False,True])
def test_explicit_migration_scrubs_logical_sqlite_plaintext_and_retains_authority(tmp_path,wal):
    path,connection=legacy(tmp_path,wal=wal)
    connection.close()
    with pytest.raises(ConnectionError,match='Migrate'):
        MCPConnections(tmp_path,public_origin='https://rocoto.example')
    result=migrate_connections(tmp_path)
    assert result=={'status':'ready','migrated':1}
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    record=broker._record('notion','alice')
    assert record['generation']==5 and record['access_mode']=='read'
    assert json.loads(record['tokens'])['access_token']=='legacy-private-access'
    with sqlite3.connect(path)as db:
        assert db.execute('SELECT status,tokens,client_info FROM connections').fetchone()==('reconnect_required',None,None)
        assert db.execute('SELECT status FROM flows').fetchone()==('interrupted',)
    for p in path.parent.iterdir():
        if p.is_file():assert b'legacy-private-'not in p.read_bytes()
    assert migrate_connections(tmp_path)['migrated']==0


def test_migration_transaction_rolls_back_unsupported_scope(tmp_path):
    path,connection=legacy(tmp_path,scope='default elevated');connection.close()
    with pytest.raises(CredentialError):migrate_connections(tmp_path)
    with sqlite3.connect(path)as db:
        assert db.execute('SELECT tokens FROM connections').fetchone()[0]
        assert db.execute('SELECT count(*)FROM connections_v2').fetchone()[0]==0
    with pytest.raises(ConnectionError):MCPConnections(tmp_path,public_origin='https://rocoto.example')


def test_cleanup_crash_blocks_start_and_is_recoverable(tmp_path,monkeypatch):
    path,connection=legacy(tmp_path);connection.close()
    real=sqlite3.connect
    class Wrapper:
        def __init__(self,db):object.__setattr__(self,'db',db)
        def __getattr__(self,name):return getattr(self.db,name)
        def __setattr__(self,name,value):setattr(self.db,name,value)
        def execute(self,query,*args):
            if query=='VACUUM':raise sqlite3.OperationalError('fixture crash')
            return self.db.execute(query,*args)
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:Wrapper(real(*a,**kw)))
    with pytest.raises(CredentialError):migrate_connections(tmp_path)
    monkeypatch.setattr(sqlite3,'connect',real)
    with real(path)as db:
        assert db.execute('SELECT phase FROM credential_format').fetchone()[0]=='cleanup_pending'
        assert db.execute('SELECT tokens FROM connections').fetchone()[0]is None
    with pytest.raises(ConnectionError,match='Finish'):MCPConnections(tmp_path,public_origin='https://rocoto.example')
    assert migrate_connections(tmp_path)['status']=='ready'


def test_rollback_new_plaintext_is_rejected_instead_of_silent_import(tmp_path):
    path,connection=legacy(tmp_path);connection.close();migrate_connections(tmp_path)
    with sqlite3.connect(path)as db:db.execute("UPDATE connections SET tokens='old-runtime-new-private-token'")
    with pytest.raises(ConnectionError,match='Reconcile'):MCPConnections(tmp_path,public_origin='https://rocoto.example')
    with pytest.raises(CredentialError):migrate_connections(tmp_path)


@pytest.mark.parametrize('change',['owner','provider','generation','field','company'])
def test_ciphertext_cannot_move_between_authorities(tmp_path,change):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        generation=broker._begin('notion','alice')
        storage=_Storage(broker,'alice','notion',generation)
        await storage.set_tokens(OAuthToken(access_token='bound-private',scope='default'))
        with sqlite3.connect(broker.database)as db:sealed=db.execute('SELECT tokens FROM connections_v2').fetchone()[0]
        binding=dict(principal='alice',provider='notion',generation=generation,field='tokens')
        vault=broker._vault
        if change=='owner':binding['principal']='bob'
        elif change=='provider':binding['provider']='linear'
        elif change=='generation':binding['generation']+=1
        elif change=='field':binding['field']='client_info'
        else:vault=Vault(broker.directory,'a'*64)
        with pytest.raises(CredentialError):vault.open(sealed,**binding)
    asyncio.run(scenario())


def test_missing_key_never_regenerates_over_ciphertext(tmp_path):
    async def scenario():
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        generation=broker._begin('notion','alice')
        await _Storage(broker,'alice','notion',generation).set_tokens(OAuthToken(access_token='private',scope='default'))
        broker._vault.path.unlink()
        with pytest.raises(ConnectionError):MCPConnections(tmp_path,public_origin='https://rocoto.example')
        assert not broker._vault.path.exists()
    asyncio.run(scenario())


def test_concurrent_key_publication_retains_one_complete_key(tmp_path):
    def create(_):
        broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
        return broker._vault.path.read_bytes()
    with ThreadPoolExecutor(max_workers=8)as pool:keys=list(pool.map(create,range(16)))
    assert len(set(keys))==1 and len(keys[0])==44
    assert not list((tmp_path/'mcp-connections').glob('.provider-key-*'))


@pytest.mark.parametrize('target',['key','lock','database'])
def test_symlink_and_private_mode_boundaries(tmp_path,target):
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    path={'key':broker._vault.path,'lock':broker.directory/'.credentials.lock','database':broker.database}[target]
    external=tmp_path/'outside';external.write_bytes(path.read_bytes());path.unlink();path.symlink_to(external)
    with pytest.raises((ConnectionError,CredentialError,OSError)):
        MCPConnections(tmp_path,public_origin='https://rocoto.example')


@pytest.mark.parametrize('kind',['backslashes','emoji','oauth_json'])
def test_near_limit_credentials_round_trip_after_json_and_fernet_expansion(tmp_path,kind):
    from provider_credentials import MAX_SECRET_BYTES
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example');vault=broker._vault
    if kind=='backslashes':value='\\' * MAX_SECRET_BYTES
    elif kind=='emoji':value='🔐'*(MAX_SECRET_BYTES//4)
    else:value=json.dumps({'access_token':'x'*(MAX_SECRET_BYTES-50)})
    assert len(value.encode())<=MAX_SECRET_BYTES
    binding=dict(principal='alice',provider='notion',generation=1,field='tokens')
    sealed=vault.seal(value,**binding)
    assert vault.open(sealed,**binding)==value


def test_migration_bounds_legacy_values_before_loading_credentials(tmp_path):
    from provider_credentials import MAX_SECRET_BYTES
    path,connection=legacy(tmp_path)
    connection.execute('UPDATE connections SET tokens=?',('x'*(MAX_SECRET_BYTES+1),));connection.commit();connection.close()
    with pytest.raises(CredentialError):migrate_connections(tmp_path)
    with sqlite3.connect(path)as db:
        assert db.execute('SELECT length(tokens)FROM connections').fetchone()[0]==MAX_SECRET_BYTES+1


def test_migration_counts_wal_bytes_in_database_bound(tmp_path,monkeypatch):
    import provider_credentials
    path,connection=legacy(tmp_path,wal=True)
    try:
        total=sum(p.stat().st_size for p in path.parent.iterdir()if p.name.startswith('connections.sqlite'))
        monkeypatch.setattr(provider_credentials,'MAX_LEGACY_DATABASE_BYTES',max(path.stat().st_size,total-1))
        with pytest.raises(CredentialError):migrate_connections(tmp_path)
    finally:connection.close()


@pytest.mark.parametrize('interrupted',[False,True])
def test_large_valid_legacy_migration_reopens_after_ciphertext_expansion(tmp_path,monkeypatch,interrupted):
    import provider_credentials as credentials
    path,db=legacy(tmp_path,wal=interrupted)
    token=OAuthToken(access_token='x'*(64*1024-200),scope='default').model_dump_json()
    assert len(token.encode())<=credentials.MAX_SECRET_BYTES
    db.execute('DELETE FROM connections')
    db.executemany('INSERT INTO connections VALUES(?,?,?,?,?,?,?,?,?)',
        [('owner-'+str(n),'notion',4,'connected',token,None,None,'[]','stamp')for n in range(410)])
    db.commit();db.close()
    assert path.stat().st_size<32*1024*1024
    real=sqlite3.connect
    if interrupted:
        class CrashBeforeVacuum:
            def __init__(self,db):object.__setattr__(self,'db',db)
            def __getattr__(self,name):return getattr(self.db,name)
            def __setattr__(self,name,value):setattr(self.db,name,value)
            def execute(self,query,*args):
                if query=='VACUUM':raise sqlite3.OperationalError('synthetic cleanup crash')
                return self.db.execute(query,*args)
        monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:CrashBeforeVacuum(real(*a,**kw)))
        with pytest.raises(CredentialError):migrate_connections(tmp_path)
        monkeypatch.setattr(sqlite3,'connect',real)
        assert path.stat().st_size>32*1024*1024
        with real(path)as db:assert db.execute('SELECT phase FROM credential_format').fetchone()[0]=='cleanup_pending'
        with pytest.raises(ConnectionError,match='Finish'):MCPConnections(tmp_path,public_origin='https://rocoto.example')
    assert migrate_connections(tmp_path)=={'status':'ready','migrated':0 if interrupted else 410}
    assert path.stat().st_size>32*1024*1024
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    assert json.loads(broker._record('notion','owner-409')['tokens'])['access_token']==json.loads(token)['access_token']
    assert migrate_connections(tmp_path)=={'status':'ready','migrated':0}


def test_encrypted_store_recovery_sidecars_remain_bounded(tmp_path,monkeypatch):
    import provider_credentials as credentials
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    journal=Path(str(broker.database)+'-journal');journal.write_bytes(b'0'*8192);journal.chmod(0o600)
    monkeypatch.setattr(credentials,'MAX_ENCRYPTED_STORAGE_BYTES',broker.database.stat().st_size+8191)
    with pytest.raises(ConnectionError,match='bounded'):MCPConnections(tmp_path,public_origin='https://rocoto.example')
    with pytest.raises(CredentialError):migrate_connections(tmp_path)


def test_live_connection_growth_enforces_encrypted_cap_and_rolls_back(tmp_path,monkeypatch):
    import provider_credentials as credentials
    broker=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    generation=broker._begin('notion','alice')
    # Set the limit after construction: every short-lived DB connection must
    # enforce it, including a reopened instance, before its actual write.
    ceiling=broker.database.stat().st_size+4096
    monkeypatch.setattr(credentials,'MAX_ENCRYPTED_DATABASE_BYTES',ceiling)
    private=OAuthToken(access_token='synthetic-private-'+'x'*60000,scope='default').model_dump_json()
    with pytest.raises(ConnectionError,match='bounded provider credential store')as error:
        broker._update('notion','alice',generation,tokens=private)
    assert 'synthetic-private-'not in str(error.value)
    assert broker.database.stat().st_size<=ceiling
    assert broker._record('notion','alice')['tokens']is None
    reopened=MCPConnections(tmp_path,public_origin='https://rocoto.example')
    with pytest.raises(ConnectionError,match='bounded provider credential store'):
        reopened._update('notion','alice',generation,client_info=private)
    assert reopened._record('notion','alice')['client_info']is None
