"""A saved chat proposal cannot authorize network access or choose another owner."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from custom_connectors import CustomConnectors
from source_intake import IntakeActor, SourceStore
from storage_connections import StorageConnections


def fixture(tmp_path):
    import asyncssh
    store = CustomConnectors(tmp_path)
    sources = SourceStore(tmp_path)
    manager = StorageConnections(store, sources)
    record = store.create(name='Recordings', description='Import the completed recordings.',
        kind='sftp', collection='shop', owner='alice')
    package = {'summary':'Import recordings from a saved folder.', 'instructions':'Review the folder and server key.',
        'secret_fields':[{'name':name, 'label':label, 'description':'Enter this in the private form.'}
            for name,label in [('SFTP_USER','Username'),('SFTP_PASSWORD','Password')]],
        'files':[{'path':'README.md', 'content':'Use the fixed SFTP importer.'}]}
    store.save_package(record['id'], package, expected_revision=1, owner='alice')
    target = {'host':'recordings.example.com', 'port':22, 'root':'/recordings',
        'host_key':asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode().strip(),
        'mode':'watch', 'stable_seconds':60, 'poll_seconds':120,
        'authentication':{'username_field':'SFTP_USER','password_field':'SFTP_PASSWORD'}}
    return store, sources, manager, record['id'], package, target


SCOPE = {'owner':'alice','collection':'shop'}


def propose(manager, key, target):
    return manager.propose(key, target, 'Import completed camera recordings.', expected_revision=2, **SCOPE)


def ready(tmp_path):
    store, sources, manager, key, package, target = fixture(tmp_path)
    proposal = propose(manager, key, target)
    manager.approve(key, proposal['id'], **SCOPE)
    store.set_secret(key,'SFTP_USER','private-user-123',owner='alice')
    store.set_secret(key,'SFTP_PASSWORD','private-password-123',owner='alice')
    return store, sources, manager, key, package, target


def test_proposal_requires_owner_action_and_snapshot_contains_only_references(tmp_path):
    store,sources,manager,key,_,target = fixture(tmp_path)
    proposal = propose(manager,key,target)
    assert propose(manager,key,target)['id'] == proposal['id']
    with pytest.raises(ValueError): manager.snapshot(key,**SCOPE)
    assert not sources.list_sources(IntakeActor('alice'))
    approved = manager.approve(key,proposal['id'],**SCOPE)
    assert approved['status'] == 'configured'  # Reachability requires an actual check.
    with pytest.raises(ValueError): manager.snapshot(key,**SCOPE)
    store.set_secret(key,'SFTP_USER','private-user-123',owner='alice')
    store.set_secret(key,'SFTP_PASSWORD','private-password-123',owner='alice')
    snapshot = manager.snapshot(key,**SCOPE)
    assert manager.current(snapshot)
    assert snapshot.config.owner_id == 'alice'
    assert snapshot.config.root == '/recordings'
    assert len(sources.list_sources(IntakeActor('alice'))) == 1
    credentials = manager.credentials(snapshot)
    assert credentials.config_sha256 == snapshot.config.sha256
    assert credentials.password == 'private-password-123'
    public = json.dumps(manager.describe(key,**SCOPE)) + repr(snapshot) + repr(credentials)
    assert 'private-user-123' not in public and 'private-password-123' not in public


@pytest.mark.parametrize('change',['owner','collection','source','generation'])
def test_stale_or_foreign_proposal_cannot_grant(tmp_path,change):
    store,_,manager,key,package,target = fixture(tmp_path)
    p = propose(manager,key,target);scope = dict(SCOPE)
    if change=='owner': scope['owner']='bob'
    elif change=='collection': scope['collection']='warehouse'
    elif change=='source': store.save_package(key,package,expected_revision=2,owner='alice')
    else:
        manager.approve(key,p['id'],**SCOPE)
        manager.pause(key,expected_generation=1,**SCOPE)
    with pytest.raises(ValueError): manager.approve(key,p['id'],**scope)


@pytest.mark.parametrize('change',['password','remove','source','pause','retarget'])
def test_running_snapshot_is_fenced_by_each_authority_change(tmp_path,change):
    store,_,manager,key,package,target = ready(tmp_path)
    snapshot=manager.snapshot(key,**SCOPE)
    if change=='password': store.set_secret(key,'SFTP_PASSWORD','replacement-password',owner='alice')
    elif change=='remove': store.remove_secret(key,'SFTP_PASSWORD',owner='alice')
    elif change=='source': store.save_package(key,package,expected_revision=2,owner='alice')
    elif change=='pause': manager.pause(key,expected_generation=1,**SCOPE)
    else:
        target['root']='/replacement'
        p=propose(manager,key,target);manager.approve(key,p['id'],**SCOPE)
    assert not manager.current(snapshot)
    with pytest.raises(ValueError): manager.credentials(snapshot)


def test_restart_and_poll_change_reuse_source_but_retarget_creates_one(tmp_path):
    store,sources,manager,key,_,target=ready(tmp_path)
    first=manager.snapshot(key,**SCOPE)
    reopened=StorageConnections(CustomConnectors(tmp_path),SourceStore(tmp_path))
    assert reopened.current(first)
    assert reopened.snapshot(key,**SCOPE)==first
    target['poll_seconds']=300
    p=propose(manager,key,target);manager.approve(key,p['id'],**SCOPE)
    second=manager.snapshot(key,**SCOPE)
    assert first.config.source_id==second.config.source_id
    assert not manager.current(first)
    target['root']='/second-folder'
    p=propose(manager,key,target);manager.approve(key,p['id'],**SCOPE)
    assert manager.snapshot(key,**SCOPE).config.source_id!=first.config.source_id
    assert len(sources.list_sources(IntakeActor('alice')))==2


def test_pause_waits_for_intake_commit_then_blocks_later_chunks(tmp_path):
    import threading
    _,sources,manager,key,_,_=ready(tmp_path)
    snapshot=manager.snapshot(key,**SCOPE)
    started=threading.Event();finished=threading.Event()
    def pause():
        started.set();manager.pause(key,expected_generation=1,**SCOPE);finished.set()
    with sources._write():
        thread=threading.Thread(target=pause);thread.start();assert started.wait(2)
        assert not finished.wait(.05)
        assert manager.current(snapshot)
    thread.join(2);assert finished.is_set()
    assert not manager.current(snapshot)


@pytest.mark.parametrize('field,value',[
    ('host','127.0.0.1'),('host','::1'),('host','fc00::1'),('host','fe80::1'),
    ('host','ff02::1'),('host','::ffff:127.0.0.1'),('host','localhost'),('host','https://example.com'),
    ('root','/recordings/../private'),('port',True),('poll_seconds',1),('host_key','unverified'),
    ('authentication',{'username_field':'SFTP_USER','password_field':'UNDECLARED'}),
])
def test_target_validation_rejects_ambiguous_or_undeclared_access(tmp_path,field,value):
    _,_,manager,key,_,target=fixture(tmp_path);target[field]=value
    with pytest.raises(ValueError): propose(manager,key,target)


def test_public_proposal_cannot_become_a_private_credential(tmp_path):
    store,_,manager,key,_,target=fixture(tmp_path)
    manager.propose(key,target,'Public marker-should-stay-public.',expected_revision=2,**SCOPE)
    with pytest.raises(ValueError): store.set_secret(key,'SFTP_PASSWORD','marker-should-stay-public',owner='alice')


def test_observer_source_and_foreign_company_cannot_supply_execution_authority(tmp_path):
    _,_,manager,key,_,_=ready(tmp_path/'company-a')
    snapshot=manager.snapshot(key,**SCOPE)
    other=StorageConnections(CustomConnectors(tmp_path/'company-b'),SourceStore(tmp_path/'company-b'))
    assert not other.current(snapshot)
    with pytest.raises(ValueError): other.credentials(snapshot)
    with pytest.raises(ValueError): manager.snapshot(key,owner='bob',collection='shop')


def test_current_guard_reads_without_waiting_for_an_uncommitted_credential_edit(tmp_path,monkeypatch):
    import time
    store,_,manager,key,_,_=ready(tmp_path)
    snapshot=manager.snapshot(key,**SCOPE)
    with store.db() as transaction:
        transaction.execute('BEGIN IMMEDIATE')
        # The pending writer has not yet changed authority. Reading its preceding
        # committed state is permitted and must not wait on the writer lock.
        def forbidden():raise AssertionError('The event-loop guard opened a writable store')
        monkeypatch.setattr(store,'db',forbidden)
        started=time.monotonic()
        assert manager.current(snapshot)
        assert time.monotonic()-started<.2


def test_guard_rejects_substituted_same_owner_source(tmp_path):
    from custom_execution import encoded
    from storage_connections import StorageSnapshot
    _,sources,manager,key,_,_=ready(tmp_path)
    snapshot=manager.snapshot(key,**SCOPE);value=snapshot.metadata
    other=sources.create_source(IntakeActor('alice'),collection='shop',name='Other',kind='sftp')
    value['config']['source_id']=other['id']
    assert not manager.current(StorageSnapshot(encoded(value)))
