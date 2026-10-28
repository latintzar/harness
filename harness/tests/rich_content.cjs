/* Self-contained browser tests: local static fixtures only, zero provider calls.
 * NODE_PATH=/root/.claude/skills/gstack/node_modules PLAYWRIGHT_CHROMIUM_EXECUTABLE=/home/peter/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell node --test harness/tests/rich_content.cjs
 */
const {test,before,after}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
const web=require('./web_fixture.cjs').web(),theme=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../theme.json'),'utf8'));let browser,server,origin;
const FRAME_CSP="default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:; font-src 'self'; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'";
before(async()=>{
 server=http.createServer((req,res)=>{const route=new URL(req.url,'http://localhost').pathname;
  if(route==='/theme.css'){res.setHeader('Content-Type','text/css');res.end(':root{'+Object.entries({...theme.palette,...theme.typography,'connection-online':theme.connection_online,accent:theme.palette.link,'icon-size-meta':'16px',radius:'12px'}).map(([k,v])=>'--'+k+':'+v+';').join('')+'}');return;}
  if(route==='/'){res.setHeader('Content-Type','text/html');res.setHeader('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-src 'self'");res.end('<!doctype html><html><head><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/rich-content.css"><script defer src="/vendor/marked-18.0.9.umd.js"></script><script defer src="/vendor/dompurify-3.4.16.min.js"></script><script defer src="/rich-content.js"></script></head><body><main id="content" class="markdown-content"></main></body></html>');return;}
  const file=path.resolve(web,'.'+route);if(!file.startsWith(web+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',({'.js':'application/javascript','.html':'text/html','.css':'text/css','.woff2':'font/woff2','.woff':'font/woff','.ttf':'font/ttf'})[path.extname(file)]||'application/octet-stream');res.setHeader('X-Content-Type-Options','nosniff');if(route==='/rich-diagram.html')res.setHeader('Content-Security-Policy',FRAME_CSP);fs.createReadStream(file).pipe(res);
 });await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox']});
});
after(async()=>{await browser?.close();await new Promise(r=>server?.close(r));});
async function fixture(width=900){const context=await browser.newContext({viewport:{width,height:800}}),page=await context.newPage(),external=[];
 await page.route('**/*',r=>{if(!r.request().url().startsWith(origin+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
 await page.goto(origin);await page.waitForFunction(()=>window.RocotoRichContent);await page.evaluate(()=>{window.copied=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>window.copied.push(value)}});});
 return {page,external,close:()=>context.close()};}
async function render(page,text,options={}){await page.evaluate(async({text,options})=>{
 const node=(tag,cls,value)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(value!==undefined)el.textContent=value;return el;},icon=name=>{const el=node('span','icon');el.setAttribute('data-icon',name);return el;};
 window.marked.use({gfm:true,renderer:{html(){return'';},image(){return'';}}});RocotoRichContent.configure(marked);
 const fragment=DOMPurify.sanitize(marked.parse(text),{RETURN_DOM_FRAGMENT:true,ALLOWED_TAGS:['p','br','strong','em','a','pre','code','table','thead','tbody','tr','th','td','ul','li'],ALLOWED_ATTR:['href','class']});
 for(const el of fragment.querySelectorAll('[class]'))if(el.tagName!=='CODE'||!/^language-[a-z0-9_+-]{1,32}$/i.test(el.className))el.removeAttribute('class');
 const root=document.querySelector('#content');root.replaceChildren(fragment);for(const table of root.querySelectorAll('table')){const wrap=node('div','markdown-table');table.before(wrap);wrap.append(table);}
 await RocotoRichContent.enrich(root,{node,icon,...options});
 },{text,options});}
test('code highlighting keeps literal source and copy works without executing markup',async()=>{const h=await fixture();try{
 const source='const value = "<img src=x onerror=alert(1)>";\n';await render(h.page,'```javascript\n'+source+'```');
 assert(await h.page.locator('code .hljs-keyword').count()>0);assert.equal(await h.page.locator('#content img').count(),0);
 await h.page.getByRole('button',{name:'Copy code',exact:true}).click();assert.deepEqual(await h.page.evaluate(()=>copied),[source]);assert.deepEqual(h.external,[]);
}finally{await h.close();}});
test('unknown languages remain literal and repeated enrichment creates one toolbar',async()=>{const h=await fixture();try{
 await render(h.page,'```unavailable\n<raw>&source\n```');await h.page.evaluate(()=>RocotoRichContent.enrich(document.querySelector('#content')));
 assert.equal(await h.page.getByRole('button',{name:'Copy code',exact:true}).count(),1);assert.equal(await h.page.locator('pre code').textContent(),'<raw>&source\n');
}finally{await h.close();}});
test('table retains existing wrapper; copy and expanded view preserve values and close focus',async()=>{const h=await fixture(390);try{
 await render(h.page,'| Product | Units |\n|---|---:|\n| Coffee | 12 |\n| Tea | 4 |');assert.equal(await h.page.locator('.markdown-table').count(),1);
 await h.page.getByRole('button',{name:'Copy table',exact:true}).click();assert.deepEqual(await h.page.evaluate(()=>copied),['Product\tUnits\nCoffee\t12\nTea\t4']);
 await h.page.getByRole('button',{name:'Expand table',exact:true}).click();assert.equal(await h.page.getByRole('dialog').locator('tr').count(),3);
 await h.page.keyboard.press('Escape');assert.equal(await h.page.getByRole('dialog').count(),0);assert.equal(await h.page.getByRole('button',{name:'Expand table',exact:true}).evaluate(el=>el===document.activeElement),true);
}finally{await h.close();}});
test('standard inline and display math render while money and code stay literal',async()=>{const h=await fixture();try{
 await render(h.page,'Inline \\(x^2 + y^2\\), $12 and $15.\n\n$$\\frac{1}{2}$$\n\n`\\(code\\)`');
 assert.equal(await h.page.locator('.katex').count(),2);assert((await h.page.locator('#content').textContent()).includes('$12 and $15'));assert.equal(await h.page.locator('p > code').last().textContent(),'\\(code\\)');
}finally{await h.close();}});
test('math prevents links/images/HTML and infinite macros with fresh bounded options',async()=>{const h=await fixture();try{
 await render(h.page,'```math\n\\href{javascript:alert(1)}{click} \\includegraphics{https://attacker.invalid/image}\n```\n\n```math\n\\def\\loop{\\loop}\\loop\n```');
 assert.equal(await h.page.locator('#content a,#content img').count(),0);assert.deepEqual(h.external,[]);assert(await h.page.locator('#content').textContent());
}finally{await h.close();}});
test('Mermaid renders an actual isolated diagram with a copyable source',async()=>{const h=await fixture();try{
 const source='flowchart LR\n A[Source] --> B[Result]\n';await render(h.page,'```mermaid\n'+source+'```');
 const frame=h.page.locator('iframe.rich-diagram');assert.equal(await frame.getAttribute('sandbox'),'allow-scripts');assert.equal(await frame.count(),1);
 assert(await h.page.frameLocator('iframe.rich-diagram').locator('svg .node').count()>=2);
 assert.equal(await frame.evaluate(el=>{try{return el.contentDocument===null;}catch{return true;}}),true);
 await h.page.getByRole('button',{name:'Copy diagram',exact:true}).click();assert.deepEqual(await h.page.evaluate(()=>copied),[source]);assert.deepEqual(h.external,[]);
}finally{await h.close();}});
test('diagram configuration and raw HTML are refused without creating a frame',async()=>{const h=await fixture();try{
 await render(h.page,'```mermaid\n%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\n A --> B\n```\n\n<script>alert(1)</script>');
 assert.equal(await h.page.locator('iframe').count(),0);assert.equal(await h.page.locator('#content script').count(),0);assert((await h.page.locator('pre').textContent()).includes('securityLevel'));assert.deepEqual(h.external,[]);
}finally{await h.close();}});
test('strict diagram labels and click declarations cannot execute or fetch remotely',async()=>{const h=await fixture();try{
 await render(h.page,'```mermaid\nflowchart LR\n A["<img src=https://attacker.invalid/pixel onerror=alert(1)>"] --> B[Safe]\n click B "javascript:alert(1)"\n```');
 assert.equal(await h.page.frameLocator('iframe.rich-diagram').locator('img,script:not([src]),a[href]').count(),0);assert.deepEqual(h.external,[]);
}finally{await h.close();}});
test('pending streams keep diagrams as source until final enrichment; bounded source stays readable',async()=>{const h=await fixture();try{
 await render(h.page,'```mermaid\nflowchart LR\n A-->B\n```',{pending:true});assert.equal(await h.page.locator('iframe').count(),0);
 await render(h.page,'```mermaid\nflowchart LR\n A-->B\n```');assert.equal(await h.page.locator('iframe').count(),1);
 await render(h.page,'```mermaid\n'+('A'.repeat(16000))+'\n```');assert.equal(await h.page.locator('iframe').count(),0);assert.equal((await h.page.locator('pre').textContent()).trim().length,16000);
}finally{await h.close();}});
test('diagram failure and missing optional highlighter keep original content usable',async()=>{const h=await fixture();try{
 await render(h.page,'```mermaid\nnot a diagram\n```');assert((await h.page.locator('pre').textContent()).includes('not a diagram'));
 await h.page.route('**/vendor/highlight-11.12.0/highlight.min.js',r=>r.abort());await render(h.page,'```python\nprint("kept")\n```');assert.equal(await h.page.locator('pre code').textContent(),'print("kept")\n');
}finally{await h.close();}});

test('mobile diagram stays visible after source toggle and expands independently',async()=>{const h=await fixture(390);try{
 await render(h.page,'```mermaid\nflowchart LR\n A[Source] --> B[Result]\n```');
 const source=h.page.getByRole('button',{name:'View source',exact:true});await source.click();assert.equal(await h.page.locator('pre').isVisible(),true);
 await h.page.getByRole('button',{name:'View diagram',exact:true}).click();assert.equal(await h.page.locator('pre').isVisible(),false);
 const svg=h.page.frameLocator('iframe.rich-diagram').locator('#diagram svg');assert.equal(await svg.isVisible(),true);
 const bounds=await svg.boundingBox();assert(bounds.width>50&&bounds.height>20,JSON.stringify(bounds));
 await h.page.getByRole('button',{name:'Expand diagram',exact:true}).click();await h.page.getByRole('dialog').locator('iframe').waitFor();
 await h.page.waitForFunction(()=>document.querySelector('dialog iframe')&&!document.querySelector('dialog iframe').hasAttribute('aria-busy'));
 assert.equal(await h.page.getByRole('dialog').frameLocator('iframe').locator('svg').isVisible(),true);await h.page.keyboard.press('Escape');
}finally{await h.close();}});

test('reference styling uses an unboxed table and icon-only controls revealed on hover and focus',async()=>{const h=await fixture(1600);try{
 await render(h.page,'```js\nconst answer = 42;\n```\n\n| Name | Value |\n|---|---|\n| Review | 42 |');
 assert.equal(await h.page.locator('.rich-language').textContent(),'JavaScript');assert.equal(await h.page.locator('.rich-table .rich-language,.rich-table .rich-toolbar').count(),0);
 const table=h.page.locator('.rich-table'),actions=table.locator('.rich-actions');
 assert.equal(await table.evaluate(el=>getComputedStyle(el).borderTopWidth),'0px');assert.equal(await actions.evaluate(el=>getComputedStyle(el).opacity),'0');
 assert.equal(await table.locator('th').first().evaluate(el=>getComputedStyle(el).backgroundColor),'rgba(0, 0, 0, 0)');
 await table.hover();await h.page.waitForFunction(()=>getComputedStyle(document.querySelector('.rich-table .rich-actions')).opacity==='1');
 await h.page.mouse.move(0,0);await h.page.getByRole('button',{name:'Copy table',exact:true}).focus();assert.equal(await actions.evaluate(el=>getComputedStyle(el).pointerEvents),'auto');
 for(const label of ['Copy code','Copy table','Expand table']){const button=h.page.getByRole('button',{name:label,exact:true});assert.equal(await button.getAttribute('title'),label);assert.equal(await button.locator('.rich-action-label').evaluate(el=>getComputedStyle(el).clip),'rect(0px, 0px, 0px, 0px)');}
}finally{await h.close();}});
test('mobile table and diagram controls stay reachable with borderless presentation',async()=>{const h=await fixture(390);try{
 await render(h.page,'| Name | Value |\n|---|---|\n| Review | 42 |\n\n```mermaid\nflowchart LR\n A-->B\n```');
 for(const selector of ['.rich-table','.rich-diagram-block']){const block=h.page.locator(selector);assert.equal(await block.evaluate(el=>getComputedStyle(el).borderTopWidth),'0px');assert.equal(await block.locator('.rich-actions').evaluate(el=>getComputedStyle(el).opacity),'1');assert.equal(await block.locator('.rich-language').count(),0);}
 for(const button of await h.page.locator('.rich-actions button').all()){const bounds=await button.boundingBox();assert(bounds.width>=36&&bounds.height>=36);assert(bounds.x>=0&&bounds.x+bounds.width<=390);}
 assert.equal(await h.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
}finally{await h.close();}});
