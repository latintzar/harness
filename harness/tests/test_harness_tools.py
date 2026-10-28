"""The harness describes itself: get_harness, its capability table and the developer paragraph.

Every answer here is read from the running code: the active template (pack.py), the live tool
registry (agent_bridge.tool_specs, monitoring_tools.FAMILIES, the MCP read lists), the layer map
and the stores a surface hands a read. The job-shop test template under tests/packs/jobshop is
the second segment: under it the same tool answers Office, Dispatch and Desk.
"""
import asyncio
import json
from pathlib import Path
import re
import shutil
import sys

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import layout
import harness_tools
import layers
import monitoring_tools
import pack
from agent_test_support import retail_agent_store as AgentStore
from retail_agent_inputs import RetailInputProvider
from monitoring import MonitoringStore
from skills import SkillStore

JOBSHOP = Path(__file__).resolve().parent / 'verticals' / 'jobshop'
OWNER, OTHER = 'person-7f3a-alice', 'person-9c1d-bob'
SOURCES = {'a': {'id': 'a', 'video_id': 'recording-a', 'dataset': 'shop', 'start': 0, 'end': 20}}
# The copy gate's own pattern (tools/copy-gate.py): text a person or a model reads says what is.
NEGATIVE = re.compile(r"(?i)(^|[\s>«(·'\"`])(no|not|nothing|never|nobody|none|don't|doesn't|can't|cannot|won't|isn't|aren't|"
                      r"didn't|wasn't|hasn't|haven't|without|unless)\b(?! *=)")
# The capabilities the map of 11 October 2026 named, section F, by the id each has in the table.
NAMED = (
    'live_camera_viewing', 'home_numbers', 'product_names_and_prices', 'sales_and_conversion', 'queue_and_shelf_trackers',
    'agents_running_unattended', 'phone_alerts', 'viber_and_sms', 'onboarding_conversations', 'ready_made_agents',
    'customer_mcp_access', 'action_agents', 'webhooks_in', 'webhooks_out', 'scheduled_work', 'agent_set_wakes',
    'cheap_screening', 'sandbox_and_custom_connectors', 'self_description', 'postgresql')
# What the owner asked for beside them on that day: the demo's data said plainly, trackers that run
# by themselves, the scripts and the views the harness makes, and the registry of data sources.
ALSO = ('demo_data', 'tracker_runner', 'tracker_scripts', 'tracker_views', 'data_sources', 'vertical')
# Words of the retail segment. An answer under another template holds them in its tool names alone:
# the registry of this service still carries retail's tools, and `tools` lists what it holds.
RETAIL_WORDS = re.compile(r"(?i)\b(retail|cameras?|shel(f|ves)|queue (watch|time)|till|catalog|footage|recordings?|cosmos|"
                          r"shop summary|core trackers?|home|sales|conversion|visitors?|basket)\b")


class View:
    """The scoped index view a read arrives with: one company, every source in reach."""
    business_scopes = []
    def get_clip(self, key): return SOURCES.get(key)


def company(state, *, agents=True):
    """One company's stores as a surface hands them to a read: (the scoped store, the agent store)."""
    store = MonitoringStore(state, source_lookup=lambda owner, key: SOURCES.get(key))
    return monitoring_tools.scoped(store, View()), AgentStore(state, input_provider=RetailInputProvider(monitoring=store)) if agents else None


def ask(state, section='all', *, owner=OWNER, agents=True, stores=None):
    """get_harness through the registry's own read, as every surface calls it."""
    store, held = stores or company(state, agents=agents)
    return monitoring_tools.read(store, owner, 'shop', 'get_harness', {} if section == 'all' else {'section': section}, agents=held)


def strings(value, where=''):
    """Every (place, text) of an answer: its keys and its values."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield where, key
            yield from strings(item, f'{where}/{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, f'{where}[{index}]')
    elif isinstance(value, str):
        yield where, value


@pytest.fixture
def retail():
    with pack.use('retail') as template:
        yield template


@pytest.fixture
def jobshop():
    with pack.use(JOBSHOP) as template:
        yield template


# The tool and where it is offered.

def test_get_harness_is_one_read_of_the_registry_on_every_tool_surface(retail):
    import native_mcp
    import public_mcp
    from agent_bridge import tool_specs
    assert harness_tools in monitoring_tools.FAMILIES
    assert harness_tools.READ_NAMES == {'get_harness'} and harness_tools.WRITE_NAMES == frozenset()
    assert 'get_harness' in monitoring_tools.READ_NAMES and 'get_harness' not in monitoring_tools.WRITE_NAMES
    # A registry read reaches the parent conversation, a sub-agent, native sub-agents and a customer's own agent.
    for names in ({spec['name'] for spec in tool_specs(True)}, {spec['name'] for spec in tool_specs(False)},
                  native_mcp.READ_TOOLS, public_mcp.READ_TOOLS):
        assert 'get_harness' in names
    spec, = harness_tools.specs(True)
    assert harness_tools.specs(False) == [spec] and spec['name'] == 'get_harness'
    assert spec['inputSchema'] == {'type': 'object', 'properties': {'section': {'type': 'string', 'enum': list(harness_tools.SECTIONS)}},
                                   'required': [], 'additionalProperties': False}
    with pytest.raises(ValueError):
        harness_tools.call(None, 'get_harness', {})


def test_the_tool_lists_are_the_live_registry(tmp_path, retail):
    import native_mcp
    import public_mcp
    from agent_bridge import tool_specs
    tools = ask(tmp_path, 'tools')['tools']
    parent, child = [spec['name'] for spec in tool_specs(True)], [spec['name'] for spec in tool_specs(False)]
    assert [row['name'] for row in tools['conversation']] == parent
    assert tools['sub_agent'] == child
    assert set(tools['customer_mcp']) == set(public_mcp.READ_TOOLS)
    assert set(tools['native_sub_agent']) == set(native_mcp.READ_TOOLS) & set(child)
    assert tools['counts'] == {'conversation': len(parent), 'sub_agent': len(child),
                               'native_sub_agent': len(tools['native_sub_agent']), 'customer_mcp': len(public_mcp.READ_TOOLS)}
    by_name = {row['name']: row for row in tools['conversation']}
    # A tool of a family carries the family and its effect; a tool of the conversation's own list says so.
    assert by_name['get_harness'] == {'name': 'get_harness', 'family': 'harness', 'effect': 'read',
                                      **({'layer': layers.LAYER['harness_tools']} if 'harness_tools' in layers.LAYER else {})}
    assert (by_name['list_trackers']['family'], by_name['list_trackers']['effect'], by_name['list_trackers']['layer']) == ('trackers', 'read', 3)
    assert (by_name['install_core_trackers']['family'], by_name['install_core_trackers']['effect']) == ('trackers', 'write')
    assert (by_name['record_tracker_event']['family'], by_name['record_tracker_event']['effect']) == ('observations', 'write')
    assert by_name['set_chat_title']['family'] == 'conversation'
    assert by_name['set_chat_title']['effect'] == 'write'
    assert by_name['get_business_profile']['effect'] == 'read'
    # Every registry name has its family, every write stays a tool of the parent conversation.
    registered = {name: family for family in monitoring_tools.FAMILIES for name in family.READ_NAMES | family.WRITE_NAMES}
    assert all(by_name[name]['family'] != 'conversation' for name in registered)
    assert {row['name'] for row in tools['conversation'] if row['effect'] == 'write'} == harness_tools.registry().writes
    assert monitoring_tools.WRITE_NAMES.isdisjoint(tools['sub_agent'] + tools['customer_mcp'] + tools['native_sub_agent'])
    families = {row['name']: row for row in tools['families']}
    assert families['harness'] == {'name': 'harness', 'reads': 1, 'writes': 0,
                                   **({'layer': layers.LAYER['harness_tools']} if 'harness_tools' in layers.LAYER else {})}
    assert families['trackers']['reads'] == len(monitoring_tools.TRACKERS.READ_NAMES)
    assert sum(row['reads'] for row in tools['families']) == len(harness_tools.registry().reads)
    assert sum(row['writes'] for row in tools['families']) == len(harness_tools.registry().writes)


def test_the_hosted_conversation_and_its_sub_agent_read_it_as_the_member_of_the_turn(tmp_path, retail):
    from agent_bridge import CodexVideoAgent

    class Index:
        clips = list(SOURCES.values())
        def status(self): return {'ready': False, 'datasets': ['shop']}
        def get_clip(self, key): return SOURCES.get(key)

    async def prove():
        agent = CodexVideoAgent(tmp_path, Index(), lambda: [])
        agent.monitoring_store = MonitoringStore(tmp_path, source_lookup=lambda owner, key: SOURCES.get(key))
        agent.automation_store = AgentStore(tmp_path, input_provider=RetailInputProvider(monitoring=agent.monitoring_store))
        mine = agent.automation_store.create({'title': 'Queue watch', 'instruction': 'Follow the queue.', 'dataset': 'shop',
            'agent_kind': 'tracker', 'input_kind': 'tracker_events', 'mode': 'ongoing'}, ['shop'], owner_principal_id=OWNER)
        agent.children['chat'] = {'id': 'chat', 'dataset': 'shop', 'owner_principal_id': OWNER, 'messages': []}
        agent.children['child'] = {'id': 'child', 'parent_id': 'chat', 'dataset': 'shop', 'owner_principal_id': OWNER, 'messages': []}
        agent.active['chat'] = {'cancel': asyncio.Event()}
        for chat_id in ('chat', 'child'):
            content, _, success = await agent._tool('get_harness', {'section': 'agents'}, 'shop', chat_id)
            assert success, content
            answer = json.loads(content[0]['text'])
            tracker = next(item for item in answer['agent_classes'] if item['kind'] == 'tracker')
            assert [row['id'] for row in tracker['agents']] == [mine['id']]
            assert set(answer) == {'schema', 'section', 'product', 'agent_classes', 'ready_agents', 'problems'}
        content, _, success = await agent._tool('get_harness', {'section': 'everything'}, 'shop', 'chat')
        assert not success
    asyncio.run(prove())


# The answer follows the template, the stores and the registry.

def test_retail_is_described_from_its_manifest(tmp_path, retail):
    answer = ask(tmp_path)
    assert answer['schema'] == 'rocoto.harness.v1' and answer['section'] == 'all' and answer['problems'] == []
    assert answer['product'] == {**retail.product(), 'vertical': {'id': 'retail', 'revision': retail.revision}}
    assert [(row['layer'], row['name']) for row in answer['layers']] == [(1, 'Core'), (2, 'Harness'), (3, 'Vertical')]
    assert answer['layers'][0]['modules'] == 0, 'The installed OpenAI core stays outside our source tree.'
    assert answer['layers'][1]['modules'] == sum(value in layers.SEGMENT_FREE for value in layers.LAYER.values())
    assert answer['layers'][2]['modules'] == sum(value == layers.TEMPLATE for value in layers.LAYER.values())
    assert answer['layers'][2]['is'] == 'Retail, the vertical named retail'
    assert {'canvases', 'list', 'player', 'tiles'} <= set(answer['layers'][2]['provides'])
    assert answer['built_with']['rule'] == 'Every capability is a tool first; a screen shows what the tools made.'
    assert answer['built_with']['imports_that_cross_it_today'] == sum(len(found) for found in layers.CROSSINGS.values())
    assert [chat['title'] for chat in answer['fixed_conversations']] == ['Organization', 'Cameras', 'Messaging']
    assert answer['fixed_conversations'][0] == {'purpose': 'organization', 'title': 'Organization', 'opens': 'chat',
        'job': 'Business setup, locations, catalog and till, required tracker coverage.', 'skill': 'organization-intake'}
    assert [(item['kind'], item['label'], item['authority'], item['agents']) for item in answer['agent_classes']] == [
        ('action', 'Action', 'granted_effects', []), ('tracker', 'Tracker', 'participant_reach', [])]
    assert [row['id'] for row in answer['ready_agents']] == [row['id'] for row in retail.ready_agents()]
    assert all(row['input_kind'] == 'tracker_events' and row['event_types'] for row in answer['ready_agents'])
    display_set, = answer['tile_sets']
    assert (display_set['id'], display_set['revision'], display_set['install_tool']) == ('retail', 'retail-core-v1', 'install_core_trackers')
    assert [item['key'] for item in display_set['items']] == [tracker['key'] for tracker in retail.tile_set('retail')['trackers']]
    assert answer['event_vocabulary']['types'] == 48 and list(answer['event_vocabulary']['groups']) == retail.vocabulary()['groups']
    assert sum(len(names) for names in answer['event_vocabulary']['groups'].values()) == 48
    assert answer['surfaces'] == retail.surfaces() and answer['flow'] == retail.flow()
    assert answer['connections_expected'] == retail.connections_expected()
    assert [row['kind'] for row in answer['wakes']] == [
        'chat_message', 'phone_reply', 'event', 'schedule', 'inbound_webhook', 'agent_set', 'tracker-script-run']
    assert {row['state'] for row in answer['wakes']} <= set(harness_tools.STATES)
    # One adapter is built; the read says what it can see of each channel from here.
    assert [(row['channel'], row['adapter'], row['state'], row['ready']) for row in answer['channels']] == [
        (name, 'built', 'partial', harness_tools.UNKNOWN) for name in ('whatsapp', 'viber', 'email', 'voice')]
    assert answer['capabilities'] == harness_tools.capabilities(retail)
    assert [row['id'] for row in answer['capabilities']] == [row['id'] for row in retail.capabilities() if 'id' in row] + [
        row[0] for row in harness_tools.CAPABILITIES]


def test_under_the_job_shop_template_the_same_tool_answers_office_dispatch_and_desk(tmp_path, jobshop):
    answer = ask(tmp_path)
    assert answer['product']['business'] == 'Job shops' and answer['product']['vertical'] == {'id': 'jobshop', 'revision': jobshop.revision}
    assert [chat['title'] for chat in answer['fixed_conversations']] == ['Organization', 'Office']
    assert [(item['kind'], item['label'], item['plural']) for item in answer['agent_classes']] == [
        ('action', 'Dispatch', 'Dispatch agents'), ('tracker', 'Desk', 'Desks')]
    assert answer['layers'][2]['is'] == 'Job shops, the vertical named jobshop'
    assert len(answer['layers']) == 3
    assert [surface['title'] for surface in answer['surfaces']] == ['Backlog']
    assert [row['id'] for row in answer['ready_agents']] == ['late-job-watch']
    assert [display_set['id'] for display_set in answer['tile_sets']] == ['jobshop']
    assert answer['event_vocabulary'] == {'types': 3, 'groups': {'shop_floor': ['job_finished', 'job_opened'], 'office': ['week_full']}}
    assert [(row['id'], row['skill']) for row in answer['data_sources']] == [('backlog', 'backlog-intake')]
    assert [row['id'] for row in answer['skills']] == ['backlog-intake']
    assert [stage['stage'] for stage in answer['flow']] == ['intake', 'estimate', 'publish', 'tell']
    # The display_set a job shop declares has yet to install in this service, and the answer says so.
    assert any('list_trackers' in problem for problem in answer['problems'])
    assert any('jobshop_trackers' in problem for problem in answer['problems'])
    assert 'list_trackers' not in {row['name'] for row in answer['tools']['conversation']}
    # The harness parts stay the harness's: the same layer names and the same registry.
    assert [row['name'] for row in answer['layers']] == ['Core', 'Harness', 'Vertical']
    with pack.use('retail'):
        retail = ask(tmp_path / 'retail')
    common = {row['name'] for row in answer['tools']['conversation']}
    assert common < {row['name'] for row in retail['tools']['conversation']}
    assert 'list_cameras' not in common
    # The module counts of layers 3 and 4 are the code of the default template, so they ride with its answer alone.
    assert [(row['layer'], row.get('modules')) for row in answer['layers']] == [
        (1, retail['layers'][0]['modules']), (2, retail['layers'][1]['modules']), (3, None)]
    assert all('modules' not in row for row in answer['layers'][2:]) and retail['layers'][2]['modules'] > 0
    # The table of what works is the harness's rows: a template's own rows stay with that template.
    assert [row['id'] for row in answer['capabilities']] == [row[0] for row in harness_tools.CAPABILITIES]
    assert jobshop.capabilities() == [] and len(retail['capabilities']) > len(answer['capabilities'])
    skills = next(row for row in answer['capabilities'] if row['id'] == 'skills')
    assert skills['state'] == 'real' and next(row for row in retail['capabilities'] if row['id'] == 'skills')['state'] == 'partial'
    # Every statement of the answer is free of retail's words. The tool names are the registry's own, and a problem
    # row names the value a tool of that registry takes.
    for where, value in strings({key: part for key, part in answer.items() if key not in ('tools', 'problems')}):
        assert not RETAIL_WORDS.search(value), (where, value)
    assert RETAIL_WORDS.search(json.dumps(retail['capabilities'])) and RETAIL_WORDS.search('Retail offers three')
    assert 'Cameras' not in json.dumps(answer['fixed_conversations']) and 'Tracker' not in json.dumps(answer['agent_classes'])


def test_agents_come_from_the_store_a_surface_holds_and_belong_to_the_reader(tmp_path, retail):
    store, agents = company(tmp_path)
    definition = {'instruction': 'Follow the events.', 'dataset': 'shop', 'input_kind': 'tracker_events', 'mode': 'ongoing'}
    watch = agents.create({**definition, 'title': 'Queue watch', 'agent_kind': 'tracker'}, ['shop'], owner_principal_id=OWNER)
    notify = agents.create({**definition, 'title': 'Notify supplier', 'agent_kind': 'action'}, ['shop'], owner_principal_id=OWNER)
    agents.create({**definition, 'title': 'Another person\'s watch', 'agent_kind': 'tracker'}, ['shop'], owner_principal_id=OTHER)
    agents.create({**definition, 'title': 'Warehouse watch', 'agent_kind': 'tracker', 'dataset': 'warehouse'}, ['warehouse'], owner_principal_id=OWNER)
    classes = {item['kind']: item['agents'] for item in ask(tmp_path, 'agents', stores=(store, agents))['agent_classes']}
    assert classes['tracker'] == [{'id': watch['id'], 'title': 'Queue watch', 'status': 'draft', 'mode': 'ongoing',
        'input_kind': 'tracker_events', 'chat_id': watch['chat_id'], 'runs_started': 0, 'last_run': None}]
    assert [row['id'] for row in classes['action']] == [notify['id']]
    # A customer's own agent over MCP arrives with the monitoring store alone: the lists say so.
    assert {item['kind']: item['agents'] for item in ask(tmp_path, 'agents', stores=(store, None))['agent_classes']} == {
        'action': harness_tools.UNKNOWN, 'tracker': harness_tools.UNKNOWN}


def test_skills_carry_the_company_s_installed_state_where_the_surface_holds_its_stores(tmp_path, retail):
    from agent_bridge import tool_specs
    seeds = {row['id']: row for row in ask(tmp_path, 'skills')['skills']}
    assert list(seeds) == [seed['id'] for seed in retail.skills()]
    assert {row['installed'] for row in seeds.values()} == {harness_tools.UNKNOWN}, 'a company the skill store has yet to open'
    held = retail.skill_tool_gaps({spec['name'] for spec in tool_specs(True)} | {'web_search'})
    assert {key for key, row in seeds.items() if not row['tools_ready']} == set(held)
    assert seeds['pattern-overview']['waits_for'] == list(held['pattern-overview']) and seeds['camera-intake']['waits_for'] == []
    assert seeds['shopify-order-context']['connections'] == ['shopify']
    skills = SkillStore(tmp_path)
    skills.install('camera-intake')
    own = skills.create({'name': 'Morning walk', 'description': 'The opening checks.', 'instructions': 'Walk the shop floor.',
                         'required_tools': ['list_trackers'], 'required_connections': []})
    rows = {row['id']: row for row in ask(tmp_path, 'skills')['skills']}
    assert rows['camera-intake']['installed'] is True and rows['organization-intake']['installed'] is False
    assert rows[own['id']] == {'id': own['id'], 'name': 'Morning walk', 'origin': 'company', 'installed': False,
                               'tools_ready': True, 'waits_for': [], 'connections': []}
    assert rows['camera-intake']['origin'] == 'vertical' and len(rows) == len(seeds) + 1
    # Over MCP the answer keeps to the template's seeds: a company's own skills stay with its conversations.
    mcp = ask(tmp_path, 'skills', agents=False)['skills']
    assert [row['id'] for row in mcp] == list(seeds) and {row['installed'] for row in mcp} == {harness_tools.UNKNOWN}


def test_the_registry_of_data_sources_says_what_the_company_has_today(tmp_path, retail):
    store, agents = company(tmp_path)
    sources = {row['id']: row for row in ask(tmp_path, 'data', stores=(store, agents))['data_sources']}
    assert list(sources) == [row['id'] for row in retail.data_sources()]
    assert sources['observations'] == {'id': 'observations', 'title': 'Committed observations', 'skill': None, 'tools_registered': True,
        'read_with': ['list_monitoring_profiles', 'list_tracker_events', 'get_tracker_event'], 'connected': False,
        'first_page': {'list_monitoring_profiles': {'profiles': 0, 'trackers': 0}, 'list_tracker_events': {'events': 0}}}
    assert sources['trackers']['connected'] is False and sources['trackers']['first_page'] == {'list_trackers': {'trackers': 0}}
    # A source whose read tools belong to the conversation itself names the tool that answers.
    for key, first, skill in (('catalog', 'get_catalog_setup', 'catalog-onboarding'), ('business', 'get_business_profile', 'organization-intake'),
                              ('cameras', 'list_cameras', 'camera-intake')):
        assert (sources[key]['connected'], sources[key]['read_first'], sources[key]['skill']) == (harness_tools.UNKNOWN, first, skill)
        assert 'first_page' not in sources[key] and 'rows' not in sources[key]
    saved = store.save_profile(OWNER, {'name': 'Queue', 'dataset': 'shop', 'vocabulary': [
        {'type': 'entered_queue', 'description': 'A person joins the queue.'}]}, datasets=['shop'])
    store.commit_event(OWNER, {'profile_id': saved['id'], 'profile_revision': 1, 'event_key': 'e1', 'type': 'entered_queue',
        'description': 'One person joins the queue.', 'source': {'clip_id': 'a', 'start_s': 2, 'end_s': 3}, 'track_id': 'p1'})
    for held in (agents, None):  # the hosted conversation, then a customer's own agent over MCP
        observed = {row['id']: row for row in ask(tmp_path, 'data', stores=(store, held))['data_sources']}['observations']
        assert observed['connected'] is True
        assert observed['first_page'] == {'list_monitoring_profiles': {'profiles': 1, 'trackers': 0}, 'list_tracker_events': {'events': 1}}
    # Another person reads their own rows.
    theirs = {row['id']: row for row in ask(tmp_path, 'data', owner=OTHER, stores=(store, agents))['data_sources']}
    assert theirs['observations']['connected'] is False
    # The numbers are one page of each read: with more events than a page holds, the read itself gives the total.
    for number in range(2, 203):
        store.commit_event(OWNER, {'profile_id': saved['id'], 'profile_revision': 1, 'event_key': f'e{number}', 'type': 'entered_queue',
            'description': 'One person joins the queue.', 'source': {'clip_id': 'a', 'start_s': 2, 'end_s': 3}, 'track_id': f'p{number}'})
    page = {row['id']: row for row in ask(tmp_path, 'data', stores=(store, agents))['data_sources']}['observations']
    assert page['connected'] is True and page['first_page']['list_tracker_events'] == {'events': 200}
    assert len(store.list_events(OWNER, 'shop', limit=500)) == 202


def test_a_tool_the_manifest_names_and_the_registry_lacks_is_a_problem_row(tmp_path):
    root = tmp_path / 'jobshop'
    shutil.copytree(JOBSHOP, root)
    manifest = json.loads((root / 'vertical.json').read_text())
    manifest['data_sources'][0]['read_with'].append('list_open_jobs')
    manifest['surfaces'][0]['tabs'][0]['read_with'] = 'read_backlog'
    (root / 'vertical.json').write_text(json.dumps(manifest))
    with pack.use(root) as template:
        answer = ask(tmp_path / 'state', 'tools')
        expected = template.check_tools({row['name'] for row in answer['tools']['conversation']})
        assert answer['problems'][:len(expected)] == expected
        assert any('read_backlog' in problem for problem in expected)
        assert any('list_open_jobs' in problem for problem in expected)


def test_one_section_returns_its_own_parts(tmp_path, retail):
    always = {'schema', 'section', 'product', 'problems'}
    for section, parts in harness_tools.PARTS.items():
        assert set(ask(tmp_path, section)) == always | set(parts), section
    assert set(ask(tmp_path)) == always | {key for parts in harness_tools.PARTS.values() for key in parts}
    assert set(harness_tools.SECTIONS) == {'all', *harness_tools.PARTS}
    with pytest.raises(ValueError, match='Use the declared fields of this tool'):
        ask(tmp_path, 'everything')
    with pytest.raises(ValueError, match='Choose a section of the harness'):
        harness_tools.read(None, 'get_harness', {'section': 'everything'})


# Hygiene: names, titles, counts and states.

def test_the_answer_is_free_of_paths_people_prompts_and_host_settings(tmp_path):
    import public_mcp
    for source in ('retail', JOBSHOP):
        with pack.use(source) as template:
            state = tmp_path / template.id
            store, agents = company(state)
            agents.create({'title': 'Queue watch', 'instruction': 'Follow the queue with care.', 'dataset': 'shop', 'agent_kind': 'tracker',
                           'input_kind': 'tracker_events', 'mode': 'ongoing'}, ['shop'], owner_principal_id=OWNER)
            SkillStore(state)
            for held in (agents, None):
                answer = ask(state, stores=(store, held))
                text = json.dumps(answer, ensure_ascii=False)
                assert json.loads(text) == answer and len(text) < 64 * 1024
                # The public MCP filter drops path-like and credential-like keys: here it has nothing to drop.
                assert public_mcp._public(answer) == answer
                for banned in (str(HERE), str(tmp_path), '/home/', '/tmp/', OWNER, OTHER, 'ROCOTO_', '.sqlite', 'prompts/', '.py',
                               'rocoto-', 'staging host', 'Follow the queue with care', template.prompt('system')[:60]):
                    assert banned not in text, banned
                for where, value in strings(answer):
                    assert not value.startswith(('/', '~', 'http')), (where, value)
                    assert not NEGATIVE.search(value), (where, value)
    # The public filter is a working check: it drops a path-like key.
    assert public_mcp._public({'state_path': '/x', 'title': 'Home'}) == {'title': 'Home'}


# The capability table.

def test_the_capability_table_has_a_row_for_every_capability_the_map_named():
    """The table of the default template: retail's own rows, then the harness rows with retail's sentences joined."""
    retail, jobshop = pack.load('retail'), pack.load(JOBSHOP)
    rows = harness_tools.capability_rows(retail)
    ids = [row['id'] for row in rows]
    assert len(set(ids)) == len(ids) and len({row['title'] for row in rows}) == len(rows)
    assert set(NAMED) <= set(ids), sorted(set(NAMED) - set(ids))
    assert set(ALSO) <= set(ids), sorted(set(ALSO) - set(ids))
    assert harness_tools.capability_problems(retail) == [] and harness_tools.capability_problems(jobshop) == []
    repo = layout.REPO
    for template in (retail, jobshop):
        for row in harness_tools.capability_rows(template):
            key, state = row['id'], row['state']
            assert state in harness_tools.STATES, key
            assert row['title'].strip() and (row['works'] or row['missing'] or row['for_show']), key
            # real works today, partial names what is missing, planned has zero working part.
            assert (state == 'real') <= bool(row['works']) and (state == 'partial') <= bool(row['missing']), key
            assert (state == 'planned') <= (not row['works'] and bool(row['for_show'] or row['missing'])), key
            for text in (row['title'], row['works'], row['missing'], row['for_show']):
                assert not NEGATIVE.search(text), (key, text)
                assert 'ROCOTO_' not in text and '.py' not in text and '/' not in text.replace('/mcp', ''), (key, text)
                # Host names and the state of one host stay with the documents of that host.
                assert 'rocoto-' not in text and 'staging host' not in text, (key, text)
            assert row['where'], key
            for name in row['where']:
                assert (HERE / name).exists() or (repo / name).exists(), (key, name)
    # The harness rows hold for every segment: a job shop reads them as they stand, free of retail's words.
    harness = harness_tools.capability_rows(jobshop)
    assert [row['id'] for row in harness] == [row[0] for row in harness_tools.CAPABILITIES]
    for row in harness:
        for text in (row['title'], row['works'], row['missing'], row['for_show']):
            assert not RETAIL_WORDS.search(text), (row['id'], text)
    # Retail joins its sentences to a harness row and sets the state its segment gives the row.
    by_id = {row['id']: row for row in rows}
    plain = {row['id']: row for row in harness}
    assert by_id['skills']['state'] == 'partial' and plain['skills']['state'] == 'real'
    assert by_id['skills']['works'].startswith(plain['skills']['works']) and by_id['skills']['works'] != plain['skills']['works']
    assert by_id['action_agents']['title'] == 'Action Agents' and plain['action_agents']['title'] == 'Agents with granted effects'
    assert by_id['skills']['where'] == plain['skills']['where'] + ['verticals/retail/skills.json']
    answered = harness_tools.capabilities(retail)
    assert [row['id'] for row in answered] == ids and all(set(row) <= {'id', 'title', 'state', 'works', 'missing', 'for_show'} for row in answered)
    outgoing = next(row for row in answered if row['id'] == 'webhooks_out')
    assert outgoing['state'] == 'real' and 'durable effect identity' in outgoing['works']
    # The description of itself states its own gaps: the paragraph that waits and the channel state it has yet to read.
    assert by_id['self_description']['state'] == 'real'
    import agent_bridge
    wired = 'developer_prompt' in layout.source('agent_bridge').read_text() or 'get_harness' in agent_bridge.SYSTEM
    assert ('waits for its place in the developer prompt' in by_id['self_description']['missing']) == (not wired)


def test_the_front_door_prints_the_capability_table_row_for_row(tmp_path, capsys):
    table = harness_tools.capability_table()
    assert table == harness_tools.capability_table(pack.load('retail')), 'the front door prints the table of the default template'
    assert table.count('\n') == len(harness_tools.capability_rows(pack.load('retail'))) + 1
    with pack.use(JOBSHOP):
        assert harness_tools.main([]) == 0 and capsys.readouterr().out.strip() == table
    document = (HERE / 'README.md').read_text()
    assert table in document, 'run `python harness_tools.py --readme`: it writes the table under What works today'
    assert document.count('| Capability | Real today |') == 1
    # The command writes the table in place of a stale one and leaves every other line as it was.
    stale = tmp_path / 'README.md'
    stale.write_text(document.replace('| Webhooks out | — | — |', '| Webhooks out | Real. | — |').replace('| Skills |', '| Skills, an older row |'))
    assert stale.read_text() != document
    harness_tools.write_readme(stale)
    assert stale.read_text() == document
    (tmp_path / 'bare.md').write_text('# A document that has yet to get its table\n')
    with pytest.raises(ValueError, match='Give README.md its capability table'):
        harness_tools.write_readme(tmp_path / 'bare.md')


def _orphan_documents(root, declared):
    """Every source Markdown file is canonical, a typed archive, or product material."""
    allowed = set(declared) | {'HARNESS.md',
        'verticals/retail/web/control-room/kit/README.md',
        'verticals/retail/web/assets/demo-companies/README.md'}
    orphans = []
    for path in root.rglob('*.md'):
        relative = path.relative_to(root)
        parts = relative.parts
        if relative.as_posix() in allowed or parts[0] in {'records', 'design', 'deploy', 'dist-preview'}:
            continue
        if any(part.startswith('.') or part in {'node_modules', '__pycache__'} for part in parts):
            continue
        if 'prompts' in parts or (parts[0] == 'verticals' and 'skills' in parts):
            continue
        # Each additional declared vertical can document its own role, pages and data.
        if (len(parts) == 3 and parts[0] == 'verticals' and path.name in {'README.md', 'PAGES.md', 'DATA.md'}
                and (path.parent / 'vertical.json').is_file()):
            continue
        orphans.append(relative.as_posix())
    return sorted(orphans)


def test_the_front_door_and_source_tree_follow_the_twelve_document_index():
    declared = [row[1] for row in harness_tools.DOCUMENTS]
    assert len(declared) == len(set(declared)) == 12
    assert all((HERE / name).is_file() for name in declared)
    assert _orphan_documents(HERE, declared) == []
    document = (HERE / 'README.md').read_text()
    listing = document.split('## Living documents', 1)[1]
    links = re.findall(r'\]\(([^)#]+\.md)(?:#[^)]*)?\)', listing)
    assert [name for name in links if name in declared] == declared
    for name in ('runtime/README.md', 'mechanics/README.md', 'sdk/README.md', 'verticals/retail/README.md'):
        assert f']({name})' in document
    # The user's required entry stays a pointer into the canonical builder ledger.
    handoff = (HERE / 'HARNESS.md').read_text()
    assert 'mechanics/README.md#what-the-builders-needed' in handoff
    mechanics = (HERE / 'mechanics/README.md').read_text()
    assert re.findall(r'^## (.+)$', mechanics, flags=re.M)[-1] == 'What the builders needed'


def test_document_gate_rejects_unindexed_living_docs_and_admits_typed_records(tmp_path):
    for name in ('mechanics/NEW.md', 'sdk/SECOND-INDEX.md', 'verticals/new/README.md',
                 'records/2026-10-11-proof.md', 'design/SERVICE.md', 'deploy/worker/README.md',
                 'verticals/new/prompts/system.md'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Test source\n')
    assert _orphan_documents(tmp_path, ()) == ['mechanics/NEW.md', 'sdk/SECOND-INDEX.md', 'verticals/new/README.md']
    (tmp_path / 'verticals/new/vertical.json').write_text('{}')
    assert _orphan_documents(tmp_path, ('mechanics/NEW.md',)) == ['sdk/SECOND-INDEX.md']


def test_the_table_is_true_to_the_code_it_names(retail):
    """The rows a machine can check today, checked: change the row in the change that changes the code."""
    from agent_bridge import tool_specs
    import agent_catalogue
    import messaging_provider
    rows = {row['id']: row for row in harness_tools.capability_rows(retail)}
    state = lambda key: rows[key]['state']
    ready = rows['ready_made_agents']
    # Retired definitions remain readable; only the three active ready-made agents are offered.
    assert [row['name'] for row in agent_catalogue.available()['templates']] == ['Queue watch', 'Empty shelf watch', 'Shop summary']
    assert all(row['name'] in ready['works'] for row in agent_catalogue.available()['templates'])
    assert len(agent_catalogue.retired()['templates']) == 15
    assert 'inactive draft' in ready['works'] and 'event types' in ready['works']
    assert 'saved conversations' in ready['for_show']
    assert state('ready_made_agents') == 'partial' and 'phone' in ready['missing']
    # A run sends one alert, as its last act: both ready-made Trackers are written that way, and the row says who the host alerts.
    for row in agent_catalogue.available()['templates']:
        if row['agent_kind'] != 'tracker':
            continue
        text = row['instructions']
        assert text.count('send_alert') == 1 and text.index('First write your report') < text.index('send_alert'), row['id']
        assert 'one send_alert' in text and text.rstrip().endswith('so it comes last.') and ' for each ' not in text, row['id']
        assert 'participants' not in row['description'] and 'one alert to its own member' in row['description'], row['id']
    assert 'alerts each participant for every committed event the agent follows' in ready['missing']
    assert 'wait for live phone delivery' in ready['missing'] and 'commissioned provider' in rows['phone_alerts']['missing']
    bridge = layout.source('agent_bridge').read_text()
    assert 'The receipt ends this turn' in layout.source('messaging').read_text()
    assert "Review this turn's saved message receipt." in bridge or 'saved message receipt' in bridge
    # Skills: the mechanism is real, and four of retail's thirteen seeds are usable on a turn.
    held = retail.skill_tool_gaps({spec['name'] for spec in tool_specs(True)} | {'web_search'})
    assert (len(retail.skills()), len(retail.skills()) - len(held), len(held)) == (13, 4, 9)
    assert 'Four of retail\'s thirteen seeds' in rows['skills']['works'] and rows['skills']['missing'].startswith('Nine seeds wait')
    assert state('skills') == 'partial'
    # Channels: one adapter, WhatsApp.
    assert 'whatsapp' in {adapter.channel for adapter in messaging_provider.ChannelAdapter.__subclasses__() if getattr(adapter, 'channel', None)}
    assert state('viber_and_sms') == 'partial' and state('phone_alerts') == 'partial'
    # Live input: the service reports it as off for every collection.
    from workspace_capabilities import WorkspaceCapabilities
    assert WorkspaceCapabilities.live_input_ready('any') is False and state('live_camera_viewing') == 'planned'
    page = (HERE / 'verticals' / 'retail' / 'web' / 'retail-ui.js').read_text()
    assert ('Connect cameras button' in rows['live_camera_viewing']['for_show']) == ("agentButton('Connect cameras'" in page)
    # The core adapter is called by the managed runtime, with an honest publication boundary.
    runtime = layout.source('managed_runtime').read_text()
    assert 'ManagedCodexClient' in runtime and 'ManagedRuntime(client, ManagedPolicy())' in runtime
    assert 'OpenAI-managed Agents API' in rows['agent_runtime']['works']
    assert 'live' in rows['agent_runtime']['missing']
    # The data sources of retail: three with a skill, two that wait for theirs.
    assert [source['id'] for source in retail.data_sources() if 'skill' not in source] == ['observations', 'trackers']
    # Standing grants survive clean recovery; an admitted unresolved run keeps its identity.
    source = layout.source('background_agents').read_text()
    assert 'def recover(self,' in source and 'active_run_id' in source
    from background_agents import AgentDefinition
    assert AgentDefinition.model_fields['max_runs'].metadata[1].le == 20
    # The broker's history bound and the day a released row stays, and the till writer with tests as its callers.
    broker = layout.source('native_custom_broker').read_text()
    assert 'MAX_HISTORY=2048' in broker and 'RELEASED_SECONDS=86400' in broker
    writers = [path.name for path in (paths[0] for paths in layout.modules().values()) if 'record_till_sales' in path.read_text()]
    assert writers == ['retail_records.py']
    # The generated history marks every record, and the one inbound webhook route is the messaging provider's.
    assert 'generated_history' in layout.source('retail_demo_history').read_text()
    routes = re.findall(r"@app\.(?:api_route|post|get)\('([^']*webhook[^']*)'", layout.source('server').read_text())
    assert '/api/messaging/webhook/{environment}' in routes
    assert state('webhooks_in') == state('webhooks_out') == 'real'
    # Tracker scripts and views: the tools the rows name are in the registry, and the rows say what works and what is open.
    registered = {spec['name'] for spec in tool_specs(True)}
    assert {'save_tracker_script', 'test_tracker_script', 'publish_tracker_script', 'get_tracker_script',
            'save_tracker_presentation', 'get_tracker_presentation'} <= registered
    for key in ('tracker_scripts', 'tracker_views'):
        assert rows[key]['works'] and rows[key]['missing'] and 'Being built' not in rows[key]['works'] + rows[key]['missing'], key
    assert all('Being built' not in note for _, _, note in harness_tools.WAKES)


# The paragraph that tells an agent it can ask.

def test_the_developer_paragraph_carries_the_template_s_names_and_stays_the_same_for_a_revision():
    retail, jobshop = pack.load('retail'), pack.load(JOBSHOP)
    text = harness_tools.developer_prompt(retail)
    assert text == harness_tools.developer_prompt(pack.load('retail')), 'the same revision gives the same text'
    assert text.startswith('You are an agent of Rocoto, built as three levels.')
    for part in ('The Retail vertical', 'fixed conversations (Organization, Cameras and Messaging)', 'agent classes (Action and Tracker)',
                 'product surfaces (Home and Cameras)', 'Call get_harness to read'):
        assert part in text, part
    other = harness_tools.developer_prompt(jobshop)
    for part in ('The Job shops vertical', 'fixed conversations (Organization and Office)', 'agent classes (Dispatch and Desk)',
                 'product surfaces (Backlog)'):
        assert part in other, part
    assert 'Cameras' not in other and 'Tracker' not in other
    # One paragraph of plain text: a changed revision alone leaves it as it was, and the active template is the default.
    for value in (text, other):
        assert '\n' not in value and not NEGATIVE.search(value) and len(value) < 1200
    with pack.use(JOBSHOP):
        assert harness_tools.developer_prompt() == other
    assert not NEGATIVE.search(harness_tools.DESCRIPTION)
    # It names what the tool returns: each part it promises is a part of the answer.
    promised = re.search(r'read the current (.+) with their present state', text).group(1).replace(' and ', ', ').split(', ')
    assert promised == ['conversations', 'agents', 'tools', 'wakes', 'channels', 'recipes', 'skills', 'surfaces', 'data sources']
    assert {'conversations', 'agents', 'tools', 'wakes', 'channels', 'recipes', 'skills', 'surfaces', 'data'} <= set(harness_tools.SECTIONS)
