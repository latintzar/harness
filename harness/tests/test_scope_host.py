from pathlib import Path
from types import SimpleNamespace
import sys
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).parents[1]))
from business_scope import ScopeActor
from chat_permissions import ChatPermissions
from scope_host import filter_view,scope_current
from retail_scope_routes import register_host_routes
from retail_scope import install_scope as setup_scope, RetailScope
from host import Collections, BareWorkspace
from source_visibility import ReadyIndex
from test_business_scope import fixture


class Index:
    def __init__(self):
        self.clips=[{'id':str(i),'video_id':video,'dataset':dataset,'start':0.,'end':1.,'camera':video}
            for i,(video,dataset) in enumerate([('owned-one','business'),('owned-wide','business'),
                ('unassigned','business'),('other-video','other')])]
        self.embeddings=np.eye(4);self.report={'private':'hidden'}
    def status(self):return {'datasets':sorted({row['dataset'] for row in self.clips})}
    def get_clip(self,key):return next((row for row in self.clips if row['id']==key),None)


def host_fixture(tmp_path):
    store,owner,members,save,k=fixture(tmp_path)
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]})
    children={'chat':{'id':'chat','owner_principal_id':'alice','dataset':'business','environment_id':'demo'},
        'child':{'id':'child','owner_principal_id':'alice','dataset':'business','native_root_id':'chat','parent_id':'chat'}}
    selected={'state':tmp_path,'index':ReadyIndex(Index(),tmp_path),'business_scope':store,'environment_id':'demo',
        'agent':SimpleNamespace(children=children,active={})}
    selected['chats']=ChatPermissions(tmp_path)
    selected['scope_policy']=RetailScope(selected)
    selected['collections']=Collections()
    selected['collections'].register(lambda principal:selected['index'].status()['datasets'])
    selected['vertical']=SimpleNamespace(source_options=selected['scope_policy'].source_options)
    return selected,store,owner,members,save,k


def test_setup_is_inert_and_requires_actual_ready_source_owner(tmp_path):
    members={'owner':{'role':'owner','id':'owner'}}
    selected={'state':tmp_path,'index':ReadyIndex(Index(),tmp_path),'agent':SimpleNamespace(children={}),
        'environment_id':'demo','organization_chats':SimpleNamespace(company_id='company',owner='organization'),
        'chats':SimpleNamespace(principal=lambda session:session['principal']['id'])}
    store=setup_scope(selected,lambda s,p:members.get(p),lambda s:list(members.values()))
    assert not list(tmp_path.iterdir())
    actor=ScopeActor(store.company_id,'owner')
    assert store.source_authorized(actor,'business','owned-one')
    assert not store.source_authorized(actor,'other','owned-one')
    assert not store.source_authorized(actor,'business','source-feed-label')
    assert scope_current(selected['agent'].business_scope_view(selected['index'].snapshot('owner'),'owner'))
    assert not list(tmp_path.iterdir())


def test_aggregate_view_and_tool_view_share_current_exact_recording_filter(tmp_path):
    selected,store,_,members,save,k=host_fixture(tmp_path)
    source=selected['index'].snapshot('alice')
    aggregate=filter_view(selected,source,'alice')
    assert [row['id'] for row in aggregate.clips]==['0','3']
    tool=filter_view(selected,source,'alice',chat_id='chat')
    assert [row['id'] for row in tool.clips]==['0']
    assert tool.embeddings.tolist()==[[1,0,0,0]] and 'private' not in tool.report
    assert scope_current(aggregate) and scope_current(tool)
    save('members',{'member_id':'alice','role':'alert_recipient','location_ids':[k['a']],'function_ids':[]})
    assert not scope_current(tool)
    assert filter_view(selected,source,'alice',chat_id='chat').clips==[]
    members.pop('alice')
    revoked=filter_view(selected,source,'alice')
    assert revoked.clips==[] and not scope_current(revoked)


def test_root_selection_applies_to_native_child_and_changes_invalidate_admitted_read(tmp_path):
    selected,store,_,_,_,k=host_fixture(tmp_path)
    actor=ScopeActor('company','alice');revision=store.effective(actor,'business')['revision']
    store.bind_chat(actor,'chat','business',[],revision)
    source=selected['index'].snapshot('alice')
    assert filter_view(selected,source,'alice',chat_id='child').clips==[]
    store.bind_chat(actor,'chat','business',[k['a']],revision,expected_selection_revision=1)
    tool=filter_view(selected,source,'alice',chat_id='child')
    assert [row['id'] for row in tool.clips]==['0'] and scope_current(tool)
    selected['agent'].children['chat']['dataset']='other'
    assert not scope_current(tool)
    assert not scope_current(filter_view(selected,source,'alice',chat_id='child'))


def test_wrong_owner_company_and_current_account_role_are_fail_closed(tmp_path):
    selected,_,_,members,_,_=host_fixture(tmp_path)
    source=selected['index'].snapshot('alice')
    good=filter_view(selected,source,'alice')
    assert scope_current(good)
    members['alice']['role']='browse'
    assert not scope_current(good)
    assert filter_view(selected,selected['index'].snapshot('bob'),'alice').clips==[]
    source.source_visibility_company=str(tmp_path/'other')
    assert filter_view(selected,source,'alice').clips==[]
    assert filter_view(selected,selected['index'].snapshot(None),None,collection='business').clips==[]
    assert [row['id'] for row in filter_view(selected,selected['index'].snapshot(None),None).clips]==['3']


def test_host_routes_use_current_acl_observer_and_source_options(tmp_path):
    selected,store,owner,members,_,k=host_fixture(tmp_path)
    app=FastAPI()
    @app.middleware('http')
    async def session(request,call_next):
        principal=request.headers.get('x-person','owner');member=members.get(principal)
        request.state.workspace=selected
        request.state.session={'authenticated':member is not None,'can_query':True,
            'role':member['role'] if member else 'operator','principal':{'id':principal},'environment':{'id':'demo'}}
        if request.headers.get('x-observe'):
            request.state.session['viewing_as']={'principal':{'id':request.headers['x-observe']},'environment':{'id':'demo'}}
        return await call_next(request)
    register_host_routes(app)
    with TestClient(app) as client:
        read=client.get('/api/business/scope?collection=business')
        assert read.status_code==200
        assert {r['id'] for r in read.json()['source_options']}=={'owned-one','owned-wide','unassigned'}
        observed=client.get('/api/business/scope?collection=business',headers={'x-observe':'alice'})
        assert observed.status_code==200 and not observed.json()['can_manage']
        assert [r['id'] for r in observed.json()['source_options']]==['owned-one']
        assert client.get('/api/chats/chat/scope').status_code==404
        assert client.get('/api/chats/child/scope',headers={'x-person':'alice'}).status_code==403
        assert client.get('/api/business/scope?collection=foreign').status_code==404
        revision=store.effective(owner,'business')['revision']
        headers={'x-person':'alice','x-video-demo':'1'}
        body={'location_ids':[k['a']],'expected_revision':revision,'expected_selection_revision':0}
        selected['agent'].active['chat']={}
        assert client.put('/api/chats/chat/scope',headers=headers,json=body).status_code==409
        selected['agent'].active.clear()
        assert client.put('/api/chats/chat/scope',headers=headers,json=body).status_code==200
        members.pop('alice')
        assert client.get('/api/chats/chat/scope',headers=headers).status_code==403


def test_shared_history_requires_all_prior_evidence_and_legacy_history_is_owner_only(tmp_path):
    from scope_host import history_allowed
    selected,store,owner,members,save,k=host_fixture(tmp_path)
    chat=selected['agent'].children['chat'];chat['messages']=[]
    alice=ScopeActor('company','alice')
    scope=store.chat_scope(alice,'chat','business')
    store.admit_history(alice,'chat',[scope],{'business':['owned-one']})
    chat['messages']=[{'role':'assistant','content':'Evidence from kitchen'}]
    save('members',{'member_id':'bob','role':'colleague','location_ids':[k['a']],'function_ids':[k['packing']]})
    assert history_allowed(selected,chat,'alice')
    assert not history_allowed(selected,chat,'bob')
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['b']],'function_ids':[]})
    assert not history_allowed(selected,chat,'alice')
    assert store.history('chat')['sources']=={'business':['owned-one']}
    legacy={'id':'legacy','dataset':'business','owner_principal_id':'alice','messages':[{'content':'unknown old context'}]}
    assert not history_allowed(selected,legacy,'alice')
    assert history_allowed(selected,legacy,'owner')


def test_history_union_is_immutable_across_chat_selection_and_survives_restart(tmp_path):
    from retail_business_scope import RetailBusinessScope as BusinessScope
    selected,store,owner,_,_,k=host_fixture(tmp_path)
    revision=store.effective(owner,'business')['revision']
    first=store.bind_chat(owner,'owner-chat','business',[k['a']],revision)
    admitted=store.admit_history(owner,'owner-chat',[first],{'business':['owned-one']})
    assert admitted['reset']
    same=store.admit_history(owner,'owner-chat',[store.chat_scope(owner,'owner-chat','business')],{'business':['owned-one']})
    assert not same['reset']
    second=store.bind_chat(owner,'owner-chat','business',[k['b']],revision,expected_selection_revision=1)
    changed=store.admit_history(owner,'owner-chat',[second],{'business':[]})
    assert changed['reset'] and changed['binding']!=admitted['binding']
    restarted=BusinessScope(tmp_path,company_id='company',member=store.member)
    receipt=restarted.history('owner-chat')
    assert receipt['sources']=={'business':['owned-one']}
    assert set(receipt['scopes']['business']['location_ids'])=={k['a'],k['b']}


def test_shared_source_mapping_uses_actual_member_after_ready_ownership(tmp_path):
    selected,store,_,members,_,_=host_fixture(tmp_path)
    organization=SimpleNamespace(company_id='company',owner='organization',
        owns=lambda row:row.get('owner_principal_id')=='organization',can_read=lambda row,session:True)
    selected['organization_chats']=organization
    selected['agent'].children['shared']={'id':'shared','dataset':'business','owner_principal_id':'organization'}
    # Simulate an imported original owned exclusively by the organization. Its
    # raw Ready snapshot is admitted, while virtual principal scope must be empty.
    original=selected['index'].snapshot
    def snapshot(principal,**kwargs):
        result=original(principal,**kwargs)
        if principal!='organization':result.clips=[]
        return result
    selected['index'].snapshot=snapshot
    installed=setup_scope(selected,lambda s,p:members.get(p),lambda s:list(members.values()))
    assert selected['index'].snapshot('organization').clips==[]
    assert installed.source_authorized(ScopeActor('company','owner'),'business','owned-one')
    assert not installed.source_authorized(ScopeActor('company','absent'),'business','owned-one')


def test_shared_collection_authority_never_crosses_another_readable_chat(tmp_path):
    selected,store,_,members,_,_=host_fixture(tmp_path)
    allowed={'other'}
    selected['organization_chats']=SimpleNamespace(owner='organization',
        can_read=lambda row,session:row.get('owner_principal_id')=='organization' and row.get('dataset') in allowed,
        owns=lambda row:row.get('owner_principal_id')=='organization')
    selected['agent'].children.update({key:{'id':key,'dataset':key,'owner_principal_id':'organization'}
        for key in ['business','other']})
    source=selected['index'].snapshot('organization',apply_scope=False)
    aggregate=filter_view(selected,source,'alice')
    assert [row['dataset'] for row in aggregate.clips]==['other'] and scope_current(aggregate)
    assert filter_view(selected,source,'alice',collection='business').clips==[]
    allowed.clear()
    assert not scope_current(aggregate)


def test_managed_grandchild_history_keeps_root_scope_after_revocation(tmp_path):
    from scope_host import history_allowed
    selected, store, _, _, save, keys = host_fixture(tmp_path)
    records = selected['agent'].children
    records['child'].pop('native_root_id')
    records['child']['managed_root_id'] = 'chat'
    records['child']['messages'] = []
    records['grandchild'] = dict(records['child'], id='grandchild', parent_id='child',
        messages=[{'role': 'assistant', 'content': 'Private location evidence'}])
    actor = ScopeActor('company', 'alice')
    store.admit_history(actor, 'chat', [store.chat_scope(actor, 'chat', 'business')],
        {'business': ['owned-one']})
    assert all(history_allowed(selected, records[key], 'alice') for key in ('chat', 'child', 'grandchild'))
    save('members', {'member_id': 'alice', 'role': 'colleague', 'location_ids': [], 'function_ids': []})
    assert all(not history_allowed(selected, records[key], 'alice') for key in ('chat', 'child', 'grandchild'))
    save('members', {'member_id': 'alice', 'role': 'colleague', 'location_ids': [keys['a']], 'function_ids': [keys['cooking']]})
    records['child']['owner_principal_id'] = 'foreign'
    assert not history_allowed(selected, records['grandchild'], 'alice')
