"""Browser lifecycle check; controlled media events isolate async hover races."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import threading
import pytest


def test_same_origin_preview_cancels_and_cleans_up():
    playwright = pytest.importorskip('playwright.sync_api')
    web = Path(__file__).parents[1] / 'verticals/retail/web/control-room'
    scripts = {'/home-model.js', '/location-view.js'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (web / self.path[1:]).read_bytes() if self.path in scripts else b'<main id="root"></main><script src="/home-model.js"></script><script src="/location-view.js"></script>'
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript' if self.path in scripts else 'text/html')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; media-src 'self'; style-src 'self' 'unsafe-inline'")
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with playwright.sync_playwright() as p:
            opts = {'headless': True, 'args': ['--no-sandbox']}
            if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
                opts['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
            browser = p.chromium.launch(**opts)
            try:
                page = browser.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                page.evaluate('''()=>{
                  const urls=new WeakMap(),times=new WeakMap();window.plays=[];window.pauses=0;
                  window.testHidden=false;Object.defineProperty(document,'hidden',{get:()=>window.testHidden});
                  window.previewObservers=[];
                  window.IntersectionObserver=class {constructor(callback){this.callback=callback;window.previewObservers.push(this)}observe(){}disconnect(){this.disconnected=true}};
                  Object.defineProperty(HTMLMediaElement.prototype,'src',{get(){return urls.get(this)||''},set(v){urls.set(this,v)}});
                  const oldHas=HTMLMediaElement.prototype.hasAttribute,oldRemove=HTMLMediaElement.prototype.removeAttribute;
                  HTMLMediaElement.prototype.hasAttribute=function(k){return k==='src'?urls.has(this):oldHas.call(this,k)};
                  HTMLMediaElement.prototype.removeAttribute=function(k){return k==='src'?urls.delete(this):oldRemove.call(this,k)};
                  HTMLMediaElement.prototype.load=function(){};
                  HTMLMediaElement.prototype.pause=function(){window.pauses++};
                  HTMLMediaElement.prototype.play=function(){return new Promise(resolve=>window.plays.push(resolve))};
                  Object.defineProperty(HTMLMediaElement.prototype,'currentTime',{get(){return times.get(this)||0},set(v){times.set(this,v)}});
                  Object.defineProperty(HTMLMediaElement.prototype,'duration',{get(){return 20}});
                  window.testData={locations:[{id:'shop',name:'Shop',metrics:{},products:[{id:'item',label:'Observed item',counts:{interactions:1},events:[{label:'Pick up',time_s:4,end_s:5,media:'/control-room/camera.mp4',source_clip_duration_s:20}]}]}]};
                  window.view=LocationView.create({root:document.querySelector('#root'),data:testData});
                }''')
                page.locator('.location-product-card').first.click()
                row = page.locator('.location-history-row').first
                row.dispatch_event('pointerenter')
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src').endswith('/control-room/camera.mp4')
                row.dispatch_event('pointerleave')
                page.locator('.location-history-row video').evaluate("v=>v.dispatchEvent(new Event('loadedmetadata'))")
                assert page.evaluate('window.plays.length') == 0
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src') == ''
                row.dispatch_event('pointerenter')
                page.locator('.location-history-row video').evaluate("v=>{Object.defineProperty(v,'readyState',{value:1,configurable:true});v.dispatchEvent(new Event('loadedmetadata'))}")
                page.wait_for_function('()=>window.plays.length===1')
                row.dispatch_event('pointerleave')
                page.evaluate('window.plays[0]()')
                assert not page.locator('.location-history-row video').evaluate("v=>v.classList.contains('playing')")
                row.dispatch_event('pointerenter')
                page.wait_for_function('()=>window.plays.length===2')
                page.evaluate('window.plays[1]()')
                page.wait_for_function("()=>document.querySelector('.location-history-row video').classList.contains('playing')")
                assert page.locator('.location-history-row video').evaluate('(v)=>v.currentTime') == pytest.approx(3.4)
                page.evaluate("window.testHidden=true;document.dispatchEvent(new Event('visibilitychange'))")
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src') == ''
                row.dispatch_event('pointerenter')
                assert page.evaluate('window.plays.length') == 2
                page.evaluate("window.testHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src') == '', 'return waits for fresh pointer/focus intent'
                row.dispatch_event('pointerenter')
                page.wait_for_function('()=>window.plays.length===3')
                page.evaluate("window.previewObservers.at(-1).callback([{isIntersecting:false}]);window.plays[2]()")
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src') == ''
                assert not page.locator('.location-history-row video').evaluate("v=>v.classList.contains('playing')")
                row.dispatch_event('pointerenter')
                assert page.evaluate('window.plays.length') == 3
                page.locator('#locationProductClose').click()
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src') == ''
                page.locator('.location-product-card').first.click()
                row = page.locator('.location-history-row').first
                row.dispatch_event('pointerenter')
                page.evaluate("view.setData({locations:[{id:'different',name:'Other',metrics:{},products:[]}]})")
                assert not page.locator('#locationProductDialog').evaluate('(d)=>d.open')
                assert page.locator('.location-history-row video').evaluate('(v)=>v.src') == ''
                page.evaluate('view.destroy()')
                assert page.locator('#root').inner_html() == ''
                assert page.evaluate('window.previewObservers.every(x=>x.disconnected)')
                assert errors == []
            finally: browser.close()
    finally: server.shutdown(); server.server_close()
