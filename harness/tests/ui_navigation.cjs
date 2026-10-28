/* Free browser regressions. All API traffic is fixture-owned; no model/provider calls.
 * NODE_PATH=/path/to/playwright/node_modules PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium \
 *   node --test harness/tests/ui_navigation.cjs
 */
const {test,before,after}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
const web=process.env.ROCOTO_UI_TEST_WEB || require('./web_fixture.cjs').web(),pub=process.env.ROCOTO_UI_TEST_PUBLIC || path.resolve(__dirname, '../../app/public');
let browser,server,origin;
const retailHarness=JSON.parse(require('node:child_process').execFileSync(process.env.ROCOTO_UI_TEST_PYTHON || 'python3',['-c','import layout; layout.install(); import pack,json; print(json.dumps(pack.load("retail").shell()))'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},encoding:'utf8'}));
const upstreamCatalogue=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../runtime/plugin_catalogue.json')));const theme=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../theme.json'))),palette=theme.palette || {};
const skill={id:'source-review',name:'Source Review',description:'Review source evidence.',instructions:'Inspect supplied source frames.',revision:1,installed:true,installed_revision:1,latest_revision:1,required_connections:[],publisher:'Rocoto'};
const related={...skill,id:'shopify-orders',name:'Shopify Orders',description:'Review linked order records.',installed:false,required_connections:[{provider:'shopify',scopes:['read:orders']}]};
const chat={id:'fixture-chat',title:'Fixture café review',kind:'chat',dataset:'cafe',owner_principal_id:'owner',pinned:false,archived:false,metadata_revision:0,context:{used_tokens:12345,window_tokens:1000000},messages:[{role:'user',content:'Describe this source.'},{role:'assistant',content:'Fixture saved response.'}],running:false};
before(async()=>{
 server=http.createServer((req,res)=>{const route=new URL(req.url,'http://localhost').pathname; if(route==='/theme.css'||route==='/static/harness/theme.css'){res.setHeader('Content-Type','text/css');res.end(':root{--fallback:0;--accent:#de0637;--font:Arial,sans-serif;--wordmark-font:Fraunces,serif;--muted:#777;--border:#ddd;--surface:#f7f7f7;--radius:.75rem;--radius-sm:.5rem;--radius-lg:1.25rem;--connection-online:green;'+Object.entries({...palette,...theme.typography}).map(([k,v])=>'--'+k+':'+v+';').join('')+'}');return;}const file=path.resolve(route.startsWith('/brand/')||route.startsWith('/connection-brands/')||route.startsWith('/fonts/')?pub:web,'.'+(route==='/'?'/index.html':route));if(![web,pub].some(root=>file.startsWith(root+path.sep))||!fs.existsSync(file)){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',({'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml','.png':'image/png'})[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res);});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin='http://127.0.0.1:'+server.address().port;
 browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox']});
});
after(async()=>{await browser?.close();await new Promise(resolve=>server?.close(resolve));});

test('Connection access selection is explicit and follows host readiness',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width),p=h.page,submitted=[];let ready=false;
  try{
   await p.route('**/api/connections',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({connections:[{provider:'notion',id:'notion',name:'Notion',authentication:'oauth',status:'disconnected',available:true,access_mode:'read',access_modes:['read','read_write'],write_available:ready}]})}));
   await p.route('**/api/connections/notion/connect',route=>{submitted.push(route.request().postDataJSON());return route.fulfill({contentType:'application/json',body:JSON.stringify({flow_id:'fixture-mode',status:'awaiting_authorization',authorization_url:'https://mcp.notion.com/authorize'})});});
   await p.reload();await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();await p.getByRole('button',{name:'Connect account',exact:true}).click();
   const choice=p.getByLabel('Account access',{exact:true});await choice.waitFor();assert.equal(await choice.inputValue(),'read');assert.equal(await choice.locator('option[value="read_write"]').evaluate(option=>option.disabled),true);assert.deepEqual(submitted,[]);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`connection-access-${width}.png`)});
   await p.keyboard.press('Escape');ready=true;await p.reload();await p.getByRole('button',{name:'Connect account',exact:true}).click();await choice.selectOption('read_write');await p.locator('#connection-setup-dialog').getByRole('button',{name:'Connect',exact:true}).click();
   await p.getByRole('link',{name:'Authorize account',exact:true}).waitFor();assert.deepEqual(submitted,[{access_mode:'read_write'}]);assert.deepEqual(h.errors,[]);
  }finally{await h.close();}
 }
});
async function pageFor(width=1600, options={}){const context=await browser.newContext({viewport:{width,height:width===390?844:1000}}),page=await context.newPage(),writes=[],errors=[],payloads=[];let observer=false,customPolls=0;const agentRecords=structuredClone(options.agents||[]).map(agent=>({...agent,chat_id:agent.chat_id||'agent-chat-'+agent.id})),agentChats=agentRecords.map(agent=>options.agentConversations?.find(item=>item.id===agent.chat_id)||{...chat,id:agent.chat_id,title:agent.title,kind:'agent_conversation',agent_id:agent.id,agent_setup:{agent_id:agent.id,mode:agent.mode},dataset:agent.dataset,messages:[{role:'user',content:agent.instruction||'Review this collection.'},{role:'assistant',content:'Agent conversation ready.'}]});const fileRecords=structuredClone(options.files || []),openedFiles=[];const customConnections=structuredClone(options.customConnectors || []);page.route('https://raw.githubusercontent.com/**',route=>process.env.ROCOTO_UI_REAL_LOGOS==='1'?route.continue():route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="gray"/></svg>'}));page.setDefaultTimeout(5000);page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',route=>{const r=route.request(),p=new URL(r.url()).pathname;let data={};if(p.startsWith('/api/files/')&&p.endsWith('/opened')&&r.method()==='POST'){assert.equal(r.headers()['x-rocoto-page'],'1');const record=fileRecords.find(file=>p==='/api/files/'+file.id+'/opened');assert(record);openedFiles.push(record.id);record.last_opened_at='2026-10-05T12:00:00Z';return route.fulfill({contentType:'application/json',body:JSON.stringify(record)});}if(p.startsWith('/api/files/')&&r.method()==='PATCH'){assert.equal(r.headers()['x-rocoto-page'],'1');const record=fileRecords.find(file=>p==='/api/files/'+file.id);assert(record);record.title=r.postDataJSON().title;return route.fulfill({contentType:'application/json',body:JSON.stringify(record)});}if(!['GET','HEAD'].includes(r.method())){writes.push(p);payloads.push({path:p,body:r.postDataJSON()});assert(options.agentUpdates && /^\/api\/agents\/[A-Za-z0-9_-]+\/update$/.test(p) || options.cameraReview && p==='/api/retail/cameras/review' || p==='/api/observer' || options.chatEvents && p==='/api/chat' || options.credentialReply && p.includes('/credential-requests/') || options.questionReply && p.includes('/questions/') || options.approvalReply && p.includes('/approvals/') || options.customConnectors && (p.startsWith('/api/connections/custom') || p.startsWith('/api/retail/storage/')),'Unexpected mutation during UI test');if(p==='/api/observer')observer=r.method()==='POST';}
 const principal=observer?'customer':'owner';const record={...chat,...options.chat,owner_principal_id:principal,read_only:observer,title:observer?'Customer review':options.chat?.title || chat.title};
 if(p.includes('/approvals/')&&r.method()==='POST'){assert.deepEqual(r.postDataJSON(),options.approvalReply);const item=record.messages.flatMap(message=>message.native_approvals || []).find(item=>p.endsWith('/'+item.id));assert(item);return new Promise(resolve=>setTimeout(()=>{item.status=options.approvalConflict?'cancelled':options.approvalReply.action==='allow_once'?'approved':'declined';resolve(route.fulfill({status:options.approvalConflict?409:200,contentType:'application/json',body:JSON.stringify({request:item})}));},options.approvalDelay || 0));}
 if(p.includes('/questions/')&&r.method()==='POST'){assert.deepEqual(r.postDataJSON(),options.questionReply);const item=(record.messages.flatMap(message=>message.native_questions || [])).find(item=>p.endsWith('/'+item.id));assert(item);item.status=options.questionConflict?'interrupted':options.questionReply.action==='submit'?'submitted':options.questionReply.action==='timeout'?'timed_out':'skipped';return new Promise(resolve=>setTimeout(()=>resolve(route.fulfill({status:options.questionConflict?409:200,contentType:'application/json',body:JSON.stringify({request:item})})),options.questionDelay || 0));}
 if(p.includes('/credential-requests/')&&r.method()==='POST'){assert.deepEqual(r.postDataJSON(),options.credentialReply);const requests=(options.chat?.messages || []).flatMap(message=>message.credential_requests || []),item=requests.find(item=>p.endsWith('/'+item.id));assert(item);item.status=options.credentialConflict?'cancelled':options.credentialReply.action==='submit'?'submitted':'skipped';if(options.credentialConflict)item.resolution_reason='connector_changed';if(item.status==='skipped')item.skip_reason=options.credentialReply.reason;return route.fulfill({status:options.credentialConflict?409:200,contentType:'application/json',body:JSON.stringify(options.credentialConflict?{detail:'Connector setup changed.',request:item}:item)});}
 if(p==='/api/chat'&&options.chatEvents)return route.fulfill({contentType:'application/x-ndjson',body:options.chatEvents.map(event=>JSON.stringify(event)).join('\n')+'\n'});
 if(p==='/api/session'||p==='/api/observer')data={authenticated:true,demo_browsing:false,can_query:!observer,read_only:observer,role:options.role || 'owner',principal:{id:'owner',display_name:'Fixture Owner'},effective_principal_id:principal,environment:options.environment || {id:'demo',name:'Demo company'},environments:options.environments || [{id:'demo',name:'Demo company',category:'demo',collections:[{id:'cafe',name:'Café'},{id:'warehouse',name:'Warehouse'}]}],viewing_as:observer?{principal:{id:'customer',display_name:'Customer'},environment:{id:'demo',name:'Demo company'},read_only:true}:null};
 else if(p==='/api/retail/clip/fixture-clip')data={metadata:{id:'fixture-clip',dataset:'cafe',camera:'Front counter',start:0,end:8},descriptions:[]}; else if(p==='/api/observers')data={principals:[{id:'customer',display_name:'Customer',environments:[{id:'demo',name:'Demo company',role:'operator'}]}]};
 else if(p==='/api/status')data={demo_guidance:options.guidance || {cafe:{name:'Fixture Café',questions:[{label:'Review café service',prompt:'Inspect the counter and cite a moment.'}]},warehouse:{name:'Fixture Warehouse',questions:[{label:'Review warehouse routes',prompt:'Inspect routes near stored materials.'}]}},agent:{authenticated:true,active_chats:options.chat?.running?{'fixture-chat':true}:{}},corpus:options.corpus || [{video_id:'fixture-source',dataset:'cafe',duration:8},{video_id:'fixture-warehouse',dataset:'warehouse',duration:8}],connection:{host:'fixture-host',managed_authenticated:true,check_status:'authenticated',checked_at:new Date().toISOString()}};
 else if(p==='/api/chats')data={chats:[record,...(observer?[]:agentChats),...(options.children || []),...customConnections.map(item=>({...record,id:item.chat_id,title:item.name+' setup',connector_id:item.id,running:Boolean(options.backgroundBuilder),messages:[{role:'assistant',content:options.backgroundBuilder?'':'Let’s prepare this connector.',progress:options.backgroundBuilder?[{id:'setup',text:'Starting connector setup.'}]:[]}]}))]};else if(p.startsWith('/api/chats/'))data=(!observer&&agentChats.find(item=>p.endsWith('/'+item.id))) || (options.children || []).find(item=>p.endsWith('/'+item.id)) || (customConnections.some(item=>p.endsWith('/'+item.chat_id))?{...record,id:customConnections.find(item=>p.endsWith('/'+item.chat_id)).chat_id,title:'Connector setup',running:options.backgroundBuilder&&++customPolls<2,messages:[{role:'assistant',content:options.backgroundBuilder?customPolls>=2?'Connector package prepared.':'':'Let’s prepare this connector.',progress:options.backgroundBuilder?[{id:'setup',text:'Preparing connector adapter.'}]:[]}]}:record);
 else if(p==='/api/connections/custom'){if(r.method()==='POST'){if(options.cameraSetup){const body=r.postDataJSON();assert.equal(body.collection,options.cameraSetup.collection);assert.equal(body.kind,'api');assert.equal(body.name,options.cameraSetup.name);assert(body.description.includes(options.cameraSetup.reference));assert(body.description.includes('secure credential form'));}else assert.deepEqual(r.postDataJSON(),options.expectedCustomCreate);const item={id:'custom-new',...r.postDataJSON(),status:'draft',summary:r.postDataJSON().description,chat_id:'custom-builder-chat',chat_url:'/?chat=custom-builder-chat',secret_fields:[],files:[]};customConnections.push(item);data=item;}else data={connections:observer&&options.privateCustom?[]:customConnections};}
 else if(p.startsWith('/api/connections/custom/')){const parts=p.split('/'),item=customConnections.find(item=>item.id===parts[4]);assert(item);if(parts[5]==='execution'){const setup=item.execution_setup;assert(setup);if(parts[6]==='target'){assert.deepEqual(r.postDataJSON(),{proposal_id:setup.proposal.id});setup.target=setup.proposal.target;setup.proposal.current=false;setup.target_generation++;}else if(parts[6]==='test'){assert.deepEqual(r.postDataJSON(),{expected_revision:item.revision});setup.last_test={id:'test-browser',status:'passed',revision:item.revision};}else if(parts[6]==='activate'){assert.deepEqual(r.postDataJSON(),{test_id:'test-browser',expected_active_generation:0});setup.active_status=item.active_status='active';setup.active_revision=item.active_revision=item.revision;setup.active_generation++;}else if(parts[6]==='revoke'){assert.deepEqual(r.postDataJSON(),{});setup.active_status=item.active_status='revoked';}data=setup;}else if(parts[5]==='secrets'){const field=item.secret_fields.find(field=>field.name===(r.method()==='DELETE'?parts[6]:r.postDataJSON().name));assert(field);if(r.method()==='POST')assert.equal(r.postDataJSON().value,'fixture-secret-value');field.configured=r.method()==='POST';data={name:field.name,configured:field.configured,updated_at:'2026-10-04T12:00:00Z'};}else data=item;}
 else if(p==='/api/plugins/installed')data={plugins:options.nativePlugins || [],available:options.pluginsAvailable!==false,installable_ids:options.installablePlugins || ['notion']}; else if(p==='/api/plugins/catalogue')data=upstreamCatalogue;else if(p==='/api/connections')data={connections:options.connections || [{id:'openai-docs',provider:'openai-docs',name:'OpenAI Docs',description:'Search OpenAI documentation.',status:'disconnected',authentication:'anonymous',read_only:true}]}; else if(p==='/api/files')data={files:fileRecords}; else if(p.startsWith('/api/files/')&&p.endsWith('/content')){const file=fileRecords.find(file=>p==='/api/files/'+file.id+'/content');data={file,content:Object.hasOwn(options,'fileContent')?options.fileContent:'# Review\n\nA useful business report.'};} else if(p==='/api/skills/catalogue')data={skills:[skill,related]};else if(p==='/api/skills')data={skills:[skill]};
 else if(p==='/api/business/map')data=typeof options.businessMap==='function'?options.businessMap(new URL(r.url())):options.businessMap||{status:'empty',map:null,data:null};
 else if(p==='/api/business')data=options.business || {};
 else if(p==='/api/usage')data={company:{available_usd:'30',prepaid_funded_usd:'40',reserved_usd:'0',monthly_used_usd:'10',monthly_limit_usd:'40'},context:chat.context,codex:{route:'OpenAI API',limits:[]},storage:{},billing:{status:'unconfigured'}};
 else if(p==='/api/retail/cameras/review'){assert.deepEqual(r.postDataJSON(),{collection:'cafe',camera_ids:['camera-1','camera-2','camera-3','camera-4']});data={...record,id:'camera-review',title:'Camera review',messages:[]};} else if(p==='/api/retail/cameras')data={cameras:Array.from({length:5},(_,i)=>({id:'camera-'+(i+1),name:'View '+(i+1),collection:'cafe',asset_ids:[],clip_count:1}))}; else if(p==='/api/workspace/capabilities')data=options.capabilitiesByDataset?.[new URL(r.url()).searchParams.get('dataset')] || options.capabilities || {recurring_agents:{configurable:true,ready:false,notice:'Configure your agent here. Connect a live camera feed to start scheduled runs.'}}; else if(p==='/api/agents')data={agents:observer?[]:agentRecords,capabilities:options.capabilities || {recurring_agents:{configurable:true,ready:false,notice:'Configure your agent here. Connect a live camera feed to start scheduled runs.'}}}; else if(p.startsWith('/api/agents/')&&p!=='/api/agents/catalogue'){const parts=p.split('/'),agent=!observer&&agentRecords.find(item=>item.id===parts[3]);if(!agent)return route.fulfill({status:404,contentType:'application/json',body:'{}'});if(r.method()==='POST'){const body=r.postDataJSON();if(parts[4]==='update'){assert.equal(body.expected_revision,agent.revision);Object.assign(agent,body,{revision:agent.revision+1,status:'draft'});delete agent.expected_revision;}else if(parts[4]==='pause')agent.status='paused';else if(parts[4]==='activate')agent.status='active';}data=agent;}else if(p==='/api/agents/catalogue')data={templates:[{id:'service-review',name:'Review service',description:'Review service opportunities.',industry:'Café',use_case:'Service',mode:'ad_hoc',instructions:'Review supplied footage and cite evidence.',watch_for:['People waiting'],required_input:['Select footage']}]};
 if(p==='/api/connections'&&options.holdConnections)return options.holdConnections(route,data);
 if(p==='/api/status'&&options.statusDelay)return new Promise(resolve=>setTimeout(()=>resolve(route.fulfill({contentType:'application/json',body:JSON.stringify(data)})),options.statusDelay));
 return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});});
 await page.goto(origin+'/?chat=fixture-chat');if(options.chat?.messages?.length===0)await page.locator('#welcome').waitFor();else await page.locator('#messages .message').first().waitFor();
 return {page,writes,payloads,errors,openedFiles,fileRecords,close:async()=>{assert.deepEqual(errors,[]);await context.close();}};
}
async function openSettingsPage(page,view){if(!await page.locator('#settings-nav').isVisible()){await page.locator('#account-button').click();await page.locator('#open-settings').click();}await page.locator('#settings-'+view).click();}
test('Home stays selected while a saved conversation is open',async()=>{const h=await pageFor();try{assert.equal(await h.page.locator('#new-chat').getAttribute('aria-current'),'page');assert.equal(await h.page.locator('#rail-chats').isVisible(),false);}finally{await h.close();}});
test('New chat reveals the composer immediately on mobile and preserves the previous draft',async()=>{
 for(const width of [390,1600]){
  const h=await pageFor(width),p=h.page;
  try{
   await p.locator('#message').fill('Keep this draft in the saved conversation.');
   await p.locator('#new-chat').click();await p.locator('body.empty-chat').waitFor();
   assert.equal(await p.locator('body.chats-open').count(),0,'New chat opens its composer, without a chat-list overlay');
   assert.equal(await p.locator('#rail-chats').getAttribute('aria-expanded'),'false');
   assert.equal(await p.locator('#chat-list').isVisible(),width===1600);
   assert.equal(await p.locator('#message').inputValue(),'');
   assert.equal(await p.evaluate(()=>document.activeElement.id),'message');
   assert.equal(await p.locator('#message').evaluate(el=>{const r=el.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el;}),true,'Composer receives input without dismissing another surface');
   await p.locator('#workspace-backdrop').evaluate(async el=>{const match=getComputedStyle(el).backgroundImage.match(/url\(["']?(.*?)["']?\)/);if(!match)throw Error('Missing landing artwork');const image=new Image();image.src=match[1];await image.decode();});
   if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`new-chat-revealed-${width}.png`)});}
   await p.goto(origin+'/?chat=fixture-chat');await p.locator('#messages .message').first().waitFor();
   assert.equal(await p.locator('#message').inputValue(),'Keep this draft in the saved conversation.');
   assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
 }
});
test('Chat sidebar lists submitted conversations while unsent drafts survive navigation and reload',async()=>{
 const h=await pageFor(1600,{children:[{...chat,id:'empty-server-chat',title:'Empty saved chat',messages:[]},{...chat,id:'assistant-only',title:'Setup introduction',messages:[{role:'assistant',content:'Ready for your setup.'}]}]}),p=h.page;
 try{
  assert.deepEqual(await p.locator('#chat-list .chat-link').allTextContents(),['Fixture café review']);
  assert.equal(await p.getByText('Pin chats to keep them here.',{exact:true}).count(),0);
  for(const button of await p.locator('.chat-section-toggle').all())assert.equal(await button.evaluate(el=>el.lastElementChild?.tagName.toLowerCase()),'svg');
  await p.locator('#add-chat').click();const draftUrl=p.url();await p.locator('#message').fill('Keep this unsent draft private.');assert.equal(await p.locator('#chat-list .chat-link').count(),1);
  await p.locator('#chat-list .chat-link').first().click();await p.goto(draftUrl);await p.locator('#welcome').waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep this unsent draft private.');assert.equal(await p.locator('#chat-list .chat-link').count(),1);
  let held;await p.route('**/api/chat',r=>{held=r;});await p.locator('#send').click();await p.locator('#chat-list .chat-link').filter({hasText:'New chat'}).waitFor();assert(held);
  await held.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'done',status:'completed'})+'\n'});await p.waitForFunction(()=>!document.querySelector('#send').classList.contains('stop'));
 }finally{await h.close();}
});
test('Quick Search and its draft survive completion of held session initialization',async()=>{
 let signal;const held=new Promise(resolve=>{signal=resolve;});
 const h=await pageFor(390,{holdConnections:(route,data)=>{signal({route,data});}}),p=h.page;
 try{
  const pending=await held;await p.keyboard.press('Control+k');await p.locator('body.empty-search').waitFor();
  await p.locator('#message').fill('Keep this source search draft.');const searchUrl=p.url();
  await pending.route.fulfill({contentType:'application/json',body:JSON.stringify(pending.data)});
  await p.waitForLoadState('networkidle');
  assert.equal(await p.locator('body.empty-search').count(),1,'Finishing initial loading preserves the selected Search view');
  assert.equal(await p.locator('#message').inputValue(),'Keep this source search draft.');
  assert.equal(p.url(),searchUrl);assert.equal(await p.locator('#welcome h1').textContent(),'Quick Search');assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});
test('Running native and domain tools use active labels rather than bare tool names',async()=>{
 const cases=[['cosmos_inspect','Inspecting footage'],['mcp__rocoto__inspect_clip','Viewing a clip'],['mcp__provider__custom_action','Running Custom Action']];
 for(const [name,label]of cases){const h=await pageFor(390,{chat:{running:true,messages:[chat.messages[0],{role:'assistant',content:'',tools:[{id:'live-tool',name,status:'running',arguments:{}}],progress:[{id:'live-tool',activity:'tool_progress',text:'Provider progress detail'}]}]}}),p=h.page;try{assert.equal(await p.locator('.current-tool .current-activity').textContent(),label);}finally{await h.close();}}
});
test('Incremental chat titles update their row without overwriting Settings or another owner',async()=>{
 const h=await pageFor(),p=h.page;try{
  await p.route('**/api/profile/activity?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify(profileFixture('organisation'))}));
  await p.evaluate(()=>{const original=window.fetch;window.fetch=(input,options)=>new URL(String(input),location.href).pathname!=='/api/chat'?original(input,options):Promise.resolve(new Response(new ReadableStream({start(controller){window.fixtureTitleEvents=(events,finish=false)=>{for(const event of events)controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));if(finish)controller.close();};}}),{headers:{'Content-Type':'application/x-ndjson'}}));});
  await p.locator('#message').fill('Inspect the queue.');await p.locator('#send').click();await p.waitForFunction(()=>typeof window.fixtureTitleEvents==='function');
  await p.evaluate(()=>window.fixtureTitleEvents([{type:'title',title:'Queue',partial:true}]));await p.locator('#chat-list .chat-link').getByText('Queue',{exact:true}).waitFor();assert.equal(await p.locator('#chat-title').textContent(),'Queue');
  await openSettingsPage(p,'stats');await p.locator('#stats-activity .profile-calendar').first().waitFor();await p.evaluate(()=>window.fixtureTitleEvents([{type:'title',title:'Queue inspection',partial:true}]));await p.waitForFunction(()=>document.querySelector('#chat-list').textContent.includes('Queue inspection'));assert.equal(await p.locator('#chat-title').textContent(),'Stats');
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();
  await p.evaluate(()=>window.fixtureTitleEvents([{type:'title',title:'Private owner final title',partial:false},{type:'done',status:'completed'}],true));await p.waitForTimeout(50);assert.equal(await p.getByText('Private owner final title',{exact:true}).count(),0);assert.equal(await p.locator('#chat-list .chat-link').getByText('Customer review',{exact:true}).count(),1);
 }finally{await h.close();}
});
test('Attachment menu supports keyboard entry, nested Skills and focus restoration',async()=>{const h=await pageFor(390);const p=h.page;try{await p.locator('#attachment-toggle').focus();await p.keyboard.press('ArrowDown');assert.equal(await p.locator('#attachment-menu').isVisible(),true);assert.equal(await p.evaluate(()=>document.activeElement.id),'attach-images');await p.keyboard.press('ArrowDown');await p.keyboard.press('ArrowDown');assert.equal(await p.evaluate(()=>document.activeElement.id),'attach-skills');await p.keyboard.press('ArrowRight');assert.equal(await p.evaluate(()=>document.activeElement.closest('#skills-submenu')!==null),true);const menuBox=await p.locator('#skills-submenu').boundingBox();assert(menuBox.x>=0&&menuBox.x+menuBox.width<=390);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'plus-skills-mobile.png')});await p.keyboard.press('ArrowLeft');assert.equal(await p.evaluate(()=>document.activeElement.id),'attach-skills');await p.keyboard.press('Escape');assert.equal(await p.locator('#attachment-menu').isVisible(),false);assert.equal(await p.evaluate(()=>document.activeElement.id),'attachment-toggle');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Usage hover and keyboard popup reach breakdown and restore focus',async()=>{const h=await pageFor(390);const p=h.page;try{await p.locator('#context-ring').hover();await p.waitForTimeout(650);assert.equal(await p.locator('#context-tooltip').isVisible(),true);await p.locator('#context-ring').focus();await p.keyboard.press('Enter');assert.equal(await p.locator('#usage-popover').isVisible(),true);assert.equal(await p.evaluate(()=>document.activeElement.id),'usage-breakdown');assert.equal(await p.locator('#context-tooltip').isVisible(),false);const box=await p.locator('#usage-popover').boundingBox();assert(box.x>=0&&box.x+box.width<=390);await p.keyboard.press('Escape');assert.equal(await p.evaluate(()=>document.activeElement.id),'context-ring');await p.keyboard.press('Enter');await p.keyboard.press('Enter');await p.locator('#usage-view').waitFor({state:'visible'});assert(new URL(p.url()).searchParams.get('settings')==='usage');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Slash skills and nested connection skills use actual catalogue definitions',async()=>{const h=await pageFor();const p=h.page;try{await p.locator('#message').fill('/source');await p.keyboard.press('Enter');assert.equal(await p.locator('.selected-skill').count(),1);await p.locator('#message').fill('@shop');await p.keyboard.press('ArrowRight');assert((await p.locator('#command-menu').textContent()).includes('/shopify-orders'));await p.keyboard.press('ArrowLeft');assert((await p.locator('#command-menu').textContent()).includes('@Shopify'));await p.keyboard.press('ArrowRight');await p.keyboard.press('Enter');await p.locator('#skill-dialog').waitFor({state:'visible'});assert.equal(await p.locator('#skill-title').textContent(),'Shopify Orders');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Owner observation shows customer history with editing disabled',async()=>{const h=await pageFor(390);const p=h.page;try{await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor({state:'visible'});assert((await p.locator('#viewing-banner').textContent()).includes('Read only'));assert.equal(await p.locator('#message').evaluate(e=>e.readOnly),true);await p.locator('#new-chat').click();assert.equal(await p.locator('#add-chat').isDisabled(),true);assert(await p.locator('.chat-link').filter({hasText:'Customer review'}).count());assert.equal(await p.locator('.chat-link').filter({hasText:'Fixture café review'}).count(),0);assert.equal(await p.locator('.chat-row-actions button:enabled').count(),0);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}});

test('Saved chats keep one mode; Quick Search opens a fresh search workspace',async()=>{const h=await pageFor();const p=h.page;try{assert.equal(await p.locator('#home-mode-switch').isVisible(),false);await p.keyboard.press('Control+k');await p.locator('body.empty-search').waitFor();assert.equal(await p.locator('#home-mode-switch').isVisible(),false);assert.equal(await p.locator('body.empty-search').count(),1);assert.equal(await p.locator('#home-quick-search').getAttribute('aria-pressed'),'true');assert.equal(await p.locator('.message-assistant').count(),0);assert.notEqual(new URL(p.url()).searchParams.get('chat'),'fixture-chat');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Subagent detail is watchable with a return path and no composer',async()=>{const child={...chat,id:'child-1',title:'Queue inspection',parent_id:'fixture-chat',environment_id:'demo',messages:[{role:'assistant',content:'Fixture child evidence.'}]};const h=await pageFor(390,{children:[child],chat:{messages:[...chat.messages,{role:'assistant',content:'Delegated the source review.',subchats:[{chat_id:'child-1',title:'Queue inspection'}]}]}});const p=h.page;try{await p.locator('.completed-turn > summary').first().click();await p.locator('.subagent-group > summary').click();await p.locator('#founder-nav').click();await p.locator('#founder-demo-options').getByRole('button',{name:'Café',exact:true}).click();assert.equal(await p.locator('.subagent-group').evaluate(el=>el.open),true);await p.locator('.subchat-card').click();await p.locator('.child-chat-title').waitFor({state:'visible'});assert.equal(await p.locator('.composer-area').isVisible(),false);assert.equal(await p.locator('#home-mode-switch').isVisible(),false);assert.equal(await p.locator('.child-chat-title .delegate-icon').count(),1);assert.equal(await p.locator('#chat-list').getByText('Queue inspection',{exact:true}).count(),0);await p.getByRole('button',{name:'Return to parent'}).click();await p.locator('.composer-area').waitFor({state:'visible'});assert.equal(new URL(p.url()).searchParams.get('chat'),'fixture-chat');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Running work is inline, updates one lightweight progress row, and keeps completed tools',async()=>{const h=await pageFor(1600,{chat:{running:true,messages:[{role:'user',content:'Inspect the source.'},{role:'assistant',content:'',pending:true,tools:[{name:'search_video',status:'completed',arguments:{query:'queue'},output:'Fixture source candidates'},{name:'inspect_clip',status:'running',arguments:{clip_id:'fixture'}}],progress:[{id:'old',text:'Old lightweight progress.'},{id:'current',text:'Reviewing the source frames.'}]}]}});const p=h.page;try{await p.locator('.live-activity').waitFor();assert.equal(await p.locator('.live-activity').evaluate(el=>Boolean(el.closest('details'))),false);assert.equal(await p.locator('.current-activity').count(),1);assert.equal(await p.locator('.current-activity').textContent(),'Viewing a clip');assert.equal(await p.locator('.assistant-label').count(),0);if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'fixture-active-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'fixture-active-mobile.png')});}assert.equal(await p.getByText('Searched footage',{exact:true}).isVisible(),false);assert((await p.locator('.completed-activity').textContent()).includes('Searched footage'));await p.emulateMedia({reducedMotion:'reduce'});assert.equal(await p.locator('.live-activity .activity-row').first().evaluate(el=>getComputedStyle(el).animationName),'none');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
for(const width of [1600,390])test(`Codex activity uses flat rows and actual theme typography at ${width}`,async()=>{
 const tools=[{id:'search',name:'search_video',status:'complete',arguments:{query:'service'},output:'Found source candidates'},{id:'compact',name:'compact_context',status:'complete',arguments:{},output:'Context compacted'},{id:'command',name:'run_command',status:'running',arguments:{command:'python build_report.py'}}],progress=[{id:'plan',activity:'plan',text:'Review the source and prepare the report.'},{id:'comment',activity:'commentary',text:'I’ll check the source before preparing the file.'}];
 const h=await pageFor(width,{chat:{running:true,messages:[{role:'user',content:'Review the source and make a report.'},{role:'assistant',content:'',pending:true,started_at:'2026-10-05T12:00:00Z',tools,progress} ]}}),p=h.page;
 const c=await pageFor(width,{chat:{messages:[{role:'user',content:'Review the source and make a report.'},{role:'assistant',content:'The report is ready. Use `report.docx`.',elapsed_ms:90500,tools:tools.map(tool=>({...tool,status:'complete',output:tool.output || 'Created report.docx'})),progress}]}}),q=c.page;
 try{
  await p.locator('.current-activity').waitFor();await proofShot(p,`tool-running-${width}`);await q.locator('.completed-turn > summary').click();await proofShot(q,`tool-completed-expanded-${width}`);
  assert.equal(await p.locator('.tool-group').count(),0,'The legacy tool-count card must be removed');assert.equal(await q.locator('.tool-group').count(),0);assert.equal(await p.getByText('Searched footage',{exact:true}).isVisible(),false,'A later minor tool replaces earlier minor calls');assert.equal(await p.getByText('Context automatically compacted',{exact:true}).isVisible(),true);
  assert.equal(await p.locator('.current-activity').textContent(),'Running command');assert.equal(await p.locator('.current-tool > summary').getAttribute('aria-label'),'Running command · Details');assert.equal(await p.locator('.current-tool > summary > .icon').count(),2);
  for(const page of [p,q]){const typography=await page.evaluate(()=>({body:getComputedStyle(document.body).fontSize,font:getComputedStyle(document.body).fontFamily,code:getComputedStyle(document.querySelector('.tool-call pre')).fontSize,codeFont:getComputedStyle(document.querySelector('.tool-call pre')).fontFamily}));assert.equal(typography.body,'14px');assert(typography.font.includes('system-ui'));assert.equal(typography.code,'13px');assert(typography.codeFont.includes('monospace'));const rows=await page.locator('.tool-call').evaluateAll(nodes=>nodes.filter(n=>!n.hidden).map(n=>{const css=getComputedStyle(n);return {border:css.borderTopWidth,background:css.backgroundColor,font:css.fontSize};}));for(const row of rows)assert.deepEqual(row,{border:'0px',background:'rgba(0, 0, 0, 0)',font:'14px'});}
  const shimmer=p.locator('.current-activity');assert.equal(await shimmer.evaluate(el=>getComputedStyle(el).animationName),'activity-glisten-text');const a=await shimmer.evaluate(el=>getComputedStyle(el).backgroundPosition);await p.waitForTimeout(180);assert.notEqual(await shimmer.evaluate(el=>getComputedStyle(el).backgroundPosition),a);await p.locator('.current-tool > summary').focus();await p.keyboard.press('Enter');assert.equal(await p.locator('.current-tool pre').isVisible(),true);
  await p.emulateMedia({reducedMotion:'reduce'});assert.equal(await shimmer.evaluate(el=>getComputedStyle(el).animationName),'none');assert.notEqual(await shimmer.evaluate(el=>getComputedStyle(el).color),'rgba(0, 0, 0, 0)');
  assert.equal(await q.getByText('Searched footage',{exact:true}).isVisible(),true);await q.locator('.tool-call > summary').first().focus();await q.keyboard.press('Enter');assert.equal(await q.getByText('Found source candidates',{exact:true}).isVisible(),true);assert.equal(await q.locator('.tool-call > summary .icon').count(),6);await q.locator('.completed-turn > summary').click();assert.equal(await q.getByText('Searched footage',{exact:true}).isVisible(),false);assert.equal(await q.locator('.assistant-text').isVisible(),true);assert.equal(await q.locator('.activity-shimmer').count(),0);assert.deepEqual(h.writes,[]);assert.deepEqual(c.writes,[]);
 }finally{await h.close();await c.close();}
});

test('Founder menu groups demo choices and changes scope through a fresh chat',async()=>{const h=await pageFor();const p=h.page;try{assert.equal(await p.locator('#workspace-toggle').isVisible(),false);await p.locator('#founder-nav').click();assert.equal(await p.locator('#founder-demo-popover').isVisible(),true);await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')).find(row=>row.id===new URL(location.href).searchParams.get('chat')).dataset),'warehouse');assert.notEqual(new URL(p.url()).searchParams.get('chat'),'fixture-chat');assert.equal(await p.locator('.message-assistant').count(),0);await p.locator('#founder-nav').click();assert.equal(await p.locator('#founder-demo-popover').getByRole('button',{name:'Warehouse',exact:true}).getAttribute('aria-pressed'),'true');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Ongoing agent shows observable criteria and review action with thresholds under details',async()=>{const agent={id:'watch-fixture',title:'Queue review',instruction:'Review waiting customers.',description:'Find growing queues.',mode:'ongoing',dataset:'cafe',status:'draft',max_runs:1,runs_started:0,revision:1,interval_seconds:30,screening_status:'plan_review_required',watch_plan:{families:[{id:'queue',observable:'Customers waiting at the counter',queries:['people queueing'],score_threshold:.2,threshold_basis:'Pilot threshold for review.',verification_question:'Are people waiting for service?'}]}};const h=await pageFor(1600,{agents:[agent]});const p=h.page;try{await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').first().click();await p.locator('#agent-info-button').click();await p.getByRole('button',{name:'Review in chat',exact:true}).waitFor();assert((await p.locator('#agent-info-dialog').textContent()).includes('Are people waiting for service?'));assert.equal(await p.getByText('Candidate score threshold: 0.2. Pilot threshold for review.',{exact:true}).isVisible(),false);await p.locator('.agent-technical > summary').filter({hasText:'Technical details'}).click();assert.equal(await p.getByText('Candidate score threshold: 0.2. Pilot threshold for review.',{exact:true}).isVisible(),true);assert.deepEqual(h.writes,[]);}finally{await h.close();}});

test('Camera batch review requires an explicit bounded selection and sends exact source IDs',async()=>{const h=await pageFor(390,{cameraReview:true});const p=h.page;try{await p.locator('#account-button').click();await p.locator('#open-settings').click();await p.locator('#settings-cameras').click();await p.locator('#review-cameras:enabled').waitFor();await p.locator('#review-cameras').click();const choices=p.locator('.camera-review-options input');for(let i=0;i<4;i++)await choices.nth(i).check();assert.equal(await choices.nth(4).isDisabled(),true);assert.deepEqual(h.writes,[]);await p.getByRole('button',{name:'Review selected cameras',exact:true}).click();assert.deepEqual(h.writes,['/api/retail/cameras/review']);}finally{await h.close();}});

test('Agent catalogue details use their actual criteria and hide raw instructions until opened',async()=>{const h=await pageFor(390);const p=h.page;try{await p.locator('#marketplace-nav').click();await p.locator('#marketplace-nav').click();await p.locator('#customize-agents').click();await p.locator('.marketplace-card').first().click();await p.locator('.catalogue-detail-screen h1').waitFor();assert.equal(await p.locator('.catalogue-detail-screen h1').textContent(),'Review service');assert.equal(await p.locator('dialog[open]').count(),0);assert((await p.locator('.agent-flow').textContent()).includes('People waiting'));assert.equal(await p.locator('.skill-instructions').isVisible(),false);await p.locator('.catalogue-detail-screen summary').filter({hasText:'Agent instructions'}).click();assert.equal(await p.locator('.skill-instructions').isVisible(),true);assert.deepEqual(h.writes,[]);}finally{await h.close();}});

test('Interrupted parent keeps completed subagents discoverable without an assistant response',async()=>{const child={...chat,id:'orphan-child',title:'Completed source review',parent_id:'fixture-chat',environment_id:'demo'};const h=await pageFor(1600,{children:[child],chat:{messages:[{role:'user',content:'Investigate the sources.'}]}});const p=h.page;try{await p.locator('#chat-subagents .subagent-group > summary').click();assert.equal(await p.locator('#chat-subagents .subchat-card').count(),1);assert.equal(await p.locator('#chat-list').getByText(child.title,{exact:true}).count(),0);await p.locator('#chat-subagents .subchat-card').click();assert.equal(await p.locator('.composer-area').isVisible(),false);assert.deepEqual(h.writes,[]);}finally{await h.close();}});

const documentFile={id:'report-1',chat_id:'fixture-chat',dataset:'cafe',name:'service-review.md',title:'Service review',media_type:'text/markdown',size_bytes:56,created_at:'2026-10-04T12:00:00Z',download_url:'/api/files/report-1/download',preview_url:'/api/files/report-1/content',copyable:true,kind:'document',status:'ready'};
test('Request edits exposes and focuses the composer while preserving the desktop split viewer',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width,{files:[documentFile],chat:{messages:[chat.messages[0],{role:'assistant',content:'Created the service review.',artifacts:[documentFile]}]}}),p=h.page;
  try{
   await p.locator('.artifact-card-main').click();await p.locator('#file-preview-content').waitFor();
   await p.getByRole('button',{name:'Request edits',exact:true}).click();
   assert.equal(await p.locator('#chat-files-panel').isVisible(),width===1600);
   assert.equal(await p.locator('#message').inputValue(),'Update the file “service-review.md” (file ID report-1): ');
   assert.equal(await p.locator('#message').evaluate(el=>{const r=el.getBoundingClientRect();return document.activeElement===el&&document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el;}),true);
   await p.keyboard.type('Add a summary.');assert((await p.locator('#message').inputValue()).endsWith('Add a summary.'));
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`request-edits-${width}.png`)});
   assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
 }
});
test('Finished streamed tools retire their current progress while retaining output and concurrent work',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width),p=h.page;
  try{
   await p.evaluate(()=>{const original=window.fetch;window.fetch=(input,options)=>{if(new URL(String(input),location.href).pathname!=='/api/chat')return original(input,options);return Promise.resolve(new Response(new ReadableStream({start(controller){window.fixtureToolEvents=(events,finish=false)=>{for(const event of events)controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));if(finish)controller.close();};}}),{headers:{'Content-Type':'application/x-ndjson'}}));};});
   await p.locator('#message').fill('Review both source files.');await p.locator('#send').click();await p.waitForFunction(()=>Boolean(window.fixtureToolEvents));
   const send=async(events,expected)=>{await p.evaluate(events=>window.fixtureToolEvents(events),events);await p.waitForFunction(expected=>document.querySelector('.current-activity')?.textContent===expected,expected,{timeout:1500});assert.equal(await p.locator('.current-activity').count(),1);};
   await send([{type:'tool_start',id:'first',name:'run_command',arguments:{command:'read first'}},{type:'progress',item_id:'first',activity:'tool_progress',text:'Inspecting the first source'}],'Inspecting the first source');
   await p.locator('.current-tool > summary').click();assert.equal(await p.locator('.current-tool').evaluate(el=>el.open),true);await p.waitForTimeout(180);const shimmerBefore=await p.locator('.current-activity').evaluate(el=>el.getAnimations()[0].effect.getComputedTiming().progress);
   await send([{type:'progress',item_id:'first',activity:'tool_progress',text:' — checking details'}],'Inspecting the first source — checking details');assert.equal(await p.locator('.current-tool').evaluate(el=>el.open),true);const shimmerAfter=await p.locator('.current-activity').evaluate(el=>el.getAnimations()[0].effect.getComputedTiming().progress);assert((shimmerAfter-shimmerBefore+1)%1<.2,'Progress updates preserve shimmer phase');
   await send([{type:'tool_start',id:'second',name:'run_command',arguments:{command:'read second'}},{type:'progress',item_id:'second',activity:'tool_progress',text:'Inspecting the second source'}],'Inspecting the second source');
   assert.equal(await p.locator('.current-tool').evaluate(el=>el.open),false,'Another tool has its own disclosure state');
   await send([{type:'tool_end',id:'second',name:'run_command',success:true,output:'Second source reviewed'}],'Inspecting the first source — checking details');
   await send([{type:'tool_end',id:'first',name:'run_command',success:false,output:'First source needs a different reader'}],'Thinking');
   assert.deepEqual(await p.locator('.tool-output').allTextContents(),['First source needs a different reader','Second source reviewed']);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`tool-progress-finished-${width}.png`)});
   await send([{type:'tool_start',id:'third',name:'read_file',arguments:{file_id:'A'}},{type:'progress',item_id:'third',activity:'tool_progress',text:'Reading the document'}],'Reading the document');
   await p.evaluate(()=>window.fixtureToolEvents([{type:'tool_end',id:'third',name:'read_file',success:true,output:'Document contents'},{type:'delta',text:'The source review is complete.'},{type:'done'}],true));
   await p.getByText('The source review is complete.',{exact:true}).waitFor();await p.locator('.current-activity').waitFor({state:'hidden'});
   assert.deepEqual(await p.locator('.tool-output').allTextContents(),['First source needs a different reader','Second source reviewed','Document contents']);
  }finally{await h.close();}
 }
});
test('Restored running turns exclude ended and uncorrelated tool progress from current activity',async()=>{
 const h=await pageFor(390,{chat:{running:true,messages:[chat.messages[0],{role:'assistant',content:'',pending:true,tools:[{id:'finished',name:'run_command',status:'complete',output:'Saved command output'}],progress:[{id:'finished',activity:'tool_progress',text:'Old command progress'},{id:'unknown',activity:'tool_progress',text:'Uncorrelated progress'}]}]}}),p=h.page;
 try{assert.equal(await p.locator('.current-activity').textContent(),'Thinking');assert.deepEqual(await p.locator('.tool-output').allTextContents(),['Saved command output']);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});
test('Chat files show a safe in-app preview, download and bounded copy',async()=>{const h=await pageFor(1600,{files:[documentFile],fileContent:'# Service review\n\n<script>window.badPreview=true</script>\n\nUseful **observations**.'});const p=h.page;try{await p.context().grantPermissions(['clipboard-read','clipboard-write']);await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list').getByRole('button',{name:/Service review/}).click();await p.locator('#file-preview-content strong').waitFor();assert.equal(await p.evaluate(()=>window.badPreview),undefined);assert.equal(await p.locator('#file-preview-content script').count(),0);assert.equal(await p.locator('#file-download').getAttribute('href'),origin+'/api/files/report-1/download');await p.locator('#file-copy').click();assert((await p.evaluate(()=>navigator.clipboard.readText())).includes('Service review'));assert.equal(await p.locator('#file-copy').textContent(),'Copied');if(process.env.ROCOTO_UI_PROOF_DIR){await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'files-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'files-mobile.png')});}await p.keyboard.press('Escape');assert.equal(await p.locator('#chat-files-panel').isVisible(),false);assert.equal(await p.evaluate(()=>document.activeElement.id),'chat-context-toggle');assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Gallery Files supports grid and list and keeps packages download-only',async()=>{const bundle={...documentFile,id:'package-1',name:'review-package.zip',title:'Review package',media_type:'application/zip',size_bytes:520000,copyable:false,kind:'media_export',download_url:'/api/retail/exports/export-1/download',preview_url:null};const h=await pageFor(390,{files:[documentFile,bundle]});const p=h.page;try{await p.locator('#library-nav').click();await p.keyboard.press('Escape');await p.locator('#workspace-files-tab').click();await p.locator('#generated-files .generated-file').first().waitFor();assert.equal(await p.locator('#generated-files .generated-file').count(),2);await p.locator('#library-view [data-gallery-view=list]').click();assert.equal(await p.locator('body').evaluate(e=>e.classList.contains('gallery-list-view')),true);await p.locator('#generated-files').getByRole('button',{name:/Review package/}).click();assert.equal(await p.locator('#file-copy').count(),0);assert.equal(await p.locator('#file-download').getAttribute('href'),origin+'/api/retail/exports/export-1/download');assert((await p.locator('#file-preview').textContent()).includes('Editing package'));const box=await p.locator('#chat-files-panel').boundingBox();assert(box.x>=0&&box.x+box.width<=390);assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('File preview stays scoped when switching chats and observed workspaces',async()=>{const h=await pageFor(1600,{files:[documentFile]});const p=h.page;try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list').getByRole('button',{name:/Service review/}).click();await p.locator('#file-preview-content').waitFor();await p.locator('#new-chat').click();assert.equal(await p.locator('#file-preview-content').count(),0);await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();assert.equal(await p.locator('#chat-files-panel').isVisible(),false);}finally{await h.close();}});

test('Completed work collapses commentary and tools while keeping final text and artifacts',async()=>{const h=await pageFor(1600,{files:[documentFile],chat:{messages:[chat.messages[0],{role:'assistant',content:'Created the service review.',elapsed_ms:90500,progress:[{id:'comment',activity:'commentary',text:'I will review the source first.'}],tools:[{name:'search_video',status:'complete',output:'Source results'}],artifacts:[documentFile]}]}});const p=h.page;try{assert.equal(await p.locator('.completed-turn > summary').textContent(),'Worked for 1m 30s');assert.equal(await p.getByText('I will review the source first.',{exact:true}).isVisible(),false);assert.equal(await p.getByText('Created the service review.',{exact:true}).isVisible(),true);await p.locator('.artifact-card-main').hover();assert.equal(await p.locator('.artifact-open-preview').isVisible(),true);assert.equal(await p.locator('.artifact-type').isVisible(),false);await p.locator('.artifact-open-menu > summary').click();assert.equal(await p.locator('.artifact-open-actions').getByRole('link',{name:'Download',exact:true}).getAttribute('href'),origin+'/api/files/report-1/download');await p.locator('.artifact-open-menu > summary').click();await p.locator('.artifact-card-main').click();await p.locator('#file-preview-content').waitFor();assert.equal(await p.locator('#chat-files-list').isVisible(),false);assert.equal(await p.getByRole('button',{name:'Request edits',exact:true}).isVisible(),true);if(process.env.ROCOTO_UI_PROOF_DIR){await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'artifact-viewer-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'artifact-viewer-mobile.png')});}await p.keyboard.press('Escape');await p.locator('.completed-turn > summary').click();assert.equal(await p.getByText('I will review the source first.',{exact:true}).isVisible(),true);assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Chat details groups outputs, parent-controlled subagents and sources',async()=>{const child={...chat,id:'context-child',parent_id:'fixture-chat',environment_id:'demo',title:'Service investigation'};const h=await pageFor(1600,{children:[child],chat:{messages:[{...chat.messages[0],clip_attachments:[{id:'fixture-clip',title:'Front counter'}]},{role:'assistant',content:'Created the report.',artifacts:[documentFile]}]}});const p=h.page;try{await p.locator('#chat-context-toggle').click();const popup=p.locator('#chat-context-popup');assert.equal(await popup.getByText('Outputs',{exact:true}).count(),1);assert.equal(await popup.getByText('Subagents',{exact:true}).count(),1);assert.equal(await popup.getByText('Sources',{exact:true}).count(),1);await popup.getByRole('button',{name:'1 done',exact:true}).click();await popup.locator('.subchat-card').click();assert.equal(await p.locator('.composer-area').isVisible(),false);await p.getByRole('button',{name:'Return to parent'}).click();await p.locator('#chat-context-toggle').click();if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'chat-details-desktop.png')});await popup.getByRole('button',{name:'Front counter',exact:true}).click();await p.locator('#clip-dialog').waitFor();assert.equal(await p.locator('#clip-dialog').evaluate(el=>el.matches(':modal')),false);assert.equal(await p.locator('#clip-player').evaluate(el=>getComputedStyle(el).objectFit),'contain');assert.equal(await p.locator('#clip-details-sidebar').isVisible(),false);await p.locator('#clip-metadata-drawer > summary').click();assert.equal(await p.locator('#clip-details-sidebar').isVisible(),true);await p.locator('#close-clip').click();assert.equal(await p.locator('body').evaluate(el=>el.classList.contains('clip-viewer-open')),false);assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Full upstream connections preserve descriptions and details and appear in Plus',async()=>{const h=await pageFor(1600);const p=h.page;try{await p.locator('#attachment-toggle').click();assert.equal(await p.locator('#attachment-connections button').first().textContent(),'Gmail');await p.keyboard.press('Escape');await p.locator('#marketplace-nav').click();await p.locator('.connection-catalogue-item').first().waitFor();assert.equal(await p.locator('.connection-catalogue-item').count(),upstreamCatalogue.plugins.length+1);
 const assertHeadingBounds=async(width)=>{
  const boxes=await p.evaluate(()=>{const view=document.querySelector('#marketplace-view'),edge=view.getBoundingClientRect().left+parseFloat(getComputedStyle(view).paddingLeft),read=id=>{const el=document.querySelector(id),box=el.getBoundingClientRect();return{left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,clipped:el.scrollWidth>el.clientWidth+1};};return{edge,viewOverflow:view.scrollWidth>view.clientWidth+1,viewScroll:view.scrollLeft,title:read('#marketplace-title'),description:read('#marketplace-description'),search:read('#marketplace-search-wrap'),input:read('#marketplace-search'),add:read('#add-custom-connector')};});
  for(const key of ['title','search','input','add']){assert.ok(boxes[key].left>=boxes.edge-1,`${width}px ${key} stays inside the content gutter: ${JSON.stringify(boxes)}`);assert.ok(boxes[key].right<=width-15,`${width}px ${key} stays inside the viewport: ${JSON.stringify(boxes)}`);assert.equal(boxes[key].clipped,false,`${width}px ${key} text fits`);}
  assert.equal(boxes.viewOverflow,false,'Connections content stays inside its scrolling view');assert.equal(boxes.viewScroll,0);assert.ok(boxes.input.width>=160,`${width}px search remains a usable field: ${JSON.stringify(boxes)}`);if(width===390)assert.ok(boxes.search.top>=boxes.description.bottom+8);else assert.ok(boxes.search.left>boxes.title.left+200);assert.equal(await p.getByRole('button',{name:'Add Custom Connector',exact:true}).isVisible(),true);
 };
 for(const width of [1600,390]){
  await p.setViewportSize({width,height:width===390?844:1000});await assertHeadingBounds(width);
  if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`connections-full-${width}.png`)});}
  await p.locator('#marketplace-search').fill('Notion');assert.equal(await p.locator('.connection-catalogue-item').count(),1);await assertHeadingBounds(width);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`connections-search-${width}.png`)});
  await p.locator('#marketplace-search').fill('');assert.equal(await p.locator('.connection-catalogue-item').count(),upstreamCatalogue.plugins.length+1);
 }
 await p.setViewportSize({width:1600,height:1000});await p.locator('#marketplace-search').fill('gmail');assert.equal(await p.locator('.connection-catalogue-item').count(),1);await p.locator('.connection-catalogue-item .marketplace-card').click();assert.equal(await p.locator('.connection-hero h1').textContent(),'Gmail');if(process.env.ROCOTO_UI_REAL_LOGOS==='1')await p.locator('.connection-hero-logo').evaluate(img=>img.complete?Promise.resolve():new Promise(resolve=>{img.onload=resolve;img.onerror=resolve;}));assert.equal(await p.locator('.connection-hero p').textContent(),'Read and manage Gmail');assert.equal(await p.locator('.connection-long-description').textContent(),'Use Gmail to summarize inbox activity, draft replies, and organize email threads through the connected Gmail app.\n');assert.equal(await p.locator('.connection-details-section h2').filter({hasText:'Apps 1'}).count(),1);if(process.env.ROCOTO_UI_PROOF_DIR){await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'upstream-gmail-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'upstream-gmail-mobile.png')});}assert.equal(await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).isDisabled(),true);await p.getByRole('button',{name:'Setup options',exact:true}).click();assert((await p.locator('#connection-setup-dialog').textContent()).includes('Rocoto setup is being prepared'));assert.deepEqual(h.writes,[]);}finally{await h.close();}});
test('Preview copy enforces byte limit even if metadata claims copyable',async()=>{const h=await pageFor(1600,{files:[{...documentFile,size_bytes:102401}],fileContent:'x'.repeat(102401)});const p=h.page;try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();await p.locator('#file-preview-content').waitFor();assert.equal(await p.locator('#file-copy').count(),0);}finally{await h.close();}});

test('Office documents use actual preview formats and quoted spreadsheet cells',async()=>{const spreadsheet={...documentFile,name:'service.xlsx',media_type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',preview_format:'table',display_type:'spreadsheet'};const h=await pageFor(390,{files:[spreadsheet],fileContent:'Topic,Note\nQueue,"Two guests, waiting"\nService,"Line one\nLine two"'});const p=h.page;try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();await p.locator('.file-table').waitFor();assert.equal(await p.locator('.file-table tr').count(),3);assert.equal(await p.locator('.file-table tr').nth(1).locator('td').nth(1).textContent(),'Two guests, waiting');assert.equal(await p.locator('.file-table tr').nth(2).locator('td').nth(1).textContent(),'Line one\nLine two');}finally{await h.close();}});

test('PDF preview uses the private rendered PDF and its native page viewer',async()=>{const pdf={...documentFile,name:'report.pdf',media_type:'application/pdf',preview_format:'markdown',display_type:'pdf'};const h=await pageFor(390,{files:[pdf]});const p=h.page;try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();await p.locator('.file-pdf-frame').waitFor();assert.equal(await p.locator('.file-pdf-frame').getAttribute('src'),'/api/files/report-1/preview#toolbar=0&navpanes=0&view=FitV');await p.getByRole('combobox',{name:'Document zoom'}).selectOption('1.25');assert((await p.locator('.file-pdf-frame').getAttribute('src')).endsWith('zoom=125'));}finally{await h.close();}});

test('Completed trail keeps commentary and tools while discarding transient thinking and progress',async()=>{
 const progress=[
  {id:'think-1',activity:'thinking',text:'Thinking.\nReasoning finished.\n'},
  {id:'comment',activity:'commentary',text:'I will prepare the report from the supplied records.'},
  {id:'think-2',activity:'thinking',text:'Thinking.\nReasoning finished.\n'},
  {id:'write',activity:'tool_progress',text:'Writing the document.'},
  {id:'wait',activity:'waiting_for_children',text:'Waiting for the child investigations to finish.'},
  {id:'intermediate',activity:'intermediate_answer',text:'The source review is complete.'},
  {id:'legacy-comment',text:'Earlier saved commentary.'},
 ];
 const h=await pageFor(1600,{chat:{messages:[chat.messages[0],{role:'assistant',content:'The report is ready.',elapsed_ms:13184,progress,tools:[{name:'create_file',status:'complete',output:'Saved report'}]}]}}),p=h.page;
 try{
  assert.equal(await p.locator('.completed-turn > summary').textContent(),'Worked for 13s');
  assert.equal(await p.getByText('The report is ready.',{exact:true}).isVisible(),true);
  await p.locator('.completed-turn > summary').click();
  const trail=p.locator('.completed-turn');
  assert.doesNotMatch(await trail.innerText(),/Thinking|Reasoning finished|Writing the document|Waiting for the child/);
  for(const text of ['I will prepare the report from the supplied records.','The source review is complete.','Earlier saved commentary.'])assert.equal(await trail.getByText(text,{exact:true}).isVisible(),true);
  assert.equal(await trail.getByText('Created a file',{exact:true}).isVisible(),true);
  await trail.locator('.tool-call > summary').click();
  assert.equal(await trail.getByText('Saved report',{exact:true}).isVisible(),true);
  assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});

test('Light appearance and consistent icon tiers cover chat, catalogue and file views',async()=>{
 const wordFile={...documentFile,name:'service-review.docx',media_type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',preview_format:'markdown',display_type:'document'};
 const h=await pageFor(1600,{files:[wordFile],chat:{messages:[chat.messages[0],{role:'assistant',content:'The review is ready.',elapsed_ms:13000,artifacts:[wordFile]}]}}),p=h.page;
 const iconSize=async(selector,size)=>{assert.deepEqual(await p.locator(selector).first().evaluate(el=>{const css=getComputedStyle(el);return [parseFloat(css.width)-parseFloat(css.paddingLeft)-parseFloat(css.paddingRight),parseFloat(css.height)-parseFloat(css.paddingTop)-parseFloat(css.paddingBottom)];}),[size,size]);};
 const screenshot=async name=>{if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,name+'.png')});}};
 try{
  await p.emulateMedia({colorScheme:'dark'});
  assert.equal(await p.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'light');
  assert.equal(await p.locator('.workspace').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
  assert.equal(await p.locator('.brand img').evaluate(el=>getComputedStyle(el).filter),'none');
  await iconSize('#new-chat > .icon',24);await iconSize('#marketplace-nav > .icon',24);
  await iconSize('#attachment-toggle > .icon',20);await iconSize('.chat-section-toggle > .icon',16);await iconSize('.artifact-card-main > .icon',20);
  await screenshot('light-chat-desktop');await p.setViewportSize({width:390,height:844});await screenshot('light-chat-mobile');
  await iconSize('#new-chat > .icon',24);await iconSize('#attachment-toggle > .icon',20);
  await p.locator('.artifact-card-main').click();await p.locator('#file-preview-content').waitFor();await iconSize('#file-download > .icon',20);
  await screenshot('light-file-mobile');await p.setViewportSize({width:1600,height:1000});await screenshot('light-file-desktop');await p.keyboard.press('Escape');
  await p.locator('#marketplace-nav').click();await p.locator('.connection-catalogue-item').first().waitFor();
  if(process.env.ROCOTO_UI_REAL_LOGOS==='1')await p.locator('.connection-catalogue-item .marketplace-card > img').evaluateAll(images=>Promise.all(images.slice(0,12).map(image=>image.decode().catch(()=>{}))));
  await iconSize('.connection-catalogue-item .marketplace-card > img',20);await iconSize('.catalogue-add > .icon',20);await iconSize('#customize-connections > .icon',20);
  await screenshot('light-catalogue-desktop');await p.setViewportSize({width:390,height:844});await screenshot('light-catalogue-mobile');
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth),390);
  assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});

const customConnector={id:'custom-erp',name:'Warehouse ERP',kind:'api',description:'Read orders from our private ERP.',summary:'Search order references and delivery times.',status:'prepared',chat_id:'erp-builder',chat_url:'/?chat=erp-builder',collection:'cafe',revision:2,instructions:'Add the API token, then review the connector package.',secret_fields:[{name:'ERP_API_TOKEN',label:'ERP API token',description:'Read access to your ERP.',configured:false}],package_url:'/api/connections/custom/custom-erp/package',files:[{path:'connector.py',size_bytes:482}]};
test('Gallery storage setup preserves its selected business and opens the secure setup chat',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width,{customConnectors:[],expectedCustomCreate:{name:'Archive server',description:'Read recordings from our SFTP archive as new files arrive.',kind:'sftp',collection:'warehouse'}}),p=h.page;
  try{
   await p.goto(origin+'/?library=1&collection=warehouse');await p.locator('#library-view').waitFor();
   assert.equal(await p.locator('#gallery-connect-storage').count(),1);
   await p.locator('#gallery-connect-storage').click();assert.equal(await p.locator('#custom-connector-title').textContent(),'Connect storage');
   assert.equal(await p.locator('#custom-connector-kind').inputValue(),'sftp');
   await p.locator('#custom-connector-name').fill('Archive server');await p.locator('#custom-connector-description').fill('Read recordings from our SFTP archive as new files arrive.');
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`gallery-storage-${width}.png`)});
   await p.locator('#create-custom-connector').click();await p.getByText('Let’s prepare this connector.',{exact:true}).waitFor();assert.equal(new URL(p.url()).searchParams.get('chat'),'custom-builder-chat');
   assert.deepEqual(h.writes,['/api/connections/custom']);
   await p.locator('#attachment-toggle').click();await p.locator('#attachment-connections').getByRole('button',{name:'Add Custom Connector',exact:true}).click();assert.equal(await p.locator('#custom-connector-title').textContent(),'Add Custom Connector');assert.equal(await p.locator('#custom-connector-kind').inputValue(),'api');
  }finally{await h.close();}
 }
});
test('Custom connector creation opens a real builder chat from Customize and Plus',async()=>{
 const h=await pageFor(390,{customConnectors:[],expectedCustomCreate:{name:'Warehouse ERP',description:'Read deliveries from our private ERP.',kind:'api',collection:'cafe'}}),p=h.page;
 try{await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#add-custom-connector').click();await p.locator('#custom-connector-dialog').waitFor();
 await p.locator('#custom-connector-kind').selectOption('api');await p.locator('#custom-connector-name').fill('Warehouse ERP');await p.locator('#custom-connector-description').fill('Read deliveries from our private ERP.');
 if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'custom-connector-create-mobile.png')});await p.setViewportSize({width:1600,height:1000});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'custom-connector-create-desktop.png')});await p.setViewportSize({width:390,height:844});}
 await p.locator('#create-custom-connector').click();await p.getByText('Let’s prepare this connector.',{exact:true}).waitFor();assert.equal(new URL(p.url()).searchParams.get('chat'),'custom-builder-chat');assert.equal(await p.locator('#custom-connector-dialog').isVisible(),false);
 await p.locator('#attachment-toggle').click();await p.locator('#attachment-connections').getByRole('button',{name:'Add Custom Connector',exact:true}).click();assert.equal(await p.locator('#custom-connector-dialog').isVisible(),true);await p.keyboard.press('Escape');assert.equal(await p.locator('#custom-connector-description').inputValue(),'');
 assert.deepEqual(h.writes,['/api/connections/custom']);}finally{await h.close();}
});
test('Custom connector details save and revoke declared secrets without persisting values',async()=>{
 const h=await pageFor(1600,{customConnectors:[customConnector]}),p=h.page;
 try{await p.locator('#marketplace-nav').click();await p.locator('.custom-connector-card').getByRole('button',{name:/Warehouse ERP/}).click();await p.getByRole('heading',{name:'Warehouse ERP',exact:true}).waitFor();assert.equal(await p.getByText('Setup in progress',{exact:true}).count(),1);assert.equal(await p.getByText('Connected',{exact:true}).count(),0);assert.equal(await p.getByRole('link',{name:'Download connector',exact:true}).getAttribute('href'),origin+'/api/connections/custom/custom-erp/package');
 const secret=p.getByLabel('ERP API token',{exact:true});assert.equal(await secret.getAttribute('type'),'password');await secret.fill('fixture-secret-value');await p.getByRole('button',{name:'Save ERP API token',exact:true}).click();await p.getByText('Configured',{exact:true}).waitFor();assert.equal(await secret.inputValue(),'');assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('fixture-secret-value')),false);assert.equal((await p.locator('body').innerText()).includes('fixture-secret-value'),false);
 if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'custom-connector-detail-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'custom-connector-detail-mobile.png')});}
 await p.getByRole('button',{name:'Remove ERP API token',exact:true}).click();await p.getByText('Awaiting credential',{exact:true}).waitFor();assert.deepEqual(h.writes,['/api/connections/custom/custom-erp/secrets','/api/connections/custom/custom-erp/secrets/ERP_API_TOKEN']);
 await p.getByRole('button',{name:'Continue setup',exact:true}).click();await p.getByText('Let’s prepare this connector.',{exact:true}).waitFor();assert.equal(new URL(p.url()).searchParams.get('chat'),'erp-builder');}finally{await h.close();}
});
test('Observed custom connector metadata remains read-only',async()=>{
 const h=await pageFor(390,{customConnectors:[customConnector]}),p=h.page;
 try{await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');assert.equal(await p.locator('#add-custom-connector').isDisabled(),true);await p.locator('.custom-connector-card').getByRole('button',{name:/Warehouse ERP/}).click();await p.getByLabel('ERP API token',{exact:true}).waitFor();assert.equal(await p.getByLabel('ERP API token',{exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Save ERP API token',exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Continue setup',exact:true}).isDisabled(),true);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}
});

test('Custom connector access review precedes checks and activation at desktop and phone widths',async()=>{
 const schema={type:'object',properties:{},additionalProperties:false};
 const proposed={...customConnector,execution_setup:{runtime_available:true,active_status:'unconfigured',active_generation:0,target_generation:0,active_revision:null,target:null,last_test:null,
  tools:[{name:'find_orders',description:'Find orders and their delivery times.'}],proposal:{id:'target-browser',current:true,reason:'Read order references to match them with delivery footage.',
   target:{origin:'https://orders.example.com',authentication:{kind:'bearer',credential:'ERP_API_TOKEN'},operations:[{id:'orders',method:'GET',path:'/v1/orders',query_schema:schema}]}}}};
 for(const width of [1600,390]){const h=await pageFor(width,{customConnectors:[proposed]}),p=h.page;
  try{await p.goto(origin+'/?customize=1&tab=connections&item=custom-erp');await p.getByRole('button',{name:'Review access',exact:true}).click();
   const dialog=p.getByRole('dialog',{name:'Review connection access',exact:true});await dialog.waitFor();
   assert.equal(await dialog.getByText('https://orders.example.com',{exact:true}).count(),1);
   assert.equal(await dialog.getByText('GET /v1/orders',{exact:true}).count(),1);assert.equal(await dialog.locator('input[type=password]').count(),0);assert.deepEqual(h.writes,[]);
   if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`custom-access-${width}.png`)});}
   await dialog.getByRole('button',{name:'Approve access',exact:true}).click();await dialog.waitFor({state:'hidden'});
   await p.getByRole('button',{name:'Run checks',exact:true}).click();await p.getByText('Connection checks passed.',{exact:true}).waitFor();
   await p.getByRole('button',{name:'Activate connection',exact:true}).click();await p.getByText('Connected',{exact:true}).waitFor();
   await p.getByRole('button',{name:'Remove access',exact:true}).click();await p.getByText('Access removed',{exact:true}).waitFor();
   assert.deepEqual(h.writes,['target','test','activate','revoke'].map(action=>'/api/connections/custom/custom-erp/execution/'+action));assert.deepEqual(h.errors,[]);
  }finally{await h.close();}}
});

test('Observed connector access and Gallery storage controls remain read-only',async()=>{
 const item={...customConnector,execution_setup:{tools:[{name:'read_orders',description:'Read order details.'}],proposal:{current:true}}};
 const h=await pageFor(390,{customConnectors:[item]}),p=h.page;
 try{await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('.custom-connector-card').getByRole('button',{name:/Warehouse ERP/}).click();
  assert.equal(await p.getByRole('button',{name:'Review access',exact:true}).isDisabled(),true);
  await p.locator('#library-nav').click();assert.equal(await p.locator('#gallery-connect-storage').isDisabled(),true);assert.deepEqual(h.writes,['/api/observer']);
 }finally{await h.close();}
});

test('Changing observed workspace clears the prior custom connector and unsaved credential',async()=>{
 const h=await pageFor(1600,{customConnectors:[customConnector],privateCustom:true}),p=h.page;
 try{await p.locator('#marketplace-nav').click();await p.locator('.custom-connector-card').getByRole('button',{name:/Warehouse ERP/}).click();await p.getByLabel('ERP API token',{exact:true}).fill('unsaved-fixture-secret');await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();assert.equal(await p.getByRole('heading',{name:'Warehouse ERP',exact:true}).count(),0);assert.equal(await p.locator('input[type=password]').count(),0);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('unsaved-fixture-secret')),false);await p.locator('#marketplace-nav').click();await p.getByRole('heading',{name:'Communication',exact:true}).waitFor();assert.equal(await p.locator('.custom-connector-card').count(),0);}finally{await h.close();}
});

test('Background custom connector chat polls actual progress and completion',async()=>{
 const h=await pageFor(1600,{customConnectors:[],backgroundBuilder:true,expectedCustomCreate:{name:'',description:'Connect our ordering API.',kind:'api',collection:'cafe'}}),p=h.page;
 try{await p.locator('#attachment-toggle').click();await p.locator('#attachment-connections').getByRole('button',{name:'Add Custom Connector',exact:true}).click();await p.locator('#custom-connector-description').fill('Connect our ordering API.');await p.locator('#create-custom-connector').click();await p.getByText('Preparing connector adapter.',{exact:true}).waitFor();assert.equal(await p.locator('#send').getAttribute('aria-label'),'Stop response');await p.getByText('Connector package prepared.',{exact:true}).waitFor({timeout:6000});assert.equal(await p.locator('#send').getAttribute('aria-label'),'Send message');assert.deepEqual(h.writes,['/api/connections/custom']);}finally{await h.close();}
});
test('Custom connector detail URL reload restores declared setup fields',async()=>{
 const h=await pageFor(390,{customConnectors:[customConnector]}),p=h.page;
 try{await p.goto(origin+'/?customize=1&tab=connections&item=custom-erp');await p.getByLabel('ERP API token',{exact:true}).waitFor();assert.equal(await p.getByRole('heading',{name:'Warehouse ERP',exact:true}).isVisible(),true);assert.equal(await p.getByText('Setup in progress',{exact:true}).count(),1);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});
test('Concurrent same-name tools retain outputs by their native call IDs',async()=>{
 const h=await pageFor(1600,{chatEvents:[{type:'tool_start',id:'first',name:'read_file',arguments:{file_id:'A'}},{type:'tool_start',id:'second',name:'read_file',arguments:{file_id:'B'}},{type:'tool_end',id:'first',name:'read_file',success:true,output:'CONTENTS_A'},{type:'tool_end',id:'second',name:'read_file',success:true,output:'CONTENTS_B'},{type:'delta',text:'Files read.'},{type:'done'}]}),p=h.page;
 try{await p.locator('#message').fill('Read both files.');await p.locator('#send').click();await p.getByText('Files read.',{exact:true}).waitFor();const pairs=await p.locator('.tool-call').evaluateAll(tools=>tools.map(tool=>({args:JSON.parse(tool.querySelector('pre').textContent),output:tool.querySelector('.tool-output').textContent})));assert.deepEqual(pairs,[{args:{file_id:'A'},output:'CONTENTS_A'},{args:{file_id:'B'},output:'CONTENTS_B'}]);assert.deepEqual(h.writes,['/api/chat']);}finally{await h.close();}
});

const credentialRequest={id:'credentials-1',connector_id:'custom-erp',connector_revision:2,title:'Connect Warehouse ERP',reason:'Read delivery references and compare them with your footage.',fields:[{name:'ERP_API_TOKEN',label:'ERP API token',description:'A token with read access to orders.',configured:false}],status:'pending'};
function credentialChat(requests=[credentialRequest]){return {...chat,messages:[chat.messages[0],{role:'assistant',content:'The connector needs access to your ERP.',credential_requests:structuredClone(requests)}]};}
test('Credential request auto-opens masked, reveals explicitly and submits outside chat storage',async()=>{
 const h=await pageFor(1600,{customConnectors:[customConnector],chat:credentialChat(),credentialReply:{action:'submit',values:{ERP_API_TOKEN:'private-widget-fixture'}}}),p=h.page;
 try{await p.locator('#credential-request-dialog').waitFor();const input=p.getByLabel('ERP API token',{exact:true});assert.equal(await input.getAttribute('type'),'password');await input.fill('private-widget-fixture');await p.getByRole('button',{name:'Show ERP API token',exact:true}).click();assert.equal(await input.getAttribute('type'),'text');await p.getByRole('button',{name:'Hide ERP API token',exact:true}).click();assert.equal(await input.getAttribute('type'),'password');assert((await p.locator('.question-box-lock').getAttribute('title')).includes('confirmation only'));
 if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await input.fill('');await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'credential-request-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'credential-request-mobile.png')});await input.fill('private-widget-fixture');}
 await p.getByRole('button',{name:'Send',exact:true}).click();await p.locator('#credential-request-dialog').waitFor({state:'hidden'});assert.equal(await p.getByText('Credentials provided',{exact:true}).isVisible(),true);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('private-widget-fixture')),false);assert.equal((await p.locator('body').innerText()).includes('private-widget-fixture'),false);assert.equal(await p.locator('#credential-request-dialog input').count(),0);assert.deepEqual(h.writes,['/api/chats/fixture-chat/credential-requests/credentials-1']);}finally{await h.close();}
});
test('Closing secure entry sends explicit Skip and discards private input',async()=>{
 const h=await pageFor(390,{chat:credentialChat(),credentialReply:{action:'skip',reason:'Skipped by user.'}}),p=h.page;
 try{await p.locator('#credential-request-dialog').waitFor();await p.getByLabel('ERP API token',{exact:true}).fill('discarded-fixture-value');await p.getByRole('button',{name:'Skip secure entry',exact:true}).click();await p.locator('#credential-request-dialog').waitFor({state:'hidden'});assert.equal(await p.getByText('Credential setup skipped',{exact:true}).isVisible(),true);assert.equal(await p.locator('#credential-request-dialog input').count(),0);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('discarded-fixture-value')),false);assert.deepEqual(h.writes,['/api/chats/fixture-chat/credential-requests/credentials-1']);}finally{await h.close();}
});
test('Credential requests remain read-only in observed and child chats',async()=>{
 const h=await pageFor(390,{chat:credentialChat()}),p=h.page;
 try{await p.locator('#credential-request-dialog').waitFor();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.goto(origin+'/?chat=fixture-chat');await p.locator('.credential-request-card').waitFor();assert.equal(await p.locator('#credential-request-dialog').isVisible(),false);assert.equal(await p.locator('.credential-request-card button').isDisabled(),true);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}
 const child={...credentialChat(),id:'credential-child',parent_id:'fixture-chat',title:'Child setup',environment_id:'demo'},childHarness=await pageFor(390,{children:[child]}),childPage=childHarness.page;
 try{await childPage.goto(origin+'/?chat=credential-child');await childPage.locator('.child-chat-title').waitFor();assert.equal(await childPage.locator('#credential-request-dialog').isVisible(),false);assert.equal(await childPage.locator('.credential-request-card button').isDisabled(),true);assert.deepEqual(childHarness.writes,[]);}finally{await childHarness.close();}
});
test('Leaving a credential request clears fields and does not reopen an already dismissed request',async()=>{
 const h=await pageFor(1600,{chat:credentialChat()}),p=h.page;
 try{await p.locator('#credential-request-dialog').waitFor();await p.getByLabel('ERP API token',{exact:true}).fill('navigation-fixture-secret');await p.locator('#new-chat').click();await p.locator('#chat-list').getByRole('button',{name:'Fixture café review',exact:true}).click();assert.equal(await p.locator('#credential-request-dialog').isVisible(),false);await p.getByRole('button',{name:'Add credentials',exact:true}).click();assert.equal(await p.getByLabel('ERP API token',{exact:true}).inputValue(),'');assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

test('Pending credentials show a steady waiting row and queued requests open independently',async()=>{
 const second={...credentialRequest,id:'credentials-2',title:'Connect order archive'};const h=await pageFor(1600,{chat:{...credentialChat([credentialRequest,second]),running:true},credentialReply:{action:'submit',values:{ERP_API_TOKEN:'first-request-fixture'}}}),p=h.page;
 try{await p.locator('#credential-request-dialog').waitFor();assert.equal(await p.locator('.credential-waiting .current-activity').textContent(),'Waiting for credentials');assert.equal(await p.locator('.credential-waiting').evaluate(element=>getComputedStyle(element).animationName),'none');if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'credential-cards-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'credential-cards-mobile.png')});}
 await p.getByLabel('ERP API token',{exact:true}).fill('first-request-fixture');await p.getByRole('button',{name:'Send',exact:true}).click();await p.locator('#credential-request-title').filter({hasText:'Connect order archive'}).waitFor();assert.equal(await p.getByLabel('ERP API token',{exact:true}).inputValue(),'');await p.locator('#new-chat').click();await p.goto(origin+'/?chat=fixture-chat');assert.equal(await p.locator('[data-credential-request-id=credentials-1]').getByText('Credentials provided',{exact:true}).count(),1);}finally{await h.close();}
});
test('Changed connector metadata closes a stale credential request and clears submitted values',async()=>{
 const h=await pageFor(390,{chat:credentialChat(),credentialConflict:true,credentialReply:{action:'submit',values:{ERP_API_TOKEN:'stale-fixture-value'}}}),p=h.page;
 try{await p.locator('#credential-request-dialog').waitFor();await p.getByLabel('ERP API token',{exact:true}).fill('stale-fixture-value');await p.getByRole('button',{name:'Send',exact:true}).click();await p.locator('#credential-request-dialog').waitFor({state:'hidden'});assert.equal(await p.getByText('Connector setup changed',{exact:true}).isVisible(),true);assert.equal(await p.locator('#credential-request-dialog input').count(),0);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('stale-fixture-value')),false);}finally{await h.close();}
});
test('Streamed credential resolution replaces the request and keeps only safe metadata',async()=>{
 const request={...credentialRequest,fields:[{...credentialRequest.fields[0],value:'unexpected-fixture-field'}]};const h=await pageFor(1600,{chatEvents:[{type:'credential_request',request},{type:'credential_request_resolved',request:{...request,status:'submitted'}},{type:'delta',text:'Connection access saved.'},{type:'done'}]}),p=h.page;
 try{await p.locator('#message').fill('Prepare the connector.');await p.locator('#send').click();await p.getByText('Connection access saved.',{exact:true}).waitFor();assert.equal(await p.locator('.credential-request-card').count(),1);assert.equal(await p.getByText('Credentials provided',{exact:true}).isVisible(),true);assert.equal(await p.locator('#credential-request-dialog').isVisible(),false);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('unexpected-fixture-field')),false);}finally{await h.close();}
});
test('Background and expired credential requests leave the active chat undisturbed',async()=>{
 const background={...credentialChat(),id:'background-owner-chat',title:'Background setup',environment_id:'demo'},expired={...credentialRequest,status:'cancelled',resolution_reason:'expired'};const h=await pageFor(390,{chat:credentialChat([expired]),children:[background]}),p=h.page;
 try{await p.getByText('Credential request expired',{exact:true}).waitFor();assert.equal(await p.locator('#credential-request-dialog').isVisible(),false);assert.equal(await p.locator('.credential-request-card button').count(),0);await p.goto(origin+'/?chat=background-owner-chat');await p.locator('#credential-request-dialog').waitFor();assert.equal(await p.getByLabel('ERP API token',{exact:true}).inputValue(),'');assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

const recurringAgent={id:'scheduled-1',title:'Daily service review',description:'Review the previous day and highlight service opportunities.',mode:'scheduled',dataset:'cafe',status:'draft',max_runs:10,runs_started:0,interval_seconds:86400,revision:1,instruction:'Review service and cite evidence.',execution:{configurable:true,ready:false,notice:'Configure your agent here. Connect a live camera feed to start scheduled runs.'}};test('Creating an agent binds the current setup conversation without creating a second chat',async()=>{
 const record={...chat,agent_setup:{mode:'scheduled'}},agent={...recurringAgent,chat_id:chat.id},h=await pageFor(390,{chat:record,chatEvents:[{type:'agent',agent},{type:'delta',text:'This conversation is your agent.'},{type:'done',status:'completed'}]}),p=h.page;
 try{await p.route('**/api/agents',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({agents:[agent]})}));await p.locator('#message').fill('Save this as my daily agent.');Object.assign(record,{kind:'agent_conversation',title:agent.title,agent_id:agent.id,agent_setup:{agent_id:agent.id,mode:agent.mode},messages:[...chat.messages,{role:'user',content:'Save this as my daily agent.'},{role:'assistant',content:'This conversation is your agent.',agents:[agent]}]});await p.locator('#send').click();await p.getByText('This conversation is your agent.',{exact:true}).waitFor();await p.locator('#agent-info-button').waitFor();assert.equal(new URL(p.url()).searchParams.get('agent'),agent.id);assert.equal(h.payloads[0].body.chat_id,chat.id);assert.deepEqual(h.payloads[0].body.agent_setup,{mode:'scheduled'});const saved=await p.evaluate(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')));assert.equal(saved.length,1);assert.equal(saved[0].id,chat.id);assert.equal(saved[0].kind,'agent_conversation');assert.equal(saved[0].title,agent.title);assert.equal(saved[0].agent_id,agent.id);assert.deepEqual(saved[0].agent_setup,{agent_id:agent.id,mode:'scheduled'});assert.equal(saved[0].messages.at(-1).agents[0].chat_id,chat.id);assert.deepEqual(h.writes,['/api/chat']);}
 finally{await h.close();}
});
test('Agent conversations retain history and drafts, with frequency controls in scoped info',async()=>{
 for(const width of [1600,390]){const agent={...recurringAgent,skill_ids:['source-review'],watch_for:['Service queues'],evidence_window:'last_interval',lookback_seconds:86400,fresh_inspections_per_run:0},h=await pageFor(width,{agents:[agent],agentUpdates:true}),p=h.page;try{
  await p.locator('#message').fill('Keep my regular chat draft.');await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').click();await p.locator('#agent-info-button').waitFor();assert.equal(new URL(p.url()).searchParams.get('agent'),agent.id);assert.equal(await p.locator('#agent-context').isVisible(),false);assert.equal(await p.locator('#chat-list').isVisible(),false);assert.equal(await p.locator('#agents-nav').getAttribute('aria-current'),'page');assert.equal(await p.locator('#new-chat').getAttribute('aria-current'),'false');assert.equal(await p.locator('#chat-list .chat-link').count(),1);await p.getByText('Agent conversation ready.',{exact:true}).waitFor();await p.locator('#message').fill('Keep my agent briefing draft.');await proofShot(p,`persistent-agent-${width}`);
  await p.locator('#agent-info-button').click();await p.getByLabel('Run every',{exact:true}).waitFor();await proofShot(p,`persistent-agent-info-${width}`);await p.getByRole('group',{name:'Frequency unit',exact:true}).getByRole('button',{name:'Hours',exact:true}).click();assert.equal(await p.locator('#agent-info-dialog select').count(),0);await p.getByLabel('Run every',{exact:true}).fill('12');await p.getByRole('button',{name:'Save frequency',exact:true}).click();await p.waitForFunction(()=>document.querySelector('#agent-chat-meta').textContent.includes('12 hours'));const payload=h.payloads.find(item=>item.path.endsWith('/update')).body;assert.equal(payload.expected_revision,1);assert.equal(payload.interval_seconds,43200);assert.deepEqual(payload.skill_ids,['source-review']);assert.deepEqual(payload.watch_for,['Service queues']);assert.equal(payload.lookback_seconds,86400);assert.equal(payload.fresh_inspections_per_run,0);assert.equal(payload.instruction,agent.instruction);assert.equal(Object.hasOwn(payload,'status'),false);
  await p.keyboard.press('Escape');assert.equal(await p.locator('#message').inputValue(),'Keep my agent briefing draft.');await p.goto(origin+'/?chat=fixture-chat');await p.locator('#message').waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep my regular chat draft.');await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').click();await p.locator('#agent-info-button').waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep my agent briefing draft.');await p.reload();await p.locator('#agent-info-button').waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep my agent briefing draft.');await p.getByText('Agent conversation ready.',{exact:true}).waitFor();assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 }finally{await h.close();}}
});
test('A scheduled wake updates the open persistent conversation through read-only polling',async()=>{
 const h=await pageFor(390,{agents:[recurringAgent]}),p=h.page;let woke=false,reads=0;
 try{await p.route('**/api/chats/agent-chat-scheduled-1',route=>{reads++;return route.fulfill({contentType:'application/json',body:JSON.stringify({...chat,id:'agent-chat-scheduled-1',kind:'agent_conversation',agent_id:'scheduled-1',agent_setup:{agent_id:'scheduled-1',mode:'scheduled'},title:recurringAgent.title,messages:[{role:'user',content:'Daily review briefing.'},{role:'assistant',content:woke?'New scheduled findings.':'Previous scheduled findings.'}],running:false})});});await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').click();await p.getByText('Previous scheduled findings.',{exact:true}).waitFor();await p.locator('#message').fill('Preserve my follow-up while it wakes.');woke=true;await p.getByText('New scheduled findings.',{exact:true}).waitFor({timeout:7000});assert(reads>=2);assert.equal(await p.locator('#message').inputValue(),'Preserve my follow-up while it wakes.');assert.deepEqual(h.writes,[]);assert.equal(new URL(p.url()).searchParams.get('agent'),'scheduled-1');}
 finally{await h.close();}
});
test('Pending agent detail and info responses stay within their navigation and owner scope',async()=>{
 for(const target of ['new-chat','observer','close-info']){const h=await pageFor(390,{agents:[recurringAgent]}),p=h.page;let held=null;try{
  await p.route('**/api/agents/scheduled-1',route=>{held=route;});await p.locator('#agents-nav').click();if(target==='close-info')await p.getByRole('button',{name:'About '+recurringAgent.title,exact:true}).click();else await p.locator('.agent-collection-card').click();for(let i=0;i<50&&!held;i++)await p.waitForTimeout(10);assert(held);
  if(target==='close-info')await p.keyboard.press('Escape');else if(target==='new-chat'){await p.locator('#new-chat').click();await p.locator('#message').fill('New scope draft.');}else{await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();}
  await held.fulfill({contentType:'application/json',body:JSON.stringify({...recurringAgent,chat_id:'agent-chat-scheduled-1'})});await p.waitForTimeout(70);assert.equal(await p.locator('#agent-info-dialog').count(),0);assert.equal(await p.locator('#agent-info-button').isVisible(),false);if(target==='new-chat')assert.equal(await p.locator('#message').inputValue(),'New scope draft.');if(target==='observer')assert.equal(await p.getByText('Agent conversation ready.',{exact:true}).count(),0);
 }finally{await h.close();}}
});
test('Typed agent cards open the saved conversation and malformed cards stay inert',async()=>{
 const item={id:'scheduled-1',title:'Daily service review',chat_id:'agent-chat-scheduled-1',mode:'scheduled',status:'draft',conversation_url:'https://external.invalid/arbitrary'},record={...chat,messages:[chat.messages[0],{role:'assistant',content:'Your agent is ready to review.',agents:[item,{...item,id:'javascript:bad'},{...item,chat_id:'../foreign'}]}]},h=await pageFor(390,{agents:[recurringAgent],chat:record,chatEvents:[{type:'agent',agent:item},{type:'agent',agent:{...item,status:'paused'}},{type:'delta',text:'Linked to the same agent.'},{type:'done',status:'completed'}]}),p=h.page;try{
  assert.equal(await p.locator('.agent-conversation-card').count(),1);assert.equal(await p.locator('.agent-conversation-card').getAttribute('href'),'/?agents=1&agent=scheduled-1');await p.locator('#message').fill('Find that agent.');await p.locator('#send').click();await p.getByText('Linked to the same agent.',{exact:true}).waitFor();assert.equal(await p.locator('.message-assistant').last().locator('.agent-conversation-card').count(),1);await p.locator('.agent-conversation-card').last().click();await p.locator('#agent-info-button').waitFor();await p.getByText('Agent conversation ready.',{exact:true}).waitFor();assert.equal(h.writes.filter(path=>path==='/api/chat').length,1);assert.equal(h.writes.filter(path=>path==='/api/agents').length,0);
 }finally{await h.close();}
});

function organizationSetupFixture(purpose,collection,id=`orgchat-${purpose}-${collection}`){return{id,title:purpose==='cameras'?'Cameras':'Organization',kind:'organization_chat',organization_purpose:purpose,environment_id:'demo',owner_principal_id:'organization-fixture',dataset:collection,parent_id:null,read_only:false,messages:[],running:false};}
async function installCameraSetupFixture(h,collection){
 const record=organizationSetupFixture('cameras',collection);
 await h.page.route('**/api/organization/chats',route=>{const request=route.request();assert.equal(request.method(),'POST');assert.equal(request.headers()['x-rocoto-page'],'1');assert.deepEqual(request.postDataJSON(),{purpose:'cameras',collection});h.writes.push('/api/organization/chats');h.payloads.push({path:'/api/organization/chats',body:request.postDataJSON()});return route.fulfill({contentType:'application/json',body:JSON.stringify(record)});});
 await h.page.route(`**/api/chats/${record.id}`,route=>route.fulfill({contentType:'application/json',body:JSON.stringify(record)}));
 return record;
}

test('Camera provider picker uses original logos and creates a scoped setup chat',async()=>{
 const h=await pageFor(1600),p=h.page;
 try{const setup=await installCameraSetupFixture(h,'cafe');await p.goto(origin+'/?cameras=1&collection=cafe');await p.getByRole('button',{name:'Add cameras',exact:true}).click();await p.locator('#camera-provider-dialog').waitFor();assert.deepEqual(await p.locator('#camera-provider-options .camera-provider strong').allTextContents(),['Hikvision','Dahua','Axis','Hanwha Vision','Uniview','TP-Link VIGI']);assert.equal(await p.locator('#camera-provider-more').getAttribute('open'),null);assert.equal(await p.locator('#camera-provider-methods .camera-provider strong').textContent(),'ONVIF / RTSP');assert.equal(await p.locator('.camera-provider img').count(),12);await p.waitForFunction(()=>Array.from(document.querySelectorAll('.camera-provider img')).every(img=>img.complete&&img.naturalWidth>0));assert.equal(await p.locator('#camera-provider-dialog input, #camera-provider-dialog textarea').count(),0);
 if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'camera-providers-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'camera-providers-mobile.png')});}
 await p.getByRole('button',{name:/^Hikvision/}).click();await p.locator('#welcome h1').filter({hasText:'Cameras'}).waitFor();assert.equal(new URL(p.url()).searchParams.get('chat'),setup.id);assert.match(await p.locator('#message').inputValue(),/Help me connect my Hikvision cameras\./);assert.match(await p.locator('#message').inputValue(),/https:\/\/supportusa\.hikvision\.com\//);assert.equal(await p.locator('#camera-provider-dialog').isVisible(),false);assert.deepEqual(h.writes,['/api/organization/chats']);}finally{await h.close();}
});
test('None of these begins camera identification in chat and the picker restores focus',async()=>{
 const h=await pageFor(390),p=h.page;
 try{const setup=await installCameraSetupFixture(h,'warehouse');await p.goto(origin+'/?cameras=1&collection=warehouse');const add=p.getByRole('button',{name:'Add cameras',exact:true});await add.click();await p.keyboard.press('Escape');assert.equal(await p.evaluate(()=>document.activeElement.id),'add-cameras');await add.click();await p.getByRole('button',{name:'None of these',exact:true}).click();await p.locator('#welcome h1').filter({hasText:'Cameras'}).waitFor();assert.equal(new URL(p.url()).searchParams.get('chat'),setup.id);assert.equal(await p.locator('#message').inputValue(),'');assert.deepEqual(h.writes,['/api/organization/chats']);}finally{await h.close();}
});
test('Scheduled agent configuration remains available while activation awaits a live feed',async()=>{
 const h=await pageFor(1600,{agents:[recurringAgent]}),p=h.page;
 try{await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').first().click();await p.locator('#agent-info-button').click();await p.locator('#agent-info-title').filter({hasText:recurringAgent.title}).waitFor();assert.equal(await p.getByRole('button',{name:'Activate',exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Run once',exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Edit in chat',exact:true}).isEnabled(),true);await p.getByRole('button',{name:'Edit in chat',exact:true}).click();await p.locator('#agent-setup-notice').waitFor();assert.equal(await p.locator('#agent-setup-notice').textContent(),'Configure your agent here. Connect a live camera feed to start scheduled runs.Connect cameras');assert.equal(await p.locator('#message').isEditable(),true);assert.equal(new URL(p.url()).searchParams.get('agent'),'scheduled-1');
 if(process.env.ROCOTO_UI_PROOF_DIR){await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'agent-setup-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'agent-setup-mobile.png')});}
 await p.locator('#message').fill('Make this a daily summary.');assert.equal(await p.locator('#agent-setup-notice').isVisible(),true);await p.locator('#agent-setup-notice').getByRole('button',{name:'Connect cameras',exact:true}).click();await p.locator('#camera-provider-dialog').waitFor();assert.deepEqual(h.writes,[]);}finally{await h.close();}
});
test('Saved agent setup context restores on reload and ready or ad hoc chats omit the notice',async()=>{
 const h=await pageFor(390,{chat:{...chat,agent_setup:{mode:'scheduled',agent_id:'scheduled-1'}},capabilities:{recurring_agents:{ready:false,configurable:true,notice:'Configure your agent here. Connect a live camera feed to start scheduled runs.'}}}),p=h.page;
 try{await p.locator('#agent-setup-notice').waitFor();await p.reload();await p.locator('#agent-setup-notice').waitFor();await p.locator('#new-chat').click();assert.equal(await p.locator('#agent-setup-notice').isVisible(),false);}finally{await h.close();}
 for(const options of [{chat:{...chat,agent_setup:{mode:'ad_hoc'}}},{chat:{...chat,agent_setup:{mode:'scheduled'}},capabilities:{recurring_agents:{ready:true,configurable:true,notice:null}}}]){const ready=await pageFor(390,options);try{assert.equal(await ready.page.locator('#agent-setup-notice').isVisible(),false);}finally{await ready.close();}}
});
test('Agent setup metadata accompanies chat submission without blocking configuration',async()=>{
 const h=await pageFor(1600,{agents:[recurringAgent],chatEvents:[{type:'delta',text:'Your updated draft is saved.'},{type:'done'}]}),p=h.page;
 try{await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').first().click();await p.locator('#agent-info-button').click();await p.getByRole('button',{name:'Edit in chat',exact:true}).click();await p.locator('#message').fill('Make it a daily summary.');await p.locator('#send').click();await p.getByText('Your updated draft is saved.',{exact:true}).waitFor();assert.deepEqual(h.payloads.find(item=>item.path==='/api/chat').body.agent_setup,{mode:'scheduled',agent_id:'scheduled-1'});assert.equal(await p.locator('#agent-setup-notice').isVisible(),true);}finally{await h.close();}
});

test('Camera setup stays read-only in an observed workspace',async()=>{
 const h=await pageFor(390),p=h.page;
 try{await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.goto(origin+'/?cameras=1&collection=cafe');await p.getByRole('button',{name:'Add cameras',exact:true}).waitFor();assert.equal(await p.getByRole('button',{name:'Add cameras',exact:true}).isDisabled(),true);assert.equal(await p.locator('#camera-provider-dialog').isVisible(),false);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}
});
test('Ad hoc agents and verified live-ready agents retain execution controls',async()=>{
 for(const agent of [{...recurringAgent,mode:'ad_hoc'},{...recurringAgent,execution:{configurable:true,ready:true,notice:null}}]){
 const h=await pageFor(1600,{agents:[agent]}),p=h.page;try{await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').first().click();await p.locator('#agent-info-button').click();await p.locator('#agent-info-title').filter({hasText:agent.title}).waitFor();assert.equal(await p.getByRole('button',{name:'Run once',exact:true}).isEnabled(),true);if(agent.mode==='scheduled')assert.equal(await p.getByRole('button',{name:'Activate',exact:true}).isEnabled(),true);assert.equal(await p.locator('.agent-readiness-note').count(),0);assert.deepEqual(h.writes,[]);}finally{await h.close();}}
});
test('Readiness follows the selected company scope while configuring a new agent',async()=>{
 const h=await pageFor(390,{capabilitiesByDataset:{cafe:{recurring_agents:{configurable:true,ready:true}},warehouse:{recurring_agents:{configurable:true,ready:false}}}}),p=h.page;
 try{await p.locator('#agents-nav').click();await p.locator('#create-agent').click();assert.equal(await p.locator('#agent-setup-notice').isVisible(),false);await p.locator('#founder-nav').click();await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();await p.locator('#agent-setup-notice').waitFor();assert.equal(await p.locator('#message').isEditable(),true);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});
test('A recurring agent can still be paused when live-feed readiness is lost',async()=>{
 const h=await pageFor(1600,{agents:[{...recurringAgent,status:'active'}]}),p=h.page;
 try{await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').first().click();await p.locator('#agent-info-button').click();await p.getByRole('button',{name:'Pause',exact:true}).waitFor();assert.equal(await p.getByRole('button',{name:'Pause',exact:true}).isEnabled(),true);assert.equal(await p.getByRole('button',{name:'Edit in chat',exact:true}).isDisabled(),true);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

test('Customer company welcome omits demo suggestions while demo scope keeps them',async()=>{
 for(const environment of [{id:'company-private',name:'A real company'},{id:'customer',name:'Demo'},{id:'demo',name:'Demo company'}]){const h=await pageFor(390,{environment}),p=h.page;try{if(!await p.locator('#add-chat').isVisible())await p.locator('#rail-chats').click();await p.locator('#add-chat').click();await p.locator('#welcome').waitFor();assert.equal(await p.locator('#welcome .suggestions').isVisible(),environment.id==='demo');assert.deepEqual(h.writes,[]);}finally{await h.close();}}
});
test('Escape leaves fullscreen before closing the video viewer',async()=>{
 const h=await pageFor(),p=h.page;try{await p.goto(origin+'/?clip=fixture-clip');await p.locator('#clip-dialog').waitFor();await p.locator('#fullscreen-clip').click();await p.waitForFunction(()=>Boolean(document.fullscreenElement));await p.keyboard.press('Escape');await p.waitForFunction(()=>!document.fullscreenElement);assert.equal(await p.locator('#clip-dialog').isVisible(),true);await p.keyboard.press('Escape');assert.equal(await p.locator('#clip-dialog').isVisible(),false);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

test('Demo questions follow reviewed collection metadata and selecting a suggestion fills the composer',async()=>{
 const h=await pageFor(),p=h.page;try{await p.locator('#new-chat').click();await p.getByRole('button',{name:'Review café service',exact:true}).click();assert.equal(await p.locator('#message').inputValue(),'Inspect the counter and cite a moment.');assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')).find(row=>row.id===new URL(location.href).searchParams.get('chat')).dataset),'cafe');await p.locator('#founder-nav').click();await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();await p.getByRole('button',{name:'Review warehouse routes',exact:true}).waitFor();assert.equal(await p.getByRole('button',{name:'Review café service',exact:true}).count(),0);await p.getByRole('button',{name:'Review warehouse routes',exact:true}).click();assert.equal(await p.locator('#message').inputValue(),'Inspect routes near stored materials.');await p.locator('#quick-search-nav').click();assert.equal(await p.locator('#welcome .suggestions').isVisible(),false);assert.deepEqual(h.writes,[]);}finally{await h.close();}
 const missing=await pageFor(390,{guidance:{}});try{await missing.page.locator('#new-chat').click();assert.equal(await missing.page.locator('#welcome .suggestions').isVisible(),false);}finally{await missing.close();}
});
test('Expanded camera systems and connection methods each start the selected setup conversation',async()=>{
 for(const provider of [{name:'Hanwha Vision',reference:'https://supportportal.hanwhavision.com/'},{name:'Uniview',reference:'https://www.uniview.com/Support/Download_Center/SDK/'},{name:'TP-Link VIGI',reference:'https://www.tp-link.com/us/support/faq/4201/'},{name:'Bosch',reference:'https://cdn.commerce.boschsecurity.com/',more:true},{name:'Avigilon',reference:'https://www.avigilon.com/',more:true},{name:'ONVIF / RTSP',reference:'https://www.onvif.org/'}]){const h=await pageFor(390),p=h.page;try{const setup=await installCameraSetupFixture(h,'cafe');await p.goto(origin+'/?cameras=1&collection=cafe');await p.getByRole('button',{name:'Add cameras',exact:true}).click();if(provider.more){await p.locator('#camera-provider-more > summary').click();assert.equal(await p.locator('#camera-provider-more-options .camera-provider').count(),5);if(process.env.ROCOTO_UI_PROOF_DIR&&provider.name==='Bosch'){await p.locator('#camera-provider-more-options').getByRole('button',{name:/^Avigilon/}).scrollIntoViewIfNeeded();await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'camera-providers-expanded-mobile.png')});await p.setViewportSize({width:1600,height:1000});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'camera-providers-expanded-desktop.png')});}}await p.getByRole('button',{name:new RegExp('^'+provider.name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'))}).click();await p.locator('#welcome h1').filter({hasText:'Cameras'}).waitFor();assert.equal(new URL(p.url()).searchParams.get('chat'),setup.id);const draft=await p.locator('#message').inputValue();assert(draft.includes(provider.name));assert(draft.includes(provider.reference));assert.deepEqual(h.writes,['/api/organization/chats']);}finally{await h.close();}}
});

test('Composer height follows wrapped text when the browser narrows and widens',async()=>{
 const h=await pageFor(),p=h.page;try{await p.locator('#message').fill('Review the footage and highlight the moments that deserve attention.');const measure=()=>p.locator('#message').evaluate(el=>({height:el.clientHeight,scroll:el.scrollHeight}));const wide=await measure();await p.setViewportSize({width:390,height:844});await p.waitForTimeout(80);const narrow=await measure();assert(narrow.height>wide.height,'Narrow composer should expand for wrapped text');assert(narrow.scroll<=narrow.height+1,'Wrapped composer text must fit before its scrolling limit');await p.setViewportSize({width:1600,height:1000});await p.waitForTimeout(80);assert.equal((await measure()).height,wide.height,'Wide composer should return to its content height');assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

const nativeQuestion={id:'question-opaque-1',root_chat_id:'fixture-chat',actor_chat_id:'fixture-child',actor_title:'Service review',kind:'question',status:'pending',blocking:true,questions:[{id:'scope',header:'Scope',question:'Which part should I review?',is_other:true,options:[{label:'Counter service',description:'Follow the ordering and handover.'},{label:'Seating area',description:'Review how the tables are used.'}]},{id:'format',header:'Format',question:'What should the report include?',is_other:false,options:[]}],created_at:'2026-10-04T12:00:00Z'};
function questionChat(request=nativeQuestion){return {...chat,messages:[chat.messages[0],{role:'assistant',content:'I have a question before I continue.',native_questions:[structuredClone(request)]}]};}
test('Ordinary questions require complete answers and submit once through the parent chat',async()=>{
 const h=await pageFor(1600,{chat:questionChat(),questionReply:{action:'submit',answers:{scope:['Counter service'],format:['Include timestamps and practical suggestions.']}}}),p=h.page;try{await p.locator('#native-question-dialog').waitFor();assert.equal(await p.getByRole('button',{name:'Send',exact:true}).isDisabled(),true);await p.getByRole('radio',{name:/^Counter service/}).click();assert.equal(await p.getByRole('button',{name:'Send',exact:true}).isDisabled(),true);await p.getByLabel('Your answer · Format',{exact:true}).fill('Include timestamps and practical suggestions.');if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'native-question-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'native-question-mobile.png')});}await p.locator('#native-question-dialog form').evaluate(form=>{form.requestSubmit();form.requestSubmit();});await p.locator('#native-question-dialog').waitFor({state:'hidden'});await p.getByText('Answers sent',{exact:true}).waitFor();assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);assert.equal(await p.locator('#credential-request-dialog').isVisible(),false);}finally{await h.close();}
});
test('Leaving an ordinary question preserves the card and supports a typed answer',async()=>{
 const one={...nativeQuestion,questions:[nativeQuestion.questions[0]]};const h=await pageFor(1600,{chat:questionChat(one),questionReply:{action:'submit',answers:{scope:['Both areas together.']}}}),p=h.page;try{await p.locator('#native-question-dialog').waitFor();await p.locator('#new-chat').click();await p.locator('#chat-list').getByRole('button',{name:'Fixture café review',exact:true}).click();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);await p.getByRole('button',{name:'Answer question',exact:true}).click();await p.getByLabel('Your answer · Scope',{exact:true}).fill('Both areas together.');await p.getByRole('button',{name:'Send',exact:true}).click();await p.getByText('Answers sent',{exact:true}).waitFor();assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);}finally{await h.close();}
});

test('Ordinary questions can be skipped and terminal conflicts close the pending form',async()=>{
 for(const questionConflict of [false,true]){const h=await pageFor(390,{chat:questionChat(),questionReply:{action:'skip'},questionConflict}),p=h.page;try{await p.locator('#native-question-dialog').waitFor();await p.getByRole('button',{name:'Skip',exact:true}).click();await p.locator('#native-question-dialog').waitFor({state:'hidden'});await p.getByText(questionConflict?'Question closed':'Question skipped',{exact:true}).waitFor();assert.equal(await p.locator('.native-question-card button').count(),0);assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);}finally{await h.close();}}
});
test('Observer and child chats show ordinary question cards read-only',async()=>{
 const h=await pageFor(390,{chat:questionChat()}),p=h.page;try{await p.locator('#native-question-dialog').waitFor();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.goto(origin+'/?chat=fixture-chat');await p.locator('.native-question-card').waitFor();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);assert.equal(await p.locator('.native-question-card button').isDisabled(),true);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}
 const child={...questionChat(),id:'fixture-child',parent_id:'fixture-chat',title:'Service review'};const c=await pageFor(390,{children:[child]});try{await c.page.goto(origin+'/?chat=fixture-child');await c.page.locator('.native-question-card').waitFor();assert.equal(await c.page.locator('#native-question-dialog').isVisible(),false);assert.equal(await c.page.locator('.native-question-card button').isDisabled(),true);assert.equal(await c.page.locator('.composer-area').isVisible(),false);assert.deepEqual(c.writes,[]);}finally{await c.close();}
});
test('Streamed ordinary questions normalize public metadata and terminal events resolve the same card',async()=>{
 const extra={...nativeQuestion,native_request_id:'private-native-id',thread_id:'private-thread',questions:[nativeQuestion.questions[0]]};
 for(const terminal of [false,true]){const h=await pageFor(1600,{chatEvents:[{type:'user_input_request',request:extra},...(terminal?[{type:'user_input_resolved',request:{...extra,status:'resolved'}}]:[]),{type:'done'}]}),p=h.page;try{await p.locator('#message').fill('Review the source.');await p.locator('#send').click();await p.locator('.native-question-card').waitFor();assert.equal(await p.locator('.native-question-card').count(),1);if(terminal){await p.getByText('Question resolved',{exact:true}).waitFor();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);}else{await p.locator('#native-question-dialog').waitFor();await p.locator('#new-chat').click();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);}assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('private-native-id')),false);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('private-thread')),false);assert.deepEqual(h.writes,['/api/chat']);}finally{await h.close();}}
});
test('Terminal ordinary requests stay readable and never auto-open answer controls',async()=>{
 for(const status of ['delivering','submitted','skipped','timed_out','resolved','interrupted','delivery_uncertain','unsupported']){const h=await pageFor(390,{chat:questionChat({...nativeQuestion,status,questions:status==='unsupported'?[]:nativeQuestion.questions})}),p=h.page;try{await p.locator('.native-question-card').waitFor();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);assert.equal(await p.locator('.native-question-card button').count(),0);assert.deepEqual(h.writes,[]);}finally{await h.close();}}
});

test('Ordinary question submission stays guarded after dismissal and pending requests open in order',async()=>{
 const h=await pageFor(390,{chat:questionChat(),questionReply:{action:'skip'},questionDelay:350}),p=h.page;try{await p.locator('#native-question-dialog').waitFor();await p.getByRole('button',{name:'Skip',exact:true}).click();await p.keyboard.press('Escape');assert.equal(await p.locator('.native-question-card button').isDisabled(),true);await p.getByText('Question skipped',{exact:true}).waitFor();assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);}finally{await h.close();}
 const two=questionChat();two.messages[1].native_questions.push({...structuredClone(nativeQuestion),id:'question-opaque-2',questions:[{id:'next',header:'Next step',question:'What would you like next?',is_other:false,options:[]}]});const queued=await pageFor(390,{chat:two,questionReply:{action:'skip'}}),q=queued.page;try{await q.locator('#native-question-dialog').waitFor();await q.getByRole('button',{name:'Skip',exact:true}).click();await q.locator('#native-question-dialog h2').filter({hasText:'What would you like next?'}).waitFor();assert.equal(await q.locator('.native-question-card').count(),2);await q.locator('#new-chat').click();assert.equal(await q.locator('#native-question-dialog').isVisible(),false);assert.deepEqual(queued.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);}finally{await queued.close();}
});


test('Native output image is previewed through the private image endpoint',async()=>{
 const file={...documentFile,name:'chart.png',media_type:'image/png',display_type:'image',preview_format:null,copyable:false,representation:'native_original',inline_preview_type:'image'};
 const h=await pageFor(1600,{files:[file],fileContent:null}),p=h.page;
 try{
  await p.route('**/api/files/report-1/preview',route=>route.fulfill({contentType:'image/png',headers:{'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; sandbox; frame-ancestors 'self'"},body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+cM5sAAAAASUVORK5CYII=','base64')}));
  await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();
  const image=p.locator('#file-preview-content img');await image.waitFor();await image.evaluate(img=>img.complete?null:new Promise(r=>{img.onload=r;img.onerror=r}));
  assert.equal(await image.getAttribute('src'),'/api/files/report-1/preview');assert.equal(await image.evaluate(img=>img.naturalWidth),1);assert.equal(await p.locator('#file-copy').count(),0);
  for(const width of [1600,390]){await p.setViewportSize({width,height:width===390?844:1000});const bounds=await p.locator('#chat-files-panel').boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=width);}
 }finally{await h.close();}
});
test('Native SVG stays inert text and binary Office uses download fallback',async()=>{
 for(const [name,media,preview,content] of [['drawing.svg','application/octet-stream','text','<svg onload="window.badNative=true"><script>window.badNative=true</script></svg>'],['report.docx','application/octet-stream',null,null]]){
  const file={...documentFile,name,media_type:media,preview_format:preview,copyable:Boolean(preview),representation:'native_original',inline_preview_type:null};const h=await pageFor(390,{files:[file],fileContent:content}),p=h.page;
  try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();await p.getByRole('button',{name:'Request edits',exact:true}).waitFor();await p.waitForTimeout(100);assert.equal(await p.evaluate(()=>window.badNative),undefined);assert.equal(await p.locator('#file-preview img,#file-preview iframe,#file-preview script').count(),0);assert.equal(await p.locator('#file-download').count(),1);if(preview)assert.equal(await p.locator('#file-preview-content').textContent(),content);else assert((await p.locator('#file-preview').textContent()).includes('Download this file'));}finally{await h.close();}
 }
});
test('Native PDF has a useful download fallback after strict sandbox compatibility proof',async()=>{
 const file={...documentFile,name:'report.pdf',media_type:'application/pdf',display_type:'pdf',preview_format:null,copyable:false,representation:'native_original',inline_preview_type:null};
 const h=await pageFor(390,{files:[file],fileContent:null}),p=h.page;
 try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();await p.getByText('PDF ready to download',{exact:true}).waitFor();assert.equal(await p.locator('.file-pdf-frame').count(),0);assert.equal(await p.locator('#file-download').count(),1);assert.equal(await p.getByRole('combobox',{name:'Document zoom'}).isVisible(),false);for(const width of [1600,390]){await p.setViewportSize({width,height:width===390?844:1000});if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`native-pdf-fallback-${width}.png`)});}}finally{await h.close();}
});

for (const cached of [true,false]) test(`Deep-linked reload keeps the requested ${cached?'cached':'uncached'} chat throughout session restoration`,async()=>{
 const answer={role:'assistant',content:'REQUESTED_CHAT_ANSWER',elapsed_ms:18000};
 const h=await pageFor(1600,{chat:{messages:[chat.messages[0],answer]}}),p=h.page;
 try{
  const target={...chat,messages:[{role:'assistant',text:'REQUESTED_CHAT_ANSWER',elapsed_ms:18000}],environment_id:'demo'};
  const other={...target,id:'wrong-cached-chat',title:'Wrong cached chat',messages:[{role:'assistant',text:'WRONG_CACHED_CHAT',elapsed_ms:13000}]};
  await p.addInitScript(({target,other,cached})=>{
   for(const key of ['rocoto-video-chats-v1','rocoto-video-chats-v1:demo%3Aowner'])localStorage.setItem(key,JSON.stringify(cached?[other,target]:[other]));
   window.startupMessages=[];
   new MutationObserver(()=>{const text=document.querySelector('#messages')?.textContent||'';if(text)window.startupMessages.push(text);}).observe(document,{subtree:true,childList:true});
  },{target,other,cached});
  await p.route('**/api/session',async route=>{await new Promise(resolve=>setTimeout(resolve,250));await route.fallback();});
  await p.route('**/api/chats?include_archived=true',async route=>{await new Promise(resolve=>setTimeout(resolve,250));await route.fallback();});
  await p.reload();await p.getByText('REQUESTED_CHAT_ANSWER',{exact:true}).waitFor();
  await p.waitForTimeout(700);
  const observed=await p.evaluate(()=>window.startupMessages);
  assert(observed.length>0);
  assert(!observed.some(text=>text.includes('WRONG_CACHED_CHAT')),'Rendered another cached chat while restoring the requested URL');
  assert.equal(await p.locator('.completed-turn > summary').innerText(),'Worked for 18s');
  assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});


// Gallery intake fixtures accept real request bytes and enforce offsets/replay.
async function uploadPage(width=1600, options={}) {
 const h=await pageFor(width,options.empty?{environment:{id:'customer',name:'Sample Company'},corpus:[],chat:{dataset:''}}:{}), p=h.page, rows=new Map(), keys=new Map(), calls=[], chunks=new Map();let nextId=0,libraryReads=0,lost=false,patchGate=null,patchEntered=null,createGate=null,createAttempts=0;
 const intake={available:options.available!==false,can_upload:options.canUpload!==false&&options.available!==false,chunk_bytes_max:4,collections:options.empty?['company']:['cafe','warehouse'],default_collection:options.empty?'company':null};
 const json=(route,data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 await p.route('**/api/retail/library**',async route=>{
  const r=route.request(),u=new URL(r.url()),parts=u.pathname.split('/'),method=r.method();calls.push({path:u.pathname,method,offset:r.headers()['upload-offset']});
  if(u.pathname==='/api/retail/library'){libraryReads++;return json(route,{folder:'',folders:[],assets:options.assets || []});}
  if(u.pathname==='/api/retail/library/imports'){
   if(method==='GET'){if(options.intakeDelay)await new Promise(resolve=>setTimeout(resolve,options.intakeDelay));return json(route,{imports:[...rows.values()].filter(row=>row.collection===u.searchParams.get('collection')),intake});}
   assert.equal(method,'POST');const body=r.postDataJSON(),key=r.headers()['idempotency-key'];assert(key);
   let row=keys.get(key);if(!row){assert(intake.can_upload);row={id:'upload-'+String(++nextId).padStart(32,'0'),collection:body.collection,filename:body.filename,expected_bytes:body.size,committed_bytes:0,status:'uploading',transfer_status:'uploading',stage:null,stage_status:null,created_at:Date.now()/1000,expires_at:Date.now()/1000+3600,error:null};keys.set(key,row);rows.set(row.id,row);chunks.set(row.id,[]);}
   if(options.gateCreate&&!createGate){createGate={};createGate.promise=new Promise(resolve=>createGate.release=resolve);await createGate.promise;}
   if(options.loseCreateReplies && ++createAttempts<=options.loseCreateReplies)return route.abort('failed');
   return json(route,{import:row,chunk_bytes_max:4},201);
  }
  const row=rows.get(parts[5]);assert(row,'fixture import must exist');
  if(parts[6]==='content'){
   assert.equal(method,'PATCH');if(options.rejectChunks)return json(route,{detail:'upload_receive_capacity'},429);const bytes=r.postDataBuffer(),offset=Number(r.headers()['upload-offset']);assert(bytes.length<=4);assert.equal(r.headers()['x-chunk-sha256'],require('node:crypto').createHash('sha256').update(bytes).digest('hex'));
   if(options.gatePatch && !patchGate){patchGate={};patchGate.promise=new Promise(resolve=>patchGate.release=resolve);patchEntered?.();await patchGate.promise;}
   if(offset===row.committed_bytes){chunks.get(row.id).push({offset,bytes});row.committed_bytes+=bytes.length;}
   else {const prior=chunks.get(row.id).find(chunk=>chunk.offset===offset);assert(prior&&prior.bytes.equals(bytes),'exact retry only');}
   if(options.loseReply&&!lost){lost=true;return route.abort('failed');}
  }else if(parts[6]==='complete'){assert.equal(row.committed_bytes,row.expected_bytes);row.status='received';row.transfer_status='sealed';row.stage='prepare';row.stage_status='queued';}
  else if(method==='DELETE'){row.status=options.cancelPending?'cancelling':'cancelled';row.transfer_status=row.transfer_status==='sealed'?'sealed':'cancelled';}
  return json(route,{import:row,chunk_bytes_max:4});
 });
 if(options.entry==='cameras'){await p.goto(origin+'/?cameras=1'+(options.empty?'':'&collection=cafe'));await p.locator('#camera-upload').waitFor();}
 else {await p.locator('#library-nav').click();await p.keyboard.press('Escape');if(options.empty)await p.waitForFunction(()=>new URL(location.href).searchParams.get('collection')==='company');else await p.waitForFunction(()=>new URL(location.href).searchParams.get('collection')==='cafe');
 if(intake.available&&intake.can_upload)await p.waitForFunction(()=>document.querySelector('#gallery-upload').title==='Upload videos up to 256 MiB');}

 return {...h,rows,keys,calls,chunks,intake,libraryReads:()=>libraryReads,createWaiting:()=>createGate?.release,patchWaiting:()=>patchGate?.release,waitForPatch:()=>new Promise(resolve=>patchEntered=resolve),open:async()=>{await p.locator(options.entry==='cameras'?'#camera-upload':'#gallery-upload').click();},select:async(name='camera.mp4',text='abcdefghijkl')=>p.locator('#gallery-upload-input').setInputFiles({name,mimeType:'video/mp4',buffer:Buffer.from(text)})};
}

test('Gallery upload uses bounded chunks and exact replay after a lost reply; indexing only refreshes when Ready',async()=>{
 const h=await uploadPage(1600,{loseReply:true}),p=h.page;
 try{
  await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();
  assert.equal(h.rows.size,1);assert.equal([...h.rows.values()][0].committed_bytes,12);assert.equal(h.chunks.values().next().value.length,3);
  assert.deepEqual(h.calls.filter(c=>c.method==='PATCH').map(c=>c.offset),['0','0','4','8']);
  const count=h.libraryReads(),row=[...h.rows.values()][0];row.status='preparing';row.stage_status='running';await p.getByText('Preparing video',{exact:true}).waitFor();assert.equal(h.libraryReads(),count);
  row.status='indexing';row.stage='embed';await p.getByText('Indexing video',{exact:true}).waitFor();assert.equal(h.libraryReads(),count);
  row.stage='publish';await p.getByText('Adding to Gallery',{exact:true}).waitFor();assert.equal(h.libraryReads(),count);
  row.status='ready';row.stage=null;row.stage_status=null;await p.getByText('Ready',{exact:true}).waitFor();assert.equal(h.libraryReads(),count+1);
  await p.locator('#library-view [data-gallery-view=list]').click();assert.equal(await p.locator('body').evaluate(el=>el.classList.contains('gallery-list-view')),true);
  const stored=await p.evaluate(()=>JSON.stringify(localStorage));assert(!stored.includes('abcdefghijkl'));assert(!stored.includes('YWJjZGVmZ2hpamts'));
 }finally{await h.close();}
});

test('Gallery pause and reload resume confirm original file identity and retain the opaque transfer',async()=>{
 const h=await uploadPage(390,{gatePatch:true}),p=h.page;
 try{
  const entered=h.waitForPatch();await h.select();await entered;await p.getByRole('button',{name:'Pause camera.mp4',exact:true}).click();h.patchWaiting()();await p.getByText('Paused',{exact:true}).waitFor();
  const row=[...h.rows.values()][0];assert.equal(row.committed_bytes,4);const id=row.id;
  await p.reload();await p.waitForFunction(()=>new URL(location.href).searchParams.get('collection')==='cafe');await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).waitFor();
  await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).click();await h.select('camera.mp4','xxxxefghijkl');await p.getByText('Choose the original file to resume this transfer.',{exact:true}).waitFor();assert.equal(row.committed_bytes,4);assert.equal(h.rows.size,1);
  await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).click();await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(row.id,id);assert.equal(h.rows.size,1);assert.deepEqual(h.calls.filter(c=>c.method==='PATCH').map(c=>c.offset),['0','4','8']);
 }finally{h.patchWaiting()?.();await h.close();}
});

test('Gallery cancellation waits for server confirmation and shows pipeline failures',async()=>{
 const h=await uploadPage(1600,{cancelPending:true}),p=h.page,initialReads=h.libraryReads();
 try{
  await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();await p.getByRole('button',{name:'Cancel camera.mp4',exact:true}).click();await p.getByText('Cancelling…',{exact:true}).waitFor();assert.equal(await p.getByText('Cancelled',{exact:true}).count(),0);
  const row=[...h.rows.values()][0];row.status='cancelled';await p.getByText('Cancelled',{exact:true}).waitFor();
  await h.select('second.mp4');await p.getByText('Waiting for preparation',{exact:true}).waitFor();const failed=[...h.rows.values()][1];failed.status='needs_attention';failed.error='provider_receipt_missing';await p.getByText('Needs attention',{exact:true}).waitFor();assert.equal(await p.getByRole('button',{name:'Retry second.mp4',exact:true}).count(),0);assert.equal(h.libraryReads(),initialReads);
  if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'upload-status-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'upload-status-mobile.png')});}
 }finally{await h.close();}
});

test('Gallery upload admission follows actual pipeline capability and read-only scope',async()=>{
 for(const options of [{available:false},{canUpload:false}]){const h=await uploadPage(390,options);try{await h.page.locator('#gallery-upload').waitFor();assert.equal(await h.page.locator('#gallery-upload').isEnabled(),true);await h.open();await h.page.locator('#source-upload-dialog').waitFor();assert.equal(await h.page.locator('#upload-choose').isDisabled(),true);await h.select();await h.page.waitForTimeout(100);assert.equal(h.calls.filter(c=>c.method!=='GET').length,0);}finally{await h.close();}}
});


test('Gallery lost create receipt reuses one admission and cancelling an uncertain create stops its empty transfer',async()=>{
 for(const lostReplies of [1,2]){
  const h=await uploadPage(1600,{loseCreateReplies:lostReplies,intakeDelay:180}),p=h.page;
  try{
   await h.select();
   if(lostReplies===1){await p.getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(h.calls.filter(call=>call.path==='/api/retail/library/imports'&&call.method==='POST').length,2);}
   else {await p.getByText('Upload paused',{exact:true}).waitFor();await p.getByRole('button',{name:'Cancel camera.mp4',exact:true}).click();await p.getByText('Cancelled',{exact:true}).waitFor();assert.equal(h.calls.filter(call=>call.method==='PATCH').length,0);}
   assert.equal(h.rows.size,1);assert.equal(h.keys.size,1);
  }finally{await h.close();}
 }
});

test('Gallery transfer Retry is bounded, visible and resumes confirmed bytes',async()=>{
 const options={rejectChunks:true},h=await uploadPage(390,options),p=h.page;
 try{await h.select();await p.getByRole('button',{name:'Retry camera.mp4',exact:true}).waitFor();assert.equal(h.calls.filter(call=>call.method==='PATCH').length,1);assert.equal([...h.rows.values()][0].committed_bytes,0);options.rejectChunks=false;await p.getByRole('button',{name:'Retry camera.mp4',exact:true}).click();await p.getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(h.rows.size,1);}finally{await h.close();}
});

test('Gallery cancellation during admission waits for the receipt, then sends Stop without bytes',async()=>{
 const h=await uploadPage(1600,{gateCreate:true}),p=h.page;
 try{
  await h.select();await p.waitForFunction(()=>document.querySelector('.gallery-import-state')?.textContent==='Starting upload…');while(!h.createWaiting())await new Promise(resolve=>setTimeout(resolve,10));
  await p.getByRole('button',{name:'Cancel camera.mp4',exact:true}).click();await p.getByText('Cancelling…',{exact:true}).waitFor();assert.equal(h.calls.filter(call=>call.method==='DELETE').length,0);h.createWaiting()();await p.getByText('Cancelled',{exact:true}).waitFor();assert.equal(h.calls.filter(call=>call.method==='PATCH').length,0);
 }finally{h.createWaiting()?.();await h.close();}
});

test('Gallery empty customer workspace uses only the host-selected collection',async()=>{
 const h=await uploadPage(390,{empty:true}),p=h.page;
 try{assert.equal(new URL(p.url()).searchParams.get('collection'),'company');assert.equal(await p.locator('#library-collection-control').textContent(),'Sample Company');await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal([...h.rows.values()][0].collection,'company');assert(!h.calls.some(call=>call.path.includes('cafe')));}finally{await h.close();}
});

test('Gallery workspace switch fences a late admission receipt and clears local file handles',async()=>{
 const h=await uploadPage(1600,{gateCreate:true}),p=h.page;
 try{
  await h.select();while(!h.createWaiting())await new Promise(resolve=>setTimeout(resolve,10));
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();h.createWaiting()();await p.waitForTimeout(100);assert.equal(h.calls.filter(call=>call.method==='PATCH').length,0);
  assert.equal(await p.locator('#gallery-upload').isDisabled(),true);const local=await p.evaluate(()=>Object.fromEntries(Object.entries(localStorage)));for(const [key,value] of Object.entries(local))if(key.startsWith('rocoto-upload-transfers-v1:')&&key.includes('customer'))assert.equal(value,'[]');
 }finally{h.createWaiting()?.();await h.close();}
});


test('Gallery multiple files retain individual progress and controls within mobile width',async()=>{
 const h=await uploadPage(1600,{gatePatch:true}),p=h.page;
 try{
  const entered=h.waitForPatch();await p.locator('#gallery-upload-input').setInputFiles([{name:'entrance-camera.mp4',mimeType:'video/mp4',buffer:Buffer.from('abcdefghijkl')},{name:'counter-camera.mp4',mimeType:'video/mp4',buffer:Buffer.from('mnopqrstuvwx')}]);await entered;
  assert.equal(await p.locator('.gallery-import').count(),2);assert.equal(await p.getByRole('button',{name:'Pause entrance-camera.mp4',exact:true}).count(),1);
  if(process.env.ROCOTO_UI_PROOF_DIR){await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'uploads-active-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'uploads-active-mobile.png')});}
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await p.getByRole('button',{name:'Cancel counter-camera.mp4',exact:true}).click();h.patchWaiting()();await p.getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(h.rows.size,1);assert.equal(await p.locator('.gallery-import').count(),1);
 }finally{h.patchWaiting()?.();await h.close();}
});

test('Gallery file selection limits stay visible while status polling continues',async()=>{
 const h=await uploadPage(390),p=h.page;
 try{
  await p.locator('#gallery-upload-input').setInputFiles(Array.from({length:5},(_,i)=>({name:`camera-${i}.mp4`,mimeType:'video/mp4',buffer:Buffer.from('test')})));await p.getByText('Choose up to 4 files at a time.',{exact:true}).waitFor();await p.waitForTimeout(1700);assert.equal(await p.getByText('Choose up to 4 files at a time.',{exact:true}).isVisible(),true);assert.equal(h.rows.size,0);
 }finally{await h.close();}
});


test('Gallery selecting a paused file through Upload files resumes its canonical transfer once',async()=>{
 const h=await uploadPage(390,{gatePatch:true}),p=h.page;
 try{
  const entered=h.waitForPatch();await h.select();await entered;await p.getByRole('button',{name:'Pause camera.mp4',exact:true}).click();h.patchWaiting()();await p.getByText('Paused',{exact:true}).waitFor();await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(h.rows.size,1);assert.equal(h.chunks.values().next().value.length,3);assert.equal(await p.locator('.gallery-import').count(),1);
 }finally{h.patchWaiting()?.();await h.close();}
});

test('Gallery delayed polling replies preserve confirmed Ready and avoid duplicate refreshes',async()=>{
 const h=await uploadPage(1600),p=h.page;
 try{
  await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();const row=[...h.rows.values()][0];row.status='ready';row.stage=null;row.stage_status=null;await p.getByText('Ready',{exact:true}).waitFor();const reads=h.libraryReads();row.status='received';row.stage='prepare';row.stage_status='queued';await p.waitForTimeout(1700);assert.equal(await p.getByText('Ready',{exact:true}).isVisible(),true);assert.equal(h.libraryReads(),reads);
 }finally{await h.close();}
});


test('Gallery preserves resume and Stop after the reserved allowance closes new upload admission',async()=>{
 const h=await uploadPage(390,{gatePatch:true}),p=h.page;
 try{
  const entered=h.waitForPatch();await h.select();await entered;
  h.intake.can_upload=false;h.intake.can_manage_imports=true;h.intake.available=false;h.intake.notice='The current upload allowance is reserved.';
  await p.getByText(h.intake.notice,{exact:true}).waitFor();assert.equal(await p.locator('#gallery-upload').isEnabled(),true);
  await p.getByRole('button',{name:'Pause camera.mp4',exact:true}).click();h.patchWaiting()();await p.getByText('Paused',{exact:true}).waitFor();
  await p.reload();await p.waitForFunction(()=>new URL(location.href).searchParams.get('collection')==='cafe');await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).waitFor();
  assert.equal(await p.locator('#gallery-upload').isEnabled(),true);await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).click();await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();
  assert.equal(h.rows.size,1);assert.equal(h.calls.filter(c=>c.method==='POST'&&c.path==='/api/retail/library/imports').length,1);
  await p.getByRole('button',{name:'Cancel camera.mp4',exact:true}).click();await p.getByText('Cancelled',{exact:true}).waitFor();
 }finally{h.patchWaiting()?.();await h.close();}
});

test('Gallery recovers the original admission after reload when its create receipt was lost and allowance is reserved',async()=>{
 const h=await uploadPage(1600,{loseCreateReplies:2}),p=h.page;
 try{
  await h.select();await p.getByText('Upload paused',{exact:true}).waitFor();
  h.intake.can_upload=false;h.intake.can_manage_imports=true;h.intake.available=false;
  await p.reload();await p.waitForFunction(()=>new URL(location.href).searchParams.get('collection')==='cafe');await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).waitFor();
  await p.getByRole('button',{name:'Choose file to resume camera.mp4',exact:true}).click();await h.select();await p.getByText('Waiting for preparation',{exact:true}).waitFor();
  assert.equal(h.rows.size,1);assert.equal(h.keys.size,1);assert.equal(h.calls.filter(c=>c.method==='POST'&&c.path==='/api/retail/library/imports').length,3);
  assert.equal([...h.rows.values()][0].committed_bytes,12);
 }finally{await h.close();}
});


test('Native plugin installation is confirmed separately from account connection',async()=>{
 const h=await pageFor(1600),p=h.page,installed=[];
 try{
  await p.route('**/api/plugins/installed',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({plugins:installed,available:true,installable_ids:['notion']})}));
  await p.route('**/api/plugins/notion/install',async route=>{assert.equal(route.request().method(),'POST');assert.deepEqual(route.request().postDataJSON(),{});installed.push({id:'notion',installed:true,enabled:true,version:'1.0.0',connected:false,callable:false,runtime:'codex'});await route.fulfill({contentType:'application/json',body:JSON.stringify(installed[0])});});
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).click();
  await p.locator('#connection-setup-dialog').waitFor();assert.equal(installed.length,0);
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).waitFor();
  assert.equal(installed.length,1);assert.equal(await p.locator('#customize-installed').getByRole('button',{name:'Notion',exact:true}).count(),1);
  assert.equal(await p.locator('#connection-setup-dialog').getByText('Connected',{exact:true}).count(),0);
 }finally{await h.close();}
});


async function nativePluginPage(options={}) {
 const notion={id:'notion',provider:'notion',name:'Notion',description:'Read and manage Notion.',status:options.connected?'connected':'disconnected',authentication:'oauth',read_only:true};
 const h=await pageFor(options.width || 1600,{connections:[notion]}),p=h.page,installed=options.installed?[{id:'notion',installed:true,enabled:true,version:'1.0.0',runtime:'codex',connected:false,callable:false}]:[],writes=[];let release,started;
 await p.route('**/api/plugins/installed',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({plugins:installed,available:options.available!==false,installable_ids:options.installable || ['notion'],cleanup_pending_ids:options.cleanupPending || []})}));
 await p.route('**/api/plugins/notion/install',async route=>{
  writes.push('install');assert.equal(route.request().method(),'POST');assert.deepEqual(route.request().postDataJSON(),{});
  if(options.gate){started=true;await new Promise(resolve=>release=resolve);}
  if(options.installStatus)return route.fulfill({status:options.installStatus,contentType:'application/json',body:JSON.stringify({detail:'safe fixture error'})});
  const record={id:'notion',installed:true,enabled:true,version:'1.0.0',runtime:'codex',connected:false,callable:false};if(!installed.length)installed.push(record);
  if(options.loseInstallReply)return route.abort('failed');
  return route.fulfill({contentType:'application/json',body:JSON.stringify(record)});
 });
 await p.route('**/api/plugins/notion/installation',async route=>{writes.push('remove');assert.equal(route.request().method(),'DELETE');installed.splice(0);return route.fulfill({contentType:'application/json',body:JSON.stringify({removed:true,plugin_id:'notion',cleanup_confirmed:true})});});
 await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();
 return {...h,installed,pluginWrites:writes,release:()=>release?.(),started:()=>started,open:async()=>p.locator('.connection-hero-actions').getByRole('button',{name:options.installed?'Installed':'Install plugin',exact:true}).click()};
}

test('Native package removal preserves the separately authorized account and preview layout',async()=>{
 const h=await nativePluginPage({installed:true,connected:true}),p=h.page;
 try{
  assert.equal(await p.locator('.connection-account-state').getByText('Connected',{exact:true}).count(),1);
  await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true}).waitFor();
  assert.equal(h.pluginWrites.length,0);
  if(process.env.ROCOTO_UI_REAL_LOGOS==='1')await p.locator('#connection-setup-content img').evaluate(image=>image.decode().catch(()=>{}));
  if(process.env.ROCOTO_UI_PROOF_DIR){for(const width of [1600,390]){await p.setViewportSize({width,height:width===390?844:1000});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`native-plugin-installed-${width}.png`)});assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}}
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true}).click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();
  assert.deepEqual(h.pluginWrites,['remove']);assert.equal(h.installed.length,0);
  assert.equal(await p.locator('.connection-account-state').getByText('Connected',{exact:true}).count(),1);
  assert.equal(h.writes.some(path=>path.startsWith('/api/connections/')),false);
 }finally{await h.close();}
});

test('Native plugin install requires both host readiness and pinned package availability',async()=>{
 for(const options of [{available:false},{installable:['gmail']}]){
  const h=await nativePluginPage(options),p=h.page;
  try{assert.equal(await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).isDisabled(),true);assert.equal(await p.locator('.connection-hero h1').textContent(),'Notion');assert.equal(h.pluginWrites.length,0);}finally{await h.close();}
 }
});

test('Native plugin account connection remains an explicit separate action after install',async()=>{
 const h=await nativePluginPage({width:390}),p=h.page;
 try{
  await h.open();if(process.env.ROCOTO_UI_REAL_LOGOS==='1')await p.locator('#connection-setup-content img').evaluate(image=>image.decode().catch(()=>{}));if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'native-plugin-install-mobile.png')});
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Connect account',exact:true}).waitFor();assert.deepEqual(h.pluginWrites,['install']);
  assert.equal(h.writes.some(path=>path.startsWith('/api/connections/')),false);
  await p.keyboard.press('Escape');await p.locator('#new-chat').click();await p.keyboard.press('Escape');await p.locator('#attachment-toggle').click();assert.equal(await p.locator('#attachment-connections button').first().textContent(),'Notion');await p.locator('#attachment-connections button').first().click();
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Connect',exact:true}).waitFor();
  assert.equal(await p.locator('#connection-setup-dialog').getByText('Connected',{exact:true}).count(),0);
 }finally{await h.close();}
});

test('Native plugin sign-in and install failures stay visible without claiming installation',async()=>{
 for(const installStatus of [401,503]){
  const h=await nativePluginPage({installStatus}),p=h.page;
  try{await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();await p.locator('#connection-setup-dialog [role=alert]').waitFor();assert.equal(await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).count(),0);assert.equal(h.installed.length,0);assert.deepEqual(h.pluginWrites,['install']);if(installStatus===401)assert.equal(await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).isDisabled(),true);}finally{await h.close();}
 }
});

test('Native plugin late install receipt cannot enter an observed workspace',async()=>{
 const h=await nativePluginPage({gate:true}),p=h.page;
 try{
  await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Installing…',exact:true}).waitFor();
  await p.keyboard.press('Escape');await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();h.release();await p.waitForTimeout(100);
  assert.equal(await p.locator('#connection-setup-dialog').isVisible(),false);assert.equal(await p.locator('#customize-installed').getByRole('button',{name:'Notion',exact:true}).count(),0);
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();
  const action=p.locator('.connection-hero-actions button').last();assert.equal(await action.isDisabled(),true);assert.deepEqual(h.pluginWrites,['install']);
 }finally{h.release();await h.close();}
});

test('Native plugin late status fetch cannot erase a confirmed install',async()=>{
 const h=await nativePluginPage(),p=h.page;let release,held=false;
 try{
  await p.route('**/api/plugins/installed',async route=>{held=true;await new Promise(resolve=>release=resolve);return route.fulfill({contentType:'application/json',body:JSON.stringify({plugins:[],available:true,installable_ids:['notion']})});});
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();
  await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).waitFor();
  assert(held);release();await p.waitForTimeout(100);assert.equal(await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).count(),1);assert.deepEqual(h.pluginWrites,['install']);
 }finally{release?.();await h.close();}
});


test('Native plugin lost install reply reconciles the actual receipt without repeating installation',async()=>{
 const h=await nativePluginPage({loseInstallReply:true}),p=h.page;
 try{await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).waitFor();assert.deepEqual(h.pluginWrites,['install']);assert.equal(await p.locator('#connection-setup-dialog [role=alert]').count(),0);}finally{await h.close();}
});

test('Native plugin finishing after another setup opens preserves the current dialog',async()=>{
 const h=await nativePluginPage({gate:true}),p=h.page;
 try{
  await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Installing…',exact:true}).waitFor();await p.keyboard.press('Escape');
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('gmail');await p.locator('.connection-catalogue-item .marketplace-card').click();await p.getByRole('button',{name:'Setup options',exact:true}).click();
  assert.equal(await p.locator('#connection-setup-content h2').textContent(),'Gmail');h.release();await p.waitForTimeout(100);assert.equal(await p.locator('#connection-setup-content h2').textContent(),'Gmail');assert.deepEqual(h.pluginWrites,['install']);
 }finally{h.release();await h.close();}
});


test('Native plugin removal remains available while new installation is paused',async()=>{
 const h=await nativePluginPage({installed:true,available:false}),p=h.page;
 try{await h.open();const remove=p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true});assert.equal(await remove.isEnabled(),true);await remove.click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();assert.equal(h.installed.length,0);assert.deepEqual(h.pluginWrites,['remove']);assert.equal(await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).isDisabled(),true);}finally{await h.close();}
});


test('Native plugin removal keeps a retry action until active work cleanup is confirmed',async()=>{
 const h=await nativePluginPage({installed:true}),p=h.page;let calls=0;
 try{
  await p.route('**/api/plugins/notion/installation',route=>{calls++;h.installed.splice(0);return route.fulfill({status:calls===1?503:200,contentType:'application/json',body:JSON.stringify({removed:true,plugin_id:'notion',cleanup_confirmed:calls>1})});});
  await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true}).click();
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Finish removal',exact:true}).waitFor();
  assert.equal(await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).count(),0);
  assert.equal(await p.locator('#connection-setup-dialog [role=alert]').count(),1);await p.keyboard.press('Escape');
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Finish removal',exact:true}).click();
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Finish removal',exact:true}).click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();assert.equal(calls,2);
 }finally{await h.close();}
});

test('Native plugin pending removal restores from the private server receipt after reload',async()=>{
 const h=await nativePluginPage({cleanupPending:['notion'],available:false}),p=h.page;
 try{const finish=p.locator('.connection-hero-actions').getByRole('button',{name:'Finish removal',exact:true});await finish.waitFor();assert.equal(await finish.isEnabled(),true);await finish.click();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Finish removal',exact:true}).click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();assert.deepEqual(h.pluginWrites,['remove']);}finally{await h.close();}
});

const mcpToken='rmcp_'+ 'private-mcp-fixture-'.repeat(4).slice(0,64);
async function mcpPage(options={}) {
 const h=await pageFor(options.width || 1600),p=h.page,keys=[{id:'mcp-key-existing',label:'Desktop client',dataset:'cafe',created_at:Date.now()/1000,expires_at:Date.now()/1000+604800,revoked_at:null,permission:'video:read'}],mutations=[];let release,gate=new Promise(resolve=>release=resolve);
 await p.route('**/api/mcp/connections**',async route=>{const req=route.request(),pathname=new URL(req.url()).pathname;
  if(req.method()==='POST'){mutations.push(req.postDataJSON());if(options.gate)await gate;const connection={...keys[0],id:'mcp-key-new',label:req.postDataJSON().label,dataset:req.postDataJSON().dataset};keys.push(connection);return route.fulfill({status:options.fail?401:201,contentType:'application/json',body:JSON.stringify(options.fail?{error:'private-error-fixture'}:{connection,url:'https://video-demo.soyrocoto.com/mcp',token:mcpToken})});}
  if(req.method()==='DELETE'){mutations.push({revoke:pathname.split('/').pop()});const connection=keys.find(key=>pathname.endsWith('/'+key.id));connection.revoked_at=Date.now()/1000;return route.fulfill({contentType:'application/json',body:JSON.stringify({connection})});}
  return route.fulfill({contentType:'application/json',body:JSON.stringify({available:options.available!==false,url:'https://video-demo.soyrocoto.com/mcp',connections:keys})});
 });
 const open=async()=>{await p.goto(origin+'/?settings=mcp');await p.locator('#mcp-content').getByText('Desktop client',{exact:true}).waitFor();};
 return {...h,keys,mutations,open,release};
}

test('MCP access settings show scoped metadata and one top title',async()=>{
 const h=await mcpPage(),p=h.page;
 try{await h.open();assert.equal(await p.locator('#chat-title').textContent(),'Rocoto MCP');assert.equal(await p.locator('#settings-mcp').getAttribute('aria-current'),'page');assert.equal(await p.locator('#mcp-content h1').count(),0);assert.equal(await p.locator('#settings-mcp svg').count(),1);assert.equal(await p.getByRole('button',{name:'Create access key',exact:true}).isEnabled(),true);assert.deepEqual(h.mutations,[]);}finally{h.release();await h.close();}
});

async function createMcp(h,label='Codex desktop') {const p=h.page;await p.getByRole('button',{name:'Create access key',exact:true}).click();await p.getByLabel('Key name',{exact:true}).fill(label);await p.getByLabel('Collection',{exact:true}).selectOption('cafe');await p.locator('#mcp-dialog').getByRole('button',{name:'Create access key',exact:true}).click();}
async function assertMcpPrivate(p){assert.equal(await p.evaluate(token=>JSON.stringify({local:Object.entries(localStorage),session:Object.entries(sessionStorage),history:history.state,url:location.href}).includes(token),mcpToken),false);assert.equal(await p.locator('#messages').textContent().then(text=>text.includes(mcpToken)),false);}

test('MCP access creates one masked key, copies it and clears on close and reload',async()=>{
 const h=await mcpPage(),p=h.page;
 try{await p.context().grantPermissions(['clipboard-read','clipboard-write']);await h.open();await p.getByRole('button',{name:'Copy address',exact:true}).click();assert.equal(await p.evaluate(()=>navigator.clipboard.readText()),'https://video-demo.soyrocoto.com/mcp');await createMcp(h);await p.locator('#mcp-secret').waitFor();assert.equal(await p.locator('#mcp-secret').getAttribute('type'),'password');assert.equal(await p.locator('#mcp-secret').inputValue(),mcpToken);await p.getByRole('button',{name:'Show key',exact:true}).click();assert.equal(await p.locator('#mcp-secret').getAttribute('type'),'text');await p.getByRole('button',{name:'Hide key',exact:true}).click();await p.getByRole('button',{name:'Copy key',exact:true}).click();assert.equal(await p.evaluate(()=>navigator.clipboard.readText()),mcpToken);await assertMcpPrivate(p);await p.getByRole('button',{name:'Done',exact:true}).click();assert.equal(await p.locator('#mcp-secret').count(),0);await p.reload();await p.getByText('Codex desktop',{exact:true}).waitFor();assert.equal(await p.locator('#mcp-secret').count(),0);await assertMcpPrivate(p);assert.deepEqual(h.mutations,[{label:'Codex desktop',dataset:'cafe',ttl_seconds:604800}]);}finally{h.release();await h.close();}
});

test('MCP access revokes a chosen key after confirmation',async()=>{
 const h=await mcpPage(),p=h.page;
 try{await h.open();await p.getByRole('button',{name:'Revoke Desktop client',exact:true}).click();assert.deepEqual(h.mutations,[]);await p.getByRole('button',{name:'Revoke access key',exact:true}).click();await p.getByText('Fixture Café · Revoked',{exact:true}).waitFor();assert.equal(await p.locator('#mcp-dialog').isVisible(),false);assert.deepEqual(h.mutations,[{revoke:'mcp-key-existing'}]);}finally{h.release();await h.close();}
});

test('MCP access observer keeps metadata read only',async()=>{
 const h=await mcpPage(),p=h.page;
 try{await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await h.open();assert.equal(await p.getByRole('button',{name:'Create access key',exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Revoke Desktop client',exact:true}).isDisabled(),true);assert.deepEqual(h.mutations,[]);}finally{h.release();await h.close();}
});

test('MCP access discards a late create response after workspace changes',async()=>{
 const h=await mcpPage({gate:true}),p=h.page;
 try{await h.open();await createMcp(h);await p.getByRole('button',{name:'Close MCP setup',exact:true}).click();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();h.release();await p.waitForTimeout(120);assert.equal(await p.locator('#mcp-secret').count(),0);assert.equal(await p.locator('#mcp-dialog').isVisible(),false);await assertMcpPrivate(p);}finally{h.release();await h.close();}
});

test('MCP access clears the key when navigation or authentication changes',async()=>{
 const h=await mcpPage(),p=h.page;
 try{await h.open();await createMcp(h);await p.locator('#mcp-secret').waitFor();await p.keyboard.press('Escape');assert.equal(await p.locator('#mcp-secret').count(),0);await p.locator('#settings-appearance').click();assert.equal(await p.locator('#mcp-dialog').isVisible(),false);await assertMcpPrivate(p);}finally{h.release();await h.close();}
 const denied=await mcpPage({fail:true}),q=denied.page;
 try{await denied.open();await createMcp(denied);await q.locator('#session-screen').waitFor();assert.equal(await q.locator('#mcp-dialog').isVisible(),false);assert.equal(await q.locator('#mcp-secret').count(),0);assert.equal((await q.locator('body').innerText()).includes('private-error-fixture'),false);}finally{denied.release();await denied.close();}
});

test('MCP access fits desktop and mobile with a private key dialog',async()=>{
 for(const width of [1600,390]) {const h=await mcpPage({width}),p=h.page;
 try{await h.open();const body=await p.locator('body').boundingBox();assert(body.width<=width);if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`mcp-settings-${width}.png`)});}await createMcp(h);await p.locator('#mcp-secret').waitFor();const box=await p.locator('#mcp-dialog').boundingBox();assert(box.x>=0&&box.x+box.width<=width);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`mcp-key-${width}.png`)});await assertMcpPrivate(p);}finally{h.release();await h.close();}}
});

test('MCP access preserves a newly created key when an earlier list request finishes late',async()=>{
 const h=await mcpPage(),p=h.page;let release,entered;const gate=new Promise(resolve=>release=resolve),started=new Promise(resolve=>entered=resolve);
 try{await h.open();const snapshot=structuredClone(h.keys);
  await p.route('**/api/mcp/connections',async route=>{if(route.request().method()!=='GET')return route.fallback();entered();await gate;return route.fulfill({contentType:'application/json',body:JSON.stringify({available:true,url:'https://video-demo.soyrocoto.com/mcp',connections:snapshot})});});
  await p.locator('#settings-mcp').click();await started;await createMcp(h);await p.locator('#mcp-secret').waitFor();release();await p.waitForTimeout(100);await p.getByRole('button',{name:'Done',exact:true}).click();assert.equal(await p.getByText('Codex desktop',{exact:true}).count(),1);
 }finally{release();h.release();await h.close();}
});

const originalNativeSkill={id:'native-skill:'+'a'.repeat(64),name:'notion-research-documentation',command:'notion:notion-research-documentation',description:'Research across Notion and synthesize into structured documentation.',publisher:'Notion',plugin_id:'notion',native_plugin:true,installed:true,revision:1,installed_revision:1,latest_revision:1,package_version:'0.1.7',source_url:'https://github.com/openai/plugins/blob/pinned/plugins/notion/skills/notion-research-documentation/SKILL.md'};
async function nativeSkillRoutes(p,available=()=>true){
 await p.route('**/api/skills',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({skills:[skill,...(available()?[originalNativeSkill]:[])]})}));
 await p.route('**/api/skills/catalogue',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({skills:[skill,related,...(available()?[originalNativeSkill]:[])]})}));
}

test('Original installed skills appear as namespaced slash commands and submit only their opaque IDs',async()=>{
 const h=await pageFor(1600,{chatEvents:[{type:'delta',text:'Selected workflow.'},{type:'done',status:'completed'}]}),p=h.page;
 try{
  await nativeSkillRoutes(p);await p.reload();await p.locator('#message').fill('/notion:');
  await p.locator('#command-menu').getByText('/'+originalNativeSkill.command,{exact:true}).click();
  await p.locator('#selected-skills').getByText('/'+originalNativeSkill.command,{exact:true}).waitFor();
  await p.locator('#message').fill('Prepare the requested report.');await p.locator('#composer').evaluate(form=>form.requestSubmit());
  await p.getByText('Selected workflow.',{exact:true}).waitFor();const payload=h.payloads.find(item=>item.path==='/api/chat').body;
  assert.deepEqual(payload.skill_ids,[originalNativeSkill.id]);assert.equal(JSON.stringify(payload).includes('/codex-state'),false);
  await p.locator('#message').fill('/source');await p.locator('#command-menu').getByText('/source-review',{exact:true}).waitFor();
 }finally{await h.close();}
});

test('Original installed skills have usable details and icon slash entries in Plus on desktop and mobile',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width),p=h.page;
  try{
   await nativeSkillRoutes(p);await p.reload();await p.locator('#attachment-toggle').click();await p.locator('#attach-skills').click();
   const option=p.locator('#installed-skill-options').getByRole('button',{name:'/'+originalNativeSkill.command,exact:true});await option.waitFor();assert.equal(await option.locator('svg').count(),1);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'native-skills-plus-'+width+'.png')});
   await option.click();await p.locator('.selected-skill-open').filter({hasText:originalNativeSkill.command}).click();
   const use=p.locator('#skill-dialog').getByRole('button',{name:'Use /'+originalNativeSkill.command,exact:true});await use.waitFor();assert.equal(await use.isEnabled(),true);
   assert.equal(await p.locator('#skill-dialog .skill-instructions').count(),0);assert.equal(await p.locator('#skill-dialog').getByRole('link',{name:'View skill instructions'}).count(),1);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'native-skill-details-'+width+'.png')});
   await use.click();assert.equal(await p.locator('#skill-dialog').isVisible(),false);assert.equal(await p.locator('#selected-skills .selected-skill').count(),1);
  }finally{await h.close();}
 }
});

test('Plugin removal invalidates native drafts even when catalogue refresh is unavailable',async()=>{
 const h=await nativePluginPage({installed:true}),p=h.page;
 try{
  await nativeSkillRoutes(p,()=>h.installed.length>0);await p.goto(origin+'/?chat=fixture-chat');await p.locator('#message').fill('/notion:');await p.locator('#command-menu').getByText('/'+originalNativeSkill.command,{exact:true}).click();
  for(const endpoint of ['**/api/skills','**/api/skills/catalogue'])await p.route(endpoint,route=>h.installed.length?route.fallback():route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'fixture catalogue reconnecting'})}));
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).click();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true}).click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();
  await p.keyboard.press('Escape');await p.locator('#new-chat').click();await p.locator('.chat-link').filter({hasText:'Fixture café review'}).click();await p.locator('#message').fill('/notion:');
  assert.equal(await p.locator('#command-menu').getByText('/'+originalNativeSkill.command,{exact:true}).count(),0);assert.equal(await p.locator('#selected-skills .selected-skill').count(),0);
  await p.locator('#message').fill('/source');await p.locator('#command-menu').getByText('/source-review',{exact:true}).waitFor();
 }finally{await h.close();}
});


test('Original skills ignore an older catalogue response after package removal',async()=>{
 const h=await nativePluginPage({installed:true}),p=h.page;let release,entered;
 const gate=new Promise(resolve=>release=resolve),started=new Promise(resolve=>entered=resolve);let hold=false;
 try{
  await nativeSkillRoutes(p,()=>h.installed.length>0);await p.reload();
  await p.route('**/api/skills/catalogue',async route=>{if(!hold)return route.fallback();hold=false;entered();await gate;return route.fulfill({contentType:'application/json',body:JSON.stringify({skills:[skill,related,originalNativeSkill]})});});
  await p.locator('#marketplace-nav').click();hold=true;await p.locator('#customize-skills').click();await started;
  await p.locator('#marketplace-nav').click();await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();
  await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).click();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true}).click();await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();
  await p.waitForTimeout(80);release();await p.waitForTimeout(80);await p.keyboard.press('Escape');await p.locator('#new-chat').click();await p.locator('#message').fill('/notion:');
  assert.equal(await p.locator('#command-menu').getByText('/'+originalNativeSkill.command,{exact:true}).count(),0);
  await p.locator('#marketplace-nav').click();await p.locator('#customize-skills').click();assert.equal(await p.locator('.marketplace-card').filter({hasText:originalNativeSkill.name}).count(),0);
 }finally{release();h.release();await h.close();}
});

async function controlledAbortDeadlines(page) {
 await page.evaluate(()=>{
  let elapsed=0;const pending=[];
  AbortSignal.timeout=milliseconds=>{const controller=new AbortController();pending.push({controller,at:elapsed+milliseconds});return controller.signal;};
  window.advanceAbortDeadlines=milliseconds=>{elapsed+=milliseconds;for(const item of pending)if(!item.controller.signal.aborted&&item.at<=elapsed)item.controller.abort(new DOMException('The operation timed out.','TimeoutError'));};
 });
}

test('Native plugin slow install keeps Installing across the old deadline and accepts its bounded receipt',async()=>{
 const h=await nativePluginPage({gate:true}),p=h.page;
 try{
  await controlledAbortDeadlines(p);await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Install plugin',exact:true}).click();
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Installing…',exact:true}).waitFor();
  await p.evaluate(()=>window.advanceAbortDeadlines(31000));await p.waitForTimeout(80);
  assert.equal(await p.locator('#connection-setup-dialog').getByRole('button',{name:'Installing…',exact:true}).count(),1);
  assert.equal(await p.locator('#connection-setup-dialog [role=alert]').count(),0);assert.equal(h.installed.length,0);
  await p.evaluate(()=>window.advanceAbortDeadlines(390000));await p.waitForTimeout(80);
  assert.equal(await p.locator('#connection-setup-dialog').getByRole('button',{name:'Installing…',exact:true}).count(),1);
  h.release();await p.locator('.connection-hero-actions').getByRole('button',{name:'Installed',exact:true}).waitFor();
  assert.deepEqual(h.pluginWrites,['install']);assert.equal(h.installed.length,1);assert.equal(await p.locator('#connection-setup-dialog [role=alert]').count(),0);
 }finally{h.release();await h.close();}
});

test('Native plugin removal keeps Removing until bounded worker cleanup confirms',async()=>{
 const h=await nativePluginPage({installed:true}),p=h.page;let release;const gate=new Promise(resolve=>release=resolve);
 try{
  await p.route('**/api/plugins/notion/installation',async route=>{h.pluginWrites.push('remove');await gate;h.installed.splice(0);await route.fulfill({contentType:'application/json',body:JSON.stringify({removed:true,plugin_id:'notion',cleanup_confirmed:true})});});
  await controlledAbortDeadlines(p);await h.open();await p.locator('#connection-setup-dialog').getByRole('button',{name:'Remove plugin',exact:true}).click();
  await p.locator('#connection-setup-dialog').getByRole('button',{name:'Removing…',exact:true}).waitFor();
  await p.evaluate(()=>window.advanceAbortDeadlines(46000));await p.waitForTimeout(80);
  assert.equal(await p.locator('#connection-setup-dialog').getByRole('button',{name:'Removing…',exact:true}).count(),1);
  assert.equal(h.installed.length,1);assert.equal(await p.locator('#connection-setup-dialog [role=alert]').count(),0);
  release();await p.locator('.connection-hero-actions').getByRole('button',{name:'Install plugin',exact:true}).waitFor();assert.equal(h.installed.length,0);assert.deepEqual(h.pluginWrites,['remove']);
 }finally{release();await h.close();}
});

const providerApproval={id:'approval-fixture',kind:'provider_write',root_chat_id:'fixture-chat',actor_chat_id:'fixture-chat',provider:'notion',server_name:'notion',tool:'create_document',status:'pending',arguments_preview:{title:'Delivery review',content:'<script>window.approvalInjected=true</script>\nShipment 54 needs review.'},arguments_sha256:'a'.repeat(64),created_at:new Date().toISOString(),expires_at:new Date(Date.now()+300000).toISOString()};
for(const width of [1600,390])test(`Native approval card preserves exact content and sends one decision at ${width}px`,async()=>{const approval=structuredClone(providerApproval),h=await pageFor(width,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Review this document.',native_approvals:[approval]}]},approvalReply:{action:'allow_once'},approvalDelay:250});const p=h.page;try{const card=p.locator('.native-approval-card');await card.waitFor();if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`approval-${width}.png`)});}assert.equal(await card.locator('.native-approval-arguments').textContent().then(t=>t.includes(approval.arguments_preview.content)),true);assert.equal(await p.evaluate(()=>Boolean(window.approvalInjected)),false);await p.getByRole('button',{name:'Allow once',exact:true}).click();assert.equal(await p.getByRole('button',{name:'Allow once',exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Decline',exact:true}).isDisabled(),true);await card.getByText('Allowed once',{exact:true}).waitFor();assert.equal(h.payloads.length,1);assert.deepEqual(h.payloads[0].body,{action:'allow_once'});await p.reload();await card.getByText('Allowed once',{exact:true}).waitFor();assert.equal(await card.getByRole('button').count(),0);assert.equal(await card.locator('details').evaluate(el=>el.open),false);await p.waitForFunction(width=>{const box=document.querySelector('.native-approval-card')?.getBoundingClientRect();return box&&box.width>0&&box.x>=0&&box.right<=width;},width);assert.deepEqual(h.errors,[]);}finally{await h.close();}});
test('Native approval closes on stale response and observers can only read',async()=>{const approval=structuredClone(providerApproval),h=await pageFor(390,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Review this action.',native_approvals:[approval]}]},approvalReply:{action:'decline'},approvalConflict:true});const p=h.page;try{await p.getByRole('button',{name:'Decline',exact:true}).click();await p.locator('.native-approval-card').getByText('Action closed',{exact:true}).waitFor();assert.equal(await p.locator('.native-approval-card button').count(),0);approval.status='pending';await p.reload();await p.locator('.native-approval-card').waitFor();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor({state:'visible'});assert.equal(await p.locator('.native-approval-card button:enabled').count(),0);assert.deepEqual(h.writes,['/api/chats/fixture-chat/approvals/approval-fixture','/api/observer']);}finally{await h.close();}});

const providerResource={...documentFile,id:'provider-output',name:'Service report',title:'Service report',kind:'provider_resource',representation:'provider_result',provider:'notion',provider_name:'Notion',source_url:'https://www.notion.so/workspace/service-report',size_bytes:null,snapshot_size_bytes:96,preview_format:'text',download_url:null,preview_url:'/api/files/provider-output/content',copyable:true};
test('Provider resource cards open original links and preview returned text literally',async()=>{
 const source='# Service report\n\n![external](https://tracking.example/track.png)\n<script>window.badProviderPreview=true</script>\n\nService was reviewed at 12:30.',h=await pageFor(1600,{files:[providerResource],fileContent:source,chat:{messages:[chat.messages[0],{role:'assistant',content:'The service report is available.',artifacts:[providerResource]}]}}),p=h.page,external=[];
 try{
  await p.context().grantPermissions(['clipboard-read','clipboard-write']);p.on('request',r=>{if(r.url().startsWith('https://tracking.example/'))external.push(r.url());});
  assert.equal(await p.locator('.provider-resource-card .artifact-type').textContent(),'Notion');
  await p.locator('.provider-resource-card .artifact-open-menu > summary').click();
  const link=p.locator('.artifact-open-actions').getByRole('link',{name:'Open provider',exact:true});
  assert.equal(await link.getAttribute('href'),providerResource.source_url);assert.equal(await link.getAttribute('target'),'_blank');assert.equal(await link.getAttribute('rel'),'noopener noreferrer');assert.equal(await link.getAttribute('referrerpolicy'),'no-referrer');
  assert.equal(await p.locator('.artifact-open-actions').getByRole('link',{name:'Download',exact:true}).count(),0);
  await p.locator('.artifact-open-actions').getByRole('button',{name:'Copy link',exact:true}).click();assert.equal(await p.evaluate(()=>navigator.clipboard.readText()),providerResource.source_url);
  await p.locator('.artifact-open-actions').getByRole('button',{name:'Preview',exact:true}).click();await p.locator('#file-preview-content').waitFor();
  assert.equal(await p.locator('#file-preview-content').textContent(),source);assert.equal(await p.locator('#file-preview-content img, #file-preview-content script, #file-preview iframe').count(),0);assert.equal(await p.evaluate(()=>window.badProviderPreview),undefined);assert.deepEqual(external,[]);assert.equal(await p.locator('#file-download').count(),0);
  await p.locator('#file-copy').click();assert.equal(await p.evaluate(()=>navigator.clipboard.readText()),source);
  assert.equal(await p.locator('#file-preview').getByText('Notion · www.notion.so · Saved text preview',{exact:true}).isVisible(),true);
  if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'provider-output-desktop.png')});await p.setViewportSize({width:390,height:844});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'provider-output-mobile.png')});}
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await p.keyboard.press('Escape');await p.reload();await p.locator('.provider-resource-card').waitFor();assert.equal(await p.locator('.provider-resource-card .artifact-type').textContent(),'Notion');assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});
test('Provider links in Gallery keep binary resources explicit without fetching or embedding them',async()=>{
 const file={...providerResource,media_type:'application/pdf',preview_format:null,copyable:false,description:'Published service report.'},h=await pageFor(390,{files:[file],chat:{messages:[chat.messages[0],{role:'assistant',content:'The report is available.',artifacts:[file]}]}}),p=h.page,requests=[];
 try{
  p.on('request',r=>requests.push(r.url()));await p.locator('.provider-resource-card .artifact-card-main').click();
  await p.locator('#file-preview').getByRole('link',{name:'Open provider',exact:true}).waitFor();assert.equal(await p.locator('#file-preview iframe, #file-preview img, #file-copy, #file-download').count(),0);assert.equal(await p.getByRole('combobox',{name:'Document zoom'}).isVisible(),false);assert.equal(await p.getByText('Published service report.',{exact:true}).isVisible(),true);
  assert.equal(requests.some(url=>url.includes('/api/files/provider-output/content')||url.includes('/preview')||url.includes('notion.so')),false);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'provider-link-mobile.png')});
  await p.keyboard.press('Escape');await p.locator('#library-nav').click();await p.keyboard.press('Escape');await p.locator('#workspace-files-tab').click();await p.locator('#generated-files .workspace-file-open').click();await p.locator('#full-file-actions').getByRole('link',{name:'Open provider',exact:true}).waitFor();assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});
test('Provider resource history rejects unsafe links while keeping its returned text preview',async()=>{
 for(const source_url of ['javascript:alert(1)','https://user:secret@example.com/a','https://127.0.0.1/a','https://service.internal/a']){
  const file={...providerResource,source_url},h=await pageFor(390,{files:[file],fileContent:'Saved source text.',chat:{messages:[chat.messages[0],{role:'assistant',content:'The source is available.',artifacts:[file]}]}}),p=h.page;
  try{await p.locator('.provider-resource-card .artifact-card-main').click();await p.locator('#file-preview-content').waitFor();assert.equal(await p.locator('#file-preview').getByRole('link',{name:'Open provider',exact:true}).count(),0);assert.equal(await p.locator('#file-preview-content').textContent(),'Saved source text.');assert.deepEqual(h.writes,[]);}finally{await h.close();}
 }
});

test('Running custom connection checks can be stopped without revoking access',async()=>{
 const setup={runtime_available:true,active_status:'active',active_revision:2,active_generation:1,target:{origin:'https://orders.example.com'},tools:[{name:'find_orders',description:'Find orders.'}],last_test:{id:'checking',status:'pending',revision:2},execution_status:'running'};
 const item={...customConnector,execution_setup:setup};
 const h=await pageFor(1600,{customConnectors:[item]}),p=h.page,actions=[];
 try{
  await p.route('**/api/connections/custom/custom-erp/execution',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(setup)}));
  await p.route('**/api/connections/custom/custom-erp/execution/stop',route=>{actions.push('stop');setup.last_test.status='stopped';setup.execution_status='idle';return route.fulfill({contentType:'application/json',body:JSON.stringify(setup)});});
  await p.route('**/api/connections/custom/custom-erp',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(item)}));
  await p.goto(origin+'/?customize=1&tab=connections&item=custom-erp');
  await p.getByRole('button',{name:'Stop checks',exact:true}).click();await p.getByText('Connection checks stopped.',{exact:true}).waitFor();
  assert.equal(await p.getByRole('button',{name:'Run checks',exact:true}).isEnabled(),true);
  assert.equal(await p.getByRole('button',{name:'Remove access',exact:true}).isVisible(),true);
  assert.deepEqual(actions,['stop']);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}
});

const nativeOffice={...documentFile,id:'file-'+'a'.repeat(32),name:'Service review.docx',title:'Service review',media_type:'application/octet-stream',representation:'native_original',preview_format:null,copyable:false,download_url:'/api/files/file-'+'a'.repeat(32)+'/download'};
function officeResult(){return {file:{...nativeOffice,inline_preview_type:'pages',preview_format:'pages',page_preview_available:true,page_count:2,copyable:true},content:'First document page\nExact original content: Café — 123.\nVisible table\n\fSecond document page',pages:[{page:1,width:1237,height:1600},{page:2,width:1237,height:1600}],preview_status:'ready'};}
async function openOffice(p){await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').click();}
test('Native Office preview renders actual pages in the split viewer and preserves original download',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{files:[nativeOffice],fileContent:null}),p=h.page,submitted=[];
 try{
  await p.context().grantPermissions(['clipboard-read','clipboard-write']);
  await p.route('**/api/files/*/page-preview',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({file:{...nativeOffice,page_preview_available:true},preview_status:'available',pages:[],content:null})}));
  await p.route('**/api/files/*/preview',r=>{assert.equal(r.request().headers()['x-rocoto-page'],'1');submitted.push(r.request().postDataJSON());return r.fulfill({contentType:'application/json',body:JSON.stringify(officeResult())});});
  await p.route('**/api/files/*/pages/*',r=>{const n=new URL(r.request().url()).pathname.split('/').at(-1),actual=process.env.ROCOTO_DOCUMENT_PAGES;return r.fulfill({contentType:'image/png',body:actual?fs.readFileSync(path.join(actual,`page-${n.padStart(4,'0')}.png`)):Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+cM5sAAAAASUVORK5CYII=','base64')});});
  await openOffice(p);await p.locator('.file-preview-page').first().waitFor();assert.equal(await p.locator('.file-preview-page').count(),2);assert.equal(await p.locator('.file-pdf-frame').count(),0);assert.deepEqual(submitted,[{}]);
  assert.equal(await p.locator('#file-download').getAttribute('href'),origin+nativeOffice.download_url);await p.locator('#file-copy').click();assert((await p.evaluate(()=>navigator.clipboard.readText())).includes('Exact original content'));
  const fit=await p.locator('.file-preview-page img').first().boundingBox();const view=await p.locator('.file-pages-viewport').boundingBox();assert(fit.width<=view.width&&fit.height<view.height);await p.getByRole('combobox',{name:'Document zoom'}).selectOption('1.25');assert((await p.locator('.file-preview-page img').first().boundingBox()).width>fit.width);await p.getByRole('combobox',{name:'Document zoom'}).selectOption('fit');
  if(process.env.ROCOTO_UI_PROOF_DIR){await p.locator('.file-preview-page img').first().evaluate(image=>image.decode());await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`office-preview-${width}.png`)});}
  await p.keyboard.press('Escape');assert.equal(await p.locator('#chat-files-panel').isVisible(),false);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}}
});
test('Native Office closed availability preserves useful original download without starting conversion',async()=>{const h=await pageFor(390,{files:[nativeOffice],fileContent:null}),p=h.page;let calls=0;try{await p.route('**/api/files/*/page-preview',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({file:{...nativeOffice,page_preview_available:false},preview_status:'unavailable',pages:[],content:null})}));await p.route('**/api/files/*/preview',r=>{calls++;return r.fulfill({status:503,body:'{}'});});await openOffice(p);await p.getByText('Download this file to open its full contents.',{exact:true}).waitFor();assert.equal(calls,0);assert.equal(await p.locator('#file-download').isVisible(),true);}finally{await h.close();}});
test('Native Office late availability after closing the viewer never starts conversion',async()=>{const h=await pageFor(1600,{files:[nativeOffice],fileContent:null}),p=h.page;let waiting,posts=0;try{await p.route('**/api/files/*/page-preview',r=>{waiting=r;});await p.route('**/api/files/*/preview',r=>{posts++;return r.fulfill({contentType:'application/json',body:JSON.stringify(officeResult())});});await openOffice(p);await p.waitForFunction(()=>document.querySelector('#file-preview').textContent.includes('Loading preview'));for(let i=0;!waiting&&i<50;i++)await p.waitForTimeout(10);assert(waiting);await p.keyboard.press('Escape');await waiting.fulfill({contentType:'application/json',body:JSON.stringify({file:{...nativeOffice,page_preview_available:true},preview_status:'available'})}).catch(()=>{});await p.waitForTimeout(50);assert.equal(posts,0);assert.equal(await p.locator('#chat-files-panel').isVisible(),false);}finally{await h.close();}});
test('Native Office observer reads prepared pages and never admits a conversion',async()=>{const h=await pageFor(390,{files:[nativeOffice],fileContent:null}),p=h.page;let ready=false,posts=0;try{await p.route('**/api/files/*/page-preview',r=>r.fulfill({contentType:'application/json',body:JSON.stringify(ready?officeResult():{file:{...nativeOffice,page_preview_available:true},preview_status:'available',pages:[],content:null})}));await p.route('**/api/files/*/preview',r=>{posts++;return r.fulfill({status:503,body:'{}'});});await p.route('**/api/files/*/pages/*',r=>r.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+cM5sAAAAASUVORK5CYII=','base64')}));await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.goto(origin+'/?chat=fixture-chat');await openOffice(p);await p.getByText('Download this file to open its full contents.',{exact:true}).waitFor();assert.equal(posts,0);await p.keyboard.press('Escape');ready=true;await p.reload();await openOffice(p);await p.locator('.file-preview-page').first().waitFor();assert.equal(posts,0);assert.equal(await p.getByRole('button',{name:'Request edits',exact:true}).isDisabled(),true);}finally{await h.close();}});
test('Native Office preparation failure keeps original download and a visible retry explanation',async()=>{const h=await pageFor(1600,{files:[nativeOffice],fileContent:null}),p=h.page;try{await p.route('**/api/files/*/page-preview',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({file:{...nativeOffice,page_preview_available:true},preview_status:'available'})}));await p.route('**/api/files/*/preview',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'document_preview_unavailable'})}));await openOffice(p);await p.getByText('Open the file again to prepare its preview. The original is ready to download.',{exact:true}).waitFor();assert.equal(await p.locator('#file-download').isVisible(),true);assert.equal(await p.locator('.file-preview-page').count(),0);}finally{await h.close();}});
test('Generated Office uses the same actual page viewer and copies exact source text',async()=>{const generated={...nativeOffice,representation:undefined,preview_format:'markdown',copyable:true,media_type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},source='# Exact **source**';const h=await pageFor(1600,{files:[generated],fileContent:source}),p=h.page;try{await p.context().grantPermissions(['clipboard-read','clipboard-write']);await p.route('**/api/files/*/page-preview',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({...officeResult(),file:{...generated,inline_preview_type:'pages',preview_format:'pages',copy_source:'original_source'},content:source})}));await p.route('**/api/files/*/pages/*',r=>r.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+cM5sAAAAASUVORK5CYII=','base64')}));await openOffice(p);await p.locator('.file-preview-page').first().waitFor();assert.equal(await p.locator('#file-copy').getAttribute('title'),'Copy source text');await p.locator('#file-copy').click();assert.equal(await p.evaluate(()=>navigator.clipboard.readText()),source);assert.equal(await p.locator('#file-download').getAttribute('href'),origin+generated.download_url);}finally{await h.close();}});

const queueStorage='rocoto-video-queue-v1:demo%3Aowner';
const queueAttachment={id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',url:'/attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png',name:'Counter.png',mime:'image/png'};
const queueClip={id:'fixture-clip',camera:'Front counter',dataset:'cafe',start:0,end:8,thumbnail:'/thumbnails/fixture-clip.jpg'};
async function queuePage(width=1600,options={}){
 const h=await pageFor(width,{chat:{running:true},...options}),p=h.page;
 await p.route('**/attachments/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png',r=>r.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" fill="#777"/><path d="M8 12h32M8 22h25M8 32h30" stroke="white"/></svg>'}));
 const items=[{id:'queue-first',chat_id:chat.id,query:'Review this image and clip with the selected skill. Keep the detailed observations together for the final source review.',attachments:[queueAttachment],clip_attachments:[queueClip],skill_ids:[skill.id],dataset:'cafe'},{id:'queue-second',chat_id:chat.id,query:'Then summarize the findings.',attachments:[],clip_attachments:[],skill_ids:[],dataset:'cafe'}];
 await p.evaluate(({key,items})=>localStorage.setItem(key,JSON.stringify(items)),{key:queueStorage,items});await p.reload();await p.locator('.queued-message').first().waitFor();return {...h,items};
}
async function readQueue(p){return p.evaluate(key=>JSON.parse(localStorage.getItem(key)||'[]'),queueStorage);}
for(const width of [1600,390])test(`Queued message matches compact reference and restores every attachment at ${width}px`,async()=>{
 const h=await queuePage(width),p=h.page;
 try{
  const row=p.locator('.queued-message').first();await row.locator('.queued-thumbnail').waitFor();
  assert.equal(await row.locator('.queued-leading-icon path').count(),1);assert.equal(await row.getByRole('button',{name:'Steer',exact:true}).locator('svg path').count(),1);
  assert.equal(await row.locator('.queued-thumbnail').getAttribute('src'),queueAttachment.url);
  await p.waitForFunction(()=>{const el=document.querySelector('.queued-text');return el && getComputedStyle(el).whiteSpace==='nowrap' && getComputedStyle(el).textOverflow==='ellipsis' && el.clientWidth>0 && el.scrollWidth>el.clientWidth;});
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`queue-${width}.png`)});}
  await row.getByRole('button',{name:'Queued message options'}).click();await p.getByRole('button',{name:'Edit queued message',exact:true}).click();
  assert.equal(await p.locator('#message').inputValue(),h.items[0].query);assert.deepEqual((await readQueue(p)).map(v=>v.id),['queue-second']);
  assert.equal(await p.locator('#attachment-previews img').getAttribute('src'),queueAttachment.url);
  assert.equal(await p.locator('#clip-attachment-previews').getByRole('button',{name:'Front counter · 0:00–0:08'}).count(),1);
  assert.equal(await p.locator('#selected-skills .selected-skill').count(),1);
  await p.locator('#composer').evaluate(form=>form.requestSubmit());const restored=(await readQueue(p)).at(-1);
  assert.equal(restored.query,h.items[0].query);assert.deepEqual(restored.attachments,[queueAttachment]);assert.deepEqual(restored.clip_attachments,[queueClip]);assert.deepEqual(restored.skill_ids,[skill.id]);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});
test('Queued Edit preserves composer text, images, clips and skills until each is cleared',async()=>{
 const h=await queuePage(),p=h.page;
 try{
  await p.locator('.queued-message').first().getByRole('button',{name:'Queued message options'}).click();await p.getByRole('button',{name:'Edit queued message',exact:true}).click();
  const open=async()=>{await p.locator('.queued-message').getByRole('button',{name:'Queued message options'}).click();return p.getByRole('button',{name:'Edit queued message',exact:true});};
  assert.equal(await (await open()).isDisabled(),true);await p.keyboard.press('Escape');await p.locator('#message').fill('');
  assert.equal(await (await open()).isDisabled(),true);await p.keyboard.press('Escape');await p.getByRole('button',{name:'Remove attachment Counter.png',exact:true}).click();
  assert.equal(await (await open()).isDisabled(),true);await p.keyboard.press('Escape');await p.getByRole('button',{name:'Remove clip Front counter',exact:true}).click();
  assert.equal(await (await open()).isDisabled(),true);await p.keyboard.press('Escape');await p.getByRole('button',{name:'Remove skill Source Review',exact:true}).click();
  assert.equal(await (await open()).isEnabled(),true);await p.keyboard.press('Escape');assert.deepEqual((await readQueue(p)).map(i=>i.id),['queue-second']);
  await p.locator('.queued-message').getByRole('button',{name:'Remove queued message:'}).click();assert.deepEqual(await readQueue(p),[]);assert.equal(await p.locator('#message-queue').isVisible(),false);
 }finally{await h.close();}
});
test('Queued Steer fences Edit and removal until one exact request completes',async()=>{
 const h=await queuePage(),p=h.page,sent=[];let release;const hold=new Promise(r=>release=r);
 try{
  await p.route('**/api/chat/fixture-chat/steer',async route=>{sent.push(route.request().postDataJSON());await hold;await route.fulfill({contentType:'application/json',body:'{}'});});
  const row=p.locator('.queued-message').first();await row.getByRole('button',{name:'Steer',exact:true}).click();await p.waitForFunction(()=>document.querySelector('.queued-message .steer-button').disabled);
  assert.equal(await row.getByRole('button',{name:'Remove queued message:'}).isDisabled(),true);
  await row.getByRole('button',{name:'Queued message options'}).click();assert.equal(await p.getByRole('button',{name:'Edit queued message',exact:true}).isDisabled(),true);await p.keyboard.press('Escape');
  assert.equal((await readQueue(p)).length,2);release();await p.waitForFunction(key=>JSON.parse(localStorage.getItem(key)).length===1,queueStorage);
  assert.deepEqual(sent,[{message:h.items[0].query,attachments:[queueAttachment.id],clip_attachments:['fixture-clip'],skill_ids:[skill.id]}]);assert.equal(await p.locator('#message').inputValue(),'');
 }finally{release();await h.close();}
});
test('Queued menu closes with Escape and idle queue keeps Send next',async()=>{
 const h=await queuePage(390,{chat:{running:false}}),p=h.page;
 try{
  assert.equal(await p.getByRole('button',{name:'Steer',exact:true}).count(),0);assert.equal(await p.getByRole('button',{name:'Send next',exact:true}).isVisible(),true);
  const toggle=p.locator('.queued-message').first().getByRole('button',{name:'Queued message options'});await toggle.click();await p.getByRole('button',{name:'Edit queued message',exact:true}).waitFor();await p.keyboard.press('Escape');assert.equal(await p.getByRole('button',{name:'Edit queued message',exact:true}).isVisible(),false);
 }finally{await h.close();}
});

test('Queued Edit stays open across progress refresh and rechecks a newer composer draft',async()=>{
 const h=await queuePage(),p=h.page;
 try{
  await p.locator('.queued-message').first().getByRole('button',{name:'Queued message options'}).click();await p.waitForTimeout(2800);
  assert.equal(await p.getByRole('button',{name:'Edit queued message',exact:true}).isVisible(),true);
  await p.locator('#message').evaluate(el=>{el.value='A newer draft';el.dispatchEvent(new Event('input',{bubbles:true}));});
  await p.getByRole('button',{name:'Edit queued message',exact:true}).click();assert.equal(await p.locator('#message').inputValue(),'A newer draft');assert.equal((await readQueue(p)).length,2);
 }finally{await h.close();}
});
test('Queued Steer failure retains attachments and a late reply stays in its original session',async()=>{
 const h=await queuePage(),p=h.page;let release;const held=new Promise(r=>release=r);
 try{
  await p.route('**/api/chat/fixture-chat/steer',route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'Ready for another attempt.'})}));
  await p.locator('.queued-message').first().getByRole('button',{name:'Steer',exact:true}).click();await p.getByText('Ready for another attempt.',{exact:true}).waitFor();assert.equal((await readQueue(p)).length,2);
  assert.equal(await p.locator('.queued-message').first().getByRole('button',{name:'Remove queued message:'}).isEnabled(),true);
  await p.route('**/api/chat/fixture-chat/steer',async route=>{await held;await route.fulfill({contentType:'application/json',body:'{}'});});
  await p.locator('.queued-message').first().getByRole('button',{name:'Steer',exact:true}).click();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor({state:'visible'});
  release();await p.waitForTimeout(120);assert.equal(await p.locator('.queued-message').count(),0);assert.equal(await p.locator('#message').inputValue(),'');assert.equal(await p.locator('.message').filter({hasText:h.items[0].query}).count(),0);
 }finally{release();await h.close();}
});

test('Queued observer with can_query retains read-only controls and every queued item',async()=>{
 const h=await queuePage(),p=h.page;
 try{
  await p.route('**/api/session',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({authenticated:true,can_query:true,read_only:true,role:'owner',principal:{id:'owner',display_name:'Fixture Owner'},effective_principal_id:'owner',environment:{id:'demo',name:'Demo company'},viewing_as:{principal:{id:'owner',display_name:'Fixture Owner'},environment:{id:'demo',name:'Demo company'},read_only:true}})}));
  await p.reload();await p.locator('.queued-message').first().waitFor();assert.equal(await p.locator('.queued-message .steer-button:enabled').count(),0);assert.equal(await p.locator('.queued-remove:enabled').count(),0);
  await p.locator('.queued-message').first().getByRole('button',{name:'Queued message options'}).click();assert.equal(await p.getByRole('button',{name:'Edit queued message',exact:true}).isDisabled(),true);assert.equal((await readQueue(p)).length,2);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});
for(const boundary of ['read_only','parent_id'])test(`Queued ${boundary} transition blocks controls and completion drain before removing an item`,async()=>{
 const h=await queuePage(),p=h.page,mutations=[];let running=true;
 try{
  await p.route('**/api/chat',route=>{mutations.push(route.request().postDataJSON());return route.fulfill({status:403,contentType:'application/json',body:'{"detail":"Read-only workspace."}'});});
  await p.route('**/api/chats/fixture-chat',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({...chat,running,[boundary]:boundary==='parent_id'?'parent-fixture':true})}));
  await p.waitForFunction(()=>{const buttons=[...document.querySelectorAll('.queued-message .steer-button,.queued-remove')];return buttons.length>0&&buttons.every(button=>button.disabled);});
  running=false;await p.waitForFunction(()=>document.querySelector('.queue-heading button')?.disabled===true);
  assert.equal((await readQueue(p)).length,2);assert.deepEqual(mutations,[]);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});

test('Custom late target approval preserves the newer access dialog',async()=>{
 const target={origin:'https://orders.example.com',authentication:{kind:'none'},operations:[{id:'orders',method:'GET',path:'/v1/orders',query_schema:{type:'object',properties:{},additionalProperties:false}}]};
 const make=(id,name)=>({...customConnector,id,name,execution_setup:{runtime_available:true,active_status:'unconfigured',active_generation:0,target_generation:0,active_revision:null,target:null,last_test:null,tools:[{name:'find_orders',description:'Find orders.'}],proposal:{id:'target-'+id,current:true,reason:'Read orders.',target}}});
 const h=await pageFor(1600,{customConnectors:[make('custom-a','First adapter'),make('custom-b','Second adapter')]}),p=h.page;let release;const blocked=new Promise(r=>release=r);
 try{
  await p.route('**/api/connections/custom/custom-a/execution/target',async route=>{await blocked;await route.fulfill({contentType:'application/json',body:'{}'});});
  await p.goto(origin+'/?customize=1&tab=connections&item=custom-a');await p.getByRole('button',{name:'Review access',exact:true}).click();await p.getByRole('button',{name:'Approve access',exact:true}).click();await p.keyboard.press('Escape');
  await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('.custom-connector-card').filter({hasText:'Second adapter'}).getByRole('button').click();await p.getByRole('button',{name:'Review access',exact:true}).click();
  assert.equal(await p.getByRole('dialog',{name:'Review connection access'}).isVisible(),true);release();await p.waitForTimeout(200);
  assert.equal(await p.getByRole('dialog',{name:'Review connection access'}).isVisible(),true,'Earlier target response closed the second adapter’s access dialog');
 }finally{release();await h.close();}
});

const storageTarget={host:'recordings.example.com',port:22,root:'/camera-recordings',host_key:'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEvidenceFixturePublicServerKey',mode:'watch',stable_seconds:60,poll_seconds:120,authentication:{username_field:'SFTP_USER',password_field:'SFTP_PASSWORD'}};
function storageFixture(mode='watch') { const target={...storageTarget,mode};return {status:'setup',generation:0,target:null,proposal:{id:'storage-target-fixture',revision:2,generation:0,current:true,target,reason:'Import completed recordings from the archive folder.',host_key_fingerprint:'SHA256:fixture-reviewed-server-key'},source_type:'incremental_folder',host_key_fingerprint:null,credentials_ready:true,runtime_available:true,funded_ready:true,can_run:false,can_pause:false,acquisition:{status:'idle',cleanup_confirmed:true,last_check:null,last_import:null,next_check:null,error:null}}; }
async function storageHarness(width=1600,{setup=storageFixture(),files=[]}={}) {
 const item={...customConnector,id:'custom-storage',name:'Recording archive',kind:'sftp',chat_id:'storage-builder',chat_url:'/?chat=storage-builder',description:'Import recordings from our archive folder.',summary:'Import completed recordings from your storage folder.',instructions:'Review the server and folder, then choose when to import.',secret_fields:[{name:'SFTP_USER',label:'Username',description:'Private storage login.',configured:true},{name:'SFTP_PASSWORD',label:'Password',description:'Private storage password.',configured:true}],storage_setup:setup};
 const h=await pageFor(width,{customConnectors:[item]}),p=h.page;let fail='',pausePending=false;
 await p.route('**/api/connections/custom/custom-storage',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({...item,storage_setup:setup})}));
 await p.route('**/api/retail/library/imports?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({imports:files,intake:{available:true,can_upload:false,can_manage_imports:true,collections:['cafe']}})}));
 await p.route('**/api/retail/storage/custom-storage**',route=>{const method=route.request().method(),part=new URL(route.request().url()).pathname.split('/').at(-1);if(method!=='GET'){h.writes.push(new URL(route.request().url()).pathname);const body=route.request().postDataJSON();h.payloads.push({path:part,body});if(fail)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:fail})});if(part==='approve'){assert.deepEqual(body,{proposal_id:setup.proposal.id});setup.target=structuredClone(setup.proposal.target);setup.status='configured';setup.generation++;setup.proposal.current=false;setup.can_run=true;}else if(part==='run'){assert.deepEqual(body,{expected_generation:setup.generation});setup.can_run=false;setup.can_pause=true;setup.acquisition={...setup.acquisition,status:'running',cleanup_confirmed:false,source_id:'source-storage'};}else if(part==='pause'){assert.deepEqual(body,{expected_generation:setup.generation});if(setup.status!=='paused')setup.generation++;setup.status='paused';setup.can_run=false;setup.can_pause=pausePending;setup.acquisition.status=pausePending?'stopping':'paused';setup.acquisition.cleanup_confirmed=!pausePending;}}
  return route.fulfill({status:part==='pause'&&pausePending?202:200,contentType:'application/json',body:JSON.stringify(setup)});});
 return {...h,setup,item,files,setFailure:value=>{fail=value;},setPausePending:value=>{pausePending=value;},open:async()=>{await p.goto(origin+'/?customize=1&tab=connections&item=custom-storage');await p.locator('.storage-setup').waitFor();},approve:async()=>{await p.getByRole('button',{name:'Review folder',exact:true}).click();await p.getByRole('button',{name:'Approve folder',exact:true}).click();await p.locator('#connection-setup-dialog').waitFor({state:'hidden'});}};
}

test('SFTP folder review preserves exact server identity and approval stays separate from importing at desktop and mobile',async()=>{
 for(const width of [1600,390]){const h=await storageHarness(width),p=h.page;try{
  await h.open();await p.getByRole('button',{name:'Review folder',exact:true}).click();const modal=p.locator('#connection-setup-dialog');
  assert.equal(await modal.getAttribute('aria-label'),'Review storage folder');assert((await modal.textContent()).includes(storageTarget.host));assert((await modal.textContent()).includes(storageTarget.root));assert((await modal.textContent()).includes('SHA256:fixture-reviewed-server-key'));
  await modal.getByText('Exact server key',{exact:true}).click();assert.equal(await modal.locator('.storage-key-value').textContent(),storageTarget.host_key);await modal.getByText('Exact server key',{exact:true}).click();assert.deepEqual(h.writes,[]);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`storage-folder-review-${width}.png`)});
  await p.keyboard.press('Escape');assert.equal(await p.getByRole('button',{name:'Review folder',exact:true}).isVisible(),true);await h.approve();
  assert.equal((await p.locator('.storage-import-state').textContent()).trim(),'Configured');assert.equal(h.writes.length,1);assert(h.writes[0].endsWith('/approve'));assert.equal(await p.getByRole('button',{name:'Watch folder',exact:true}).isEnabled(),true);assert.equal(await p.getByText('Connected',{exact:true}).count(),0);
  assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 }finally{await h.close();}}
});

test('SFTP import controls follow host readiness and credential setup keeps the chat path',async()=>{
 const setup=storageFixture('one_time');setup.status='configured';setup.target=setup.proposal.target;setup.proposal.current=false;setup.runtime_available=false;setup.credentials_ready=false;setup.funded_ready=false;
 const h=await storageHarness(390,{setup}),p=h.page;try{await h.open();assert.equal(await p.getByRole('button',{name:'Import once',exact:true}).isDisabled(),true);assert((await p.locator('.storage-setup').textContent()).includes('Complete the private login form in the setup chat.'));assert.equal(await p.locator('.custom-secret-field input[type=password]').count(),2);
  await p.getByRole('button',{name:'Continue setup',exact:true}).click();await p.waitForURL(/chat=storage-builder/);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});

test('SFTP one-time and watched imports use approved mode and Pause waits for confirmed cleanup',async()=>{
 for(const mode of ['one_time','watch']){const h=await storageHarness(1600,{setup:storageFixture(mode)}),p=h.page;try{await h.open();await h.approve();await p.getByRole('button',{name:mode==='watch'?'Watch folder':'Import once',exact:true}).click();await p.getByRole('button',{name:'Pause imports',exact:true}).waitFor();assert.equal(h.payloads.find(row=>row.path==='run').body.expected_generation,1);
  h.setPausePending(true);await p.getByRole('button',{name:'Pause imports',exact:true}).click();await p.getByRole('button',{name:'Retry pause',exact:true}).waitFor();assert.equal((await p.locator('.storage-import-state').textContent()).trim(),'Pausing imports…');assert.equal(await p.getByRole('button',{name:'Retry pause',exact:true}).isEnabled(),true);await p.getByRole('button',{name:'Retry pause',exact:true}).click();await p.waitForTimeout(50);
  h.setup.acquisition.status='paused';h.setup.acquisition.cleanup_confirmed=true;h.setup.can_pause=false;await p.waitForFunction(()=>document.querySelector('.storage-import-state')?.textContent.trim()==='Paused');assert.equal(h.writes.filter(path=>path.endsWith('/run')).length,1);assert.equal(h.writes.filter(path=>path.endsWith('/pause')).length,2);assert.equal(h.setup.generation,2);
 }finally{await h.close();}}
});

test('SFTP progress follows only its source and keeps indexing distinct from Ready',async()=>{
 const setup=storageFixture();setup.status='configured';setup.target=setup.proposal.target;setup.proposal.current=false;setup.acquisition={...setup.acquisition,status:'waiting',source_id:'source-storage',last_check:'2026-10-05T12:00:00Z',next_check:'2026-10-05T12:02:00Z'};setup.can_pause=true;
 const files=[{id:'upload-one',source_id:'source-storage',collection:'cafe',filename:'Counter morning.mp4',status:'indexing',stage:'embed',stage_status:'running',expected_bytes:8192,committed_bytes:8192},{id:'upload-other',source_id:'source-other',collection:'cafe',filename:'Other folder private.mp4',status:'ready',expected_bytes:1024,committed_bytes:1024}];
 for(const width of [1600,390]){const h=await storageHarness(width,{setup:structuredClone(setup),files:structuredClone(files)}),p=h.page;try{await h.open();await p.locator('.storage-import-files').waitFor();assert((await p.locator('.storage-import-files').textContent()).includes('Indexing video'));assert(!(await p.locator('.storage-import-files').textContent()).includes('Other folder'));assert.equal((await p.locator('.storage-import-state').textContent()).trim(),'Watching folder');
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`storage-import-progress-${width}.png`)});
  h.files[0].status='ready';await p.locator('.storage-import-files .gallery-import-state').filter({hasText:'Ready'}).waitFor();assert.equal(h.writes.length,0);assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 }finally{await h.close();}}
});

test('SFTP stale approval and observed workspaces keep folder actions scoped',async()=>{
 const h=await storageHarness(),p=h.page;try{await h.open();h.setFailure('changed_target');await p.getByRole('button',{name:'Review folder',exact:true}).click();await p.getByRole('button',{name:'Approve folder',exact:true}).click();await p.locator('.storage-import-error').filter({hasText:'latest folder details'}).waitFor();assert.equal(await p.getByRole('button',{name:'Watch folder',exact:true}).count(),0);await p.keyboard.press('Escape');
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor({state:'visible'});await h.open();assert.equal(await p.getByRole('button',{name:'Review folder',exact:true}).isDisabled(),true);assert.equal(await p.locator('.storage-setup button:enabled').count(),0);
 }finally{await h.close();}
});

test('SFTP late acquisition receipt cannot replace another connector or reopen its dialog',async()=>{
 const h=await storageHarness(),p=h.page;let release,entered;const gate=new Promise(resolve=>release=resolve),started=new Promise(resolve=>entered=resolve);
 try{await h.open();await h.approve();await p.route('**/api/retail/storage/custom-storage/run',async route=>{entered();await gate;await route.fulfill({contentType:'application/json',body:JSON.stringify({...h.setup,acquisition:{status:'running',cleanup_confirmed:false}})});});await p.getByRole('button',{name:'Watch folder',exact:true}).click();await started;await p.locator('#new-chat').click();release();await p.waitForTimeout(80);assert.equal(await p.locator('.storage-setup').isVisible(),false);assert.equal(await p.locator('dialog[open]').count(),0);assert.equal((await p.locator('.storage-import-state').textContent()).trim(),'Configured');
 }finally{release?.();await h.close();}
});

const steelGuidance={'steel-process':{name:'SIJ Acroni',industry:'Factory',description:'Steel production at SIJ Acroni, from material handling to the rolling line.',company_url:'https://www.acroni.si/',brand_image:'/assets/demo-companies/sij-acroni.png',questions:[{label:'Review material handling',prompt:'Inspect the steel handling sequence and cite the footage.'}]}};
test('Verified demo company branding belongs to its empty chat and preserves questions at desktop and mobile',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{chat:{dataset:'steel-process'},guidance:steelGuidance,corpus:[{video_id:'verified-steel',dataset:'steel-process',duration:8}],environments:[{id:'demo',name:'Demo company',category:'demo',collections:[{id:'steel-process',name:'SIJ Acroni'}]}]}),p=h.page;try{
  assert.equal(await p.locator('.demo-company-context').count(),0);await p.locator('#new-chat').click();await p.keyboard.press('Escape');const context=p.locator('.demo-company-context');await context.waitFor();
  assert.equal(await p.locator('#welcome h1').textContent(),'New chat');assert.equal(await context.locator('a').getAttribute('href'),steelGuidance['steel-process'].company_url);assert.equal(await context.locator('p').textContent(),steelGuidance['steel-process'].description);
  const logo=context.getByRole('img',{name:'SIJ Acroni'});await logo.evaluate(image=>image.decode());assert(await logo.evaluate(image=>image.naturalWidth>=480&&Math.abs(image.naturalWidth/image.naturalHeight-480/146)<0.02));assert((await logo.boundingBox()).width<=132);assert((await logo.boundingBox()).height>30);
  await p.getByRole('button',{name:'Review material handling',exact:true}).waitFor();assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`demo-company-welcome-${width}.png`)});
  await p.getByRole('button',{name:'Review material handling',exact:true}).click();assert.equal(await p.locator('#message').inputValue(),steelGuidance['steel-process'].questions[0].prompt);assert.equal(h.writes.length,0);
  await p.locator('#quick-search-nav').click();assert.equal(await context.count(),0);await p.locator('#new-chat').click();await context.waitFor();await p.locator('#agents-nav').click();await p.locator('#create-agent').click();assert.equal(await context.count(),0);assert.equal(await p.locator('#welcome h1').textContent(),'Configure your agent');
 }finally{await h.close();}}
});
test('Unverified factory and customer companies keep the ordinary welcome',async()=>{
 for(const options of [{guidance:{'steel-process':{...steelGuidance['steel-process'],name:'Factory',brand_image:undefined}}},{guidance:steelGuidance,environment:{id:'customer',name:'Demo'}}]){const h=await pageFor(390,{...options,chat:{dataset:'steel-process'},corpus:[{video_id:'fixture-steel',dataset:'steel-process',duration:8}]}),p=h.page;try{await p.locator('#new-chat').click();assert.equal(await p.locator('.demo-company-context').count(),0);assert.equal(await p.locator('#welcome h1').textContent(),'New chat');assert.deepEqual(h.writes,[]);}finally{await h.close();}}
});

test('SFTP polling holds during a mutation and keeps confirmed file progress on a failed refresh',async()=>{
 const h=await storageHarness(),p=h.page;let release,entered,reads=0;const gate=new Promise(resolve=>release=resolve),started=new Promise(resolve=>entered=resolve);
 try{await h.open();await h.approve();await p.route('**/api/retail/storage/custom-storage',route=>{reads++;return route.fulfill({contentType:'application/json',body:JSON.stringify(h.setup)});});await p.route('**/api/retail/storage/custom-storage/run',async route=>{entered();await gate;h.setup.acquisition={status:'running',source_id:'source-storage',cleanup_confirmed:false};h.setup.can_run=false;h.setup.can_pause=true;await route.fulfill({contentType:'application/json',body:JSON.stringify(h.setup)});});
  await p.getByRole('button',{name:'Watch folder',exact:true}).click();await started;const before=reads;await p.waitForTimeout(1650);assert.equal(reads,before,'Polling must wait for the admitted mutation receipt');assert.equal(await p.getByRole('button',{name:'Watch folder',exact:true}).isDisabled(),true);
  h.files.push({id:'upload-owned',source_id:'source-storage',collection:'cafe',filename:'Recording.mp4',status:'uploading',expected_bytes:8192,committed_bytes:4096});release();await p.locator('.storage-import-files').waitFor();assert.equal(await p.locator('.storage-import-files progress').getAttribute('value'),'4096');
  await p.route('**/api/retail/library/imports?*',route=>route.fulfill({status:503,contentType:'application/json',body:'{}'}));await p.getByText('Recording progress is reconnecting. The last confirmed status stays visible.',{exact:true}).waitFor();assert.equal(await p.locator('.storage-import-files progress').getAttribute('value'),'4096');
  h.setup.acquisition.source_id='source-replacement';await p.locator('.storage-import-files').waitFor({state:'detached'});assert.equal(await p.getByText('Recording.mp4',{exact:true}).count(),0,'A retargeted folder cannot retain a different source’s progress');
 }finally{release?.();await h.close();}
});

test('SFTP completed one-time import keeps its receipt and directs another import through setup',async()=>{
 const setup=storageFixture('one_time');setup.status='configured';setup.target=setup.proposal.target;setup.proposal.current=false;setup.acquisition.status='complete';setup.can_run=false;
 const h=await storageHarness(390,{setup}),p=h.page;try{await h.open();await p.getByText('Continue setup to approve another import.',{exact:true}).waitFor();assert.equal((await p.locator('.storage-import-state').textContent()).trim(),'Folder checked');assert.equal(await p.getByRole('button',{name:'Import once',exact:true}).count(),0);await p.getByRole('button',{name:'Continue setup',exact:true}).click();await p.waitForURL(/chat=storage-builder/);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

test('Gallery file cards follow the reference and open the full active pane while chat stays split',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{files:[documentFile]}),p=h.page;try{await p.locator('#library-nav').click();await p.keyboard.press('Escape');await p.locator('#workspace-files-tab').click();await p.locator('.workspace-file-tile').waitFor();assert.equal(await p.locator('#gallery-context').isVisible(),false);await p.locator('.workspace-file-open').click();await p.locator('body.workspace-file-viewer').waitFor();const box=await p.locator('#chat-files-panel').boundingBox();assert.equal(Math.round(box.x),width===390?56:72);assert.equal(Math.round(box.width),width-(width===390?56:72));assert.equal(await p.getByRole('textbox',{name:'Ask about this file'}).isVisible(),true);await p.keyboard.press('Escape');assert.equal(await p.locator('body.workspace-file-viewer').count(),0);await p.locator('#new-chat').click();await p.keyboard.press('Escape');}finally{await h.close();}}
});

function proofShot(p,name){if(!process.env.ROCOTO_UI_PROOF_DIR)return Promise.resolve();fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});return p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,name+'.png')});}
async function galleryFiles(p){await p.locator('#library-nav').click();await p.keyboard.press('Escape');await p.locator('#workspace-files-tab').click();await p.locator('.workspace-file-tile').first().waitFor();}

test('Gallery card menu renames only its display title and full viewer asks about the owned file',async()=>{
 for(const width of [1600,390]){const file={...documentFile,title:'Customer service review',name:'review.md',sha256:'a'.repeat(64),created_at:'2026-10-04T12:00:00Z'},other={...documentFile,id:'second-report',name:'schedule.txt',title:'Opening schedule'},h=await pageFor(width,{files:[file,other],chatEvents:[{type:'delta',text:'I reviewed the saved file.'},{type:'done'}]}),p=h.page;
 try{await galleryFiles(p);const card=p.locator('.workspace-file-tile').first();const box=await card.boundingBox();assert(Math.abs(box.width-box.height)<2);await card.getByLabel('Select Customer service review',{exact:true}).check();assert.equal(await card.evaluate(el=>el.classList.contains('is-selected')),true);await card.locator('summary').click();await p.waitForTimeout(20);const menuBox=await card.locator('.workspace-file-menu-items').boundingBox();assert(menuBox.x>=(width===390?56:72)&&menuBox.x+menuBox.width<=width);await proofShot(p,`gallery-card-menu-${width}`);assert.equal(await card.getByRole('link',{name:'Download',exact:true}).getAttribute('href'),origin+file.download_url);await card.getByRole('button',{name:'Rename',exact:true}).click();await p.locator('#rename-file-input').fill('Weekly customer review');await p.locator('#rename-file-save').click();await p.getByRole('button',{name:'Weekly customer review',exact:true}).waitFor();assert.equal(h.fileRecords[0].name,file.name);assert.equal(h.fileRecords[0].sha256,file.sha256);
 await p.getByRole('button',{name:'Weekly customer review',exact:true}).click();await p.locator('#file-preview-content').waitFor();await p.waitForFunction(()=>document.querySelector('#full-file-actions #file-download'));assert.deepEqual(h.openedFiles,[file.id]);await proofShot(p,`gallery-full-file-${width}`);await p.locator('#file-selector').selectOption(other.id);await p.locator('#file-preview-content').waitFor();assert.equal(await p.locator('#file-selector').inputValue(),other.id);await p.locator('#file-question').fill('Summarize the opening schedule.');await p.locator('#file-question-send').click();await p.getByText('I reviewed the saved file.',{exact:true}).waitFor();const sent=h.payloads.find(row=>row.path==='/api/chat').body;assert(sent.message.includes(other.id));assert(sent.message.includes('Summarize the opening schedule.'));assert.equal(await p.locator('body.workspace-file-viewer').count(),0);
 }finally{await h.close();}}
});

test('Gallery keeps chat previews split and provider actions explicit in the full pane',async()=>{
 const external={...providerResource,id:'resource-full',title:'Provider plan'},h=await pageFor(1600,{files:[documentFile,external]}),p=h.page;
 try{await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'View all files',exact:true}).click();await p.locator('#chat-files-list .generated-file').first().click();await p.locator('#file-preview-content').waitFor();const split=await p.locator('#chat-files-panel').boundingBox();assert(split.x>72&&split.width<1200);assert.equal(await p.locator('#file-question-form').isVisible(),false);await p.keyboard.press('Escape');await galleryFiles(p);await p.getByRole('button',{name:external.title,exact:true}).click();await p.locator('#file-preview-content').waitFor();assert.equal(await p.locator('#full-file-actions').getByRole('link',{name:'Open provider',exact:true}).getAttribute('href'),external.source_url);assert.equal(await p.locator('#full-file-actions #file-download').count(),0);assert.equal(await p.locator('#chat-files-panel iframe').count(),0);await proofShot(p,'gallery-provider-full');
 }finally{await h.close();}
});

test('Collection search focuses on entry and details expose breadcrumb and item actions at desktop and mobile',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{agents:[recurringAgent]}),p=h.page;try{
 await p.locator('#agents-nav').click();await p.locator('.agent-collection-card').waitFor();assert.equal(await p.evaluate(()=>document.activeElement.id),'agents-search');await p.locator('#agents-search').fill('Service');await p.locator('#create-agent').focus();await p.waitForTimeout(4200);assert.equal(await p.evaluate(()=>document.activeElement.id),'create-agent');assert.equal(await p.locator('#agents-search').inputValue(),'Service');await p.locator('#agents-search').fill('');await p.locator('.agent-collection-card').click();await p.locator('#agent-info-button').waitFor();assert.equal(await p.locator('#chat-title').innerText(),'Agents\n'+recurringAgent.title);assert.equal(await p.locator('#agent-context').isVisible(),false);assert.equal(await p.locator('#create-agent').isVisible(),false);assert.equal(await p.locator('#agents-skills-link').isVisible(),false);assert.equal(await p.locator('#context-new-agent').isVisible(),false);assert.equal(await p.locator('#message').isEditable(),true);await proofShot(p,`agent-item-${width}`);await p.locator('#agent-info-button').click();await p.getByRole('button',{name:'Edit in chat',exact:true}).waitFor();await proofShot(p,`agent-info-${width}`);await p.keyboard.press('Escape');await p.locator('#agents-nav').click();await p.locator('#agents-search').waitFor();assert.equal(await p.evaluate(()=>document.activeElement.id),'agents-search');
 await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').waitFor();assert.equal(await p.evaluate(()=>document.activeElement.id),'marketplace-search');await p.locator('#marketplace-search').fill('Notion');await p.locator('.connection-catalogue-item .marketplace-card').click();await p.locator('.detail-breadcrumb').waitFor();assert.equal(await p.locator('#marketplace-search').isVisible(),false);assert.equal(await p.locator('#add-custom-connector').isVisible(),false);assert((await p.locator('.detail-breadcrumb').innerText()).includes('Notion'));await p.locator('.detail-breadcrumb button').click();await p.locator('#marketplace-search').waitFor();assert.equal(await p.evaluate(()=>document.activeElement.id),'marketplace-search');
 }finally{await h.close();}}
});

async function linkHarness(width=1600,options={}){
 const h=await uploadPage(width,options),p=h.page,links=[],calls=[],keys=new Map();let first=true,held=null;
 const policy={available:true,can_create:options.linkAvailable!==false,can_manage:true,collections:options.empty?['company']:['cafe','warehouse'],default_collection:options.empty?'company':null};
 await p.route('**/api/retail/source-links**',async route=>{const req=route.request(),u=new URL(req.url()),body=req.method()==='POST'?req.postDataJSON():null;calls.push({path:u.pathname,method:req.method(),body,query:u.search});if(req.method()==='GET'){assert.notEqual(u.search,'?collection=','Empty scope must be omitted for host-selected default');return route.fulfill({contentType:'application/json',body:JSON.stringify(u.pathname==='/api/retail/source-links'?{links,intake:policy}:{link:links.find(row=>u.pathname.endsWith(row.id))})});}
 assert.equal(req.headers()['x-rocoto-page'],'1');if(u.pathname==='/api/retail/source-links'){assert.equal(body.collection,options.empty?'company':'cafe');assert(body.idempotency_key);let link=keys.get(body.idempotency_key);if(!link){link={id:'link-'+(keys.size+1),provider:options.needsAction?'google_drive':'dropbox',collection:body.collection,status:options.needsAction?'needs_action':'downloading',received_bytes:4,size_bytes:12,filename:options.needsAction?null:'Shared recording.mp4',...(options.needsAction?{action:{code:'browser_download_required',message:'Download this file with your provider, then choose Upload files.'}}:{})};keys.set(body.idempotency_key,link);links.push(link);}
 if(options.holdCreate&&first){first=false;held=route;return;}
 if(options.loseReply&&first){first=false;return route.abort('failed');}return route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({link})});}
 const link=links.find(row=>u.pathname.split('/')[4]===row.id);assert(link);if(req.method()==='DELETE'){link.status=options.cancelPending&&link.status!=='cancelling'?'cancelling':'cancelled';return route.fulfill({status:link.status==='cancelling'?202:200,contentType:'application/json',body:JSON.stringify({link})});}assert.deepEqual(body,{});link.status='queued';return route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({link})});});
 return {...h,links,linkCalls:calls,linkPolicy:policy,held:()=>held};
}

for(const width of [1600,390])test(`Upload dropzone follows loading and closed admission while links stay independent at ${width}`,async()=>{
 const h=await linkHarness(width,{available:false}),p=h.page;let release,releaseReopen;
 try{
  const gate=new Promise(resolve=>release=resolve);let waiting=false;
  await p.route('**/api/retail/library/imports?*',async route=>{waiting=true;await gate;await route.fallback();});
  await h.open();await p.waitForFunction(()=>document.querySelector('#source-upload-dialog').open);while(!waiting)await p.waitForTimeout(10);
  const zone=p.locator('#upload-dropzone');
  const drag=async type=>zone.evaluate((element,type)=>{const dataTransfer=new DataTransfer();dataTransfer.items.add(new File(['abcdefghijkl'],'Readiness fixture.mp4',{type:'video/mp4'}));element.dispatchEvent(new DragEvent(type,{bubbles:true,cancelable:true,dataTransfer}));},type);
  assert.equal(await zone.getAttribute('aria-disabled'),'true');assert.match(await p.locator('#source-upload-status').textContent(),/Checking upload availability/);
  await drag('dragover');assert.equal(await zone.evaluate(el=>el.classList.contains('is-dragging')),false);await drag('drop');assert.match(await p.locator('#source-upload-status').textContent(),/Try this file again when uploads are ready/);
  assert.equal(h.calls.filter(row=>row.method!=='GET').length,0);await proofShot(p,`upload-checking-${width}`);
  release();await p.waitForFunction(()=>document.querySelector('#source-upload-status').textContent.includes('Video uploads are paused for this workspace.'));
  assert.equal(await zone.getAttribute('aria-disabled'),'true');assert.equal(await zone.getAttribute('aria-describedby'),'source-upload-status');assert.equal(await p.locator('#upload-choose').isDisabled(),true);assert.equal(await p.locator('#upload-drop-label').textContent(),'Video uploads paused');
  await drag('dragover');assert.equal(await zone.evaluate(el=>el.classList.contains('is-dragging')),false);await drag('drop');assert.match(await p.locator('#source-upload-status').textContent(),/Try this file again when uploads are ready/);assert.equal(h.calls.filter(row=>row.method!=='GET').length,0);
  assert(!(await p.locator('#source-upload-dialog').textContent()).includes('Your workspace owner can enable'));await proofShot(p,`upload-paused-${width}`);
  await p.locator('#source-link-submit:enabled').waitFor();await p.locator('#source-link-input').fill('https://example.com/fixture.mp4');await p.locator('#source-link-submit').click();await p.locator('#source-link-list').getByText('Downloading',{exact:true}).waitFor();assert.equal(h.linkCalls.filter(row=>row.method==='POST').length,1);assert.equal(h.calls.filter(row=>row.method!=='GET').length,0);
  h.intake.notice='The current upload allowance is reserved.';await p.waitForFunction(()=>document.querySelector('#source-upload-status').textContent.includes('The current upload allowance is reserved.'));
  h.intake.available=true;h.intake.can_upload=true;h.intake.notice='';h.linkPolicy.can_create=false;await p.locator('#upload-choose:enabled').waitFor();await p.waitForFunction(()=>document.querySelector('#source-link-submit').disabled);
  assert.equal(await zone.getAttribute('aria-disabled'),'false');assert.equal(await p.locator('#upload-drop-label').textContent(),'Drop files here');assert(!(await p.locator('#source-upload-status').textContent()).includes('Try this file again'));
  await drag('dragover');assert.equal(await zone.evaluate(el=>el.classList.contains('is-dragging')),true);await drag('drop');await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal([...h.chunks.values()][0].length,3);assert.equal(h.linkCalls.filter(row=>row.method==='POST').length,1);await proofShot(p,`upload-ready-${width}`);assert.deepEqual(h.errors,[]);
  await p.keyboard.press('Escape');h.intake.available=false;h.intake.can_upload=false;const reopenGate=new Promise(resolve=>releaseReopen=resolve);await p.route('**/api/retail/library/imports?*',async route=>{await reopenGate;await route.fallback();});const writesBeforeReopen=h.calls.filter(row=>row.method!=='GET').length;
  await p.locator('#gallery-upload').click();assert.equal(await zone.getAttribute('aria-disabled'),'true');assert.equal(await p.locator('#upload-drop-label').textContent(),'Checking availability…');await drag('dragover');assert.equal(await zone.evaluate(el=>el.classList.contains('is-dragging')),false);await drag('drop');assert.equal(h.calls.filter(row=>row.method!=='GET').length,writesBeforeReopen);
  releaseReopen();await p.waitForFunction(()=>document.querySelector('#upload-drop-label').textContent==='Video uploads paused');await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(await zone.getAttribute('aria-disabled'),'true');assert.equal(h.rows.size,1);assert.equal(h.calls.filter(row=>row.method!=='GET').length,writesBeforeReopen);
 }finally{release?.();releaseReopen?.();await h.close();}
});

test('Shared Upload dialog accepts dropped bytes and links from Gallery and Cameras at desktop and mobile',async()=>{
 for(const width of [1600,390]){const h=await linkHarness(width,{loseReply:true}),p=h.page;try{
 await h.open();await p.locator('#source-link-submit:enabled').waitFor();await p.locator('#source-link-input').fill('https://www.dropbox.com/scl/fi/fixture/recording.mp4?dl=1');await p.locator('#source-link-submit').click();await p.locator('#source-link-list').getByText('Downloading',{exact:true}).waitFor();assert.equal(h.linkCalls.filter(row=>row.method==='POST').length,2);assert.deepEqual(h.linkCalls.filter(row=>row.method==='POST')[0].body,h.linkCalls.filter(row=>row.method==='POST')[1].body);assert.equal(await p.locator('#source-link-input').inputValue(),'');
 await p.locator('#upload-dropzone').evaluate(element=>{const dataTransfer=new DataTransfer();dataTransfer.items.add(new File(['abcdefghijkl'],'Dropped recording.mp4',{type:'video/mp4'}));element.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer}));});await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal([...h.chunks.values()][0].length,3);await proofShot(p,`upload-dialog-${width}`);const box=await p.locator('#source-upload-dialog').boundingBox();assert(box.x>=0&&box.x+box.width<=width);assert(!await p.evaluate(()=>JSON.stringify(localStorage).includes('dropbox.com')));await p.keyboard.press('Escape');assert.equal(await p.evaluate(()=>document.activeElement.id),'gallery-upload');
 await p.goto(origin+'/?cameras=1&collection=cafe');await p.locator('#camera-upload').click();await p.locator('#source-upload-dialog').waitFor();await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert(new URL(p.url()).searchParams.has('cameras'));assert.equal(await p.locator('#source-link-input').inputValue(),'');await p.keyboard.press('Escape');assert.equal(await p.evaluate(()=>document.activeElement.id),'camera-upload');
 }finally{await h.close();}}
});

test('Shared-link action states distinguish browser downloads, preparation and confirmed cancellation',async()=>{
 const h=await linkHarness(390,{needsAction:true,cancelPending:true}),p=h.page;try{await h.open();await p.locator('#source-link-submit:enabled').waitFor();await p.locator('#source-link-input').fill('https://drive.google.com/file/d/fixture/view');await p.locator('#source-link-submit').click();await p.locator('#source-link-list').getByText('Action needed',{exact:true}).waitFor();assert.equal(await p.locator('#source-link-list').getByRole('button',{name:'Choose downloaded file',exact:true}).isEnabled(),true);assert.equal(h.calls.filter(row=>row.method!=='GET').length,0);await p.locator('#source-link-list').getByRole('button',{name:'Cancel',exact:true}).click();await p.locator('#source-link-list').getByText('Cancelling…',{exact:true}).waitFor();await p.locator('#source-link-list').getByRole('button',{name:'Cancel',exact:true}).click();await p.locator('#source-link-list').getByText('Cancelled',{exact:true}).waitFor();assert.equal(h.linkCalls.filter(row=>row.method==='DELETE').length,2);
 const row={id:'upload-'+'f'.repeat(32),collection:'cafe',filename:'Shared recording.mp4',status:'received',transfer_status:'sealed',committed_bytes:12,expected_bytes:12,stage:'prepare',stage_status:'queued'};h.links[0]={...h.links[0],status:'received',import:row};await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(await p.locator('#upload-progress').getByText('Ready',{exact:true}).count(),0);row.status='ready';row.stage=null;await p.locator('#upload-progress').getByText('Ready',{exact:true}).waitFor();
 }finally{await h.close();}
});

test('Shared-link lost reply is never replayed after a workspace change and late results stay private',async()=>{
 const h=await linkHarness(1600,{holdCreate:true}),p=h.page;try{await h.open();await p.locator('#source-link-submit:enabled').waitFor();await p.locator('#source-link-input').fill('https://example.com/private-recording.mp4');await p.locator('#source-link-submit').click();while(!h.held())await p.waitForTimeout(10);await p.keyboard.press('Escape');await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await h.held().abort('failed');await p.waitForTimeout(100);assert.equal(h.linkCalls.filter(row=>row.method==='POST').length,1);assert.equal(await p.locator('#source-link-list').textContent(),'');assert.equal(await p.locator('#source-link-input').inputValue(),'');assert.equal(await p.locator('#source-upload-dialog').isVisible(),false);assert(!await p.evaluate(()=>JSON.stringify(localStorage).includes('private-recording')));
 }finally{await h.held()?.abort().catch(()=>{});await h.close();}
});

test('Shared Upload keeps closed admission disabled and accepts only a host-selected empty-company scope',async()=>{
 const h=await linkHarness(390,{empty:true,linkAvailable:false}),p=h.page;try{await h.open();await p.locator('#source-link-input').waitFor();assert.equal(await p.locator('#source-link-submit').isDisabled(),true);h.linkPolicy.can_create=true;await p.locator('#source-link-submit:enabled').waitFor();await p.locator('#source-link-input').fill('https://example.com/customer.mp4');await p.locator('#source-link-submit').click();await p.locator('#source-link-list').getByText('Downloading',{exact:true}).waitFor();assert.equal(h.linkCalls.find(row=>row.method==='POST').body.collection,'company');await p.keyboard.press('Escape');await p.goto(origin+'/?cameras=1');await p.locator('#camera-upload').click();await p.locator('#source-link-submit:enabled').waitFor();assert(new URL(p.url()).searchParams.has('cameras'));assert(!h.linkCalls.some(row=>row.query==='?collection='));}finally{await h.close();}
});

test('Founder picker switches Gallery demo while keeping folders in the grid',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page,scopes=[];try{
 await p.route('**/api/retail/library?*',route=>{const collection=new URL(route.request().url()).searchParams.get('collection');scopes.push(collection);return route.fulfill({contentType:'application/json',body:JSON.stringify({folder:'',folders:[{name:'Front counter',path:'counter',asset_count:2}],assets:[]})});});await p.locator('#library-nav').click();await p.getByRole('button',{name:'Front counter 2 source files'}).waitFor();assert.equal(await p.locator('#gallery-context').isVisible(),false);assert.equal(await p.locator('#library-collection').isVisible(),false);await p.locator('#founder-nav').click();await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();await p.waitForFunction(()=>new URL(location.href).searchParams.get('collection')==='warehouse');assert.equal(await p.locator('#library-view').isVisible(),true);assert.equal(await p.locator('#library-collection-control').textContent(),'Fixture Warehouse');assert(scopes.includes('warehouse'));await proofShot(p,`gallery-folders-${width}`);await p.locator('#new-chat').click();assert.equal(await p.locator('#environment-toggle').isVisible(),false);assert.equal(await p.locator('#founder-nav').isVisible(),true);
 }finally{await h.close();}}
});

test('Camera details use their own actions and Gallery video stays on the indexed interval with the main rail visible',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page;try{
 const clip={id:'fixture-clip',dataset:'cafe',start:16,end:24,camera:'Front counter',video_url:'/clips/fixture-clip.mp4'},asset={asset_id:'source-video',collection:'cafe',filename:'Opening shift.mp4',bytes:24000,duration:32,clips:[clip,{...clip,id:'next-clip',start:24,end:32}]};await p.route('**/api/retail/cameras/camera-1?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({camera:{id:'camera-1',name:'Front counter',collection:'cafe',assets:[asset],clips:[],history:[]}})}));await p.route('**/api/retail/assets/source-video?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({asset})}));await p.route('**/api/retail/library?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({folder:'',folders:[],assets:[asset]})}));
 await p.goto(origin+'/?cameras=1&collection=cafe');await p.locator('#camera-list button').first().click();await p.locator('.detail-breadcrumb').waitFor();assert.equal(await p.locator('#camera-search').isVisible(),false);assert.equal(await p.locator('#add-cameras').isVisible(),false);assert.equal(await p.getByRole('button',{name:'Review camera',exact:true}).isVisible(),true);await p.locator('.camera-source-link').click();await p.locator('#library-asset').waitFor();assert.equal(await p.locator('.gallery-heading').isVisible(),false);assert.equal(await p.locator('#library-files').isVisible(),false);await p.locator('#library-asset .evidence-card').first().click();await p.locator('body.gallery-clip-viewer').waitFor();const box=await p.locator('#clip-dialog').boundingBox();assert.equal(Math.round(box.x),width===390?56:72);assert.equal(await p.locator('#clip-player').getAttribute('src'),'/clips/fixture-clip.mp4');assert((await p.locator('#clip-interval-note').textContent()).includes('0:16'));await proofShot(p,`gallery-video-full-${width}`);await p.keyboard.press('Escape');assert.equal(await p.locator('#clip-dialog').isVisible(),false);
 }finally{await h.close();}}
});

const directGoogleProviders=[
 {id:'google-drive',provider:'google-drive',name:'Google Drive',description:'Find and organize files with Google Drive.',authentication:'oauth',native_direct:true,developer:'Google',category:'Google Workspace',registration:'static',setup_reason:'registered_client_required',status:'setup_required',available:false,access_mode:'read',access_modes:['read','read_write'],write_available:false,reference:'https://developers.google.com/workspace/drive/mcp'},
 {id:'google-docs',provider:'google-docs',name:'Google Docs',description:'Read and edit documents with Google Docs.',authentication:'oauth',native_direct:true,developer:'Google',category:'Google Workspace',registration:'static',setup_reason:'registered_client_required',status:'setup_required',available:false,access_mode:'read',access_modes:['read','read_write'],write_available:false,reference:'https://developers.google.com/workspace/docs/mcp'}
];
async function googleCatalogue(p,query='Google Drive'){await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill(query);}
function directGoogleCard(p,provider){return p.locator(`.marketplace-card[data-connection-id="provider:${provider}"]`);}

test('Direct Google entries preserve original package metadata and show actual customer setup requirements',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{connections:directGoogleProviders}),p=h.page;try{
 await googleCatalogue(p);await directGoogleCard(p,'google-drive').waitFor();const original=p.locator('.marketplace-card[data-connection-id="google-drive"]');await original.waitFor();assert((await original.innerText()).includes('OpenAI · Plugin'));assert((await directGoogleCard(p,'google-drive').innerText()).includes('Google · Setup required'));await directGoogleCard(p,'google-drive').click();await p.locator('.connection-hero h1').waitFor();assert.equal(await p.locator('.connection-hero h1').textContent(),'Google Drive');assert.equal(await p.locator('.connection-hero p').first().textContent(),directGoogleProviders[0].description);assert.equal(await p.getByRole('button',{name:'Install plugin',exact:true}).count(),0);assert.equal(await p.locator('.connection-information').getByRole('link',{name:'Source',exact:true}).getAttribute('href'),directGoogleProviders[0].reference);assert.equal(await p.locator('.connection-information').getByText('Google',{exact:true}).count(),1);await p.getByRole('button',{name:'Setup required',exact:true}).click();await p.getByText('Your workspace owner can enable Google account sign-in.',{exact:true}).waitFor();assert.equal(await p.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true}).isDisabled(),true);await proofShot(p,`google-setup-${width}`);await p.keyboard.press('Escape');await p.locator('.detail-breadcrumb button').click();await original.click();const upstream=upstreamCatalogue.plugins.find(item=>item.id==='google-drive');assert.equal(upstream.supported_api,false);assert.equal(await p.locator('.connection-hero p').first().textContent(),upstream.description);assert.equal(await p.getByRole('button',{name:'Install plugin',exact:true}).isDisabled(),true);assert.equal(await p.locator('.connection-long-description').innerText(),upstream.long_description.trim());assert.equal(await p.locator('.connection-information').getByRole('link',{name:'Source',exact:true}).getAttribute('href'),upstream.source_url);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

test('Direct Google consent uses its canonical provider and late Drive responses cannot enter Docs setup',async()=>{
 const rows=directGoogleProviders.map(row=>({...row,available:true,status:'disconnected',setup_reason:null,write_available:true})),h=await pageFor(1600,{connections:rows}),p=h.page,submissions=[];let held;
 try{await p.route('**/api/connections/*/connect',r=>{submissions.push({path:new URL(r.request().url()).pathname,body:r.request().postDataJSON()});assert.equal(r.request().headers()['x-rocoto-page'],'1');if(r.request().url().includes('/google-drive/')){held=r;return;}return r.fulfill({contentType:'application/json',body:JSON.stringify({flow_id:'docs-flow',status:'awaiting_authorization',authorization_url:'https://accounts.google.com/o/oauth2/v2/auth?state=docs-fixture'})});});
 await googleCatalogue(p);await directGoogleCard(p,'google-drive').click();await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).click();await p.getByLabel('Account access',{exact:true}).selectOption('read_write');await p.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true}).click();while(!held)await p.waitForTimeout(10);await p.keyboard.press('Escape');await p.locator('.detail-breadcrumb button').click();await p.locator('#marketplace-search').fill('Google Docs');await directGoogleCard(p,'google-docs').click();await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).click();await held.fulfill({contentType:'application/json',body:JSON.stringify({flow_id:'drive-flow',status:'awaiting_authorization',authorization_url:'https://accounts.google.com/o/oauth2/v2/auth?state=drive-fixture'})});await p.waitForTimeout(50);assert.equal(await p.locator('#connection-setup-content h2').textContent(),'Google Docs');assert.equal(await p.locator('.connection-authorize').count(),0);await p.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true}).click();await p.getByRole('link',{name:'Authorize account',exact:true}).waitFor();assert((await p.locator('.connection-authorize').getAttribute('href')).includes('docs-fixture'));assert.deepEqual(submissions,[{path:'/api/connections/google-drive/connect',body:{access_mode:'read_write'}},{path:'/api/connections/google-docs/connect',body:{access_mode:'read'}}]);assert.equal(await p.getByText('Connected',{exact:true}).count(),0);
 }finally{await held?.abort().catch(()=>{});await h.close();}
});

test('Direct Google Plus and mentions favor usable providers while skills remain server-authorized',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{connections:directGoogleProviders.map(row=>({...row,available:true,status:'connected',setup_reason:null,access_mode:'read'}))}),p=h.page;try{await p.locator('#attachment-toggle').click();await p.locator('#attachment-connections').getByRole('button',{name:'Google Drive',exact:true}).waitFor();assert.equal(await p.locator('#attachment-connections').getByRole('button',{name:'Google Drive',exact:true}).count(),1);await p.locator('#attachment-connections').getByRole('button',{name:'Google Drive',exact:true}).click();assert.equal(await p.locator('#message').inputValue(),'@Google Drive ');await p.locator('#message').fill('@google');await p.locator('#command-menu').waitFor();assert.equal(await p.locator('#command-menu .command-option').filter({has:p.getByText('@Google Drive',{exact:true})}).count(),1);assert.equal(await p.locator('#command-menu .command-option').filter({has:p.getByText('@Google Docs',{exact:true})}).locator('use').first().getAttribute('href'),'#i-document');await proofShot(p,`google-mentions-${width}`);await p.locator('#message').fill('/google');await p.locator('#command-menu').waitFor();assert.equal(await p.locator('#command-menu .command-option').filter({hasText:'/google'}).count(),0);await p.keyboard.press('Escape');await p.locator('#attachment-toggle').click();await p.locator('#attach-skills').click();assert.equal(await p.locator('#installed-skill-options').getByText('/source-review',{exact:true}).isVisible(),true);assert.equal(await p.locator('#installed-skill-options').getByText(/google/i).count(),0);assert.deepEqual(h.writes,[]);}finally{await h.close();}}
});

test('Observed Google connection details stay read only and write access follows commissioning',async()=>{
 const h=await pageFor(390,{connections:directGoogleProviders.map(row=>({...row,available:true,status:'disconnected',setup_reason:null}))}),p=h.page;try{await googleCatalogue(p,'Google Docs');await directGoogleCard(p,'google-docs').click();await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).click();assert.equal(await p.getByLabel('Account access',{exact:true}).locator('option[value="read_write"]').evaluate(node=>node.disabled),true);await p.keyboard.press('Escape');await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await googleCatalogue(p,'Google Docs');await directGoogleCard(p,'google-docs').click();assert.equal(await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).isDisabled(),true);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}
});

const bearerConnection={id:'fixture-vault',provider:'fixture-vault',name:'Data room',description:'Read files in your data room.',native_direct:true,developer:'Fixture',authentication:'bearer',available:true,status:'disconnected',access_mode:'read',access_modes:['read','read_write'],write_available:true};
async function openBearerSetup(p){await p.goto(origin+'/?customize=1&tab=connections&item=provider%3Afixture-vault');await p.locator('.connection-hero').getByRole('button',{name:/^(Connect|Setup required|Connected)$/}).click();await p.locator('#connection-setup-dialog').waitFor();}
async function bearerPrivate(p,token){assert.equal(await p.evaluate(token=>JSON.stringify(Object.entries(localStorage)).includes(token)||JSON.stringify(Object.entries(sessionStorage)).includes(token),token),false);assert.equal((await p.locator('body').innerText()).includes(token),false);assert.equal(p.url().includes(token),false);}
test('Bearer connection entry masks, reveals and privately submits the exact token with selected access',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{connections:[bearerConnection]}),p=h.page,submissions=[];let held;const token='synthetic-Bearer_123+/=';
  try{await p.route('**/api/connections/fixture-vault/connect',r=>{assert.equal(r.request().headers()['x-rocoto-page'],'1');submissions.push(r.request().postDataJSON());held=r;});await openBearerSetup(p);const input=p.getByLabel('Access token',{exact:true}),connect=p.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true});await input.waitFor();assert.equal(await input.getAttribute('type'),'password');assert.equal(await input.getAttribute('autocomplete'),'new-password');assert.equal(await connect.isDisabled(),true);assert.equal(await p.getByText('Sent securely to this connection’s credential store.',{exact:true}).isVisible(),true);
   await input.fill(' token with spaces ');assert.equal(await connect.isDisabled(),true);await input.fill(token);assert.equal(await connect.isEnabled(),true);await p.getByRole('button',{name:'Show access token',exact:true}).click();assert.equal(await input.getAttribute('type'),'text');await p.getByRole('button',{name:'Hide access token',exact:true}).click();assert.equal(await input.getAttribute('type'),'password');await p.getByLabel('Account access',{exact:true}).selectOption('read_write');await bearerPrivate(p,token);await proofShot(p,`bearer-connection-${width}`);
   await connect.click();await p.waitForFunction(()=>document.querySelector('#connection-token')?.value==='');assert.equal(await input.getAttribute('type'),'password');assert.equal(await connect.isDisabled(),true);assert.equal(await input.isDisabled(),true);for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);assert.deepEqual(submissions,[{access_mode:'read_write',token}]);await bearerPrivate(p,token);
   await p.route('**/api/connections/flows/bearer-flow',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({flow_id:'bearer-flow',provider:'fixture-vault',status:'connected'})}));await held.fulfill({contentType:'application/json',body:JSON.stringify({flow_id:'bearer-flow',provider:'fixture-vault',status:'connecting'})});held=null;await p.locator('.connection-flow-status').getByText('Connected',{exact:true}).waitFor();assert.equal(await input.inputValue(),'');assert.equal(await p.locator('.connection-authorize').count(),0);await bearerPrivate(p,token);assert.deepEqual(h.writes,[]);
  }finally{if(held)await held.abort().catch(()=>{});await h.close();}
 }
});
test('Bearer entry clears on dismissal and page exit, and failure copy stays credential-free',async()=>{
 const h=await pageFor(390,{connections:[bearerConnection]}),p=h.page,token='synthetic-private-error';
 try{await openBearerSetup(p);const input=p.getByLabel('Access token',{exact:true});await input.fill(token);await p.getByRole('button',{name:'Show access token',exact:true}).click();await p.keyboard.press('Escape');assert.equal(await input.inputValue(),'');assert.equal(await input.getAttribute('type'),'password');await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).click();assert.equal(await input.inputValue(),'');await input.fill(token);await p.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide')));assert.equal(await input.inputValue(),'');await input.fill(token);await p.locator('#close-connection-setup').click();assert.equal(await input.inputValue(),'');await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).click();await input.fill(token);
  await p.route('**/api/connections/fixture-vault/connect',r=>r.fulfill({status:400,contentType:'application/json',body:JSON.stringify({detail:token})}));await input.press('Enter');await p.locator('#connection-setup-content .agent-form-error').waitFor();assert.equal(await input.inputValue(),'');assert.equal(await input.isEnabled(),true);assert.equal(await p.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true}).isDisabled(),true);await bearerPrivate(p,token);
  await input.fill(token);await p.route('**/api/connections/fixture-vault/connect',r=>r.fulfill({contentType:'application/json',body:'{"flow_id":"failed-flow","status":"failed"}'}));await input.press('Enter');await p.getByText('Connection needs another attempt.',{exact:true}).waitFor();assert.equal(await input.isEnabled(),true);assert.equal(await input.inputValue(),'');await bearerPrivate(p,token);
  await input.fill(token);await p.route('**/api/connections/fixture-vault/connect',r=>r.fulfill({status:401,contentType:'application/json',body:JSON.stringify({detail:token})}));await input.press('Enter');await p.locator('#session-screen').waitFor();assert.equal(await input.inputValue(),'');assert.equal(await p.locator('#connection-setup-dialog').isVisible(),false);await bearerPrivate(p,token);
 }finally{await h.close();}
});
test('Bearer late replies preserve a newer setup and a changed session',async()=>{
 const h=await pageFor(1600,{connections:[bearerConnection]}),p=h.page;let held;
 try{await p.route('**/api/connections/fixture-vault/connect',r=>{held=r;});await openBearerSetup(p);const input=p.getByLabel('Access token',{exact:true});await input.fill('synthetic-first-token');await input.press('Enter');for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);await p.keyboard.press('Escape');await p.locator('.connection-hero').getByRole('button',{name:'Connect',exact:true}).click();await input.fill('synthetic-new-token');await held.fulfill({status:401,contentType:'application/json',body:'{"detail":"old session"}'});held=null;await p.waitForTimeout(80);assert.equal(await p.locator('#connection-setup-dialog').isVisible(),true);assert.equal(await p.locator('#session-screen').isVisible(),false);assert.equal(await input.inputValue(),'synthetic-new-token');assert.equal(await p.locator('.connection-flow-status').count(),0);
  await input.press('Enter');for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);await p.keyboard.press('Escape');await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await held.fulfill({contentType:'application/json',body:'{"flow_id":"old-flow","status":"connected"}'});held=null;await p.waitForTimeout(80);assert.equal(await p.locator('#connection-setup-dialog').isVisible(),false);assert.equal(await p.locator('.connection-flow-status').count(),0);assert.equal(await input.inputValue(),'');await bearerPrivate(p,'synthetic-new-token');assert.deepEqual(h.writes,['/api/observer']);
 }finally{if(held)await held.abort().catch(()=>{});await h.close();}
});
test('Bearer host readiness and observer access gate credentials while OAuth and anonymous remain token-free',async()=>{
 const h=await pageFor(390,{connections:[{...bearerConnection,available:false,status:'setup_required'}]}),p=h.page;
 try{await openBearerSetup(p);assert.equal(await p.getByLabel('Access token',{exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Show access token',exact:true}).isDisabled(),true);assert.equal(await p.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true}).isDisabled(),true);await p.keyboard.press('Escape');await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#marketplace-search').fill('Data room');await p.locator('.marketplace-card[data-connection-id="provider:fixture-vault"]').click();assert.equal(await p.locator('.connection-hero').getByRole('button',{name:'Setup required',exact:true}).isDisabled(),true);assert.deepEqual(h.writes,['/api/observer']);}finally{await h.close();}
 for(const authentication of ['oauth','anonymous']){const g=await pageFor(1600,{connections:[{...bearerConnection,authentication}]}),q=g.page,submissions=[];try{await q.route('**/api/connections/fixture-vault/connect',r=>{submissions.push(r.request().postDataJSON());return r.fulfill({contentType:'application/json',body:'{"flow_id":"fixture-flow","status":"connected"}'});});await openBearerSetup(q);assert.equal(await q.getByLabel('Access token',{exact:true}).count(),0);await q.locator('#connection-setup-content').getByRole('button',{name:'Connect',exact:true}).click();await q.locator('.connection-flow-status').waitFor();assert.deepEqual(submissions,[{access_mode:'read'}]);}finally{await g.close();}}
});

for(const width of [1600,390])test(`Saved business summary keeps sourced identity and location truthful at ${width}`,async()=>{
 const profile={name:'Fixture Workshop',type:'Coffee roastery',website:'https://www.fixture.example/',location:'Head office in Tokyo; the filmed site is unspecified.',description:'<img src=x onerror="window.businessInjected=true"> Saved source-backed description.',operations:['Serve drinks'],assumptions:['Confirm the camera site'],source_urls:['https://fixture.example/about','javascript:alert(1)','https://user:private@unsafe.example/']};
 const business={profile,revision:1,provenance:{epistemic_status:'agent_researched_unconfirmed',recorded_at:'2026-10-05T12:00:00Z'},settings:{initial_history_hours:48},footage_relationship:{basis:'The camera location still needs confirmation.'}};
 const h=await pageFor(width,{business}),p=h.page;
 try{
  await p.goto(origin+'/?business=1&collection=cafe');const summary=p.getByRole('region',{name:'Business profile'});await summary.waitFor();
  assert.equal(await summary.locator('h3').textContent(),profile.name);assert.equal(await summary.locator('.business-summary-description').textContent(),profile.description);assert.equal(await summary.locator('script,iframe,.business-summary-description img').count(),0);assert.equal(await summary.locator('img:not(.source-favicon)').count(),0);assert.equal(await p.evaluate(()=>window.businessInjected),undefined);
  assert.equal(await summary.locator('.business-summary-status').textContent(),'Research draft');assert.equal(await summary.locator('.business-summary-location p').textContent(),profile.location);
  const website=summary.locator('.business-summary-website a');assert.equal(await website.textContent(),'fixture.example');assert.equal(await website.getAttribute('href'),profile.website);
  const maps=new URL(await summary.getByRole('link',{name:'Search Google Maps'}).getAttribute('href'));assert.equal(maps.origin+maps.pathname,'https://www.google.com/maps/search/');assert.deepEqual([...maps.searchParams],[['api','1'],['query',profile.name]]);
  const stamp=summary.locator('time');assert.match(await stamp.textContent(),/^Profile updated /);assert.equal(await stamp.getAttribute('datetime'),'2026-10-05T12:00:00.000Z');
  await summary.locator('summary').click();assert.equal(await summary.locator('.business-summary-sources a').count(),2);assert.equal(await summary.locator('a[href*="unsafe.example"]').count(),0);assert.equal(await p.getByRole('heading',{name:profile.name,exact:true}).count(),1);
  assert.equal(await p.getByText('Serve drinks',{exact:true}).count(),1);assert.equal(await p.getByText('Confirm the camera site',{exact:true}).count(),1);assert.equal(await p.getByText(business.footage_relationship.basis,{exact:true}).count(),1);assert.equal(await p.locator('.business-profile-editor').count(),0);assert.equal(await p.getByRole('button',{name:'Edit business',exact:true}).count(),1);await p.getByRole('button',{name:'Edit business',exact:true}).click();await p.locator('#business-edit-dialog').waitFor();assert.equal(await p.getByLabel('Business name',{exact:true}).inputValue(),profile.name);await p.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`business-summary-${width}.png`)});
  assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});
test('Settings entry focuses local search once and preserves existing section controls',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page;try{
  await p.goto(origin+'/?cameras=1&collection=cafe');const search=p.getByRole('searchbox',{name:'Search settings'});await search.waitFor();await p.waitForFunction(()=>document.activeElement.id==='settings-search');
  await search.fill('usage');assert.equal(await p.locator('#settings-usage').isVisible(),true);assert.equal(await p.locator('#settings-cameras').isVisible(),false);await p.locator('#settings-usage').click();await p.locator('#usage-view').waitFor();assert.equal(await p.evaluate(()=>document.activeElement.id),'settings-usage');assert.equal(await search.inputValue(),'usage');
  await search.fill('');await p.locator('#settings-cameras').click();await p.locator('.camera-link').first().waitFor();assert.equal(await p.evaluate(()=>document.activeElement.id),'settings-cameras');assert.equal(await p.locator('#settings-cameras').getAttribute('aria-current'),'page');
  await p.locator('#camera-search').fill('View 1');assert.equal(await p.evaluate(()=>document.activeElement.id),'camera-search');assert.equal(await p.locator('.camera-link').count(),1);
  await search.fill('missing setting');assert.equal(await p.getByText('Try another setting.',{exact:true}).isVisible(),true);await search.fill('');
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`settings-shell-${width}.png`)});
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

test('Message row padding and keyboard focus reveal exact copy and recorded timestamp',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{chat:{messages:[{role:'user',content:'Review **this** exact message.',created_at:'2026-10-05T09:41:00Z'},{role:'assistant',content:'The saved answer.',completed_at:'2026-10-05T09:42:00Z'},{role:'assistant',content:'Historical message without recorded timing.'}]}}),p=h.page;try{
  await p.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copiedMessage=text;}}}));await p.mouse.move(0,0);
  const rows=p.locator('#messages > .message'),user=rows.nth(0),answer=rows.nth(1),legacy=rows.nth(2),actions=user.locator('.message-actions');
  assert.equal(await actions.count(),1);assert.equal(await actions.evaluate(el=>getComputedStyle(el).opacity),'0');await user.hover({position:{x:2,y:2}});assert.equal(await actions.evaluate(el=>getComputedStyle(el).opacity),'1');
  assert.equal(await actions.locator('time').getAttribute('datetime'),'2026-10-05T09:41:00.000Z');assert.equal(await actions.locator('time').getAttribute('title'),await p.evaluate(()=>new Date('2026-10-05T09:41:00Z').toLocaleString()));
  await user.getByRole('button',{name:'Copy message',exact:true}).click();assert.equal(await p.evaluate(()=>window.copiedMessage),'Review **this** exact message.');
  await p.mouse.move(0,0);await answer.focus();assert.equal(await answer.locator('.message-actions').evaluate(el=>getComputedStyle(el).opacity),'1');await p.keyboard.press('Tab');assert.equal(await answer.getByRole('button',{name:'Copy message',exact:true}).evaluate(el=>el===document.activeElement),true);await p.keyboard.press('Enter');assert.equal(await p.evaluate(()=>window.copiedMessage),'The saved answer.');
  assert.equal(await legacy.locator('time').count(),0);assert.equal(await legacy.getByRole('button',{name:'Copy message',exact:true}).count(),1);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`message-actions-${width}.png`)});assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

test('Semantic tool groups retain lifecycle rows and open only owned file and clip references',async()=>{
 for(const width of [1600,390]){const clip={id:'fixture-clip',dataset:'cafe',camera:'Counter',start:0,end:8,thumbnail_url:'/thumbnails/fixture-clip.jpg'},tools=[{id:'read',name:'read_files',status:'complete',arguments:{paths:['/workspace/private/report.txt']},output:'Private runtime text'},{id:'command',name:'run_command',status:'complete',arguments:{command:'pwd'},output:'/workspace'},{id:'web',name:'web_search',status:'complete',arguments:{query:'Public reference'},output:'Found source'},{id:'compact',name:'compact_context',status:'complete',output:'Compacted'},{id:'file',name:'read_file',status:'complete',arguments:{file_id:documentFile.id},output:'Read saved file'},{id:'image',name:'inspect_clip',status:'complete',arguments:{clip_id:clip.id},output:'Reviewed source'},{id:'failed',name:'run_command',status:'failed',arguments:{command:'false'},output:'Exit 1'}];
 const h=await pageFor(width,{files:[documentFile],chat:{messages:[chat.messages[0],{role:'assistant',content:'The source review is ready.',elapsed_ms:92000,tools,artifacts:[documentFile],results:[clip]}]}}),p=h.page;try{
  await p.route('**/api/files/report-1',r=>r.fulfill({contentType:'application/json',body:JSON.stringify(documentFile)}));await p.route('**/thumbnails/*',r=>r.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="gray"/></svg>'}));
  await p.locator('.completed-turn > summary').click();const group=p.locator('.tool-trail-group').first();await group.waitFor();assert.match(await group.locator(':scope > summary').textContent(),/Read files, ran a command, searched the web/);assert.equal(await group.locator(':scope > summary').evaluate(el=>getComputedStyle(el).borderWidth),'0px');await group.locator(':scope > summary').click();
  assert.equal(await group.locator('[data-tool-id="read"]').count(),1);assert.equal(await group.locator('[data-tool-id="compact"]').count(),0);assert.equal(await group.locator('[data-tool-id="failed"]').count(),0);
  const raw=p.locator('[data-tool-id="read"]');assert.equal(await raw.locator('a,button').count(),0);assert.equal(await raw.locator('[title="/workspace/private/report.txt"]').count(),1);
  const file=p.locator('[data-tool-id="file"]');await file.getByRole('button',{name:documentFile.name,exact:true}).click();await p.locator('#file-preview-content').waitFor();assert.equal(await p.locator('#files-panel-title').textContent(),documentFile.title);await p.locator('#close-chat-files').click();
  const media=p.locator('[data-tool-id="image"]');await media.locator('summary').click();const image=media.locator('.tool-trail-media img');await image.waitFor();assert.equal(new URL(await image.getAttribute('src'),origin).pathname,clip.thumbnail_url);assert.equal(await media.locator('.tool-trail-media').evaluate(el=>getComputedStyle(el).flexWrap),'nowrap');await media.locator('.tool-trail-media button').click();await p.locator('#clip-dialog').waitFor();await p.keyboard.press('Escape');
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`semantic-tool-trail-${width}.png`)});assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

const captionedClips=Array.from({length:5},(_,index)=>({id:'caption-clip-'+index,dataset:'cafe',camera:'Counter',start:index*8,end:index*8+8,thumbnail_url:'/thumbnails/caption-'+index+'.jpg',caption:index===0?'<img onerror=alert(1)> One person approaches the counter.':'A question-specific example '+index,caption_kind:'assistant_summary'}));
test('Explicit video presentation uses up to three inline players with literal question captions',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{chat:{messages:[chat.messages[0],{role:'assistant',content:'These clips show the relevant moments.',evidence_presentation:{mode:'players',items:captionedClips.slice(0,3)}}]}}),p=h.page;try{
  const players=p.locator('.evidence-inline-player');await players.first().waitFor();assert.equal(await players.count(),3);assert.equal(await p.locator('.evidence-caption').first().textContent(),captionedClips[0].caption);assert.equal(await p.locator('.evidence-caption img').count(),0);
  for(let i=0;i<3;i++){const video=players.nth(i).locator('video');assert.equal(await video.getAttribute('src'),'/clips/caption-clip-'+i+'.mp4');assert.equal(await video.getAttribute('preload'),'none');assert.equal(await video.evaluate(el=>el.controls&&!el.autoplay&&el.playsInline),true);}
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`inline-video-${width}.png`)});assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});
test('Streamed gallery presentation preserves chosen captions and larger sets stay galleries',async()=>{
 const h=await pageFor(390,{chatEvents:[{type:'evidence_presentation',presentation:{mode:'gallery',items:captionedClips}},{type:'delta',text:'Review these five examples.'},{type:'done'}]}),p=h.page;try{
  await p.locator('#message').fill('Show relevant examples.');await p.locator('#send').click();await p.getByText('Review these five examples.',{exact:true}).waitFor();assert.equal(await p.locator('.evidence-inline-player').count(),0);assert.equal(await p.locator('#messages .evidence-card').count(),4);assert.equal(await p.locator('#messages .evidence-caption').count(),4);await p.getByRole('button',{name:'View all 5 clips',exact:true}).click();assert.equal(await p.locator('#gallery-grid .evidence-caption').count(),5);assert.equal(await p.locator('#gallery-grid .evidence-caption').last().textContent(),captionedClips[4].caption);await p.keyboard.press('Escape');
  assert.equal(await p.evaluate(()=>Object.values(localStorage).some(value=>value.includes('evidence_presentation') && value.includes('A question-specific example 4'))),true);
 }finally{await h.close();}
 const g=await pageFor(390,{chat:{messages:[chat.messages[0],{role:'assistant',content:'An older malformed player selection.',evidence_presentation:{mode:'players',items:captionedClips}}]}});try{assert.equal(await g.page.locator('.evidence-inline-player').count(),0);assert.equal(await g.page.locator('.evidence-card').count(),4);}finally{await g.close();}
});

test('Tool file preview discards a late owner-scoped metadata reply',async()=>{
 const h=await pageFor(390,{files:[documentFile],chat:{messages:[chat.messages[0],{role:'assistant',content:'A saved file.',tools:[{id:'private-file',name:'read_file',status:'complete',arguments:{file_id:documentFile.id}}],artifacts:[documentFile]}]}}),p=h.page;let held;
 try{await p.route('**/api/files/report-1',r=>{held=r;});await p.locator('.completed-turn > summary').click();await p.locator('[data-tool-id="private-file"]').getByRole('button',{name:documentFile.name,exact:true}).click();for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await held.fulfill({contentType:'application/json',body:JSON.stringify(documentFile)});held=null;await p.waitForTimeout(40);assert.equal(await p.locator('#chat-files-panel').isVisible(),false);assert.equal(await p.locator('#file-preview-content').count(),0);assert.deepEqual(h.writes,['/api/observer']);}
 finally{if(held)await held.abort().catch(()=>{});await h.close();}
});

test('External source icons request only public domains and preserve internal evidence links',async()=>{
 const h=await pageFor(390,{chat:{messages:[chat.messages[0],{role:'assistant',content:'[Reference](https://docs.example.com/private/report?token=fixture-private) and [internal clip](/?clip=fixture-clip) and [local](http://localhost/example).'}]}}),p=h.page,hosts=[];
 try{await p.route('**/api/source-icon?*',r=>{hosts.push(new URL(r.request().url()).search);return r.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><circle cx="8" cy="8" r="6"/></svg>'});});await p.reload();const link=p.locator('.assistant-text').getByRole('link',{name:'Reference',exact:true});await link.waitFor();await link.locator('.source-favicon.loaded').waitFor();assert.equal(await link.locator('.source-favicon').getAttribute('src'),'/api/source-icon?host=docs.example.com');assert.equal(await link.getAttribute('href'),'https://docs.example.com/private/report?token=fixture-private');assert.equal(await p.locator('a').filter({hasText:'internal clip'}).locator('.source-favicon').count(),0);assert.equal(await p.locator('a').filter({hasText:'local'}).locator('.source-favicon').count(),0);assert(hosts.every(value=>value==='?host=docs.example.com'));assert.deepEqual(h.writes,[]);}
 finally{await h.close();}
});

test('Sanitized rich answers render code, math and diagrams with working copy and table expansion',async()=>{
 const content=['Inline math \\(x^2 + y^2\\) and $20 currency.','','```javascript','const answer = 42;','```','','| Name | Value |','| --- | --- |','| Review | 42 |','','```mermaid','flowchart LR',' A[Input] --> B[Review]','```','','<img src=x onerror="window.richInjected=true">'].join('\n');
 for(const width of [1600,390]){const h=await pageFor(width,{chat:{messages:[chat.messages[0],{role:'assistant',content}]}}),p=h.page;try{
  await p.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.richCopied=text;}}}));
  await p.locator('.rich-code .hljs-keyword').waitFor();await p.locator('.rich-math .katex').waitFor();assert((await p.locator('.assistant-text').textContent()).includes('$20 currency.'));assert.equal(await p.evaluate(()=>window.richInjected),undefined);assert.equal(await p.locator('.assistant-text img[src="x"]').count(),0);
  await p.getByRole('button',{name:'Copy code',exact:true}).click();assert.equal(await p.evaluate(()=>window.richCopied),'const answer = 42;\n');await p.getByRole('button',{name:'Copy table',exact:true}).click();assert.equal(await p.evaluate(()=>window.richCopied),'Name\tValue\nReview\t42');
  await p.getByRole('button',{name:'Expand table',exact:true}).click();await p.getByRole('dialog',{name:'Table',exact:true}).waitFor();assert.equal(await p.locator('.rich-dialog th').count(),2);await p.keyboard.press('Escape');assert.equal(await p.getByRole('button',{name:'Expand table',exact:true}).evaluate(el=>el===document.activeElement),true);
  const frame=p.locator('iframe.rich-diagram');await frame.waitFor();assert.equal(await frame.getAttribute('sandbox'),'allow-scripts');await frame.contentFrame().locator('svg').waitFor({timeout:15000});await p.getByRole('button',{name:'View source',exact:true}).click();assert.equal(await p.locator('.rich-code code.language-mermaid').isVisible(),true);await p.getByRole('button',{name:'View diagram',exact:true}).click();await frame.contentFrame().locator('svg').waitFor({state:'visible',timeout:5000});
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`rich-answer-${width}.png`)});assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

const nativeAudioFile={...documentFile,id:'file-audio',name:'voice-note.wav',title:'Voice note',media_type:'audio/wav',representation:'native_original',display_type:'audio',inline_preview_type:'audio',inline_preview_url:'/api/files/file-audio/preview',preview_format:null,preview_url:'/api/files/file-audio/content',download_url:'/api/files/file-audio/download',copyable:false};
function wavFixture(){const body=Buffer.alloc(44+4000,128);body.write('RIFF',0);body.writeUInt32LE(body.length-8,4);body.write('WAVEfmt ',8);body.writeUInt32LE(16,16);body.writeUInt16LE(1,20);body.writeUInt16LE(1,22);body.writeUInt32LE(8000,24);body.writeUInt32LE(8000,28);body.writeUInt16LE(1,32);body.writeUInt16LE(8,34);body.write('data',36);body.writeUInt32LE(4000,40);return body;}
test('Native audio uses a private player and the shared viewer pauses playback when closed',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{files:[nativeAudioFile],fileContent:null,chat:{messages:[chat.messages[0],{role:'assistant',content:'Your audio file is ready.',artifacts:[nativeAudioFile]}]}}),p=h.page;
 try{await p.route('**/api/files/file-audio/preview',r=>r.fulfill({contentType:'audio/wav',body:wavFixture()}));const audio=p.locator('.artifact-card audio');await audio.waitFor();assert.equal(await audio.getAttribute('src'),nativeAudioFile.inline_preview_url);assert.equal(await audio.getAttribute('preload'),'none');assert.equal(await audio.evaluate(el=>el.controls&&!el.autoplay),true);await audio.evaluate(el=>{el.load();});await p.waitForFunction(()=>document.querySelector('.artifact-card audio')?.duration===.5);await audio.evaluate(el=>{const original=el.pause;el.pause=function(){window.artifactAudioPaused=true;return original.call(this);};});
  await p.locator('.artifact-card-main').click();const preview=p.locator('#file-preview-content audio');await preview.waitFor();assert.equal(await p.evaluate(()=>window.artifactAudioPaused),true);assert.equal(await preview.getAttribute('src'),nativeAudioFile.inline_preview_url);assert.equal(await p.getByRole('combobox',{name:'Document zoom'}).isVisible(),false);await preview.evaluate(el=>{const original=el.pause;el.pause=function(){window.previewAudioPaused=true;return original.call(this);};});if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`native-audio-${width}.png`)});await p.locator('#close-chat-files').click();assert.equal(await p.evaluate(()=>window.previewAudioPaused),true);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});
test('Native video opens only the host-authorized source and unsupported media stays downloadable',async()=>{
 const video={...nativeAudioFile,id:'file-video',name:'review.mp4',title:'Review video',media_type:'video/mp4',display_type:'video',inline_preview_type:'video',inline_preview_url:'/api/files/file-video/preview',download_url:'/api/files/file-video/download'},h=await pageFor(390,{files:[video],fileContent:null,chat:{messages:[chat.messages[0],{role:'assistant',content:'Saved video.',artifacts:[video]}]}}),p=h.page;
 try{assert.equal(await p.locator('.artifact-card video').count(),0);await p.locator('.artifact-card-main').click();const player=p.locator('#file-preview-content video');await player.waitFor();assert.equal(await player.getAttribute('src'),video.inline_preview_url);assert.equal(await player.evaluate(el=>el.controls&&!el.autoplay&&el.playsInline),true);assert.equal(await player.evaluate(el=>getComputedStyle(el).objectFit),'contain');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'native-video-390.png')});assert.deepEqual(h.writes,[]);}finally{await h.close();}
 for(const file of [{...nativeAudioFile,inline_preview_url:'https://private.example.com/audio.wav'},{...nativeAudioFile,media_type:'text/html'},{...nativeAudioFile,representation:'unverified'},{...nativeAudioFile,inline_preview_url:'/api/files/someone-else/preview'}]){const g=await pageFor(390,{files:[file],fileContent:null,chat:{messages:[chat.messages[0],{role:'assistant',content:'Saved file.',artifacts:[file]}]}});try{assert.equal(await g.page.locator('.artifact-card audio').count(),0);await g.page.locator('.artifact-card-main').click();await g.page.locator('#file-preview').getByText('Download this file to open its full contents.',{exact:true}).waitFor();assert.equal(await g.page.locator('#file-preview audio, #file-preview video').count(),0);assert.equal(await g.page.locator('#file-download').getAttribute('href'),origin+file.download_url);}finally{await g.close();}}
});

const draftStorageKey='rocoto-video-draft-v1:demo%3Aowner:fixture-chat';
test('Composer drafts retain exact text and attachment metadata across tabs reload and rejected admission',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page,submitted=[];try{
  const text='  Inspect this image and keep my exact draft.\nSecond line.  ',attachment={id:'a'.repeat(32),url:'/attachments/'+'a'.repeat(32)+'.png',name:'Review.png',mime:'image/png'};
  await p.route('**/api/attachments',r=>r.fulfill({contentType:'application/json',body:JSON.stringify(attachment)}));await p.route('**/api/chat',r=>{submitted.push(r.request().postDataJSON());return r.fulfill({status:402,contentType:'application/json',body:JSON.stringify({detail:'The available credit has been used.'})});});
  await p.locator('#message').fill(text);await p.locator('#image-files').setInputFiles({name:'Review.png',mimeType:'image/png',buffer:Buffer.from('fixture image')});await p.locator('.attachment-preview').getByText('Review.png',{exact:true}).waitFor();await p.locator('#marketplace-nav').click();await p.reload();await p.goto(origin+'/?chat=fixture-chat');await p.waitForFunction(value=>document.querySelector('#message').value===value,text);assert.equal(await p.locator('#message').inputValue(),text);assert.equal(await p.locator('.attachment-preview').count(),1);
  const saved=await p.evaluate(key=>JSON.parse(localStorage.getItem(key)),draftStorageKey);assert.equal(saved.text,text);assert.deepEqual(saved.attachments,[attachment]);assert(!JSON.stringify(saved).includes('data:'));await p.locator('#send').click();await p.locator('.message-assistant').getByText('The available credit has been used.',{exact:true}).waitFor();assert.equal(submitted.length,1);assert.deepEqual(submitted[0].attachments,[attachment.id]);assert.equal(await p.locator('#message').inputValue(),text);assert.equal(await p.locator('.attachment-preview').count(),1);await p.reload();assert.equal(await p.locator('#message').inputValue(),text);assert.equal(await p.locator('.attachment-preview').count(),1);
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();assert.equal(await p.locator('#message').inputValue(),'');assert.equal(await p.locator('.attachment-preview').count(),0);assert.equal(await p.evaluate(key=>JSON.parse(localStorage.getItem(key)).text,draftStorageKey),text);
 }finally{await h.close();}}
});
test('Funded admission notice persists above the composer and usage bars retain real balances',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page;try{
  await p.route('**/api/usage*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({company:{available_usd:'0',prepaid_funded_usd:'40',provider_credit_funded_usd:'0',reserved_usd:'2',monthly_used_usd:'38',monthly_limit_usd:'40',monthly_available_usd:'0'},codex:{route:'OpenAI API',limits:[]},billing:{status:'prepaid_api',auto_reload:false}})}));await p.reload();await p.locator('#funding-notice').waitFor();assert.match(await p.locator('#funding-notice').textContent(),/credit|allowance|limit/i);await p.locator('#message').fill('Keep this for later.');await p.locator('#send').click();assert.deepEqual(h.writes,[]);assert.equal(await p.locator('#message').inputValue(),'Keep this for later.');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`funding-notice-${width}.png`)});
  await p.locator('#funding-notice').getByRole('button',{name:'View usage',exact:true}).click();await p.locator('#usage-view').waitFor();assert.equal(await p.getByRole('progressbar',{name:'Monthly limit remaining',exact:true}).getAttribute('value'),'0');assert.equal(await p.getByRole('progressbar',{name:'Extra credit remaining',exact:true}).getAttribute('value'),'0');assert.equal(await p.getByRole('button',{name:'Add credits',exact:true}).isDisabled(),true);assert.equal(await p.getByRole('button',{name:'Set up auto-reload',exact:true}).isDisabled(),true);assert.equal(await p.getByText('Included in your plan',{exact:true}).count(),0);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`usage-bars-${width}.png`)});assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 }finally{await h.close();}}
});
test('Rocoto evidence cards place source time on the thumbnail and use only provided captions',async()=>{
 for(const width of [1600,390]){const clips=[{id:'evidence-a',camera:'Cafe Place Preview',dataset:'cafe',start:2,end:6,thumbnail_url:'/thumbnails/a.jpg',caption:'Two people wait beside the counter.'},{id:'evidence-b',camera:'Cafe Other Preview',dataset:'cafe',start:8,end:16,thumbnail_url:'/thumbnails/b.jpg'}],h=await pageFor(width,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Review these moments.',evidence_presentation:{mode:'gallery',items:clips}}]}}),p=h.page;try{
  await p.route('**/thumbnails/*',r=>r.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#a6a29a"/></svg>'}));await p.reload();assert.equal(await p.locator('.chats-caption > span').textContent(),'Agents');const cards=p.locator('#messages .evidence-card');assert.equal(await cards.first().locator('.evidence-timestamp').textContent(),'0:02–0:06');assert.equal(await cards.first().locator('.evidence-caption').textContent(),clips[0].caption);assert.equal(await cards.nth(1).locator('.evidence-caption').count(),0);assert.equal(await cards.locator('.evidence-camera').count(),0);await cards.first().locator('.evidence-preview').waitFor({state:'visible'});const {media,time,play}=await p.evaluate(()=>{const card=document.querySelector('#messages .evidence-card');return Object.fromEntries([['media','.evidence-preview'],['time','.evidence-timestamp'],['play','.play-icon']].map(([key,selector])=>{const el=card.querySelector(selector),box=el.getBoundingClientRect();return [key,{x:box.x,y:box.y,width:box.width,height:box.height,display:getComputedStyle(el).display}];}));});assert(media.width>0&&time.width>0&&play.width>0,JSON.stringify({media,time,play}));assert(time.y<media.y+media.height/2);assert(play.x<media.x+media.width/2&&play.y+play.height/2>media.y+media.height/2,JSON.stringify({media,time,play}));if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`evidence-cards-${width}.png`)});assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

test('Accepted sends clear only their own draft and preserve a different chat draft',async()=>{
 const h=await pageFor(),p=h.page;let held;try{await p.route('**/api/chat',r=>{held=r;});await p.locator('#message').fill('Admit this exact draft.');await p.locator('#send').click();await p.waitForFunction(()=>document.querySelector('#send').disabled);assert.equal(await p.locator('#message').inputValue(),'Admit this exact draft.');await p.locator('#add-chat').click();const next=new URL(p.url()).searchParams.get('chat');await p.locator('#message').fill('Keep my next chat separate.');assert(held);await held.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'done'})+'\n'});held=null;await p.waitForFunction(key=>localStorage.getItem(key)===null,draftStorageKey);assert.equal(await p.locator('#message').inputValue(),'Keep my next chat separate.');await p.reload();assert.equal(new URL(p.url()).searchParams.get('chat'),next);assert.equal(await p.locator('#message').inputValue(),'Keep my next chat separate.');}finally{if(held)await held.abort();await h.close();}
});
test('A late send response never clears or displays the previous owner draft in observation',async()=>{
 const h=await pageFor(),p=h.page;let held;try{await p.route('**/api/chat',r=>{held=r;});await p.locator('#message').fill('Private owner draft.');await p.locator('#send').click();await p.waitForFunction(()=>document.querySelector('#send').disabled);await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await held.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'delta',text:'Private late answer.'})+'\n'+JSON.stringify({type:'done'})+'\n'});held=null;await p.waitForTimeout(30);assert.equal(await p.locator('#message').inputValue(),'');assert.equal(await p.getByText('Private late answer.',{exact:true}).count(),0);assert.equal(await p.evaluate(key=>JSON.parse(localStorage.getItem(key)).text,draftStorageKey),'Private owner draft.');}finally{if(held)await held.abort();await h.close();}
});
test('A business switch starts another chat even when the original has only a saved draft',async()=>{
 const h=await pageFor(1600,{chat:{messages:[]}}),p=h.page;try{await p.waitForFunction(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')||'[]').some(row=>row.id==='fixture-chat'));await p.locator('#message').fill('A café draft belongs to the café.');await p.locator('#founder-nav').click();await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();assert.notEqual(new URL(p.url()).searchParams.get('chat'),'fixture-chat');assert.equal(await p.locator('#message').inputValue(),'');const stored=await p.evaluate(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')));assert.equal(stored.find(item=>item.id==='fixture-chat').dataset,'cafe');assert.equal(await p.evaluate(key=>JSON.parse(localStorage.getItem(key)).text,draftStorageKey),'A café draft belongs to the café.');}finally{await h.close();}
});

const profileFixture=scope=>({schema_version:1,scope,organisation_allowed:true,timezone:'UTC',start_date:'2026-07-07',end_date:'2026-10-05',identity:{name:scope==='organisation'?'Fixture Company':'Fixture Owner',avatar_url:null},totals:{tokens:3000,agent_runs:4,agent_runs_completed:3,chats:6,alerts_sent:null},metrics:{tokens:{available:true,coverage:'attributed_api_receipts',since:'2026-10-04'},agent_runs:{available:true}},daily:[{date:'2026-10-04',tokens:1000,agent_runs:1},{date:'2026-10-05',tokens:2000,agent_runs:3}],weekly:[{start_date:'2026-09-28',end_date:'2026-10-04',tokens:1000,agent_runs:1},{start_date:'2026-10-05',end_date:'2026-10-05',tokens:2000,agent_runs:3}],agents:[]});
test('Settings Profile and Plugins use scoped records and preserve their actual availability',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{nativePlugins:[{id:'notion',installed:true,enabled:true,version:'0.1.7'}],connections:[{id:'notion',provider:'notion',name:'Notion',authentication:'oauth',available:true,status:'disconnected'}]}),p=h.page,scopes=[];try{
  await p.route('**/api/profile/activity?*',r=>{const scope=new URL(r.request().url()).searchParams.get('scope');scopes.push(scope);return r.fulfill({contentType:'application/json',body:JSON.stringify(profileFixture(scope))});});await p.locator('#account-button').click();await p.locator('#open-profile').click();await p.locator('#profile-view .profile-identity h2').waitFor();assert.equal(await p.locator('#profile-view .profile-identity h2').textContent(),'Fixture Owner');assert.equal(await p.locator('.profile-day-labels').first().locator('span').count(),7);assert.equal(await p.getByText('Alerts sent',{exact:true}).locator('..').locator('dd').textContent(),'—');await p.getByRole('button',{name:'Weekly',exact:true}).click();assert.equal(await p.getByRole('button',{name:'Weekly',exact:true}).getAttribute('aria-pressed'),'true');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`profile-${width}.png`)});
  await openSettingsPage(p,'stats');await p.locator('#stats-activity .profile-calendar').first().waitFor();assert((await p.locator('.stats-activity-scope').textContent()).includes('Fixture Company'));assert(scopes.includes('person')&&scopes.includes('organisation'));await p.reload();await p.locator('#stats-activity .profile-calendar').first().waitFor();assert((await p.locator('.stats-activity-scope').textContent()).includes('Fixture Company'));
  await p.locator('#account-button').click();await p.locator('#open-settings').click();await p.locator('#settings-plugins').click();await p.locator('.plugin-settings-row').first().waitFor();assert.equal(await p.locator('.plugin-settings-row').first().locator('.plugin-settings-state').textContent(),'Installed');assert.equal(await p.locator('.plugin-settings input[type="checkbox"]').count(),0);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`plugins-${width}.png`)});await p.getByRole('tab',{name:/MCPs/}).click();assert.equal(await p.locator('.plugin-settings-row').first().locator('.plugin-settings-state').textContent(),'Account setup');await p.getByRole('button',{name:'Manage Notion',exact:true}).click();await p.locator('.connection-detail-screen').waitFor();assert.equal(await p.getByRole('button',{name:'Connect account',exact:true}).isEnabled(),true);assert.deepEqual(h.writes,[]);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 }finally{await h.close();}}
});
test('Profile ignores a late previous-owner result and organisation stays hidden during observation search',async()=>{
 const h=await pageFor(390),p=h.page;let held,first=true;try{await p.route('**/api/profile/activity?*',r=>{if(first){first=false;held=r;return;}return r.fulfill({contentType:'application/json',body:JSON.stringify({...profileFixture('person'),identity:{name:'Customer'},organisation_allowed:false})});});await p.locator('#account-button').click();await p.locator('#open-profile').click();for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await held.fulfill({contentType:'application/json',body:JSON.stringify(profileFixture('person'))});held=null;if(await p.locator('#profile-menu').isHidden())await p.locator('#account-button').click();await p.locator('#open-profile').click();await p.locator('.profile-identity h2').getByText('Customer',{exact:true}).waitFor();assert.equal(await p.locator('.profile-identity h2').getByText('Fixture Owner',{exact:true}).count(),0);await p.locator('#settings-search').fill('Stats');assert.equal(await p.locator('#settings-stats').isVisible(),false);assert.equal(await p.locator('#message').inputValue(),'');}finally{if(held)await held.abort();await h.close();}
});
test('Camera Streams keep setup separate while recorded previews cycle through actual source clips',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{business:{connections:[{id:'camera-setup',type:'camera_stream',provider:'Axis',source_description:'Reception camera setup.'}]}}),p=h.page,records=[1,2].map(i=>({id:'camera-'+i,name:'Recorded view '+i,collection:'cafe',asset_ids:['asset-'+i],clip_count:1,reference_clip:{id:'recorded-'+i,start:0,end:8,thumbnail_url:'/thumbnails/recorded-'+i+'.jpg'}}));try{
  await p.route('**/api/retail/cameras?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({cameras:records})}));await p.locator('#account-button').click();await p.locator('#open-settings').click();await p.locator('#settings-cameras').click();await p.getByRole('button',{name:'Preview Recorded view 1',exact:true}).waitFor();await p.getByRole('button',{name:'Preview Recorded view 1',exact:true}).click();assert.equal(await p.locator('#clip-position').textContent(),'1 of 2');await p.locator('#next-clip').click();assert.equal(await p.locator('#clip-position').textContent(),'2 of 2');assert.equal(await p.locator('#clip-player').getAttribute('src'),'/clips/recorded-2.mp4');await p.keyboard.press('Escape');await p.locator('#camera-streams-tab').click();await p.locator('.camera-stream-state').waitFor();assert.equal(await p.locator('.camera-stream-state').textContent(),'Setup in progress');assert.equal(await p.locator('#camera-streams-content video').count(),0);assert.equal(await p.locator('#camera-layout').isVisible(),false);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`camera-streams-${width}.png`)});await p.locator('#add-cameras').click();await p.locator('#camera-provider-dialog').waitFor();assert.deepEqual(h.writes,[]);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 }finally{await h.close();}}
});
test('Pending send admission freezes attachment and skill changes until the exact draft is accepted',async()=>{
 const h=await pageFor(),p=h.page;let held,uploads=0;try{await p.route('**/api/chat',r=>{held=r;});await p.route('**/api/attachments',r=>{uploads++;return r.abort();});await p.locator('#message').fill('Hold this draft.');await p.locator('#send').click();await p.waitForFunction(()=>document.querySelector('#message').readOnly);assert.equal(await p.locator('#attachment-toggle').isDisabled(),true);await p.locator('#message').evaluate(el=>{const data=new DataTransfer();data.items.add(new File(['fixture'],'late.png',{type:'image/png'}));el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true}));});assert.equal(uploads,0);assert.equal(await p.locator('.attachment-preview').count(),0);assert.equal(await p.locator('#message').inputValue(),'Hold this draft.');await held.fulfill({status:402,contentType:'application/json',body:JSON.stringify({detail:'Available credit is used.'})});held=null;await p.waitForFunction(()=>!document.querySelector('#message').readOnly);assert.equal(await p.locator('#attachment-toggle').isEnabled(),true);assert.equal(await p.locator('#message').inputValue(),'Hold this draft.');}finally{if(held)await held.abort();await h.close();}
});
test('Workspace settings persist actual actor preferences and filter team by organization',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page;try{
  await p.route('**/api/observers',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({principals:[{id:'customer',display_name:'Demo teammate',environments:[{id:'demo',role:'operator'}]},{id:'other',display_name:'Other company private name',environments:[{id:'other',role:'owner'}]}]})}));
  await p.locator('#account-button').click();await p.locator('#open-settings').click();await p.locator('#settings-appearance').click();assert.deepEqual(await p.locator('.settings-shell-group-title').allTextContents(),['Account','Workspace','Organization','Integrations']);await p.getByLabel('UI font size',{exact:true}).fill('16');await p.getByLabel('UI font size',{exact:true}).press('Tab');await p.getByLabel('Code font size',{exact:true}).fill('15');await p.getByLabel('Code font size',{exact:true}).press('Tab');await p.getByLabel('Reduce motion',{exact:true}).selectOption('reduce');await p.reload();await p.getByLabel('UI font size',{exact:true}).waitFor();assert.equal(await p.getByLabel('UI font size',{exact:true}).inputValue(),'16');assert.equal(await p.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--font-size-code').trim()),'15px');assert.equal(await p.locator('html').getAttribute('data-reduce-motion'),'true');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`appearance-${width}.png`)});
  await p.locator('#settings-team').click();await p.getByText('Demo teammate',{exact:true}).waitFor();assert.equal(await p.getByText('Other company private name',{exact:true}).count(),0);assert.equal(await p.getByText('Fixture Owner',{exact:true}).count()>=1,true);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`team-${width}.png`)});
  await p.locator('#settings-billing').click();await p.getByText('Customer payment setup',{exact:true}).waitFor();assert.equal(await p.getByText('Being prepared',{exact:true}).count(),1);assert.equal(await p.locator('#billing-view input').count(),0);await p.getByRole('button',{name:'View usage',exact:true}).click();await p.locator('#usage-view').waitFor();
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();if(await p.locator('#profile-menu').isHidden())await p.locator('#account-button').click();await p.locator('#open-settings').click();await p.locator('#settings-appearance').click();assert.equal(await p.getByLabel('UI font size',{exact:true}).inputValue(),'16');assert.equal(await p.evaluate(()=>Object.keys(localStorage).some(k=>k.startsWith('rocoto-settings-v1:')&&k.includes('customer'))),false);
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 }finally{await h.close();}}
});
test('Desktop completion notifications only follow successful live completion with actor consent',async()=>{
 const h=await pageFor(1600,{chatEvents:[{type:'delta',text:'Completed safely.'},{type:'done',status:'completed'}]}),p=h.page;try{
  await p.context().addInitScript(()=>{window.__notices=[];class FixtureNotification{static permission='default';static async requestPermission(){FixtureNotification.permission='granted';return 'granted';}constructor(title,options){window.__notices.push({title,...options});}close(){}}Object.defineProperty(window,'Notification',{value:FixtureNotification,configurable:true});});await p.reload();await p.locator('#account-button').click();await p.locator('#open-settings').click();await p.locator('#settings-notifications').click();await p.getByRole('button',{name:'Enable desktop notifications',exact:true}).click();await p.getByRole('switch',{name:'Desktop task completion'}).waitFor();assert.equal(await p.getByRole('switch',{name:'Desktop task completion'}).isChecked(),true);assert.equal(await p.evaluate(()=>window.__notices.length),0);
  await p.locator('#new-chat').click();await p.locator('#message').fill('Complete this active task.');await p.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}));await p.locator('#send').click();await p.waitForFunction(()=>window.__notices.length===1);assert.deepEqual(await p.evaluate(()=>window.__notices.map(({title,body})=>({title,body}))),[{title:'Rocoto',body:'Your task is finished.'}]);await p.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,get:()=>false}));await p.locator('#message').fill('Foreground task.');await p.locator('#send').click();await p.waitForFunction(()=>document.querySelector('#send').disabled===false);await p.waitForTimeout(30);assert.equal(await p.evaluate(()=>window.__notices.length),1);await p.reload();await p.locator('#messages').waitFor();assert.equal(await p.evaluate(()=>window.__notices.length),0);
 }finally{await h.close();}
});
const mapMetadata={id:'map-'+ 'a'.repeat(32),collection:'cafe',title:'Fixture neighborhood',url:'https://malicious.invalid/private',created_at:'2026-10-05T12:00:00Z'};
test('Saved maps use private scoped data inline and expand, while stale latest maps await new context',async()=>{
 for(const width of [1600,390]){const reads=[],h=await pageFor(width,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Here is the saved neighborhood.',business_map:mapMetadata}]},business:{revision:2,profile:{name:'Fixture cafe',location:'Current location'},connections:[],settings:{}},businessMap:url=>{reads.push(url.pathname+url.search);return {status:'stale',map:mapMetadata,data:url.searchParams.has('map_id')?mapSourceFixture():null};}}),p=h.page;try{
  await p.locator('#messages .business-map[data-map-state="ready"]').waitFor();assert(reads.every(url=>url.startsWith('/api/business/map?collection=cafe')));assert(reads.some(url=>url.includes('map_id='+mapMetadata.id)));await p.getByText('Saved map · The business location has changed.',{exact:true}).waitFor();assert.equal(await p.locator('.business-map-pin').count(),3);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`inline-map-${width}.png`)});await p.getByRole('button',{name:'Expand map',exact:true}).click();await p.locator('#business-map-dialog .business-map[data-map-state="ready"]').waitFor();await p.locator('#business-map-dialog').getByRole('button',{name:'Fixture shop, Shop',exact:true}).click();assert.equal(await p.locator('#business-map-dialog .business-map-selected strong').textContent(),'Fixture shop');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`expanded-map-${width}.png`)});await p.keyboard.press('Escape');await p.waitForFunction(()=>document.querySelector('#business-map-expanded').textContent==='');
  await openSettingsPage(p,'business');await p.getByText('Prepare a map for the current business location in chat.',{exact:true}).waitFor();assert.equal(await p.locator('#business-content .business-map-svg').count(),0);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});
test('A map streaming event is stored as metadata and late previous-owner geometry is discarded',async()=>{
 const h=await pageFor(390,{chatEvents:[{type:'business_map',map:mapMetadata},{type:'delta',text:'A scoped map.'},{type:'done',status:'completed'}]}),p=h.page;let held;try{
  await p.route('**/api/business/map?*',r=>{held=r;});await p.locator('#message').fill('Show the saved map.');await p.locator('#send').click();await p.waitForFunction(()=>document.querySelector('#send').disabled===false);for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('malicious.invalid')),false);await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await held.fulfill({contentType:'application/json',body:JSON.stringify({status:'ready',map:mapMetadata,data:mapSourceFixture()})});held=null;await p.waitForTimeout(40);assert.equal(await p.locator('.business-map-svg').count(),0);assert.equal(await p.evaluate(()=>JSON.stringify(Object.entries(localStorage)).includes('Fixture address')),false);assert.equal(await p.locator('#business-map-dialog').isVisible(),false);
 }finally{if(held)await held.abort();await h.close();}
});

function mapSourceFixture() {
  const polygon = (size) => ({
      type: "Polygon",
      coordinates: [
        [
          [-3 - size, 40 - size],
          [-3 + size, 40 - size],
          [-3 + size, 40 + size],
          [-3 - size, 40 + size],
          [-3 - size, 40 - size],
        ],
      ],
    }),
    feature = (geometry, properties = {}) => ({
      type: "Feature",
      geometry,
      properties,
    }),
    collection = (features) => ({ type: "FeatureCollection", features });
  return {
    origin: {
      name: "Fixture cafe",
      coordinates: [-3, 40],
      address: "Fixture address",
      url: "https://example.com/cafe",
      source: "Fixture source",
    },
    bbox: [-3.02, 39.98, -2.98, 40.02],
    pois: [
      {
        id: "shop-one",
        name: "Fixture shop",
        category: "shop",
        coordinates: [-2.995, 40.002],
        url: "https://example.com/shop",
        walkMinutes: 4,
      },
      {
        id: "transit-one",
        name: "Fixture station",
        category: "transit",
        coordinates: [-3.005, 39.998],
        url: "https://example.com/station",
        walkMinutes: 6,
      },
    ],
    catchments: [5, 10, 15].map((minutes) => ({
      minutes,
      geometry: polygon(minutes * 0.0007),
    })),
    roads: collection([
      feature(
        {
          type: "LineString",
          coordinates: [
            [-3.01, 40],
            [-3, 40],
            [-2.99, 40],
          ],
        },
        { name: "Fixture street", kind: "residential" },
      ),
    ]),
    buildings: collection([feature(polygon(0.001), { use: "residential" })]),
    green: collection([feature(polygon(0.0005))]),
    stats: { totalBuildings: 1, taggedBuildings: 1 },
    methodology: {
      source: "Fixture mapped data",
      sourceTimestamp: null,
      walkingSpeedKmh: 4.8,
      walkingMethod: "network",
      catchmentNote: "Estimated nearby-street envelope.",
      poiTimeNote: "Rounded network distance plus street connector.",
      buildingUseNote: "Mapped use from source tags.",
      uncertainty: ["Fixture uncertainty."],
      attribution: "Fixture contributor",
      license: "https://example.com/license",
    },
    sources: [
      {
        title: "Fixture source",
        url: "https://example.com/source",
        retrieved_at: "2026-10-05T12:00:00Z",
      },
    ],
  };
}
test('Chat refresh preserves an in-flight image attachment until upload metadata is saved',async()=>{
 const h=await pageFor(),p=h.page;let held;const attachment={id:'b'.repeat(32),url:'/attachments/'+'b'.repeat(32)+'.png',name:'Still uploading.png',mime:'image/png'};try{
  await p.route('**/api/attachments',r=>{held=r;});await p.locator('#message').fill('Keep the pending image.');await p.locator('#image-files').setInputFiles({name:attachment.name,mimeType:'image/png',buffer:Buffer.from('fixture')});await p.locator('.attachment-preview').getByText('Uploading',{exact:true}).waitFor();await p.locator('#account-button').click();await p.locator('#connection-info summary').click();const refreshed=p.waitForResponse(r=>new URL(r.url()).pathname==='/api/chats');await p.getByRole('button',{name:'Reconnect server',exact:true}).click();await refreshed;await p.waitForTimeout(30);assert.equal(await p.locator('.attachment-preview').count(),1);await held.fulfill({contentType:'application/json',body:JSON.stringify(attachment)});held=null;await p.locator('.attachment-preview').getByText(attachment.name,{exact:true}).waitFor();assert.deepEqual(await p.evaluate(key=>JSON.parse(localStorage.getItem(key)).attachments,draftStorageKey),[attachment]);
 }finally{if(held)await held.abort();await h.close();}
});
test('Editing the business location revalidates a previously cached historical map without relabeling its geometry',async()=>{
 let revision=1,historicalReads=0;const h=await pageFor(1600,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Original map.',business_map:mapMetadata}]},businessMap:url=>{if(url.searchParams.has('map_id'))historicalReads++;return {status:revision===1?'ready':'stale',map:mapMetadata,data:revision===1||url.searchParams.has('map_id')?mapSourceFixture():null};}}),p=h.page;try{
  await p.locator('#messages .business-map[data-map-state="ready"]').waitFor();assert.equal(historicalReads,1);await p.route('**/api/business?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({revision,profile:{name:'Fixture cafe',location:revision===1?'Original address':'Changed address'},settings:{},connections:[]})}));await p.route('**/api/business/profile',r=>{assert.equal(r.request().postDataJSON().profile.location,'Changed address');revision++;return r.fulfill({contentType:'application/json',body:'{}'});});await openSettingsPage(p,'business');await p.getByRole('button',{name:'Edit business',exact:true}).click();await p.getByLabel('Location',{exact:true}).fill('Changed address');await p.getByRole('button',{name:'Save changes',exact:true}).click();await p.getByText('Prepare a map for the current business location in chat.',{exact:true}).waitFor();await p.locator('#new-chat').click();await p.locator('#chat-list').getByText('Fixture café review',{exact:true}).click();await p.locator('#messages .business-map[data-map-state="ready"]').waitFor();await p.locator('#messages').getByText('Saved map · The business location has changed.',{exact:true}).waitFor();assert(historicalReads>=2);assert.equal(await p.locator('#messages .business-map-selected strong').textContent(),'Fixture cafe');assert.equal(await p.locator('#messages .business-map-selected span').textContent(),'Fixture address');
 }finally{await h.close();}
});

const statsBusinessFixture={revision:1,profile:{name:'Fixture Cafe',business_type:'Cafe',location:'Fixture Street',website:'https://example.com',description:'A business description grounded in the supplied profile.',operations:['Morning service'],assumptions:[],source_urls:['https://example.com/about']},settings:{initial_history_hours:48},connections:[],provenance:{epistemic_status:'operator_provided',recorded_at:'2026-10-05T12:00:00Z'}};
test('Settings separates Your Business from organization Stats and preserves legacy entry links',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{business:statsBusinessFixture}),p=h.page,reads=[];try{
  await p.route('**/api/profile/activity?*',r=>{reads.push(new URL(r.request().url()).searchParams.get('scope'));return r.fulfill({contentType:'application/json',body:JSON.stringify(profileFixture('organisation'))});});
  await p.locator('#message').fill('Preserve this chat draft.');await openSettingsPage(p,'business');await p.locator('#business-view').getByText('Fixture Cafe',{exact:true}).first().waitFor();
  assert.equal(new URL(p.url()).searchParams.get('settings'),'business');assert.equal(await p.locator('#settings-nav').isVisible(),true);assert.equal(await p.locator('#stats-nav').count(),0);assert.equal(await p.locator('#stats-view').isVisible(),false);assert.deepEqual(reads,[]);
  assert.equal(await p.getByText('Profile fields',{exact:true}).count(),0);assert.equal(await p.locator('#business-view input[name="initial_history_hours"]').count(),0);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`settings-business-${width}.png`)});
  await p.getByRole('button',{name:'Edit business',exact:true}).click();await p.getByLabel('Business name',{exact:true}).fill('Cancelled edit');await p.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal(await p.locator('#business-edit-dialog').count(),0);assert.equal(await p.locator('#business-view').getByText('Fixture Cafe',{exact:true}).count(),1);
  await p.locator('#settings-stats').click();await p.locator('#stats-activity .profile-calendar').first().waitFor();assert.equal(new URL(p.url()).searchParams.get('settings'),'stats');assert.equal(await p.locator('#business-view').isVisible(),false);assert.equal(await p.locator('#stats-view').getByText('Fixture Cafe',{exact:true}).count(),0);assert((await p.locator('.stats-activity-scope').textContent()).includes('All demo collections'));assert.deepEqual(reads,['organisation']);
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`settings-stats-${width}.png`)});
  await p.locator('#settings-cameras').click();await p.locator('#cameras-view').waitFor();assert.equal(await p.locator('#add-cameras').isVisible(),true);
  await p.goto(origin+'/?chat=fixture-chat');await p.locator('#messages .message').first().waitFor();assert.equal(await p.locator('#message').inputValue(),'Preserve this chat draft.');
  for(const [legacy,view]of [['?business=1&collection=cafe','business'],['?settings=organisation-profile','stats'],['?stats=1','stats']]){await p.goto(origin+'/'+legacy);await p.locator('#'+view+'-view').waitFor();assert.equal(new URL(p.url()).searchParams.get('settings'),view);assert.equal(new URL(p.url()).searchParams.has('stats'),false);}
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});
test('Founder demo switching stays private and preserves the existing chat business and draft',async()=>{
 const h=await pageFor(1600,{chat:{messages:[]},business:statsBusinessFixture,statusDelay:1000}),p=h.page;try{
  await p.waitForFunction(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')||'[]').some(c=>c.id==='fixture-chat'));await p.locator('#message').fill('A café draft stays with this chat.');await p.locator('#founder-nav').click();await p.locator('#founder-demo-search').fill('Warehouse');await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();
  assert.notEqual(new URL(p.url()).searchParams.get('chat'),'fixture-chat');assert.equal(await p.locator('#message').inputValue(),'');assert.equal(await p.evaluate(key=>JSON.parse(localStorage.getItem(key)).text,draftStorageKey),'A café draft stays with this chat.');
  const stored=await p.evaluate(()=>JSON.parse(localStorage.getItem('rocoto-video-chats-v1:demo%3Aowner')));assert.equal(stored.find(c=>c.id==='fixture-chat').dataset,'cafe');
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();assert.equal(await p.locator('#founder-nav').isVisible(),false);assert.equal(await p.locator('#founder-demo-popover').isVisible(),false);
 }finally{await h.close();}
 for(const options of [{role:'operator'},{environment:{id:'customer',name:'Customer business'},environments:[{id:'customer',name:'Customer business',category:'company'}]}]){const k=await pageFor(390,options);try{assert.equal(await k.page.locator('#founder-nav').isVisible(),false);}finally{await k.close();}}
});
test('New Chat and Search have centered clean starts and distinct full-area dreamscape artwork',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width,{chat:{messages:[]}}),p=h.page;try{
  await p.locator('body.empty-chat').waitFor();assert.equal(await p.locator('#chat-title').isVisible(),false);assert.equal(await p.locator('#chat-context-toggle').isVisible(),false);assert.equal(await p.locator('#home-mode-switch').isVisible(),false);
  const check=async(name)=>{const value=await p.locator('#workspace-backdrop').evaluate(el=>{const r=el.getBoundingClientRect(),w=document.querySelector('.workspace').getBoundingClientRect(),c=document.querySelector('.composer-area').getBoundingClientRect();return{visible:getComputedStyle(el).display!=='none',image:getComputedStyle(el).backgroundImage,width:r.width,height:r.height,workspaceWidth:w.width,workspaceHeight:w.height,centerDelta:Math.abs(c.x+c.width/2-(w.x+w.width/2+(parseFloat(getComputedStyle(document.querySelector('.workspace')).paddingLeft)-parseFloat(getComputedStyle(document.querySelector('.workspace')).paddingRight))/2))};});assert(value.visible);assert(value.image.includes(name+'-dreamscape.png'));assert(value.width>=value.workspaceWidth-2&&value.height>=value.workspaceHeight-2);assert(value.centerDelta<5);await p.evaluate(url=>new Promise((resolve,reject)=>{const i=new Image();i.onload=resolve;i.onerror=reject;i.src=url;}),'/assets/'+name+'-dreamscape.png');};
  await check('chat');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`empty-chat-${width}.png`)});
  await p.locator('#quick-search-nav').click();await p.locator('body.empty-search').waitFor();await check('search');if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`empty-search-${width}.png`)});
  await p.goto(origin+'/?chat=fixture-chat');await p.locator('#new-chat').click();await p.locator('#message').fill('A draft remains readable.');assert.equal(await p.locator('#message').inputValue(),'A draft remains readable.');assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.writes,[]);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}}
});

test('Empty Chat and Search align welcome content and composer without changing saved chats',async(t)=>{
 for(const width of [1600,390])await t.test(`${width}px`,async()=>{
  const h=await pageFor(width),p=h.page;
  try{
   const savedComposer=await p.locator('#composer').boundingBox();assert.equal(await p.locator('#welcome').isVisible(),false);
   await p.locator('#new-chat').click();await p.locator('body.empty-chat').waitFor();await p.locator('#welcome .suggestions button').first().waitFor();await p.keyboard.press('Escape');
   const geometry=async()=>p.evaluate(()=>{
    const box=el=>{const r=el.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,center:r.x+r.width/2,right:r.right};};
    const welcome=document.querySelector('#welcome'),list=welcome.querySelector('.suggestions');
    return{welcome:box(welcome),icon:box(welcome.querySelector('.welcome-icon')),title:box(welcome.querySelector('h1')),composer:box(document.querySelector('#composer')),suggestions:box(list),rows:[...list.querySelectorAll('button')].map(row=>{
     const range=document.createRange();range.selectNodeContents(row.firstChild);const text=range.getBoundingClientRect(),css=getComputedStyle(row);
     return{row:box(row),text:{x:text.x,right:text.right},chevron:box(row.querySelector('.icon')),textAlign:css.textAlign,leftInset:parseFloat(css.paddingLeft)+parseFloat(css.borderLeftWidth),rightInset:parseFloat(css.paddingRight)+parseFloat(css.borderRightWidth)};
    }),overflow:document.documentElement.scrollWidth>innerWidth};
   });
   const check=(layout,withSuggestions)=>{
    const info=JSON.stringify({viewport:width,...layout});
    for(const key of ['welcome','icon','title',...(withSuggestions?['suggestions']:[])])assert(Math.abs(layout[key].center-layout.composer.center)<=1,`${key} horizontal center: ${info}`);
    for(const key of ['welcome',...(withSuggestions?['suggestions']:[])])assert(Math.abs(layout[key].width-layout.composer.width)<=1,`${key} primary width: ${info}`);
    if(withSuggestions){assert(layout.rows.length>0);for(const item of layout.rows){assert.equal(item.textAlign,'left');assert(Math.abs(item.text.x-item.row.x-item.leftInset)<=1,`left-aligned suggestion text: ${info}`);assert(Math.abs(item.chevron.right-item.row.right+item.rightInset)<=1,`trailing chevron: ${info}`);assert(item.text.right<=item.chevron.x,`separate text and chevron: ${info}`);}}
    assert.equal(layout.overflow,false,info);
   };
   const chatLayout=await geometry();
   if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`landing-alignment-chat-${width}.png`)});}
   await p.locator('#quick-search-nav').click();await p.locator('body.empty-search').waitFor();assert.equal(await p.locator('#welcome .suggestions').isVisible(),false);const searchLayout=await geometry();
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`landing-alignment-search-${width}.png`)});
   await p.goto(origin+'/?chat=fixture-chat');await p.locator('#messages .message').first().waitFor();assert.equal(await p.locator('#welcome').isVisible(),false);assert.equal(await p.locator('body.empty-chat,body.empty-search').count(),0);assert(Math.abs((await p.locator('#composer').boundingBox()).width-savedComposer.width)<=1);assert.deepEqual(h.writes,[]);
   check(chatLayout,true);check(searchLayout,false);
  }finally{await h.close();}
 });
});


test('Business onboarding discards a late owner response after observer switching',async()=>{
 const h=await pageFor(1600,{business:{revision:1,profile:{name:'Owner private business'},settings:{},connections:[]}}),p=h.page;let held;
 try{
  await p.route('**/api/organization/chats',r=>{assert.equal(r.request().method(),'POST');assert.deepEqual(r.request().postDataJSON(),{collection:'cafe',purpose:'organization'});held=r;});await openSettingsPage(p,'business');await p.getByRole('button',{name:'Edit in chat',exact:true}).click();
  for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);
  await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();
  assert.equal(await p.locator('#business-chat-drawer').isVisible(),false);
  await held.fulfill({contentType:'application/json',body:JSON.stringify(organizationSetupFixture('organization','cafe','orgchat-old-owner-private-setup'))});held=null;await p.waitForTimeout(100);
  assert.equal(await p.locator('#business-chat-drawer').isVisible(),false);assert.equal(await p.locator('#business-chat-frame').getAttribute('src'),null);assert.equal(await p.locator('#viewing-banner').isVisible(),true);assert.deepEqual(h.errors,[]);
 }finally{if(held)await held.abort();await h.close();}
});
for(const [scenario,label] of [['valid','opens only the current setup response'],['leave','discards a late response after leaving Business'],['collection','discards a late response after changing the collection'],['late_error','suppresses an error from the previous view']])test('Business onboarding '+label,async()=>{
 const h=await pageFor(390,{business:{revision:1,profile:{name:'Owner business'},settings:{},connections:[]}}),p=h.page;let held;
 try{
  await p.route('**/api/organization/chats',r=>{assert.equal(r.request().method(),'POST');assert.deepEqual(r.request().postDataJSON(),{collection:'cafe',purpose:'organization'});held=r;});await openSettingsPage(p,'business');await p.getByRole('button',{name:'Edit in chat',exact:true}).click();
  for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);
  if(scenario==='leave'||scenario==='late_error')await p.locator('#new-chat').click();
  if(scenario==='collection'){await p.locator('#founder-nav').click();await p.locator('#founder-demo-options').getByRole('button',{name:'Warehouse',exact:true}).click();}
  if(scenario==='valid')await p.route('**/?chat=orgchat-valid-setup&drawer=1',r=>r.fulfill({contentType:'text/html',body:'<p>Fixture setup</p>'}));
  await held.fulfill({status:scenario==='late_error'?409:200,contentType:'application/json',body:JSON.stringify(scenario==='late_error'?{detail:'Old scope error'}:organizationSetupFixture('organization','cafe','orgchat-valid-setup'))});held=null;await p.waitForTimeout(100);
  assert.equal(await p.locator('#business-chat-drawer').isVisible(),scenario==='valid');assert.equal(await p.locator('#business-chat-frame').getAttribute('src'),scenario==='valid'?'/?chat=orgchat-valid-setup&drawer=1':null);assert.equal(await p.locator('#notice').textContent(),'');assert.deepEqual(h.errors,[]);
 }finally{if(held)await held.abort();await h.close();}
});
test('Founder demo selection preserves the route back to an authorized company',async()=>{
 const h=await pageFor(1600,{environments:[{id:'demo',name:'Demo company',category:'demo',collections:[{id:'cafe',name:'Café'}]},{id:'customer',name:'Customer business',category:'company'}]}),p=h.page;
 try{
  const session=await p.evaluate(()=>fetch('/api/session').then(r=>r.json()));let switched=false;
  await p.route('**/api/session/company',r=>{assert.deepEqual(r.request().postDataJSON(),{environment_id:'customer'});switched=true;return r.fulfill({contentType:'application/json',body:'{}'});});
  await p.route('**/api/session',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({...session,environment:{id:'customer',name:'Customer business'},environments:session.environments})}));
  await p.locator('#environment-toggle').click();assert.equal(await p.locator('#environment-options').getByRole('button',{name:'Café',exact:true}).count(),0);await p.locator('#environment-options').getByRole('button',{name:'Customer business',exact:true}).click();
  await p.waitForFunction(()=>document.querySelector('#workspace-person').textContent==='Customer business');assert(switched);assert.equal(await p.locator('#founder-nav').isVisible(),true);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}
});


test('Background chat refresh preserves playing media while updating another message',async()=>{
 const messages=[chat.messages[0],{role:'assistant',content:'Your audio file is ready.',artifacts:[nativeAudioFile]},{role:'assistant',content:'An earlier update.'}];
 for(const width of [1600,390]){const h=await pageFor(width,{files:[nativeAudioFile],fileContent:null,chat:{messages}}),p=h.page;
 try{
  await p.route('**/api/files/file-audio/preview',r=>r.fulfill({contentType:'audio/wav',body:wavFixture()}));
  const audio=p.locator('.artifact-card audio');await audio.waitFor();await audio.evaluate(async el=>{el.loop=true;await el.play();window.playingAudio=el;});
  await p.route('**/api/chats?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({chats:[{...chat,messages:[...messages.slice(0,2),{role:'assistant',content:'The server update arrived.'}]}]})}));
  await p.getByText('The server update arrived.',{exact:true}).waitFor({timeout:12000});
  assert.deepEqual(await p.evaluate(()=>({connected:window.playingAudio.isConnected,paused:window.playingAudio.paused,samePlayer:window.playingAudio===document.querySelector('.artifact-card audio')})),{connected:true,paused:false,samePlayer:true});
  if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`playback-refresh-${width}.png`)});
  await openSettingsPage(p,'business');assert.equal(await p.evaluate(()=>window.playingAudio.paused),true);assert.deepEqual(h.writes,[]);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}}
});
for(const scenario of ['valid','leave','return','new_chat','observer','latest','stale_error'])test('Delayed evidence preview honors current navigation and intent: '+scenario,async()=>{
 const h=await pageFor(390,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Review [first source](/?clip=delayed-first) and [second source](/?clip=delayed-second).'}]}}),p=h.page,held=new Map(),seen=new Set();
 const reply=id=>({metadata:{id,dataset:'cafe',camera:id==='delayed-first'?'First Source':'Second Source',start:0,end:8},descriptions:[]});
 try{
  await p.route('**/api/retail/clip/delayed-*',r=>{const id=new URL(r.request().url()).pathname.split('/').pop();if(!seen.has(id)){seen.add(id);held.set(id,r);}else return r.fulfill({contentType:'application/json',body:JSON.stringify(reply(id))});});
  await p.getByRole('link',{name:'first source',exact:true}).click();for(let i=0;!held.has('delayed-first')&&i<100;i++)await p.waitForTimeout(10);assert(held.has('delayed-first'));
  if(['leave','return','stale_error'].includes(scenario)){await openSettingsPage(p,'business');await p.locator('#business-view').waitFor();}
  if(scenario==='return'){
   await p.locator('#new-chat').click();assert.equal(await p.locator('#rail-chats').isVisible(),true);assert.equal(await p.locator('#chat-list').isVisible(),false);
   await p.setViewportSize({width:1600,height:1000});await p.waitForFunction(()=>document.querySelector('#rail-chats').hidden);assert.equal(await p.locator('#rail-chats').isVisible(),false);await p.setViewportSize({width:390,height:844});await p.locator('#rail-chats').waitFor();assert.equal(await p.locator('#chat-list').isVisible(),false);
   if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'mobile-chats-empty.png')});}
   await p.locator('#rail-chats').click();assert.equal(await p.locator('#rail-chats').getAttribute('aria-expanded'),'true');assert.equal(await p.locator('#chat-list').isVisible(),true);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'mobile-chats-open.png')});
   await p.locator('#chat-list').getByText('Fixture café review',{exact:true}).click();assert.equal(new URL(p.url()).searchParams.get('chat'),'fixture-chat');assert.equal(await p.locator('#chat-list').isVisible(),false);assert.equal(await p.locator('#rail-chats').getAttribute('aria-expanded'),'false');
  }
  if(scenario==='new_chat')await p.locator('#new-chat').click();
  if(scenario==='observer'){await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();}
  if(scenario==='latest'){
   await p.getByRole('link',{name:'second source',exact:true}).click();for(let i=0;!held.has('delayed-second')&&i<100;i++)await p.waitForTimeout(10);assert(held.has('delayed-second'));
   await held.get('delayed-second').fulfill({contentType:'application/json',body:JSON.stringify(reply('delayed-second'))});held.delete('delayed-second');await p.locator('#clip-dialog').waitFor();assert.equal(await p.locator('#clip-dialog-title').textContent(),'Second Source');
  }
  await held.get('delayed-first').fulfill({status:scenario==='stale_error'?404:200,contentType:'application/json',body:JSON.stringify(scenario==='stale_error'?{detail:'Expired'}:reply('delayed-first'))});held.delete('delayed-first');await p.waitForTimeout(150);
  assert.equal(await p.locator('#clip-dialog').evaluate(el=>el.open),['valid','latest'].includes(scenario));
  if(['valid','latest'].includes(scenario))assert.equal(await p.locator('#clip-dialog-title').textContent(),scenario==='valid'?'First Source':'Second Source');
  assert.equal(await p.locator('#notice').textContent(),'');assert.deepEqual(h.writes,scenario==='observer'?['/api/observer']:[]);assert.deepEqual(h.errors,[]);
 }finally{for(const r of held.values())await r.abort();await h.close();}
});

test('Saved source links resolve when matching evidence arrives in another message',async()=>{
 const messages=[chat.messages[0],{role:'assistant',content:'Review [source recording](/media/fixture-source#t=0).'},{role:'assistant',content:'Evidence is loading.'}];
 const h=await pageFor(1600,{chat:{messages}}),p=h.page;
 try{
 assert.equal(await p.getByRole('link',{name:'source recording',exact:true}).count(),0);
 await p.route('**/api/chats?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({chats:[{...chat,messages:[...messages.slice(0,2),{role:'assistant',content:'Evidence has arrived.',results:[{id:'fixture-clip',video_id:'fixture-source',dataset:'cafe',camera:'Front counter',start:0,end:8}]}]}]})}));
 await p.getByText('Evidence has arrived.',{exact:true}).waitFor({timeout:12000});
 assert.equal(await p.getByRole('link',{name:'source recording',exact:true}).count(),1,'Existing media links must resolve when their owned evidence arrives');
 await p.getByRole('link',{name:'source recording',exact:true}).click();await p.locator('#clip-dialog').waitFor();assert.equal(await p.locator('#clip-dialog-title').textContent(),'Front Counter');assert.deepEqual(h.errors,[]);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
 });

// Synthetic, labeled frame bytes: these prove UI metadata/lifecycle, not model sampling.
const inspectionFrameBodies=[['#284f66','FRAME 1 — 0:08'],['#52683c','FRAME 2 — 0:12.125'],['#674b63','FRAME 3 — 0:15.9']].map(([color,label])=>`<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="${color}"/><rect x="18" y="18" width="284" height="144" rx="8" fill="none" stroke="white"/><text x="160" y="84" text-anchor="middle" fill="white" font-family="sans-serif" font-size="18">${label}</text><text x="160" y="112" text-anchor="middle" fill="white" font-family="sans-serif" font-size="13">Synthetic inspected-frame fixture</text></svg>`);
const inspectionFrames=[8,12.125,15.9].map((time,index)=>({url:`/api/retail/inspection-previews/${'a'.repeat(32)}/${index}.jpg`,time,clip_id:'fixture-clip',sha256:require('node:crypto').createHash('sha256').update(inspectionFrameBodies[index]).digest('hex'),basis:'synthetic UI fixture',width:320,height:180}));
const inspectionSource={id:'fixture-clip',dataset:'cafe',camera:'Front counter',start:8,end:16,thumbnail_url:'/thumbnails/fixture-clip.jpg'};
const inspectionTool=(name='inspect_clip',frames=inspectionFrames)=>({id:'inspected-frames',name,status:'complete',arguments:{clip_id:'fixture-clip'},output:'Three source snapshots reviewed.',frame_previews:frames});
async function routeInspectionFrames(page){await page.route('**/api/retail/inspection-previews/**',route=>{const index=Number(new URL(route.request().url()).pathname.match(/\/(\d)\.jpg$/)?.[1]);assert(index>=0&&index<3);return route.fulfill({contentType:'image/svg+xml',body:inspectionFrameBodies[index]});});await page.route('**/thumbnails/fixture-clip.jpg',route=>route.fulfill({contentType:'image/svg+xml',body:inspectionFrameBodies[0]}));}
async function openInspectionTrail(page){const worked=page.locator('.completed-turn').last();if(await worked.count()&&!await worked.evaluate(el=>el.open))await worked.locator(':scope > summary').click();const tool=page.locator('[data-tool-id="inspected-frames"]').last();if(!await tool.evaluate(el=>el.open))await tool.locator(':scope > summary').click();return tool;}

test('Inspected frames show exact saved snapshots and times, retain DOM on refresh, and open the fenced source',async()=>{
 for(const width of [1600,390]){const messages=[chat.messages[0],{role:'assistant',content:'I reviewed these three snapshots in order.',elapsed_ms:14000,tools:[inspectionTool()],results:[inspectionSource]},{role:'assistant',content:'The remaining context is unchanged.'}],h=await pageFor(width,{chat:{messages}}),p=h.page;
 try{
  await routeInspectionFrames(p);await p.reload();const tool=await openInspectionTrail(p),strip=tool.locator('.tool-trail-inspection-frames');await strip.waitFor();const images=strip.locator('img');assert.equal(await images.count(),3);assert.deepEqual(await images.evaluateAll(els=>els.map(el=>new URL(el.src).pathname)),inspectionFrames.map(frame=>frame.url));assert.deepEqual(await images.evaluateAll(els=>els.map(el=>el.alt)),['Snapshot · 0:08 in source','Snapshot · 0:12.125 in source','Snapshot · 0:15.9 in source']);assert.equal(await tool.locator('img[src*="/thumbnails/"]').count(),0);assert.deepEqual(await strip.locator('.tool-trail-media-item > span').allTextContents(),['0:08 in source','0:12.125 in source','0:15.9 in source']);assert.equal(await strip.locator('.tool-trail-media-item > span').evaluateAll(els=>els.every(el=>el.scrollWidth<=el.clientWidth)),true,'Every precise source time remains visible');
  await p.waitForFunction(()=>[...document.querySelectorAll('.tool-trail-inspection-frames img')].every(img=>img.complete&&img.naturalWidth===320));assert.deepEqual(await images.evaluateAll(els=>els.map(el=>({fit:getComputedStyle(el).objectFit,referrer:el.referrerPolicy}))),Array(3).fill({fit:'contain',referrer:'no-referrer'}));assert.equal(await images.evaluateAll(els=>new Set(els.map(el=>Math.round(el.getBoundingClientRect().y))).size),1);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await images.first().evaluate(el=>{window.savedInspectionFrame=el;});await p.route('**/api/chats?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({chats:[{...chat,messages:[...messages.slice(0,2),{role:'assistant',content:'The other server message changed.'}]}]})}));await p.getByText('The other server message changed.',{exact:true}).waitFor({timeout:12000});assert.equal(await p.evaluate(()=>window.savedInspectionFrame.isConnected&&window.savedInspectionFrame===document.querySelector('.tool-trail-inspection-frames img')),true);
  if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`inspected-frames-${width}.png`)});}
  await strip.getByRole('button',{name:'Snapshot · 0:12.125 in source',exact:true}).click();await p.locator('#clip-dialog').waitFor();assert.equal(await p.locator('#clip-dialog-title').textContent(),'Front Counter');await p.keyboard.press('Escape');
  assert.deepEqual(h.writes,[]);
 }finally{await h.close();}}
});

test('Inspected frames from native live tool_end normalize, persist, and survive saved reload',async()=>{
 const history={messages:[...chat.messages]},h=await pageFor(390,{chat:history}),p=h.page;
 try{
  await routeInspectionFrames(p);await p.evaluate(()=>{const original=window.fetch;window.fetch=(input,options)=>{if(new URL(String(input),location.href).pathname!=='/api/chat')return original(input,options);return Promise.resolve(new Response(new ReadableStream({start(controller){window.fixtureInspectionEvents=(events,finish=false)=>{for(const event of events)controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));if(finish)controller.close();};}}),{headers:{'Content-Type':'application/x-ndjson'}}));};});
  await p.locator('#message').fill('Inspect the source snapshots.');await p.locator('#send').click();await p.waitForFunction(()=>Boolean(window.fixtureInspectionEvents));const frames=inspectionFrames.map(frame=>({...frame,label:'Ignored untrusted label',authorized:true}));
  await p.evaluate(frames=>window.fixtureInspectionEvents([{type:'tool_start',id:'inspected-frames',name:'mcp__rocoto__inspect_clip',arguments:{clip_id:'fixture-clip'}},{type:'tool_end',id:'inspected-frames',name:'mcp__rocoto__inspect_clip',success:true,output:'Three source snapshots reviewed.',frame_previews:frames}]),frames);
  const live=p.locator('[data-tool-id="inspected-frames"]');await live.waitFor();await live.locator(':scope > summary').click();assert.equal(await live.locator('.tool-trail-inspection-frames img').count(),3);assert.equal(await p.locator('#send').getAttribute('aria-label'),'Stop response');
  await p.evaluate(()=>window.fixtureInspectionEvents([{type:'delta',text:'The inspected snapshots are ready.'},{type:'done'}],true));await p.getByText('The inspected snapshots are ready.',{exact:true}).waitFor();
  const persisted=await p.evaluate(()=>Object.entries(localStorage).filter(([key])=>key.startsWith('rocoto-video-chats-v1')).flatMap(([,value])=>JSON.parse(value)).flatMap(c=>c.messages||[]).flatMap(m=>m.tools||[]).find(t=>t.id==='inspected-frames')?.frame_previews);assert.deepEqual(persisted,inspectionFrames);
  history.messages=[...chat.messages,{role:'user',content:'Inspect the source snapshots.'},{role:'assistant',content:'The inspected snapshots are ready.',tools:[inspectionTool('mcp__rocoto__inspect_clip',frames)]}];await p.reload();const saved=await openInspectionTrail(p);assert.deepEqual(await saved.locator('.tool-trail-inspection-frames img').evaluateAll(els=>els.map(el=>new URL(el.src).pathname)),inspectionFrames.map(frame=>frame.url));assert.equal(await saved.getByText('Ignored untrusted label',{exact:true}).count(),0);let held;await p.route('**/api/retail/clip/fixture-clip',r=>{held=r;});await saved.getByRole('button',{name:'Snapshot · 0:08 in source',exact:true}).click();for(let i=0;!held&&i<50;i++)await p.waitForTimeout(10);assert(held);await openSettingsPage(p,'business');await held.fulfill({contentType:'application/json',body:JSON.stringify({metadata:inspectionSource,descriptions:[]})});await p.waitForTimeout(100);assert.equal(await p.locator('#clip-dialog').evaluate(el=>el.open),false);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});

test('Inspected frames reject public and malformed metadata while preserving the source thumbnail fallback',async()=>{
 const invalid=[{...inspectionFrames[0],url:'https://public.example/frame.jpg'},{...inspectionFrames[1],url:'/api/retail/inspection-previews/'+ 'a'.repeat(32)+'/1.jpg?token=untrusted'},{...inspectionFrames[2],time:-1},{...inspectionFrames[0],sha256:'bad'}];
 const h=await pageFor(390,{chat:{messages:[chat.messages[0],{role:'assistant',content:'The source interval remains available.',tools:[inspectionTool('mcp__rocoto__inspect_clip',invalid)],results:[inspectionSource]}]}}),p=h.page,external=[];
 try{await p.route('https://public.example/**',r=>{external.push(r.request().url());return r.abort();});await routeInspectionFrames(p);await p.reload();const tool=await openInspectionTrail(p);assert.equal(await tool.locator('.tool-trail-inspection-frames').count(),0);assert.equal(await tool.locator('.tool-trail-media img').count(),1);assert.equal(new URL(await tool.locator('.tool-trail-media img').getAttribute('src'),origin).pathname,inspectionSource.thumbnail_url);assert.deepEqual(external,[]);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

test('Mobile source viewer owns hit targets above underlying evidence gallery controls',async()=>{
 const h=await pageFor(390,{chat:{messages:[chat.messages[0],{role:'assistant',content:'Review this source interval.',results:[inspectionSource]}]}}),p=h.page;
 try{
  await routeInspectionFrames(p);await p.reload();await p.locator('.evidence-preview').click();await p.locator('#clip-dialog').waitFor();
  const hit=await p.locator('.evidence-preview .play-icon').evaluate(el=>{const r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,viewer=document.querySelector('#clip-dialog'),v=viewer.getBoundingClientRect(),top=document.elementFromPoint(x,y);return {point:{x,y},overlapsViewer:x>v.left&&x<v.right&&y>v.top&&y<v.bottom,viewerOwnsHit:Boolean(top?.closest('#clip-dialog')),actualTop:top?.outerHTML.slice(0,180)};});
  assert.equal(hit.overlapsViewer,true,'The source-card control must actually overlap the open viewer');
  if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,'source-viewer-layer-390.png')});}
  assert.equal(hit.viewerOwnsHit,true,JSON.stringify(hit));
  const original=p.locator('#show-original');assert.equal(await original.evaluate(el=>{const r=el.getBoundingClientRect();return Boolean(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('#show-original'));}),true);await original.click();
  await p.locator('#account-button').click();await p.locator('#profile-menu').waitFor();assert.equal(await p.locator('#profile-menu').evaluate(el=>{const r=el.getBoundingClientRect();return Boolean(document.elementFromPoint(r.x+r.width/2,r.y+12)?.closest('#profile-menu'));}),true,'The persistent account menu remains above the source viewer');await p.keyboard.press('Escape');assert.equal(await p.locator('#clip-dialog').evaluate(el=>el.open),false);assert.deepEqual(h.writes,[]);
 }finally{await h.close();}
});

// Customize follows the supplied Codex reference; fixture provenance controls visibility.
test('Customize has a plain Your connections divider and preserves real installed tools',async()=>{
 const visible={id:'customer-docs-verification',name:'Credential flow verification',kind:'api',summary:'Customer-owned quality checks',status:'prepared',revision:1,chat_id:'customer-connector',files:[],secret_fields:[]};
 const hidden={...visible,id:'synthetic-verification',name:'Synthetic setup proof',visibility:'verification'};
 const connections=[{id:'openai-docs',provider:'openai-docs',name:'OpenAI Docs',description:'Search OpenAI documentation.',status:'connected',authentication:'anonymous',read_only:true},{id:'rocoto-core',provider:'rocoto-core',name:'Rocoto runtime core',description:'Internal runtime tools.',status:'connected',authentication:'anonymous',visibility:'internal'}];
 const h=await pageFor(1600,{connections,customConnectors:[visible,hidden],nativePlugins:[{id:'gmail',name:'Gmail',installed:true}]}),p=h.page;
 try{await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');await p.locator('#customize-installed').getByRole('button',{name:'Gmail',exact:true}).waitFor();assert.equal(await p.locator('#customize-yours').evaluate(el=>el.tagName),'H2');assert.equal(await p.locator('#customize-yours svg').count(),0);assert.equal(await p.locator('#customize-installed').getByRole('button',{name:'OpenAI Docs',exact:true}).count(),1);assert.equal(await p.locator('#customize-installed').getByRole('button',{name:'Credential flow verification',exact:true}).count(),1);assert.equal(await p.getByText('Synthetic setup proof',{exact:true}).count(),0);assert.equal(await p.getByText('Rocoto runtime core',{exact:true}).count(),0);assert.equal(await p.locator('#customize-installed .installed-label').count(),0);assert.deepEqual(h.writes,[]);}finally{await h.close();}
});

test('Customize agent cards are separate with compact filters and search at top right',async()=>{
 const templates=[['review-service','Review customer service','Find moments to improve service and help your team.','Café'],['review-queues','Review queues','See where customers wait and when queues build up.','Retail'],['review-deliveries','Review deliveries','Check arrivals, unloading and handovers.','Warehouse'],['review-safety','Review workplace safety','Review movement around work areas and equipment.','Manufacturing']].map(([id,name,description,industry],index)=>({id,name,description,industry,use_case:index<2?'Service':'Operations',mode:index%2?'scheduled':'ad_hoc',interval_seconds:3600,instructions:'Review supplied footage and cite evidence.',watch_for:['Review source evidence'],required_input:['Select footage'],required_connections:[]}));
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page;try{await p.route('**/api/agents/catalogue',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({templates})}));await p.locator('#marketplace-nav').click();if(width===390)await p.locator('#customize-agents').evaluate(el=>el.click());else await p.locator('#customize-agents').click();await p.keyboard.press('Escape');await p.locator('.agent-catalogue-grid .marketplace-item').first().waitFor();assert.equal(await p.locator('.agent-catalogue-grid .marketplace-item').count(),4);assert.equal(await p.getByLabel('Search agents',{exact:true}).isVisible(),true);const layout=await p.evaluate(()=>{const card=document.querySelector('.agent-catalogue-grid .marketplace-item'),grid=card.parentElement,search=document.querySelector('#marketplace-search-wrap').getBoundingClientRect(),title=document.querySelector('#marketplace-title').getBoundingClientRect();return{gap:parseFloat(getComputedStyle(grid).rowGap),border:getComputedStyle(card).borderTopWidth,radius:parseFloat(getComputedStyle(card).borderRadius),search:{left:search.left,right:search.right,top:search.top},title:{left:title.left,top:title.top},appearance:[...document.querySelectorAll('#marketplace-filters select')].map(el=>getComputedStyle(el).appearance),filterTextFits:[...document.querySelectorAll('#marketplace-filters select')].map(el=>{const style=getComputedStyle(el),context=document.createElement('canvas').getContext('2d');context.font=style.font;return el.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)>=context.measureText(el.selectedOptions[0].text).width;}),overflow:document.documentElement.scrollWidth>innerWidth};});assert.ok(layout.gap>=12);assert.equal(layout.border,'1px');assert.ok(layout.radius>=12);assert.equal(layout.overflow,false);assert.deepEqual(layout.appearance,['none','none','none','none']);assert.deepEqual(layout.filterTextFits,[true,true,true,true],'Every filter label remains readable');if(width===1600)assert.ok(layout.search.left>layout.title.left+200);assert.ok(layout.search.right<=width);await p.getByLabel('Filter agents by industry').selectOption('Café');assert.equal(await p.locator('.agent-catalogue-grid .marketplace-item').count(),1);await p.getByLabel('Filter agents by industry').selectOption('');if(process.env.ROCOTO_UI_PROOF_DIR){fs.mkdirSync(process.env.ROCOTO_UI_PROOF_DIR,{recursive:true});await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`customize-agents-${width}.png`)});}await p.getByLabel('Search agents',{exact:true}).fill('deliveries');assert.equal(await p.locator('.agent-catalogue-grid .marketplace-item').count(),1);assert.deepEqual(h.writes,[]);}finally{await h.close();}}
});


test('Turn completion and question attention badges clear on opening, including an auto-skipped question',async()=>{
 const h=await pageFor(),p=h.page;try{
  const q={id:'attention-question',root_chat_id:'fixture-chat',actor_chat_id:'fixture-chat',status:'pending',blocking:false,questions:[{id:'scope',header:'Scope',question:'Which location?',options:[],is_other:true}]};
  await p.route('**/api/chat',r=>r.fulfill({contentType:'application/x-ndjson',body:[{type:'user_input_request',request:q},{type:'user_input_resolved',request:{...q,status:'timed_out'}},{type:'delta',text:'Ready for the next step.'},{type:'done',status:'completed'}].map(x=>JSON.stringify(x)).join('\n')+'\n'}));
  await p.locator('#message').fill('Ask me about the location.');await p.locator('#send').click();
  await p.locator('.chat-question-dot').waitFor();assert.equal(await p.locator('.chat-completion-dot').count(),0);await p.locator('#chat-list .chat-link').first().click();assert.equal(await p.locator('.chat-question-dot').count(),0);
  await p.route('**/api/chat',r=>r.fulfill({contentType:'application/x-ndjson',body:[{type:'delta',text:'Finished.'},{type:'done',status:'completed'}].map(x=>JSON.stringify(x)).join('\n')+'\n'}));
  await p.locator('#message').fill('Continue.');await p.locator('#send').click();await p.locator('.chat-completion-dot').waitFor();assert.equal(await p.locator('.chat-completion-dot').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(36, 99, 189)');await p.locator('#chat-list .chat-link').first().click();assert.equal(await p.locator('.chat-completion-dot').count(),0);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}
});
test('Sending anchors the latest message above the response and new chats omit the subagent aggregate',async()=>{
 for(const width of [1600,390]){const h=await pageFor(width),p=h.page;try{
  let held;await p.route('**/api/chat',r=>{held=r;});await p.locator('#message').fill('Review this next turn.');await p.locator('#send').click();
  await p.waitForFunction(()=>document.querySelectorAll('.message-user').length===2);const top=await p.locator('.message-user').last().evaluate(el=>el.getBoundingClientRect().top-document.querySelector('#conversation').getBoundingClientRect().top);assert(Math.abs(top-72)<3,`New message starts near top: ${top}`);
  await held.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'delta',text:'A clear response.'})+'\n'+JSON.stringify({type:'done',status:'completed'})+'\n'});await p.locator('.assistant-text').last().filter({hasText:'A clear response.'}).waitFor();const after=await p.locator('.message-user').last().evaluate(el=>el.getBoundingClientRect().top-document.querySelector('#conversation').getBoundingClientRect().top);assert(Math.abs(after-top)<3,`Streaming retains eye position: ${top} -> ${after}`);
  await p.locator('#new-chat').click();assert.equal(await p.locator('#chat-subagents').isVisible(),false);if(width===1600)for(const toggle of await p.locator('.chat-section-toggle').all()){const gap=await toggle.evaluate(el=>{const span=el.querySelector('span'),svg=el.lastElementChild;return svg.getBoundingClientRect().left-span.getBoundingClientRect().right});assert(gap>=0&&gap<12,'Chevron immediately follows label');}
  assert.deepEqual(h.errors,[]);if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`chat-navigation-${width}.png`)});
 }finally{await h.close();}}
});

test('Question panel single choice sends immediately while multiple choice requires Send',async()=>{
 for(const width of [1600,390]) for(const multiple of [false,true]){
  const question={...nativeQuestion,expires_at:new Date(Date.now()+30000).toISOString(),questions:[{...nativeQuestion.questions[0],multiple}]};
  const h=await pageFor(width,{chat:{...questionChat(question),running:true},questionReply:{action:'submit',answers:{scope:multiple?['Counter service','Seating area']:['Counter service']}}}),p=h.page;
  try{
   await p.locator('#native-question-dialog').waitFor();await p.locator('#message').fill('Keep this composer draft.');
   assert.equal(await p.locator('.credential-waiting .current-activity').textContent(),'Waiting for your answer');
   assert.equal(await p.locator('.credential-waiting').evaluate(el=>getComputedStyle(el).animationName),'none');
   const row=p.getByRole(multiple?'checkbox':'radio',{name:/^Counter service/});await row.hover();
   const bounds=await p.locator('#native-question-dialog').evaluate(el=>{const r=el.getBoundingClientRect(),c=document.querySelector('.composer').getBoundingClientRect(),x=el.querySelector('.question-box-close').getBoundingClientRect();return{above:r.bottom<=c.top,closeFits:x.right<=r.right,overflow:document.documentElement.scrollWidth>innerWidth};});
   assert(bounds.above&&bounds.closeFits&&!bounds.overflow);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`question-options-${multiple?'multiple':'single'}-${width}.png`)});
   await row.click();if(multiple){await p.getByRole('checkbox',{name:/^Seating area/}).click();assert.deepEqual(h.writes,[]);await p.getByRole('button',{name:'Send',exact:true}).click();}
   await p.getByText('Answers sent',{exact:true}).waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep this composer draft.');assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);
  }finally{await h.close();}
 }
});
test('Question deadline sends only timeout and keeps the completed card readable',async()=>{
 const question={...nativeQuestion,expires_at:new Date(Date.now()+1500).toISOString(),questions:[nativeQuestion.questions[0]]};
 const h=await pageFor(390,{chat:questionChat(question),questionReply:{action:'timeout'}}),p=h.page;
 try{await p.getByText('Question timed out',{exact:true}).waitFor();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);assert.equal(await p.locator('.native-question-card button').count(),0);assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1']);}finally{await h.close();}
});
test('Closing a native question reports Skip and a late answer reply stays in its original session',async()=>{
 const h=await pageFor(1600,{chat:questionChat(),questionReply:{action:'skip'},questionDelay:900}),p=h.page;
 try{await p.locator('#native-question-dialog').waitFor();await p.getByRole('button',{name:'Skip question',exact:true}).click();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor();await p.waitForTimeout(1000);assert.equal(await p.locator('#native-question-dialog').isVisible(),false);assert.equal(await p.getByText('Question skipped',{exact:true}).count(),0);assert.deepEqual(h.writes,['/api/chats/fixture-chat/questions/question-opaque-1','/api/observer']);}finally{await h.close();}
});


test('Organization chat controls open canonical shared conversations and Messaging opens the same member modal',async()=>{
 const h=await pageFor(),p=h.page;try{
  const org={id:'orgchat-fixture',title:'Organization',organization_purpose:'organization',kind:'organization_chat',environment_id:'demo',owner_principal_id:'organization-fixture',dataset:'cafe',read_only:false,messages:[],running:false};
  await p.route('**/api/organization/chats',r=>r.fulfill({contentType:'application/json',body:JSON.stringify(org)}));
  await p.locator('.organization-chat-link').filter({hasText:'Organization'}).click();await p.waitForFunction(()=>new URL(location.href).searchParams.get('chat')==='orgchat-fixture');assert.equal(await p.locator('#chat-title').innerText(),'Organization');assert.equal(await p.locator('#welcome h1').innerText(),'Organization');assert.equal(await p.locator('.organization-chat-link').count(),3);assert.equal(await p.locator('#chat-list .chat-link').filter({hasText:'Organization'}).count(),0);
  await p.route('**/api/messaging?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({member_id:'owner',members:[{id:'owner',display_name:'Fixture Owner',is_self:true,role:'owner'}],contacts:[],readiness:{configured:true,ready:false,otp_ready:false,reason:'auth_template_required',sender_phone:'+19789589459'},setup_chat_id:'setup-fixture'})}));
  await p.goto(origin+'/?settings=messaging');await p.locator('#messaging-view').waitFor();await p.locator('#messaging-view').getByRole('button',{name:'Add a number'}).click();await p.locator('dialog.messaging-profile-dialog').waitFor();assert.equal(await p.locator('dialog.messaging-profile-dialog').getByRole('radio',{name:/WhatsApp/}).isChecked(),true);assert.equal(await p.getByRole('button',{name:'Send security code'}).isDisabled(),true);assert.match(await p.locator('dialog.messaging-profile-dialog').innerText(),/Security code delivery is being prepared/);assert.deepEqual(h.errors,[]);
 }finally{await h.close();}
});

test('Server-admitted organization questions and private entry follow member write access',async()=>{
 for(const secure of [false,true])for(const readOnly of [false,true]){
  const h=await pageFor(1600),p=h.page,sent=[],id='orgchat-question-fixture';
  const pending=secure?{...structuredClone(credentialRequest),expires_at:new Date(Date.now()+30000).toISOString()}:{...structuredClone(nativeQuestion),root_chat_id:id,actor_chat_id:id,expires_at:new Date(Date.now()+30000).toISOString()};
  const org={...chat,id,title:'Organization',kind:'organization_chat',organization_purpose:'organization',environment_id:'demo',owner_principal_id:'organization-fixture',read_only:readOnly,messages:[chat.messages[0],{role:'assistant',content:'Choose the next step.',[secure?'credential_requests':'native_questions']:[pending]}]};
  try{
   await p.route('**/api/organization/chats',r=>r.fulfill({contentType:'application/json',body:JSON.stringify(org)}));
   await p.route(`**/api/chats/${id}/${secure?'credential-requests':'questions'}/*`,r=>{sent.push(r.request().postDataJSON());pending.status='skipped';return r.fulfill({contentType:'application/json',body:JSON.stringify({request:pending})});});
   await p.locator('.organization-chat-link').filter({hasText:'Organization'}).click();await p.locator('#chat-title').filter({hasText:'Organization'}).waitFor();
   const panel=p.locator(secure?'#credential-request-dialog':'#native-question-dialog'),card=p.locator(secure?'.credential-request-card':'.native-question-card');
   if(readOnly){assert.equal(await panel.isVisible(),false);assert.equal(await card.locator('button').isDisabled(),true);assert.deepEqual(sent,[]);}
   else{await panel.waitFor();await panel.getByRole('button',{name:'Skip',exact:true}).click();await panel.waitFor({state:'hidden'});assert.deepEqual(sent,[secure?{action:'skip',reason:'Skipped by user.'}:{action:'skip'}]);}
  }finally{await h.close();}
 }
});

test('Question and credential records are compact muted trail rows beside small reopen actions',async()=>{
 for(const width of [1600,390])for(const secure of [false,true]){
  const h=await pageFor(width,{chat:secure?credentialChat():questionChat()}),p=h.page;
  try{
   const trail=p.locator(secure?'.credential-request-card':'.native-question-card');await trail.waitFor();
   const geometry=await trail.evaluate(el=>{const css=getComputedStyle(el),r=el.getBoundingClientRect(),action=el.querySelector('button');return{height:r.height,padding:css.padding,border:css.borderTopWidth,background:css.backgroundColor,action:action.textContent,overflow:document.documentElement.scrollWidth>innerWidth};});
   assert.equal(geometry.padding,'0px');assert.equal(geometry.border,'0px');assert.equal(geometry.background,'rgba(0, 0, 0, 0)');assert(geometry.height<=64,JSON.stringify(geometry));assert.equal(geometry.action,'Open');assert.equal(geometry.overflow,false);
   if(secure){assert.equal(await p.locator('#credential-request-dialog .question-box-deadline').count(),0);assert.equal(await p.locator('#credential-request-dialog .question-box-kind .icon').count(),0);assert.equal(await p.locator('#credential-request-dialog .question-box-lock .icon').count(),1);}
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`compact-${secure?'secure':'question'}-${width}.png`)});
   assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
 }
});

test('Organization connector controls follow configure authority while shared details and downloads stay readable',async()=>{
 const target={origin:'https://orders.example.com',authentication:{kind:'bearer',credential:'ERP_API_TOKEN'},operations:[{id:'orders',method:'GET',path:'/v1/orders',query_schema:{type:'object',properties:{},additionalProperties:false}}]};
 for(const authority of [{role:'operator',organization_shared:true,can_configure:false,writable:false},{role:'operator',organization_shared:true,writable:false},{role:'admin',organization_shared:true,can_configure:true,writable:true},{role:'operator',organization_shared:false,can_configure:false,writable:true}]){
  const item={...customConnector,organization_shared:authority.organization_shared,...(Object.hasOwn(authority,'can_configure')?{can_configure:authority.can_configure}:{}),secret_fields:customConnector.secret_fields.map(field=>({...field,configured:true})),execution_setup:{runtime_available:true,active_status:'active',active_generation:0,target_generation:1,active_revision:1,target,last_test:{id:'previous-test',status:'passed',revision:2},tools:[{name:'find_orders',description:'Find order references.'}],proposal:{id:'next-target',current:true,reason:'Review access to the order service.',target}}};
  const h=await pageFor(1600,{role:authority.role,customConnectors:[item]}),p=h.page;
  try{
   await p.goto(origin+'/?customize=1&tab=connections&item=custom-erp');await p.getByLabel('ERP API token',{exact:true}).waitFor();
   assert.equal(await p.getByRole('heading',{name:'Warehouse ERP',exact:true}).isVisible(),true);
   assert.equal(await p.getByRole('link',{name:'Download connector',exact:true}).getAttribute('href'),origin+'/api/connections/custom/custom-erp/package');
   assert.equal(await p.getByLabel('ERP API token',{exact:true}).isDisabled(),!authority.writable);
   for(const label of ['Save ERP API token','Remove ERP API token','Review access','Run checks','Activate connection','Remove access'])assert.equal(await p.getByRole('button',{name:label,exact:true}).isDisabled(),!authority.writable,label);
   if(!authority.writable){
    await p.locator('.custom-secret-field').evaluate(form=>{form.querySelector('input').value='fixture-secret-value';form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));});
    for(const label of ['Remove ERP API token','Review access','Run checks','Activate connection','Remove access'])await p.getByRole('button',{name:label,exact:true}).evaluate(button=>button.dispatchEvent(new MouseEvent('click',{bubbles:true})));
    assert.equal(await p.getByRole('dialog',{name:'Review connection access',exact:true}).isVisible(),false);assert.deepEqual(h.writes,[]);
   }else{
    await p.getByLabel('ERP API token',{exact:true}).fill('fixture-secret-value');await p.getByRole('button',{name:'Save ERP API token',exact:true}).click();await p.getByText('Credential saved privately.',{exact:true}).waitFor();
    await p.getByRole('button',{name:'Review access',exact:true}).click();await p.getByRole('button',{name:'Approve access',exact:true}).click();await p.getByRole('dialog',{name:'Review connection access',exact:true}).waitFor({state:'hidden'});
    await p.getByRole('button',{name:'Run checks',exact:true}).click();await p.waitForFunction(()=>document.querySelector('.custom-execution')?.textContent.includes('Connection checks passed.'));
    await p.getByRole('button',{name:'Activate connection',exact:true}).click();await p.getByRole('button',{name:'Activate connection',exact:true}).waitFor({state:'hidden'});
    await p.getByRole('button',{name:'Remove access',exact:true}).click();await p.getByText('Access removed',{exact:true}).waitFor();
    assert.deepEqual(h.writes,['/api/connections/custom/custom-erp/secrets',...['target','test','activate','revoke'].map(action=>'/api/connections/custom/custom-erp/execution/'+action)]);
   }
  }finally{await h.close();}
 }
});

test('Questions arriving in a hidden tab open when the conversation becomes visible',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width),p=h.page;
  try{
   await p.waitForLoadState('networkidle');
   await p.evaluate(()=>{const original=window.fetch;window.fetch=(input,options)=>new URL(String(input),location.href).pathname!=='/api/chat'?original(input,options):Promise.resolve(new Response(new ReadableStream({start(controller){window.fixtureQuestionEvent=event=>controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));window.fixtureQuestionFinish=()=>controller.close();}}),{headers:{'Content-Type':'application/x-ndjson'}}));});
   await p.locator('#message').fill('Ask me which area to inspect.');await p.locator('#send').click();await p.waitForFunction(()=>typeof fixtureQuestionEvent==='function');
   await p.evaluate(request=>{window.fixtureTabVisible=false;Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>fixtureTabVisible?'visible':'hidden'});fixtureQuestionEvent({type:'user_input_request',request});},{...nativeQuestion,expires_at:new Date(Date.now()+30000).toISOString()});
   await p.locator('.native-question-card').waitFor();assert.equal(await p.locator('#native-question-dialog').isVisible(),false);
   await p.evaluate(()=>{fixtureTabVisible=true;document.dispatchEvent(new Event('visibilitychange'));});
   await p.locator('#native-question-dialog').waitFor({timeout:1200});assert.equal(await p.getByRole('radio',{name:/^Counter service/}).isEnabled(),true);
   await proofShot(p,`returned-question-${width}`);
   assert.deepEqual(h.writes,[]);
   await p.evaluate(()=>{fixtureQuestionEvent({type:'done',status:'completed'});fixtureQuestionFinish();});
  }finally{await h.close();}
 }
});
test('Secure entry arriving behind a dialog opens after its dismissal in the same conversation',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width),p=h.page;
  try{
   await p.waitForLoadState('networkidle');
   await p.evaluate(()=>{const original=window.fetch;window.fetch=(input,options)=>new URL(String(input),location.href).pathname!=='/api/chat'?original(input,options):Promise.resolve(new Response(new ReadableStream({start(controller){window.fixtureQuestionEvent=event=>controller.enqueue(new TextEncoder().encode(JSON.stringify(event)+'\n'));window.fixtureQuestionFinish=()=>controller.close();}}),{headers:{'Content-Type':'application/x-ndjson'}}));});
   await p.locator('#message').fill('Prepare the connection.');await p.locator('#send').click();await p.waitForFunction(()=>typeof fixtureQuestionEvent==='function');
   await p.evaluate(request=>{document.querySelector('#gallery-dialog').showModal();fixtureQuestionEvent({type:'credential_request',request});},{...credentialRequest,expires_at:new Date(Date.now()+30000).toISOString()});
   await p.locator('.credential-request-card').waitFor();assert.equal(await p.locator('#credential-request-dialog').isVisible(),false);
   await p.evaluate(()=>document.querySelector('#gallery-dialog').close());
   await p.locator('#credential-request-dialog').waitFor({timeout:1200});assert.equal(await p.locator('[data-credential-name]').first().getAttribute('type'),'password');
   await proofShot(p,`resumed-secure-${width}`);
   assert.deepEqual(h.writes,[]);
   await p.evaluate(()=>{fixtureQuestionEvent({type:'done',status:'completed'});fixtureQuestionFinish();});
  }finally{await h.close();}
 }
});

function businessLocationFixture() {
 return {schema_version:1,collection:'cafe',revision:7,configured:true,can_manage:true,can_manage_kinds:['locations','functions','rooms','cameras','members'],locations:[{id:'north',name:'North café',address:'10 Market Street'},{id:'south',name:'South café',address:'22 High Street'}],functions:[{id:'cooking',name:'Cooking'}],rooms:[{id:'kitchen',name:'Kitchen',location_id:'north',function_ids:['cooking']}],cameras:[],members:[{member_id:'owner',display_name:'Workspace owner',account_role:'owner',role:'organization_admin',location_ids:['north','south'],function_ids:[]}],effective:{location_ids:['north','south'],can_query:true}};
}
async function installBusinessLocationFixture(page,options={}) {
 const state={graph:businessLocationFixture(),selected:null,selectionRevision:2,writes:[],chatReads:0,hold:null,conflict:false};
 await page.route('**/api/retail/scope**',async route=>{
  const req=route.request();if(req.method()==='POST'){const body=req.postDataJSON();assert.equal(req.headers()['x-rocoto-page'],'1');assert.equal(body.collection,'cafe');assert.equal(body.expected_revision,state.graph.revision);state.writes.push({method:'POST',path:new URL(req.url()).pathname,body});state.graph.revision++;if(req.url().endsWith('/locations'))state.graph.locations.push({...body.value,id:'host-east'});}
  return route.fulfill({contentType:'application/json',body:JSON.stringify(state.graph)});
 });
 await page.route('**/api/retail/chats/*/scope',async route=>{
  const req=route.request();if(req.method()==='PUT'){const body=req.postDataJSON();assert.equal(req.headers()['x-rocoto-page'],'1');state.writes.push({method:'PUT',path:new URL(req.url()).pathname,body});if(state.conflict){state.graph.revision++;state.conflict=false;return route.fulfill({status:409,contentType:'application/json',body:'{"detail":"scope_revision_conflict"}'});}assert.equal(body.expected_revision,state.graph.revision);assert.equal(body.expected_selection_revision,state.selectionRevision);state.selected=body.location_ids;state.selectionRevision++;}
  else{state.chatReads++;if(options.hold&&state.chatReads>1){state.hold=route;return;}}
  return route.fulfill({contentType:'application/json',body:JSON.stringify({collection:'cafe',configured:true,can_query:state.graph.effective.can_query,revision:state.graph.revision,selection_revision:state.selectionRevision,selected_location_ids:state.selected,location_ids:state.selected===null?state.graph.locations.map(row=>row.id):state.selected.filter(id=>state.graph.locations.some(row=>row.id===id))})});
 });
 await page.reload();await page.getByRole('button',{name:'Chat locations: All locations',exact:true}).waitFor();return state;
}
test('Business location settings preserve the business profile and save scoped changes with its revision',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width,{business:{revision:4,profile:{name:'Fixture Cafe',location:'Madrid',type:'Café',description:'A neighborhood café.'},connections:[]}}),p=h.page;
  try{
   const fixture=await installBusinessLocationFixture(p);await p.locator('#message').fill('Keep my existing chat draft.');await openSettingsPage(p,'business');await p.getByRole('heading',{name:'Locations and access',exact:true}).waitFor();assert.equal(await p.locator('.business-profile').count(),1);
   await p.getByRole('button',{name:'Add location',exact:true}).click();await p.getByLabel('Name',{exact:true}).fill('East café');await p.getByLabel('Address',{exact:true}).fill('3 River Road');await p.getByRole('button',{name:'Save changes',exact:true}).click();await p.getByRole('heading',{name:'East café',exact:true}).waitFor();
   assert.deepEqual(fixture.writes,[{method:'POST',path:'/api/retail/scope/locations',body:{collection:'cafe',expected_revision:7,value:{name:'East café',address:'3 River Road'}}}]);assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`business-location-settings-${width}.png`),fullPage:true});
   await p.goto(origin+'/?chat=fixture-chat');await p.locator('#message').waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep my existing chat draft.');assert.deepEqual(h.errors,[]);
  }finally{await h.close();}
 }
});
test('Chat location selection persists all one and multiple locations while its business and draft stay fixed',async()=>{
 for(const width of [1600,390]){
  const h=await pageFor(width),p=h.page;
  try{
   const fixture=await installBusinessLocationFixture(p);await p.locator('#message').fill('Keep this question while choosing locations.');
   await p.getByRole('button',{name:'Chat locations: All locations',exact:true}).click();await p.getByLabel('All permitted locations',{exact:true}).uncheck();await p.getByLabel('North café',{exact:true}).check();
   if(process.env.ROCOTO_UI_PROOF_DIR)await p.screenshot({path:path.join(process.env.ROCOTO_UI_PROOF_DIR,`chat-location-choice-${width}.png`)});
   await p.getByRole('button',{name:'Apply locations',exact:true}).click();await p.getByRole('button',{name:'Chat locations: North café',exact:true}).waitFor();assert.equal(await p.locator('#message').inputValue(),'Keep this question while choosing locations.');
   await p.reload();await p.getByRole('button',{name:'Chat locations: North café',exact:true}).waitFor();await p.getByRole('button',{name:'Chat locations: North café',exact:true}).click();await p.getByLabel('South café',{exact:true}).check();await p.getByRole('button',{name:'Apply locations',exact:true}).click();await p.getByRole('button',{name:'Chat locations: 2 locations',exact:true}).waitFor();
   await p.getByRole('button',{name:'Chat locations: 2 locations',exact:true}).click();await p.getByLabel('All permitted locations',{exact:true}).check();await p.getByRole('button',{name:'Apply locations',exact:true}).click();await p.getByRole('button',{name:'Chat locations: All locations',exact:true}).waitFor();
   assert.deepEqual(fixture.writes.map(row=>row.body),[{location_ids:['north'],expected_revision:7,expected_selection_revision:2},{location_ids:['north','south'],expected_revision:7,expected_selection_revision:3},{location_ids:null,expected_revision:7,expected_selection_revision:4}]);assert(fixture.writes.every(row=>row.path==='/api/retail/chats/fixture-chat/scope'));assert.equal(await p.locator('#dataset').inputValue(),'cafe');assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(h.errors,[]);
  }finally{await h.close();}
 }
});
test('Chat location selection refreshes after conflicts and discards late reads after navigation',async()=>{
 const h=await pageFor(),p=h.page;
 try{
  const fixture=await installBusinessLocationFixture(p);fixture.conflict=true;await p.getByRole('button',{name:'Chat locations: All locations',exact:true}).click();await p.getByLabel('South café',{exact:true}).uncheck();await p.getByRole('button',{name:'Apply locations',exact:true}).click();await p.getByText('Locations changed. Reopen the editor to review the latest access.',{exact:true}).waitFor();assert.equal(await p.getByLabel('North café',{exact:true}).isChecked(),true);await p.keyboard.press('Escape');
  await p.getByRole('button',{name:'Chat locations: All locations',exact:true}).click();await p.getByLabel('South café',{exact:true}).uncheck();await p.getByRole('button',{name:'Apply locations',exact:true}).click();await p.getByRole('button',{name:'Chat locations: North café',exact:true}).waitFor();assert.equal(fixture.writes.at(-1).body.expected_revision,8);
  const delayed=await installBusinessLocationFixture(p,{hold:true});await p.getByRole('button',{name:'Chat locations: All locations',exact:true}).click();for(let n=0;n<50&&!delayed.hold;n++)await p.waitForTimeout(10);assert(delayed.hold);await p.keyboard.press('Escape');await p.locator('#new-chat').click();await delayed.hold.fulfill({contentType:'application/json',body:JSON.stringify({collection:'cafe',configured:true,can_query:true,revision:7,selection_revision:2,selected_location_ids:['south'],location_ids:['south']})});await p.waitForTimeout(100);assert.equal(await p.locator('#chat-location-scope').isVisible(),false);assert.equal(await p.locator('.chat-location-popover:popover-open').count(),0);assert.deepEqual(delayed.writes,[]);
 }finally{await h.close();}
});


test('Retail camera uploads preserve bounded replay and private link state through the extracted component',async()=>{
 for(const width of [1600,390]){const h=await linkHarness(width,{entry:'cameras',loseReply:true}),p=h.page;
  try{
   await h.open();await p.locator('#source-link-submit:enabled').waitFor();await p.locator('#source-link-input').fill('https://www.dropbox.com/scl/fi/fixture/recording.mp4?dl=1');await p.locator('#source-link-submit').click();await p.locator('#source-link-list').getByText('Downloading',{exact:true}).waitFor();
   const posts=h.linkCalls.filter(row=>row.method==='POST');assert.equal(posts.length,2);assert.deepEqual(posts[0].body,posts[1].body);assert.equal(await p.locator('#source-link-input').inputValue(),'');
   await p.locator('#upload-choose:enabled').waitFor();await p.locator('#upload-dropzone').evaluate(element=>{const dataTransfer=new DataTransfer();dataTransfer.items.add(new File(['abcdefghijkl'],'Dropped recording.mp4',{type:'video/mp4'}));element.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer}));});
   await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal([...h.chunks.values()][0].length,3);assert(!await p.evaluate(()=>JSON.stringify(localStorage).includes('dropbox.com')));
   const box=await p.locator('#source-upload-dialog').boundingBox();assert(box.x>=0&&box.x+box.width<=width);await p.keyboard.press('Escape');assert.equal(await p.evaluate(()=>document.activeElement.id),'camera-upload');
   await h.open();await p.locator('#upload-progress').getByText('Waiting for preparation',{exact:true}).waitFor();assert.equal(h.rows.size,1);assert.equal(await p.locator('#source-link-input').inputValue(),'');
  }finally{await h.close();}
 }
});

test('Wake signing secrets are revealed once, copied privately and cleared with the conversation',async()=>{
 for(const {width,kind} of [1600,390].flatMap(width=>['hook','endpoint'].map(kind=>({width,kind})))){const h=await pageFor(width),p=h.page;let revealed=false,calls=0;try{
  await p.context().grantPermissions(['clipboard-read','clipboard-write']);
  await p.route('**/api/wakes?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({[kind==='endpoint'?'endpoints':'sources']:[{id:'hook-fixture',kind,label:'Order received',status:'active',secret_available:!revealed,url:'https://example.invalid/events'}],daily_limits:{turn:{used:2,limit:48}}})}));
  await p.route(`**/api/wakes/${kind==='endpoint'?'endpoints':'hooks'}/hook-fixture/secret`,route=>{assert.equal(route.request().method(),'POST');assert.equal(route.request().headers()['x-rocoto-page'],'1');calls++;revealed=true;return route.fulfill({contentType:'application/json',headers:{'cache-control':'no-store'},body:JSON.stringify({secret:'fixture-private-signing-secret'})});});
  await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'Wakes',exact:true}).click();
  const dialog=p.getByRole('dialog',{name:'Wakes',exact:true});await dialog.waitFor();await dialog.getByRole('button',{name:'Show signing secret'}).waitFor();await proofShot(p,`wake-controls-${kind}-${width}`);await dialog.getByRole('button',{name:'Show signing secret'}).click();
  await dialog.getByLabel('Signing secret').waitFor();assert.match(await dialog.locator('[role=status]').textContent(),new RegExp(kind==='endpoint'?'receiver':'sender'));assert.equal(await dialog.getByLabel('Signing secret').inputValue(),'fixture-private-signing-secret');
  await dialog.getByRole('button',{name:'Copy signing secret',exact:true}).click();assert.equal(await p.evaluate(()=>navigator.clipboard.readText()),'fixture-private-signing-secret');
  const box=await dialog.boundingBox();assert(box.x>=0&&box.x+box.width<=width);assert.equal(calls,1);
  assert.equal(await p.evaluate(()=>JSON.stringify({...localStorage}).includes('fixture-private-signing-secret')),false);
  await dialog.getByRole('button',{name:'Close wakes',exact:true}).click();assert.equal(await p.getByLabel('Signing secret').count(),0);
  await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'Wakes',exact:true}).click();await dialog.waitFor();assert.equal(await dialog.getByRole('button',{name:'Show signing secret'}).count(),0);assert.equal(calls,1);
 }finally{await h.close();}}
});

test('A late wake secret is discarded after navigation and observers retain read-only controls',async()=>{
 const h=await pageFor(390),p=h.page;let release,posted=false;try{
  await p.route('**/api/wakes?*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({sources:[{id:'late-hook',kind:'hook',label:'Late order',status:'active',secret_available:true}],daily_limits:{}})}));
  await p.route('**/api/wakes/hooks/late-hook/secret',route=>{posted=true;return new Promise(resolve=>{release=()=>resolve(route.fulfill({contentType:'application/json',body:JSON.stringify({secret:'late-private-value'})}).catch(()=>{}));});});
  await p.locator('#chat-context-toggle').click();await p.getByRole('button',{name:'Wakes',exact:true}).click();await p.getByRole('button',{name:'Show signing secret'}).click();await p.waitForFunction(()=>document.querySelector('[aria-label="Wakes"] [role="status"]')?.textContent.includes('Opening'));assert(posted);
  await p.keyboard.press('Escape');await p.locator('#new-chat').click();release();await p.waitForTimeout(100);assert.equal(await p.getByLabel('Signing secret').count(),0);assert.equal(await p.evaluate(()=>document.body.textContent.includes('late-private-value')||JSON.stringify({...localStorage}).includes('late-private-value')),false);
  await p.goto(origin+'/?chat=fixture-chat');await p.locator('#messages .message').first().waitFor();await p.locator('#account-button').click();await p.locator('#observer-select').selectOption(JSON.stringify(['demo','customer']));await p.locator('#viewing-banner').waitFor({state:'visible'});await p.goto(origin+'/?chat=fixture-chat');await p.locator('#messages .message').first().waitFor();await p.locator('#chat-context-toggle').click();assert.equal(await p.getByRole('button',{name:'Wakes',exact:true}).count(),0);
 }finally{release?.();await h.close();}
});

test('Standing agent grants show a daily allowance while legacy grants keep their total-run limit',async()=>{
 for(const kind of ['standing','once']){const agent={...recurringAgent,status:'paused',max_runs:3,runs_started:25,grant:{kind},execution:{configurable:true,ready:true}},h=await pageFor(390,{agents:[agent]}),p=h.page;try{
  await p.locator('#agents-nav').click();await p.getByRole('button',{name:'About Daily service review',exact:true}).click();const dialog=p.locator('#agent-info-dialog');await dialog.waitFor();
  if(kind==='standing'){assert.equal(await dialog.getByLabel('Runs / day',{exact:true}).inputValue(),'3');assert.equal(await dialog.getByText('3 / day',{exact:true}).count(),1);assert.equal(await dialog.getByRole('button',{name:'Activate',exact:true}).isEnabled(),true);}
  else{assert.equal(await dialog.getByLabel('Run limit',{exact:true}).inputValue(),'3');assert.equal(await dialog.getByText('25 of 3 started',{exact:true}).count(),1);assert.equal(await dialog.getByRole('button',{name:'Activate',exact:true}).count(),0);}
 }finally{await h.close();}}
});

test('Retail agent details retain watch criteria, credit fields and exact input selection when frequency changes',async()=>{
 const agent={...recurringAgent,agent_kind:'tracker',input_kind:'tracker_events',profile_ids:['profile-counter'],event_types:['queue'],watch_for:['Waiting customers'],watch_plan:{families:[{observable:'Counter queue',verification_question:'Are customers waiting?',queries:['customers waiting'],score_threshold:.7,threshold_basis:'Reviewed sample'}]},fresh_inspections_per_run:1,credit_ceiling_usd:2,last_screening:{screened_window_count:9,candidate_window_count:2,created_at:'2026-10-11T12:00:00Z',meaning:'Queue candidates saved.'}};
 const h=await pageFor(390,{agents:[agent],agentUpdates:true}),p=h.page;try{
  await p.locator('#agents-nav').click();await p.getByRole('button',{name:'About Daily service review',exact:true}).click();const dialog=p.locator('#agent-info-dialog');await dialog.waitFor();
  await dialog.getByText('Are customers waiting?',{exact:true}).waitFor();assert.equal(await dialog.getByText('Are customers waiting?',{exact:true}).count(),1);assert.equal(await dialog.getByText('Latest footage check',{exact:true}).count(),1);assert.equal(await dialog.getByText('1 Cosmos inspection per run',{exact:true}).count(),1);assert.equal(await p.locator('#agent-fresh-inspections').count(),1);
  await dialog.getByLabel('Run every',{exact:true}).fill('2');const saved=p.waitForResponse(response=>response.url().endsWith('/update')&&response.request().method()==='POST');await dialog.getByRole('button',{name:'Save frequency',exact:true}).click();await saved;
  const update=h.payloads.find(item=>item.path.endsWith('/update'))?.body;assert(update);for(const key of ['agent_kind','input_kind','profile_ids','event_types','watch_plan','fresh_inspections_per_run'])assert.deepEqual(update[key],agent[key],key);
 }finally{await h.close();}
});
