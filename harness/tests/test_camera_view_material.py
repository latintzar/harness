"""The camera view paints its approved design under both readers.

The glass material of the activity panel, the 450 ms flash of a major action, the ambient backdrop taken
from the camera frame, the trail that warms with dwell, the arrival and chapter filmstrip, the observed
items and the movement graph are read back from the shipped page as the browser computed them.
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import math
import re
import shutil
import subprocess
import threading

import pytest

from test_maintained_player_view import HERE, RELEASED, browser_options, maintained_site  # noqa: F401

WEB = HERE / 'verticals/retail/web/control-room'
STYLESHEETS = ['style.css', 'control-room.css', 'weather-widget.css', 'kit/twelvelabs-material.css', 'kit/rocoto-kit.css']
# The benchmark's exact values, each carried by one named theme token.
EXACT = {'camera-glass': '#41403e', 'camera-glass-inner': '#484848', 'camera-glass-outer': '#545454',
         'camera-glow-warm': '#ffd3be', 'camera-glow-cool': '#bff3a4', 'camera-glass-ink': '#faf7f3',
         'camera-trail-rest': '#ffdc91', 'camera-trail-dwell': '#ff3625', 'camera-ambient-glow': '#ead9b5',
         'camera-accent': '#eb823e', 'camera-control': '#3d3d3b', 'camera-paper': '#f8f8f5'}
# A colour as the browser paints it: [red, green, blue, alpha] in 8-bit steps.
PAINT = """value=>{const c=document.createElement('canvas');c.width=c.height=1;const g=c.getContext('2d');
  g.fillStyle=value;g.fillRect(0,0,1,1);return [...g.getImageData(0,0,1,1).data];}"""
# The flash variable on every animation frame, beside the decoded playback time and the wall clock.
SAMPLE = """()=>{window.flashSamples=[];const wrap=document.querySelector('.video-wrap');
  const tick=()=>{flashSamples.push([decodedFrameTime,Number(wrap.style.getPropertyValue('--interaction-flash')||0),performance.now()]);
    window.flashFrame=requestAnimationFrame(tick);};tick();}"""


def theme_css():
    tokens = json.loads((HERE / 'theme.json').read_text()); values = {**tokens['palette'], **tokens['typography']}
    values.update(ink='#201e1b', paper='#ffffff', accent='#ed5a2b', border='#ddd', muted='#777', surface='#f2f1ee', radius='0.75rem')
    return (':root{' + ''.join(f'--{k}:{v};' for k, v in values.items()) + '}').encode()


@pytest.fixture
def bundle_site(tmp_path):
    """The bundle reader: the shipped page, a small recording, its tracker boxes and its saved events."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg: pytest.skip('Video decoder test needs ffmpeg')
    bundle = tmp_path / 'bundle'; (bundle / 'media').mkdir(parents=True)
    subprocess.run([ffmpeg, '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=8', '-t', '4',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(bundle / 'media/shop.mp4')], check=True)
    subprocess.run([ffmpeg, '-loglevel', 'error', '-i', str(bundle / 'media/shop.mp4'), '-frames:v', '1', str(bundle / 'media/shop.jpg')], check=True)
    carton = dict(person_id='Person P1', object_ref='carton', item_description='Test carton', person_crop_url='media/shop.jpg',
                  person_description='A shopper in a dark jacket stands at the shelf.')
    events = [dict(carton, event='pick_up', playback_start_s=1.0, playback_end_s=1.5, evidence='Person P1 lifts a carton from the shelf.'),
              dict(carton, event='examine_object', playback_start_s=1.5, playback_end_s=3.5, evidence='Person P1 turns the carton in both hands.')]
    clip = dict(id='shop', title='Shop aisle', duration_s=4, is_primary_trial=True, media='media/shop.mp4', poster='media/shop.jpg',
                models={'8b': {'windows': [{'offset_s': 0, 'events': events}]}})
    # Visitor P1 walks for one second and then stays; visitor P0 stands still throughout.
    frames = {str(n): [dict(id='P1', box=[20 + 10 * min(n, 8), 20, 140 + 10 * min(n, 8), 220]), dict(id='P0', box=[200, 60, 300, 230])] for n in range(33)}
    files = {'data.json': {'clips': [clip]}, 'tracks.json': {'shop': dict(fps=8, width=320, height=240, frames=frames)}, 'ux-data.json': {'clips': {}},
        # The profile a product bundle carries: labels and evidence kinds, emphasis left to the page's own rule.
        'monitoring-profile.json': {'schema_version': 1, 'event_types': {
            'pick_up': {'label': 'Pick up item', 'scope': 'person', 'evidence_kind': 'interaction'},
            'examine_object': {'label': 'Examine item', 'scope': 'person', 'evidence_kind': 'interaction'}}}}
    for name, value in files.items(): (bundle / name).write_text(json.dumps(value))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(WEB), **kwargs)
        def answer(self, body, kind):
            self.send_response(200); self.send_header('Content-Type', kind); self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            path = self.path.split('?')[0].split('#')[0].lstrip('/')
            try:
                if path == 'theme.css': return self.answer(theme_css(), 'text/css')
                if path == 'api/control-room/bootstrap':
                    return self.answer(json.dumps({'schema': 'rocoto.player-bootstrap.v1', 'read_authority': 'legacy', 'sources': []}).encode(), 'application/json')
                if path.startswith('api/'): self.send_response(404); self.end_headers(); return None
                if (bundle / path).is_file() and path:
                    return self.answer((bundle / path).read_bytes(), 'video/mp4' if path.endswith('.mp4') else 'image/jpeg' if path.endswith('.jpg') else 'application/json')
                return super().do_GET()
            except (BrokenPipeError, ConnectionResetError): return None
        def log_message(self, *_): pass
    class Host(ThreadingHTTPServer): request_queue_size = 64
    server = Host(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try: yield f'http://127.0.0.1:{server.server_port}'
    finally: server.shutdown(); server.server_close()


def open_view(browser, origin, reader, **context):
    page = browser.new_page(viewport={'width': 1600, 'height': 1000}, **context); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(origin + '/index.html')
    ready = "typeof maintainedState!=='undefined'&&maintainedState.status==='ready'" if reader == 'maintained' else "typeof selected!=='undefined'&&selected"
    page.wait_for_function(f"()=>{ready}&&video.readyState>=2&&decodedFrameTime!==null")
    # The bundle reader opens on its replay edge; these tests hold the playhead themselves.
    page.evaluate("leaveLiveReplay();video.pause();video.muted=true")
    page.wait_for_function("()=>video.paused&&!video.seeking&&decodedFrameTime!==null")
    return page, errors


def at(page, time, reader):
    page.evaluate('t=>seek(t)', time)
    page.wait_for_function("t=>!video.seeking&&decodedFrameTime!==null&&Math.abs(decodedFrameTime-t)<.07", arg=time)
    if reader == 'maintained':
        page.wait_for_function("()=>maintainedState.status==='ready'&&Math.abs(Number(maintainedState.value.playhead_s)-MaintainedPlayer.streamTime(selected,decodedFrameTime))<1e-6")


@pytest.fixture
def site(request):
    """One reader's host and the visitor its fixture puts at a pick-up after one second."""
    if request.param == 'maintained':
        origin, _ = request.getfixturevalue('maintained_site'); return origin, 'maintained', 'visit-a'
    return request.getfixturevalue('bundle_site'), 'bundle', 'P1'


BOTH = pytest.mark.parametrize('site', ['bundle', 'maintained'], indirect=True)


def test_every_camera_token_is_defined_and_carries_the_benchmark_value():
    palette = json.loads((HERE / 'theme.json').read_text())['palette']
    for name, value in EXACT.items(): assert palette.get(name, '').lower() == value, name
    used = set()
    for sheet in STYLESHEETS: used.update(re.findall(r'var\(--(camera-[a-z0-9-]+)', (WEB / sheet).read_text()))
    used.update(re.findall(r'var\(--(camera-[a-z0-9-]+)', (WEB / 'app.js').read_text()))
    used.discard('camera-ambient')  # the page sets this one itself, from the camera frame
    assert used and not sorted(name for name in used if name not in palette)
    # The material's source rule stays beside its stylesheet, and the tokens are that rule's own values.
    provenance = json.loads((WEB / 'kit/twelvelabs-material-provenance.json').read_text())
    rule = provenance['exact_rule']
    for part in ['backdrop-filter:blur(4px)', 'background-color:#41403e99', 'border-radius:20px',
                 'box-shadow:inset 8px 8px 15px 7px #48484899,inset -8px -8px 15px #54545499']: assert part in rule
    material = (WEB / 'kit/twelvelabs-material.css').read_text()
    for part in ['backdrop-filter: blur(4px)', 'var(--camera-glass) 60', 'var(--camera-glass-inner) 60', 'var(--camera-glass-outer) 60',
                 'inset 8px 8px 15px 7px', 'inset -8px -8px 15px']: assert part in material
    # The scrubber's movement graph is part of the view again.
    assert not re.search(r'\.duration-layers\s*\{[^}]*display\s*:\s*none', (WEB / 'style.css').read_text())


@BOTH
def test_activity_panel_carries_the_glass_material(site):
    playwright = pytest.importorskip('playwright.sync_api'); origin, reader, visitor = site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, reader)
            page.evaluate('id=>selectPerson(id)', visitor); at(page, 1.0, reader)
            page.wait_for_function("()=>!document.querySelector('#contextualActivity').hidden")
            seen = page.evaluate("""()=>{const panel=document.querySelector('#contextualActivity'),style=getComputedStyle(panel),probe=document.createElement('div');
              document.body.append(probe);const paint=(property,value)=>{probe.style.cssText='';probe.style.setProperty(property,value);return getComputedStyle(probe).getPropertyValue(property);};
              probe.className='twelvelabs-card-material';const radius=parseFloat(getComputedStyle(probe).borderTopLeftRadius);probe.className='';
              const result={classed:panel.classList.contains('twelvelabs-card-material'),blur:style.backdropFilter,radius,
                fill:[style.backgroundColor,paint('background-color','color-mix(in srgb, #41403e 60%, transparent)')],
                shadow:[style.boxShadow,paint('box-shadow','inset 8px 8px 15px 7px color-mix(in srgb, #484848 60%, transparent), inset -8px -8px 15px color-mix(in srgb, #545454 60%, transparent)')],
                ink:[style.color,paint('color','#faf7f3')],corner:[style.left,style.top],
                name:[panel.querySelector('strong').innerText,getComputedStyle(panel.querySelector('strong')).fontWeight],
                description:panel.querySelector('.contextual-copy>span')?.innerText||''};probe.remove();return result;}""")
            assert seen['classed'] and seen['blur'] == 'blur(4px)' and abs(seen['radius'] - 20) < .01
            assert seen['fill'][0] == seen['fill'][1] and seen['shadow'][0] == seen['shadow'][1]
            assert seen['ink'][0] == seen['ink'][1]
            # Top-left over the footage: the bold action name, then what was handled.
            assert seen['corner'] == ['15px', '15px'] and seen['name'] == ['Pick up item', '600'] and seen['description'] == 'Test carton'
            assert errors == []
        finally: browser.close()


def test_a_sustained_action_shows_its_photo_and_a_running_timer(bundle_site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, bundle_site, 'bundle')
            page.evaluate("selectPerson('P1')"); at(page, 2.0, 'bundle')
            panel = page.locator('#contextualActivity')
            assert panel.locator('strong').inner_text() == 'Examining item' and panel.locator('time').inner_text() == '0.5s'
            assert panel.locator('.contextual-event-photo img').get_attribute('src') == 'media/shop.jpg'
            at(page, 3.0, 'bundle'); assert panel.locator('time').inner_text() == '1.5s'
            # The panel is temporary: it leaves when the action ends.
            at(page, 3.75, 'bundle'); assert panel.is_hidden()
            assert errors == []
        finally: browser.close()


def play_through_the_pick_up(page):
    page.evaluate(SAMPLE)
    page.evaluate("video.playbackRate=1;video.play()")
    page.wait_for_function("()=>decodedFrameTime>2.9||video.ended", timeout=20000)
    page.evaluate("video.pause();cancelAnimationFrame(window.flashFrame)")
    return page.evaluate('flashSamples')


def one_brief_flash(samples, reader):
    """What a playback pass through the pick-up shows, as the first shortfall it has or None."""
    lit = [sample for sample in samples if sample[1] > 0]
    if not lit: return 'the pick-up raises the flash'
    # Nothing before the action starts; the rise follows the start (the maintained reader learns of it within a read).
    if any(value != 0 for time, value, _ in samples if time is not None and time < 1.0): return 'the flash waits for the action'
    if not 1.0 <= lit[0][0] < (2.0 if reader == 'maintained' else 1.45): return f'the flash rises at the start, not at {lit[0][0]}'
    if max(value for _, value, _ in lit) <= .7 or not all(0 < value <= 1 for _, value, _ in lit): return 'the flash peaks once, inside 0..1'
    # One brief pulse: back to zero within 600 ms on the playback clock and on the wall clock, and it stays there.
    after = [sample for sample in samples if sample[2] > lit[-1][2]]
    if not after or any(value != 0 for _, value, _ in after): return 'the flash ends and stays ended'
    if after[0][0] - lit[0][0] > .6: return f'{after[0][0] - lit[0][0]:.2f} s of playback from rise to zero'
    if after[0][2] - lit[0][2] > 600: return f'{after[0][2] - lit[0][2]:.0f} ms of wall clock from rise to zero'
    return None


@BOTH
def test_a_major_action_flashes_at_its_start_and_is_gone_within_600_ms(site):
    playwright = pytest.importorskip('playwright.sync_api'); origin, reader, visitor = site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, reader, reduced_motion='no-preference')
            page.evaluate('id=>selectPerson(id)', visitor)
            # The measurement is real time at normal speed. A pass in which the machine starved the page of frames
            # or of its read is run again; the claim is about a pass the page was given time for.
            for attempt in range(4):
                at(page, .25, reader); samples = play_through_the_pick_up(page); shortfall = one_brief_flash(samples, reader)
                starved = max((b[2] - a[2] for a, b in zip(samples, samples[1:])), default=0) > 150
                if shortfall is None or not (starved or (reader == 'maintained' and shortfall.startswith('the pick-up raises'))): break
            assert shortfall is None, shortfall
            assert page.evaluate("document.querySelector('.video-wrap').style.getPropertyValue('--interaction-flash')") == '0'
            # A seek that lands inside the action's first 450 ms paints the flash of that moment, held while paused.
            at(page, 1.25, reader)
            held, frame = page.evaluate("[Number(document.querySelector('.video-wrap').style.getPropertyValue('--interaction-flash')),decodedFrameTime]")
            assert held == pytest.approx(math.sin(math.pi * (frame - 1.0) / .45), abs=1e-6) and held > .5
            at(page, 2.5, reader)
            assert page.evaluate("document.querySelector('.video-wrap').style.getPropertyValue('--interaction-flash')") == '0'
            # The slim inset edge is the two glow tokens, painted by that variable.
            edge = page.evaluate("""()=>{const wrap=document.querySelector('.video-wrap');wrap.style.setProperty('--interaction-flash','.5');
              const style=getComputedStyle(wrap,'::after'),probe=document.createElement('div');document.body.append(probe);
              probe.style.boxShadow='#ffd3be -20px -20px 60px 0 inset, #bff3a4 20px 20px 60px 0 inset';
              const result=[style.boxShadow,getComputedStyle(probe).boxShadow,style.opacity,style.display];probe.remove();wrap.style.setProperty('--interaction-flash','0');return result;}""")
            assert edge[0] == edge[1] and edge[2] == '0.5' and edge[3] != 'none'
            assert errors == []
        finally: browser.close()


@BOTH
def test_reduced_motion_keeps_the_flash_at_zero(site):
    playwright = pytest.importorskip('playwright.sync_api'); origin, reader, visitor = site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, reader, reduced_motion='reduce')
            page.evaluate('id=>selectPerson(id)', visitor); at(page, .25, reader)
            samples = play_through_the_pick_up(page)
            assert len(samples) > 10 and all(value == 0 for _, value, _ in samples)
            assert page.evaluate("getComputedStyle(document.querySelector('.video-wrap'),'::after').display") == 'none'
            # The action itself is still shown: its chapter is in the filmstrip.
            assert page.locator('#personJourney [data-journey]').count() >= 1
            assert errors == []
        finally: browser.close()


def ambient(page):
    return page.evaluate("""()=>{const footage=document.querySelector('.footage');
      return [getComputedStyle(footage).getPropertyValue('--camera-ambient').trim(),getComputedStyle(footage,'::before').backgroundImage];}""")


def test_ambient_backdrop_is_the_recording_poster_under_the_bundle_reader(bundle_site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, bundle_site, 'bundle')
            variable, painted = ambient(page)
            assert variable == 'url("media/shop.jpg")' and painted.endswith('/media/shop.jpg")')
            assert errors == []
        finally: browser.close()


def test_ambient_backdrop_is_taken_from_the_decoded_camera_frame(maintained_site):  # noqa: F811
    playwright = pytest.importorskip('playwright.sync_api'); origin, state = maintained_site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, 'maintained')
            # The maintained host publishes this recording without a poster: the page samples its own decoded frame.
            page.wait_for_function("()=>getComputedStyle(document.querySelector('.footage')).getPropertyValue('--camera-ambient').includes('data:image/jpeg')")
            variable, painted = ambient(page)
            assert variable.startswith('url("data:image/jpeg;base64,') and painted == variable
            frame = page.evaluate("""async url=>{const image=new Image();image.src=url.slice(5,-2);await image.decode();
              const c=document.createElement('canvas');c.width=image.naturalWidth;c.height=image.naturalHeight;const g=c.getContext('2d');g.drawImage(image,0,0);
              const data=g.getImageData(0,0,c.width,c.height).data;let low=255,high=0;for(let i=0;i<data.length;i+=4){low=Math.min(low,data[i]);high=Math.max(high,data[i]);}
              return {width:c.width,height:c.height,spread:high-low};}""", variable)
            # A small copy of the 320 x 240 picture, with the picture's own contrast in it.
            assert frame['width'] / frame['height'] == pytest.approx(320 / 240, abs=.01) and frame['width'] <= 320 and frame['spread'] > 100
            # The same frame stands in for the recording's thumbnails: camera tray and visitor chapters.
            page.evaluate("selectPerson('visit-a')")
            assert 'data:image/jpeg' in page.locator('#personJourney .chapter-thumb').first.get_attribute('style')
            assert page.locator('#clipGrid .clip img').first.get_attribute('src').startswith('data:image/jpeg')
            # Revoked access takes the backdrop and the thumbnails with the picture; returning access brings them back.
            state['revoked'] = True; page.evaluate('seek(1.75)')
            page.wait_for_function("()=>maintainedState.status==='unavailable'"); page.wait_for_function(RELEASED)
            assert ambient(page) == ['none', 'none'] and page.locator('#clipGrid .clip img').count() == 0
            assert 'data:image' not in page.content()
            state['revoked'] = False
            page.wait_for_function("()=>maintainedState.status==='ready'&&decodedFrameTime!==null&&getComputedStyle(document.querySelector('.footage')).getPropertyValue('--camera-ambient').includes('data:image/jpeg')")
            assert errors == []
        finally: browser.close()


# A picture the page made itself: its width and height once decoded.
DECODED = """async source=>{const image=new Image();image.src=source;await image.decode();return [image.naturalWidth,image.naturalHeight];}"""


def test_maintained_visitors_and_actions_are_pictured_by_the_tracked_box(maintained_site):  # noqa: F811
    playwright = pytest.importorskip('playwright.sync_api'); origin, state = maintained_site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, 'maintained')
            # The host publishes visitors without crops: each is pictured by its own box on the decoded frame.
            page.evaluate('enterRoomMode()'); at(page, .5, 'maintained')
            page.wait_for_function("()=>document.querySelectorAll('#visitorList img.visitor-photo').length===2")
            for visit in ['visit-a', 'visit-b']:
                source = page.locator(f'#visitorList [data-visitor-id="{visit}"] img.visitor-photo').get_attribute('src')
                assert source.startswith('data:image/jpeg;base64,')
                width, height = page.evaluate(DECODED, source)
                # Both fixture boxes are 120 x 200 of the 320 x 240 picture: full height, widened to the tile's 62 : 80.
                assert width / height == pytest.approx(62 / 80, abs=.02) and height <= 240
            assert page.locator('#personJourney [data-room-person] img.chapter-thumb').count() == 2
            # The pick-up is pictured on the frame of its start: filmstrip, top-left panel and observed item share it.
            page.evaluate("selectPerson('visit-a')"); at(page, 1.0, 'maintained')
            page.wait_for_function("()=>document.querySelector('#personJourney [data-journey] img.chapter-thumb')")
            crop = page.locator('#personJourney [data-journey] img.chapter-thumb').get_attribute('src')
            assert crop.startswith('data:image/jpeg;base64,') and page.evaluate(DECODED, crop)[0] / page.evaluate(DECODED, crop)[1] == pytest.approx(62 / 80, abs=.02)
            page.wait_for_function("()=>document.querySelector('#contextualActivity .contextual-event-photo img')")
            assert page.locator('#contextualActivity .contextual-event-photo img').get_attribute('src') == crop
            assert page.locator('#visitorItems .visitor-product img').get_attribute('src') == crop
            # An action first reached more than a second after its start keeps the recording's frame as its thumbnail.
            page.evaluate("setClip(data.clips[1].id);seek(3)")
            page.wait_for_function("()=>maintainedState.status==='ready'&&Number(maintainedState.value.playhead_s)>=17&&document.querySelectorAll('#personJourney [data-journey]').length===2")
            assert page.locator('#personJourney [data-journey] img.chapter-thumb').count() == 1
            # Revoked access takes every picture the page made.
            state['revoked'] = True; page.evaluate('seek(3.5)')
            page.wait_for_function("()=>maintainedState.status==='unavailable'"); page.wait_for_function(RELEASED)
            assert 'data:image' not in page.content()
            assert page.evaluate('[sampledFrames.size,portraits.size,eventCrops.size]') == [0, 0, 0]
            assert errors == []
        finally: browser.close()


def test_camera_view_inside_the_product_frame_keeps_its_spacing_weather_and_named_way_back(maintained_site):  # noqa: F811
    playwright = pytest.importorskip('playwright.sync_api'); origin, state = maintained_site
    from test_maintained_player_view import maintained_bootstrap
    point = dict(latitude=42.6842677, longitude=23.3162691, timezone='Europe/Sofia')
    header = """()=>{const back=document.querySelector('#locationBack'),main=getComputedStyle(document.querySelector('main')),wrap=document.querySelector('.video-wrap').getBoundingClientRect();
      return {label:getComputedStyle(back,'::after').content,brand:getComputedStyle(document.querySelector('.brand')).display,
        weather:document.querySelector('#headerWeather').getClientRects().length>0&&document.querySelector('.weather-current').innerText.includes('20'),
        padding:[main.paddingLeft,main.paddingRight,main.paddingBottom],inset:wrap.left,text:document.querySelector('.watch-header').innerText};}"""
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            def frame(bootstrap):
                state['bootstrap'] = bootstrap
                page = browser.new_page(viewport={'width': 1096, 'height': 820}); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                # The chip reads this service's own weather route.
                from test_weather_view import ANSWER
                page.route('**/api/control-room/weather*', lambda route: route.fulfill(json=ANSWER))
                page.goto(origin + '/index.html?embedded=1')
                page.wait_for_function("()=>typeof maintainedState!=='undefined'&&maintainedState.status==='ready'&&decodedFrameTime!==null")
                return page, errors
            # A location the host names: the way back reads that name and the weather chip keeps its place.
            named = dict(maintained_bootstrap(), location=dict(id='shop', name='Vitosha Boulevard 85', address='85 Vitosha Boulevard, Sofia',
                coordinates=dict(point, source='https://example.test/place'), weather=dict(point, name='Vitosha Boulevard 85')))
            page, errors = frame((200, named))
            page.wait_for_function("()=>document.querySelector('.weather-current')?.innerText.includes('20')")
            seen = page.evaluate(header)
            assert seen['label'] == '"Vitosha Boulevard 85"' and seen['brand'] == 'none' and seen['weather']
            # The footage keeps the page's own margins inside the frame's rounded card.
            assert seen['padding'] == ['18px', '18px', '18px'] and seen['inset'] == 18
            assert errors == []; page.close()
            # A host that leaves the location unnamed: the way back is its arrow alone.
            page, errors = frame(None)
            seen = page.evaluate(header)
            assert seen['label'] == 'none' and seen['text'].strip() == '' and seen['brand'] == 'none'
            assert errors == []
        finally: browser.close()


def trail(page):
    return page.evaluate("""()=>{const paint=%s,path=[...document.querySelectorAll('#trackOverlay .dwell-trail')].at(-1),point=document.querySelector('#trackOverlay .dwell-point');
      if(!path)return null;const style=getComputedStyle(path);
      return {stroke:paint(style.stroke),width:parseFloat(style.strokeWidth),glow:parseFloat(/drop-shadow\\(.*?([\\d.]+)px\\)$/.exec(style.filter)?.[1]??
        /([\\d.]+)px\\)$/.exec(style.filter)?.[1]??0),filter:style.filter,point:point?paint(getComputedStyle(point).fill):null};}""" % PAINT)


def test_trail_warms_from_pale_yellow_to_red_and_glows_stronger_on_dwell(maintained_site):  # noqa: F811
    playwright = pytest.importorskip('playwright.sync_api'); origin, _ = maintained_site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, 'maintained')
            page.evaluate("selectPerson('visit-a')")
            # The fixture's visitor stands in one place: the longer the stay, the warmer the trail.
            at(page, .5, 'maintained'); early = trail(page)
            at(page, 3.5, 'maintained'); late = trail(page)
            assert early and late and early['stroke'] != late['stroke']
            rest, dwell = page.evaluate(PAINT, '#ffdc91'), page.evaluate(PAINT, '#ff3625')
            # A quarter of the way from the resting colour, then the dwell colour itself.
            quarter = [round(rest[i] + (dwell[i] - rest[i]) * .25) for i in range(3)]
            assert all(abs(early['stroke'][i] - quarter[i]) <= 1 for i in range(3)) and late['stroke'] == dwell
            assert late['point'] == dwell and late['width'] > early['width'] and late['glow'] > early['glow'] > 0
            # Both ends of the ramp are theme tokens: a re-themed dwell colour is the colour painted.
            page.evaluate("document.documentElement.style.setProperty('--camera-trail-dwell','#0044ff');updateOverlay()")
            assert trail(page)['stroke'] == [0, 68, 255, 255]
            assert errors == []
        finally: browser.close()


FILMSTRIP = """()=>{const strip=document.querySelector('#personJourney'),box=strip.getBoundingClientRect();
  return {height:box.height,hidden:strip.hidden,rows:[...strip.querySelectorAll('.chapter-row')].map(row=>({person:row.dataset.roomPerson||null,
    title:row.querySelector('strong').innerText,description:row.querySelector('.chapter-description').innerText,time:row.querySelector('time').innerText,
    pictured:!!row.querySelector('img.chapter-thumb')||/url\\(/.test(row.querySelector('.chapter-thumb').getAttribute('style')||'')}))};}"""
# The movement graph as painted over the scrubber: its line, its place and how far along the recording it reaches.
GRAPH = """()=>{const host=document.querySelector('#durationLayers'),line=host.querySelector('.motion-line'),seek=document.querySelector('#mainSeek').getBoundingClientRect();
  if(!line)return null;const box=line.getBoundingClientRect(),area=host.getBoundingClientRect();
  return {display:getComputedStyle(host).display,above:area.bottom<=seek.bottom&&area.height>20,reach:(box.right-seek.left)/seek.width,length:line.getTotalLength()};}"""


def test_bundle_reader_paints_arrivals_observed_items_and_the_movement_graph(bundle_site):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, bundle_site, 'bundle')
            page.evaluate('enterRoomMode()'); at(page, 2.0, 'bundle')
            # With nobody selected the filmstrip under the controls shows who arrived, in order.
            strip = page.evaluate(FILMSTRIP)
            assert not strip['hidden'] and strip['height'] > 120
            assert [(row['person'], row['title'], row['time']) for row in strip['rows']] == [('P0', 'First observed', '0:00'), ('P1', 'First observed', '0:00')]
            assert all(row['pictured'] for row in strip['rows'])
            page.locator('#personJourney [data-room-person="P1"]').click()
            page.wait_for_function("()=>selectedPerson==='P1'&&!video.seeking&&decodedFrameTime!==null")
            at(page, 2.0, 'bundle')
            assert [row['title'] for row in page.evaluate(FILMSTRIP)['rows']] == ['First observed', 'Pick up item', 'Examine item']
            # The session panel: stats, the visitor's description, the item observed with what was done to it.
            summary = page.locator('#personSummary')
            assert summary.locator('.visitor-session-stats').inner_text().split() == ['4.0s', 'Observed', '2', 'Interactions']
            assert summary.locator('.visitor-description').inner_text() == 'A shopper in a dark jacket stands at the shelf.'
            assert page.locator('#visitorItems .visitor-product').count() == 1
            assert page.locator('#visitorItems .visitor-product strong').inner_text() == 'Test carton'
            assert page.locator('#visitorItems .visitor-product p').inner_text() == 'Pick up item · Examine item'
            assert page.locator('#visitorItems .visitor-product img').get_attribute('src') == 'media/shop.jpg'
            # A saved recording holds all of its events: a visitor with none reads as a number.
            page.evaluate("selectPerson('P0')")
            assert summary.locator('.visitor-session-stats').inner_text().split() == ['4.0s', 'Observed', '0', 'Interactions']
            assert page.locator('#personHeading').inner_text() == 'Visitor 0' and page.locator('#visitorItems .visitor-product').count() == 0
            page.evaluate("selectPerson('P1')"); assert page.locator('#personHeading').inner_text() == 'Visitor 1'
            # The movement graph sits over the scrubber for the whole recording, with a tick at each chapter.
            graph = page.evaluate(GRAPH)
            assert graph and graph['display'] != 'none' and graph['above'] and graph['length'] > 0 and graph['reach'] > .95
            assert page.locator('#seekChapters i').count() == 2
            assert errors == []
        finally: browser.close()


def test_maintained_reader_paints_the_same_filmstrip_items_and_graph(maintained_site):  # noqa: F811
    playwright = pytest.importorskip('playwright.sync_api'); origin, state = maintained_site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, 'maintained')
            page.evaluate('enterRoomMode()'); at(page, 1.5, 'maintained')
            strip = page.evaluate(FILMSTRIP)
            assert not strip['hidden'] and strip['height'] > 120
            assert [(row['person'], row['title'], row['description']) for row in strip['rows']] == [
                ('visit-a', 'First tracked', 'Visitor 1'), ('visit-b', 'First tracked', 'Visitor 2')]
            page.locator('#personJourney [data-room-person="visit-a"]').click()
            page.wait_for_function("()=>selectedPerson==='visit-a'"); at(page, 1.5, 'maintained')
            # The pick-up read at this playhead is the observed item; the basket estimate stays beside it.
            summary = page.locator('#personSummary')
            assert page.locator('#visitorItems .visitor-product strong').inner_text() == 'Test carton'
            assert page.locator('#visitorItems .visitor-product p').inner_text() == 'Pick up item'
            assert 'Test carton' in page.locator('#visitBasket').inner_text()
            # The movement graph reaches the playhead and stops there: later tracking stays unread.
            graph = page.evaluate(GRAPH)
            assert graph and graph['display'] != 'none' and graph['above']
            assert graph['reach'] == pytest.approx(1.5 / 4, abs=.04)
            at(page, 3.0, 'maintained'); assert page.evaluate(GRAPH)['reach'] == pytest.approx(3 / 4, abs=.04)
            # A rewind takes the later item and the later part of the graph with it.
            at(page, .5, 'maintained')
            assert page.locator('#visitorItems .visitor-product').count() == 0 and page.evaluate(GRAPH)['reach'] == pytest.approx(.5 / 4, abs=.04)
            assert not [r for r in state['requests'] if r.split('?')[0] in ['/data.json', '/tracks.json', '/ux-data.json', '/monitoring-profile.json']]
            assert errors == []
        finally: browser.close()


@BOTH
def test_camera_view_reads_in_the_kit_inter_with_tabular_figures(site):
    playwright = pytest.importorskip('playwright.sync_api'); origin, reader, visitor = site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(**browser_options())
        try:
            page, errors = open_view(browser, origin, reader)
            page.evaluate('id=>selectPerson(id)', visitor); at(page, 1.0, reader)
            page.wait_for_function("()=>document.fonts.status==='loaded'")
            families = page.evaluate("""()=>{const seen={};for(const node of document.querySelectorAll('.watch-header,.watch-header *,main,main *')){
              if(!node.getClientRects().length||node.closest('svg')&&node.tagName!=='text')continue;const family=getComputedStyle(node).fontFamily;seen[family]=(seen[family]||0)+1;}return seen;}""")
            assert families and all(family.startswith('Inter') for family in families), families
            assert not [family for family in families if 'mono' in family.lower()]
            # The kit's own font files are the ones that paint.
            assert page.evaluate("['400','500','600','700'].every(weight=>document.fonts.check(weight+' 14px Inter'))")
            assert page.evaluate("[...document.fonts].filter(face=>face.family.replaceAll('\"','')==='Inter'&&face.status==='loaded').length") >= 2
            assert page.evaluate("getComputedStyle(document.querySelector('#playerTime')).fontVariantNumeric") == 'tabular-nums'
            assert errors == []
        finally: browser.close()
