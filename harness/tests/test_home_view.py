"""Home: nine Core Tracker tiles on and under the live hero, fed by one view model from either reader."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import re
import subprocess
import threading
import pytest

HERE = Path(__file__).parents[1]
WEB = Path(os.environ.get('ROCOTO_HOME_TEST_WEB', HERE / 'verticals/retail/web/control-room'))
CORE = ['Visitors', 'Product interactions', 'Picked up', 'Put back', 'Basket', 'Queue time', 'Empty shelves', 'Sales', 'Conversion']
PAGE = b'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link href="kit/rocoto-kit.css" rel="stylesheet"><link href="kit/twelvelabs-material.css" rel="stylesheet"><link href="location-view.css" rel="stylesheet">
<style>html,body{margin:0;background:var(--camera-paper)}*{box-sizing:border-box}[hidden]{display:none!important}</style>
<body><div id="locationRoot"></div><script src="home-model.js"></script><script src="home-marks.js"></script><script src="location-view.js"></script>'''
# The bundle reader's answer in small: seven visitors over three recordings, five interactions, one pick-up.
BUNDLE = {'locations': [{'id': 'shop-aisle', 'name': 'Shop aisle',
    'metrics': {'observed_interactions': 5, 'observed_pickups': 1, 'observed_putbacks': 0, 'observed_tracks': 7, 'observed_visitors': None,
                'conversion_rate': None, 'revenue': None, 'average_transaction_value': None, 'average_queue_time_s': None},
    'metric_sources': {'observed_tracks': {'series': [{'label': 'One', 'clipId': 'a', 'value': 2}, {'label': 'Two', 'clipId': 'b', 'value': 1}, {'label': 'Three', 'clipId': 'c', 'value': 4}]},
                       'observed_interactions': {'series': [{'label': 'One', 'value': 3}, {'label': 'Two', 'value': 0}, {'label': 'Three', 'value': 2}]}},
    'sessions': [{'id': f's{n}', 'personId': f'P{n}', 'clipId': 'a', 'first_s': n, 'last_s': n + 2} for n in range(7)],
    'events': [{'id': 'e1', 'type': 'pick_up', 'label': 'Pick up item', 'description': 'Takes the green product', 'is_interaction': True, 'time_s': 1.3, 'end_s': 2, 'clipId': 'a', 'personId': 'P1', 'image': '/crop.jpg'}]
            + [{'id': f'e{n}', 'type': 'examine_object', 'is_interaction': True, 'time_s': n, 'end_s': n + 1, 'clipId': 'a', 'personId': 'P1'} for n in range(2, 6)],
    'products': [{'id': 'p1', 'label': 'A small, green item held by Person P1, possibly a tool.', 'image': '/crop.jpg', 'counts': {'interactions': 2, 'pickups': 1, 'putbacks': 0}, 'events': []}]}]}
# The maintained reader's answer: its location, its visitors and its events.
MAINTAINED = {'location': {'id': 'sofia', 'name': 'Vitosha Boulevard 85', 'address': '85 Vitosha Boulevard, Sofia'}, 'visitors': {'count': 142}, 'events': []}
BANNED = re.compile(r'recording|saved observation|demo footage|recorded replay|available|starts with|awaiting|waiting for|\bof \d+ interactions|—|\bPOS\b', re.I)


def theme_css():
    tokens = json.loads((HERE / 'theme.json').read_text()); values = {**tokens['palette'], **tokens['typography']}
    values.update(ink='#201e1b', paper='#ffffff', accent='#ed5a2b', border='#ddd', muted='#777', surface='#f2f1ee', radius='0.75rem')
    values['radius-sm'] = '0.5rem'; values['radius-lg'] = '1.25rem'
    return (':root{' + ''.join(f'--{k}:{v};' for k, v in values.items()) + '}').encode()


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0].lstrip('/')
            target = (WEB / path).resolve() if path else None
            if not path: body, kind = PAGE, 'text/html'
            elif path == 'theme.css': body, kind = theme_css(), 'text/css'
            elif target.is_file() and target.is_relative_to(WEB.resolve()):
                body = target.read_bytes(); kind = {'.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2'}.get(target.suffix, 'application/octet-stream')
            else: body, kind = b'{}', 'application/json'
            self.send_response(200 if path != 'api/agents' else 404); self.send_header('Content-Type', kind)
            if not path: self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}/'
    server.shutdown(); server.server_close()


def launch(p):
    opts = {'headless': True, 'args': ['--no-sandbox']}
    if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'): opts['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
    return p.chromium.launch(**opts)


def opened(browser, origin, data, size=(1600, 1000), **context):
    page = browser.new_page(viewport={'width': size[0], 'height': size[1]}, **context); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(origin)
    page.evaluate("""data=>{window.calls=[];window.view=LocationView.create({root:document.querySelector('#locationRoot'),data,
      onOpenSession:target=>calls.push(['session',target]),onOpenCameras:id=>calls.push(['cameras',id])});view.show();}""", data)
    page.wait_for_function("()=>document.fonts.status==='loaded'")
    return page, errors


TILES = """()=>[...document.querySelectorAll('#locationRoot .home-tile')].map(t=>({key:t.dataset.tile||null,tag:t.tagName,surface:t.dataset.surface||null,state:t.dataset.state||null,
  title:t.querySelector('.home-tile-title').textContent,value:t.querySelector('.home-tile-value')?.textContent??null,unit:t.querySelector('.home-tile-unit')?.textContent??null,
  needs:t.querySelector('.home-tile-needs')?.textContent??null,mark:!!t.querySelector('.home-tile-mark svg'),spoken:t.getAttribute('aria-label')}))"""


def test_one_model_reads_the_bundle_and_the_maintained_wire():
    script = """const M=require(process.argv[1]),[bundle,maintained]=JSON.parse(process.argv[2]);
      const a=M.from(bundle,{period:'week'}),b=M.from(maintained,{period:'month'});
      const row=t=>[t.key,t.label,t.value,t.text+t.unit,t.state,t.needs,t.period];
      const agents=M.from({...maintained,agents:[{id:'a1',agent_kind:'tracker',title:'Freezer door left open',status:'active',runs:[{id:'r1',created_at:1791590400}]},{id:'a2',agent_kind:'action',title:'Message the manager'}]},{now:1791676800000});
      console.log(JSON.stringify({bundle:a.tiles.map(row),maintained:b.tiles.map(row),keys:Object.keys(a.tiles[1]).sort(),event:a.tile('picked_up').events[0],series:a.tile('visitors').series,
        share:a.tile('picked_up').fraction,products:a.products.handled.map(p=>[p.label,p.count]),other:a.tileFor('visitors','year').period,agents:agents.agents.map(row),agentEvents:agents.agents[0].events.length}));"""
    answer = json.loads(subprocess.run(['node', '-e', script, str(WEB / 'home-model.js'), json.dumps([BUNDLE, MAINTAINED])], check=True, capture_output=True, text=True).stdout)
    assert [row[1] for row in answer['bundle']] == CORE and [row[1] for row in answer['maintained']] == CORE
    assert [row[3] for row in answer['bundle']] == ['7', '5', '1', '0', '', '0s', '0', '', '']
    assert [row[3] for row in answer['maintained']] == ['142', '0', '0', '0', '', '0s', '0', '', '']
    for rows in (answer['bundle'], answer['maintained']):
        needs = {row[1]: row[5] for row in rows if row[4] == 'needs'}
        assert needs == {'Basket': 'Needs till data', 'Sales': 'Needs till data', 'Conversion': 'Needs till data'}
        assert all(len(text.split()) <= 3 for text in needs.values())
    assert {row[6] for row in answer['bundle']} == {'week'} and {row[6] for row in answer['maintained']} == {'month'} and answer['other'] == 'year'
    # The tile the history view reads: the agreed fields, a series of {t, value} and events in one shape.
    assert {'key', 'label', 'value', 'unit', 'state', 'needs', 'series', 'events', 'period'} <= set(answer['keys'])
    assert [point['value'] for point in answer['series']] == [2, 1, 4] and all('t' in point for point in answer['series'])
    assert {'id', 'at', 'label', 'description', 'thumb', 'visitor_id', 'source_id'} <= set(answer['event'])
    assert answer['event']['label'] == 'Pick up item' and answer['event']['visitor_id'] == 'P1' and answer['event']['source_id'] == 'a'
    assert answer['share'] == pytest.approx(.2) and answer['products'] == [['A small, green item', 2]]
    # A Tracking Agent joins the same family; an action agent stays out.
    assert [row[:5] for row in answer['agents']] == [['agent:a1', 'Freezer door left open', 1, '1', 'live']] and answer['agentEvents'] == 1


# The page data the bundle import builds: the nine Core Trackers as tiles, the company's own trackers after them, and products.
PAGE_DATA = {'core_trackers': [
    {'key': 'visitors', 'label': 'Visitors', 'value': 142, 'unit': '', 'state': 'live', 'core': True, 'series': [{'t': '2026-10-08T00:00:00Z', 'value': 60}, {'t': '2026-10-09T00:00:00Z', 'value': 82}],
     'events': [{'id': 'v1', 'label': 'Visitor 4', 'visitor_id': 'P4', 'source_id': 'cam-1'}], 'periods': {'month': {'value': 611}}},
    {'key': 'product_interactions', 'label': 'Product interactions', 'value': 0, 'core': True},
    {'key': 'picked_up', 'label': 'Picked up', 'value': 0, 'core': True},
    {'key': 'put_back', 'label': 'Put back', 'value': 0, 'core': True},
    {'key': 'basket', 'label': 'Basket', 'value': 12.4, 'currency': 'EUR', 'estimated': True, 'core': True},
    {'key': 'queue_time', 'label': 'Queue time', 'value': 0, 'core': True},
    {'key': 'empty_shelves', 'label': 'Empty shelves', 'value': 0, 'core': True},
    {'key': 'sales', 'label': 'Sales', 'value': None, 'state': 'needs', 'needs': 'Needs till data', 'core': True},
    {'key': 'conversion', 'label': 'Conversion', 'value': None, 'core': True},
    {'key': 'freezer-door', 'label': 'Freezer door left open', 'value': 3, 'core': False},
    {'key': 'entrance-groups', 'label': 'Groups at the entrance', 'value': 0, 'core': False}],
  'products': [{'name': 'Oat drink 1 l', 'image_url': '/oat.jpg', 'price': {'amount': 2.49, 'currency': 'EUR'}, 'basis': 'estimated', 'counts': {'interactions': 4, 'pickups': 3, 'putbacks': 1}}]}


def test_the_model_takes_its_tiles_from_core_trackers_when_the_page_data_carries_them():
    script = """const M=require(process.argv[1]),[bundle,page]=JSON.parse(process.argv[2]);
      const given=M.from({...bundle,...page},{period:'week'}),derived=M.from(bundle,{period:'week'}),row=t=>[t.key,t.label,t.text+t.unit,t.state,t.needs,t.core,t.custom,t.stage,t.estimated,t.mark];
      console.log(JSON.stringify({given:given.tiles.map(row),derived:derived.tiles.map(row),month:given.tileFor('visitors','month').text,event:given.tile('visitors').events[0],
        products:given.products.handled.map(p=>[p.label,p.image,p.count,p.price,p.estimated]),core:given.core.length,own:given.agents.map(t=>t.label)}));"""
    answer = json.loads(subprocess.run(['node', '-e', script, str(WEB / 'home-model.js'), json.dumps([BUNDLE, PAGE_DATA])], check=True, capture_output=True, text=True).stdout)
    given = answer['given']
    # Core first, in the order given, then the company's own trackers as the same kind of tile.
    assert [row[1] for row in given] == CORE + ['Freezer door left open', 'Groups at the entrance'] and answer['core'] == 9 and answer['own'] == ['Freezer door left open', 'Groups at the entrance']
    assert [row[2] for row in given] == ['142', '0', '0', '0', '€12.40', '0s', '0', '', '', '3', '0']
    assert [row[3] for row in given] == ['live', 'zero', 'zero', 'zero', 'live', 'zero', 'zero', 'needs', 'needs', 'live', 'zero']
    assert [row[4] for row in given[7:9]] == ['Needs till data', 'Needs till data']
    assert [row[5] for row in given] == [True] * 9 + [False] * 2 and [row[6] for row in given] == [False] * 9 + [True] * 2
    assert [row[7] for row in given] == [True] * 4 + [False] * 7, 'the first four core tiles stand on the footage'
    assert [row[8] for row in given] == [False] * 4 + [True] + [False] * 6, 'only the estimated money figure is marked'
    assert [row[9] for row in given[:9]] == ['line', 'line', 'ring', 'ring', 'line', 'line', 'ring', 'line', 'arc']
    assert answer['month'] == '611' and answer['event']['visitor_id'] == 'P4' and answer['event']['source_id'] == 'cam-1'
    assert answer['products'] == [['Oat drink 1 l', '/oat.jpg', 4, '€2.49', True]]
    # Without the key the nine are derived, as before.
    assert [row[1] for row in answer['derived']] == CORE and [row[2] for row in answer['derived']][:4] == ['7', '5', '1', '0']


# A company tracker's rows as the page data carries them once its presentation is applied (core_trackers/presentation.py).
POINTS = [{'t': f'2026-10-{day:02d}T00:00:00Z', 'value': value} for day, value in zip(range(4, 11), [4, 6, 5, 7, 8, 6, 6])]
PRESENTED = [
    {'key': 'agent_freezer', 'label': 'Freezer door left open', 'value': 6, 'unit': 'times', 'state': 'ready', 'core': False, 'chat': True, 'locked': False, 'series': POINTS, 'revision': 4,
     'kind': 'count', 'mark': 'bars', 'view': 'count', 'presentation_revision': 2, 'agent_id': 'a1', 'periods': {'month': {'value': 180, 'unit': 'times', 'series': POINTS}}},
    {'key': 'queue-watch', 'label': 'Queue watch', 'value': 3, 'unit': 'alerts', 'state': 'ready', 'core': False, 'series': POINTS, 'kind': 'count', 'mark': 'line', 'view': 'count', 'presentation_revision': 1},
    {'key': 'shelf_checks', 'label': 'Shelf checks on time', 'value': 86.5, 'unit': '%', 'state': 'ready', 'core': False, 'series': POINTS, 'kind': 'rate', 'mark': 'ring', 'view': 'rate', 'presentation_revision': 1, 'agent_id': 'a2'},
    {'key': 'returns-desk', 'label': 'Returns at the desk', 'value': 84.5, 'unit': 'EUR', 'state': 'ready', 'core': False, 'series': POINTS, 'kind': 'money', 'mark': 'line', 'view': 'count', 'currency': 'EUR', 'presentation_revision': 1},
    {'key': 'chiller', 'label': 'Chiller shelves', 'value': 1, 'unit': 'empty', 'state': 'ready', 'core': False, 'series': POINTS, 'kind': 'count', 'mark': 'arc', 'fraction': .25, 'view': 'shelves', 'presentation_revision': 1},
    {'key': 'counter_wait', 'label': 'Wait at the counter', 'value': 95, 'unit': 's', 'state': 'ready', 'core': False, 'series': POINTS, 'kind': 'duration', 'mark': 'bars', 'view': 'count', 'presentation_revision': 1}]


def test_a_company_tile_draws_the_mark_and_the_format_its_presentation_names():
    script = """const M=require(process.argv[1]),[maintained,rows]=JSON.parse(process.argv[2]);
      const agents=[{id:'a1',agent_kind:'tracker',title:'Freezer door',status:'active',runs:[{id:'r1',created_at:1791590400}]},{id:'a3',agent_kind:'tracker',title:'Entrance groups',status:'active'}];
      const model=M.from({...maintained,core_trackers:rows,agents},{period:'week'}),row=t=>[t.key,t.label,t.text,t.unit,t.kind,t.mark,t.fraction,t.core,t.custom,t.stage,t.agent_id??null,t.presentation_revision??null,t.view??null];
      console.log(JSON.stringify({own:model.agents.map(row),month:row(model.tileFor('agent_freezer','month')),core:model.core.length}));"""
    answer = json.loads(subprocess.run(['node', '-e', script, str(WEB / 'home-model.js'), json.dumps([MAINTAINED, PRESENTED])], check=True, capture_output=True, text=True).stdout)
    assert answer['own'] == [
        ['agent_freezer', 'Freezer door left open', '6', 'times', 'count', 'bars', None, False, True, False, 'a1', 2, 'count'],
        # A key that sounds like a Core Tracker keeps the format and the mark its presentation names.
        ['queue-watch', 'Queue watch', '3', 'alerts', 'count', 'line', None, False, True, False, None, 1, 'count'],
        ['shelf_checks', 'Shelf checks on time', '86.5', '%', 'rate', 'ring', .865, False, True, False, 'a2', 1, 'rate'],
        ['returns-desk', 'Returns at the desk', '€84.50', '', 'money', 'line', None, False, True, False, None, 1, 'count'],
        ['chiller', 'Chiller shelves', '1', 'empty', 'count', 'arc', .25, False, True, False, None, 1, 'shelves'],
        ['counter_wait', 'Wait at the counter', '1:35', 'min', 'duration', 'bars', None, False, True, False, None, 1, 'count'],
        # One tile for one agent: the agent whose tracker publishes a row is that row; an agent before its first output keeps its own tile.
        ['agent:a3', 'Entrance groups', '0', '', 'count', 'line', None, False, True, False, 'a3', None, None]]
    assert answer['month'][2:4] == ['180', 'times'] and answer['core'] == 0


def test_a_paused_agent_pauses_its_trackers_tile_and_a_row_carries_what_the_shell_draws():
    script = """const M=require(process.argv[1]),[maintained,rows]=JSON.parse(process.argv[2]);
      const agents=[{id:'a1',agent_kind:'tracker',title:'Freezer door',status:'paused'},{id:'a2',agent_kind:'tracker',title:'Shelf checks',status:'active'},{id:'a3',agent_kind:'tracker',title:'Entrance groups',status:'paused'}];
      const waiting={key:'gate',label:'Gate left open',value:null,state:'needs',needs:'Needs first run',core:false,agent_id:'a4',kind:'count',mark:'line'};
      const resting={key:'doors',label:'Doors closed in time',value:20,core:false,kind:'count',mark:'ring',problems:[{block:'tile',field:'fraction',needs:'Needs a share of a whole.'}]};
      const canvases={chiller:{revision:4,presentation:{family:'state',blocks:[],revision:1}}};
      const model=M.from({...maintained,core_trackers:[...rows,waiting,resting],core_tracker_canvases:canvases,agents:[...agents,{id:'a4',agent_kind:'tracker',title:'Gate',status:'paused'}]},{period:'week'});
      const row=t=>[t.key,t.state,t.text,t.unit],own=model.agents;
      console.log(JSON.stringify({own:own.map(row),month:row(model.tileFor('agent_freezer','month')),listed:Object.keys(own[0]).includes('published'),
        rides:own.map(t=>!!t.published&&t.published.row.key===t.key),raw:model.tile('chiller').published.row===undefined?null:[model.tile('chiller').published.row.unit,model.tile('chiller').published.canvas.presentation.family],
        problems:model.tile('doors').problems,fraction:model.tile('doors').fraction}));"""
    answer = json.loads(subprocess.run(['node', '-e', script, str(WEB / 'home-model.js'), json.dumps([MAINTAINED, PRESENTED])], check=True, capture_output=True, text=True).stdout)
    states = {row[0]: row[1] for row in answer['own']}
    # The tile of a tracker whose Tracking Agent is paused says so, whether it comes from the page row or from the agent itself.
    assert states['agent_freezer'] == 'paused' and states['agent:a3'] == 'paused' and states['shelf_checks'] == 'live' and answer['month'][1] == 'paused'
    assert states['gate'] == 'needs', 'a tracker still to run first says what it needs'
    assert len([key for key in states if key in {'agent:a1', 'agent:a2', 'agent:a4'}]) == 0, 'one tile for one agent'
    # The page's own row and canvas ride with each supplied tile, outside its listed fields: the shell's canvas reads them as published.
    assert answer['listed'] is False and answer['rides'][:-1] == [True] * (len(answer['rides']) - 1) and answer['rides'][-1] is False
    assert answer['raw'] == ['empty', 'state']
    # A tile whose mark waits for a field carries the row's problems.
    assert answer['problems'] == [{'block': 'tile', 'field': 'fraction', 'needs': 'Needs a share of a whole.'}] and answer['fraction'] is None


def test_one_grid_holds_core_tiles_then_the_companys_trackers_then_new_tracker(site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page, errors = opened(browser, site, {**MAINTAINED, **PAGE_DATA})
            tiles = page.evaluate(TILES)
            assert [t['title'] for t in tiles] == CORE + ['Freezer door left open', 'Groups at the entrance', 'New tracker']
            order = page.evaluate("()=>[...document.querySelectorAll('#locationPageTiles>.home-tile')].map(t=>t.querySelector('.home-tile-title').textContent)")
            assert order == CORE[4:] + ['Freezer door left open', 'Groups at the entrance', 'New tracker'], 'one grid, New tracker at its end'
            assert [h.inner_text() for h in page.locator('#locationRoot h2').all() if h.is_visible()] == ['Products', 'Neighbourhood'], 'Home has no agents section'
            # An estimated money figure says so beside its numeral; nothing else carries the tag.
            tags = page.evaluate("()=>Object.fromEntries([...document.querySelectorAll('.home-tile[data-tile]')].map(t=>[t.dataset.tile,t.querySelector('.home-tile-tag').textContent]))")
            assert tags['basket'] == 'Estimated' and [key for key, text in tags.items() if text] == ['basket']
            assert page.locator('[data-tile=basket]').get_attribute('aria-label') == 'Basket, €12.40, estimated. Open history'
            # The tag stands whole in the tile's top line, clear of the title and of the numeral.
            box = lambda selector: page.locator('[data-tile=basket] ' + selector).bounding_box()
            tile, tag, title, figure = page.locator('[data-tile=basket]').bounding_box(), box('.home-tile-tag'), box('.home-tile-title'), box('.home-tile-value')
            assert tag['width'] > 40 and tag['x'] >= title['x'] + title['width'] and tag['x'] + tag['width'] <= tile['x'] + tile['width'] - 8
            assert tag['y'] + tag['height'] <= figure['y'] and figure['x'] + figure['width'] <= tile['x'] + tile['width'] - 8
            assert page.locator('.home-board-name small').first.inner_text() == '≈ €2.49'
            assert errors == []
        finally: browser.close()


def test_home_shows_every_core_tracker_as_a_finished_tile(site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page, errors = opened(browser, site, BUNDLE)
            page.evaluate("view.setLocation({name:'Vitosha Boulevard 85',address:'85 Vitosha Boulevard, Sofia'})")
            tiles = page.evaluate(TILES)
            assert [t['title'] for t in tiles] == CORE + ['New tracker'] and all(t['tag'] == 'BUTTON' for t in tiles)
            assert [t['surface'] for t in tiles[:9]] == ['glass'] * 4 + ['paper'] * 5
            assert [(t['value'] or '') + (t['unit'] or '') for t in tiles[:9]] == ['7', '5', '1', '0', '', '0s', '0', '', '']
            assert [t['needs'] for t in tiles[:9]] == ['', '', '', '', 'Needs till data', '', '', 'Needs till data', 'Needs till data']
            assert all(t['mark'] for t in tiles[:9]), 'every tile carries its one mark'
            assert tiles[0]['spoken'] == 'Visitors, 7. Open history' and tiles[4]['spoken'] == 'Basket. Needs till data'
            assert page.locator('#locationViewName').inner_text() == 'Vitosha Boulevard 85'
            assert page.locator('#locationHeroAddress').inner_text() == '85 Vitosha Boulevard, Sofia'
            assert [b.inner_text() for b in page.locator('.home-period [role=radio]').all()] == ['Weekly', 'Monthly', 'Annual']
            assert [h.inner_text() for h in page.locator('.home-board h3').all()] == ['Most handled', 'Most picked up', 'Most put back']
            assert page.locator('.home-board-row img').count() == 2 and page.locator('.home-board-name').first.inner_text() == 'A small, green item'
            # What the page says: the number alone, its need in three words, and the product's one face.
            text = page.locator('#locationRoot').inner_text()
            assert not BANNED.search(text), BANNED.search(text)
            style = page.evaluate("""()=>{const all=[...document.querySelectorAll('#locationRoot *')].filter(e=>e.getClientRects().length&&!e.closest('svg'));
              return {fonts:[...new Set(all.map(e=>getComputedStyle(e).fontFamily.split(',')[0].trim().replace(/"/g,'')))],
                upper:all.filter(e=>getComputedStyle(e).textTransform==='uppercase').length,
                small:all.filter(e=>e.children.length===0&&e.textContent.trim()&&parseFloat(getComputedStyle(e).fontSize)<12).map(e=>e.className)}}""")
            # One face: the product's own, the theme's font token that the chats read.
            face = json.loads((HERE / 'theme.json').read_text())['typography']['font'].split(',')[0].strip().strip('"')
            assert style == {'fonts': [face], 'upper': 0, 'small': []}
            accent = page.evaluate("()=>({live:getComputedStyle(document.querySelector('#locationOpenCameras')).backgroundColor,accent:getComputedStyle(document.documentElement).getPropertyValue('--camera-accent').trim()})")
            assert accent == {'live': 'rgb(235, 130, 62)', 'accent': '#eb823e'}
            page.locator('#locationOpenCameras').click()
            assert page.evaluate('calls') == [['cameras', 'shop-aisle']] and errors == []
        finally: browser.close()


def test_the_maintained_reader_shows_visitors_zero_counts_and_what_a_measure_needs(site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page, errors = opened(browser, site, MAINTAINED)
            tiles = page.evaluate(TILES)
            assert [(t['title'], (t['value'] or '') + (t['unit'] or ''), t['needs']) for t in tiles[:9]] == [
                ('Visitors', '142', ''), ('Product interactions', '0', ''), ('Picked up', '0', ''), ('Put back', '0', ''), ('Basket', '', 'Needs till data'),
                ('Queue time', '0s', ''), ('Empty shelves', '0', ''), ('Sales', '', 'Needs till data'), ('Conversion', '', 'Needs till data')]
            assert page.locator('#locationViewName').inner_text() == 'Vitosha Boulevard 85'
            assert [row.inner_text().split('\n')[-1] for row in page.locator('.home-board-row').all()] == ['0', '0', '0']
            assert not BANNED.search(page.locator('#locationRoot').inner_text()) and errors == []
            # A shop that has only its address is named by it; the word for the kind of place never stands in.
            page.evaluate("view.setData({location:{id:'x',name:'',address:'12 Market Street'},visitors:[],events:[]})")
            assert page.locator('#locationViewName').inner_text() == '12 Market Street' and page.locator('#locationHeroAddress').is_hidden()
        finally: browser.close()


def test_a_number_that_arrives_changes_its_tile_in_place(site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page, errors = opened(browser, site, MAINTAINED)
            page.evaluate("window.kept=[...document.querySelectorAll('.home-tile')];document.querySelector('[data-tile=visitors]').focus()")
            arrived = {**MAINTAINED, 'visitors': {'count': 143}, 'events': [{'id': 'e1', 'type': 'product.pick_up', 'visitor_id': 'v1', 'source_id': 'camera', 'at': '2026-10-10T10:00:00Z'}]}
            page.evaluate("data=>{window.now=Date.parse('2026-10-10T12:00:00Z');view.setData(data)}", arrived)
            after = page.evaluate("""()=>({same:[...document.querySelectorAll('.home-tile')].every((t,i)=>t===kept[i]),focus:document.activeElement.dataset.tile,
              ticked:[...document.querySelectorAll('.home-tile.has-ticked')].map(t=>t.dataset.tile),lit:[...document.querySelectorAll('.home-tile.has-arrived')].map(t=>t.dataset.tile),
              tick:getComputedStyle(document.querySelector('[data-tile=visitors] .home-tile-value')).animationDuration,flash:getComputedStyle(document.querySelector('[data-tile=visitors]'),'::after').animationDuration})""")
            assert after['same'] and after['focus'] == 'visitors', 'tiles are kept, never rebuilt'
            assert after['ticked'] == ['visitors', 'product_interactions', 'picked_up'] == after['lit']
            assert after['tick'] == '0.18s' and after['flash'] == '0.45s'
            assert [t['value'] for t in page.evaluate(TILES)[:4]] == ['143', '1', '1', '0']
            # The period is a view of the same numbers: changing it is quiet.
            page.evaluate("document.querySelectorAll('.has-ticked,.has-arrived').forEach(n=>n.classList.remove('has-ticked','has-arrived'))")
            page.locator('[data-global-period=month]').click()
            assert page.locator('.has-ticked').count() == 0 and page.locator('[data-global-period=month]').get_attribute('aria-checked') == 'true' and errors == []
            page.close()
            # Reduced motion: the number changes, nothing moves.
            page, errors = opened(browser, site, MAINTAINED, reduced_motion='reduce')
            page.evaluate("data=>view.setData(data)", arrived)
            assert [t['value'] for t in page.evaluate(TILES)[:2]] == ['143', '1'] and page.locator('.has-ticked,.has-arrived').count() == 0
            assert page.evaluate("getComputedStyle(document.querySelector('.home-live-dot')).animationName") == 'none' and errors == []
        finally: browser.close()


def test_a_tile_is_a_button_that_opens_its_history_and_the_keyboard_reaches_everything(site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page, errors = opened(browser, site, BUNDLE)
            page.evaluate("""()=>{window.opened=[];window.RocotoHomeHistory={open(tile,model,element){opened.push({key:tile.key,fields:Object.keys(tile),period:tile.period,
              model:typeof model.tile==='function'&&model.tiles.length,element:element===document.querySelector(`[data-tile="${tile.key}"]`)});}};}""")
            page.locator('[data-tile=visitors]').click()
            page.locator('[data-tile=sales]').focus(); page.keyboard.press('Enter')
            first, second = page.evaluate('opened')
            assert (first['key'], first['element'], first['model'], first['period']) == ('visitors', True, 9, 'week') and second['key'] == 'sales' and second['element']
            assert {'key', 'label', 'value', 'unit', 'state', 'needs', 'series', 'events', 'period'} <= set(first['fields'])
            # The period toggle is one radio group: arrows move it, and the tiles then carry that period.
            page.locator('[data-global-period=week]').focus(); page.keyboard.press('ArrowRight')
            assert page.evaluate("document.activeElement.dataset.globalPeriod") == 'month'
            page.locator('[data-tile=visitors]').click()
            assert page.evaluate('opened[2].period') == 'month'
            # Tab order: View live, the period, then every tile, the last of them New tracker.
            page.evaluate("window.heard=0;addEventListener('rocoto:home-new-tracker',()=>heard++)")
            page.locator('#locationOpenCameras').focus(); order = ['locationOpenCameras']
            for _ in range(11):
                page.keyboard.press('Tab'); order.append(page.evaluate("document.activeElement.id||document.activeElement.dataset.tile||document.activeElement.dataset.globalPeriod||document.activeElement.className"))
            assert order[:2] == ['locationOpenCameras', 'month'] and order[2:11] == ['visitors', 'product_interactions', 'picked_up', 'put_back', 'basket', 'queue_time', 'empty_shelves', 'sales', 'conversion']
            assert 'home-tile-new' in order[11]
            page.locator('.home-tile-new').press('Enter')
            assert page.evaluate('heard') == 1
            ring = page.evaluate("()=>{const t=document.querySelector('[data-tile=basket]');t.focus();return getComputedStyle(t).outlineStyle}")
            assert ring == 'solid'
            # Without the shared history module a tile still opens: its number and its events.
            page.evaluate("delete window.RocotoHomeHistory")
            page.locator('[data-tile=picked_up]').click()
            assert page.locator('#locationMetricDialog').evaluate('d=>d.open') and page.locator('#locationMetricTitle').inner_text() == 'Picked up'
            page.locator('.home-tile-history-row').first.click()
            assert page.evaluate('calls.at(-1)') == ['session', {'clipId': 'a', 'personId': 'P1', 'time_s': 1.3}] and errors == []
        finally: browser.close()


def test_marks_come_from_the_shared_chart_module_when_it_is_loaded(site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page = browser.new_page(viewport={'width': 1600, 'height': 1000}); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(site)
            page.evaluate("""data=>{window.drawn=[];const note=kind=>(el,value,opts)=>{drawn.push([el.closest('[data-tile]').dataset.tile,kind,Array.isArray(value)?value.length:value,opts.surface]);el.innerHTML='<svg></svg>';};
              window.RocotoCharts={line:note('line'),spark:note('spark'),ring:note('ring'),arc:note('arc'),bars:note('bars'),ghost:(el,kind,opts)=>note('ghost:'+kind)(el,null,opts)};
              window.view=LocationView.create({root:document.querySelector('#locationRoot'),data});view.show();}""", BUNDLE)
            assert page.evaluate('drawn') == [['visitors', 'line', 3, 'glass'], ['product_interactions', 'line', 3, 'glass'], ['picked_up', 'ring', .2, 'glass'], ['put_back', 'ring', 0, 'glass'],
                ['basket', 'ghost:line', None, 'paper'], ['queue_time', 'spark', 2, 'paper'], ['empty_shelves', 'ring', 0, 'paper'], ['sales', 'ghost:line', None, 'paper'], ['conversion', 'ghost:arc', None, 'paper']]
            # The same numbers again draw nothing; a changed share redraws that one mark.
            page.evaluate("data=>{drawn.length=0;view.setData(data);}", BUNDLE)
            assert page.evaluate('drawn') == []
            changed = json.loads(json.dumps(BUNDLE)); changed['locations'][0]['metrics']['observed_pickups'] = 2
            page.evaluate("data=>view.setData(data)", changed)
            assert page.evaluate('drawn') == [['picked_up', 'ring', .4, 'glass']] and errors == []
            # A company tracker's tile draws the mark its presentation names: bars, a ring, an arc, a line.
            page.evaluate("data=>{drawn.length=0;view.setData(data);}", {**MAINTAINED, 'core_trackers': PRESENTED})
            assert page.evaluate('drawn') == [['agent_freezer', 'bars', 7, 'paper'], ['queue-watch', 'spark', 7, 'paper'], ['shelf_checks', 'ring', .865, 'paper'],
                ['returns-desk', 'spark', 7, 'paper'], ['chiller', 'arc', .25, 'paper'], ['counter_wait', 'bars', 7, 'paper']] and errors == []
        finally: browser.close()


@pytest.mark.parametrize('size', [(1600, 1000), (1096, 820), (390, 844), (320, 640)])
def test_home_fits_its_window(site, size):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            data = {**MAINTAINED, 'agents': [{'id': 'a1', 'agent_kind': 'tracker', 'title': 'Freezer door left open for longer than a minute', 'status': 'active'}]}
            for payload in (BUNDLE, data):
                page, errors = opened(browser, site, payload, size)
                page.evaluate("view.setLocation({name:'Vitosha Boulevard 85',address:'85 Vitosha Boulevard, Sofia'})")
                fit = page.evaluate("""()=>{const w=document.documentElement.clientWidth,out=[];
                  for(const el of document.querySelectorAll('#locationRoot *')){if(el.closest('dialog,svg,.home-tags'))continue;const r=el.getBoundingClientRect();if(r.width&&(r.right>w+.5||r.left<-.5))out.push(el.className);}
                  // Inside a tile the numeral (or what the measure needs) and its mark stand side by side, each whole.
                  const squeezed=[...document.querySelectorAll('.home-tile[data-tile]')].filter(tile=>{const copy=[...tile.querySelectorAll('.home-tile-figure,.home-tile-needs')].find(n=>n.getClientRects().length),mark=tile.querySelector('.home-tile-mark').getBoundingClientRect(),box=tile.getBoundingClientRect(),text=copy.getBoundingClientRect();
                    return text.right>box.right-8||text.left<box.left||mark.width>0&&(text.right>mark.left+.5||mark.width<24||mark.right>box.right);}).map(tile=>tile.dataset.tile);
                  return {page:document.documentElement.scrollWidth<=w,out,squeezed}}""")
                assert fit == {'page': True, 'out': [], 'squeezed': []} and errors == [], (size, fit)
                page.close()
        finally: browser.close()


def page_data(visitors, picked, sales=None):
    """The page data a tracker pass publishes, in small: three Core Trackers with their week, month and year readings."""
    def tracker(key, label, value, unit=''):
        reading = lambda scale: {'value': None if value is None else value * scale, 'unit': unit, 'state': 'needs' if value is None else 'ready',
            'needs': 'Needs till data' if value is None else None, 'series': [], 'summary': '', 'period_label': 'period',
            'canvas': {'key': key, 'rows': [], 'total_rows': 0, 'state': 'ready', 'needs': None, 'summary': ''}}
        return {'key': key, 'label': label, **{k: v for k, v in reading(1).items() if k not in {'canvas', 'period_label'}}, 'events': [], 'core': True, 'chat': False,
                'locked': True, 'periods': {'week': reading(1), 'month': reading(4), 'year': reading(50)}}
    return {'schema_version': 1, 'clips': [], 'location': {'id': 'shop', 'name': 'Corner shop'}, 'currency': 'EUR', 'products': [], 'baskets': {},
            'core_trackers': [tracker('visitors', 'Visitors', visitors), tracker('picked_up', 'Picked up', picked), tracker('sales', 'Sales', sales)],
            'core_tracker_canvases': {}}


VIEWER = """data=>{
  window.data=data;window.trackData={};window.maintained=()=>false;window.visitBasket=null;window.intervals=[];
  const every=window.setInterval;window.setInterval=(run,ms)=>{intervals.push(ms);return every(run,ms);};
  window.AreaWeather={create:()=>({place:null,mountMap(){},open(){},status(){return null;}})};
  const shell=document.createElement('div');shell.innerHTML='<div id="benchmarkWorkspace"></div><div id="headerWeather"></div><button id="locationBack"></button><a class="brand" href="#"></a><div style="height:3000px"></div>';
  document.body.append(shell);ControlRoom.init();LocationView.active.show();}"""
SEEN = """()=>({same:[...document.querySelectorAll('.home-tile[data-tile]')].every((t,i)=>t===kept[i]),count:document.querySelectorAll('.home-tile[data-tile]').length,
  values:[...document.querySelectorAll('.home-tile[data-tile] .home-tile-value')].map(n=>n.textContent),month:document.querySelector('[data-global-period=month]').getAttribute('aria-checked'),
  open:document.querySelector('dialog.rocoto-history')===sheet&&sheet.open,numeral:document.querySelector('dialog.rocoto-history .rocoto-history-numeral strong').textContent,
  scroll:document.scrollingElement.scrollTop,products:LocationView.active.model().tiles.length})"""


def test_a_new_revision_refreshes_the_tiles_in_place_and_a_hidden_page_rests(site):
    """Trackers run by themselves: the page asks for the revision of its data every 15 seconds and reads the data
    again once it moves. The tiles stay the same nodes; the open full view, the chosen period and the scroll stay."""
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 800}, reduced_motion='reduce'); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            served = {'revision': 'r1', 'data': page_data(7, 2), 'asked': [], 'pages': 0, 'fail': False}
            def revision(route):
                tag = '"' + served['revision'] + '"'
                served['asked'].append(route.request.headers.get('if-none-match'))
                if route.request.headers.get('if-none-match') == tag: route.fulfill(status=304, headers={'ETag': tag})
                else: route.fulfill(status=200, content_type='application/json', headers={'ETag': tag},
                                    body=json.dumps({'schema': 'rocoto.home-revision.v1', 'revision': served['revision'], 'outputs': [], 'run': None}))
            def data(route):
                served['pages'] += 1
                if served['fail']: route.fulfill(status=503, body='')
                else: route.fulfill(status=200, content_type='application/json', body=json.dumps(served['data']))
            page.route('**/api/control-room/revision', revision); page.route('**/data.json', data)
            page.goto(site)
            for script in ('/display-charts.js', 'home-history.js', 'location-data.js', 'control-room.js'): page.add_script_tag(url=script)
            page.evaluate(VIEWER, served['data'])
            page.wait_for_function("()=>document.querySelectorAll('.home-tile[data-tile]').length===3")
            assert served['asked'] == [None], 'the first revision is asked as the script loads, ahead of the page data'
            assert 15000 in page.evaluate('intervals')
            page.locator('[data-global-period=month]').click()
            page.locator('[data-tile=visitors]').click(); page.wait_for_selector('dialog.rocoto-history[open]')
            page.evaluate("()=>{window.kept=[...document.querySelectorAll('.home-tile[data-tile]')];window.sheet=document.querySelector('dialog.rocoto-history');document.scrollingElement.scrollTop=240;}")
            before = page.evaluate(SEEN)
            assert before['values'][:2] == ['28', '8'] and before['numeral'] == '28' and before['scroll'] == 240
            check = lambda: page.evaluate('ControlRoom.checkRevision()')
            # The same revision: one small question, a 304, and the page data stays unread.
            check()
            assert served['asked'] == [None, '"r1"'] and served['pages'] == 0 and page.evaluate(SEEN) == before
            # A pass published new page data, with a new recording among its clips.
            held = {'id': 'held-clip', 'title': 'Aisle', 'duration_s': 4}
            page.evaluate('clip=>{data.clips.push(clip);window.heldClip=data.clips[0];}', held)
            served.update(revision='r2', data={**page_data(9, 3, 120), 'clips': [{**held, 'title': 'Aisle, read again'}, {'id': 'new-clip', 'title': 'Till', 'duration_s': 6}]})
            check()
            after = page.evaluate(SEEN)
            assert served['pages'] == 1 and after['values'] == ['36', '12', '€480'] and after['numeral'] == '36'
            assert page.evaluate("()=>[data.clips.map(c=>c.id),data.clips[0]===heldClip,data.clips[0].title]") == [['held-clip', 'new-clip'], True, 'Aisle'], \
                'a new recording joins; the ones the player holds stay the same objects'
            assert after['same'] and after['count'] == 3, 'the tiles are the same nodes'
            assert after['month'] == 'true' and after['open'] and after['scroll'] == 240, 'the chosen period, the open full view and the scroll position stay'
            check()
            assert served['asked'][-1] == '"r2"' and served['pages'] == 1
            # A hidden page asks nothing; shown again, it asks at once.
            page.evaluate("()=>Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.hiddenNow===true})")
            page.evaluate('window.hiddenNow=true'); served.update(revision='r3', data=page_data(10, 3, 120))
            asked = len(served['asked']); check(); check()
            assert len(served['asked']) == asked and served['pages'] == 1
            page.evaluate("()=>{window.hiddenNow=false;document.dispatchEvent(new Event('visibilitychange'));}")
            # Well inside the 15 seconds of the timer: the page asks because it is shown again.
            page.wait_for_function("()=>document.querySelector('[data-tile=visitors] .home-tile-value').textContent==='40'", timeout=3000)
            assert served['pages'] == 2 and page.evaluate(SEEN)['same']
            # The shell rests the viewer while another screen covers it.
            page.evaluate("()=>window.postMessage({type:'rocoto:player-suspend'},location.origin)"); page.wait_for_timeout(50)
            asked = len(served['asked']); served.update(revision='r4', data=page_data(11, 3, 120)); check()
            assert len(served['asked']) == asked
            # A page read that fails leaves the revision as it was, so the next check reads again.
            served['fail'] = True
            page.evaluate("()=>window.postMessage({type:'rocoto:player-resume'},location.origin)")
            page.wait_for_function("()=>true"); page.wait_for_timeout(150)
            assert served['pages'] == 3 and page.evaluate(SEEN)['values'][0] == '40'
            served['fail'] = False; check()
            assert served['asked'][-1] == '"r3"' and served['pages'] == 4 and page.evaluate(SEEN)['values'][0] == '44'
            assert errors == []
        finally: browser.close()


def test_home_names_the_state_of_its_trackers_from_the_same_poll(site):
    """What the tools write reaches the page: trackers on request, a pass that stopped, a location that waits."""
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 800}, reduced_motion='reduce'); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            served = {'tag': 1, 'schedule': None, 'run': None, 'pages': 0}
            def revision(route):
                tag = f'"r1.{served["tag"]}"'
                if route.request.headers.get('if-none-match') == tag: route.fulfill(status=304, headers={'ETag': tag})
                else: route.fulfill(status=200, content_type='application/json', headers={'ETag': tag}, body=json.dumps(
                    {'schema': 'rocoto.home-revision.v1', 'revision': 'r1', 'outputs': [], 'run': served['run'], 'schedule': served['schedule']}))
            def data(route):
                served['pages'] += 1
                route.fulfill(status=200, content_type='application/json', body=json.dumps(page_data(7, 2)))
            page.route('**/api/control-room/revision', revision); page.route('**/data.json', data)
            page.goto(site)
            for script in ('/display-charts.js', 'home-history.js', 'location-data.js', 'control-room.js'): page.add_script_tag(url=script)
            page.evaluate(VIEWER, page_data(7, 2))
            page.wait_for_function("()=>document.querySelectorAll('.home-tile[data-tile]').length===3")
            line = lambda: page.evaluate("()=>{const n=document.querySelector('#locationTrackerState');return n&&!n.hidden?n.textContent:null;}")
            def tell(schedule, run):
                served.update(tag=served['tag'] + 1, schedule=schedule, run=run)
                page.evaluate('ControlRoom.checkRevision()')
                return line()
            healthy = {'automatic': True, 'needs_attention': False, 'running': False, 'requested': False, 'service': 'on'}
            done = {'status': 'completed', 'trigger': 'events', 'started_at': '2026-10-11T12:00:00Z', 'finished_at': '2026-10-11T12:00:01Z', 'error': None}
            assert line() is None and tell(healthy, done) is None, 'a location whose trackers are current shows its tiles alone'
            assert tell({**healthy, 'automatic': False}, done) == 'Trackers run on request at this location.'
            stopped = {**done, 'status': 'failed', 'error': 'A Core Tracker stopped. Its last output stays in place.'}
            assert tell(healthy, stopped) == 'A Core Tracker stopped. Its last output stays in place.'
            waiting = {**done, 'status': 'failed', 'error': 'These trackers run as the member who installed them. That member needs management access to this location.'}
            assert tell({**healthy, 'needs_attention': True}, waiting) == waiting['error'] + ' The trackers wait for new observations or a request.'
            assert tell({**healthy, 'service': 'off'}, done) == 'The tracker service is switched off. The tiles show the last completed pass.'
            assert page.evaluate("()=>{const n=document.querySelector('#locationTrackerState');return [n.getAttribute('role'),n.className,n.previousElementSibling.id,document.querySelector('#locationRoot').dataset.trackers];}") == ['status', 'home-address', 'locationHeroAddress', 'off']
            assert tell(healthy, done) is None and page.evaluate("()=>document.querySelector('#locationRoot').dataset.trackers") == 'current'
            assert served['pages'] == 0 and errors == [], 'the state arrives with the revision; the page data is read when its revision moves'
        finally: browser.close()


def test_an_open_full_view_keeps_its_period_its_scroll_and_its_focus_across_a_refresh(site):
    """A tracker pass publishes while a full view is open on another period than Home shows. The sheet stays on its
    own period, takes that period's new number, and keeps how far it is scrolled and the row that holds the focus."""
    playwright = pytest.importorskip('playwright.sync_api')
    def listed(visitors):
        data = page_data(visitors, 2)
        row = lambda n: {'id': f'v{n}', 'at': f'2026-10-10T{8 + n // 4:02d}:{n % 4 * 15:02d}:00+03:00', 'label': f'Visitor {n + 1}', 'description': 'Walks the tools aisle', 'thumb': None, 'visitor_id': f'P{n}', 'source_id': 'clip-a'}
        for reading in data['core_trackers'][0]['periods'].values():
            reading['canvas']['rows'] = [row(n) for n in range(24)]; reading['canvas']['total_rows'] = 24
        return data
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 800}, reduced_motion='reduce'); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            served = {'revision': 'r1', 'data': listed(7)}
            def revision(route):
                tag = '"' + served['revision'] + '"'
                if route.request.headers.get('if-none-match') == tag: route.fulfill(status=304, headers={'ETag': tag})
                else: route.fulfill(status=200, content_type='application/json', headers={'ETag': tag}, body=json.dumps({'schema': 'rocoto.home-revision.v1', 'revision': served['revision'], 'outputs': [], 'run': None}))
            page.route('**/api/control-room/revision', revision)
            page.route('**/data.json', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(served['data'])))
            page.goto(site)
            for sheet in ('/display-charts.css', 'home-history.css'): page.add_style_tag(url=sheet)
            for script in ('/display-charts.js', 'home-history.js', 'location-data.js', 'control-room.js'): page.add_script_tag(url=script)
            page.evaluate(VIEWER, served['data'])
            page.wait_for_function("()=>document.querySelectorAll('.home-tile[data-tile]').length===3")
            page.locator('[data-tile=visitors]').click(); page.wait_for_selector('dialog.rocoto-history[open]')
            # The sheet moves to Monthly while Home stays on Weekly.
            page.locator('dialog.rocoto-history [data-period=month]').click()
            assert page.evaluate("RocotoHomeHistory.period") == 'month'
            page.evaluate("""()=>{const list=document.querySelector('dialog.rocoto-history .rocoto-history-made');list.scrollTop=220;
              document.querySelector('dialog.rocoto-history [data-row="events:9"]').focus({preventScroll:true});list.dataset.kept='yes';}""")
            sheet = """()=>{const d=document.querySelector('dialog.rocoto-history'),list=d.querySelector('.rocoto-history-made');
              return {numeral:d.querySelector('.rocoto-history-numeral strong').textContent,period:[...d.querySelectorAll('[data-period]')].find(b=>b.getAttribute('aria-checked')==='true').dataset.period,
                scroll:list.scrollTop,focus:document.activeElement.dataset.row||document.activeElement.tagName,kept:list.dataset.kept||null,
                home:document.querySelector('[data-global-period][aria-checked=true]').dataset.globalPeriod,tile:document.querySelector('[data-tile=visitors] .home-tile-value').textContent}}"""
            before = page.evaluate(sheet)
            assert before == {'numeral': '28', 'period': 'month', 'scroll': 220, 'focus': 'events:9', 'kept': 'yes', 'home': 'week', 'tile': '7'}
            # The same numbers published again leave the open sheet exactly as it stands.
            served.update(revision='r2', data=listed(7)); page.evaluate('ControlRoom.checkRevision()')
            assert page.evaluate(sheet) == before, 'the sheet is drawn again only when its tile changed'
            # New numbers: the sheet takes its own period's reading and the reader keeps their place.
            served.update(revision='r3', data=listed(9)); page.evaluate('ControlRoom.checkRevision()')
            after = page.evaluate(sheet)
            assert after == {'numeral': '36', 'period': 'month', 'scroll': 220, 'focus': 'events:9', 'kept': None, 'home': 'week', 'tile': '9'}
            assert page.evaluate("RocotoHomeHistory.period") == 'month' and errors == []
        finally: browser.close()

