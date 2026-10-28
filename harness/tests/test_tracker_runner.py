"""Trackers run by themselves: the stamp, the lease, the pass, its bounds and the server's tick.

Every test uses its own copy of one small company: a retail location with its Core Trackers
installed, one generated day of observations and a page the host has published. No provider and
no model is called anywhere here.
"""
import asyncio
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

KEYS = ['visitors', 'product_interactions', 'picked_up', 'put_back', 'basket', 'queue_time', 'empty_shelves', 'sales', 'conversion']
AUCKLAND = {'latitude': -36.85, 'longitude': 174.76, 'timezone': 'Pacific/Auckland', 'source': 'test fixture',
            'provenance': {'kind': 'operator_entry'}}
NOON = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc).timestamp()
BUILT = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()


def open_company(root, environment='shop'):
    import retail_records as records
    return records.company_from_access(root / 'environments' / environment, root, environment, 'peter', 'retail-demo')


def build(root, *, environment='shop', coordinates=None, publish=True):
    """A retail location with Core Trackers, one generated day and, with `publish`, its page."""
    from access import AccessStore
    from core_trackers import install_core_trackers
    from core_trackers.run import run_trackers
    from import_control_room_bundle import publish_core_trackers
    from retail_demo_history import generate
    from seed_control_room import seed_location
    import tracker_runner as runner
    access = AccessStore(root)
    state = access.create_environment(environment, 'Corner shop')
    access.issue(environment, owner=True, principal_id='peter')
    seeded = seed_location(state, root / 'access.sqlite', environment, 'peter', lifecycle='demo', name='Corner shop',
                           coordinates=coordinates)
    company = open_company(root, environment)
    install_core_trackers(company, seeded['location_id'], 'retail')
    generate(company, seeded['location_id'], seeded['room_id'], last_day=date(2026, 10, 9), days=1)
    if publish:
        with runner.host_pass(company, seeded['location_id']):
            run_trackers(company, seeded['location_id'])
            publish_core_trackers(company, seeded['location_id'], location_name='Corner shop')
    return seeded['location_id'], seeded['room_id']


def settle(root, environment='shop'):
    """Give the build’s host run a fixed time long before any clock a test reads, its own or the
    wall’s: the tests then read the same on any day."""
    with sqlite3.connect(root / 'environments' / environment / 'tracker-runs.sqlite') as db:
        db.execute('UPDATE tracker_schedule SET manual_after=0,next_after=0')
        db.execute('UPDATE tracker_runs SET started_at=?,finished_at=?', (BUILT, BUILT + 1))


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp('runner-template')
    location, room = build(root)
    return root, location, room


@pytest.fixture
def shop(template, tmp_path):
    """One test's own copy: (root, state, company, location, room, clock, sweep)."""
    import tracker_runner as runner
    source, location, room = template
    root = tmp_path / 'state'
    shutil.copytree(source, root)
    settle(root)
    now = [NOON]
    def sweep(**more):
        return runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={}, **more)
    return SimpleNamespace(root=root, state=root / 'environments' / 'shop', company=open_company(root), location=location,
        room=room, now=now, sweep=sweep, runs=runner.TrackerRuns(root / 'environments' / 'shop', clock=lambda: now[0]),
        key=('peter', 'retail-demo', location))


def more_recordings(shop, day=date(2026, 10, 10)):
    """Another day of observations through the ordinary ingestion."""
    from retail_demo_history import generate
    return generate(shop.company, shop.location, shop.room, last_day=day, days=1, seed=day.toordinal())


def revisions(shop):
    import tracker_runner as runner
    return {o['tracker_key']: o['revision'] for o in runner.current_outputs(shop.state, *shop.key)}


def tiles(shop):
    import tracker_runner as runner
    return {o['tracker_key']: o['tile'] for o in runner.current_outputs(shop.state, *shop.key)}


def page(shop):
    import tracker_runner as runner
    return runner.page_directory(shop.state, 'peter') / 'data.json'


def stamp(shop):
    import tracker_runner as runner
    return runner.input_stamp(shop.state, *shop.key)


def test_new_events_cause_one_pass_and_a_new_revision(shop):
    import tracker_runner as runner
    first = shop.sweep()
    assert [(run['trigger'], run['status'], run['page_published']) for run in first] == [('events', 'completed', True)]
    assert [output['key'] for output in first[0]['outputs']] == KEYS and first[0]['errors'] == []
    before, page_before, home_before = revisions(shop), page(shop).read_bytes(), runner.home_revision(shop.state, 'peter', shop.location)
    assert set(before) == set(KEYS) and home_before['run']['status'] == 'completed'
    # Nothing new: no pass, however long the service waits.
    for later in (5, 600, 7200):
        shop.now[0] = NOON + later
        assert shop.sweep() == []
    assert len(shop.runs.runs(*shop.key)) == 2  # the host's publish and the first pass
    # Two batches of commits inside one interval are one pass.
    more_recordings(shop)
    more_recordings(shop, date(2026, 10, 11))
    shop.now[0] += 60
    second = shop.sweep()
    assert [(run['trigger'], run['status']) for run in second] == [('events', 'completed')]
    after = revisions(shop)
    changed = sorted(output['key'] for output in second[0]['outputs'] if output['changed'])
    assert 'visitors' in changed and all(after[key] == before[key] + 1 for key in changed)
    assert all(after[key] == before[key] for key in KEYS if key not in changed)
    assert page(shop).read_bytes() != page_before
    home = runner.home_revision(shop.state, 'peter', shop.location)
    assert home['revision'] != home_before['revision'] and {row['key']: row['revision'] for row in home['outputs']} == after
    shop.now[0] += 600
    assert shop.sweep() == []


def test_a_pass_waits_out_the_minimum_interval(shop):
    shop.sweep()
    more_recordings(shop)
    shop.now[0] += 30
    assert shop.sweep() == []  # inside the minimum interval of 60 seconds
    shop.now[0] += 31
    assert [run['trigger'] for run in shop.sweep()] == ['events']


def test_derived_rows_and_outputs_do_not_change_the_stamp(shop):
    import retail_records as records
    from core_trackers import add_tracker, remove_tracker
    before = stamp(shop)
    assert shop.sweep()[0]['status'] == 'completed'
    assert stamp(shop) == before, 'what a pass writes stays out of its own stamp'
    records.derive(shop.company, shop.location)
    assert stamp(shop) == before
    seen = {before}
    def moved(change):
        change()
        value = stamp(shop)
        assert value not in seen
        seen.add(value)
    moved(lambda: add_tracker(shop.company, shop.location, 'spills', 'Spills', 'Liquid on the floor.'))
    # Removing the newest row and adding another leaves the row count and the highest rowid as they were.
    moved(lambda: (remove_tracker(shop.company, shop.location, 'spills'),
                   add_tracker(shop.company, shop.location, 'leaks', 'Leaks', 'Water under the cabinet.')))
    moved(lambda: records.record_till_sales(shop.company, shop.location, [{'transaction_id': 'T-1', 'at': '2026-10-09T12:00:00Z',
        'currency': 'EUR', 'lines': [{'label': 'Water', 'quantity': 1, 'unit_price': '1.20'}]}]))
    moved(lambda: shop.company.locations.save_product('peter', {'id': 'catalog-water', 'collection': 'retail-demo',
        'location_id': shop.location, 'label': 'Water 500 ml', 'provenance': {'kind': 'catalog_import'}}))
    moved(lambda: shop.company.locations.configure_location('peter', {'collection': 'retail-demo', 'location_id': shop.location, 'coordinates': AUCKLAND}))
    moved(lambda: records.register_zone(shop.company, shop.location, records.cameras(shop.company, shop.location)[0]['id'],
                                         'shelf_9', 'shelf', 'Shelf 9', data_basis='generated_history'))
    moved(lambda: more_recordings(shop))


def test_second_claim_waits_for_the_lease(shop):
    import tracker_runner as runner
    other = runner.TrackerRuns(shop.state, clock=lambda: shop.now[0])  # a second process on the same file
    held = shop.runs.claim(*shop.key, trigger='events', stamp='s1', day='2026-10-11')
    assert held and other.claim(*shop.key, trigger='events', stamp='s1', day='2026-10-11') is None
    assert other.state(*shop.key)['running'] and shop.sweep() == []
    shop.runs.finish(held['id'], held['lease_token'], status='completed', stamp='s1', day='2026-10-11')
    again = other.claim(*shop.key, trigger='manual', stamp='s1', day='2026-10-11')
    assert again and again['id'] != held['id']
    # A pass that lost its lease closes nothing: the pass that holds it owns the location's state.
    assert shop.runs.finish(held['id'], held['lease_token'], status='failed')['status'] == 'completed'
    assert other.state(*shop.key)['running'] and other.state(*shop.key)['failures'] == 0


def test_expired_lease_is_interrupted_and_rerun_gives_the_same_revisions(shop):
    import tracker_runner as runner
    assert shop.sweep()[0]['status'] == 'completed'
    done, page_done = revisions(shop), page(shop).read_bytes()
    # A pass starts and its process stops: the lease stays, the run row stays open.
    more_recordings(shop)
    crashed = shop.runs.claim(*shop.key, trigger='events', stamp=stamp(shop), day='2026-10-11')
    shop.now[0] += runner.LEASE_SECONDS - 1
    assert shop.sweep() == []
    shop.now[0] += 2
    assert shop.sweep() == [], 'a pass that stopped mid-way counts as a failure: the next one waits out the backoff'
    rows = {run['id']: run for run in shop.runs.runs(*shop.key)}
    assert rows[crashed['id']]['status'] == 'interrupted' and rows[crashed['id']]['error'] == runner.INTERRUPTED
    state = shop.runs.state(*shop.key)
    assert (state['failures'], state['running'], state['next_after']) == (1, False, shop.now[0] + 120)
    shop.now[0] = state['next_after']
    rerun = shop.sweep()
    assert [run['status'] for run in rerun] == ['completed'] and shop.runs.state(*shop.key)['failures'] == 0
    once, page_once = revisions(shop), page(shop).read_bytes()
    assert once != done and page_once != page_done
    # The same work again changes nothing: equal content keeps its revision and the page keeps its bytes.
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 60
    assert [run['trigger'] for run in shop.sweep()] == ['manual']
    assert revisions(shop) == once and page(shop).read_bytes() == page_once


def test_failure_backs_off_keeps_the_last_output_and_needs_attention_after_five(shop, monkeypatch):
    import tracker_runner as runner
    from core_trackers import visitors
    assert shop.sweep()[0]['status'] == 'completed'
    good = revisions(shop)
    def broken(*args, **kwargs):
        raise RuntimeError('boom at /srv/private/state.sqlite with a customer payload')
    working = visitors.run
    monkeypatch.setattr(visitors, 'run', broken)
    more_recordings(shop)
    shop.now[0] += 60
    failed = shop.sweep()[0]
    assert failed['status'] == 'failed' and failed['errors'] == [{'key': 'visitors', 'error': 'The processor stopped with RuntimeError.'}]
    assert '/srv' not in json.dumps(failed) and 'payload' not in json.dumps(failed)
    assert failed['page_published'], 'the other eight publish; the failed tracker keeps its last output'
    assert revisions(shop)['visitors'] == good['visitors'] and tiles(shop)['visitors']['state'] == 'ready'
    assert json.loads(page(shop).read_text())['core_trackers'][0]['revision'] == good['visitors']
    state = shop.runs.state(*shop.key)
    assert state['failures'] == 1 and state['processed_stamp'] != stamp(shop) and state['next_after'] == shop.now[0] + 120
    shop.now[0] += 119
    assert shop.sweep() == []  # backoff: one minute doubled per failure
    for expected in (2, 3, 4, 5):
        shop.now[0] = shop.runs.state(*shop.key)['next_after']
        assert shop.sweep()[0]['status'] == 'failed' and shop.runs.state(*shop.key)['failures'] == expected
    state = shop.runs.state(*shop.key)
    assert state['needs_attention'] and state['next_after'] - shop.now[0] <= runner.FAILURE_CEILING_SECONDS
    shop.now[0] += 10 * runner.FAILURE_CEILING_SECONDS
    assert shop.sweep() == [], 'after five failures in a row the location waits for new inputs or a request'
    # A request runs it; once the processor works again the failures clear.
    shop.runs.request(*shop.key, by='peter')
    assert shop.sweep()[0]['status'] == 'failed'
    monkeypatch.setattr(visitors, 'run', working)
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 60
    assert shop.sweep()[0]['status'] == 'completed'
    state = shop.runs.state(*shop.key)
    assert state['failures'] == 0 and not state['needs_attention'] and revisions(shop)['visitors'] == good['visitors'] + 1


def test_new_inputs_end_the_wait_for_attention(shop, monkeypatch):
    import tracker_runner as runner
    from core_trackers import sales
    monkeypatch.setattr(sales, 'run', lambda *a, **k: (_ for _ in ()).throw(ValueError('Connect the till first.')))
    for _ in range(runner.ATTENTION_AFTER):
        shop.now[0] = max(shop.now[0], shop.runs.state(*shop.key)['next_after'])
        assert shop.sweep()[0]['errors'] == [{'key': 'sales', 'error': 'Connect the till first.'}]
    shop.now[0] = shop.runs.state(*shop.key)['next_after']
    assert shop.sweep() == []
    more_recordings(shop)
    assert [run['trigger'] for run in shop.sweep()] == ['events']


def test_day_rollover_runs_once_per_local_day(template, tmp_path):
    import tracker_runner as runner
    root = tmp_path / 'state'
    location, _ = build(root, coordinates=AUCKLAND)
    settle(root)
    state, key = root / 'environments' / 'shop', ('peter', 'retail-demo', location)
    now = [datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc).timestamp()]  # 23:00 on 11 October in Auckland
    sweep = lambda: runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={})
    runs = runner.TrackerRuns(state, clock=lambda: now[0])
    first = sweep()
    assert [(run['trigger'], run['day']) for run in first] == [('events', '2026-10-11')]
    data = runner.page_directory(state, 'peter') / 'data.json'
    week = lambda: json.loads(data.read_text())['core_trackers'][0]['periods']['week']['period_label']
    base = lambda: json.loads(data.read_text())['core_trackers'][0]['period']['label']
    assert week() == '5 Oct to 11 Oct 2026' and base() == 'Fri 9 Oct 2026'
    before, revision = {o['tracker_key']: o['revision'] for o in runner.current_outputs(state, *key)}, runner.home_revision(state, 'peter')['revision']
    now[0] += 59 * 60
    assert sweep() == []  # 23:59 local: the same day
    now[0] += 31 * 60  # 00:30 on 12 October in Auckland; still 11 October in UTC
    assert datetime.fromtimestamp(now[0], timezone.utc).date() == date(2026, 10, 11)
    turned = sweep()
    assert [(run['trigger'], run['day'], run['status']) for run in turned] == [('day', '2026-10-12', 'completed')]
    assert week() == '6 Oct to 12 Oct 2026', 'the week, month and year readings end on the location’s current local day'
    assert base() == 'Fri 9 Oct 2026', 'the tile’s own reading stays the latest day that has data'
    assert {o['tracker_key']: o['revision'] for o in runner.current_outputs(state, *key)} == before
    assert runner.home_revision(state, 'peter')['revision'] != revision, 'an open page learns that its windows moved'
    for later in (60, 3600, 12 * 3600):
        now[0] += later
        assert sweep() == [], 'one pass per local day'
    # With a daily time, the day's pass waits for that time on the location's own clock.
    runs.set_policy(*key, {'daily_at': '06:00'}, expected_revision=1)
    now[0] = datetime(2026, 10, 12, 11, 30, tzinfo=timezone.utc).timestamp()  # 00:30 on 13 October in Auckland
    assert sweep() == []
    now[0] = datetime(2026, 10, 12, 17, 5, tzinfo=timezone.utc).timestamp()  # 06:05 on 13 October in Auckland
    assert [(run['trigger'], run['day']) for run in sweep()] == [('day', '2026-10-13')]


def test_a_location_without_coordinates_turns_its_day_in_utc(shop):
    shop.now[0] = datetime(2026, 10, 11, 23, 50, tzinfo=timezone.utc).timestamp()
    assert shop.sweep()[0]['day'] == '2026-10-11'
    shop.now[0] += 20 * 60
    assert [(run['trigger'], run['day']) for run in shop.sweep()] == [('day', '2026-10-12')]


def test_budget_stops_between_trackers(shop, monkeypatch):
    import tracker_runner as runner
    from core_trackers import product_interactions, visitors
    shop.sweep()
    good, page_before = revisions(shop), page(shop).read_bytes()
    more_recordings(shop)
    elapsed, ran = [0.0], []
    def slow(module):
        original = module.run
        def run(*args, **kwargs):
            elapsed[0] += 3.0
            ran.append(module.KEY)
            return original(*args, **kwargs)
        monkeypatch.setattr(module, 'run', run)
    slow(visitors)
    slow(product_interactions)
    shop.now[0] += 60
    run = runner.run_location(shop.company, shop.location, shop.runs, trigger='events', stamp=stamp(shop), day='2026-10-11',
                              budget_seconds=5, clock=lambda: elapsed[0])
    assert run['status'] == 'budget_exceeded' and ran == ['visitors', 'product_interactions']
    assert [output['key'] for output in run['outputs']] == ['visitors', 'product_interactions'], 'the pass stops between trackers'
    assert not run['page_published'] and page(shop).read_bytes() == page_before
    after = revisions(shop)
    assert all(after[key] == good[key] for key in KEYS[2:])
    state = shop.runs.state(*shop.key)
    assert state['failures'] == 1 and state['processed_stamp'] != stamp(shop) and state['next_after'] > shop.now[0]
    assert not state['running']


def test_company_tracker_failure_leaves_core_outputs_and_page(shop):
    from core_trackers import add_tracker
    shop.sweep()
    add_tracker(shop.company, shop.location, 'spills', 'Spills', 'Liquid on the floor.', script='company.spills')
    more_recordings(shop)
    shop.now[0] += 60
    called = []
    def custom(tracker, company, location_id, period, data):
        called.append(tracker['key'])
        raise RuntimeError('sandbox unavailable')
    run = shop.sweep(custom=custom)[0]
    assert called == ['spills'] and run['status'] == 'completed', 'a company tracker’s fault is its own'
    assert run['errors'] == [{'key': 'spills', 'error': 'The processor stopped with RuntimeError.'}]
    assert [output['key'] for output in run['outputs']] == KEYS and run['page_published']
    shown = json.loads(page(shop).read_text())['core_trackers']
    assert [tile['key'] for tile in shown] == KEYS + ['spills'] and all(tile['core'] and tile['revision'] for tile in shown[:9])
    assert shown[9]['needs'] == 'Needs first run' and shown[9]['core'] is False
    assert shop.runs.state(*shop.key)['failures'] == 0 and shop.runs.state(*shop.key)['processed_stamp'] == stamp(shop)
    # With no sandbox in this pass the company's script is listed as skipped and still stays out of the process.
    more_recordings(shop, date(2026, 10, 11))
    shop.now[0] += 60
    assert shop.sweep()[0]['skipped'] == [{'key': 'spills', 'status': 'skipped', 'reason': 'Script runs in the sandbox.'}]


def test_company_script_is_never_imported(shop, tmp_path, monkeypatch):
    """A tracker row written straight into the store, as an attacker with database access would."""
    import retail_records as records
    from core_trackers import installed_trackers, tracker_id
    from core_trackers.run import period_readings, run_trackers
    planted = tmp_path / 'planted'
    planted.mkdir()
    flag = tmp_path / 'imported.flag'
    for name in ('planted_company_tracker', 'planted_core_tracker', 'planted_recipe_key'):
        (planted / f'{name}.py').write_text(f"open({str(flag)!r}, 'a').write({name!r} + '\\n')\ndef run(*args, **kwargs):\n    raise SystemExit('ran')\n")
    monkeypatch.syspath_prepend(str(planted))
    installed = {t['key']: t for t in installed_trackers(shop.company, shop.location)}
    forged = lambda key, **more: {**installed['visitors'], 'id': tracker_id(shop.company.id, shop.location, key), 'key': key, **more}
    records.put_many(shop.company, 'tracker', shop.location, [
        forged('own', core=False, locked=False, processor={'script': 'planted_company_tracker', 'reads': [], 'summary': 'agent'}),
        forged('fake_core', processor={'script': 'planted_core_tracker', 'reads': [], 'summary': 'script'}),
        # A Core key whose record names another script: the recipe's script is the only one that runs.
        {**installed['sales'], 'processor': {**installed['sales']['processor'], 'script': 'planted_recipe_key'}}])
    result = run_trackers(shop.company, shop.location)
    period_readings(shop.company, shop.location)
    assert not flag.exists() and not [name for name in sys.modules if name.startswith('planted_')]
    assert sorted((row['key'], row['reason']) for row in result['skipped']) == [
        ('fake_core', 'A Core Tracker runs the script of its recipe.'), ('own', 'Script runs in the sandbox.'),
        ('sales', 'A Core Tracker runs the script of its recipe.')]
    assert [output['tracker_key'] for output in result['outputs']] == [key for key in KEYS if key != 'sales'] and result['errors'] == []
    # The unattended pass reads the same rows and keeps the same rule.
    run = shop.sweep()[0]
    assert not flag.exists() and len(run['skipped']) == 3 and run['status'] == 'completed'


def test_windowless_commit_reaches_the_trackers(shop):
    """An observation a tool or a route commits has no stream window; the pass still brings it in."""
    from cosmos_archive_import import retail_profile
    shop.sweep()
    before = tiles(shop)['picked_up']['value']
    source = sorted(row['id'] for row in shop.company.monitoring.registered_sources('peter', 'retail-demo'))[-1]
    profile = retail_profile(shop.company, shop.location)
    event = shop.company.monitoring.commit_event('peter', {'profile_id': profile['id'], 'profile_revision': profile['revision'],
        'event_key': 'from-a-tool', 'type': 'pick_up', 'description': 'Takes a bottle from the shelf.',
        'source': {'clip_id': source, 'start_s': 1.0, 'end_s': 2.0}, 'track_id': 'P0', 'object_ref': 'object_77',
        'object_description': 'A bottle of water'})
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='event' AND id=?", (event['id'],)).fetchone()[0] == 0
    shop.now[0] += 60
    run = shop.sweep()
    assert [r['trigger'] for r in run] == ['events'] and tiles(shop)['picked_up']['value'] == before + 1
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE kind='event' AND id=?", (event['id'],)).fetchone()[0] == 1
    shop.now[0] += 600
    assert shop.sweep() == []


def test_queued_stream_projections_are_drained_for_a_company_with_no_session(shop, monkeypatch):
    """A window a host command admits waits in the stream queue; the pass drains it and reads it whole."""
    import cosmos_archive_import
    import tracker_runner as runner
    events = shop.sweep()[0]['derived']['events']
    monkeypatch.setattr(cosmos_archive_import, 'drain', lambda company, pipeline: None)  # admitted, left in the queue
    more_recordings(shop)
    assert runner.projection_pending(shop.state, 'peter') > 0
    shop.now[0] += 60
    waited, run = {}, []
    assert runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0], pending_since=waited) is None, 'the pass lets the batch land whole'
    # An open workspace drains its own queue; the pass leaves it that work and waits.
    queued = runner.projection_pending(shop.state, 'peter')
    assert runner.tracker_pass(shop.root, shop.root, clock=lambda: shop.now[0], pending_since=waited, served={'shop'}) == []
    assert runner.projection_pending(shop.state, 'peter') == queued
    for _ in range(20):  # each sweep drains a bounded part of the queue
        shop.now[0] += runner.PROBE_SECONDS
        run = runner.tracker_pass(shop.root, shop.root, clock=lambda: shop.now[0], pending_since=waited)
        if run:
            break
    assert runner.projection_pending(shop.state, 'peter') == 0 and [r['status'] for r in run] == ['completed']
    assert tiles(shop)['visitors']['period']['label'] == 'Sat 10 Oct 2026' and run[0]['derived']['events'] > events
    assert shop.now[0] - NOON < 60 + runner.PROJECTION_WAIT_SECONDS, 'the queue drained; the pass had no need of the two-minute bound'


def test_a_queue_that_stays_full_holds_a_pass_for_two_minutes_at_most(shop, monkeypatch):
    import tracker_runner as runner
    shop.sweep()
    more_recordings(shop)
    shop.now[0] += 60
    monkeypatch.setattr(runner, 'projection_pending', lambda state, owner: 3)
    waited = {}
    assert runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0], pending_since=waited) is None
    assert runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0] + runner.PROJECTION_WAIT_SECONDS - 1, pending_since=waited) is None
    assert runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0] + runner.PROJECTION_WAIT_SECONDS, pending_since=waited)[0] == 'events'


def test_the_pass_runs_as_the_installing_owner_and_says_why_when_that_access_ends(shop):
    import tracker_runner as runner
    shop.sweep()
    good, page_before = revisions(shop), page(shop).read_bytes()
    more_recordings(shop)
    with sqlite3.connect(shop.root / 'access.sqlite') as db:
        db.execute("UPDATE principal_environments SET role='browse' WHERE principal_id='peter' AND environment='shop'")
    shop.now[0] += 60
    run = shop.sweep()[0]
    assert run['status'] == 'failed' and run['error'] == runner.OWNER_ACCESS and run['outputs'] == []
    assert revisions(shop) == good and page(shop).read_bytes() == page_before, 'the location keeps its last outputs'
    assert shop.runs.last_run('peter', shop.location)['error'] == runner.OWNER_ACCESS
    with sqlite3.connect(shop.root / 'access.sqlite') as db:
        db.execute("UPDATE principal_environments SET role='owner' WHERE principal_id='peter' AND environment='shop'")
    shop.now[0] = shop.runs.state(*shop.key)['next_after']
    assert shop.sweep()[0]['status'] == 'completed'


def test_the_page_keeps_the_location_it_shows(template, tmp_path):
    """The pass refreshes a page a host import published for this location. It makes no page of its own."""
    import tracker_runner as runner
    root = tmp_path / 'state'
    location, _ = build(root, publish=False)
    state = root / 'environments' / 'shop'
    now = [NOON]
    run = runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={})[0]
    assert run['status'] == 'completed' and not run['page_published'] and len(run['outputs']) == 9
    assert not runner.page_directory(state, 'peter').exists() and runner.home_revision(state, 'peter')['revision'] is None
    # A page that shows another location stays that location's page.
    from import_control_room_bundle import publish_core_trackers
    company = open_company(root)
    publish_core_trackers(company, location, location_name='Corner shop')
    manifest = runner.page_directory(state, 'peter') / 'manifest.json'
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), 'location': {'id': 'location-elsewhere', 'name': 'Elsewhere'}}))
    before = (runner.page_directory(state, 'peter') / 'data.json').read_bytes()
    runner.TrackerRuns(state).request('peter', 'retail-demo', location, by='peter')
    now[0] += 60
    run = runner.tracker_pass(root, root, clock=lambda: now[0], pending_since={})[0]
    assert run['trigger'] == 'manual' and not run['page_published']
    assert (runner.page_directory(state, 'peter') / 'data.json').read_bytes() == before


def test_a_request_for_a_few_trackers_leaves_the_whole_pass_to_the_next_probe(shop):
    shop.sweep()
    good = revisions(shop)
    more_recordings(shop)
    shop.runs.request(*shop.key, by='peter', only=['visitors'])
    shop.now[0] += 60
    run = shop.sweep()[0]
    assert (run['trigger'], run['only'], [o['key'] for o in run['outputs']]) == ('manual', ['visitors'], ['visitors'])
    assert revisions(shop)['visitors'] == good['visitors'] + 1 and revisions(shop)['sales'] == good['sales']
    shop.now[0] += 60
    assert [(r['trigger'], len(r['outputs'])) for r in shop.sweep()] == [('events', 9)]


async def ticks(app_state, count=3):
    import tracker_runner as runner
    for _ in range(count):
        await runner.tracker_tick(app_state)
        await asyncio.sleep(0.05)
    task = app_state.tracker_runner['task']
    await runner.tracker_stop(app_state)
    return task


def test_runner_off_switch_and_automatic_false(shop):
    import tracker_runner as runner
    with pytest.raises(ValueError, match='ROCOTO_TILE_RUNNER'):
        runner.runner_state(shop.root, shop.root, {'ROCOTO_VIDEO_TRACKER_RUNNER': 'maybe'})
    off = SimpleNamespace(tracker_runner=runner.runner_state(shop.root, shop.root, {'ROCOTO_VIDEO_TRACKER_RUNNER': ' OFF '}))
    assert off.tracker_runner['enabled'] is False and runner.runner_state(shop.root, shop.root, {})['enabled'] is True
    assert asyncio.run(ticks(off)) is None and shop.runs.runs(*shop.key, limit=5)[0]['trigger'] == 'host'
    # The location's own switch: automatic off keeps its passes to requests.
    shop.runs.set_policy(*shop.key, {'automatic': False}, expected_revision=1)
    more_recordings(shop)
    shop.now[0] += 3600
    assert shop.sweep() == []
    shop.runs.request(*shop.key, by='peter')
    assert [run['trigger'] for run in shop.sweep()] == ['manual']
    shop.now[0] += 3600
    assert shop.sweep() == []
    with pytest.raises(ValueError, match='current revision'):
        shop.runs.set_policy(*shop.key, {'automatic': True}, expected_revision=1)
    for bad in ({'min_interval_seconds': 5}, {'budget_seconds': 900}, {'daily_at': '25:00'}, {'automatic': 'yes'}, {'surprise': 1}):
        with pytest.raises(ValueError):
            shop.runs.set_policy(*shop.key, bad, expected_revision=2)


def test_the_tick_runs_one_sweep_at_a_time_in_a_thread_and_shutdown_waits_for_it(shop, monkeypatch):
    import threading
    import tracker_runner as runner
    on = SimpleNamespace(tracker_runner=runner.runner_state(shop.root, shop.root, {}))
    sweeps, passes, release = [], [], threading.Event()
    sweep, one_pass = runner.tracker_pass, runner.run_location
    def counted(*args, **kwargs):
        sweeps.append(threading.current_thread().name)
        return sweep(*args, **kwargs)
    def slow(*args, **kwargs):
        passes.append(kwargs['trigger'])
        release.wait(10)
        return one_pass(*args, **kwargs)
    monkeypatch.setattr(runner, 'tracker_pass', counted)
    monkeypatch.setattr(runner, 'run_location', slow)
    monkeypatch.setattr(runner, 'PROBE_SECONDS', 0)
    async def scenario():
        for _ in range(200):  # ticks of the server loop while the first sweep's pass is still at work
            await runner.tracker_tick(on)
            await asyncio.sleep(0.02)
            if passes and len(sweeps) == 1 and _ > 5:
                break
        assert passes == ['events'] and len(sweeps) == 1 and sweeps[0] != threading.current_thread().name
        stopping = asyncio.create_task(runner.tracker_stop(on))
        await asyncio.sleep(0.2)
        assert not stopping.done(), 'shutdown waits for the pass in hand'
        release.set()
        await stopping
    asyncio.run(scenario())
    newest = runner.TrackerRuns(shop.state).runs(*shop.key, limit=1)[0]
    assert (newest['trigger'], newest['status']) == ('events', 'completed'), 'the run row is closed when shutdown returns'
    assert on.tracker_runner['enabled'] is False and on.tracker_runner['task'] is None
    # Two sweeps at once in one process: the second returns at once and leaves the work to the first.
    more_recordings(shop)
    assert runner._PASS.acquire(blocking=False)
    try:
        assert sweep(shop.root, shop.root, clock=lambda: NOON + 86400 * 365) == []
    finally:
        runner._PASS.release()
    assert len(sweep(shop.root, shop.root, clock=lambda: NOON + 86400 * 365)) == 1


def test_a_paused_company_tracker_sits_out_the_automatic_passes(shop):
    from core_trackers import add_tracker
    add_tracker(shop.company, shop.location, 'spills', 'Spills', 'Liquid on the floor.', script='company.spills')
    seen = []
    custom = lambda tracker, *rest: seen.append(tracker['key'])
    shop.sweep(custom=custom)
    assert seen == ['spills']
    shop.runs.set_policy(*shop.key, {'paused': ['spills']}, expected_revision=1)
    more_recordings(shop)
    shop.now[0] += 60
    run = shop.sweep(custom=custom)[0]
    assert seen == ['spills'] and len(run['outputs']) == 9 and shop.runs.state(*shop.key)['processed_stamp'] == stamp(shop)
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 60
    shop.sweep(custom=custom)
    assert seen == ['spills', 'spills'], 'a request runs every tracker of the location'


def test_the_recipe_revision_is_followed_at_the_start_of_a_pass(shop):
    import retail_records as records
    shop.sweep()
    installed = records.rows(shop.company, 'tracker_installation', shop.location)[0]
    records.put_many(shop.company, 'tracker_installation', shop.location, [{**installed, 'recipe_revision': 'retail-core-v0'}])
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 60
    assert shop.sweep()[0]['status'] == 'completed'
    assert records.rows(shop.company, 'tracker_installation', shop.location)[0]['recipe_revision'] == 'retail-core-v1'
    shop.now[0] += 600
    assert shop.sweep() == [], 'the reinstall is part of the pass that made it'


def test_a_host_command_shares_the_lease_and_waits_for_a_running_pass(shop):
    import tracker_runner as runner
    from core_trackers.run import main
    service = runner.TrackerRuns(shop.state)  # the server's own pass, on the wall clock a host command reads
    held = service.claim(*shop.key, trigger='events', stamp='s', day='2026-10-11')
    waits = []
    with pytest.raises(ValueError, match='A tracker pass of this location is running'):
        with runner.host_pass(shop.company, shop.location, wait_seconds=2, sleep=waits.append):
            pytest.fail('the command starts once the pass in hand is done')
    assert waits == [1, 1]
    service.finish(held['id'], held['lease_token'], status='completed')
    arguments = ['--state', str(shop.state), '--access-state', str(shop.root), '--environment-id', 'shop', '--owner', 'peter',
                 '--location-id', shop.location]
    assert main(arguments + ['--publish'])['status'] == 'completed'
    newest = runner.TrackerRuns(shop.state).runs(*shop.key, limit=1)[0]
    assert (newest['trigger'], newest['status'], newest['page_published'], len(newest['outputs'])) == ('host', 'completed', True, 9)
    # The command ran the location's whole pass with its page, so the service has nothing left to do.
    assert runner.tracker_pass(shop.root, shop.root, pending_since={}) == []
    # A run over a chosen day leaves the location's own pass to the service; a failed command leaves no lease behind.
    main(arguments + ['--day', '2026-10-09'])
    with pytest.raises(RuntimeError):
        with runner.host_pass(shop.company, shop.location):
            raise RuntimeError('stopped')
    state = runner.TrackerRuns(shop.state).state(*shop.key)
    assert not state['running'] and state['failures'] == 0 and state['last_status'] == 'failed'


def test_run_rows_stay_bounded(shop, monkeypatch):
    import tracker_runner as runner
    monkeypatch.setattr(runner, 'KEEP_RUNS', 3)
    for index in range(6):
        claimed = shop.runs.claim(*shop.key, trigger='manual', stamp=f's{index}', day='2026-10-11')
        shop.runs.finish(claimed['id'], claimed['lease_token'], status='completed', stamp=f's{index}')
        shop.now[0] += 1
    assert len(shop.runs.runs(*shop.key, limit=100)) == 3


def test_a_company_with_no_trackers_costs_a_file_check_and_makes_no_file(tmp_path):
    from access import AccessStore
    import tracker_runner as runner
    access = AccessStore(tmp_path)
    state = access.create_environment('empty', 'Empty')
    assert runner.tracker_pass(tmp_path, tmp_path) == [] and runner.installed_locations(state) == []
    assert sorted(path.name for path in state.iterdir()) == [] and not (tmp_path / 'tracker-runs.sqlite').exists()
    assert runner.TrackerRuns(state).state('peter', 'retail-demo', 'location-x')['policy'] == runner.DEFAULT_POLICY
    assert not (state / 'tracker-runs.sqlite').exists()


def test_cold_environment_is_run_without_a_session(template, tmp_path, monkeypatch):
    """The server's own loop runs the trackers of a company nobody has opened since the restart."""
    import time
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app
    import tracker_runner as runner
    source, location, _ = template
    shutil.copytree(source, tmp_path / 'state')
    state = tmp_path / 'state' / 'environments' / 'shop'
    runs = runner.TrackerRuns(state)
    assert [run['trigger'] for run in runs.runs('peter', 'retail-demo', location)] == ['host']
    app = make_app(tmp_path, monkeypatch)
    assert app.state.tracker_runner['enabled'] is True
    with TestClient(app):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(runs.runs('peter', 'retail-demo', location)) < 2:
            time.sleep(0.1)
        assert sorted(app.state.workspaces) == ['demo'], 'the company stays unloaded'
    newest = runs.runs('peter', 'retail-demo', location)[0]
    assert (newest['trigger'], newest['status'], newest['page_published']) == ('events', 'completed', True)
    assert not runs.state('peter', 'retail-demo', location)['running'], 'shutdown waits for the pass in hand'
    # Off at startup: the same server leaves the trackers to requests through the host commands.
    monkeypatch.setenv('ROCOTO_VIDEO_TRACKER_RUNNER', 'off')
    shutil.rmtree(tmp_path / 'state')
    shutil.copytree(source, tmp_path / 'state')
    quiet = make_app(tmp_path, monkeypatch)
    assert quiet.state.tracker_runner['enabled'] is False
    with TestClient(quiet):
        time.sleep(2.5)
    assert [run['trigger'] for run in runner.TrackerRuns(state).runs('peter', 'retail-demo', location)] == ['host']


# ── What two reviewers broke, kept as it was fixed ─────────────────────────────────────────────

PAGO_PAGO = {'latitude': -14.28, 'longitude': -170.7, 'timezone': 'Pacific/Pago_Pago', 'source': 'test fixture',
             'provenance': {'kind': 'operator_entry'}}


def on_request_only(shop):
    """Keep the location's passes to requests, so a sweep shows its projection alone."""
    shop.runs.set_policy(*shop.key, {'automatic': False}, expected_revision=1)


def in_location_store(shop, event_id):
    with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
        return db.execute("SELECT COUNT(*) FROM records WHERE kind='event' AND id=?", (event_id,)).fetchone()[0]


def tool_commit(shop, key, *, source=None, track='P0', words='A bottle of water'):
    """An observation the way a tool or a route commits it: no stream window."""
    from cosmos_archive_import import retail_profile
    source = source or sorted(row['id'] for row in shop.company.monitoring.registered_sources('peter', 'retail-demo'))[-1]
    profile = retail_profile(shop.company, shop.location)
    return shop.company.monitoring.commit_event('peter', {'profile_id': profile['id'], 'profile_revision': profile['revision'],
        'event_key': key, 'type': 'pick_up', 'description': 'Takes a bottle from the shelf.',
        'source': {'clip_id': source, 'start_s': 1.0, 'end_s': 2.0}, 'track_id': track, 'object_ref': 'object_' + key,
        'object_description': words})


def test_a_company_with_a_damaged_store_leaves_the_others_their_passes(shop, caplog):
    """One company's store the service cannot read stays that company's fault: a line where an
    operator reads it, a status its tools read, and every other company still gets its pass."""
    import os
    from access import AccessStore
    import tracker_runner as runner
    access = AccessStore(shop.root)
    damaged, closed = access.create_environment('aaa-damaged', 'Damaged'), access.create_environment('zzz-closed', 'Closed')
    (damaged / 'location-intelligence.sqlite').write_bytes(b'this is not a database' * 500)
    (closed / 'location-intelligence.sqlite').write_bytes(b'')
    (closed / 'location-intelligence.sqlite').chmod(0o000)
    try:
        with caplog.at_level('WARNING', logger='rocoto.video.trackers'):
            first = shop.sweep()
            assert shop.sweep() == []
        assert [(run['trigger'], run['status']) for run in first] == [('events', 'completed')], 'the healthy company runs'
        lines = [record.getMessage() for record in caplog.records]
        assert len([line for line in lines if 'aaa-damaged' in line and 'DatabaseError' in line]) == 1, 'one line per fault'
        assert runner.TrackerRuns(shop.state).service()['fault'] is None and runner.TrackerRuns(shop.state).service()['state'] == 'on'
        assert 'DatabaseError' in runner.TrackerRuns(damaged).service()['fault']
        if os.geteuid():  # the owner of a file reads it whatever its mode only as root
            assert len([line for line in lines if 'zzz-closed' in line and 'OperationalError' in line]) == 1
        # Repaired: the fault clears at the next sweep.
        (damaged / 'location-intelligence.sqlite').unlink()
        shop.sweep()
        assert runner.TrackerRuns(damaged).service()['fault'] is None
    finally:
        (closed / 'location-intelligence.sqlite').chmod(0o600)


def test_queued_inspections_ahead_leave_the_projections_reachable(shop, monkeypatch):
    """A company nobody has opened: each item's paid inspection waits in the same queue, ahead of
    the projections. The pass still brings every observation to the trackers."""
    import cosmos_archive_import
    import tracker_runner as runner
    shop.sweep()
    monkeypatch.setattr(cosmos_archive_import, 'drain', lambda company, pipeline: None)  # admitted, left in the queue
    more_recordings(shop)
    waiting = runner.projection_pending(shop.state, 'peter')
    assert waiting > 0
    with sqlite3.connect(shop.state / 'stream-ingestion.sqlite') as db:
        window, event = db.execute("SELECT window_id,event_id FROM stream_work WHERE status='pending' LIMIT 1").fetchone()
        db.execute("UPDATE stream_work SET rowid=rowid+1000000 WHERE status='pending'")
        first = db.execute('SELECT MAX(rowid) FROM stream_work WHERE rowid<1000000').fetchone()[0] + 1
        db.executemany("INSERT INTO stream_work(rowid,id,owner,window_id,event_id,kind,body,status) VALUES(?,?,'peter',?,?,'inspect_product','{}','pending')",
                       [(first + index, f'inspection-{index}', window, event) for index in range(600)])
    shop.now[0] += 600
    waited, run = {}, []
    for _ in range(12):  # each sweep drains a bounded part of the queue; a minute in all, inside the two-minute bound
        shop.now[0] += runner.PROBE_SECONDS
        run = runner.tracker_pass(shop.root, shop.root, clock=lambda: shop.now[0], pending_since=waited)
        if run:
            break
    assert runner.projection_pending(shop.state, 'peter') == 0 and [r['status'] for r in run] == ['completed']
    assert tiles(shop)['visitors']['period']['label'] == 'Sat 10 Oct 2026'
    with sqlite3.connect(shop.state / 'stream-ingestion.sqlite') as db:
        assert db.execute("SELECT COUNT(*) FROM stream_work WHERE kind='inspect_product' AND status='pending' AND id LIKE 'inspection-%'").fetchone()[0] == 600, \
            'the paid inspections stay with the server'


def test_a_pass_leaves_its_own_stamp_where_it_was_after_a_confirmed_match(shop):
    """An inspection confirms two visitors' items as one catalog product the model worded two ways.
    The pass carries that product's price onto the row each time; the location still comes to rest."""
    shop.sweep()
    shop.company.locations.save_product('peter', {'id': 'catalog-water', 'collection': 'retail-demo', 'location_id': shop.location,
        'label': 'Water 500 ml', 'status': 'confirmed', 'sku': '8410000000017', 'attributes': {'product_type': 'bottled-beverage'},
        'price': {'amount': '1.20', 'currency': 'EUR', 'basis': 'catalog_sourced', 'source': 'price list', 'observed_at': '2026-10-01T00:00:00+00:00'},
        'provenance': {'kind': 'catalog_import'}})
    events = [tool_commit(shop, 'k1'), tool_commit(shop, 'k2', track='P1', words='A clear water bottle with a blue cap')]
    shop.now[0] += 600
    assert [run['status'] for run in shop.sweep()] == ['completed']
    for index, event in enumerate(events):
        shop.company.locations.apply_product_match('peter', event['id'], {'product_id': 'catalog-water', 'status': 'confirmed', 'score': 0.97,
            'evidence': {'ocr_text': '8410000000017'}, 'inspection_id': f'inspection-{index}'})
    passes = []
    for _ in range(8):  # nothing is committed from here on
        shop.now[0] += 600
        passes.append(len(shop.sweep()))
    assert sum(passes[:3]) >= 1, 'the confirmed matches are new inputs'
    assert passes[3:] == [0] * 5 and shop.runs.state(*shop.key)['processed_stamp'] == stamp(shop), 'the location comes to rest'


def test_west_of_utc_an_evening_observation_is_in_the_week(shop):
    """A shop west of UTC: its late evening already carries tomorrow's UTC date."""
    from retail_demo_history import generate
    shop.company.locations.configure_location('peter', {'collection': 'retail-demo', 'location_id': shop.location, 'coordinates': PAGO_PAGO})
    friday = tiles(shop)['visitors']['value']
    evening = generate(shop.company, shop.location, shop.room, last_day=date(2026, 10, 11), days=1, seed=10, last_day_until='10:30')
    assert evening['visitors'] > 0  # seen around 22:30 on 10 October in Pago Pago: 09:30 on 11 October in UTC
    shop.now[0] = datetime(2026, 10, 11, 10, 45, tzinfo=timezone.utc).timestamp()  # 23:45 on 10 October in Pago Pago
    assert [(run['trigger'], run['day']) for run in shop.sweep()] == [('events', '2026-10-10')]
    tile = json.loads(page(shop).read_text())['core_trackers'][0]
    assert (tile['key'], tile['period']['label'], tile['value']) == ('visitors', 'Sun 11 Oct 2026', evening['visitors'])
    week = tile['periods']['week']
    assert (week['period_label'], week['value']) == ('5 Oct to 11 Oct 2026', friday + evening['visitors']), \
        'the week, month and year readings hold what the day tile shows'
    shop.now[0] = datetime(2026, 10, 11, 11, 5, tzinfo=timezone.utc).timestamp()  # 00:05 on 11 October in Pago Pago
    assert [(run['trigger'], run['day']) for run in shop.sweep()] == [('day', '2026-10-11')]
    assert json.loads(page(shop).read_text())['core_trackers'][0]['periods']['week']['period_label'] == '5 Oct to 11 Oct 2026'


def test_output_revisions_stay_bounded(shop, monkeypatch):
    import tracker_runner as runner
    monkeypatch.setattr(runner, 'KEEP_REVISIONS', 1, raising=False)
    shop.sweep()
    def kept():
        with sqlite3.connect(shop.state / 'location-intelligence.sqlite') as db:
            return sorted(db.execute("SELECT json_extract(body,'$.tracker_key'),json_extract(body,'$.revision') FROM records WHERE kind='tracker_output_revision'"))
    more_recordings(shop)
    shop.now[0] += 600
    changed = [output['key'] for output in shop.sweep()[0]['outputs'] if output['changed']]
    assert 'visitors' in changed
    assert kept() == sorted(revisions(shop).items()), 'each tracker keeps its newest revisions; the current output stays whole'


def test_a_pass_that_takes_its_process_down_counts_as_a_failure(shop):
    """A pass whose process stops each time: the expired lease is the only sign, and it counts."""
    import tracker_runner as runner
    attempts = []
    for _ in range(40):
        reason = runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0], pending_since={})
        if reason:
            assert shop.runs.claim(*shop.key, trigger=reason[0], stamp=reason[1], day=reason[2])  # the process stops here
            attempts.append(round(shop.now[0] - NOON))
        shop.now[0] += runner.LEASE_SECONDS + 1
    state = shop.runs.state(*shop.key)
    assert len(attempts) == runner.ATTENTION_AFTER and state['failures'] == runner.ATTENTION_AFTER and state['needs_attention']
    gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:])]
    assert gaps == sorted(gaps) and gaps[-1] > 2 * runner.LEASE_SECONDS, 'each stopped pass doubles the wait'
    assert [run['status'] for run in shop.runs.runs(*shop.key, limit=50)].count('interrupted') == runner.ATTENTION_AFTER
    # A request still runs it, and a pass that completes clears the count.
    shop.runs.request(*shop.key, by='peter')
    assert [run['status'] for run in shop.sweep()] == ['completed'] and shop.runs.state(*shop.key)['failures'] == 0


def test_a_commit_placed_later_or_stamped_earlier_still_reaches_the_location(shop, monkeypatch):
    import monitoring
    import retail_records as records
    import tracker_runner as runner
    on_request_only(shop)
    # A tool commits an observation on a recording that gets its camera a moment later.
    shop.company.monitoring.register_source('peter', {'id': 'late-recording', 'video_id': 'late-recording', 'dataset': 'retail-demo',
        'start': 0, 'end': 30, 'sha256': 'b' * 64, 'media_url': '/control-room/late.mp4', 'capture_start_utc': '2026-10-11T10:00:00+00:00'})
    early = tool_commit(shop, 'early', source='late-recording')
    assert shop.sweep() == [] and in_location_store(shop, early['id']) == 0
    records.ensure_camera(shop.company, shop.location, shop.room, 'Shop aisle', ['late-recording'])
    assert shop.sweep() == [] and in_location_store(shop, early['id']) == 1, 'placed once its recording has a camera'
    # The host clock steps back two seconds between two commits.
    first = tool_commit(shop, 'one')
    assert shop.sweep() == [] and in_location_store(shop, first['id']) == 1
    real = monitoring.time.time
    monkeypatch.setattr(monitoring.time, 'time', lambda: real() - 2)
    second = tool_commit(shop, 'two')
    monkeypatch.setattr(monitoring.time, 'time', real)
    assert second['committed_at'] < first['committed_at']
    assert shop.sweep() == [] and in_location_store(shop, second['id']) == 1, 'read in commit order, whatever the clock said'
    assert runner.commit_count(shop.state, 'peter') == shop.runs.projection('peter', 'retail-demo')['commits']


def test_a_chosen_day_from_the_host_leaves_the_service_its_whole_pass(shop):
    from core_trackers.run import main
    shop.sweep()
    arguments = ['--state', str(shop.state), '--access-state', str(shop.root), '--environment-id', 'shop', '--owner', 'peter',
                 '--location-id', shop.location]
    main(arguments + ['--day', '2026-10-01', '--publish'])
    shown = lambda: json.loads(page(shop).read_text())['core_trackers'][0]['period']['label']
    assert tiles(shop)['visitors']['period']['label'] == 'Thu 1 Oct 2026' == shown()
    with sqlite3.connect(shop.state / 'tracker-runs.sqlite') as db:  # the command ran on the wall clock
        db.execute('UPDATE tracker_schedule SET manual_after=0,next_after=0')
    shop.now[0] += 120
    assert [(run['trigger'], run['status']) for run in shop.sweep()] == [('events', 'completed')], 'the service runs the location’s own pass'
    assert tiles(shop)['visitors']['period']['label'] == 'Fri 9 Oct 2026' == shown()
    shop.now[0] += 600
    assert shop.sweep() == []


def test_a_host_command_that_stops_frees_its_location_within_two_minutes(shop):
    """A host command renews a short lease while it works; one that is killed stops renewing."""
    import time
    import tracker_runner as runner
    on_request_only(shop)
    assert runner.HOST_LEASE_SECONDS <= 120
    held = shop.runs.claim(*shop.key, trigger='host', stamp=stamp(shop), day='2026-10-11', lease_seconds=runner.HOST_LEASE_SECONDS)  # killed here
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += runner.HOST_LEASE_SECONDS - 1
    assert runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0], pending_since={}) is None
    shop.now[0] += 2
    assert runner.due(shop.runs, shop.state, *shop.key, now=shop.now[0], pending_since={})[0] == 'manual'
    state = shop.runs.state(*shop.key)
    assert not state['running'] and state['failures'] == 0, 'a host command that stopped is its operator’s to run again'
    assert {run['id']: run['status'] for run in shop.runs.runs(*shop.key)}[held['id']] == 'interrupted'
    # A command that takes longer than its lease keeps the location: the lease is renewed while it works.
    wall = runner.TrackerRuns(shop.state)
    handle = SimpleNamespace(state=shop.state, owner='peter', collection='retail-demo')  # the lease needs these three alone
    with runner.host_pass(handle, shop.location, lease_seconds=0.4, beat_seconds=0.05):
        time.sleep(1.2)
        assert wall.state(*shop.key)['running'] and wall.claim(*shop.key, trigger='events', stamp='s', day='2026-10-11') is None
    assert not wall.state(*shop.key)['running']


def test_the_time_budget_covers_the_page_publish(shop, monkeypatch):
    import tracker_runner as runner
    from core_trackers import common, visitors
    shop.sweep()
    more_recordings(shop)
    page_before, elapsed, original = page(shop).read_bytes(), [0.0], visitors.run
    def run(*args, **kwargs):
        if common.READING.get():  # the week, month and year readings the page publish makes
            elapsed[0] += 4.0
        return original(*args, **kwargs)
    monkeypatch.setattr(visitors, 'run', run)
    shop.now[0] += 600
    closed = runner.run_location(shop.company, shop.location, shop.runs, trigger='events', stamp=stamp(shop), day='2026-10-11',
                                 budget_seconds=5, clock=lambda: elapsed[0])
    assert closed['status'] == 'budget_exceeded' and not closed['page_published'] and len(closed['outputs']) == 9
    assert page(shop).read_bytes() == page_before, 'the page keeps its last data'
    assert shop.runs.state(*shop.key)['failures'] == 1


def test_the_engine_revision_covers_every_module_a_pass_runs(tmp_path):
    import tracker_runner as runner
    names = {path.name for path in runner.engine_sources()}
    assert {'retail_records.py', 'visit_basket.py', 'catalog_finder.py', 'import_control_room_bundle.py', 'run.py', 'common.py', 'retail.json'} <= names
    one, two = tmp_path / 'derive.py', tmp_path / 'publish.py'
    one.write_text('a = 1\n'); two.write_text('b = 1\n')
    before = runner.engine_revision([one, two])
    two.write_text('b = 2\n')
    assert runner.engine_revision([one, two]) != before and runner.engine_revision() == runner.engine_revision()


def test_the_pass_reads_membership_as_the_server_does(shop):
    """A person whose registry record is gone is no member, whatever a leftover membership row says."""
    import tracker_runner as runner
    shop.sweep()
    good = revisions(shop)
    more_recordings(shop)
    with sqlite3.connect(shop.root / 'access.sqlite') as db:
        db.execute("DELETE FROM principals WHERE id='peter'")
    shop.now[0] += 600
    run = shop.sweep()[0]
    assert run['status'] == 'failed' and run['error'] == runner.OWNER_ACCESS and revisions(shop) == good


# ── Script trackers in the pass, and their rows in the same run history ──

def test_a_tool_cannot_put_a_script_name_on_a_company_tracker(shop, tmp_path, monkeypatch):
    """`processor.script` names code the service imports for a Core Tracker. A change keeps the
    record's value, so a company's code reaches the sandbox alone."""
    from core_trackers import add_tracker, update_tracker
    from core_trackers.run import run_trackers, sandboxed
    planted = tmp_path / 'planted'
    planted.mkdir()
    flag = tmp_path / 'imported.flag'
    (planted / 'planted_by_a_tool.py').write_text(f"open({str(flag)!r}, 'a').write('ran')\ndef run(*args, **kwargs):\n    raise SystemExit('ran')\n")
    monkeypatch.syspath_prepend(str(planted))
    own = add_tracker(shop.company, shop.location, 'spills', 'Spills', 'Liquid on the floor.')
    assert own['processor']['script'] is None and not sandboxed(own)
    changed = update_tracker(shop.company, shop.location, 'spills', {'processor': {'script': 'planted_by_a_tool', 'reads': ['events'], 'summary': 'script',
                                                                                    'sandbox': {'kind': 'tracker-python-v1', 'script_id': 'script-x', 'revision': 1}}})
    assert changed['processor'] == {'script': None, 'reads': ['events'], 'summary': 'script',
                                    'sandbox': {'kind': 'tracker-python-v1', 'script_id': 'script-x', 'revision': 1}} and sandboxed(changed)
    assert update_tracker(shop.company, shop.location, 'spills', {'processor': 'planted_by_a_tool'})['processor'] == {'script': None}
    seen = []
    result = run_trackers(shop.company, shop.location, custom=lambda tracker, *rest: seen.append(tracker['key']))
    assert seen == [] and not flag.exists() and 'planted_by_a_tool' not in sys.modules and result['status'] == 'completed'


def test_a_script_run_is_listed_in_its_pass_and_its_fault_is_its_own(shop):
    from core_trackers import add_tracker, update_tracker
    from core_trackers.run import PASS
    shop.sweep()
    add_tracker(shop.company, shop.location, 'spills', 'Spills', 'Liquid on the floor.')
    update_tracker(shop.company, shop.location, 'spills', {'processor': {'sandbox': {'kind': 'tracker-python-v1', 'script_id': 'script-x', 'revision': 1}}})
    contexts = []
    def custom(tracker, company, location_id, period, data):
        contexts.append(PASS.get())
        return {'script_run': {'key': tracker['key'], 'status': 'failed', 'seconds': 1.5, 'error': {'code': 'script_error', 'message': 'KeyError', 'line': 3},
                               'output': None, 'wake': {'queued': True, 'id': 'wake-1'}}, 'output': None, 'error': 'The script stopped at line 3: KeyError'}
    shop.now[0] += 60
    run = shop.sweep(custom=custom)[0]
    assert run['status'] == 'completed' and run['page_published'] and [o['key'] for o in run['outputs']] == KEYS
    assert run['errors'] == [{'key': 'spills', 'error': 'The script stopped at line 3: KeyError'}]
    assert run['scripts'] == [{'key': 'spills', 'status': 'failed', 'seconds': 1.5, 'error': {'code': 'script_error', 'message': 'KeyError', 'line': 3},
                               'output': None, 'wake': {'queued': True, 'id': 'wake-1'}}]
    assert shop.runs.runs(*shop.key, limit=1)[0]['scripts'] == run['scripts'] and shop.runs.state(*shop.key)['failures'] == 0
    context = contexts[0]
    assert context['trigger'] == 'events' and context['run_id'] == run['id'] and context['deadline'] > context['clock']() and PASS.get() is None
    assert (context['day'], context['daily']) == (run['day'], True), 'a script reads the location’s local day and whether the pass stands for it'
    # A run that publishes hands its stored output to the pass; a tracker with no published script adds nothing.
    stored = {'tracker_key': 'spills', 'tracker_id': 'tracker-x', 'core': False, 'revision': 4, 'tile': {'state': 'ready'}}
    more_recordings(shop)
    shop.now[0] += 60
    run = shop.sweep(custom=lambda *args: {'script_run': {'key': 'spills', 'status': 'succeeded', 'output': 'published'}, 'output': stored})[0]
    assert run['outputs'][-1] == {'key': 'spills', 'tracker_id': 'tracker-x', 'core': False, 'revision': 4, 'changed': True, 'state': 'ready'} and run['errors'] == []
    more_recordings(shop, date(2026, 10, 11))
    shop.now[0] += 60
    quiet = shop.sweep(custom=lambda *args: {'script_run': None})[0]
    assert quiet['scripts'] == [] and len(quiet['outputs']) == 9 and quiet['status'] == 'completed'


def test_a_script_test_is_a_row_of_the_history_and_leaves_the_schedule(shop):
    import tracker_runner as runner
    shop.sweep()
    before, last = shop.runs.state(*shop.key), shop.runs.last_run('peter', shop.location)
    entry = {'key': 'spills', 'script_revision': 2, 'trigger': 'test', 'status': 'failed', 'seconds': 0.4, 'error': {'code': 'script_error', 'message': 'boom', 'line': 2}}
    shop.now[0] += 10
    row = shop.runs.record(*shop.key, trigger='test', status='failed', started_at=shop.now[0] - 1, wall_ms=400,
                           period={'start': 'a', 'end': 'b', 'label': 'c'}, scripts=[entry], error='The script stopped at line 2: boom')
    assert (row['trigger'], row['status'], row['wall_ms'], row['scripts'], row['outputs'], row['error']) == (
        'test', 'failed', 400, [entry], [], 'The script stopped at line 2: boom')
    assert shop.runs.runs(*shop.key, limit=1)[0] == row and row['id'].startswith('trun-')
    assert shop.runs.state(*shop.key) == before, 'a failed test leaves the failures, the waits and the lease as they are'
    assert shop.runs.last_run('peter', shop.location) == last and runner.home_revision(shop.state, 'peter', shop.location)['run']['trigger'] == 'events'
    assert shop.sweep() == [], 'a test starts no pass'
    for trigger, status in (('events', 'completed'), ('test', 'running'), ('manual', 'failed')):
        with pytest.raises(ValueError):
            shop.runs.record(*shop.key, trigger=trigger, status=status, started_at=shop.now[0])
    with pytest.raises(ValueError, match='known run trigger'):
        shop.runs.claim(*shop.key, trigger='test', stamp='s', day='2026-10-11')
    assert 'test' in runner.TRIGGERS and 'test' not in runner.PASS_TRIGGERS


def test_a_ledger_from_before_script_runs_is_read_and_gains_its_columns(shop):
    import tracker_runner as runner
    path = shop.state / 'tracker-runs.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE tracker_runs DROP COLUMN scripts')
        db.execute('DROP TABLE tracker_script_jobs')
    older = runner.TrackerRuns(shop.state, clock=lambda: shop.now[0])
    assert older.runs(*shop.key, limit=1)[0]['scripts'] == [] and older.jobs_pending() == []
    job = {'request_id': 'a' * 32, 'scope': {'company_id': 'c', 'owner_id': 'peter', 'root_chat_id': 'tracker-run-' + 'a' * 32}}
    older.job_open(job, owner='peter', collection='retail-demo', location_id=shop.location, tracker_id='tracker-x')
    assert older.jobs_pending() == [job]
    older.job_open(job, owner='peter', collection='retail-demo', location_id=shop.location, tracker_id='tracker-x')
    assert len(older.jobs_pending()) == 1
    older.job_close(job)
    assert older.jobs_pending() == []
    run = runner.tracker_pass(shop.root, shop.root, clock=lambda: shop.now[0], pending_since={})[0]
    assert run['status'] == 'completed' and run['scripts'] == []
    with sqlite3.connect(path) as db:
        assert 'scripts' in {row[1] for row in db.execute('PRAGMA table_info(tracker_runs)')}


def test_the_service_start_wires_script_trackers_and_clears_the_jobs_a_stopped_process_left(shop):
    import tracker_runner as runner
    import tracker_scripts as scripts
    assert 'tracker_scripts.py' in {path.name for path in runner.engine_sources()}
    held = scripts._BOX
    job = {'request_id': 'b' * 32, 'scope': {'company_id': 'c', 'owner_id': 'peter', 'root_chat_id': 'tracker-run-' + 'b' * 32}}
    shop.runs.job_open(job, owner='peter', collection='retail-demo', location_id=shop.location, tracker_id='tracker-x')
    class Box:
        enabled, event_runs, loop, recovered = True, False, None, []
        async def recover(self, pending, closed=None):
            self.recovered.extend(pending)
            for item in pending:
                closed(item)
            return [item['request_id'] for item in pending]
    async def scenario(environ, box=None):
        state = SimpleNamespace(tracker_runner=runner.runner_state(shop.root, shop.root, environ))
        started = await runner.tracker_start(state, sandbox=box)
        recovery = state.tracker_runner['recovery']
        if recovery is not None:
            await recovery
        loop_is_set = started.loop is asyncio.get_running_loop()
        await runner.tracker_stop(state)
        return state.tracker_runner, started, loop_is_set
    try:
        on, box, loop_is_set = asyncio.run(scenario({}, Box()))
        assert callable(on['custom']) and loop_is_set and scripts.box() is box
        assert box.recovered == [job] and shop.runs.jobs_pending() == [], 'the job a stopped process left is cleared with the broker and closed in the ledger'
        assert on['scripts'] == {'sandbox': False, 'event_runs': False}
        # The host's switches reach the sandbox handle; with the sandbox off no recovery starts.
        plain, made, _ = asyncio.run(scenario({'ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS': 'on'}))
        assert (made.enabled, made.event_runs, plain['recovery']) == (False, True, None) and callable(plain['custom'])
        ready, made, _ = asyncio.run(scenario({'ROCOTO_CUSTOM_EXECUTOR_ENABLED': '1'}, None))
        assert (made.enabled, made.event_runs) == (True, False)
        off, made, _ = asyncio.run(scenario({'ROCOTO_VIDEO_TRACKER_RUNNER': 'off'}))
        assert off['custom'] is None, 'a service that is switched off runs no script either'
        with pytest.raises(ValueError, match='ROCOTO_TILE_SCRIPT_EVENT_RUNS'):
            runner.runner_state(shop.root, shop.root, {'ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS': 'maybe'})
    finally:
        scripts._BOX = held


def test_script_trackers_run_after_the_page_in_a_time_of_their_own(shop, monkeypatch):
    """The pass's own work is the Core Trackers and the page data, inside its time budget. The
    script trackers follow under the same lease, renewed for them, with a time beside that budget:
    what a script job takes leaves the pass's status, its run row's time and the schedule alone."""
    import tracker_runner as runner
    import tracker_scripts as scripts
    from core_trackers import add_tracker, update_tracker
    from core_trackers.run import PASS
    shop.sweep()
    for key in ('spills', 'leaks'):
        add_tracker(shop.company, shop.location, key, key.capitalize(), 'Liquid on the floor.')
        update_tracker(shop.company, shop.location, key, {'processor': {'sandbox': {'kind': 'tracker-python-v1', 'script_id': 'script-' + key, 'revision': 1}}})
    order, elapsed, seen = [], [0.0], []
    real_publish = __import__('import_control_room_bundle').publish_core_trackers
    monkeypatch.setattr('import_control_room_bundle.publish_core_trackers', lambda *args, **kwargs: (order.append('page'), real_publish(*args, **kwargs))[1])
    def custom(tracker, company, location_id, period, data):
        context = PASS.get()
        order.append(tracker['key'])
        seen.append((context['deadline'] - context['clock'](), shop.runs.state(*shop.key)['running']))
        elapsed[0] += 40.0   # a job that takes forty seconds
        return {'script_run': {'key': tracker['key'], 'status': 'unavailable', 'retry': tracker['key'] == 'leaks'}, 'output': None}
    shop.now[0] += 60
    run = runner.run_location(shop.company, shop.location, shop.runs, trigger='events', stamp=stamp(shop), day='2026-10-11',
                              budget_seconds=30, custom=custom, clock=lambda: elapsed[0])
    assert order == ['page', 'leaks', 'spills'], 'the page data is published before a script job starts'
    assert (run['status'], run['error'], run['page_published'], run['wall_ms']) == ('completed', None, True, 0), 'eighty seconds of script jobs cost a 30-second budget nothing'
    assert [round(left) for left, _ in seen] == [scripts.SCRIPT_PASS_SECONDS, scripts.SCRIPT_PASS_SECONDS - 40] and all(running for _, running in seen)
    state = shop.runs.state(*shop.key)
    assert state['failures'] == 0 and state['processed_stamp'] == stamp(shop) and state['next_after'] == shop.now[0] + 60
    assert [(s['key'], s['status']) for s in run['scripts']] == [('leaks', 'unavailable'), ('spills', 'unavailable')]
    # The script that asked for another pass gets one from the service, for itself alone.
    assert (state['requested_at'], state['requested_only']) == (shop.now[0], ['leaks'])
    with sqlite3.connect(shop.state / 'tracker-runs.sqlite') as db:
        assert db.execute('SELECT requested_by FROM tracker_schedule').fetchone()[0] == runner.SERVICE == 'tracker-service'
    # A person's own request made meanwhile stands: the service leaves it as it is.
    shop.runs.request(*shop.key, by='peter')
    shop.now[0] += 120
    order.clear()
    again = shop.sweep(custom=custom)[0]
    assert again['trigger'] == 'manual' and again['only'] is None and shop.runs.state(*shop.key)['requested_only'] == ['leaks']
    # A pass that used up its own budget leaves the scripts for the next one, and a pass with no sandbox lists them as before.
    more_recordings(shop)
    shop.now[0] += 600
    order.clear()
    elapsed[0] = 0.0
    late = runner.run_location(shop.company, shop.location, shop.runs, trigger='events', stamp=stamp(shop), day='2026-10-11',
                               budget_seconds=0, custom=custom, clock=lambda: elapsed[0])
    assert late['status'] == 'budget_exceeded' and order == [] and late['scripts'] == []
    assert scripts.SCRIPT_PASS_SECONDS + runner.SCRIPT_LEASE_MARGIN < runner.LEASE_SECONDS
    # A fault of the script time itself stays there: the pass's own work stands and each script carries the reason.
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(shop.runs, 'renew', broken)
    more_recordings(shop, date(2026, 10, 11))
    shop.now[0] += 7200
    order.clear()
    elapsed[0] = 0.0
    kept = runner.run_location(shop.company, shop.location, shop.runs, trigger='events', stamp=stamp(shop), day='2026-10-11',
                               budget_seconds=30, custom=custom, clock=lambda: elapsed[0])
    assert (kept['status'], kept['page_published'], order, kept['scripts']) == ('completed', True, ['page'], [])
    assert kept['errors'] == [{'key': key, 'error': 'The processor stopped with OperationalError.'} for key in ('leaks', 'spills')]
    assert shop.runs.state(*shop.key)['failures'] == 0


def test_a_pass_on_new_events_before_the_daily_time_leaves_the_day_to_its_pass(shop):
    """A location with a daily time has its day's pass at that time, whatever arrived earlier."""
    assert shop.sweep()[0]['day'] == '2026-10-11'
    shop.runs.set_policy(*shop.key, {'daily_at': '18:00'}, expected_revision=1)
    shop.now[0] = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc).timestamp()
    assert shop.sweep() == [], 'the day turned and its pass waits for 18:00'
    more_recordings(shop)
    early = shop.sweep()[0]
    assert (early['trigger'], early['status'], early['day']) == ('events', 'completed', '2026-10-12')
    assert shop.runs.state(*shop.key)['processed_day'] == '2026-10-11', 'new events recompute the trackers and leave the day as it was'
    shop.now[0] = datetime(2026, 10, 12, 17, 59, tzinfo=timezone.utc).timestamp()
    assert shop.sweep() == []
    shop.now[0] = datetime(2026, 10, 12, 18, 1, tzinfo=timezone.utc).timestamp()
    assert [(run['trigger'], run['day']) for run in shop.sweep()] == [('day', '2026-10-12')] and shop.runs.state(*shop.key)['processed_day'] == '2026-10-12'
    # After the daily time a pass on new events stands for the day, so the day has one pass.
    more_recordings(shop, date(2026, 10, 11))
    shop.now[0] = datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc).timestamp()
    assert [(run['trigger'], run['day']) for run in shop.sweep()] == [('events', '2026-10-13')] and shop.runs.state(*shop.key)['processed_day'] == '2026-10-13'
    shop.now[0] += 3600
    assert shop.sweep() == []


def test_a_trackers_tests_keep_their_own_rows_and_a_removed_tracker_leaves_the_history(shop, monkeypatch):
    import tracker_runner as runner
    shop.sweep()
    passes = lambda: [run['id'] for run in shop.runs.runs(*shop.key, limit=200) if run['trigger'] != 'test']
    tests = lambda key: [run for run in shop.runs.runs(*shop.key, limit=200) if run['trigger'] == 'test' and run['scripts'][0]['key'] == key]
    before = passes()
    for index in range(runner.KEEP_TEST_RUNS + 5):
        for key in ('spills', 'leaks'):
            shop.now[0] += 1
            shop.runs.record(*shop.key, trigger='test', status='failed', started_at=shop.now[0], scripts=[{'key': key, 'status': 'failed', 'n': index}])
    assert len(tests('spills')) == len(tests('leaks')) == runner.KEEP_TEST_RUNS == 10 and passes() == before
    assert [run['scripts'][0]['n'] for run in tests('spills')] == list(range(14, 4, -1)), 'the newest ten'
    # A pass closes: its pruning counts the location's passes, and the tests stay.
    monkeypatch.setattr(runner, 'KEEP_RUNS', 2)
    for index in range(4):
        claimed = shop.runs.claim(*shop.key, trigger='manual', stamp=f's{index}', day='2026-10-11')
        shop.runs.finish(claimed['id'], claimed['lease_token'], status='completed', stamp=f's{index}', outputs=[{'key': 'spills', 'revision': index}, {'key': 'visitors', 'revision': 1}],
                         errors=[{'key': 'spills', 'error': 'x'}], scripts=[{'key': 'spills', 'status': 'failed'}, {'key': 'leaks', 'status': 'succeeded'}])
        shop.now[0] += 1
    assert len(passes()) == 2 and len(tests('spills')) == len(tests('leaks')) == 10
    # Removing a tracker takes its tests and its entries of each pass row; the rows and the other trackers stay.
    assert shop.runs.forget_tracker(*shop.key, 'spills') == 12
    assert tests('spills') == [] and len(tests('leaks')) == 10 and len(passes()) == 2
    for run in shop.runs.runs(*shop.key, limit=200):
        if run['trigger'] != 'test':
            assert run['outputs'] == [{'key': 'visitors', 'revision': 1}] and run['errors'] == [] and run['scripts'] == [{'key': 'leaks', 'status': 'succeeded'}]
    assert shop.runs.forget_tracker(*shop.key, 'spills') == 0 and runner.TrackerRuns(shop.root / 'nowhere').forget_tracker(*shop.key, 'spills') == 0
