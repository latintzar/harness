import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from test_server_access import fixture_app
from test_workflow_routes import HEADERS,login
from test_storage_connections import fixture as setup,ready,SCOPE,propose
from agent_bridge import CodexVideoAgent,tool_specs
from storage_runtime import StorageRuntime


def test_application_owner_scope_observer_csrf_and_default_closed(tmp_path,monkeypatch):
 app=fixture_app(tmp_path,monkeypatch)
 store,sources,authority,key,package,target=setup(tmp_path/'state')
 proposal=propose(authority,key,target);route=f'/api/connections/custom/{key}/storage'
 with TestClient(app)as alice,TestClient(app)as bob,TestClient(app)as observer:
  assert alice.get(route).status_code==401
  login(alice,app,'alice');login(bob,app,'bob');login(observer,app,'peter',owner=True)
  assert bob.get(route).status_code==404
  assert bob.post(route+'/approve',json={'proposal_id':proposal['id']},headers=HEADERS).status_code==404
  value=alice.get(route);assert value.status_code==200
  assert value.headers['cache-control']=='private, no-store'
  assert value.json()['status']=='setup'and not value.json()['runtime_available']
  assert alice.post(route+'/approve',json={'proposal_id':proposal['id']}).status_code==403
  assert alice.post(route+'/approve',json={'proposal_id':proposal['id'],'owner':'bob'},headers=HEADERS).status_code==400
  assert alice.post(route+'/approve',json={'proposal_id':proposal['id']},headers=HEADERS).json()['status']=='configured'
  assert alice.post(route+'/run',json={'expected_generation':1},headers=HEADERS).status_code==409
  embedded=alice.get(f'/api/connections/custom/{key}').json()
  assert embedded['storage_setup']['status']=='configured'
  assert observer.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS).status_code==200
  public=observer.get(route).json();assert public['can_run']is False and public['can_pause']is False
  for action,body in [('approve',{'proposal_id':proposal['id']}),('run',{'expected_generation':1}),('pause',{'expected_generation':1})]:
   assert observer.post(route+'/'+action,json=body,headers=HEADERS).status_code==403
  assert alice.post(route+'/pause',json={'expected_generation':1},headers=HEADERS).json()['status']=='paused'
 assert sources.list_imports(__import__('source_intake').IntakeActor('alice'))==[]


def test_model_proposal_and_status_are_owner_bound_and_grant_no_acquisition(tmp_path):
 store,sources,authority,key,package,target=setup(tmp_path)
 agent=CodexVideoAgent(tmp_path,object(),lambda:{})
 agent.custom_connector_store=store
 agent.children['setup']={'id':'setup','owner_principal_id':'alice','dataset':'shop'}
 specs={x['name']:x for x in tool_specs()}
 assert {'propose_storage_connection','get_storage_connection'}<=set(specs)
 assert 'propose_storage_connection'not in {x['name']for x in tool_specs(False)}
 async def scenario():
  args={'connector_id':key,'expected_revision':2,'target':target,'reason':'Read the recording archive.'}
  result,_,ok=await agent._tool('propose_storage_connection',args,scope='shop',chat_id='setup')
  assert ok and json.loads(result[0]['text'])['status']=='proposed'
  result,_,ok=await agent._tool('get_storage_connection',{'connector_id':key},scope='shop',chat_id='setup')
  value=json.loads(result[0]['text']);assert ok and value['status']=='setup'and not value['runtime_available']
  result,_,ok=await agent._tool('get_custom_connector',{'connector_id':key},scope='shop',chat_id='setup')
  assert ok and json.loads(result[0]['text'])['storage_setup']['status']=='setup'
  for changes in ({'parent_id':'parent'},{'owner_principal_id':'bob'},{'dataset':'other'}):
   original=dict(agent.children['setup']);agent.children['setup'].update(changes)
   _,_,ok=await agent._tool('propose_storage_connection',args,scope='shop',chat_id='setup')
   assert not ok;agent.children['setup']=original
  assert agent._storage_runtime().runs.records()==[]
 asyncio.run(scenario())


def test_app_and_storage_setup_metadata_load_without_optional_asyncssh(tmp_path):
 # A published app checkpoint can precede commissioning of the SSH dependency.
 candidate=Path(__file__).parents[1]
 script='''
import layout
layout.install()
import importlib.abc,sys
class Absent(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='asyncssh' or fullname.startswith('asyncssh.'):raise ModuleNotFoundError('optional worker dependency')
sys.meta_path.insert(0,Absent())
import server,storage_runtime
from custom_connectors import CustomConnectors
from source_intake import SourceStore
from storage_connections import StorageConnections
from pathlib import Path
state=Path(sys.argv[1]);store=CustomConnectors(state)
key=store.create(name='Archive',description='Set up stored recordings.',kind='sftp',collection='shop',owner='alice')['id']
value=storage_runtime.StorageRuntime(StorageConnections(store,SourceStore(state))).describe(key,owner='alice',collection='shop')
assert value['runtime_available']is False and value['status']=='setup'
print('optional-dependency-closed')
'''
 result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'state')],cwd=candidate,
                       capture_output=True,text=True,timeout=30)
 assert result.returncode==0,result.stderr
 assert result.stdout.strip()=='optional-dependency-closed'
