/* Real profile renderers with controlled saved-artwork responses. */
const {test,before,after}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
const base=path.resolve(__dirname,'..'),web=require('./web_fixture.cjs').web(),theme=JSON.parse(fs.readFileSync(path.join(base,'theme.json')));
let server,browser,origin;
const businessCover={id:'cover-'+'a'.repeat(32),url:'/api/profile-covers/cover-'+'a'.repeat(32),target:'business',target_id:'cafe'};
const memberCover={id:'cover-'+'b'.repeat(32),url:'/api/profile-covers/cover-'+'b'.repeat(32),target:'member',target_id:'alice'};
before(async()=>{
  server=http.createServer((req,res)=>{
    const route=new URL(req.url,'http://localhost').pathname;
    if(route==='/'){res.setHeader('content-type','text/html');res.end('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/business-summary.css"><link rel="stylesheet" href="/messaging-settings.css"><script defer src="/business-summary.js"></script><script defer src="/messaging-settings.js"></script><main id="root"></main>');return;}
    if(route==='/theme.css'){res.setHeader('content-type','text/css');res.end(':root{'+Object.entries({...theme.palette,...theme.typography,radius:'12px','radius-sm':'8px','radius-lg':'20px'}).map(([k,v])=>'--'+k+':'+v+';').join('')+'}*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--paper);font:var(--font-size-ui)/1.5 var(--font);color:var(--ink)}button,input{font:inherit}#root{max-width:1000px;margin:auto}');return;}
    const relative=route.startsWith('/api/profile-covers/')?'/assets/chat-dreamscape.png':route;
    const file=path.resolve(web,'.'+relative);
    if(!file.startsWith(web+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
    res.setHeader('content-type',relative.endsWith('.js')?'text/javascript':relative.endsWith('.png')?'image/png':'text/css');fs.createReadStream(file).pipe(res);
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));origin='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||'/opt/google/chrome/chrome',args:['--no-sandbox']});
});
after(async()=>{await browser?.close();await new Promise(r=>server?.close(r));});
async function render(page){
  await page.waitForFunction(()=>window.RocotoBusinessSummary&&window.RocotoMessaging);
  await page.evaluate(({businessCover,memberCover})=>{
    const node=(tag,cls='',text)=>{const el=document.createElement(tag);el.className=cls;if(text!==undefined)el.textContent=text;return el;};
    const helpers={node,icon:()=>node('span'),safeUrl:()=>null};
    document.querySelector('#root').replaceChildren(RocotoBusinessSummary.renderProfile({profile:{name:'Artwork browser fixture',type:'Café',description:'Controlled profile render fixture'},cover:businessCover},helpers));
    window.openMember=()=>window.dialog=RocotoMessaging.openMember({member:{id:'alice',display_name:'Profile fixture',role:'owner',cover:memberCover},data:{principal_id:'alice',environment_id:'company-a',collection:'cafe'}});
  },{businessCover,memberCover});
}
test('saved business/member covers render after reload and fit desktop/mobile',async()=>{
  const out='/tmp/rocoto-profile-covers';fs.mkdirSync(out,{recursive:true});
  for(const width of [1600,390]){
    const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage();
    try{
      await page.route('**/*',route=>route.request().url().startsWith(origin+'/')?route.continue():route.abort());
      await page.goto(origin);await render(page);
      for(let pass=0;pass<2;pass++){
        const image=page.locator('.business-profile-cover-image');await image.waitFor();await image.evaluate(img=>img.decode());
        assert.equal(await image.getAttribute('src'),businessCover.url);
        assert.equal(await page.locator('.business-profile').evaluate(el=>el.firstElementChild.className),'business-profile-cover');
        assert.equal(await page.locator('body').evaluate(el=>el.scrollWidth<=innerWidth),true);
        if(!pass){await page.reload();await render(page);}
      }
      await page.screenshot({path:path.join(out,`business-${width}.png`),fullPage:true});
      await page.evaluate(()=>openMember());
      const person=page.locator('.messaging-profile-cover-image');await person.evaluate(img=>img.decode());assert.equal(await person.getAttribute('src'),memberCover.url);
      const box=await page.locator('dialog').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width);
      await page.screenshot({path:path.join(out,`member-${width}.png`),fullPage:true});
      await page.evaluate(()=>dialog.update({principal_id:'alice',environment_id:'company-a',collection:'cafe',members:[{id:'alice',cover:null}]}));
      assert.equal(await person.count(),0);
      await page.evaluate(()=>dialog.update({principal_id:'alice',environment_id:'company-b',collection:'cafe'}));
      assert.equal(await person.count(),0);
    }finally{await context.close();}
  }
});
test('external and another member artwork references stay inert',async()=>{
  const context=await browser.newContext(),page=await context.newPage();
  try{
    await page.goto(origin);await render(page);
    await page.evaluate(()=>{
      window.dialog=RocotoMessaging.openMember({member:{id:'alice',cover:{url:'https://example.org/tracking.png',target:'member',target_id:'alice'}},data:{principal_id:'alice'}});
    });
    assert.equal(await page.locator('.messaging-profile-cover-image').count(),0);
    await page.evaluate(()=>{dialog.close();window.dialog=RocotoMessaging.openMember({member:{id:'alice',cover:{url:'/api/profile-covers/cover-'+'b'.repeat(32),target:'member',target_id:'bob'}},data:{principal_id:'alice'}});});
    assert.equal(await page.locator('.messaging-profile-cover-image').count(),0);
  }finally{await context.close();}
});
