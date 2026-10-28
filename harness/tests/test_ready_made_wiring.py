"""Ready-made selections become scoped inactive drafts before any model turn."""
import asyncio
import dataclasses
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent_catalogue
import pack
from test_chat_message_context import capture_native
from test_skill_runtime import HEADERS, make_app


def test_catalogue_offers_available_agents_and_retired_chats_remain_readable(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        session = client.post('/api/session', json={'code': app.state.access.issue('demo')}, headers=HEADERS).json()
        assert client.get('/api/agents/catalogue').json() == agent_catalogue.available()
        current = client.get('/api/ready-agents')
        assert current.status_code == 200
        assert current.json() == {'publisher': 'Rocoto', 'ready_agents': agent_catalogue.available()['templates']}
        assert 'templates' not in current.json()
        retired = agent_catalogue.retired()['templates'][0]
        response = client.post('/api/chat', json={'chat_id': 'retired-new', 'dataset': 'cafe', 'message': 'Configure it.',
            'agent_setup': {'template_id': retired['id'], 'mode': 'ad_hoc'}}, headers=HEADERS)
        assert response.status_code == 400
        agent = app.state.agent
        assert 'retired-new' not in agent.children
        agent.children['retired-old'] = {'id': 'retired-old', 'dataset': 'cafe', 'parent_id': None,
            'owner_principal_id': session['principal']['id'], 'messages': [],
            'agent_setup': {'template_id': retired['id'], 'mode': retired['mode']}}
        assert agent.agent_setup_context('retired-old')['template']['id'] == retired['id']


@pytest.mark.parametrize('template_id', ['queue-watch', 'empty-shelf-watch', 'shop-summary'])
def test_selection_saves_exact_defaults_and_only_the_owners_matching_profiles(tmp_path, monkeypatch, template_id):
    app = make_app(tmp_path, monkeypatch)
    calls = capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id='alice')}, headers=HEADERS)
        app.state.access.issue('demo', principal_id='bob')
        monitor, store = app.state.agent.monitoring_store, app.state.agent.automation_store
        def profile(owner, dataset, kind):
            return monitor.save_profile(owner, {'name': kind, 'dataset': dataset,
                'vocabulary': [{'type': kind, 'description': 'A committed observation.'}]}, datasets=['cafe', 'elsewhere'])
        profile('alice', 'cafe', 'entered_queue')
        profile('alice', 'cafe', 'display_empty')
        profile('alice', 'cafe', 'unrelated')
        profile('alice', 'elsewhere', 'entered_queue')
        profile('bob', 'cafe', 'entered_queue')
        expected = agent_catalogue.draft(template_id, 'cafe', monitor.list_profiles('alice', 'cafe'))
        response = client.post('/api/chat', json={'chat_id': 'ready-made', 'dataset': 'cafe', 'message': 'Configure it.',
            'agent_setup': {'template_id': template_id, 'mode': 'ad_hoc', 'agent_kind': 'action', 'input_kind': 'camera_footage'}}, headers=HEADERS)
        assert response.status_code == 200, response.text
        saved = store.list(owner_principal_id='alice')
        assert len(saved) == 1
        saved = saved[0]
        assert {key: saved[key] for key in expected} == expected
        assert saved['status'] == 'draft' and saved['runs_started'] == 0 and saved['expires_at'] is None
        assert saved['chat_id'] == 'ready-made' and store.list(owner_principal_id='bob') == []
        chat = client.get('/api/chats/ready-made').json()
        assert chat['kind'] == 'agent_conversation' and chat['agent_id'] == saved['id']
        assert chat['messages'][0]['content'] == 'Configure it.'
        assert client.post('/api/chat', json={'chat_id': 'ready-made', 'dataset': 'cafe', 'message': 'Continue.'}, headers=HEADERS).status_code == 200
        assert len(store.list(owner_principal_id='alice')) == 1
        assert len(calls) == 2



def test_ready_made_with_no_matching_profile_waits_for_its_own_event_types(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id='alice')}, headers=HEADERS)
        selected = app.state.workspaces['demo']
        monitor, store = selected['monitoring'], selected['agents']
        profile = monitor.save_profile('alice', {'name': 'Shelves', 'dataset': 'cafe',
            'vocabulary': [{'type': 'display_empty', 'description': 'A shelf is empty.'}]}, datasets=['cafe'])
        response = client.post('/api/chat', json={'chat_id': 'queue', 'dataset': 'cafe', 'message': 'Set it up.',
            'agent_setup': {'template_id': 'queue-watch', 'mode': 'ongoing'}}, headers=HEADERS)
        assert response.status_code == 200, response.text
        saved = store.list(owner_principal_id='alice')[0]
        assert saved['profile_ids'] == []
        assert saved['event_types'] == agent_catalogue.template('queue-watch')['event_types']
        alerts = []
        monkeypatch.setattr(selected['phone_delivery'], 'fan_out', lambda *args, **kwargs: alerts.append(args))
        monitor.commit_event('alice', {'profile_id': profile['id'], 'profile_revision': profile['revision'],
            'event_key': 'shelf-only', 'type': 'display_empty', 'description': 'An empty shelf.',
            'source': {'clip_id': selected['index'].clips[0]['id'], 'start_s': 0, 'end_s': 1}, 'track_id': 'p1'})
        assert monitor.events_for(saved) == []
        assert selected['agent'].workspace_capabilities.execution(saved)['ready'] is False
        assert alerts == []


def test_old_client_edit_preserves_ready_made_event_scope(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    capture_native(monkeypatch, app.state.agent)
    with TestClient(app) as client:
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id='alice')}, headers=HEADERS)
        response = client.post('/api/chat', json={'chat_id': 'queue', 'dataset': 'cafe', 'message': 'Set it up.',
            'agent_setup': {'template_id': 'queue-watch', 'mode': 'ongoing'}}, headers=HEADERS)
        assert response.status_code == 200, response.text
        store = app.state.workspaces['demo']['agents']
        saved = store.list(owner_principal_id='alice')[0]
        response = client.post(f"/api/agents/{saved['id']}/update", headers=HEADERS, json={
            'title': 'Queue tracker', 'instruction': 'Keep the queue display current.', 'dataset': 'cafe',
            'mode': 'ongoing', 'interval_seconds': saved['interval_seconds'], 'expected_revision': saved['revision']})
        assert response.status_code == 200, response.text
        assert response.json()['event_types'] == saved['event_types']
        assert store.get(saved['id'])['event_types'] == saved['event_types']


def test_unrestricted_ready_made_follows_every_profile_in_its_collection():
    retail = pack.active()
    entry = {**retail.ready_agents()[0], 'event_types': []}
    with pack.use(dataclasses.replace(retail, _templates=(entry,))):
        result = agent_catalogue.draft(entry['id'], 'shop', [
            {'id': 'a', 'dataset': 'shop', 'vocabulary': [{'type': 'restocked'}]},
            {'id': 'b', 'dataset': 'shop', 'vocabulary': []},
            {'id': 'other', 'dataset': 'elsewhere', 'vocabulary': []}])
    assert result['profile_ids'] == ['a', 'b']


def test_local_mcp_exposes_scoped_script_and_presentation_reads(tmp_path, monkeypatch):
    import mcp_server
    import retail_mcp_services
    import tracker_scripts
    import tracker_presentation
    import retail_business_scope as business_scope
    from mcp.server.fastmcp.exceptions import ToolError
    from test_local_mcp_scope import Index, legacy, call
    monkeypatch.setattr(retail_mcp_services, 'QdrantRetrievalIndex', lambda _: Index([legacy('clip', 'shop')]))
    seen = []
    def read(context, name, args):
        seen.append((context.owner, context.dataset, name, args))
        return {'name': name, **args}
    monkeypatch.setattr(tracker_scripts, 'read', read)
    monkeypatch.setattr(tracker_presentation, 'read', read)
    current = [True]
    original = business_scope.legacy_scope_current
    monkeypatch.setattr(business_scope, 'legacy_scope_current', lambda *args: current[0] and original(*args))
    async def prove():
        bound = mcp_server.make_server(tmp_path, principal_id='alice', dataset='shop')
        anonymous = mcp_server.make_server(tmp_path)
        names = {'get_tracker_script', 'get_tracker_presentation'}
        tools = {tool.name: tool for tool in await bound.list_tools()}
        assert names <= tools.keys()
        assert all(set(tools[name].inputSchema['properties']) == {'tracker_id', 'revision'} for name in names)
        assert names.isdisjoint({tool.name for tool in await anonymous.list_tools()})
        for name in names:
            assert await call(bound, name, {'tracker_id': 'mine'}) == {'name': name, 'tracker_id': 'mine'}
            assert await call(bound, name, {'tracker_id': 'mine', 'revision': 2}) == {'name': name, 'tracker_id': 'mine', 'revision': 2}
        assert all(owner == 'alice' and dataset == 'shop' for owner, dataset, _, _ in seen)
        count = len(seen)
        current[0] = False
        for name in names:
            with pytest.raises(ToolError, match='current location assignments'):
                await call(bound, name, {'tracker_id': 'mine'})
        assert len(seen) == count
    asyncio.run(prove())


def test_local_mcp_script_contract_is_readable_only_for_its_own_saved_agent(tmp_path, monkeypatch):
    import mcp_server
    import retail_mcp_services
    from agent_test_support import retail_agent_store as AgentStore
    from retail_agent_inputs import RetailInputProvider
    from mcp.server.fastmcp.exceptions import ToolError
    from test_local_mcp_scope import Index, legacy, call
    monkeypatch.setattr(retail_mcp_services, 'QdrantRetrievalIndex', lambda _: Index([legacy('clip', 'shop')]))
    saved = AgentStore(tmp_path,input_provider=RetailInputProvider()).create({'title': 'My tracker', 'instruction': 'Keep a display current.',
        'dataset': 'shop', 'agent_kind': 'tracker', 'input_kind': 'tracker_events'}, ['shop'], owner_principal_id='alice')
    async def prove():
        mine = mcp_server.make_server(tmp_path, principal_id='alice', dataset='shop')
        answer = await call(mine, 'get_tracker_script', {'tracker_id': saved['id']})
        assert answer['agent_id'] == saved['id'] and answer['script'] is None and answer['contract']
        other = mcp_server.make_server(tmp_path, principal_id='bob', dataset='shop')
        with pytest.raises(ToolError):
            await call(other, 'get_tracker_script', {'tracker_id': saved['id']})
    asyncio.run(prove())
