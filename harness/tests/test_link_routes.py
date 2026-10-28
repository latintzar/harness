import asyncio
import json
from pathlib import Path
import sys
import os
import time
import pytest
from fastapi.testclient import TestClient
CANDIDATE=Path(__file__).parents[1]
BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',CANDIDATE))
sys.path[:0]=[str(CANDIDATE),str(BASE),str(BASE/'tests')]
import source_service
import server
from link_intake import LinkIntake,member_current
from test_link_intake import Transport,VIDEO
from test_server_access import fixture_app
from test_workflow_routes import login
HEADERS={"X-Rocoto-Page":"1"}
from test_source_host_routes import commission_fixture

BODY={'collection':'cafe','url':'https://www.dropbox.com/scl/fi/abc/clip.mp4?rlkey=private-link-secret&dl=0','idempotency_key':'one'}

def setup(tmp_path,monkeypatch,*,commission=True):
 transport=Transport()
 import link_intake
 monkeypatch.setattr(link_intake,'PublicDownload',lambda:transport)
 app=fixture_app(tmp_path,monkeypatch)
 if commission:commission_fixture(tmp_path/'state')
 return app,transport

def wait(client,identity,status):
 for _ in range(100):
  response=client.get('/api/source-links/'+identity)
  if response.status_code==200 and response.json()['link']['status']in status:return response.json()['link']
  time.sleep(.02)
 raise AssertionError(response.text)

def test_real_app_admission_projection_reload_and_no_paid_worker(tmp_path,monkeypatch):
 app,transport=setup(tmp_path,monkeypatch)
 def forbidden(*args,**kwargs):raise AssertionError('provider worker constructed')
 monkeypatch.setattr(source_service,'SourceService',forbidden);monkeypatch.setattr(source_service,'SourceEmbedding',forbidden)
 with TestClient(app)as client:
  login(client,app,'alice')
  assert client.get('/api/source-links').json()['intake']['can_create']
  response=client.post('/api/source-links',json=BODY,headers=HEADERS);assert response.status_code==202,response.text
  assert'private-link-secret'not in response.text
  identity=response.json()['link']['id'];row=wait(client,identity,{'received'})
  assert row['import']['status']=='received'and row['import']['stage']=='prepare'and 'object_id'not in json.dumps(row)
  assert client.get('/api/library/imports').json()['imports'][0]['id']==row['import_id']
  assert client.post('/api/source-links',json=BODY,headers=HEADERS).json()['link']['id']==identity
  assert not client.get('/api/source-links').json()['intake']['can_create']
  assert client.delete('/api/source-links/'+identity,headers=HEADERS).json()['link']['import']['status']=='cancelled'
 assert len(transport.data)==len(VIDEO)and transport.calls==1
 assert not list((tmp_path/'state').glob('**/embedding-*/attempt.json'))


def test_actual_http_owner_company_observer_and_csrf(tmp_path,monkeypatch):
 app,transport=setup(tmp_path,monkeypatch);app.state.access.create_environment('other-company','Other')
 with TestClient(app)as alice,TestClient(app)as bob,TestClient(app)as observer,TestClient(app)as other:
  assert alice.get('/api/source-links').status_code==401
  login(alice,app,'alice');login(bob,app,'bob');login(observer,app,'owner',owner=True)
  other.post('/api/session',json={'code':app.state.access.issue('other-company',principal_id='alice')},headers=HEADERS)
  body={**BODY,'url':'https://drive.google.com/file/d/example/view?resourcekey=private-link-secret'}
  assert alice.post('/api/source-links',json=body).status_code==403
  assert alice.post('/api/source-links',json=body,headers={**HEADERS,'Origin':'https://other.example.com'}).status_code==403
  response=alice.post('/api/source-links',json=body,headers=HEADERS);assert response.status_code==202,response.text
  row=response.json()['link'];assert row['status']=='needs_action'
  path='/api/source-links/'+row['id']
  for stranger in (bob,other):
   assert stranger.get(path).status_code==404
   assert stranger.get('/api/source-links').json()['links']==[]
   assert stranger.delete(path,headers=HEADERS).status_code==404
  observer.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS)
  assert observer.get(path).status_code==200
  assert not observer.get('/api/source-links').json()['intake']['can_manage']
  assert observer.post('/api/source-links',json=body,headers=HEADERS).status_code==403
  assert observer.post(path+'/retry',json={},headers=HEADERS).status_code==403
  assert observer.delete(path,headers=HEADERS).status_code==403
  assert transport.calls==0


def test_disabled_malformed_private_body_and_host_collection_validation(tmp_path,monkeypatch):
 app,transport=setup(tmp_path,monkeypatch,commission=False)
 with TestClient(app)as client:
  login(client,app,'alice');assert not client.get('/api/source-links').json()['intake']['available']
  assert client.post('/api/source-links',json=BODY,headers=HEADERS).status_code==503
  commission_fixture(tmp_path/'state')
  for body in [{**BODY,'company':'evil'},{**BODY,'url':{'private':'secret'}}]:
   result=client.post('/api/source-links',json=body,headers=HEADERS);assert result.status_code==400 and 'secret'not in result.text
  result=client.post('/api/source-links',json={**BODY,'collection':'another'},headers=HEADERS);assert result.status_code==404
  result=client.post('/api/source-links',content='{"url":"private-secret","url":"other"}',headers={**HEADERS,'Content-Type':'application/json'});assert result.status_code==400 and 'private-secret'not in result.text
  result=client.post('/api/source-links',content='x'*17000,headers={**HEADERS,'Content-Type':'application/json'});assert result.status_code==413
 assert transport.calls==0


def test_membership_fence_is_company_bound_and_survives_logout(tmp_path,monkeypatch):
 app,_=setup(tmp_path,monkeypatch);state=tmp_path/'state'
 with TestClient(app)as client:
  login(client,app,'alice')
  assert member_current(state,state,'demo','alice')
  assert not member_current(state,state,'other','alice')
  client.delete('/api/session',headers=HEADERS)
  assert member_current(state,state,'demo','alice')
  with app.state.access.connect()as db:db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
  assert not member_current(state,state,'demo','alice')


def test_http_stop_reports_202_during_foreign_lease_then_200_after_cleanup(tmp_path,monkeypatch):
 import fcntl
 import link_intake
 from source_intake import SourceStore
 monkeypatch.setattr(link_intake,'STOP_WAIT_SECONDS',.025)
 app,_=setup(tmp_path,monkeypatch)
 with TestClient(app)as client:
  login(client,app,'alice')
  result=client.post('/api/source-links',json={**BODY,'url':'https://we.tl/synthetic'},headers=HEADERS)
  identity=result.json()['link']['id'];path='/api/source-links/'+identity
  fd=SourceStore._open(tmp_path/'state/source-links'/(identity+'.lock'),os.O_RDWR|os.O_CREAT)
  try:
   fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
   pending=client.delete(path,headers=HEADERS)
   assert pending.status_code==202 and pending.json()['link']['status']=='cancelling'
  finally:os.close(fd)
  stopped=client.delete(path,headers=HEADERS)
  assert stopped.status_code==200 and stopped.json()['link']['status']=='cancelled'
