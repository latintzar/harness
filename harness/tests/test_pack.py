"""The template contract: the retail manifest loads, its text is today's text, and a second template swaps in.

tests/verticals/jobshop is a small template for another segment (a sheet-metal job shop). Every read
point that serves template data is exercised under it, so a read point that keeps a retail
constant fails here.
"""
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import types
from typing import get_args

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import agent_catalogue
import intake_instructions
import organization_chats
import pack
import skills
from organization_chats import OrganizationChats
from skills import SkillStore

JOBSHOP = Path(__file__).resolve().parent / 'verticals' / 'jobshop'

# sha256 of the text every deployed conversation was started with, recorded on 11 October 2026
# before the strings moved out of agent_bridge.py, intake_instructions.py, skills.py and
# agent_catalogue.py. A changed character in a prompt changes the toolset digest of every chat,
# and each chat then starts a new native thread on its next turn. Change a pin in the same
# release as the prompt change it records.
PINS = {
    'prompts/system.md': '1ab5221e99bf66bb8450cac2e43562e4dcbb5a7142d6f4e1c392964d1ec0071d',
    'prompts/intake-common.md': '7b9e1c1b8160c5c3794f96213befb9346379810a160fb14e227197d001cd6674',
    'prompts/cameras.md': 'fac952946696a036eabf877bde50379986f418efb599a46066cc95552830eca1',
    'prompts/organization.md': '81b70dda5f48b715170a65dc2864ad605d07c55272e085dbd4b97636e8ea1d84',
}
ASSEMBLED = {
    'every conversation': 'e1ab84cd3dc969601fcdca5ec4661fb0952f98c0178486f7cdfeea577c651e26',
    'organization': '517911f5b7e625baf044cd12737ca9a3b033c46a450c859c291c65396ce12cd5',
    'cameras': '9b6f2b43903e512d28baf2c5a549a93bb65bf25e6dad7d45a5479bc8c1590852',
}
SKILL_SEEDS = '17b8a4f66f97840e53ee207410f32df881a6fc970f9302a84f648cbd2930cc3e'
AGENT_TEMPLATES = 'fd6d2d575ea062a1a7c748ca71e8bb3c74cc9078279a589d646fbea7d23b1b3c'
# 'orgchat-' + sha256(owner NUL collection NUL purpose)[:32] for the owner organization-0123 and the collection cafe.
FIXED_CHAT_IDS = {
    'organization': 'orgchat-42af8748ae18ed634d09a36a6b5525f1',
    'cameras': 'orgchat-3c97fdc82da34f04210011297ec0beb6',
    'messaging': 'orgchat-f37165529928d9f6d62e917bd06b4b61',
}


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


@pytest.fixture
def retail():
    with pack.use('retail') as template:
        yield template


@pytest.fixture
def jobshop():
    with pack.use(JOBSHOP) as template:
        yield template


def company(tmp_path, name='state'):
    chats = OrganizationChats(tmp_path / name, environment_id='demo', member=lambda person: True)
    chats.owner = 'organization-0123'
    return chats


class Agent:
    """The two attributes the fixed-chat code reads from the conversation store."""
    def __init__(self, chats):
        self.organization_chats = chats
        self.children = {}

    def _save_child(self, record):
        self.children[record['id']] = record


# The retail template.

def test_the_retail_template_loads_with_every_file_it_names_and_every_tool_registered(retail):
    from agent_bridge import tool_specs
    assert retail.manifest['schema'] == pack.SCHEMA == 'rocoto.vertical.v1'
    assert (retail.id, retail.root) == ('retail', HERE / 'verticals' / 'retail')
    assert 'organization' in retail.purposes()
    for key in retail.files:
        assert (retail.root / key).resolve().is_file(), key
    display_set = json.loads((pack.PACKS / 'retail' / 'core_trackers' / 'retail.json').read_text())
    assert retail.tile_set_path('retail') == pack.PACKS / 'retail' / 'core_trackers' / 'retail.json'
    assert retail.tile_set('retail') == display_set and retail.tile_sets()[0]['revision'] == display_set['revision']
    assert [tracker['key'] for tracker in retail.tile_set('retail')['trackers']] == [
        'visitors', 'product_interactions', 'picked_up', 'put_back', 'basket', 'queue_time',
        'empty_shelves', 'sales', 'conversion']
    assert retail.vocabulary()['types'] == display_set['vocabulary'] and len(display_set['vocabulary']) == 48
    assert retail.vocabulary()['groups'] == ['shopper_journey', 'service_maintenance', 'other']
    # Every tool a surface tab, a data source or a tile_set names is one a conversation can call today.
    registered = {spec['name'] for spec in tool_specs(True)}
    assert retail.check_tools(registered) == []
    # Every seeded skill the manifest points at exists, and each named family imports.
    seeds = {seed['id'] for seed in retail.skills()}
    assert {chat['skill'] for chat in retail.fixed_chats() if 'skill' in chat} <= seeds
    assert {source['skill'] for source in retail.data_sources() if 'skill' in source} <= seeds
    families = retail.import_tool_families()
    contributed = {name for family in families for name in family.READ_NAMES | family.WRITE_NAMES}
    assert {'get_tracker', 'list_tracker_runs', 'list_trackers', 'get_tracker_script',
            'get_tracker_presentation', 'install_core_trackers'} <= contributed
    assert contributed <= registered


def test_the_retail_prompts_are_today_s_text_byte_for_byte(retail):
    for key, expected in PINS.items():
        assert hashlib.sha256((retail.root / key).read_bytes()).hexdigest() == expected, key
    assert sha(retail.prompt('system')) == PINS['prompts/system.md']
    assert sha(retail.prompt('fixed_chat_common')) == PINS['prompts/intake-common.md']
    assert sha(retail.fixed_chat_text('cameras')) == PINS['prompts/cameras.md']
    assert sha(retail.fixed_chat_text('organization')) == PINS['prompts/organization.md']
    assert retail.fixed_chat_text('messaging') is None and retail.fixed_chat_prompt('messaging') is None


def test_the_developer_prompt_in_agent_bridge_and_the_template_file_hold_the_same_text(retail):
    # The vertical's instructions preserve their recorded text; the live prompt also introduces
    # the harness and is composed afresh from the active vertical for both runtime paths.
    import agent_bridge
    import harness_tools
    from customer_workflows import CUSTOMER_WORKFLOW_INSTRUCTIONS
    from managed_policy import ManagedPolicy
    original = retail.prompt('system') + '\n' + CUSTOMER_WORKFLOW_INSTRUCTIONS
    assert sha(original) == ASSEMBLED['every conversation']
    assembled = original + '\n' + harness_tools.developer_prompt(retail)
    assert agent_bridge.system_instructions() == agent_bridge.SYSTEM == assembled
    agent = types.SimpleNamespace(children={'chat': {}},
        managed_mcp_host=types.SimpleNamespace(transport=object(), url='https://example.invalid/runtime/mcp'),
        tool_registry=types.SimpleNamespace(read_names=lambda surface: set()))
    configuration = ManagedPolicy().base_configuration(agent, 'chat', [], assembled)
    assert configuration['agent']['instructions'] == assembled
    assert configuration['environment']['type'] == 'openai_hosted'


def test_a_fixed_chat_receives_the_same_assembled_instructions_as_before(tmp_path, retail):
    import agent_bridge
    chats = company(tmp_path)
    agent = Agent(chats)
    for purpose in ('organization', 'cameras'):
        record = chats.ensure(agent, 'cafe', purpose)
        from customer_workflows import CUSTOMER_WORKFLOW_INSTRUCTIONS
        original = retail.prompt('system') + '\n' + CUSTOMER_WORKFLOW_INSTRUCTIONS
        assert sha(original + '\n' + retail.fixed_chat_prompt(purpose)) == ASSEMBLED[purpose]
        assembled = intake_instructions.for_chat(agent, record['id'], original)
        assert assembled.startswith(original) and assembled.endswith(retail.fixed_chat_prompt(purpose))
        assert ('Job recipes for this conversation:' in assembled) == (purpose == 'organization')
        current = intake_instructions.for_chat(agent, record['id'], agent_bridge.system_instructions())
        assert current.startswith(agent_bridge.system_instructions())
        assert current.endswith(retail.fixed_chat_prompt(purpose))
    messaging = chats.ensure(agent, 'cafe', 'messaging')
    assert intake_instructions.for_chat(agent, messaging['id'], 'base') == 'base'
    assert intake_instructions.COMMON == retail.prompt('fixed_chat_common')
    assert intake_instructions.CAMERA == retail.fixed_chat_text('cameras')
    assert intake_instructions.ORGANIZATION == retail.fixed_chat_text('organization')


def test_fixed_chat_ids_titles_and_order_are_the_ones_companies_already_have(tmp_path, retail):
    chats = company(tmp_path)
    agent = Agent(chats)
    assert organization_chats.PURPOSES == retail.purposes() == {
        'organization': 'Organization', 'cameras': 'Cameras', 'messaging': 'Messaging'}
    assert list(organization_chats.PURPOSES) == ['organization', 'cameras', 'messaging']
    for purpose, expected in FIXED_CHAT_IDS.items():
        assert chats.identity('cafe', purpose) == expected
        assert chats.ensure(agent, 'cafe', purpose)['title'] == retail.purposes()[purpose]
    with pytest.raises(ValueError, match=r'^Choose Organization, Cameras or Messaging\.$'):
        chats.identity('cafe', 'office')


def test_retail_skill_seeds_and_ready_agents_are_the_recorded_ones(tmp_path, retail):
    assert sha(canonical(list(retail.skills()))) == SKILL_SEEDS
    assert skills.BUILTINS == retail.skills() and len(skills.BUILTINS) == 13
    catalogue = SkillStore(tmp_path / 'company').catalogue()['skills']
    assert [item['id'] for item in catalogue] == sorted(seed['id'] for seed in retail.skills())
    by_id = {item['id']: item for item in catalogue}
    for seed in retail.skills():
        assert {key: by_id[seed['id']][key] for key in seed} == seed
    # The ready-made agents: `available()` is the three of ready_agents.json, and the fifteen
    # retired ones stay readable under their ids, because saved conversations name them. The
    # Customize route and new-chat creation use `available()`; saved chats may resolve retired IDs.
    assert agent_catalogue.available() == {'publisher': 'Rocoto', 'templates': retail.ready_agents()}
    assert [row['id'] for row in retail.ready_agents()] == ['queue-watch', 'empty-shelf-watch', 'shop-summary']
    assert retail.ready_agents() == json.loads((retail.root / 'ready_agents.json').read_text())
    assert sha(canonical(agent_catalogue.retired())) == AGENT_TEMPLATES and len(agent_catalogue.retired()['templates']) == 15
    # The one answer that holds both lists marks each retired row, so a reader of it can tell the two apart.
    assert agent_catalogue.catalogue()['templates'] == retail.ready_agents() + [
        {**row, 'retired': True} for row in agent_catalogue.retired()['templates']]
    assert all('retired' not in row for row in agent_catalogue.available()['templates'] + agent_catalogue.retired()['templates'])
    assert agent_catalogue.template('queue-review')['retired'] is True and 'retired' not in agent_catalogue.template('queue-watch')
    assert agent_catalogue.template('shop-summary')['agent_kind'] == 'action'
    assert agent_catalogue.template('queue-review')['mode'] == 'scheduled'
    assert agent_catalogue.template('office-hours') is None


def test_agent_classes_carry_the_stored_kinds_and_their_authority(retail):
    assert retail.kinds() == ('action', 'tracker') and retail.default_kind() == 'action'
    assert retail.authority('tracker') == 'participant_reach' and retail.authority('action') == 'granted_effects'
    assert set(pack.AUTHORITIES) == {'participant_reach', 'granted_effects'}
    assert retail.agent_class('tracker')['create_label'] == 'New tracker'
    with pytest.raises(pack.PackError, match=r'^Choose an agent class of this product: Action or Tracker\.$'):
        retail.authority('desk')
    with pytest.raises(pack.PackError, match='Choose a tile_set of this vertical: retail'):
        retail.tile_set('jobshop')


def test_the_shell_is_what_the_sidebar_draws_today(retail):
    shell = retail.shell()
    assert shell['schema'] == 'rocoto.harness-shell.v1'
    assert shell['vertical'] == {'id': 'retail', 'revision': retail.revision} and shell['product'] == {'name': 'Rocoto'}
    assert shell['fixed_chats'] == [
        {'purpose': 'organization', 'title': 'Organization', 'icon': 'building', 'opens': 'chat',
         'job': 'Business setup, locations, catalog and till, required tracker coverage.'},
        {'purpose': 'cameras', 'title': 'Cameras', 'icon': 'video', 'opens': 'chat',
         'job': 'Camera connection, privacy, zones, clocks and readiness.'},
        {'purpose': 'messaging', 'title': 'Messaging setup', 'icon': 'chat', 'opens': 'member_setup',
         'job': "Each member's verified phone channel."}]
    assert [(section['key'], section['type']) for section in shell['sections']] == [
        ('Organization', 'fixed'), ('Action', 'agent_class'), ('Tracker', 'agent_class'), ('Recent', 'recent')]
    action, tracker = shell['sections'][1:3]
    assert (action['kind'], action['authority'], action['icon'], action['create_label'], action['empty_label']) == (
        'action', 'granted_effects', 'skill', 'New action agent', 'Action agents appear here.')
    assert (tracker['kind'], tracker['authority'], tracker['icon'], tracker['create_label'], tracker['empty_label']) == (
        'tracker', 'participant_reach', 'eye', 'New tracker', 'Trackers appear here.')
    assert tracker['starter_prompt'].startswith('Help me create a Tracker Agent for this location.')
    assert shell['surfaces'] == [{'id': 'home', 'title': 'Home', 'nav': 'primary'},
                                 {'id': 'cameras', 'title': 'Cameras', 'nav': 'secondary'}]
    # The answer goes to a browser: names, labels and states only; file paths stay on the host.
    assert str(HERE) not in json.dumps(shell) and 'prompts/' not in json.dumps(shell)


def test_the_manifest_states_surfaces_connections_data_sources_and_flow(retail):
    tabs = {(surface['id'], tab['id']): tab for surface in retail.surfaces() for tab in surface['tabs']}
    assert tabs[('home', 'tiles')]['display'] == 'tiles' and tabs[('home', 'canvases')]['display'] == 'canvases'
    assert all(tab['produced_by'] and tab['read_with'] for tab in tabs.values())
    assert [(item['id'], item.get('skill')) for item in retail.connections_expected()] == [
        ('camera', 'camera-intake'), ('till', 'catalog-onboarding')]
    assert [source['id'] for source in retail.data_sources()] == ['observations', 'trackers', 'catalog', 'business', 'cameras']
    assert [(stage['stage'], stage['attaches']) for stage in retail.flow()] == [
        ('observe', 'host'), ('admit', 'host'), ('derive', 'script'), ('process', 'script'),
        ('publish', 'host'), ('alert', 'host'), ('act', 'agent')]
    # Accessors hand out copies: a caller's edit stays with the caller.
    retail.surfaces()[0]['title'] = 'Changed'
    retail.fixed_chat('organization')['title'] = 'Changed'
    assert retail.surfaces()[0]['title'] == 'Home' and retail.purposes()['organization'] == 'Organization'


# The swap: every read point follows the active template.

def test_fixed_chats_follow_the_active_template(tmp_path, jobshop):
    chats = company(tmp_path)
    agent = Agent(chats)
    assert organization_chats.PURPOSES == {'organization': 'Organization', 'office': 'Office'}
    office = chats.ensure(agent, 'cafe', 'office')
    assert office['id'] == 'orgchat-d018764335ac3cd5dbc85db297a4d992' and office['title'] == 'Office'
    assert chats.canonical(office)
    # The Organization conversation keeps its id under every template.
    assert chats.identity('cafe', 'organization') == FIXED_CHAT_IDS['organization']
    for purpose in ('cameras', 'messaging'):
        with pytest.raises(ValueError, match=r'^Choose Organization or Office\.$'):
            chats.identity('cafe', purpose)
    # A conversation saved under another template stays a plain record here.
    assert not chats.canonical({**office, 'id': FIXED_CHAT_IDS['cameras'], 'organization_purpose': 'cameras'})


def test_fixed_chat_instructions_follow_the_active_template(tmp_path, jobshop):
    chats = company(tmp_path)
    agent = Agent(chats)
    office = chats.ensure(agent, 'cafe', 'office')
    text = intake_instructions.for_chat(agent, office['id'], 'base')
    assert text == 'base\n' + (JOBSHOP / 'prompts/common.md').read_text() + '\n' + (JOBSHOP / 'prompts/office.md').read_text()
    assert text.endswith((JOBSHOP / 'prompts/office.md').read_text())
    for retail_sentence in ('Company intake conversation:', 'Camera intake workflow:', 'Organization intake workflow:', 'POS'):
        assert retail_sentence not in text
    assert intake_instructions.COMMON == jobshop.prompt('fixed_chat_common')
    assert intake_instructions.ORGANIZATION.startswith('Organization workflow for a job shop:')
    with pytest.raises(AttributeError):
        intake_instructions.CAMERA
    assert jobshop.prompt('system').startswith("You are Rocoto's assistant for a sheet-metal job shop.")


def test_skill_seeds_follow_the_active_template(tmp_path):
    with pack.use('retail'):
        SkillStore(tmp_path / 'existing')
    with pack.use(JOBSHOP) as jobshop:
        assert [seed['id'] for seed in skills.BUILTINS] == ['backlog-intake']
        fresh = SkillStore(tmp_path / 'fresh').catalogue()['skills']
        assert [item['id'] for item in fresh] == ['backlog-intake']
        assert fresh[0]['instructions'] == jobshop.skills()[0]['instructions'] and fresh[0]['publisher'] == 'Rocoto'
    # A company keeps every skill row it was seeded with, whichever template opens its state later.
    with pack.use('retail') as retail:
        reopened = SkillStore(tmp_path / 'existing').catalogue()['skills']
        assert [item['id'] for item in reopened] == sorted(seed['id'] for seed in retail.skills())
        assert all(item['revision'] == 1 for item in reopened)


def test_ready_agents_classes_display_set_and_shell_follow_the_active_template(jobshop):
    assert [item['id'] for item in agent_catalogue.catalogue()['templates']] == ['late-job-watch']
    assert agent_catalogue.available() == agent_catalogue.catalogue() and agent_catalogue.retired()['templates'] == []
    assert agent_catalogue.template('queue-review') is None
    assert [item['label'] for item in jobshop.agent_classes()] == ['Dispatch', 'Desk']
    assert jobshop.authority('tracker') == 'participant_reach' and jobshop.default_kind() == 'action'
    assert [tracker['key'] for tracker in jobshop.tile_set('jobshop')['trackers']] == ['backlog', 'delivery_estimate']
    assert jobshop.tile_set_path('jobshop') == JOBSHOP / 'tile_sets' / 'jobshop.json'
    assert sorted(jobshop.vocabulary()['types']) == ['job_finished', 'job_opened', 'week_full']
    shell = jobshop.shell()
    assert [section['key'] for section in shell['sections']] == ['Organization', 'Dispatch', 'Desk', 'Recent']
    assert [chat['title'] for chat in shell['fixed_chats']] == ['Organization', 'Office']
    assert shell['surfaces'] == [{'id': 'backlog', 'title': 'Backlog', 'nav': 'primary'}]
    assert shell['sections'][2]['starter_prompt'].startswith('Help me create a Desk')
    assert jobshop.digest() != pack.load('retail').digest()


# The process template and the test swap.

def test_the_process_template_comes_from_rocoto_pack_once_and_a_swap_restores_it(monkeypatch):
    monkeypatch.setattr(pack, '_ACTIVE', None)
    monkeypatch.setattr(pack.layout, '_INSTALLED_VERTICAL', None)
    monkeypatch.delenv('ROCOTO_PACK', raising=False)
    monkeypatch.delenv('ROCOTO_VERTICAL', raising=False)
    first = pack.active()
    assert first.id == 'retail' == pack.DEFAULT and pack.ENVIRONMENT == 'ROCOTO_VERTICAL'
    monkeypatch.setenv('ROCOTO_PACK', 'another')
    assert pack.active() is first
    with pytest.raises(RuntimeError):
        with pack.use(JOBSHOP) as swapped:
            assert pack.active() is swapped and swapped.id == 'jobshop'
            raise RuntimeError('a failing test body')
    assert pack.active() is first
    for value, message in (('Retail/..', 'ROCOTO_VERTICAL names one vertical folder under verticals/'),
                           ('absent', 'has its manifest at absent/vertical.json')):
        monkeypatch.setattr(pack, '_ACTIVE', None)
        monkeypatch.setenv('ROCOTO_PACK', value)
        with pytest.raises(pack.PackError, match=message):
            pack.active()
    monkeypatch.setattr(pack, '_ACTIVE', None)
    monkeypatch.setenv('ROCOTO_PACK', ' retail ')
    assert pack.active().id == 'retail'
    declared = (HERE / '.env.example').read_text().splitlines()
    assert 'ROCOTO_VERTICAL=bare' in declared


def test_a_template_that_fails_validation_stops_the_service_while_it_starts():
    # server.py imports skills at its top, and skills loads the process template as it is imported.
    assert 'from skills import SkillStore' in (HERE / 'server.py').read_text().split('\ndef ', 1)[0]
    def start(value):
        # The script only imports: the vertical it prints is the one the import itself loaded.
        return subprocess.run([sys.executable, '-c', 'import layout; layout.install(); import skills, pack; print(pack._ACTIVE.id)'], cwd=HERE,
            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'ROCOTO_PACK': value},
            capture_output=True, text=True, timeout=60)
    good = start('retail')
    assert good.returncode == 0 and good.stdout.strip() == 'retail', good.stderr
    bad = start('absent')
    assert bad.returncode != 0 and 'PackError: The vertical absent has its manifest at absent/vertical.json' in bad.stderr


def test_a_skill_seed_past_the_store_s_bounds_stops_the_service_while_it_starts(tmp_path):
    # Skill rows are immutable and every new company is seeded, so a seed the store declines is
    # found at start, by the loader and again by the store's own rule.
    copy_of_jobshop(tmp_path, seed(instructions='x' * 20000))
    script = f'import layout; layout.install(); import pack; pack.PACKS = __import__("pathlib").Path({str(tmp_path)!r}); import skills'
    started = subprocess.run([sys.executable, '-c', script], cwd=HERE, capture_output=True, text=True, timeout=60,
        env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'ROCOTO_PACK': 'jobshop'})
    assert started.returncode != 0
    assert 'PackError: skills.json[0] carries 20000 characters of instructions; a skill holds up to 16000.' in started.stderr
    # The store applies its own rule to the seeds of whichever template it is handed.
    whole = pack.load(JOBSHOP)
    long = dataclasses.replace(whole, _skills=({**whole.skills()[0], 'instructions': 'x' * 20000},))
    sentence = r'^Correct the skill seed backlog-intake of the vertical jobshop: Use a bounded, readable skill name'
    with pytest.raises(pack.PackError, match=sentence):
        skills._seeds(long)
    with pack.use(long):
        with pytest.raises(pack.PackError, match=sentence):
            SkillStore(tmp_path / 'company')
    assert not (tmp_path / 'company' / 'skills.sqlite').exists()


def test_a_seed_the_loader_accepts_is_one_the_skill_store_saves(tmp_path):
    def fields(**changes):
        return {'name': 'Backlog intake', 'description': 'Read the backlog.', 'instructions': 'Read it.',
                'required_tools': [], 'required_connections': [], **changes}
    edges = {
        'instructions': (pack.SEED_INSTRUCTIONS, lambda count: {'instructions': 'x' * count}),
        'tools': (pack.SEED_TOOLS, lambda count: {'required_tools': [f'tool_{number}' for number in range(count)]}),
        'connections': (pack.SEED_CONNECTIONS, lambda count: {'required_connections': [
            {'provider': f'provider-{number}', 'scopes': ['read:orders']} for number in range(count)]}),
        'scopes': (pack.SEED_SCOPES, lambda count: {'required_connections': [
            {'provider': 'shopify', 'scopes': [f'read:scope{number}' for number in range(count)]}]}),
    }
    # At each bound both accept; one past it both refuse.
    for label, (bound, make) in edges.items():
        for count, accepted in ((bound, True), (bound + 1, False)):
            body = fields(**make(count))
            try:
                skills._definition(body)
                store = True
            except ValueError:
                store = False
            root = copy_of_jobshop(tmp_path / f'{label}-{count}', seed(**body))
            try:
                template = pack.load(root)
                loader = True
            except pack.PackError:
                loader = False
            assert (store, loader) == (accepted, accepted), (label, count)
            if accepted:
                with pack.use(template):
                    saved = SkillStore(tmp_path / f'company-{label}').catalogue()['skills'][0]
                assert {key: saved[key] for key in body} == body
    # Every seed of both templates in the tree passes the store's rule unchanged.
    for source in ('retail', JOBSHOP):
        template = pack.load(source)
        assert [definition for _, definition in skills._seeds(template)] == [
            {key: row[key] for key in skills._FIELDS} for row in template.skills()]
    assert (pack._SLUG.pattern, pack._TOOL.pattern, pack._SCOPE.pattern, pack._PROVIDER.pattern) == (
        skills._NAME.pattern, skills._TOOL.pattern, skills._SCOPE.pattern, skills._NAME.pattern)


def test_the_loader_holds_agent_classes_and_ready_made_agents_to_the_agent_store():
    from agent_kind_policy import has_participant_reach_context
    from background_agents import AgentDefinition
    from retail_agent_inputs import RetailAgentDefinition
    generic = AgentDefinition(title='Plan orders', instruction='Plan orders.', dataset='workspace', agent_kind='planner')
    assert generic.agent_kind == 'planner' and generic.input_kind == 'none'
    stored = RetailAgentDefinition.model_fields
    assert set(get_args(stored['input_kind'].annotation)) == {'none', *pack.INPUT_KINDS}
    assert stored['agent_kind'].default == pack.DEFAULT_KIND
    assert get_args(stored['mode'].annotation) == pack.AGENT_MODES
    assert set(pack.KIND_AUTHORITY.values()) == set(pack.AUTHORITIES)
    # Authority follows the classes declared by the retail vertical.
    agent = types.SimpleNamespace(children={'desk': {'agent_kind': 'tracker'}, 'dispatch': {'agent_kind': 'action'}},
        automation_store=types.SimpleNamespace(agent_classes={row['kind']: row for row in pack.load('retail').agent_classes()}))
    assert pack.KIND_AUTHORITY == {'tracker': 'participant_reach', 'action': 'granted_effects'}
    assert has_participant_reach_context(agent, 'desk') and not has_participant_reach_context(agent, 'dispatch')

    def saved(**changes):
        return RetailAgentDefinition(**{'title': 'Late job watch', 'instruction': 'List the late jobs.', 'dataset': 'cafe', **changes})
    first, last = pack.AGENT_INTERVAL
    for mode, start in (('ongoing', first), ('scheduled', pack.AGENT_SCHEDULED_FROM)):
        assert saved(mode=mode, interval_seconds=start) and saved(mode=mode, interval_seconds=last)
        for outside in (start - 1, last + 1):
            with pytest.raises(ValueError):
                saved(mode=mode, interval_seconds=outside)
    assert saved(instruction='x' * pack.AGENT_INSTRUCTION) and saved(skill_ids=[f'skill-{n}' for n in range(pack.AGENT_SKILLS)])
    lines, length = pack.AGENT_WATCH_FOR
    assert saved(mode='ongoing', max_runs=pack.AGENT_RUNS[0]) and saved(mode='ongoing', max_runs=pack.AGENT_RUNS[1])
    assert saved(watch_for=['x' * length] * lines)
    for outside in ({'instruction': 'x' * (pack.AGENT_INSTRUCTION + 1)},
                    {'skill_ids': [f'skill-{n}' for n in range(pack.AGENT_SKILLS + 1)]},
                    {'mode': 'ongoing', 'max_runs': pack.AGENT_RUNS[0] - 1}, {'mode': 'ongoing', 'max_runs': pack.AGENT_RUNS[1] + 1},
                    {'watch_for': ['x'] * (lines + 1)}, {'watch_for': ['x' * (length + 1)]}):
        with pytest.raises(ValueError):
            saved(**outside)
    # The loader accepts a ready-made agent at each of those bounds.
    for template in (pack.load('retail'), pack.load(JOBSHOP)):
        for row in template.ready_agents() + template.retired_ready_agents():
            assert saved(mode=row['mode'], interval_seconds=row['interval_seconds'], instruction=row['instructions'],
                         skill_ids=row['skill_ids'])


def ready_made_ids():
    return [row['id'] for row in pack.load('retail').ready_agents()]


@pytest.mark.parametrize('template_id', ready_made_ids())
def test_a_draft_from_each_ready_made_agent_activates_and_starts_its_first_run(tmp_path, template_id):
    """The fixture of tests/test_monitoring.py: a monitoring store with one source, a profile and a
    committed event; an agent store; a scheduler with a harness that answers a turn."""
    import asyncio
    import time
    from background_agents import AgentDefinition, AgentScheduler, AgentStore
    from monitoring import MonitoringStore
    from retail_agent_inputs import RetailInputProvider, RetailAgentDefinition
    from workspace_capabilities import WorkspaceCapabilities
    sources = {'a': {'id': 'a', 'video_id': 'recording-a', 'dataset': 'shop', 'start': 0, 'end': 20}}

    class Index:
        clips = list(sources.values())
        def status(self): return {'ready': False, 'datasets': ['shop']}
        def get_clip(self, key): return sources.get(key)

    class Harness:
        def __init__(self): self.children = {}; self.child_tasks = {}; self.calls = []
        def _save_child(self, record): self.children[record['id']] = record
        async def turn(self, message, chat_id, dataset, **options):
            self.calls.append((message, options))
            self.children[chat_id]['messages'].append({'role': 'assistant', 'content': 'One observation, with its evidence link.'})
            yield {'type': 'done', 'status': 'completed'}

    async def prove():
        with pack.use('retail') as retail:
            row = agent_catalogue.template(template_id)
            vocabulary = retail.vocabulary()['types']
            assert row['input_kind'] == 'tracker_events' and row['agent_kind'] in retail.kinds()
            assert row['event_types'] and set(row['event_types']) <= set(vocabulary)
            # Each skill it names is one a turn admits today; the three shipped ones name zero.
            from agent_bridge import tool_specs
            assert set(row['skill_ids']).isdisjoint(retail.skill_tool_gaps({spec['name'] for spec in tool_specs(True)} | {'web_search'}))
            monitoring = MonitoringStore(tmp_path, source_lookup=lambda owner, key: sources.get(key))
            reads = monitoring.save_profile('alice', {'name': row['name'], 'dataset': 'shop', 'vocabulary': [
                {'type': kind, 'description': vocabulary[kind]['label']} for kind in row['event_types']]}, datasets=['shop'])
            other = monitoring.save_profile('alice', {'name': 'Tables', 'dataset': 'shop', 'vocabulary': [
                {'type': 'table_cleaned', 'description': 'A table is cleaned.'}]}, datasets=['shop'])
            observed = monitoring.commit_event('alice', {
                'profile_id': reads['id'], 'profile_revision': reads['revision'], 'event_key': 'e1', 'type': row['event_types'][0],
                'description': 'Seen once at the shop.', 'source': {'clip_id': 'a', 'start_s': 2, 'end_s': 3}, 'track_id': 'p1'})
            definition = agent_catalogue.draft(template_id, 'shop', monitoring.list_profiles('alice', 'shop'))
            # The draft is the agent store's own definition, and it follows the profile that holds its event types.
            assert RetailAgentDefinition.model_validate(definition).model_dump()['instruction'] == row['instructions']
            assert (definition['title'], definition['mode'], definition['agent_kind']) == (row['name'], row['mode'], row['agent_kind'])
            assert definition['profile_ids'] == [reads['id']] and other['id'] not in definition['profile_ids']
            index = Index()
            agents, harness = AgentStore(tmp_path, input_provider=RetailInputProvider(monitoring=monitoring, index=index), agent_classes=retail.agent_classes()), Harness()
            scheduler = AgentScheduler(agents, harness, asyncio.Semaphore(1))
            saved = agents.create(definition, ['shop'], owner_principal_id='alice')
            assert saved['status'] == 'draft' and saved['input_kind'] == 'tracker_events'
            assert WorkspaceCapabilities(inputs=lambda: agents.inputs).execution(saved)['ready'] is True
            active = agents.activate(saved['id'], time.time() + 1000)
            assert active['status'] == 'active'
            await scheduler.tick()
            await asyncio.gather(*scheduler.tasks.values())
            run = agents.get(saved['id'])['last_run']
            assert run['status'] == 'completed' and agents.get(saved['id'])['runs_started'] == 1
            message, options = harness.calls[0]
            assert message.startswith(row['instructions']) and observed['id'] in message
            assert harness.children[saved['chat_id']]['agent_kind'] == row['agent_kind']
            assert agents.run_report(saved['id'], run['id'])['observations'][0]['id'] == observed['id']
        with pack.use('retail'):
            with pytest.raises(ValueError, match='Choose an available ready-made agent'):
                agent_catalogue.draft('queue-review', 'shop')  # a retired template stays closed to new drafts
    asyncio.run(prove())


def test_a_draft_takes_the_default_class_and_its_input_where_a_template_leaves_them_out(jobshop):
    definition = agent_catalogue.draft('late-job-watch', 'cafe')
    assert (definition['agent_kind'], definition['input_kind'], definition['profile_ids']) == ('action', 'tracker_events', [])
    assert (definition['title'], definition['mode'], definition['interval_seconds'], definition['skill_ids']) == (
        'Late job watch', 'scheduled', 86400, ['backlog-intake'])


# One state directory, one template.

def test_a_state_directory_stays_with_the_template_that_created_it(tmp_path):
    state = tmp_path / 'company'
    with pack.use('retail') as retail:
        chats = company(tmp_path, 'company')
        cameras = chats.ensure(Agent(chats), 'cafe', 'cameras')
        store = SkillStore(state)
        assert len(store.catalogue()['skills']) == 13 and store.path.name == pack.SEEDED
        assert json.loads((state / pack.BINDING).read_text()) == {
            'schema': 'rocoto.workspace-template.v1', 'template': 'retail'}
        assert (state / pack.BINDING).stat().st_mode & 0o777 == 0o600 and pack.bind(state) is retail
    sentence = (r'^This state directory belongs to the vertical retail; this service serves jobshop\. '
                r'Start it with ROCOTO_VERTICAL=retail, or give jobshop a state directory of its own\.$')
    with pack.use(JOBSHOP):
        with pytest.raises(pack.PackError, match=sentence):
            SkillStore(state)
        with pytest.raises(pack.PackError, match=sentence):
            company(tmp_path, 'company')
    # Back under its own template the company holds exactly what it had.
    with pack.use('retail') as retail:
        assert [item['id'] for item in SkillStore(state).catalogue()['skills']] == sorted(row['id'] for row in retail.skills())
        assert company(tmp_path, 'company').canonical(cameras)
    assert sorted(path.name for path in state.iterdir()) == [
        '.workspace-identity.json', '.workspace-identity.lock', '.workspace-template.json', 'skills.sqlite']


def test_a_directory_seeded_before_the_record_existed_belongs_to_the_default_template(tmp_path):
    state = tmp_path / 'deployed'
    with pack.use('retail'):
        SkillStore(state)
    (state / pack.BINDING).unlink()  # every company opened before 11 October 2026 looks like this
    with pack.use(JOBSHOP):
        with pytest.raises(pack.PackError, match='belongs to the vertical retail; this service serves jobshop'):
            SkillStore(state)
        with pytest.raises(pack.PackError, match='belongs to the vertical retail; this service serves jobshop'):
            company(tmp_path, 'deployed')
        assert not (state / pack.BINDING).exists()
        # A new directory takes the vertical that opens it first, whichever store opens it.
        company(tmp_path, 'fresh')
        assert json.loads((tmp_path / 'fresh' / pack.BINDING).read_text())['template'] == 'jobshop'
        assert [item['id'] for item in SkillStore(tmp_path / 'fresh').catalogue()['skills']] == ['backlog-intake']
    with pack.use('retail'):
        assert len(SkillStore(state).catalogue()['skills']) == 13
        assert json.loads((state / pack.BINDING).read_text())['template'] == pack.DEFAULT == 'retail'
        with pytest.raises(pack.PackError, match='belongs to the vertical jobshop; this service serves retail'):
            SkillStore(tmp_path / 'fresh')
        # A damaged record keeps the directory closed.
        for damage in ('', '{"template": "retail"}', '{"schema": "rocoto.workspace-template.v1", "template": "Retail/.."}',
                       '{"schema": "rocoto.workspace-template.v1", "template": "retail", "note": 1}', '[]', 'x' * 5000):
            (state / pack.BINDING).write_text(damage)
            with pytest.raises(pack.PackError, match="restore that file from the directory's backup"):
                SkillStore(state)
        (state / pack.BINDING).unlink()
        (state / pack.BINDING).symlink_to(tmp_path / 'fresh' / pack.BINDING)
        with pytest.raises(pack.PackError, match="restore that file from the directory's backup"):
            SkillStore(state)


# What the loader refuses.

def copy_of_jobshop(tmp_path, change=None, text=None):
    """A private copy of the test template with one change to its manifest."""
    root = tmp_path / 'jobshop'
    shutil.copytree(JOBSHOP, root)
    manifest = json.loads((root / 'vertical.json').read_text())
    if change:
        change(manifest, root)
        (root / 'vertical.json').write_text(json.dumps(manifest))
    if text:
        (root / 'vertical.json').write_text(text((root / 'vertical.json').read_text()))
    return root


def outside(name):
    def change(manifest, root):
        (root.parent / name).write_text('[]' if name.endswith('.json') else 'text\n')
    return change


def edit(path, value, also=None):
    """Set manifest[path...] = value, then apply another change."""
    def change(manifest, root):
        target = manifest
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
        if also:
            also(manifest, root)
    return change


def link_out(manifest, root):
    (root.parent / 'kept-elsewhere.md').write_text('text\n')
    (root / 'prompts' / 'link.md').symlink_to(root.parent / 'kept-elsewhere.md')
    manifest['prompts']['system'] = 'prompts/link.md'


def duplicate(key, index, **changes):
    def change(manifest, root):
        manifest[key].append({**manifest[key][index], **changes})
    return change


def put(relative, data, also=None):
    """Write these bytes at a path inside the copy, then apply another change."""
    def change(manifest, root):
        (root / relative).write_bytes(data)
        if also:
            also(manifest, root)
    return change


def rows(relative, change_rows):
    """Change the JSON one file of the copy holds."""
    def change(manifest, root):
        value = json.loads((root / relative).read_text())
        change_rows(value)
        (root / relative).write_text(json.dumps(value))
    return change


def capability(*written):
    """Give the job-shop template these rows of the table of what works."""
    def change(manifest, root):
        (root / 'capabilities.json').write_text(json.dumps(list(written)))
        manifest['capabilities'] = 'capabilities.json'
    return change


def seed(**changes):
    """Change the one skill seed of the job-shop template."""
    return rows('skills.json', lambda seeds: seeds[0].update(changes))


def second_name(manifest, root):
    os.link(root / 'prompts' / 'system.md', root.parent / 'second-name.md')


def kinds(*names):
    def change(manifest, root):
        for row, name in zip(manifest['agent_classes'], names):
            row['kind'] = name
    return change


PROMPT = 'a prompt holds printing characters, line feeds and tabs, and this one holds '
WORDS = "write the prompt's words in that file"
READY_MADE = {'id': 'late', 'name': 'Late', 'mode': 'scheduled', 'description': 'Late jobs.',
              'instructions': 'List the late jobs.', 'interval_seconds': 86400}


REFUSED = {
    'a prompt path outside the vertical folder': (
        edit(['prompts', 'system'], '../outside.md', outside('outside.md')), 'outside the vertical folder'),
    'a fixed chat prompt outside the vertical folder': (
        edit(['fixed_chats', 1, 'prompt'], 'prompts/../../outside.md', outside('outside.md')), 'outside the vertical folder'),
    'a skills list outside the vertical folder': (
        edit(['skills'], '../outside.json', outside('outside.json')), 'outside the vertical folder'),
    'a tile_set outside the vertical and the harness': (
        edit(['tile_sets', 0, 'path'], '../outside.json', outside('outside.json')), 'outside the vertical folder'),
    'an absolute path': (edit(['prompts', 'system'], '/etc/hostname.md'), 'relative path inside the vertical folder'),
    'a link that leaves the vertical folder': (link_out, 'add that regular file to the vertical'),
    'a named file that is absent': (edit(['fixed_chats', 1, 'prompt'], 'prompts/missing.md'), 'add that regular file'),
    'a prompt that is another kind of file': (edit(['prompts', 'system'], 'skills.json'), r'names a \.md file'),
    'a missing organization chat': (edit(['fixed_chats', 0, 'purpose'], 'company'), 'includes the organization conversation'),
    'a duplicate purpose': (duplicate('fixed_chats', 1), 'names the purpose office twice'),
    'a duplicate kind': (duplicate('agent_classes', 1, label='Bench', default=False), 'names the kind tracker twice'),
    'a duplicate label': (edit(['agent_classes', 1, 'label'], 'Dispatch'), 'names the label Dispatch twice'),
    'an unknown authority': (edit(['agent_classes', 1, 'authority'], 'root'), "one of the harness's authorities"),
    'two default classes': (edit(['agent_classes', 1, 'default'], True), 'exactly one class as the default'),
    'zero default classes': (edit(['agent_classes', 0, 'default'], False), 'exactly one class as the default'),
    'a key outside the contract': (edit(['colour'], 'red'), 'carries colour, outside the contract'),
    'a fixed chat key outside the contract': (edit(['fixed_chats', 0, 'owner'], 'me'), 'carries owner'),
    'another schema': (edit(['schema'], 'rocoto.pack.v2'), 'declares the schema rocoto.vertical.v1'),
    'an id that differs from its folder': (edit(['id'], 'other'), 'equals its folder name jobshop'),
    'a fixed chat skill the vertical lacks': (edit(['fixed_chats', 1, 'skill'], 'ghost'), 'skill names ghost'),
    'a data source skill the vertical lacks': (edit(['data_sources', 0, 'skill'], 'ghost'), 'skill names ghost'),
    'a connection skill the vertical lacks': (edit(['connections_expected', 0, 'skill'], 'ghost'), 'skill names ghost'),
    'a tile_set revision that differs from its file': (edit(['tile_sets', 0, 'revision'], 'jobshop-core-test-2'), 'revision equals the revision written'),
    'a processor outside the display_set prefix': (edit(['tile_sets', 0, 'processors'], 'core_trackers.'), 'processor script under core_trackers.'),
    'an event group the vocabulary uses and the manifest omits': (edit(['event_vocabulary', 'groups'], ['shop_floor']), 'week_full uses office'),
    'a vocabulary reference to another display_set': (edit(['event_vocabulary', 'ref'], 'tile_sets/retail#vocabulary'), 'points at a tile_set of this vertical'),
    'an unknown display': (edit(['surfaces', 0, 'tabs', 0, 'display'], 'chart'), 'is one of tiles, canvases, player or list'),
    'an unknown flow attachment': (edit(['flow', 0, 'attaches'], 'human'), 'is one of script, agent or host'),
    'an unknown way to open a fixed chat': (edit(['fixed_chats', 1, 'opens'], 'popup'), 'is one of chat or member_setup'),
    'a read tool written as prose': (edit(['data_sources', 0, 'read_with'], ['List trackers']), 'a list of read tool names'),
    'a tool family written as a path': (edit(['tool_families'], ['../tools.py']), 'module or module:attribute'),
    'a ready-made agent that lacks its mode': (edit(['ready_agents'], [{'id': 'late', 'name': 'Late'}]), 'mode is one of'),
    # The skill store's bounds on a seed (skills.py): a seed the loader accepts is one the store saves.
    'a skill seed with instructions past the store bound': (
        seed(instructions='x' * 16001), 'carries 16001 characters of instructions; a skill holds up to 16000'),
    'a skill seed that names one tool twice': (
        seed(required_tools=['get_business_profile', 'get_business_profile']), 'names the tool get_business_profile twice'),
    'a skill seed with 33 tools': (
        seed(required_tools=[f'tool_{number}' for number in range(33)]), 'names 33 tools; a skill needs up to 32'),
    'a skill seed with a write scope': (
        seed(required_connections=[{'provider': 'shopify', 'scopes': ['write:orders']}]), 'a list of read scopes'),
    'a skill seed with a connection that names zero scopes': (
        seed(required_connections=[{'provider': 'shopify', 'scopes': []}]), 'names 1 to 16 read scopes'),
    'a skill seed with one provider twice': (
        seed(required_connections=[{'provider': 'shopify', 'scopes': ['read:orders']}] * 2), 'names the provider shopify twice'),
    'a skill seed with nine connections': (
        seed(required_connections=[{'provider': f'p{number}', 'scopes': ['read:orders']} for number in range(9)]),
        'names 9 connections; a skill needs up to 8'),
    'a skill seed with a control character': (seed(instructions='Read\x1b the backlog.'), 'instructions is text'),
    # The agent kinds the harness stores (background_agents.py) and the authority it applies to each.
    'an invalid class identifier': (edit(['agent_classes', 0, 'kind'], 'Dispatch orders'), 'lowercase agent class identifier'),
    'a tracker with the authority of an action': (
        edit(['agent_classes', 1, 'authority'], 'granted_effects'), 'existing class tracker keeps its authority participant_reach'),
    'an action with the reach of a tracker': (
        edit(['agent_classes', 0, 'authority'], 'participant_reach'), 'existing class action keeps its authority granted_effects'),
    'an invalid input identifier': (
        edit(['agent_classes', 0, 'input_kinds'], ['order updates']), 'lowercase input provider identifier'),
    'an input kind named twice': (
        edit(['agent_classes', 0, 'input_kinds'], ['tracker_events', 'tracker_events']), 'names the input kind tracker_events twice'),
    # Named files: one regular file each, inside its size, and prompts as plain text.
    'a prompt past its size': (put('prompts/system.md', b'x' * (256 * 1024 + 1)), 'that file holds up to 262144 bytes'),
    'a skills list past its size': (put('skills.json', b'[]' + b' ' * (1024 * 1024)), 'that file holds up to 1048576 bytes'),
    'a manifest past its size': (None, 'that file holds up to 1048576 bytes'),
    'an empty prompt': (put('prompts/system.md', b''), WORDS),
    'a prompt of spaces': (put('prompts/office.md', b' \n\t\n'), WORDS),
    'a prompt with control characters': (put('prompts/system.md', b'Read the \x1bbacklog.\n'), PROMPT + r'U\+001B'),
    'a prompt with a zero byte': (put('prompts/system.md', b'Read\x00 the backlog.\n'), PROMPT + r'U\+0000'),
    'a prompt that starts with a byte order mark': (
        put('prompts/system.md', b'\xef\xbb\xbfRead the backlog.\n'), 'starts with its first word, the byte order mark removed'),
    'a prompt with carriage returns': (put('prompts/common.md', b'Read the backlog.\r\nAnswer.\r\n'), PROMPT + r'U\+000D'),
    'a prompt in another encoding': (put('prompts/system.md', 'Café'.encode('latin-1')), 'save it as UTF-8 text'),
    'a prompt with a second name on disk': (second_name, 'one regular file with a single name on disk'),
    'a prompt beside the prompts folder': (
        edit(['prompts', 'system'], 'system.md', put('system.md', b'Read the backlog.\n')), "keep each prompt in the vertical's prompts/ folder"),
    'a value outside JSON': (edit(['demo'], {'share': float('nan')}), 'is UTF-8 JSON: NaN is outside JSON'),
    'a manifest nested thousands deep': (None, 'vertical.json is UTF-8 JSON'),
    'a number of five thousand digits': (None, 'vertical.json is UTF-8 JSON'),
    'two fixed chats with one title': (edit(['fixed_chats', 1, 'title'], 'Organization'), 'names the title Organization twice'),
    'a fixed chat skill written as a list': (
        edit(['fixed_chats', 1, 'skill'], ['backlog-intake']), r"fixed_chats\[1\]\.skill names \['backlog-intake'\]"),
    'a data source skill written as a list': (
        edit(['data_sources', 0, 'skill'], ['backlog-intake']), r"data_sources\[0\]\.skill names \['backlog-intake'\]"),
    'a connection skill written as an object': (
        edit(['connections_expected', 0, 'skill'], {'id': 'backlog-intake'}), r'connections_expected\[0\]\.skill names'),
    'a tile_set tracker whose processor is a string': (
        rows('tile_sets/jobshop.json', lambda tile_set: tile_set['trackers'][0].update(processor='jobshop_trackers.backlog')),
        'each tracker has a key and a processor script under jobshop_trackers.'),
    'a tile_set tracker written as a string': (
        rows('tile_sets/jobshop.json', lambda tile_set: tile_set['trackers'].append('backlog')),
        'each tracker has a key and a processor script under jobshop_trackers.'),
    # A ready-made agent: what the Customize page draws and a draft agent starts from.
    'a ready-made agent with its name and mode alone': (
        edit(['ready_agents'], [{'id': 'late', 'name': 'Late', 'mode': 'ad_hoc'}]), 'description is one line of readable text'),
    'a ready-made agent that lacks its instructions': (
        edit(['ready_agents'], [{key: value for key, value in READY_MADE.items() if key != 'instructions'}]),
        'instructions is text of up to 8000 characters'),
    'a scheduled ready-made agent that runs every ten seconds': (
        edit(['ready_agents'], [{**READY_MADE, 'interval_seconds': 10}]), 'interval_seconds is a whole number of seconds from 60 to 604800'),
    'a scheduled ready-made agent that lacks its interval': (
        edit(['ready_agents'], [{key: value for key, value in READY_MADE.items() if key != 'interval_seconds'}]),
        'interval_seconds is a whole number of seconds from 60 to 604800'),
    'a ready-made agent with a skill the vertical lacks': (
        edit(['ready_agents'], [{**READY_MADE, 'skill_ids': ['ghost']}]), 'skill_ids names ghost'),
    'a ready-made agent of a kind the harness lacks': (
        edit(['ready_agents'], [{**READY_MADE, 'agent_kind': 'desk'}]), r'ready_agents\[0\].agent_kind names a declared agent class'),
    'a ready-made agent on an input the harness lacks': (
        edit(['ready_agents'], [{**READY_MADE, 'input_kind': 'emails'}]), r'ready_agents\[0\].input_kind is an input of the class action'),
    'a ready-made agent on an input its class leaves out': (
        edit(['ready_agents'], [{**READY_MADE, 'agent_kind': 'tracker', 'input_kind': 'camera_footage'}]),
        'input_kind is an input of the class tracker: tracker_events'),
    'a ready-made agent with twenty-one runs': (
        edit(['ready_agents'], [{**READY_MADE, 'max_runs': 21}]), 'max_runs is a whole number of runs from 1 to 20'),
    'a ready-made agent that reads an event type outside the vocabulary': (
        edit(['ready_agents'], [{**READY_MADE, 'event_types': ['job_opened', 'week_empty']}]),
        'event_types names week_empty; add that event type to the vocabulary'),
    'a ready-made agent with nine lines of what it looks for': (
        edit(['ready_agents'], [{**READY_MADE, 'watch_for': ['A late job'] * 9}]), 'watch_for holds up to 8 lines'),
    # The sidebar keys a section by its name: a class label stays apart from the two fixed sections and from the other label.
    'a class labelled as the Recent section': (
        edit(['agent_classes', 1, 'label'], 'Recent'), 'the label Recent, which the sidebar reads as its section Recent'),
    'a class labelled as the Organization section in other letters': (
        edit(['agent_classes', 0, 'label'], 'organization'), 'the label organization, which the sidebar reads as its section Organization'),
    'two class labels the sidebar reads as one': (
        edit(['agent_classes', 1, 'label'], 'DISPATCH'), 'the label DISPATCH, which the sidebar reads as its section Dispatch'),
    # The template's rows of the table of what works.
    'a capability row with neither an id nor a harness row': (
        capability({'title': 'The board', 'state': 'real', 'works': 'A board.', 'where': ['pack.py']}),
        'is a row of its own with an id, or sentences for a harness row named in adds_to'),
    'a capability row of its own that lacks its files': (
        capability({'id': 'board', 'title': 'The board', 'state': 'real', 'works': 'A board.'}), 'needs where'),
    'a capability row in a state outside the three': (
        capability({'id': 'board', 'title': 'The board', 'state': 'done', 'works': 'A board.', 'where': ['pack.py']}),
        'state is one of real, partial or planned'),
    'a capability row that says nothing': (
        capability({'id': 'board', 'title': 'The board', 'state': 'planned', 'where': ['pack.py']}),
        'says something: a sentence under works, missing or for_show, and the files behind it'),
    'a capability addition that says nothing': (capability({'adds_to': 'skills'}), 'says something: a sentence under works, missing or for_show'),
    'a capability row whose file leaves the repository': (
        capability({'id': 'board', 'title': 'The board', 'state': 'real', 'works': 'A board.', 'where': ['../../etc/passwd']}),
        'where is a list of file names inside the repository'),
    'two capability rows with one id': (
        capability({'id': 'board', 'title': 'The board', 'state': 'real', 'works': 'A board.', 'where': ['pack.py']},
                   {'id': 'board', 'title': 'The second board', 'state': 'real', 'works': 'A board.', 'where': ['pack.py']}),
        'names the row board twice'),
    'a capability file outside the vertical folder': (
        edit(['capabilities'], '../capabilities.json', outside('capabilities.json')), 'outside the vertical folder'),
}
# Three manifests that JSON text alone can express.
RAW = {
    'a manifest past its size': lambda raw: raw + ' ' * (1024 * 1024),
    'a manifest nested thousands deep': lambda raw: '[' * 100000,
    'a number of five thousand digits': lambda raw: raw.replace('"demo": null', '"demo": {"count": ' + '1' * 5000 + '}'),
}


@pytest.mark.parametrize('case', sorted(REFUSED))
def test_the_loader_refuses(tmp_path, case):
    change, message = REFUSED[case]
    root = copy_of_jobshop(tmp_path, change, RAW.get(case))
    with pytest.raises(pack.PackError, match=message):
        pack.load(root)
    # The command prints the same sentence in one line.
    assert pack.main([str(root)]) == 1




def test_the_loader_refuses_missing_keys_and_repeated_keys_and_accepts_the_untouched_copy(tmp_path):
    assert pack.load(copy_of_jobshop(tmp_path / 'whole')).digest() == pack.load(JOBSHOP).digest()
    with pytest.raises(pack.PackError, match='needs flow'):
        pack.load(copy_of_jobshop(tmp_path / 'short', lambda manifest, root: manifest.pop('flow')))
    repeated = copy_of_jobshop(tmp_path / 'twice', text=lambda raw: raw.replace('"demo": null', '"demo": null, "id": "jobshop"'))
    with pytest.raises(pack.PackError, match='names the key id twice'):
        pack.load(repeated)
    changed = copy_of_jobshop(tmp_path / 'changed')
    (changed / 'prompts' / 'office.md').write_text((changed / 'prompts' / 'office.md').read_text().replace('Keep', 'keep'))
    assert pack.load(changed).digest() != pack.load(JOBSHOP).digest()


def test_the_command_validates_one_template_and_prints_what_it_declares(tmp_path, capsys):
    assert pack.main(['retail']) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].startswith('vertical retail, revision retail-pack-2026-10-11.4, digest ')
    assert 'fixed chats: Organization (organization), Cameras (cameras), Messaging (messaging)' in printed
    assert 'agent classes: Action (action, granted_effects), Tracker (tracker, participant_reach)' in printed
    assert 'tile_sets: retail at retail-core-v1, 9 trackers' in printed and 'event types: 48 in 3 groups' in printed
    assert 'skills: 13; ready-made agents: 3 available, 15 retired' in printed
    assert pack.main([str(JOBSHOP)]) == 0
    assert 'surfaces: Backlog (list)' in capsys.readouterr().out
    broken = copy_of_jobshop(tmp_path, edit(['agent_classes', 1, 'authority'], 'root'))
    assert pack.main([str(broken)]) == 1
    assert capsys.readouterr().out.startswith("RED · agent_classes[1].authority is one of the harness's authorities")


# The checks the code that owns the registry runs.

def test_check_tools_names_each_tool_a_registry_lacks(jobshop):
    assert jobshop.tool_references() == [
        ('surface backlog, tab orders', 'list_trackers'), ('data source backlog', 'list_trackers'),
        ('data source backlog', 'get_tracker'), ('tile_set jobshop', 'install_core_trackers')]
    assert jobshop.check_tools({'list_trackers', 'get_tracker', 'install_core_trackers', 'send_message'}) == []
    assert jobshop.check_tools(['list_trackers']) == [
        'data source backlog uses get_tracker; register that tool or correct the name in vertical.json.',
        'tile_set jobshop uses install_core_trackers; register that tool or correct the name in vertical.json.']
    assert len(jobshop.check_tools(())) == 4
    assert jobshop.skill_tool_gaps({'get_business_profile'}) == {}
    assert jobshop.skill_tool_gaps(()) == {'backlog-intake': ('get_business_profile',)}


def test_check_tile_sets_holds_each_display_set_to_its_install_tool_and_its_processor_module(jobshop, tmp_path, monkeypatch):
    from agent_bridge import tool_specs
    with pack.use('retail'):
        live = tool_specs(True)
    install = next(spec for spec in live if spec['name'] == 'install_core_trackers')
    assert [field['enum'] for field in install['inputSchema']['properties'].values() if 'enum' in field] == [['retail']]
    # Retail's display_set installs: the tool takes its id and its processors import.
    assert pack.load('retail').check_tile_sets(live) == []
    # The job shop's display_set names a tool that takes retail alone and a module the service has yet to hold.
    assert jobshop.check_tile_sets(live) == [
        'tile_set jobshop installs through install_core_trackers, which takes retail; add jobshop to the values that tool takes.',
        'tile_set jobshop keeps its processor scripts in the module jobshop_trackers; add that module to the service.']
    # Each half is its own check: a tool that takes the id, a tool with zero enumerated fields, a module on the path.
    takes = [{'name': 'install_core_trackers', 'inputSchema': {'properties': {'business_type': {'type': 'string', 'enum': ['retail', 'jobshop']}}}}]
    plain = [{'name': 'install_core_trackers', 'inputSchema': {'properties': {'location_id': {'type': 'string'}}}}]
    for specs in (takes, plain, []):
        assert jobshop.check_tile_sets(specs) == [
            'tile_set jobshop keeps its processor scripts in the module jobshop_trackers; add that module to the service.']
    (tmp_path / 'jobshop_trackers.py').write_text('')
    monkeypatch.syspath_prepend(str(tmp_path))
    assert jobshop.check_tile_sets(takes) == [] and len(jobshop.check_tile_sets(live)) == 1


def test_a_template_states_its_own_rows_of_the_table_of_what_works(tmp_path, retail):
    rows = retail.capabilities()
    assert retail.manifest['capabilities'] == 'capabilities.json' and 'capabilities.json' in retail.files
    own, added = [row for row in rows if 'id' in row], [row for row in rows if 'adds_to' in row]
    assert [row['id'] for row in own] == [
        'home_numbers', 'demo_data', 'live_camera_viewing', 'product_names_and_prices', 'sales_and_conversion',
        'queue_and_shelf_trackers', 'tracker_runner', 'tracker_scripts', 'tracker_views', 'onboarding_conversations']
    assert [row['adds_to'] for row in added] == ['ready_made_agents', 'action_agents', 'scheduled_work', 'data_sources', 'skills', 'postgresql']
    assert all(row['state'] in pack.CAPABILITY_STATES and row['where'] for row in own)
    rows[0]['title'] = 'changed'
    assert retail.capabilities()[0]['title'] == 'The numbers on Home', 'every accessor returns its own copy'
    # The key is optional: a template that leaves it out states zero rows of its own.
    assert pack.load(JOBSHOP).capabilities() == [] and pack.load(JOBSHOP).manifest['capabilities'] is None
    # A template with rows loads them, and its digest follows the file.
    root = copy_of_jobshop(tmp_path, capability(
        {'id': 'backlog_board', 'title': 'The backlog board', 'state': 'partial', 'works': 'The order log.', 'missing': 'A calendar view is planned.',
         'where': ['pack.py']}, {'adds_to': 'skills', 'works': 'One seed, usable.'}))
    loaded = pack.load(root)
    assert [row.get('id') or row['adds_to'] for row in loaded.capabilities()] == ['backlog_board', 'skills']
    assert loaded.digest() != pack.load(JOBSHOP).digest()


def test_the_skills_table_of_the_retail_document_is_what_a_turn_admits(retail):
    from agent_bridge import tool_specs
    # A turn admits a skill against the registry and web_search, which the turn adds by itself
    # (`resolve_skills` in agent_bridge.py). The table in verticals/retail/PACK.md is this result.
    admitted = {spec['name'] for spec in tool_specs(True)} | {'web_search'}
    held = retail.skill_tool_gaps(admitted)
    assert sorted(row['id'] for row in retail.skills() if row['id'] not in held) == [
        'camera-intake', 'catalog-onboarding', 'organization-intake', 'public-business-research']
    assert sorted(held) == [
        'axis-recording-context', 'evidence-export-manifest', 'gmail-correspondence-context', 'pattern-overview',
        'search-planning', 'shopify-order-context', 'source-clip-selects', 'source-evidence-inspection',
        'square-order-context']
    # From the registry alone the method also holds the skill that needs web_search.
    assert set(retail.skill_tool_gaps(admitted - {'web_search'})) - set(held) == {'public-business-research'}


def test_tool_families_import_by_the_names_the_manifest_gives(tmp_path, monkeypatch):
    source = "from types import SimpleNamespace\nBACKLOG = SimpleNamespace(READ_NAMES=frozenset({'list_jobs'}), WRITE_NAMES=frozenset())\n"
    root = copy_of_jobshop(tmp_path / 'named', edit(['tool_families'], ['jobshop_tool_family:BACKLOG', 'jobshop_tool_family']))
    (root / 'jobshop_tool_family.py').write_text(source)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.delitem(sys.modules, 'jobshop_tool_family', raising=False)
    named = pack.load(root)
    assert named.tool_families() == ('jobshop_tool_family:BACKLOG', 'jobshop_tool_family')
    families = named.import_tool_families()
    assert families[0] is families[1].BACKLOG
    assert families[0].READ_NAMES == {'list_jobs'}
    absent_root = copy_of_jobshop(tmp_path / 'absent', edit(['tool_families'], ['jobshop_tool_family:ORDERS']))
    (absent_root / 'jobshop_tool_family.py').write_text(source)
    monkeypatch.syspath_prepend(str(absent_root))
    monkeypatch.delitem(sys.modules, 'jobshop_tool_family', raising=False)
    with pytest.raises(pack.PackError, match='tool_families names jobshop_tool_family:ORDERS'):
        pack.load(absent_root).import_tool_families()
    monkeypatch.delitem(sys.modules, 'jobshop_tool_family', raising=False)
    assert pack.load(JOBSHOP).import_tool_families() == []


# The published artifact.

def test_the_artifact_selection_carries_every_file_the_template_reads(monkeypatch):
    spec = importlib.util.spec_from_file_location('build_preview_for_pack_test', HERE / 'deploy' / 'build_preview.py')
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    repo = HERE.parent

    def listing(command, cwd):
        # Stand in for `git ls-files`: every file of the directory counts as tracked.
        if command[-1] != str(HERE.relative_to(repo)):
            return b''
        names = [path.relative_to(repo).as_posix() for path in HERE.rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts]
        return '\0'.join(names).encode()

    monkeypatch.setattr(build.subprocess, 'check_output', listing)
    selected, _ = build.artifact_files(repo, HERE)
    retail = pack.load('retail')
    assert HERE / 'sdk' / 'pack.py' in selected and HERE / 'sdk' / 'layers.py' in selected
    assert retail.root / 'vertical.json' in selected
    for key in retail.files:
        assert (retail.root / key).resolve() in selected, key
    for key in PINS:
        assert retail.root / key in selected, key
    # A release holds the files the loader reads: the manifest, the seed lists and prompts/. The
    # documents of a template stay in the checkout, where their links resolve.
    chosen = {path.relative_to(HERE).as_posix() for path in selected if path.is_relative_to(HERE / 'verticals')}
    assert {'verticals/retail/vertical.json'} | {f'verticals/retail/{key}' for key in retail.files} <= chosen
    assert all(name.startswith('verticals/retail/') for name in chosen)
    assert 'verticals/retail/README.md' not in chosen
    assert (HERE / 'sdk' / 'README.md').is_file() and (retail.root / 'README.md').is_file()
    # Every prompt the loader accepts sits under prompts/, which is the folder the selection carries.
    assert all(key.startswith('prompts/') for key in retail.files if key.endswith('.md'))
    # The test template stays out of the published service.
    assert all('tests' not in path.relative_to(HERE).parts for path in selected if path.is_relative_to(HERE))
    assert build.artifact_destination(repo, repo / 'out', retail.root / 'vertical.json') == (
        repo / 'out' / 'app/src/prototypes/video/verticals/retail/vertical.json')


def test_a_tracked_loader_has_every_file_of_the_default_template_tracked():
    # The builder packages tracked files, and its own guard reads top-level modules. This holds
    # the rest: once pack.py is in the repository, every file the default template reads is too.
    repo = HERE.parent
    try:
        listed = subprocess.run(['git', 'ls-files', '-z', '--', str(HERE.relative_to(repo))], cwd=repo,
                                capture_output=True, timeout=60)
    except OSError:
        pytest.skip('git is absent here')
    if listed.returncode:
        pytest.skip('this copy of the tree is outside a git checkout')
    tracked = {repo / name for name in listed.stdout.decode().split('\0') if name}
    retail = pack.load(pack.DEFAULT)
    needed = [retail.root / pack.MANIFEST] + [(retail.root / key).resolve() for key in sorted(retail.files)]
    assert untracked_template_files(tracked, needed) == []


def untracked_template_files(tracked, needed):
    """The template files a commit that carries pack.py leaves out, as paths under the harness directory."""
    if HERE / 'sdk' / 'pack.py' not in tracked:
        return []
    return [path.relative_to(HERE).as_posix() for path in needed if path not in tracked]


def test_a_commit_that_carries_the_loader_and_leaves_a_template_file_out_is_named():
    retail = pack.load(pack.DEFAULT)
    needed = [retail.root / pack.MANIFEST] + [(retail.root / key).resolve() for key in sorted(retail.files)]
    assert untracked_template_files({HERE / 'sdk' / 'pack.py', *needed}, needed) == []
    assert untracked_template_files({HERE / 'sdk' / 'pack.py', *needed[:-1]}, needed) == [needed[-1].relative_to(HERE).as_posix()]
    assert untracked_template_files({HERE / 'sdk' / 'pack.py'}, needed)[0] == 'verticals/retail/vertical.json'
    # Before the loader lands, the vertical lands with it in one commit.
    assert untracked_template_files(set(), needed) == []


def test_primary_vertical_selection_and_legacy_conflict_are_validated_before_load(monkeypatch):
    monkeypatch.setattr(pack, '_ACTIVE', None)
    monkeypatch.setattr(pack.layout, '_INSTALLED_VERTICAL', None)
    monkeypatch.delenv('ROCOTO_PACK', raising=False)
    monkeypatch.setenv('ROCOTO_VERTICAL', 'retail')
    assert pack.active().root == pack.PACKS / 'retail'
    monkeypatch.setattr(pack, '_ACTIVE', None)
    monkeypatch.setenv('ROCOTO_PACK', 'jobshop')
    with pytest.raises(pack.PackError, match='different verticals'):
        pack.active()
