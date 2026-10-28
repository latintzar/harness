"""A script tracker inside the tracker runner's passes: what a run publishes, what a stopped run
leaves in place, the pause, review, the wake policy and written text.

Each test has its own copy of one small company (a retail location with its Core Trackers and a
generated day). The sandbox is a double that runs the real entry file and the real script in a
local process. No provider and no model is called."""
from retail_agent_inputs import RetailInputProvider
import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_tracker_runner import KEYS, NOON, build, more_recordings, open_company, settle
from test_tracker_scripts import COUNT, LocalSandbox, loop_thread  # noqa: F401  (loop_thread is a fixture)

FRAGILE = COUNT.replace('    rows = [', '    if len(input["events"]) > LIMIT:\n        raise RuntimeError("more events than this script expects")\n    rows = [')
WITH_TEXT = COUNT.replace('"by_type": [{"type": "spill", "value": len(rows)}]}}',
                          '"by_type": []}, "texts": [{"slot": "memo", "facts": {"count": len(rows), "day": period["label"]}}] if period["name"] == "day" else []}')


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp('script-template')
    location, room = build(root)
    return root, location, room


@pytest.fixture
def shop(template, tmp_path, loop_thread):  # noqa: F811
    import tracker_runner as runner
    import tracker_scripts as scripts
    source, location, room = template
    root = tmp_path / 'state'
    shutil.copytree(source, root)
    settle(root)
    now = [NOON]
    state = root / 'environments' / 'shop'
    box = LocalSandbox(tmp_path / 'jobs', loop=loop_thread)
    (tmp_path / 'jobs').mkdir()
    fixture = SimpleNamespace(root=root, state=state, company=open_company(root), location=location, room=room, now=now, box=box, scripts=scripts,
        runs=runner.TrackerRuns(state, clock=lambda: now[0]), key=('peter', 'retail-demo', location), store=scripts.TrackerScripts(open_company(root)))
    fixture.sweep = lambda **more: runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={}, custom=scripts.sandbox_processor(box), **more)
    return fixture


def tracker_of(shop, key='pickups'):
    from core_trackers import installed_trackers
    return next(t for t in installed_trackers(shop.company, shop.location) if t['key'] == key)


def go_live(shop, source=COUNT, key='pickups', agent_id=None, **policy):
    """Save, test and publish a script for a new tracker of the company, as the tools do."""
    from core_trackers import add_tracker
    tracker = add_tracker(shop.company, shop.location, key, 'Pickups', 'Items a visitor picks up.', event_types=['pick_up'], agent_id=agent_id)
    shop.store.save(tracker, source, expected_revision=0, policy=policy)
    answer = asyncio.run(shop.scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    assert answer['status'] == 'passed', answer
    shop.scripts.publish_script(shop.company, tracker, expected_revision=1)
    return tracker_of(shop, key)


def manual(shop):
    """One requested pass of the location; returns its run row."""
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 120
    return shop.sweep()[0]


def stored(shop, key='pickups'):
    import tracker_runner as runner
    return next((o for o in runner.current_outputs(shop.state, *shop.key) if o['tracker_key'] == key), None)


def core_revisions(shop):
    import tracker_runner as runner
    return {o['tracker_key']: o['revision'] for o in runner.current_outputs(shop.state, *shop.key) if o['core']}


def page(shop):
    import tracker_runner as runner
    return json.loads((runner.page_directory(shop.state, 'peter') / 'data.json').read_text())


def agent_for(shop, **more):
    """An active Tracking Agent the wake queue accepts wakes for."""
    from agent_test_support import retail_agent_store as AgentStore
    store = AgentStore(shop.state, input_provider=RetailInputProvider(monitoring=shop.company.monitoring))
    agent = store.create({'title': 'Pickup watch', 'instruction': 'Watch pickups.', 'dataset': 'retail-demo', 'agent_kind': 'tracker',
                          'input_kind': 'tracker_events', 'mode': 'ongoing', 'max_runs': 5, **more}, ['retail-demo'], owner_principal_id='peter')
    store.activate(agent['id'], time.time() + 3600)
    return store, agent


def wakes(shop):
    with sqlite3.connect(shop.state / 'background-agents.sqlite') as db:
        return [json.loads(row[0]) for row in db.execute('SELECT body FROM agent_wakes ORDER BY rowid')]


def test_a_published_script_runs_in_the_pass_and_publishes_like_a_core_tracker(shop):
    shop.sweep()
    core = core_revisions(shop)
    tracker = go_live(shop)
    assert tracker['processor'] == {'script': None, 'reads': ['events'], 'summary': 'script',
                                    'sandbox': {'kind': 'tracker-python-v1', 'script_id': shop.store.head(tracker)['id'], 'revision': 1}}
    assert stored(shop) is None and len(shop.box.jobs) == 1, 'the test ran one job and published zero outputs'
    # The location's next pass is the script's run for this local day, whatever started the pass.
    shop.now[0] += 120
    run = shop.sweep()[0]
    assert run['trigger'] == 'events' and run['status'] == 'completed' and run['errors'] == [] and run['page_published']
    assert [o['key'] for o in run['outputs']] == KEYS + ['pickups'] and run['outputs'][-1] == {
        'key': 'pickups', 'tracker_id': tracker['id'], 'core': False, 'revision': 1, 'changed': True, 'state': 'ready'}
    entry = run['scripts'][0]
    assert {k: entry[k] for k in ('key', 'script_revision', 'trigger', 'status', 'error', 'output', 'output_revision', 'wake', 'retry')} == {
        'key': 'pickups', 'script_revision': 1, 'trigger': 'events', 'status': 'succeeded', 'error': None, 'output': 'published',
        'output_revision': 1, 'wake': None, 'retry': False} and entry['seconds'] > 0
    assert shop.box.jobs[-1]['wait_seconds'] == shop.scripts.SLOT_WAIT_SECONDS and 0 < shop.box.jobs[-1]['seconds'] <= shop.scripts.JOB_SECONDS
    assert shop.box.jobs[-1]['periods'] == ['day', 'week', 'month', 'year'], 'one job computes the four periods'
    output = stored(shop)
    tile, canvas = output['tile'], output['canvas']
    assert output['revision'] == 1 and output['core'] is False and output['tracker_id'] == tracker['id']
    assert (tile['key'], tile['label'], tile['tile_family'], tile['canvas_family'], tile['unit'], tile['state']) == ('pickups', 'Pickups', 'metric', 'occurrences', 'events', 'ready')
    assert tile['company_id'] == shop.company.id and tile['location_id'] == shop.location and tile['camera_ids'] == output['camera_ids'] != []
    assert tile['script_revision'] == canvas['script_revision'] == 1 and tile['value'] == canvas['total_rows'] > 0
    assert canvas['family'] == 'occurrences' and canvas['key'] == 'pickups' and canvas['rows'][0]['type'] == 'pick_up'
    assert set(canvas['rows'][0]) >= {'id', 'at', 'label', 'description', 'thumb', 'visitor_id', 'source_id', 'camera_id'}
    assert shop.scripts.content_of(tile, canvas) == output['content_sha256']
    assert core_revisions(shop) == core, 'the Core outputs stand as they were'
    # The page carries the tile with the week, the month and the year, beside the nine.
    shown = page(shop)
    mine = shown['core_trackers'][9]
    assert [t['key'] for t in shown['core_trackers']] == KEYS + ['pickups'] and mine['core'] is False and mine['revision'] == 1
    assert set(mine['periods']) == {'week', 'month', 'year'} and mine['periods']['week']['value'] >= mine['value']
    assert mine['periods']['year']['period_label'].endswith('2026') and 'pickups' in shown['core_tracker_canvases']
    # The same events and records: the last result stands and the sandbox takes zero jobs.
    jobs = len(shop.box.jobs)
    again = manual(shop)
    assert [(s['status'], s['output'], s['output_revision']) for s in again['scripts']] == [('reused', 'unchanged', 1)] and len(shop.box.jobs) == jobs
    assert again['outputs'][-1]['changed'] is False and stored(shop) == output
    # New events of its type: a new revision.
    more_recordings(shop)
    newer = manual(shop)
    assert newer['scripts'][0]['output'] == 'published' and stored(shop)['revision'] == 2 and stored(shop)['tile']['value'] > tile['value']
    health = shop.scripts.health_of(shop.store.head(tracker))
    assert health['status'] == 'ready' and health['consecutive_failures'] == 0 and health['last_good']['output_revision'] == 2


def test_the_script_never_runs_in_the_service_process(shop, tmp_path):
    flag = tmp_path / 'ran.txt'
    source = (f"import os\nopen({str(flag)!r}, 'a').write(str(os.getpid()) + '\\n')\n" + COUNT)
    before = set(sys.modules)
    go_live(shop, source)
    manual(shop)
    pids = {int(line) for line in flag.read_text().split()}
    assert pids and os.getpid() not in pids, 'the script’s code ran in the job’s process alone'
    assert not {name for name in set(sys.modules) - before if name in {'tracker', 'rocoto_entry'}}


def test_event_runs_follow_the_hosts_switch_and_each_local_day_has_its_run(shop):
    scripts = shop.scripts
    tracker = go_live(shop)
    shop.now[0] += 120
    first = shop.sweep()[0]
    assert first['trigger'] == 'events' and first['scripts'][0]['status'] == 'succeeded' and stored(shop)['revision'] == 1
    assert scripts.health_of(shop.store.head(tracker))['day'] == {'day': '2026-10-11', 'done': True}
    # More events the same local day: the script ran for the day, and with event runs off it waits.
    more_recordings(shop)
    shop.now[0] += 120
    same = shop.sweep()[0]
    assert same['trigger'] == 'events' and [(s['status'], s['reason'], s['retry']) for s in same['scripts']] == [('waiting', scripts.EVENT_RUNS_WAIT, False)]
    assert stored(shop)['revision'] == 1 and len(shop.box.jobs) == 2
    # The location's day turns: the script runs with the switch off.
    shop.now[0] += 86400
    day = shop.sweep()[0]
    assert day['trigger'] == 'day' and day['scripts'][0]['status'] == 'succeeded' and stored(shop)['revision'] == 2
    # The host switches event runs on: new events start a run.
    shop.box.event_runs = True
    more_recordings(shop, date(2026, 10, 12))
    shop.now[0] += 120
    events = shop.sweep()[0]
    assert events['trigger'] == 'events' and events['scripts'][0]['status'] == 'succeeded' and stored(shop)['revision'] == 3
    assert shop.scripts.switches({}) == {'sandbox': False, 'event_runs': False}
    assert shop.scripts.switches({'ROCOTO_CUSTOM_EXECUTOR_ENABLED': '1', 'ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS': ' ON '}) == {'sandbox': True, 'event_runs': True}
    with pytest.raises(ValueError, match='ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS'):
        shop.scripts.switches({'ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS': 'yes'})


@pytest.mark.parametrize('fault', ['raises', 'time_limit', 'oversize', 'reserved', 'fabricated_id', 'unavailable'])
def test_a_stopped_run_leaves_the_last_output_and_the_core_outputs(shop, fault):
    scripts = shop.scripts
    tracker = go_live(shop, FRAGILE.replace('LIMIT', '100000'))
    manual(shop)
    good = stored(shop)
    readings = page(shop)['core_trackers'][9]['periods']
    more_recordings(shop)
    late = {'ok': False, 'error': {'code': 'time_limit', 'message': 'The script ran past its 30 seconds.', 'line': None, 'traceback': None}}
    fine = {'ok': True, 'output': {'value': 1, 'unit': 'events', 'state': 'ready', 'needs': None, 'summary': 'One.', 'series': [], 'events': [],
                                   'canvas': {'rows': [], 'total_rows': 0}}}
    def bad(**change):
        output = json.loads(json.dumps(fine['output']))
        for path, value in change.items():
            target = output
            *parents, last = path.split('__')
            for name in parents:
                target = target[name]
            target[last] = value
        return {'status': 'succeeded', 'results': [{'ok': True, 'output': output}] + [fine] * 3, 'log': ''}
    shop.box.answer = {
        'raises': {'status': 'succeeded', 'results': [{'ok': False, 'error': {'code': 'script_error', 'message': "KeyError: 'aisle'", 'line': 14, 'traceback': 'tb'}}] + [fine] * 3, 'log': ''},
        'time_limit': {'status': 'failed', 'error': scripts.fault('time_limit')},
        'oversize': {'status': 'succeeded', 'results': [fine, fine, {'ok': False, 'error': {'code': 'output_too_large', 'message': 'One output is up to 114688 bytes of JSON; this one has 200000.'}}, late], 'log': ''},
        'reserved': bad(canvas__label='Visitors'),
        'fabricated_id': bad(canvas__rows=[{'id': 'event-of-another-company'}], canvas__total_rows=1),
        'unavailable': {'status': 'unavailable', 'error': scripts.fault('sandbox_busy')}}[fault]
    run = manual(shop)
    assert run['status'] == 'completed' and run['page_published'], 'a script’s fault is its own: the pass completes'
    assert [o['key'] for o in run['outputs']] == KEYS + ['pickups'] and run['outputs'][-1]['changed'] is False
    assert all(o['revision'] >= 1 for o in run['outputs'][:9]) and len(core_revisions(shop)) == 9
    assert stored(shop) == good, 'the last published output stands, byte for byte'
    assert page(shop)['core_trackers'][9]['revision'] == good['revision'] and page(shop)['core_trackers'][9]['periods'] == readings
    entry = run['scripts'][0]
    expected = {'raises': ('failed', 'script_error'), 'time_limit': ('failed', 'time_limit'), 'oversize': ('failed', 'output_too_large'),
                'reserved': ('rejected', 'output_contract'), 'fabricated_id': ('rejected', 'output_contract'), 'unavailable': ('unavailable', 'sandbox_busy')}[fault]
    assert (entry['status'], entry['error']['code']) == expected and entry['output'] is None
    assert entry['error']['message'] == scripts.FAULTS[expected[1]], 'a run row carries this host’s sentence for the code'
    health, words = scripts.health_of(shop.store.head(tracker)), scripts.health_of(shop.store.head(tracker), words=True)
    if fault == 'unavailable':
        assert run['errors'] == [] and health['consecutive_failures'] == 0 and health['status'] == 'ready'
        # A busy sandbox passes by itself: the day stays owed and the service asks for the script's next pass.
        assert entry['reason'] == f"{scripts.FAULTS['sandbox_busy']} {scripts.RETRY_SOON}" and entry['retry'] is True
        assert shop.runs.state(*shop.key)['requested_only'] == ['pickups'] and health['day'] == {'day': '2026-10-11', 'done': True}
    else:
        assert [e['key'] for e in run['errors']] == ['pickups'] and run['errors'][0]['error'].startswith('The script stopped') and entry['retry'] is False
        assert health['consecutive_failures'] == 1 and health['status'] == 'failing' and health['last_run']['error_code'] == expected[1]
        assert 'message' not in health['last_run'] and words['last_run']['message'], 'the script’s own words stay with its health'
    if fault == 'raises':
        assert run['errors'][0]['error'] == 'The script stopped at line 14 in its day reading with script_error. get_tracker_script shows the message.'
        assert entry['error'] == {'code': 'script_error', 'line': 14, 'period': 'day', 'message': 'The script raised an error.'}
        assert words['last_run']['message'] == "KeyError: 'aisle'" and (words['last_run']['line'], words['last_run']['period']) == (14, 'day')
        assert 'KeyError' not in json.dumps(run) and 'aisle' not in json.dumps(shop.runs.runs(*shop.key, limit=5))
    if fault == 'reserved':
        assert 'day: canvas.label' in words['last_run']['message'] and 'canvas.label' not in json.dumps(run)
    assert '/' not in json.dumps(run['errors']) and 'peter' not in json.dumps(run['scripts'])


def test_three_stopped_runs_in_a_row_pause_the_script_and_say_why(shop):
    from core_trackers import common
    scripts = shop.scripts
    store, agent = agent_for(shop)
    # A script that stops once the location holds more pickups than it does now.
    data = common.read_location(shop.company, shop.location)
    probe = {'id': 'tracker-probe', 'key': 'probe', 'label': 'Probe', 'tracker': {'event_types': ['pick_up']}}
    count = len(scripts.build_input(data, probe, scripts.job_periods(shop.company, shop.location, data))['events'])
    tracker = go_live(shop, FRAGILE.replace('LIMIT', str(count)), agent_id=agent['id'], wake='on_failure')
    assert manual(shop)['scripts'][0]['status'] == 'succeeded' and wakes(shop) == []
    good = stored(shop)
    for attempt in (1, 2, 3):
        more_recordings(shop, date(2026, 10, 10 + attempt))
        run = manual(shop)
        entry = run['scripts'][0]
        assert entry['status'] == 'failed' and entry['error'] == {'code': 'script_error', 'line': 4, 'period': 'day', 'message': 'The script raised an error.'}
        assert entry['wake']['queued'] is True and len(wakes(shop)) == attempt
        health = scripts.health_of(shop.store.head(tracker), words=True)
        assert health['last_run']['message'] == 'RuntimeError: more events than this script expects'
        assert health['consecutive_failures'] == attempt and (health['status'] == 'paused') == (attempt == 3)
    paused = health['paused']
    assert paused['revision'] == 1 and paused['error_code'] == 'script_error' and paused['line'] == 4
    assert paused['reason'] == ('The script is paused after 3 stopped runs in a row. The last one: RuntimeError: more events than this script expects. '
                                'A passing test of the published revision, or publishing a corrected revision, starts it again.')
    # A read outside the location's cameras gets the same pause in this host's words.
    plain = scripts.health_of(shop.store.head(tracker))['paused']['reason']
    assert plain == ('The script is paused after 3 stopped runs in a row. The last one: it stopped at line 4 with script_error. '
                     'A passing test of the published revision, or publishing a corrected revision, starts it again.')
    assert 'paused after 3 stopped runs' in wakes(shop)[-1]['text'] and wakes(shop)[0]['provider'] == 'tracker_script'
    assert wakes(shop)[0]['connection_id'] == tracker['id'] and 'more events than this script expects' in wakes(shop)[0]['text']
    # Paused: the passes go on for the Core Trackers, the script takes zero jobs and wakes nobody.
    jobs = len(shop.box.jobs)
    more_recordings(shop, date(2026, 10, 14))
    held = manual(shop)
    assert held['status'] == 'completed' and [(s['status'], s['reason']) for s in held['scripts']] == [('paused', plain)]
    assert len(shop.box.jobs) == jobs and len(wakes(shop)) == 3 and stored(shop) == good
    # A corrected revision, tested and published, starts it again.
    shop.store.save(tracker, COUNT, expected_revision=1)
    assert scripts.health_of(shop.store.head(tracker))['status'] == 'paused', 'saving a draft leaves the published revision paused'
    assert asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=2, sandbox=shop.box))['status'] == 'passed'
    scripts.publish_script(shop.company, tracker, expected_revision=2)
    assert scripts.health_of(shop.store.head(tracker))['status'] in {'ready', 'waiting'}
    resumed = manual(shop)
    assert resumed['scripts'][0]['status'] == 'succeeded' and stored(shop)['tile']['script_revision'] == 2


def test_a_passing_test_of_the_paused_revision_starts_it_again(shop):
    scripts = shop.scripts
    tracker = go_live(shop)
    manual(shop)
    shop.box.answer = {'status': 'failed', 'error': scripts.fault('script_stopped')}
    for attempt in (1, 2, 3):
        more_recordings(shop, date(2026, 10, 10 + attempt))
        manual(shop)
    assert scripts.health_of(shop.store.head(tracker))['status'] == 'paused'
    assert asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))['status'] == 'failed'
    assert scripts.health_of(shop.store.head(tracker))['status'] == 'paused', 'a failing test leaves the pause'
    shop.box.answer = None
    assert asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))['status'] == 'passed'
    health = scripts.health_of(shop.store.head(tracker))
    assert health['paused'] is None and health['consecutive_failures'] == 0
    assert manual(shop)['scripts'][0]['status'] == 'succeeded'


def test_review_publishes_nothing_until_the_proposal_is_approved(shop):
    scripts = shop.scripts
    tracker = go_live(shop, publish='review', wake='never')
    first = manual(shop)
    entry = first['scripts'][0]
    assert entry['status'] == 'succeeded' and entry['output'] == 'proposed' and entry['proposal_id'].startswith('proposal-')
    assert stored(shop) is None and [o['key'] for o in first['outputs']] == KEYS, 'nothing of the script is on the surface'
    assert page(shop)['core_trackers'][9]['needs'] == 'Needs first run'
    proposal = shop.store.proposal(tracker)
    assert proposal['proposal_id'] == entry['proposal_id'] and proposal['script_revision'] == 1 and proposal['value'] > 0
    with pytest.raises(ValueError, match='the proposal to approve is the one it shows now'):
        scripts.approve_output(shop.company, tracker, 'proposal-' + '0' * 16)
    output = scripts.approve_output(shop.company, tracker, proposal['proposal_id'])
    assert output['revision'] == 1 and stored(shop) == output and shop.store.proposal(tracker) is None
    assert output['tile']['value'] == proposal['value'] and output['content_sha256'].startswith(proposal['proposal_id'].removeprefix('proposal-'))
    shown = manual(shop)
    assert shown['scripts'][0]['status'] == 'reused' and set(page(shop)['core_trackers'][9]['periods']) == {'week', 'month', 'year'}
    # New events: the published output stays while the next one waits.
    more_recordings(shop)
    second = manual(shop)
    assert second['scripts'][0]['output'] == 'proposed' and stored(shop) == output and page(shop)['core_trackers'][9]['revision'] == 1
    waiting = shop.store.proposal(tracker)
    assert waiting['proposal_id'] != proposal['proposal_id'] and waiting['value'] > output['tile']['value']
    with pytest.raises(ValueError):
        scripts.approve_output(shop.company, tracker, proposal['proposal_id'])  # the earlier proposal is gone
    assert scripts.approve_output(shop.company, tracker, waiting['proposal_id'])['revision'] == 2
    # The agent turns review off: the next valid output is published by the run.
    shop.store.set_policy(tracker, {'publish': 'automatic'}, expected_revision=1)
    more_recordings(shop, date(2026, 10, 12))
    assert manual(shop)['scripts'][0]['output'] == 'published' and stored(shop)['revision'] == 3


@pytest.mark.parametrize('policy,expected', [('never', []), ('on_failure', ['failed']), ('on_change', ['published']),
                                             ('every_run', ['published', 'unchanged', 'failed'])])
def test_the_wake_policy_enqueues_exactly_what_it_says(shop, policy, expected):
    scripts = shop.scripts
    store, agent = agent_for(shop)
    go_live(shop, agent_id=agent['id'], wake=policy)
    seen = []
    def step(label, run):
        entry = run['scripts'][0]
        before = len(seen)
        seen.extend([label] * (len(wakes(shop)) - sum(1 for _ in seen)))
        assert (entry['wake'] or {}).get('queued', False) == (len(seen) > before), (label, entry['wake'])
        return entry
    assert step('published', manual(shop))['output'] == 'published'
    assert step('reused', manual(shop))['status'] == 'reused'
    # The events change and the script's output stays the same: it ran, and nothing on the surface changed.
    shop.box.answer = {'status': 'succeeded', 'log': '', 'results': [{'ok': True, 'output': {k: v for k, v in entry.items()}} for entry in [
        {'value': stored(shop)['tile']['value'], 'unit': 'events', 'state': 'ready', 'needs': None, 'summary': stored(shop)['tile']['summary'],
         'series': stored(shop)['tile']['series'], 'events': [{'id': row['id']} for row in stored(shop)['tile']['events']],
         'canvas': {'rows': [{'id': row['id']} for row in stored(shop)['canvas']['rows']], 'total_rows': stored(shop)['canvas']['total_rows'],
                    'by_type': stored(shop)['canvas']['by_type']}}] * 4]}
    import retail_records as records
    records.register_zone(shop.company, shop.location, records.cameras(shop.company, shop.location)[0]['id'], 'till', 'checkout', 'Till', data_basis='operator_entry')
    assert step('unchanged', manual(shop))['output'] == 'unchanged'
    shop.box.answer = {'status': 'failed', 'error': scripts.fault('script_stopped')}
    more_recordings(shop, date(2026, 10, 12))
    assert step('failed', manual(shop))['status'] == 'failed'
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_busy')}
    more_recordings(shop, date(2026, 10, 13))
    assert step('unavailable', manual(shop))['status'] == 'unavailable'
    assert seen == expected
    queued = wakes(shop)
    assert len(queued) == len(expected) and all(w['agent_id'] == agent['id'] and w['status'] == 'pending' and len(w['text']) < 700 for w in queued)
    tracker = tracker_of(shop)
    assert all(w['requires_input'] is False and w['source_scope']['tracker_generation'] == tracker['generation_id']
        and w['source_scope']['camera_ids'] == sorted(camera['id'] for camera in records.cameras(shop.company, shop.location))
        for w in queued)
    if policy == 'on_change':
        assert queued[0]['text'].startswith('Pickups has a new output, revision 1: ')


@pytest.mark.parametrize('policy,expected', [('on_change', ['published', 'paused']), ('never', [])])
def test_the_run_that_pauses_a_script_wakes_its_agent_under_every_policy_but_never(shop, policy, expected):
    """Under on_change a stopped run is silent, and a script that stops for good would stay unseen.
    The run that pauses it wakes the agent, with the reason; `never` keeps its word."""
    scripts = shop.scripts
    store, agent = agent_for(shop)
    tracker = go_live(shop, agent_id=agent['id'], wake=policy)
    assert manual(shop)['scripts'][0]['output'] == 'published'
    shop.box.answer = {'status': 'failed', 'error': scripts.fault('script_stopped')}
    queued = []
    for attempt in (1, 2, 3):
        more_recordings(shop, date(2026, 10, 10 + attempt))
        queued.append((manual(shop)['scripts'][0]['wake'] or {}).get('queued', False))
    assert queued == [False, False, policy == 'on_change'] and scripts.health_of(shop.store.head(tracker))['status'] == 'paused'
    texts = [wake['text'] for wake in wakes(shop)]
    assert len(texts) == len(expected) and (not texts or 'The script is paused after 3 stopped runs in a row.' in texts[-1])


def test_a_wake_stays_inside_the_queues_own_rules(shop):
    """A tracker whose agent is paused, or that is managed from a conversation, queues nothing: the queue's rule is kept and named."""
    scripts = shop.scripts
    store, agent = agent_for(shop)
    go_live(shop, agent_id=agent['id'], wake='every_run')
    store.pause(agent['id'])
    entry = manual(shop)['scripts'][0]
    assert entry['status'] == 'succeeded' and entry['wake'] == {'queued': False, 'reason': 'Use the active, reviewed agent revision within its execution allowance.'}
    assert wakes(shop) == [] and stored(shop)['revision'] == 1
    go_live(shop, key='others', wake='every_run')
    more_recordings(shop)
    entries = {s['key']: s for s in manual(shop)['scripts']}
    assert entries['others']['wake'] == {'queued': False, 'reason': scripts.NO_AGENT} and wakes(shop) == []


def revision_copy(shop, output, revision):
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        row = db.execute("SELECT body FROM records WHERE kind='tracker_output_revision' AND id=?", (f"{output['id']}-r{revision}",)).fetchone()
    return json.loads(row[0]) if row else None


def test_text_slots_are_written_by_the_agent_and_stay_with_their_facts(shop):
    scripts = shop.scripts
    store, agent = agent_for(shop)
    tracker = go_live(shop, WITH_TEXT, agent_id=agent['id'], wake='on_change')
    assert manual(shop)['scripts'][0]['output'] == 'published'
    output = stored(shop)
    slot = output['canvas']['texts'][0]
    assert set(slot) == {'slot', 'facts', 'text', 'written_at'} and slot['slot'] == 'memo' and slot['text'] is None and slot['facts']['count'] == output['tile']['value']
    assert 'It marks 1 text slot to write with write_tracker_text and output_revision 1: memo.' in wakes(shop)[0]['text']
    assert all('texts' not in reading['canvas'] for reading in page(shop)['core_trackers'][9]['periods'].values())
    with pytest.raises(ValueError) as unnamed:
        scripts.write_text(shop.company, tracker, 'memo', 'Hello')
    assert str(unnamed.value) == scripts.TEXT_FOR, 'a text names the output revision or the proposal it was written for'
    with pytest.raises(ValueError, match='Choose one of the text slots this output marks: memo.'):
        scripts.write_text(shop.company, tracker, 'headline', 'Hello', output_revision=1)
    with pytest.raises(ValueError, match='earlier output'):
        scripts.write_text(shop.company, tracker, 'memo', 'Hello', output_revision=7)
    with pytest.raises(ValueError):
        scripts.write_text(shop.company, tracker, 'memo', '   ', output_revision=1)
    assert stored(shop) == output
    written = scripts.write_text(shop.company, tracker, 'memo', '  A quiet morning at the shelf.  ', output_revision=1)
    assert written == {'stored_with': 'output', 'output_revision': 2, 'slot': 'memo'}
    after = stored(shop)
    assert after['revision'] == after['tile']['revision'] == after['canvas']['revision'] == 2
    assert after['canvas']['texts'][0]['text'] == 'A quiet morning at the shelf.' and after['canvas']['texts'][0]['written_at']
    assert after['content_sha256'] == scripts.content_of(after['tile'], after['canvas']) != output['content_sha256']
    # The revision the text was written for stays as it was published; the text is the output's next revision.
    assert revision_copy(shop, after, 1) == {**output, 'id': output['id'] + '-r1'} and revision_copy(shop, after, 2)['canvas']['texts'][0]['text'] == 'A quiet morning at the shelf.'
    # The same words for the same facts change nothing; the revision first named still names those facts.
    assert scripts.write_text(shop.company, tracker, 'memo', 'A quiet morning at the shelf.', output_revision=1) == {'stored_with': 'output', 'output_revision': 2, 'slot': 'memo'}
    assert stored(shop) == after
    # The tracker's row of the page follows without a pass, with the readings of the output it stands on.
    assert scripts.show_on_page(shop.company, tracker) is True and shop.runs.state(*shop.key)['requested_at'] is None
    shown = page(shop)
    assert shown['core_tracker_canvases']['pickups']['texts'][0]['text'] == 'A quiet morning at the shelf.'
    assert shown['core_trackers'][9]['revision'] == 2 and set(shown['core_trackers'][9]['periods']) == {'week', 'month', 'year'}
    # The next pass finds the same facts: the result is reused, the text stays and the agent sleeps on.
    assert manual(shop)['scripts'][0]['status'] == 'reused' and page(shop)['core_tracker_canvases']['pickups']['texts'][0]['text'] == 'A quiet morning at the shelf.'
    assert len(wakes(shop)) == 1 and stored(shop) == after
    # New events change the facts: the slot is open again and the agent is woken once, with the revision to name.
    more_recordings(shop)
    assert manual(shop)['scripts'][0]['output'] == 'published'
    newest = stored(shop)
    assert newest['revision'] == 3 and newest['canvas']['texts'][0]['text'] is None and len(wakes(shop)) == 2
    assert 'output_revision 3: memo.' in wakes(shop)[-1]['text'] and newest['canvas']['texts'][0]['facts'] != slot['facts']
    # A text composed from the earlier facts is refused: its words belong to the output it names.
    for earlier in (1, 2):
        with pytest.raises(ValueError) as moved:
            scripts.write_text(shop.company, tracker, 'memo', f"{slot['facts']['count']} pickups today.", output_revision=earlier)
        assert str(moved.value) == scripts.TEXT_MOVED
    assert stored(shop) == newest and revision_copy(shop, newest, 3) == {**newest, 'id': newest['id'] + '-r3'}


def test_a_text_and_a_pass_write_one_after_the_other(shop):
    """A run's publish and a written text each read the output that stands and write its next
    revision. Each does that under the tracker's own lock and in one transaction, so a pass that
    publishes while a text is on its way keeps both: the newer facts refuse the earlier text."""
    import threading
    scripts = shop.scripts
    tracker = go_live(shop, WITH_TEXT)
    manual(shop)
    first = stored(shop)
    # While a writer holds the tracker's output, a text waits for it.
    more_recordings(shop)
    gate = scripts.writing(tracker)
    gate.acquire()
    answer = []
    def write():
        try:
            answer.append(scripts.write_text(shop.company, tracker, 'memo', 'Seventeen pickups today.', output_revision=1))
        except ValueError as exc:
            answer.append(str(exc))
    writer = threading.Thread(target=write)
    writer.start()
    writer.join(.5)
    assert writer.is_alive() and stored(shop) == first, 'the text waits for the writer that holds the output'
    gate.release()
    writer.join(10)
    assert answer == [{'stored_with': 'output', 'output_revision': 2, 'slot': 'memo'}]
    # Now the other order: the pass publishes first, and the text written for the earlier facts is refused whole.
    assert manual(shop)['scripts'][0]['output'] == 'published'
    newer = stored(shop)
    assert newer['revision'] == 3 and newer['canvas']['texts'][0]['text'] is None and newer['tile']['value'] > first['tile']['value']
    with pytest.raises(ValueError, match='earlier output'):
        scripts.write_text(shop.company, tracker, 'memo', 'Seventeen pickups today.', output_revision=2)
    assert stored(shop) == newer and scripts.health_of(shop.store.head(tracker))['last_good']['output_revision'] == 3
    assert manual(shop)['scripts'][0]['status'] == 'reused' and stored(shop) == newer and page(shop)['core_trackers'][9]['value'] == newer['tile']['value']


def test_text_written_for_a_waiting_proposal_is_published_with_it(shop):
    scripts = shop.scripts
    store, agent = agent_for(shop)
    tracker = go_live(shop, WITH_TEXT, agent_id=agent['id'], publish='review', wake='on_change')
    first = manual(shop)['scripts'][0]
    proposal = shop.store.proposal(tracker)
    assert proposal['texts'][0]['text'] is None and stored(shop) is None and first['wake']['queued'] is True
    assert f"write_tracker_text and proposal_id {proposal['proposal_id']}: memo." in wakes(shop)[0]['text']
    with pytest.raises(ValueError) as moved:
        scripts.write_text(shop.company, tracker, 'memo', 'Words.', proposal_id='proposal-' + '0' * 16)
    assert str(moved.value) == scripts.PROPOSAL_MOVED
    assert scripts.write_text(shop.company, tracker, 'memo', 'Written before approval.', proposal_id=proposal['proposal_id']) == {
        'stored_with': 'proposal', 'proposal_id': proposal['proposal_id'], 'slot': 'memo'}
    assert stored(shop) is None
    # The same computed output on changed inputs is the same proposal: its name follows what the script computed,
    # the text written into it stays, and the agent is woken once for it.
    import retail_records as records
    constant = {'status': 'succeeded', 'log': '', 'results': [{'ok': True, 'output': {**entry, 'texts': texts}} for entry, texts in zip(
        [{'value': proposal['value'], 'unit': 'events', 'state': 'ready', 'needs': None, 'summary': proposal['summary'], 'series': [],
          'events': [{'id': row['id']} for row in proposal['bundle']['day']['accepted']['events']],
          'canvas': {'rows': [{'id': row['id']} for row in proposal['bundle']['day']['accepted']['canvas']['rows']],
                     'total_rows': proposal['bundle']['day']['accepted']['canvas']['total_rows'], 'by_type': []}}] * 4,
        [proposal['bundle']['day']['accepted']['texts'], [], [], []])]}
    shop.box.answer = constant
    records.register_zone(shop.company, shop.location, records.cameras(shop.company, shop.location)[0]['id'], 'till', 'checkout', 'Till', data_basis='operator_entry')
    again = manual(shop)['scripts'][0]
    assert (again['status'], again['output'], again['proposal_id'], again['wake']) == ('succeeded', 'unchanged', proposal['proposal_id'], None)
    assert shop.store.proposal(tracker)['texts'][0]['text'] == 'Written before approval.' and len(wakes(shop)) == 1
    shop.box.answer = None
    output = scripts.approve_output(shop.company, tracker, proposal['proposal_id'])
    assert output['canvas']['texts'][0]['text'] == 'Written before approval.' and output['revision'] == 1
    assert output['camera_ids'] == proposal['camera_ids'] != [], 'a proposal names every camera of its location, like the output it becomes'


def test_a_test_publishes_nothing_and_is_a_row_of_the_run_history(shop):
    from core_trackers import add_tracker
    import tracker_runner as runner
    scripts = shop.scripts
    shop.sweep()
    last = shop.runs.last_run('peter', shop.location)
    tracker = add_tracker(shop.company, shop.location, 'pickups', 'Pickups', 'Items a visitor picks up.', event_types=['pick_up'])
    shop.store.save(tracker, COUNT, expected_revision=0)
    before = runner.current_outputs(shop.state, *shop.key)
    answer = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    assert answer['status'] == 'passed' and answer['revision'] == 1 and answer['error'] is None and answer['contract_errors'] == []
    assert answer['preview']['tile']['value'] == answer['preview']['canvas']['total_rows'] > 0 and len(answer['preview']['canvas']['rows']) <= 20
    assert 'revision' not in answer['preview']['tile'] and [r['period'] for r in answer['readings']] == ['day', 'week', 'month', 'year']
    assert answer['log'].startswith('counted') and answer['input']['counts']['events'] > 0 and answer['limits'] == {'seconds': 30, 'output_bytes': 114688, 'memory_mb': 384}
    assert runner.current_outputs(shop.state, *shop.key) == before, 'a test stores zero outputs'
    by_id = lambda run_id: next(run for run in shop.runs.runs(*shop.key, limit=50) if run['id'] == run_id)
    row = by_id(answer['run_id'])
    assert (row['trigger'], row['status'], row['error']) == ('test', 'completed', None)
    assert row['scripts'] == [{'key': 'pickups', 'tracker_id': tracker['id'], 'script_revision': 1, 'trigger': 'test', 'status': 'passed',
                               'seconds': answer['seconds'], 'error': None, 'output': None, 'output_revision': None, 'proposal_id': None, 'wake': None, 'reason': None}]
    assert shop.runs.last_run('peter', shop.location) == last, 'the location’s last pass stands; a test is one tracker’s'
    assert shop.runs.state(*shop.key)['failures'] == 0
    tested = shop.store.head(tracker)['tested']
    assert tested['revision'] == 1 and tested['status'] == 'passed' and tested['run_id'] == answer['run_id']
    # A failing test: the error, its line and the log, as data in the same answer.
    shop.store.save(tracker, 'def run(input):\n    print("about to stop")\n    return input["missing"]\n', expected_revision=1)
    failed = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=2, sandbox=shop.box))
    assert failed['status'] == 'failed' and failed['error']['code'] == 'script_error' and failed['error']['line'] == 3
    assert failed['error']['message'] == "KeyError: 'missing'" and 'tracker.py", line 3' in failed['error']['traceback'] and set(failed['log'].splitlines()) == {'about to stop'}
    assert failed['preview'] is None and by_id(failed['run_id'])['status'] == 'failed'
    assert by_id(failed['run_id'])['error'] == 'The script stopped at line 3 in its day reading with script_error. get_tracker_script shows the message.'
    assert by_id(failed['run_id'])['scripts'][0]['error'] == {'code': 'script_error', 'line': 3, 'period': 'day', 'message': 'The script raised an error.'}
    with pytest.raises(ValueError, match='Test this revision'):
        scripts.publish_script(shop.company, tracker, expected_revision=2)
    with pytest.raises(ValueError, match='current revision'):
        asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    # An output that differs from the contract is rejected, path by path.
    shop.store.save(tracker, COUNT.replace('"by_type"', '"label"'), expected_revision=2)
    rejected = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=3, sandbox=shop.box))
    assert rejected['status'] == 'rejected' and rejected['contract_errors'][0] == {'path': 'day: canvas.label', 'message': 'The host stamps this name. Give your canvas key another name.'}
    # A sandbox that is off takes no job: the test says so and the revision stays untested.
    off = LocalSandbox(shop.box.tmp_path, enabled=False)
    off.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_offline')}
    away = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=3, sandbox=off))
    assert away['status'] == 'unavailable' and away['error']['code'] == 'sandbox_offline' and shop.store.head(tracker)['tested']['status'] == 'rejected'
    assert away['run_id'] is None, 'a test the sandbox never took leaves the run history as it is'
    assert runner.current_outputs(shop.state, *shop.key) == before


def test_a_tracker_keeps_a_days_number_of_sandbox_jobs(shop, monkeypatch):
    scripts = shop.scripts
    monkeypatch.setattr(scripts, 'JOBS_PER_DAY', 2)
    tracker = go_live(shop)                       # one job: its test
    assert manual(shop)['scripts'][0]['status'] == 'succeeded'   # the second
    more_recordings(shop)
    held = manual(shop)['scripts'][0]
    assert (held['status'], held['reason'], held['retry']) == ('waiting', f'{scripts.DAILY_LIMIT} {scripts.RETRY_NEXT}', False) and len(shop.box.jobs) == 2
    limited = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    assert limited['status'] == 'unavailable' and limited['error']['code'] == 'daily_limit' and len(shop.box.jobs) == 2
    # The day's number counts the jobs the sandbox took: a busy or absent sandbox costs the tracker nothing.
    monkeypatch.setattr(scripts, 'JOBS_PER_DAY', 3)
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_busy')}
    for _ in range(3):
        assert asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))['error']['code'] == 'sandbox_busy'
    assert shop.store.head(tracker)['jobs']['count'] == 2
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_cleanup'), 'started': True}
    assert asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))['status'] == 'unavailable'
    assert shop.store.head(tracker)['jobs']['count'] == 3, 'a job the broker took counts, however it ended'
    off = LocalSandbox(shop.box.tmp_path, enabled=False, loop=shop.box.loop)
    more_recordings(shop, date(2026, 10, 12))
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 120
    import tracker_runner as runner
    away = runner.tracker_pass(shop.root, shop.root, clock=lambda: shop.now[0], pending_since={}, custom=scripts.sandbox_processor(off))[0]['scripts'][0]
    assert (away['status'], away['error']['code'], away['reason']) == ('unavailable', 'sandbox_offline', f"{scripts.FAULTS['sandbox_offline']} {scripts.RETRY_NEXT}")
    assert off.jobs == [] and shop.store.head(tracker)['jobs']['count'] == 3


def test_removing_a_tracker_takes_its_script_records_with_it(shop):
    from core_trackers import remove_tracker
    tracker = go_live(shop, WITH_TEXT, publish='review')
    manual(shop)
    shop.store.save(tracker, COUNT, expected_revision=1)
    kinds = lambda: {row[0] for row in sqlite3.connect(shop.state / 'location-intelligence.sqlite').execute(
        "SELECT kind FROM records WHERE kind LIKE 'tracker_script%' OR kind='tracker_output_proposal'")}
    assert kinds() == {'tracker_script', 'tracker_script_revision', 'tracker_output_proposal'}
    remove_tracker(shop.company, shop.location, 'pickups')
    assert kinds() == set()


# ── What two reviewers broke, kept as it was fixed ─────────────────────────────────────────────

def test_a_tracker_added_again_under_a_removed_key_starts_with_nothing(shop):
    """A tracker's id follows from its key, and a Tracking Agent's key from its agent. What a
    removed tracker published and ran goes with it, so the next record under that key is new."""
    import tracker_runner as runner
    from core_trackers import add_tracker, remove_tracker
    go_live(shop)
    manual(shop)
    more_recordings(shop)
    manual(shop)
    mine = lambda: [tuple(row) for row in sqlite3.connect(shop.state / 'location-intelligence.sqlite').execute(
        "SELECT kind,COUNT(*) FROM records WHERE json_extract(body,'$.tracker_key')='pickups' GROUP BY kind ORDER BY kind")]
    assert dict(mine())['tracker_output'] == 1 and dict(mine())['tracker_output_revision'] == 2 and dict(mine())['tracker_script_readings'] == 1
    history = lambda: [(run['trigger'], [part for part in ('outputs', 'errors', 'skipped', 'scripts') if any(row.get('key') == 'pickups' for row in run[part])])
                       for run in shop.runs.runs(*shop.key, limit=50)]
    assert sum(1 for trigger, parts in history() if parts) == 3 and ('test', ['scripts']) in history()
    passes = sum(1 for trigger, _ in history() if trigger != 'test')
    remove_tracker(shop.company, shop.location, 'pickups')
    assert mine() == [], 'its output and each kept revision go with the tracker, its script and its readings'
    assert [o for o in runner.home_revision(shop.state, 'peter', shop.location)['outputs'] if o['key'] == 'pickups'] == []
    assert all(parts == [] for _, parts in history()) and all(trigger != 'test' for trigger, _ in history())
    assert sum(1 for trigger, _ in history() if trigger != 'test') == passes, 'the location’s passes stay, each less the tracker’s entries'
    # The same key again, as another tracker: an empty record.
    again = add_tracker(shop.company, shop.location, 'pickups', 'Pickups, again', 'Items a visitor puts back.', event_types=['put_back'])
    assert stored(shop) is None and shop.store.head(again) is None and shop.store.proposal(again) is None
    context = SimpleNamespace(state=shop.state, owner='peter', dataset='retail-demo')
    assert shop.scripts._script_runs(context, again) == []
    run = manual(shop)
    tile = next(t for t in page(shop)['core_trackers'] if t['key'] == 'pickups')
    assert (tile['label'], tile['value'], tile['needs']) == ('Pickups, again', None, 'Needs first run') and run['status'] == 'completed'


def test_a_script_has_its_run_for_each_local_day_and_a_job_that_waited_is_run_again(shop):
    """With event runs off a script still runs once for each local day of its location. The run is
    the script's own: the day's first pass runs it whatever started that pass, and a job the
    sandbox had to turn away is owed until a later pass has run it."""
    scripts = shop.scripts
    tracker = go_live(shop)
    seen = []
    for number in (1, 2, 3):   # three days whose recordings land before the day's first sweep
        more_recordings(shop, date(2026, 10, 10 + number))
        shop.now[0] += 86400
        run = shop.sweep()[0]
        seen.append((run['trigger'], run['day'], [(s['status'], s['output']) for s in run['scripts']]))
    assert seen == [('events', f'2026-10-{11 + number}', [('succeeded', 'published')]) for number in (1, 2, 3)]
    assert stored(shop)['revision'] == 3 and len(shop.box.jobs) == 4, 'the test, then one job a day'
    # A day whose pass finds both sandbox workers in use: the run stays owed, and the service asks for its pass.
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_busy')}
    shop.now[0] += 86400
    busy = shop.sweep()[0]
    assert busy['trigger'] == 'day' and busy['status'] == 'completed' and busy['page_published']
    assert [(s['status'], s['reason'], s['retry']) for s in busy['scripts']] == [('unavailable', f"{scripts.FAULTS['sandbox_busy']} {scripts.RETRY_SOON}", True)]
    assert scripts.health_of(shop.store.head(tracker))['day'] == {'day': '2026-10-15', 'done': False}
    state = shop.runs.state(*shop.key)
    assert state['requested_only'] == ['pickups'] and state['failures'] == 0 and state['processed_day'] == '2026-10-15'
    # The pass the service asked for runs that script alone.
    shop.box.answer = None
    shop.now[0] += 120
    again = shop.sweep()[0]
    assert (again['trigger'], again['only'], [o['key'] for o in again['outputs']]) == ('manual', ['pickups'], ['pickups'])
    assert [(s['status'], s['retry']) for s in again['scripts']] == [('reused', False)] or again['scripts'][0]['status'] == 'succeeded'
    assert scripts.health_of(shop.store.head(tracker))['day'] == {'day': '2026-10-15', 'done': True} and shop.sweep() == []
    # The service asks a bounded number of times a day; past that the run waits for the location's next pass.
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_turn')}
    asked = []
    for attempt in range(scripts.RETRIES_PER_DAY + 2):
        more_recordings(shop, date(2026, 10, 15 + attempt))
        shop.runs.request(*shop.key, by='peter')
        shop.now[0] += 120
        entry = shop.sweep()[0]['scripts'][0]
        asked.append(entry['retry'])
        while shop.runs.state(*shop.key)['requested_at'] is not None:   # each pass the service asked for meets the same sandbox
            shop.now[0] += 120
            asked.append(shop.sweep()[0]['scripts'][0]['retry'])
    assert sum(asked) == scripts.RETRIES_PER_DAY - 1 and asked[-1] is False, 'one request was used for the busy day pass; the rest are granted once each'
    assert entry['reason'] == f"{scripts.FAULTS['sandbox_turn']} {scripts.RETRY_NEXT}" and shop.runs.state(*shop.key)['failures'] == 0
    # A fault that waits for the operator or for tomorrow starts no pass of the service's own.
    for code in ('sandbox_offline', 'sandbox_maintenance', 'sandbox_daily_limit', 'company_daily_limit', 'sandbox_history_share'):
        assert code not in scripts.TRANSIENT and code in scripts.FAULTS
    # A later pass of the same local day runs what is owed, on new events too. A sandbox that is being
    # commissioned is for the operator: the service asks for no pass of its own.
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_offline')}
    more_recordings(shop, date(2026, 10, 21))
    shop.now[0] += 86400
    first = shop.sweep()[0]
    assert first['trigger'] == 'events' and [(s['status'], s['reason'], s['retry']) for s in first['scripts']] == [
        ('unavailable', f"{scripts.FAULTS['sandbox_offline']} {scripts.RETRY_NEXT}", False)]
    assert scripts.health_of(shop.store.head(tracker))['day'] == {'day': '2026-10-16', 'done': False} and shop.runs.state(*shop.key)['requested_at'] is None
    shop.box.answer = None
    more_recordings(shop, date(2026, 10, 22))
    shop.now[0] += 600
    owed = shop.sweep()[0]
    assert owed['trigger'] == 'events' and [(s['status'], s['output']) for s in owed['scripts']] == [('succeeded', 'published')]
    more_recordings(shop, date(2026, 10, 23))
    shop.now[0] += 600
    assert [(s['status'], s['reason']) for s in shop.sweep()[0]['scripts']] == [('waiting', scripts.EVENT_RUNS_WAIT)]


def test_a_daily_time_holds_the_days_pass_and_the_scripts_run_for_it(shop):
    """A location with a daily time has its day's pass at that time. New events before it start a
    pass that recomputes the trackers and leaves the day to that pass, where the script has its run."""
    scripts = shop.scripts
    tracker = go_live(shop)
    shop.now[0] += 120
    assert shop.sweep()[0]['scripts'][0]['status'] == 'succeeded'
    shop.runs.set_policy(*shop.key, {'daily_at': '18:00'}, expected_revision=1)
    morning = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc).timestamp()
    seen = []
    for at, recordings in ((morning - 8.5 * 3600, None), (morning, date(2026, 10, 11)), (morning + 360, None), (morning + 9.1 * 3600, None), (morning + 14 * 3600, None)):
        if recordings:
            more_recordings(shop, recordings)
        shop.now[0] = at
        seen.append([(run['trigger'], [(s['status'], s['reason']) for s in run['scripts']]) for run in shop.sweep()])
    assert seen[0] == [] and seen[2] == [] and seen[4] == []
    assert seen[1] == [('events', [('waiting', scripts.DAILY_TIME_WAIT)])], 'before the daily time the script’s run waits for the day’s pass'
    assert shop.runs.state(*shop.key)['processed_day'] == '2026-10-11' or seen[3], 'the events pass left the day to the daily pass'
    assert [(trigger, [status for status, _ in entries]) for trigger, entries in seen[3]] == [('day', ['succeeded'])]
    assert shop.runs.state(*shop.key)['processed_day'] == '2026-10-12' and stored(shop)['revision'] == 2
    assert scripts.health_of(shop.store.head(tracker))['day'] == {'day': '2026-10-12', 'done': True}
    # After the daily time a pass on new events stands for the day, as it does at a location with no daily time.
    import tracker_runner as runner
    assert runner.at_daily_time(None, '18:00', morning) is False and runner.at_daily_time(None, '18:00', morning + 9 * 3600) is True
    assert runner.at_daily_time('Pacific/Auckland', '06:00', datetime(2026, 10, 12, 17, 5, tzinfo=timezone.utc).timestamp()) is True
    assert runner.at_daily_time(None, None, morning) is True


def test_a_script_job_costs_the_pass_and_the_page_nothing(shop, monkeypatch):
    """The pass's own work ends with its page data. Script jobs run after it in a time of their
    own: a slow job, a held sandbox turn or a job that the script time has no room for leaves the
    pass completed, the page published and the location's schedule as a pass alone would."""
    import tracker_runner as runner
    scripts = shop.scripts
    tracker = go_live(shop)
    manual(shop)
    good, before = stored(shop), page(shop)
    order = []
    real_publish = __import__('import_control_room_bundle').publish_core_trackers
    def publish(*args, **kwargs):
        order.append('page')
        return real_publish(*args, **kwargs)
    monkeypatch.setattr('import_control_room_bundle.publish_core_trackers', publish)
    class Slow(LocalSandbox):
        async def run(self, **job):
            order.append('job')
            await asyncio.sleep(3)
            return await super().run(**job)
    slow = Slow(shop.box.tmp_path, loop=shop.box.loop)
    more_recordings(shop)
    shop.now[0] += 120
    started = time.monotonic()
    run = runner.run_location(shop.company, shop.location, shop.runs, trigger='events', stamp=runner.input_stamp(shop.state, *shop.key),
                              day='2026-10-12', budget_seconds=2, custom=scripts.sandbox_processor(slow))
    assert time.monotonic() - started >= 3 and order == ['page', 'job'], 'the page is published before the script job starts'
    assert (run['status'], run['error'], run['page_published']) == ('completed', None, True) and run['wall_ms'] < 2000, 'the run row’s time is the pass’s own work'
    assert [(s['status'], s['output']) for s in run['scripts']] == [('succeeded', 'published')]
    state = shop.runs.state(*shop.key)
    assert state['failures'] == 0 and state['processed_stamp'] == runner.input_stamp(shop.state, *shop.key) and not state['running']
    assert state['next_after'] - shop.now[0] <= 60, 'the next pass waits the location’s interval, whatever the script took'
    newer = stored(shop)
    assert newer['revision'] == good['revision'] + 1 and [o['key'] for o in run['outputs']] == KEYS + ['pickups'] and run['outputs'][-1]['changed'] is True
    shown = page(shop)['core_trackers'][9]
    assert shown['revision'] == newer['revision'] and shown['value'] == newer['tile']['value'] > before['core_trackers'][9]['value'], 'the script’s own row of the page follows its run'
    # A pass whose script time has no room for a job leaves the job to another pass, which the service asks for.
    monkeypatch.setattr(scripts, 'SCRIPT_PASS_SECONDS', scripts.MIN_JOB_SECONDS - 1)
    more_recordings(shop, date(2026, 10, 12))
    waiting = manual(shop)
    assert waiting['status'] == 'completed' and waiting['page_published']
    assert [(s['status'], s['reason'], s['retry']) for s in waiting['scripts']] == [('waiting', f'{scripts.BUDGET_WAIT} {scripts.RETRY_SOON}', True)]
    assert shop.runs.state(*shop.key)['requested_only'] == ['pickups'] and shop.store.head(tracker)['jobs']['count'] == 3, 'a job that never started counts for nothing'
    monkeypatch.undo()
    shop.now[0] += 120
    assert [(s['status'], s['output']) for s in shop.sweep()[0]['scripts']] == [('succeeded', 'published')]
    # A sandbox that keeps the host waiting past a job's time is asked to stop; the script is blameless and the job is counted.
    monkeypatch.setattr(scripts, 'MIN_JOB_SECONDS', 11)
    monkeypatch.setattr(scripts, 'JOB_SECONDS', 1)
    monkeypatch.setattr(scripts, 'SLOT_WAIT_SECONDS', 0)
    monkeypatch.setattr(scripts, 'HOST_GRACE_SECONDS', .3)
    stops = []
    class Hung(LocalSandbox):
        async def run(self, *, cancelled=None, **job):
            while not cancelled.is_set():
                await asyncio.sleep(.02)
            stops.append(job['seconds'])
            return {'status': 'failed', 'error': scripts.fault('stopped'), 'seconds': 1.3, 'started': True}
    hung = Hung(shop.box.tmp_path, loop=shop.box.loop)
    more_recordings(shop, date(2026, 10, 13))
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 120
    jobs = shop.store.head(tracker)['jobs']['count']
    late = runner.tracker_pass(shop.root, shop.root, clock=lambda: shop.now[0], pending_since={}, custom=scripts.sandbox_processor(hung))[0]
    assert late['status'] == 'completed' and late['page_published'] and stops == [1]
    assert [(s['status'], s['error']['code'], s['retry']) for s in late['scripts']] == [('unavailable', 'sandbox_busy', True)]
    health = scripts.health_of(shop.store.head(tracker))
    assert health['consecutive_failures'] == 0 and health['paused'] is None and shop.store.head(tracker)['jobs']['count'] == jobs + 1


def test_an_output_outside_the_contract_is_a_stopped_run_of_the_script(shop):
    """A whole number of any size in an output is a rejected run: recorded, counted toward the
    pause and woken for, like every stopped run, and the page is published as it should be."""
    scripts = shop.scripts
    store, agent = agent_for(shop)
    tracker = go_live(shop, agent_id=agent['id'], wake='on_failure')
    manual(shop)
    good = stored(shop)
    huge = {'value': 10 ** 400, 'unit': 'events', 'state': 'ready', 'needs': None, 'summary': 'Many.', 'series': [], 'events': [],
            'canvas': {'rows': [], 'total_rows': 0, 'share': 10 ** 400}}
    shop.box.answer = {'status': 'succeeded', 'log': '', 'results': [{'ok': True, 'output': huge}] * 4}
    for attempt in (1, 2, 3):
        more_recordings(shop, date(2026, 10, 10 + attempt))
        run = manual(shop)
        entry = run['scripts'][0]
        assert run['status'] == 'completed' and run['page_published'] and (entry['status'], entry['error']['code']) == ('rejected', 'output_contract')
        assert run['errors'] == [{'key': 'pickups', 'error': 'The script stopped in its day reading with output_contract. get_tracker_script shows the message.'}]
        assert entry['wake']['queued'] is True and stored(shop) == good
        health = scripts.health_of(shop.store.head(tracker), words=True)
        assert health['consecutive_failures'] == attempt and health['last_run']['message'].startswith('day: value: value is a finite number')
    assert health['status'] == 'paused' and len(wakes(shop)) == 3 and shop.store.head(tracker)['jobs']['count'] == 5
    assert page(shop)['core_trackers'][9]['revision'] == good['revision']


def test_review_keeps_the_week_the_month_and_the_year_with_the_calendar(shop):
    """In review mode an unchanged day leaves the published output as it is. Its week, month and
    year still end on the location's current day, as the Core tiles beside it do."""
    scripts = shop.scripts
    tracker = go_live(shop, publish='review', wake='never')
    manual(shop)
    scripts.approve_output(shop.company, tracker, shop.store.proposal(tracker)['proposal_id'])
    week = lambda: scripts.saved_readings(shop.company, shop.location, [tracker])['pickups']['week']['tile']['period']['label']
    core = lambda: next(tile for tile in page(shop)['core_trackers'] if tile['key'] == 'visitors')['periods']['week']['period_label']
    manual(shop)
    assert week() == core() == '5 Oct to 11 Oct 2026'
    shop.now[0] += 3 * 86400
    run = shop.sweep()[0]
    assert run['trigger'] == 'day' and [(s['status'], s['output']) for s in run['scripts']] == [('succeeded', 'unchanged')]
    assert shop.store.proposal(tracker) is None and stored(shop)['revision'] == 1
    assert week() == core() == '8 Oct to 14 Oct 2026', 'the readings moved with the calendar while the day’s output stood'
    assert next(tile for tile in page(shop)['core_trackers'] if tile['key'] == 'pickups')['periods']['week']['period_label'] == '8 Oct to 14 Oct 2026'


def test_a_test_the_turn_stopped_or_the_sandbox_turned_away_leaves_the_history_and_the_last_test(shop):
    scripts = shop.scripts
    tracker = go_live(shop)
    passes = lambda: [run['id'] for run in shop.runs.runs(*shop.key, limit=200) if run['trigger'] != 'test']
    tests = lambda: [run for run in shop.runs.runs(*shop.key, limit=200) if run['trigger'] == 'test']
    before, tested, jobs = passes(), shop.store.head(tracker)['tested'], shop.store.head(tracker)['jobs']['count']
    assert tested['status'] == 'passed' and len(tests()) == 1
    # The person stops the turn while the job runs: the script is blameless and its passing test stands.
    shop.box.answer = {'status': 'failed', 'error': scripts.fault('stopped'), 'started': True}
    stopped = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    assert (stopped['status'], stopped['error']['code'], stopped['run_id']) == ('unavailable', 'stopped', None)
    assert stopped['next_step'] == 'Test this revision again with test_tracker_script.' and shop.store.head(tracker)['tested'] == tested
    assert shop.store.head(tracker)['jobs']['count'] == jobs + 1, 'the broker took the job, so it counts'
    scripts.publish_script(shop.company, tracker, expected_revision=1)
    # A sandbox that takes no job: 205 tests leave every pass row where it was and cost the tracker nothing.
    shop.box.answer = {'status': 'unavailable', 'error': scripts.fault('sandbox_offline')}
    for _ in range(205):
        away = asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    assert (away['status'], away['run_id']) == ('unavailable', None) and passes() == before and len(tests()) == 1
    assert shop.store.head(tracker)['jobs']['count'] == jobs + 1 and shop.runs.last_run('peter', shop.location)['id'] == before[0]
    # Tests that run keep their own rows, a few per tracker, beside the location's passes.
    import tracker_runner as runner
    shop.box.answer = {'status': 'failed', 'error': scripts.fault('script_stopped')}
    for _ in range(runner.KEEP_TEST_RUNS + 4):
        shop.store.refund_job(tracker)
        asyncio.run(scripts.test_script(shop.company, tracker, expected_revision=1, sandbox=shop.box))
    assert len(tests()) == runner.KEEP_TEST_RUNS == 10 and passes() == before


def test_the_input_of_a_seeded_location_holds_no_host_provenance(shop):
    from core_trackers import common
    scripts = shop.scripts
    data = common.read_location(shop.company, shop.location)
    probe = {'id': 'tracker-probe', 'key': 'probe', 'label': 'Probe', 'tracker': {'event_types': []}}
    document = scripts.build_input(data, probe, scripts.job_periods(shop.company, shop.location, data))
    names = set()
    def walk(value):
        if isinstance(value, dict):
            names.update(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk({'events': document['events'], 'records': document['records']})
    assert document['records']['items'] and document['records']['visits'] and document['records']['sales'] and not names & scripts.INTERNAL
    assert any('provenance' in row for row in data.items) and any(row.get('provenance') for row in data.visits), 'the host’s own rows carry it'
    text = json.dumps(document)
    assert '.jsonl' not in text and 'source_sha256' not in text and 'tracks_sha256' not in text
