"""The harness's tracker tools: definitions, outputs, schedule and runs, read and written as tools first.

The reads are the same code on every surface. The writes are tools of the hosted parent
conversation. No provider and no model is called here.
"""
import asyncio
from copy import copy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_tracker_runner import KEYS, NOON, build, open_company, settle

READS = {'list_trackers', 'get_tracker', 'list_tracker_runs'}
WRITES = {'save_tracker', 'remove_tracker', 'set_tracker_schedule', 'run_trackers', 'install_core_trackers'}


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp('tools-template')
    location, room = build(root)
    return root, location, room


class View:
    """The scoped index view a turn or an MCP read arrives with."""
    def __init__(self, scopes, current=True):
        self.business_scopes, self.business_scope_current = scopes, lambda: current
    def get_clip(self, key):
        return None


@pytest.fixture
def shop(template, tmp_path):
    """One test's copy of the company, with a hosted agent that has its stores and no model."""
    from business_scope import ScopeActor
    from monitoring import monitoring_profile_authorized
    from organization_chats import OrganizationChats
    import tracker_runner as runner
    source, location, room = template
    root = tmp_path / 'state'
    shutil.copytree(source, root)
    state = root / 'environments' / 'shop'
    settle(root)
    company = open_company(root)
    host = {'state': state, 'business_scope': company.scope, 'location_intelligence': company.locations,
            'monitoring': company.monitoring, 'environment_id': 'shop', 'index': View([])}
    store = copy(company.monitoring)
    store.profile_authorized = lambda owner, record: monitoring_profile_authorized(host, owner, record)
    agent = SimpleNamespace(children={}, monitoring_store=store, automation_store=None, business_scope_host=host,
        retrieval=View([]), _turn_senders={}, _save_child=lambda record: None)
    agent.organization_chats = OrganizationChats(state, environment_id='shop', member=company.scope.member, records=lambda: agent.children)
    agent.children['chat'] = {'id': 'chat', 'dataset': 'retail-demo', 'owner_principal_id': 'peter', 'messages': []}
    now = [NOON]
    fixture = SimpleNamespace(root=root, state=state, company=company, location=location, room=room, agent=agent, now=now,
        key=('peter', 'retail-demo', location), runs=runner.TrackerRuns(state, clock=lambda: now[0]),
        sweep=lambda **more: runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={}, **more))
    def view(actor='peter', current=True):
        return View([company.scope.effective(ScopeActor(company.id, actor), 'retail-demo')], current)
    def call(name, args=None, *, chat='chat', run='active', actor='peter', index=None):
        import monitoring_tools
        active = {'cancel': asyncio.Event()} if run == 'active' else run
        return monitoring_tools.call(agent, chat, 'retail-demo', name, args or {}, active, index or view(actor))
    fixture.call, fixture.view = call, view
    return fixture


def test_reads_are_listed_on_every_surface_and_writes_only_in_the_hosted_parent_chat():
    import monitoring_tools
    import native_mcp
    import public_mcp
    from agent_bridge import tool_specs
    parent, child = {s['name']: s for s in tool_specs(True)}, {s['name']: s for s in tool_specs(False)}
    assert READS <= monitoring_tools.READ_NAMES and WRITES <= monitoring_tools.WRITE_NAMES
    assert READS | WRITES <= set(parent) and READS <= set(child) and not WRITES & set(child)
    assert READS <= public_mcp.READ_TOOLS and READS <= native_mcp.READ_TOOLS
    assert not WRITES & public_mcp.READ_TOOLS and not WRITES & native_mcp.READ_TOOLS
    for name in READS | WRITES:
        schema = parent[name]['inputSchema']
        assert schema['additionalProperties'] is False and not {'owner', 'owner_principal_id', 'principal_id', 'dataset'} & set(schema['properties'])
    # The present observation tools keep their names; the families join after them, each with its own reads.
    listed = [s['name'] for s in monitoring_tools.specs(False)]
    assert listed[:6] == ['list_monitoring_profiles', 'list_tracker_events', 'get_tracker_event', 'list_trackers', 'get_tracker', 'list_tracker_runs']
    joined = [name for family in monitoring_tools.FAMILIES[1:] for name in sorted(family.READ_NAMES)]
    assert sorted(listed[6:]) == sorted(joined) and 'get_tracker_script' in joined
    assert monitoring_tools.NAMES == monitoring_tools.READ_NAMES | monitoring_tools.WRITE_NAMES
    assert monitoring_tools.ASYNC_NAMES <= monitoring_tools.WRITE_NAMES


def test_a_family_joins_with_one_line(monkeypatch):
    """The extension point other tool families use: specs, name sets, read and call."""
    import monitoring_tools
    seen = []
    family = SimpleNamespace(READ_NAMES=frozenset({'get_tracker_script'}), WRITE_NAMES=frozenset({'save_tracker_script'}),
        specs=lambda write: [monitoring_tools.tool('get_tracker_script', 'Read a script.', {})] + ([monitoring_tools.tool('save_tracker_script', 'Save a script.', {})] if write else []),
        read=lambda context, name, args: seen.append(('read', context.owner, context.dataset, name)) or {'ok': True},
        call=lambda context, name, args: seen.append(('call', context.owner, context.chat_id, name)) or {'saved': True})
    monkeypatch.setattr(monitoring_tools, 'FAMILIES', (monitoring_tools.TRACKERS, family))
    assert [s['name'] for s in monitoring_tools.specs(True)][-2:] == ['get_tracker_script', 'save_tracker_script']
    assert 'save_tracker_script' not in [s['name'] for s in monitoring_tools.specs(False)]
    store = SimpleNamespace(state='.', profile_authorized=lambda owner, record: True)
    assert monitoring_tools.read(store, 'peter', 'shop', 'get_tracker_script', {}) == {'ok': True}
    with pytest.raises(ValueError):
        monitoring_tools.read(store, 'peter', 'shop', 'save_tracker_script', {})
    agent = SimpleNamespace(children={'chat': {'id': 'chat', 'dataset': 'shop', 'owner_principal_id': 'peter'}},
        monitoring_store=store, retrieval=View([]))
    assert monitoring_tools.call(agent, 'chat', 'shop', 'save_tracker_script', {}, {'cancel': asyncio.Event()}) == {'saved': True}
    assert seen == [('read', 'peter', 'shop', 'get_tracker_script'), ('call', 'peter', 'chat', 'save_tracker_script')]


def test_list_trackers_and_get_tracker_return_outputs_and_the_last_run(shop):
    import monitoring_tools
    run = shop.sweep()[0]
    listing = shop.call('list_trackers')['trackers']
    assert [row['key'] for row in listing] == KEYS
    profiles = shop.call('list_monitoring_profiles')['trackers']
    added = {'revision', 'schedule', 'output', 'last_run'}
    assert [{k: v for k, v in row.items() if k not in added} for row in listing] == profiles, 'the same rows the profile list carries'
    for row in listing:
        assert row['core'] and row['locked'] and row['location_id'] == shop.location
        assert row['schedule'] == {'automatic': True, 'min_interval_seconds': 60, 'budget_seconds': 60, 'daily_at': None, 'revision': 1,
                                   'requested': False, 'running': False, 'needs_attention': False, 'service': 'on'}
        assert set(row['output']) == {'revision', 'state', 'needs', 'value', 'unit', 'period', 'summary'} and row['output']['revision'] >= 1
        assert row['last_run'] == {'id': run['id'], 'trigger': 'events', 'status': 'completed', 'started_at': run['started_at'],
                                   'finished_at': run['finished_at'], 'error': None}
    assert shop.call('list_trackers', {'location_id': shop.location})['trackers'] == listing
    visitors = listing[0]
    assert visitors['output']['state'] == 'ready' and visitors['output']['unit'] == 'visitors' and visitors['output']['summary']
    full = shop.call('get_tracker', {'tracker_id': visitors['id'], 'rows': 3})
    assert full['key'] == 'visitors' and full['output']['revision'] == visitors['output']['revision']
    assert full['output']['tile']['value'] == visitors['output']['value'] and full['output']['canvas']['family'] == 'metric_history'
    assert len(full['output']['canvas']['rows']) == 3 < full['output']['canvas']['total_rows'] and len(full['output']['tile']['events']) == 3
    assert full['script'] == {'script': 'core_trackers.visitors', 'reads': ['tracked_visits', 'events'], 'summary': 'script'}
    assert full['view'] == {'tile': {'family': 'metric', 'unit': 'visitors'}, 'canvas': {'family': 'metric_history'}}
    assert [r['trigger'] for r in full['runs']] == ['events'] and [o['key'] for o in full['runs'][0]['outputs']] == ['visitors']
    runs = shop.call('list_tracker_runs', {'location_id': shop.location})
    assert [r['trigger'] for r in runs['runs']] == ['events', 'host'] and runs['schedule']['revision'] == 1
    assert [o['key'] for o in runs['runs'][0]['outputs']] == KEYS and runs['runs'][0]['page_published'] is True
    assert len(shop.call('list_tracker_runs', {'location_id': shop.location, 'limit': 1})['runs']) == 1
    only = shop.call('list_tracker_runs', {'location_id': shop.location, 'tracker_key': 'sales'})['runs']
    assert [[o['key'] for o in r['outputs']] for r in only] == [['sales']]
    # Results carry what a customer reads: no principal, no stamp, no file path.
    everything = json.dumps([listing, full, runs])
    assert 'peter' not in everything and str(shop.root) not in everything and 'stamp' not in everything and 'lease' not in everything
    with pytest.raises(ValueError, match='Choose a tracker from list_trackers'):
        shop.call('get_tracker', {'tracker_id': 'tracker-unknown'})
    # A child conversation reads too; the same function answers the other surfaces from a scoped store.
    shop.agent.children['child'] = {'id': 'child', 'parent_id': 'chat', 'dataset': 'retail-demo', 'owner_principal_id': 'peter'}
    assert shop.call('list_trackers', chat='child', run=None)['trackers'] == listing
    scoped = monitoring_tools.scoped(shop.company.monitoring, shop.view())
    assert monitoring_tools.read(scoped, 'peter', 'retail-demo', 'list_trackers', {})['trackers'] == listing


def test_writes_go_through_the_tracker_functions_and_the_run_ledger(shop):
    import tracker_runner as runner
    shop.sweep()
    before = runner.input_stamp(shop.state, *shop.key)
    saved = shop.call('save_tracker', {'location_id': shop.location, 'key': 'spills', 'label': 'Spills', 'observes': 'Liquid on the floor of the aisle.',
                                       'event_types': ['spill']})
    assert saved == {'id': saved['id'], 'key': 'spills', 'label': 'Spills', 'core': False, 'chat': True, 'locked': False,
        'location_id': shop.location, 'revision': 1, 'observes': 'Liquid on the floor of the aisle.', 'event_types': ['spill']}
    assert runner.input_stamp(shop.state, *shop.key) != before, 'the service sees the new tracker at its next probe'
    listing = shop.call('list_trackers')['trackers']
    own = listing[-1]
    assert [row['key'] for row in listing] == KEYS + ['spills'] and (own['core'], own['editable'], own['revision'], own['output']) == (False, True, 1, None)
    with pytest.raises(ValueError, match='current revision'):
        shop.call('save_tracker', {'location_id': shop.location, 'key': 'spills', 'label': 'Wet floor', 'observes': 'Water on the floor.'})
    with pytest.raises(ValueError, match='current revision'):
        shop.call('save_tracker', {'location_id': shop.location, 'key': 'spills', 'label': 'Wet floor', 'observes': 'Water on the floor.', 'expected_revision': 7})
    changed = shop.call('save_tracker', {'location_id': shop.location, 'key': 'spills', 'label': 'Wet floor', 'observes': 'Water on the floor.', 'expected_revision': 1})
    assert (changed['label'], changed['revision'], changed['event_types']) == ('Wet floor', 2, ['spill'])
    # Its own schedule: whether it joins the automatic passes.
    paused = shop.call('set_tracker_schedule', {'location_id': shop.location, 'key': 'spills', 'automatic': False, 'expected_revision': 1})
    assert paused['schedule']['automatic'] is False and paused['schedule']['revision'] == 2
    rows = {row['key']: row for row in shop.call('list_trackers')['trackers']}
    assert rows['spills']['schedule']['automatic'] is False and rows['visitors']['schedule']['automatic'] is True
    assert shop.runs.policy(*shop.key)['paused'] == ['spills']
    with pytest.raises(ValueError, match='current revision'):
        shop.call('set_tracker_schedule', {'location_id': shop.location, 'key': 'spills', 'automatic': True, 'expected_revision': 1})
    with pytest.raises(ValueError, match='belong to the location'):
        shop.call('set_tracker_schedule', {'location_id': shop.location, 'key': 'spills', 'min_interval_seconds': 120, 'expected_revision': 2})
    # A request is a row in the run ledger; the service's own tick runs it.
    asked = shop.call('run_trackers', {'location_id': shop.location, 'only': ['visitors']})
    assert asked['requested'] is True and asked['only'] == ['visitors'] and asked['schedule']['requested'] is True
    assert shop.runs.state(*shop.key)['requested_at'] is not None
    shop.now[0] += 60
    assert [(run['trigger'], run['only']) for run in shop.sweep()] == [('manual', ['visitors'])]
    assert shop.call('list_tracker_runs', {'location_id': shop.location, 'limit': 1})['runs'][0]['trigger'] == 'manual'
    with pytest.raises(ValueError, match='Choose trackers of this location'):
        shop.call('run_trackers', {'location_id': shop.location, 'only': ['elsewhere']})
    removed = shop.call('remove_tracker', {'location_id': shop.location, 'key': 'spills'})
    assert removed['removed'] is True and [row['key'] for row in shop.call('list_trackers')['trackers']] == KEYS
    with pytest.raises(ValueError, match='Choose a tracker of this location'):
        shop.call('remove_tracker', {'location_id': shop.location, 'key': 'spills'})
    # The hosted chat hands over what the model wrote: each tool checks it against its own schema.
    before = shop.call('list_trackers')['trackers']
    for name, args in (('save_tracker', {'location_id': shop.location, 'key': 'Bad Key', 'label': 'x', 'observes': 'y'}),
                       ('save_tracker', {'location_id': shop.location, 'key': 'leaks', 'label': ['a list'], 'observes': 'y'}),
                       ('save_tracker', {'location_id': shop.location, 'key': 'leaks', 'label': 'Leaks', 'observes': 'y', 'owner': 'maria'}),
                       ('run_trackers', {'location_id': shop.location, 'only': [{'key': 'visitors'}]}),
                       ('set_tracker_schedule', {'location_id': shop.location, 'automatic': 'yes', 'expected_revision': 2}),
                       ('remove_tracker', {'key': 'visitors'}), ('list_tracker_runs', {'location_id': shop.location, 'limit': 5000}),
                       ('get_tracker', {'tracker_id': 7}), ('list_trackers', {'location_id': None})):
        with pytest.raises(ValueError, match='Use the declared fields of this tool'):
            shop.call(name, args)
    with pytest.raises(ValueError, match='short key'):
        shop.call('save_tracker', {'location_id': shop.location, 'key': 'leaks', 'label': '   ', 'observes': 'y'})
    assert shop.call('list_trackers')['trackers'] == before


def test_a_core_key_answers_locked(shop):
    from core_trackers import LOCKED
    before = shop.call('list_trackers')['trackers']
    for name, args in (('save_tracker', {'key': 'visitors', 'label': 'Mine', 'observes': 'Everyone.', 'expected_revision': 1}),
                       ('save_tracker', {'key': 'basket', 'label': 'Mine', 'observes': 'Every basket.'}),
                       ('remove_tracker', {'key': 'sales'}),
                       ('set_tracker_schedule', {'key': 'queue_time', 'automatic': False, 'expected_revision': 1})):
        with pytest.raises(ValueError) as refusal:
            shop.call(name, {'location_id': shop.location, **args})
        assert str(refusal.value) == LOCKED
    assert shop.call('list_trackers')['trackers'] == before and shop.runs.policy(*shop.key)['revision'] == 1


def test_the_location_schedule_and_the_install_belong_to_the_organization_conversation(shop):
    import monitoring_tools
    from access import AccessStore
    from business_scope import ScopeActor
    import tracker_runner as runner
    schedule = {'location_id': shop.location, 'automatic': False, 'min_interval_seconds': 300, 'daily_at': '06:30', 'expected_revision': 1}
    install = {'location_id': shop.location, 'business_type': 'retail'}
    for name, args in (('set_tracker_schedule', schedule), ('install_core_trackers', install)):
        with pytest.raises(ValueError) as refusal:
            shop.call(name, args)
        assert str(refusal.value) == monitoring_tools.ORGANIZATION_ONLY
    organization = shop.agent.organization_chats.ensure(shop.agent, 'retail-demo', 'organization')
    cameras = shop.agent.organization_chats.ensure(shop.agent, 'retail-demo', 'cameras')
    assert organization['owner_principal_id'].startswith('organization-')
    # The Organization conversation belongs to the company; the member whose turn it is acts.
    with pytest.raises(ValueError, match='current company member'):
        shop.call('list_trackers', chat=organization['id'])
    shop.agent._turn_senders[organization['id']] = {'principal_id': 'peter'}
    shop.agent._turn_senders[cameras['id']] = {'principal_id': 'peter'}
    assert [row['key'] for row in shop.call('list_trackers', chat=organization['id'])['trackers']] == KEYS
    with pytest.raises(ValueError) as refusal:
        shop.call('install_core_trackers', install, chat=cameras['id'])
    assert str(refusal.value) == monitoring_tools.ORGANIZATION_ONLY
    saved = shop.call('set_tracker_schedule', schedule, chat=organization['id'])['schedule']
    assert (saved['automatic'], saved['min_interval_seconds'], saved['daily_at'], saved['revision']) == (False, 300, '06:30', 2)
    assert shop.runs.policy(*shop.key) == {'automatic': False, 'min_interval_seconds': 300, 'budget_seconds': 60, 'daily_at': '06:30', 'paused': [], 'revision': 2}
    cleared = shop.call('set_tracker_schedule', {'location_id': shop.location, 'automatic': True, 'daily_at': '', 'expected_revision': 2}, chat=organization['id'])
    assert cleared['schedule']['daily_at'] is None and cleared['schedule']['automatic'] is True
    with pytest.raises(ValueError, match='Name the schedule fields'):
        shop.call('set_tracker_schedule', {'location_id': shop.location, 'expected_revision': 3}, chat=organization['id'])
    installed = shop.call('install_core_trackers', install, chat=organization['id'])
    assert [t['key'] for t in installed['trackers']] == KEYS and installed['recipe_revision'] == 'retail-core-v1'
    assert runner.installed_locations(shop.state) == [shop.key]
    with pytest.raises(ValueError, match='Use the declared fields of this tool'):  # the recipes this build maintains
        shop.call('install_core_trackers', {'location_id': shop.location, 'business_type': 'bakery'}, chat=organization['id'])
    # A second owner of the company: the records and the unattended pass stay with the member who installed.
    AccessStore(shop.root).issue('shop', owner=True, principal_id='maria')
    shop.agent._turn_senders[organization['id']] = {'principal_id': 'maria'}
    maria = shop.view('maria')
    assert shop.company.scope.effective(ScopeActor(shop.company.id, 'maria'), 'retail-demo')['location_ids'] == [shop.location]
    assert shop.call('list_trackers', chat=organization['id'], index=maria)['trackers'] == []
    with pytest.raises(ValueError) as refusal:
        shop.call('install_core_trackers', install, chat=organization['id'], index=maria)
    assert str(refusal.value) == monitoring_tools.INSTALLED_BY_MEMBER
    assert runner.installed_locations(shop.state) == [shop.key]


def test_writes_need_management_access_and_installed_trackers(shop):
    import monitoring_tools
    from business_scope import ScopeActor
    actor = ScopeActor(shop.company.id, 'peter')
    view = shop.company.scope.describe(actor, 'retail-demo')
    view = shop.company.scope.mutate(actor, 'retail-demo', 'locations', {'name': 'Second shop', 'address': ''}, view['revision'])
    second = view['locations'][-1]['id']
    for name, args in (('save_tracker', {'key': 'spills', 'label': 'Spills', 'observes': 'Liquid on the floor.'}),
                       ('run_trackers', {}), ('set_tracker_schedule', {'key': 'spills', 'automatic': False, 'expected_revision': 1})):
        with pytest.raises(ValueError) as refusal:
            shop.call(name, {'location_id': second, **args})
        assert str(refusal.value) == monitoring_tools.INSTALL_FIRST
    with pytest.raises(ValueError, match='Choose a location in your current access'):
        shop.call('save_tracker', {'location_id': 'location-of-another-company', 'key': 'spills', 'label': 'Spills', 'observes': 'Liquid.'})
    # A member whose management access ended changes nothing; one whose access ended reads and asks for nothing.
    changes = (('save_tracker', {'key': 'spills', 'label': 'Spills', 'observes': 'Liquid on the floor.'}), ('remove_tracker', {'key': 'spills'}),
               ('set_tracker_schedule', {'key': 'spills', 'automatic': False, 'expected_revision': 1}))
    real = shop.company.locations.authorized
    def reader(owner, collection, location_id, *, write=False):
        if write:
            raise ValueError('Use location management access to update its catalog.')
        return real(owner, collection, location_id)
    shop.company.locations.authorized = reader
    for name, args in changes:
        with pytest.raises(ValueError) as refusal:
            shop.call(name, {'location_id': shop.location, **args})
        assert str(refusal.value) == monitoring_tools.MANAGEMENT
    assert shop.call('run_trackers', {'location_id': shop.location})['requested'] is True
    shop.company.locations.authorized = real
    with sqlite3.connect(shop.root / 'access.sqlite') as db:
        db.execute("DELETE FROM principal_environments WHERE principal_id='peter' AND environment='shop'")
    for name, args in changes + (('run_trackers', {}),):
        with pytest.raises(ValueError):
            shop.call(name, {'location_id': shop.location, **args}, index=View([]))
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='tracker'").fetchone()[0] == 9


def test_a_reader_sees_only_locations_in_their_present_access(shop):
    """Over MCP the store is a fresh one with no guard of its own: the scoped view decides, read by read."""
    import monitoring_tools
    from monitoring import MonitoringStore
    shop.sweep()
    visitors = shop.call('list_trackers')['trackers'][0]['id']
    fresh = lambda view: monitoring_tools.scoped(MonitoringStore(shop.state), view)
    read = lambda view, name, args=None, owner='peter': monitoring_tools.read(fresh(view), owner, 'retail-demo', name, args or {})
    scope = shop.view().business_scopes[0]
    assert scope['configured'] and shop.location in scope['location_ids']
    assert len(read(shop.view(), 'list_trackers')['trackers']) == 9
    elsewhere = View([{**scope, 'includes_unassigned': False, 'location_ids': ['location-elsewhere']}])
    closed = View([{**scope, 'can_query': False}])
    changed = shop.view(current=False)
    unscoped = View([])  # a view with no scope for this business: a configured business stays closed
    for view in (elsewhere, closed, changed, unscoped):
        assert read(view, 'list_trackers')['trackers'] == []
        for name, args in (('list_trackers', {'location_id': shop.location}), ('list_tracker_runs', {'location_id': shop.location})):
            with pytest.raises(ValueError, match='Choose a location in your current access'):
                read(view, name, args)
        with pytest.raises(ValueError, match='Choose a tracker from list_trackers'):
            read(view, 'get_tracker', {'tracker_id': visitors})
    # Another principal of the same company reads their own records: none here.
    assert read(shop.view(), 'list_trackers', owner='maria')['trackers'] == []
    assert read(shop.view(), 'list_tracker_runs', {'location_id': shop.location}, owner='maria')['runs'] == []
    # The hosted chat's own guard closes the same reads when the member's scope leaves the location out.
    shop.agent.monitoring_store.profile_authorized = lambda owner, record: record['location_id'] != shop.location
    assert shop.call('list_trackers')['trackers'] == []


def test_a_child_and_a_participants_turn_are_refused(tmp_path):
    """The dispatcher closes every tracker tool to a participant's turn; a child conversation reads and changes nothing."""
    from agent_bridge import CREATOR_TOOLS, CodexVideoAgent
    from monitoring import MonitoringStore
    class Index:
        clips = []
        def status(self): return {'ready': False, 'datasets': ['shop']}
        def get_clip(self, key): return None
    async def scenario():
        agent = CodexVideoAgent(tmp_path, Index(), lambda: [])
        agent.monitoring_store = MonitoringStore(tmp_path)
        agent.children['chat'] = {'id': 'chat', 'dataset': 'shop', 'owner_principal_id': 'alice', 'messages': []}
        agent.children['child'] = {'id': 'child', 'parent_id': 'chat', 'dataset': 'shop', 'owner_principal_id': 'alice'}
        agent.active['chat'] = {'cancel': asyncio.Event()}
        agent.active['child'] = {'cancel': asyncio.Event()}
        arguments = {'save_tracker': {'location_id': 'l', 'key': 'spills', 'label': 'Spills', 'observes': 'Liquid.'},
            'remove_tracker': {'location_id': 'l', 'key': 'spills'}, 'run_trackers': {'location_id': 'l'},
            'set_tracker_schedule': {'location_id': 'l', 'automatic': False, 'expected_revision': 1},
            'install_core_trackers': {'location_id': 'l', 'business_type': 'retail'}}
        async def answer(name, chat, args=None):
            blocks, _, ok = await agent._tool(name, args if args is not None else arguments.get(name, {}), 'shop', chat)
            return ok, blocks[0]['text']
        for name in sorted(WRITES):
            assert await answer(name, 'child') == (False, 'Save tracker changes from the active parent conversation.'), name
        ok, text = await answer('list_trackers', 'child')
        assert ok and json.loads(text) == {'trackers': []}
        # A stopped run and a turn with no run change nothing either.
        agent.active['chat']['cancel'].set()
        assert (await answer('save_tracker', 'chat'))[1] == 'Save tracker changes from the active parent conversation.'
        del agent.active['chat']
        assert (await answer('run_trackers', 'chat'))[1] == 'Save tracker changes from the active parent conversation.'
        # A participant's own turn: every tool that resolves as the creator stays closed, reads included.
        agent.active['chat'] = {'cancel': asyncio.Event()}
        agent._turn_guests = {'chat': 'bob'}
        for name in sorted(READS | WRITES):
            assert await answer(name, 'chat') == (False, CREATOR_TOOLS), name
        # Another business's conversation is outside this one.
        agent._turn_guests = {}
        assert not (await agent._tool('list_trackers', {}, 'other-business', 'chat'))[2]
    asyncio.run(scenario())


# ── What two reviewers broke, kept as it was fixed ─────────────────────────────────────────────

def narrow_to_a_room_with_no_camera(shop):
    """Another owner narrows the installing member to one function of the location: a room with no camera."""
    from access import AccessStore
    from business_scope import ScopeActor
    peter = ScopeActor(shop.company.id, 'peter')
    view = shop.company.scope.describe(peter, 'retail-demo')
    view = shop.company.scope.mutate(peter, 'retail-demo', 'functions', {'name': 'Back office'}, view['revision'])
    office = view['functions'][-1]['id']
    shop.company.scope.mutate(peter, 'retail-demo', 'rooms', {'name': 'Office', 'location_id': shop.location, 'function_ids': [office]}, view['revision'])
    AccessStore(shop.root).issue('shop', owner=True, principal_id='maria')
    with sqlite3.connect(shop.root / 'access.sqlite') as db:
        db.execute("UPDATE principal_environments SET role='operator' WHERE principal_id='peter' AND environment='shop'")
    maria = ScopeActor(shop.company.id, 'maria')
    shop.company.scope.mutate(maria, 'retail-demo', 'members', {'member_id': 'peter', 'role': 'function_admin',
        'location_ids': [shop.location], 'function_ids': [office]}, shop.company.scope.describe(maria, 'retail-demo')['revision'])
    effective = shop.company.scope.effective(peter, 'retail-demo')
    assert (effective['location_ids'], effective['camera_ids'], effective['source_ids'], effective['can_manage']) == ([shop.location], [], [], True)


def test_a_member_narrowed_to_one_function_reads_and_runs_what_their_cameras_cover(shop):
    """A tracker's output is the record of the cameras it read. A member reads it, and the pass runs
    as them, while their access covers those cameras."""
    import monitoring_tools
    from monitoring import MonitoringStore
    import tracker_runner as runner
    shop.sweep()
    row = next(row for row in shop.call('list_trackers')['trackers'] if row['key'] == 'product_interactions')
    whole = shop.call('get_tracker', {'tracker_id': row['id'], 'rows': 500})['output']
    assert row['output']['value'] and whole['tile']['events'] and whole['canvas']['rows']
    # A row of a recording outside the reader's sources stays out, whatever the surface.
    hidden = whole['tile']['events'][0]['source_id']
    scoped = monitoring_tools.scoped(MonitoringStore(shop.state), shop.view())
    lookup = scoped.source_lookup
    scoped.source_lookup = lambda owner, key: None if key == hidden else lookup(owner, key)
    partial = monitoring_tools.read(scoped, 'peter', 'retail-demo', 'get_tracker', {'tracker_id': row['id'], 'rows': 500})['output']
    for part, whole_rows in ((partial['tile']['events'], whole['tile']['events']), (partial['canvas']['rows'], whole['canvas']['rows'])):
        assert part == [entry for entry in whole_rows if entry.get('source_id') != hidden] and len(part) < len(whole_rows)
    good = {output['tracker_key']: output['revision'] for output in runner.current_outputs(shop.state, *shop.key)}
    assert shop.call('list_tracker_runs', {'location_id': shop.location})['runs'][0]['derived']['events'] > 0
    narrow_to_a_room_with_no_camera(shop)
    assert shop.call('list_tracker_events', {'limit': 500})['events'] == []
    for read in (lambda name, args=None: shop.call(name, args),
                 lambda name, args=None: monitoring_tools.read(monitoring_tools.scoped(MonitoringStore(shop.state), shop.view()), 'peter', 'retail-demo', name, args or {})):
        listing = read('list_trackers')['trackers']
        assert [entry['key'] for entry in listing] == KEYS, 'the location’s trackers are still listed'
        assert all(entry['output'] is None and entry['output_access'] == monitoring_tools.CAMERA_SCOPE for entry in listing)
        full = read('get_tracker', {'tracker_id': row['id'], 'rows': 500})
        assert full['output'] is None and full['output_access'] == monitoring_tools.CAMERA_SCOPE
        runs = read('list_tracker_runs', {'location_id': shop.location})['runs']
        assert runs and all(run['derived'] is None for run in runs + full['runs']), 'a pass’s counts add up the same cameras'
        assert hidden not in json.dumps([listing, full, runs])
    # The unattended pass acts as the installing member: it stops where that member's cameras end.
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        db.execute("UPDATE records SET body=json_set(body,'$.label','Shelf moved') WHERE kind='zone' AND rowid=(SELECT MIN(rowid) FROM records WHERE kind='zone')")
    shop.now[0] += 600
    run = shop.sweep()[0]
    assert (run['status'], run['error'], run['outputs']) == ('failed', runner.CAMERA_ACCESS, [])
    assert {output['tracker_key']: output['revision'] for output in runner.current_outputs(shop.state, *shop.key)} == good


def test_a_current_manager_takes_over_when_the_installing_member_left(shop):
    import monitoring_tools
    from access import AccessStore
    import tracker_runner as runner
    shop.sweep()
    organization = shop.agent.organization_chats.ensure(shop.agent, 'retail-demo', 'organization')
    AccessStore(shop.root).issue('shop', owner=True, principal_id='maria')
    install = {'location_id': shop.location, 'business_type': 'retail'}
    shop.agent._turn_senders[organization['id']] = {'principal_id': 'maria'}
    ask = lambda name, args=None: shop.call(name, args, chat=organization['id'], index=shop.view('maria'))
    # While the installing member manages the location, the installation stays theirs.
    with pytest.raises(ValueError) as refusal:
        ask('install_core_trackers', install)
    assert str(refusal.value) == monitoring_tools.INSTALLED_BY_MEMBER
    assert 'installed_by_another_member' not in ask('list_tracker_runs', {'location_id': shop.location})
    # The installing member leaves the company; the unattended pass fails closed.
    with sqlite3.connect(shop.root / 'access.sqlite') as db:
        db.execute("DELETE FROM principal_environments WHERE principal_id='peter' AND environment='shop'")
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        db.execute("UPDATE records SET body=json_set(body,'$.label','Shelf moved') WHERE kind='zone' AND rowid=(SELECT MIN(rowid) FROM records WHERE kind='zone')")
    for _ in range(runner.ATTENTION_AFTER):
        shop.now[0] += 4000
        assert [(run['status'], run['error']) for run in shop.sweep()] == [('failed', runner.OWNER_ACCESS)]
    # A member who manages the location reads why its trackers stopped.
    runs = ask('list_tracker_runs', {'location_id': shop.location})
    other = runs['installed_by_another_member']
    assert runs['runs'] == [] and other['schedule']['needs_attention'] is True
    assert other['last_run']['status'] == 'failed' and other['last_run']['error'] == runner.OWNER_ACCESS and other['next_step'] == monitoring_tools.TAKE_OVER
    assert 'peter' not in json.dumps(runs)
    # From the Organization conversation that member installs them again: they run as the member who asked.
    installed = ask('install_core_trackers', install)
    assert [t['key'] for t in installed['trackers']] == KEYS and installed['took_over'] == monitoring_tools.TOOK_OVER
    assert ('maria', 'retail-demo', shop.location) in runner.installed_locations(shop.state)
    assert [row['key'] for row in ask('list_trackers')['trackers']] == KEYS
    assert ask('run_trackers', {'location_id': shop.location})['requested'] is True
    shop.now[0] += 600
    assert [(run['trigger'], run['status']) for run in shop.sweep()] == [('manual', 'completed')]
    assert 'installed_by_another_member' not in ask('list_tracker_runs', {'location_id': shop.location}), 'the location has its trackers again'


def test_run_trackers_names_the_state_of_the_service(shop):
    """With the service switched off at the host, a request waits, and the tool says so."""
    import monitoring_tools
    import tracker_runner as runner
    runner.runner_state(shop.root, shop.root, {'ROCOTO_VIDEO_TRACKER_RUNNER': 'off'})
    asked = shop.call('run_trackers', {'location_id': shop.location})
    assert asked['requested'] is True and asked['schedule']['service'] == 'off' and asked['starts'] == monitoring_tools.SERVICE_WAITS
    assert shop.call('list_trackers')['trackers'][0]['schedule']['service'] == 'off'
    assert shop.call('list_tracker_runs', {'location_id': shop.location})['schedule']['service'] == 'off'
    # The service runs: its sweep says so, and the request it found is its first pass.
    assert [run['trigger'] for run in shop.sweep()] == ['manual']
    again = shop.call('run_trackers', {'location_id': shop.location})
    assert again['schedule']['service'] == 'on' and again['starts'] == monitoring_tools.SERVICE_RUNS
    description = next(spec['description'] for spec in monitoring_tools.specs(True) if spec['name'] == 'run_trackers')
    assert 'service' in description and 'starts' in description


def test_a_family_with_a_name_in_two_places_is_refused_as_the_module_loads():
    """A name is one tool: a read or a write, of one family."""
    import monitoring_tools
    from agent_bridge import tool_specs
    for write in (True, False):  # beside every other tool of the harness, each name is listed once
        names = [spec['name'] for spec in tool_specs(write)]
        assert len(names) == len(set(names))
    def family(read, write, listed=None, parent=None):
        return SimpleNamespace(READ_NAMES=frozenset(read), WRITE_NAMES=frozenset(write),
            specs=lambda w: [monitoring_tools.tool(name, name, {}) for name in ((parent if parent is not None else sorted(set(read) | set(write))) if w else (listed if listed is not None else sorted(read)))],
            read=lambda context, name, args: {}, call=lambda context, name, args: {})
    good = family({'get_tracker_script'}, {'save_tracker_script'})
    reads, writes = monitoring_tools.registry((monitoring_tools.TRACKERS, good))
    assert 'get_tracker_script' in reads and 'save_tracker_script' in writes and not reads & writes
    assert monitoring_tools.registry(monitoring_tools.FAMILIES) == (monitoring_tools.READ_NAMES, monitoring_tools.WRITE_NAMES)
    for clash in (family({'publish_tracker_script'}, {'publish_tracker_script'}),          # a read and a write at once
                  family({'list_tracker_events'}, set()),                                  # an observation tool's name
                  family({'list_trackers'}, set()),                                        # another family's name
                  family({'get_tracker_script'}, {'save_tracker_script'}, listed=['get_tracker_script', 'save_tracker_script']),  # a write listed to children
                  family({'get_tracker_script'}, {'save_tracker_script'}, parent=['get_tracker_script'])):                         # a name with no specification
        with pytest.raises(ValueError, match='tool family'):
            monitoring_tools.registry((monitoring_tools.TRACKERS, clash))


def test_a_family_may_wait_on_the_event_loop_for_a_declared_write(monkeypatch):
    """The awaited path of a family write: the same checks as `call`, then the family's `acall`."""
    import monitoring_tools
    seen = []
    async def acall(context, name, args):
        seen.append((context.owner, context.chat_id, name, args, type(context).__name__))
        return {'tested': True}
    family = SimpleNamespace(READ_NAMES=frozenset(), WRITE_NAMES=frozenset({'test_tracker_script'}), ASYNC_NAMES=frozenset({'test_tracker_script'}),
        specs=lambda write: [monitoring_tools.tool('test_tracker_script', 'Test a script.', {'revision': {'type': 'integer'}}, ['revision'])] if write else [],
        read=None, call=None, acall=acall)
    monkeypatch.setattr(monitoring_tools, 'FAMILIES', (monitoring_tools.TRACKERS, family))
    store = SimpleNamespace(state='.', profile_authorized=lambda owner, record: True)
    agent = SimpleNamespace(children={'chat': {'id': 'chat', 'dataset': 'shop', 'owner_principal_id': 'peter'},
                                      'child': {'id': 'child', 'parent_id': 'chat', 'dataset': 'shop', 'owner_principal_id': 'peter'}},
        monitoring_store=store, retrieval=View([]))
    run = {'cancel': asyncio.Event()}
    assert asyncio.run(monitoring_tools.call_async(agent, 'chat', 'shop', 'test_tracker_script', {'revision': 1}, run)) == {'tested': True}
    assert seen == [('peter', 'chat', 'test_tracker_script', {'revision': 1}, 'WriteContext')]
    for chat, args, active, sentence in (('child', {'revision': 1}, run, 'Save tracker changes from the active parent conversation.'),
                                         ('chat', {'revision': 1}, None, 'Save tracker changes from the active parent conversation.'),
                                         ('chat', {'revision': 'one'}, run, 'Use the declared fields of this tool.'),
                                         ('chat', {'revision': 1, 'owner': 'maria'}, run, 'Use the declared fields of this tool.')):
        with pytest.raises(ValueError) as refusal:
            asyncio.run(monitoring_tools.call_async(agent, chat, 'shop', 'test_tracker_script', args, active))
        assert str(refusal.value) == sentence
    with pytest.raises(ValueError):
        asyncio.run(monitoring_tools.call_async(agent, 'chat', 'another-business', 'test_tracker_script', {'revision': 1}, run))
    with pytest.raises(ValueError):
        asyncio.run(monitoring_tools.call_async(agent, 'chat', 'shop', 'save_tracker', {}, run))  # a write that runs in a thread
    assert len(seen) == 1
    # A family declares a waited name among its writes and brings `acall`, or it is refused as the module loads.
    for broken in (SimpleNamespace(**{**vars(family), 'ASYNC_NAMES': frozenset({'get_tracker_script'})}), SimpleNamespace(**{**vars(family), 'acall': None})):
        with pytest.raises(ValueError):
            monitoring_tools.registry((monitoring_tools.TRACKERS, broken))
