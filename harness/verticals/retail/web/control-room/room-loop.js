/* Monotonic clock for explicitly labelled prerecorded room replay. */
(function(root){
  'use strict';
  function create(recordings, now=()=>performance.now()/1000){
    const clips=recordings.map(({id,duration_s})=>({id:String(id),duration_s:Number(duration_s)}));
    if(!clips.length||clips.some(c=>!Number.isFinite(c.duration_s)||c.duration_s<=0))throw new Error('Replay requires recordings with positive durations');
    const duration_s=clips.reduce((sum,c)=>sum+c.duration_s,0),epoch=now();
    function positionAt(elapsed_s){
      const elapsed=Number(elapsed_s);
      if(!Number.isFinite(elapsed)||elapsed<0)throw new Error('Replay time must be nonnegative');
      const cycle=Math.floor(elapsed/duration_s),within=elapsed-cycle*duration_s;
      let start=0;
      for(let index=0;index<clips.length;index++){
        const c=clips[index];
        if(within<start+c.duration_s||index===clips.length-1)return {clip_id:c.id,time_s:Math.min(c.duration_s,Math.max(0,within-start)),elapsed_s:elapsed,cycle,index};
        start+=c.duration_s;
      }
    }
    return Object.freeze({duration_s,edge:()=>positionAt(Math.max(0,now()-epoch)),positionAt});
  }
  const api=Object.freeze({create});if(typeof module!=='undefined'&&module.exports)module.exports=api;root.RoomLoop=api;
})(typeof window!=='undefined'?window:globalThis);
