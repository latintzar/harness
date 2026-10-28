"""A tracker's presentation: the kit, the saved document, its checks, its tools and the page rows it gives.

The harness writes how a tracker is shown. These tests hold the kit an agent reads, the three
checks a save meets (size, kit, the output's own fields), the lock of a Core Tracker, the
revisions, and the rows Home and the canvas read. No provider and no model is called here.
"""
import asyncio
from copy import copy
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from core_trackers import presentation as kit

HERE = Path(__file__).parents[1]
NEGATIVE = re.compile(r"(?i)(^|[\s>«(·'\"`])(no|not|nothing|never|nobody|none|don't|doesn't|can't|cannot|won't|isn't|aren't|didn't|wasn't|hasn't|haven't|without|unless)\b")
READ, WRITE = 'get_tracker_presentation', 'save_tracker_presentation'
POINTS = [{'t': f'2026-10-11T{hour:02d}:00:00Z', 'value': hour % 5} for hour in range(24)]
ROWS = [{'id': f'event-{n}', 'at': f'2026-10-11T1{n}:05:00Z', 'label': 'Door open', 'description': f'The freezer door stayed open, time {n + 1}.',
         'thumb': None, 'product': None} for n in range(3)]
# The first worked example: a count with a history line and a list of instances.
FREEZER = {'schema': kit.SCHEMA_ID,
    'tile': {'label': 'Freezer door left open', 'mark': 'line', 'format': 'count', 'unit': 'times', 'bind': {'series': 'series'}},
    'canvas': {'family': 'metric', 'blocks': [{'block': 'figure'}, {'block': 'period'}, {'block': 'line', 'bind': {'series': 'series'}},
        {'block': 'summary', 'bind': {'text': 'summary'}}, {'block': 'instances', 'title': 'Times it stayed open', 'bind': {'rows': 'rows'}}]}}
# The second: a rate with a ring and a live timeline with comments.
CHECKS = {'schema': kit.SCHEMA_ID, 'tile': {'label': 'Shelf checks on time', 'mark': 'ring', 'format': 'rate'},
    'canvas': {'family': 'live', 'blocks': [{'block': 'figure'}, {'block': 'gauge', 'chart': 'ring', 'bind': {'fraction': 'ratio'}},
        {'block': 'key_points', 'bind': {'items': 'key_points'}}, {'block': 'timeline', 'bind': {'rows': 'rows', 'comments': 'memos'}}]}}


def output(value=14, unit='times', **canvas):
    """A tracker output in the shape `core_trackers.common.publish` writes."""
    tile = {'key': 'freezer', 'label': 'Freezer door', 'value': value, 'unit': unit, 'state': 'ready', 'needs': None, 'series': POINTS,
            'events': ROWS, 'period': {'start': '2026-10-11T00:00:00Z', 'end': '2026-10-12T00:00:00Z', 'label': 'Sun 11 Oct 2026'},
            'trend': POINTS[:7], 'summary': 'The door stayed open 14 times.', 'revision': 3}
    view = {'key': 'freezer', 'label': 'Freezer door', 'family': 'occurrences', 'value': value, 'unit': unit, 'state': 'ready', 'needs': None,
            'period': tile['period'], 'summary': tile['summary'], 'series': POINTS, 'history': POINTS[:7], 'rows': ROWS, 'total_rows': 3,
            'revision': 3, **canvas}
    return {'tile': tile, 'canvas': view, 'revision': 3}


def refused(spec, **more):
    with pytest.raises(ValueError) as caught:
        kit.validate_presentation(spec, **more)
    sentence = str(caught.value)
    assert not NEGATIVE.search(sentence), sentence
    return sentence


def changed(spec, path, value=KeyError):
    """A copy of a presentation with one value set, or one key removed."""
    made = json.loads(json.dumps(spec)); at = made
    for key in path[:-1]:
        at = at[key]
    if value is KeyError:
        del at[path[-1]]
    else:
        at[path[-1]] = value
    return made


def test_the_kit_states_what_every_mark_family_and_block_needs():
    script = (HERE / 'verticals/retail/web/tracker-canvas.js').read_text()
    drawn = {name: (label, needs) for name, label, needs in re.findall(r"\{ id: '(\w+)', name: '([^']+)', needs: '([^']+)'", script)}
    assert set(kit.FAMILIES) == set(drawn) == {'metric', 'live', 'occurrences', 'state', 'ranking'}
    for name, family in kit.FAMILIES.items():
        assert (family['name'], family['needs']) == drawn[name], 'the kit and the canvas say one sentence'
        assert set(family['requires']) <= set(family['allows']) <= set(kit.BLOCKS) and family['rail'] in family['allows']
        assert [block['block'] for block in family['default']] and set(family['requires']) <= {block['block'] for block in family['default']}
        kit.validate_presentation({'schema': kit.SCHEMA_ID, 'tile': FREEZER['tile'], 'canvas': {'family': name, 'blocks': kit.family_blocks(name)}})
    sentences = [family['needs'] for family in kit.FAMILIES.values()] + [block['needs'] for block in kit.BLOCKS.values()] + [mark['needs'] for mark in kit.MARKS.values()]
    assert all(re.fullmatch(r'Needs [a-z ,]+\.', sentence) for sentence in sentences), sentences
    for block in kit.BLOCKS.values():
        assert block['draws'] and all(shape.rstrip('?') in kit.SHAPES for shape in [*block['fields'].values(), *(block.get('reads') or {}).values()])
    assert set(kit.FITS) == set(kit.SHAPES) - {'readings'}
    assert set(kit.FORMATS) == {'count', 'money', 'duration', 'rate'} and set(kit.MARKS) == {'line', 'bars', 'ring', 'arc'}
    # Everything a model or a person reads here says what is.
    import tracker_presentation as tools
    read = [json.dumps(kit.KIT), *[spec['description'] for spec in tools.specs(True)], *[json.dumps(spec['inputSchema']) for spec in tools.specs(True)],
            *[value for value in vars(kit).values() if isinstance(value, str)], *[value for value in vars(tools).values() if isinstance(value, str) and ' ' in value]]
    assert [text for text in read if NEGATIVE.search(text)] == []
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(kit.SCHEMA)
    assert kit.SCHEMA['$id'] == kit.KIT['presentation'] == kit.SCHEMA_ID


def test_the_two_examples_pass_and_each_wrong_document_names_what_to_change():
    assert kit.validate_presentation(FREEZER) == FREEZER and kit.validate_presentation(CHECKS) == CHECKS
    assert kit.validate_presentation(changed(FREEZER, ['tile', 'label'], '  Freezer   door ')) ['tile']['label'] == 'Freezer door'
    blocks = FREEZER['canvas']['blocks']
    wrong = {
        'a block outside the kit': (changed(FREEZER, ['canvas', 'blocks'], blocks + [{'block': 'heatmap'}]), '«heatmap» is outside the kit. Choose each block from the kit: figure,'),
        'a timeline inside the chart family': (changed(FREEZER, ['canvas', 'blocks'], blocks + [{'block': 'timeline', 'bind': {'rows': 'rows'}}]), 'The metric canvas takes these blocks: figure, period, line,'),
        'the chart family with its line gone': (changed(FREEZER, ['canvas', 'blocks'], [blocks[0], blocks[4]]), 'The metric canvas keeps its figure and line blocks. Needs a number over time.'),
        'a unit on a rate': (changed(CHECKS, ['tile', 'unit'], 'points'), 'The money, duration and rate formats write their own unit.'),
        'a ring on a count with its share unbound': (changed(CHECKS, ['tile', 'format'], 'count'), 'A ring or an arc shows a share of a whole.'),
        'a binding that walks a path': (changed(FREEZER, ['canvas', 'blocks', 2, 'bind', 'series'], 'canvas.series[0]'), 'A binding names one field of the output'),
        'a fourth part': ({**FREEZER, 'style': {'color': 'red'}}, 'A presentation has three parts'),
        'a block twice': (changed(FREEZER, ['canvas', 'blocks'], blocks + [blocks[3]]), 'Use each block once.'),
        'a block with its binding gone': (changed(FREEZER, ['canvas', 'blocks', 2], {'block': 'line'}), 'The line block binds series. Needs a number over time.'),
        'a mark outside the kit': (changed(FREEZER, ['tile', 'mark'], 'donut'), 'Choose the tile mark from the kit: line, bars, ring, arc.'),
        'a family outside the kit': (changed(FREEZER, ['canvas', 'family'], 'heat'), 'Choose the canvas family from the kit: metric, live, occurrences, state, ranking.'),
        'a style on a block': (changed(FREEZER, ['canvas', 'blocks', 0], {'block': 'figure', 'color': 'red'}), 'The figure block stands by its name alone.'),
        'nine blocks': (changed(FREEZER, ['canvas', 'blocks'], blocks * 2), 'Give the canvas 1 to 8 blocks.'),
        'another schema': (changed(FREEZER, ['schema'], 'v0'), 'Set schema to rocoto.tracker-presentation.v1.'),
    }
    for case, (spec, start) in wrong.items():
        assert start in refused(spec), case
    for value in (None, [], 'tile', 3):
        assert refused(value) == kit.SHAPE


def test_an_oversize_presentation_is_refused_before_anything_reads_it():
    assert kit.LIMITS['bytes'] == 4096
    padded = changed(FREEZER, ['canvas', 'blocks', 4, 'title'], 'x' * 5000)
    assert refused(padded) == 'A presentation holds up to 4,096 bytes: one tile and up to 8 blocks.'
    # The size is the first check: a document this large is weighed, and its content stays unread.
    assert refused({'schema': 'other', 'tile': 'x' * 5000}) == kit.TOO_LARGE
    assert len(json.dumps(FREEZER)) < kit.LIMITS['bytes'] / 4


def test_a_binding_the_output_lacks_answers_with_the_blocks_needs():
    published = output(ratio=.865, key_points=['One point.'], memos=[{'id': 'm1', 'at': '2026-10-11T12:00:00Z', 'text': 'A calm hour.'}])
    assert kit.check_output(FREEZER, published['tile'], published['canvas']) == [] and kit.check_output(CHECKS, published['tile'], published['canvas']) == []
    assert kit.validate_presentation(FREEZER, output=published) == FREEZER
    missing = changed(FREEZER, ['canvas', 'blocks', 2, 'bind', 'series'], 'hourly')
    problem, = kit.check_output(missing, published['tile'], published['canvas'])
    assert (problem['block'], problem['field'], problem['bound_to'], problem['needs']) == ('line', 'series', 'hourly', 'Needs a number over time.')
    assert refused(missing, output=published) == ('The line block binds series to «hourly», which is a name the output leaves out. '
                                                  'Needs a number over time. Output fields that fit: history, series.')
    # A field of another shape is told by what it holds.
    assert refused(changed(FREEZER, ['canvas', 'blocks', 2, 'bind', 'series'], 'summary'), output=published).startswith(
        'The line block binds series to «summary», which is text. Needs a number over time.')
    assert refused(CHECKS, output=output(ratio=86.5, key_points=[], memos=[])).startswith('The gauge block binds fraction to «ratio», which is a number. Needs a share of a whole.')
    assert refused(CHECKS, output=output()).startswith('The gauge block binds fraction to «ratio», which is a name the output leaves out.')
    # The tile's own bindings are checked the same way.
    assert refused(changed(FREEZER, ['tile', 'bind', 'series'], 'trend'), output={**published, 'tile': {**published['tile'], 'trend': []}}).startswith(
        'The tile binds series to «trend», which is a list of 0 entries. Needs a number over time.')
    # A text block also reads a text slot the tracker's agent writes.
    slots = output(texts=[{'slot': 'memo', 'facts': {'count': 14}, 'text': 'Fourteen times today.', 'written_at': '2026-10-11T12:00:00Z'}])
    memo = changed(FREEZER, ['canvas', 'blocks', 3, 'bind', 'text'], 'memo')
    assert kit.validate_presentation(memo, output=slots) == memo and kit.output_fields(slots['tile'], slots['canvas'])['slots'] == ['memo']
    # A tracker that still needs its source is checked once its numbers arrive.
    waiting = output(value=None); waiting['tile'].update(state='needs', series=[]); waiting['canvas'].update(state='needs', rows=[])
    assert kit.check_output(missing, waiting['tile'], waiting['canvas']) == []
    fields = kit.output_fields(published['tile'], published['canvas'])
    assert fields['canvas']['series'] == ['points'] and fields['canvas']['ratio'] == ['number', 'share'] and fields['canvas']['rows'] == ['rows', 'timed_rows']
    assert fields['tile'] == {'series': ['points'], 'trend': ['points']} and 'revision' not in fields['canvas'] and 'key' not in fields['canvas']
    families = {family['id']: family['available'] for family in kit.families_for(published)}
    assert families == {'metric': True, 'live': True, 'occurrences': True, 'state': False, 'ranking': False}
    assert all('available' not in family for family in kit.families_for(None))


def test_a_tile_row_carries_the_names_home_reads_and_a_rate_of_86_5_reads_as_86_5_percent():
    published = output(value=86.5, unit='points', ratio=.865)
    row = kit.apply_to_tile({**published['tile'], 'core': False, 'periods': {'week': {'value': 91.2, 'unit': 'points', 'series': POINTS, 'canvas': {**published['canvas'], 'ratio': .912}}}},
                            {**CHECKS, 'tile': {**CHECKS['tile'], 'bind': {'fraction': 'ratio'}}, 'revision': 4}, published['canvas'], agent_id='agent-1')
    assert (row['label'], row['kind'], row['mark'], row['view'], row['presentation_revision'], row['agent_id']) == ('Shelf checks on time', 'rate', 'ring', 'rate', 4, 'agent-1')
    assert (row['value'], row['unit'], row['fraction']) == (86.5, '%', .865), 'a rate above 1 is the percentage itself, whatever unit came with it'
    assert (row['periods']['week']['unit'], row['periods']['week']['fraction'], row['periods']['week']['canvas']['presentation']['family']) == ('%', .912, 'live')
    # The page reads it as 86.5 %, the same on the tile and wherever the row is read.
    script = "const M=require(process.argv[1]),t=M.from({location:{id:'x'},core_trackers:[JSON.parse(process.argv[2])]},{period:'month'}).tiles[0];console.log(JSON.stringify([t.text,t.unit,t.fraction,t.kind,t.mark,t.core,t.custom,t.stage]))"
    import subprocess
    read = lambda value: json.loads(subprocess.run(['node', '-e', script, str(HERE / 'verticals/retail/web/control-room/home-model.js'), json.dumps(value)], check=True, capture_output=True, text=True).stdout)
    assert read(row) == ['86.5', '%', .865, 'rate', 'ring', False, True, False]
    ratio = kit.apply_to_tile({**output(value=.865, unit=None)['tile'], 'core': False}, CHECKS)
    assert ratio['unit'] is None and read(ratio)[:3] == ['86.5', '%', .865], 'a ratio reads as its percentage'
    stated = kit.apply_to_tile({**output(value=.5, unit='%')['tile'], 'core': False}, CHECKS)
    assert stated['unit'] == '%' and read(stated)[:2] == ['0.5', '%'], 'a script that says % is read as it says'
    # A count takes the presentation's own unit, a trend binding gives the tile its 30 daily values, and a duration keeps its seconds.
    count = kit.apply_to_tile({**output()['tile'], 'core': False}, changed(FREEZER, ['tile', 'bind', 'series'], 'trend'))
    assert (count['unit'], count['kind'], count['mark'], count['view'], count['series']) == ('times', 'count', 'line', 'count', POINTS[:7]) and read(count)[:2] == ['14', 'times']
    plain = kit.apply_to_tile({**output(unit='doors')['tile'], 'core': False}, changed(FREEZER, ['tile', 'unit']))
    assert plain['unit'] == 'doors', 'a count whose presentation names no unit keeps the one its script published'
    wait = kit.apply_to_tile({**output(value=95, unit='s')['tile'], 'core': False}, changed(changed(FREEZER, ['tile', 'format'], 'duration'), ['tile', 'unit']))
    assert read(wait)[:2] == ['1:35', 'min']
    assert kit.apply_to_tile({**output()['tile'], 'core': False}, {**FREEZER, 'canvas': {'family': 'state', 'blocks': []}})['view'] == 'shelves'


def test_text_a_script_wrote_reaches_the_page_with_its_links_inert():
    link = 'Refill now: [open the order](https://example.test/pay) today.'
    published = output(summary_note=link, key_points=[link], memos=[{'id': 'm', 'at': '2026-10-11T12:00:00Z', 'text': link, 'recommendation': link}],
                       texts=[{'slot': 'memo', 'facts': {}, 'text': link, 'written_at': None}])
    published['canvas']['summary'] = published['tile']['summary'] = link
    spec = {**CHECKS, 'canvas': {'family': 'live', 'blocks': [{'block': 'summary', 'bind': {'text': 'summary_note'}}, {'block': 'key_points', 'bind': {'items': 'key_points'}},
        {'block': 'timeline', 'bind': {'rows': 'rows', 'comments': 'memos'}}]}, 'revision': 2}
    page = kit.canvas_for_page(published['canvas'], spec)
    plain = 'Refill now: open the order today.'
    assert page['summary'] == page['summary_note'] == plain and page['key_points'] == [plain]
    assert page['memos'] == [{'id': 'm', 'at': '2026-10-11T12:00:00Z', 'text': plain, 'recommendation': plain}] and page['texts'][0]['text'] == plain
    assert 'https://' not in json.dumps(page) and published['canvas']['summary'] == link, 'the stored output keeps what its script wrote'
    assert kit.apply_to_tile({**published['tile'], 'core': False}, spec)['summary'] == plain
    assert page['presentation'] == {'family': 'live', 'blocks': spec['canvas']['blocks'], 'revision': 2} and page['problems'] == []
    # A binding the output stops filling is named on the page, with the block's own sentence.
    gone = kit.canvas_for_page({k: v for k, v in published['canvas'].items() if k != 'key_points'}, spec)
    assert [(p['block'], p['needs']) for p in gone['problems']] == [('key_points', 'Needs one to five short points.')]


# ── With a company's stores ────────────────────────────────────────────────────────────────────

class View:
    """The scoped index view a turn or an MCP read arrives with."""
    def __init__(self, scopes, current=True):
        self.business_scopes, self.business_scope_current = scopes, lambda: current
    def get_clip(self, key):
        return None


@pytest.fixture
def shop(tmp_path):
    """A company with one location: its Core Trackers, one tracker of its own with a published
    output, a page, and a hosted agent with its stores and no model."""
    from access import AccessStore
    from business_scope import ScopeActor
    from core_trackers import add_tracker, common, install_core_trackers
    from import_control_room_bundle import publish_core_trackers
    from monitoring import monitoring_profile_authorized
    from organization_chats import OrganizationChats
    import retail_records as records
    from seed_control_room import seed_location
    root = tmp_path / 'state'
    access = AccessStore(root)
    state = access.create_environment('shop', 'Corner shop')
    access.issue('shop', owner=True, principal_id='peter')
    seeded = seed_location(state, root / 'access.sqlite', 'shop', 'peter', lifecycle='demo', name='Corner shop')
    location = seeded['location_id']
    company = records.company_from_access(state, root, 'shop', 'peter', 'retail-demo')
    install_core_trackers(company, location, 'retail')
    tracker = add_tracker(company, location, 'freezer', 'Freezer door', 'Each time the freezer door stays open.', agent_id='agent-freezer')
    def publish(value=14, unit='times', **canvas):
        data = common.read_location(company, location)
        return common.publish(data, 'freezer', common.day_period(__import__('datetime').date(2026, 10, 11)), value=value, unit=unit, series=POINTS,
            trend=POINTS[:7], events=ROWS, canvas={'rows': ROWS, 'total_rows': 3, **canvas}, summary='The door stayed open 14 times.')
    publish()
    publish_core_trackers(company, location, location_name='Corner shop')
    host = {'state': state, 'business_scope': company.scope, 'location_intelligence': company.locations, 'monitoring': company.monitoring,
            'environment_id': 'shop', 'index': View([])}
    store = copy(company.monitoring)
    store.profile_authorized = lambda owner, record: monitoring_profile_authorized(host, owner, record)
    agent = SimpleNamespace(children={}, monitoring_store=store, automation_store=None, business_scope_host=host, retrieval=View([]),
                            _turn_senders={}, _save_child=lambda record: None)
    agent.organization_chats = OrganizationChats(state, environment_id='shop', member=company.scope.member, records=lambda: agent.children)
    agent.children['chat'] = {'id': 'chat', 'dataset': 'retail-demo', 'owner_principal_id': 'peter', 'messages': []}
    view = lambda actor='peter': View([company.scope.effective(ScopeActor(company.id, actor), 'retail-demo')])
    def call(name, args, *, chat='chat', run='active'):
        import monitoring_tools
        return monitoring_tools.call(agent, chat, 'retail-demo', name, args, {'cancel': asyncio.Event()} if run == 'active' else run, view())
    from control_room_routes import owner_directory
    return SimpleNamespace(root=root, state=state, company=company, location=location, tracker=tracker, call=call, publish=publish, agent=agent,
                           page=lambda: json.loads((owner_directory(state, 'peter') / 'data.json').read_text()))


def rows(shop, kind):
    import retail_records as records
    return records.rows(shop.company, kind, shop.location)


def test_the_read_is_listed_on_the_hosted_native_and_public_surfaces_and_the_write_in_the_hosted_parent_chat():
    import monitoring_tools
    import native_mcp
    import public_mcp
    from agent_bridge import tool_specs
    parent, child = {s['name']: s for s in tool_specs(True)}, {s['name']: s for s in tool_specs(False)}
    assert READ in monitoring_tools.READ_NAMES and WRITE in monitoring_tools.WRITE_NAMES
    assert {READ, WRITE} <= set(parent) and READ in child and WRITE not in child
    assert READ in public_mcp.READ_TOOLS and READ in native_mcp.READ_TOOLS and WRITE not in public_mcp.READ_TOOLS | native_mcp.READ_TOOLS
    for name in (READ, WRITE):
        schema = parent[name]['inputSchema']
        assert schema['additionalProperties'] is False and schema['required'] == ['tracker_id']
        assert not {'owner', 'owner_principal_id', 'principal_id', 'dataset'} & set(schema['properties'])


def test_an_agent_reads_the_kit_the_output_fields_and_what_stays_unfilled(shop):
    read = shop.call(READ, {'tracker_id': shop.tracker['id']})
    assert read['kit'] == kit.KIT and read['presentation']['revision'] == 0 and read['presentation']['saved_by'] is None
    assert (read['key'], read['core'], read['locked'], read['agent_id'], read['location_id']) == ('freezer', False, False, 'agent-freezer', shop.location)
    assert read['presentation']['tile'] == {'label': 'Freezer door', 'mark': 'line', 'format': 'count', 'bind': {'series': 'series'}}
    assert [block['block'] for block in read['presentation']['canvas']['blocks']] == ['figure', 'facts', 'summary', 'instances']
    assert (read['output']['revision'], read['output']['value'], read['output']['unit']) == (1, 14, 'times')
    assert read['output']['fields']['canvas']['rows'] == ['rows', 'timed_rows'] and read['output']['fields']['canvas']['series'] == ['points']
    assert {family['id']: family['available'] for family in read['families']} == {'metric': True, 'live': True, 'occurrences': True, 'state': False, 'ranking': False}
    assert read['problems'] == [] and read['revisions'] == []
    # The tracker is also named by its Tracking Agent's id; a principal and a file path stay out of the answer.
    assert shop.call(READ, {'tracker_id': 'agent-freezer'})['tracker_id'] == shop.tracker['id']
    assert 'peter' not in json.dumps(read) and str(shop.state) not in json.dumps(read)
    for wrong in ({'tracker_id': 'tracker-elsewhere'}, {'tracker_id': ''}):
        with pytest.raises(ValueError, match='Choose a tracker from list_trackers.'):
            shop.call(READ, wrong)
    with pytest.raises(ValueError, match='Use the declared fields of this tool.'):
        shop.call(READ, {'tracker_id': shop.tracker['id'], 'owner': 'peter'})


def test_a_core_tracker_answers_locked(shop):
    from core_trackers import LOCKED
    before = (rows(shop, 'tracker_presentation'), rows(shop, 'tracker_presentation_revision'), shop.page())
    for core in [t for t in rows(shop, 'tracker') if t['core']][:3]:
        with pytest.raises(ValueError) as caught:
            shop.call(WRITE, {'tracker_id': core['id'], 'tile': FREEZER['tile']})
        assert str(caught.value) == LOCKED
        with pytest.raises(ValueError) as direct:
            kit.save_presentation(shop.company, core, {'canvas': {'family': 'live'}})
        assert str(direct.value) == LOCKED
    assert (rows(shop, 'tracker_presentation'), rows(shop, 'tracker_presentation_revision'), shop.page()) == before


def test_a_save_is_checked_against_the_output_and_keeps_every_revision(shop):
    from core_trackers import tracker_records
    tracker = shop.tracker['id']
    definition = tracker_records(shop.state, 'peter', 'retail-demo')
    first = shop.call(WRITE, {'tracker_id': tracker, 'expected_revision': 0, 'tile': FREEZER['tile'], 'canvas': FREEZER['canvas']})
    assert first['presentation']['revision'] == 1 and first['presentation']['saved_by'] == 'harness' and first['presentation']['saved_at'].endswith('Z')
    assert first['presentation']['tile'] == FREEZER['tile'] and first['presentation']['canvas'] == FREEZER['canvas'] and first['page_updated'] is True
    # The write is the two presentation rows: the tracker's definition, its output and its script stay as they were.
    assert tracker_records(shop.state, 'peter', 'retail-demo') == definition
    # A canvas named by its family alone takes that family's own blocks; the tile stays.
    second = shop.call(WRITE, {'tracker_id': tracker, 'expected_revision': 1, 'canvas': {'family': 'live'}})['presentation']
    assert second['revision'] == 2 and second['tile'] == FREEZER['tile'] and second['canvas'] == {'family': 'live', 'blocks': [{'block': 'figure'}, {'block': 'timeline', 'bind': {'rows': 'rows'}}]}
    # The revision read is the revision changed.
    for stale in ({'expected_revision': 1}, {}, {'expected_revision': 7}):
        with pytest.raises(ValueError, match='Read the presentation again with get_tracker_presentation'):
            shop.call(WRITE, {'tracker_id': tracker, **stale, 'canvas': {'family': 'metric'}})
    # A refused change leaves the saved one in place: a block outside the kit, a field the output lacks, an oversize document.
    refusals = [({'canvas': {'family': 'metric', 'blocks': FREEZER['canvas']['blocks'] + [{'block': 'heatmap'}]}}, 'is outside the kit'),
                ({'canvas': {'family': 'state'}}, 'The state_counts block binds shelves to «shelves», which is a name the output leaves out. Needs shelf states.'),
                ({'canvas': changed(FREEZER, ['canvas', 'blocks', 2, 'bind', 'series'], 'hourly')['canvas']}, 'which is a name the output leaves out. Needs a number over time.'),
                ({'tile': {**FREEZER['tile'], 'label': 'x' * 5000}}, 'A presentation holds up to 4,096 bytes'),
                ({}, 'Name the tile or the canvas to change.')]
    for change, sentence in refusals:
        with pytest.raises(ValueError) as caught:
            shop.call(WRITE, {'tracker_id': tracker, 'expected_revision': 2, **change})
        assert sentence in str(caught.value) and not NEGATIVE.search(str(caught.value))
    read = shop.call(READ, {'tracker_id': tracker})
    assert read['presentation'] == second and [row['revision'] for row in read['revisions']] == [2, 1]
    assert shop.call(READ, {'tracker_id': tracker, 'revision': 1})['presentation'] == first['presentation'], 'the prior revision stays readable'
    with pytest.raises(ValueError, match='Choose a revision this presentation has kept.'):
        shop.call(READ, {'tracker_id': tracker, 'revision': 9})
    assert [row['revision'] for row in rows(shop, 'tracker_presentation')] == [2] and len(rows(shop, 'tracker_presentation_revision')) == 2
    # A child conversation and a stopped run read; the save stays with the active parent conversation.
    shop.agent.children['child'] = {'id': 'child', 'dataset': 'retail-demo', 'owner_principal_id': 'peter', 'parent_id': 'chat', 'messages': []}
    assert shop.call(READ, {'tracker_id': tracker}, chat='child')['presentation']['revision'] == 2
    for where in ({'chat': 'child'}, {'run': None}):
        with pytest.raises(ValueError, match='Save tracker changes from the active parent conversation.'):
            shop.call(WRITE, {'tracker_id': tracker, 'expected_revision': 2, 'canvas': {'family': 'metric'}}, **where)
    # The newest revisions are kept.
    import core_trackers.presentation as module
    for revision in range(2, 2 + module.KEEP_REVISIONS):
        kit.save_presentation(shop.company, shop.tracker, {'canvas': {'family': 'metric' if revision % 2 else 'occurrences'}}, expected_revision=revision)
    kept = sorted(row['revision'] for row in rows(shop, 'tracker_presentation_revision'))
    assert kept == list(range(3, 3 + module.KEEP_REVISIONS)), 'fifty-two saves keep the newest fifty'


def test_the_page_shows_one_number_on_tile_and_canvas_and_core_rows_stay_as_they_were(shop):
    from control_room_routes import owner_directory
    from import_control_room_bundle import publish_core_trackers
    path = owner_directory(shop.state, 'peter') / 'data.json'
    before = shop.page()
    core = lambda page: json.dumps([row for row in page['core_trackers'] if row['core']]) + json.dumps({k: v for k, v in page['core_tracker_canvases'].items() if k != 'freezer'})
    assert [row['key'] for row in before['core_trackers']][-1] == 'freezer' and sum(row['core'] for row in before['core_trackers']) == 9
    row = before['core_trackers'][-1]
    # Before any save the tracker shows its default presentation at revision 0.
    assert (row['label'], row['kind'], row['mark'], row['unit'], row['view'], row['presentation_revision'], row['agent_id'], row['core']) == ('Freezer door', 'count', 'line', 'times', 'count', 0, 'agent-freezer', False)
    saved = shop.call(WRITE, {'tracker_id': shop.tracker['id'], 'expected_revision': 0, 'tile': {**FREEZER['tile'], 'mark': 'bars', 'unit': 'doors'}, 'canvas': FREEZER['canvas']})
    after = shop.page()
    row, canvas = after['core_trackers'][-1], after['core_tracker_canvases']['freezer']
    assert saved['page_updated'] is True and (row['label'], row['mark'], row['unit'], row['presentation_revision']) == ('Freezer door left open', 'bars', 'doors', 1)
    # One output revision, one number: the tile row and the canvas read the same value.
    assert (row['value'], row['revision']) == (canvas['value'], canvas['revision']) == (14, 1)
    assert canvas['presentation'] == {'family': 'metric', 'blocks': FREEZER['canvas']['blocks'], 'revision': 1} and canvas['problems'] == []
    assert core(after) == core(before), 'a saved presentation changes its own tracker’s row and canvas'
    assert {k: v for k, v in after.items() if k not in {'core_trackers', 'core_tracker_canvases'}} == {k: v for k, v in before.items() if k not in {'core_trackers', 'core_tracker_canvases'}}
    # A pass publishes the same page from the saved presentation, and a second publish writes the same bytes.
    written = path.read_bytes()
    publish_core_trackers(shop.company, shop.location, location_name='Corner shop')
    assert path.read_bytes() == written
    # A new output revision reaches both at once.
    shop.publish(value=15)
    publish_core_trackers(shop.company, shop.location, location_name='Corner shop')
    later = shop.page()
    assert (later['core_trackers'][-1]['value'], later['core_trackers'][-1]['revision']) == (later['core_tracker_canvases']['freezer']['value'], later['core_tracker_canvases']['freezer']['revision']) == (15, 2)
    assert later['core_trackers'][-1]['mark'] == 'bars' and core(later) == core(before)


def test_an_amount_is_written_in_the_locations_currency_on_tile_and_canvas(shop):
    from control_room_routes import owner_directory
    from import_control_room_bundle import publish_core_trackers
    path = owner_directory(shop.state, 'peter') / 'data.json'
    page = shop.page(); page['currency'] = 'EUR'; path.write_text(json.dumps(page))
    saved = shop.call(WRITE, {'tracker_id': shop.tracker['id'], 'expected_revision': 0, 'tile': {'label': 'Returns at the desk', 'mark': 'line', 'format': 'money'}})
    row = shop.page()['core_trackers'][-1]
    assert saved['page_updated'] is True and (row['kind'], row['currency'], row['value']) == ('money', 'EUR', 14)
    # Home and the canvas read that one row, so both write the same characters; a pass keeps the currency.
    publish_core_trackers(shop.company, shop.location, location_name='Corner shop')
    assert shop.page()['core_trackers'][-1]['currency'] == 'EUR' and all('currency' not in tile for tile in shop.page()['core_trackers'] if tile['core'])


def test_a_tracking_agents_tracker_joins_the_page_with_its_first_output(shop):
    from core_trackers import add_tracker
    from import_control_room_bundle import publish_core_trackers, tracker_page
    add_tracker(shop.company, shop.location, 'agent_new', 'Queue watch', 'When the queue passes five.', agent_id='agent-new')
    add_tracker(shop.company, shop.location, 'returns_desk', 'Returns desk', 'Each return at the desk.')
    tiles, canvases = tracker_page(shop.company, shop.location, {})
    keys = [tile['key'] for tile in tiles if not tile['core']]
    assert keys == ['freezer', 'returns_desk'], 'the agent’s own tile stands on Home until its tracker has an output'
    waiting = next(tile for tile in tiles if tile['key'] == 'returns_desk')
    assert (waiting['state'], waiting['needs'], waiting['kind'], waiting['mark'], waiting['view'], waiting['core']) == ('needs', 'Needs first run', 'count', 'line', 'count', False)
    assert 'returns_desk' not in canvases and 'agent_id' not in waiting
    publish_core_trackers(shop.company, shop.location, location_name='Corner shop')
    assert [row['key'] for row in shop.page()['core_trackers'] if not row['core']] == keys


# ── What a review broke, held ──────────────────────────────────────────────────────────────────

def test_a_list_a_script_wrote_reaches_the_page_as_the_kits_own_fields():
    # A script publishes a list of its own with addresses under names the publisher's link rule leaves open.
    own = {'id': 'x1', 'at': '2026-10-11T10:00:00Z', 'description': 'A row the script wrote.', 'product': 'Gum', 'evidence_url': 'https://evil.example/phish',
           'thumbnail_url': 'https://evil.example/t.jpg', 'thumbnail': '//evil.example/p.jpg', 'media_url': 'https://evil.example/v.mp4', 'thumb': 'https://evil.example/x.jpg',
           'nested': {'href': 'https://evil.example/deep'}}
    ranked = {'product': 'Gum', 'value': 3, 'picked': 2, 'returned': 1, 'bought': 0, 'thumbnail_url': 'https://evil.example/g.jpg', 'thumb': '//evil.example/g.jpg'}
    published = output(my_rows=[own], top=[ranked])
    for rail in ('instances', 'timeline'):
        family = 'live' if rail == 'timeline' else 'ranking'
        blocks = [{'block': rail, 'bind': {'rows': 'my_rows'}}] + ([{'block': 'ranking', 'bind': {'items': 'top'}}] if family == 'ranking' else [])
        spec = kit.validate_presentation({'schema': kit.SCHEMA_ID, 'tile': FREEZER['tile'], 'canvas': {'family': family, 'blocks': blocks}}, output=published)
        page = kit.canvas_for_page(published['canvas'], spec)
        assert page['my_rows'] == [{'id': 'x1', 'at': '2026-10-11T10:00:00Z', 'description': 'A row the script wrote.', 'product': 'Gum'}], rail
        assert 'evil.example' not in json.dumps({k: v for k, v in page.items() if k != 'top' or family == 'ranking'})
        if family == 'ranking':
            assert page['top'] == [{'product': 'Gum', 'value': 3, 'picked': 2, 'returned': 1, 'bought': 0}]
        # The rows the host rebuilt from its own records stay as the output holds them.
        assert page['rows'] == published['canvas']['rows'] and published['canvas']['my_rows'][0]['evidence_url'], 'the stored output keeps what its script wrote'
    # Each period's canvas on a tile row is read the same way.
    spec = {'schema': kit.SCHEMA_ID, 'tile': FREEZER['tile'], 'canvas': {'family': 'occurrences', 'blocks': [{'block': 'instances', 'bind': {'rows': 'my_rows'}}]}}
    row = kit.apply_to_tile({**published['tile'], 'core': False, 'periods': {'week': {'value': 9, 'series': POINTS, 'canvas': published['canvas']}}}, spec, published['canvas'])
    assert 'evil.example' not in json.dumps(row['periods']['week']['canvas']['my_rows'])


def test_a_wrong_typed_family_answers_one_sentence():
    saved = kit.presentation_of({'label': 'Freezer door', 'canvas': {'family': 'occurrences'}})
    for family in ([], {}, ['metric'], {'metric': 1}, 3, None, True):
        with pytest.raises(ValueError) as caught:
            kit.validate_presentation(kit.merge_presentation(saved, {'canvas': {'family': family}}))
        assert str(caught.value) in ('A canvas has a family and its blocks.',) and not NEGATIVE.search(str(caught.value)), family
        with pytest.raises(ValueError):
            kit.validate_presentation(kit.merge_presentation(saved, {'canvas': {'family': family, 'blocks': FREEZER['canvas']['blocks']}}))
    for change in ({'tile': []}, {'tile': {'mark': []}}, {'tile': {**FREEZER['tile'], 'bind': []}}, {'tile': {**FREEZER['tile'], 'bind': {'fraction': {}}}}, {'canvas': []},
                   {'canvas': {'family': 'metric', 'blocks': {}}}, {'canvas': {'family': 'metric', 'blocks': [[]]}}, {'canvas': {'family': 'metric', 'blocks': [{'block': []}]}},
                   {'canvas': {'family': 'metric', 'blocks': [{'block': 'figure'}, {'block': 'line', 'bind': {'series': []}}]}},
                   {'canvas': {'family': 'live', 'blocks': [{'block': 'gauge', 'chart': [], 'bind': {'fraction': 'ratio'}}, {'block': 'timeline', 'bind': {'rows': 'rows'}}]}}):
        with pytest.raises(ValueError) as caught:
            kit.validate_presentation(kit.merge_presentation(saved, change))
        assert not NEGATIVE.search(str(caught.value)), change


def test_the_default_label_ends_on_a_whole_word_and_every_family_leads_with_the_number():
    long = 'Every visitor who waited over two minutes at the pegboard'
    label = kit.default_presentation({'label': long})['tile']['label']
    assert len(label) <= kit.LIMITS['label'] and long.startswith(label + ' ') and label == 'Every visitor who waited'
    assert kit.default_presentation({'label': 'Supercalifragilisticexpialidocious queue'})['tile']['label'] == 'Supercalifragilisticexpi', 'one word longer than a tile is cut at the limit'
    assert kit.default_presentation({'label': '  Freezer   door '})['tile']['label'] == 'Freezer door' and kit.default_presentation({})['tile']['label'] == 'Tracker'
    # One tracker shows one number on its tile and on its canvas: every family's own blocks start with the figure.
    for name, family in kit.FAMILIES.items():
        assert family['default'][0] == {'block': 'figure'} and 'figure' in family['allows'], name
    # The limits are what a Home tile draws on one line beside its mark.
    assert (kit.LIMITS['label'], kit.LIMITS['unit']) == (24, 8)
    assert 'label of 1 to 24 characters' in refused(changed(FREEZER, ['tile', 'label'], 'Freezer door left open today'))
    assert 'a word of up to 8 characters' in refused(changed(FREEZER, ['tile', 'unit'], 'doors left open'))
    # A count whose presentation names no unit takes the one its script published when a tile holds it.
    bare = changed(FREEZER, ['tile', 'unit'])
    assert kit.apply_to_tile({**output(unit='doors')['tile'], 'core': False}, bare)['unit'] == 'doors'
    assert kit.apply_to_tile({**output(unit='doors left open today')['tile'], 'core': False}, bare)['unit'] is None


def test_a_tile_binding_the_output_leaves_unfilled_rests_on_the_row_and_leaves_a_canvas_pick_open(shop):
    from import_control_room_bundle import publish_core_trackers
    ring = {'label': 'Doors closed in time', 'mark': 'ring', 'format': 'count', 'unit': 'doors', 'bind': {'fraction': 'ratio'}}
    shop.publish(value=20, ratio=.5)
    shop.call(WRITE, {'tracker_id': shop.tracker['id'], 'expected_revision': 0, 'tile': ring, 'canvas': FREEZER['canvas']})
    row = shop.page()['core_trackers'][-1]
    assert (row['mark'], row['fraction'], row['problems']) == ('ring', .5, [])
    # The script changes and stops publishing the share.
    shop.publish(value=20)
    publish_core_trackers(shop.company, shop.location, location_name='Corner shop')
    row, canvas = shop.page()['core_trackers'][-1], shop.page()['core_tracker_canvases']['freezer']
    assert 'fraction' not in row and canvas['problems'] == []
    problem, = row['problems']
    assert (problem['block'], problem['field'], problem['needs']) == ('tile', 'fraction', 'Needs a share of a whole.') and problem['sentence'].startswith('The tile binds fraction to «ratio»')
    read = shop.call(READ, {'tracker_id': shop.tracker['id']})
    assert [(p['block'], p['field'], p['bound_to']) for p in read['problems']] == [('tile', 'fraction', 'ratio')]
    # A person's pick names the canvas alone, and it is checked against the canvas alone.
    picked = kit.save_presentation(shop.company, shop.tracker, {'canvas': {'family': 'live'}}, expected_revision=1, saved_by='person')
    assert picked['revision'] == 2 and picked['canvas']['family'] == 'live' and picked['tile'] == ring
    # A change of the tile is checked against the output, and so is a canvas the output leaves unfilled.
    for change, start in (({'tile': ring}, 'The tile binds fraction to «ratio», which is a name the output leaves out. Needs a share of a whole.'),
                          ({'canvas': {'family': 'state'}}, 'The state_counts block binds shelves to «shelves»')):
        with pytest.raises(kit.Unfilled) as caught:
            kit.save_presentation(shop.company, shop.tracker, change, expected_revision=2)
        assert str(caught.value).startswith(start) and caught.value.problem['needs'].startswith('Needs ')
    # A tile saved ahead of the first output is named the same way once the output arrives.
    assert kit.apply_to_tile({**output()['tile'], 'core': False}, {'schema': kit.SCHEMA_ID, 'tile': ring, 'canvas': FREEZER['canvas']}, output()['canvas'])['problems'][0]['field'] == 'fraction'
    # Each period's reading carries its own.
    reading = kit.apply_to_tile({**output()['tile'], 'core': False, 'periods': {'week': {'value': 3, 'series': POINTS, 'canvas': output(ratio=.2)['canvas']},
        'month': {'value': 9, 'series': POINTS, 'canvas': output()['canvas']}}}, {'schema': kit.SCHEMA_ID, 'tile': ring, 'canvas': FREEZER['canvas']}, output()['canvas'])
    assert reading['periods']['week']['problems'] == [] and reading['periods']['week']['fraction'] == .2 and reading['periods']['month']['problems'][0]['block'] == 'tile'


def test_a_core_tracker_reads_as_maintained_with_its_recipe(shop):
    for key in ('sales', 'conversion', 'picked_up', 'product_interactions'):
        core = next(t for t in rows(shop, 'tracker') if t['key'] == key)
        read = shop.call(READ, {'tracker_id': core['id']})
        assert (read['core'], read['locked'], read['presentation'], read['output']) == (True, True, None, None)
        assert read['maintained'].startswith('A Core Tracker is shown as its recipe draws it') and not NEGATIVE.search(read['maintained'])
        assert read['recipe'] == {'revision': core['recipe_revision'], 'tile': core['tile'], 'canvas': core['canvas']}
        assert 'tile' not in json.dumps({k: v for k, v in read.items() if k not in {'recipe', 'kit', 'maintained'}}), 'the read names no mark and no format for a Core Tracker'


def test_a_tracker_made_again_under_a_removed_trackers_key_starts_with_its_own_presentation(shop):
    from core_trackers import add_tracker, remove_tracker, update_tracker
    from import_control_room_bundle import publish_core_trackers, tracker_page
    first = add_tracker(shop.company, shop.location, 'returns_desk', 'Returns desk', 'Each return at the desk.')
    money = {'label': 'Returns at the desk', 'mark': 'bars', 'format': 'money'}
    kit.save_presentation(shop.company, first, {'tile': money})
    kept = lambda kind: [row['id'] for row in rows(shop, kind) if row['tracker_id'] == first['id']]
    assert len(kept('tracker_presentation')) == len(kept('tracker_presentation_revision')) == 1
    # Removal clears saved revisions before a page pass or reuse of the same key.
    remove_tracker(shop.company, shop.location, 'returns_desk')
    assert kept('tracker_presentation') == kept('tracker_presentation_revision') == [] and len(rows(shop, 'tracker_presentation_revision')) == 0
    again = add_tracker(shop.company, shop.location, 'returns_desk', 'Queue at the till', 'How many wait at the till.')
    assert again['id'] == first['id']
    read = shop.call(READ, {'tracker_id': again['id']})
    assert (read['presentation']['revision'], read['presentation']['tile']['label'], read['presentation']['tile']['format'], read['revisions']) == (0, 'Queue at the till', 'count', [])
    # A removal and a new tracker between two passes: a presentation saved for a later revision of the earlier tracker belongs to that one.
    grown = update_tracker(shop.company, shop.location, 'returns_desk', {'label': 'Queue at the till today'}, expected_revision=1)
    kit.save_presentation(shop.company, grown, {'tile': money})
    kit.save_presentation(shop.company, grown, {'canvas': {'family': 'live'}}, expected_revision=1)
    remove_tracker(shop.company, shop.location, 'returns_desk')
    third = add_tracker(shop.company, shop.location, 'returns_desk', 'Baskets left behind', 'Each basket left in an aisle.')
    read = shop.call(READ, {'tracker_id': third['id']})
    assert (read['presentation']['revision'], read['presentation']['tile']['label'], read['revisions']) == (0, 'Baskets left behind', [])
    tiles, _ = tracker_page(shop.company, shop.location, {})
    tile = next(tile for tile in tiles if tile['key'] == 'returns_desk')
    assert (tile['label'], tile['kind'], tile['mark'], tile['presentation_revision']) == ('Baskets left behind', 'count', 'line', 0)
    saved = kit.save_presentation(shop.company, third, {'canvas': {'family': 'occurrences'}}, expected_revision=0)
    assert saved['revision'] == 1 and [row['revision'] for row in shop.call(READ, {'tracker_id': third['id']})['revisions']] == [1], 'the earlier tracker’s revisions go with it'
    # The helper a tracker's removal calls: both kinds of row, inside the removal's own transaction.
    with shop.company.locations.connect(True) as db:
        kit.forget(db, 'peter', third)
    assert kept('tracker_presentation') == kept('tracker_presentation_revision') == []


def freezer_agent(shop):
    """The host's stores and the Tracking Agent record of the shop's own tracker, as a route holds them."""
    selected = {'state': shop.state, 'monitoring': shop.agent.monitoring_store, 'business_scope': shop.company.scope, 'location_intelligence': shop.company.locations}
    return selected, {'id': 'agent-freezer', 'agent_kind': 'tracker', 'owner_principal_id': 'peter', 'dataset': 'retail-demo', 'title': 'Freezer door', 'profile_ids': []}


def test_the_canvas_reads_the_revision_the_period_and_the_format_home_shows(shop):
    from control_room_routes import owner_directory
    from monitoring_routes import shown_presentation
    import retail_records as records
    path = owner_directory(shop.state, 'peter') / 'data.json'
    page = shop.page(); page['currency'] = 'EUR'; path.write_text(json.dumps(page))
    shop.call(WRITE, {'tracker_id': shop.tracker['id'], 'expected_revision': 0, 'tile': {'label': 'Returns at the desk', 'mark': 'line', 'format': 'money'}})
    selected, agent = freezer_agent(shop)
    def readings(revision, week):
        reading = {'tile': {**output(value=week)['tile'], 'revision': revision}, 'canvas': {**output(value=week)['canvas'], 'revision': revision}}
        records.put_many(shop.company, 'tracker_script_readings', shop.location, [records.stamp(shop.company, shop.location, {
            'id': 'readings-' + shop.tracker['id'].removeprefix('tracker-'), 'tracker_id': shop.tracker['id'], 'tracker_key': 'freezer', 'output_revision': revision,
            'readings': {name: reading for name in ('week', 'month', 'year')}})])
    readings(1, 80)
    kit.save_presentation(shop.company, shop.tracker, {'canvas': {'family': 'metric'}}, expected_revision=1)
    from import_control_room_bundle import show_presentation
    assert show_presentation(shop.company, shop.location, shop.tracker['id']) is True
    home = shop.page()['core_trackers'][-1]
    assert (home['revision'], home['value'], home['currency'], home['periods']['week']['value'], sorted(home['periods'])) == (1, 14, 'EUR', 80, ['month', 'week', 'year'])
    seen = shown_presentation(selected, agent, 'peter')
    assert seen['output']['tile'] == home and seen['output']['canvas'] == shop.page()['core_tracker_canvases']['freezer'] and seen['output']['revision'] == 1
    # A pass publishes revision 2 and stops ahead of the page: Home still shows revision 1, and so does the canvas.
    shop.publish(value=15); readings(2, 81)
    assert kit.current_output(shop.state, 'peter', shop.tracker)['revision'] == 2 and shop.page()['core_trackers'][-1] == home
    behind = shown_presentation(selected, agent, 'peter')
    assert behind['output']['revision'] == 1 and behind['output']['tile'] == home, 'the canvas reads the row Home is showing'
    assert {family['id'] for family in behind['families'] if family['available']} == {'metric', 'live', 'occurrences'}
    # A tracker the owner's page has yet to hold is assembled as the page would write it: readings, currency and all.
    page = shop.page(); page['core_trackers'] = [row for row in page['core_trackers'] if row.get('tracker_id') != shop.tracker['id']]; page['core_tracker_canvases'].pop('freezer')
    path.write_text(json.dumps(page))
    fresh = shown_presentation(selected, agent, 'peter')['output']
    assert (fresh['revision'], fresh['tile']['value'], fresh['tile']['currency'], fresh['tile']['kind'], fresh['tile']['periods']['week']['value'], sorted(fresh['tile']['periods'])) == (2, 15, 'EUR', 'money', 81, ['month', 'week', 'year'])
    assert fresh['canvas']['revision'] == 2 and fresh['canvas']['presentation']['family'] == 'metric'
    # A reader whose access to the location has ended reads the presentation alone.
    assert 'output' not in shown_presentation(selected, agent, 'someone-else')


def test_a_refused_first_save_leaves_a_tracking_agent_without_a_record(shop):
    import tracker_presentation as tools
    from core_trackers import tracker_records
    selected, _ = freezer_agent(shop)
    agent = {'id': 'agent-queue', 'agent_kind': 'tracker', 'owner_principal_id': 'peter', 'dataset': 'retail-demo', 'title': 'Queue watch', 'profile_ids': []}
    before = tracker_records(shop.state, 'peter', 'retail-demo')
    for change, revision, sentence in (({'canvas': {'family': 'heat'}}, 0, 'A canvas has a family and its blocks.'), ({'canvas': {'family': []}}, 0, 'A canvas has a family and its blocks.'),
                                       ({'tile': {'label': 'Queue', 'mark': 'donut', 'format': 'count'}}, 0, 'Choose the tile mark from the kit'), ({}, 0, 'Name the tile or the canvas to change.'),
                                       ({'canvas': {'family': 'live'}}, 3, 'Read the presentation again')):
        with pytest.raises(ValueError) as caught:
            tools.save_for_agent(selected, agent, change, expected_revision=revision)
        assert sentence in str(caught.value)
        assert tracker_records(shop.state, 'peter', 'retail-demo') == before, 'a refused save writes nothing'
    saved = tools.save_for_agent(selected, agent, {'canvas': {'family': 'live'}}, expected_revision=0)
    linked, = [record for record in tracker_records(shop.state, 'peter', 'retail-demo') if record.get('agent_id') == 'agent-queue']
    assert saved['presentation']['revision'] == 1 and linked['location_id'] == shop.location and saved['tracker_id'] == linked['id']


def test_display_ownership_uses_a_real_host_agent_and_preserves_legacy_principal(shop):
    from import_control_room_bundle import tracker_page
    tiles, canvases = tracker_page(shop.company, shop.location, {})
    assert tiles and all(row['owner_principal_id'] == 'peter' for row in tiles)
    assert all('owner_agent_id' not in row for row in tiles if row['core'])
    assert next(row for row in tiles if row['key'] == 'freezer')['owner_agent_id'] == 'agent-freezer'
    organization = shop.agent.organization_chats.ensure(shop.agent, 'retail-demo', 'organization')
    calls = []
    def resolve(owner, collection, *, create=False):
        calls.append((owner, collection, create))
        return organization['id']
    shop.company.owner_agent_resolver = resolve
    tiles, canvases = tracker_page(shop.company, shop.location, {})
    assert all(row['owner_agent_id'] == (organization['id'] if row['core'] else 'agent-freezer') for row in tiles)
    assert all(row['owner_agent_id'] == (organization['id'] if row['core'] else 'agent-freezer') for row in canvases.values())
    assert calls and all(not create for _, _, create in calls)
    assert shop.agent.organization_chats.canonical(shop.agent.children[organization['id']])
