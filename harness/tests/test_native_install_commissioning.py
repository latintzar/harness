import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).parents[1]/'deploy/native-executor'
def load(name):
    spec=importlib.util.spec_from_file_location('fixture_'+name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def bundle(tmp_path):
    upgrade=load('upgrade_installer')
    sources={}
    for name in upgrade.NAMES:
        payload=b'[]' if name.endswith('.json') else b'# fixture\n'
        path=tmp_path/name;path.write_bytes(payload);path.chmod(0o644)
        sources[name]=hashlib.sha256(payload).hexdigest()
    evidence={'status':'passed','model_calls':0,'image':upgrade.IMAGE,'remaining_jobs':0,'remaining_volumes':[],
      'interrupted_upload_cleanup':True,'source_sha256':sources}
    proof=tmp_path/'proof-evidence.json';proof.write_text(json.dumps(evidence));proof.chmod(0o644)
    record={'schema_version':1,'profile':upgrade.PROFILE,'runtime':'rocoto-runsc','image':upgrade.IMAGE,
      'sources':sources,'proof_sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}
    (tmp_path/'installer-release.json').write_text(json.dumps(record));(tmp_path/'installer-release.json').chmod(0o644)
    return upgrade,record

@pytest.mark.skipif(os.geteuid()!=0,reason='commissioning verifies actual root ownership')
def test_bundle_requires_exact_proven_sources_and_complete_zero_provider_cleanup(tmp_path):
    upgrade,record=bundle(tmp_path)
    assert upgrade.bundle_record(tmp_path)==record
    (tmp_path/'native_install_worker.py').write_text('# changed\n')
    with pytest.raises(RuntimeError,match='checksum'):upgrade.bundle_record(tmp_path)

@pytest.mark.skipif(os.geteuid()!=0,reason='commissioning verifies actual root ownership')
def test_service_validates_full_closure_and_profile_before_enabling(tmp_path,monkeypatch):
    service=load('broker_service');upgrade,record=bundle(tmp_path)
    observed=[]
    import native_install_broker
    monkeypatch.setattr(native_install_broker,'Installer',lambda host,**kwargs:observed.append(kwargs))
    service.configure_installer(object(),tmp_path);assert observed==[]
    receipt=tmp_path/'installer-commissioning.json';receipt.write_text(json.dumps(record));receipt.chmod(0o644)
    service.configure_installer(object(),tmp_path);assert observed==[{'allowed_sources':[]}]
    record['image']='changed';receipt.write_text(json.dumps(record))
    with pytest.raises(SystemExit,match='full native installer'):service.configure_installer(object(),tmp_path)
    assert len(observed)==1

@pytest.mark.parametrize('boundary',['native','installer','container'])
def test_upgrade_quiescence_checks_every_live_boundary(tmp_path,monkeypatch,boundary):
    upgrade=load('upgrade_installer');monkeypatch.setattr(upgrade,'STATE',tmp_path)
    with sqlite3.connect(tmp_path/'leases.sqlite3') as db:
        db.execute('CREATE TABLE leases(status TEXT)');db.execute('INSERT INTO leases VALUES(?)',('running' if boundary=='native' else 'stopped',))
    (tmp_path/'installer').mkdir()
    with sqlite3.connect(tmp_path/'installer/jobs.sqlite3') as db:
        db.execute('CREATE TABLE jobs(status TEXT)');db.execute('INSERT INTO jobs VALUES(?)',('uncertain' if boundary=='installer' else 'released',))
    monkeypatch.setattr(upgrade,'run',lambda args:'rocoto-native-fixture' if boundary=='container' else '')
    with pytest.raises(RuntimeError,match='active_|container_present'):upgrade.quiescent()


def test_upgrade_pins_canonical_bundle_before_caller_alias_changes(tmp_path,monkeypatch,capsys):
    upgrade=load('upgrade_installer')
    original=tmp_path/'reviewed';original.mkdir();(original/'installer-sources.json').write_text('[{"plugin_id":"notion"}]')
    replacement=tmp_path/'replacement';replacement.mkdir();(replacement/'installer-sources.json').write_text('[{},{}]')
    alias=tmp_path/'alias';alias.symlink_to(original,target_is_directory=True)
    arguments=SimpleNamespace(bundle=alias,apply=False)
    monkeypatch.setattr(upgrade.argparse.ArgumentParser,'parse_args',lambda _:arguments)
    monkeypatch.setattr(upgrade.os,'geteuid',lambda:0)
    monkeypatch.setattr(upgrade.socket,'gethostname',lambda:'rocoto-staging')
    monkeypatch.setattr(upgrade,'root_file',lambda *args:None)
    monkeypatch.setattr(upgrade,'quiescent',lambda:None)
    monkeypatch.setattr(upgrade,'DEST',tmp_path/'installed')
    def validate(path):
        assert path==original.resolve()
        alias.unlink();alias.symlink_to(replacement,target_is_directory=True)
        return {'sources':{}}
    monkeypatch.setattr(upgrade,'bundle_record',validate)
    def run(args):
        if 'inspect' in args:return upgrade.IMAGE
        if 'is-active' in args:return 'active'
        return '1'
    monkeypatch.setattr(upgrade,'run',run)
    previous=os.umask(0o077)
    try:upgrade.main()
    finally:os.umask(previous)
    assert json.loads(capsys.readouterr().out)['supported_plugin_count']==1
