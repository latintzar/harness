"""UI and native agent share actual readiness; draft setup never grants execution."""
from retail_agent_inputs import RetailInputProvider
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from background_agents import AgentScheduler
from agent_test_support import retail_agent_store as AgentStore
from test_background_agents import definition
from test_skill_runtime import HEADERS, make_app
from test_chat_message_context import capture_native


def retail_capabilities(index, *, monitoring=None, custom_connectors=None, custom_execution=None):
    from workspace_capabilities import WorkspaceCapabilities
    from retail_host import RetailWorkspace
    inputs=RetailInputProvider(index=index,monitoring=monitoring)
    selected={'index':index,'agents':SimpleNamespace(inputs=inputs),'environment_id':'demo',
        'custom_connectors':custom_connectors or SimpleNamespace(list=lambda **kwargs:[]),
        'intake_projection':SimpleNamespace(snapshot=lambda *args,**kwargs:{'intake':{},'imports':[],'next_offset':None})}
    vertical=RetailWorkspace(None,selected)
    return WorkspaceCapabilities(inputs=lambda:inputs,contribution=vertical.capabilities,
        custom_connectors=custom_connectors,custom_execution=custom_execution)


def test_imported_archive_can_configure_but_cannot_start_recurring_agents(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS)
        ready = client.get('/api/workspace/capabilities?dataset=cafe')
        assert ready.status_code == 200
        assert ready.json()['footage']['search_ready'] is True
        assert ready.json()['live_camera_feeds']['ready'] is False
        recurring = ready.json()['recurring_agents']
        assert recurring['configurable'] is True and recurring['ready'] is False
        # Imported recordings support inspection; event agents wait for committed observations.
        from retail_agent_inputs import OBSERVATION_NOTICE
        assert recurring['notice'] == OBSERVATION_NOTICE and 'camera' not in OBSERVATION_NOTICE
        for mode in ('scheduled', 'ongoing'):
            body = definition(mode=mode, input_kind='tracker_events', fresh_inspections_per_run=0)
            saved = client.post('/api/agents', json=body, headers=HEADERS).json()
            for action in ('run', 'activate'):
                response = client.post(f"/api/agents/{saved['id']}/{action}",
                    json={'expected_revision': 1}, headers=HEADERS)
                assert response.status_code == 400
                assert response.json()['detail'] == 'Save source-backed observations to start this agent.'
            row = client.get(f"/api/agents/{saved['id']}").json()
            assert row['status'] == 'draft' and row['runs_started'] == 0
            assert row['execution']['ready'] is False
        saved = client.post('/api/agents', json=definition(mode='ad_hoc', input_kind='tracker_events',
            fresh_inspections_per_run=0), headers=HEADERS).json()
        assert client.get(f"/api/agents/{saved['id']}").json()['execution']['ready'] is False
        selected = app.state.workspaces['demo']
        principal = client.get('/api/session').json()['principal']['id']
        monitoring, clip = selected['monitoring'], selected['index'].clips[0]
        profile = monitoring.save_profile(principal, {'name': 'Handoffs', 'dataset': 'cafe',
            'vocabulary': [{'type': 'handoff', 'description': 'A cup is handed to someone.'}]}, datasets=['cafe'])
        monitoring.commit_event(principal, {'profile_id': profile['id'], 'profile_revision': profile['revision'],
            'event_key': 'handoff', 'type': 'handoff', 'description': 'A cup changes hands.',
            'source': {'clip_id': clip['id'], 'start_s': 0, 'end_s': 1}, 'track_id': 'p1'})
        assert client.get(f"/api/agents/{saved['id']}").json()['execution']['ready'] is True
        assert client.post(f"/api/agents/{saved['id']}/run", json={'expected_revision': 1}, headers=HEADERS).status_code == 200


def test_setup_context_persists_and_cannot_fake_live_readiness(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    calls = capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        client.post('/api/session', json={'code':app.state.access.issue('demo')}, headers=HEADERS)
        response = client.post('/api/chat', json={'chat_id':'setup','dataset':'cafe','message':'Configure it.',
            'agent_setup':{'mode':'ad_hoc','template_id':'queue-watch'}, 'live_camera_feeds':{'ready':True}}, headers=HEADERS)
        assert response.status_code == 200
        chat = client.get('/api/chats/setup').json()
        assert chat['agent_setup']['mode'] == 'ongoing'
        assert chat['workspace_capabilities']['recurring_agents']['ready'] is False
        assert chat['messages'][0]['content'] == 'Configure it.'
        assert client.post('/api/chat', json={'chat_id':'setup','dataset':'cafe','message':'Continue.',
            'agent_setup':{'mode':'ad_hoc'}}, headers=HEADERS).status_code == 200
        assert client.get('/api/chats/setup').json()['agent_setup']['mode'] == 'ongoing'
        assert client.get('/api/workspace/capabilities?dataset=ungranted').status_code == 400
    assert len(calls) == 2


def test_scheduler_blocks_preexisting_recurring_activation_before_claim(tmp_path):
    async def prove():
        store = AgentStore(tmp_path,input_provider=RetailInputProvider())
        harness = SimpleNamespace(children={})
        index = SimpleNamespace(clips=[{'id':'a','dataset':'cafe'}])
        scheduler = AgentScheduler(store,harness,asyncio.Semaphore(2))
        saved = store.create(definition(input_kind='tracker_events', fresh_inspections_per_run=0), ['cafe'])
        store.activate(saved['id'], time.time()+1000)
        await scheduler.tick()
        assert store.get(saved['id'])['runs_started'] == 0
        assert scheduler.tasks == {}
    asyncio.run(prove())


def test_capability_tool_is_scoped_and_credential_values_stay_private(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    agent = app.state.agent
    agent.children['parent'] = {'id':'parent','dataset':'cafe','owner_principal_id':'person-a','messages':[]}
    store = agent.custom_connector_store
    a = store.create(name='A',kind='api',description='A custom API',owner='person-a',collection='cafe')
    store.create(name='Other',kind='api',description='Another API',owner='person-b',collection='cafe')
    async def prove():
        result = await agent._tool('get_workspace_capabilities', {}, 'cafe', 'parent')
        assert result[2] is True, result
        text = json.dumps(result)
        assert a['id'] in text and 'Another API' not in text
        assert json.loads(result[0][0]['text'])['custom_connectors'][0]['execution'] == 'setup_draft'
        assert 'setup_available' in text
        denied = await agent._tool('get_workspace_capabilities', {}, 'warehouse', 'parent')
        assert denied[2] is False
    # Tool actors are host-owned records, backed by the same live AccessStore
    # membership as ordinary HTTP/native admission; a name alone grants nothing.
    with TestClient(app) as client:
        response=client.post('/api/session',json={'code':app.state.access.issue(
            'demo',principal_id='person-a')},headers=HEADERS)
        assert response.status_code==200
        asyncio.run(prove())


def test_ad_hoc_input_readiness_requires_indexed_recordings():
    from workspace_capabilities import WorkspaceCapabilities
    provider = retail_capabilities(SimpleNamespace(status=lambda:{'ready':False}, clips=[]))
    state = provider.execution(definition(mode='ad_hoc', input_kind='camera_footage'))
    assert state['ready'] is False
    assert state['required_input'] == 'indexed_recordings'


def test_saved_setup_chat_refreshes_mode_after_edit_elsewhere(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        client.post('/api/session', json={'code':app.state.access.issue('demo')}, headers=HEADERS)
        saved = client.post('/api/agents', json=definition(mode='ad_hoc', fresh_inspections_per_run=0),
                            headers=HEADERS).json()
        response = client.post('/api/chat', json={'chat_id':'edit-agent','dataset':'cafe','message':'Configure it.',
            'agent_setup':{'mode':'ad_hoc','agent_id':saved['id']}}, headers=HEADERS)
        assert response.status_code == 200
        assert client.get('/api/chats/edit-agent').json()['agent_setup']['mode'] == 'ad_hoc'
        updated = client.post(f"/api/agents/{saved['id']}/update",
            json={**definition(mode='scheduled', fresh_inspections_per_run=0), 'expected_revision':1}, headers=HEADERS)
        assert updated.status_code == 200
        assert client.get('/api/chats/edit-agent').json()['agent_setup']['mode'] == 'scheduled'
        # Refresh must be a projection of current saved state, preserving user text.
        assert app.state.agent.children['edit-agent']['messages'][0]['content'] == 'Configure it.'


def test_observer_reads_selected_owner_capabilities_and_children_keep_parent_control(tmp_path, monkeypatch):
    from test_custom_connectors import package
    app = make_app(tmp_path, monkeypatch)
    agent = app.state.agent
    store = agent.custom_connector_store
    own = store.create(name='Customer database', description='Delivery records',kind='sql',
                       owner='customer',collection='cafe')
    other = store.create(name='Other database', description='Other records',kind='sql',
                         owner='other',collection='cafe')
    store.save_package(own['id'], package(), expected_revision=1, owner='customer')
    store.set_secret(own['id'], 'DATABASE_URL', 'private-connection-value', owner='customer')
    agent.children['parent'] = {'id':'parent','dataset':'cafe','owner_principal_id':'customer','messages':[]}
    agent.children['child'] = {'id':'child','dataset':'cafe','owner_principal_id':'customer','parent_id':'parent','messages':[]}
    pending = store.request_credentials(own['id'], owner='customer',chat_id='parent',collection='cafe',reason='Verify access.')
    with TestClient(app) as customer, TestClient(app) as observer:
        customer.post('/api/session', json={'code':app.state.access.issue('demo',principal_id='customer')}, headers=HEADERS)
        observer.post('/api/session', json={'code':app.state.access.issue('demo',owner=True,principal_id='peter')}, headers=HEADERS)
        assert observer.post('/api/observer', json={'environment_id':'demo','principal_id':'customer'}, headers=HEADERS).status_code == 200
        capability = observer.get('/api/chats/parent').json()['workspace_capabilities']
        assert {row['id'] for row in capability['custom_connectors']} == {own['id']}
        assert capability['permissions'] == {'read_only':True,'parent_controlled':False,
                                             'configure_connections':False,'submit_credentials':False}
        assert capability['pending_credential_requests'][0]['id'] == pending['id']
        assert 'private-connection-value' not in json.dumps(capability)
        assert other['id'] not in json.dumps(capability)
        child = customer.get('/api/chats/child').json()['workspace_capabilities']
        assert child['permissions']['parent_controlled'] is True
        assert child['permissions']['configure_connections'] is False
        assert child['permissions']['submit_credentials'] is False
        assert child['recurring_agents']['configurable'] is False
        assert child['pending_credential_requests'] == []


def test_setup_context_checks_saved_agent_owner_and_business_before_chat_creation(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    calls = capture_native(monkeypatch, app.state.agent)
    saved = app.state.agent.automation_store.create(definition(mode='ad_hoc',fresh_inspections_per_run=0),
                                                    ['cafe'], owner_principal_id='other')
    with TestClient(app) as client:
        client.post('/api/session', json={'code':app.state.access.issue('demo',principal_id='customer')}, headers=HEADERS)
        response = client.post('/api/chat', json={'chat_id':'forged-setup','dataset':'cafe','message':'Configure it.',
            'agent_setup':{'mode':'scheduled','agent_id':saved['id']}}, headers=HEADERS)
        assert response.status_code == 404
        assert 'forged-setup' not in app.state.agent.children
        assert not calls


def test_custom_readiness_distinguishes_active_version_from_repair_and_runtime(tmp_path):
    from workspace_capabilities import WorkspaceCapabilities
    from test_custom_execution import setup,activate,package
    store,manager,key=setup(tmp_path);index=SimpleNamespace(status=lambda:{},clips=[])
    view=retail_capabilities(index,custom_connectors=store,custom_execution=manager)
    def read():return view.snapshot('shop',principal='alice')['custom_connectors'][0]
    assert read()['execution']=='prepared_package'
    activate(manager,key)
    assert read()['execution']=='active_read_tools' and read()['active_revision']==2
    store.save_package(key,package(),expected_revision=2,owner='alice')
    assert read()['revision']==3 and read()['active_revision']==2 and read()['execution']=='active_read_tools'
    manager.runtime=None
    assert read()['runtime_available']is False and read()['execution']=='prepared_package'


def test_recurring_agents_are_ready_once_the_person_has_a_committed_observation(tmp_path):
    """The snapshot every turn receives states what the scheduler does: an event-input agent runs on
    its interval and on new events once its owner reads one committed observation in the collection."""
    from monitoring import MonitoringStore
    from workspace_capabilities import WorkspaceCapabilities
    from retail_agent_inputs import OBSERVATION_NOTICE
    sources = {'a': {'id': 'a', 'video_id': 'recording-a', 'dataset': 'shop', 'start': 0, 'end': 20}}

    class Index:
        clips = list(sources.values())
        def status(self): return {'ready': False, 'datasets': ['shop']}
        def get_clip(self, key): return sources.get(key)

    async def prove():
        monitoring = MonitoringStore(tmp_path, source_lookup=lambda owner, key: sources.get(key))
        capability = retail_capabilities(Index(), monitoring=monitoring)
        recurring = lambda person='alice', **options: capability.snapshot('shop', principal=person, **options)['recurring_agents']
        assert recurring() == {'configurable': True, 'ready': False, 'notice': OBSERVATION_NOTICE}
        saved = monitoring.save_profile('alice', {'name': 'Product interactions', 'dataset': 'shop', 'vocabulary': [
            {'type': 'pick_up', 'description': 'Item visibly lifted from its original place.'}]}, datasets=['shop'])
        assert recurring()['ready'] is False, 'a saved profile holds zero observations'
        observed = monitoring.commit_event('alice', {'profile_id': saved['id'], 'profile_revision': saved['revision'], 'event_key': 'e1',
            'type': 'pick_up', 'description': 'Picks up a blue packet.', 'source': {'clip_id': 'a', 'start_s': 2, 'end_s': 3}, 'track_id': 'p1'})
        assert recurring() == {'configurable': True, 'ready': True, 'notice': None}
        # The fact is the scheduler's own: the same store answers a scheduled event-input agent, which then runs.
        agents = AgentStore(tmp_path,input_provider=RetailInputProvider(monitoring=monitoring,index=Index()))
        harness = SimpleNamespace(children={}, child_tasks={}, calls=[])
        harness._save_child = lambda record: harness.children.__setitem__(record['id'], record)
        async def turn(message, chat_id, dataset, **options):
            harness.calls.append(message)
            yield {'type': 'done', 'status': 'completed'}
        harness.turn = turn
        scheduler = AgentScheduler(agents, harness, asyncio.Semaphore(1))
        agent = agents.create({'title': 'Pickup summary', 'instruction': 'Summarize pickups.', 'dataset': 'shop',
            'agent_kind': 'tracker', 'input_kind': 'tracker_events', 'mode': 'scheduled', 'interval_seconds': 60},
            ['shop'], owner_principal_id='alice')
        assert capability.execution(agent)['ready'] is True
        agents.activate(agent['id'], time.time() + 1000)
        await scheduler.tick(); await asyncio.gather(*scheduler.tasks.values())
        assert agents.get(agent['id'])['last_run']['status'] == 'completed' and observed['id'] in harness.calls[0]
        # It is one person's fact in one collection, and it leaves the other facts as they were.
        assert recurring('bob')['ready'] is False and recurring(None)['ready'] is False
        assert capability.snapshot('warehouse', principal='alice')['recurring_agents']['ready'] is False
        assert recurring(read_only=True) == {'configurable': False, 'ready': True, 'notice': None}
        assert capability.snapshot('shop', principal='alice')['live_camera_feeds'] == {'ready': False, 'status': 'setup_available'}
        assert retail_capabilities(Index()).snapshot('shop', principal='alice')['recurring_agents']['ready'] is False
        # With no collection the line is the person's across the collections they have.
        assert capability.snapshot(None, principal='alice')['recurring_agents'] == {'configurable': True, 'ready': True, 'notice': None}
        assert capability.snapshot(None, principal='bob')['recurring_agents']['ready'] is False
        # The member of a turn, where the caller names one, is whose fact the line states.
        company = 'organization-0123456789abcdef0123456789abcdef'
        assert capability.snapshot('shop', principal=company)['recurring_agents']['ready'] is False
        assert capability.snapshot('shop', principal=company, actor='alice')['recurring_agents']['ready'] is True
        assert capability.snapshot('shop', principal=company, actor='bob')['recurring_agents']['ready'] is False
        # The line is as wide as the collection; one agent's own check follows the profiles it names.
        other = monitoring.save_profile('alice', {'name': 'Queue', 'dataset': 'shop', 'vocabulary': [
            {'type': 'entered_queue', 'description': 'A person joins the queue.'}]}, datasets=['shop'])
        narrow = agents.create({'title': 'Queue only', 'instruction': 'Follow the queue.', 'dataset': 'shop', 'agent_kind': 'tracker',
            'input_kind': 'tracker_events', 'mode': 'ongoing', 'profile_ids': [other['id']]}, ['shop'], owner_principal_id='alice')
        assert recurring()['ready'] is True and capability.execution(narrow)['ready'] is False
        assert capability.execution(narrow)['notice'] == 'Save source-backed observations to start this agent.'
    asyncio.run(prove())


def test_fixed_company_chat_readiness_follows_the_acting_member(tmp_path, monkeypatch):
    """A company chat keeps company ownership and reads readiness for each member's turn."""
    from retail_agent_inputs import OBSERVATION_NOTICE
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        code = app.state.access.issue('demo', principal_id='alice', display_name='Alice')
        assert client.post('/api/session', json={'code': code}, headers=HEADERS).status_code == 200
        selected = app.state.workspaces['demo']
        monitoring, agent, clip = selected['monitoring'], selected['agent'], selected['index'].clips[0]
        collection = clip['dataset']
        saved = monitoring.save_profile('alice', {'name': 'Shelves', 'dataset': collection, 'vocabulary': [
            {'type': 'pick_up', 'description': 'Item lifted.'}]}, datasets=[collection])
        monitoring.commit_event('alice', {'profile_id': saved['id'], 'profile_revision': saved['revision'], 'event_key': 'e1',
            'type': 'pick_up', 'description': 'Picks up a packet.', 'source': {'clip_id': clip['id'], 'start_s': 0, 'end_s': 1}, 'track_id': 'p1'})
        ready = {'configurable': True, 'ready': True, 'notice': None}
        # The member's own readiness, by collection and across their collections.
        assert client.get(f'/api/workspace/capabilities?dataset={collection}').json()['recurring_agents'] == ready
        assert client.get('/api/agents').json()['capabilities']['recurring_agents'] == ready
        made = client.post('/api/organization/chats', json={'purpose': 'organization', 'collection': collection}, headers=HEADERS).json()
        record = agent.children[made['id']]
        assert record['owner_principal_id'].startswith('organization-')
        agent._turn_senders = {**getattr(agent, '_turn_senders', {}), made['id']: {'principal_id': 'alice'}}
        assert agent.capability_snapshot(made['id'], collection)['recurring_agents'] == ready
        app.state.access.issue('demo', principal_id='bob', display_name='Bob')
        agent._turn_senders[made['id']] = {'principal_id': 'bob'}
        assert agent.capability_snapshot(made['id'], collection)['recurring_agents'] == {
            'configurable': True, 'ready': False, 'notice': OBSERVATION_NOTICE}
        assert agent.children[made['id']]['owner_principal_id'] == record['owner_principal_id']
