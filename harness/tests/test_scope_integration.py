"""Real authenticated routes + native context boundaries; zero inference."""
import pytest
from fastapi.testclient import TestClient
from test_server_access import fixture_app
from business_scope import ScopeActor

H={'X-Video-Demo':'1'}

def login(client,app,person,owner=False):
    return client.post('/api/session',json={'code':app.state.access.issue('demo',owner=owner,
        principal_id=person,display_name=person)},headers=H).json()

def configure(client):
    revision=0
    def save(kind,value):
        nonlocal revision
        reply=client.post('/api/business/scope/'+kind,json={'collection':'cafe','expected_revision':revision,'value':value},headers=H)
        assert reply.status_code==200,reply.text
        revision=reply.json()['revision'];return reply.json()
    site=save('locations',{'name':'North'})['locations'][0]['id']
    room=save('rooms',{'name':'Kitchen','location_id':site,'function_ids':[]})['rooms'][0]['id']
    save('cameras',{'name':'Camera','location_id':site,'room_ids':[room],'source_ids':['demo-video']})
    save('members',{'member_id':'alice','role':'colleague','location_ids':[site],'function_ids':[]})
    return site,save


def test_actual_http_media_and_chat_history_follow_current_scope(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as owner,TestClient(app) as alice,TestClient(app) as bob:
        login(owner,app,'owner',True);login(alice,app,'alice');login(bob,app,'bob')
        assert bob.get('/api/clip/demo-clip').status_code==200 # rev0 preserved
        site,save=configure(owner)
        assert alice.get('/api/clip/demo-clip').status_code==200
        assert bob.get('/api/clip/demo-clip').status_code==404
        selected=app.state.workspaces['demo'];store=selected['business_scope']
        chat={'id':'scoped-chat','owner_principal_id':'alice','dataset':'cafe','messages':[], 'kind':'chat'}
        selected['agent'].children[chat['id']]=chat
        actor=ScopeActor(store.company_id,'alice');scope=store.chat_scope(actor,chat['id'],'cafe')
        store.admit_history(actor,chat['id'],[scope],{'cafe':['demo-video']})
        chat['messages']=[{'role':'assistant','content':'Kitchen evidence'}]
        assert alice.get('/api/chats/scoped-chat').status_code==200
        save('members',{'member_id':'alice','role':'colleague','location_ids':[],'function_ids':[]})
        assert alice.get('/api/clip/demo-clip').status_code==404
        assert alice.get('/api/chats/scoped-chat').status_code==404
        assert 'Kitchen evidence' not in alice.get('/api/chats').text


def test_real_member_removal_and_revocation_after_await_fail_closed(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as owner,TestClient(app) as alice:
        login(owner,app,'owner',True);login(alice,app,'alice');configure(owner)
        selected=app.state.workspaces['demo']
        async def thumbnail(*args,**kwargs):
            with app.state.access.connect() as db:db.execute("DELETE FROM principal_environments WHERE principal_id='alice'")
            return tmp_path/'fixture.jpg'
        monkeypatch.setattr(selected['media'],'clip',thumbnail)
        result=alice.get('/clips/demo-clip.mp4')
        assert result.status_code in {403,404}
        assert b'fixture-image' not in result.content


def test_revoked_history_hides_files_exports_and_agent_report_records(tmp_path,monkeypatch):
    import json
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as owner,TestClient(app) as alice:
        login(owner,app,'owner',True);login(alice,app,'alice');site,save=configure(owner)
        selected=app.state.workspaces['demo'];store=selected['business_scope']
        chat={'id':'scoped-evidence','owner_principal_id':'alice','dataset':'cafe','messages':[], 'kind':'chat'}
        selected['agent'].children[chat['id']]=chat
        actor=ScopeActor(store.company_id,'alice')
        store.admit_history(actor,chat['id'],[store.chat_scope(actor,chat['id'],'cafe')],{'cafe':['demo-video']})
        chat['messages']=[{'role':'assistant','content':'Kitchen evidence'}]
        file=selected['files'].create({'name':'evidence.md','content':'Kitchen evidence'},
            owner_principal_id='alice',chat_id=chat['id'],dataset='cafe')
        package={'id':'export-'+'a'*32,'owner_principal_id':'alice','dataset':'cafe','status':'failed',
            'title':'Kitchen evidence','created_at':'2026-10-06T00:00:00Z',
            'selection':{'items':[{'clip_id':'demo-clip'}]}}
        with selected['exports'].connect() as db:
            db.execute('INSERT INTO exports(id,owner,dataset,body) VALUES(?,?,?,?)',
                (package['id'],'alice','cafe',json.dumps(package)))
        assert alice.get('/api/files/'+file['id']).status_code==200
        assert alice.get('/api/exports/'+package['id']).status_code==200
        run={'id':'run-one','chat_id':chat['id'],'dataset':'cafe','owner_principal_id':'alice','report_messages':chat['messages']}
        session=alice.get('/api/session').json()
        assert selected['chats'].can_read(run,session)
        save('members',{'member_id':'alice','role':'colleague','location_ids':[],'function_ids':[]})
        assert not selected['chats'].can_read(run,session)
        assert alice.get('/api/files/'+file['id']).status_code==404
        assert alice.get('/api/exports/'+package['id']).status_code==404
        assert 'Kitchen evidence' not in alice.get('/api/files').text
        assert alice.get('/api/exports').json()['exports']==[]


def test_clip_relations_require_both_endpoints_in_current_scope(tmp_path,monkeypatch):
    import copy,json
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as owner,TestClient(app) as alice:
        login(owner,app,'owner',True);login(alice,app,'alice')
        selected=app.state.workspaces['demo']
        raw=selected['index']._raw
        raw.clips.append({**copy.deepcopy(raw.clips[0]),'id':'hidden-clip','video_id':'hidden-video'})
        configure(owner)
        (selected['state']/'relations.json').write_text(json.dumps([
            {'from_clip_id':'demo-clip','to_clip_id':'hidden-clip','evidence':'Restricted kitchen detail'},
            {'from_clip_id':'demo-clip','to_clip_id':'demo-clip','evidence':'Visible relation'}]))
        value=alice.get('/api/clip/demo-clip')
        assert value.status_code==200,value.text
        assert value.json()['relations']==[{'from_clip_id':'demo-clip','to_clip_id':'demo-clip','evidence':'Visible relation'}]


def test_canonical_scope_mutations_return_the_committed_revision(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    headers = {'X-Rocoto-Page': '1'}
    with TestClient(app) as owner:
        login(owner, app, 'owner', True)
        response = owner.post('/api/retail/scope/locations', json={
            'collection': 'cafe', 'expected_revision': 0, 'value': {'name': 'North'}}, headers=headers)
        assert response.status_code == 200, response.text
        graph = response.json()
        assert graph['revision'] == 1
        assert owner.get('/api/business/scope?collection=cafe').json()['revision'] == 1
        selected = app.state.workspaces['demo']
        selected['agent'].children['selection-chat'] = {
            'id': 'selection-chat', 'owner_principal_id': 'owner', 'dataset': 'cafe',
            'environment_id': 'demo', 'messages': [], 'kind': 'chat'}
        response = owner.put('/api/retail/chats/selection-chat/scope', json={
            'location_ids': [graph['locations'][0]['id']], 'expected_revision': 1,
            'expected_selection_revision': 0}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['selection_revision'] == 1
        assert owner.get('/api/chats/selection-chat/scope').json()['selection_revision'] == 1
        assert owner.post('/api/retail/scope/locations', json={
            'collection': 'cafe', 'expected_revision': 0, 'value': {'name': 'Stale'}},
            headers=headers).status_code == 409
        assert owner.get('/api/retail/scope?collection=cafe').json()['revision'] == 1


@pytest.mark.parametrize('withdrawal', ['removed', 'downgraded'])
def test_canonical_scope_mutation_still_rechecks_current_membership(tmp_path, monkeypatch, withdrawal):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as owner:
        login(owner, app, 'owner', True)
        selected = app.state.workspaces['demo']
        original = selected['business_scope'].mutate
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            with app.state.access.connect() as db:
                if withdrawal == 'removed':
                    db.execute("DELETE FROM principal_environments WHERE principal_id='owner'")
                else:
                    db.execute("UPDATE principal_environments SET role='browse' WHERE principal_id='owner'")
            return result
        monkeypatch.setattr(selected['business_scope'], 'mutate', mutate)
        response = owner.post('/api/retail/scope/locations', json={
            'collection': 'cafe', 'expected_revision': 0, 'value': {'name': 'Private location'}},
            headers={'X-Rocoto-Page': '1'})
        assert response.status_code == 403
        assert 'Private location' not in response.text
