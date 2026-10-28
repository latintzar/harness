"""The weather chip, its preview and the forecast sheet on the page; the map frame and its states.

The page reads this server's two routes. The answers here are the product's own service output over a
fixture upstream: this file opens no socket to an outside host.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import sys
import threading

import pytest

HERE = Path(__file__).parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).parent))
import location_context as lc
from test_location_context import NOON, SOFIA, upstream

WEB = HERE / 'verticals/retail/web/control-room'
ANSWER = lc.forecast(lc.checked(upstream()), SOFIA, NOON, NOON)
PLACE = {key: SOFIA[key] for key in ('location_id', 'name', 'address', 'latitude', 'longitude', 'timezone')}
PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link href="kit/rocoto-kit.css" rel="stylesheet"><link href="style.css" rel="stylesheet"><link href="weather-widget.css" rel="stylesheet">
<body class="showing-location"><header class="watch-header"><a class="brand" href="#">Rocoto</a><div id="headerWeather"></div></header>
<div id="locationRoot"><div id="mapBox" style="width:320px;height:200px"><img src="/api/control-room/map" alt=""></div></div>
<script src="weather-widget.js"></script></body></html>'''
POLICY = "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; object-src 'none'"
READY = "()=>document.querySelector('#headerWeather').dataset.state==='ready'"


def theme_css():
    tokens = json.loads((HERE / 'theme.json').read_text()); values = {**tokens['palette'], **tokens['typography']}
    values.update(ink='#201e1b', paper='#ffffff', accent='#ed5a2b', border='#ddd', muted='#777', surface='#f2f1ee', radius='0.75rem')
    values['radius-sm'] = '0.5rem'; values['radius-lg'] = '1.25rem'
    return (':root{' + ''.join(f'--{key}:{value};' for key, value in values.items()) + '}').encode()


def picture():
    from PIL import Image
    output = io.BytesIO(); Image.new('RGB', (512, 320), (236, 236, 232)).save(output, 'PNG')
    return output.getvalue()


@pytest.fixture
def site():
    """The page with the widget's own files; `routes` sets what each of our two routes answers."""
    routes = {'weather': 'ready', 'map': 'ready', 'asked': []}
    png = picture()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0].lstrip('/'); target = (WEB / path).resolve() if path else None
            status, kind = 200, 'application/json'
            if not path: body, kind = PAGE, 'text/html'
            elif path == 'theme.css': body, kind = theme_css(), 'text/css'
            elif path in ('api/control-room/weather', 'api/control-room/map'):
                name = path.rsplit('/', 1)[-1]; routes['asked'].append(self.path)
                if routes[name] == 'needs': status, body = 404, b'{"detail":"Add this shop\\u2019s address to see its weather and map."}'
                elif routes[name] == 'away': status, body = 503, json.dumps({'detail': f'{name}_returns_shortly'}).encode()
                elif name == 'weather': body = json.dumps(ANSWER).encode()
                else: body, kind = png, 'image/png'
            elif target.is_file() and target.is_relative_to(WEB.resolve()):
                body = target.read_bytes(); kind = {'.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2'}.get(target.suffix, 'application/octet-stream')
            else: status, body = 404, b'{}'
            self.send_response(status); self.send_header('Content-Type', kind); self.send_header('Cache-Control', 'no-store')
            if not path: self.send_header('Content-Security-Policy', POLICY)
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}/', routes
    server.shutdown(); server.server_close()


def launch(p):
    options = {'headless': True, 'args': ['--no-sandbox']}
    if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'): options['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
    return p.chromium.launch(**options)


def opened(browser, origin, size=(1600, 1000), wait=READY, **context):
    page = browser.new_page(viewport={'width': size[0], 'height': size[1]}, **context); page.problems, page.outside = [], []
    page.on('pageerror', lambda error: page.problems.append(str(error)))
    page.on('request', lambda request: None if request.url.startswith((origin, 'data:')) else page.outside.append(request.url))
    page.goto(origin)
    page.evaluate("place=>{window.weather=RocotoWeather.create({host:document.querySelector('#headerWeather'),place});}", PLACE)
    if wait: page.wait_for_function(wait)
    return page


def browser_test(work):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = launch(p)
        try: work(browser)
        finally: browser.close()


def test_chip_shows_temperature_condition_and_city_from_our_route(site):
    origin, routes = site

    def work(browser):
        page = opened(browser, origin)
        chip = page.locator('.weather-trigger')
        assert chip.inner_text().split('\n') == ['20°', 'Partly cloudy', 'Sofia']
        assert chip.get_attribute('aria-label') == 'Weather in Sofia: 20°, Partly cloudy. Open the forecast.'
        # One read of our weather route for this location; the page speaks to this server and no other host.
        assert [asked for asked in routes['asked'] if 'weather' in asked] == ['/api/control-room/weather?location_id=location-7f3a']
        assert page.outside == [] and page.problems == []
        # The widget speaks in the page's own face.
        assert page.evaluate("()=>[getComputedStyle(document.querySelector('.weather-trigger')).fontFamily,getComputedStyle(document.body).fontFamily]") == \
            [json.loads((HERE / 'theme.json').read_text())['typography']['font']] * 2
    browser_test(work)


def test_preview_leads_with_the_number_on_hover_and_on_keyboard_focus(site):
    origin, _ = site

    def work(browser):
        page = opened(browser, origin)
        assert page.locator('.weather-preview').is_hidden()
        page.hover('.weather-trigger'); page.wait_for_selector('.weather-preview:not([hidden])')
        # The card opens on its number. The first words in it are the temperature and the sky.
        read = page.evaluate("""()=>{const card=document.querySelector('.weather-preview');return {first:card.firstElementChild.className,
          lead:card.querySelector('.weather-figure>strong').textContent,sky:card.querySelector('.weather-figure em').textContent,
          line:!!card.querySelector('.weather-line svg path'),measures:[...card.querySelectorAll('.weather-stats li')].map(li=>[li.querySelector('strong').textContent,li.querySelector('span').textContent]),
          box:card.getBoundingClientRect().toJSON(),width:document.documentElement.clientWidth}}""")
        assert read['first'] == 'weather-figure' and read['lead'] == '20°' and read['sky'] == 'Partly cloudy' and read['line']
        assert read['measures'] == [['21°', 'High'], ['10°', 'Low'], ['0%', 'Rain']]
        assert read['box']['left'] >= 0 and read['box']['right'] <= read['width']
        page.mouse.move(10, 600); page.wait_for_selector('.weather-preview', state='hidden')
        page.keyboard.press('Tab'); page.keyboard.press('Tab')
        assert page.evaluate("document.activeElement.className") == 'weather-trigger'
        page.wait_for_selector('.weather-preview:not([hidden])')
        page.keyboard.press('Escape'); page.wait_for_selector('.weather-preview', state='hidden')
        assert page.problems == []
    browser_test(work)


def test_sheet_opens_from_the_chip_scrolls_hours_then_days_and_gives_focus_back(site):
    origin, _ = site

    def work(browser):
        page = opened(browser, origin)
        page.keyboard.press('Tab'); page.keyboard.press('Tab'); page.keyboard.press('Enter')
        page.wait_for_selector('dialog.weather-sheet[open]')
        read = page.evaluate("""()=>{const sheet=document.querySelector('dialog.weather-sheet'),title=sheet.querySelector('h2'),type=getComputedStyle(title);
          return {title:title.textContent,size:parseFloat(type.fontSize),weight:Number(type.fontWeight),case:type.textTransform,labelled:sheet.getAttribute('aria-labelledby')===title.id,
            figure:sheet.querySelector('.weather-sheet-figure>strong').textContent,side:sheet.querySelector('.weather-figure-side').innerText.split('\\n'),
            measures:[...sheet.querySelectorAll('.weather-stats li')].map(li=>li.innerText.replace(/\\s+/g,' ')),
            sections:[...sheet.querySelectorAll('.weather-section h3')].map(h=>h.textContent),
            hours:[...sheet.querySelectorAll('.weather-hour')].map(li=>[li.querySelector('time').textContent,li.querySelector('strong').textContent]),
            days:[...sheet.querySelectorAll('.weather-days li')].map(li=>[li.querySelector('.weather-day-full')?.textContent||li.querySelector('.weather-day-name').textContent,li.querySelector('.weather-day-low').textContent.replace('Low ',''),li.querySelector('.weather-day-high').textContent.replace('High ','')]),
            body:[sheet.querySelector('.weather-sheet-body').scrollHeight,sheet.querySelector('.weather-sheet-body').clientHeight],foot:sheet.querySelector('.weather-sheet-body').dataset.foot}}""")
        # A heading that names the sheet, at heading size; then the number.
        assert read['title'] == 'Weather in Sofia' and read['size'] >= 20 and read['weight'] >= 600 and read['case'] == 'none' and read['labelled']
        assert read['figure'] == '20°' and read['side'] == ['Partly cloudy', 'Feels like 19°']
        assert read['measures'] == ['21° High', '10° Low', '9 km/h Wind', '48% Humidity', '07:33 Sunrise', '18:52 Sunset']
        assert read['sections'] == ['Next 24 hours', 'Next 7 days']
        assert len(read['hours']) == 24 and read['hours'][0] == ['Now', '13°'] and read['hours'][1][0] == '15:00' and read['hours'][10][0] == '00:00'
        assert len(read['days']) == 7 and read['days'][0] == ['Today', '10°', '21°'] and read['days'][1][0] == 'Sunday'
        assert read['body'][0] > read['body'][1] and read['foot'] == 'more'
        # The hours step with their buttons and with the arrow keys; the sheet scrolls on to the days.
        strip = "document.querySelector('.weather-hours')"
        assert page.evaluate(f"{strip}.scrollLeft") == 0 and page.locator('.weather-step').first.is_disabled()
        page.locator('.weather-step').last.click(); page.wait_for_function(f"()=>{strip}.scrollLeft>200")
        page.locator('.weather-hours').focus(); page.keyboard.press('End')
        page.wait_for_function(f"()=>{strip}.dataset.edge==='end'"); assert page.locator('.weather-step').last.is_disabled()
        page.locator('.weather-sheet-body').focus(); page.keyboard.press('End')
        page.wait_for_function("()=>document.querySelector('.weather-sheet-body').dataset.foot==='end'")
        assert page.locator('.weather-days li').last.is_visible() and page.locator('.weather-credit a').get_attribute('rel') == 'noopener noreferrer'
        page.keyboard.press('Escape'); page.wait_for_selector('dialog.weather-sheet', state='hidden')
        assert page.evaluate("document.activeElement.className") == 'weather-trigger' and page.outside == [] and page.problems == []
    browser_test(work)


def test_a_forecast_that_is_away_or_needs_an_address_is_a_finished_state(site):
    origin, routes = site

    def work(browser):
        routes['weather'] = 'away'
        page = opened(browser, origin, wait="()=>document.querySelector('#headerWeather').dataset.state==='waiting'")
        assert page.locator('.weather-trigger').inner_text().split('\n') == ['Returns shortly', 'Sofia']
        page.click('.weather-trigger'); page.wait_for_selector('dialog.weather-sheet[open]')
        assert page.locator('.weather-sheet-figure').inner_text() == 'Returns shortly' and page.locator('.weather-sheet-body svg path').count() == 1
        # Refresh is the one act here; the forecast takes the sheet when the route answers again.
        routes['weather'] = 'ready'; page.click('.weather-retry'); page.wait_for_function(READY)
        assert page.locator('.weather-sheet-figure>strong').inner_text() == '20°' and page.locator('.weather-hour').count() == 24
        page.close()
        routes['weather'] = 'needs'
        page = opened(browser, origin, wait="()=>document.querySelector('#headerWeather').dataset.state==='needs'")
        assert page.locator('.weather-trigger').inner_text() == 'Needs shop address'
        page.click('.weather-trigger'); page.wait_for_selector('dialog.weather-sheet[open]')
        assert page.locator('.weather-sheet-figure').inner_text() == 'Needs shop address' and page.locator('.weather-retry').count() == 0
        assert page.problems == []
    browser_test(work)


def test_every_map_picture_shows_whole_in_its_own_size_and_colours_and_a_hidden_frame_asks_nothing(site):
    origin, routes = site
    look = "()=>{const frame=document.querySelector('#mapBox'),image=frame.querySelector('img'),style=getComputedStyle(image);return [style.objectFit,style.filter,getComputedStyle(frame).borderBottomRightRadius,image.getAttribute('src')||''];}"

    def work(browser):
        page = opened(browser, origin)
        page.evaluate("weather.destroy()")
        # A frame with no size yet (Home behind the camera view) asks no picture.
        page.evaluate("()=>{const box=document.querySelector('#mapBox');box.querySelector('img').removeAttribute('src');box.style.display='none';window.frame=RocotoWeather.map(box,{place:{location_id:'location-7f3a'}});}")
        routes['asked'].clear(); page.wait_for_timeout(900)
        assert [asked for asked in routes['asked'] if '/map' in asked] == [] and page.evaluate(look)[3] == ''
        # Shown, it asks once, in its own size: one picture point to a pixel.
        page.evaluate("document.querySelector('#mapBox').style.cssText='width:249px;height:210px'")
        page.wait_for_function("()=>frame.state()==='ready'")
        fit, tone, corner, source = page.evaluate(look)
        assert (fit, tone, corner) == ('fill', 'none', '8px')  # shown whole, as it arrived; the corner leaves the credit whole
        assert source.endswith('/api/control-room/map?width=256&height=224&location_id=location-7f3a')
        assert [asked for asked in routes['asked'] if '/map' in asked] == ['/api/control-room/map?width=256&height=224&location_id=location-7f3a']
        # A frame larger than 512 points takes the picture in its own proportions.
        page.evaluate("document.querySelector('#mapBox').style.cssText='width:680px;height:210px'")
        page.wait_for_function("()=>document.querySelector('#mapBox img').src.endsWith('/api/control-room/map?width=512&height=160&location_id=location-7f3a')")
        assert page.outside == [] and page.problems == []
    browser_test(work)


def test_map_frame_shows_the_picture_and_holds_a_designed_state_while_it_is_away(site):
    origin, routes = site
    mount = "()=>{window.frame=RocotoWeather.map(document.querySelector('#mapBox'),{place:{location_id:'location-7f3a',name:'Vitosha Boulevard 85'}});return frame.state();}"

    def work(browser):
        page = opened(browser, origin)
        page.evaluate("weather.destroy()")
        assert page.evaluate(mount) == 'loading'
        page.wait_for_function("()=>frame.state()==='ready'&&getComputedStyle(document.querySelector('#mapBox img')).opacity==='1'")
        read = page.evaluate("()=>{const box=document.querySelector('#mapBox'),image=box.querySelector('img');return [box.classList.contains('rocoto-map'),image.alt,getComputedStyle(image).opacity,new URL(image.src).pathname+new URL(image.src).search,!!box.querySelector('.rocoto-map-pin'),box.querySelector('p').textContent]}")
        # The picture is asked in the frame's own size, from our route, for this location.
        assert read == [True, 'Map of the streets around Vitosha Boulevard 85', '1', '/api/control-room/map?width=320&height=192&location_id=location-7f3a', True, '']
        assert page.evaluate("RocotoWeather.map(document.querySelector('#mapBox'))===frame")
        # A frame that changes its size takes the picture in its new shape, so the mark and credit in its corners stay whole.
        page.evaluate("document.querySelector('#mapBox').style.cssText='width:250px;height:210px'")
        page.wait_for_function("()=>document.querySelector('#mapBox img').src.endsWith('/api/control-room/map?width=256&height=224&location_id=location-7f3a')&&frame.state()==='ready'")
        for answer, state, words in (('away', 'waiting', 'Returns shortly'), ('needs', 'needs', 'Needs shop address')):
            routes['map'] = routes['weather'] = answer
            page.evaluate("frame.refresh()"); page.wait_for_function(f"()=>frame.state()==='{state}'&&getComputedStyle(document.querySelector('#mapBox img')).opacity==='0'")
            assert page.evaluate("document.querySelector('#mapBox p').textContent") == words
        assert page.outside == [] and page.problems == []
    browser_test(work)


@pytest.mark.parametrize('size', [(1600, 1000), (390, 844), (320, 640)])
def test_chip_preview_and_sheet_fit_their_window_and_rest_under_reduced_motion(site, size):
    origin, _ = site

    def work(browser):
        page = opened(browser, origin, size, reduced_motion='reduce')
        # The page's map takes its frame from the widget, as the product page does on show.
        page.evaluate("weather.mountMap()"); page.wait_for_function("()=>document.querySelector('#mapBox').dataset.mapState==='ready'")
        page.locator('.weather-trigger').focus(); page.evaluate("document.querySelector('.weather-trigger').dispatchEvent(new PointerEvent('pointerenter',{pointerType:'mouse'}))")
        page.wait_for_selector('.weather-preview:not([hidden])')
        inside = """root=>{const width=document.documentElement.clientWidth,out=[];
          for(const el of document.querySelectorAll(root+','+root+' *')){if(el.closest('.weather-hours-track,.rocoto-map-streets,svg,.visually-hidden'))continue;const box=el.getBoundingClientRect();if(box.width&&(box.right>width+.5||box.left<-.5))out.push(el.className);}
          return out}"""
        assert page.evaluate(inside, '#headerWeather') == []
        page.click('.weather-trigger'); page.wait_for_selector('dialog.weather-sheet[open]')
        fit = page.evaluate("""()=>{const d=document.documentElement,sheet=document.querySelector('dialog.weather-sheet'),box=sheet.getBoundingClientRect(),body=sheet.querySelector('.weather-sheet-body');
          const clipped=[...sheet.querySelectorAll('h2,h3,.weather-figure-side em,.weather-stats strong,.weather-stats span,.weather-day-name,.weather-day-low,.weather-day-high,.weather-day-rain')].filter(el=>el.scrollWidth>el.clientWidth+1).map(el=>el.className||el.tagName);
          return {page:d.scrollWidth<=d.clientWidth,sheet:box.left>=0&&box.right<=d.clientWidth&&box.top>=0&&box.bottom<=d.clientHeight,across:body.scrollWidth<=body.clientWidth,clipped,
            moving:sheet.getAnimations({subtree:true}).length+document.querySelector('#headerWeather').getAnimations({subtree:true}).length}}""")
        assert fit == {'page': True, 'sheet': True, 'across': True, 'clipped': [], 'moving': 0}
        assert page.evaluate(inside, 'dialog.weather-sheet') == [] and page.evaluate(inside, '#mapBox') == [] and page.problems == []
    browser_test(work)


def test_paint_and_type_come_from_the_theme():
    css, script = (WEB / 'weather-widget.css').read_text(), (WEB / 'weather-widget.js').read_text()
    tokens = json.loads((HERE / 'theme.json').read_text())['palette']
    # The page's own face, sentence case, proportional figures in one family.
    assert 'var(--font)' in css and '--camera-font' not in css and 'monospace' not in css and 'text-transform' not in css
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(', css)
    local = set(re.findall(r'(--weather-[a-z-]+)\s*:', css + script)) | set(re.findall(r"setProperty\('(--weather-[a-z-]+)'", script))
    used = set(re.findall(r'var\((--weather-[a-z-]+)', css)) | set(re.findall(r"token\('(--weather-[a-z-]+)'", script))
    assert used - local <= {f'--{name}' for name in tokens}, sorted(used - local - {f'--{name}' for name in tokens})
    # The weather tokens sit in one contiguous block of the theme, each one in use.
    names = list(tokens); block = [index for index, name in enumerate(names) if name.startswith('weather-')]
    assert block == list(range(block[0], block[-1] + 1))
    # The map tone is the one token this service reads: it bakes it into the tiles' picture (location_context.theme_tone).
    served = {f'--{name}' for name in re.findall(r"'(weather-[a-z-]+)'", (HERE / 'verticals/retail/location_context.py').read_text())}
    assert served == {'--weather-map-tone'} and '--weather-map-tone' not in used
    assert {f'--{names[index]}' for index in block} <= used | served
    # The page asks our two routes; the one outside address is the forecast's credit link.
    assert set(re.findall(r'https?://[^\s"\'<>`]+', script)) == {'https://open-meteo.com/'}
