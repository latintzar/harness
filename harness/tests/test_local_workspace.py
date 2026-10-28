"""Run the public native backend with private state and real observer authority."""
from pathlib import Path
import importlib.util,json,os,socket,stat,subprocess,sys,time
import httpx,pytest
ROOT=Path(__file__).resolve().parents[2]

def load_tool():
 spec=importlib.util.spec_from_file_location('local_workspace_dev',ROOT/'tools/dev.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_local_environment_discards_ambient_provider_and_workspace_authority(tmp_path,monkeypatch):
 monkeypatch.setenv('OPENAI_API_KEY','fixture-ambient-key');monkeypatch.setenv('ROCOTO_STATE','/unrelated/private-state');monkeypatch.setenv('ROCOTO_SESSION_COOKIE','other_company')
 env=load_tool().environment(tmp_path,True,'https://preview.example.test')
 assert 'OPENAI_API_KEY' not in env and 'ROCOTO_STATE' not in env
 assert env['ROCOTO_READ_ONLY']=='true' and env['ROCOTO_SESSION_COOKIE']=='agent_workspace_session'
 assert Path(env['HOME']).is_relative_to(tmp_path)

def test_marker_requires_owned_private_regular_file(tmp_path):
 module=load_tool();marker=tmp_path/'marker';marker.write_text('{}');marker.chmod(0o644)
 with pytest.raises(ValueError):module.private_json(marker)
 marker.chmod(0o600);assert module.private_json(marker)=={}
 symlink=tmp_path/'link';symlink.symlink_to(marker)
 with pytest.raises(ValueError):module.private_json(symlink)

def test_public_native_workspace_starter_and_read_only_observer(tmp_path):
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 state=tmp_path/'private';log=(tmp_path/'server.log').open('w+')
 process=subprocess.Popen([sys.executable,'-B',str(ROOT/'tools/dev.py'),'--guest','--state',str(state),'--port',str(port),'--base-path','/apps/agent-workspace/'],cwd=ROOT,stdout=log,stderr=log)
 try:
  with httpx.Client(base_url=f'http://127.0.0.1:{port}/apps/agent-workspace/',trust_env=False,timeout=5) as client:
   for _ in range(100):
    if process.poll() is not None:log.seek(0);pytest.fail(log.read())
    try:
     response=client.get('api/session')
     if response.status_code==200:break
    except httpx.ConnectError:pass
    time.sleep(.1)
   else:pytest.fail('Native workspace did not become ready.')
   session=response.json();assert session['authenticated'] and session['read_only'] and session['host_read_only'] and not session['can_query']
   assert session['viewing_as']['principal']['id']=='owner'
   cookie=response.headers['set-cookie'];assert 'agent_workspace_session=' in cookie and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Path=/apps/agent-workspace/' in cookie
   receipt=json.loads((state/'starter.json').read_text());rows=client.get('api/chats').json()['chats'];assert len(rows)==1 and rows[0]['id']==receipt['organization_chat'] and rows[0]['messages']==[]
   files=client.get('api/files',params={'chat_id':rows[0]['id'],'dataset':'company'}).json()['files'];assert len(files)==receipt['source_files']==6
   definition=next(row for row in files if row['name']=='bare-vertical.json');content=client.get(f"api/files/{definition['id']}/content");assert content.status_code==200 and 'Agent workspace' in content.text
   assert client.get('api/workspace/capabilities',params={'dataset':'company'}).json()['permissions']['read_only']
   assert client.get('api/plugins/catalogue').status_code==200
   for method,path,body in [('POST','api/observer',{}),('DELETE','api/observer',None),('POST','api/session',{'code':'invalid'}),('POST','api/turn',{}),('PATCH',f"api/files/{definition['id']}",{'title':'Changed'}),('POST','api/agents',{})]:
    assert client.request(method,path,json=body,headers={'X-Rocoto-Page':'1'}).status_code==403
   assert client.get(f'http://127.0.0.1:{port}/api/session').status_code==404
   assert stat.S_IMODE(state.stat().st_mode)==0o700 and stat.S_IMODE((state/'owner-code.txt').stat().st_mode)==0o600
   assert json.loads((state/'.agent-workspace-local.json').read_text())['audience']=='guest'
 finally:
  process.terminate();process.wait(timeout=20);log.close()
 assert json.loads((state/'network.json').read_text())['blocked_external_attempts']==0

def test_local_starter_refuses_nonempty_unmarked_state(tmp_path):
 state=tmp_path/'private';state.mkdir();(state/'retained-record').write_text('preserved')
 result=subprocess.run([sys.executable,'-B',str(ROOT/'tools/dev.py'),'--guest','--state',str(state)],capture_output=True,text=True,timeout=10)
 assert result.returncode!=0 and 'fresh state' in result.stderr and (state/'retained-record').read_text()=='preserved'
