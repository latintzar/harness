/* Weather and the neighbourhood map of this location.
 *
 *   RocotoWeather.create({host, place})   the header chip, its preview card and the forecast sheet
 *   RocotoWeather.map(el, opts)           the neighbourhood map with its loading and waiting states
 *
 * The page reads two routes of its own server and no outside host:
 *   /api/retail/location/weather   the place, its city, the current hour, 24 hours, 7 days
 *   /api/retail/location/map       one picture of the streets around the shop, the shop at its centre
 *
 * Paint comes from weather-widget.css and the theme tokens; type is the page's own face. The chip sits on the
 * page, so its preview and its sheet are the page's near-neutral glass. Numbers lead: the figure, then its
 * measures, each number over its name. A line is a neutral core over a low light, warm where the hours are
 * warm and cool where they are cool.
 */
(function(root){
 'use strict';
 const WEATHER='/api/retail/location/weather',MAP='/api/retail/location/map';
 const REFRESH_MS=15*60*1000,RETRY_MS=5*60*1000,PREVIEW_DELAY_MS=140,HOURS=24;
 const doc=root.document;
 const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const finite=value=>typeof value==='number'&&Number.isFinite(value);
 const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
 const reduced=()=>root.matchMedia?.('(prefers-reduced-motion: reduce)').matches===true;
 const degrees=value=>finite(value)?`${Math.round(value)||0}°`:'';
 const token=(name,fallback)=>parseFloat(root.getComputedStyle(doc.documentElement).getPropertyValue(name))||fallback;
 const compact=()=>root.matchMedia?.('(max-width:600px)').matches===true;
 let serial=0;

 /* Lucide 1.34.0, the set the kit installs. The kit sprite carries the transport icons; these are the sky. */
 const ICONS={
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
  moon:'<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>',
  'cloud-sun':'<path d="M12 2v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="M20 12h2"/><path d="m19.07 4.93-1.41 1.41"/><path d="M15.947 12.65a4 4 0 0 0-5.925-4.128"/><path d="M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6Z"/>',
  'cloud-moon':'<path d="M13 16a3 3 0 0 1 0 6H7a5 5 0 1 1 4.9-6z"/><path d="M18.376 14.512a6 6 0 0 0 3.461-4.127c.148-.625-.659-.97-1.248-.714a4 4 0 0 1-5.259-5.26c.255-.589-.09-1.395-.716-1.248a6 6 0 0 0-4.594 5.36"/>',
  cloud:'<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
  cloudy:'<path d="M17.5 12a1 1 0 1 1 0 9H9.006a7 7 0 1 1 6.702-9z"/><path d="M21.832 9A3 3 0 0 0 19 7h-2.207a5.5 5.5 0 0 0-10.72.61"/>',
  'cloud-fog':'<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M16 17H7"/><path d="M17 21H9"/>',
  'cloud-drizzle':'<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M8 19v1"/><path d="M8 14v1"/><path d="M16 19v1"/><path d="M16 14v1"/><path d="M12 21v1"/><path d="M12 16v1"/>',
  'cloud-rain':'<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M16 14v6"/><path d="M8 14v6"/><path d="M12 16v6"/>',
  'cloud-snow':'<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M8 15h.01"/><path d="M8 19h.01"/><path d="M12 17h.01"/><path d="M12 21h.01"/><path d="M16 15h.01"/><path d="M16 19h.01"/>',
  'cloud-lightning':'<path d="M6 16.326A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 .5 8.973"/><path d="m13 12-3 5h4l-3 5"/>',
  'map-pin':'<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>',
  x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  'chevron-left':'<path d="m15 18-6-6 6-6"/>',
  'chevron-right':'<path d="m9 18 6-6-6-6"/>'
 };
 const icon=(name,extra)=>`<svg class="weather-icon${extra?' '+extra:''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name]||ICONS.cloud}</svg>`;

 /* The city a person would say for a stored address: the same rule the server applies. */
 function cityOf(address,zone){
  const parts=String(address||'').split(',').slice(1).map(part=>part.split(/\s+/).filter(word=>word&&!/\d/.test(word)).join(' ')).filter(Boolean);
  const named=zone&&String(zone).includes('/')?String(zone).split('/').pop().replaceAll('_',' '):'';
  return parts.find(part=>named&&part.toLowerCase()===named.toLowerCase())||parts[0]||named;
 }

 /* A smooth curve through every hour that never rises above a value it did not reach (monotone cubic). */
 function monotone(points){
  const n=points.length;
  if(n<2)return n?`M${points[0][0]},${points[0][1]}`:'';
  if(n===2)return `M${points[0][0]},${points[0][1]}L${points[1][0]},${points[1][1]}`;
  const dx=[],slope=[],tangent=[];
  for(let i=0;i<n-1;i++){dx[i]=points[i+1][0]-points[i][0]||1e-6;slope[i]=(points[i+1][1]-points[i][1])/dx[i];}
  tangent[0]=slope[0];tangent[n-1]=slope[n-2];
  for(let i=1;i<n-1;i++)tangent[i]=slope[i-1]*slope[i]<=0?0:(slope[i-1]+slope[i])/2;
  for(let i=0;i<n-1;i++){
   if(slope[i]===0){tangent[i]=0;tangent[i+1]=0;continue;}
   const a=tangent[i]/slope[i],b=tangent[i+1]/slope[i],reach=Math.hypot(a,b);
   if(reach>3){tangent[i]=3/reach*a*slope[i];tangent[i+1]=3/reach*b*slope[i];}
  }
  let d=`M${points[0][0]},${points[0][1]}`;
  for(let i=0;i<n-1;i++){
   const [x0,y0]=points[i],[x1,y1]=points[i+1],third=dx[i]/3;
   d+=`C${(x0+third).toFixed(2)},${(y0+tangent[i]*third).toFixed(2)} ${(x1-third).toFixed(2)},${(y1-tangent[i+1]*third).toFixed(2)} ${x1},${y1}`;
  }
  return d;
 }
 /* Where each hour sits in a box drawn in the element's own pixels. The series keeps its own range. */
 function layout(values,{width,height,top,bottom,left,right}){
  const max=Math.max(...values),min=Math.min(...values),span=max-min,last=values.length-1,inner=height-top-bottom;
  return values.map((value,index)=>[
   +(left+(width-left-right)*(last>0?index/last:.5)).toFixed(2),
   +(span>0?top+inner*(1-(value-min)/span):top+inner/2).toFixed(2)]);
 }
 /* The line: a low light under a neutral core. Warm sits at the top of the box, cool at its foot. */
 function lineMark(values,box,{dots=false}={}){
  const points=layout(values,box),d=monotone(points),id=`weatherLine${++serial}`,foot=box.height-box.bottom;
  const paint=(name,stops)=>`<linearGradient id="${id}-${name}" gradientUnits="userSpaceOnUse" x1="0" y1="${box.top}" x2="0" y2="${foot}">${stops}</linearGradient>`;
  const marks=dots?points.slice(1).map(([x,y])=>`<circle class="weather-line-point" cx="${x}" cy="${y}" r="1.75"/>`).join(''):'';
  const svg=`<svg class="weather-line-svg" viewBox="0 0 ${box.width} ${box.height}" width="${box.width}" height="${box.height}" aria-hidden="true" focusable="false"><defs>${paint('light','<stop offset="0" class="weather-light-warm"/><stop offset="1" class="weather-light-cool"/>')}${paint('core','<stop offset="0" class="weather-core-warm"/><stop offset="1" class="weather-core-cool"/>')}</defs><path class="weather-line-light" d="${d}" stroke="url(#${id}-light)"/><path class="weather-line-stroke" d="${d}" stroke="url(#${id}-core)"/>${marks}</svg>`;
  return {svg,points};
 }
 /* The mark a waiting forecast keeps: the same curve, drawn in dots. */
 function ghostMark(box,stretch){
  const d=monotone(layout([.42,.36,.3,.44,.62,.7,.58,.46,.4],box));
  return `<svg class="weather-line-svg" viewBox="0 0 ${box.width} ${box.height}" ${stretch?'preserveAspectRatio="none"':`width="${box.width}" height="${box.height}"`} aria-hidden="true" focusable="false"><path class="weather-ghost-path" d="${d}"/></svg>`;
 }

 const inert=()=>({place:null,open(){},refresh(){},mountMap(){},status:()=>({loaded:false,configured:false,state:'idle'}),destroy(){}});
 const located=place=>place&&typeof place==='object'&&finite(place.latitude)&&finite(place.longitude)&&Math.abs(place.latitude)<=90&&Math.abs(place.longitude)<=180;
 const routeFor=(base,place,extra)=>{const query=new URLSearchParams(extra||{});if(place?.location_id)query.set('location_id',place.location_id);const text=query.toString();return text?`${base}?${text}`:base;};

 function create({host,onLocation,place:configured}={}){
  if(!host)return inert();
  // The chip stands for a location that holds coordinates; the server reads them from its own stores.
  if(!located(configured)){host.hidden=true;return inert();}
  const place={...configured};
  let answer=null,state='loading',loading=false,previewTimer=0,refreshTimer=0,loadedAt=0,askedAt=0,sheet=null,drawnKey='',destroyed=false;
  host.hidden=false;host.classList.add('weather-widget');host.dataset.state=state;
  host.innerHTML='<button class="weather-trigger" type="button" aria-haspopup="dialog"><span class="weather-current"></span><span class="weather-place"></span></button><div class="weather-preview" data-surface="paper" aria-hidden="true" hidden></div>';
  const trigger=host.querySelector('.weather-trigger'),current=host.querySelector('.weather-current'),placeLabel=host.querySelector('.weather-place'),preview=host.querySelector('.weather-preview');
  const city=()=>answer?.city||place.city||cityOf(place.address,place.timezone)||place.name||'';
  const waits=()=>state==='needs'?'Needs shop address':'Returns shortly';

  /* ---- chip --------------------------------------------------------------------------------- */
  function drawChip(){
   host.dataset.state=state;
   const now=answer?.current;
   if(state==='ready'&&now){
    current.innerHTML=`${icon(now.icon)}<strong>${degrees(now.temperature_c)}</strong><span class="weather-condition">${esc(now.condition)}</span>`;
    trigger.setAttribute('aria-label',`Weather in ${city()}: ${degrees(now.temperature_c)}, ${now.condition}. Open the forecast.`);
   }else if(state==='loading'){
    current.innerHTML='<i class="weather-skeleton weather-skeleton-dot"></i><i class="weather-skeleton weather-skeleton-short"></i><i class="weather-skeleton weather-skeleton-long"></i>';
    trigger.setAttribute('aria-label',`Weather in ${city()}. Loading.`);
   }else{
    current.innerHTML=`${icon(state==='needs'?'map-pin':'cloud','weather-icon-ghost')}<span class="weather-condition">${waits()}</span>`;
    trigger.setAttribute('aria-label',`Weather in ${city()}. ${waits()}.`);
   }
   placeLabel.textContent=state==='needs'?'':city();placeLabel.hidden=!placeLabel.textContent;
  }

  /* ---- preview card ------------------------------------------------------------------------- */
  const WIDE_BOX={width:560,height:96,top:16,bottom:16,left:4,right:4};
  // The preview's line fills the card's own width, whatever the window leaves it.
  const previewBox=()=>({width:Math.max(160,Math.min(token('--weather-preview-width',320),root.innerWidth-24)-48),height:56,top:10,bottom:10,left:6,right:6});
  /* Measures: each number over its name, on one grid. */
  const measures=(rows,columns)=>rows.length?`<ul class="weather-stats" style="--columns:${columns||rows.length}">${rows.map(([value,unit,label])=>`<li><strong>${esc(value)}${unit?` <small>${esc(unit)}</small>`:''}</strong><span>${label}</span></li>`).join('')}</ul>`:'';
  function daily(today){
   const rows=[];
   if(finite(today?.high_c))rows.push([degrees(today.high_c),'','High']);
   if(finite(today?.low_c))rows.push([degrees(today.low_c),'','Low']);
   return rows;
  }
  function figure(now,full){
   const feels=full&&finite(now.feels_like_c)?`<span>Feels like ${degrees(now.feels_like_c)}</span>`:'';
   return `<strong>${degrees(now.temperature_c)}</strong><div class="weather-figure-side"><em>${esc(now.condition)}</em>${feels}</div>`;
  }
  function drawPreview(){
   const box=previewBox(),line=mark=>`<div class="weather-line" style="width:${box.width}px;height:${box.height}px">${mark}</div>`;
   if(state==='ready'&&answer){
    const now=answer.current,today=answer.daily[0],mark=lineMark(answer.hourly.map(hour=>hour.temperature_c),box),rows=daily(today);
    if(finite(today?.rain_chance_percent))rows.push([`${Math.round(today.rain_chance_percent)}%`,'','Rain']);
    preview.innerHTML=`<div class="weather-figure">${figure(now,false)}</div>${line(`${mark.svg}<i class="weather-now" style="left:${mark.points[0][0]}px;top:${mark.points[0][1]}px"></i>`)}${measures(rows)}`;
   }else if(state==='loading'){
    preview.innerHTML=`<div class="weather-figure"><i class="weather-skeleton weather-skeleton-numeral"></i></div>${line(ghostMark(box))}`;
   }else{
    preview.innerHTML=`<p class="weather-waits">${waits()}</p>${line(ghostMark(box))}`;
   }
  }
  function hidePreview(){clearTimeout(previewTimer);host.classList.remove('is-previewing');if(preview.hidden)return;preview.hidden=true;}
  function showPreview(){clearTimeout(previewTimer);if(sheet?.open)return;preview.hidden=false;host.classList.add('is-previewing');}

  /* ---- forecast sheet ----------------------------------------------------------------------- */
  const hourLabel=(hour,index)=>index===0?'Now':hour.time.slice(11,16);
  const rain=value=>finite(value)&&value>=15?`${Math.round(value)}%`:'';
  function hoursHtml(){
   const step=compact()?token('--weather-hour-width-compact',56):token('--weather-hour-width',64);
   const hours=answer.hourly.slice(0,HOURS),box={width:step*hours.length,height:80,top:38,bottom:10,left:step/2,right:step/2};
   const wet=hours.some(hour=>rain(hour.rain_chance_percent));
   const mark=lineMark(hours.map(hour=>hour.temperature_c),box,{dots:true});
   const columns=hours.map((hour,index)=>{
    const label=hourLabel(hour,index),day=index>0&&hour.time.endsWith('T00:00');
    return `<li class="weather-hour${index===0?' is-now':''}${day?' is-day':''}" style="--y:${mark.points[index][1]}px"><time datetime="${esc(hour.time)}">${label}</time>${icon(hour.icon)}<span class="visually-hidden">${esc(hour.condition)}</span><strong>${degrees(hour.temperature_c)}</strong><span class="weather-hour-rain">${rain(hour.rain_chance_percent)}${rain(hour.rain_chance_percent)?'<span class="visually-hidden"> chance of rain</span>':''}</span></li>`;
   }).join('');
   return `<div class="weather-hours${wet?' has-rain':''}" style="--weather-hour-step:${step}px" tabindex="0" role="group" aria-label="Next 24 hours, hour by hour"><div class="weather-hours-track" style="width:${box.width}px"><div class="weather-line weather-hours-line" style="width:${box.width}px;height:${box.height}px">${mark.svg}<i class="weather-now" style="left:${mark.points[0][0]}px;top:${mark.points[0][1]}px"></i></div><ol>${columns}</ol></div></div>`;
  }
  function daysHtml(){
   const days=answer.daily,low=Math.min(...days.map(day=>day.low_c)),high=Math.max(...days.map(day=>day.high_c)),span=high-low||1,at=value=>clamp((value-low)/span,0,1);
   return `<ol class="weather-days">${days.map((day,index)=>{
    const from=at(day.low_c),to=Math.max(at(day.high_c),from+.04),now=index===0&&finite(answer.current.temperature_c)?clamp(at(answer.current.temperature_c),from,to):null;
    return `<li${index===0?' class="is-today"':''}><span class="weather-day-name">${index===0?'Today':`<span class="weather-day-full">${esc(day.weekday)}</span><span class="weather-day-short" aria-hidden="true">${esc(String(day.weekday).slice(0,3))}</span>`}</span>${icon(day.icon)}<span class="visually-hidden">${esc(day.condition)}</span><span class="weather-day-rain">${rain(day.rain_chance_percent)}${rain(day.rain_chance_percent)?'<span class="visually-hidden"> chance of rain</span>':''}</span><span class="weather-day-low"><span class="visually-hidden">Low </span>${degrees(day.low_c)}</span><span class="weather-range" style="--from:${from.toFixed(4)};--to:${to.toFixed(4)}" aria-hidden="true"><i class="weather-range-light"></i><i class="weather-range-core"></i>${now===null?'':`<i class="weather-range-now" style="--at:${now.toFixed(4)}"></i>`}</span><strong class="weather-day-high"><span class="visually-hidden">High </span>${degrees(day.high_c)}</strong></li>`;
   }).join('')}</ol>`;
  }
  function drawSheet(force){
   if(!sheet)return;
   const key=state==='ready'&&answer?`ready:${answer.observed_at}:${answer.hourly[0]?.time}`:state;
   if(!force&&key===drawnKey)return;
   drawnKey=key;sheet.dataset.state=state;
   sheet.querySelector('.weather-sheet-heading h2').textContent=city()?`Weather in ${city()}`:'Weather';
   const head=sheet.querySelector('.weather-sheet-figure'),body=sheet.querySelector('.weather-sheet-body');
   const foot=()=>{body.dataset.foot=body.scrollTop>=body.scrollHeight-body.clientHeight-2?'end':'more';body.dataset.top=body.scrollTop>2?'scrolled':'start';};
   body.onscroll=foot;root.requestAnimationFrame(foot);
   if(state==='ready'&&answer){
    const now=answer.current;
    const today=answer.daily[0],rows=daily(today);
    if(finite(now.wind_kmh))rows.push([String(Math.round(now.wind_kmh)),'km/h','Wind']);
    if(finite(now.humidity_percent))rows.push([`${Math.round(now.humidity_percent)}%`,'','Humidity']);
    if(today?.sunrise)rows.push([today.sunrise,'','Sunrise']);
    if(today?.sunset)rows.push([today.sunset,'','Sunset']);
    head.innerHTML=figure(now,true);
    body.innerHTML=`${measures(rows)}<section class="weather-section"><div class="weather-section-heading"><h3 id="weatherHoursHeading">Next 24 hours</h3><div class="weather-hours-steps"><button type="button" class="weather-step" data-step="-1" aria-label="Earlier hours">${icon('chevron-left')}</button><button type="button" class="weather-step" data-step="1" aria-label="Later hours">${icon('chevron-right')}</button></div></div>${hoursHtml()}</section><section class="weather-section"><div class="weather-section-heading"><h3>Next ${answer.daily.length} days</h3></div>${daysHtml()}</section><p class="weather-credit">Weather data by <a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a></p>`;
    const strip=body.querySelector('.weather-hours'),step=()=>strip.querySelector('.weather-hour').getBoundingClientRect().width*4;
    strip.scrollLeft=0;
    const travel=direction=>strip.scrollBy({left:direction*step(),behavior:reduced()?'auto':'smooth'});
    const edges=()=>{const [earlier,later]=body.querySelectorAll('.weather-step');earlier.disabled=strip.scrollLeft<=1;later.disabled=strip.scrollLeft>=strip.scrollWidth-strip.clientWidth-1;strip.dataset.edge=earlier.disabled?'start':later.disabled?'end':'middle';};
    body.querySelectorAll('.weather-step').forEach(button=>button.addEventListener('click',()=>travel(Number(button.dataset.step))));
    strip.addEventListener('keydown',event=>{
     if(event.key==='ArrowRight'||event.key==='ArrowLeft'){event.preventDefault();travel(event.key==='ArrowRight'?1:-1);}
     else if(event.key==='Home'){event.preventDefault();strip.scrollTo({left:0});}
     else if(event.key==='End'){event.preventDefault();strip.scrollTo({left:strip.scrollWidth});}
    });
    strip.addEventListener('scroll',edges,{passive:true});edges();
   }else if(state==='loading'){
    head.innerHTML='<i class="weather-skeleton weather-skeleton-numeral"></i>';
    body.innerHTML=`<div class="weather-line weather-line-wide">${ghostMark(WIDE_BOX,true)}</div>`;
   }else{
    head.innerHTML=`<p class="weather-waits">${waits()}</p>`;
    body.innerHTML=`<div class="weather-line weather-line-wide">${ghostMark(WIDE_BOX,true)}</div>${state==='needs'?'':'<button type="button" class="weather-retry">Refresh</button>'}`;
    body.querySelector('.weather-retry')?.addEventListener('click',()=>refresh(true));
   }
  }
  function buildSheet(){
   sheet=doc.createElement('dialog');sheet.className='weather-modal weather-sheet';sheet.dataset.surface='paper';sheet.setAttribute('aria-labelledby','weatherSheetTitle');
   sheet.innerHTML=`<header class="weather-sheet-heading"><h2 id="weatherSheetTitle"></h2><button type="button" class="weather-sheet-close" aria-label="Close weather">${icon('x')}</button></header><div class="weather-sheet-figure weather-figure"></div><div class="weather-sheet-body" tabindex="0" role="group" aria-label="Forecast"></div>`;
   doc.body.append(sheet);
   sheet.querySelector('.weather-sheet-close').addEventListener('click',close);
   sheet.addEventListener('click',event=>{if(event.target===sheet)close();});
   sheet.addEventListener('cancel',event=>{event.preventDefault();close();});
  }
  function open(){
   hidePreview();
   if(!sheet)buildSheet();
   if(sheet.open)return;
   sheet.classList.remove('is-closing');sheet.showModal();drawSheet(true);
   // The sheet grows from the chip that opened it.
   const from=trigger.getBoundingClientRect(),to=sheet.getBoundingClientRect();
   if(from.width&&to.width){
    sheet.style.setProperty('--weather-from-x',`${((from.left+from.width/2)-(to.left+to.width/2)).toFixed(1)}px`);
    sheet.style.setProperty('--weather-from-y',`${((from.top+from.height/2)-(to.top+to.height/2)).toFixed(1)}px`);
   }
   sheet.classList.remove('is-open');void sheet.offsetWidth;sheet.classList.add('is-open');
   wake();
  }
  function close(){
   if(!sheet?.open||sheet.classList.contains('is-closing'))return;
   if(reduced()){sheet.close();return;}
   sheet.classList.add('is-closing');
   const done=()=>{sheet.classList.remove('is-closing','is-open');if(sheet.open)sheet.close();};
   sheet.addEventListener('animationend',done,{once:true});root.setTimeout(done,260);
  }

  /* ---- data --------------------------------------------------------------------------------- */
  function draw(){if(destroyed)return;drawChip();drawPreview();if(sheet?.open)drawSheet(false);}
  async function refresh(visible){
   if(loading||destroyed)return;
   loading=true;askedAt=Date.now();if(visible&&state!=='ready'){state='loading';draw();}
   try{
    const response=await fetch(routeFor(WEATHER,place),{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(12000)});
    if(response.status===404){answer=null;state='needs';}
    else if(!response.ok)throw Error('weather');
    else{
     const next=await response.json();
     if(!next?.current||!finite(next.current.temperature_c)||!Array.isArray(next.hourly)||next.hourly.length<2||!Array.isArray(next.daily)||!next.daily.length)throw Error('weather');
     answer=next;state='ready';loadedAt=Date.now();
    }
   }catch{
    // An answer already on the page stays; a first read that fails rests as the waiting chip.
    if(!answer)state='waiting';
   }finally{loading=false;draw();}
  }
  function mountMap(){
   const targets=[doc.querySelector('#locationMapHost'),...doc.querySelectorAll(`[data-rocoto-map],img[src*="${MAP}"]`)].filter(Boolean);
   for(const target of targets)map(target,{place});
  }

  trigger.addEventListener('pointerenter',event=>{if(event.pointerType==='touch')return;clearTimeout(previewTimer);previewTimer=root.setTimeout(showPreview,PREVIEW_DELAY_MS);});
  host.addEventListener('pointerleave',hidePreview);
  trigger.addEventListener('focus',()=>{if(trigger.matches(':focus-visible'))showPreview();});
  trigger.addEventListener('blur',hidePreview);
  trigger.addEventListener('keydown',event=>{if(event.key==='Escape')hidePreview();});
  trigger.addEventListener('click',open);
  // A shown page asks again every fifteen minutes; a waiting chip asks every five. A hidden page rests.
  const wake=()=>{if(!doc.hidden&&Date.now()-askedAt>=(state==='ready'?REFRESH_MS:RETRY_MS))refresh();};
  doc.addEventListener('visibilitychange',wake);
  // The hours take their narrow step when the window crosses the narrow width.
  const narrow=root.matchMedia?.('(max-width:600px)'),reflow=()=>{if(sheet?.open)drawSheet(true);};
  narrow?.addEventListener?.('change',reflow);
  refreshTimer=root.setInterval(wake,60*1000);
  // A map the page adds later takes its states as it arrives.
  let pending=0;
  const watcher=typeof MutationObserver==='function'&&doc.querySelector('#locationRoot')?new MutationObserver(()=>{if(!pending)pending=root.requestAnimationFrame(()=>{pending=0;mountMap();});}):null;
  watcher?.observe(doc.querySelector('#locationRoot'),{childList:true,subtree:true});
  draw();refresh();onLocation?.(place);
  return {place,open,close,refresh:()=>refresh(true),mountMap,
   status:()=>({loaded:state==='ready',loading,state,error:state==='waiting'||state==='needs'?state:null,configured:true}),
   destroy(){destroyed=true;clearTimeout(previewTimer);clearInterval(refreshTimer);doc.removeEventListener('visibilitychange',wake);narrow?.removeEventListener?.('change',reflow);watcher?.disconnect();sheet?.remove();host.replaceChildren();}};
 }

 /* ---- the neighbourhood map ------------------------------------------------------------------
  * map(el): el is the <img src="/api/retail/location/map"> itself or the box that holds it. The box becomes the
  * frame: the picture fades in when it has loaded, a drawn street grid holds its place while it loads, and a
  * finished waiting state says what the map needs. The picture is centred on the shop, so the mark at the
  * frame's centre is the shop. The picture arrives finished from our route, with its maker's mark and credit drawn
  * in: the frame asks it in its own size and shows it whole. */
 const maps=typeof WeakMap==='function'?new WeakMap():null;
 function map(el,opts={}){
  if(!el||!doc)return null;
  const image=el.tagName==='IMG'?el:el.querySelector('img')||doc.createElement('img'),frame=el.tagName==='IMG'?el.parentElement:el;
  if(!frame)return null;
  const held=maps?.get(frame);if(held)return held;
  const place=opts.place||null,label=place?.name||place?.address||'';
  frame.classList.add('rocoto-map');frame.dataset.mapState='loading';
  // A frame with a height of its own keeps it in every state; a shown frame with none becomes a square.
  const size=()=>{const box=frame.getBoundingClientRect();if(box.width>0&&!(box.height>1))frame.classList.add('rocoto-map-auto');};
  image.classList.add('rocoto-map-image');image.alt=label?`Map of the streets around ${label}`:'Map of the streets around this shop';image.decoding='async';image.draggable=false;
  if(!image.parentElement){frame.replaceChildren(image);}
  const wait=doc.createElement('div');wait.className='rocoto-map-wait';wait.innerHTML='<i class="rocoto-map-streets" aria-hidden="true"></i><p role="status"></p>';
  const pin=doc.createElement('i');pin.className='rocoto-map-pin';pin.setAttribute('aria-hidden','true');
  frame.append(wait,pin);
  const note=wait.querySelector('p');let retry=0,asked='';
  const shape=()=>{
   // The picture is asked in the frame's own size, one picture point to a pixel up to 512 on its long side, so its
   // mark and credit are drawn at their own size and the whole picture fits the frame.
   const {width,height}=frame.getBoundingClientRect(),side=value=>clamp(Math.round(value/32)*32,128,512);
   if(!(width>0&&height>0))return null;
   const scale=Math.min(1,512/Math.max(width,height));
   return {width:side(width*scale),height:side(height*scale)};
  };
  const source=()=>{const sized=shape();return sized?routeFor(MAP,{location_id:opts.locationId||place?.location_id},sized):'';};
  const settle=next=>{frame.dataset.mapState=next;note.textContent=next==='needs'?'Needs shop address':next==='waiting'?'Returns shortly':'';};
  // A frame with no size yet asks nothing: its first picture is asked when it is shown.
  const load=()=>{clearTimeout(retry);settle('loading');asked=source();if(asked)image.src=asked;else image.removeAttribute('src');};
  // The picture follows the frame's proportions: a frame that has changed shape takes the picture in its new one.
  const fit=()=>{const next=source(),now=frame.dataset.mapState;if(next&&next!==asked&&(now==='ready'||now==='loading')){asked=next;image.src=next;}};
  image.addEventListener('load',()=>{if(image.naturalWidth>0){settle('ready');fit();}});
  image.addEventListener('error',async()=>{
   let next='waiting';
   // The weather route answers for the same location: 404 there means the shop's coordinates are still to add.
   try{const response=await fetch(routeFor(WEATHER,{location_id:opts.locationId||place?.location_id}),{credentials:'same-origin',cache:'no-store'});if(response.status===404)next='needs';}catch{}
   settle(next);
   if(next==='waiting')retry=root.setTimeout(()=>{if(!doc.hidden&&frame.isConnected)load();},RETRY_MS);
  });
  // A frame that changes its proportions asks for the picture in its new shape.
  let reshape=0;
  const watch=typeof ResizeObserver==='function'?new ResizeObserver(()=>{size();clearTimeout(reshape);reshape=root.setTimeout(fit,700);}):null;
  watch?.observe(frame);
  const handle={el:frame,image,refresh:load,state:()=>frame.dataset.mapState,destroy(){clearTimeout(retry);clearTimeout(reshape);watch?.disconnect();wait.remove();pin.remove();maps?.delete(frame);}};
  maps?.set(frame,handle);
  size();load();
  return handle;
 }

 const api=Object.freeze({create,map,icon,cityOf});
 root.RocotoWeather=api;
 // The name the page's own glue calls.
 root.AreaWeather=api;
})(window);
