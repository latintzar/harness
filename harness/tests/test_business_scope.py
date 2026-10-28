import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from business_scope import ScopeActor, ScopeError
from retail_business_scope import RetailBusinessScope as BusinessScope


def fixture(tmp_path):
    members={'owner':{'id':'owner','role':'owner','display_name':'Owner'},'alice':{'id':'alice','role':'operator','display_name':'Alice'},'bob':{'id':'bob','role':'operator','display_name':'Bob'}}
    store=BusinessScope(tmp_path,company_id='company',member=members.get,members=lambda:list(members.values()),source_authorized=lambda actor,collection,source:source.startswith('owned-'))
    owner=ScopeActor('company','owner')
    def save(kind,value):return store.mutate(owner,'business',kind,value,store.effective(owner,'business')['revision'])
    a=save('locations',{'name':'Kitchen','address':'Site A'})['locations'][0]['id']
    b=save('locations',{'name':'Workshop','address':'Site B'})['locations'][1]['id']
    cooking=save('functions',{'name':'Cooking'})['functions'][0]['id']
    packing=save('functions',{'name':'Packing'})['functions'][1]['id']
    room=save('rooms',{'name':'Kitchen room','location_id':a,'function_ids':[cooking]})['rooms'][0]['id']
    other=save('rooms',{'name':'Packing room','location_id':a,'function_ids':[packing]})['rooms'][1]['id']
    cam=save('cameras',{'name':'Kitchen camera','location_id':a,'room_ids':[room],'source_ids':['owned-one']})['cameras'][0]['id']
    mixed=save('cameras',{'name':'Wide camera','location_id':a,'room_ids':[room,other],'source_ids':['owned-wide']})['cameras'][1]['id']
    return store,owner,members,save,dict(a=a,b=b,cooking=cooking,packing=packing,room=room,other=other,cam=cam,mixed=mixed)


def test_browse_is_inert_and_cross_company_refused(tmp_path):
    store=BusinessScope(tmp_path,company_id='company',member=lambda _: {'role':'owner'})
    assert store.describe(ScopeActor('company','owner'),'business')['revision']==0
    assert not list(tmp_path.iterdir())
    with pytest.raises(ScopeError):store.describe(ScopeActor('other','owner'),'business')


def test_persisted_many_to_many_camera_and_current_member_scope(tmp_path):
    store,owner,members,save,k=fixture(tmp_path)
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]})
    actor=ScopeActor('company','alice')
    view=store.describe(actor,'business')
    assert [c['id'] for c in view['cameras']]==[k['cam']]
    assert view['effective']['source_ids']==['owned-one']
    assert len(view['rooms'])==1 and len(view['functions'])==1
    assert view['members'][0]['display_name']=='Alice'
    # A camera spanning a forbidden room does not become readable merely by overlap.
    with pytest.raises(ScopeError):store.camera(actor,'business',k['mixed'])
    restarted=BusinessScope(tmp_path,company_id='company',member=members.get)
    assert restarted.effective(actor,'business')['source_ids']==['owned-one']
    members.pop('alice')
    with pytest.raises(ScopeError):restarted.effective(actor,'business')


def test_location_admin_cannot_cross_location_or_raise_account_role(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    save('members',{'member_id':'alice','role':'location_admin','location_ids':[k['a']],'function_ids':[]})
    actor=ScopeActor('company','alice'); revision=store.effective(actor,'business')['revision']
    for kind,value in [('locations',{'name':'New site'}),('rooms',{'name':'Foreign','location_id':k['b'],'function_ids':[]}),('members',{'member_id':'bob','role':'location_admin','location_ids':[k['a']],'function_ids':[]}),('members',{'member_id':'owner','role':'colleague','location_ids':[],'function_ids':[]})]:
        with pytest.raises(ScopeError):store.mutate(actor,'business',kind,value,revision)
    result=store.mutate(actor,'business','members',{'member_id':'bob','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]},revision)
    assert result['revision']==revision+1


def test_source_ownership_revision_observer_and_actual_account_revocation(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    revision=store.effective(owner,'business')['revision']
    with pytest.raises(ScopeError,match='revision'):store.mutate(owner,'business','functions',{'name':'Other'},revision-1)
    with pytest.raises(ScopeError,match='read_only'):store.mutate(ScopeActor('company','owner',True),'business','functions',{'name':'Other'},revision)
    with pytest.raises(ScopeError,match='source_denied'):save('cameras',{'name':'Foreign source','location_id':k['a'],'room_ids':[k['room']],'source_ids':['private-bob']})
    assert store.effective(owner,'business')['revision']==revision


def test_chat_selected_scope_is_fixed_and_revocation_never_falls_back_to_all(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a'],k['b']],'function_ids':[]})
    actor=ScopeActor('company','alice');rev=store.effective(actor,'business')['revision']
    store.bind_chat(actor,'chat','business',[k['a']],rev)
    assert store.chat_scope(actor,'chat','business')['source_ids']==['owned-one','owned-wide']
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['b']],'function_ids':[]})
    assert store.chat_scope(actor,'chat','business')['source_ids']==[]
    with pytest.raises(ScopeError,match='business_fixed'):store.chat_scope(owner,'chat','other')


def test_alert_recipient_has_no_search_grant_and_camera_revisions_preserve_source_history(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    save('members',{'member_id':'bob','role':'alert_recipient','location_ids':[k['a']],'function_ids':[]})
    assert not store.effective(ScopeActor('company','bob'),'business')['can_query']
    save('cameras',{'id':k['cam'],'name':'Camera moved','location_id':k['a'],'room_ids':[k['other']],'source_ids':['owned-one','owned-two']})
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]})
    view=store.effective(ScopeActor('company','alice'),'business')
    assert k['cam'] not in view['camera_ids'] and view['source_ids']==['owned-one']


def test_function_admin_delegation_preserves_intersection_and_bounded_inputs(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    save('members',{'member_id':'alice','role':'function_admin','location_ids':[k['a']],'function_ids':[k['cooking']]})
    actor=ScopeActor('company','alice');revision=store.effective(actor,'business')['revision']
    with pytest.raises(ScopeError):store.mutate(actor,'business','members',{'member_id':'bob','role':'colleague','location_ids':[k['a']],'function_ids':[]},revision)
    with pytest.raises(ScopeError):store.mutate(owner,'business','locations',{'name':'<script>','admin':True},revision)
    value=store.mutate(actor,'business','members',{'member_id':'bob','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]},revision)
    assert value['revision']==revision+1


def test_account_role_change_during_mutation_rolls_back(tmp_path):
    store,owner,members,save,k=fixture(tmp_path)
    revision=store.effective(owner,'business')['revision']
    def source_check(*args):
        members['owner']['role']='operator'
        return True
    store.source_authorized=source_check
    with pytest.raises(ScopeError):
        store.mutate(owner,'business','cameras',{'id':k['cam'],'name':'Changed','location_id':k['a'],
            'room_ids':[k['room']],'source_ids':['owned-one']},revision)
    members['owner']['role']='owner'
    assert store.effective(owner,'business')['revision']==revision


def test_function_admin_cannot_delegate_ungranted_function_of_shared_room(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    save('rooms',{'id':k['room'],'name':'Shared room','location_id':k['a'],'function_ids':[k['cooking'],k['packing']]})
    save('members',{'member_id':'alice','role':'function_admin','location_ids':[k['a']],'function_ids':[k['cooking']]})
    actor=ScopeActor('company','alice');effective=store.effective(actor,'business')
    assert effective['function_ids']==[k['cooking']]
    with pytest.raises(ScopeError):
        store.mutate(actor,'business','members',{'member_id':'bob','role':'colleague',
            'location_ids':[k['a']],'function_ids':[k['packing']]},effective['revision'])


def test_explicit_recording_unbind_retires_current_access_but_preserves_audit(tmp_path):
    store,owner,_,save,k=fixture(tmp_path)
    save('cameras',{'id':k['cam'],'name':'Moved','location_id':k['a'],'room_ids':[k['other']],
        'source_ids':['owned-one','owned-two']})
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]})
    actor=ScopeActor('company','alice')
    assert store.effective(actor,'business')['source_ids']==['owned-one']
    save('cameras',{'id':k['cam'],'name':'Moved','location_id':k['a'],'room_ids':[k['other']],'source_ids':['owned-two']})
    assert store.effective(actor,'business')['source_ids']==[]
    events=store.events(owner,'business')
    assert events[-1]['kind']=='cameras' and events[-1]['record_id']==k['cam']
    with pytest.raises(ScopeError):store.events(actor,'business')


@pytest.mark.parametrize('value',[[{}],[[]],['valid',1],None])
def test_malformed_selection_raises_safe_scope_error(tmp_path,value):
    store,owner,_,_,_=fixture(tmp_path)
    if value is None:
        with pytest.raises(ScopeError):store.mutate(owner,'business','members',{'member_id':'alice','role':'colleague','location_ids':None,'function_ids':[]},store.effective(owner,'business')['revision'])
    else:
        with pytest.raises(ScopeError):store.effective(owner,'business',value)


def test_current_guard_detects_role_graph_chat_changes_and_writer_contention(tmp_path):
    import sqlite3
    store,owner,members,save,k=fixture(tmp_path)
    view=store.bind_chat(owner,'chat','business',[k['a']],store.effective(owner,'business')['revision'])
    def current():return store.current(owner,'business',view['revision'],account_role=view['account_role'],
        chat_id='chat',selection_revision=view['selection_revision'])
    assert current()
    with sqlite3.connect(store.path) as writer:
        writer.execute('BEGIN EXCLUSIVE')
        assert current() is False
    assert current()
    members['owner']['role']='operator';assert current() is False
    members['owner']['role']='owner'
    store.bind_chat(owner,'chat','business',[k['b']],view['revision'],expected_selection_revision=1)
    assert current() is False
    assert [event['body']['selection_revision'] for event in store.events(owner,'business') if event['kind']=='chat_scope']==[1,2]


def test_ready_index_filter_preserves_owner_boundary_and_exact_aligned_vectors(tmp_path):
    from types import SimpleNamespace
    import numpy as np
    from retail_scope_views import scoped_index_view
    store,owner,_,save,k=fixture(tmp_path)
    save('members',{'member_id':'alice','role':'colleague','location_ids':[k['a']],'function_ids':[k['cooking']]})
    effective=store.effective(ScopeActor('company','alice'),'business')
    index=SimpleNamespace(clips=[{'id':str(i),'video_id':video,'source_id':'owned-one','dataset':dataset,'start':0,'end':1,'duration':1}
        for i,(video,dataset) in enumerate([('owned-one','business'),('foreign','business'),('owned-one','other'),('owned-wide','business')])],
        embeddings=np.array([[1,0],[2,0],[3,0],[4,0]],dtype=float),report={'private_timing':123})
    view=scoped_index_view(index,effective)
    assert [r['id'] for r in view.clips]==['0'] and view.embeddings.tolist()==[[1,0]]
    assert view.embeddings.flags.writeable is False and 'private_timing' not in view.report
    assert len(index.clips)==4
    empty=scoped_index_view(index,{**effective,'source_ids':[]})
    assert empty.clips==[] and empty.embeddings.shape==(0,2)
    # SourceStore feed source_id, camera name or another business cannot supply an original recording grant.
    assert view.report['video_count']==1
    owner_all=scoped_index_view(index,store.effective(owner,'business'))
    assert [r['id'] for r in owner_all.clips]==['0','1','3']
    owner_location=scoped_index_view(index,store.effective(owner,'business',[k['a']]))
    assert [r['id'] for r in owner_location.clips]==['0','3']
