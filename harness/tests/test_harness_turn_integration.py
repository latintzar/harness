"""Both native transports use the current vertical; company chats retain member authority."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent_bridge
import pack
from background_agents import AgentStore
from customer_workflows import CUSTOMER_WORKFLOW_INSTRUCTIONS
from harness_tools import developer_prompt
from deploy.migration.legacy.isolated_turn import isolated_instructions
from organization_chats import OrganizationChats
from test_isolated_turn import Launcher, NativeProtocol, root_agent
from workspace_capabilities import WorkspaceCapabilities


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('runtime', ['host', 'isolated'])
@pytest.mark.parametrize('template', ['retail', Path(__file__).parent / 'verticals' / 'jobshop'], ids=['retail', 'jobshop'])
async def test_each_transport_composes_active_vertical_and_harness_on_start_and_resume(tmp_path, monkeypatch, runtime, template):
    with pack.use(template) as active:
        agent = root_agent(tmp_path)
        agent.registry.clear()
        protocol = NativeProtocol()
        protocol.returncode = 0
        if runtime == 'isolated':
            agent.isolated_launcher = Launcher(agent, protocol)
            monkeypatch.setattr('deploy.migration.legacy.isolated_turn.QUIET_SECONDS', .01)
        else:
            @asynccontextmanager
            async def lease(**kwargs):
                yield SimpleNamespace(child_env={}, cli_args=[], cli_config={}, mode='openai_api', login_status_required=False)
            async def launch(*args, **kwargs):
                return protocol
            agent.auth = SimpleNamespace(lease=lease)
            monkeypatch.setattr(agent_bridge.asyncio, 'create_subprocess_exec', launch)
            monkeypatch.setattr(agent_bridge.shutil, 'which', lambda name: '/fixture/codex')
            monkeypatch.setattr(agent_bridge, 'startup_policy', lambda *args: {})
        expected = '\n'.join((active.prompt('system'), CUSTOMER_WORKFLOW_INSTRUCTIONS, developer_prompt(active)))
        if runtime == 'isolated':
            expected = isolated_instructions(expected)
        for message, method in [('Report', 'thread/start'), ('Follow up', 'thread/resume')]:
            protocol.calls.clear()
            output = [value async for value in agent._turn(message, 'chat', 'cafe')]
            assert not any(value['type'] == 'error' for value in output), output
            request = next(params for name, params in protocol.calls if name == method)
            assert request['developerInstructions'] == expected
            assert request['developerInstructions'].count(CUSTOMER_WORKFLOW_INSTRUCTIONS) == 1
            assert request['developerInstructions'].count(developer_prompt(active)) == 1


def company_agent(tmp_path):
    index = SimpleNamespace(status=lambda: {'ready': False, 'datasets': ['cafe']}, clips=[])
    agent = agent_bridge.CodexVideoAgent(tmp_path, index, {})
    organization = OrganizationChats(tmp_path, environment_id='demo', member=lambda person: person in {'alice', 'bob'},
        records=lambda: agent.children)
    agent.organization_chats = organization
    record = organization.ensure(agent, 'cafe', 'organization')
    agent._turn_senders = {record['id']: {'principal_id': 'alice', 'channel': 'web'}}
    agent.automation_store = AgentStore(tmp_path)
    return agent, record


def definition(**changes):
    return {'title': 'Daily observations', 'instruction': 'Summarize the committed observations.',
        'mode': 'ad_hoc', 'agent_kind': 'tracker', 'input_kind': 'tracker_events', **changes}


def test_fixed_chat_readiness_uses_member_while_connections_keep_company_scope(tmp_path):
    agent, record = company_agent(tmp_path)
    queried = []
    def connections(owner):
        queried.append(owner)
        return {'connections': []}
    agent.workspace_capabilities = WorkspaceCapabilities(agent.retrieval,
        connections=SimpleNamespace(catalogue=connections),
        monitoring=SimpleNamespace(list_events=lambda owner, collection, limit: [{'id': 'event'}] if owner == 'alice' else []))
    assert agent.capability_snapshot(record['id'], 'cafe')['recurring_agents']['ready'] is True
    agent._turn_senders[record['id']]['principal_id'] = 'bob'
    assert agent.capability_snapshot(record['id'], 'cafe')['recurring_agents']['ready'] is False
    assert queried == [agent.organization_chats.owner, agent.organization_chats.owner]


@pytest.mark.anyio
async def test_fixed_chat_proposal_creates_member_owned_agent_and_separate_conversation(tmp_path):
    agent, record = company_agent(tmp_path)
    # A legacy setup field cannot promote a shared fixed chat to someone's personal agent.
    record['agent_setup'] = {'mode': 'ad_hoc'}
    result = await agent._scoped_tool('propose_agent', definition(), 'cafe', record['id'])
    assert result[2] is True, result
    saved = json.loads(result[0][0]['text'])
    assert saved['owner_principal_id'] == 'alice'
    assert saved['chat_id'] != record['id']
    assert agent.children[saved['chat_id']]['owner_principal_id'] == 'alice'
    assert agent.organization_chats.canonical(record)
    assert 'agent_id' not in record


@pytest.mark.anyio
@pytest.mark.parametrize('tool', ['get_agent', 'update_agent'])
async def test_fixed_chat_agent_access_follows_current_member(tmp_path, tool):
    agent, record = company_agent(tmp_path)
    saved = agent.automation_store.create(definition(dataset='cafe'), ['cafe'], owner_principal_id='alice')
    args = {'agent_id': saved['id']}
    if tool == 'update_agent':
        args.update(definition(title='Updated observations'), expected_revision=1)
    result = await agent._scoped_tool(tool, args, 'cafe', record['id'])
    assert result[2] is True, result
    current = agent.automation_store.get(saved['id'])
    agent._turn_senders[record['id']]['principal_id'] = 'bob'
    args['expected_revision'] = current['revision']
    denied = await agent._scoped_tool(tool, args, 'cafe', record['id'])
    assert denied[2] is False
    assert agent.automation_store.get(saved['id']) == current


@pytest.mark.anyio
@pytest.mark.parametrize('sender', [None, 'outsider'])
async def test_fixed_chat_requires_live_member_before_proposal(tmp_path, sender):
    agent, record = company_agent(tmp_path)
    agent._turn_senders[record['id']] = {'principal_id': sender}
    result = await agent._scoped_tool('propose_agent', definition(), 'cafe', record['id'])
    assert result[2] is False
    assert agent.automation_store.list() == []
