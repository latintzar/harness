import copy,hashlib,importlib.util,json,os,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
spec=importlib.util.spec_from_file_location('compat_upgrade',Path(__file__).parents[1]/'deploy/native-executor/upgrade_installer_compat.py');upgrade=importlib.util.module_from_spec(spec);spec.loader.exec_module(upgrade)

def fixtures(tmp_path):
 bundle=tmp_path/'bundle';bundle.mkdir()
 release={'schema_version':1,'image':'image','runtime':'runtime','previous_sources':upgrade.PREVIOUS,'sources':upgrade.CURRENT,'proof_sha256':'proof'}
 proof={'private_candidate':True,'installed_service_changed':False,'image_digest':'image','runtime':'runtime','all_jobs_terminal':True,'remaining_native_containers':0,'model_turns':0,'model_requests':0,'provider_requests':0,'upstream_scripts_executed':0,'supported_plugin_count':50,'removed':True,'other_owner_empty':True,'source_sha256':[{'path':n,'sha256':h}for n,h in upgrade.REPLACEMENTS.items()],'installer_worker_sha256':upgrade.REPLACEMENTS['native_install_worker.py'],'native_skills':[{'name':'qodo:'+n,'enabled':True,'plugin_id':'qodo@rocoto-pinned-api'}for n in ['qodo-review','qodo-review-resolver','qodo-setup','qodo-codebase-wisdom']]}
 loaded={'compat-release.json':release,'compat-proof.json':proof}
 u=SimpleNamespace(IMAGE='image',RUNTIME='runtime',root_file=lambda p,h:None,load=lambda p,h=None:loaded[p.name])
 return bundle,u,release,proof

def test_proof_and_fixed_source_closure_match(tmp_path):
 b,u,r,p=fixtures(tmp_path);assert upgrade.bundle_record(u,b)==r

@pytest.mark.parametrize('fault',['source','skill','write','cleanup','worker'])
def test_wrong_actual_proof_rejected_before_any_mutation(tmp_path,fault):
 b,u,r,p=fixtures(tmp_path)
 if fault=='source':p['source_sha256'][0]['sha256']='0'*64
 elif fault=='skill':p['native_skills'].pop()
 elif fault=='write':p['provider_requests']=1
 elif fault=='cleanup':p['all_jobs_terminal']=False
 else:p['installer_worker_sha256']='0'*64
 with pytest.raises(RuntimeError):upgrade.bundle_record(u,b)

@pytest.mark.parametrize('fail_start',[False,True])
def test_four_file_commit_or_rollback_keeps_later_database_rows(tmp_path,monkeypatch,fail_start):
 dest=tmp_path/'installed';dest.mkdir();state=tmp_path/'state';state.mkdir();bundle=tmp_path/'bundle';bundle.mkdir()
 before={n:(n+'-original').encode()for n in upgrade.CHANGED}
 for n,raw in before.items():(dest/n).write_bytes(raw)
 db=state/'leases.sqlite3';db.write_bytes(b'existing-ledger\n')
 records={'installer-commissioning.json':{'sources':{k:v for k,v in upgrade.PREVIOUS.items()if k!='custom_worker.py' and k!='native_custom_broker.py'}},'custom-commissioning.json':{'sources':{k:upgrade.PREVIOUS[k]for k in ['native_executor.py','native_package_mount.py','native_custom_broker.py','custom_worker.py','broker_service.py']}}}
 replacements={n:('verified '+n).encode()for n in upgrade.REPLACEMENTS};hashes={n:hashlib.sha256(raw).hexdigest()for n,raw in replacements.items()}
 for n,raw in replacements.items():(bundle/n).write_bytes(raw)
 monkeypatch.setattr(upgrade,'REPLACEMENTS',hashes);monkeypatch.setattr(upgrade,'CURRENT',upgrade.PREVIOUS|hashes)
 seen=[];starts=0
 def run(args):
  nonlocal starts
  seen.append(args)
  if args[1]=='is-active':return 'active'
  if args[1]=='start':
   starts+=1
   with db.open('ab') as f:f.write(b'later-durable-row\n')
  return ''
 def atomic(path,raw,mode=0o644,uid=0,gid=0):path.write_bytes(raw)
 def root_file(path,checksum=None):
  assert path.is_file()
  if checksum:assert hashlib.sha256(path.read_bytes()).hexdigest()==checksum
 control=tmp_path/'control';control.touch()
 u=SimpleNamespace(DEST=dest,STATE=state,SERVICE='native-only',CONTROL=control,IMAGE='image',RUNTIME='runtime',canonical_bundle=lambda p:p,quiescent=lambda:seen.append('idle'),service_pids=lambda:{'app':'1','media':'2','docker':'3'},run=run,root_file=root_file,atomic=atomic)
 monkeypatch.setattr(upgrade,'load_common',lambda:u);monkeypatch.setattr(upgrade,'installed',lambda u:copy.deepcopy(records))
 release={'proof_sha256':'p'};monkeypatch.setattr(upgrade,'bundle_record',lambda u,b:release)
 def ready(u,updated):
  assert all((dest/n).read_bytes()==raw for n,raw in replacements.items())
  assert all(json.loads((dest/n).read_bytes())==v for n,v in updated.items())
  if fail_start:raise RuntimeError('synthetic_readiness_failure')
  return {'verified':True}
 monkeypatch.setattr(upgrade,'ready',ready);monkeypatch.setattr(upgrade.os,'getuid',lambda:0);monkeypatch.setattr(upgrade.socket,'gethostname',lambda:'rocoto-staging');monkeypatch.setattr(sys,'argv',['upgrade','--bundle',str(bundle),'--apply'])
 old=os.umask(0o022)
 try:
  if fail_start:
   with pytest.raises(RuntimeError,match='synthetic_readiness'):upgrade.main()
   assert all((dest/n).read_bytes()==raw for n,raw in before.items())
  else:upgrade.main()
 finally:os.umask(old)
 assert db.read_bytes().startswith(b'existing-ledger\n') and db.read_bytes().count(b'later-durable-row')==starts
 assert all(args[-1]=='native-only'for args in seen if isinstance(args,list))
 assert seen.count('idle')>=3
