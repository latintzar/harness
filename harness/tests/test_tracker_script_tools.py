"""The harness's tools for a tracker's processing script: one read on every surface, six writes in
the hosted parent conversation. The sandbox is a double that runs the real entry file and the real
script in a local process. No provider and no model is called."""
from retail_agent_inputs import RetailInputProvider
import asyncio
from copy import copy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_tracker_runner import KEYS, NOON, build, open_company, settle
from test_tracker_scripts import COUNT, LocalSandbox, NEGATIVE, loop_thread  # noqa: F401  (loop_thread is a fixture)

READ = {'get_tracker_script'}
WRITES = {'save_tracker_script', 'test_tracker_script', 'publish_tracker_script', 'set_tracker_script_policy', 'approve_tracker_output', 'write_tracker_text'}
WITH_TEXT = COUNT.replace('"by_type": [{"type": "spill", "value": len(rows)}]}}',
                          '"by_type": []}, "texts": [{"slot": "memo", "facts": {"count": len(rows)}}] if period["name"] == "day" else []}')


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp('script-tools-template')
    location, room = build(root)
    return root, location, room


class View:
    def __init__(self, scopes, current=True):
        self.business_scopes, self.business_scope_current = scopes, lambda: current
    def get_clip(self, key):
        return None
    def status(self):
        return {'ready': False, 'datasets': ['retail-demo']}


@pytest.fixture
def shop(template, tmp_path, loop_thread, monkeypatch):  # noqa: F811
    """One test's copy of the company, a hosted agent with its stores, and the sandbox double."""
    from agent_test_support import retail_agent_store as AgentStore
    from business_scope import ScopeActor
    from monitoring import monitoring_profile_authorized
    from organization_chats import OrganizationChats
    import monitoring_tools
    import tracker_runner as runner
    import tracker_scripts as scripts
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
    agent = SimpleNamespace(children={}, monitoring_store=store, automation_store=AgentStore(state, input_provider=RetailInputProvider(monitoring=company.monitoring)),
        business_scope_host=host, retrieval=View([]), _turn_senders={}, _save_child=lambda record: None, active={})
    agent.organization_chats = OrganizationChats(state, environment_id='shop', member=company.scope.member, records=lambda: agent.children)
    agent.children['chat'] = {'id': 'chat', 'dataset': 'retail-demo', 'owner_principal_id': 'peter', 'messages': []}
    (tmp_path / 'jobs').mkdir()
    box = LocalSandbox(tmp_path / 'jobs', loop=loop_thread)
    monkeypatch.setattr(scripts, '_BOX', box)
    now = [NOON]
    fixture = SimpleNamespace(root=root, state=state, company=company, location=location, room=room, agent=agent, now=now, box=box, scripts=scripts,
        key=('peter', 'retail-demo', location), runs=runner.TrackerRuns(state, clock=lambda: now[0]),
        sweep=lambda **more: runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={}, custom=scripts.sandbox_processor(box), **more))
    view = lambda actor='peter', current=True: View([company.scope.effective(ScopeActor(company.id, actor), 'retail-demo')], current)
    def call(name, args=None, *, chat='chat', run='active', index=None):
        active = {'cancel': asyncio.Event()} if run == 'active' else run
        if name in monitoring_tools.ASYNC_NAMES:
            return asyncio.run(monitoring_tools.call_async(agent, chat, 'retail-demo', name, args or {}, active, index or view()))
        return monitoring_tools.call(agent, chat, 'retail-demo', name, args or {}, active, index or view())
    def own(key='pickups'):
        return call('save_tracker', {'location_id': location, 'key': key, 'label': 'Pickups', 'observes': 'Items a visitor picks up.', 'event_types': ['pick_up']})
    def manual():
        fixture.runs.request(*fixture.key, by='peter')
        now[0] += 120
        return fixture.sweep()[0]
    fixture.call, fixture.view, fixture.own, fixture.manual = call, view, own, manual
    return fixture


def test_the_read_is_on_every_surface_and_the_writes_in_the_hosted_parent_conversation():
    import monitoring_tools
    import native_mcp
    import public_mcp
    import tracker_scripts as scripts
    from agent_bridge import tool_specs
    parent, child = {s['name']: s for s in tool_specs(True)}, {s['name']: s for s in tool_specs(False)}
    assert scripts.READ_NAMES == READ and scripts.WRITE_NAMES == WRITES and scripts in monitoring_tools.FAMILIES
    assert READ <= monitoring_tools.READ_NAMES and WRITES <= monitoring_tools.WRITE_NAMES and monitoring_tools.ASYNC_NAMES == {'test_tracker_script'}
    assert READ | WRITES <= set(parent) and READ <= set(child) and not WRITES & set(child)
    assert READ <= public_mcp.READ_TOOLS and READ <= native_mcp.READ_TOOLS and not WRITES & (public_mcp.READ_TOOLS | native_mcp.READ_TOOLS)
    for name in READ | WRITES:
        schema = parent[name]['inputSchema']
        assert schema['additionalProperties'] is False and 'tracker_id' in schema['required']
        assert not {'owner', 'owner_principal_id', 'principal_id', 'dataset', 'company_id'} & set(schema['properties'])
    assert [s['name'] for s in scripts.specs(False)] == ['get_tracker_script']


def test_a_script_goes_from_a_draft_to_the_surface_through_the_tools(shop):
    scripts = shop.scripts
    tracker = shop.own()
    first = shop.call('get_tracker_script', {'tracker_id': tracker['id']})
    assert first['script'] is None and first['next_step'] == 'Save the first revision with save_tracker_script and expected_revision 0.'
    assert first['contract'] == scripts.TRACKER_SCRIPT_INSTRUCTIONS and first['example'] == scripts.EXAMPLE_SOURCE and first['event_types'] == ['pick_up']
    assert first['limits']['seconds'] == 30 and first['limits']['periods'] == ['day', 'week', 'month', 'year'] and first['runs'] == []
    # A source the contract refuses comes back as data, with its line; nothing is stored.
    refused = shop.call('save_tracker_script', {'tracker_id': tracker['id'], 'source': 'def run(input):\n    return {\n', 'expected_revision': 0})
    assert refused['status'] == 'rejected' and refused['error'] == {'code': 'script_syntax', 'message': "Syntax error: '{' was never closed.", 'line': 2}
    assert shop.call('get_tracker_script', {'tracker_id': tracker['id']})['script'] is None
    saved = shop.call('save_tracker_script', {'tracker_id': tracker['id'], 'source': COUNT, 'expected_revision': 0})
    assert saved['status'] == 'saved' and saved['script']['revision'] == 1 and saved['script']['published_revision'] is None
    assert saved['script']['policy'] == {'publish': 'automatic', 'wake': 'on_failure'} and saved['script']['health']['status'] == 'draft'
    with pytest.raises(ValueError, match='current revision'):
        shop.call('save_tracker_script', {'tracker_id': tracker['id'], 'source': COUNT + '\n', 'expected_revision': 0})
    with pytest.raises(ValueError) as untested:
        shop.call('publish_tracker_script', {'tracker_id': tracker['id'], 'expected_revision': 1})
    assert str(untested.value) == scripts.TEST_FIRST
    tested = shop.call('test_tracker_script', {'tracker_id': tracker['id'], 'expected_revision': 1})
    assert tested['status'] == 'passed' and tested['tracker_id'] == tracker['id'] and tested['preview']['tile']['value'] > 0
    assert tested['next_step'] == 'Publish this revision with publish_tracker_script.'
    published = shop.call('publish_tracker_script', {'tracker_id': tracker['id'], 'expected_revision': 1})
    assert published['status'] == 'published' and published['script']['published_revision'] == 1
    assert published['runs']['requested'] is True and published['runs']['event_runs'] == 'off' and scripts.EVENT_RUNS_NOTE in published['runs']['note']
    assert 'once each local day of the location' in published['runs']['note']
    assert shop.runs.state(*shop.key)['requested_only'] == ['pickups']
    # The runner's next sweep runs it: the request the publish made.
    shop.now[0] += 120
    run = shop.sweep()[0]
    assert run['trigger'] == 'manual' and run['only'] == ['pickups'] and run['scripts'][0]['output'] == 'published'
    # get_tracker returns the script's revision, policy, health and last test with the tile and canvas.
    whole = shop.call('get_tracker', {'tracker_id': tracker['id']})
    script = whole['script']
    assert script['sandbox'] == {'kind': 'tracker-python-v1', 'script_id': 'script-' + tracker['id'].removeprefix('tracker-'), 'revision': 1}
    assert script['script'] is None and (script['revision'], script['published_revision'], script['policy']) == (1, 1, {'publish': 'automatic', 'wake': 'on_failure'})
    assert script['health']['status'] == 'ready' and script['last_test']['status'] == 'passed' and script['last_test']['run_id'] == tested['run_id']
    assert whole['output']['tile']['script_revision'] == 1 and whole['output']['revision'] == 1
    assert sorted((r['trigger'], r['scripts'][0]['status']) for r in whole['runs']) == [('manual', 'succeeded'), ('test', 'passed')]
    core = shop.call('get_tracker', {'tracker_id': shop.call('list_trackers')['trackers'][0]['id']})['script']
    assert set(core) == {'script', 'reads', 'summary'}, 'a Core Tracker’s script is its recipe’s processor'
    # list_tracker_runs: each script run of a pass and each test, with trigger, status, seconds, error and what was published.
    # (A test row is stamped by the wall clock and this fixture's passes by its own, so the two are read by trigger.)
    listing = sorted(shop.call('list_tracker_runs', {'location_id': shop.location, 'tracker_key': 'pickups'})['runs'], key=lambda r: r['trigger'])
    assert [(r['trigger'], r['status']) for r in listing] == [('manual', 'completed'), ('test', 'completed')]
    assert {k: listing[0]['scripts'][0][k] for k in ('key', 'status', 'error', 'output', 'output_revision')} == {
        'key': 'pickups', 'status': 'succeeded', 'error': None, 'output': 'published', 'output_revision': 1} and listing[0]['scripts'][0]['seconds'] > 0
    assert listing[1]['scripts'][0]['status'] == 'passed' and listing[1]['outputs'] == []
    everything = shop.call('list_tracker_runs', {'location_id': shop.location})['runs']
    assert {'manual', 'test', 'host'} <= {r['trigger'] for r in everything} and all('scripts' in r for r in everything)
    # get_tracker_script: the source, the runs and the output it published.
    read = shop.call('get_tracker_script', {'tracker_id': tracker['id']})
    assert read['script']['source'] == COUNT and read['script']['source_revision'] == 1 and read['output']['revision'] == 1
    assert sorted((r['trigger'], r['status'], r['output']) for r in read['runs']) == [('manual', 'succeeded', 'published'), ('test', 'passed', None)]
    assert read['proposal'] is None and read['next_step'] is None
    # An older revision stays readable after a newer save.
    shop.call('save_tracker_script', {'tracker_id': tracker['id'], 'source': COUNT + '# two\n', 'expected_revision': 1})
    assert shop.call('get_tracker_script', {'tracker_id': tracker['id'], 'revision': 1})['script']['source'] == COUNT
    newest = shop.call('get_tracker_script', {'tracker_id': tracker['id']})['script']
    assert newest['source'].endswith('# two\n') and (newest['revision'], newest['published_revision'], newest['last_test']) == (2, 1, None)
    # Results carry no principal and no file path.
    for result in (first, saved, tested, published, whole, read, listing, everything):
        text = json.dumps({k: v for k, v in result.items() if k not in {'contract', 'example'}} if isinstance(result, dict) else result)
        assert 'peter' not in text and str(shop.root) not in text and 'owner' not in text and '/tmp' not in text and '/home' not in text


def test_review_and_text_through_the_tools(shop):
    tracker = shop.own()
    shop.call('save_tracker_script', {'tracker_id': tracker['id'], 'source': WITH_TEXT, 'expected_revision': 0, 'publish': 'review', 'wake': 'never'})
    assert shop.call('test_tracker_script', {'tracker_id': tracker['id'], 'expected_revision': 1})['status'] == 'passed'
    shop.call('publish_tracker_script', {'tracker_id': tracker['id'], 'expected_revision': 1})
    assert shop.manual()['scripts'][0]['output'] == 'proposed'
    read = shop.call('get_tracker_script', {'tracker_id': tracker['id']})
    proposal = read['proposal']
    assert read['output'] is None and proposal['texts'] == [{'slot': 'memo', 'facts': {'count': proposal['value']}, 'text': None, 'written_at': None}]
    assert shop.call('get_tracker', {'tracker_id': tracker['id']})['output'] is None, 'nothing is published while the proposal waits'
    with pytest.raises(ValueError) as unnamed:
        shop.call('write_tracker_text', {'tracker_id': tracker['id'], 'slot': 'memo', 'text': 'A steady day.'})
    assert str(unnamed.value) == shop.scripts.TEXT_FOR
    stored = shop.call('write_tracker_text', {'tracker_id': tracker['id'], 'slot': 'memo', 'text': 'A steady day.', 'proposal_id': proposal['proposal_id']})
    assert stored['stored_with'] == 'proposal' and stored['page'] is None
    with pytest.raises(ValueError) as moved:
        shop.call('approve_tracker_output', {'tracker_id': tracker['id'], 'proposal_id': 'proposal-' + 'f' * 16})
    assert str(moved.value) == shop.scripts.PROPOSAL_MOVED
    jobs, runs = len(shop.box.jobs), len(shop.runs.runs(*shop.key, limit=100))
    approved = shop.call('approve_tracker_output', {'tracker_id': tracker['id'], 'proposal_id': proposal['proposal_id']})
    assert approved['status'] == 'published' and approved['output']['revision'] == 1
    # The tracker's own row of the page is assembled again at once; the tool asks for no pass and starts no script run.
    assert approved['page'] == {'refreshed': True, 'note': shop.scripts.PAGE_SHOWS} and shop.runs.state(*shop.key)['requested_at'] is None
    shown = page(shop)
    mine = next(tile for tile in shown['core_trackers'] if tile['key'] == 'pickups')
    assert mine['revision'] == 1 and set(mine['periods']) == {'week', 'month', 'year'} and shown['core_tracker_canvases']['pickups']['texts'][0]['text'] == 'A steady day.'
    after = shop.call('get_tracker_script', {'tracker_id': tracker['id']})
    assert after['proposal'] is None and after['output']['texts'][0]['text'] == 'A steady day.'
    # The agent changes its policies; the script's revision and its test stand.
    changed = shop.call('set_tracker_script_policy', {'tracker_id': tracker['id'], 'expected_revision': 1, 'publish': 'automatic', 'wake': 'on_change'})
    assert changed['script']['policy'] == {'publish': 'automatic', 'wake': 'on_change'} and changed['script']['published_revision'] == 1
    assert changed['script']['last_test']['status'] == 'passed'
    with pytest.raises(ValueError, match='Name the policy to change'):
        shop.call('set_tracker_script_policy', {'tracker_id': tracker['id'], 'expected_revision': 1})
    with pytest.raises(ValueError, match='declared fields'):
        shop.call('set_tracker_script_policy', {'tracker_id': tracker['id'], 'expected_revision': 1, 'wake': 'sometimes'})
    # A text for the published output names its revision, gives the output its next one and shows on the page at once.
    rewritten = shop.call('write_tracker_text', {'tracker_id': tracker['id'], 'slot': 'memo', 'text': 'A steady day, again.', 'output_revision': 1})
    assert (rewritten['stored_with'], rewritten['output_revision'], rewritten['page']['refreshed']) == ('output', 2, True)
    assert shop.call('get_tracker', {'tracker_id': tracker['id']})['output']['canvas']['texts'][0]['text'] == 'A steady day, again.'
    assert page(shop)['core_tracker_canvases']['pickups']['texts'][0]['text'] == 'A steady day, again.'
    assert shop.runs.state(*shop.key)['requested_at'] is None and len(shop.box.jobs) == jobs and len(shop.runs.runs(*shop.key, limit=100)) == runs, (
        'a written text and an approval start no pass and no sandbox job')
    with pytest.raises(ValueError, match='declared fields'):
        shop.call('write_tracker_text', {'tracker_id': tracker['id'], 'slot': 'memo', 'text': 'x', 'proposal_id': 'proposal-1'})


def page(shop):
    import tracker_runner as runner
    return json.loads((runner.page_directory(shop.state, 'peter') / 'data.json').read_text())


def arguments(tracker_id):
    return {'save_tracker_script': {'tracker_id': tracker_id, 'source': COUNT, 'expected_revision': 0},
            'test_tracker_script': {'tracker_id': tracker_id, 'expected_revision': 1},
            'publish_tracker_script': {'tracker_id': tracker_id, 'expected_revision': 1},
            'set_tracker_script_policy': {'tracker_id': tracker_id, 'expected_revision': 1, 'wake': 'never'},
            'approve_tracker_output': {'tracker_id': tracker_id, 'proposal_id': 'proposal-' + 'a' * 16},
            'write_tracker_text': {'tracker_id': tracker_id, 'slot': 'memo', 'text': 'Words.', 'output_revision': 1}}


def test_a_core_tracker_answers_every_write_with_locked(shop):
    from core_trackers import LOCKED
    import sqlite3
    listing = shop.call('list_trackers')['trackers']
    assert [row['key'] for row in listing] == KEYS
    for row in (listing[0], listing[7]):
        for name, args in arguments(row['id']).items():
            with pytest.raises(ValueError) as refusal:
                shop.call(name, args)
            assert str(refusal.value) == LOCKED, name
        read = shop.call('get_tracker_script', {'tracker_id': row['id']})
        assert read['core'] is True and read['script'] is None and read['locked'] == LOCKED
    assert shop.call('list_trackers')['trackers'] == listing and shop.box.jobs == []
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind LIKE 'tracker_script%' OR kind='tracker_output_proposal'").fetchone()[0] == 0


def test_a_child_a_stopped_run_and_a_turn_with_no_run_change_nothing(shop):
    tracker = shop.own()
    shop.agent.children['child'] = {'id': 'child', 'parent_id': 'chat', 'dataset': 'retail-demo', 'owner_principal_id': 'peter'}
    stopped = {'cancel': asyncio.Event()}
    stopped['cancel'].set()
    for name, args in arguments(tracker['id']).items():
        for chat, run in (('child', 'active'), ('chat', stopped), ('chat', None)):
            with pytest.raises(ValueError) as refusal:
                shop.call(name, args, chat=chat, run=run)
            assert str(refusal.value) == 'Save tracker changes from the active parent conversation.', (name, chat)
    # The child reads the script like any other read.
    assert shop.call('get_tracker_script', {'tracker_id': tracker['id']}, chat='child')['script'] is None
    assert shop.box.jobs == [] and shop.scripts.TrackerScripts(shop.company).head(tracker) is None
    # Another business's conversation and an unknown tracker are outside.
    with pytest.raises(ValueError, match='current authenticated business conversation'):
        import monitoring_tools
        asyncio.run(monitoring_tools.call_async(shop.agent, 'chat', 'another-business', 'test_tracker_script', arguments(tracker['id'])['test_tracker_script'], {'cancel': asyncio.Event()}))
    with pytest.raises(ValueError) as unknown:
        shop.call('save_tracker_script', arguments('tracker-of-another-company')['save_tracker_script'])
    assert str(unknown.value) == shop.scripts.UNKNOWN


def test_the_dispatcher_awaits_the_test_and_closes_every_write_to_a_child(tmp_path):
    """Through `agent_bridge._tool`, as a model's call arrives."""
    from agent_bridge import CodexVideoAgent
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
        for name, args in arguments('tracker-x').items():
            blocks, _, ok = await agent._tool(name, args, 'shop', 'child')
            assert (ok, blocks[0]['text']) == (False, 'Save tracker changes from the active parent conversation.'), name
        # In the parent conversation the awaited path reaches the family, which names the workspace it needs.
        blocks, _, ok = await agent._tool('test_tracker_script', arguments('tracker-x')['test_tracker_script'], 'shop', 'chat')
        assert (ok, blocks[0]['text']) == (False, 'Use the current location workspace.')
        blocks, _, ok = await agent._tool('test_tracker_script', {'tracker_id': 'tracker-x'}, 'shop', 'chat')
        assert (ok, blocks[0]['text']) == (False, 'Use the declared fields of this tool.')
    asyncio.run(scenario())


def test_script_writes_need_management_and_the_writers_own_installation(shop):
    scripts = shop.scripts
    tracker = shop.own()
    real = shop.company.locations.authorized
    def reader(owner, collection, location_id, *, write=False):
        if write:
            raise ValueError('Use location management access to update its catalog.')
        return real(owner, collection, location_id)
    shop.company.locations.authorized = reader
    for name, args in arguments(tracker['id']).items():
        with pytest.raises(ValueError) as refusal:
            shop.call(name, args)
        assert str(refusal.value) == scripts.MANAGEMENT, name
    assert shop.call('get_tracker_script', {'tracker_id': tracker['id']})['script'] is None
    shop.company.locations.authorized = real
    # A member whose present access leaves the location out reads and changes nothing of it: the tracker reads as absent.
    for name, args in {'get_tracker_script': {'tracker_id': tracker['id']}, **arguments(tracker['id'])}.items():
        with pytest.raises(ValueError) as outside:
            shop.call(name, args, index=View([]))
        assert str(outside.value) == scripts.UNKNOWN, name


def test_the_read_answers_a_customers_agent_over_mcp_inside_its_scope(shop):
    """Over MCP the store is a fresh one with no company handle: the scoped view decides, read by read."""
    import monitoring_tools
    from monitoring import MonitoringStore
    scripts = shop.scripts
    tracker = shop.own()
    shop.call('save_tracker_script', arguments(tracker['id'])['save_tracker_script'])
    fresh = lambda view: monitoring_tools.scoped(MonitoringStore(shop.state), view)
    read = lambda view, owner='peter', name='get_tracker_script': monitoring_tools.read(fresh(view), owner, 'retail-demo', name, {'tracker_id': tracker['id']})
    answer = read(shop.view())
    assert answer['script']['source'] == COUNT and answer['script']['revision'] == 1 and answer['script']['health']['status'] == 'draft'
    assert answer['contract'] == scripts.TRACKER_SCRIPT_INSTRUCTIONS and answer['output'] is None and answer['runs'] == []
    scope = shop.view().business_scopes[0]
    for view in (View([{**scope, 'includes_unassigned': False, 'location_ids': ['location-elsewhere']}]), View([{**scope, 'can_query': False}]),
                 shop.view(current=False), View([])):
        with pytest.raises(ValueError) as closed:
            read(view)
        assert str(closed.value) == scripts.UNKNOWN
    with pytest.raises(ValueError) as theirs:
        read(shop.view(), owner='maria')   # another member reads their own records: this tracker is none of them
    assert str(theirs.value) == scripts.UNKNOWN
    for write in sorted(WRITES):
        with pytest.raises(ValueError, match='Choose an available observation read tool'):
            read(shop.view(), name=write)


def promote(shop, chat='chat', **more):
    return shop.call('promote_chat_to_agent', {'title': 'Pickup watch', 'instruction': 'Summarize pickups.', 'agent_kind': 'tracker',
                                                'input_kind': 'tracker_events', **more}, chat=chat)


def test_promoting_a_chat_links_the_tracking_agent_to_one_tracker_record(shop):
    from core_trackers import installed_trackers
    profile = shop.agent.monitoring_store.save_profile('peter', {'name': 'Pickups', 'dataset': 'retail-demo', 'vocabulary': [
        {'type': 'pick_up', 'description': 'Visible pickup'}, {'type': 'put_back', 'description': 'Visible return'}]}, datasets=['retail-demo'])
    promoted = promote(shop, profile_ids=[profile['id']])
    agent_id = promoted['id']
    record = promoted['tracker']
    assert record['location_id'] == shop.location and record['key'] == 'agent_' + agent_id.removeprefix('agent-') and record['label'] == 'Pickup watch'
    assert record['event_types'] == ['pick_up', 'put_back'] and 'tracker_next_step' not in promoted
    saved = next(t for t in installed_trackers(shop.company, shop.location) if t['key'] == record['key'])
    assert saved['agent_id'] == agent_id and saved['agent'] == {'kind': 'tracking_agent', 'chat': True, 'agent_id': agent_id}
    assert saved['core'] is False and saved['processor'] == {'script': None, 'reads': ['events'], 'summary': 'agent'}
    assert shop.agent.children['chat']['agent_id'] == agent_id
    # The location lists one row for the two: the record's, with the agent's state.
    rows = [row for row in shop.call('list_trackers')['trackers'] if not row['core']]
    assert [(row['id'], row['agent_id'], row['location_id'], row['kind'], row['status']) for row in rows] == [(record['id'], agent_id, shop.location, 'tracking_agent', 'draft')]
    assert [t['key'] for t in shop.call('list_monitoring_profiles')['trackers']] == KEYS + [record['key']]
    # The agent writes its script by either id: the tracker's or its own.
    by_agent = shop.call('save_tracker_script', {'tracker_id': agent_id, 'source': COUNT, 'expected_revision': 0})
    assert by_agent['tracker_id'] == record['id'] and by_agent['script']['revision'] == 1
    assert shop.call('get_tracker_script', {'tracker_id': agent_id})['tracker_id'] == record['id']
    # Promoting the same chat again returns the same agent and the same record.
    assert promote(shop, profile_ids=[profile['id']])['tracker']['id'] == record['id']
    assert sum(1 for t in installed_trackers(shop.company, shop.location) if not t['core']) == 1


def test_a_location_named_at_promotion_is_checked_before_anything_is_made(shop):
    shop.agent.children['second'] = {'id': 'second', 'dataset': 'retail-demo', 'owner_principal_id': 'peter', 'messages': []}
    with pytest.raises(ValueError, match='Choose a location in your current access'):
        promote(shop, location_id='location-of-another-company')
    assert shop.agent.automation_store.list() == [] and 'agent_id' not in shop.agent.children['chat']
    with pytest.raises(ValueError, match='A location belongs to a Tracker Agent'):
        shop.call('promote_chat_to_agent', {'title': 'Weekly', 'instruction': 'Report.', 'agent_kind': 'action', 'input_kind': 'tracker_events', 'location_id': shop.location})
    assert promote(shop, location_id=shop.location)['tracker']['location_id'] == shop.location
    # An Action Agent has no tracker record.
    action = shop.call('promote_chat_to_agent', {'title': 'Weekly', 'instruction': 'Report.', 'agent_kind': 'action', 'input_kind': 'tracker_events'}, chat='second')
    assert 'tracker' not in action


def test_an_agent_promoted_earlier_gets_its_record_at_its_first_script(shop):
    from core_trackers import installed_trackers
    spec = {'title': 'Door watch', 'instruction': 'Watch the door.', 'dataset': 'retail-demo', 'agent_kind': 'tracker', 'input_kind': 'tracker_events'}
    earlier = shop.agent.automation_store.create(spec, ['retail-demo'], owner_principal_id='peter')
    row = next(row for row in shop.call('list_trackers')['trackers'] if row['id'] == earlier['id'])
    assert row['location_id'] is None and row['kind'] == 'tracking_agent'
    # The id list_trackers gives it reads the contract before the first save, with the step that gives it its record.
    unlinked = shop.call('get_tracker_script', {'tracker_id': earlier['id']})
    assert (unlinked['tracker_id'], unlinked['agent_id'], unlinked['label'], unlinked['location_id']) == (None, earlier['id'], 'Door watch', None)
    assert unlinked['contract'] == shop.scripts.TRACKER_SCRIPT_INSTRUCTIONS and unlinked['example'] == shop.scripts.EXAMPLE_SOURCE and unlinked['limits']['seconds'] == 30
    assert unlinked['script'] is None and unlinked['runs'] == [] and unlinked['next_step'] == shop.scripts.UNLINKED
    with pytest.raises(ValueError) as first:
        shop.call('test_tracker_script', {'tracker_id': earlier['id'], 'expected_revision': 1})
    assert str(first.value) == shop.scripts.SAVE_FIRST
    saved = shop.call('save_tracker_script', {'tracker_id': earlier['id'], 'source': COUNT, 'expected_revision': 0})
    record = next(t for t in installed_trackers(shop.company, shop.location) if t.get('agent_id') == earlier['id'])
    assert saved['tracker_id'] == record['id'] and saved['location_id'] == shop.location and record['label'] == 'Door watch'
    rows = [row for row in shop.call('list_trackers')['trackers'] if not row['core']]
    assert [(row['id'], row.get('agent_id')) for row in rows] == [(record['id'], earlier['id'])], 'one row for the agent and its record'
    assert shop.call('test_tracker_script', {'tracker_id': earlier['id'], 'expected_revision': 1})['status'] == 'passed'
    # Another member's agent is outside this member's tools.
    theirs = shop.agent.automation_store.create({**spec, 'title': 'Theirs'}, ['retail-demo'], owner_principal_id='maria')
    for name, args in (('save_tracker_script', {'tracker_id': theirs['id'], 'source': COUNT, 'expected_revision': 0}), ('get_tracker_script', {'tracker_id': theirs['id']})):
        with pytest.raises(ValueError) as unknown:
            shop.call(name, args)
        assert str(unknown.value) == shop.scripts.UNKNOWN


def test_every_sentence_a_tool_returns_says_what_is(shop):
    scripts = shop.scripts
    tracker = shop.own()
    said = []
    for name, args in arguments(tracker['id']).items():
        if name == 'save_tracker_script':
            continue
        try:
            shop.call(name, args)
        except ValueError as exc:
            said.append(str(exc))
    assert said and said[0] == scripts.SAVE_FIRST
    said.append(shop.call('save_tracker_script', {'tracker_id': tracker['id'], 'source': 'x = 1\n', 'expected_revision': 0})['error']['message'])
    shop.call('save_tracker_script', arguments(tracker['id'])['save_tracker_script'])
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_offline')}
    away = shop.call('test_tracker_script', {'tracker_id': tracker['id'], 'expected_revision': 1})
    assert away['status'] == 'unavailable' and away['error']['message'] == scripts.FAULTS['sandbox_offline']
    said.append(away['error']['message'])
    assert [text for text in said if NEGATIVE.search(text)] == []


# ── What two reviewers broke, kept as it was fixed ─────────────────────────────────────────────

LEAKS = 'def run(input):\n    raise RuntimeError("SEEN " + str([[e["camera_id"], e["description"]] for e in input["events"][:2]]))\n'
COPIES = COUNT.replace('str(len(rows)) + " events, "', '"SEEN " + str([e["description"] for e in input["events"][:1]]) + " "')


def test_a_script_reads_every_camera_of_its_location_and_so_does_whoever_runs_and_reads_it(shop):
    """A script copies what it reads into its summary, its facts and the message of its own
    exception. A member tests, approves and writes for a script while their present access covers
    every camera of the location, and reads its output, its proposal and its messages the same
    way. Every other read gets the code and the line."""
    import monitoring_tools
    from monitoring import MonitoringStore
    import tracker_runner as runner
    from test_tracker_tools import narrow_to_a_room_with_no_camera
    scripts = shop.scripts
    tracker = shop.own()
    key = {'tracker_id': tracker['id']}
    shop.call('save_tracker_script', {**key, 'source': LEAKS, 'expected_revision': 0})
    failed = shop.call('test_tracker_script', {**key, 'expected_revision': 1})
    assert failed['status'] == 'failed' and failed['error']['message'].startswith('RuntimeError: SEEN [[') and 'camera-' in failed['error']['message']
    shop.call('save_tracker_script', {**key, 'source': COPIES, 'expected_revision': 1, 'publish': 'review', 'wake': 'never'})
    assert shop.call('test_tracker_script', {**key, 'expected_revision': 2})['status'] == 'passed'
    shop.call('publish_tracker_script', {**key, 'expected_revision': 2})
    shop.now[0] += 120
    assert shop.sweep()[0]['scripts'][0]['output'] == 'proposed'
    # A stopped run of the published script, so the health holds a message too.
    shop.call('save_tracker_script', {**key, 'source': LEAKS, 'expected_revision': 2})
    assert shop.call('test_tracker_script', {**key, 'expected_revision': 3})['status'] == 'failed'
    whole = shop.call('get_tracker_script', key)
    proposal = whole['proposal']
    assert proposal['summary'].startswith("SEEN ['") and 'output_access' not in whole and whole['output'] is None
    cameras = runner.location_cameras(shop.state, 'retail-demo', shop.location)
    record = scripts.TrackerScripts(shop.company).proposal(tracker)
    assert record['camera_ids'] == cameras != [], 'a proposal is the record of every camera of its location'
    # The run rows hold this host's words for every reader: the code, the line and where the message is.
    rows = json.dumps(shop.call('list_tracker_runs', {'location_id': shop.location, 'limit': 100}))
    assert 'SEEN' not in rows and 'RuntimeError' not in rows and 'The script stopped at line 2 in its day reading with script_error.' in rows
    assert 'SEEN' not in json.dumps(shop.call('get_tracker', key)), 'get_tracker returns the script’s health by code and line'
    assert 'SEEN' not in json.dumps(whole['runs']) and whole['runs'][0]['error'] == {'code': 'script_error', 'line': 2, 'period': 'day', 'message': 'The script raised an error.'}

    narrow_to_a_room_with_no_camera(shop)
    assert shop.call('list_tracker_events', {'limit': 500})['events'] == []
    before = (len(shop.box.jobs), shop.runs.runs(*shop.key, limit=100), runner.current_outputs(shop.state, *shop.key), scripts.TrackerScripts(shop.company).head(tracker))
    # The writes that read the location's rows, or publish what they add up to, name the access they need.
    for name, args in (('test_tracker_script', {**key, 'expected_revision': 3}), ('approve_tracker_output', {**key, 'proposal_id': proposal['proposal_id']}),
                       ('write_tracker_text', {**key, 'slot': 'memo', 'text': 'Words.', 'proposal_id': proposal['proposal_id']})):
        with pytest.raises(ValueError) as refusal:
            shop.call(name, args)
        assert str(refusal.value) == runner.CAMERA_ACCESS, name
    assert before == (len(shop.box.jobs), shop.runs.runs(*shop.key, limit=100), runner.current_outputs(shop.state, *shop.key), scripts.TrackerScripts(shop.company).head(tracker))
    assert scripts.TrackerScripts(shop.company).proposal(tracker) == record, 'nothing ran, nothing was published and no job was counted'
    # The reads: on the hosted surface and over MCP, the script and its runs stay listed and every word a script wrote stays out.
    for read in (lambda name, args: shop.call(name, args),
                 lambda name, args: monitoring_tools.read(monitoring_tools.scoped(MonitoringStore(shop.state), shop.view()), 'peter', 'retail-demo', name, args)):
        narrowed = read('get_tracker_script', key)
        assert narrowed['output'] is None and narrowed['proposal'] is None and narrowed['output_access'] == scripts.CAMERA_SCOPE
        assert narrowed['script']['source'] == LEAKS and narrowed['script']['published_revision'] == 2, 'the script’s own source is its definition'
        said = json.dumps([narrowed['script']['health'], narrowed['runs'], read('get_tracker', key), read('list_tracker_runs', {'location_id': shop.location, 'limit': 100}),
                           read('list_trackers', {})])
        assert 'SEEN' not in said and 'takes an item' not in said and all(camera not in said for camera in cameras)
        assert narrowed['script']['health']['last_run'] is None or 'message' not in narrowed['script']['health']['last_run']
    # With the cameras back in a reader's access the same read carries the words again.
    assert monitoring_tools.read(monitoring_tools.scoped(MonitoringStore(shop.state), shop.view('maria')), 'peter', 'retail-demo', 'get_tracker_script', key)['proposal']['summary'].startswith('SEEN')


def test_a_stopped_runs_message_is_read_with_the_cameras_and_the_rows_stay_plain(shop):
    """An unattended run that stops keeps the script's message in the script's health."""
    from test_tracker_tools import narrow_to_a_room_with_no_camera
    scripts = shop.scripts
    tracker = shop.own()
    key = {'tracker_id': tracker['id']}
    shop.call('save_tracker_script', {**key, 'source': COUNT, 'expected_revision': 0})
    assert shop.call('test_tracker_script', {**key, 'expected_revision': 1})['status'] == 'passed'
    shop.call('publish_tracker_script', {**key, 'expected_revision': 1})
    shop.box.answer = {'status': 'succeeded', 'log': '', 'results': [{'ok': False, 'error': {
        'code': 'script_error', 'message': 'RuntimeError: SEEN camera-1 takes a bottle', 'line': 7, 'traceback': 'tb'}}] * 4}
    for _ in range(scripts.PAUSE_AFTER):
        shop.runs.request(*shop.key, by='peter')
        shop.now[0] += 120
        run = shop.sweep()[0]
        assert run['scripts'][0]['status'] == 'failed' and 'SEEN' not in json.dumps(run)
    health = shop.call('get_tracker_script', key)['script']['health']
    assert health['status'] == 'paused' and health['last_run']['message'] == 'RuntimeError: SEEN camera-1 takes a bottle' and health['last_run']['line'] == 7
    assert 'The last one: RuntimeError: SEEN camera-1 takes a bottle.' in health['paused']['reason']
    plain = shop.call('get_tracker', key)['script']['health']
    assert 'message' not in plain['last_run'] and plain['paused']['reason'] == (
        'The script is paused after 3 stopped runs in a row. The last one: it stopped at line 7 with script_error. '
        'A passing test of the published revision, or publishing a corrected revision, starts it again.')
    narrow_to_a_room_with_no_camera(shop)
    narrowed = shop.call('get_tracker_script', key)
    assert narrowed['script']['health'] == plain and narrowed['output_access'] == scripts.CAMERA_SCOPE and 'SEEN' not in json.dumps(narrowed)
