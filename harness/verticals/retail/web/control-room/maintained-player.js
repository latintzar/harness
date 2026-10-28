/* One source-clock read lifecycle for the maintained camera player. */
(function(root){
 'use strict';
 const seconds=value=>{const n=typeof value==='string'&&value.trim()!==''?Number(value):value;return typeof n==='number'&&Number.isFinite(n)&&n>=0&&n<=Number.MAX_SAFE_INTEGER?n:null;};
 const id=value=>typeof value==='string'&&value.length>0&&value.length<=256?value:null;
 const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
 // Canonical snapshot types, named in the vocabulary the journey presentation already labels.
 // The namespace decides the name: joining a queue and entering the view stay distinct.
 const EVENT_NAMES=Object.freeze({'visitor.enter':'enter_view','visitor.exit':'leave_view','product.touch':'touch','product.pick_up':'pick_up','product.carry':'carry','product.place_in_basket':'place_in_basket','product.put_back':'put_back','product.put_down':'put_down','product.place_elsewhere':'place_elsewhere','product.drop':'drop_item','product.hand_over':'hand_over','product.examine':'examine_object','product.rotate':'rotate_item','queue.enter':'entered_queue','queue.exit':'left_queue','checkout.reached':'reached_checkout','checkout.exit':'left_checkout','payment.cash_handed':'handed_cash','payment.card_presented':'presented_card','display.look':'look_at_display','stock.empty':'shelf_empty','stock.available':'shelf_available','stock.replenished':'shelf_replenished'});
 function windowRange(value){const start=seconds(value?.start_s),end=seconds(value?.end_s);if(start===null||end===null||end<=start)throw Error('Playback time unavailable');return {start_s:start,end_s:end};}
 function mapping(clip){const local=windowRange(clip.source_window),stream=windowRange(clip.stream_window);if(Math.abs(local.end_s-local.start_s-stream.end_s+stream.start_s)>1e-6)throw Error('Playback time unavailable');return {local,stream};}
 function streamTime(clip,localTime){const {local,stream}=mapping(clip);if(!Number.isFinite(localTime)||localTime<local.start_s-1e-6||localTime>local.end_s+1e-6)return null;return stream.start_s+localTime-local.start_s;}
 function localTime(clip,time){const {local,stream}=mapping(clip);return Number.isFinite(time)&&time>=stream.start_s-1e-6&&time<=stream.end_s+1e-6?local.start_s+time-stream.start_s:null;}
 function safeUrl(value,base=root.location?.href){if(!value)return null;try{const url=new URL(value,base),origin=new URL(base).origin;return url.origin===origin&&['http:','https:'].includes(url.protocol)?url.href:null;}catch{return null;}}
 // The location record of the session's camera: name, address, coordinates and the weather place the header reads.
 // It is display data. Bounded text and real coordinates are kept; an unusable record leaves the field empty.
 const text=(value,maximum)=>typeof value==='string'&&value.trim()!==''&&value.length<=maximum?value:null;
 function point(value){return value&&typeof value.latitude==='number'&&typeof value.longitude==='number'&&Math.abs(value.latitude)<=90&&Math.abs(value.longitude)<=180&&text(value.timezone,100)?{latitude:value.latitude,longitude:value.longitude,timezone:value.timezone}:null;}
 function place(value){
  if(!value||typeof value!=='object'||Array.isArray(value)||!id(value.id))return null;
  const name=text(value.name,200);if(!name)return null;
  const address=text(value.address,500)||'',coordinates=point(value.coordinates),spot=coordinates&&point(value.weather);
  return {id:value.id,name,address,coordinates:coordinates&&{...coordinates,source:text(value.coordinates.source,2000)},weather:spot&&{name:text(value.weather.name,200)||name,address:text(value.weather.address,500)||address,...spot,coordinate_source:text(value.weather.coordinate_source,2000),coordinate_precision:text(value.weather.coordinate_precision,100)}};
 }
 function bootstrap(value,base=root.location?.href){
  if(value?.schema!=='rocoto.player-bootstrap.v1'||!['legacy','maintained'].includes(value.read_authority))throw Error('Camera connection unavailable');
  if(value.read_authority==='legacy')return {authority:'legacy'};
  if(!Array.isArray(value.sources))throw Error('Camera connection unavailable');
  const clips=[];
  for(const source of value.sources){
   if(!id(source.source_id)||!id(source.epoch_id)||!Array.isArray(source.artifacts))throw Error('Camera connection unavailable');
   for(const artifact of source.artifacts){
    if(!id(artifact.source_asset_id)||!id(artifact.mapping_id))throw Error('Recording connection unavailable');
    const media=safeUrl(artifact.media_url,base);if(!media)continue;
    const times=mapping(artifact),clipId=[source.source_id,source.epoch_id,artifact.source_asset_id,artifact.mapping_id].map(encodeURIComponent).join('/');
    clips.push({...artifact,id:clipId,source_id:source.source_id,source_recording_id:source.source_id,epoch_id:source.epoch_id,title:source.label||'Camera',room_id:source.source_id,room_name:source.label||'Camera',media,poster:safeUrl(artifact.poster_url,base),tracks_url:safeUrl(artifact.geometry_url,base),source_window:times.local,stream_window:times.stream,duration_s:times.local.end_s,initial_time_s:times.local.start_s,is_primary_trial:true,viewer_enabled:true,models:{maintained:{windows:[]}}});
   }
  }
  // Nothing playable under the maintained authority is an unavailable view, never a pending load.
  if(!clips.length)throw Error('Camera connection unavailable');
  return {authority:'maintained',data:{clips,location:place(value.location)}};
 }
 // One session is the mapped recordings of one source and tracker epoch, in stream order.
 function session(clips,clip){return clips.filter(c=>c.source_id===clip.source_id&&c.epoch_id===clip.epoch_id).sort((a,b)=>a.stream_window.start_s-b.stream_window.start_s||a.id.localeCompare(b.id));}
 // The room loop: playback continues into the following recording and, after the last, returns to the first.
 function following(clips,clip){const rows=session(clips,clip),index=rows.findIndex(c=>c.id===clip.id);return index<0?null:rows[(index+1)%rows.length];}
 function validate(value,requested){
  if(value?.schema!=='rocoto.realtime-snapshot.v1'||value.source_id!==requested.sourceId||value.epoch_id!==requested.epochId||value.clock!=='stream'||seconds(value.playhead_s)!==requested.time)throw Error('Observation time unavailable');
  const p=value.playback;
  if(p?.schema!=='rocoto.epoch-playback.v1'||p.source_id!==requested.sourceId||p.epoch_id!==requested.epochId||seconds(p.playhead_s)!==requested.time||!Array.isArray(p.visitors)||!Array.isArray(p.artifacts))throw Error('Visitor connection unavailable');
  if(value.status!=='ephemeral'||!value.snapshot||!Array.isArray(value.snapshot.visits))throw Error('Observations are preparing');
  const ids=new Set();
  for(const visitor of p.visitors){if(!id(visitor.visit_subject_id)||!id(visitor.tracker_id)||!id(visitor.birth_id)||ids.has(visitor.visit_subject_id)||!Array.isArray(visitor.segments))throw Error('Visitor connection unavailable');ids.add(visitor.visit_subject_id);}
  return value;
 }
 function create({fetcher=(...args)=>fetch(...args),onChange=()=>{},now=()=>Date.now(),minInterval=500,retryDelay=3000}={}){
  let context=null,pending=null,value=null,status='idle',version=0,lastRequest=-Infinity,retryAt=0,suspended=false,destroyed=false,timer=null;
  // A seek, a changed session and a resumed view read ahead of the steady cadence. Their allowance is
  // two reads at once, refilled by one per interval, so a scrub or a held key stays bounded.
  let urgent=false,allowance=2,allowanceAt=null;
  function refill(){const at=now();if(allowanceAt!==null)allowance=Math.min(2,allowance+(minInterval>0?(at-allowanceAt)/minInterval:2));allowanceAt=at;return allowance;}
  function notify(){onChange({status,value,context:context?{...context}:null});}
  function cancel(){version++;pending?.abort();pending=null;clearTimeout(timer);timer=null;}
  function clear(nextStatus='idle'){cancel();value=null;status=nextStatus;notify();}
  function schedule(delay){clearTimeout(timer);timer=setTimeout(()=>{timer=null;pump();},Math.max(0,delay));}
  function pump(){
   if(destroyed||suspended||!context||pending)return;
   const wait=Math.max(retryAt-now(),urgent?(1-refill())*minInterval:lastRequest+minInterval-now());if(wait>0){schedule(wait);return;}
   if(urgent){allowance-=1;urgent=false;}
   const requested={...context},generation=++version,controller=new AbortController();pending=controller;lastRequest=now();if(!value){status='loading';notify();}
   const url=`/api/retail/realtime/sources/${encodeURIComponent(requested.sourceId)}/epochs/${encodeURIComponent(requested.epochId)}/snapshot?at_s=${encodeURIComponent(requested.time)}`;
   Promise.resolve().then(()=>fetcher(url,{credentials:'same-origin',cache:'no-store',signal:controller.signal})).then(async response=>{
    if(!response.ok)throw Error('Observation connection unavailable');
    const result=validate(await response.json(),requested);
    if(controller.signal.aborted||generation!==version||suspended||!context||context.key!==requested.key||context.time<requested.time)return;
    value=result;status='ready';retryAt=0;notify();
   }).catch(()=>{if(generation!==version||controller.signal.aborted)return;value=null;status='unavailable';retryAt=now()+retryDelay;notify();}).finally(()=>{
    if(generation!==version)return;pending=null;
    if(!suspended&&!destroyed&&context)schedule(Math.max(context.paused?5000:minInterval,retryAt-now()));
   });
  }
  function update(next){
   if(destroyed)return;
   if(!next||!id(next.sourceId)||!id(next.epochId)||seconds(next.time)===null){context=null;clear();return;}
   const key=JSON.stringify([next.sourceId,next.epochId]),changed=context?.key!==key,rewound=context&&next.time<context.time-1e-6;
   context={...next,key};
   if(changed||rewound){clear('loading');urgent=true;retryAt=0;}
   if(!suspended&&!(next.paused&&value&&seconds(value.playhead_s)===next.time&&now()-lastRequest<5000))pump();
  }
  function suspend(flag){if(destroyed)return;suspended=!!flag;if(suspended){clear('suspended');}else{urgent=true;retryAt=0;pump();}}
  function invalidate(){context=null;clear('loading');retryAt=0;}
  function destroy(){destroyed=true;context=null;cancel();value=null;status='idle';}
  return {update,suspend,invalidate,destroy,state:()=>({status,value,context})};
 }
 function visitors(value){
  return (value?.playback?.visitors||[]).map(person=>{
   const observed=person.segments.filter(segment=>['observed','reported'].includes(segment.state)).map(segment=>windowRange(segment.stream_window)).sort((a,b)=>a.start_s-b.start_s),merged=[];
   for(const part of observed){const last=merged.at(-1);if(last&&part.start_s<=last.end_s)last.end_s=Math.max(last.end_s,part.end_s);else merged.push({...part});}
   const firstPoint=person.first_observed||person.first_reported,first=seconds(firstPoint?.stream_s),last=merged.at(-1)?.end_s??first;
   return {...person,id:person.visit_subject_id,firstPoint,visibilityKnown:!!person.first_observed&&!person.segments.some(s=>s.state==='reported'),first:first??0,last:last??0,observed:merged.reduce((sum,w)=>sum+w.end_s-w.start_s,0),events:value.snapshot.visits.find(v=>v.visit_id===person.visit_subject_id)?.chapters||[]};
  }).sort((a,b)=>a.first-b.first||a.id.localeCompare(b.id));
 }
 function visit(value,visitId){return value?.snapshot?.visits?.find(v=>v.visit_id===visitId)||null;}
 function trackerVisit(value,clip,track,at){
  if(value?.playback?.tracker_namespace!==clip.geometry_binding?.tracker_namespace||clip.geometry_binding?.source_sha256!==clip.source_sha256||!track?.birth_id)return null;
  const candidates=visitors(value).filter(v=>v.tracker_id===String(track.id)&&v.birth_id===String(track.birth_id));
  if(candidates.length!==1)return null;
  const person=candidates[0],stream=streamTime(clip,at),first=person.first_observed||person.first_reported;
  // A fresh geometry frame with the same host-bound birth may extend past the
  // previous response's clipped segment. Never substitute a raw ID for a birth.
  return stream!==null&&first&&stream>=seconds(first.stream_s)?person.id:null;
 }
 function chapters(value,visitId,clips){
  const rows=visit(value,visitId)?.chapters||[];
  return rows.map(chapter=>{
   const clock=chapter.continuity;if(!clock)return null;
   const clip=clips.find(c=>c.source_asset_id===clock.source_asset_id&&c.epoch_id===clock.epoch_id&&c.mapping_id===clock.mapping_id&&c.source_id===value.source_id);if(!clip)return null;
   const at=seconds(clock.source_window?.start_s),end=seconds(clock.source_window?.end_s),stream=seconds(chapter.start_s);
   if(at===null||end===null||stream===null||stream>seconds(value.playhead_s))return null;
   const canonical=String(chapter.type||'observe'),type=Object.hasOwn(EVENT_NAMES,canonical)?EVENT_NAMES[canonical]:canonical.replaceAll('.','_'),key=JSON.stringify([chapter.event_id,chapter.event_revision]);
   return {...chapter,key,event_id:chapter.event_id,person_ids:[visitId],event:type,interaction:canonical.startsWith('product.'),playback_start_s:at,playback_end_s:end,stream_start_s:stream,stream_end_s:seconds(chapter.end_s),source_asset_id:clip.source_asset_id,clip_id:clip.id,item_description:chapter.product_description||'',evidence:chapter.description||'',eligible_for_overlay:true,maintained:true,poster:clip.poster};
  }).filter(Boolean);
 }
 root.MaintainedPlayer={bootstrap,create,session,following,streamTime,localTime,visitors,visit,trackerVisit,chapters,seconds,safeUrl,same};
})(typeof window==='undefined'?globalThis:window);
