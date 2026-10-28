/* Exercise the real shared contact card with a vertical's exact channel selection. */
const {test}=require('node:test'),assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require('playwright');
for(const width of [1600,390])test(`selected email and voice enrollment at ${width}`,async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width,height:900}});
  await page.setContent('<html><body><button id="profile">Profile</button></body></html>');
  await page.addStyleTag({content:':root{--font:Arial;--ink:#222;--muted:#666;--surface:#fff;--accent:#d23;--radius:12px}body{margin:0;font:16px Arial}dialog{max-width:calc(100vw - 40px)}'});
  await page.addStyleTag({path:path.resolve(__dirname,'../web/messaging-settings.css')});
  await page.addScriptTag({path:path.resolve(__dirname,'../web/messaging-settings.js')});
  async function open(channel){await page.evaluate(channel=>{
   window.sent=[];
   window.RocotoMessaging.openMember({member:{id:'alice',name:'Alice',is_self:true},page:'channel',channel,
    data:{principal_id:'alice',provider:channel,channels:[{channel,ready:true}],readiness:{ready:true,otp_ready:true}},
    onRequestCode:async body=>{window.sent.push(body);return{id:'challenge-fixture',phone:body.phone,status:'sent',expires_at:Date.now()/1000+300,attempts_remaining:3}}});
  },channel)}
  await open('email');
  assert.equal(await page.getByRole('radio').count(),1);
  await page.getByLabel('Email address',{exact:true}).fill('alice@example.test');
  await page.getByRole('button',{name:'Send security code',exact:true}).click();
  await page.getByLabel('Security code',{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>window.sent),[{phone:'alice@example.test',member_id:'alice',channel:'email'}]);
  assert.equal(await page.getByLabel('Country code',{exact:true}).count(),0);
  await page.getByRole('button',{name:'Close profile',exact:true}).click();
  await open('voice');
  assert.equal(await page.getByRole('radio').count(),1);
  await page.getByLabel('Country code',{exact:true}).fill('+34');
  await page.getByLabel('Phone number',{exact:true}).fill('612 345 678');
  await page.getByRole('button',{name:'Send security code',exact:true}).click();
  await page.getByLabel('Security code',{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>window.sent),[{phone:'+34612345678',member_id:'alice',channel:'voice'}]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 }finally{await browser.close()}
});
