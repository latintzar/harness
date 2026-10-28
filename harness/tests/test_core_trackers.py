"""The retail Core Trackers over the preserved Cosmos output, with a synthetic finder result."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

ARCHIVE = Path('/home/peter/.local/state/rocoto/reason2-retail-20261008/vast/review-site/viewer-source')
pytestmark = pytest.mark.skipif(not (ARCHIVE / 'data.json').is_file(), reason='Needs the preserved Cosmos output on this host.')

KEYS = ['visitors', 'product_interactions', 'picked_up', 'put_back', 'basket', 'queue_time', 'empty_shelves', 'sales', 'conversion']
TILE = {'key', 'label', 'value', 'unit', 'state', 'needs', 'series', 'events', 'period'}
EVENT = {'id', 'at', 'label', 'description', 'thumb', 'visitor_id', 'source_id'}
PROVENANCE = re.compile(r'recording|replay|demo|footage|dataset|saved observation|generated|cosmos|archive|synthetic', re.I)
FOUND = {'schema_version': 1, 'found_at': '2026-10-10T18:30:00Z', 'products': [
    {'type': 'green-packaged-item', 'name': 'Fire starter sticks', 'brand': 'Fixture', 'category': 'Camping', 'price': 4.99,
     'currency': 'USD', 'price_source_url': 'https://shop.example/fire-sticks', 'image_url': 'https://shop.example/fire-sticks.jpg', 'confidence': 0.4},
    {'type': 'bottled-beverage', 'name': 'Sparkling water 500 ml', 'brand': None, 'category': 'Beverages', 'price': 1.49,
     'currency': 'USD', 'price_source_url': 'https://shop.example/water', 'image_url': None, 'confidence': 0.3}]}


def run_seed(root, found=None):
    from seed_retail_demo import seed
    return seed(root / 'environments' / 'retail-demo', root, 'retail-demo', 'peter', lifecycle='demo', archive=ARCHIVE,
        found_products=found, day=date(2026, 10, 10), history_days=2, product_types_output=root / 'product_types.json',
        publish_media=False)


@pytest.fixture(scope='module')
def seeded(tmp_path_factory):
    from access import AccessStore
    import retail_records as records
    root = tmp_path_factory.mktemp('retail')
    access = AccessStore(root)
    access.create_environment('retail-demo', 'Retail demo')
    access.issue('retail-demo', owner=True, principal_id='peter')
    first = run_seed(root)
    types_sha = hashlib.sha256((root / 'product_types.json').read_bytes()).hexdigest()
    found = root / 'found_products.json'
    found.write_text(json.dumps(FOUND))
    second = run_seed(root, found)
    page = Path(second['page']['bundle_directory']) / 'data.json'
    second_page = page.read_bytes()
    third = run_seed(root, found)
    third_page = page.read_bytes()
    company = records.company_from_access(root / 'environments' / 'retail-demo', root, 'retail-demo', 'peter', 'retail-demo')
    return {'root': root, 'first': first, 'second': second, 'third': third, 'company': company,
            'location': second['location_id'], 'page': page, 'second_page': second_page, 'third_page': third_page,
            'types_sha': types_sha}


def test_events_come_from_model_answer_files_or_the_marked_history(seeded):
    import retail_records as records
    from cosmos_archive_import import answer_events, archive_windows, sha
    company, location = seeded['company'], seeded['location']
    events = [e for e in company.locations._rows('peter', 'event') if e['location_id'] == location]
    receipts = {r['id']: r for r in records.rows(company, 'event_receipt', location)}
    assert events and {e['id'] for e in events} == set(receipts)
    real = [r for r in receipts.values() if r['data_basis'] == 'model_output']
    assert len(real) == 10 == seeded['first']['ingested']['admitted_events']
    for receipt in real:
        path = ARCHIVE / receipt['answer_file']
        assert path.is_file() and sha(path) == receipt['answer_sha256']
        clip, suffix = path.stem.rsplit('-', 1)
        raws = [raw for window in archive_windows(ARCHIVE, clip, 60.0, suffix) for _, raw in answer_events(window['answer'])]
        assert receipt['raw'] in raws
    rest = [r for r in receipts.values() if r['data_basis'] != 'model_output']
    assert rest and all(r['data_basis'] == 'generated_history' and r['answer_file'] is None
                        and r['source_id'].startswith('history-') for r in rest)
    skipped = seeded['first']['ingested']['skipped_by_reason']
    assert skipped == {'person_label_without_tracked_person': 1, 'recording_without_tracker_output': 14,
                       'second_model_answer_for_recording': 2, 'type_outside_vocabulary': 1}


def test_structure_checks_skip_what_the_tracker_and_vocabulary_do_not_support():
    from core_trackers import load_recipe
    from cosmos_archive_import import validate
    vocabulary = load_recipe('retail')['vocabulary']
    seen = {'P0': [0.0, 1.0, 2.0]}
    check = lambda raw: validate(raw, seen=seen, start_s=0.0, end_s=5.0, vocabulary=vocabulary)[1]
    base = {'person_id': 'P0', 'event': 'pick_up', 'start_s': 1.0, 'end_s': 2.0, 'evidence': 'Takes a box.'}
    assert check(base) is None and check({**base, 'person_id': 'Person 0'}) is None
    assert check({**base, 'person_id': 'P7'}) == 'person_label_without_tracked_person'
    assert check({**base, 'start_s': 4.0, 'end_s': 9.0}) == 'time_outside_recording'
    assert check({**base, 'start_s': 3.5, 'end_s': 4.5}) == 'tracked_person_absent_during_event'
    assert check({**base, 'event': 'fall'}) == 'type_outside_vocabulary'
    assert check({'person_id': None, 'event': 'display_empty', 'start_s': 0, 'end_s': 1, 'evidence': 'Shelf 2 is empty'}) is None


def test_every_tracker_returns_a_valid_tile_and_canvas(seeded):
    data = json.loads(seeded['page'].read_text())
    tiles = data['core_trackers']
    assert [t['key'] for t in tiles] == KEYS and set(data['core_tracker_canvases']) == set(KEYS)
    for tile in tiles:
        assert TILE <= set(tile) and tile['core'] is True and tile['chat'] is False
        assert tile['state'] in {'ready', 'needs'}
        if tile['state'] == 'needs':
            assert tile['value'] is None and 1 <= len(tile['needs'].split()) <= 3
        else:
            assert isinstance(tile['value'], (int, float)) and tile['needs'] is None and len(tile['series']) == 24
        assert all(set(point) == {'t', 'value'} for point in tile['series'])
        assert all(EVENT <= set(event) for event in tile['events'])
        canvas = data['core_tracker_canvases'][tile['key']]
        assert canvas['revision'] == tile['revision'] and canvas['period'] == tile['period']
        assert isinstance(canvas['summary'], str) and canvas['summary'] and isinstance(canvas['rows'], list)
        assert {'company_id', 'location_id', 'camera_ids', 'tracker_id'} <= set(tile) and len(tile['camera_ids']) == 3
    by_key = {t['key']: t for t in tiles}
    assert all(by_key[key]['state'] == 'ready' for key in KEYS)
    assert any(event['thumb'] for event in by_key['product_interactions']['events'])
    canvases = data['core_tracker_canvases']
    assert canvases['conversion']['visitors'] == by_key['visitors']['value']
    assert canvases['queue_time']['distribution'] and 'shelves' in canvases['empty_shelves']
    assert {p['basis'] for p in data['products'] if p['price']} == {'estimated'} and data['currency'] == 'USD'
    assert by_key['sales']['currency'] == 'USD' and by_key['sales']['basis'] == 'estimated'
    assert sum(p['interactions'] for p in data['products']) == by_key['product_interactions']['value']
    assert sum(p['pickups'] for p in data['products']) == by_key['picked_up']['value']
    assert all(p['price_source_url'].startswith('https://') for p in data['products'] if p['price'])
    assert data['baskets'] and all(basket['visit_id'] == visit for visit, basket in data['baskets'].items())
    assert {clip['id'] for clip in data['clips']} >= {'intel-clip2', 'intel-clip8', 'intel-clip14'}


def test_first_run_needs_prices_until_the_finder_result_arrives(seeded):
    first = {t['key']: t for t in seeded['first']['tiles']}
    assert first['sales']['state'] == 'needs' and first['sales']['needs'] == 'Needs catalog prices'
    types = json.loads((seeded['root'] / 'product_types.json').read_text())
    assert [t['type'] for t in types['product_types']] == ['bottled-beverage', 'chilled-packaged-food', 'frozen-food', 'green-packaged-item']
    green = types['product_types'][-1]
    assert green['count'] == 3 and green['readable_text'] == ['Dry Ice'] and green['evidence_crops']
    assert types['shelf_text'] == ['Camping Lantern', 'Poncho']
    assert seeded['second']['found_products']['unfound_types'] == ['chilled-packaged-food', 'frozen-food']


def test_sales_lines_carry_the_estimated_basis_and_till_rows_share_the_tables(seeded):
    import retail_records as records
    from core_trackers.run import run_trackers
    company, location = seeded['company'], seeded['location']
    sales = records.rows(company, 'sale', location)
    lines = records.rows(company, 'sale_line', location)
    assert sales and lines and {s['basis'] for s in sales} == {'estimated'} == {line['basis'] for line in lines}
    assert all(s['provenance']['kind'] == 'basket_at_last_observation' for s in sales)
    canvas = json.loads(seeded['page'].read_text())['core_tracker_canvases']['sales']
    assert canvas['rows'] and all(line['basis'] == 'estimated' for row in canvas['rows'] for line in row['lines'])
    priced = [line for line in lines if line['unit_price']]
    assert priced and all(line['price_source'].startswith('https://shop.example/') for line in priced)
    records.record_till_sales(company, location, [{'transaction_id': 'T-1', 'at': '2026-10-10T12:00:00Z', 'currency': 'USD',
        'lines': [{'label': 'Fire starter sticks', 'quantity': 2, 'unit_price': '4.99', 'product_id': 'catalog-green-packaged-item'}]}])
    output = next(o for o in run_trackers(company, location, only=['sales'])['outputs'])
    assert output['canvas']['basis'] == 'pos' and output['tile']['value'] == 9.98
    with company.locations.connect(True) as db:
        db.execute("DELETE FROM records WHERE owner='peter' AND kind IN ('sale','sale_line') AND id LIKE 'sale-pos-%'")
    assert run_trackers(company, location, only=['sales'])['outputs'][0]['canvas']['basis'] == 'estimated'


def test_running_twice_gives_identical_outputs(seeded):
    assert seeded['second']['tiles'] == seeded['third']['tiles']
    assert seeded['second_page'] == seeded['third_page']
    assert seeded['third']['history']['written'] == 0 and seeded['second']['history']['written'] == 0
    assert seeded['third']['derived'] == seeded['second']['derived']
    assert seeded['third']['ingested'] == seeded['second']['ingested']
    assert hashlib.sha256((seeded['root'] / 'product_types.json').read_bytes()).hexdigest() == seeded['types_sha']


def test_publishing_the_page_data_again_keeps_it_identical(seeded):
    from import_control_room_bundle import publish_core_trackers
    company, location = seeded['company'], seeded['location']
    publish_core_trackers(company, location)
    before = seeded['page'].read_bytes()
    publish_core_trackers(company, location)
    assert seeded['page'].read_bytes() == before


def test_the_host_command_and_the_service_pass_give_identical_outputs(seeded):
    """The seed, the run command and the service's own pass are one pass under one lease."""
    import tracker_runner as runner
    from core_trackers.run import main
    company, location, root = seeded['company'], seeded['location'], seeded['root']
    runs = runner.TrackerRuns(company.state)
    seeds = runs.runs('peter', 'retail-demo', location)
    assert [run['trigger'] for run in seeds] == ['host'] * 3 and all(run['status'] == 'completed' and run['page_published'] for run in seeds)
    assert [output['key'] for output in seeds[0]['outputs']] == KEYS
    main(['--state', str(company.state), '--access-state', str(root), '--environment-id', 'retail-demo', '--owner', 'peter',
          '--location-id', location, '--publish'])
    current = lambda: {o['tracker_key']: (o['revision'], o['content_sha256']) for o in runner.current_outputs(company.state, 'peter', 'retail-demo', location)}
    by_command, page = current(), seeded['page'].read_bytes()
    assert set(by_command) == set(KEYS)
    # The command ran the whole pass, so the service finds nothing to do; asked to, it computes the same.
    assert runner.tracker_pass(root, root, pending_since={}) == []
    runs.request('peter', 'retail-demo', location, by='peter')
    soon = runs.state('peter', 'retail-demo', location)['manual_after'] + 1
    served = runner.tracker_pass(root, root, clock=lambda: soon, pending_since={})
    assert [(run['trigger'], run['status'], run['page_published']) for run in served] == [('manual', 'completed', True)]
    assert [output['key'] for output in served[0]['outputs']] == KEYS and not any(output['changed'] for output in served[0]['outputs'])
    assert current() == by_command and seeded['page'].read_bytes() == page


def test_customer_read_strings_carry_plain_numbers(seeded):
    data = json.loads(seeded['page'].read_text())
    read = []
    def walk(value, key=None):
        if isinstance(value, dict):
            for name, child in value.items():
                walk(child, name)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and key in {'label', 'summary', 'needs', 'description', 'unit', 'product', 'band', 'name', 'title'}:
            read.append(value)
    walk({k: data[k] for k in ('core_trackers', 'core_tracker_canvases', 'products', 'baskets')})
    walk([{'title': clip['title']} for clip in data['clips']])
    assert len(read) > 100
    assert [text for text in read if PROVENANCE.search(text)] == []
    assert 'data_basis' not in json.dumps({k: data[k] for k in ('core_trackers', 'core_tracker_canvases', 'products', 'baskets')})


def test_core_trackers_are_the_locked_retail_recipe_beside_the_customers_own(seeded):
    from core_trackers import LOCKED, add_tracker, install_core_trackers, location_trackers, remove_tracker, update_tracker
    import monitoring_tools
    company, location = seeded['company'], seeded['location']
    installed = install_core_trackers(company, location, 'retail')
    assert [t['key'] for t in installed if t['core']] == KEYS
    assert all(t['company_id'] == company.id and t['location_id'] == location and t['business_type'] == 'retail' for t in installed if t['core'])
    for change in (lambda: remove_tracker(company, location, 'visitors'), lambda: update_tracker(company, location, 'sales', {'label': 'Mine'})):
        with pytest.raises(ValueError, match=re.escape(LOCKED)):
            change()
    own = add_tracker(company, location, 'spills', 'Spills', 'Liquid on the floor of the aisle.', event_types=['spill'])
    assert own['core'] is False and own['chat'] is True

    class Agents:
        def list(self, owner_principal_id=None):
            return [{'id': 'agent-1', 'title': 'Door watch', 'agent_kind': 'tracker', 'dataset': 'retail-demo', 'url': '/?agents=1&agent=agent-1', 'status': 'draft'},
                    {'id': 'agent-2', 'title': 'Weekly report', 'agent_kind': 'action', 'dataset': 'retail-demo'}]
    listing = location_trackers(company.state, 'peter', 'retail-demo', location, agents=Agents())
    assert [t['key'] for t in listing] == KEYS + ['spills', 'agent-1']
    assert all(t['core'] and not t['chat'] and t['locked'] and not t['editable'] and not t['removable'] for t in listing[:9])
    assert all(not t['core'] and t['chat'] and t['editable'] for t in listing[9:])
    assert listing[-1]['kind'] == 'tracking_agent' and set(listing[0]['layers']) == {'tracker', 'processor', 'canvas', 'tile'}
    tool = monitoring_tools.read(company.monitoring, 'peter', 'retail-demo', 'list_monitoring_profiles', {}, agents=Agents())
    assert tool['profiles'] and [t['key'] for t in tool['trackers']] == KEYS + ['spills', 'agent-1']
    assert location_trackers(company.state, 'someone-else', 'retail-demo') == []
    assert remove_tracker(company, location, 'spills')['key'] == 'spills'
    assert [t['key'] for t in install_core_trackers(company, location, 'retail')] == KEYS


def test_found_products_follow_a_strict_contract(seeded, tmp_path):
    from catalog_finder import import_found_products
    company, location = seeded['company'], seeded['location']
    def attempt(change):
        path = tmp_path / 'found.json'
        value = json.loads(json.dumps(FOUND))
        change(value)
        path.write_text(json.dumps(value))
        return import_found_products(company, location, path)
    with pytest.raises(ValueError):
        attempt(lambda v: v['products'][0].update(type='unlisted-type'))
    with pytest.raises(ValueError):
        attempt(lambda v: v['products'][0].update(price_source_url='ftp://shop.example/x'))
    with pytest.raises(ValueError):
        attempt(lambda v: v['products'][0].update(downloaded_image='/tmp/x.jpg'))
    with pytest.raises(ValueError):
        attempt(lambda v: v['products'].append(v['products'][0]))
    assert attempt(lambda v: None)['products'] == 2


def test_history_plan_is_deterministic_and_complete():
    from retail_demo_history import plan
    products = [('green-packaged-item', 'A green product from the shelf'), ('frozen-food', 'Frozen food products')]
    first, second = plan(products, date(2026, 10, 9), days=30), plan(products, date(2026, 10, 9), days=30)
    assert first == second and 250 <= len(first) <= 480
    fields = {'person_id', 'event', 'start_s', 'end_s', 'object_ref', 'item_description', 'recipient_person_id', 'object_box',
              'object_box_at_s', 'reason_hypothesis', 'location_description', 'evidence', 'certainty', 'id_association',
              'continues_from_previous', 'continues_after_clip'}
    kinds = set()
    for recording in first:
        assert recording['duration_s'] <= 58 and set(recording['answer']) == {'people', 'objects', 'events', 'coverage'}
        for event in recording['answer']['events']:
            assert set(event) == fields and 0 <= event['start_s'] <= event['end_s'] <= recording['duration_s']
            kinds.add(event['event'])
    assert kinds == {'reach_toward', 'pick_up', 'examine_object', 'put_back', 'place_in_basket', 'entered_queue',
                     'reached_checkout', 'presented_card', 'handed_cash', 'display_empty', 'restocked'}
    by_day = {}
    for recording in first:
        by_day[recording['source_id'][8:16]] = by_day.get(recording['source_id'][8:16], 0) + 1
    assert len(by_day) == 30 and by_day['20261003'] > by_day['20261004']  # Saturday is busier than Sunday.


def test_a_later_bundle_import_keeps_the_trackers_page_data(seeded, tmp_path):
    from control_room_routes import read_bundle
    from test_import_control_room_bundle import fixture, install
    run = tmp_path / 'run'
    reviews, cameras = fixture(run)
    company = seeded['company']
    selected = {'state': company.state, 'monitoring': company.monitoring}
    assert read_bundle(selected, 'peter') is not None  # licensed footage, shown as its publisher released it
    manifest_path = seeded['page'].with_name('manifest.json')
    manifest = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps({**manifest, 'privacy': {k: v for k, v in manifest['privacy'].items() if k != 'licence'}}))
    assert read_bundle(selected, 'peter') is None  # as published with no licence named: the page gate refuses it
    manifest_path.write_text(json.dumps(manifest))
    before = json.loads(seeded['page'].read_text())
    install(run, company.state, reviews, cameras)
    after = json.loads(seeded['page'].read_text())
    for key in ('core_trackers', 'core_tracker_canvases', 'products', 'baskets'):
        assert after[key] == before[key]
    ids = [clip['id'] for clip in after['clips']]
    assert {'retailaction-000027-rank0', 'intel-clip2'} <= set(ids) and len(ids) == len(set(ids))
    manifest = json.loads((seeded['page'].parent / 'manifest.json').read_text())
    assert 'intel-clip2' in manifest['source_ids'] and any(name.startswith('sources/intel-clip2/') for name in manifest['files'])
    assert manifest['privacy']['sources']['intel-clip2']['face_redaction'] == 'source_as_published'
    assert read_bundle({'state': company.state, 'monitoring': company.monitoring}, 'peter') is not None


def test_a_tracking_agent_with_a_record_is_one_row_of_its_location(seeded):
    """Promotion links a Tracking Agent to its tracker record; the location then lists one row for the two."""
    from core_trackers import agent_tracker, agent_tracker_key, link_tracking_agent, location_trackers, remove_tracker, update_tracker
    company, location = seeded['company'], seeded['location']
    agent = {'id': 'agent-0123456789abcdef0123456789abcdef', 'title': 'Door watch', 'description': 'People at the door.', 'instruction': 'Watch the door.'}

    class Agents:
        def list(self, owner_principal_id=None):
            return [{**agent, 'agent_kind': 'tracker', 'dataset': 'retail-demo', 'url': '/?agents=1&agent=' + agent['id'], 'status': 'active'},
                    {'id': 'agent-2', 'title': 'Shelf watch', 'agent_kind': 'tracker', 'dataset': 'retail-demo', 'url': None, 'status': 'draft'}]
    record = link_tracking_agent(company, location, agent, event_types=['entered'])
    try:
        assert record['key'] == agent_tracker_key(agent['id']) == 'agent_0123456789abcdef0123456789abcdef' and record['agent_id'] == agent['id']
        assert record['label'] == 'Door watch' and record['tracker'] == {'observes': 'People at the door.', 'stream': 'events', 'event_types': ['entered']}
        assert record['core'] is False and record['processor']['script'] is None
        assert link_tracking_agent(company, 'another-location', agent) == record == agent_tracker(company.state, 'peter', 'retail-demo', agent['id'])
        listing = location_trackers(company.state, 'peter', 'retail-demo', location, agents=Agents())
        assert [t['key'] for t in listing] == KEYS + [record['key'], 'agent-2']
        mine = listing[9]
        assert (mine['id'], mine['agent_id'], mine['location_id'], mine['kind'], mine['status'], mine['url']) == (
            record['id'], agent['id'], location, 'tracking_agent', 'active', '/?agents=1&agent=' + agent['id'])
        assert listing[10]['location_id'] is None and 'agent_id' in listing[10]
        # Listed for another location, the linked agent stays with its own: zero rows there.
        assert [t['key'] for t in location_trackers(company.state, 'peter', 'retail-demo', 'another-location', agents=Agents())] == ['agent-2']
        # A change sent with a script name keeps the record's: a company's code reaches the sandbox alone.
        assert update_tracker(company, location, record['key'], {'processor': {'script': 'os', 'summary': 'script'}})['processor'] == {'summary': 'script', 'script': None}
    finally:
        remove_tracker(company, location, record['key'])
    assert agent_tracker(company.state, 'peter', 'retail-demo', agent['id']) is None
