"""HTTP capability and company boundaries, with no model/provider calls."""
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import server


class FixtureIndex:
    def __init__(self, company="demo", thumbnail=None):
        self.company = company
        self.clips = [{"id": company + "-clip", "video_id": company + "-video",
                       "dataset": "cafe", "camera": company + " camera", "start": 0., "end": 1.,
                       "sample_time": .5, "path": str(thumbnail), "thumbnail_path": str(thumbnail)}]

    def status(self):
        return {"ready": True, "datasets": ["cafe"]}

    def get_clip(self, clip_id):
        return next((row for row in self.clips if row["id"] == clip_id), None)


class FixtureAgent:
    instances = {}

    def __init__(self, state, index=None, corpus=None):
        self.state = Path(state)
        self.company = index.company if index else ("demo" if Path(state).name == "state" else Path(state).name)
        self.active = {self.company + "-chat": {"status": "running"}}
        self.children = {self.company + "-chat": {"id": self.company + "-chat", "running": False,
                         "messages": [{"role": "user", "content": self.company + " private investigation"}]}}
        self.child_tasks = {}
        self.registry = {}
        self.stopped = []
        self.instances[self.company] = self

    def status(self):
        return self.active

    def _save_child(self,child):
        self.children[child['id']]=child

    def current_agent_setup(self,chat_id):
        return self.children[chat_id].get('agent_setup')

    async def control(self, chat_id, operation, *args):
        self.stopped.append(chat_id)
        return {"ok": True}


def fixture_app(tmp_path, monkeypatch):
    image = tmp_path / "fixture.jpg"
    image.write_bytes(b"fixture-image")
    monkeypatch.setattr(server, "HarnessAgent", FixtureAgent)
    index_factory = lambda path: FixtureIndex(Path(path).parent.name, image)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    return server.create_app(tmp_path / "state", FixtureIndex("demo", image),index_factory=index_factory)


def test_read_only_demo_cannot_interrupt_or_read_private_chat(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/api/session/demo", headers={"X-Video-Demo": "1"})
        assert response.status_code == 200
        assert response.json()["can_query"] is False
        assert client.post("/api/chat/demo-chat/stop", json={}, headers={"X-Video-Demo": "1"}).status_code == 403
        assert FixtureAgent.instances["demo"].stopped == []
        assert client.get("/api/chats/demo-chat").status_code == 403
        assert client.get("/api/status").json()["agent"]["active_chats"] == {}


def test_company_code_cannot_read_other_company_media_or_chats(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    store = app.state.access
    store.create_environment("company-a", "Company A")
    store.create_environment("company-b", "Company B")
    with TestClient(app) as company_a, TestClient(app) as company_b:
        for client, name in [(company_a, "company-a"), (company_b, "company-b")]:
            response=client.post("/api/session", json={"code": store.issue(name)},headers={"X-Video-Demo": "1"})
            assert response.status_code==200
            # Explicit fixture identity; production never claims unowned legacy chats.
            client.get('/api/status')
            FixtureAgent.instances[name].children[name+'-chat']['owner_principal_id']=response.json()['principal']['id']
            assert client.get(f"/api/clip/{name}-clip").status_code == 200
            assert client.get(f"/api/chats/{name}-chat").status_code == 200
        assert company_b.get("/api/clip/company-a-clip").status_code == 404
        assert company_b.get("/clips/company-a-clip.mp4").status_code == 404
        assert company_b.get("/thumbnails/company-a-clip.jpg").status_code == 404
        assert company_b.get("/api/chats/company-a-chat").status_code == 404
        assert company_b.post("/api/environment", json={"environment_id": "company-a"},
                              headers={"X-Video-Demo": "1"}).status_code == 403


def agent_definition(**changes):
    return {'title':'Visible activity','instruction':'Describe visible activity with evidence.',
        'dataset':'cafe','mode':'ad_hoc','fresh_inspections_per_run':0,**changes}


def claim_event_report(app, key):
    """Use committed fixture observations through the supported report-run boundary."""
    import time

    workspace = app.state.workspaces['demo']
    store, monitoring = workspace['agents'], workspace['monitoring']
    definition = store.get(key)
    owner = definition['owner_principal_id']
    profile = monitoring.save_profile(owner, {
        'name': 'Fixture activity', 'dataset': 'cafe',
        'vocabulary': [{'type': 'pick_up', 'description': 'A visible item pickup.'}],
    }, datasets=['cafe'])
    observed = monitoring.commit_event(owner, {
        'profile_id': profile['id'], 'profile_revision': profile['revision'],
        'event_key': 'report-fixture-' + key, 'type': 'pick_up',
        'description': 'Fixture pickup observation.',
        'source': {'clip_id': 'demo-clip', 'start_s': 0, 'end_s': .5}, 'track_id': 'fixture-person',
    })
    store.activate(key, time.time() + 100)
    run = store.claim(key, {observed['id']: 'demo-clip'}, source_records=[observed])
    assert run is not None
    assert run['definition']['input_kind'] == 'tracker_events'
    assert run['observations'] == [observed]
    return store, run


def test_agents_are_private_to_exact_principal_in_same_company(tmp_path,monkeypatch):
    from background_agents import AgentStore
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    with TestClient(app) as alice,TestClient(app) as bob:
        alice.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='alice',display_name='Alice')},headers=headers)
        bob.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='bob',display_name='Bob')},headers=headers)
        record=alice.post('/api/agents',json=agent_definition(),headers=headers).json()
        assert record['owner_principal_id']=='alice'
        key=record['id']
        assert [r['id'] for r in alice.get('/api/agents').json()['agents']]==[key]
        assert bob.get('/api/agents').json()['agents']==[]
        assert bob.get(f'/api/agents/{key}').status_code==404
        for action,body in [('update',agent_definition(expected_revision=1)),('activate',{'expected_revision':1}),
                            ('run',{'expected_revision':1}),('pause',{})]:
            assert bob.post(f'/api/agents/{key}/{action}',json=body,headers=headers).status_code==404
        legacy=app.state.workspaces['demo']['agents'].create(agent_definition(),['cafe'])
        assert alice.get(f"/api/agents/{legacy['id']}").status_code==404
        assert bob.get(f"/api/agents/{legacy['id']}").status_code==404
        changed=alice.post(f'/api/agents/{key}/update',json=agent_definition(title='Updated',expected_revision=1),headers=headers)
        assert changed.status_code==200 and changed.json()['revision']==2
        assert alice.post(f'/api/agents/{key}/update',json=agent_definition(expected_revision=1),headers=headers).status_code==400


def test_owner_observer_can_read_target_chats_agents_and_reports_but_cannot_write(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    access=app.state.access
    with TestClient(app) as customer,TestClient(app) as owner:
        customer.post('/api/session',json={'code':access.issue('demo',principal_id='customer',display_name='Customer')},headers=headers)
        record=customer.post('/api/agents',json=agent_definition(agent_kind='tracker',input_kind='tracker_events'),headers=headers).json();key=record['id']
        store,run=claim_event_report(app,key)
        store.finish(run['id'],'completed',report_messages=[{'role':'assistant','content':'Customer report'}])
        fake=FixtureAgent.instances['demo']
        fake.children['customer-chat']={'id':'customer-chat','owner_principal_id':'customer','messages':[{'role':'assistant','content':'Customer chat'}]}
        customer.post('/api/chats/customer-chat/metadata',json={'pinned':True,'expected_revision':0},headers=headers)
        owner.post('/api/session',json={'code':access.issue('demo',owner=True,principal_id='peter',display_name='Peter')},headers=headers)
        assert owner.get(f'/api/agents/{key}').status_code==404
        assert customer.get('/api/observers').status_code==403
        people=owner.get('/api/observers').json()['principals']
        assert any(person['id']=='customer' for person in people)
        observed=owner.post('/api/observer',json={'environment_id':'demo','principal_id':'customer'},headers=headers)
        assert observed.status_code==200 and observed.json()['can_query'] is False
        assert owner.get('/api/chats/customer-chat').json()['pinned'] is True
        assert owner.get(f'/api/agents/{key}').status_code==200
        assert owner.get(run['report_url']).json()['messages'][0]['content']=='Customer report'
        assert owner.get('/api/clip/demo-clip').status_code==200
        assert owner.get('/api/library?collection=cafe').status_code==200
        for path,body in [(f'/api/agents/{key}/activate',{'expected_revision':1}),
            (f'/api/agents/{key}/run',{'expected_revision':1}),(f'/api/agents/{key}/pause',{}),
            (f'/api/agents/{key}/update',agent_definition(expected_revision=1)),('/api/agents',agent_definition()),
            ('/api/chat',{'chat_id':'customer-chat','dataset':'cafe','message':'Write'}),
            ('/api/chats/customer-chat/metadata',{'archived':True}),('/api/chat/customer-chat/stop',{}),
            ('/api/usage/settings',{'expected_revision':0,'monthly_limit_usd':'100'}),
            ('/api/attachments',{}),('/api/business/profile',{})]:
            assert owner.post(path,json=body,headers=headers).status_code==403,path
        restored=owner.delete('/api/observer',headers=headers)
        assert restored.status_code==200 and restored.json()['effective_principal_id']=='peter'
        assert owner.get('/api/chats/customer-chat').status_code==404
        assert customer.get('/api/chats/customer-chat').json()['archived'] is False


def test_agent_report_rejects_different_or_unassigned_run_owner(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='alice',display_name='Alice')},headers=headers)
        key=client.post('/api/agents',json=agent_definition(agent_kind='tracker',input_kind='tracker_events'),headers=headers).json()['id']
        store,run=claim_event_report(app,key)
        store.finish(run['id'],'completed',report_messages=[{'role':'assistant','content':'Private report'}])
        assert client.get(run['report_url']).status_code==200
        with store.connect() as db:
            stored=store._read(db,'runs',run['id']);stored['owner_principal_id']='someone-else';store._save(db,'runs',stored)
        assert client.get(run['report_url']).status_code==404


def test_uploads_are_private_to_principal_and_cannot_cross_chat_or_steer_inputs(tmp_path,monkeypatch):
    import io
    from PIL import Image
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    image=io.BytesIO();Image.new('RGB',(3,3)).save(image,format='PNG')
    files={'file':('reference.png',image.getvalue(),'image/png')}
    with TestClient(app) as alice,TestClient(app) as bob,TestClient(app) as owner:
        for client,principal in [(alice,'alice'),(bob,'bob')]:
            client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id=principal,display_name=principal)},headers=headers)
        upload=alice.post('/api/attachments',files=files,headers=headers)
        assert upload.status_code==200
        data=upload.json();key=data['id']
        own_image=alice.get(data['url'])
        assert own_image.status_code==200
        assert own_image.headers['cache-control']=='private, no-store'
        assert bob.get(data['url']).status_code==404
        assert bob.post('/api/chat',json={'chat_id':'new-bob','dataset':'cafe','message':'Inspect',
            'attachments':[key]},headers=headers).status_code==403
        fake=FixtureAgent.instances['demo'];fake.children['bob-chat']={'id':'bob-chat','owner_principal_id':'bob','messages':[]}
        assert bob.post('/api/chat/bob-chat/steer',json={'message':'Inspect','attachments':[key]},headers=headers).status_code==403
        assert fake.stopped==[]
        legacy='f'*32;(tmp_path/'state'/'attachments'/f'{legacy}.png').write_bytes(image.getvalue())
        assert alice.get(f'/attachments/{legacy}.png').status_code==404
        owner.post('/api/session',json={'code':app.state.access.issue('demo',owner=True,principal_id='owner',display_name='Owner')},headers=headers)
        owner.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=headers)
        assert owner.get(data['url']).status_code==200
        assert owner.post('/api/attachments',files=files,headers=headers).status_code==403
        bob.post('/api/session/demo',headers=headers)
        assert bob.post('/api/attachments',files=files,headers=headers).status_code==403


def test_subagent_is_readable_but_only_parent_can_control_it(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    headers = {'X-Video-Demo': '1'}
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id='alice')}, headers=headers)
        agent = FixtureAgent.instances['demo']
        agent.children['child'] = {'id': 'child', 'owner_principal_id': 'alice', 'parent_id': 'parent',
                                   'dataset': 'cafe', 'running': False, 'messages': []}
        assert client.get('/api/chats/child').status_code == 200
        for path, body in [('/api/chat', {'chat_id': 'child', 'dataset': 'cafe', 'message': 'Continue'}),
                           ('/api/chat/child/steer', {'message': 'Change the task'}),
                           ('/api/chat/child/stop', {})]:
            result = client.post(path, json=body, headers=headers)
            assert result.status_code == 403
            assert result.json()['detail']['code'] == 'child_chat_parent_controlled'
        assert agent.stopped == []


def test_agent_chat_is_persistent_private_and_scope_locked(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    with TestClient(app) as alice, TestClient(app) as bob:
        for client, name in ((alice,'alice'),(bob,'bob')):
            client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id=name)},headers=headers)
        record=alice.post('/api/agents',json=agent_definition(),headers=headers).json()
        chat_id=record['chat_id']
        child=alice.get('/api/chats/'+chat_id)
        assert child.status_code==200 and child.json()['kind']=='agent_conversation'
        assert child.json()['messages'][0]['content']==record['instruction']
        assert bob.get('/api/chats/'+chat_id).status_code==404
        assert alice.get('/api/agents/'+record['id']).json()['chat_id']==chat_id
        assert len(alice.get('/api/chats/'+chat_id).json()['messages'])==1
        assert alice.post('/api/chat',json={'chat_id':chat_id,'message':'Check another business','dataset':'company'},headers=headers).status_code==400


def test_agent_running_report_excludes_earlier_conversation(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='alice')},headers=headers)
        record=client.post('/api/agents',json=agent_definition(agent_kind='tracker',input_kind='tracker_events'),headers=headers).json()
        store,run=claim_event_report(app,record['id'])
        assert client.get(run['report_url']).json()['messages']==[]
        store.mark_conversation_run(run['id'],1)
        child=FixtureAgent.instances['demo'].children[record['chat_id']]
        child['messages'].append({'role':'assistant','content':'Current run evidence'})
        response=client.get(run['report_url'])
        assert response.status_code==200
        assert response.json()['messages']==[{'role':'assistant','content':'Current run evidence'}]
        assert client.post('/api/chat',json={'chat_id':record['chat_id'],'message':'Manual turn','dataset':'cafe'},headers=headers).status_code==409
