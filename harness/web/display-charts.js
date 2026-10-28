/* Rocoto marks: the glowing no-axis line, the ring and the arc, bars, and the tile-size spark.
 *
 *   RocotoCharts.line(el, points, opts)    a smooth line through every measurement, an end point, an optional
 *                                          area fade and a hover read-out
 *   RocotoCharts.spark(el, points, opts)   the same line at tile size
 *   RocotoCharts.ring(el, fraction, opts)  a full ring, filled clockwise from twelve o'clock
 *   RocotoCharts.arc(el, fraction, opts)   an open gauge with a knob at the value
 *   RocotoCharts.bars(el, values, opts)    one bar per value, one of them lit
 *
 * A mark is drawn in the host element's own pixels, so strokes and dots stay crisp at any size, and it is
 * redrawn when the host changes size. Calling a mark again on the same element moves it to the new values.
 * The first paint draws the mark in; a later call moves it. With reduced motion both happen at once.
 * Paint comes from display-charts.css and the theme tokens: this file holds geometry, motion and the read-out.
 * The host gives the mark its box (a width and a height). A mark sits on dark glass when its host is inside
 * .twelvelabs-card-material or [data-surface="glass"], and on the light page everywhere else.
 */
(function(root){
 'use strict';
 const NS='http://www.w3.org/2000/svg';
 const handles=typeof WeakMap==='function'?new WeakMap():null;
 let serial=0;

 /* ---- numbers and geometry (pure) --------------------------------------------------------------- */
 const finite=v=>typeof v==='number'&&Number.isFinite(v);
 const clamp=(v,low,high)=>Math.max(low,Math.min(high,v));
 const fix=v=>Math.round(v*100)/100;
 function valueOf(point){
  const raw=point!==null&&typeof point==='object'?point.value:point;
  if(finite(raw))return raw;
  if(typeof raw==='string'&&raw.trim()!==''&&Number.isFinite(Number(raw)))return Number(raw);
  return null;
 }
 /* A smooth curve that passes through every measured point and never overshoots one (monotone cubic). */
 function monotonePath(points){
  const n=points.length;
  if(!n)return '';
  if(n===1)return `M${points[0][0]},${points[0][1]}`;
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
   d+=`C${fix(x0+third)},${fix(y0+tangent[i]*third)} ${fix(x1-third)},${fix(y1-tangent[i+1]*third)} ${x1},${y1}`;
  }
  return d;
 }
 /* Where each sample sits in a box. A missing sample stays missing: the line breaks there and resumes at the
  * next measurement. The floor is a number (0 puts zero at the foot), 'auto' (the series' own range) or
  * 'soft', the default: the series' own range, widened until it spans two fifths of the largest value, so a
  * line keeps its shape and a small change stays small. A level series rests: at the foot when it is zero,
  * through the middle otherwise. */
 function lineLayout(samples,box){
  const {width,height,top=0,right=0,bottom=0,left=0,floor='soft'}=box;
  const valid=samples.filter(v=>v!==null),own=typeof floor!=='number';
  if(!valid.length)return {points:samples.map(()=>null),runs:[],min:0,max:0,base:height-bottom};
  const max=Math.max(...valid),least=Math.min(...valid);
  let min=own?least:Math.min(floor,least);
  if(floor==='soft'&&max>least){min=max-Math.max(max-least,Math.abs(max)*.4);if(least>=0)min=Math.max(0,min);}
  const span=max-min;
  const innerWidth=Math.max(1,width-left-right),innerHeight=Math.max(1,height-top-bottom),last=samples.length-1;
  const x=i=>last>0?left+innerWidth*i/last:left+innerWidth;
  const y=v=>span>0?top+innerHeight*(1-(v-min)/span):max===0?top+innerHeight:top+innerHeight/2;
  const points=samples.map((v,i)=>v===null?null:[fix(x(i)),fix(y(v))]),runs=[];
  let run=[];
  for(const point of points){if(point)run.push(point);else if(run.length){runs.push(run);run=[];}}
  if(run.length)runs.push(run);
  return {points,runs,min,max,base:height-bottom};
 }
 const strokePath=runs=>runs.filter(run=>run.length>1).map(monotonePath).join('');
 const areaPath=(runs,base)=>runs.filter(run=>run.length>1).map(run=>`${monotonePath(run)}L${run[run.length-1][0]},${fix(base)}L${run[0][0]},${fix(base)}Z`).join('');
 /* A point on a circle; degrees run clockwise from twelve o'clock. */
 function polar(cx,cy,r,degrees){const a=(degrees-90)*Math.PI/180;return [fix(cx+r*Math.cos(a)),fix(cy+r*Math.sin(a))];}
 function arcPath(cx,cy,r,from,to){
  const [x0,y0]=polar(cx,cy,r,from),[x1,y1]=polar(cx,cy,r,to),sweep=to-from;
  if(Math.abs(sweep)>=359.99){const [mx,my]=polar(cx,cy,r,from+180);return `M${x0},${y0}A${r},${r} 0 1 1 ${mx},${my}A${r},${r} 0 1 1 ${x0},${y0}`;}
  return `M${x0},${y0}A${r},${r} 0 ${Math.abs(sweep)>180?1:0} ${sweep>=0?1:0} ${x1},${y1}`;
 }
 /* A bar with rounded shoulders, standing on the baseline. */
 function barPath(x,top,width,base,radius){
  const r=Math.max(0,Math.min(radius,width/2,base-top));
  return `M${fix(x)},${fix(base)}V${fix(top+r)}Q${fix(x)},${fix(top)} ${fix(x+r)},${fix(top)}H${fix(x+width-r)}Q${fix(x+width)},${fix(top)} ${fix(x+width)},${fix(top+r)}V${fix(base)}Z`;
 }
 /* The theme states each curve as cubic-bezier(x1,y1,x2,y2); this evaluates it for a frame loop. */
 function bezier(x1,y1,x2,y2){
  const curve=(a,b,t)=>3*a*t*(1-t)*(1-t)+3*b*t*t*(1-t)+t*t*t;
  return progress=>{
   if(progress<=0)return 0;if(progress>=1)return 1;
   let low=0,high=1,t=progress;
   for(let i=0;i<24;i++){const x=curve(x1,x2,t);if(Math.abs(x-progress)<1e-4)break;if(x<progress)low=t;else high=t;t=(low+high)/2;}
   return curve(y1,y2,t);
  };
 }
 function parseEasing(text,fallback){
  const m=/cubic-bezier\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)/.exec(String(text||''));
  return m?bezier(+m[1],+m[2],+m[3],+m[4]):fallback;
 }
 function parseDuration(text,fallback){
  const m=/^\s*(-?[\d.]+)\s*(ms|s)?\s*$/.exec(String(text||''));
  if(!m)return fallback;
  return m[2]==='s'?Number(m[1])*1000:Number(m[1]);
 }
 const geometry=Object.freeze({valueOf,monotonePath,lineLayout,strokePath,areaPath,polar,arcPath,barPath,bezier,parseEasing,parseDuration});

 /* ---- the page ---------------------------------------------------------------------------------- */
 const doc=root.document;
 const DRAW_EASE=bezier(.16,1,.3,1);
 const svg=(name,attrs,parent)=>{const node=doc.createElementNS(NS,name);for(const key in attrs||{})node.setAttribute(key,attrs[key]);if(parent)parent.append(node);return node;};
 const put=(node,attrs)=>{for(const key in attrs)node.setAttribute(key,attrs[key]);return node;};
 const element=(name,className,parent)=>{const node=doc.createElement(name);if(className)node.className=className;if(parent)parent.append(node);return node;};
 const reducedMotion=()=>typeof root.matchMedia==='function'&&root.matchMedia('(prefers-reduced-motion: reduce)').matches;
 const styleOf=node=>typeof root.getComputedStyle==='function'?root.getComputedStyle(node):null;
 const token=(node,name)=>styleOf(node)?.getPropertyValue(name)||'';
 const duration=(node,name,fallback)=>parseDuration(token(node,name),fallback);
 const easing=(node,name)=>parseEasing(token(node,name),DRAW_EASE);
 const pixels=(node,name,fallback)=>{const n=parseFloat(token(node,name));return Number.isFinite(n)?n:fallback;};
 /* One value moved over time. With reduced motion, or a zero duration, it lands at once. */
 function tween(ms,ease,step,done){
  if(reducedMotion()||!(ms>0)||typeof root.requestAnimationFrame!=='function'){step(1);if(done)done();return null;}
  let frame=0,start=null,cancelled=false;
  const tick=now=>{
   if(cancelled)return;
   if(start===null)start=now;
   const progress=clamp((now-start)/ms,0,1);
   step(ease(progress));
   if(progress<1)frame=root.requestAnimationFrame(tick);else if(done)done();
  };
  step(0);frame=root.requestAnimationFrame(tick);
  return {cancel(){cancelled=true;root.cancelAnimationFrame(frame);}};
 }
 function surfaceOf(host,asked){
  if(asked==='glass'||asked==='paper')return asked;
  const marked=host.closest?.('[data-surface]')?.dataset.surface;
  if(marked==='glass'||marked==='paper')return marked;
  return host.closest?.('.twelvelabs-card-material')?'glass':'paper';
 }
 /* The shared frame of a mark: one stage inside the host, a blurred layer for the light and a crisp one for
  * the ink, redrawn when the host changes size. */
 function mount(host,kind,fallback){
  const stage=element('span',`rocoto-chart rocoto-${kind}`);
  const glow=svg('svg',{class:'rocoto-chart-glow','aria-hidden':'true',focusable:'false'},stage);
  const ink=svg('svg',{class:'rocoto-chart-ink','aria-hidden':'true',focusable:'false'},stage);
  host.replaceChildren(stage);
  const frame={host,stage,glow,ink,id:`rocoto-mark-${++serial}`,width:0,height:0,seen:'',layers:[glow,ink],painted:false,drawing:false,observer:null,running:null};
  /* A host that has no box yet (its sheet is still closed) is drawn at rest and painted in once it has one. */
  frame.laidOut=()=>host.clientWidth>0;
  frame.measure=()=>{
   let width=host.clientWidth,height=host.clientHeight;
   if(!width)width=fallback.width;
   if(!height){height=fallback.height;stage.style.height=`${height}px`;}else stage.style.height='';
   frame.width=width;frame.height=height;frame.seen=`${host.clientWidth}x${host.clientHeight}`;
   for(const layer of frame.layers)put(layer,{viewBox:`0 0 ${width} ${height}`});
   return {width,height};
  };
  frame.watch=redraw=>{
   if(typeof root.ResizeObserver!=='function')return;
   frame.observer=new root.ResizeObserver(()=>{
    if(`${host.clientWidth}x${host.clientHeight}`===frame.seen)return;
    redraw();
   });
   frame.observer.observe(host);
  };
  frame.stop=()=>{frame.running?.cancel();frame.running=null;};
  frame.release=()=>{frame.stop();frame.observer?.disconnect();if(stage.parentNode===host)stage.remove();handles?.delete(host);};
  return frame;
 }
 const existing=(host,kind)=>{const handle=handles?.get(host);if(handle&&handle.kind===kind&&handle.el===host&&host.contains(handle.stage))return handle;handle?.destroy();return null;};
 const describe=(stage,label,role='img')=>{if(label){stage.setAttribute('role',role);stage.setAttribute('aria-label',label);}else if(role==='img'){stage.removeAttribute('role');stage.setAttribute('aria-hidden','true');}};

 /* ---- line and spark ---------------------------------------------------------------------------- */
 const LINE_SIZES={
  spark:{top:6,right:7,bottom:6,left:1,end:3,dot:0,width:120,height:40},
  tile:{top:12,right:10,bottom:12,left:2,end:4.5,dot:2,width:320,height:88},
  large:{top:26,right:16,bottom:22,left:4,end:6,dot:2.5,width:640,height:236}
 };
 const plain=value=>finite(value)?value.toLocaleString('en-US',{maximumFractionDigits:1}):'';
 function pointLabel(point){
  if(point===null||typeof point!=='object')return '';
  if(typeof point.label==='string'&&point.label)return point.label;
  const at=point.t??point.at;
  if(typeof at==='string'&&at){const time=new Date(at);return Number.isFinite(time.getTime())?time.toLocaleDateString('en-GB',{weekday:'short',day:'numeric',month:'short'}):at;}
  return '';
 }
 function line(host,points,opts){
  if(!host||!doc)return null;
  const reuse=existing(host,'line');
  if(reuse){reuse.update(points,opts);return reuse;}
  const frame=mount(host,'line',LINE_SIZES.tile),{stage,glow,ink,id}=frame;
  const glowPaint=svg('linearGradient',{id:`${id}-glow`,gradientUnits:'userSpaceOnUse'},svg('defs',null,glow));
  svg('stop',{offset:'0',class:'rocoto-glow-warm rocoto-stop-dim'},glowPaint);svg('stop',{offset:'.7',class:'rocoto-glow-mid'},glowPaint);svg('stop',{offset:'1',class:'rocoto-glow-cool'},glowPaint);
  const glowStroke=svg('path',{class:'rocoto-line-light',stroke:`url(#${id}-glow)`},glow),glowEnd=svg('circle',{class:'rocoto-line-bloom'},glow);
  /* The area fade has a layer of its own under the light, so its two ends dissolve into the surface. */
  const wash=svg('svg',{class:'rocoto-chart-wash','aria-hidden':'true',focusable:'false'});stage.prepend(wash);frame.layers.push(wash);
  const defs=svg('defs',null,ink);
  const strokePaint=svg('linearGradient',{id:`${id}-stroke`,gradientUnits:'userSpaceOnUse'},defs);
  svg('stop',{offset:'0',class:'rocoto-stop-warm rocoto-stop-dim'},strokePaint);svg('stop',{offset:'.7',class:'rocoto-stop-mid'},strokePaint);svg('stop',{offset:'1',class:'rocoto-stop-cool'},strokePaint);
  const areaPaint=svg('linearGradient',{id:`${id}-area`,gradientUnits:'userSpaceOnUse',x1:'0',x2:'0'},svg('defs',null,wash));
  svg('stop',{offset:'0',class:'rocoto-area-top'},areaPaint);svg('stop',{offset:'1',class:'rocoto-area-base'},areaPaint);
  const cursorPaint=svg('linearGradient',{id:`${id}-cursor`,gradientUnits:'userSpaceOnUse',x1:'0',x2:'0'},defs);
  for(const [offset,name] of [['0','rocoto-cursor-edge'],['.18','rocoto-cursor-body'],['.82','rocoto-cursor-body'],['1','rocoto-cursor-edge']])svg('stop',{offset,class:name},cursorPaint);
  const area=svg('path',{class:'rocoto-line-area',fill:`url(#${id}-area)`},wash);
  const cursor=svg('rect',{class:'rocoto-line-cursor',fill:`url(#${id}-cursor)`,width:'1',visibility:'hidden'},ink);
  const stroke=svg('path',{class:'rocoto-line-stroke',stroke:`url(#${id}-stroke)`},ink);
  const dots=svg('g',{class:'rocoto-line-points'},ink);
  const end=svg('circle',{class:'rocoto-line-end'},ink);
  const marker=svg('circle',{class:'rocoto-line-marker',visibility:'hidden'},ink);
  const readout=element('output','rocoto-chart-readout',stage);readout.hidden=true;
  const readValue=element('strong','',readout),readLabel=element('span','',readout);
  const state={source:[],samples:[],shown:[],opts:{},layout:null,focus:null,size:'tile'};

  function place(values){
   const size=LINE_SIZES[state.size],{width,height}=frame;
   const layout=lineLayout(values,{width,height,top:size.top,right:size.right,bottom:size.bottom,left:size.left,floor:state.opts.floor===undefined?'soft':state.opts.floor});
   const d=strokePath(layout.runs),flat=layout.runs.flat(),last=flat[flat.length-1]||null;
   put(stroke,{d});put(glowStroke,{d});
   put(area,{d:state.opts.area?areaPath(layout.runs,height):''});
   for(const paint of [glowPaint,strokePaint])put(paint,{x1:size.left,x2:fix(width-size.right),y1:0,y2:0});
   put(areaPaint,{y1:size.top,y2:height});put(cursorPaint,{y1:0,y2:height});
   const showDots=size.dot>0&&state.opts.points!==false&&flat.length<=14;
   const isolated=layout.runs.filter(run=>run.length===1).map(run=>run[0]);
   const marks=(showDots?flat.slice(0,-1):isolated.filter(p=>p!==last));
   while(dots.childNodes.length>marks.length)dots.lastChild.remove();
   marks.forEach((point,index)=>{const dot=dots.childNodes[index]||svg('circle',null,dots);put(dot,{cx:point[0],cy:point[1],r:size.dot||1.5,opacity:fix(.35+.65*clamp(point[0]/Math.max(1,width)/.7,0,1))});});
   const showEnd=last&&state.opts.end!==false;
   put(end,showEnd?{cx:last[0],cy:last[1],r:size.end,visibility:'visible'}:{visibility:'hidden',r:0});
   put(glowEnd,showEnd?{cx:last[0],cy:last[1],r:fix(size.end*2.1),visibility:'visible'}:{visibility:'hidden',r:0});
   state.layout=layout;state.shown=values.slice();
   if(state.focus!==null)showFocus(state.focus,false);
  }
  function drawIn(ms){
   frame.stop();
   if(reducedMotion()||!(ms>0)){stage.dataset.painted='true';return;}
   stage.dataset.painted='drawing';
   for(const path of [stroke,glowStroke])put(path,{pathLength:'1','stroke-dasharray':'1 1','stroke-dashoffset':'1'});
   frame.drawing=true;
   frame.running=tween(ms,easing(stage,'--chart-ease'),progress=>{
    const offset=fix(1-progress);
    stroke.setAttribute('stroke-dashoffset',offset);glowStroke.setAttribute('stroke-dashoffset',offset);
    stage.style.setProperty('--mark-arrival',String(fix(clamp((progress-.55)/.45,0,1))));
    stage.style.setProperty('--mark-reveal',String(fix(progress)));
   },()=>{
    for(const path of [stroke,glowStroke]){path.removeAttribute('pathLength');path.removeAttribute('stroke-dasharray');path.removeAttribute('stroke-dashoffset');}
    stage.style.removeProperty('--mark-arrival');stage.style.removeProperty('--mark-reveal');stage.dataset.painted='true';frame.running=null;frame.drawing=false;
   });
   if(!frame.running)frame.drawing=false;
  }
  function settle(){
   frame.stop();frame.drawing=false;
   for(const path of [stroke,glowStroke]){path.removeAttribute('pathLength');path.removeAttribute('stroke-dasharray');path.removeAttribute('stroke-dashoffset');}
   stage.style.removeProperty('--mark-arrival');stage.style.removeProperty('--mark-reveal');
  }
  function render(reason){
   frame.measure();
   const previous=state.shown,next=state.samples,hasLine=next.some(v=>v!==null);
   if(!frame.laidOut()){settle();place(next);stage.dataset.painted='false';return;}
   if(reason==='resize'&&frame.painted){
    /* The box changed: the line keeps drawing in where it is; a value on its way lands at once. */
    if(!frame.drawing)frame.stop();
    place(next);return;
   }
   const comparable=frame.painted&&previous.length===next.length&&next.length>1&&previous.every((v,i)=>(v===null)===(next[i]===null));
   if(!comparable){
    const fresh=!frame.painted;
    settle();place(next);
    if(hasLine){frame.painted=true;drawIn(duration(stage,'--chart-draw-duration',700)*(fresh?1:.65));}
    else stage.dataset.painted='true';
    return;
   }
   settle();stage.dataset.painted='true';
   frame.running=tween(duration(stage,'--chart-change-duration',240),easing(stage,'--chart-ease'),progress=>{
    place(next.map((value,i)=>value===null?null:previous[i]+(value-previous[i])*progress));
   },()=>{frame.running=null;});
  }
  function valueText(index){
   const point=state.source[index],value=state.samples[index];
   const text=typeof state.opts.format==='function'?state.opts.format(value,point,index):plain(value);
   const label=typeof state.opts.caption==='function'?state.opts.caption(point,index):pointLabel(point);
   return [String(text??''),String(label??'')];
  }
  function showFocus(index,announce=true){
   const point=state.layout?.points[index];
   if(!point){hideFocus();return;}
   state.focus=index;
   const [x,y]=point,{width,height}=frame,[text,label]=valueText(index);
   put(cursor,{x:Math.round(x)-.5,y:0,height,visibility:'visible'});
   put(marker,{cx:x,cy:y,r:LINE_SIZES[state.size].end,visibility:'visible'});
   readValue.textContent=text;readLabel.textContent=label;readLabel.hidden=!label;readout.hidden=false;
   const side=x>width*.6?'left':'right';
   readout.dataset.side=side;readout.style.left=`${fix(x)}px`;readout.style.top=`${fix(clamp(y,18,Math.max(18,height-18)))}px`;
   if(state.opts.hover){stage.setAttribute('aria-valuenow',String(index));stage.setAttribute('aria-valuetext',[text,label].filter(Boolean).join(', '));}
   if(announce&&typeof state.opts.onFocus==='function')state.opts.onFocus(index,state.source[index]);
  }
  function hideFocus(announce=true){
   if(state.focus===null&&readout.hidden)return;
   state.focus=null;cursor.setAttribute('visibility','hidden');marker.setAttribute('visibility','hidden');readout.hidden=true;
   if(announce&&typeof state.opts.onFocus==='function')state.opts.onFocus(null,null);
  }
  function nearest(clientX){
   const box=stage.getBoundingClientRect(),x=clientX-box.left;let best=-1,distance=Infinity;
   (state.layout?.points||[]).forEach((point,index)=>{if(!point)return;const gap=Math.abs(point[0]-x);if(gap<distance){distance=gap;best=index;}});
   return best;
  }
  const measured=()=>state.samples.map((v,i)=>v===null?-1:i).filter(i=>i>=0);
  function step(delta){
   const order=measured();if(!order.length)return;
   const at=state.focus===null?order.length-1:order.indexOf(state.focus);
   showFocus(order[clamp((at<0?order.length-1:at)+delta,0,order.length-1)]);
  }
  const onMove=event=>{if(!state.opts.hover)return;const index=nearest(event.clientX);if(index>=0&&index!==state.focus)showFocus(index);};
  const onLeave=()=>{if(state.opts.hover&&doc.activeElement!==stage)hideFocus();};
  const onKey=event=>{
   if(!state.opts.hover)return;
   const moves={ArrowLeft:-1,ArrowDown:-1,ArrowRight:1,ArrowUp:1,PageDown:-5,PageUp:5,Home:-Infinity,End:Infinity};
   if(!(event.key in moves))return;
   event.preventDefault();
   const order=measured();if(!order.length)return;
   if(event.key==='Home')showFocus(order[0]);else if(event.key==='End')showFocus(order[order.length-1]);else step(moves[event.key]);
  };
  const onFocusIn=()=>{if(state.opts.hover&&state.focus===null&&stage.matches(':focus-visible'))step(0);};
  const onFocusOut=()=>{if(state.opts.hover)hideFocus();};
  stage.addEventListener('pointermove',onMove);stage.addEventListener('pointerdown',onMove);stage.addEventListener('pointerleave',onLeave);
  stage.addEventListener('keydown',onKey);stage.addEventListener('focus',onFocusIn);stage.addEventListener('blur',onFocusOut);

  const handle={kind:'line',el:host,stage,
   update(nextPoints,nextOpts){
    state.opts={...(nextOpts||{})};
    state.source=Array.isArray(nextPoints)?nextPoints:[];
    state.samples=state.source.map(valueOf);
    state.size=LINE_SIZES[state.opts.size]?state.opts.size:'tile';
    stage.dataset.size=state.size;stage.dataset.surface=surfaceOf(host,state.opts.surface);
    stage.dataset.empty=String(!state.samples.some(v=>v!==null));
    if(state.opts.hover){
     put(stage,{role:'slider',tabindex:'0','aria-orientation':'horizontal','aria-valuemin':'0','aria-valuemax':String(Math.max(0,state.samples.length-1))});
     if(state.opts.label)stage.setAttribute('aria-label',state.opts.label);
     const order=measured();
     if(order.length){const [text,label]=valueText(order[order.length-1]);put(stage,{'aria-valuenow':String(order[order.length-1]),'aria-valuetext':[text,label].filter(Boolean).join(', ')});}
    }else{stage.removeAttribute('tabindex');describe(stage,state.opts.label);}
    if(state.focus!==null&&state.samples[state.focus]==null)hideFocus(false);
    render(frame.painted?'change':'first');
    return handle;
   },
   focus(index){if(index===null||index===undefined)hideFocus(false);else showFocus(index,false);return handle;},
   blur(){hideFocus(false);return handle;},
   get geometry(){return {width:frame.width,height:frame.height,points:state.layout?.points||[]};},
   destroy(){frame.release();}
  };
  handles?.set(host,handle);
  frame.watch(()=>render('resize'));
  return handle.update(points,opts);
 }
 const spark=(host,points,opts)=>line(host,points,{size:'spark',points:false,...(opts||{}),hover:false});

 /* ---- ring and arc ------------------------------------------------------------------------------ */
 const share=value=>clamp(finite(value)?value:Number(value)||0,0,1);
 function gauge(host,fraction,opts,kind){
  if(!host||!doc)return null;
  const reuse=existing(host,kind);
  if(reuse){reuse.update(fraction,opts);return reuse;}
  const open=kind==='arc',frame=mount(host,kind,{width:92,height:92}),{stage,glow,ink,id}=frame;
  const glowPaint=svg('linearGradient',{id:`${id}-glow`,gradientUnits:'userSpaceOnUse'},svg('defs',null,glow));
  svg('stop',{offset:'0',class:'rocoto-glow-warm'},glowPaint);svg('stop',{offset:'1',class:'rocoto-glow-cool'},glowPaint);
  const strokePaint=svg('linearGradient',{id:`${id}-stroke`,gradientUnits:'userSpaceOnUse'},svg('defs',null,ink));
  svg('stop',{offset:'0',class:'rocoto-stop-warm'},strokePaint);svg('stop',{offset:'1',class:'rocoto-stop-cool'},strokePaint);
  const light=svg('path',{class:'rocoto-gauge-light',stroke:`url(#${id}-glow)`},glow);
  const track=svg('path',{class:'rocoto-gauge-track'},ink),value=svg('path',{class:'rocoto-gauge-value',stroke:`url(#${id}-stroke)`},ink);
  const knob=svg('circle',{class:'rocoto-gauge-knob'},ink);
  const state={fraction:0,shown:0,opts:{},box:null};
  /* The gauge opens at the bottom: 240 degrees from eight o'clock round to four o'clock. */
  const START=open?-120:0,SWEEP=open?240:360;
  function box(){
   const {width,height}=frame.measure(),size=Math.min(width,height);
   const wanted=finite(state.opts.stroke)?state.opts.stroke:pixels(stage,'--chart-ring-stroke',7);
   const weight=clamp(wanted,2,Math.max(2,size*.13)),r=fix(Math.max(1,(size-weight)/2-1)),cx=fix(width/2),cy=fix(open?height/2+r*.12:height/2);
   state.box={cx,cy,r,weight,size};
   for(const path of [track,value,light])put(path,{'stroke-width':weight});
   put(track,{d:arcPath(cx,cy,r,START,START+SWEEP)});
   put(glowPaint,{x1:fix(cx+r),y1:fix(cy-r),x2:fix(cx-r),y2:fix(cy+r)});put(strokePaint,{x1:fix(cx+r),y1:fix(cy-r),x2:fix(cx-r),y2:fix(cy+r)});
  }
  function place(f){
   const {cx,cy,r,weight}=state.box,to=START+SWEEP*f;
   /* Zero keeps its starting dot: the round cap of an arc with no length. */
   const d=f<=0?arcPath(cx,cy,r,START,START+.01):arcPath(cx,cy,r,START,Math.min(to,START+359.99));
   put(value,{d});put(light,{d});
   const [kx,ky]=polar(cx,cy,r,to);
   put(knob,open?{cx:kx,cy:ky,r:fix(weight*.9),visibility:'visible'}:{visibility:'hidden',r:0});
   state.shown=f;
  }
  function render(reason){
   box();
   if(!frame.laidOut()){frame.stop();place(state.fraction);state.shown=0;return;}
   if(reason==='resize'&&frame.painted){frame.stop();place(state.fraction);return;}
   frame.stop();
   const from=frame.painted?state.shown:0,to=state.fraction,fresh=!frame.painted;
   frame.painted=true;stage.dataset.painted='true';
   frame.running=tween(duration(stage,fresh?'--chart-draw-duration':'--chart-change-duration',fresh?700:240),easing(stage,'--chart-ease'),progress=>place(from+(to-from)*progress),()=>{frame.running=null;});
  }
  const handle={kind,el:host,stage,
   update(nextFraction,nextOpts){
    state.opts={...(nextOpts||{})};state.fraction=share(nextFraction);
    stage.dataset.surface=surfaceOf(host,state.opts.surface);stage.dataset.zero=String(state.fraction<=0);
    describe(stage,state.opts.label);
    render(frame.painted?'change':'first');
    return handle;
   },
   get fraction(){return state.shown;},
   destroy(){frame.release();}
  };
  handles?.set(host,handle);
  frame.watch(()=>render('resize'));
  return handle.update(fraction,opts);
 }
 const ring=(host,fraction,opts)=>gauge(host,fraction,opts,'ring');
 const arc=(host,fraction,opts)=>gauge(host,fraction,opts,'arc');

 /* ---- bars -------------------------------------------------------------------------------------- */
 function bars(host,values,opts){
  if(!host||!doc)return null;
  const reuse=existing(host,'bars');
  if(reuse){reuse.update(values,opts);return reuse;}
  const frame=mount(host,'bars',{width:96,height:56}),{stage,glow,ink,id}=frame;
  const litPaint=svg('linearGradient',{id:`${id}-lit`,gradientUnits:'userSpaceOnUse',x1:'0',x2:'0'},svg('defs',null,ink));
  svg('stop',{offset:'0',class:'rocoto-stop-cool'},litPaint);svg('stop',{offset:'1',class:'rocoto-stop-warm'},litPaint);
  const glowPaint=svg('linearGradient',{id:`${id}-glow`,gradientUnits:'userSpaceOnUse',x1:'0',x2:'0'},svg('defs',null,glow));
  svg('stop',{offset:'0',class:'rocoto-glow-cool'},glowPaint);svg('stop',{offset:'1',class:'rocoto-glow-warm'},glowPaint);
  const light=svg('path',{class:'rocoto-bars-light',fill:`url(#${id}-glow)`},glow),group=svg('g',{class:'rocoto-bars-group'},ink);
  const labels=element('ol','rocoto-bars-labels',stage);
  const state={source:[],samples:[],shown:[],opts:{},lit:-1,hover:null,columns:[]};
  function litIndex(){
   if(state.hover!==null)return state.hover;
   const wanted=state.opts.highlight===undefined?'last':state.opts.highlight,n=state.samples.length;
   if(wanted===null||wanted===false||!n)return -1;
   if(wanted==='max'){let best=-1,top=-Infinity;state.samples.forEach((v,i)=>{if(v!==null&&v>top){top=v;best=i;}});return best;}
   if(wanted==='last'){for(let i=n-1;i>=0;i--)if(state.samples[i]!==null)return i;return -1;}
   return finite(wanted)&&wanted>=0&&wanted<n?wanted:-1;
  }
  function place(heights){
   const {width,height}=frame,large=stage.dataset.size==='large',labelled=large&&state.opts.labels!==false,n=heights.length;
   const top=labelled?30:large?8:2,bottom=labelled?30:0,base=height-bottom,room=Math.max(4,base-top),stub=large?4:3;
   const valid=state.samples.filter(v=>v!==null),peak=Math.max(...valid,0)||1;
   const gap=large?Math.max(8,width/Math.max(1,n)*.28):finite(state.opts.gap)?state.opts.gap:4;
   const column=large?width/Math.max(1,n):0;
   const barWidth=large?Math.min(column-gap,84):clamp((width-gap*(n-1))/Math.max(1,n),2,12);
   const used=large?width:barWidth*n+gap*Math.max(0,n-1),offset=large?0:width-used;
   const radius=large?Math.min(10,barWidth*.2):Math.min(3,barWidth/2),lit=litIndex();
   while(group.childNodes.length>n)group.lastChild.remove();
   state.columns=[];let lightPath='';
   heights.forEach((value,index)=>{
    const node=group.childNodes[index]||svg('path',null,group);
    const x=large?column*index+(column-barWidth)/2:offset+index*(barWidth+gap);
    const tall=value===null?0:stub+Math.max(0,value)/peak*(room-stub),y=base-tall;
    const d=value===null?'':barPath(x,y,barWidth,base,radius);
    put(node,{d,class:index===lit?'rocoto-bar is-lit':'rocoto-bar'});
    /* The lit bar takes its paint inline: a stylesheet rule outranks a fill attribute. */
    node.style.fill=index===lit&&!large?`url(#${id}-lit)`:'';
    if(index===lit){lightPath=d;put(litPaint,{y1:fix(y),y2:fix(base)});put(glowPaint,{y1:fix(y),y2:fix(base)});}
    state.columns.push({x:x+barWidth/2,top:y,left:large?column*index:x-gap/2,right:large?column*(index+1):x+barWidth+gap/2});
   });
   put(light,{d:lightPath});state.lit=lit;state.shown=heights.slice();
   labels.hidden=!labelled;
   if(labelled){
    while(labels.childNodes.length>n)labels.lastChild.remove();
    state.source.forEach((point,index)=>{
     const item=labels.childNodes[index]||(()=>{const li=element('li','',labels);element('strong','',li);element('span','',li);return li;})();
     const value=state.samples[index],text=typeof state.opts.format==='function'?state.opts.format(value,point,index):plain(value);
     item.firstChild.textContent=text;item.lastChild.textContent=point!==null&&typeof point==='object'?String(point.label??''):'';
     item.style.left=`${fix(state.columns[index].x)}px`;item.style.setProperty('--bar-top',`${fix(state.columns[index].top)}px`);item.style.setProperty('--bar-column',`${fix(Math.max(24,column-6))}px`);
     item.classList.toggle('is-lit',index===lit);
    });
   }
  }
  function render(reason){
   frame.measure();
   const next=state.samples,previous=state.shown;
   if(!frame.laidOut()){frame.stop();place(next);return;}
   if(reason==='hover'||reason==='resize'&&frame.painted){frame.stop();place(next);return;}
   frame.stop();
   const fresh=!frame.painted,same=!fresh&&previous.length===next.length;
   const from=same?previous:next.map(v=>v===null?null:0);
   frame.painted=true;stage.dataset.painted='true';
   frame.running=tween(duration(stage,fresh?'--chart-draw-duration':'--chart-change-duration',fresh?700:240),easing(stage,'--chart-ease'),progress=>{
    place(next.map((value,i)=>value===null?null:(from[i]??0)+(value-(from[i]??0))*progress));
   },()=>{place(next);frame.running=null;});
  }
  const columnAt=clientX=>{const x=clientX-stage.getBoundingClientRect().left;return state.columns.findIndex(c=>x>=c.left&&x<c.right);};
  const onMove=event=>{if(!state.opts.hover)return;const index=columnAt(event.clientX),next=index>=0&&state.samples[index]!==null?index:null;if(next===state.hover)return;state.hover=next;render('hover');if(typeof state.opts.onFocus==='function')state.opts.onFocus(next,next===null?null:state.source[next]);};
  const onLeave=()=>{if(state.hover===null)return;state.hover=null;render('hover');if(typeof state.opts.onFocus==='function')state.opts.onFocus(null,null);};
  stage.addEventListener('pointermove',onMove);stage.addEventListener('pointerleave',onLeave);
  const handle={kind:'bars',el:host,stage,
   update(nextValues,nextOpts){
    state.opts={...(nextOpts||{})};state.source=Array.isArray(nextValues)?nextValues:[];state.samples=state.source.map(valueOf);state.hover=null;
    stage.dataset.size=state.opts.size==='large'?'large':'tile';stage.dataset.surface=surfaceOf(host,state.opts.surface);
    /* Labelled bars are read as a list; bare bars read as one picture. */
    if(stage.dataset.size==='large'&&state.opts.labels!==false){stage.removeAttribute('role');stage.removeAttribute('aria-hidden');if(state.opts.label)labels.setAttribute('aria-label',state.opts.label);}
    else describe(stage,state.opts.label);
    render(frame.painted?'change':'first');
    return handle;
   },
   get lit(){return state.lit;},
   destroy(){frame.release();}
  };
  handles?.set(host,handle);
  frame.watch(()=>render('resize'));
  return handle.update(values,opts);
 }

 /* ---- the mark a waiting measure keeps: the same shape, drawn in dots ----------------------------- */
 function ghost(host,kind,opts){
  if(!host||!doc)return null;
  handles?.get(host)?.destroy();
  const shape=kind==='ring'||kind==='arc'||kind==='bars'?kind:'line',frame=mount(host,'ghost',shape==='line'?LINE_SIZES.tile:{width:92,height:92}),{stage,ink}=frame;
  frame.glow.remove();stage.dataset.shape=shape;stage.setAttribute('aria-hidden','true');
  const path=svg('path',{class:'rocoto-ghost-path'},ink);
  function render(){
   const {width,height}=frame.measure();stage.dataset.surface=surfaceOf(host,opts?.surface);
   if(shape==='line'){
    const samples=[.34,.3,.46,.62,.5,.56,.78,.9],layout=lineLayout(samples,{width,height,top:height*.18,bottom:height*.18,left:2,right:6,floor:0});
    put(path,{d:strokePath(layout.runs)});
   }else if(shape==='bars'){
    const n=7,gap=4,barWidth=clamp((width-gap*(n-1))/n,2,12),used=barWidth*n+gap*(n-1);let d='';
    [.3,.5,.42,.66,.58,.82,1].forEach((v,i)=>{d+=barPath(width-used+i*(barWidth+gap),height-3-v*(height-6),barWidth,height,Math.min(3,barWidth/2));});
    put(path,{d});
   }else{
    const size=Math.min(width,height),weight=clamp(pixels(stage,'--chart-ring-stroke',7),2,Math.max(2,size*.13)),r=fix(Math.max(1,(size-weight)/2-1));
    put(path,{d:shape==='arc'?arcPath(width/2,height/2+r*.12,r,-120,120):arcPath(width/2,height/2,r,0,360)});
   }
  }
  const handle={kind:'ghost',el:host,stage,update(){render();return handle;},destroy(){frame.release();}};
  handles?.set(host,handle);frame.watch(render);render();
  return handle;
 }

 const api=Object.freeze({line,spark,ring,arc,bars,ghost,geometry});
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 root.RocotoCharts=api;
})(typeof window!=='undefined'?window:globalThis);
