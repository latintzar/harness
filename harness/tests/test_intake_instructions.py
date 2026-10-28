"""Exercise onboarding policy through the actual native fresh/resume protocol."""
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import pack
from agent_bridge import tool_specs
from intake_instructions import for_chat, CAMERA, ORGANIZATION
from deploy.migration.legacy.native_policy import isolated_thread_params
from organization_chats import OrganizationChats
from skills import SkillStore
from test_isolated_turn import root_agent, NativeProtocol

# A second template (tests/verticals/jobshop) with an Office conversation in place of Cameras.
JOBSHOP = Path(__file__).resolve().parent / 'verticals' / 'jobshop'
RETAIL_TEXT = {'cameras': CAMERA, 'organization': ORGANIZATION}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('template,purpose,heading', [('retail', 'cameras', 'Company intake conversation:'),
    ('retail', 'organization', 'Company intake conversation:'), (JOBSHOP, 'office', 'Job shop conversation:')],
    ids=['cameras', 'organization', 'office'])
async def test_actual_native_fresh_and_resumed_company_chat_receive_purpose_policy(tmp_path, template, purpose, heading):
    with pack.use(template) as active:
        expected = active.fixed_chat_text(purpose)
        assert expected and RETAIL_TEXT.get(purpose, expected) == expected
        await fresh_and_resumed_turns_receive(tmp_path, active, purpose, expected, heading)


async def fresh_and_resumed_turns_receive(tmp_path, active, purpose, expected, heading):
    agent = root_agent(tmp_path)
    org = OrganizationChats(tmp_path, environment_id='demo', member=lambda p:p == 'alice', records=lambda:agent.children)
    agent.organization_chats = org
    record = org.ensure(agent, 'cafe', purpose)
    protocol = NativeProtocol()

    class Launcher:
        enabled = True
        company_id = org.company_id

        @asynccontextmanager
        async def lease(self, scope, run_id):
            assert scope.owner_id == org.owner
            class Session:
                process = protocol
                stopped = False
                _closing = False
                def thread_params(self, instructions, tools):
                    return isolated_thread_params(instructions, tools)
                async def bind_mcp(self, mcp):
                    return {'keyPath':'mcp_servers', 'value':{'rocoto':{'private':'scoped-token'}}, 'mergeStrategy':'replace'}
                async def stop(self):
                    self._closing = True
                    self.stopped = True
            session = Session()
            try:
                yield session
            finally:
                await session.stop()

    agent.isolated_launcher = Launcher()
    for expected_method in ('thread/start', 'thread/resume'):
        protocol.calls.clear()
        output = [event async for event in agent._turn('Continue setup', record['id'], 'cafe')]
        assert not any(event['type'] == 'error' for event in output), output
        params = next(params for method, params in protocol.calls if method == expected_method)
        assert expected in params['developerInstructions']
        assert heading in params['developerInstructions']
        assert active.prompt('fixed_chat_common') + '\n' + expected in params['developerInstructions']
        if active.id == 'retail':
            assert 'private credential' in params['developerInstructions']
        else:
            # Another template's conversation carries its own standing text and none of retail's.
            assert 'Company intake conversation:' not in params['developerInstructions']
            assert CAMERA not in params['developerInstructions'] and ORGANIZATION not in params['developerInstructions']
        assert 'baseInstructions' not in params
        assert agent.registry[record['id']] == 'isolated-root'
        # Host onboarding guidance remains developer policy, not a forged user utterance.
        turn_input = next(params['input'] for method, params in protocol.calls if method == 'turn/start')
        assert expected not in json.dumps(turn_input)


def test_forged_purpose_and_personal_conversation_do_not_acquire_company_instructions(tmp_path):
    agent = root_agent(tmp_path)
    org = OrganizationChats(tmp_path, environment_id='demo', member=lambda p:True)
    agent.organization_chats = org
    agent.children['chat'].update(kind='organization_chat', organization_purpose='cameras')
    assert for_chat(agent, 'chat', 'base') == 'base'
    record = org.ensure(agent, 'cafe', 'messaging')
    assert for_chat(agent, record['id'], 'base') == 'base'


def test_registered_intake_skills_resolve_only_with_existing_capabilities(tmp_path):
    store = SkillStore(tmp_path)
    names = {spec['name'] for spec in tool_specs(True)}
    for key in ('camera-intake', 'organization-intake'):
        skill = store.install(key, expected_revision=1)
        assert set(skill['required_tools']) <= names
        snapshot = store.resolve([key], available_tools=names)
        assert snapshot['authority'] == 'instructions_only'
        assert 'Company intake conversation:' in snapshot['skills'][0]['instructions']
