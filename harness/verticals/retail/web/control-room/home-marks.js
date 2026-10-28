/* Home · the marks a tile carries, drawn here when the shared chart module is away.
 * Same three calls as RocotoCharts: line(el, points, opts), ring(el, fraction, opts), bars(el, values, opts).
 * Paint comes from the stylesheet (location-view.css, .home-mark…): the luminous pair lives in the stroke's
 * gradient and its glow, and nowhere else. */
(function(root){
 'use strict';
 let sequence=0;
 const value=p=>{if(p&&typeof p==='object')p=p.value;const n=Number(p);return p===null||p===undefined||p===''||!Number.isFinite(n)?null:n;};
 const stops=id=>`<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="100%" y2="0"><stop offset="0" class="home-mark-stop-a"/><stop offset="1" class="home-mark-stop-b"/></linearGradient>`;
 /* A smooth curve through every measured point (monotone cubic): it never rises above a point it did not reach. */
 function monotone(points){
  const n=points.length;if(n<2)return '';
  if(n===2)return `M${points[0][0]},${points[0][1]}L${points[1][0]},${points[1][1]}`;
  const dx=[],slope=[],tangent=[];
  for(let i=0;i<n-1;i++){dx[i]=points[i+1][0]-points[i][0];slope[i]=(points[i+1][1]-points[i][1])/dx[i];}
  tangent[0]=slope[0];tangent[n-1]=slope[n-2];
  for(let i=1;i<n-1;i++)tangent[i]=slope[i-1]*slope[i]<=0?0:(slope[i-1]+slope[i])/2;
  for(let i=0;i<n-1;i++){
   if(slope[i]===0){tangent[i]=0;tangent[i+1]=0;continue;}
   const a=tangent[i]/slope[i],b=tangent[i+1]/slope[i],h=Math.hypot(a,b);
   if(h>3){tangent[i]=3/h*a*slope[i];tangent[i+1]=3/h*b*slope[i];}
  }
  let d=`M${points[0][0]},${points[0][1]}`;
  for(let i=0;i<n-1;i++){const [x0,y0]=points[i],[x1,y1]=points[i+1],h=dx[i]/3;d+=`C${(x0+h).toFixed(2)},${(y0+tangent[i]*h).toFixed(2)} ${(x1-h).toFixed(2)},${(y1-tangent[i+1]*h).toFixed(2)} ${x1},${y1}`;}
  return d;
 }
 function clear(el,kind,opts){el.replaceChildren();el.classList.add('home-mark',`home-mark-${kind}`);el.dataset.tone=opts?.tone==='paper'?'paper':'glass';el.setAttribute('aria-hidden','true');}
 /* The glowing no-axis line. Zero is its floor; a tile with one point or none rests as a level line. */
 function line(el,points,opts={}){
  if(!el)return null;clear(el,'line',opts);
  const width=opts.width||320,height=opts.height||88,pad=opts.pad??12,samples=(points||[]).map(value),valid=samples.filter(v=>v!==null),id=`homeMark${++sequence}`;
  let path,end,resting=false;
  if(valid.length<2||valid.every(v=>v===valid[0])){resting=true;const y=+(height*(valid[0]>0?.5:.72)).toFixed(2);path=`M0,${y}L${width},${y}`;end=[width,y];}
  else{
   const max=Math.max(...valid,0),min=Math.min(...valid,0),span=(max-min)||1,last=samples.length-1,runs=[];let run=[];
   samples.forEach((v,i)=>{if(v===null){if(run.length)runs.push(run);run=[];}else run.push([+(i*width/last).toFixed(2),+(height-pad-(v-min)/span*(height-2*pad)).toFixed(2)]);});if(run.length)runs.push(run);
   path=runs.filter(r=>r.length>1).map(monotone).join(' ');end=runs.flat().at(-1);
  }
  el.classList.toggle('is-resting',resting);
  el.innerHTML=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" focusable="false"><defs>${stops(id).replace('x2="100%"',`x2="${width}"`)}</defs><path class="home-mark-glow" d="${path}" stroke="url(#${id})"/><path class="home-mark-stroke" d="${path}" stroke="url(#${id})"/></svg><i class="home-mark-end"></i>`;
  const dot=el.lastElementChild;dot.style.left=`${(end[0]/width*100).toFixed(2)}%`;dot.style.top=`${(end[1]/height*100).toFixed(2)}%`;
  return el;
 }
 /* A ring is a share of a whole; with `arc` it opens at the foot and reads as a gauge. Zero keeps its first dot. */
 function ring(el,fraction,opts={}){
  if(!el)return null;clear(el,opts.arc?'arc':'ring',opts);
  const r=33,full=2*Math.PI*r,sweep=opts.arc?.75:1,track=full*sweep,share=Math.max(0,Math.min(1,Number(fraction)||0)),id=`homeMark${++sequence}`,turn=opts.arc?135:-90;
  el.classList.toggle('is-resting',share===0);
  el.innerHTML=`<svg viewBox="0 0 80 80" focusable="false"><defs><linearGradient id="${id}" x1="0" y1="1" x2="1" y2="0"><stop offset="0" class="home-mark-stop-a"/><stop offset="1" class="home-mark-stop-b"/></linearGradient></defs><circle class="home-mark-track" cx="40" cy="40" r="${r}" stroke-dasharray="${track.toFixed(2)} ${full.toFixed(2)}" transform="rotate(${turn} 40 40)"/><circle class="home-mark-glow" cx="40" cy="40" r="${r}" stroke="url(#${id})" stroke-dasharray="${Math.max(share*track,.01).toFixed(2)} ${full.toFixed(2)}" transform="rotate(${turn} 40 40)"/><circle class="home-mark-stroke" cx="40" cy="40" r="${r}" stroke="url(#${id})" stroke-dasharray="${Math.max(share*track,.01).toFixed(2)} ${full.toFixed(2)}" transform="rotate(${turn} 40 40)"/></svg>`;
  return el;
 }
 /* Bars: one per camera, recording or day; the newest stands brightest. */
 function bars(el,values,opts={}){
  if(!el)return null;clear(el,'bars',opts);
  const samples=(values||[]).map(value).map(v=>v===null?0:v),max=Math.max(...samples,0)||1,count=Math.max(samples.length,1),width=opts.width||320,height=opts.height||88,gap=count>24?2:4,bar=Math.max(2,(width-gap*(count-1))/count),id=`homeMark${++sequence}`;
  el.classList.toggle('is-resting',!samples.some(v=>v>0));
  el.innerHTML=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" focusable="false"><defs>${stops(id).replace('x2="100%"',`x2="${width}"`)}</defs>${samples.map((v,i)=>{const h=Math.max(2,v/max*(height-4));return `<rect class="home-mark-bar${i===count-1?' is-newest':''}" x="${(i*(bar+gap)).toFixed(2)}" y="${(height-h).toFixed(2)}" width="${bar.toFixed(2)}" height="${h.toFixed(2)}" rx="${Math.min(bar/2,3).toFixed(2)}" fill="url(#${id})"/>`;}).join('')}</svg>`;
  return el;
 }
 const api=Object.freeze({line,ring,bars});
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 root.RocotoHomeMarks=api;
})(typeof window!=='undefined'?window:globalThis);
