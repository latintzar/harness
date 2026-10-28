"""A model's connection proposal becomes a grant only through its owner's action."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from custom_setup import CustomSetup
from test_custom_execution import setup, package, target


def proposal(manager, key):
    return CustomSetup(manager).propose(key, target(), 'Read the inventory needed for shipment checks.',
        expected_revision=2, owner='alice', collection='shop')


def test_proposal_does_not_change_target_or_activate(tmp_path):
    store, manager, key = setup(tmp_path)
    changed = target(); changed['origin'] = 'https://new.example.com'
    control = CustomSetup(manager)
    before = manager.status(key, owner='alice', collection='shop')
    saved = control.propose(key, changed, 'Read the replacement inventory service.',
        expected_revision=2, owner='alice', collection='shop')
    assert manager.status(key, owner='alice', collection='shop') == before
    assert saved['status'] == 'proposed'
    approved = control.approve(key, saved['id'], owner='alice', collection='shop')
    assert approved['target']['origin'] == 'https://new.example.com'
    assert approved['generation'] == before['target_generation'] + 1
    assert manager.status(key, owner='alice', collection='shop')['active_revision'] is None


@pytest.mark.parametrize('change', ['owner', 'collection', 'connector', 'revision', 'grant'])
def test_approval_rechecks_current_owner_source_and_target(tmp_path, change):
    store, manager, key = setup(tmp_path)
    saved = proposal(manager, key); control = CustomSetup(manager)
    args = {'owner':'alice', 'collection':'shop'}
    if change == 'owner': args['owner'] = 'bob'
    elif change == 'collection': args['collection'] = 'warehouse'
    elif change == 'connector':
        key = store.create(name='Other', description='Read.', kind='api', owner='alice', collection='shop')['id']
    elif change == 'revision': store.save_package(key, package(), expected_revision=2, owner='alice')
    else:
        changed = target(); changed['origin'] = 'https://other.example.com'
        manager.grant_target(key, changed, owner='alice', collection='shop', expected_generation=1)
    with pytest.raises(ValueError): control.approve(key, saved['id'], **args)


def test_approval_race_checks_revision_in_grant_transaction(tmp_path, monkeypatch):
    store, manager, key = setup(tmp_path); saved = proposal(manager, key)
    original = manager.grant_target
    def race(*args, **kwargs):
        store.save_package(key, package(), expected_revision=2, owner='alice')
        return original(*args, **kwargs)
    monkeypatch.setattr(manager, 'grant_target', race)
    with pytest.raises(ValueError): CustomSetup(manager).approve(key, saved['id'], owner='alice', collection='shop')


@pytest.mark.parametrize('change', ['credential', 'extra_operation', 'missing_operation', 'private_url'])
def test_proposals_expose_only_current_declared_read_contract(tmp_path, change):
    store, manager, key = setup(tmp_path); value = target()
    if change == 'credential': value['origin'] = 'https://private-customer-token.example.com'
    elif change == 'extra_operation':
        extra = copy.deepcopy(value['operations'][0]); extra['id'] = 'extra'; value['operations'].append(extra)
    elif change == 'missing_operation': value['operations'][0]['id'] = 'other'
    else: value['origin'] = 'http://127.0.0.1'
    with pytest.raises(ValueError): CustomSetup(manager).propose(key, value, 'Read inventory.',
        expected_revision=2, owner='alice', collection='shop')


def test_view_is_scoped_and_survives_restart_with_stale_marking(tmp_path):
    store, manager, key = setup(tmp_path); saved = proposal(manager, key)
    control = CustomSetup(manager)
    view = control.describe(key, owner='alice', collection='shop')
    assert view['proposal']['id'] == saved['id']
    assert view['proposal']['current'] is True
    assert view['tools'][0]['name'] == 'list_inventory'
    assert 'private-customer-token' not in json.dumps(view)
    store.save_package(key, package(), expected_revision=2, owner='alice')
    assert control.describe(key, owner='alice', collection='shop')['proposal']['current'] is False
    with pytest.raises(ValueError): control.describe(key, owner='bob', collection='shop')


def test_proposal_is_idempotent_and_bounded(tmp_path):
    store, manager, key = setup(tmp_path)
    one = proposal(manager, key); two = proposal(manager, key)
    assert one['id'] == two['id']
    control = CustomSetup(manager)
    for i in range(31):
        control.propose(key, target(), f'Read inventory check {i}.', expected_revision=2, owner='alice', collection='shop')
    with pytest.raises(ValueError):
        control.propose(key, target(), 'Another proposal.', expected_revision=2, owner='alice', collection='shop')


def test_new_secret_cannot_be_an_existing_public_proposal_value(tmp_path):
    store, manager, key = setup(tmp_path)
    CustomSetup(manager).propose(key, target(), 'Review secret-shaped-marker in public configuration.',
        expected_revision=2, owner='alice', collection='shop')
    with pytest.raises(ValueError):
        store.set_secret(key, 'API_TOKEN', 'secret-shaped-marker', owner='alice')


def test_native_tools_accept_manifest_and_propose_without_owner_authority(tmp_path):
    import asyncio
    from jsonschema import Draft202012Validator
    from agent_bridge import CodexVideoAgent, tool_specs
    store, manager, key = setup(tmp_path)
    agent = CodexVideoAgent(tmp_path, object(), lambda:{})
    agent.custom_connector_store = store
    agent.custom_execution_manager = manager
    agent.children['chat'] = {'id':'chat','owner_principal_id':'alice','dataset':'shop'}
    specs = {item['name']:item for item in tool_specs()}
    assert Draft202012Validator(specs['save_custom_connector']['inputSchema']).is_valid({
        'connector_id':key,'expected_revision':2,'package':package()})
    assert 'propose_custom_connector_target' in specs
    assert 'propose_custom_connector_target' not in {item['name'] for item in tool_specs(False)}
    assert 'read_custom_connector' in {item['name'] for item in tool_specs(False)}
    async def scenario():
        args = {'connector_id':key, 'target':target(), 'reason':'Read inventory.', 'expected_revision':2}
        output, _, ok = await agent._tool('propose_custom_connector_target', args, scope='shop', chat_id='chat')
        assert ok and json.loads(output[0]['text'])['status'] == 'proposed'
        assert manager.status(key, owner='alice', collection='shop')['target_generation'] == 1
        output, _, ok = await agent._tool('get_custom_connector', {'connector_id':key}, scope='shop', chat_id='chat')
        assert ok and json.loads(output[0]['text'])['execution_setup']['proposal']['current']
        for name, value in [('test_custom_connector', {'connector_id':key,'expected_revision':2}),
                            ('read_custom_connector', {'connector_id':key,'tool':'list_inventory','arguments':{}})]:
            _, _, ok = await agent._tool(name, value, scope='shop', chat_id='chat')
            assert not ok  # A manifest cannot commission its own runtime.
        agent.children['chat']['parent_id'] = 'parent'
        _, _, ok = await agent._tool('propose_custom_connector_target', args, scope='shop', chat_id='chat')
        assert not ok
        class ScopedRead:
            async def invoke(self, connector_id, tool, arguments, *, owner, collection, cancelled):
                assert (connector_id,tool,arguments,owner,collection)==(key,'list_inventory',{},'alice','shop')
                return {'result':{'inventory':[]}}
        agent.custom_coordinator=ScopedRead()
        output, _, ok = await agent._tool('read_custom_connector', {'connector_id':key,'tool':'list_inventory','arguments':{}},scope='shop',chat_id='chat')
        assert ok and json.loads(output[0]['text'])['result']=={'inventory':[]}
    asyncio.run(scenario())


def test_owner_http_approves_saved_proposal_and_requires_actual_runtime(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        owner = client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()['principal']['id']
        store = app.state.agent.custom_connector_store
        manager = app.state.agent.custom_execution_manager
        key = store.create(name='Inventory',description='Read inventory.',kind='api',owner=owner,collection='cafe')['id']
        store.save_package(key,package(),expected_revision=1,owner=owner)
        saved = CustomSetup(manager).propose(key,target(),'Read inventory.',expected_revision=2,owner=owner,collection='cafe')
        root = f'/api/connections/custom/{key}'
        detail = client.get(root).json()
        assert detail['execution_setup']['proposal']['id']==saved['id']
        assert not detail['execution_setup']['runtime_available']
        assert client.post(root+'/execution/target',json={'proposal_id':saved['id']}).status_code==403
        assert client.post(root+'/execution/target',json={'proposal_id':saved['id'],'target':target()},headers=HEADERS).status_code==400
        approved = client.post(root+'/execution/target',json={'proposal_id':saved['id']},headers=HEADERS)
        assert approved.status_code==200 and approved.json()['generation']==1
        assert approved.headers['cache-control']=='private, no-store'
        assert client.post(root+'/execution/test',json={'expected_revision':2},headers=HEADERS).status_code==409
        assert client.post(root+'/execution/activate',json={'test_id':'fabricated','expected_active_generation':0},headers=HEADERS).status_code==409
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        assert client.get(root+'/execution').status_code==404
        assert client.post(root+'/execution/target',json={'proposal_id':saved['id']},headers=HEADERS).status_code==404
