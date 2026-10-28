(function(root){
 'use strict';
 const requestedOverview=new URLSearchParams(location.hash.slice(1)).get('view')==='location';
 let initialized=false,weatherHost=null,view,weather,roomRoot,playerHost,workspace,sessionDialog,pendingSnapshot=null;
 const q=s=>document.querySelector(s);
 async function refreshData(){
  // Embedded Home already has the server's authorized location projection.
  // Load each camera's paths when selected instead of downloading the whole archive.
  if(pendingSnapshot&&!maintained()){applySnapshot(pendingSnapshot);return;}if(maintained())return;
  const primary=data.clips.filter(c=>c.is_primary_trial===true&&c.viewer_enabled!==false);
  let cursor=0;
  async function loadNext(){
   while(cursor<primary.length&&!pendingSnapshot){
    const clip=primary[cursor++];
    if(trackData[clip.id]||!clip.tracks_url)continue;
    try{const r=await fetch(clip.tracks_url);if(r.ok){const tr=await r.json();indexTracks(tr);trackData[clip.id]=tr;}}catch{}
   }
  }
  await Promise.all(Array.from({length:Math.min(3,primary.length)},loadNext));
  if(pendingSnapshot)applySnapshot(pendingSnapshot);
  else view.setData(LocationData.build({clips:data.clips,tracks:trackData,eventTypes:root.monitoringProfile?.event_types,page:data}));
 }
 // One weather chip: on the Home hero beside «View live», in the header over the camera view.
 function placeWeather(home){const chip=weatherHost||(weatherHost=q('#headerWeather')),target=home?q('.home-place'):q('.watch-header');if(chip&&target&&chip.parentElement!==target)target.append(chip);}
 function showOverview(){maintainedReader?.suspend(true);leaveLiveReplay();video.pause();personOutline?.clear();q('#compare').hidden=true;roomRoot.hidden=false;document.body.classList.add('showing-location');q('#locationBack').hidden=true;view.show();placeWeather(true);weather.mountMap();history.replaceState(null,'','#view=location');}
 function showCameras(){maintainedReader?.suspend(false);requestDecodedFrame();if(sessionDialog?.open)sessionDialog.close();view.hide();placeWeather(false);q('#compare').hidden=false;document.body.classList.remove('showing-location');q('#locationBack').hidden=false;history.replaceState(null,'',`#clip=${encodeURIComponent(selected.id)}`);updateOverlay();}
 // The compact session shows the player while the overview keeps the camera view closed. The maintained reader follows
 // the player: it reads for as long as the dialog shows it and rests again once the dialog closes over the overview.
 function closeSession(){if(!sessionDialog)return;video.pause();leaveLiveReplay();personOutline?.clear();playerHost.append(workspace);if(document.body.classList.contains('showing-location'))maintainedReader?.suspend(true);history.replaceState(null,'','#view=location');}
 function openSession(target){
  if(!target?.clipId)return;leaveLiveReplay();video.pause();maintainedReader?.suspend(false);
  if(!sessionDialog){sessionDialog=document.createElement('dialog');sessionDialog.className='compact-session';sessionDialog.id='compactSession';sessionDialog.innerHTML='<header><h2>Visitor session</h2><button class="session-full" type="button">Open cameras</button><button class="session-close" aria-label="Close visitor session">×</button></header><div class="compact-session-body"></div>';document.body.append(sessionDialog);sessionDialog.querySelector('.session-close').onclick=()=>sessionDialog.close();sessionDialog.querySelector('.session-full').onclick=()=>{sessionDialog.close();showCameras();};sessionDialog.addEventListener('close',closeSession);}
  sessionDialog.querySelector('.compact-session-body').append(workspace);sessionDialog.showModal();
  setClip(target.clipId);if(target.personId!==null&&target.personId!==undefined)selectPerson(target.personId);seek(target.time_s||0);sessionDialog.querySelector('h2').textContent=target.personId?personTitle(target.personId)+' · session':'Visitor session';
 }
 // Inside the product frame the way back reads the location's own name.
 function nameBack(name){const back=q('#locationBack');if(typeof name==='string'&&name.trim())back.dataset.place=name.trim();else delete back.dataset.place;}
 function applySnapshot(snapshot){
  if(maintained()||!view||!snapshot||!Array.isArray(snapshot.locations))return;
  // The trackers' tiles, canvases, catalog and baskets belong to the page; the snapshot adds the location's record around them.
  const carried={};for(const key of ['core_trackers','core_tracker_canvases','products','baskets','currency'])if(data?.[key]!==undefined)carried[key]=data[key];
  // The recordings behind Home's footage come from the page's clips; the snapshot carries the location's record.
  const built=LocationData.build({clips:data?.clips||[],tracks:trackData,eventTypes:root.monitoringProfile?.event_types,page:data}).locations||[];
  const footage=(built.find(location=>location.id===snapshot.locations[0]?.id)||built[0])?.recordings;
  if(footage?.length&&!Array.isArray(snapshot.recordings))carried.recordings=footage;
  view.setData({...snapshot,...carried});
  visitBasket?.invalidate();
  const location=snapshot.locations[0];
  if(location){view.setLocation({name:location.name,address:location.address||location.display_name,display_name:location.address||location.display_name});nameBack(location.name);}
 }
 root.addEventListener('message',event=>{
  if(event.source!==root.parent||event.origin!==location.origin||event.data?.type!=='rocoto:location-snapshot')return;
  pendingSnapshot=event.data.snapshot;applySnapshot(pendingSnapshot);
 });
 // Trackers run by themselves. The page asks which revision of its data is published and, once that moves, reads the
 // data again and changes the tiles where they stand: the open full view, the chosen period and the scroll position stay.
 const PAGE_KEYS=['core_trackers','core_tracker_canvases','products','baskets','currency'],REVISION_MS=15000;
 let revision,revisionTag=null,revisionBusy=false,revisionTimer=null,pageRested=false,trackers=null;
 async function askRevision(){
  const response=await fetch('/api/tiles/revision',{credentials:'same-origin',cache:'no-store',headers:revisionTag?{'If-None-Match':revisionTag}:{}});
  if(response.status!==200)return null;
  const value=await response.json();return typeof value.revision==='string'?{revision:value.revision,tag:response.headers.get('ETag'),schedule:value.schedule||null,run:value.run||null}:null;
 }
 // The same answer carries what the tools set and what the last pass did. Home names a location whose trackers run on
 // request, stopped or wait, in the run's own sentence, on a line under the address; a current location shows its tiles alone.
 function trackerState(schedule,run){
  if(!schedule)return {kind:'current',text:''};
  if(schedule.service==='off')return {kind:'off',text:'The tracker service is switched off. The tiles show the last completed pass.'};
  if(schedule.needs_attention)return {kind:'attention',text:[run?.error,'The trackers wait for new observations or a request.'].filter(Boolean).join(' ')};
  if(schedule.automatic===false)return {kind:'on-request',text:'Trackers run on request at this location.'};
  if(run&&['failed','budget_exceeded','interrupted'].includes(run.status))return {kind:'stopped',text:run.error||'The last pass stopped early. The tiles show the last completed pass.'};
  return {kind:'current',text:''};
 }
 function showTrackers(next){
  if(next)trackers={schedule:next.schedule,run:next.run};
  if(!roomRoot||!trackers)return;
  const state=trackerState(trackers.schedule,trackers.run),address=q('#locationHeroAddress');let line=q('#locationTrackerState');
  roomRoot.dataset.trackers=state.kind;
  if(!line&&address){line=document.createElement('p');line.id='locationTrackerState';line.className='home-address';line.setAttribute('role','status');address.after(line);}
  if(line){line.textContent=state.text;line.hidden=!state.text;}
 }
 // Asked while this script loads, ahead of the page's own read of its data, so the page holds data at least this new.
 const firstRevision=askRevision().then(first=>{if(first){revision=first.revision;revisionTag=first.tag;showTrackers(first);}}).catch(()=>{});
 async function refreshPage(){
  const response=await fetch('data.json',{credentials:'same-origin',cache:'no-store'});if(!response.ok)return false;
  const fresh=await response.json();
  for(const key of PAGE_KEYS){if(fresh[key]===undefined)delete data[key];else data[key]=fresh[key];}
  // A new recording joins the page's list; the ones the player holds stay the same objects.
  const known=new Set(data.clips.map(clip=>clip.id));for(const clip of fresh.clips||[])if(!known.has(clip.id))data.clips.push(clip);
  const page=document.scrollingElement,top=page?.scrollTop,inner=roomRoot.scrollTop;
  if(pendingSnapshot)applySnapshot(pendingSnapshot);else view.setData(LocationData.build({clips:data.clips,tracks:trackData,eventTypes:root.monitoringProfile?.event_types,page:data}));
  if(page&&page.scrollTop!==top)page.scrollTop=top;if(roomRoot.scrollTop!==inner)roomRoot.scrollTop=inner;
  return true;
 }
 async function checkRevision(){
  if(!view||document.hidden||pageRested||revisionBusy||maintained())return;revisionBusy=true;
  try{
   await firstRevision;
   const next=await askRevision();if(!next)return;
   showTrackers(next);
   // The same page data under a new tag: the schedule or a run moved, and the line above already says so.
   if(next.revision===revision){revisionTag=next.tag;return;}
   // The page's own data is the newer revision's once it has been read; until then the next check asks again.
   if(await refreshPage()){revision=next.revision;revisionTag=next.tag;}
  }catch{}finally{revisionBusy=false;}
 }
 root.addEventListener('message',event=>{
  if(event.source!==root.parent||event.origin!==location.origin)return;
  if(event.data?.type==='rocoto:player-suspend')pageRested=true;
  if(event.data?.type==='rocoto:player-resume'){pageRested=false;checkRevision();}
 });
 function init(){
  if(initialized)return;initialized=true;document.body.classList.toggle('embedded-viewer',new URLSearchParams(location.search).get('embedded')==='1');workspace=q('#benchmarkWorkspace');playerHost=workspace.parentElement;roomRoot=q('#locationRoot');
  view=LocationView.create({root:roomRoot,data:maintained()?{locations:data.location?[data.location]:[]}:LocationData.build({clips:data.clips,tracks:trackData,eventTypes:root.monitoringProfile?.event_types,page:data}),onOpenCameras:()=>{showCameras();returnToLiveReplay();},onOpenSession:openSession,onLocationChange:()=>weather.open()});
  weather=AreaWeather.create({host:q('#headerWeather'),place:data.location?.weather});nameBack(data.location?.name);
  // The maintained record names its location even while its coordinates await commissioning.
  if(weather.place)view.setLocation(weather.place);else if(maintained()&&data.location)view.setLocation({name:data.location.name,address:data.location.address});
  weather.mountMap();
  q('#locationBack').onclick=showOverview;q('.brand').onclick=e=>{e.preventDefault();showOverview();};
  if(pendingSnapshot)applySnapshot(pendingSnapshot);
  refreshData();if(requestedOverview)showOverview();else q('#locationBack').hidden=false;
  showTrackers();revisionTimer=setInterval(checkRevision,REVISION_MS);document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkRevision();});
 }
 root.ControlRoom={init,showOverview,showCameras,openSession,checkRevision,weatherStatus:()=>weather?.status()};
})(window);
