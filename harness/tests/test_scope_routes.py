import sys
from pathlib import Path
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).parents[1]))
from business_scope import ScopeActor
from retail_business_scope import RetailBusinessScope as BusinessScope
from retail_scope_routes import register_scope_routes
from test_business_scope import fixture

HEADERS={'x-rocoto-page':'1'}

def app_fixture(tmp_path,*,configured=True):
    if configured:store,owner,members,save,keys=fixture(tmp_path)
    else:
        members={'owner':{'id':'owner','role':'owner','display_name':'Owner'}}
        store=BusinessScope(tmp_path,company_id='company',member=members.get,members=lambda:list(members.values()))
        owner=ScopeActor('company','owner');save=keys=None
    chats={'chat':{'id':'chat','dataset':'business','owner':'owner'}}
    app=FastAPI()
    @app.middleware('http')
    async def session(request,call_next):
        principal=request.headers.get('x-person','owner');member=members.get(principal)
        request.state.session={'authenticated':member is not None,'role':member['role'] if member else 'browse',
            'can_query':member is not None and member['role']!='browse','viewing_as':request.headers.get('x-observe')=='1'}
        request.state.workspace={'company':'company'};request.state.person=principal
        return await call_next(request)
    def actor(request,write=False):return ScopeActor('company',request.state.person,request.state.session['viewing_as'])
    def collection(request,selected):
        if selected!='business':raise HTTPException(404,'collection_unavailable')
        return selected
    def chat(request,key,write=False):
        record=chats.get(key)
        if not record or record['owner']!=request.state.person:raise HTTPException(404,'chat_unavailable')
        return record
    register_scope_routes(app,store_for_workspace=lambda _:store,actor_for_request=actor,
        collection_for_request=collection,chat_for_request=chat)
    return TestClient(app),store,owner,members,save,keys,chats


def test_http_absent_reads_and_rejections_create_no_store(tmp_path):
    client,store,*_=app_fixture(tmp_path,configured=False)
    read=client.get('/api/business/scope',params={'collection':'business'})
    assert read.status_code==200 and read.json()['revision']==0
    assert read.headers['cache-control']=='private, no-store'
    assert client.get('/api/business/scope',params={'collection':'business'},headers={'x-person':'missing'}).status_code==401
    assert client.get('/api/business/scope',params={'collection':'foreign'}).status_code==404
    assert client.post('/api/business/scope/locations',json={'collection':'business','expected_revision':0,'value':{'name':'A'}}).status_code==403
    assert not list(tmp_path.iterdir())


def test_http_writes_cas_history_and_owner_observation(tmp_path):
    client,store,owner,members,save,k,_=app_fixture(tmp_path)
    revision=store.effective(owner,'business')['revision']
    body={'collection':'business','expected_revision':revision,'value':{'name':'Customer service'}}
    response=client.post('/api/business/scope/functions',json=body,headers=HEADERS)
    assert response.status_code==200 and response.json()['revision']==revision+1
    assert client.post('/api/business/scope/functions',json=body,headers=HEADERS).status_code==409
    body['expected_revision']+=1
    assert client.post('/api/business/scope/functions',json=body,headers={**HEADERS,'x-observe':'1'}).status_code==403
    assert client.post('/api/business/scope/functions',content='{"collection":"business","collection":"other"}',headers={**HEADERS,'content-type':'application/json'}).status_code==400
    events=client.get('/api/business/scope/events',params={'collection':'business'}).json()['events']
    assert events[-1]['kind']=='functions' and events[-1]['actor']=='owner'
    assert client.get('/api/business/scope/events',params={'collection':'business'},headers={'x-person':'alice'}).status_code==403


def test_http_member_selection_is_host_chat_bound_and_revoke_is_immediate(tmp_path):
    client,store,owner,members,save,k,chats=app_fixture(tmp_path)
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]})
    chats['alice-chat']={'id':'alice-chat','dataset':'business','owner':'alice'}
    headers={**HEADERS,'x-person':'alice'}
    revision=store.effective(owner,'business')['revision']
    body={'location_ids':[k['a']],'expected_revision':revision,'expected_selection_revision':0}
    assert client.put('/api/chats/chat/scope',json=body,headers=headers).status_code==404
    reply=client.put('/api/chats/alice-chat/scope',json=body,headers=headers)
    assert reply.status_code==200 and reply.json()['source_ids']==['owned-one'] and reply.json()['selection_revision']==1
    assert client.put('/api/chats/alice-chat/scope',json=body,headers=headers).status_code==409
    body.update(location_ids=[k['b']],expected_selection_revision=1)
    assert client.put('/api/chats/alice-chat/scope',json=body,headers=headers).status_code==403
    save('members',{'member_id':'alice','role':'alert_recipient','location_ids':[k['a']],'function_ids':[]})
    denied=client.get('/api/chats/alice-chat/scope',headers=headers).json()
    assert denied['source_ids']==[] and denied['can_query'] is False
    members.pop('alice')
    assert client.get('/api/chats/alice-chat/scope',headers=headers).status_code==401


@pytest.mark.parametrize('value',[{'room_ids':[{}]},{'room_ids':[1]},{'room_ids':'wrong'}])
def test_invalid_nested_types_are_neutral_400(tmp_path,value):
    client,store,owner,_,_,k,_=app_fixture(tmp_path)
    body={'collection':'business','expected_revision':store.effective(owner,'business')['revision'],
        'value':{'name':'Camera','location_id':k['a'],'source_ids':[],**value}}
    assert client.post('/api/business/scope/cameras',json=body,headers=HEADERS).status_code==400
