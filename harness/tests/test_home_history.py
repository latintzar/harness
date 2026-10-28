"""The marks (RocotoCharts) and the full view of a Home tile (RocotoHomeHistory), for all nine Core Trackers."""
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
PAGE = b'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link href="kit/rocoto-kit.css" rel="stylesheet"><link href="kit/twelvelabs-material.css" rel="stylesheet"><link href="/display-charts.css" rel="stylesheet"><link href="home-history.css" rel="stylesheet">
<style>html,body{margin:0;background:var(--camera-paper)}[hidden]{display:none!important}.stage{padding:24px;background:var(--camera-night)}
.tile{display:block;width:220px;height:120px;margin:8px;padding:0;border:0}.mark{display:block;width:180px;height:64px;margin:8px}.mark.round{width:72px;height:72px}</style>
<body><div class="stage"><button type="button" class="tile twelvelabs-card-material" id="glassTile">Glass</button><div class="twelvelabs-card-material"><span class="mark" id="glassMark"></span></div></div>
<button type="button" class="tile" id="paperTile">Paper</button><div id="marks"></div>
<script src="/display-charts.js"></script><script src="home-history.js"></script>'''
BANNED = re.compile(r'recording|saved observation|demo footage|recorded replay|available|starts with|awaiting|waiting for|\bof \d+ interactions|—|\bPOS\b|\bfrom \d+ (cameras|recordings)', re.I)
NEGATIVE = re.compile(r"\b(no|not|never|without|cannot|nothing)\b|n't", re.I)
SIZES = [(1600, 1000), (390, 844)]


def at(hour, minute, second=0):
    return f'2026-10-10T{hour:02d}:{minute:02d}:{second:02d}+03:00'


WEEK = [{'t': f'2026-10-{day:02d}T00:00:00+03:00', 'value': value} for day, value in zip(range(4, 11), [120, 140, 150, 160, 170, 240, 112])]
scaled = lambda factor, digits=0: [{'t': p['t'], 'value': round(p['value'] * factor, digits) if digits else round(p['value'] * factor)} for p in WEEK]


def event(index, label, description, **more):
    return {'id': f'e{index}', 'at': at(17, 40 - index), 'label': label, 'description': description, 'thumb': '/thumb.jpg', 'visitor_id': f'visit-{index}',
            'source_id': 'clip-a', 'camera_id': 'camera-1', 'person_id': f'P{index}', 'at_s': 1.5 + index, 'type': 'pick_up', 'product': None, **more}


def line(product, quantity, price, basis='estimated'):
    return {'product': product, 'product_id': product.lower().replace(' ', '-'), 'quantity': quantity, 'unit_price': price, 'amount': round(quantity * price, 2),
            'currency': 'EUR', 'basis': basis, 'image': '/thumb.jpg'}


def held(index, label, lines, basis='estimated'):
    total = round(sum(item['amount'] for item in lines), 2)
    return {'id': f'{label.lower()}-{index}', 'at': at(17, 28 - index), 'label': label, 'description': ', '.join(f"{item['quantity']} x {item['product']}" for item in lines),
            'thumb': '/thumb.jpg', 'visitor_id': f'visit-{index}', 'source_id': 'clip-a', 'camera_id': 'camera-1', 'person_id': f'P{index}',
            'units': sum(item['quantity'] for item in lines), ('total' if label == 'Sale' else 'amount'): total, 'currency': 'EUR', 'basis': basis, 'lines': lines}


def wait(index, seconds, end):
    start = f"{end[:17]}{int(end[17:19]) - seconds:02d}{end[19:]}"
    return {'id': f'queue-{index}', 'at': end, 'label': 'Queue wait', 'description': f'Waited {seconds} s', 'thumb': None, 'visitor_id': f'visit-{index}', 'source_id': 'clip-till',
            'camera_id': 'camera-till', 'person_id': f'P{index}', 'start_at': start, 'end_at': end, 'wait_s': seconds}


def shelf(key, label, camera, spans):
    last = spans[-1] if spans else None
    empty = bool(last) and last[1] is None
    return {'shelf': key, 'label': label, 'camera_id': 'camera-1', 'camera': camera, 'state': 'empty' if empty else 'stocked', 'empty_since': last[0] if empty else None,
            'replenished_at': next((s[1] for s in reversed(spans) if s[1]), None), 'intervals': [{'empty_since': a, 'replenished_at': b} for a, b in spans]}


COUNT_ROWS = [event(0, 'Pick up item', 'Takes a green pack from the pegboard'), event(1, 'Examine item', 'Turns the pack over in both hands'), event(2, 'Put back item', 'Hangs the pack back on its hook')]
BASKETS = [held(0, 'Basket', [line('Climbing cord', 1, 8.7), line('Carabiner', 2, 4.2)]), held(1, 'Basket', [line('Headlamp', 1, 18.5, 'pos'), line('Batteries', 1, 3.9)], 'pos')]
SALES = [held(0, 'Sale', [line('Headlamp', 1, 18.5), line('Batteries', 2, 3.9)])]
SHELVES = [shelf('s3', 'Boxed tools, lower shelf', 'Aisle 1', [(at(11, 48), at(12, 31))]), shelf('s6', 'Tarps and pegs, end cap', 'Aisle 1', []),
           shelf('s1', 'Cord and rope, top shelf', 'Aisle 3', [(at(16, 12), None)]), shelf('s2', 'Pegboard hooks, row 2', 'Aisle 2', [(at(9, 2), at(9, 31)), (at(15, 3), None)])]


def canvas(key, **more):
    return {'key': key, 'state': 'ready', 'needs': None, 'period': {'start': at(0, 0), 'end': at(23, 59), 'label': '4 to 10 October'}, 'rows': [], 'total_rows': 0, **more}


def tile(key, label, value, unit='', **more):
    return {'key': key, 'label': label, 'value': value, 'unit': unit, 'state': 'live', 'needs': None, 'series': WEEK, 'events': [], 'period': 'week', **more}


TILES = {t['key']: t for t in [
    tile('visitors', 'Visitors', 1092, text='1,092', kind='count', canvas=canvas('visitors', rows=[{**r, 'label': 'Visitor'} for r in COUNT_ROWS], summary='Saturday is the busiest day of the week.')),
    tile('product_interactions', 'Product interactions', 354, kind='count', canvas=canvas('product_interactions', rows=COUNT_ROWS, summary='The pegboard packs are handled most.')),
    tile('picked_up', 'Picked up', 225, kind='count', canvas=canvas('picked_up', rows=COUNT_ROWS[:1])),
    tile('put_back', 'Put back', 62, kind='count', canvas=canvas('put_back', rows=COUNT_ROWS[2:])),
    tile('basket', 'Basket', 10.55, text='€10.55', kind='money', series=scaled(.07, 2), canvas=canvas('basket', rows=BASKETS, baskets=221, units=707, basis='estimated', summary='A basket holds 3.2 items on average.')),
    tile('queue_time', 'Queue time', 15, 's', kind='duration', series=scaled(.1), canvas=canvas('queue_time', rows=[wait(0, 19, at(17, 26, 30)), wait(1, 8, at(16, 48, 20))], total_rows=160, longest_s=41,
         distribution=[{'band': 'under 10 s', 'value': 52}, {'band': '10 to 20 s', 'value': 68}, {'band': '20 to 30 s', 'value': 31}, {'band': '30 s and over', 'value': 9}], summary='160 visitors queued this week.')),
    tile('empty_shelves', 'Empty shelves and replenishment', 2, 'empty now', kind='count', series=[{'t': p['t'], 'value': v} for p, v in zip(WEEK, [1, 1, 1, 1, 1, 2, 2])],
         canvas=canvas('empty_shelves', shelves=SHELVES, became_empty=4, rows=[event(9, 'Shelf empty', 'Cord and rope, top shelf is empty')], summary='2 shelves are empty now.')),
    tile('sales', 'Sales', 2638.92, text='€2,638.92', kind='money', series=scaled(2.4, 2), canvas=canvas('sales', rows=SALES, transactions=203, units=648, basis='estimated', summary='Saturday is the strongest day.')),
    tile('conversion', 'Conversion', 18.6, '%', kind='rate', series=scaled(.1, 1), canvas=canvas('conversion', visitors=1092, purchases=203, ratio=.1859, basis='estimated', summary='Saturday converts best.')),
]}
NEEDS = {'key': 'sales', 'label': 'Sales', 'value': None, 'unit': '', 'state': 'needs', 'needs': 'Needs till data', 'series': [], 'events': [], 'period': 'week', 'kind': 'money',
         'canvas': {'key': 'sales', 'state': 'needs', 'needs': 'Needs till data', 'rows': [], 'total_rows': 0, 'summary': 'Sales start when the till is connected or a visitor leaves with a basket.'}}
NUMERALS = {'visitors': '1,092', 'product_interactions': '354', 'picked_up': '225', 'put_back': '62', 'basket': '€10.55', 'queue_time': '15s', 'empty_shelves': '2empty now',
            'sales': '€2,638.92', 'conversion': '18.6%'}


def theme_css():
    tokens = json.loads((HERE / 'theme.json').read_text()); values = {**tokens['palette'], **tokens['typography']}
    values.update(ink='#201e1b', paper='#ffffff', accent='#ed5a2b', border='#ddd', muted='#777', surface='#f2f1ee', radius='0.75rem')
    values['radius-sm'] = '0.5rem'; values['radius-lg'] = '1.25rem'
    return (':root{' + ''.join(f'--{k}:{v};' for k, v in values.items()) + '}').encode()


@pytest.fixture(scope='module')
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0].lstrip('/')
            target = (WEB / path).resolve() if path else None
            if not path: body, kind = PAGE, 'text/html'
            elif path == 'theme.css': body, kind = theme_css(), 'text/css'
            elif target.is_file() and target.is_relative_to(WEB.resolve()):
                body = target.read_bytes(); kind = {'.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}.get(target.suffix, 'application/octet-stream')
            else: body, kind = b'', 'application/octet-stream'
            self.send_response(200); self.send_header('Content-Type', kind)
            if not path: self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}/'
    server.shutdown(); server.server_close()


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        opts = {'headless': True, 'args': ['--no-sandbox']}
        if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'): opts['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
        chromium = p.chromium.launch(**opts)
        yield chromium
        chromium.close()


SETUP = """()=>{window.calls=[];window.other={};
  for(const type of ['rocoto:open-session','rocoto:open-organization-chat','rocoto:tile-period','rocoto:tile-open','rocoto:tile-close'])document.addEventListener(type,e=>calls.push([type,e.detail]));
  window.ControlRoom={openSession:target=>calls.push(['ControlRoom',target])};
  window.model={period:'week',periods:[{key:'week',label:'Weekly'},{key:'month',label:'Monthly'},{key:'year',label:'Annual'}],timezone:'Europe/Sofia',now:'2026-10-10T17:40:00+03:00',currency:'EUR',
    place:{name:'Vitosha Boulevard 85'},media:id=>id?`/media/${id}.mp4`:null,tileFor:(key,period)=>{calls.push(['tileFor',key,period]);return window.other[period]||null;}};
  window.show=(tile,id)=>RocotoHomeHistory.open(tile,model,document.getElementById(id||'paperTile'));
  window.texts=selector=>[...document.querySelectorAll('dialog.rocoto-history '+selector)].map(node=>node.textContent);}"""


def opened(browser, origin, size=(1600, 1000), motion='reduce'):
    page = browser.new_page(viewport={'width': size[0], 'height': size[1]}, reduced_motion=motion); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' and 'Failed to load resource' not in message.text else None)
    page.goto(origin); page.evaluate(SETUP)
    return page, errors


def show(page, tile, where='paperTile'):
    page.evaluate('([tile,id])=>{show(tile,id)}', [tile, where])
    page.wait_for_selector('dialog.rocoto-history[open]')


def test_the_geometry_of_the_marks():
    done = subprocess.run(['node', str(Path(__file__).with_name('home_charts.cjs'))], check=True, capture_output=True, text=True)
    assert 'ok' in done.stdout


def test_marks_draw_on_glass_and_on_the_page_and_move_to_new_values(site, browser):
    page, errors = opened(browser, site)
    drawn = page.evaluate("""()=>{const host=document.getElementById('marks'),make=(round)=>{const el=document.createElement('span');el.className='mark'+(round?' round':'');host.append(el);return el;};
      const points=[3,5,4,null,8,9].map((value,i)=>({t:`2026-10-0${i+1}T00:00:00Z`,value}));
      const line=RocotoCharts.line(make(),points,{label:'Visitors'}),again=RocotoCharts.line(line.el,points.map(p=>({...p,value:p.value===null?null:p.value+1})),{label:'Visitors'});
      const glass=RocotoCharts.line(document.getElementById('glassMark'),points,{}),spark=RocotoCharts.spark(make(),points),ring=RocotoCharts.ring(make(true),.4,{label:'Picked up'}),arc=RocotoCharts.arc(make(true),1.7),
        bars=RocotoCharts.bars(make(),[2,6,null,4],{highlight:'max'}),ghost=RocotoCharts.ghost(make(),'line');
      const of=h=>({kind:h.kind,surface:h.stage.dataset.surface,size:h.stage.dataset.size||null,painted:h.stage.dataset.painted||null,svgs:h.stage.querySelectorAll('svg').length,role:h.stage.getAttribute('role'),label:h.stage.getAttribute('aria-label')});
      return {same:line===again,line:of(line),paths:(line.stage.querySelector('.rocoto-line-stroke').getAttribute('d').match(/M/g)||[]).length,end:line.stage.querySelector('.rocoto-line-end').getAttribute('visibility'),
        glass:of(glass),spark:of(spark),ring:{...of(ring),fraction:ring.fraction},arc:{...of(arc),fraction:arc.fraction,knob:arc.stage.querySelector('.rocoto-gauge-knob').getAttribute('visibility')},
        bars:{...of(bars),lit:bars.lit,drawn:[...bars.stage.querySelectorAll('.rocoto-bar')].map(b=>Boolean(b.getAttribute('d')))},ghost:of(ghost),
        colours:[...document.querySelectorAll('.rocoto-chart [fill^="#"],.rocoto-chart [stroke^="#"]')].length,tab:[...document.querySelectorAll('#marks .rocoto-chart[tabindex]')].length};}""")
    assert drawn['same'] and drawn['line'] == {'kind': 'line', 'surface': 'paper', 'size': 'tile', 'painted': 'true', 'svgs': 3, 'role': 'img', 'label': 'Visitors'}
    assert drawn['paths'] == 2 and drawn['end'] == 'visible', 'a missing measurement breaks the line; the newest one carries the end point'
    assert drawn['glass']['surface'] == 'glass' and drawn['spark']['size'] == 'spark'
    assert drawn['ring']['fraction'] == pytest.approx(.4) and drawn['ring']['label'] == 'Picked up'
    assert drawn['arc']['fraction'] == 1 and drawn['arc']['knob'] == 'visible', 'a share is held between nothing and all'
    assert drawn['bars']['lit'] == 1 and drawn['bars']['drawn'] == [True, True, False, True]
    assert drawn['ghost']['kind'] == 'ghost' and drawn['colours'] == 0 and drawn['tab'] == 0
    assert errors == []
    page.close()


@pytest.mark.parametrize('size', SIZES, ids=lambda s: f'{s[0]}x{s[1]}')
def test_every_core_tracker_has_its_full_view_and_fits_its_window(site, browser, size):
    page, errors = opened(browser, site, size)
    font = page.evaluate("()=>getComputedStyle(document.documentElement).getPropertyValue('--font').replace(/[\"' ]/g,'')")
    assert len(TILES) == 9
    for key, wanted in TILES.items():
        show(page, wanted, 'glassTile' if key in ('visitors', 'product_interactions', 'picked_up', 'put_back') else 'paperTile')
        seen = page.evaluate("""()=>{const d=document.querySelector('dialog.rocoto-history'),box=d.getBoundingClientRect(),all=[...d.querySelectorAll('*')].filter(n=>!n.closest('svg')&&n.getClientRects().length);
          const title=d.querySelector('.rocoto-history-title'),type=getComputedStyle(title);
          return {title:title.textContent,size:parseFloat(type.fontSize),weight:Number(type.fontWeight),numeral:d.querySelector('.rocoto-history-numeral strong')?.textContent??null,
            chart:Boolean(d.querySelector('.rocoto-history-chart .rocoto-line-stroke')?.getAttribute('d')),periods:[...d.querySelectorAll('[data-period]')].map(b=>b.textContent),
            surface:d.dataset.surface,text:d.innerText,summary:d.querySelector('.rocoto-history-summary')?.textContent??null,
            page:[document.documentElement.scrollWidth,document.documentElement.clientWidth],inside:box.left>=0&&box.top>=0&&box.right<=innerWidth&&box.bottom<=innerHeight,
            wide:all.filter(n=>{const r=n.getBoundingClientRect();return r.right>box.right+1||r.left<box.left-1;}).map(n=>n.className),
            fonts:[...new Set(all.map(n=>getComputedStyle(n).fontFamily.replace(/["' ]/g,'')))],upper:all.filter(n=>getComputedStyle(n).textTransform==='uppercase').length,
            name:d.getAttribute('aria-label')};}""")
        assert seen['title'] == wanted['label'] and seen['size'] >= 18 and seen['weight'] >= 600, 'the heading names the tracker at heading size'
        assert seen['numeral'] == NUMERALS[key] and seen['chart'] and seen['periods'] == ['Weekly', 'Monthly', 'Annual']
        assert seen['surface'] == ('glass' if key in ('visitors', 'product_interactions', 'picked_up', 'put_back') else 'paper')
        assert seen['page'][0] <= seen['page'][1] and seen['inside'] and seen['wide'] == [], (key, seen['page'], seen['wide'])
        assert seen['fonts'] == [font] and seen['upper'] == 0, 'one face, the product\'s own; nothing set in capitals'
        assert not BANNED.search(seen['text']), BANNED.search(seen['text'])
        assert seen['summary'] == wanted['canvas'].get('summary') and seen['name'].startswith(wanted['label'])
        page.evaluate('()=>RocotoHomeHistory.close()')
        page.wait_for_function('()=>!RocotoHomeHistory.isOpen')
    assert errors == []
    page.close()


def test_a_count_lists_the_events_behind_it_and_a_row_opens_that_visitors_session(site, browser):
    page, errors = opened(browser, site)
    show(page, TILES['product_interactions'], 'glassTile')
    rows = page.evaluate("()=>[...document.querySelectorAll('dialog .rocoto-history-rows>li')].map(li=>({button:li.firstElementChild.tagName,bold:li.querySelector('.rocoto-history-copy strong').textContent,"
                         "line:li.querySelector('.rocoto-history-copy span').textContent,time:li.querySelector('time').textContent,thumb:li.querySelector('.rocoto-history-thumb img').getAttribute('src'),preview:li.querySelector('[data-preview]')!==null}))")
    assert rows == [{'button': 'BUTTON', 'bold': r['label'], 'line': r['description'], 'time': f'Sat 17:{40 - i}', 'thumb': '/thumb.jpg', 'preview': True} for i, r in enumerate(COUNT_ROWS)]
    page.locator('dialog button.rocoto-history-row').nth(1).click()
    calls = page.evaluate('()=>calls')
    assert ['rocoto:open-session', {'visitor_id': 'visit-1', 'source_id': 'clip-a', 'at': at(17, 39), 'event_id': 'e1', 'tracker': 'product_interactions', 'person_id': 'P1'}] in calls
    assert ['ControlRoom', {'clipId': 'clip-a', 'personId': 'P1', 'time_s': 2.5}] in calls
    # a page that opens the session itself stops the default
    page.evaluate("()=>{calls.length=0;document.addEventListener('rocoto:open-session',e=>e.preventDefault(),{once:true});}")
    page.locator('dialog button.rocoto-history-row').first.click()
    assert [name for name, _ in page.evaluate('()=>calls')] == ['rocoto:open-session']
    assert errors == []
    page.close()


def test_basket_and_sales_show_their_lines_and_the_estimated_tag(site, browser):
    page, errors = opened(browser, site)
    read = """()=>({tag:texts('.rocoto-history-numeral .rocoto-history-tag'),figures:texts('.rocoto-history-figures dt').map((label,i)=>[label,texts('.rocoto-history-figures dd strong')[i]]),
      groups:[...document.querySelectorAll('dialog .rocoto-history-groups>li')].map(li=>({label:li.querySelector('.rocoto-history-group strong').textContent,time:li.querySelector('.rocoto-history-group .rocoto-history-copy span').textContent,
        total:li.querySelector('.rocoto-history-group .rocoto-history-amount strong').textContent,estimated:li.querySelectorAll('.rocoto-history-group .rocoto-history-tag').length,
        lines:[...li.querySelectorAll('.rocoto-history-lines li')].map(l=>[l.querySelector('.rocoto-history-line-name').textContent,l.querySelector('.rocoto-history-line-quantity').textContent,l.querySelector('.rocoto-history-amount strong').textContent,l.querySelectorAll('.rocoto-history-tag').length,l.querySelector('img')!==null])})),
      beside:[...document.querySelectorAll('dialog .rocoto-history-tag')].every(tag=>tag.textContent==='Estimated'&&tag.parentElement.querySelector('strong')!==null)})"""
    show(page, TILES['basket'])
    basket = page.evaluate(read)
    assert basket['tag'] == ['Estimated'] and basket['figures'] == [['Baskets', '221'], ['Items', '707']] and basket['beside']
    assert basket['groups'][0] == {'label': 'Basket', 'time': 'Sat 17:28', 'total': '€17.10', 'estimated': 1,
        'lines': [['Climbing cord', '1 × €8.70', '€8.70', 0, True], ['Carabiner', '2 × €4.20', '€8.40', 0, True]]}
    # a till total stands alone; an estimated line inside it carries its own tag
    assert basket['groups'][1]['estimated'] == 0 and [l[3] for l in basket['groups'][1]['lines']] == [0, 1]
    page.locator('dialog button.rocoto-history-group').first.click()
    assert ['ControlRoom', {'clipId': 'clip-a', 'personId': 'P0', 'time_s': 0}] in page.evaluate('()=>calls')
    page.evaluate('()=>RocotoHomeHistory.close()')
    show(page, TILES['sales'])
    sales = page.evaluate(read)
    assert sales['tag'] == ['Estimated'] and sales['figures'] == [['Transactions', '203'], ['Items sold', '648']]
    assert sales['groups'] == [{'label': 'Sale', 'time': 'Sat 17:28', 'total': '€26.30', 'estimated': 1, 'lines': [['Headlamp', '1 × €18.50', '€18.50', 0, True], ['Batteries', '2 × €3.90', '€7.80', 0, True]]}]
    assert errors == []
    page.close()


def test_queue_time_shows_the_distribution_and_the_waits(site, browser):
    page, errors = opened(browser, site)
    show(page, TILES['queue_time'])
    seen = page.evaluate("""()=>({bands:texts('.rocoto-history-bars .rocoto-bars-labels li span'),counts:texts('.rocoto-history-bars .rocoto-bars-labels li strong'),
      lit:[...document.querySelectorAll('dialog .rocoto-bars-labels li')].findIndex(li=>li.classList.contains('is-lit')),bars:document.querySelectorAll('dialog .rocoto-history-bars .rocoto-bar').length,
      figures:texts('.rocoto-history-figures dt').map((label,i)=>[label,texts('.rocoto-history-figures dd strong')[i]]),plain:document.querySelector('dialog .rocoto-history-rows').hasAttribute('data-plain'),
      rows:[...document.querySelectorAll('dialog .rocoto-history-rows>li')].map(li=>[li.querySelector('strong').textContent,li.querySelector('.rocoto-history-copy span').textContent,li.querySelector('time').textContent,li.querySelector('.rocoto-history-thumb')!==null])})""")
    assert seen['bands'] == ['Under 10 s', '10 to 20 s', '20 to 30 s', '30 s and over'] and seen['counts'] == ['52', '68', '31', '9'] and seen['lit'] == 1 and seen['bars'] == 4
    assert seen['figures'] == [['Longest wait', '41s'], ['Waits', '160']] and seen['plain']
    assert seen['rows'] == [['Waited 19s', '17:26:11 to 17:26:30', 'Sat 17:26', False], ['Waited 8s', '16:48:12 to 16:48:20', 'Sat 16:48', False]]
    assert errors == []
    page.close()


def test_empty_shelves_lists_each_shelf_with_its_state_and_times(site, browser):
    page, errors = opened(browser, site)
    show(page, TILES['empty_shelves'])
    rows = page.evaluate("()=>[...document.querySelectorAll('dialog .rocoto-history-shelves>li')].map(li=>[li.dataset.empty,li.querySelector('strong').textContent,li.querySelector('.rocoto-history-copy span').textContent,"
                         "[...li.querySelectorAll('.rocoto-history-times>span')].map(s=>s.textContent)])")
    assert rows == [['true', 'Cord and rope, top shelf', 'Aisle 3', ['Empty since 16:12', 'Empty for 1h 28m']],
                    ['true', 'Pegboard hooks, row 2', 'Aisle 2', ['Empty since 15:03', 'Empty for 2h 37m']],
                    ['false', 'Boxed tools, lower shelf', 'Aisle 1', ['Empty since 11:48', 'Replenished 12:31']],
                    ['false', 'Tarps and pegs, end cap', 'Aisle 1', ['Stocked']]]
    assert page.evaluate("()=>document.querySelectorAll('dialog .rocoto-history-rows:not(.rocoto-history-shelves)').length") == 0, 'the shelves are the list; their changes stay in the record'
    assert errors == []
    page.close()


def test_conversion_shows_the_two_counts_and_the_ratio(site, browser):
    page, errors = opened(browser, site)
    show(page, TILES['conversion'])
    seen = page.evaluate("()=>({figures:texts('.rocoto-history-figures dt').map((label,i)=>[label,texts('.rocoto-history-figures dd strong')[i]]),share:document.querySelector('dialog .rocoto-history-share span').style.width,"
                         "tags:document.querySelectorAll('dialog .rocoto-history-tag').length,numeral:texts('.rocoto-history-numeral strong')[0]})")
    assert seen == {'figures': [['Visitors', '1,092'], ['Purchases', '203']], 'share': '18.6%', 'tags': 0, 'numeral': '18.6%'}
    assert errors == []
    page.close()


def test_a_tile_without_a_canvas_record_draws_from_its_own_fields(site, browser):
    page, errors = opened(browser, site)
    bare = lambda key, **more: {k: v for k, v in {**TILES[key], **more}.items() if k != 'canvas'}
    show(page, bare('picked_up', events=[{'id': 'a', 'at': 4.2, 'time': '0:04', 'label': 'Pick up item', 'description': 'Takes the green product', 'thumb': '/thumb.jpg', 'visitor_id': 'P1', 'source_id': 'clip-a'}]))
    assert page.evaluate("()=>[texts('.rocoto-history-rows strong'),texts('.rocoto-history-rows time'),texts('.rocoto-history-summary')]") == [['Pick up item'], ['0:04'], []]
    page.locator('dialog button.rocoto-history-row').click()
    assert ['ControlRoom', {'clipId': 'clip-a', 'personId': 'P1', 'time_s': 4.2}] in page.evaluate('()=>calls')
    page.evaluate('()=>RocotoHomeHistory.close()')
    show(page, bare('queue_time', events=[{'id': f'w{i}', 'at': at(12, i), 'label': 'Waited', 'description': '', 'duration_s': s, 'visitor_id': i, 'source_id': 'clip-a'} for i, s in enumerate([30, 70, 80, 200, 400])]))
    assert page.evaluate("()=>[texts('.rocoto-bars-labels li span'),texts('.rocoto-bars-labels li strong')]") == [['Under 1 min', '1 to 2 min', '2 to 3 min', '3 to 5 min', 'Over 5 min'], ['1', '2', '0', '1', '1']]
    page.evaluate('()=>RocotoHomeHistory.close()')
    show(page, bare('empty_shelves', events=[{'id': 's1', 'type': 'shelf_empty', 'at': at(9, 14), 'item': 'rack', 'item_label': 'Batteries, counter rack', 'source_id': 'clip-a'},
                                             {'id': 's2', 'type': 'shelf_replenished', 'at': at(9, 40), 'item': 'rack', 'source_id': 'clip-a'}]))
    assert page.evaluate("()=>texts('.rocoto-history-shelves .rocoto-history-times>span')") == ['Empty since 09:14', 'Replenished 09:40']
    page.evaluate('()=>RocotoHomeHistory.close()')
    show(page, bare('sales', estimated=True, figures=[{'label': 'Largest sale', 'text': '€56.97', 'estimated': True}, {'label': 'Receipts', 'value': 203}]))
    assert page.evaluate("()=>[texts('.rocoto-history-figures dt'),texts('.rocoto-history-figures .rocoto-history-tag'),texts('.rocoto-history-numeral .rocoto-history-tag')]") == [['Largest sale', 'Receipts'], ['Estimated'], ['Estimated']]
    assert errors == []
    page.close()


@pytest.mark.parametrize('size', SIZES, ids=lambda s: f'{s[0]}x{s[1]}')
def test_a_measure_that_needs_a_source_says_what_to_connect_and_opens_the_organization_agent(site, browser, size):
    page, errors = opened(browser, site, size)
    show(page, NEEDS)
    seen = page.evaluate("""()=>{const d=document.querySelector('dialog.rocoto-history'),box=d.getBoundingClientRect();return {state:d.dataset.state,statement:texts('.rocoto-history-statement')[0],numeral:d.querySelector('.rocoto-history-numeral'),
      tools:d.querySelector('.rocoto-history-tools').hidden,ghost:Boolean(d.querySelector('.rocoto-history-ghost .rocoto-ghost-path')?.getAttribute('d')),body:texts('.rocoto-history-connect p')[0],
      action:d.querySelector('[data-agent]').textContent,accent:getComputedStyle(d.querySelector('[data-agent]')).backgroundColor,text:d.innerText,name:d.getAttribute('aria-label'),
      fits:document.documentElement.scrollWidth<=document.documentElement.clientWidth&&box.left>=0&&box.right<=innerWidth&&box.bottom<=innerHeight}}""")
    assert seen['state'] == 'needs' and seen['statement'] == 'Needs till data' and len(seen['statement'].split()) <= 3
    assert seen['numeral'] is None and seen['tools'] and seen['ghost'] and seen['fits']
    assert seen['body'] == NEEDS['canvas']['summary'] and seen['action'] == 'Connect till data' and seen['name'] == 'Sales. Needs till data'
    assert seen['accent'] == page.evaluate("()=>{const p=document.createElement('i');p.style.color='var(--camera-accent)';document.body.append(p);const c=getComputedStyle(p).color;p.remove();return c;}")
    assert not BANNED.search(seen['text']) and not NEGATIVE.search(seen['text']) and '–' not in seen['text']
    page.locator('dialog [data-agent]').click()
    assert ['rocoto:open-organization-chat', {'purpose': 'organization', 'tracker': 'sales', 'needs': 'Needs till data', 'prompt': 'Connect till data for Sales at Vitosha Boulevard 85.'}] in page.evaluate('()=>calls')
    page.evaluate('()=>RocotoHomeHistory.close()')
    # without a record, the sheet says which source fills the measure
    show(page, {k: v for k, v in NEEDS.items() if k != 'canvas'})
    assert page.evaluate("()=>texts('.rocoto-history-connect p')[0]") == 'The Organization agent connects your till. Sales fills from the first receipt.'
    assert errors == []
    page.close()


def test_a_count_with_zero_events_shows_zero(site, browser):
    page, errors = opened(browser, site)
    show(page, {'key': 'put_back', 'label': 'Put back', 'value': 0, 'unit': '', 'state': 'zero', 'needs': None, 'series': [{'t': p['t'], 'value': 0} for p in WEEK], 'events': [], 'period': 'week'}, 'glassTile')
    seen = page.evaluate("()=>({numeral:texts('.rocoto-history-numeral strong')[0],first:texts('.rocoto-history-first p')[0],chart:document.querySelector('dialog .rocoto-line-stroke').getAttribute('d'),text:document.querySelector('dialog').innerText})")
    assert seen['numeral'] == '0' and seen['first'] == 'The first one lands here.' and seen['chart']
    assert not NEGATIVE.search(seen['text']) and '—' not in seen['text'] and '-' not in seen['numeral']
    assert errors == []
    page.close()


def test_the_period_toggle_asks_for_that_period_and_a_new_number_arrives_in_place(site, browser):
    page, errors = opened(browser, site)
    page.evaluate('other=>{window.other=other}', {'month': {**TILES['visitors'], 'value': 4810, 'text': '4,810', 'period': 'month', 'canvas': canvas('visitors', rows=COUNT_ROWS[:1])}})
    show(page, TILES['visitors'], 'glassTile')
    node = page.evaluate("()=>{window.kept=document.querySelector('dialog.rocoto-history');return texts('.rocoto-history-numeral strong')[0];}")
    assert node == '1,092'
    page.locator('dialog [data-period=month]').click()
    seen = page.evaluate("()=>({numeral:texts('.rocoto-history-numeral strong')[0],checked:[...document.querySelectorAll('dialog [data-period]')].map(b=>b.getAttribute('aria-checked')),rows:document.querySelectorAll('dialog .rocoto-history-rows>li').length,calls})")
    assert seen['numeral'] == '4,810' and seen['checked'] == ['false', 'true', 'false'] and seen['rows'] == 1
    assert ['tileFor', 'visitors', 'month'] in seen['calls'] and ['rocoto:tile-period', {'key': 'visitors', 'period': 'month'}] in seen['calls']
    # the arrow keys move the choice; the same sheet stays
    page.locator('dialog [data-period=month]').focus(); page.keyboard.press('ArrowRight')
    assert page.evaluate("()=>[document.activeElement.dataset.period,document.activeElement.getAttribute('aria-checked'),document.querySelector('dialog.rocoto-history')===kept]") == ['year', 'true', True]
    page.keyboard.press('Home')
    assert page.evaluate("()=>[document.activeElement.dataset.period,texts('.rocoto-history-numeral strong')[0]]") == ['week', '1,092']
    # a new number for the open tile lands in the same sheet; another tile's number leaves it alone
    answers = page.evaluate("tile=>[RocotoHomeHistory.update({...tile,value:1093,text:'1,093'}),RocotoHomeHistory.update({...tile,key:'sales'}),texts('.rocoto-history-numeral strong')[0],RocotoHomeHistory.key,document.querySelector('dialog.rocoto-history')===kept]", TILES['visitors'])
    assert answers == [True, False, '1,093', 'visitors', True]
    assert errors == []
    page.close()


def test_an_update_keeps_the_sheets_period_its_scroll_and_its_focus(site, browser):
    page, errors = opened(browser, site, (1280, 720))
    rows = [event(n, f'Visitor {n + 1}', 'Walks the tools aisle') for n in range(24)]
    month = {**TILES['visitors'], 'value': 4810, 'text': '4,810', 'period': 'month', 'canvas': canvas('visitors', rows=rows)}
    page.evaluate('other=>{window.other=other}', {'month': month})
    show(page, {**TILES['visitors'], 'canvas': canvas('visitors', rows=rows)}, 'glassTile')
    assert page.evaluate('RocotoHomeHistory.period') == 'week'
    page.locator('dialog [data-period=month]').click()
    page.evaluate("""()=>{document.querySelector('dialog .rocoto-history-made').scrollTop=240;document.querySelector('dialog [data-row="events:11"]').focus({preventScroll:true});}""")
    place = "()=>{const list=document.querySelector('dialog .rocoto-history-made');return [RocotoHomeHistory.period,texts('.rocoto-history-numeral strong')[0],list.scrollTop,document.activeElement.dataset.row||document.activeElement.tagName,texts('[data-period][aria-checked=true]')[0]];}"
    assert page.evaluate(place) == ['month', '4,810', 240, 'events:11', 'Monthly']
    # Home hands the open sheet its tile in the sheet's own period: the number changes and the reader's place holds.
    assert page.evaluate('tile=>RocotoHomeHistory.update(tile)', {**month, 'value': 4811, 'text': '4,811'}) is True
    assert page.evaluate(place) == ['month', '4,811', 240, 'events:11', 'Monthly']
    # The chart keeps the focus the same way.
    page.locator('dialog [role=slider]').focus()
    page.evaluate('tile=>RocotoHomeHistory.update(tile)', {**month, 'value': 4812, 'text': '4,812'})
    assert page.evaluate("()=>[document.activeElement.getAttribute('role'),texts('.rocoto-history-numeral strong')[0]]") == ['slider', '4,812']
    assert errors == []
    page.close()


def test_the_view_kind_comes_from_the_tile_before_its_key(site, browser):
    page, errors = opened(browser, site)
    # A company tracker whose key sounds like Queue time and whose presentation names a plain count.
    watch = tile('queue-watch', 'Queue watch', 3, 'alerts', kind='count', view='count', canvas=canvas('queue-watch', rows=COUNT_ROWS, total_rows=3, summary='The queue passed five people three times.'))
    show(page, watch)
    seen = page.evaluate("()=>({kind:document.querySelector('dialog.rocoto-history').dataset.kind,rows:document.querySelectorAll('dialog .rocoto-history-rows>li').length,bars:document.querySelectorAll('dialog [data-bars]').length,numeral:texts('.rocoto-history-numeral strong')[0]})")
    assert seen == {'kind': 'count', 'rows': 3, 'bars': 0, 'numeral': '3alerts'}
    page.evaluate('RocotoHomeHistory.close()'); page.wait_for_function('()=>!RocotoHomeHistory.isOpen')
    # The same key without a named view is read by its key, as a Core Tracker is.
    show(page, {k: v for k, v in watch.items() if k != 'view'})
    assert page.evaluate("()=>document.querySelector('dialog.rocoto-history').dataset.kind") == 'queue' and errors == []
    page.close()


def test_the_sheet_grows_from_its_tile_and_closing_returns_focus_to_the_tile(site, browser):
    page, errors = opened(browser, site, motion='no-preference')
    page.locator('#glassTile').focus()
    show(page, TILES['product_interactions'], 'glassTile')
    growing = page.evaluate("()=>({origin:document.getElementById('glassTile').hasAttribute('data-history-origin'),expanded:document.getElementById('glassTile').getAttribute('aria-expanded'),moving:document.querySelector('dialog.rocoto-history').getAnimations().length>0,modal:document.querySelector('dialog.rocoto-history').matches(':modal')})")
    assert growing == {'origin': True, 'expanded': 'true', 'moving': True, 'modal': True}
    page.wait_for_function("()=>document.querySelector('dialog.rocoto-history').getAnimations().length===0")
    # a row plays its clip range while the pointer is on it
    page.locator('dialog button.rocoto-history-row').first.hover()
    assert page.evaluate("()=>{const v=document.querySelector('dialog .rocoto-history-thumb video');return v&&[new URL(v.src).pathname,v.muted,v.getAttribute('aria-hidden')];}") == ['/media/clip-a.mp4', True, 'true']
    page.keyboard.press('Escape')
    page.wait_for_function('()=>!RocotoHomeHistory.isOpen')
    after = page.evaluate("()=>({focus:document.activeElement.id,origin:document.getElementById('glassTile').hasAttribute('data-history-origin'),expanded:document.getElementById('glassTile').getAttribute('aria-expanded'),left:document.querySelectorAll('dialog.rocoto-history').length,names:calls.map(c=>c[0])})")
    assert after == {'focus': 'glassTile', 'origin': False, 'expanded': 'false', 'left': 0, 'names': ['rocoto:tile-open', 'rocoto:tile-close']}
    # the scrim closes it too
    show(page, TILES['basket'])
    page.wait_for_function("()=>document.querySelector('dialog.rocoto-history').getAnimations().length===0")
    page.mouse.click(6, 6)
    page.wait_for_function('()=>!RocotoHomeHistory.isOpen')
    assert page.evaluate('()=>document.activeElement.id') == 'paperTile'
    assert errors == []
    page.close()


def test_the_keyboard_reaches_everything_and_reduced_motion_holds_still(site, browser):
    page, errors = opened(browser, site)
    show(page, TILES['product_interactions'], 'glassTile')
    assert page.evaluate("()=>document.getAnimations().length") == 0
    stops = []
    for _ in range(9):
        page.keyboard.press('Tab')
        stops.append(page.evaluate("()=>{const a=document.activeElement;return a.dataset.period?'period':a.classList.contains('rocoto-history-close')?'close':a.getAttribute('role')==='slider'?'chart':a.classList.contains('rocoto-history-row')?'row':a.closest('dialog.rocoto-history')?'sheet':a===document.body?'browser':'outside';}"))
    assert stops[:6] == ['period', 'close', 'chart', 'row', 'row', 'row'] and 'outside' not in stops
    # the chart reads out its points from the keyboard
    page.locator('dialog [role=slider]').focus(); page.keyboard.press('ArrowLeft')
    read = page.evaluate("()=>{const c=document.querySelector('dialog [role=slider]'),o=c.querySelector('.rocoto-chart-readout');return [o.hidden,o.querySelector('strong').textContent,o.querySelector('span').textContent,c.getAttribute('aria-valuetext'),c.getAttribute('aria-label')];}")
    assert read == [False, '240', 'Fri 9 Oct', '240, Fri 9 Oct', 'Product interactions over the period']
    # a hovered row keeps still under reduced motion
    page.locator('dialog button.rocoto-history-row').first.hover()
    assert page.evaluate("()=>document.querySelector('dialog .rocoto-history-thumb video')") is None
    page.keyboard.press('Escape')
    page.wait_for_function('()=>!RocotoHomeHistory.isOpen')
    assert page.evaluate('()=>document.activeElement.id') == 'glassTile' and errors == []
    page.close()
