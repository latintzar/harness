"""Validate immutable upgrade inputs and idle/rollback boundaries, without services."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import pytest


def module():
 path=Path(__file__).parents[1]/'deploy/native-executor/upgrade_custom.py'
 spec=importlib.util.spec_from_file_location('custom_upgrade_fixture',path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def write(path,value):path.write_text(json.dumps(value));return hashlib.sha256(path.read_bytes()).hexdigest()

@pytest.fixture
def prepared(tmp_path,monkeypatch):
 u=module();folder=tmp_path/'bundle';folder.mkdir();source={}
 for name in u.NAMES:
  data=json.dumps([{'plugin_id':f'plugin-{n}'}for n in range(47)]).encode()if name.endswith('.json')else('# fixture '+name+'\n').encode()
  (folder/name).write_bytes(data);source[name]=hashlib.sha256(data).hexdigest()
 broker={'status':'passed','provider_calls':0,'model_calls':0,'image':u.IMAGE,'runtime':u.RUNTIME,'installed_service_changed':False,
  'remaining_jobs':0,'remaining_volumes':[],'remaining_containers':'','interrupted_upload_restart_cleanup':True,'source_sha256':source,
  'checks':{'retired_request_fenced':True,'stopped_job':{'whole_worker_stop':True},'successful_job':{'worker_sha256':source['custom_worker.py'],'cleanup_confirmed':True,
   'result':{'uid':10001,'host_hidden':True,'network_denied':True,'source_readonly':True,'quota_written_bytes':48*1024*1024}}}}
 coordinator={**{k:broker[k]for k in('status','provider_calls','model_calls','image','runtime','installed_service_changed','remaining_jobs','remaining_volumes','remaining_containers')},
  'sources':[{'path':k,'sha256':v}for k,v in source.items()],
  'result':{'status':'passed','provider_calls':0,'model_calls':0,'runtime':{'profile':u.PROFILE,'image_digest':u.IMAGE,'worker_sha256':source['custom_worker.py']},
   'checks':{k:True for k in('wrong_owner_denied','verified_test_then_active_invocation','fabricated_401_success_denied','whole_worker_stop_preserves_active_target','host_auth_sni_and_receipts')}}}
 record={'schema_version':1,'profile':u.PROFILE,'image':u.IMAGE,'runtime':u.RUNTIME,'sources':source,
  'proof_sha256':write(folder/'proof-evidence.json',broker),'coordinator_proof_sha256':write(folder/'coordinator-evidence.json',coordinator)}
 write(folder/'custom-release.json',record)
 # Ownership is tested independently; ordinary CI exercises byte/proof checks
 # using caller-owned isolated fixtures rather than changing system ownership.
 monkeypatch.setattr(u,'canonical_bundle',lambda path:path.resolve(strict=True))
 def fixture_file(path,checksum=None):
  assert path.is_file()and not path.is_symlink()
  if checksum is not None:u.require(u.digest(path)==checksum,'source_checksum_changed')
 monkeypatch.setattr(u,'root_file',fixture_file)
 return u,folder,record,broker,coordinator


def test_matching_source_and_actual_proof_contract_is_accepted(prepared):
 u,folder,record,_,_=prepared
 assert u.bundle_record(folder)==record

@pytest.mark.parametrize('change',['source','image','extra_source','worker','broker_cleanup','broker_calls','retirement','coordinator_owner','coordinator_calls','coordinator_source','duplicate_source'])
def test_wrong_proof_or_source_fails_before_any_mutation(prepared,change):
 u,folder,record,broker,coordinator=prepared
 if change=='source':(folder/'native_executor.py').write_text('# changed\n')
 elif change=='image':record['image']='sha256:'+'f'*64
 elif change=='extra_source':record['sources']['outside.py']='0'*64
 elif change=='worker':broker['checks']['successful_job']['worker_sha256']='0'*64
 elif change=='broker_cleanup':broker['remaining_jobs']=1
 elif change=='broker_calls':broker['provider_calls']=1
 elif change=='retirement':broker['checks']['retired_request_fenced']=False
 elif change=='coordinator_owner':coordinator['result']['checks']['wrong_owner_denied']=False
 elif change=='coordinator_calls':coordinator['result']['model_calls']=1
 elif change=='coordinator_source':next(row for row in coordinator['sources'] if row['path']=='native_custom_broker.py')['sha256']='0'*64
 elif change=='duplicate_source':coordinator['sources'].append(coordinator['sources'][0])
 record['proof_sha256']=write(folder/'proof-evidence.json',broker);record['coordinator_proof_sha256']=write(folder/'coordinator-evidence.json',coordinator)
 write(folder/'custom-release.json',record)
 with pytest.raises(RuntimeError):u.bundle_record(folder)

@pytest.mark.parametrize('boundary',['native','installer','custom','container'])
def test_active_or_uncertain_job_blocks_upgrade(tmp_path,monkeypatch,boundary):
 u=module();monkeypatch.setattr(u,'STATE',tmp_path);monkeypatch.setattr(u,'root_file',lambda path,checksum=None:None)
 for relative,table,terminal,label in [('leases.sqlite3','leases','stopped','native'),('installer/jobs.sqlite3','jobs','released','installer'),('custom/jobs.sqlite3','jobs','released','custom')]:
  path=tmp_path/relative;path.parent.mkdir(parents=True,exist_ok=True)
  with sqlite3.connect(path)as db:db.execute('CREATE TABLE '+table+'(status TEXT)');db.execute('INSERT INTO '+table+' VALUES(?)',('uncertain'if boundary==label else terminal,))
 monkeypatch.setattr(u,'run',lambda argv:'rocoto-native-custom-fixture'if boundary=='container'else'')
 with pytest.raises(RuntimeError,match='active_|container_present'):u.quiescent()


def test_root_source_rejects_symlinks_even_to_valid_content(tmp_path):
 u=module();target=tmp_path/'source';target.write_text('source');link=tmp_path/'alias';link.symlink_to(target)
 with pytest.raises(RuntimeError,match='invalid_root_owned_source'):u.root_file(link)


def test_installer_refresh_preserves_original_catalogue_and_historical_proof(prepared,tmp_path,monkeypatch):
 u,folder,record,_,_=prepared;dest=tmp_path/'installed';dest.mkdir();monkeypatch.setattr(u,'DEST',dest)
 previous={'schema_version':1,'profile':u.INSTALL_PROFILE,'image':u.IMAGE,'runtime':u.RUNTIME,
  'sources':{name:record['sources'][name]for name in u.INSTALL_NAMES},'proof_sha256':'e'*64,'at':'historical-installer'}
 for name in u.INSTALL_NAMES:(dest/name).write_bytes((folder/name).read_bytes())
 checksum=write(dest/'installer-commissioning.json',previous)
 monkeypatch.setattr(u,'OLD',{'installer-commissioning.json':checksum})
 unit=tmp_path/'unit';unit.write_text('unit');monkeypatch.setattr(u,'UNIT',unit);monkeypatch.setattr(u,'UNIT_SHA',u.digest(unit));monkeypatch.setattr(u,'run',lambda _:u.IMAGE)
 actual,catalogue=u.installed(record,folder);assert actual==previous and len(catalogue)==47
 record['sources']['installer-sources.json']='f'*64
 with pytest.raises(RuntimeError,match='installer_identity_changed'):u.installed(record,folder)

@pytest.mark.skipif(os.geteuid()!=0,reason='rollback checks real root directory identities and ownership')
def test_rollback_restores_sources_all_databases_and_preserves_new_durable_rows(tmp_path,monkeypatch):
 u=module();dest=tmp_path/'installed';dest.mkdir();state=tmp_path/'state';state.mkdir();unit=tmp_path/'unit';unit.write_text('same-unit')
 monkeypatch.setattr(u,'DEST',dest);monkeypatch.setattr(u,'STATE',state);monkeypatch.setattr(u,'UNIT',unit)
 for name in u.NAMES|u.RECORDS:(dest/name).write_text('old-'+name)
 for relative in u.DATABASES:
  p=state/relative;p.parent.mkdir(exist_ok=True)
  with sqlite3.connect(p)as db:db.execute('CREATE TABLE saved(value TEXT)');db.execute('INSERT INTO saved VALUES(?)',(relative,))
 rollback=tmp_path/'rollback';rollback.mkdir(mode=0o700);saved=u.snapshot(rollback)
 for name in u.NAMES|u.RECORDS:(dest/name).write_text('new-'+name)
 with sqlite3.connect(state/'leases.sqlite3')as db:db.execute("INSERT INTO saved VALUES('later durable row')")
 preserved=u.restore(rollback,saved);assert preserved==['leases.sqlite3']
 for name in u.NAMES|u.RECORDS:assert(dest/name).read_text()=='old-'+name
 for relative in u.DATABASES:
  with sqlite3.connect(state/relative)as db:values=db.execute('SELECT value FROM saved').fetchall()
  assert values==[(relative,),('later durable row',)]if relative=='leases.sqlite3'else values==[(relative,)]
 assert unit.read_text()=='same-unit'
 for p,info in saved['directories'].items():assert Path(p).stat().st_ino==info['inode']


def test_canonical_bundle_is_pinned_before_validation_alias_changes(prepared,tmp_path,monkeypatch,capsys):
 from types import SimpleNamespace
 u,folder,record,_,_=prepared;replacement=tmp_path/'other';replacement.mkdir();alias=tmp_path/'alias';alias.symlink_to(folder)
 monkeypatch.setattr(u.argparse.ArgumentParser,'parse_args',lambda _:SimpleNamespace(bundle=alias,apply=False))
 monkeypatch.setattr(u.os,'geteuid',lambda:0);monkeypatch.setattr(u.socket,'gethostname',lambda:'rocoto-staging')
 def checked(path):
  assert path==folder.resolve();alias.unlink();alias.symlink_to(replacement);return record
 monkeypatch.setattr(u,'bundle_record',checked);monkeypatch.setattr(u,'installed',lambda rec,path:({},[{}]*47));monkeypatch.setattr(u,'quiescent',lambda:None)
 monkeypatch.setattr(u,'service_pids',lambda:{'fixture':'1'});monkeypatch.setattr(u,'run',lambda _: 'active')
 previous=os.umask(0o077)
 try:u.main()
 finally:os.umask(previous)
 assert json.loads(capsys.readouterr().out)['apply']is False
