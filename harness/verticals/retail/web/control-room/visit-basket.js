/* Playback-time basket projection. The server owns identity, evidence and prices. */
(function(root){
 'use strict';
 const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const money=price=>`${esc(price.amount)} ${esc(price.currency)}`;
 function create({host,fetcher=(...args)=>fetch(...args),clock=()=>Date.now()}){
  let context=null,request=null,cached=null,generation=0,retryAt=0,destroyed=false,snapshotSignature=null;
  function clear(){snapshotSignature=null;generation++;request?.abort();request=null;context=null;cached=null;retryAt=0;host.hidden=true;host.replaceChildren();}
  function image(url){try{const parsed=new URL(url,location.href);return url&&parsed.origin===location.origin&&/^https?:$/.test(parsed.protocol)?parsed.href:null;}catch{return null;}}
  function row(item,uncertain=false){
   const url=image(item.image),price=item.line_estimate,estimate=item.identity_basis==='vision_estimate'||item.quantity_basis==='estimated';
   const status=uncertain?'Association under review':item.state==='in_basket'?'In basket':'Held';
   return `<article class="basket-item">${url?`<img src="${esc(url)}" alt="" loading="lazy">`:''}<div><strong>${esc(item.description)}</strong><span>${Number.isFinite(item.quantity)?`${esc(item.quantity)} ${estimate?'estimated ':''}${item.quantity===1?'item':'items'}`:'Quantity under review'} · ${status}</span>${price&&!uncertain?`<span class="basket-money">${money(price)} <small>estimated</small></span>`:''}</div></article>`;
  }
  function render(value){
   const current=value.items||[],uncertain=value.uncertain_items||[],pending=value.unresolved_observations||[],totals=value.totals||[],coverage=value.coverage||{};
   host.hidden=false;
   const details=[];
   if(coverage.unpriced_units)details.push(`${coverage.unpriced_units} ${coverage.unpriced_units===1?'item':'items'} awaiting a price`);
   if(coverage.uncertain_instances)details.push(`${coverage.uncertain_instances} item associations under review`);
   if(coverage.unresolved_observations)details.push(`${coverage.unresolved_observations} observations awaiting item details`);
   host.innerHTML=`<h3>Basket estimate</h3>${totals.length?`<div class="basket-totals">${totals.map(t=>`<div><strong class="basket-money">${money(t)}</strong><span>${t.partial?'Priced portion':'Estimated value'} · ${esc(t.priced_units)} ${t.priced_units===1?'item':'items'}</span></div>`).join('')}</div>`:''}${current.map(i=>row(i)).join('')}${uncertain.length?`<details class="basket-uncertain"><summary>Items under review · ${uncertain.length}</summary>${uncertain.map(i=>row(i,true)).join('')}</details>`:''}${details.length?`<p class="basket-coverage">${details.map(esc).join(' · ')}</p>`:''}${!current.length&&!uncertain.length&&!pending.length?'<p class="basket-coverage">Item observations appear here as this visitor handles products.</p>':''}`;
  }
  function showSnapshot(next){
   const signature=JSON.stringify(next);if(signature===snapshotSignature)return;clear();snapshotSignature=signature;if(!next)return;
   host.hidden=false;if(next.status!=='ready'){host.innerHTML='<p class="basket-coverage">'+(next.status==='unavailable'?'Basket connection unavailable. Reconnecting…':'Loading basket…')+'</p>';return;}
   const basket=next.basket;if(!basket){host.innerHTML='<p class="basket-coverage">Item observations appear here as this visitor handles products.</p>';return;}
   render({...basket,totals:[],coverage:{uncertain_instances:basket.uncertain_items?.length||0}});
   const stamp=document.createElement('p');stamp.className='basket-coverage';stamp.textContent=`As of ${Number(next.at_s).toFixed(1)}s`;host.append(stamp);
  }
  function within(value,time){return value&&Number.isFinite(value.validity?.from_s)&&time>=value.validity.from_s&&(value.validity.next_change_s===null||time<value.validity.next_change_s);}
  function update(next){
   if(destroyed)return;
   if(!next||!next.sourceId||next.personId===null||next.personId===undefined||!Number.isFinite(next.time)){clear();return;}
   const key=JSON.stringify([next.sourceId,String(next.personId)]);
   if(context?.key!==key){clear();context={...next,key};}else context={...next,key};
   if(cached&&within(cached.value,next.time)&&clock()-cached.received<10000){host.hidden=false;return;}
   cached=null;
   // Hide a projection at a temporal boundary until its matching response arrives.
   host.hidden=true;host.replaceChildren();
   if(request||clock()<retryAt)return;
   const token=++generation,controller=new AbortController(),requested={...context};request=controller;
   const query=new URLSearchParams({source_id:requested.sourceId,track_id:String(requested.personId),at_s:String(requested.time)});
   fetcher(`/api/retail/visit-basket?${query}`,{signal:controller.signal,credentials:'same-origin',cache:'no-store'}).then(async response=>{
    if(!response.ok)throw Error('Basket projection unavailable');
    const value=await response.json();
    if(controller.signal.aborted||token!==generation||context?.key!==requested.key)return;
    if(value.as_of_s!==requested.time||!within(value,requested.time))throw Error('Basket clock mismatch');
    if(!within(value,context.time))return;
    cached={value,received:clock()};render(value);
   }).catch(()=>{if(token===generation){cached=null;host.hidden=true;host.replaceChildren();retryAt=clock()+5000;}}).finally(()=>{
    if(token!==generation)return;request=null;
    // A slow response may arrive after another event boundary. Fetch current time.
    if(context&&!cached&&clock()>=retryAt)update(context);
   });
  }
  function invalidate(){const previous=context;clear();if(previous)update(previous);}
  const refresh=setInterval(()=>{if(context&&!document.hidden)update(context);},10000);
  function destroy(){clear();destroyed=true;clearInterval(refresh);}
  return {update,showSnapshot,clear,invalidate,destroy};
 }
 root.VisitBasketView={create};
})(window);
