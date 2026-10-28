/* The full view of a Home tile.
 *
 *   RocotoHomeHistory.open(tile, model, element)   the tile grows in place into its full view
 *   RocotoHomeHistory.update(tile)                 new values for the open tile (a changed number lights the edge);
 *                                                  the sheet keeps its period, its scroll position and its focus
 *   RocotoHomeHistory.period                       the period the open sheet shows
 *   RocotoHomeHistory.close()                      the sheet returns into its tile; focus returns to the tile
 *
 * tile     {key, label, value, unit, state, needs, series:[{t, value, label}], period,
 *           events:[{id, at, label, description, thumb, visitor_id, source_id}]}
 *          Also read when present:
 *            text          the number as the tile writes it; the sheet then shows the same characters
 *            kind          'count' | 'money' | 'duration' | 'rate'; view 'shelves' | 'queue' picks a full view
 *                          by name (otherwise the key says it: empty_shelves, queue_time)
 *            estimated     true puts the Estimated tag beside the numeral
 *            figures       [{label, value, unit, text, estimated}]    the till figures of Sales and Basket
 *            shelves       [{id, label, description, thumb, empty_since, replenished_at, source_id}]
 *                          (otherwise paired from the shelf events: empty, then replenished)
 *            distribution  [{label, value}]                           Queue time, one bar per length of wait
 *                          (otherwise counted from the waits: duration_s, or queue entry to till)
 *            periods       {week:{value, series, events, ...}, ...}   the same tile in another period
 *            connect       {body, action, prompt}                     copy for a measure that needs a source
 *            empty         the sentence a count with zero events shows
 *            summary       one paragraph under the chart
 *            canvas        the tracker's canvas record from the page data (core_tracker_canvases[key]). When it
 *                          is present the sheet is drawn from it: rows (the events behind a count; for Basket
 *                          and Sales each row's lines with product, quantity, price and basis; for Queue time
 *                          the waits with start_at, end_at and wait_s), distribution [{band, value}], shelves
 *                          [{shelf, label, camera, state, empty_since, replenished_at, intervals}], visitors,
 *                          purchases and ratio for Conversion, basis ('estimated' puts the tag on the money
 *                          figure), summary, and series or history when the tile carries one point
 *          and on an event: time (shown as given), time_s + end_s and media (the clip range the thumbnail
 *          plays on hover; the media falls back to the recording named by source_id), target (the session
 *          ControlRoom opens), duration_s (a wait, for Queue time).
 * model    {periods:[{key, label}], tileFor(key, period), timezone, now, place, media(source_id),
 *           footage:{recordings:[{id, media}]}}, all optional.
 * element  the tile. The sheet takes its surface from it: dark glass for a tile on footage
 *          (.twelvelabs-card-material or data-surface="glass"), light glass for a tile on the page.
 *
 * Events, all bubbling from the tile (from the document when the tile is gone):
 *   rocoto:tile-open / rocoto:tile-close        {key}
 *   rocoto:tile-period                          {key, period}    answer with update(tile) for that period,
 *                                                                or hand model.tileFor(key, period)
 *   rocoto:open-session                         {visitor_id, source_id, at, event_id, tracker}
 *       left unhandled, it opens ControlRoom.openSession({clipId: source_id, personId: visitor_id, time_s})
 *   rocoto:open-organization-chat               {purpose, tracker, needs, prompt}
 *       left unhandled inside the product frame, it is posted to the parent window under the same type
 */
(function(root){
 'use strict';
 const doc=root.document;
 const here=doc?.currentScript?.src||root.location?.href||'';
 const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const finite=v=>typeof v==='number'&&Number.isFinite(v);
 const missing=value=>value===null||value===undefined||value==='';
 const reducedMotion=()=>typeof root.matchMedia==='function'&&root.matchMedia('(prefers-reduced-motion: reduce)').matches;
 const iconHref=name=>{try{return new URL(`kit/lucide.svg#${name}`,here).href;}catch{return `kit/lucide.svg#${name}`;}};
 const icon=name=>`<svg class="rocoto-history-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="${esc(iconHref(name))}"></use></svg>`;
 const PERIODS=[{key:'week',label:'Weekly'},{key:'month',label:'Monthly'},{key:'year',label:'Annual'}];
 const CURRENCY_LEAD=new Set(['€','$','£']);
 const SHELF_EMPTY=new Set(['shelf_empty','empty_shelf','stock.empty']),SHELF_FILLED=new Set(['shelf_replenished','shelf_available','stock.replenished','stock.available']);
 const QUEUE_IN=new Set(['entered_queue','queue.enter']),QUEUE_OUT=new Set(['reached_checkout','reach_checkout','left_queue','queue_exit','queue.exit','checkout.reached']);
 let current=null;

 /* ---- reading a tile ---------------------------------------------------------------------------- */
 function kindOf(tile){
  if(['count','shelves','queue','money','rate'].includes(tile.view))return tile.view;
  const key=String(tile.key||'').toLowerCase();
  if(/queue/.test(key)||tile.kind==='duration')return 'queue';
  if(/shel|replen/.test(key))return 'shelves';
  if(tile.kind==='money'||/^(sales|basket|revenue)$/.test(key))return 'money';
  if(tile.kind==='rate'||/conver/.test(key))return 'rate';
  return 'count';
 }
 const waits=tile=>['needs','waiting','missing'].includes(String(tile.state||''))||missing(tile.value)&&Boolean(tile.needs);
 function span(seconds){
  const total=Math.max(0,Math.round(Number(seconds)||0)),h=Math.floor(total/3600),m=Math.floor(total%3600/60),s=total%60;
  if(h)return [[String(h),'h'],[String(m),'m']];
  if(m)return [[String(m),'m'],[String(s),'s']];
  return [[String(s),'s']];
 }
 const spanText=seconds=>span(seconds).map(([n,u])=>`${n}${u}`).join(' ');
 const count=(value,digits)=>Number(value).toLocaleString('en-US',digits===undefined?{maximumFractionDigits:Number.isInteger(Number(value))?0:1}:{minimumFractionDigits:digits,maximumFractionDigits:digits});
 /* A number as the sheet writes it: [text, unit] pairs, each unit set small on the number's own line. */
 function parts(value,unit){
  if(typeof value==='string'&&!Number.isFinite(Number(value)))return [[value,'']];
  const n=missing(value)?0:Number(value),u=String(unit||'');
  if(['s','sec','secs','second','seconds'].includes(u.toLowerCase()))return span(n);
  if(CURRENCY_LEAD.has(u))return [[`${u}${count(n,Number.isInteger(n)?0:2)}`,'']];
  return [[count(n),u]];
 }
 const tight=unit=>unit.length===1;
 const written=entry=>typeof entry.text==='string'&&entry.text!==''?[[entry.text,String(entry.unit||'')]]:parts(entry.value,entry.unit);
 const numeralHtml=entry=>written(entry).map(([text,u])=>`<span>${esc(text)}${u?`<em${tight(u)?' class="is-tight"':''}>${esc(u)}</em>`:''}</span>`).join('');
 const numeralText=entry=>written(entry).map(([text,u])=>`${text}${u&&!tight(u)?' ':''}${u}`).join(' ');
 /* One value of the tile's series, written the way the tile writes its number. */
 function seriesText(tile,value){
  if(missing(value))return '';
  const shared=root.RocotoHomeModel?.format;
  if(typeof tile.text==='string'&&typeof shared==='function'&&tile.kind){try{const shown=shared(tile.kind,value,current?.model?.currency);if(shown&&typeof shown.text==='string')return numeralText({text:shown.text,unit:shown.unit});}catch{}}
  if(tile.kind==='duration')return numeralText({value,unit:'s'});
  if(tile.kind==='rate'&&String(tile.unit||'')==='%'&&Math.abs(value)<=1)return numeralText({value:Math.round(value*1000)/10,unit:'%'});
  return numeralText({value,unit:tile.unit});
 }
 function moment(at,model,withDay){
  if(finite(at)){const n=Math.max(0,at);return `${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`;}
  if(typeof at!=='string'||!at)return '';
  const time=new Date(at);
  if(!/^\d{4}-\d{2}-\d{2}/.test(at)||!Number.isFinite(time.getTime()))return at;
  const options={hour:'2-digit',minute:'2-digit',hour12:false};
  if(withDay)options.weekday='short';
  if(model?.timezone)options.timeZone=model.timezone;
  try{return new Intl.DateTimeFormat('en-GB',options).format(time).replace(',','');}catch{return at;}
 }
 function day(at,model){
  if(typeof at!=='string'||!/^\d{4}-\d{2}-\d{2}/.test(at))return typeof at==='string'?at:'';
  const time=new Date(at);if(!Number.isFinite(time.getTime()))return at;
  const options={weekday:'short',day:'numeric',month:'short'};if(model?.timezone)options.timeZone=model.timezone;
  try{return new Intl.DateTimeFormat('en-GB',options).format(time).replace(',','');}catch{return at;}
 }
 const seconds=at=>finite(at)?at:typeof at==='string'&&Number.isFinite(Date.parse(at))?Date.parse(at)/1000:null;
 const periodsOf=model=>(Array.isArray(model?.periods)&&model.periods.length?model.periods:PERIODS).map(p=>typeof p==='string'?(PERIODS.find(d=>d.key===p)||{key:p,label:p.replace(/^./,c=>c.toUpperCase())}):p).filter(p=>p&&p.key);
 const inPeriod=(tile,period)=>tile.periods&&tile.periods[period]?{...tile,...tile.periods[period],period,periods:tile.periods}:{...tile,period};
 const placeName=model=>typeof model?.place==='string'?model.place:model?.place?.name||model?.name||'';
 const mediaOf=(entry,model)=>entry.media||(typeof model?.media==='function'?model.media(entry.source_id):null)||(model?.footage?.recordings||[]).find(r=>String(r.id)===String(entry.source_id))?.media||null;
 /* The waits behind Queue time: stated on the events, or paired from joining the queue to reaching the till. */
 function waitsOf(tile){
  const events=Array.isArray(tile.events)?tile.events:[],stated=events.map(e=>e.duration_s).filter(finite);
  if(stated.length)return stated;
  const open=new Map(),paired=[];
  for(const event of [...events].sort((a,b)=>(seconds(a.at)??a.time_s??0)-(seconds(b.at)??b.time_s??0))){
   const who=`${event.source_id}/${event.visitor_id}`,at=seconds(event.at)??event.time_s;
   if(missing(event.visitor_id)||!finite(at))continue;
   if(QUEUE_IN.has(event.type)&&!open.has(who))open.set(who,at);
   else if(QUEUE_OUT.has(event.type)&&open.has(who)){const start=open.get(who);if(at>=start)paired.push(at-start);open.delete(who);}
  }
  return paired;
 }
 function distributionOf(tile){
  if(Array.isArray(tile.distribution)&&tile.distribution.length)return tile.distribution;
  const waited=waitsOf(tile);
  if(!waited.length)return [];
  const buckets=[['Under 1 min',0,60],['1 to 2 min',60,120],['2 to 3 min',120,180],['3 to 5 min',180,300],['Over 5 min',300,Infinity]];
  return buckets.map(([label,from,to])=>({label,value:waited.filter(s=>s>=from&&s<to).length}));
 }
 /* The shelves behind Empty shelves: stated on the tile, or paired from each shelf's own events. */
 function shelvesOf(tile){
  if(Array.isArray(tile.shelves))return tile.shelves;
  const events=Array.isArray(tile.events)?tile.events:[],rows=[],open=new Map();
  for(const event of [...events].sort((a,b)=>(seconds(a.at)??a.time_s??0)-(seconds(b.at)??b.time_s??0))){
   const shelf=`${event.source_id}/${event.item??event.item_label??event.description??''}`;
   if(SHELF_EMPTY.has(event.type)){const row={id:event.id,label:event.item_label||event.description||event.label||'',description:event.item_label?event.description:'',thumb:event.thumb,media:event.media,empty_since:event.at??event.time_s,replenished_at:null,source_id:event.source_id,visitor_id:null,target:event.target,time_s:event.time_s};rows.push(row);open.set(shelf,row);}
   else if(SHELF_FILLED.has(event.type)&&open.has(shelf)){open.get(shelf).replenished_at=event.at??event.time_s;open.delete(shelf);}
  }
  return rows.reverse();
 }
 const isShelfEvent=event=>SHELF_EMPTY.has(event.type)||SHELF_FILLED.has(event.type);

 /* ---- the tracker's canvas record --------------------------------------------------------------- */
 const capital=text=>String(text||'').replace(/^./,c=>c.toUpperCase());
 const measuredIn=series=>(Array.isArray(series)?series:[]).filter(p=>{const v=p!==null&&typeof p==='object'?p.value:p;return finite(v)||typeof v==='string'&&v.trim()!==''&&Number.isFinite(Number(v));}).length;
 const clock=(at,model)=>{
  if(typeof at!=='string'||!/^\d{4}-\d{2}-\d{2}T/.test(at)||!Number.isFinite(Date.parse(at)))return '';
  const options={hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false};if(model?.timezone)options.timeZone=model.timezone;
  try{return new Intl.DateTimeFormat('en-GB',options).format(new Date(at));}catch{return '';}
 };
 function moneyText(amount,currency,model){
  if(missing(amount)||!Number.isFinite(Number(amount)))return '';
  const n=Number(amount),code=[currency,model?.currency].find(c=>typeof c==='string'&&/^[A-Z]{3}$/.test(c));
  if(code){try{return new Intl.NumberFormat('en',{style:'currency',currency:code,minimumFractionDigits:2,maximumFractionDigits:2}).format(n);}catch{}}
  return count(n,2);
 }
 const canvasEvent=row=>({...row,time_s:finite(row.time_s)?row.time_s:finite(row.at_s)?row.at_s:undefined});
 /* A shelf as the sheet lists it: its state now, when it went empty and when it was replenished. */
 function canvasShelf(shelf){
  const spans=(Array.isArray(shelf.intervals)?shelf.intervals:[]).filter(i=>i&&i.empty_since).sort((a,b)=>String(a.empty_since).localeCompare(String(b.empty_since)));
  const last=spans[spans.length-1]||null,empty=shelf.state?shelf.state==='empty':Boolean(shelf.empty_since)&&missing(shelf.replenished_at);
  const back=empty?null:shelf.replenished_at??last?.replenished_at??null;
  return {id:shelf.shelf??shelf.id??null,label:shelf.label||'',description:shelf.camera||shelf.description||'',thumb:shelf.thumb||null,media:shelf.media||null,
   source_id:shelf.source_id??null,visitor_id:null,target:shelf.target,time_s:shelf.time_s,end_s:shelf.end_s,
   state:empty?'empty':back?'replenished':'stocked',empty_since:empty?shelf.empty_since??last?.empty_since??null:last?.empty_since??null,replenished_at:back};
 }
 const shelfOrder=shelf=>({empty:0,replenished:1,stocked:2}[shelf.state]??1);
 /* The tile as the sheet reads it. A canvas record fills what the sheet shows under the chart. */
 function resolve(tile){
  const canvas=tile&&tile.canvas;
  if(!canvas||typeof canvas!=='object')return tile;
  const kind=kindOf(tile),rows=Array.isArray(canvas.rows)?canvas.rows.filter(r=>r&&typeof r==='object'):[],next={...tile};
  if(missing(tile.summary)&&typeof canvas.summary==='string'&&canvas.summary.trim())next.summary=canvas.summary.trim();
  if(waits(tile)){
   if(missing(tile.needs)&&canvas.needs)next.needs=canvas.needs;
   if(next.summary&&!tile.connect?.body)next.connect={...(tile.connect||{}),body:next.summary};
   return next;
  }
  if(measuredIn(tile.series)<2){const longer=[canvas.series,canvas.history].find(series=>measuredIn(series)>1);if(longer)next.series=longer;}
  if(kind==='money'){
   const groups=rows.filter(r=>Array.isArray(r.lines));
   if(groups.length){next.groups=groups;next.events=[];}else if(rows.length)next.events=rows.map(canvasEvent);
   next.estimated=Boolean(tile.estimated)||canvas.basis==='estimated';
   if(!Array.isArray(tile.figures)){
    const made=/sale|revenue/.test(String(tile.key||''))?[['Transactions',canvas.transactions??canvas.total_rows],['Items sold',canvas.units]]:[['Baskets',canvas.baskets??canvas.total_rows],['Items',canvas.units]];
    next.figures=made.filter(([,value])=>finite(value)).map(([label,value])=>({label,value}));
   }
  }else if(kind==='queue'){
   if(Array.isArray(canvas.distribution)&&canvas.distribution.length)next.distribution=canvas.distribution.map(band=>({label:capital(band.label??band.band),value:band.value}));
   if(rows.length)next.events=rows.map(row=>{
    const from=clock(row.start_at,null),to=clock(row.end_at??row.at,null);
    return {...canvasEvent(row),duration_s:finite(row.wait_s)?row.wait_s:row.duration_s,label:finite(row.wait_s)?`Waited ${spanText(row.wait_s)}`:row.label,range:[row.start_at,row.end_at??row.at],description:from&&to?'':row.description};
   });
   if(!Array.isArray(tile.figures)&&finite(canvas.longest_s))next.figures=[{label:'Longest wait',value:canvas.longest_s,unit:'s'},{label:'Waits',value:canvas.total_rows??rows.length}].filter(f=>finite(f.value));
  }else if(kind==='shelves'){
   if(Array.isArray(canvas.shelves)){next.shelves=canvas.shelves.filter(s=>s&&typeof s==='object').map(canvasShelf).sort((a,b)=>shelfOrder(a)-shelfOrder(b)||String(b.replenished_at||b.empty_since||'').localeCompare(String(a.replenished_at||a.empty_since||'')));next.events=[];}
   else if(rows.length)next.events=rows.map(canvasEvent);
  }else if(kind==='rate'){
   const seen=Number(canvas.visitors),bought=Number(canvas.purchases);
   if(!Array.isArray(tile.figures)&&Number.isFinite(seen)&&Number.isFinite(bought)){
    next.figures=[{label:'Visitors',value:seen},{label:'Purchases',value:bought}];
    next.share=Math.max(0,Math.min(1,finite(canvas.ratio)?canvas.ratio:seen>0?bought/seen:0));
   }
  }else if(rows.length)next.events=rows.map(canvasEvent);
  return next;
 }
 function connectCopy(tile,model){
  const given=tile.connect||{},needs=String(tile.needs||'').trim(),thing=needs.replace(/^needs\s+/i,'')||'a source',label=tile.label||'This number',place=placeName(model);
  const known=/till|receipt|pos\b/i.test(thing)?['your till','receipt']:/queue/i.test(thing)?['a camera on the queue','person in line']:/shel/i.test(thing)?['a camera on the shelves','gap on a shelf']:/catalog|price/i.test(thing)?['your catalog','priced product']:null;
  return {
   statement:needs||'Needs a source',
   body:given.body||(known?`The Organization agent connects ${known[0]}. ${label} fills from the first ${known[1]}.`:`The Organization agent connects ${thing}. ${label} fills from there.`),
   action:given.action||`Connect ${thing}`,
   prompt:given.prompt||`Connect ${thing} for ${label}${place?` at ${place}`:''}.`
  };
 }

 /* ---- events out -------------------------------------------------------------------------------- */
 function send(type,detail,cancelable=false){
  const origin=current?.element&&current.element.isConnected?current.element:doc;
  const event=new root.CustomEvent(type,{bubbles:true,cancelable,detail});
  origin.dispatchEvent(event);
  return event;
 }
 function openSession(entry){
  const at=entry.at??entry.empty_since??entry.time_s??0;
  const detail={visitor_id:entry.visitor_id??null,source_id:entry.source_id??null,at,event_id:entry.id??null,tracker:current?.tile.key??null};
  if(!missing(entry.person_id))detail.person_id=entry.person_id;
  stopPreviews();
  const event=send('rocoto:open-session',detail,true);
  if(event.defaultPrevented||typeof root.ControlRoom?.openSession!=='function')return;
  const target=entry.target||(missing(detail.source_id)?null:{clipId:detail.source_id,personId:entry.person_id??detail.visitor_id,time_s:finite(entry.time_s)?entry.time_s:finite(at)?at:0});
  if(target)root.ControlRoom.openSession(target);
 }
 function openAgent(){
  const {tile,model}=current,copy=connectCopy(tile,model),detail={purpose:'organization',tracker:tile.key??null,needs:tile.needs||'',prompt:copy.prompt};
  const event=send('rocoto:open-organization-chat',detail,true);
  if(event.defaultPrevented||root.parent===root)return;
  try{root.parent.postMessage({type:'rocoto:open-organization-chat',...detail},root.location.origin);}catch{}
 }

 /* ---- the sheet's parts ------------------------------------------------------------------------- */
 const opens=entry=>Boolean(entry.target)||!missing(entry.source_id);
 const TAG='<span class="rocoto-history-tag">Estimated</span>';
 const pictured=list=>list.some(entry=>entry.thumb);
 function thumb(entry,list,index,model){
  const picture=entry.thumb?`<img src="${esc(entry.thumb)}" alt="" loading="lazy" decoding="async">`:`<span class="rocoto-history-thumb-rest">${icon('video')}</span>`;
  return `<span class="rocoto-history-thumb"${mediaOf(entry,model)?` data-preview="${list}:${index}"`:''}>${picture}</span>`;
 }
 /* One row of a list: a picture when the list has pictures, the copy, what stands at its end, and the way in. */
 function row(entry,list,index,model,middle,lead,attributes=''){
  const body=`${lead}${middle}${opens(entry)?icon('chevron-right'):''}`;
  return `<li${attributes}>${opens(entry)?`<button type="button" class="rocoto-history-row" data-row="${list}:${index}">${body}</button>`:`<div class="rocoto-history-row" data-row="${list}:${index}">${body}</div>`}</li>`;
 }
 function eventRows(events,tile,model){
  if(!events.length)return '';
  const pictures=pictured(events);
  return `<ol class="rocoto-history-rows"${pictures?'':' data-plain'}>${events.map((event,index)=>{
   const time=event.time||moment(event.at,model,true)||(finite(event.time_s)?moment(event.time_s):'');
   const from=Array.isArray(event.range)?clock(event.range[0],model):'',to=Array.isArray(event.range)?clock(event.range[1],model):'';
   const detail=from&&to?`${from} to ${to}`:event.description;
   return row(event,'events',index,model,`<span class="rocoto-history-copy"><strong>${esc(event.label||tile.label||'')}</strong>${detail?`<span>${esc(detail)}</span>`:''}</span>${time?`<time>${esc(time)}</time>`:''}`,pictures?thumb(event,'events',index,model):'');
  }).join('')}</ol>`;
 }
 function shelfRows(shelves,model){
  if(!shelves.length)return '';
  const now=model?.now?(finite(model.now)?model.now:Date.parse(model.now)):Date.now(),pictures=pictured(shelves);
  return `<ol class="rocoto-history-rows rocoto-history-shelves"${pictures?'':' data-plain'}>${shelves.map((shelf,index)=>{
   const since=moment(shelf.empty_since,model,false),back=missing(shelf.replenished_at)?'':moment(shelf.replenished_at,model,false),from=typeof shelf.empty_since==='string'?Date.parse(shelf.empty_since):NaN;
   const empty=shelf.state?shelf.state==='empty':!back,lasting=empty?shelf.empty_for||(Number.isFinite(from)&&Number.isFinite(now)&&now>from?spanText((now-from)/1000):''):'';
   const state=back?`<span>Replenished <b>${esc(back)}</b></span>`:lasting?`<span class="is-open">Empty for <b>${esc(lasting)}</b></span>`:'';
   const times=since||state?`${since?`<span>Empty since <b>${esc(since)}</b></span>`:''}${state}`:'<span>Stocked</span>';
   return row(shelf,'shelves',index,model,`<span class="rocoto-history-copy"><strong>${esc(shelf.label||'Shelf')}</strong>${shelf.description?`<span>${esc(shelf.description)}</span>`:''}</span><span class="rocoto-history-times">${times}</span>`,
    pictures?thumb(shelf,'shelves',index,model):'<span class="rocoto-history-dot" aria-hidden="true"></span>',` data-empty="${empty?'true':'false'}"`);
  }).join('')}</ol>`;
 }
 /* Basket and Sales: each basket or sale with its lines: product, quantity and price. The tag on an estimated
  * total covers its lines; a line carries its own when its basis differs from its basket's. */
 function groupRows(groups,tile,model){
  if(!groups.length)return '';
  return `<ol class="rocoto-history-groups">${groups.map((group,index)=>{
   const time=group.time||moment(group.at,model,true),total=moneyText(group.total??group.amount,group.currency,model),lines=(Array.isArray(group.lines)?group.lines:[]).filter(line=>line&&typeof line==='object');
   const head=`<span class="rocoto-history-copy"><strong>${esc(group.label||tile.label||'')}</strong>${time?`<span>${esc(time)}</span>`:''}</span>${total?`<span class="rocoto-history-amount"><strong>${esc(total)}</strong>${group.basis==='estimated'?TAG:''}</span>`:''}${opens(group)?icon('chevron-right'):''}`;
   const items=lines.map(line=>{
    const quantity=Number(line.quantity)||1,each=moneyText(line.unit_price,line.currency??group.currency,model);
    const amount=moneyText(line.amount??(finite(line.unit_price)?line.unit_price*quantity:null),line.currency??group.currency,model);
    return `<li>${line.image?`<span class="rocoto-history-product"><img src="${esc(line.image)}" alt="" loading="lazy" decoding="async"></span>`:'<span class="rocoto-history-product" aria-hidden="true"></span>'}<span class="rocoto-history-line-name">${esc(line.product||line.label||'')}</span><span class="rocoto-history-line-quantity">${esc(each?`${count(quantity)} × ${each}`:`× ${count(quantity)}`)}</span>${amount?`<span class="rocoto-history-amount"><strong>${esc(amount)}</strong>${line.basis==='estimated'&&group.basis!=='estimated'?TAG:''}</span>`:''}</li>`;
   }).join('');
   return `<li>${opens(group)?`<button type="button" class="rocoto-history-row rocoto-history-group" data-row="groups:${index}">${head}</button>`:`<div class="rocoto-history-row rocoto-history-group" data-row="groups:${index}">${head}</div>`}${items?`<ul class="rocoto-history-lines">${items}</ul>`:''}</li>`;
  }).join('')}</ol>`;
 }
 const firstRow=tile=>`<div class="rocoto-history-first"><span class="rocoto-history-thumb is-open" aria-hidden="true"></span><p>${esc(tile.empty||'The first one lands here.')}</p></div>`;
 function figures(tile){
  const list=Array.isArray(tile.figures)?tile.figures:[];
  if(!list.length)return '';
  const share=finite(tile.share)?`<div class="rocoto-history-share" aria-hidden="true"><span style="width:${Math.round(Math.max(0,Math.min(1,tile.share))*1000)/10}%"></span></div>`:'';
  return `<dl class="rocoto-history-figures">${list.map(figure=>`<div><dt>${esc(figure.label||'')}</dt><dd><strong>${numeralHtml(figure)}</strong>${figure.estimated?TAG:''}</dd></div>`).join('')}</dl>${share}`;
 }
 function periodToggle(tile,model){
  const periods=periodsOf(model);
  if(periods.length<2)return '';
  const chosen=periods.some(p=>p.key===tile.period)?tile.period:periods[0].key;
  return `<div class="rocoto-history-period" role="radiogroup" aria-label="Period">${periods.map(p=>`<button type="button" role="radio" data-period="${esc(p.key)}" aria-checked="${p.key===chosen}" tabindex="${p.key===chosen?0:-1}">${esc(p.label)}</button>`).join('')}</div>`;
 }
 /* What the sheet shows for this tile; the lists it draws are kept so a row finds its record again. */
 function compose(tile,model){
  const kind=kindOf(tile),lists={events:[],shelves:[],groups:[]};
  if(waits(tile)){
   const copy=connectCopy(tile,model);
   return {lists,html:`<p class="rocoto-history-statement">${esc(copy.statement)}</p><div class="rocoto-history-ghost" data-ghost></div><div class="rocoto-history-connect"><p>${esc(copy.body)}</p><button type="button" class="rocoto-history-action" data-agent>${esc(copy.action)}${icon('arrow-right')}</button></div>`};
  }
  const series=Array.isArray(tile.series)?tile.series:[],measured=series.filter(p=>root.RocotoCharts?.geometry.valueOf(p)!==null).length;
  const values=series.map(p=>root.RocotoCharts?.geometry.valueOf(p)).filter(v=>v!==null&&v!==undefined),level=values.length>1&&values.every(v=>v===values[0]);
  const all=Array.isArray(tile.events)?tile.events:[],bars=kind==='queue'?distributionOf(tile):[];
  lists.shelves=kind==='shelves'?shelvesOf(tile):[];
  lists.events=kind==='shelves'&&!Array.isArray(tile.shelves)?all.filter(event=>!isShelfEvent(event)):all;
  lists.groups=Array.isArray(tile.groups)?tile.groups:[];
  const summary=typeof tile.summary==='string'&&tile.summary.trim()?`<p class="rocoto-history-summary">${esc(tile.summary.trim())}</p>`:'';
  const made=[summary,figures(tile),bars.length?'<div class="rocoto-history-bars" data-bars></div>':'',shelfRows(lists.shelves,model),groupRows(lists.groups,tile,model),eventRows(lists.events,tile,model)].filter(Boolean);
  if(made.length===(summary?1:0)&&['count','queue','shelves'].includes(kind)&&!Number(tile.value))made.push(firstRow(tile));
  const tags=`${tile.estimated?TAG:''}${tile.state==='paused'?'<span class="rocoto-history-tag">Paused</span>':''}`;
  return {lists,bars,tools:periodToggle(tile,model),html:`<p class="rocoto-history-numeral"${numeralText(tile).length>9?' data-long':''}><strong>${numeralHtml(tile)}</strong>${tags}</p>${measured>1?`<div class="rocoto-history-chart" data-chart${level?' data-level':''}></div>`:''}${made.length?`<div class="rocoto-history-made">${made.join('')}</div>`:''}`};
 }

 /* ---- thumbnails that play their clip range on hover or focus ------------------------------------ */
 const entryOf=key=>{const [list,index]=String(key||'').split(':');return current?.lists?.[list]?.[Number(index)]||null;};
 function stopPreviews(){
  current?.dialog.querySelectorAll('.rocoto-history-thumb video').forEach(video=>{video.dataset.playing='0';video.pause();video.closest('.rocoto-history-thumb').classList.remove('is-playing');});
 }
 function preview(rowNode,on){
  const host=rowNode.querySelector('[data-preview]');
  if(!host||!current)return;
  const entry=entryOf(host.dataset.preview),media=entry&&mediaOf(entry,current.model);
  let video=host.querySelector('video');
  if(!on||!media){if(video){video.dataset.playing='0';video.pause();host.classList.remove('is-playing');}return;}
  if(reducedMotion())return;
  const start=[entry.start_s,entry.time_s,entry.at].find(finite)??0,end=finite(entry.end_s)&&entry.end_s>start+.2?entry.end_s:start+4;
  if(!video){
   video=doc.createElement('video');video.muted=true;video.playsInline=true;video.preload='metadata';video.setAttribute('aria-hidden','true');video.tabIndex=-1;
   video.addEventListener('timeupdate',()=>{if(video.dataset.playing==='1'&&video.currentTime>=end-.04)video.currentTime=start;});
   video.addEventListener('ended',()=>{if(video.dataset.playing==='1'){video.currentTime=start;video.play().catch(()=>{});}});
   video.addEventListener('playing',()=>{if(video.dataset.playing==='1')host.classList.add('is-playing');});
   video.src=media;host.append(video);
  }
  video.dataset.playing='1';
  const begin=()=>{if(video.dataset.playing!=='1')return;try{video.currentTime=start;}catch{}video.play().catch(()=>{});};
  if(video.readyState>=1)begin();else video.addEventListener('loadedmetadata',begin,{once:true});
 }

 /* ---- drawing the sheet ------------------------------------------------------------------------- */
 function render(change){
  const {dialog,tile,model}=current,body=dialog.querySelector('.rocoto-history-body'),kind=kindOf(tile),waiting=waits(tile);
  stopPreviews();
  const view=compose(tile,model);
  current.lists=view.lists;
  dialog.dataset.kind=kind;dialog.dataset.state=waiting?'needs':'live';
  dialog.querySelector('.rocoto-history-title').textContent=tile.label||'';
  dialog.setAttribute('aria-label',waiting?`${tile.label||''}. ${connectCopy(tile,model).statement}`:`${tile.label||''}, ${numeralText(tile)}`);
  const tools=dialog.querySelector('.rocoto-history-tools'),held=doc.activeElement?.closest?.('[data-period]')?.dataset.period;
  tools.innerHTML=view.tools||'';tools.hidden=!view.tools;
  if(held)tools.querySelector(`[data-period="${held}"]`)?.focus();
  body.innerHTML=view.html;
  const charts=root.RocotoCharts,chartHost=body.querySelector('[data-chart]'),barsHost=body.querySelector('[data-bars]'),ghostHost=body.querySelector('[data-ghost]');
  current.chart=null;
  /* A small count keeps zero at its foot; a larger one is drawn over its own range, so its shape reads. */
  const peak=Math.max(0,...(Array.isArray(tile.series)?tile.series:[]).map(point=>Number(point!==null&&typeof point==='object'?point.value:point)||0));
  if(charts&&chartHost)current.chart=charts.line(chartHost,tile.series,{size:'large',area:true,hover:true,label:`${tile.label||''} over the period`,
   floor:kind==='queue'||kind==='rate'||kind==='money'?'auto':peak>10?'soft':0,format:value=>seriesText(tile,value),
   caption:point=>point&&typeof point==='object'?(point.label||day(point.t??point.at,model)):''});
  if(charts&&barsHost)charts.bars(barsHost,view.bars,{size:'large',highlight:'max',hover:true,label:'Waits by length'});
  if(charts&&ghostHost)charts.ghost(ghostHost,'line');
  if(change&&!reducedMotion()){dialog.classList.remove('has-arrived');void dialog.offsetWidth;dialog.classList.add('has-arrived');}
 }
 function choosePeriod(period,focus){
  if(!current||current.tile.period===period)return;
  const {model,tile}=current,key=tile.key;
  const apply=next=>{if(!current||current.tile.key!==key)return;current.tile=resolve(next&&typeof next==='object'?{...next,period}:inPeriod(current.source,period));render(false);if(focus)current.dialog.querySelector(`[data-period="${period}"]`)?.focus();};
  const answer=typeof model?.tileFor==='function'?model.tileFor(key,period):null;
  if(answer&&typeof answer.then==='function')answer.then(apply,()=>apply(null));else apply(answer);
  send('rocoto:tile-period',{key,period});
 }
 function build(surface){
  const dialog=doc.createElement('dialog');
  dialog.className=`rocoto-history${surface==='glass'?' twelvelabs-card-material':''}`;dialog.dataset.surface=surface;
  dialog.tabIndex=-1;
  dialog.innerHTML=`<div class="rocoto-history-inner"><header class="rocoto-history-head"><h2 class="rocoto-history-title"></h2><div class="rocoto-history-tools"></div><button type="button" class="rocoto-history-close" aria-label="Close">${icon('x')}</button></header><div class="rocoto-history-body"></div></div>`;
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  dialog.addEventListener('click',event=>{
   const target=event.target;
   if(target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)close();return;}
   if(target.closest('.rocoto-history-close')){close();return;}
   const period=target.closest('[data-period]');if(period){choosePeriod(period.dataset.period,false);return;}
   if(target.closest('[data-agent]')){openAgent();return;}
   const pressed=target.closest('button.rocoto-history-row'),entry=pressed&&entryOf(pressed.dataset.row);
   if(entry)openSession(entry);
  });
  dialog.addEventListener('keydown',event=>{
   const radio=event.target.closest?.('[data-period]');
   if(!radio||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
   event.preventDefault();
   const all=[...dialog.querySelectorAll('[data-period]')],at=all.indexOf(radio);
   const next=event.key==='Home'?0:event.key==='End'?all.length-1:(at+(event.key==='ArrowLeft'||event.key==='ArrowUp'?-1:1)+all.length)%all.length;
   choosePeriod(all[next].dataset.period,true);
  });
  /* A row plays its clip while the pointer or the keyboard is on it, and rests when either leaves the row. */
  for(const [type,on] of [['pointerover',true],['pointerout',false],['focusin',true],['focusout',false]])dialog.addEventListener(type,event=>{
   const rowNode=event.target.closest?.('.rocoto-history-row');
   if(rowNode&&!(event.relatedTarget instanceof root.Node&&rowNode.contains(event.relatedTarget)))preview(rowNode,on);
  });
  return dialog;
 }

 /* ---- growing out of the tile and returning into it --------------------------------------------- */
 const timing=(dialog,name,fallback)=>{const ms=root.RocotoCharts?.geometry.parseDuration(root.getComputedStyle(dialog).getPropertyValue(name),fallback);return finite(ms)?ms:fallback;};
 const curve=(dialog,name)=>root.getComputedStyle(dialog).getPropertyValue(name).trim()||'ease-out';
 function morph(dialog,element,opening){
  const inner=dialog.querySelector('.rocoto-history-inner');
  if(reducedMotion()||typeof dialog.animate!=='function')return Promise.resolve();
  const ms=timing(dialog,opening?'--history-open-duration':'--history-close-duration',opening?400:240),easing=curve(dialog,'--history-ease');
  const sheet=dialog.getBoundingClientRect(),tile=element&&element.isConnected?element.getBoundingClientRect():null;
  if(!tile||!tile.width||!tile.height){
   const frames=[{opacity:0,transform:'translateY(12px) scale(.985)'},{opacity:1,transform:'none'}];
   return dialog.animate(opening?frames:frames.reverse(),{duration:ms*.7,easing,fill:'both'}).finished.catch(()=>{}).then(()=>{if(opening)dialog.getAnimations().forEach(a=>a.cancel());});
  }
  const dx=tile.left+tile.width/2-(sheet.left+sheet.width/2),dy=tile.top+tile.height/2-(sheet.top+sheet.height/2);
  const radius=root.getComputedStyle(element).borderTopLeftRadius||'20px',settled=root.getComputedStyle(dialog).borderTopLeftRadius;
  const small={transform:`translate(${dx}px,${dy}px)`,width:`${tile.width}px`,height:`${tile.height}px`,borderRadius:radius},large={transform:'translate(0px,0px)',width:`${sheet.width}px`,height:`${sheet.height}px`,borderRadius:settled};
  /* The content keeps its settled size while the glass grows around it, so no line reflows on the way. */
  inner.style.width=`${sheet.width}px`;inner.style.height=`${sheet.height}px`;inner.style.flex='none';
  let echo=null;
  if(opening){
   /* The tile's own face fades out of the glass as the sheet's content fades in. */
   echo=element.cloneNode(true);echo.removeAttribute('id');echo.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));
   echo.removeAttribute('data-history-origin');echo.classList.add('rocoto-history-echo');echo.setAttribute('aria-hidden','true');echo.inert=true;echo.tabIndex=-1;
   echo.style.width=`${tile.width}px`;echo.style.height=`${tile.height}px`;dialog.append(echo);
   echo.animate([{opacity:1},{opacity:0}],{duration:ms*.36,easing:'linear',fill:'both'});
  }
  const fade=opening?[{opacity:0,offset:0},{opacity:0,offset:.2},{opacity:1,offset:.9},{opacity:1,offset:1}]:[{opacity:1,offset:0},{opacity:0,offset:.45},{opacity:0,offset:1}];
  inner.animate(fade,{duration:ms,easing:'linear',fill:'both'});
  return dialog.animate(opening?[small,large]:[large,small],{duration:ms,easing,fill:'both'}).finished.catch(()=>{}).then(()=>{
   echo?.remove();
   if(opening){dialog.getAnimations().forEach(a=>a.cancel());inner.getAnimations().forEach(a=>a.cancel());inner.style.width='';inner.style.height='';inner.style.flex='';}
  });
 }
 function open(tile,model,element,opts){
  if(!doc||!tile||typeof tile!=='object')return null;
  if(current)finish(false);
  const asked=opts?.surface||element?.closest?.('[data-surface]')?.dataset.surface;
  const surface=asked==='paper'||asked==='glass'?asked:element?.closest?.('.twelvelabs-card-material')?'glass':element?'paper':'glass';
  const dialog=build(surface),periods=periodsOf(model),period=[tile.period,model?.period].find(p=>typeof p==='string'&&p)||periods[0]?.key;
  current={tile:resolve({...tile,period}),source:tile,model:model||{},element:element||null,dialog,chart:null,lists:{events:[],shelves:[],groups:[]},closing:false,returnTo:element||doc.activeElement};
  doc.body.append(dialog);
  dialog.showModal();
  dialog.focus({preventScroll:true});
  render(false);
  if(element){element.setAttribute('aria-expanded','true');element.setAttribute('data-history-origin','');}
  send('rocoto:tile-open',{key:tile.key??null});
  current.opened=morph(dialog,element,true);
  return dialog;
 }
 function finish(restore){
  if(!current)return;
  const {dialog,element,returnTo,tile}=current;
  stopPreviews();
  if(element){element.removeAttribute('data-history-origin');element.setAttribute('aria-expanded','false');}
  send('rocoto:tile-close',{key:tile.key??null});
  current=null;
  if(dialog.open)dialog.close();
  dialog.remove();
  if(restore&&returnTo&&returnTo.isConnected&&typeof returnTo.focus==='function')returnTo.focus({preventScroll:true});
 }
 function close(){
  if(!current)return Promise.resolve();
  if(current.closing)return current.closing;
  const session=current;session.dialog.classList.add('is-closing');
  session.closing=morph(session.dialog,session.element,false).then(()=>{if(current===session)finish(true);});
  return session.closing;
 }
 function update(tile){
  if(!current||!tile||tile.key!==current.tile.key)return false;
  const before=numeralText(current.tile),period=current.tile.period;
  /* The reader's place: how far the sheet's list is scrolled and which row, or the chart, holds the focus. */
  const body=current.dialog.querySelector('.rocoto-history-body'),scrolled=()=>body.querySelector('.rocoto-history-made'),top=scrolled()?.scrollTop||0,held=doc.activeElement&&body.contains(doc.activeElement)?doc.activeElement:null;
  const place=held?(held.dataset.row?`[data-row="${held.dataset.row}"]`:held.getAttribute('role')==='slider'?'[role="slider"]':held.matches('[data-agent]')?'[data-agent]':''):'';
  current.source=tile;current.tile=resolve(typeof tile.period==='string'&&tile.period&&tile.period!==period?{...tile}:inPeriod(tile,period));
  render(before!==numeralText(current.tile)&&!waits(current.tile));
  const list=scrolled();if(list&&list.scrollTop!==top)list.scrollTop=top;
  if(place)(body.querySelector(place)||current.dialog).focus({preventScroll:true});
  return true;
 }

 const api=Object.freeze({open,close,update,get isOpen(){return Boolean(current);},get key(){return current?.tile.key??null;},get period(){return current?.tile.period??null;}});
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 root.RocotoHomeHistory=api;
})(typeof window!=='undefined'?window:globalThis);
