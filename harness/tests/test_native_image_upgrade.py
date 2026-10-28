"""Reviewed image proof binding, activation fencing and three-file rollback."""
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

DEPLOY=Path(__file__).parents[1]/'deploy/native-executor'
spec=importlib.util.spec_from_file_location('image_upgrade',DEPLOY/'upgrade_image.py')
x=importlib.util.module_from_spec(spec);spec.loader.exec_module(x)
spec=importlib.util.spec_from_file_location('image_upgrade_fixture_common',DEPLOY/'upgrade_custom.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)


def write(path,value):path.write_bytes(x.canonical(value)+b'\n')
def root_file(path,h=None):
    assert path.is_file()
    if h:x.require(x.sha(path.read_bytes())==h,'source_checksum_changed')
def load(path,h=None):root_file(path,h);return json.loads(path.read_bytes())


@pytest.fixture
def bundle(tmp_path):
    p=tmp_path/'bundle';p.mkdir()
    boundary=json.loads((DEPLOY/'TOOLING-BOUNDARY-2026-10-05.json').read_bytes())
    previous={k:v for k,v in boundary['installed_sources'].items()if k not in x.RECORDS}
    records={k:v for k,v in boundary['installed_sources'].items()if k in x.RECORDS}
    for kind in x.PROOFS:shutil.copyfile(DEPLOY/f'TOOLING-{kind.upper()}-2026-10-05.json',p/(kind+'-proof.json'))
    shutil.copyfile(DEPLOY/'IMAGE-COMPLETE-2026-10-04.json',p/'previous-image.json')
    shutil.copyfile(DEPLOY/'IMAGE-TOOLING-CANDIDATE-2026-10-05.json',p/'candidate-image.json')
    # Proof byte validation is separate from installed executor replacement tests;
    # positive proof evidence here is the actual checked-in candidate receipts.
    new=load(p/'candidate-image.json')['image_id'];old=load(p/'previous-image.json')['image_id']
    r={'schema_version':1,'runtime':common.RUNTIME,'image':new,'previous_image':old,'previous_sources':previous,'previous_records':records,
       'executor_sha256':'871d0bca6b5899d9873d8b83318f5a29a2fbbe48de1751a02d278745fd0a816a',
       'proofs':{k:x.sha((p/(k+'-proof.json')).read_bytes())for k in x.PROOFS},
       'previous_image_manifest_sha256':x.sha((p/'previous-image.json').read_bytes()),'image_manifest_sha256':x.sha((p/'candidate-image.json').read_bytes())}
    write(p/'image-release.json',r)
    u=SimpleNamespace(**{name:getattr(common,name)for name in ('RUNTIME','PROFILE','INSTALL_PROFILE','NAMES','INSTALL_NAMES','CUSTOM_NAMES','sources')},load=load,
        root_file=lambda path,h=None:None if path.name=='native_executor.py'else root_file(path,h))
    return p,u,r


def test_real_four_profile_receipts_accept_exact_candidate(bundle):
    p,u,r=bundle
    assert x.bundle_record(u,p)==r


@pytest.mark.parametrize('kind,field,value',[
    ('boundary','image_id','sha256:'+'0'*64),('boundary','remaining_containers',1),
    ('bridge','remaining_leases',['running']),('bridge','installed_broker_changed',True),
    ('installer','supported_plugin_count',47),('installer','provider_requests',1),
    ('installer','other_owner_empty',False),('custom','remaining_jobs',1),('custom','model_calls',1),
])
def test_wrong_real_proof_rejected_even_after_manifest_rehash(bundle,kind,field,value):
    p,u,r=bundle;proof=load(p/(kind+'-proof.json'));proof[field]=value;write(p/(kind+'-proof.json'),proof)
    r['proofs'][kind]=x.sha((p/(kind+'-proof.json')).read_bytes());write(p/'image-release.json',r)
    with pytest.raises(RuntimeError):x.bundle_record(u,p)


@pytest.mark.parametrize('kind',['installer','custom','bridge'])
def test_candidate_proof_source_mismatch_rejected(bundle,kind):
    p,u,r=bundle;proof=load(p/(kind+'-proof.json'))
    field='sources'if kind=='custom'else'source_sha256'
    if isinstance(proof[field],dict):proof[field]['native_executor.py']='0'*64
    else:next(item for item in proof[field]if item['path']=='native_executor.py')['sha256']='0'*64
    write(p/(kind+'-proof.json'),proof);r['proofs'][kind]=x.sha((p/(kind+'-proof.json')).read_bytes());write(p/'image-release.json',r)
    with pytest.raises(RuntimeError,match='(executor|source)_changed'):x.bundle_record(u,p)


def test_tooling_proof_keeps_previous_entrypoint_and_relay(bundle):
    p,u,r=bundle;build=load(p/'candidate-image.json');build['source_sha256']['relay.py']='0'*64
    write(p/'candidate-image.json',build);r['image_manifest_sha256']=x.sha((p/'candidate-image.json').read_bytes());write(p/'image-release.json',r)
    with pytest.raises(RuntimeError,match='existing_image_component_changed'):x.bundle_record(u,p)


@pytest.mark.parametrize('extra',[b'',b'\nNETWORK="host"\n'])
def test_executor_replacement_accepts_only_single_image_assignment(extra):
    before=b'IMAGE = "old"\nRUNTIME = "rocoto-runsc"\n';after=before.replace(b'"old"',b'"new"')+extra
    if extra:
        with pytest.raises(RuntimeError,match='executor_has_other_changes'):x.changed_executor(before,after,'old','new')
    else:x.changed_executor(before,after,'old','new')


def test_refreshed_records_change_only_image_and_executor_hash():
    previous={n:{'image':'old','sources':{'native_executor.py':'oldhash','worker.py':'fixed'},'historical_proof':'same',
        'catalogue_revision':'same','future_field':{'preserve':True}}for n in x.RECORDS}
    result=x.refreshed(previous,{'image':'new','executor_sha256':'newhash'})
    for n in x.RECORDS:
        expected=copy.deepcopy(previous[n]);expected['image']='new';expected['sources']['native_executor.py']='newhash'
        assert result[n]==expected
    assert previous['custom-commissioning.json']['image']=='old'


def application(tmp_path,monkeypatch,active=False):
    state=tmp_path/'app';state.mkdir(mode=0o700);monkeypatch.setattr(x,'APP_STATE',state)
    access=state/'access.sqlite'
    with sqlite3.connect(access)as db:
        db.execute('CREATE TABLE environments(id TEXT PRIMARY KEY)');db.execute("INSERT INTO environments VALUES('demo')")
    access.chmod(0o600)
    folder=state/'custom-connectors';folder.mkdir(mode=0o700);database=folder/'connectors.sqlite'
    with sqlite3.connect(database)as db:
        db.execute('CREATE TABLE custom_execution_authority(active_snapshot TEXT,active_status TEXT)')
        db.execute('INSERT INTO custom_execution_authority VALUES(?,?)',('snapshot','active'if active else'revoked'))
    database.chmod(0o600)
    u=SimpleNamespace(pwd=SimpleNamespace(getpwnam=lambda _:SimpleNamespace(pw_uid=os.getuid())))
    return state,database,u


def test_active_custom_snapshot_prevents_image_change(tmp_path,monkeypatch):
    _,database,u=application(tmp_path,monkeypatch,True);before=database.read_bytes()
    with pytest.raises(RuntimeError,match='active_custom_snapshots_require_explicit_migration:1'):
        with x.application_guard(u):pytest.fail('active authority passed')
    assert database.read_bytes()==before


def test_guard_blocks_racing_activation_and_preserves_database_bytes(tmp_path,monkeypatch):
    state,database,u=application(tmp_path,monkeypatch);before=database.read_bytes()
    with x.application_guard(u,lock=True)as counts:
        assert counts=={'registered_companies':1,'connector_stores':1,'active_custom_snapshots':0,'promotable_custom_tests':0}
        with sqlite3.connect(database,timeout=0)as db:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                db.execute("UPDATE custom_execution_authority SET active_status='active'")
        with sqlite3.connect(state/'access.sqlite',timeout=0)as db:
            with pytest.raises(sqlite3.OperationalError,match='locked'):db.execute("INSERT INTO environments VALUES('other')")
    assert database.read_bytes()==before
    with sqlite3.connect(database,timeout=0)as db:db.execute("UPDATE custom_execution_authority SET active_status='active'")


def rehearsal(tmp_path,monkeypatch,fail=False):
    dest=tmp_path/'installed';dest.mkdir();state=tmp_path/'broker';state.mkdir();bundle=tmp_path/'bundle';bundle.mkdir()
    old={n:(n+' before').encode()for n in x.CHANGED}
    for n,raw in old.items():(dest/n).write_bytes(raw)
    (bundle/'native_executor.py').write_bytes(b'executor candidate')
    ledger=state/'leases.sqlite3';ledger.write_bytes(b'initial ledger\n')
    records={n:{'image':'old','sources':{'native_executor.py':'oldhash','worker.py':'fixed'},'historical':'preserve'}for n in x.RECORDS}
    r={'previous_image':'old','image':'new','executor_sha256':x.sha(b'executor candidate')}
    calls=[];starts=0
    def run(args):
        nonlocal starts
        calls.append(args)
        if args[1]=='start':
            starts+=1
            with ledger.open('ab')as f:f.write(b'new durable row\n')
        return ''
    def atomic(p,raw,mode=0o644,uid=0,gid=0):p.write_bytes(raw)
    control=tmp_path/'control';control.touch()
    u=SimpleNamespace(DEST=dest,STATE=state,SERVICE='broker-only',CONTROL=control,service_pids=lambda:{'app':'100','media':'200','docker':'300'},
        root_file=root_file,atomic=atomic,run=run,quiescent=lambda:calls.append('idle'))
    @contextmanager
    def guard(*a,**kw):yield x.AuthorityCounts({'active_custom_snapshots':0},lambda:None)
    monkeypatch.setattr(x,'application_guard',guard);monkeypatch.setattr(x,'bundle_record',lambda *a:r)
    monkeypatch.setattr(x,'installed',lambda *a:copy.deepcopy(records))
    attempts=[]
    def ready(u,value):
        attempts.append(value)
        if value['custom-commissioning.json']['image']=='new':
            if fail:raise RuntimeError('synthetic_readiness_failed')
            assert (dest/'native_executor.py').read_bytes()==b'executor candidate'
        else:assert all((dest/n).read_bytes()==v for n,v in old.items())
        return {'ready':True}
    monkeypatch.setattr(x,'ready',ready)
    return u,bundle,r,records,old,ledger,calls,attempts


@pytest.mark.parametrize('fail',[False,True])
def test_upgrade_or_rollback_changes_only_three_files_and_preserves_new_rows(tmp_path,monkeypatch,fail):
    u,bundle,r,records,old,ledger,calls,attempts=rehearsal(tmp_path,monkeypatch,fail)
    if fail:
        with pytest.raises(RuntimeError,match='synthetic_readiness_failed'):x.apply(u,bundle,r,records)
        assert all((u.DEST/n).read_bytes()==v for n,v in old.items())
        assert len(attempts)==2 and attempts[-1]==records
    else:
        report=x.apply(u,bundle,r,records)
        assert report['upgraded']and report['databases_written']is False
        assert set(p.name for p in u.DEST.iterdir())==x.CHANGED
    assert ledger.read_bytes().startswith(b'initial ledger\n')
    assert ledger.read_bytes().count(b'new durable row')==(2 if fail else 1)
    assert all(call[-1]=='broker-only'for call in calls if isinstance(call,list))
    evidence=next(u.STATE.parent.glob('rollback-image-*/upgrade-evidence.json'));saved=load(evidence)
    assert saved['upgraded']is(not fail)


def test_changed_inputs_after_broker_stop_leave_old_files_and_restart(tmp_path,monkeypatch):
    u,bundle,r,records,old,ledger,calls,attempts=rehearsal(tmp_path,monkeypatch)
    monkeypatch.setattr(x,'installed',lambda *a:{})
    with pytest.raises(RuntimeError,match='upgrade_inputs_changed'):x.apply(u,bundle,r,records)
    assert all((u.DEST/n).read_bytes()==v for n,v in old.items())
    assert attempts==[records]and calls[-1][-2:]==['start','broker-only']


def test_reused_restore_keeps_later_ledger_data(tmp_path):
    dest=tmp_path/'dest';dest.mkdir();saved=tmp_path/'backup';saved.mkdir()
    for n in x.CHANGED:(dest/n).write_bytes(('original '+n).encode())
    u=SimpleNamespace(DEST=dest,root_file=root_file,atomic=lambda p,raw,*a,**kw:p.write_bytes(raw))
    after={n:('replacement '+n).encode()for n in x.CHANGED};manifest=x.snapshot(u,saved,after)
    for n,raw in after.items():(dest/n).write_bytes(raw)
    ledger=tmp_path/'database';ledger.write_bytes(b'new generation accepted after upgrade')
    x.restore(u,saved,manifest)
    assert all((dest/n).read_bytes()==('original '+n).encode()for n in x.CHANGED)
    assert ledger.read_bytes()==b'new generation accepted after upgrade'


def test_new_company_connector_store_during_restart_is_rolled_back(tmp_path,monkeypatch):
    real_guard=x.application_guard
    app_state,database,authority=application(tmp_path,monkeypatch)
    company=app_state/'environments'/'second';company.mkdir(parents=True,mode=0o700)
    with sqlite3.connect(app_state/'access.sqlite')as db:db.execute("INSERT INTO environments VALUES('second')")
    u,bundle,r,records,old,ledger,calls,attempts=rehearsal(tmp_path,monkeypatch)
    u.pwd=authority.pwd
    monkeypatch.setattr(x,'application_guard',real_guard)
    original_ready=x.ready
    def ready(u,value):
        result=original_ready(u,value)
        if value['custom-commissioning.json']['image']=='new':
            folder=company/'custom-connectors';folder.mkdir(mode=0o700)
            with sqlite3.connect(folder/'connectors.sqlite'):pass
        return result
    monkeypatch.setattr(x,'ready',ready)
    with pytest.raises(RuntimeError,match='application_connector_store_created_during_upgrade'):
        x.apply(u,bundle,r,records)
    assert all((u.DEST/n).read_bytes()==raw for n,raw in old.items())
    evidence=load(next(u.STATE.parent.glob('rollback-image-*/upgrade-evidence.json')))
    assert evidence['upgraded']is False and evidence['rolled_back']is True
    assert ledger.read_bytes().count(b'new durable row')==2


@pytest.mark.parametrize('status,expires,blocked',[
    ('passed',4102444800,True),('pending',0,True),('passed',0,False),('failed',4102444800,False),
])
def test_old_app_promotable_test_receipts_gate_image_upgrade(tmp_path,monkeypatch,status,expires,blocked):
    _,database,u=application(tmp_path,monkeypatch)
    with sqlite3.connect(database)as db:
        db.execute('CREATE TABLE custom_execution_tests(status TEXT, expires REAL)')
        db.execute('INSERT INTO custom_execution_tests VALUES(?,?)',(status,expires))
    before=database.read_bytes()
    if blocked:
        with pytest.raises(RuntimeError,match='promotable_custom_tests_require_expiry_or_explicit_retirement:1'):
            with x.application_guard(u,lock=True):pytest.fail('Old test could still promote.')
    else:
        with x.application_guard(u,lock=True)as counts:assert counts['promotable_custom_tests']==0
    assert database.read_bytes()==before


def test_cli_rejects_active_jobs_before_stopping_or_replacing(tmp_path,monkeypatch):
    u,bundle,r,records,old,ledger,calls,attempts=rehearsal(tmp_path,monkeypatch)
    u.canonical_bundle=lambda p:p
    def occupied():raise RuntimeError('active_custom_jobs.sqlite3')
    u.quiescent=occupied
    monkeypatch.setattr(x,'load_common',lambda:u)
    monkeypatch.setattr(x.os,'geteuid',lambda:0)
    monkeypatch.setattr(x.socket,'gethostname',lambda:'rocoto-staging')
    monkeypatch.setattr(sys,'argv',['upgrade_image','--bundle',str(bundle),'--apply'])
    old_mask=os.umask(0o022)
    try:
        with pytest.raises(RuntimeError,match='active_custom_jobs'):x.main()
    finally:os.umask(old_mask)
    assert calls==[] and attempts==[]
    assert all((u.DEST/n).read_bytes()==raw for n,raw in old.items())
