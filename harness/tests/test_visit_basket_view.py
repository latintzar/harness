"""Browser playback boundary and late-response isolation using actual view code."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import threading
import pytest


def test_basket_selection_rewind_revocation_and_safe_rendering():
    playwright=pytest.importorskip('playwright.sync_api')
    source=Path(__file__).parents[1]/'verticals/retail/web/control-room/visit-basket.js'
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=source.read_bytes() if self.path=='/view.js' else b'<section id="basket" hidden></section><script src="/view.js"></script>'
            self.send_response(200);self.send_header('Content-Type','application/javascript' if self.path=='/view.js' else 'text/html')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; img-src 'self'")
            self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with playwright.sync_playwright() as p:
            options={'headless':True,'args':['--no-sandbox']}
            if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):options['executable_path']=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
            browser=p.chromium.launch(**options)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/')
                page.evaluate('''()=>{
                  window.requests=[];window.now=0;
                  window.view=VisitBasketView.create({host:document.querySelector('#basket'),clock:()=>now,fetcher:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))});
                  window.result=(at,from,next,description='Test carton')=>({as_of_s:at,validity:{from_s:from,next_change_s:next},items:description?[{description,quantity:1,identity_basis:'vision_estimate',state:'held',image:'https://foreign.test/private.jpg',line_estimate:{amount:'2.50',currency:'EUR'}}]:[],uncertain_items:[],unresolved_observations:[],totals:description?[{amount:'2.50',currency:'EUR',priced_units:1,partial:true}]:[],coverage:{unpriced_units:1}});
                  window.reply=(index,value,status=200)=>requests[index].resolve({ok:status===200,json:async()=>value});
                  view.update({sourceId:'camera-a',personId:'0',time:2});
                }''')
                page.wait_for_function('()=>requests.length===1')
                assert 'track_id=0' in page.evaluate('requests[0].url')
                page.evaluate("reply(0,result(2,0,3,null))")
                page.wait_for_function("()=>!document.querySelector('#basket').hidden")
                page.evaluate("view.update({sourceId:'camera-a',personId:'0',time:2.5})")
                assert page.evaluate('requests.length')==1
                page.evaluate("view.update({sourceId:'camera-a',personId:'0',time:3.1})")
                assert page.locator('#basket').is_hidden()
                page.evaluate("view.update({sourceId:'camera-a',personId:'P0',time:3.1})")
                assert page.evaluate('requests[1].options.signal.aborted')
                page.evaluate("reply(1,result(3.1,3,null,'Wrong visitor item'))")
                assert 'Wrong visitor' not in page.locator('#basket').inner_text()
                page.evaluate("reply(2,result(3.1,3,10,'<img src=x onerror=alert(1)>'))")
                page.wait_for_function("()=>!document.querySelector('#basket').hidden")
                assert '<img src=x' in page.locator('#basket').inner_text()
                assert page.locator('#basket img').count()==0
                assert '2.50 EUR' in page.locator('#basket').inner_text()
                # Rewind immediately removes a later basket; its delayed response cannot reappear.
                page.evaluate("view.clear();view.update({sourceId:'camera-a',personId:'P0',time:1})")
                assert page.locator('#basket').is_hidden()
                page.evaluate('reply(3,result(1,0,3,null))')
                page.wait_for_function("()=>!document.querySelector('#basket').hidden")
                assert '2.50 EUR' not in page.locator('#basket').inner_text()
                # Source authority is refreshed even inside an unchanged event interval.
                page.evaluate("now=10001;view.update({sourceId:'camera-a',personId:'P0',time:1.1});reply(4,{},403)")
                page.wait_for_function("()=>document.querySelector('#basket').hidden")
                # A malformed 200 response backs off; it cannot create a tight request loop.
                page.evaluate("now=20002;view.update({sourceId:'camera-a',personId:'P0',time:1.2});reply(5,{as_of_s:999})")
                page.wait_for_timeout(100)
                page.evaluate("view.update({sourceId:'camera-a',personId:'P0',time:1.2})")
                assert page.evaluate('requests.length')==6
                assert page.locator('#basket').is_hidden()
                page.evaluate("view.destroy();reply(4,result(1.1,0,3,'Late item'))")
                assert page.locator('#basket').inner_text()==''
                assert errors==[]
            finally:browser.close()
    finally:server.shutdown();server.server_close()
