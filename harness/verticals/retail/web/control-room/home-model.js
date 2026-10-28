/* Home · one view model.
 * Both readers feed it. The bundle reader hands LocationData.build's answer ({locations:[…]}); the maintained
 * reader hands its location with its visitors and events ({location, visitors, events, recordings}). The answer
 * is the same either way: nine Core Tracker tiles, the Tracking Agent tiles after them, three product lists and
 * the shop's footage. A count with zero events is 0. A measure whose source is still to connect names that source. */
(function(root){
 'use strict';
 const finite=x=>typeof x==='number'&&Number.isFinite(x);
 const clean=x=>String(x??'').replace(/\s+/g,' ').trim();
 const number=x=>{if(x&&typeof x==='object'&&!Array.isArray(x))x=x.value;if(x===null||x===undefined||x===''||typeof x==='boolean'||Array.isArray(x))return null;const n=Number(x);return Number.isFinite(n)?n:null;};
 const PERIODS=Object.freeze([{key:'week',label:'Weekly',days:7},{key:'month',label:'Monthly',days:30},{key:'year',label:'Annual',days:365}]);
 const DAY=86400000;

 /* Event vocabulary: the bundle's names and the maintained wire's canonical names read as one. */
 const PICK=new Set(['pick_up','pickup','picked_up_item','product.pick_up']);
 const PUT=new Set(['put_back','put_back_item','product.put_back']);
 const INTERACTION=new Set(['pick_up','pickup','picked_up_item','touch','touch_product','put_down','put_back','put_back_item','rotate_item','examine_object','inspect_product','hand_over','place_in_basket','carry','drop_item','conceal_item','remove_packaging','exit_with_item','place_elsewhere','reach_toward']);
 const SHELF_EMPTY=new Set(['shelf_empty','empty_shelf','stock.empty']);
 const SHELF_FILLED=new Set(['shelf_replenished','shelf_available','stock.replenished','stock.available']);
 const QUEUE_IN=new Set(['entered_queue','queue.enter']);
 const QUEUE_OUT=new Set(['reached_checkout','reach_checkout','left_queue','queue_exit','queue.exit','checkout.reached']);
 const LABELS={pick_up:'Pick up item',pickup:'Pick up item',picked_up_item:'Pick up item','product.pick_up':'Pick up item',put_back:'Put back item',put_back_item:'Put back item','product.put_back':'Put back item',put_down:'Put down item','product.put_down':'Put down item',examine_object:'Examine item','product.examine':'Examine item',inspect_product:'Inspect product',rotate_item:'Turn item','product.rotate':'Turn item',touch:'Touch item','product.touch':'Touch item',place_in_basket:'Place in basket','product.place_in_basket':'Place in basket',hand_over:'Hand over item','product.hand_over':'Hand over item',entered_queue:'Join the queue','queue.enter':'Join the queue',left_queue:'Leave the queue','queue.exit':'Leave the queue',reached_checkout:'Reach the till','checkout.reached':'Reach the till',shelf_empty:'Shelf empty','stock.empty':'Shelf empty',shelf_replenished:'Shelf restocked','stock.replenished':'Shelf restocked',shelf_available:'Shelf stocked','stock.available':'Shelf stocked'};
 /* A product reads by its name. A model's sentence about it keeps its opening noun phrase. */
 const productLabel=value=>{const text=clean(value);if(!text)return '';const cut=text.search(/\s+(?:held|carried|picked|taken|being)\b|,\s*(?:possibly|likely|probably|with|which|that)\b|[.;:(]/i);return (cut>8?text.slice(0,cut):text).replace(/[\s,]+$/,'');};
 const eventLabel=type=>LABELS[type]||clean(type.replace(/^[a-z]+\./,'').replaceAll('_',' ')).replace(/^./,c=>c.toUpperCase());
 const visitorLabel=id=>{const s=clean(id);if(!s)return 'Visitor';const tracker=/^(?:Person\s*)?P?(\d+)$/i.exec(s);return tracker?`Visitor ${tracker[1]}`:'Visitor';};

 /* The nine Core Trackers, in reading order. `stage` tiles stand on the footage; the others rest under it. */
 const TRACKERS=Object.freeze([
  {key:'visitors',label:'Visitors',kind:'count',mark:'line',stage:true},
  {key:'product_interactions',label:'Product interactions',kind:'count',mark:'line',stage:true,metric:'observed_interactions'},
  {key:'picked_up',label:'Picked up',kind:'count',mark:'ring',stage:true,metric:'observed_pickups',whole:'product_interactions'},
  {key:'put_back',label:'Put back',kind:'count',mark:'ring',stage:true,metric:'observed_putbacks',whole:'product_interactions'},
  {key:'basket',label:'Basket',kind:'money',mark:'line',metric:'average_transaction_value',needs:'Needs till data'},
  {key:'queue_time',label:'Queue time',kind:'duration',mark:'line',metric:'average_queue_time_s'},
  {key:'empty_shelves',label:'Empty shelves',kind:'count',mark:'ring',metric:'empty_shelves'},
  {key:'sales',label:'Sales',kind:'money',mark:'line',metric:'revenue',needs:'Needs till data'},
  {key:'conversion',label:'Conversion',kind:'rate',mark:'arc',metric:'conversion_rate',needs:'Needs till data'}
 ]);

 function isoTime(value){
  if(finite(value)&&value>1e9)return new Date(value<1e12?value*1000:value).toISOString();
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}/.test(value))return null;
  const t=Date.parse(value);return Number.isFinite(t)?new Date(t).toISOString():null;
 }
 /* One event, in the shape every tile and its history read. */
 function eventRow(e,index){
  const type=clean(e.type||e.event||e.kind),visitor=e.visitor_id??e.personId??e.person_id??e.visit_id??e.visit_subject_id??null,source=e.source_id??e.clipId??e.clip_id??e.recording_id??null;
  const time=[e.time_s,e.playback_start_s,e.source_time_s].find(finite),end=[e.end_s,e.playback_end_s].find(finite);
  const target=e.target||e.sessionTarget||(source!==null?{clipId:source,personId:visitor,time_s:time??0}:null);
  return {id:clean(e.id??e.event_id)||`event-${index}`,at:isoTime(e.at??e.observed_at??e.captured_at??e.received_at),label:clean(e.label)||eventLabel(type),description:clean(e.description||e.evidence||e.product_description||e.item_description),thumb:e.thumb||e.image||e.thumbnail_url||e.person_crop_url||e.snapshot_url||e.poster||null,visitor_id:visitor,source_id:source,
   type,interaction:e.is_interaction===true||e.interaction===true||((e.is_interaction===undefined&&e.interaction===undefined)&&(type.startsWith('product.')||INTERACTION.has(type))),time_s:time??null,end_s:end??null,media:e.media||e.media_url||null,item:clean(e.item_id??e.product_id??e.object_ref)||null,item_label:clean(e.product_description||e.item_description)||null,target};
 }
 function visitorRow(v,index){
  if(v===null||typeof v!=='object')return {id:`visitor-${index}`,at:null,label:'Visitor',description:'',thumb:null,visitor_id:null,source_id:null,type:'visitor',time_s:null,target:null};
  const id=v.personId??v.person_id??v.visit_subject_id??v.visit_id??v.id??null,source=v.source_id??v.clipId??v.clip_id??v.source_recording_id??null,first=[v.first_s,v.first].find(finite)??number(v.first_observed?.stream_s)??number(v.first_reported?.stream_s);
  return {id:clean(v.id??v.visit_subject_id??v.visit_id??id)||`visitor-${index}`,at:isoTime(v.at??v.first_seen_at??v.first_observed_at??v.captured_at),label:clean(v.label)||visitorLabel(v.personId??v.person_id??v.tracker_id??id),description:clean(v.description),thumb:v.thumb||v.portrait||v.image||null,visitor_id:id,source_id:source,type:'visitor',time_s:first??null,end_s:[v.last_s,v.last].find(finite)??null,target:v.target||v.sessionTarget||(source!==null?{clipId:source,personId:id,time_s:first??0}:null)};
 }

 function format(kind,value,currency){
  if(value===null)return {text:'',unit:''};
  if(kind==='duration'){const s=Math.max(0,Math.round(value));return s<60?{text:String(s),unit:'s'}:{text:`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`,unit:'min'};}
  if(kind==='rate'){return {text:String(Number((value*100).toFixed(1))),unit:'%'};}
  if(kind==='money'){
   if(typeof currency==='string'&&/^[A-Z]{3}$/.test(currency)){try{return {text:new Intl.NumberFormat('en',{style:'currency',currency,maximumFractionDigits:Math.abs(value)>=100?0:2}).format(value),unit:''};}catch{}}
   return {text:new Intl.NumberFormat('en',{maximumFractionDigits:Math.abs(value)>=100?0:2}).format(value),unit:''};
  }
  return {text:new Intl.NumberFormat('en',{maximumFractionDigits:1}).format(value),unit:''};
 }

 /* A series is a list of {t, value}: `t` is a time when the source is dated, otherwise the name of the camera
  * or recording that point belongs to. */
 function seriesRows(rows){
  return (Array.isArray(rows)?rows:[]).map((row,index)=>{
   const value=number(row);if(value===null)return null;
   const o=row&&typeof row==='object'?row:{};
   return {t:isoTime(o.t??o.at??o.time)||clean(o.t??o.label??o.at??o.time)||String(index),value,label:clean(o.label)||null,source_id:o.source_id??o.clipId??null};
  }).filter(Boolean);
 }
 function bucketed(events,period,now,measure){
  const dated=events.filter(e=>e.at);
  if(dated.length){
   const monthly=period.key==='year',count=monthly?12:period.days,rows=[];
   for(let i=count-1;i>=0;i--){
    let start,end;
    if(monthly){const d=new Date(now);start=Date.UTC(d.getUTCFullYear(),d.getUTCMonth()-i,1);end=Date.UTC(d.getUTCFullYear(),d.getUTCMonth()-i+1,1);}
    else{const d=new Date(now);end=Date.UTC(d.getUTCFullYear(),d.getUTCMonth(),d.getUTCDate()-i+1);start=end-DAY;}
    rows.push({t:new Date(start).toISOString(),value:measure(dated.filter(e=>{const t=Date.parse(e.at);return t>=start&&t<end;})),label:null,source_id:null});
   }
   return rows;
  }
  const sources=[];for(const e of events)if(e.source_id!==null&&!sources.includes(e.source_id))sources.push(e.source_id);
  return sources.length>1?sources.map(source=>({t:String(source),value:measure(events.filter(e=>e.source_id===source)),label:null,source_id:source})):[];
 }
 function queueWait(events){
  const open=new Map(),waits=[];
  for(const e of [...events].sort((a,b)=>(a.at||'').localeCompare(b.at||'')||(a.time_s??0)-(b.time_s??0))){
   const who=`${e.source_id}/${e.visitor_id}`;if(e.visitor_id===null)continue;const at=e.at?Date.parse(e.at)/1000:e.time_s;if(!finite(at))continue;
   if(QUEUE_IN.has(e.type)&&!open.has(who))open.set(who,at);
   else if(QUEUE_OUT.has(e.type)&&open.has(who)){const start=open.get(who);if(at>=start)waits.push(at-start);open.delete(who);}
  }
  return waits.length?waits.reduce((a,b)=>a+b,0)/waits.length:null;
 }

 /* A tile the page data already carries (core_trackers): kept as given, completed where a field is absent. */
 const ALIASES=[[/visit|session/,'visitors'],[/interact|handl/,'product_interactions'],[/pick/,'picked_up'],[/put.?back|return/,'put_back'],[/basket/,'basket'],[/queue|wait/,'queue_time'],[/shel|replen|stock/,'empty_shelves'],[/sale|revenue/,'sales'],[/conver/,'conversion']];
 const trackerOf=key=>{const text=String(key||'').toLowerCase(),exact=TRACKERS.find(t=>t.key===text);if(exact)return exact;const hit=ALIASES.find(([pattern])=>pattern.test(text));return hit?TRACKERS.find(t=>t.key===hit[1]):null;};
 const estimatedOf=row=>row?.estimated===true||/estimat/i.test(String(row?.basis||row?.value_basis||row?.price_basis||''));
 function suppliedTile(row,period,index,currency){
  const scoped=row.periods&&typeof row.periods==='object'&&row.periods[period.key]&&typeof row.periods[period.key]==='object'?{...row,...row.periods[period.key]}:row;
  const known=trackerOf(scoped.key),core=scoped.core!==false,kind=scoped.kind||known?.kind||'count',value=number(scoped.value);
  const state=['live','zero','needs','paused'].includes(scoped.state)?scoped.state:value===null?'needs':value===0?'zero':'live';
  const ratio=kind==='rate'&&scoped.unit==='%'&&value!==null?value/100:value;
  const shown=format(kind,ratio,scoped.currency||currency),hasText=typeof scoped.text==='string'&&scoped.text!=='';
  const unit=typeof scoped.unit==='string'&&(hasText||kind==='count')?scoped.unit:shown.unit;
  const events=(Array.isArray(scoped.events)?scoped.events:[]).map((e,i)=>({...e,...eventRow(e,i)}));
  const fraction=number(scoped.fraction)??(kind==='rate'&&ratio!==null?(ratio>1?ratio/100:ratio):null);
  return {...scoped,key:String(scoped.key??`tracker-${index}`),label:clean(scoped.label)||known?.label||'Tracker',value,unit,text:hasText?scoped.text:shown.text,state,
   needs:state==='needs'?clean(scoped.needs)||known?.needs||'Needs a source':null,series:seriesRows(scoped.series),events,period:period.key,kind,
   mark:['line','ring','arc','bars'].includes(scoped.mark)?scoped.mark:known?.mark||'line',stage:core&&(typeof scoped.stage==='boolean'?scoped.stage:!!known?.stage),custom:!core,core,
   fraction:fraction===null?null:Math.max(0,Math.min(1,fraction)),estimated:estimatedOf(scoped)};
 }

 function from(input,options){
  const data=input&&typeof input==='object'?input:{},settings=options||{};
  const period=PERIODS.find(p=>p.key===settings.period)||PERIODS[0],now=finite(settings.now)?settings.now:Date.now();
  const loc=(Array.isArray(data.locations)?data.locations[0]:null)||data.location||{};
  const summary=loc.period_summaries?.[period.key]||loc.periods?.[period.key]||loc.calendar?.[period.key]||null,source=summary||loc;
  const metrics=source.metrics||{},sources=source.metric_sources||loc.metric_sources||{};
  const metric=key=>key&&Object.hasOwn(metrics,key)?number(metrics[key]):null;
  const seriesOf=key=>seriesRows(metrics[key]?.series||sources[key]?.series||source.series?.[key]);

  const since=now-period.days*DAY,inPeriod=e=>!e.at||Date.parse(e.at)>=since;
  const allEvents=(Array.isArray(data.events)?data.events:Array.isArray(loc.events)?loc.events:[]).map(eventRow);
  const events=allEvents.filter(inPeriod),interactions=events.filter(e=>e.interaction);
  const visitorList=Array.isArray(data.visitors)?data.visitors:Array.isArray(data.visitors?.rows)?data.visitors.rows:Array.isArray(loc.sessions)?loc.sessions.filter(s=>s&&(s.first_s===undefined||s.first_s!==null)):[];
  const visitors=visitorList.map(visitorRow).filter(inPeriod);
  const visitorCount=number(data.visitors?.count)??(Array.isArray(data.visitors)?visitors.length:number(data.visitors))??metric('observed_visitors')??metric('observed_tracks')??visitors.length;

  const byKey={
   visitors:{value:visitorCount,events:visitors,series:()=>seriesRows(data.visitors?.series).length?seriesRows(data.visitors.series):[seriesOf('observed_visitors'),seriesOf('observed_tracks')].find(rows=>rows.length)||bucketed(visitors,period,now,rows=>rows.length)},
   product_interactions:{value:metric('observed_interactions')??interactions.length,events:interactions,series:()=>seriesOf('observed_interactions').length?seriesOf('observed_interactions'):bucketed(interactions,period,now,rows=>rows.length)},
   picked_up:{value:metric('observed_pickups')??interactions.filter(e=>PICK.has(e.type)).length,events:interactions.filter(e=>PICK.has(e.type)),series:()=>seriesOf('observed_pickups').length?seriesOf('observed_pickups'):bucketed(interactions.filter(e=>PICK.has(e.type)),period,now,rows=>rows.length)},
   put_back:{value:metric('observed_putbacks')??interactions.filter(e=>PUT.has(e.type)).length,events:interactions.filter(e=>PUT.has(e.type)),series:()=>seriesOf('observed_putbacks').length?seriesOf('observed_putbacks'):bucketed(interactions.filter(e=>PUT.has(e.type)),period,now,rows=>rows.length)},
   basket:{value:metric('average_transaction_value'),events:[],series:()=>seriesOf('average_transaction_value')},
   queue_time:{value:metric('average_queue_time_s')??queueWait(events)??0,events:events.filter(e=>QUEUE_IN.has(e.type)||QUEUE_OUT.has(e.type)),series:()=>seriesOf('average_queue_time_s')},
   empty_shelves:{value:metric('empty_shelves')??events.filter(e=>SHELF_EMPTY.has(e.type)).length,events:events.filter(e=>SHELF_EMPTY.has(e.type)||SHELF_FILLED.has(e.type)),series:()=>seriesOf('empty_shelves').length?seriesOf('empty_shelves'):bucketed(events.filter(e=>SHELF_EMPTY.has(e.type)),period,now,rows=>rows.length)},
   sales:{value:metric('revenue'),events:[],series:()=>seriesOf('revenue')},
   conversion:{value:metric('conversion_rate'),events:[],series:()=>seriesOf('conversion_rate')}
  };
  const currency=typeof loc.currency==='string'?loc.currency:typeof data.currency==='string'?data.currency:null;
  /* The page data names the tiles when it carries core_trackers: the nine Core Trackers first, then the
   * company's own. Without that key the nine are derived here from events and metrics. */
  const given=Array.isArray(data.core_trackers)?data.core_trackers:Array.isArray(loc.core_trackers)?loc.core_trackers:null;
  const canvases=data.core_tracker_canvases||loc.core_tracker_canvases||{};
  /* A tracker whose Tracking Agent is paused says so on its tile, whether the tile comes from the page or from the agent. */
  const resting=new Set((Array.isArray(data.agents)?data.agents:[]).filter(a=>a&&a.id&&a.status==='paused').map(a=>String(a.id)));
  const supplied=given?given.filter(row=>row&&typeof row==='object').map((row,index)=>{
   const tile={...suppliedTile(row,period,index,currency),canvas:row.periods?.[period.key]?.canvas||canvases[row.key]||null};
   if(!tile.core&&tile.agent_id&&resting.has(String(tile.agent_id))&&tile.state!=='needs')tile.state='paused';
   /* The page's own row and canvas ride along, as published: the shell's canvas of a tracker that stands by itself reads them. */
   Object.defineProperty(tile,'published',{enumerable:false,value:{row,canvas:canvases[row.key]||null}});
   return tile;
  }):null;
  const core=supplied?supplied.filter(t=>t.core):TRACKERS.map(tracker=>{
   const read=byKey[tracker.key],value=read.value===null||read.value===undefined?null:read.value,needs=value===null?tracker.needs||null:null;
   const shown=format(tracker.kind,value,currency),series=value===null?[]:read.series();
   return {key:tracker.key,label:tracker.label,value,unit:shown.unit,text:shown.text,state:value===null?'needs':value===0?'zero':'live',needs,series,events:read.events,period:period.key,kind:tracker.kind,mark:tracker.mark,stage:!!tracker.stage,custom:false,core:true,fraction:null,estimated:estimatedOf(sources[tracker.metric])};
  });
  /* A ring is a share of a whole: pick-ups and put-backs of the interactions, restocked shelves of the empty ones. */
  const tileOf=name=>core.find(t=>trackerOf(t.key)?.key===name)||null,share=(part,total)=>total>0?Math.max(0,Math.min(1,part/total)):0;
  const whole=tileOf('product_interactions')?.value||0;
  for(const name of ['picked_up','put_back']){const t=tileOf(name);if(t&&t.fraction===null)t.fraction=share(t.value||0,whole);}
  const emptied=tileOf('empty_shelves');if(emptied&&emptied.fraction===null)emptied.fraction=share(emptied.events.filter(e=>SHELF_FILLED.has(e.type)).length,emptied.value||0);
  const conversion=tileOf('conversion');if(conversion&&conversion.fraction===null)conversion.fraction=conversion.value===null?0:Math.max(0,Math.min(1,conversion.value));

  /* Tracking Agents the shop has made: one tile each, in the same family, after the core ones. */
  const own=supplied?supplied.filter(t=>!t.core):[],named=new Set(own.flatMap(t=>[t.key,String(t.agent_id??'')]));
  const agents=own.concat((Array.isArray(data.agents)?data.agents:[]).filter(a=>a&&a.id&&(a.agent_kind||'tracker')==='tracker'&&!named.has(`agent:${a.id}`)&&!named.has(String(a.id))).map(agent=>{
   const raw=Array.isArray(agent.events)?agent.events:Array.isArray(agent.incidents)?agent.incidents:Array.isArray(agent.observations?.incidents)?agent.observations.incidents:Array.isArray(agent.runs)?agent.runs.flatMap(run=>Array.isArray(run.observations)&&run.observations.length?run.observations:[{id:run.id,at:run.created_at,label:clean(agent.title||agent.name),description:clean(run.summary)}]):[];
   const rows=raw.map((e,i)=>eventRow({...e,at:e.at??e.timestamp??e.time??e.created_at,label:e.label||e.event||e.summary,thumb:e.thumb||e.thumbnail_url||e.thumbnail||e.image||e.poster},i)).filter(inPeriod),value=number(agent.count)??rows.length,paused=agent.status==='paused';
   return {key:`agent:${agent.id}`,label:clean(agent.title||agent.name)||'Tracking Agent',value,unit:'',text:format('count',value).text,state:paused?'paused':value===0?'zero':'live',needs:null,series:seriesRows(agent.series).length?seriesRows(agent.series):bucketed(rows,period,now,list=>list.length),events:rows,period:period.key,kind:'count',mark:'line',stage:false,custom:true,core:false,fraction:null,estimated:false,agent_id:String(agent.id)};
  }));

  /* Three product lists. A product is what the catalog or the camera names; its picture is its own. */
  let products=Array.isArray(data.products)?data.products:Array.isArray(source.products)?source.products:null;
  if(products)products=products.filter(p=>p&&typeof p==='object').map((p,index)=>{
   const counts=p.counts&&typeof p.counts==='object'?p.counts:{interactions:number(p.interactions??p.handled)??0,pickups:number(p.pickups??p.picked_up)??0,putbacks:number(p.putbacks??p.put_back)??0};
   const amount=number(p.price&&typeof p.price==='object'?p.price.amount??p.price.value:p.price),money=p.price&&typeof p.price==='object'?p.price.currency:p.currency;
   return {...p,id:String(p.id??p.sku??p.name??index),label:clean(p.label||p.name)||'Observed item',image:p.image_url||(typeof p.image==='string'?p.image:p.image?.url)||null,counts,
    price_text:typeof p.price==='string'&&p.price.trim()?p.price.trim():typeof p.price_text==='string'?p.price_text:amount===null?'':format('money',amount,money||currency).text,price_estimated:estimatedOf(p)||estimatedOf(p.price)};
  });
  if(!products){
   const grouped=new Map();
   for(const e of interactions){const key=e.item||e.item_label;if(!key)continue;let p=grouped.get(key);if(!p){p={id:String(key),label:e.item_label||'Observed item',image:null,counts:{interactions:0,pickups:0,putbacks:0},events:[],sessionTarget:e.target};grouped.set(key,p);}p.events.push(e);p.counts.interactions++;if(PICK.has(e.type))p.counts.pickups++;if(PUT.has(e.type))p.counts.putbacks++;if(!p.image&&e.thumb)p.image=e.thumb;}
   products=[...grouped.values()];
  }
  const ranked=count=>products.filter(p=>Number(p.counts?.[count]||0)>0).sort((a,b)=>Number(b.counts[count])-Number(a.counts[count])).slice(0,3).map(p=>({id:String(p.id),label:productLabel(p.label)||'Observed item',image:p.image||null,count:Number(p.counts[count]),price:p.price_text||'',estimated:!!p.price_estimated,product:p}));

  const recordings=(Array.isArray(data.recordings)?data.recordings:Array.isArray(loc.recordings)?loc.recordings:[]).filter(r=>r&&typeof r.media==='string'&&r.media).map(r=>{const row={id:String(r.id??r.media),title:clean(r.title),media:r.media,poster:r.poster||null,tracks_url:r.tracks_url||null};Object.defineProperty(row,'trace',{enumerable:false,get:()=>r.trace||null});return row;});
  const tiles=[...core,...agents],tile=key=>tiles.find(t=>t.key===key)||null;
  return {schema:'rocoto.home-model.v1',period:period.key,periods:PERIODS.map(p=>({key:p.key,label:p.label})),now,
   place:{id:loc.id??null,name:clean(loc.name),address:clean(loc.address||loc.display_name)},
   tiles,core,agents,tile,
   /* the same tile in another period, and the recording a source id names */
   tileFor:(key,other)=>other===period.key?tile(key):from(input,{...settings,period:other}).tile(key),
   media:source=>recordings.find(r=>r.id===String(source))?.media||null,
   products:{handled:ranked('interactions'),picked_up:ranked('pickups'),put_back:ranked('putbacks'),all:products},
   footage:{recordings,poster:recordings[0]?.poster||loc.cover_image||loc.hero_image||loc.poster||null},
   events,visitors};
 }
 const api=Object.freeze({from,TRACKERS,PERIODS,format,productLabel});
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 root.RocotoHomeModel=api;
})(typeof window!=='undefined'?window:globalThis);
