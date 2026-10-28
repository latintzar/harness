/* Location summaries from saved observations. Recording clocks are not capture dates. */
(function(root){
 'use strict';
 const interactions=new Set(['pick_up','pickup','picked_up_item','touch','touch_product','put_down','put_back','put_back_item','rotate_item','examine_object','inspect_product','hand_over','place_in_basket','carry','drop_item','conceal_item','remove_packaging','exit_with_item','place_elsewhere','reach_toward']);
 const pickups=new Set(['pick_up','pickup','picked_up_item']),putbacks=new Set(['put_back','put_back_item']);
 const labels={pick_up:'Pick up item',pickup:'Pick up item',picked_up_item:'Pick up item',put_back:'Put back item',put_back_item:'Put back item',put_down:'Put down item',examine_object:'Examine item',inspect_product:'Inspect product',rotate_item:'Turn item',touch:'Touch item',place_in_basket:'Place in basket',hand_over:'Hand over item'};
 const clean=x=>String(x??'').replace(/\s+/g,' ').trim(),safe=x=>encodeURIComponent(String(x)),finite=x=>typeof x==='number'&&Number.isFinite(x);
 const fixtureRefs=new Set(['fixture','store fixture','shelf','shelves','shelf unit','pegboard','pegboard shelf','table','tables','counter','counters','checkout counter','till','tills','cash register','display','display case','display rack','rack','racks']);
 function isFixtureTarget(event){
  const type=clean(event.object_type||event.object_category).toLowerCase().replaceAll('_',' '),ref=clean(event.object_ref).toLowerCase().replaceAll('_',' ');
  if(['product','merchandise','retail product'].includes(type))return false;
  return event.is_fixture===true||fixtureRefs.has(type)||fixtureRefs.has(ref);
 }
 function displayDescription(value){
  let text=clean(value);
  // Collapse repeated exact blocks; retain different observations and all source text.
  for(let i=0;i<4;i++){
   const middle=Math.floor(text.length/2),left=text.slice(0,middle).trim(),right=text.slice(text.length%2?middle+1:middle).trim();
   if(left&&left===right){text=left;continue;}break;
  }
  return text;
 }
 const actor=x=>/^(?:Person\s*)?(P?\d+)$/i.exec(clean(x))?.[1].toUpperCase()||null;
 function captureDate(clip){const value=clip.capture_start_utc||clip.recorded_at_utc;if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value))return null;const t=Date.parse(value);return Number.isFinite(t)?t:null;}
 function imageOf(e){return e.object_crop_verified&&e.object_crop_url?e.object_crop_url:e.person_crop_url||e.snapshot_url||null;}
 function metricSource(value,needed){return value===null?{state:'missing',needed_source:needed}:{state:'observed',basis:'saved_model_observations'};}
 function build({clips=[],tracks={},locationMetrics=null,model='8b',eventTypes=null,page=null}={}){
  const locations=new Map();
  for(const clip of clips.filter(c=>c.is_primary_trial===true&&c.viewer_enabled!==false)){
   const id=String(clip.location_id||clip.room_id||'recordings'),roomId=String(clip.room_id||id),source=String(clip.source_recording_id||clip.id),duration=finite(clip.duration_s)?Math.max(0,clip.duration_s):0;
   let location=locations.get(id);
   if(!location){location={id,name:clip.location_title||clip.location_name||clip.room_title||'Recordings',poster:clip.poster||null,rooms:[],recordings:[],events:[],products:[],sessions:[],timeline:[],availablePeriods:['recordings'],coverage:{kind:'sample_recordings',recording_count:0,tracked_recording_count:0,duration_s:0,capture_start:null,capture_end:null},metrics:{observed_interactions:0,observed_pickups:0,observed_putbacks:0,observed_tracks:0,observed_visitors:null,conversion_rate:null,revenue:null,average_transaction_value:null,average_queue_time_s:null,unmet_demand:null},metric_sources:{},missing_sources:[],_products:new Map(),_sessions:new Map(),_dates:[]};locations.set(id,location);}
   if(!location.rooms.some(r=>r.id===roomId))location.rooms.push({id:roomId,name:clip.room_title||roomId});
   const recording={id:clip.id,title:clip.title,media:clip.media,poster:clip.poster,duration_s:duration,roomId};
   // Home marks each tracked visitor on the footage from the tracker's own boxes: a live reference to the trace the page already holds.
   Object.defineProperty(recording,'trace',{enumerable:false,get:()=>tracks[clip.id]||null});
   location.recordings.push(recording);location.coverage.recording_count++;location.coverage.duration_s+=duration;
   const captured=captureDate(clip);if(captured!==null)location._dates.push([captured,captured+duration*1000]);
   function session(personId){const key=`${safe(source)}/clip/${safe(clip.id)}/person/${safe(personId)}`;let value=location._sessions.get(key);if(!value){value={id:key,personId,clipId:clip.id,source_recording_id:source,first_s:null,last_s:null,observed_s:null,events:[],portrait:null,sessionTarget:{clipId:clip.id,personId,time_s:0}};location._sessions.set(key,value);}return value;}
   const trace=tracks[clip.id],fps=trace?.fps;
   if(finite(fps)&&fps>0){location.coverage.tracked_recording_count++;for(const [frame,items] of Object.entries(trace.frames||{})){const t=finite(trace.frame_times_s?.[frame])?trace.frame_times_s[frame]:Number(frame)/fps;if(!Number.isFinite(t)||t<0||t>duration)continue;for(const item of items){const personId=actor(item.id);if(!personId)continue;const s=session(personId);s.first_s=s.first_s===null?t:Math.min(s.first_s,t);s.last_s=s.last_s===null?t:Math.max(s.last_s,t);s._sample_count=(s._sample_count||0)+1;s.sessionTarget.time_s=s.first_s;}}for(const s of location._sessions.values())if(s.clipId===clip.id&&s._sample_count){s.observed_s=Math.min(duration,s._sample_count*Math.max(1,trace.sampleStep||1)/fps);delete s._sample_count;}}
   for(const [wi,window] of (clip.models?.[model]?.windows||[]).entries())for(const [ei,event] of (window.events||[]).entries()){
    const start=event.playback_start_s,end=event.playback_end_s;
    if(event.eligible_for_overlay===false||!finite(start)||!finite(end)||start<0||end<start||start>duration+.01||end>duration+.01)continue;
    const type=clean(event.event||event.type);if(!type)continue;
    const personIds=[...new Set((Array.isArray(event.person_ids)?event.person_ids:[event.person_id]).map(actor).filter(Boolean))],primary=actor(event.primary_person_id),personId=personIds.length===1?personIds[0]:personIds.includes(primary)?primary:null;
    const eventId=`${safe(source)}/event/${safe(event.event_id??`${model}:${wi}:${ei}`)}`,kind=eventTypes?.[type]?.evidence_kind,isInteraction=kind?kind==='interaction':interactions.has(type);
    const normalized={id:eventId,type,label:eventTypes?.[type]?.label||labels[type]||type.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase()),description:displayDescription(event.evidence||event.item_description||event.description),item_description:displayDescription(event.item_description),is_interaction:isInteraction,time_s:start,end_s:end,clipId:clip.id,personId,personIds,person_keys:personIds.map(p=>`${safe(source)}/clip/${safe(clip.id)}/person/${safe(p)}`),image:imageOf(event),image_kind:event.object_crop_verified&&event.object_crop_url?'verified_crop':'context_frame',media:clip.media,poster:clip.poster,source_clip_duration_s:duration,sessionTarget:{clipId:clip.id,personId,time_s:start},observed_at:captured===null?null:new Date(captured+start*1000).toISOString(),basis:'model_observation',model,window_index:wi,source_event_id:event.event_id||null};
    location.events.push(normalized);
    for(const pid of personIds){const s=session(pid);s.events.push(normalized);if(!s.portrait&&event.person_crop_url)s.portrait=event.person_crop_url;if(s.first_s===null)s.sessionTarget.time_s=start;}
    if(!isInteraction)continue;
    location.metrics.observed_interactions++;if(pickups.has(type))location.metrics.observed_pickups++;if(putbacks.has(type))location.metrics.observed_putbacks++;
    if(isFixtureTarget(event))continue;
    const ref=clean(event.object_ref),named=ref&&!['unknown','none','null'].includes(ref.toLowerCase()),productId=`${safe(source)}/clip/${safe(clip.id)}/${safe(model)}/window/${wi}/object/${safe(named?ref:`event:${ei}`)}`;
    let product=location._products.get(productId);
    if(!product){product={id:productId,label:displayDescription(event.item_description)||(named?ref:'Observed item'),image:null,identity_basis:named?'model_reference_within_window':'single_observation',object_ref:named?ref:null,sku:null,price:null,counts:{interactions:0,pickups:0,putbacks:0},events:[],sessions:[],sessionTarget:normalized.sessionTarget};location._products.set(productId,product);}
    product.events.push(normalized);product.counts.interactions++;if(pickups.has(type))product.counts.pickups++;if(putbacks.has(type))product.counts.putbacks++;
    if(event.object_crop_verified&&event.object_crop_url||!product.image)product.image=normalized.image;
    for(const key of normalized.person_keys)if(!product.sessions.includes(key))product.sessions.push(key);
    if(normalized.observed_at)location.timeline.push({at:normalized.observed_at,event_id:normalized.id,product_id:product.id,type,clipId:clip.id,personId});
   }
  }
  for(const location of locations.values()){
   location.products=[...location._products.values()];location.sessions=[...location._sessions.values()].sort((a,b)=>location.recordings.findIndex(r=>r.id===a.clipId)-location.recordings.findIndex(r=>r.id===b.clipId)||(a.first_s??Infinity)-(b.first_s??Infinity)||a.personId.localeCompare(b.personId,undefined,{numeric:true}));location.metrics.observed_tracks=location.coverage.tracked_recording_count?location.sessions.filter(s=>s.first_s!==null).length:null;location.room_count=location.rooms.length;
   if(location._dates.length){location.coverage.capture_start=new Date(Math.min(...location._dates.map(x=>x[0]))).toISOString();location.coverage.capture_end=new Date(Math.max(...location._dates.map(x=>x[1]))).toISOString();location.availablePeriods.push('day','week','month','year');}
   location.coverage.dated_recording_count=location._dates.length;location.timeline.sort((a,b)=>a.at.localeCompare(b.at));
   const byRecording=location.recordings.map((recording,index)=>{
    const events=location.events.filter(e=>e.clipId===recording.id),waits=[],entries=new Map();
    for(const e of [...events].sort((a,b)=>a.time_s-b.time_s)){
     if(e.personId===null)continue;
     if(e.type==='entered_queue'&&!entries.has(e.personId))entries.set(e.personId,e.time_s);
     if(['reached_checkout','reach_checkout','left_queue','queue_exit'].includes(e.type)&&entries.has(e.personId)){
      const start=entries.get(e.personId);if(e.time_s>=start)waits.push(e.time_s-start);entries.delete(e.personId);
     }
    }
    const trace=tracks[recording.id],tracked=finite(trace?.fps)&&trace.fps>0,products=events.filter(e=>e.is_interaction);
    return {label:recording.title||recording.id,clipId:recording.id,index,waits,values:{observed_interactions:products.length,observed_pickups:products.filter(e=>pickups.has(e.type)).length,observed_putbacks:products.filter(e=>putbacks.has(e.type)).length,observed_tracks:tracked?location.sessions.filter(s=>s.clipId===recording.id&&s.first_s!==null).length:null,average_queue_time_s:waits.length?waits.reduce((a,b)=>a+b,0)/waits.length:null}};
   });
   const allWaits=byRecording.flatMap(row=>row.waits);location.metrics.average_queue_time_s=allWaits.length?allWaits.reduce((a,b)=>a+b,0)/allWaits.length:null;
   const needed={average_queue_time_s:'paired_queue_entry_and_exit_observations',observed_tracks:'tracker_outputs',observed_visitors:'resolved_identity_across_recordings',conversion_rate:'authoritative_POS_and_valid_visit_matching',revenue:'authoritative_POS',average_transaction_value:'authoritative_POS',unmet_demand:'validated_demand_model_and_stock_availability'};
   for(const [key,value] of Object.entries(location.metrics))location.metric_sources[key]=metricSource(value,needed[key]);
   location.metric_sources.observed_tracks={state:location.coverage.tracked_recording_count===location.coverage.recording_count?'observed':location.coverage.tracked_recording_count?'partial':'missing',needed_source:'tracker_outputs',basis:'recording_scoped_tracker_ids',identity_continuity:'separate_recordings_and_track_splits_remain_separate'};
   for(const key of ['observed_interactions','observed_pickups','observed_putbacks','observed_tracks','average_queue_time_s']){
    Object.assign(location.metric_sources[key],{series_kind:'by_recording',series_label:'By recording',series:byRecording.map(({label,clipId,index,values})=>({label,clipId,index,value:values[key]}))});
   }
   if(allWaits.length)Object.assign(location.metric_sources.average_queue_time_s,{basis:'paired_queue_event_times',pair_count:allWaits.length});
   const supplied=Array.isArray(locationMetrics)?locationMetrics.find(m=>String(m.location_id)===location.id):locationMetrics?.[location.id]||(locationMetrics?.location_id===location.id?locationMetrics:null);
   if(supplied?.authoritative===true&&typeof supplied.source==='string'&&supplied.source.trim()&&supplied.period?.start&&supplied.period?.end&&Number.isFinite(Date.parse(supplied.period.start))&&Number.isFinite(Date.parse(supplied.period.end))&&Date.parse(supplied.period.start)<=Date.parse(supplied.period.end)){
    for(const key of Object.keys(needed))if(finite(supplied.metrics?.[key])){location.metrics[key]=supplied.metrics[key];location.metric_sources[key]={state:'authoritative',source:supplied.source,period:supplied.period};if(Array.isArray(supplied.series?.[key]))location.metric_sources[key].series=supplied.series[key].filter(row=>row&&finite(row.value)).map(row=>({...row}));}
   }
   location.missing_sources=[...new Set(Object.entries(location.metrics).filter(([,v])=>v===null).map(([k])=>needed[k]).filter(Boolean))];if(!location._dates.length)location.missing_sources.push('recording_capture_timestamps');
   delete location._products;delete location._sessions;delete location._dates;
  }
  const answer={schema_version:1,basis:'saved_observations',locations:[...locations.values()]};
  // Home's page data (core_trackers, their canvases, products, baskets) travels with the answer as given.
  if(page&&typeof page==='object')for(const key of ['core_trackers','core_tracker_canvases','products','baskets','currency'])if(page[key]!==undefined)answer[key]=page[key];
  return answer;
 }
 const api=Object.freeze({build});if(typeof module!=='undefined'&&module.exports)module.exports=api;root.LocationData=api;
})(typeof window!=='undefined'?window:globalThis);
