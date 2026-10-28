/* Free component checks; network traffic is fixture-owned. */
const {test,before,after}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{chromium}=require('playwright');
const web=require('./web_fixture.cjs').web(),modulePath=process.env.ROCOTO_TOOL_TRAIL_MODULE||path.join(web,'tool-trail.js');
let browser;
before(async()=>{browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox']});});
after(async()=>{await browser?.close();});
const frame=(index=0)=>({url:'/api/retail/inspection-previews/'+'a'.repeat(32)+'/'+index+'.jpg',time:index+.125,clip_id:'source-clip-1',sha256:'b'.repeat(64),basis:'source-relative',width:1280,height:720});
async function pageFor(width=1600){const page=await browser.newPage({viewport:{width,height:844}}),requests=[];await page.route('**/*',r=>{requests.push(r.request().url());return r.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="144" height="88"><rect width="144" height="88" fill="gray"/></svg>'});});await page.setContent('<base href="https://fixture.invalid/"><main id="target"></main>');await page.addScriptTag({path:modulePath});await page.addStyleTag({path:path.join(web,'tool-trail.css')});await page.addStyleTag({content:'main{max-width:760px;margin:0 auto}details{min-width:0}.tool-list{min-width:0}'});return {page,requests};}
test('Inspected-frame metadata admits bounded receipts and strips unrelated authority',async()=>{
 const {page:p}=await pageFor();try{
  const result=await p.evaluate(value=>RocotoToolTrail.normalizeFramePreviews(value),[{...frame(),label:'Model-written name',authenticated:true,download_url:'https://elsewhere.invalid/private'}]);
  assert.deepEqual(result,[frame()]);const boundary={...frame(),clip_id:'x'.repeat(256),time:1e12,width:65535,height:65535,basis:'🖼'.repeat(128)};assert.deepEqual(await p.evaluate(value=>RocotoToolTrail.normalizeFramePreviews([value]),boundary),[boundary]);assert.deepEqual(await p.evaluate(value=>RocotoToolTrail.normalizeFramePreviews(value),[frame(),frame()]),[frame()]);
  for(const bad of [null,{},[...Array(5)].map((_,i)=>frame(i%4))])assert.deepEqual(await p.evaluate(value=>RocotoToolTrail.normalizeFramePreviews(value),bad),[]);
  const invalid=[{url:frame().url+'\n'},{sha256:'b'.repeat(64)+'\n'},{clip_id:'source-clip\n'},{url:'https://elsewhere.invalid'+frame().url},{url:'https://fixture.invalid'+frame().url},{url:frame().url+'?token=x'},{url:frame().url+'#fragment'},{url:frame().url.replace('/0.jpg','/4.jpg')},{url:frame().url.replace('/0.jpg','/../0.jpg')},{url:frame().url.replace('/0.jpg','/%30.jpg')},{url:frame().url.replace('a'.repeat(32),'A'.repeat(32))},{sha256:''},{sha256:'g'.repeat(64)},{time:-1},{time:'1.25'},{clip_id:''},{clip_id:'source\nclip'},{basis:'x'.repeat(129)},{basis:''},{basis:'\ud800'},{clip_id:'.'},{clip_id:'..'},{clip_id:'bad/clip'},{clip_id:'x'.repeat(257)},{time:1e12+1},{height:65536},{width:0},{height:1.5}];
  for(const override of invalid)assert.deepEqual(await p.evaluate(value=>RocotoToolTrail.normalizeFramePreviews([value]),{...frame(),...override}),[],JSON.stringify(override));
  assert.deepEqual(await p.evaluate(value=>RocotoToolTrail.normalizeFramePreviews([{...value,time:Infinity},{...value,time:NaN}]),frame()),[]);
 }finally{await p.close();}
});
test('Inspected frames stay in one accessible row and retain exact framing during a live turn',async()=>{
 for(const width of [1600,390]){const {page:p,requests}=await pageFor(width);try{
  await p.evaluate(frames=>{
   const icon=()=>{const e=document.createElement('span');e.className='icon';return e;},renderTool=tool=>{const d=document.createElement('details'),s=document.createElement('summary'),label=document.createElement('span');d.dataset.toolId=tool.id;label.textContent='Viewed source';s.append(label);d.append(s);d.open=true;return d;};
   window.opened=[];window.frames=frames;window.renderFixture=(actual=true,pending=true)=>document.querySelector('#target').replaceChildren(RocotoToolTrail.render([{id:'inspection',name:'inspect_clip',status:'complete',frame_previews:actual?frames:[],arguments:{clip_id:'source-clip-1'}}],{pending,icon,renderTool,resolveMedia:()=>actual?frames.map(f=>({label:`Snapshot · ${f.time} in source`,thumbnailUrl:f.url,open:()=>window.opened.push(f.clip_id)})):[{label:'Source clip thumbnail',thumbnailUrl:'/thumbnails/owned.jpg'}]}));window.renderFixture();
  },[frame(0),frame(1),frame(2)]);
  const row=p.getByLabel('Inspected frames',{exact:true});assert.equal(await row.isVisible(),true);assert.equal(await row.locator('img').count(),3);assert.equal(await p.locator('#target > .tool-trail').isVisible(),true);
  assert.deepEqual(await row.locator('img').evaluateAll(images=>images.map(img=>({fit:img.style.objectFit,referrer:img.referrerPolicy}))),Array(3).fill({fit:'contain',referrer:'no-referrer'}));
  const boxes=await row.locator('.tool-trail-media-item').evaluateAll(items=>items.map(item=>item.getBoundingClientRect().top));assert.equal(new Set(boxes).size,1);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  if(width===390)assert.equal(await row.evaluate(el=>el.scrollWidth>el.clientWidth),true);
  await row.getByRole('button',{name:'Snapshot · 0.125 in source',exact:true}).click();assert.deepEqual(await p.evaluate(()=>window.opened),['source-clip-1']);
  await p.evaluate(()=>window.renderFixture(false,false));await p.getByLabel('Tool source previews',{exact:true}).waitFor();assert.equal(await p.locator('.tool-trail-inspection-frames').count(),0);assert.equal(await p.locator('.tool-trail-media img').count(),1);
  assert(requests.every(url=>url.startsWith('https://fixture.invalid/')));
 }finally{await p.close();}}
});
test('The trail rejects external and malformed inspection thumbnail destinations',async()=>{
 const {page:p,requests}=await pageFor();try{
  await p.evaluate(frame=>{const icon=()=>document.createElement('span'),renderTool=()=>{const d=document.createElement('details'),s=document.createElement('summary');s.textContent='Frames';d.append(s);d.open=true;return d;};const invalid=[`https://elsewhere.invalid${frame.url}`,frame.url+'?token=hidden',frame.url+'#x',frame.url.replace('/0.jpg','/4.jpg'),frame.url.replace('/0.jpg','/%30.jpg')];document.querySelector('#target').replaceChildren(RocotoToolTrail.render([{id:'frame',name:'inspect_clip',status:'complete',frame_previews:[frame]}],{icon,renderTool,resolveMedia:()=>invalid.map(thumbnailUrl=>({thumbnailUrl,label:'Rejected'}))}));},frame());
  assert.equal(await p.locator('.tool-trail-media img').count(),0);assert.deepEqual(requests,[]);await p.evaluate(frame=>{const renderTool=()=>{const d=document.createElement('details');d.open=true;d.append(document.createElement('summary'));return d;};document.querySelector('#target').replaceChildren(RocotoToolTrail.render([{name:'run_command',status:'complete',frame_previews:[frame]}],{renderTool,icon:()=>document.createElement('span'),resolveMedia:()=>[{thumbnailUrl:frame.url,label:'Unrelated tool'}]}));},frame());assert.equal(await p.locator('.tool-trail-media img').count(),0);
 }finally{await p.close();}
});
