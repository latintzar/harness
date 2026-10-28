/* Home · instrument tiles on the live hero.
 * The shop's footage is the stage. The Core Trackers stand on it and under it as tiles: one numeral, one mark,
 * one state. A tile is a button: it opens its history (RocotoHomeHistory). Numbers come from one view model
 * (RocotoHomeModel) that the bundle reader and the maintained reader both feed, and a number that arrives
 * changes its tile in place. The public interface is the one the page already calls:
 * create / show / hide / setData / setLocation / destroy. */
(function(global){
'use strict';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=value=>Number.isFinite(Number(value))&&value!==null&&value!==''?Number(value):null;
const clock=value=>{const n=Math.max(0,Number(value)||0);return `${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`;};
const reducedMotion=()=>!!global.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const icon=name=>`<svg class="home-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><use href="kit/lucide.svg#${name}"></use></svg>`;
const visitorName=id=>{const tracker=/^(?:Person\s*)?P?(\d+)$/i.exec(String(id??'').trim());return tracker?`Visitor ${tracker[1]}`:'Visitor';};
const boards=[['handled','Most handled','interactions','First handled product appears here'],['picked_up','Most picked up','pickups','First pick-up appears here'],['put_back','Most put back','putbacks','First put-back appears here']];
let active=null;

function create(options){
 const host=options.root;if(!host)throw new Error('LocationView requires a root element');
 const Model=global.RocotoHomeModel;if(!Model)throw new Error('LocationView requires RocotoHomeModel');
 let data=options.data||{locations:[]},place=null,neighbourhood=null,neighbourhoodAsked=false,period='week',disposed=false,shown=false,suspended=false,agents=null,agentsAsked=0,model=null;
 let previews=[],heroKey='',heroIndex=0,tagFrame=0,quiet=true,openStamp='',boardKey=null;
 const cleanups=[],tiles=new Map(),traces=new Map(),tags=new Map();
 const feed=()=>agents&&!Array.isArray(data?.agents)?{...data,agents}:data;
 const compute=()=>Model.from(feed(),{period});
 model=compute();

 host.innerHTML=`<section class="location-view home">
<section class="home-stage" id="locationStage">
 <div class="home-footage" id="locationHeroVisual"><img id="locationHeroImage" alt="" loading="eager"><video id="locationHeroVideo" muted playsinline preload="none" disablepictureinpicture tabindex="-1"></video></div>
 <div class="home-veil"></div>
 <div class="home-tags" id="locationTags"></div>
 <div class="home-place">
  <h1 id="locationViewName"></h1>
  <p id="locationHeroAddress" class="home-address"></p>
  <button class="home-live" id="locationOpenCameras" type="button"><span class="home-live-dot" aria-hidden="true"></span>View live${icon('video')}</button>
 </div>
 <div class="home-period" role="radiogroup" aria-label="Period">${model.periods.map(p=>`<button type="button" role="radio" aria-checked="${p.key===period}" tabindex="${p.key===period?0:-1}" data-global-period="${p.key}">${p.label}</button>`).join('')}</div>
 <div id="locationStageTiles" class="home-instruments" data-surface="glass"></div>
</section>
<div id="locationPageTiles" class="home-tiles" data-surface="paper"></div>
<div class="home-lower">
 <section class="home-pane home-products" aria-labelledby="locationProductsTitle">
  <header class="home-pane-heading"><h2 id="locationProductsTitle">Products</h2><button id="locationCatalogButton" class="home-text-button" type="button">Set up catalog</button></header>
  <div id="locationProducts" class="home-boards"></div>
 </section>
 <section class="home-pane home-neighbourhood" aria-labelledby="locationHoodTitle">
  <header class="home-pane-heading"><h2 id="locationHoodTitle">Neighbourhood</h2><button id="locationAddressButton" class="home-text-button" type="button">Set address</button></header>
  <div id="locationMapHost" class="home-map"><button id="locationSetAddress" class="home-map-empty" type="button">Choose the shop’s address</button></div>
  <div id="locationNeighbourhoodSummary" class="home-hood" aria-live="polite"></div>
 </section>
</div>
<dialog class="home-dialog location-product-dialog" id="locationProductDialog" aria-labelledby="locationProductDetailTitle"><header class="home-dialog-heading"><div id="locationProductDetailTitle"></div><button id="locationProductClose" class="home-dialog-close" type="button" aria-label="Close product history">${icon('x')}</button></header><div class="location-product-history" id="locationProductHistory"></div></dialog>
<dialog class="home-dialog home-tile-dialog" id="locationMetricDialog" aria-labelledby="locationMetricTitle"><header class="home-dialog-heading"><div><h2 id="locationMetricTitle"></h2></div><button id="locationMetricClose" class="home-dialog-close" type="button" aria-label="Close history">${icon('x')}</button></header><div id="locationMetricHistory" class="home-tile-history"></div></dialog>
<dialog class="home-dialog location-catalog-dialog" id="locationCatalogDialog" aria-labelledby="locationCatalogTitle"><header class="home-dialog-heading"><div><h2 id="locationCatalogTitle">Set up catalog</h2><p>Connect product references to this shop.</p></div><button id="locationCatalogClose" class="home-dialog-close" type="button" aria-label="Close catalog setup">${icon('x')}</button></header><div id="locationCatalogBody" class="location-catalog-body"><p>Loading catalog setup…</p></div></dialog>
</section>`;
 const q=selector=>host.querySelector(selector);
 function listen(element,type,handler,opts){element?.addEventListener(type,handler,opts);cleanups.push(()=>element?.removeEventListener(type,handler,opts));}

 /* ── the stage: the shop's own footage, its name, its tags ─────────────────────────────────── */
 function renderPlace(){
  const name=place?.name||model.place.name,address=place?.address||place?.display_name||model.place.address,title=q('#locationViewName'),line=q('#locationHeroAddress');
  /* The shop's own name leads. A shop that has only its address shows the address as its name. */
  title.textContent=name||address||'Your shop';line.textContent=name?address:'';line.hidden=!line.textContent;
  q('#locationStage').setAttribute('aria-label',name||address||'Your shop');
  const button=q('#locationAddressButton');button.hidden=!!place;
  if(!neighbourhoodAsked&&!disposed&&(address||model.place.address)){
   neighbourhoodAsked=true;const loc=data?.locations?.[0]||data?.location||{};
   // A location names its neighbourhood record when it has one; the map picture stands on its own.
   if(loc.neighbourhood_url)fetch(loc.neighbourhood_url,{credentials:'same-origin'}).then(response=>response.ok?response.json():null).then(next=>{if(disposed||!next)return;neighbourhood=next;renderNeighbourhood();}).catch(()=>{});
  }
 }
 const recordings=()=>model.footage.recordings;
 function renderFootage(){
  /* The stage holds a picture only while Home shows: a hidden Home decodes nothing. */
  if(!shown)return;
  const list=recordings(),poster=model.footage.poster||(list.length?'':'location-images/store-exterior.png'),image=q('#locationHeroImage'),video=q('#locationHeroVideo'),visual=q('#locationHeroVisual');
  if(poster){if(image.getAttribute('src')!==poster)image.src=poster;visual.classList.add('has-image');}else{image.removeAttribute('src');visual.classList.remove('has-image');}
  const key=list.map(r=>r.media).join('|');
  if(key!==heroKey){heroKey=key;heroIndex=0;video.classList.remove('is-playing');clearTags();if(list.length){video.preload='auto';video.src=list[0].media;video.loop=list.length===1;}else release();}
  play();
 }
 function release(){const video=q('#locationHeroVideo');video.pause();video.classList.remove('is-playing');if(video.hasAttribute('src')){video.removeAttribute('src');video.load();}heroKey='';clearTags();}
 const canPlay=()=>shown&&!disposed&&!suspended&&!global.document.hidden&&!reducedMotion();
 function play(){const video=q('#locationHeroVideo');if(!video.getAttribute('src'))return;if(!canPlay()){video.pause();return;}const started=video.play();if(started&&typeof started.catch==='function')started.catch(()=>{});if(!tagFrame)tagLoop();}
 function rest(){q('#locationHeroVideo').pause();}
 function nextRecording(){const list=recordings(),video=q('#locationHeroVideo');if(list.length<2||!shown)return;heroIndex=(heroIndex+1)%list.length;clearTags();video.src=list[heroIndex].media;play();}
 /* The tracker's own boxes for the recording on the stage: the bundle hands them over, a maintained
  * recording names where its geometry lives. */
 function traceOf(recording){
  if(!recording)return null;if(recording.trace)return recording.trace;
  if(!recording.tracks_url)return null;
  if(!traces.has(recording.tracks_url)){traces.set(recording.tracks_url,null);fetch(recording.tracks_url,{credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(value=>{if(!disposed&&value?.frames)traces.set(recording.tracks_url,value);}).catch(()=>{});}
  return traces.get(recording.tracks_url);
 }
 function clearTags(){tags.forEach(tag=>tag.remove());tags.clear();}
 /* A tag on each tracked visitor: their name on a hairline, standing on the top of the tracker's box. */
 function drawTags(){
  const layer=q('#locationTags'),video=q('#locationHeroVideo'),recording=recordings()[heroIndex],trace=traceOf(recording);
  if(!shown||!trace?.frames||!video.videoWidth||video.readyState<2){tags.forEach(tag=>tag.classList.remove('is-seen'));return;}
  const fps=Number(trace.fps)||24,frame=Math.round(video.currentTime*fps),items=trace.frames[frame]||trace.frames[frame-1]||trace.frames[frame+1]||[];
  const box=layer.getBoundingClientRect();if(!box.width)return;
  const width=Number(trace.width)||video.videoWidth,height=Number(trace.height)||video.videoHeight,scale=Math.max(box.width/width,box.height/height),style=global.getComputedStyle(video),focus=style.objectPosition.split(' ').map(parseFloat);
  const left=(box.width-width*scale)*(Number.isFinite(focus[0])?focus[0]:50)/100,top=(box.height-height*scale)*(Number.isFinite(focus[1])?focus[1]:50)/100;
  const keep=['.home-place','.home-period','.home-instruments'].map(selector=>{const r=q(selector).getBoundingClientRect();return {left:r.left-box.left-52,right:r.right-box.left+52,top:r.top-box.top-8,bottom:r.bottom-box.top+34};});
  const seen=new Set();
  for(const item of items){
   if(!Array.isArray(item.box))continue;const id=String(item.id),x=left+(item.box[0]+item.box[2])/2*scale,y=top+item.box[1]*scale;
   if(x<56||x>box.width-56||y<44||y>box.height-24||keep.some(r=>x>r.left&&x<r.right&&y>r.top&&y<r.bottom))continue;
   seen.add(id);let tag=tags.get(id);
   if(!tag){
    tag=global.document.createElement('button');tag.type='button';tag.className='home-tag';tag.tabIndex=-1;tag.innerHTML='<span class="home-tag-name"></span><i aria-hidden="true"></i>';tag.firstChild.textContent=visitorName(id);tag.setAttribute('aria-label',`${visitorName(id)}. Open session`);
    tag.addEventListener('click',()=>options.onOpenSession?.({clipId:tag.dataset.clip,personId:id,time_s:Number(tag.dataset.time)||0}));layer.append(tag);tags.set(id,tag);
   }
   tag.dataset.clip=recording.id;tag.dataset.time=String(video.currentTime);tag.style.transform=`translate(${x.toFixed(1)}px,${y.toFixed(1)}px)`;tag.classList.add('is-seen');
  }
  tags.forEach((tag,id)=>{if(!seen.has(id))tag.classList.remove('is-seen');});
 }
 function tagLoop(){tagFrame=0;if(disposed||!shown)return;drawTags();if(canPlay())tagFrame=global.requestAnimationFrame(tagLoop);}

 /* ── tiles ─────────────────────────────────────────────────────────────────────────────────── */
 /* A mark whose binding the tracker's output leaves unfilled (the row's `problems`) rests as its ghost, and the tile says what it needs. */
 const markNeeds=tile=>(Array.isArray(tile.problems)?tile.problems:[]).filter(p=>p&&typeof p==='object'&&p.block==='tile'&&typeof p.needs==='string'&&p.needs).map(p=>p.needs)[0]||'';
 const spoken=tile=>tile.state==='needs'?`${tile.label}. ${tile.needs}`:tile.state==='paused'?`${tile.label}. Paused`:`${tile.label}, ${tile.text}${tile.unit==='%'?'%':tile.unit?` ${tile.unit}`:''}${tile.estimated?', estimated':''}. ${markNeeds(tile)?`The mark ${markNeeds(tile)[0].toLowerCase()}${markNeeds(tile).slice(1)} `:''}Open history`;
 function drawMark(node,tile){
  const rests=!!markNeeds(tile),slot=node.querySelector('.home-tile-mark'),signature=JSON.stringify([tile.state,tile.mark,tile.fraction,rests,tile.series.map(p=>p.value)]);
  /* a mark is drawn in its own box: a tile that has no layout yet draws when Home shows */
  if(!slot.clientWidth||!slot.clientHeight){delete slot.dataset.signature;return;}
  if(slot.dataset.signature===signature)return;slot.dataset.signature=signature;
  const surface=tile.stage?'glass':'paper',waiting=tile.state==='needs'||tile.state==='paused',opts={surface,label:tile.label,size:tile.stage?'tile':'spark',hover:false},own=global.RocotoHomeMarks;
  const draw=charts=>{
   if(!charts)return false;
   if((waiting||rests)&&typeof charts.ghost==='function'){charts.ghost(slot,tile.mark,opts);return true;}
   if(tile.mark==='ring'){charts.ring(slot,tile.fraction||0,opts);return true;}
   if(tile.mark==='arc'){if(typeof charts.arc==='function')charts.arc(slot,tile.fraction||0,opts);else charts.ring(slot,tile.fraction||0,{...opts,arc:true});return true;}
   /* Bars: one per point of the series, the newest lit. A tile holds the newest twelve, so each bar keeps its width. */
   if(tile.mark==='bars'&&typeof charts.bars==='function'&&tile.series.length>1){const shown=tile.series.slice(-12);charts.bars(slot,shown,{...opts,size:'tile',gap:shown.length>8?3:4});return true;}
   /* A count at zero, or a number with one point, rests as a level line. */
   const points=tile.series.length>1?tile.series:[{t:'',value:0},{t:'',value:0}];
   (tile.stage||typeof charts.spark!=='function'?charts.line:charts.spark)(slot,points,opts);return true;
  };
  try{if(draw(global.RocotoCharts))return;}catch{slot.replaceChildren();}
  try{draw(own?{...own,line:(el,points,o)=>own.line(el,points,{...o,tone:o.surface}),ring:(el,share,o)=>own.ring(el,share,{...o,tone:o.surface})}:null);}catch{slot.replaceChildren();}
 }
 function tileNode(tile){
  const node=global.document.createElement('button');node.type='button';node.className=`home-tile${tile.stage?' home-instrument twelvelabs-card-material':''}${tile.custom?' is-agent':''}`;node.dataset.tile=tile.key;
  node.innerHTML=`<span class="home-tile-title"></span><span class="home-tile-tag"></span><span class="home-tile-row"><span class="home-tile-figure"><strong class="home-tile-value"></strong><em class="home-tile-unit"></em></span><span class="home-tile-needs"></span><span class="home-tile-mark"></span></span>${icon('chevron-right')}`;
  node.addEventListener('click',()=>open(tile.key,node));
  return node;
 }
 function paintTile(node,tile,fresh){
  const value=node.querySelector('.home-tile-value'),previous=node.dataset.value,next=tile.state==='needs'||tile.state==='paused'?'':tile.text+tile.unit;
  node.dataset.state=tile.state;node.dataset.mark=tile.mark;node.dataset.surface=tile.stage?'glass':'paper';
  node.querySelector('.home-tile-title').textContent=tile.label;
  value.textContent=tile.state==='needs'||tile.state==='paused'?'':tile.text;node.querySelector('.home-tile-unit').textContent=tile.state==='needs'||tile.state==='paused'?'':tile.unit;
  node.querySelector('.home-tile-needs').textContent=tile.state==='needs'?tile.needs:tile.state==='paused'?'Paused':'';
  /* an estimated money figure says so, in the tile's top line where its numeral keeps its room */
  const estimated=!!(tile.estimated&&next);node.querySelector('.home-tile-tag').textContent=estimated?'Estimated':'';node.toggleAttribute('data-estimated',estimated);node.querySelector('.home-tile-figure').hidden=!next;
  node.setAttribute('aria-label',spoken(tile));node.dataset.value=next;
  fitUnit(node,tile);
  /* A resting mark is named where the pointer finds it, and in the tile's spoken name. */
  if(markNeeds(tile)){node.dataset.rests='';node.title=markNeeds(tile);}else if('rests' in node.dataset){delete node.dataset.rests;node.removeAttribute('title');}
  drawMark(node,tile);
  if(!fresh&&!quiet&&previous!==undefined&&previous!==next)tick(node);
 }
 /* The mark of a company tracker keeps its room. A unit that leaves the mark under MARK_FLOOR pixels, or pushes
  * it past the tile's row, steps aside: the number stands alone beside its mark, and the tile's spoken name keeps
  * the unit. A tile still to be laid out is fitted when Home shows. */
 const MARK_FLOOR=24;
 function fitUnit(node,tile){
  const unit=node.querySelector('.home-tile-unit'),slot=node.querySelector('.home-tile-mark'),row=node.querySelector('.home-tile-row');
  unit.hidden=false;
  if(tile.core||!unit.textContent||!row.clientWidth)return;
  if(slot.clientWidth<MARK_FLOOR||slot.getBoundingClientRect().right>row.getBoundingClientRect().right+.5)unit.hidden=true;
 }
 /* A number that arrives ticks once and lights its tile's edge for the flash duration. */
 function tick(node){
  if(reducedMotion())return;
  for(const name of ['has-ticked','has-arrived']){node.classList.remove(name);void node.offsetWidth;node.classList.add(name);}
 }
 function renderTiles(){
  const stage=q('#locationStageTiles'),page=q('#locationPageTiles'),wanted=new Set(model.tiles.map(t=>t.key));
  tiles.forEach((node,key)=>{if(!wanted.has(key)){node.remove();tiles.delete(key);}});
  let creator=page.querySelector('.home-tile-new');
  if(!creator){
   creator=global.document.createElement('button');creator.type='button';creator.className='home-tile home-tile-new';creator.innerHTML='<span class="home-tile-plus" aria-hidden="true"></span><span class="home-tile-title">New tracker</span>';
   creator.addEventListener('click',newTracker);page.append(creator);
  }
  const cursor={glass:0,paper:0};
  for(const tile of model.tiles){
   let node=tiles.get(tile.key);const fresh=!node,side=tile.stage?'glass':'paper',parent=tile.stage?stage:page;
   if(fresh){node=tileNode(tile);tiles.set(tile.key,node);}
   /* a tile keeps its node; it moves only when its place in the order changes */
   const here=parent.children[cursor[side]++]||null;if(here!==node)parent.insertBefore(node,here);
   paintTile(node,tile,fresh);
  }
  if(page.lastElementChild!==creator)page.append(creator);
  stage.style.setProperty('--count',String(Math.max(1,stage.children.length)));
  /* An open history follows its tile: in the period the sheet shows, and only when that tile changed, so a
   * refresh that brings the same numbers leaves the reader's scroll, focus and playing preview alone. */
  const history=global.RocotoHomeHistory;
  if(history?.isOpen&&typeof history.update==='function'){
   const own=typeof history.period==='string'&&history.period&&history.period!==model.period&&typeof model.tileFor==='function'?history.period:null;
   const open=own?model.tileFor(history.key,own):model.tile(history.key);
   if(open){const mark=stampOf(open);if(mark!==openStamp){openStamp=mark;try{history.update(open);}catch{}}}
  }else openStamp='';
 }
 /* What a full view shows of a tile: a new stamp means a new picture. */
 function stampOf(tile){return JSON.stringify([tile.key,tile.period,tile.revision??null,tile.presentation_revision??null,tile.value,tile.text,tile.unit,tile.state,tile.needs,tile.label,tile.mark,tile.kind,tile.estimated,tile.series.map(p=>p.value),tile.events.length,tile.events[0]?.id??null,tile.canvas?.revision??null,tile.canvas?.total_rows??null]);}
 function newTracker(){
  if(typeof options.onNewTracker==='function'){options.onNewTracker();return;}
  global.dispatchEvent(new CustomEvent('rocoto:home-new-tracker'));
  if(global.parent&&global.parent!==global)global.parent.postMessage({type:'rocoto:home-new-tracker'},global.location.origin);
 }
 function open(key,node){
  const tile=model.tile(key);if(!tile)return;
  /* A company's own tracker opens its canvas in the shell, where its chat and phone alerts live.
   * A Core Tracker opens its full view here. */
  const agent=!tile.core&&String(tile.key).startsWith('agent:')?String(tile.key).slice(6):tile.agent_id?String(tile.agent_id):'';
  if(agent&&global.parent&&global.parent!==global){global.parent.postMessage({type:'rocoto:home-open-tracker',agent_id:agent,period},global.location.origin);return;}
  /* A tracker of the company's own that stands by itself, ahead of any Tracking Agent: the shell's canvas draws its saved presentation too.
   * The row and the canvas this tile drew ride with the request, so both show one output revision. */
  const sent=!tile.core&&!agent&&tile.published?.canvas?.presentation&&tile.state!=='needs'?tile.published:null;
  if(sent&&global.parent&&global.parent!==global){
   const shown=sent.canvas.presentation;
   global.parent.postMessage({type:'rocoto:home-open-tracker',period,tracker:{key:tile.key,title:tile.label,mode:'scheduled',
    observations:{presentation:{canvas:{family:shown.family,blocks:shown.blocks},revision:shown.revision},output:{revision:sent.row.revision,tile:sent.row,canvas:sent.canvas}}}},global.location.origin);
   return;
  }
  const history=global.RocotoHomeHistory;
  if(history&&typeof history.open==='function'){try{history.open(tile,model,node);openStamp=stampOf(tile);return;}catch{}}
  openTile(tile);
 }
 /* The history a tile opens when the shared history module is away: its number, its mark and its events. */
 function openTile(tile){
  const dialog=q('#locationMetricDialog'),body=q('#locationMetricHistory');
  q('#locationMetricTitle').textContent=tile.label;
  const figure=tile.state==='needs'?`<p class="home-tile-history-needs">${esc(tile.needs)}</p>`:`<p class="home-tile-history-figure"><strong>${esc(tile.text)}</strong><em>${esc(tile.unit)}</em></p>`;
  body.innerHTML=`${figure}<div class="home-tile-history-mark home-tile-mark"></div>${tile.events.length?`<ol class="home-tile-history-rows">${tile.events.map((event,index)=>`<li><button type="button" class="home-tile-history-row" data-row="${index}"${event.target?'':' disabled'}><span class="home-tile-history-thumb">${event.thumb?`<img src="${esc(event.thumb)}" alt="" loading="lazy">`:''}</span><span class="home-tile-history-copy"><strong>${esc(event.label)}</strong><span>${esc(event.description)}</span></span>${event.target?icon('chevron-right'):''}</button></li>`).join('')}</ol>`:''}`;
  drawMark(body,{...tile,stage:false});
  body.querySelectorAll('[data-row]').forEach(row=>row.addEventListener('click',()=>{const event=tile.events[Number(row.dataset.row)];if(!event?.target)return;dialog.close();options.onOpenSession?.(event.target);}));
  dialog.showModal();
 }

 /* ── products ──────────────────────────────────────────────────────────────────────────────── */
 function renderProducts(){
  const lists=model.products,face=JSON.stringify(boards.map(([key])=>lists[key].map(entry=>[entry.id,entry.label,entry.image,entry.count,entry.price,entry.estimated])));
  /* The same three lists again keep their rows: a refresh leaves a focused row where it is. */
  if(face===boardKey&&q('#locationProducts').childElementCount)return;boardKey=face;
  q('#locationProducts').innerHTML=boards.map(([key,label,,empty])=>{
   const entries=lists[key];
   return `<section class="home-board"><h3>${label}</h3><ol>${entries.length?entries.map((entry,index)=>`<li><button class="home-board-row location-product-card" type="button" data-location-product="${esc(entry.id)}"><span class="home-board-rank">${index+1}</span><span class="home-board-image">${entry.image?`<img src="${esc(entry.image)}" alt="" loading="lazy">`:''}</span><span class="home-board-name"><span>${esc(entry.label)}</span>${entry.price?`<small>${entry.estimated?'≈ ':''}${esc(entry.price)}</small>`:''}</span><strong class="home-board-count">${entry.count}</strong></button></li>`).join(''):`<li><div class="home-board-row is-open"><span class="home-board-rank">1</span><span class="home-board-image is-open"></span><span class="home-board-waits">${empty}</span><strong class="home-board-count">0</strong></div></li>`}</ol></section>`;
  }).join('');
  q('#locationProducts').querySelectorAll('[data-location-product]').forEach(button=>button.addEventListener('click',()=>openProduct(model.products.all.find(p=>String(p.id)===button.dataset.locationProduct))));
 }
 function stopPreviews(){previews.forEach(preview=>preview.stop());}
 function clearPreviews(){previews.forEach(preview=>preview.destroy());previews=[];}
 /* One event's clip loops while its row is held: same origin only, one at a time, and it lets go of the
  * recording the moment the row, the page or the product frame looks away. */
 function bindPreview(row,video,event,start,end){
  let playing=false,generation=0,pending=null,visible=true,paused=false;
  const listeners=[];
  const on=(target,type,handler)=>{target.addEventListener(type,handler);listeners.push(()=>target.removeEventListener(type,handler));};
  const stop=()=>{
   playing=false;generation+=1;pending?.abort();pending=null;
   video.pause();video.classList.remove('playing');
   if(video.hasAttribute('src')){video.removeAttribute('src');video.load();}
  };
  const metadata=signal=>new Promise((resolve,reject)=>{
   if(video.readyState>=1){resolve();return;}
   const finish=fn=>()=>{video.removeEventListener('loadedmetadata',loaded);video.removeEventListener('error',failed);signal.removeEventListener('abort',aborted);fn();};
   const loaded=finish(resolve),failed=finish(()=>reject(Error('Preview unavailable'))),aborted=finish(()=>reject(Error('Preview cancelled')));
   video.addEventListener('loadedmetadata',loaded,{once:true});video.addEventListener('error',failed,{once:true});signal.addEventListener('abort',aborted,{once:true});
  });
  const start_=async()=>{
   if(disposed||global.document.hidden||paused||!visible||playing||reducedMotion())return;
   let source;try{source=new URL(event.media,global.document.baseURI);if(source.origin!==global.location.origin||!['https:','http:'].includes(source.protocol))return;}catch{return;}
   stopPreviews();playing=true;const ticket=++generation,controller=new AbortController();pending=controller;
   try{
    video.preload='metadata';video.src=source.href;
    await metadata(controller.signal);
    if(disposed||global.document.hidden||paused||!visible||!playing||ticket!==generation)return;
    video.currentTime=Math.min(start,Number.isFinite(video.duration)?Math.max(0,video.duration-.05):start);
    await video.play();
    if(!disposed&&!global.document.hidden&&!paused&&visible&&playing&&ticket===generation)video.classList.add('playing');else if(!playing||disposed||global.document.hidden||paused||!visible)video.pause();
   }catch{if(ticket===generation)stop();}
  };
  const loop=()=>{if(playing&&video.currentTime>=end)video.currentTime=start;};
  on(row,'pointerenter',start_);on(row,'pointerleave',stop);on(row,'focus',start_);on(row,'blur',stop);
  on(video,'timeupdate',loop);on(video,'error',stop);on(video,'ended',()=>{if(playing){video.currentTime=start;video.play().catch(stop);}});
  on(global.document,'visibilitychange',()=>{if(global.document.hidden)stop();});
  on(global,'message',message=>{
   if(message.source!==global.parent||message.origin!==global.location.origin)return;
   if(message.data?.type==='rocoto:player-suspend'){paused=true;stop();}
   if(message.data?.type==='rocoto:player-resume')paused=false;
  });
  const observer=typeof IntersectionObserver==='function'?new IntersectionObserver(entries=>{visible=entries.some(entry=>entry.isIntersecting);if(!visible)stop();}):null;
  observer?.observe(row);
  return {stop,destroy(){stop();observer?.disconnect();listeners.forEach(remove=>remove());}};
 }
 function openProduct(product){
  if(!product)return;clearPreviews();
  q('#locationProductDetailTitle').innerHTML=`${product.image?`<img src="${esc(product.image)}" alt="">`:''}<div><h2>${esc(Model.productLabel(product.label)||'Observed item')}</h2><p><strong>${Number(product.counts?.interactions||0)}</strong> interactions</p></div>`;
  q('#locationProductHistory').innerHTML=(product.events||[]).map((event,index)=>{const time=event.time_s??0,person=event.personId??event.visitor_id;return `<button class="location-history-row" type="button" data-history-event="${index}"><span class="location-history-preview">${event.image||event.thumb||event.poster?`<img src="${esc(event.image||event.thumb||event.poster)}" alt="${esc(event.label)}">`:''}${event.media?`<video muted playsinline preload="none" data-event-video="${index}"></video>`:''}<span>${clock(time)}</span></span><span class="location-history-copy"><strong>${esc(event.label||event.type)}</strong><span>${esc(event.description||'')}</span><small>${esc(person?visitorName(person):'Shop floor')} · ${clock(time)}${event.end_s>time?`–${clock(event.end_s)}`:''}</small></span>${icon('chevron-right')}</button>`;}).join('');
  q('#locationProductHistory').querySelectorAll('[data-history-event]').forEach(row=>{
   const event=product.events[Number(row.dataset.historyEvent)],video=row.querySelector('video'),time=Number(event.time_s||0),start=Math.max(0,time-.6),end=Math.min(Number(event.source_clip_duration_s)||Infinity,Math.max(Number(event.end_s)||start+1,time+1)+.6);
   if(video)previews.push(bindPreview(row,video,event,start,end));
   row.addEventListener('click',()=>{stopPreviews();q('#locationProductDialog').close();options.onOpenSession?.(event.sessionTarget||event.target||{clipId:event.clipId??event.source_id,personId:event.personId??event.visitor_id,time_s:time});});
  });
  q('#locationProductDialog').showModal();
 }

 /* ── neighbourhood ─────────────────────────────────────────────────────────────────────────── */
 function renderNeighbourhood(){
  const target=q('#locationNeighbourhoodSummary');if(!target)return;
  if(!neighbourhood){target.replaceChildren();return;}
  const counts=neighbourhood.counts||{},total=number(neighbourhood.total)||0,radius=number(neighbourhood.radius_m);
  const groups=[['residential','Residential'],['commercial','Commercial'],['mixed','Mixed']].map(([key,label])=>[key,label,number(counts[key])||0]).filter(([,,count])=>count>0);
  const rest=(number(counts.other)||0)+(number(counts.unknown)||0);if(rest>0)groups.push(['other','Other',rest]);
  target.innerHTML=`<p class="home-hood-figure"><strong>${total.toLocaleString('en-US')}</strong><em>buildings${radius?` within ${radius.toLocaleString('en-US')} m`:''}</em></p><div class="home-hood-split" role="img" aria-label="${esc(groups.map(([,label,count])=>`${count} ${label.toLowerCase()}`).join(', '))}">${groups.map(([key,,count])=>`<i data-kind="${key}" style="flex-grow:${count}"></i>`).join('')}</div><dl class="home-hood-legend">${groups.map(([key,label,count])=>`<div data-kind="${key}"><dd>${count.toLocaleString('en-US')}</dd><dt>${label}</dt></div>`).join('')}</dl>`;
 }

 /* ── catalog setup (the product's own routes) ──────────────────────────────────────────────── */
 function catalogLocationParams(){const loc=data?.locations?.[0]||data?.location||{};return {collection:String(loc.collection||''),location_id:String(loc.id||loc.location_id||'')};}
 function catalogMethodButton(method){const id=method.id||'';return `<button type="button" class="location-catalog-mode" data-catalog-method="${esc(id)}"><strong>${esc(method.name||method.title||'Catalog source')}</strong><span>${esc(id==='camera_observations'?'Use what the cameras see as provisional product references.':method.accept?`Import ${method.accept} with confirmed SKU and name columns.`:method.description||'Connect product references to this shop.')}</span></button>`;}
 async function openCatalogSetup(){
  const dialog=q('#locationCatalogDialog'),body=q('#locationCatalogBody');if(!dialog)return;if(!dialog.open)dialog.showModal();body.innerHTML='<p>Loading catalog setup…</p>';
  try{const params=new URLSearchParams(catalogLocationParams()),response=await fetch('/api/retail/catalog/setup?'+params,{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw Error('Catalog setup opens from this shop’s workspace.');const setup=await response.json(),methods=Array.isArray(setup.methods)?setup.methods:[];body.innerHTML=`<p><strong>${Number(setup.product_count||0)}</strong> product references connected.</p><div class="location-catalog-modes">${methods.map(catalogMethodButton).join('')}</div>`;body.querySelectorAll('[data-catalog-method]').forEach(button=>button.addEventListener('click',()=>requestCatalogMethod(button.dataset.catalogMethod,setup)));
  }catch(error){body.innerHTML=`<p class="location-catalog-error">${esc(error.message)}</p>`;}
 }
 async function requestCatalogMethod(method,setup){
  if(method==='catalog_import'){renderCatalogImport();return;}
  if(method==='camera_observations'){const body=q('#locationCatalogBody');body.innerHTML='<h3>Camera observations</h3><p>The cameras suggest provisional product references for review. Confirmed SKU and name data comes from your catalog or shelf-photo review.</p><button type="button" class="home-text-button" id="locationCatalogBack">Back to catalog sources</button>';q('#locationCatalogBack').addEventListener('click',openCatalogSetup);return;}
  if(method==='shelf_photos'){await createPhotoHandoff();return;}
  const body=q('#locationCatalogBody');body.innerHTML=`<p>${esc(setup?.catalog_url?'Open the connected catalog to continue.':'Choose a catalog source to continue.')}</p>`;
 }
 function renderCatalogImport(){
  const body=q('#locationCatalogBody');if(!body)return;body.innerHTML='<h3>Import your catalog</h3><p>Choose a CSV or JSON file with SKU and name columns.</p><form class="location-catalog-import" id="locationCatalogImport"><input id="locationCatalogFile" type="file" accept=".csv,.json,text/csv,application/json" required><button class="location-catalog-submit" type="submit">Import catalog</button><button class="home-text-button" type="button" id="locationCatalogBack">Back to catalog sources</button><p class="location-catalog-status" id="locationCatalogStatus" role="status"></p></form>';q('#locationCatalogBack').addEventListener('click',openCatalogSetup);q('#locationCatalogImport').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,file=q('#locationCatalogFile')?.files?.[0],status=q('#locationCatalogStatus'),button=form.querySelector('button[type=submit]');if(!file)return;button.disabled=true;status.textContent='Importing catalog…';try{const formData=new FormData();formData.append('file',file);const params=new URLSearchParams(catalogLocationParams()),response=await fetch('/api/retail/catalog/import-file?'+params,{method:'POST',credentials:'same-origin',headers:{'X-Rocoto-Page':'1'},body:formData});const value=await response.json().catch(()=>({}));if(!response.ok)throw Error(value.detail||'Choose a CSV or JSON catalog with SKU and name columns.');status.textContent=`Imported ${Number(value.count||0)} product references.`;}catch(error){status.textContent=error.message;}finally{button.disabled=false;}});
 }
 async function createPhotoHandoff(){
  const body=q('#locationCatalogBody');if(!body)return;body.innerHTML='<p>Preparing a private shelf-photo handoff…</p>';try{const response=await fetch('/api/retail/catalog/photo-handoff',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify(catalogLocationParams())});const value=await response.json().catch(()=>({}));if(!response.ok)throw Error(value.detail||'This catalog path opens from this shop’s workspace.');const upload=value.upload_url||value.url,qr=value.qr_svg||'';body.innerHTML=`<h3>Photograph your shelves</h3><p>Scan this code or open the private link on the phone taking the shelf photos.</p>${qr?`<div class="location-catalog-qr" aria-label="Shelf-photo upload QR">${qr}</div>`:''}${upload?`<a class="location-catalog-link" href="${esc(upload)}" target="_blank" rel="noreferrer">Open photo upload</a>`:''}<small>Up to ${Number(value.max_images||8)} photos · ${Math.round(Number(value.max_bytes||10485760)/1048576)} MB each · expires ${esc(value.expires_at||'after this setup window')}</small>`;}catch(error){body.innerHTML=`<p class="location-catalog-error">${esc(error.message)}</p>`;}
 }

 /* ── Tracking Agents the shop has made ─────────────────────────────────────────────────────── */
 function loadAgents(){
  if(disposed||options.agents===false||Array.isArray(data?.agents))return;const ticket=++agentsAsked;
  fetch('/api/agents',{credentials:'same-origin',cache:'no-store'}).then(response=>response.ok?response.json():null).then(value=>{
   if(disposed||ticket!==agentsAsked||!value)return;const list=Array.isArray(value)?value:Array.isArray(value.agents)?value.agents:null;if(!list)return;
   agents=list;model=compute();renderTiles();
  }).catch(()=>{});
 }

 function render(){
  if(disposed)return;model=compute();
  renderPlace();renderFootage();
  q('.home-period').querySelectorAll('[data-global-period]').forEach(b=>{const on=b.dataset.globalPeriod===period;b.setAttribute('aria-checked',String(on));b.tabIndex=on?0:-1;});
  renderTiles();renderProducts();renderNeighbourhood();quiet=false;
 }
 q('.home-period').querySelectorAll('[data-global-period]').forEach((button,index)=>{
  listen(button,'click',()=>{if(period===button.dataset.globalPeriod)return;period=button.dataset.globalPeriod;quiet=true;render();tellShell({type:'rocoto:period',period});});
  listen(button,'keydown',event=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key))return;event.preventDefault();const buttons=[...q('.home-period').querySelectorAll('[data-global-period]')],next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(['ArrowRight','ArrowDown'].includes(event.key)?1:-1)+buttons.length)%buttons.length;buttons[next].focus();buttons[next].click();});
 });
 listen(q('#locationMetricClose'),'click',()=>q('#locationMetricDialog').close());listen(q('#locationCatalogClose'),'click',()=>q('#locationCatalogDialog').close());listen(q('#locationCatalogButton'),'click',openCatalogSetup);
 for(const dialog of [q('#locationMetricDialog'),q('#locationProductDialog'),q('#locationCatalogDialog')])listen(dialog,'click',event=>{if(event.target===dialog)dialog.close();});
 listen(q('#locationOpenCameras'),'click',()=>options.onOpenCameras?.(model.place.id));
 listen(q('#locationAddressButton'),'click',()=>options.onLocationChange?.(place||model.place));
 listen(host,'click',event=>{if(event.target.closest('#locationSetAddress'))options.onLocationChange?.(place||model.place);});
 listen(q('#locationProductClose'),'click',()=>q('#locationProductDialog').close());listen(q('#locationProductDialog'),'close',stopPreviews);
 const hero=q('#locationHeroVideo');
 listen(hero,'ended',nextRecording);listen(hero,'playing',()=>hero.classList.add('is-playing'));
 listen(hero,'loadeddata',()=>{hero.classList.add('is-playing');q('#locationStage').dataset.frame=hero.videoWidth/hero.videoHeight<1.4?'square':'wide';drawTags();});
 listen(global.document,'visibilitychange',()=>{if(global.document.hidden)rest();else play();});
 listen(global,'message',message=>{
  if(message.source!==global.parent||message.origin!==global.location.origin)return;
  if(message.data?.type==='rocoto:player-suspend'){suspended=true;rest();}
  if(message.data?.type==='rocoto:player-resume'){suspended=false;play();}
  /* The shell keeps one period for Home and every canvas, and hands the focus back to the tile a canvas came from. */
  if(message.data?.type==='rocoto:period'&&typeof message.data.period==='string')view.setPeriod(message.data.period);
  if(message.data?.type==='rocoto:home-focus-tile'&&typeof message.data.key==='string'){const key=message.data.key,tile=model.tiles.find(t=>t.key===key||(t.agent_id&&`agent:${t.agent_id}`===key));tiles.get(tile?.key)?.focus({preventScroll:true});}
 });
 /* The sheet changes its own period: what it now shows is the tile of that period, as this model reads it. */
 listen(global.document,'rocoto:tile-period',event=>{const next=event.detail&&typeof model.tileFor==='function'?model.tileFor(event.detail.key,event.detail.period):null;if(next)openStamp=stampOf(next);});
 const tellShell=message=>{if(global.parent&&global.parent!==global){try{global.parent.postMessage(message,global.location.origin);}catch{}}};
 /* A new width gives each tile's row a new room: the tiles are fitted and their marks drawn for it, once per frame. */
 let fitFrame=0;
 listen(global,'resize',()=>{if(!shown)return;drawTags();if(!fitFrame)fitFrame=global.requestAnimationFrame(()=>{fitFrame=0;if(!disposed&&shown)renderTiles();});});
 render();loadAgents();
 const view={
  show(){host.hidden=false;shown=true;quiet=true;render();play();if(!tagFrame)tagLoop();loadAgents();},
  hide(){host.hidden=true;shown=false;stopPreviews();release();for(const id of ['#locationProductDialog','#locationMetricDialog','#locationCatalogDialog'])if(q(id).open)q(id).close();global.RocotoHomeHistory?.close?.();},
  setData(next){const previous=model.place.id;data=next||{locations:[]};render();if(previous!==model.place.id){clearPreviews();if(q('#locationProductDialog').open)q('#locationProductDialog').close();}},
  setLocation(next){place=next||null;renderPlace();},
  setPeriod(next){if(model.periods.some(p=>p.key===next)&&next!==period){period=next;quiet=true;render();}},
  model:()=>model,
  open:key=>open(key,tiles.get(key)||null),
  destroy(){disposed=true;if(tagFrame)global.cancelAnimationFrame(tagFrame);if(fitFrame)global.cancelAnimationFrame(fitFrame);clearPreviews();release();cleanups.forEach(fn=>fn());host.replaceChildren();if(active===view)active=null;}
 };
 active=view;
 return view;
}
global.LocationView={create,get active(){return active;}};
})(window);
