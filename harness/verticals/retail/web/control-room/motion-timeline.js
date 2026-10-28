/* Recorded image-plane movement only: no inferred activity, identity or depth. */
(function(root){
 'use strict';
 function profile(track,personId){
  if(!track||!Number.isFinite(track.fps)||track.fps<=0||!(track.width>0))return [];
  const maxGap=Math.max(1,track.sampleStep||1)*1.5/track.fps;
  return (track.histories?.[personId]||[]).flatMap(history=>{
   let groups=[],group=[],window=[],previous=null,stationary=0;
   for(const p of history){
    const t=p.frame/track.fps;if(![t,p.x,p.y].every(Number.isFinite))continue;
    if(previous&&(t-previous.t>maxGap||t<=previous.t)){if(group.length)groups.push(group);group=[];window=[];previous=null;stationary=0;}
    window=window.filter(q=>t-q.t<=.3);window.push({t,x:p.x,y:p.y});
    const x=window.reduce((s,q)=>s+q.x,0)/window.length,y=window.reduce((s,q)=>s+q.y,0)/window.length;
    const dt=previous?t-previous.t:0,speed=previous?Math.hypot(x-previous.x,y-previous.y)/track.width/dt:0;
    stationary=previous&&speed<=.018?stationary+dt:0;
    group.push({t,stationary_s:stationary,value:Math.sqrt(Math.min(1,stationary/6)),speed});previous={t,x,y};
   }
   if(group.length)groups.push(group);return groups;
  });
 }
 function paths(groups,duration,width=1000,height=42){
  if(!(duration>0))return [];
  return groups.filter(g=>g.length>1).map(g=>{
   // Keep the genuine gaps between observations; smooth only within each span.
   const stride=Math.max(1,Math.floor(g.length/240)),points=g.filter((_,i)=>i%stride===0||i===g.length-1).map(p=>({x:Math.min(width,Math.max(0,p.t/duration*width)),y:height-2-p.value*(height-5)}));
   let line=`M${points[0].x.toFixed(2)},${points[0].y.toFixed(2)}`;
   for(let i=1;i<points.length-1;i++){const a=points[i],b=points[i+1];line+=` Q${a.x.toFixed(2)},${a.y.toFixed(2)} ${((a.x+b.x)/2).toFixed(2)},${((a.y+b.y)/2).toFixed(2)}`;}
   const end=points.at(-1);line+=` L${end.x.toFixed(2)},${end.y.toFixed(2)}`;
   return {line,fill:`${line} L${end.x.toFixed(2)},${height} L${points[0].x.toFixed(2)},${height} Z`};
  });
 }
 function at(groups,time){
  for(const group of groups){if(!group.length||time<group[0].t||time>group.at(-1).t)continue;let result=group[0];for(const point of group){if(point.t>time)break;result=point;}return result;}return null;
 }
 const api=Object.freeze({profile,paths,at});if(typeof module!=='undefined'&&module.exports)module.exports=api;root.MotionTimeline=api;
})(typeof window!=='undefined'?window:globalThis);
