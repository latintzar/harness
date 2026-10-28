"""Custom activation is host authority over immutable source, never source claims."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from custom_connectors import CustomConnectors
from custom_execution import CustomExecution, RuntimeProfile, PROFILE, IMAGE, ExecutionError

SCHEMA={'type':'object','properties':{},'additionalProperties':False}

def package():
    return {'summary':'Read inventory.','instructions':'Run the approved inventory check.',
        'secret_fields':[{'name':'API_TOKEN','label':'API token','description':'A read-scoped token.'}],
        'files':[{'path':'src/adapter.py','content':'raise RuntimeError("source remains data")\n'}],
        'execution':{'profile':PROFILE,'entrypoint':'src/adapter.py',
            'tools':[{'name':'list_inventory','description':'List authorized inventory.',
                'input_schema':SCHEMA,'output_schema':SCHEMA,'operations':['inventory']}],
            'tests':[{'tool':'list_inventory','arguments':{}}]}}

def target():
    return {'origin':'https://inventory.example.com','authentication':{'kind':'bearer','credential':'API_TOKEN'},
        'operations':[{'id':'inventory','method':'GET','path':'/v1/inventory','query_schema':SCHEMA}]}

def setup(tmp_path,*,runtime=True,secret=True):
    store=CustomConnectors(tmp_path)
    saved=store.create(name='Inventory',description='Read inventory.',kind='api',collection='shop',owner='alice')
    key=saved['id'];store.save_package(key,package(),expected_revision=1,owner='alice')
    if secret:store.set_secret(key,'API_TOKEN','private-customer-token',owner='alice')
    manager=CustomExecution(store,runtime=RuntimeProfile(IMAGE,'a'*64)if runtime else None)
    manager.grant_target(key,target(),owner='alice',collection='shop',expected_generation=0)
    return store,manager,key

def evidence(manager,lease):
    snap=manager.test_snapshot(lease,owner='alice',collection='shop')
    return {'test_id':lease.id,'snapshot_sha256':snap.sha256,'profile':PROFILE,'image_digest':IMAGE,'worker_sha256':'a'*64,
        'broker_run_id':'broker-'+('b'*32),'cleanup_confirmed':True,
        'checks':[{'tool':'list_inventory','arguments_sha256':hashlib.sha256(b'{}').hexdigest(),
                  'result_sha256':'c'*64,'schema_valid':True,'operation_receipts':{'inventory':'d'*64}}]}

def _tested(manager,key):
    lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    result=manager.complete_test(lease,evidence(manager,lease),owner='alice',collection='shop')
    return lease,result

def activate(manager,key):
    lease,result=_tested(manager,key)
    return manager.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)


def test_source_snapshot_is_immutable_and_contains_no_credentials(tmp_path):
    store,manager,key=setup(tmp_path)
    snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    assert b'private-customer-token'not in snap.document
    assert b'private-customer-token'not in b''.join(f.content for f in snap.files)
    assert snap.files[0].content==package()['files'][0]['content'].encode()
    with pytest.raises(FrozenInstanceError):snap.document=b'changed'
    data=snap.metadata;data['owner']='bob';assert snap.metadata['owner']=='alice'
    assert store.get(key,owner='alice')['status']=='prepared'
    assert manager.status(key,owner='alice',collection='shop')['active_revision']is None
    assert manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)==snap


@pytest.mark.parametrize('change',['owner','collection','company','revision'])
def test_snapshot_scope_and_cas(tmp_path,change):
    store,manager,key=setup(tmp_path)
    args={'owner':'alice','collection':'shop','expected_revision':2}
    if change=='owner':args['owner']='bob'
    elif change=='collection':args['collection']='other'
    elif change=='revision':args['expected_revision']=1
    else:manager=CustomExecution(CustomConnectors(tmp_path/'other'))
    with pytest.raises(ValueError):manager.snapshot(key,**args)


@pytest.mark.parametrize('path',['../escape.py','/root/run.py','src//adapter.py','.hidden.py','src/adapter.js','src/missing.py'])
def test_entrypoint_is_existing_relative_python_source(tmp_path,path):
    store=CustomConnectors(tmp_path);saved=store.create(name='API',description='Read.',kind='api',collection='shop',owner='alice')
    bad=package();bad['execution']['entrypoint']=path
    with pytest.raises(ValueError):store.save_package(saved['id'],bad,expected_revision=1,owner='alice')


@pytest.mark.parametrize('change',['status','grant','runtime','external_schema','untested_tool','unknown_operation','oversized_schema'])
def test_source_manifest_never_creates_authority(tmp_path,change):
    store=CustomConnectors(tmp_path);saved=store.create(name='API',description='Read.',kind='api',collection='shop',owner='alice')
    bad=package();manifest=bad['execution']
    if change in {'status','grant','runtime'}:manifest[change]='active'
    elif change=='external_schema':manifest['tools'][0]['input_schema']={'$ref':'https://evil.example/schema'}
    elif change=='untested_tool':manifest['tests']=[]
    elif change=='unknown_operation':manifest['tools'][0]['operations']=['../../forged']
    else:manifest['tools'][0]['input_schema']={'type':'object','properties':{'x':{'type':'string','pattern':'(a+)+'}},'additionalProperties':False}
    with pytest.raises(ValueError):store.save_package(saved['id'],bad,expected_revision=1,owner='alice')


@pytest.mark.parametrize('value',['http://api.example.com','https://user:pass@api.example.com','https://api.example.com?token=private',
    'https://api.example.com/path','https://api.example.com#secret','https://127.0.0.1','https://[::1]','https://api.local','https://localhost','https://api.example.com:8443','https://@api.example.com'])
def test_owner_target_rejects_url_credentials_private_literals_and_ambiguous_origins(tmp_path,value):
    store,manager,key=setup(tmp_path)
    bad=target();bad['origin']=value
    with pytest.raises(ValueError):manager.grant_target(key,bad,owner='alice',collection='shop',expected_generation=1)
    assert manager.status(key,owner='alice',collection='shop')['target_generation']==1


@pytest.mark.parametrize('change',['write','redirect_path','query_secret','dot_path','encoded_path','arbitrary_header','secret_value'])
def test_target_operations_and_auth_refs_are_narrow(tmp_path,change):
    store,manager,key=setup(tmp_path)
    bad=target()
    if change=='write':bad['operations'][0]['method']='POST'
    elif change=='redirect_path':bad['operations'][0]['path']='//evil.example/data'
    elif change=='query_secret':bad['operations'][0]['path']='/v1/data?token=secret'
    elif change=='dot_path':bad['operations'][0]['path']='/v1/../admin'
    elif change=='encoded_path':bad['operations'][0]['path']='/v1/%2e%2e/admin'
    elif change=='arbitrary_header':bad['authentication']={'kind':'header','name':'Host','credential':'API_TOKEN'}
    else:bad['authentication']={'kind':'bearer','credential':'private-customer-token'}
    with pytest.raises(ValueError):manager.grant_target(key,bad,owner='alice',collection='shop',expected_generation=1)


def test_destination_replacement_issues_new_grant_and_invalidates_old_snapshot(tmp_path):
    store,manager,key=setup(tmp_path);snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    changed=target();changed['origin']='https://other.example.com'
    with pytest.raises(ValueError):manager.grant_target(key,changed,owner='alice',collection='shop',expected_generation=0)
    result=manager.grant_target(key,changed,owner='alice',collection='shop',expected_generation=1)
    assert result['generation']==2
    with pytest.raises(ValueError):manager.validate_snapshot(snap,owner='alice',collection='shop')
    with store.db()as db:
        rows=db.execute('SELECT generation,document FROM custom_execution_grants ORDER BY generation').fetchall()
    assert [r[0]for r in rows]==[1,2]
    assert json.loads(rows[0][1])['origin']=='https://inventory.example.com'


def test_missing_runtime_or_credentials_never_admits_tests(tmp_path):
    store,manager,key=setup(tmp_path,runtime=False)
    assert manager.status(key,owner='alice',collection='shop')['runtime_available']is False
    with pytest.raises(ExecutionError,match='commissioned'):manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    store2,manager2,key2=setup(tmp_path/'missing',secret=False)
    with pytest.raises(ExecutionError,match='credential'):manager2.begin_test(key2,owner='alice',collection='shop',expected_revision=2)


def test_host_test_receipt_activates_exact_revision_with_cas(tmp_path):
    store,manager,key=setup(tmp_path);lease,result=_tested(manager,key)
    assert result['status']=='passed'
    assert store.get(key,owner='alice')['status']=='prepared'
    with pytest.raises(ValueError):manager.activate(result['id'],owner='bob',collection='shop',expected_active_generation=0)
    active=manager.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)
    assert active['active_revision']==2 and active['active_status']=='active'
    with pytest.raises(ValueError):manager.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)
    reopened=CustomExecution(CustomConnectors(tmp_path),runtime=RuntimeProfile(IMAGE,'a'*64))
    assert reopened.active_snapshot(key,owner='alice',collection='shop').sha256==lease.snapshot_sha256
    assert 'private-customer-token'not in json.dumps(reopened.status(key,owner='alice',collection='shop'))


@pytest.mark.parametrize('change',['snapshot','runtime','worker','cleanup','check','operation','token'])
def test_host_receipts_require_exact_runtime_evidence_and_lease(tmp_path,change):
    store,manager,key=setup(tmp_path);lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2);receipt=evidence(manager,lease)
    if change=='snapshot':receipt['snapshot_sha256']='0'*64
    elif change=='runtime':receipt['image_digest']='sha256:'+'0'*64
    elif change=='worker':receipt['worker_sha256']='0'*64
    elif change=='cleanup':receipt['cleanup_confirmed']=False
    elif change=='check':receipt['checks'][0]['schema_valid']=False
    elif change=='operation':receipt['checks'][0]['operation_receipts']={}
    else:
        from dataclasses import replace
        lease=replace(lease,token='0'*64)
    with pytest.raises(ValueError):manager.complete_test(lease,receipt,owner='alice',collection='shop')
    assert manager.status(key,owner='alice',collection='shop')['active_status']=='unconfigured'


@pytest.mark.parametrize('change',['source','secret','target','revoke'])
def test_changes_fence_pending_test_and_activation(tmp_path,change):
    store,manager,key=setup(tmp_path);lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2);receipt=evidence(manager,lease)
    if change=='source':store.save_package(key,package(),expected_revision=2,owner='alice')
    elif change=='secret':store.set_secret(key,'API_TOKEN','rotated-customer-token',owner='alice')
    elif change=='target':
        new=target();new['origin']='https://other.example.com';manager.grant_target(key,new,owner='alice',collection='shop',expected_generation=1)
    else:manager.revoke(key,owner='alice',collection='shop')
    with pytest.raises(ValueError):manager.complete_test(lease,receipt,owner='alice',collection='shop')
    assert manager.status(key,owner='alice',collection='shop')['active_revision']is None


def test_repair_preserves_active_version_and_credentials_until_explicit_change(tmp_path):
    store,manager,key=setup(tmp_path);activate(manager,key)
    old=manager.active_snapshot(key,owner='alice',collection='shop')
    repair=package();repair.pop('execution');repair['secret_fields']=[];repair['files'][0]['content']='print("draft repair")\n'
    current=store.save_package(key,repair,expected_revision=2,owner='alice')
    assert current['revision']==3 and current['status']=='prepared'
    assert manager.active_snapshot(key,owner='alice',collection='shop')==old
    assert current['active_secret_fields'][0]['name']=='API_TOKEN'and current['active_secret_fields'][0]['configured']
    store.set_secret(key,'API_TOKEN','rotated-retained-token',owner='alice')
    assert manager.status(key,owner='alice',collection='shop')['active_status']=='needs_verification'
    with pytest.raises(ValueError):manager.active_snapshot(key,owner='alice',collection='shop')
    assert manager.status(key,owner='alice',collection='shop')['active_revision']==2


def test_active_secret_removal_and_missing_key_fail_closed(tmp_path):
    store,manager,key=setup(tmp_path);activate(manager,key)
    store.remove_secret(key,'API_TOKEN',owner='alice')
    with pytest.raises(ValueError):manager.active_snapshot(key,owner='alice',collection='shop')
    store.set_secret(key,'API_TOKEN','replacement-token',owner='alice')
    store.key_path.unlink()
    with pytest.raises(ValueError,match='Restore'):store.set_secret(key,'API_TOKEN','another-token',owner='alice')
    assert not store.key_path.exists()


def test_concurrent_activation_has_one_cas_winner(tmp_path):
    store,manager,key=setup(tmp_path);_,result=_tested(manager,key)
    def run(_):
        local=CustomExecution(CustomConnectors(tmp_path),runtime=RuntimeProfile(IMAGE,'a'*64))
        try:return local.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)['active_generation']
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2)as pool:answers=list(pool.map(run,range(2)))
    assert sorted(answers,key=lambda x:x is None)==[1,None]


@pytest.mark.parametrize('change',['source','secret','target','revoke'])
def test_completed_evidence_stays_historical_but_cannot_promote_stale_authority(tmp_path,change):
    store,manager,key=setup(tmp_path);_,result=_tested(manager,key)
    if change=='source':store.save_package(key,package(),expected_revision=2,owner='alice')
    elif change=='secret':store.set_secret(key,'API_TOKEN','new-private-token',owner='alice')
    elif change=='target':
        new=target();new['origin']='https://other.example.com';manager.grant_target(key,new,owner='alice',collection='shop',expected_generation=1)
    else:manager.revoke(key,owner='alice',collection='shop')
    assert manager.test_result(result['id'],owner='alice',collection='shop')['status']=='passed'
    with pytest.raises(ValueError):manager.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)


def test_revocation_closes_new_tests_until_new_owner_grant(tmp_path):
    store,manager,key=setup(tmp_path);manager.revoke(key,owner='alice',collection='shop')
    assert manager.status(key,owner='alice',collection='shop')['active_status']=='revoked'
    with pytest.raises(ValueError):manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    result=manager.grant_target(key,target(),owner='alice',collection='shop',expected_generation=1)
    assert result['generation']==2
    assert manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)


def test_receipt_expiry_and_failure_never_promote(tmp_path):
    store,manager,key=setup(tmp_path);clock=[1000.];manager.clock=lambda:clock[0]
    lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    clock[0]+=901
    assert manager.test_result(lease.id,owner='alice',collection='shop')['status']=='expired'
    with pytest.raises(ValueError):manager.complete_test(lease,{},owner='alice',collection='shop')
    lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    assert manager.fail_test(lease,owner='alice',collection='shop',reason='cleanup_uncertain')['status']=='cleanup_uncertain'
    with pytest.raises(ValueError):manager.activate(lease.id,owner='alice',collection='shop',expected_active_generation=0)


def test_secret_changed_via_private_request_advances_generation_once_per_saved_value(tmp_path):
    store,manager,key=setup(tmp_path);before=manager.status(key,owner='alice',collection='shop')['credential_generation']
    request=store.request_credentials(key,owner='alice',chat_id='parent',collection='shop',reason='Rotate access.')
    values={'API_TOKEN':'rotated-in-private-form'}
    store.resolve_credential_request(request['id'],owner='alice',chat_id='parent',action='submit',values=values)
    after=manager.status(key,owner='alice',collection='shop')['credential_generation']
    assert after==before+1
    store.resolve_credential_request(request['id'],owner='alice',chat_id='parent',action='submit',values=values)
    assert manager.status(key,owner='alice',collection='shop')['credential_generation']==after


def test_secret_entry_rejects_prior_target_metadata_and_source_tamper_fails_snapshot(tmp_path):
    store,manager,key=setup(tmp_path)
    with pytest.raises(ValueError):store.set_secret(key,'API_TOKEN','inventory.example.com',owner='alice')
    with store.db()as db:
        data=json.loads(db.execute('SELECT package FROM versions WHERE connector_id=?',(key,)).fetchone()[0])
        data['files'][0]['content']='print("tampered")'
        db.execute('UPDATE versions SET package=? WHERE connector_id=?',(json.dumps(data),key))
    with pytest.raises(ValueError):manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)


def test_active_missing_key_cannot_issue_execution_snapshot(tmp_path):
    store,manager,key=setup(tmp_path);activate(manager,key);store.key_path.unlink()
    with pytest.raises(ValueError,match='Restore'):manager.active_snapshot(key,owner='alice',collection='shop')
    assert not store.key_path.exists()


def test_execution_snapshot_rejects_mutable_or_malformed_documents(tmp_path):
    from custom_execution import ExecutionSnapshot
    store,manager,key=setup(tmp_path);snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    for document in [bytearray(snap.document),b'{}',b'{broken']:
        with pytest.raises(ValueError):ExecutionSnapshot(document,snap.files)
    with pytest.raises(ValueError):manager.fail_test({},owner='alice',collection='shop',reason='failed')


def test_identical_owner_grant_is_idempotent_but_history_capacity_is_bounded(tmp_path,monkeypatch):
    import custom_execution
    store,manager,key=setup(tmp_path)
    assert manager.grant_target(key,target(),owner='alice',collection='shop',expected_generation=1)['generation']==1
    monkeypatch.setattr(custom_execution,'MAX_GRANTS',1)
    different=target();different['origin']='https://other.example.com'
    with pytest.raises(ValueError):manager.grant_target(key,different,owner='alice',collection='shop',expected_generation=1)
    assert manager.status(key,owner='alice',collection='shop')['target_generation']==1


def test_copied_receipt_cannot_activate_under_another_company_identity(tmp_path):
    import shutil
    store,manager,key=setup(tmp_path/'original');_,result=_tested(manager,key)
    second=tmp_path/'second';second.mkdir();shutil.copytree(store.root,second/'custom-connectors')
    other=CustomExecution(CustomConnectors(second),runtime=RuntimeProfile(IMAGE,'a'*64))
    with pytest.raises(ValueError):other.activate(result['id'],owner='alice',collection='shop',expected_active_generation=0)


def test_atomic_private_submission_failure_preserves_active_generation(tmp_path):
    store,manager,key=setup(tmp_path);activate(manager,key)
    before=manager.status(key,owner='alice',collection='shop')['credential_generation']
    request=store.request_credentials(key,owner='alice',chat_id='parent',collection='shop',reason='Rotate access.')
    with pytest.raises(ValueError):store.resolve_credential_request(request['id'],owner='alice',chat_id='parent',action='submit',
        values={'API_TOKEN':'accepted-looking-token','UNKNOWN':'not-declared'})
    assert manager.status(key,owner='alice',collection='shop')['credential_generation']==before
    assert manager.active_snapshot(key,owner='alice',collection='shop')


def test_private_credential_snapshot_is_frozen_repr_hidden_and_exact(tmp_path):
    store,manager,key=setup(tmp_path);snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    private=manager.host_credentials(snap,owner='alice',collection='shop')
    assert private.as_mapping()=={'API_TOKEN':'private-customer-token'}
    assert 'private-customer-token'not in repr(private)
    assert private.snapshot_sha256==snap.sha256 and private.company_id==manager.company
    with pytest.raises(FrozenInstanceError):private.fields=()
    view=private.as_mapping();view['API_TOKEN']='changed'
    assert private.as_mapping()['API_TOKEN']=='private-customer-token'


@pytest.mark.parametrize('change',['owner','collection','company','secret','target','revoke','source','runtime'])
def test_host_credentials_reject_changed_or_other_authority(tmp_path,change):
    store,manager,key=setup(tmp_path);snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    args={'owner':'alice','collection':'shop'}
    if change=='owner':args['owner']='bob'
    elif change=='collection':args['collection']='other'
    elif change=='company':manager=CustomExecution(CustomConnectors(tmp_path/'other'),runtime=RuntimeProfile(IMAGE,'a'*64))
    elif change=='secret':store.set_secret(key,'API_TOKEN','rotated-customer-token',owner='alice')
    elif change=='target':
        new=target();new['origin']='https://other.example.com';manager.grant_target(key,new,owner='alice',collection='shop',expected_generation=1)
    elif change=='revoke':manager.revoke(key,owner='alice',collection='shop')
    elif change=='source':store.save_package(key,package(),expected_revision=2,owner='alice')
    else:manager=CustomExecution(store)
    with pytest.raises(ValueError):manager.host_credentials(snap,**args)


def test_credentials_for_retained_active_version_survive_source_only_repair(tmp_path):
    store,manager,key=setup(tmp_path);activate(manager,key);snap=manager.active_snapshot(key,owner='alice',collection='shop')
    repair=package();repair['files'][0]['content']='print("repair")\n';store.save_package(key,repair,expected_revision=2,owner='alice')
    assert manager.host_credentials(snap,owner='alice',collection='shop').as_mapping()=={'API_TOKEN':'private-customer-token'}


def test_auth_profile_requires_every_declared_secret_to_be_host_injected(tmp_path):
    store,manager,key=setup(tmp_path);public=target();public['authentication']={'kind':'none'}
    manager.grant_target(key,public,owner='alice',collection='shop',expected_generation=1)
    with pytest.raises(ValueError):manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)


@pytest.mark.parametrize('malformed',[None,[],{'profile':[]},{'origin':None,'authentication':{'kind':[]},'operations':[]}])
def test_malformed_manifest_and_grant_raise_safe_validation(tmp_path,malformed):
    from custom_execution import validate_manifest,validate_target
    for fn,args in [(validate_manifest,(malformed,package()['files'])),(validate_target,(malformed,{'API_TOKEN'}))]:
        with pytest.raises(ValueError):fn(*args)


def test_uncertain_cleanup_blocks_fresh_tests_until_matching_host_reconciliation(tmp_path):
    store,manager,key=setup(tmp_path)
    lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    manager.fail_test(lease,owner='alice',collection='shop',reason='cleanup_uncertain')
    with pytest.raises(ValueError,match='cleanup'):manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    receipt={'snapshot_sha256':lease.snapshot_sha256,'cleanup_confirmed':True,'broker_run_id':'broker-'+32*'f','receipt_sha256':64*'e'}
    with pytest.raises(ValueError):manager.confirm_test_cleanup(lease,{**receipt,'cleanup_confirmed':False},owner='alice',collection='shop')
    assert manager.confirm_test_cleanup(lease,receipt,owner='alice',collection='shop')['status']=='stopped'
    assert manager.confirm_test_cleanup(lease,receipt,owner='alice',collection='shop')['status']=='stopped'
    assert manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)


def test_completed_test_evidence_cannot_be_replayed_into_new_host_test_lease(tmp_path):
    store,manager,key=setup(tmp_path)
    first=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    old=evidence(manager,first)
    manager.complete_test(first,old,owner='alice',collection='shop')
    second=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    with pytest.raises(ValueError):manager.complete_test(second,old,owner='alice',collection='shop')
    assert manager.test_result(second.id,owner='alice',collection='shop')['status']=='pending'


@pytest.mark.parametrize('change',['revoke','source','secret','expiry'])
def test_changed_running_authority_retains_failed_cleanup_fence(tmp_path,change):
    store,manager,key=setup(tmp_path);clock=[1000.];manager.clock=lambda:clock[0]
    lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
    snapshot=manager.test_snapshot(lease,owner='alice',collection='shop')
    revision=2
    if change=='revoke':
        manager.revoke(key,owner='alice',collection='shop')
        manager.grant_target(key,target(),owner='alice',collection='shop',expected_generation=1)
    elif change=='source':
        store.save_package(key,package(),expected_revision=2,owner='alice');revision=3
    elif change=='secret':store.set_secret(key,'API_TOKEN','rotated-private-value',owner='alice')
    else:
        clock[0]+=901
        assert manager.test_result(lease.id,owner='alice',collection='shop')['status']=='expired'
    assert manager.fail_test(lease,owner='alice',collection='shop',reason='cleanup_uncertain')['status']=='cleanup_uncertain'
    recorded=manager.test_result(lease.id,owner='alice',collection='shop')
    assert recorded['snapshot_sha256']==snapshot.sha256 and recorded['revision']==2
    with pytest.raises(ValueError,match='cleanup'):
        manager.begin_test(key,owner='alice',collection='shop',expected_revision=revision)
    with pytest.raises(ValueError):manager.activate(lease.id,owner='alice',collection='shop',expected_active_generation=0)
    receipt={'snapshot_sha256':lease.snapshot_sha256,'cleanup_confirmed':True,'broker_run_id':'broker-'+32*'f','receipt_sha256':64*'e'}
    assert manager.confirm_test_cleanup(lease,receipt,owner='alice',collection='shop')['status']=='stopped'
    assert manager.begin_test(key,owner='alice',collection='shop',expected_revision=revision)


def test_wire_authority_guard_keeps_active_repair_but_fences_stale_draft(tmp_path):
    store,manager,key=setup(tmp_path);activate(manager,key)
    active=manager.active_snapshot(key,owner='alice',collection='shop')
    assert manager.authority_current(active,owner='alice',collection='shop')
    store.save_package(key,package(),expected_revision=2,owner='alice')
    assert manager.authority_current(active,owner='alice',collection='shop')
    draft=manager.snapshot(key,owner='alice',collection='shop',expected_revision=3)
    store.save_package(key,package(),expected_revision=3,owner='alice')
    assert not manager.authority_current(draft,owner='alice',collection='shop')
    assert manager.authority_current(active,owner='alice',collection='shop')
    store.set_secret(key,'API_TOKEN','new-private-wire-token',owner='alice')
    assert not manager.authority_current(active,owner='alice',collection='shop')


@pytest.mark.parametrize('change',['owner','collection','runtime','target','revoke','busy','missing'])
def test_wire_authority_guard_fails_closed_without_full_snapshot_work(tmp_path,change,monkeypatch):
    store,manager,key=setup(tmp_path);snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2)
    args={'owner':'alice','collection':'shop'};held=None
    if change=='owner':args['owner']='bob'
    elif change=='collection':args['collection']='other'
    elif change=='runtime':manager.runtime=None
    elif change=='target':
        newer=target();newer['origin']='https://next.example.com';manager.grant_target(key,newer,owner='alice',collection='shop',expected_generation=1)
    elif change=='revoke':manager.revoke(key,owner='alice',collection='shop')
    elif change=='busy':held=store.db();held.execute('BEGIN EXCLUSIVE')
    else:store.database.rename(store.database.with_suffix('.saved'))
    def forbidden(*a,**k):raise AssertionError('full snapshot/credential work reached synchronous wire guard')
    monkeypatch.setattr(manager,'_snapshot',forbidden);monkeypatch.setattr(store,'_cipher',forbidden)
    try:assert manager.authority_current(snap,**args)is False
    finally:
        if held:held.rollback();held.close()
    if change=='missing':assert not store.database.exists()
