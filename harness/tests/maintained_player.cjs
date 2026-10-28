const assert=require('node:assert/strict');
require('../verticals/retail/web/control-room/maintained-player.js');
const M=global.MaintainedPlayer;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const artifact={source_asset_id:'file',mapping_id:'map',source_sha256:'a'.repeat(64),source_window:{start_s:3,end_s:7},stream_window:{start_s:10,end_s:14},media_url:'/media',geometry_binding:{source_sha256:'a'.repeat(64),tracker_namespace:'run'}};
const boot=M.bootstrap({schema:'rocoto.player-bootstrap.v1',read_authority:'maintained',sources:[{source_id:'cam',epoch_id:'epoch',artifacts:[artifact]}]},'http://localhost/control-room/');
const clip=boot.data.clips[0];
assert.equal(M.streamTime(clip,3),10);assert.equal(M.localTime(clip,11),4);assert.equal(M.streamTime(clip,2),null);
assert.throws(()=>M.bootstrap({read_authority:'legacy'}));
assert.throws(()=>M.bootstrap({schema:'rocoto.player-bootstrap.v1',read_authority:'maintained',sources:[{source_id:'cam',epoch_id:'e',artifacts:[{...artifact,stream_window:{start_s:10,end_s:20}}]}]},'http://localhost/'));
assert.equal(M.safeUrl('https://foreign.test/private','http://localhost/'),null);
// A maintained catalog with nothing playable is unavailable; it never renders as a pending load.
const maintainedSources=sources=>({schema:'rocoto.player-bootstrap.v1',read_authority:'maintained',sources});
assert.throws(()=>M.bootstrap(maintainedSources([]),'http://localhost/'),/Camera connection unavailable/);
assert.throws(()=>M.bootstrap(maintainedSources([{source_id:'cam',epoch_id:'epoch',artifacts:[{...artifact,media_url:'https://foreign.test/private.mp4'}]}]),'http://localhost/'),/Camera connection unavailable/);
// The location record travels with the maintained bootstrap: name, address, coordinates and the weather place
// the header reads. Bounded text and real coordinates are kept; anything else leaves the field empty.
{const sofia={latitude:42.6842677,longitude:23.3162691,timezone:'Europe/Sofia'};
 const record={id:'location-1',name:'Vitosha Boulevard 85',address:'85 Vitosha Boulevard, Sofia',coordinates:{...sofia,source:'https://listing.test/85'},weather:{name:'Vitosha Boulevard 85',address:'85 Vitosha Boulevard, Sofia',...sofia,coordinate_source:'https://listing.test/85',coordinate_precision:'address_listing'},internal_note:'host only'};
 const located=value=>M.bootstrap({...maintainedSources([{source_id:'cam',epoch_id:'epoch',artifacts:[artifact]}]),location:value},'http://localhost/').data.location;
 assert.deepEqual(located(record),{id:'location-1',name:'Vitosha Boulevard 85',address:'85 Vitosha Boulevard, Sofia',coordinates:{...sofia,source:'https://listing.test/85'},weather:{name:'Vitosha Boulevard 85',address:'85 Vitosha Boulevard, Sofia',...sofia,coordinate_source:'https://listing.test/85',coordinate_precision:'address_listing'}});
 assert.equal(located(undefined),null);assert.equal(located(null),null);
 for(const broken of ['Vitosha',[record],{...record,id:''},{...record,name:' '},{...record,name:'x'.repeat(201)}])assert.equal(located(broken),null);
 // A named location with unusable coordinates keeps its name and address and has no weather place.
 for(const point of [{latitude:'42.68'},{latitude:91},{longitude:181},{latitude:NaN},{timezone:''}]){
  const partial=located({...record,coordinates:{...record.coordinates,...point},weather:{...record.weather,...point}});
  assert.deepEqual([partial.name,partial.address,partial.coordinates,partial.weather],['Vitosha Boulevard 85','85 Vitosha Boulevard, Sofia',null,null]);}
 assert.deepEqual(located({id:'location-1',name:'Shop'}),{id:'location-1',name:'Shop',address:'',coordinates:null,weather:null});}
// One session is the recordings of one source and epoch in stream order. Playback continues into
// the following recording and, after the last, returns to the first; another session stays apart.
{const part=(asset,start,end)=>({...artifact,source_asset_id:asset,mapping_id:'map-'+asset,source_window:{start_s:0,end_s:end-start},stream_window:{start_s:start,end_s:end}});
 const rooms=M.bootstrap(maintainedSources([{source_id:'cam',epoch_id:'epoch',artifacts:[part('late',30,34),part('first',10,14),part('middle',14,30)]},{source_id:'cam',epoch_id:'next-epoch',artifacts:[part('other',0,4)]},{source_id:'door',epoch_id:'epoch',artifacts:[part('door',10,14)]}]),'http://localhost/').data.clips;
 const named=asset=>rooms.find(c=>c.source_asset_id===asset),after=asset=>M.following(rooms,named(asset)).source_asset_id;
 assert.deepEqual(M.session(rooms,named('middle')).map(c=>c.source_asset_id),['first','middle','late']);
 assert.deepEqual([after('first'),after('middle'),after('late')],['middle','late','first']);
 assert.deepEqual([after('other'),after('door')],['other','door']);
 assert.equal(M.following(rooms,{...named('first'),id:'unpublished'}),null);}
function result(at=10){return {schema:'rocoto.realtime-snapshot.v1',status:'ephemeral',source_id:'cam',epoch_id:'epoch',clock:'stream',playhead_s:String(at),snapshot:{visits:[{visit_id:'visit0',chapters:[],basket:{items:[]}}]},playback:{schema:'rocoto.epoch-playback.v1',source_id:'cam',epoch_id:'epoch',tracker_namespace:'run',playhead_s:String(at),artifacts:[artifact],visitors:[{visit_subject_id:'visit0',tracker_id:'0',birth_id:'birth0',first_observed:null,first_reported:{source_asset_id:'file',source_s:3,stream_s:10},segments:[],tracking_state_at_playhead:'reported'}]}};}
assert.equal(M.visitors(result())[0].visibilityKnown,false);
assert.equal(M.trackerVisit(result(),clip,{id:'0',birth_id:'birth0'},3),'visit0');
assert.equal(M.trackerVisit(result(),clip,{id:'0'},3),null);
assert.equal(M.trackerVisit(result(),clip,{id:'P0',birth_id:'birth0'},3),null);
assert.equal(M.trackerVisit(result(),clip,{id:'0',birth_id:'other'},3),null);
// Current sidecar geometry may be newer than the response's clipped segments.
assert.equal(M.trackerVisit(result(),clip,{id:'0',birth_id:'birth0'},4),'visit0');
// Canonical chapter types keep their namespace: joining a queue never reads as entering the view,
// and every product.* action counts as an item interaction.
function chaptered(types,at=13){const value=result(at);value.snapshot.visits[0].chapters=types.map((type,index)=>({event_id:'event'+index,event_revision:1,type,start_s:'11',end_s:'12',description:'',product_description:'',continuity:{source_asset_id:'file',mapping_id:'map',epoch_id:'epoch',source_window:{start_s:'4',end_s:'5'}}}));return M.chapters(value,'visit0',boot.data.clips);}
assert.deepEqual(chaptered(['product.pick_up','product.put_back','product.examine','product.drop','product.rotate','visitor.enter','visitor.exit','queue.enter','queue.exit','checkout.reached','checkout.exit','payment.cash_handed','payment.card_presented','display.look']).map(chapter=>[chapter.event,chapter.interaction]),
 [['pick_up',true],['put_back',true],['examine_object',true],['drop_item',true],['rotate_item',true],['enter_view',false],['leave_view',false],['entered_queue',false],['left_queue',false],['reached_checkout',false],['left_checkout',false],['handed_cash',false],['presented_card',false],['look_at_display',false]]);
assert.deepEqual(chaptered(['partner.custom_step','product.custom_step']).map(chapter=>[chapter.event,chapter.interaction,chapter.type]),[['partner_custom_step',false,'partner.custom_step'],['product_custom_step',true,'product.custom_step']]);
assert.equal(chaptered(['product.pick_up'],10.5).length,0);
(async()=>{
 let now=0;const requests=[],states=[];
 const reader=M.create({now:()=>now,minInterval:500,retryDelay:3000,onChange:state=>states.push(state),fetcher:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))});
 const reply=(i,value,ok=true)=>requests[i].resolve({ok,json:async()=>value});
 reader.update({sourceId:'cam',epochId:'epoch',time:11});await tick();assert.equal(requests.length,1);
 reader.update({sourceId:'cam',epochId:'epoch',time:10});await tick();assert.equal(requests[0].options.signal.aborted,true);
 reply(0,result(11));await tick();assert.equal(reader.state().value,null);
 reply(1,result(10));await tick();assert.equal(reader.state().value.playhead_s,'10');
 for(let i=0;i<30;i++)reader.update({sourceId:'cam',epochId:'epoch',time:10+i/100});await tick();assert.equal(requests.length,2);
 now=600;reader.update({sourceId:'cam',epochId:'epoch',time:10.4});await tick();reply(2,result(999));await tick();assert.equal(reader.state().status,'unavailable');
 now=700;reader.update({sourceId:'cam',epochId:'epoch',time:10.5});await tick();assert.equal(requests.length,3);
 reader.suspend(true);assert.equal(reader.state().value,null);now=5000;reader.update({sourceId:'cam',epochId:'epoch',time:12});await tick();assert.equal(requests.length,3);
 reader.invalidate();reader.suspend(false);await tick();assert.equal(requests.length,3);
 reader.update({sourceId:'cam',epochId:'epoch',time:12});await tick();reply(3,{},false);await tick();assert.equal(reader.state().status,'unavailable');
 reader.destroy();
 // Scrubbing invalidates on every seek. Reads stay within a burst of two plus one per interval,
 // every superseded read is aborted, and the latest position is the one that is read.
 // The fetcher rejects on abort, as the browser's does; a superseded read never reports a dropped connection.
 {let at=0;const reads=[],seen=[],scrub=M.create({now:()=>at,minInterval:500,retryDelay:3000,onChange:state=>seen.push(state.status),fetcher:(url,options)=>new Promise((resolve,reject)=>{reads.push({url,options,resolve});const fail=()=>reject(new Error('aborted'));if(options.signal.aborted)fail();else options.signal.addEventListener('abort',fail);})});
  for(let i=0;i<40;i++){scrub.invalidate();scrub.update({sourceId:'cam',epochId:'epoch',time:10+i/10,paused:true});}
  await tick();assert.equal(reads.length,2);assert.equal(reads.every(read=>read.options.signal.aborted),true);assert.equal(scrub.state().value,null);
  at=500;scrub.invalidate();scrub.update({sourceId:'cam',epochId:'epoch',time:13,paused:true});await tick();
  assert.equal(reads.length,3);assert.match(reads[2].url,/at_s=13$/);
  reads[0].resolve({ok:true,json:async()=>result(10)});reads[2].resolve({ok:true,json:async()=>result(13)});await tick();
  assert.equal(scrub.state().status,'ready');assert.equal(scrub.state().value.playhead_s,'13');assert.equal(seen.includes('unavailable'),false);
  // Returning from a hidden view is one more seek-class read, inside the same allowance.
  for(let i=0;i<10;i++){scrub.suspend(true);scrub.suspend(false);}await tick();assert.equal(reads.length,3);
  scrub.destroy();}
 // A paused view re-reads every five seconds. This clock belongs to the test: the reader's timers and its now()
 // advance only through advance(), one due timer at a time, with every pending answer settled in between.
 {const real=[global.setTimeout,global.clearTimeout],timers=new Map();let clock=0,serial=0;
  const settle=async()=>{for(let i=0;i<4;i++)await tick();};
  global.setTimeout=(run,delay=0)=>{timers.set(++serial,{at:clock+Math.max(0,delay),run});return serial;};global.clearTimeout=key=>{timers.delete(key);};
  const advance=async ms=>{const end=clock+ms;for(;;){const due=[...timers].filter(([,timer])=>timer.at<=end).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];if(!due)break;timers.delete(due[0]);clock=due[1].at;due[1].run();await settle();}clock=end;await settle();};
  try{
   const reads=[],answer={ok:true},at=()=>reads.map(read=>read.at);
   const view=M.create({now:()=>clock,fetcher:async url=>{const time=Number(new URL(url,'http://localhost').searchParams.get('at_s'));reads.push({at:clock,time});return {ok:answer.ok,json:async()=>result(time)};}});
   const frame=(time,paused)=>view.update({sourceId:'cam',epochId:'epoch',time,paused});
   frame(12,true);await settle();assert.deepEqual(at(),[0]);assert.equal(view.state().status,'ready');
   // The paused frame repaints many times inside the interval; the snapshot on screen answers each one.
   for(let elapsed=250;elapsed<5000;elapsed+=250){await advance(250);frame(12,true);await settle();}
   await advance(249);assert.deepEqual(at(),[0]);
   await advance(1);assert.deepEqual(at(),[0,5000]);
   await advance(4999);assert.deepEqual(at(),[0,5000]);
   await advance(1);assert.deepEqual(at(),[0,5000,10000]);assert.equal(view.state().value.playhead_s,'12');
   // Playing again returns to the steady half second.
   await advance(100);frame(12.1,false);await settle();assert.deepEqual(at().slice(3),[]);
   await advance(400);assert.deepEqual(at().slice(3),[10500]);
   await advance(500);assert.deepEqual(at().slice(3),[10500,11000]);
   // A pause on a new frame reads that frame on the steady cadence, then waits five seconds again.
   await advance(100);frame(12.6,true);await settle();
   await advance(400);assert.deepEqual(at().slice(5),[11500]);assert.equal(reads.at(-1).time,12.6);
   await advance(4999);assert.deepEqual(at().slice(5),[11500]);
   await advance(1);assert.deepEqual(at().slice(5),[11500,16500]);
   // A refused paused read leaves the view unavailable and asks again after the same five seconds.
   answer.ok=false;await advance(5000);assert.deepEqual(at().slice(7),[21500]);assert.equal(view.state().status,'unavailable');
   await advance(4999);assert.deepEqual(at().slice(7),[21500]);
   answer.ok=true;await advance(1);assert.deepEqual(at().slice(7),[21500,26500]);assert.equal(view.state().status,'ready');
   // A hidden view reads nothing however long it stays hidden; returning reads at once.
   view.suspend(true);await advance(60000);assert.equal(reads.length,9);
   view.suspend(false);await settle();assert.deepEqual(at().slice(9),[86500]);
   view.destroy();await advance(60000);assert.equal(reads.length,10);
  }finally{[global.setTimeout,global.clearTimeout]=real;}}
 console.log('Maintained player clock, identity, cancellation, backoff, scrub allowance, paused cadence and suspension passed');
})().catch(error=>{console.error(error);process.exit(1);});
