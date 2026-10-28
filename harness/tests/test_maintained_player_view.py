"""Exercise the shipped player against the maintained wire, including real decoding."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time

import pytest

HERE = Path(__file__).parents[1]
LEGACY_READS = ['/data.json','/tracks.json','/ux-data.json','/monitoring-profile.json','/api/control-room/visit-basket']


def test_maintained_player_clock_lifecycle():
    subprocess.run(['node', str(Path(__file__).with_name('maintained_player.cjs'))], check=True)


def snapshot(at):
    artifacts = [dict(source_asset_id=f'file{i}', mapping_id=f'm{i}', source_sha256=str(i)*64,
                      source_window={'start_s':'0','end_s':'4'},
                      stream_window={'start_s':str(10+4*i),'end_s':str(14+4*i)}) for i in range(2)]
    chapters = []
    for start, kind, asset in [(11,'product.pick_up',0), (15,'product.put_back',1)]:
        if at >= start:
            chapters.append(dict(event_id=kind, event_revision=1, type=kind, start_s=str(start),
                end_s=str(start), description='Fixture item interaction', product_description='Test carton',
                continuity=dict(source_asset_id=f'file{asset}', mapping_id=f'm{asset}', epoch_id='epoch',
                source_window={'start_s':'1','end_s':'1'},stream_window={'start_s':str(start),'end_s':str(start)})))
    visitors=[]
    for visit, tracker, birth in [('visit-a','0','birth-a'),('visit-b','P0','birth-b')]:
        visitors.append(dict(visit_subject_id=visit, subject_revision=1, tracker_id=tracker,
            birth_id=birth, first_observed=None,
            first_reported=dict(source_asset_id='file0',source_s='0',stream_s='10'),
            tracking_state_at_playhead='reported',
            segments=[dict(mapping_id=a['mapping_id'],source_asset_id=a['source_asset_id'],
                source_sha256=a['source_sha256'],state='reported',
                source_window={'start_s':'0','end_s':str(min(4,at-float(a['stream_window']['start_s'])))},
                stream_window={'start_s':a['stream_window']['start_s'],'end_s':str(min(at,float(a['stream_window']['end_s'])))})
                for a in artifacts if float(a['stream_window']['start_s'])<at]))
    basket=dict(items=[dict(item_id='item-a',description='Test carton',quantity=1,
                quantity_basis='estimated',identity_basis='vision_estimate',state='held')]
                if 11<=at<15 else [],uncertain_items=[],subtotal=None,valuation_status='awaiting_prices')
    return dict(schema='rocoto.realtime-snapshot.v1', status='ephemeral', source_id='camera', epoch_id='epoch',
        clock='stream', playhead_s=str(at), snapshot=dict(visits=[dict(visit_id='visit-a',chapters=chapters,basket=basket),
        dict(visit_id='visit-b',chapters=[],basket=dict(items=[],uncertain_items=[]))]),
        playback=dict(schema='rocoto.epoch-playback.v1', source_id='camera',epoch_id='epoch',
        tracker_namespace='run',clock='stream',playhead_s=str(at),visitors=visitors,
        artifacts=[a for a in artifacts if float(a['stream_window']['start_s'])<=at]))


def maintained_bootstrap():
    return dict(schema='rocoto.player-bootstrap.v1',read_authority='maintained',sources=[
        dict(source_id='camera',epoch_id='epoch',label='Test camera',artifacts=[dict(**a,
            media_url=f'/media/{i}',geometry_url=f'/geometry/{i}',
            geometry_binding={'source_sha256':str(i)*64,'tracker_namespace':'run'},
            width=320,height=240,fps=8,duration_s=4) for i,a in enumerate(snapshot(18)['playback']['artifacts'])])])


@pytest.fixture
def maintained_site(tmp_path):
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:pytest.skip('Video decoder test needs ffmpeg')
    media=tmp_path/'camera.mp4'
    subprocess.run([ffmpeg,'-loglevel','error','-f','lavfi','-i','testsrc2=size=320x240:rate=8',
        '-t','4','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(media)],check=True)
    # bootstrap: None answers as the maintained host; (status, body) or 'interrupted' replaces it.
    # hold_from: snapshot reads at or after this stream second wait for their own release.
    state={'requests':[], 'revoked':False, 'bootstrap':None, 'interrupted':False,
           'hold_from':None, 'held':[], 'answered':[]}
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(HERE/'verticals/retail/web/control-room'),**kwargs)
        def send(self,value,status=200,kind='application/json',headers=()):
            body=json.dumps(value).encode() if kind=='application/json' else value
            try:
                self.send_response(status);self.send_header('Content-Type',kind)
                for key,item in headers:self.send_header(key,item)
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def interrupt(self,value):
            # Headers promise a body that the connection never completes.
            body=json.dumps(value).encode()
            try:
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers()
                self.wfile.write(body[:len(body)//2]);self.wfile.flush();self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        def do_GET(self):
            path=urlparse(self.path).path;state['requests'].append(self.path)
            if path=='/api/control-room/bootstrap':
                if state['bootstrap']=='interrupted':return self.interrupt(maintained_bootstrap())
                if state['bootstrap']:return self.send(state['bootstrap'][1],state['bootstrap'][0])
                return self.send(maintained_bootstrap())
            if path.startswith('/api/realtime/'):
                at=float(parse_qs(urlparse(self.path).query)['at_s'][0])
                if state['hold_from'] is not None and at>=state['hold_from']:
                    ticket={'at':at,'release':threading.Event()};state['held'].append(ticket);ticket['release'].wait(20)
                try:
                    if state['revoked']:return self.send({},403)
                    if state['interrupted']:return self.interrupt(snapshot(at))
                    return self.send(snapshot(at))
                finally:state['answered'].append(at)
            if path.startswith('/geometry/'):
                i=int(path.rsplit('/',1)[1]);return self.send(dict(width=320,height=240,fps=8,
                    source_sha256=str(i)*64,tracker_namespace='run',frames={str(n):[
                    dict(id='0',birth_id='birth-a',box=[20,20,140,220]),
                    dict(id='P0',birth_id='birth-b',box=[180,20,300,220]),
                    dict(id='7',birth_id='birth-unbound',box=[200,60,260,160])] for n in range(33)}))
            if path.startswith('/media/'):
                # The maintained media endpoint answers byte ranges (imported_media.byte_response).
                # A host that omits them leaves Chromium with seekable=[0,0]: every seek lands on 0.
                body=media.read_bytes();start,end=0,len(body)-1;requested=self.headers.get('Range')
                if requested:
                    match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested);first,last=match.groups() if match else ('','')
                    if first:start=int(first);end=min(int(last),end) if last else end
                    elif last:start=max(0,len(body)-int(last))
                    if not match or not (first or last) or not 0<=start<=end<len(body):
                        return self.send(b'',416,'video/mp4',[('Content-Range',f'bytes */{len(body)}')])
                extra=[('Accept-Ranges','bytes')]+([('Content-Range',f'bytes {start}-{end}/{len(body)}')] if requested else [])
                return self.send(body[start:end+1],206 if requested else 200,'video/mp4',extra)
            if path=='/theme.css':
                tokens=json.loads((HERE/'theme.json').read_text());values={**tokens['palette'],**tokens['typography']}
                values.update(ink='#201e1b',paper='#fbfaf7',accent='#ed5a2b',font='Arial, sans-serif',border='#ddd',muted='#777',surface='#f2f1ee')
                # The corner radii the product's theme route states; the camera stylesheets scale them.
                values.update({'radius':'0.75rem','radius-sm':'0.5rem','radius-lg':'1.25rem'})
                return self.send((':root{'+''.join(f'--{k}:{v};' for k,v in values.items())+'}').encode(),kind='text/css')
            return super().do_GET()
        def log_message(self,*_):pass
    class Host(ThreadingHTTPServer):request_queue_size=64 # Chromium opens its page-load connections together.
    server=Host(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:yield f'http://127.0.0.1:{server.server_port}',state
    finally:
        for ticket in state['held']:ticket['release'].set()
        server.shutdown();server.server_close()


def browser_options():
    options={'headless':True,'args':['--no-sandbox']}
    if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):options['executable_path']=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
    return options


READY="()=>typeof maintainedState!=='undefined'&&maintainedState.status==='ready'"
# Later data, as the page paints it: a held item, an action chapter, a person box.
WATCH="""()=>{
  window.later=()=>({basket:document.querySelector('#visitBasket').innerText.includes('Test carton'),
    chapters:document.querySelectorAll('#personJourney [data-journey]').length,
    boxes:document.querySelectorAll('#trackOverlay [data-track-id]').length});
  // Sample after the player's own seeking handler, then record any later repaint of future data.
  window.watchSeek=()=>{window.stale=false;window.dropped=false;window.atSeeking=null;window.watcher?.disconnect();
    video.addEventListener('seeking',()=>{window.atSeeking=later();
      window.watcher=new MutationObserver(()=>{const now=later();if(now.basket||now.chapters)window.stale=true;
        if(document.querySelector('#benchmarkWorkspace').innerText.includes('connection unavailable'))window.dropped=true;});
      window.watcher.observe(document.body,{subtree:true,childList:true,characterData:true});},{once:true});};
}"""
# The decoded picture read back from the video element: one pixel, or null when the element holds no frame.
PICTURE="""()=>{const c=document.createElement('canvas');c.width=video.videoWidth;c.height=video.videoHeight;
  if(!c.width||!c.height)return null;const g=c.getContext('2d');g.drawImage(video,0,0);
  const pixel=[...g.getImageData(c.width>>1,c.height>>1,1,1).data];return pixel[3]?pixel:null;}"""
# The element after its source is released: stopped, empty, with no frame, poster or buffered media.
RELEASED="()=>video.paused&&!video.hasAttribute('src')&&!video.hasAttribute('poster')&&video.readyState===0&&video.networkState===0&&video.videoWidth===0&&video.buffered.length===0"
# A point inside the painted box of one visit, as fractions of that box.
INSIDE="""([visit,fx,fy])=>{const r=document.querySelector(`[data-visit-id="${visit}"] .track-box`).getBoundingClientRect();
  return {x:r.left+3+(r.width-6)*fx,y:r.top+3+(r.height-6)*fy};}"""
# A page the browser keeps for a return (its back/forward cache) receives pagehide, then pageshow.
# Headless Chromium keeps no such cache, so these tests deliver the two events themselves.
LEAVE="persisted=>dispatchEvent(new PageTransitionEvent('pagehide',{persisted}))"
RETURN="persisted=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted}))"
PLAYING_READY="()=>!video.paused&&decodedFrameTime>.2&&maintainedState.status==='ready'"


def open_player(browser,origin,viewport=None):
    page=browser.new_page(viewport=viewport or {'width':1600,'height':1000});errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(origin+'/index.html');page.wait_for_function(READY)
    return page,errors


def until(page,check,label,timeout=10):
    # Polls host-side state while Playwright keeps dispatching page events.
    deadline=time.monotonic()+timeout
    while not check():
        assert time.monotonic()<deadline,label
        page.wait_for_timeout(20)


def reads(state):return [r for r in state['requests'] if r.startswith('/api/realtime/')]
def legacy_reads(state):return [r for r in state['requests'] if r.split('?')[0] in LEGACY_READS]


def test_actual_player_two_artifact_visit_basket_rewind_and_revocation(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page=browser.new_page(viewport={'width':1600,'height':1000});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(origin+'/index.html');page.wait_for_function("()=>typeof maintainedState!=='undefined'&&maintainedState.status==='ready'")
            assert page.locator('[data-visitor-id]').count()==2
            assert 'First tracked' in page.locator('#visitorList').inner_text()
            page.evaluate('seek(1.5)');page.wait_for_function("()=>maintainedState.value?.playhead_s>=11.5")
            # An interior click selects the exact canonical visit without toggling playback.
            rect=page.locator('#video').bounding_box()
            page.mouse.click(rect['x']+rect['width']*.25,rect['y']+rect['height']*.5)
            page.wait_for_function("()=>selectedPerson==='visit-a'")
            assert page.locator('#visitBasket').inner_text().count('Test carton')==1
            assert '1 estimated item' in page.locator('#visitBasket').inner_text()
            assert page.evaluate('video.paused')
            assert 'First tracked' in page.locator('#personJourney').inner_text()
            assert page.locator('[data-track-id="0"]').get_attribute('data-visit-id')=='visit-a'
            # One accepted pick-up reads "1 interaction" beside its chapter and held item.
            assert page.locator('#personSummary').inner_text().split()==['1.5s','Tracked','1','Interaction']
            # Under the summary: the item that pick-up observed, with what was done to it.
            assert page.locator('#visitorItems .visitor-product strong').inner_text()=='Test carton'
            assert page.locator('#visitorItems .visitor-product p').inner_text()=='Pick up item'
            # Move to the next source file; the canonical visit and held item survive.
            page.evaluate("setClip(data.clips[1].id);seek(.5)")
            page.wait_for_function("()=>maintainedState.value?.playhead_s>=14.5")
            assert page.evaluate('selectedPerson')=='visit-a'
            assert 'Test carton' in page.locator('#visitBasket').inner_text()
            page.evaluate('seek(1.5)');page.wait_for_function("()=>maintainedState.value?.playhead_s>=15.5")
            assert 'Test carton' not in page.locator('#visitBasket').inner_text()
            assert 'Put back' in page.locator('#personJourney').inner_text()
            # First-file chapter seeks its immutable local time, retaining identity.
            page.locator('#personJourney [data-journey]').first.click()
            page.wait_for_function("()=>selected.source_asset_id==='file0'&&maintainedState.status==='ready'")
            assert page.evaluate('selectedPerson')=='visit-a'
            assert abs(page.evaluate('video.currentTime')-1)<.15
            # Rewind clears the later chapter and basket immediately.
            page.evaluate('seek(.25)');page.wait_for_function("()=>maintainedState.value?.playhead_s<11")
            assert 'Test carton' not in page.locator('#visitBasket').inner_text()
            assert page.locator('#personJourney [data-journey]').count()==0
            page.evaluate("selectPerson('visit-b')");assert page.locator('#personHeading').inner_text()=='Visitor 2'
            # A tracked visitor with zero accepted action events keeps its tracker history and wording.
            assert page.locator('#personSummary').inner_text().split()==['0.3s','Tracked','—','Awaiting','events']
            assert page.locator('#visitorItems .visitor-product').count()==0
            assert page.locator('#personJourney .chapter-row').count()==1
            assert 'First tracked' in page.locator('#personJourney').inner_text()
            # Embedded hidden views stop reads and clear the displayed projection.
            page.evaluate("window.postMessage({type:'rocoto:player-suspend'},location.origin)")
            page.wait_for_function("()=>maintainedState.status==='suspended'||maintainedState.status==='loading'")
            before=len([r for r in state['requests'] if r.startswith('/api/realtime/')]);page.wait_for_timeout(650)
            assert len([r for r in state['requests'] if r.startswith('/api/realtime/')])==before
            page.evaluate("window.postMessage({type:'rocoto:player-resume'},location.origin)")
            page.wait_for_function("()=>maintainedState.status==='ready'")
            state['revoked']=True;page.evaluate('seek(1.75)')
            page.wait_for_function("()=>maintainedState.status==='unavailable'")
            assert 'Test carton' not in page.locator('#visitBasket').inner_text()
            assert page.locator('[data-track-id]').count()==0
            assert not any(r.split('?')[0] in ['/data.json','/tracks.json','/ux-data.json','/monitoring-profile.json','/api/control-room/visit-basket'] for r in state['requests'])
            assert errors==[]
        finally:browser.close()


def test_click_anywhere_inside_a_painted_person_box_selects_that_visit(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin)
            def click(visit,fx,fy):
                page.wait_for_function("visit=>document.querySelector(`[data-visit-id=\"${visit}\"] .track-box`)",arg=visit)
                point=page.evaluate(INSIDE,[visit,fx,fy]);page.mouse.click(point['x'],point['y'])
            # Corners, edges and centre of each painted box, from the room view.
            for visit in ['visit-a','visit-b']:
                for fx,fy in [(0,0),(1,0),(0,1),(1,1),(.5,.5),(.5,0),(0,.5),(1,.5),(.5,1)]:
                    page.evaluate('enterRoomMode()');click(visit,fx,fy)
                    assert page.evaluate('selectedPerson')==visit,(visit,fx,fy)
                    assert page.evaluate('video.paused')
            # With a visitor selected, the other painted box switches; the floor between boxes keeps the selection.
            page.evaluate('enterRoomMode()');click('visit-a',.5,.5);assert page.evaluate('selectedPerson')=='visit-a'
            click('visit-b',.5,0);assert page.evaluate('selectedPerson')=='visit-b'
            edge,other=page.evaluate(INSIDE,['visit-a',1,.5]),page.evaluate(INSIDE,['visit-b',0,.5])
            page.mouse.click((edge['x']+other['x'])/2,edge['y'])
            assert page.evaluate('[selectedPerson,video.paused]')==['visit-b',True]
            # An unbound tracker box inside Visitor 2's painted box stays unpainted and leaves the click with Visitor 2.
            page.evaluate('enterRoomMode()');assert page.locator('#trackOverlay [data-track-id]').count()==2
            assert page.locator('[data-track-id="7"]').count()==0
            click('visit-b',(230-180)/120,(110-20)/200)
            assert page.evaluate('selectedPerson')=='visit-b'
            # During playback a click selects; play and pause stay with their own control.
            page.evaluate("enterRoomMode();seek(0);video.muted=true;video.playbackRate=.5;video.play()")
            page.wait_for_function("()=>!video.paused&&decodedFrameTime>.2&&document.querySelector('[data-visit-id=\"visit-a\"] .track-box')")
            click('visit-a',.5,.5)
            assert page.evaluate('[selectedPerson,video.paused,video.ended]')==['visit-a',False,False]
            page.evaluate('video.pause()');assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_rewind_clears_before_the_earlier_frame_and_aborted_reads_stay_unpainted(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin);failed=[]
            page.on('requestfailed',lambda request:failed.append(request.url))
            page.evaluate('seek(1.5)');page.wait_for_function("()=>maintainedState.value?.playhead_s>=11.5")
            page.evaluate("selectPerson('visit-a')");page.evaluate(WATCH)
            assert page.evaluate('later()')=={'basket':True,'chapters':1,'boxes':2}
            cleared={'basket':False,'chapters':0,'boxes':0}
            # Rewind while the host holds every answer: the later pickup leaves with the seek itself.
            state['hold_from']=0;page.evaluate('watchSeek();seek(.25)')
            until(page,lambda:any(t['at']==10.25 for t in state['held']),'the earlier frame asks for its own snapshot')
            assert reads(state)[-1].endswith('at_s=10.25')
            page.wait_for_function("()=>Math.abs(decodedFrameTime-.25)<.07&&!document.querySelector('.footage').classList.contains('frame-pending')")
            assert page.evaluate('atSeeking')==cleared
            assert page.evaluate('[later(),stale,maintainedState.status,selectedPerson]')==[cleared,False,'loading','visit-a']
            assert 'Loading basket' in page.locator('#visitBasket').inner_text()
            assert 'Loading chapters' in page.locator('#personJourney').inner_text()
            state['hold_from']=11
            for ticket in state['held']:ticket['release'].set()
            page.wait_for_function("()=>maintainedState.status==='ready'&&Number(maintainedState.value.playhead_s)===10.25")
            assert page.evaluate('[later(),stale]')==[{'basket':False,'chapters':0,'boxes':2},False]
            # A read for a later frame is in flight when the playhead returns to the earlier one.
            earlier=len(state['held']);page.evaluate('seek(1.5)')
            until(page,lambda:any(t['at']==11.5 for t in state['held'][earlier:]),'the later frame asks for its snapshot')
            page.wait_for_function("()=>Math.abs(decodedFrameTime-1.5)<.07")
            assert page.evaluate('[later(),maintainedState.status]')==[cleared,'loading']
            page.evaluate('watchSeek();seek(.25)')
            page.wait_for_function("()=>maintainedState.status==='ready'&&Number(maintainedState.value.playhead_s)===10.25")
            # The host now answers the superseded read; its pickup and chapter stay off the earlier frame.
            answered=state['answered'].count(11.5)
            for ticket in state['held']:ticket['release'].set()
            until(page,lambda:state['answered'].count(11.5)>answered,'the host answers the superseded read')
            page.wait_for_timeout(250)
            assert page.evaluate('[later(),stale,maintainedState.status,maintainedState.value.playhead_s]')==[
                {'basket':False,'chapters':0,'boxes':2},False,'ready','10.25']
            assert any('at_s=11.5' in url for url in failed),'the browser aborts the superseded read'
            # The aborted read ends quietly: the connection never reads as dropped.
            assert page.evaluate('dropped') is False
            assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_scrubbing_keeps_snapshot_reads_bounded_and_lands_on_the_final_frame(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin);page.wait_for_timeout(1100)
            bar=page.locator('#mainSeek').bounding_box();y=bar['y']+bar['height']/2
            before=len(reads(state));started=time.monotonic()
            page.mouse.move(bar['x']+4,y);page.mouse.down()
            for step in range(60):
                page.mouse.move(bar['x']+4+(bar['width']-8)*((step%20)/20),y);page.wait_for_timeout(16)
            page.mouse.move(bar['x']+bar['width']/2,y);page.mouse.up();elapsed=time.monotonic()-started
            # The last position is the one that is read and painted.
            page.wait_for_function("()=>maintainedState.status==='ready'&&!video.seeking&&Math.abs(video.currentTime-2)<.01"
                "&&Math.abs(Number(maintainedState.value.playhead_s)-10-decodedFrameTime)<1e-6")
            during=len(reads(state))-before
            assert during<=2+math.ceil(elapsed/.5)+1,f'{during} snapshot reads in {elapsed:.2f}s of scrubbing'
            assert page.locator('#trackOverlay [data-track-id]').count()==2
            assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def speed(page,value):
    # The player's own settings panel sets the rate.
    page.locator('#settingsToggle').click();page.locator('#playbackSpeed').select_option(value)
    page.locator('#settingsToggle').click();assert page.locator('#playerSettings').is_hidden()


# One sample taken inside the page while a recording plays with its snapshot on screen, the stream
# playhead inside [start,end). A missed moment comes round again with the loop.
PLAYING="""([asset,start,end])=>{
  if(selected.source_asset_id!==asset||video.paused||video.ended||decodedFrameTime===null||maintainedState.status!=='ready')return false;
  const at=Number(maintainedState.value.playhead_s);if(at<start||at>=end)return false;
  return {visit:selectedPerson,heading:document.querySelector('#personHeading').innerText,rate:video.playbackRate,tab:journeyTab,roster:visitorListOpen,
    basket:document.querySelector('#visitBasket').innerText.includes('Test carton'),chapters:document.querySelectorAll('#personJourney [data-journey]').length,inputs:window.inputs??null};}"""
# The pause control, pressed in the same task that sees the recording playing.
PAUSE="()=>{if(video.paused||video.ended||video.readyState<2||video.duration-video.currentTime<.5)return false;document.querySelector('#playToggle').click();return true;}"


def playing(page,asset,start,end):return page.wait_for_function(PLAYING,arg=[asset,start,end]).json_value()


def test_playback_continues_into_the_next_recording_and_returns_to_the_first(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin)
            page.evaluate("selectPerson('visit-a')");speed(page,'2')
            # The session is the loop under the maintained reader; its settings offer speed, path and outline.
            page.locator('#settingsToggle').click()
            assert [label.split('\n')[0].strip() for label in page.locator('#playerSettings label:visible').all_inner_texts()]==[
                'Playback speed','Camera-view path','Person outline']
            page.locator('#settingsToggle').click()
            # One press of play; every later recording change happens with no pointer or key at all.
            page.locator('#playToggle').click()
            page.evaluate("window.inputs=0;for(const type of ['pointerdown','keydown'])addEventListener(type,()=>window.inputs++,true)")
            kept={'visit':'visit-a','heading':'Visitor 1','rate':2,'tab':'summary','roster':False,'inputs':0}
            # The session continues into the second recording: the same visitor and panel, the chosen speed,
            # and the item picked up in the first recording still held.
            assert playing(page,'file1',14,15)=={**kept,'basket':True,'chapters':1}
            # After the last recording the session returns to its first one; the later pickup is gone with the rewind.
            assert playing(page,'file0',10,11)=={**kept,'basket':False,'chapters':0}
            assert playing(page,'file1',14,18)['inputs']==0
            # A pause holds the session where it is, including at the very end of a recording.
            page.wait_for_function(PAUSE);assert page.evaluate('video.paused')
            held=page.evaluate('selected.source_asset_id')
            page.evaluate('seek(selected.duration_s)');page.wait_for_function("()=>!video.seeking&&video.currentTime>3.9")
            page.wait_for_timeout(700)
            assert page.evaluate('[selected.source_asset_id,video.paused]')==[held,True]
            # Play at the end of a recording continues into the one that follows.
            page.locator('#playToggle').click()
            assert playing(page,'file0' if held=='file1' else 'file1',10,18)['visit']=='visit-a'
            page.wait_for_function(PAUSE);assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_a_mapped_window_shorter_than_its_file_hands_over_at_the_mapped_end(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    # The first recording maps two of its four seconds; the second follows it on the stream clock.
    custom=maintained_bootstrap();first,second=custom['sources'][0]['artifacts']
    first.update(source_window={'start_s':'0','end_s':'2'},stream_window={'start_s':'10','end_s':'12'})
    second.update(stream_window={'start_s':'12','end_s':'16'});state['bootstrap']=(200,custom)
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin)
            page.evaluate("window.reached=0;video.addEventListener('timeupdate',()=>{if(selected.source_asset_id==='file0')window.reached=Math.max(window.reached,video.currentTime);})")
            page.locator('#playToggle').click();playing(page,'file1',12,16)
            # Playback left the first file at its mapped end, well before the file's own end at four seconds.
            assert 1.8<page.evaluate('window.reached')<3.2
            page.wait_for_function(PAUSE);assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_playback_follows_the_session_order_through_three_recordings(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    # Three recordings of one session, published out of order: stream order decides what plays next.
    custom=maintained_bootstrap();first,second=custom['sources'][0]['artifacts']
    third=dict(second,source_asset_id='file2',mapping_id='m2',source_sha256='2'*64,media_url='/media/2',geometry_url='/geometry/2',
        geometry_binding={'source_sha256':'2'*64,'tracker_namespace':'run'},stream_window={'start_s':'18','end_s':'22'})
    custom['sources'][0]['artifacts']=[third,first,second];state['bootstrap']=(200,custom)
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin)
            assert page.evaluate('data.clips.map(c=>c.source_asset_id)')==['file2','file0','file1']
            page.evaluate("setClip(data.clips.find(c=>c.source_asset_id==='file0').id)");page.wait_for_function(READY)
            page.evaluate("selectPerson('visit-a')");speed(page,'2')
            # Each recording that plays is written down once, in the order it played.
            page.evaluate("window.order=[];video.addEventListener('timeupdate',()=>{if(!video.paused&&order.at(-1)!==selected.source_asset_id)order.push(selected.source_asset_id);})")
            page.locator('#playToggle').click()
            page.evaluate("window.inputs=0;for(const type of ['pointerdown','keydown'])addEventListener(type,()=>window.inputs++,true)")
            page.wait_for_function('()=>order.length>=5',timeout=60000)
            assert page.evaluate('order.slice(0,5)')==['file0','file1','file2','file0','file1']
            assert page.evaluate('[selectedPerson,video.playbackRate,window.inputs]')==['visit-a',2,0]
            page.wait_for_function(PAUSE);assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_interrupted_and_revoked_reads_leave_the_view_unavailable_then_reconnect(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin)
            def unavailable(label):
                page.wait_for_function("()=>maintainedState.status==='unavailable'")
                assert page.evaluate('[readAuthority,maintainedState.value,selectedPerson]')==['maintained',None,'visit-a'],label
                assert page.locator('#visitBasket').inner_text().strip()=='Basket connection unavailable. Reconnecting…',label
                assert page.locator('#personJourney').inner_text().strip()=='Chapter connection unavailable. Reconnecting…',label
                assert page.locator('#trackOverlay [data-track-id]').count()==0,label
                # The picture goes with the access: the element stops, releases its source and keeps no frame.
                page.wait_for_function(RELEASED);assert page.evaluate(PICTURE) is None,label
                assert page.locator('#mediaLoading').inner_text()=='Camera connection unavailable. Reconnecting…',label
                assert state['requests'].count('/api/control-room/bootstrap')==1,label
                page.evaluate('enterRoomMode()')
                assert page.locator('[data-visitor-id]').count()==0,label
                assert 'Visitor connection unavailable. Reconnecting…' in page.locator('#visitorList').inner_text(),label
                assert legacy_reads(state)==[] and state['requests'].count('/api/control-room/bootstrap')==1,label
            def reconnect():
                # The next answered read restores the picture at the place asked for.
                page.evaluate('seek(1.5)')
                page.wait_for_function("()=>maintainedState.value?.playhead_s>=11.5&&video.readyState>=2&&decodedFrameTime>=1.5&&!document.querySelector('.footage').classList.contains('frame-pending')")
                assert page.evaluate(PICTURE) is not None and page.locator('#mediaLoading').is_hidden()
                assert page.locator('[data-visitor-id]').count()==2
                page.evaluate("selectPerson('visit-a')");assert 'Test carton' in page.locator('#visitBasket').inner_text()
            reconnect()
            # The connection drops halfway through a snapshot body.
            state['interrupted']=True;page.evaluate('seek(1.75)');unavailable('interrupted')
            state['interrupted']=False;reconnect()
            # Source access is revoked while the visitor is on screen.
            state['revoked']=True;page.evaluate('seek(1.75)');unavailable('revoked')
            state['revoked']=False;reconnect()
            # Access is revoked during playback: the next steady read clears the screen, with no seek involved.
            state['revoked']=True;page.evaluate('video.muted=true;video.play()');unavailable('revoked during playback')
            assert page.evaluate('video.seeking') is False
            # While access is away the recording stays away: neither play nor a chosen recording brings a frame back.
            media=len([r for r in state['requests'] if r.startswith('/media/')])
            page.evaluate('setClip(data.clips[1].id)');page.locator('#playToggle').click();page.wait_for_timeout(400)
            assert page.evaluate(RELEASED) and page.evaluate(PICTURE) is None
            assert len([r for r in state['requests'] if r.startswith('/media/')])==media
            # Reconnection restores the chosen recording and the playback that was under way.
            state['revoked']=False
            page.wait_for_function("()=>maintainedState.status==='ready'&&selected.source_asset_id==='file1'&&!video.paused&&decodedFrameTime>.1")
            page.wait_for_function(PAUSE)
            assert page.evaluate(PICTURE) is not None and page.locator('[data-track-id]').count()==2
            # The reconnected view carries its visitors and the first visitor's earlier pickup again.
            page.wait_for_function("()=>maintainedState.status==='ready'");assert page.locator('[data-visitor-id]').count()==2
            page.evaluate("selectPerson('visit-a')");assert page.locator('#personJourney [data-journey]').count()>=1
            assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def play_slowly(page):
    page.evaluate("selectPerson('visit-a');video.muted=true;video.playbackRate=.25;video.play()")
    page.wait_for_function(PLAYING_READY)


def test_a_page_the_browser_kept_reads_at_once_when_it_is_shown_again(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin);play_slowly(page)
            # Whatever the leaving event says about a return, the page that is shown again holds a reader that asks.
            for persisted in (True,False):
                # Away, the page stops playing and reading, and what it showed leaves the screen.
                page.evaluate(LEAVE,persisted)
                assert page.evaluate('[video.paused,maintainedState.value]')==[True,None],persisted
                assert page.locator('#trackOverlay [data-track-id]').count()==0,persisted
                asked=len(reads(state));page.wait_for_timeout(1200);assert len(reads(state))==asked,persisted
                # Shown again, it reads at once; visitors, boxes, picture and the playback under way return.
                page.evaluate(RETURN,persisted)
                until(page,lambda:len(reads(state))>asked,f'a page shown again reads its snapshot (persisted={persisted})',timeout=5)
                page.wait_for_function(PLAYING_READY)
                page.wait_for_function("()=>document.querySelectorAll('#trackOverlay [data-track-id]').length===2")
                assert page.evaluate('selectedPerson')=='visit-a' and page.evaluate(PICTURE) is not None,persisted
                assert page.locator('#visitBasket').is_visible() and 'Loading' not in page.locator('#visitBasket').inner_text(),persisted
                page.evaluate('enterRoomMode()');assert page.locator('[data-visitor-id]').count()==2,persisted
                page.evaluate("selectPerson('visit-a')")
            page.wait_for_function(PAUSE)
            # A page left while paused stays paused on its return, with its visitors read again.
            page.evaluate(LEAVE,True);asked=len(reads(state));page.evaluate(RETURN,True)
            until(page,lambda:len(reads(state))>asked,'a paused page shown again reads its snapshot',timeout=5)
            page.wait_for_function("()=>maintainedState.status==='ready'&&decodedFrameTime!==null")
            assert page.evaluate('video.paused') and page.locator('#trackOverlay [data-track-id]').count()==2
            assert state['requests'].count('/api/control-room/bootstrap')==1
            assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_access_revoked_while_the_page_was_away_takes_the_picture_on_its_return(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin);play_slowly(page)
            media=lambda:len([r for r in state['requests'] if r.startswith('/media/')])
            # Paused or playing when it left, the returning page asks first and the refusal takes the picture.
            for playing in (True,False):
                if not playing:page.wait_for_function(PAUSE)
                page.evaluate(LEAVE,True);state['revoked']=True;asked=len(reads(state))
                page.evaluate(RETURN,True)
                until(page,lambda:len(reads(state))>asked,f'a page shown again reads its snapshot (playing={playing})',timeout=5)
                page.wait_for_function("()=>maintainedState.status==='unavailable'")
                page.wait_for_function(RELEASED);assert page.evaluate(PICTURE) is None,playing
                assert page.locator('#mediaLoading').inner_text()=='Camera connection unavailable. Reconnecting…',playing
                assert page.locator('#trackOverlay [data-track-id]').count()==0,playing
                assert page.locator('#visitBasket').inner_text().strip()=='Basket connection unavailable. Reconnecting…',playing
                # Play pressed while access is away brings no frame and no recording bytes.
                fetched=media();page.locator('#playToggle').click();page.wait_for_timeout(400)
                assert page.evaluate(RELEASED) and page.evaluate(PICTURE) is None and media()==fetched,playing
                # Access returns: the page reconnects by itself and plays.
                state['revoked']=False;page.wait_for_function(PLAYING_READY)
                assert page.evaluate(PICTURE) is not None and page.evaluate('selectedPerson')=='visit-a',playing
            page.wait_for_function(PAUSE)
            assert state['requests'].count('/api/control-room/bootstrap')==1
            assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_the_compact_session_viewer_reads_under_the_same_authority(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page,errors=open_player(browser,origin)
            # With the location overview showing, the player is out of view and its reader rests.
            page.evaluate('ControlRoom.showOverview()');page.wait_for_function("()=>!document.querySelector('#locationRoot').hidden")
            assert page.evaluate('[maintainedState.status,maintainedState.value]')==['suspended',None]
            asked=len(reads(state));page.wait_for_timeout(700);assert len(reads(state))==asked
            # A session opened from the overview shows the player in a dialog. The overview's visit rows call this
            # entry point; under the maintained reader the overview carries the location record, so the test calls it.
            page.evaluate("ControlRoom.openSession({clipId:data.clips[0].id,personId:null,time_s:1.5})")
            until(page,lambda:len(reads(state))>asked,'the compact session viewer reads its snapshot',timeout=5)
            page.wait_for_function("()=>maintainedState.status==='ready'&&Number(maintainedState.value.playhead_s)>=11.5&&decodedFrameTime!==null")
            assert page.evaluate("document.querySelector('#compactSession').open") and page.evaluate(PICTURE) is not None
            assert page.locator('#compactSession [data-visitor-id]').count()==2
            # Revoked access takes this picture as well, and returning access restores it.
            state['revoked']=True;page.evaluate('seek(1.75)')
            page.wait_for_function("()=>maintainedState.status==='unavailable'");page.wait_for_function(RELEASED)
            assert page.evaluate(PICTURE) is None
            state['revoked']=False;page.evaluate('seek(1.5)')
            page.wait_for_function("()=>maintainedState.status==='ready'&&decodedFrameTime!==null&&video.readyState>=2")
            assert page.evaluate(PICTURE) is not None
            # Closed, the player is out of view again and its reader rests.
            page.locator('#compactSession .session-close').click()
            page.wait_for_function("()=>!document.querySelector('#compactSession').open&&maintainedState.status==='suspended'")
            asked=len(reads(state));page.wait_for_timeout(700);assert len(reads(state))==asked
            # «Open cameras» in the dialog moves on to the camera view, which keeps reading.
            page.evaluate("ControlRoom.openSession({clipId:data.clips[0].id,personId:null,time_s:1.5})")
            page.wait_for_function("()=>maintainedState.status==='ready'&&decodedFrameTime!==null")
            page.locator('#compactSession .session-full').click()
            page.wait_for_function("()=>!document.querySelector('#compactSession').open&&!document.querySelector('#compare').hidden")
            asked=len(reads(state));page.evaluate('seek(2)')
            until(page,lambda:len(reads(state))>asked,'the camera view reads after the dialog closes',timeout=5)
            page.wait_for_function("()=>maintainedState.status==='ready'&&Number(maintainedState.value.playhead_s)>=12")
            assert page.locator('[data-visitor-id]').count()==2
            assert legacy_reads(state)==[] and errors==[]
        finally:browser.close()


def test_bootstrap_failure_has_no_legacy_fallback(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site;state['bootstrap']=(503,{})
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            page=browser.new_page();page.goto(origin+'/index.html')
            page.wait_for_function("()=>!document.querySelector('#mediaError').hidden")
            assert 'Camera connection unavailable' in page.locator('#mediaError').inner_text()
            assert '/data.json' not in state['requests']
        finally:browser.close()


def test_every_unavailable_bootstrap_paints_its_notice_and_reads_nothing_else(maintained_site):
    playwright=pytest.importorskip('playwright.sync_api');origin,state=maintained_site
    unmapped=maintained_bootstrap();del unmapped['sources'][0]['artifacts'][1]['mapping_id']
    foreign=maintained_bootstrap()
    for artifact in foreign['sources'][0]['artifacts']:artifact['media_url']='https://foreign.test/private.mp4'
    cases={'host failure':(503,{}),'revoked access':(403,{}),'malformed answer':(200,{}),
        'undeclared reader':(200,dict(maintained_bootstrap(),read_authority='bundle')),
        'missing mapping':(200,unmapped),'empty catalog':(200,dict(maintained_bootstrap(),sources=[])),
        'foreign media':(200,foreign),'interrupted answer':'interrupted'}
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(**browser_options())
        try:
            for label,answer in cases.items():
                state['bootstrap']=answer;state['requests'].clear()
                page=browser.new_page();errors=[];page.on('pageerror',lambda error,errors=errors:errors.append(str(error)))
                page.goto(origin+'/index.html')
                # The sentence is painted, with nothing else of the player around it.
                notice=page.get_by_text('Camera connection unavailable. Reload to reconnect.').locator('visible=true')
                notice.wait_for(state='visible')
                assert notice.count()==1 and notice.get_attribute('role')=='alert',label
                assert page.locator('#benchmarkWorkspace').is_hidden() and page.locator('#locationRoot').is_hidden(),label
                assert 'Loading' not in page.locator('main').inner_text(),label
                page.wait_for_timeout(150)
                assert [r for r in state['requests'] if r.startswith(('/api/','/media/','/geometry/'))]==['/api/control-room/bootstrap'],label
                assert legacy_reads(state)==[] and errors==[],label
                page.close()
        finally:browser.close()
