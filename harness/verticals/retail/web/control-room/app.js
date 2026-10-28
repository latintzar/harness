'use strict';
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const name=v=>String(v??'Observation').replaceAll('_',' ');
const seconds=n=>Number.isFinite(n)?`${n.toFixed(1)}s`:'—';
const clock=n=>`${Math.floor(n/60).toString().padStart(2,'0')}:${(n%60).toFixed(1).padStart(4,'0')}`;
const playerClock=t=>{t=Math.max(0,Number(t)||0);return `${Math.floor(t/60)}:${Math.floor(t%60).toString().padStart(2,'0')}`;};
const brief=(value,length=90)=>{const text=String(value??'');return text.length>length?text.slice(0,length-1)+'…':text;};
const actorId=value=>/^(?:Person\s*)?(P?\d+)$/i.exec(String(value??'').trim())?.[1].toUpperCase()??null;
const actorLabel=value=>actorId(value)??String(value??'Unassigned');
const eventActors=e=>[...new Set((Array.isArray(e.person_ids)?e.person_ids:[e.person_id]).map(value=>e.maintained?String(value):actorId(value)).filter(x=>x!==null))];
const eventActorLabel=e=>eventActors(e).map(personTitle).join(', ');
const eventLabel=e=>name(e.event);
const timed=e=>Number.isFinite(e.playback_start_s)&&Number.isFinite(e.playback_end_s)&&e.playback_end_s>=e.playback_start_s;
const activeAt=(e,t)=>timed(e)&&t>=e.playback_start_s-.005&&t<=e.playback_end_s+.005;
const productInteraction=e=>e?.maintained?e.interaction===true:JourneyModel.isProductInteraction(e);
const eventKey=(wi,ei)=>`${wi}:${ei}`;
let data,selected,trackData={},trackLoadError=false,overlayModel='8b',loopLength=0,loopStart=0,selectedPerson=null,selectedEventKey=null,journeyTab='summary',uxData={clips:{}},timelineSignature='',playerUiSignature='',personPickerSignature='',visitorRosterSignature='',trailPointerKey=null,trailFocusKey=null;
let mediaController=null,mediaObjectUrl=null,mediaLoading=false,decodedFrameTime=null,decodedFrameRequest=null,videoFrameRequest=null,evidenceEvent=null,evidenceOrigin='auto',dismissedEvidenceKey=null;
const video=$('#video');
let readAuthority='pending',maintainedReader=null,maintainedState={status:'idle',value:null},maintainedRenderKey='';
const maintained=()=>readAuthority==='maintained';
function mappedTime(time=decodedFrameTime){return maintained()&&selected&&Number.isFinite(time)?MaintainedPlayer.streamTime(selected,time):null;}
function geometryPerson(track,time=decodedFrameTime){return maintained()?MaintainedPlayer.trackerVisit(maintainedState.value,selected,track,time):String(track.id);}
function maintainedChapters(){return MaintainedPlayer.chapters(maintainedState.value,selectedPerson,data?.clips||[]).map(pictured);}
function maintainedEvents(){const result=new Map();for(const visitor of MaintainedPlayer.visitors(maintainedState.value))for(const event of MaintainedPlayer.chapters(maintainedState.value,visitor.id,data.clips)){if(event.clip_id!==selected?.id)continue;const previous=result.get(event.key);if(previous)previous.person_ids.push(visitor.id);else result.set(event.key,pictured(event));}return [...result.values()];}
// The maintained room loop. At the end of one mapped recording playback continues into the next recording of
// the same session and, after the last, returns to the first. The visitor stays selected: setClip keeps the
// selection inside one session, and the rewind at the loop clears later data like any other seek.
let sessionPlaying=false;
function continueSession(){const next=MaintainedPlayer.following(data.clips,selected);if(!next)return;if(next.id!==selected.id)setClip(next.id);seek(next.source_window.start_s,true);}
// Unavailable authority takes the picture with it. The element stops, its source and poster are released and its
// frame is cleared; recording thumbnails and the ambient colour leave as well. The reader keeps asking at the held
// playhead, and its next answer restores the recording there, playing when it was playing.
let pictureReleased=null;
// The camera's own picture stands behind the page and in its thumbnails. A recording published with a poster uses
// that; a recording published bare lends its first decoded frame, sampled small. A visitor or an action published
// without a crop is pictured by the tracker's own box on the decoded frame: the visitor at a sighting, the action on a
// frame decoded within a second of its start. All of it is held only while the picture itself is held.
const sampledFrames=new Map(),portraits=new Map(),eventCrops=new Map();
const poster=clip=>pictureReleased?'':clip?.poster||sampledFrames.get(clip?.id)||'';
const portraitOf=visitor=>visitor?.portrait||(pictureReleased?null:portraits.get(visitor?.id)?.url)||null;
const pictured=event=>{const crop=pictureReleased?null:eventCrops.get(event.key);return crop?{...event,person_crop_url:crop}:event;};
function paintAmbient(){const image=poster(selected);$('.footage').style.setProperty('--camera-ambient',image?`url("${image}")`:'none');}
function sampleFrame(){
 if(!selected||selected.poster||sampledFrames.has(selected.id)||pictureReleased||video.readyState<2||!video.videoWidth)return;
 try{const scale=Math.min(1,320/video.videoWidth),canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(video.videoWidth*scale));canvas.height=Math.max(1,Math.round(video.videoHeight*scale));canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);sampledFrames.set(selected.id,canvas.toDataURL('image/jpeg',.72));}catch{return;}
 paintAmbient();renderGrid();renderJourney();
}
function boxCrop(box,tr){
 // The box at its full height, widened about its centre to the visitor tile's 62 : 80 so the person keeps their surroundings.
 const [left,top,right,bottom]=box,y=Math.max(0,top),height=Math.min(tr.height,bottom)-y,reach=Math.max(right-left,height*62/80)/2,centre=(left+right)/2,x=Math.max(0,centre-reach),width=Math.min(tr.width,centre+reach)-x;
 if(pictureReleased||!video.videoWidth||!(right-left>=8&&height>=8&&width>=8))return null;
 try{const sx=video.videoWidth/tr.width,sy=video.videoHeight/tr.height,scale=Math.min(1,240/Math.max(width*sx,height*sy)),canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(width*sx*scale));canvas.height=Math.max(1,Math.round(height*sy*scale));canvas.getContext('2d').drawImage(video,x*sx,y*sy,width*sx,height*sy,0,0,canvas.width,canvas.height);return {url:canvas.toDataURL('image/jpeg',.8)};}catch{return null;}
}
function capturePortrait(identity,box,tr){
 // The first sighting pictures the visitor; a clearly larger box, up to three more times, replaces a partial one.
 const held=portraits.get(identity),[left,top,right,bottom]=box,area=(Math.min(tr.width,right)-Math.max(0,left))*(Math.min(tr.height,bottom)-Math.max(0,top));
 if(held&&(held.count>=4||area<held.area*1.25))return;
 const crop=boxCrop(box,tr);if(crop)portraits.set(identity,{...crop,area,count:(held?.count||0)+1});
}
function captureEventCrops(t,tracks,tr){
 if(!tr||!tracks.length)return;let added=false;
 for(const e of maintainedEvents()){
  if(eventCrops.has(e.key)||t<e.playback_start_s-.005||t>e.playback_start_s+1)continue;
  const actor=tracks.find(item=>e.person_ids.includes(geometryPerson(item,t))),crop=actor&&boxCrop(actor.box,tr);if(crop){eventCrops.set(e.key,crop.url);added=true;}
 }
 if(added)renderJourney();
}
function heldRead(){const stream=MaintainedPlayer.streamTime(selected,pictureReleased.time);maintainedReader?.update(stream===null?null:{sourceId:selected.source_id,epochId:selected.epoch_id,time:stream,paused:!pictureReleased.playing});updatePlayerUI(pictureReleased.time);}
function releasePicture(){
 if(pictureReleased||!selected)return;
 pictureReleased={time:Number.isFinite(decodedFrameTime)?decodedFrameTime:video.currentTime,playing:!video.paused};
 if(decodedFrameRequest!==null&&video.cancelVideoFrameCallback){video.cancelVideoFrameCallback(decodedFrameRequest);decodedFrameRequest=null;}cancelOverlayFrame();
 mediaController?.abort();mediaController=null;decodedFrameTime=null;mediaLoading=false;
 video.pause();video.removeAttribute('src');video.removeAttribute('poster');video.load();
 sampledFrames.clear();portraits.clear();eventCrops.clear();$('#seekPreviewImage').style.backgroundImage='none';
 if($('#evidenceDialog').open)$('#evidenceDialog').close();$('#evidenceImage').removeAttribute('src');$('#evidenceThumbImage').removeAttribute('src');$('#contextualActivity').replaceChildren();delete $('#contextualActivity').dataset.displaySignature;
 $('.footage').classList.add('frame-pending');paintAmbient();renderGrid();
 $('#eventOverlay').replaceChildren();$('#referenceOverlay').replaceChildren();$('#evidenceThumbnail').hidden=true;$('#seekPreview').hidden=true;
 $('#mediaError').hidden=true;$('#mediaLoading').hidden=false;$('#mediaLoading').textContent='Camera connection unavailable. Reconnecting…';
 updatePlayerUI(pictureReleased.time);
}
function restorePicture(){const held=pictureReleased;pictureReleased=null;renderGrid();paintAmbient();loadClipMedia(selected);loadClipTracks(selected);seek(held.time,held.playing);}
function navigateChapter(chapter){video.pause();if(chapter.clip_id&&chapter.clip_id!==selected.id)setClip(chapter.clip_id);seek(chapter.at);}
function navigateVisitor(id){selectPerson(id);if(!maintained())return;const record=visitorRecords().find(v=>v.id===id);if(!record||tracksAt(decodedFrameTime).some(track=>geometryPerson(track)===id))return;const first=record.firstPoint,clip=data.clips.find(c=>c.source_id===selected.source_id&&c.epoch_id===selected.epoch_id&&c.source_asset_id===first?.source_asset_id);if(clip){video.pause();if(clip.id!==selected.id)setClip(clip.id);seek(Number(first.source_s));}}
// The maintained reader learns of an action with its next read, a moment after the action began. An action that
// begins while the view plays on, after the last read, is cued from the frame that learned of it: its flash and its
// temporary panel run from there. An action already past when the view arrives, as after a seek, keeps its own start.
// A seek or a lost view forgets every cue.
const actionCues=new Map();let cueHorizon=null;
function noteActionCues(){
 if(maintainedState.status!=='ready'){actionCues.clear();cueHorizon=null;return;}
 const events=maintainedEvents(),known=new Set(events.map(e=>e.key));
 for(const key of [...actionCues.keys()])if(!known.has(key))actionCues.delete(key);
 for(const e of events)if(!actionCues.has(e.key)){const arrived=cueHorizon!==null&&e.stream_start_s>cueHorizon,late=decodedFrameTime===null?0:decodedFrameTime-e.playback_start_s;actionCues.set(e.key,arrived&&late>0&&late<=1?decodedFrameTime:e.playback_start_s);}
 cueHorizon=Number(maintainedState.value.playhead_s);
}
const cueStart=e=>maintained()?actionCues.get(e.key)??e.playback_start_s:e.playback_start_s;
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
function receiveMaintained(state){
 // The first answer after an unavailable view restores the picture; the first frame then asks for its own snapshot.
 if(state.status==='ready'&&pictureReleased){maintainedState=state;restorePicture();return;}
 maintainedState=state;noteActionCues();if(state.status==='unavailable')releasePicture();const key=JSON.stringify([state.status,MaintainedPlayer.visitors(state.value).map(v=>[v.id,v.first]),state.value?.snapshot?.visits?.map(v=>[v.visit_id,v.chapters])]);if(key!==maintainedRenderKey){maintainedRenderKey=key;timelineSignature='';renderJourney();}else if(selectedPerson!==null)renderPersonSummary(maintainedChapters());if(decodedFrameTime!==null&&state.status==='ready')updateOverlay(decodedFrameTime);else if(state.status==='unavailable'){personOutline?.clear();$('#trackOverlay').replaceChildren();$('#trailAnnotations').replaceChildren();$('#contextualActivity').hidden=true;updateVisitBasket(decodedFrameTime);}}

const visitBasket=window.VisitBasketView?.create({host:$('#visitBasket')});
function updateVisitBasket(time){if(maintained()){const visible=journeyTab==='summary'&&!visitorListOpen&&selectedPerson!==null;visitBasket?.showSnapshot(visible?{status:maintainedState.status,basket:MaintainedPlayer.visit(maintainedState.value,selectedPerson)?.basket,at_s:maintainedState.value?.playhead_s}:null);return;}visitBasket?.update(journeyTab==='summary'&&!visitorListOpen&&selectedPerson!==null&&overlayModel!=='dataset_reference'?{sourceId:selected.source_recording_id||selected.id,personId:selectedPerson,time:time+(Number.isFinite(selected.source_offset_s)?selected.source_offset_s:0)}:null);}
const personOutline=window.PersonOutline?.create({canvas:$('#personOutline'),video,enabled:false,onStatus:state=>{
 const note=$('#outlineStatus');if(!note)return;note.hidden=!state.enabled;
 note.textContent=state.engine==='failed'?'Highlight unavailable. Switch off and on to retry.':state.engine==='worker-ready'?'Highlight ready':'Loading person highlight…';
}});
let roomReplayClock=null,followingLive=false,roomReplayInterval=null,liveSyncRequested=false,visitorListOpen=false;
const roomReplayClocks=new Map();
let playerViewSuspended=false;
function playerWorkSuspended(){return document.hidden||playerViewSuspended;}
function allEvents(){if(maintained())return maintainedEvents();return (selected?.models[overlayModel]?.windows||[]).flatMap((w,wi)=>(w.events||[]).map((e,ei)=>({...e,offset_s:w.offset_s||0,windowIndex:wi,eventIndex:ei,key:eventKey(wi,ei),event_id:JourneyModel.stableEventId(selected.source_recording_id||selected.id,wi,ei,e),person_keys:eventActors(e).map(id=>JourneyModel.personKey(selected.source_recording_id||selected.id,id)),person_key:eventActors(e).length===1?JourneyModel.personKey(selected.source_recording_id||selected.id,eventActors(e)[0]):null})));}
function objectFrameTime(e){if(Number.isFinite(e.playback_object_box_at_s))return e.playback_object_box_at_s;return Number.isFinite(e.object_box_at_s)?e.object_box_at_s-(e.offset_s||0):null;}
function objectBox(e){const b=e.object_box;return Array.isArray(b)&&b.length===4&&b.every(Number.isFinite)&&b.every(n=>n>=0&&n<=1000)&&b[2]>b[0]&&b[3]>b[1]?b:null;}
function markFramePending(){decodedFrameTime=null;maintainedReader?.invalidate();visitBasket?.clear();personOutline?.clear();$('.video-wrap').style.setProperty('--interaction-flash','0');decodedFrameTime=null;$('.footage').classList.add('frame-pending');$('#trackOverlay').replaceChildren();$('#trailAnnotations').replaceChildren();$('#contextualActivity').hidden=true;$('#eventOverlay').replaceChildren();$('#referenceOverlay').replaceChildren();$('#evidenceThumbnail').hidden=true;}
function acceptDecodedFrame(time){if(playerWorkSuspended()||video.seeking||video.readyState<2||Math.abs(time-video.currentTime)>.4)return;decodedFrameTime=time;mediaLoading=false;$('#mediaLoading').hidden=true;video.removeAttribute('poster');$('.footage').classList.remove('frame-pending');updateOverlay(time);sampleFrame();}
function requestDecodedFrame(){
 if(playerWorkSuspended()||video.readyState<2||video.seeking)return;const controller=mediaController,at=video.currentTime;
 if(decodedFrameRequest===null&&video.requestVideoFrameCallback)decodedFrameRequest=video.requestVideoFrameCallback((_,metadata)=>{decodedFrameRequest=null;if(controller===mediaController)acceptDecodedFrame(metadata.mediaTime);});
 requestAnimationFrame(()=>requestAnimationFrame(()=>{if(playerWorkSuspended()||controller!==mediaController||video.seeking||video.readyState<2||Math.abs(video.currentTime-at)>.1)return;if(decodedFrameRequest!==null&&video.cancelVideoFrameCallback){video.cancelVideoFrameCallback(decodedFrameRequest);decodedFrameRequest=null;}acceptDecodedFrame(at);}));
}
function clearMedia(){if(decodedFrameRequest!==null&&video.cancelVideoFrameCallback){video.cancelVideoFrameCallback(decodedFrameRequest);decodedFrameRequest=null;}markFramePending();mediaController?.abort();mediaController=null;video.pause();video.removeAttribute('src');video.load();if(mediaObjectUrl){URL.revokeObjectURL(mediaObjectUrl);mediaObjectUrl=null;}}
async function loadClipMedia(clip){
 clearMedia();if(pictureReleased){pictureReleased={time:clip.initial_time_s,playing:false};heldRead();return;}const controller=new AbortController();mediaController=controller;mediaLoading=true;video.poster=poster(clip);$('#mediaLoading').hidden=false;$('#mediaLoading').textContent='Loading recording…';$('#mediaError').hidden=true;
 try{
  // Keep the private recording on its authenticated same-origin URL. A fetched
  // Blob URL is rejected by some embedded Chromium contexts as an unsafe media
  // origin, while direct playback preserves the workspace cookie and range seeks.
  if(controller.signal.aborted||selected?.id!==clip.id)return;
  // Loading resets the element's rate; the speed control keeps stating the rate that plays.
  video.src=clip.media;video.load();video.playbackRate=Number($('#playbackSpeed').value)||1;
 }catch(error){if(controller.signal.aborted||selected?.id!==clip.id)return;$('#mediaLoading').textContent='Opening direct playback…';$('#mediaError').hidden=false;$('#mediaError').textContent='Direct playback · seeking is limited by the media host.';video.src=clip.media;}
}
video.addEventListener('error',()=>{if(!selected)return;mediaLoading=false;$('#mediaLoading').hidden=true;$('#mediaError').hidden=false;$('#mediaError').textContent='Recording unavailable · select it again to retry.';});
// Leaving the page ends the bundle reader's work. The maintained reader stays whole for as long as the page lives:
// PlayerLifecycle suspends it at this same event and, when the browser shows the page again (a return from its
// back/forward cache), resumes it with a read at once. A refused read takes the picture; an answered one plays on.
window.addEventListener('pagehide',()=>{visitBasket?.destroy();mediaController?.abort();if(mediaObjectUrl)URL.revokeObjectURL(mediaObjectUrl);});

function sizeTrackOverlay(width,height){const svg=$('#trackOverlay'),scale=width/1920;svg.setAttribute('viewBox',`0 0 ${width} ${height}`);$('.video-wrap').style.aspectRatio=`${width} / ${height}`;svg.style.setProperty('--label-font',`${27*scale}px`);svg.style.setProperty('--event-font',`${24*scale}px`);svg.style.setProperty('--label-stroke',`${Math.max(1,6*scale)}px`);}

function seek(t,play=false,liveSeek=false){if(!Number.isFinite(t)||!selected)return;if(!liveSeek)leaveLiveReplay();if(pictureReleased){pictureReleased.time=Math.max(selected.source_window.start_s,Math.min(t,selected.duration_s));if(play)pictureReleased.playing=true;heldRead();return;}if(video.readyState<1){const clipId=selected.id;video.addEventListener('loadedmetadata',()=>{if(selected.id===clipId)seek(t,play,liveSeek);},{once:true});return;}const target=Math.max(maintained()?selected.source_window.start_s:0,Math.min(t,selected.duration_s,video.duration||selected.duration_s));if(loopLength&&(target<loopStart||target>=loopStart+loopLength))loopStart=Math.floor(target/loopLength)*loopLength;video.currentTime=target;updateOverlay();if(play)video.play().catch(()=>{});}
function indexTracks(tr){tr.frameNumbers=Object.keys(tr.frames).map(Number).sort((a,b)=>a-b);tr.sampleStep=tr.frameNumbers.length>1?Math.max(1,tr.frameNumbers[1]-tr.frameNumbers[0]):1;tr.histories={};for(const frame of tr.frameNumbers)for(const item of tr.frames[String(frame)]){const id=String(item.id),segments=tr.histories[id]||(tr.histories[id]=[]);let segment=segments.at(-1);if(!segment||frame-segment.at(-1).frame>tr.sampleStep*1.5||segment.at(-1).birth_id!==item.birth_id){segment=[];segments.push(segment);}segment.push({frame,birth_id:item.birth_id,x:(item.box[0]+item.box[2])/2,y:item.box[3]});}}

const pendingTracks=new Map();
async function loadClipTracks(clip){
 if(pictureReleased||trackData[clip.id]||!clip.tracks_url)return;
 try{
  if(!pendingTracks.has(clip.id))pendingTracks.set(clip.id,fetch(clip.tracks_url).then(r=>{if(!r.ok)throw Error('Tracker data unavailable');return r.json();}).then(tr=>{if(maintained()&&(tr.source_sha256!==clip.source_sha256||tr.tracker_namespace!==clip.geometry_binding?.tracker_namespace))throw Error('Tracking connection unavailable');indexTracks(tr);trackData[clip.id]=tr;return tr;}));
  const tr=await pendingTracks.get(clip.id);if(selected?.id!==clip.id)return;
  sizeTrackOverlay(tr.width,tr.height);personPickerSignature='';renderJourney();updateOverlay();
 }catch{pendingTracks.delete(clip.id);if(selected?.id===clip.id){$('#mediaError').hidden=false;$('#mediaError').textContent='Reconnect this recording to load its person paths.';}}
}

function setClip(id,fromGrid=false,liveTransition=false){
 const previous=selected,previousPerson=selectedPerson,previousList=visitorListOpen,previousTab=journeyTab;if(liveTransition)liveSyncRequested=true;else leaveLiveReplay();selected=data.clips.find(c=>c.id===id);if(!selected)return;$('.source-credit').hidden=!selected.id.startsWith('intel-');
 $('#benchmarkWorkspace').hidden=false;$('#pendingPrimary').hidden=true;$('#benchmarkWorkspace').classList.add('primary-trial');
 if(!selected.models[overlayModel])overlayModel=Object.keys(selected.models)[0]||'8b';
 $('#overlayModel').value=overlayModel;for(const option of $('#overlayModel').options)option.disabled=!selected.models[option.value];
 clearTrailPreview();visitorListOpen=true;visitorRosterSignature='';timelineSignature='';playerUiSignature='';journeyTab='summary';selectedPerson=null;selectedEventKey=null;personPickerSignature='';evidenceEvent=null;evidenceOrigin='auto';dismissedEvidenceKey=null;$('#evidenceThumbnail').hidden=true;
 if(maintained()&&previous?.source_id===selected.source_id&&previous?.epoch_id===selected.epoch_id){selectedPerson=previousPerson;visitorListOpen=previousList;journeyTab=previousTab;}loadClipMedia(selected);loopStart=0;$('#playhead').max=selected.duration_s;$('#playhead').value=0;
 const tr=trackData[selected.id];sizeTrackOverlay(tr?.width||selected.width||1920,tr?.height||selected.height||1080);
 paintAmbient();$('#clipTitle').textContent=selected.title;
 document.querySelectorAll('.clip').forEach(b=>{b.classList.toggle('selected',b.dataset.clip===id);b.setAttribute('aria-pressed',String(b.dataset.clip===id));});
 loadClipTracks(selected);renderJourney();updateOverlay();if(Number.isFinite(selected.initial_time_s)&&!liveTransition)seek(selected.initial_time_s);
 if(fromGrid)$('.room-switcher')?.removeAttribute('open');history.replaceState(null,'',`#clip=${encodeURIComponent(id)}`);
}

function renderGrid(){
 const clips=data.clips.filter(c=>c.is_primary_trial===true&&c.viewer_enabled!==false),card=c=>`<button class="clip${c.id===selected?.id?' selected':''}" data-clip="${esc(c.id)}" aria-pressed="${c.id===selected?.id}"><span class="clip-image">${poster(c)?`<img src="${esc(poster(c))}" alt="" loading="lazy">`:''}<time>${playerClock(c.duration_s)}</time></span><span class="clip-text"><strong>${esc(c.title)}</strong></span></button>`;
 $('#clipGrid').innerHTML=clips.map(card).join('');document.querySelectorAll('.clip[data-clip]').forEach(b=>b.addEventListener('click',()=>setClip(b.dataset.clip,true)));
}

function tracksAt(t){
 if(!Number.isFinite(t))return [];const tr=trackData[selected?.id];if(!tr)return [];
 const numbers=tr.frameNumbers,frame=Math.floor(t*tr.fps+.001);let lo=0,hi=numbers.length-1,best=-1;
 while(lo<=hi){const mid=(lo+hi)>>1;if(numbers[mid]<=frame){best=mid;lo=mid+1;}else hi=mid-1;}
 if(best<0||frame-numbers[best]>Math.max(1,tr.sampleStep))return [];
 return tr.frames[String(numbers[best])]||[];
}
function smoothTrailPath(points){
 if(points.length<2)return '';let d=`M${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
 for(let i=1;i<points.length-1;i++){const p=points[i],n=points[i+1];d+=` Q${p.x.toFixed(1)} ${p.y.toFixed(1)} ${((p.x+n.x)/2).toFixed(1)} ${((p.y+n.y)/2).toFixed(1)}`;}
 const last=points.at(-1);return d+` L${last.x.toFixed(1)} ${last.y.toFixed(1)}`;
}
function trailShapes(tr,t){
 if(selectedPerson===null||!tr||!$('#trackTrails').checked)return '';
 const minFrame=0,maxFrame=t*tr.fps+.001,cell=tr.width/60;let out='';
 // The ramp runs between two theme colours: pale yellow at rest, red where the visitor stays.
 const color=heat=>`color-mix(in srgb, var(--camera-trail-dwell) ${(heat*100).toFixed(1)}%, var(--camera-trail-rest))`;
 for(const [id,segments] of Object.entries(tr.histories||{})){if(!maintained()&&selectedPerson!==null&&String(id)!==selectedPerson)continue;for(const segment of segments){if(maintained()&&geometryPerson({id,birth_id:segment[0]?.birth_id},t)!==selectedPerson)continue;
  const points=segment.filter(point=>point.frame>=minFrame&&point.frame<=maxFrame);if(points.length<2)continue;
  
  const bins=new Map(),key=p=>`${Math.floor(p.x/cell)},${Math.floor(p.y/cell)}`;
  for(const point of points)bins.set(key(point),(bins.get(key(point))||0)+tr.sampleStep/tr.fps);
  const heat=p=>Math.min(1,(bins.get(key(p))||0)/3),stride=Math.max(1,Math.round(tr.fps/10)),draw=points.filter((p,i)=>i%stride===0||i===points.length-1),opacity=.9;
  let chunk=[],lastColor='',lastHeat=0;
  const flush=()=>{if(chunk.length>1)out+=`<path class="person-trail dwell-trail" data-trail-id="${esc(id)}" style="stroke:${lastColor};opacity:${opacity};stroke-width:${(1.6+lastHeat*1.2).toFixed(1)}px;filter:drop-shadow(0 0 ${(1+lastHeat*4).toFixed(1)}px ${lastColor})" d="${smoothTrailPath(chunk)}"/>`;};
  for(let i=0;i<draw.length;i++){const nextColor=color(Math.round(heat(draw[i])*8)/8);if(lastColor&&nextColor!==lastColor){flush();chunk=chunk.length?[chunk.at(-1)]:[];}lastColor=nextColor;lastHeat=Math.round(heat(draw[i])*8)/8;chunk.push(draw[i]);}flush();
  const last=points.at(-1),h=heat(last);if(h>.2)out+=`<circle class="dwell-point" cx="${last.x}" cy="${last.y}" r="${3+h*6}" style="fill:${color(h)}" opacity="${opacity*.65}"/>`;
 }}
 return out;
}
function objectShapes(t,tr){
 if(selectedPerson===null||!$('#overlays').checked||video.readyState<2||video.seeking)return '';
 const width=tr?.width||video.videoWidth||1920,height=tr?.height||video.videoHeight||1080;
 return allEvents().filter(e=>e.eligible_for_overlay!==false&&(selectedPerson===null||eventActors(e).includes(selectedPerson))&&objectBox(e)&&Number.isFinite(objectFrameTime(e))&&Math.abs(t-objectFrameTime(e))<=.125).map(e=>{
  const box=objectBox(e),x=box[0]/1000*width,y=box[1]/1000*height,w=(box[2]-box[0])/1000*width,h=(box[3]-box[1])/1000*height;
  return `<g class="object-location"><rect class="object-box" x="${x}" y="${y}" width="${w}" height="${h}"/><text class="object-label" x="${Math.max(8,Math.min(x,width-590))}" y="${Math.max(32,y-12)}">Object location · model estimate</text></g>`;
 }).join('');
}
function updateOverlay(frameTime){
 if(playerWorkSuspended()||!selected)return;
 if(mediaLoading||decodedFrameTime===null||video.seeking){personOutline?.clear();$('.video-wrap').style.setProperty('--interaction-flash','0');updatePlayerUI(pictureReleased?pictureReleased.time:video.currentTime);$('#trackOverlay').replaceChildren();$('#trailAnnotations').replaceChildren();$('#contextualActivity').hidden=true;$('#eventOverlay').replaceChildren();$('#evidenceThumbnail').hidden=true;return;}
 const t=Number.isFinite(frameTime)?frameTime:decodedFrameTime;if(maintained()&&sessionPlaying&&!video.paused&&t>=selected.source_window.end_s){continueSession();return;}if(maintained()){const stream=mappedTime(t);maintainedReader?.update(stream===null?null:{sourceId:selected.source_id,epochId:selected.epoch_id,time:stream,paused:video.paused});}const tracks=tracksAt(t),tr=trackData[selected.id];const active=selectedEvents().filter(e=>activeAt(e,t)),interactionFlash=reducedMotion.matches?null:JourneyModel.majorInteractionAt(allEvents().filter(e=>e.eligible_for_overlay!==false).map(e=>maintained()?{...e,playback_start_s:cueStart(e)}:e),selectedPerson,t);$('.video-wrap').style.setProperty('--interaction-flash',String(interactionFlash?.opacity??0));
 const outlined=tracks.find(p=>geometryPerson(p,t)===selectedPerson&&selectedPerson!==null);if(outlined&&$('#overlays').checked)personOutline?.update({box:outlined.box,otherBoxes:tracks.filter(p=>p!==outlined).map(p=>p.box),personId:`${selected.id}:${selectedPerson}`,time:t,sourceWidth:tr.width,sourceHeight:tr.height});else personOutline?.clear();
 $('#clock').textContent=clock(t);$('#playhead').value=t;$('#playheadTime').textContent=clock(t);
 let shapes=trailShapes(tr,t);
 for(const item of tracks){
  const identity=geometryPerson(item,t),chosen=identity!==null&&identity===selectedPerson,[left,top,right,bottom]=item.box,x=Math.max(0,left),y=Math.max(0,top),width=Math.max(0,Math.min(tr.width,right)-x),height=Math.max(0,Math.min(tr.height,bottom)-y);
  if(maintained()&&identity===null)continue;if(maintained()&&!pictureReleased)capturePortrait(identity,item.box,tr);shapes+=`<g class="person-track ${chosen?'selected-person':'other-person'}" data-track-id="${esc(item.id)}" ${identity!==null?`data-visit-id="${esc(identity)}"`:''}><rect class="track-box" x="${x}" y="${y}" width="${width}" height="${height}"/>${chosen?`<text class="person-label" x="${Math.max(8,x)}" y="${Math.max(30,y-13)}">${esc(personTitle(identity))}</text>`:''}</g>`;
 }
 shapes+=objectShapes(t,tr);$('#trackOverlay').innerHTML=shapes;renderTrailAnnotations(t);updateJourneyActive(active);refreshVisitorHistory();updateAutomaticEvidence(t);
 $('#eventOverlay').replaceChildren();$('#evidenceThumbnail').hidden=true;
 if(maintained())captureEventCrops(t,tracks,tr);
 renderContextualActivity(t);
 updatePlayerUI(t);updateVisitBasket(t);if(maintained())renderDurationTimeline();
 if($('#loop').checked&&loopLength&&t>=Math.min(loopStart+loopLength,video.duration)-.05){seek(loopStart);video.play().catch(()=>{});}
}
// A bundle's tracker names a person «P1» or «1». The title reads the number; where one recording holds both
// «P1» and «1», each keeps its own name.
function personTitle(id){if(maintained()){const index=visitorRecords().findIndex(v=>v.id===id);return index>=0?`Visitor ${index+1}`:'Visitor';}const name=String(id),number=name.replace(/^P/i,'');return `Visitor ${number!==name&&Object.hasOwn(trackData[selected?.id]?.histories||{},number)?name:number}`;}
function selectPerson(id){
 if(maintained()&&!visitorRecords().some(visitor=>visitor.id===id))return;
 if(id===undefined||id===null)return;visitorListOpen=false;const next=String(id);if(selectedPerson!==next){personOutline?.clear();clearTrailPreview();selectedPerson=next;selectedEventKey=null;dismissedEvidenceKey=null;timelineSignature='';playerUiSignature='';evidenceEvent=null;evidenceOrigin='auto';$('#evidenceThumbnail').hidden=true;}
 renderJourney();updateOverlay();
}
function enterRoomMode(){
 selectedPerson=null;selectedEventKey=null;visitorListOpen=true;clearTrailPreview();personOutline?.clear();timelineSignature='';playerUiSignature='';evidenceEvent=null;evidenceOrigin='auto';dismissedEvidenceKey=null;
 $('#contextualActivity').hidden=true;$('#evidenceThumbnail').hidden=true;renderJourney();updateOverlay();
}

function cyclePerson(direction){
 if(maintained()){const ids=visitorRecords().map(v=>v.id);if(ids.length){const current=ids.indexOf(selectedPerson),base=current<0?(direction>0?-1:0):current;navigateVisitor(ids[(base+direction+ids.length)%ids.length]);}return;}
 const tr=trackData[selected?.id],ids=Object.keys(tr?.histories||{}).sort((a,b)=>(tr.histories[a][0][0].frame-tr.histories[b][0][0].frame)||a.localeCompare(b,undefined,{numeric:true}));if(!ids.length)return;
 const current=ids.indexOf(selectedPerson),base=current<0?(direction>0?-1:0):current,next=ids[(base+direction+ids.length)%ids.length];selectPerson(next);
 if(!tracksAt(video.currentTime).some(p=>String(p.id)===next)){const event=selectedEvents()[0],first=tr.histories[next][0][0].frame/tr.fps;video.pause();seek(event?.playback_start_s??first);}
}
function videoPoint(event){
 const tr=trackData[selected?.id];if(!tr)return null;const rect=video.getBoundingClientRect(),scale=Math.min(rect.width/tr.width,rect.height/tr.height),left=rect.left+(rect.width-tr.width*scale)/2,top=rect.top+(rect.height-tr.height*scale)/2;
 return {x:(event.clientX-left)/scale,y:(event.clientY-top)/scale};
}
function choosePersonAtPointer(event){
 if(event.button!==0||video.seeking||decodedFrameTime===null||event.target.closest('button,input,select,.player-controls,.player-settings,.timeline-dock,.evidence-thumbnail,.trail-annotation'))return;
 // Hit-test the boxes that are painted: under the maintained reader that is every box bound to a visit.
 const point=videoPoint(event);if(!point)return;const hits=tracksAt(decodedFrameTime).filter(p=>point.x>=p.box[0]&&point.x<=p.box[2]&&point.y>=p.box[1]&&point.y<=p.box[3]&&geometryPerson(p)!==null);if(!hits.length)return;
 hits.sort((a,b)=>(a.box[2]-a.box[0])*(a.box[3]-a.box[1])-(b.box[2]-b.box[0])*(b.box[3]-b.box[1]));event.preventDefault();event.stopPropagation();selectPerson(geometryPerson(hits[0]));
}
function eventAnchor(e){if(maintained()){if(e.clip_id!==selected.id)return null;const track=tracksAt(e.playback_start_s).find(item=>geometryPerson(item,e.playback_start_s)===selectedPerson&&selectedPerson!==null);return track?{x:(track.box[0]+track.box[2])/2,y:track.box[3]}:null;}return JourneyModel.eventAnchorFromTracks(trackData[selected?.id],selectedPerson,e.playback_start_s);}
function clearTrailPreview(){trailPointerKey=null;trailFocusKey=null;}
function setTrailPreview(key,source){
 if(source==='pointer')trailPointerKey=key;else trailFocusKey=key;
 if(decodedFrameTime!==null&&!video.seeking){renderTrailAnnotations(decodedFrameTime);renderContextualActivity(decodedFrameTime);}
}
function eventImage(e){return e?.object_crop_verified&&e?.object_crop_url?e.object_crop_url:e?.person_crop_url||e?.snapshot_url||null;}
function renderContextualActivity(t){
 const node=$('#contextualActivity');if(selectedPerson===null){node.hidden=true;return;}const events=selectedEvents(),activity=JourneyModel.activityAt(events,selectedPerson,t),timedEvent=activity?events.find(e=>e.event_id===activity.event_id||e.key===activity.event_id):null;
 const moment=events.filter(e=>!genericObservation(e)&&(maintained()?t>=cueStart(e):activeAt(e,t))&&t<cueStart(e)+1.5).at(-1),event=timedEvent||moment;
 node.hidden=!!(trailPointerKey||trailFocusKey)||!($('#overlays').checked&&(activity||event));if(node.hidden)return;
 const label=activity?.label||eventTitle(event),image=eventImage(event),description=event?.item_description||'',identity=activity?.event_id||event?.event_id||label,signature=`${identity}:${label}:${image||''}`;
 if(node.dataset.displaySignature!==signature){
  node.dataset.displaySignature=signature;node.dataset.currentEvent=event?.key||'';
  node.innerHTML=`${image?`<button class="contextual-event-photo" data-current-event="${esc(event.key)}" aria-label="Open item image"><img src="${esc(image)}" alt="${event.object_crop_verified?'Item detail':'Action evidence'}"></button>`:''}<div class="contextual-copy"><strong>${esc(label)}</strong>${description?`<span>${esc(brief(description,80))}</span>`:''}</div>${activity?'<time></time>':''}`;
 }
 const timer=node.querySelector('time');if(timer&&activity)timer.textContent=activity.elapsed_s<60?`${Math.max(0,activity.elapsed_s).toFixed(1)}s`:playerClock(activity.elapsed_s);
}
function renderTrailAnnotations(t){
 const target=$('#trailAnnotations'),tr=trackData[selected?.id];if(!tr||!$('#overlays').checked){target.replaceChildren();return;}
 const previewKey=trailPointerKey||trailFocusKey,events=selectedEvents().filter(e=>!genericObservation(e)&&(e.key===previewKey||t>=e.playback_start_s)),previewEvent=events.find(e=>e.key===previewKey),imageEvent=previewEvent,rect=video.getBoundingClientRect(),scale=Math.min(rect.width/tr.width,rect.height/tr.height),ox=(rect.width-tr.width*scale)/2,oy=(rect.height-tr.height*scale)/2,compact=rect.width<600,cardWidth=compact?115:156,clearance=compact?88:144,wanted=new Set();
 for(const e of events){
  const anchor=eventAnchor(e);if(!anchor)continue;wanted.add(e.key);
  const x=ox+anchor.x*scale,y=oy+anchor.y*scale,active=activeAt(e,t),isPreview=e.key===previewKey,isCard=e.key===imageEvent?.key,label=`${eventTitle(e)} at ${playerClock(e.playback_start_s)}`;
  let node=Array.from(target.children).find(child=>child.dataset.trailEvent===e.key);
  if(!node){node=document.createElement('button');node.dataset.trailEvent=e.key;node.dataset.eventId=e.event_id;node.style.opacity='0';node.style.transition='opacity 160ms ease';target.append(node);requestAnimationFrame(()=>{if(node.isConnected&&!node.classList.contains('is-leaving'))node.style.opacity='1';});}
  clearTimeout(node.fadeTimer);if(isCard&&node.cardExitTimer){clearTimeout(node.cardExitTimer);node.cardExitTimer=null;}const className=`${isCard?'trail-annotation':'trail-event-marker'}${active?' active':''}${e.key===selectedEventKey?' selected':''}${isPreview?' is-preview':''}`;if(!node.cardExitTimer)node.style.opacity='1';node.style.pointerEvents='';node.style.left=`${x}px`;node.style.top=`${y}px`;node.setAttribute('aria-label',isCard&&e.snapshot_url?`Open ${label} image`:label);node.title=label;
  let markup;
  if(!isCard)markup=`<span class="trail-event-dot"></span><span class="trail-event-time">${playerClock(e.playback_start_s)}</span>`;
  else{
   const image=e.object_crop_verified&&e.object_crop_url?e.object_crop_url:e.person_crop_url||e.snapshot_url,cropLabel=e.object_crop_verified&&e.object_crop_url?'Item detail':e.person_crop_url?'Interaction detail':'Camera frame',lift=Math.max(19,y-(rect.height-clearance)),left=Math.max(8,Math.min(rect.width-cardWidth-8,x+12))-x;
   node.style.setProperty('--tag-lift',`${lift}px`);node.style.setProperty('--tag-left',`${left}px`);
   markup=`<span class="annotation-anchor"></span><span class="annotation-connector"></span><span class="annotation-card twelvelabs-card-material">${image?`<img src="${esc(image)}" alt="${cropLabel}">`:''}<span><strong>${esc(eventTitle(e))}</strong><time>${playerClock(e.playback_start_s)}</time>${image?`<small>${cropLabel}</small>`:`<small>${esc(brief(eventDescription(e),90))}</small>`}</span></span>`;
  }
  if(node.isCard&&!isCard){
   if(!node.cardExitTimer){node.style.opacity='0';node.cardExitTimer=setTimeout(()=>{node.className=className;node.innerHTML=markup;node.renderedMarkup=markup;node.isCard=false;node.cardExitTimer=null;node.style.opacity='1';},160);}
  }else{node.className=className;if(node.renderedMarkup!==markup){node.innerHTML=markup;node.renderedMarkup=markup;}node.isCard=isCard;}
 }
 for(const node of Array.from(target.children))if(!wanted.has(node.dataset.trailEvent)&&!node.classList.contains('is-leaving')){
  node.classList.add('is-leaving');node.style.opacity='0';node.style.pointerEvents='none';node.fadeTimer=setTimeout(()=>{if(node.classList.contains('is-leaving'))node.remove();},160);
 }
}
function activateTrailEvent(node){
 const e=selectedEvents().find(e=>e.key===node.dataset.trailEvent);if(!e)return;selectEvent(e);
 if(node.classList.contains('trail-annotation'))expandEvidence(e.object_crop_verified?'object':!!e.person_crop_url);
}
function queueIntervals(events,id=null){
 const result=[];let entry=null;
 for(const e of events.filter(e=>timed(e)&&(!id||actorId(e.primary_person_id??e.person_id)===id||(eventActors(e).length===1&&eventActors(e)[0]===id))).sort((a,b)=>a.playback_start_s-b.playback_start_s)){
  if(e.event==='entered_queue'&&entry===null)entry=e;
  if(['reached_checkout','left_queue'].includes(e.event)&&entry!==null&&e.playback_start_s>=entry.playback_start_s){result.push({start:entry.playback_start_s,end:e.playback_start_s,entry_key:entry.key,end_key:e.key});entry=null;}
 }
 return {complete:result,pending:entry};
}
function stillnessIntervals(id){
 const tr=trackData[selected.id];if(!tr)return [];
 tr.stillness=tr.stillness||{};if(tr.stillness[id])return tr.stillness[id];const intervals=[],radius=tr.width/60;
 for(const segment of tr.histories?.[id]||[]){let anchor=segment[0],last=anchor;const save=()=>{if(last&&anchor&&(last.frame-anchor.frame)/tr.fps>=2)intervals.push({start:anchor.frame/tr.fps,end:last.frame/tr.fps});};
  for(const point of segment){if(Math.hypot(point.x-anchor.x,point.y-anchor.y)>radius){save();anchor=point;}last=point;}save();}
 return tr.stillness[id]=intervals;
}
function genericObservation(e){return ['other_observation','other_observed_action','miscellaneous','other'].includes(e.event)||e.event_group==='other_observation'&&!productInteraction(e);}
function selectedEvents(){return allEvents().filter(e=>timed(e)&&e.eligible_for_overlay!==false&&eventActors(e).includes(selectedPerson)).sort((a,b)=>a.playback_start_s-b.playback_start_s);}
function displayEventLabel(e){return genericObservation(e)?'Scene note':eventLabel(e);}
function eventTitle(e){return window.JourneyModel?.canonicalActionTitle(e)||(genericObservation(e)?'Observation':displayEventLabel(e).replace(/^./,c=>c.toUpperCase()));}
function eventDescription(e){return window.JourneyModel?.eventDescription(e)||e.item_description||e.evidence||e.person_description||'';}
function sessionClip(point){return data.clips.find(c=>c.source_id===selected.source_id&&c.epoch_id===selected.epoch_id&&c.source_asset_id===point?.source_asset_id);}
function personChapters(){
 // With nobody selected the filmstrip is the room's arrivals, in order: one card for each visitor.
 if(selectedPerson===null&&maintained())return visitorRecords().map((visitor,index)=>{const first=visitor.firstPoint,clip=sessionClip(first);return clip?{at:Number(first.source_s),stream_s:Number(first.stream_s),end:Number(first.source_s),clip_id:clip.id,title:visitor.visibilityKnown?'First observed':'First tracked',description:`Visitor ${index+1}`,personId:visitor.id,image:portraitOf(visitor),event:null,kind:'arrival'}:null;}).filter(Boolean);
 if(selectedPerson===null){
  const events=allEvents().filter(e=>timed(e)&&e.eligible_for_overlay!==false&&['entered_store','enter_store'].includes(e.event));
  return visitorRecords().map(visitor=>{const entered=events.find(e=>eventActors(e).includes(visitor.id)),at=entered?.playback_start_s??visitor.first;return {at,end:at,title:entered?'Entered store':'First observed',description:personTitle(visitor.id),personId:visitor.id,image:visitor.portrait,event:entered||null,kind:'arrival'};}).sort((a,b)=>a.at-b.at||a.personId.localeCompare(b.personId,undefined,{numeric:true}));
 }
 if(maintained()){const rows=maintainedChapters().map(event=>({at:event.playback_start_s,end:event.playback_end_s,stream_s:event.stream_start_s,clip_id:event.clip_id,title:eventTitle(event),description:eventDescription(event),event,kind:'event'})),visitor=visitorRecords().find(v=>v.id===selectedPerson),first=visitor?.firstPoint,clip=data.clips.find(c=>c.source_id===selected.source_id&&c.epoch_id===selected.epoch_id&&c.source_asset_id===first?.source_asset_id);if(clip)rows.push({at:Number(first.source_s),stream_s:Number(first.stream_s),end:Number(first.source_s),clip_id:clip.id,title:visitor.visibilityKnown?'First observed':'First tracked',description:visitor.visibilityKnown?'First recorded position':'Tracker position · visibility unknown',kind:'tracking'});return rows.sort((a,b)=>a.stream_s-b.stream_s);}
 const tr=trackData[selected?.id],segments=tr?.histories?.[selectedPerson]||[],rows=selectedEvents().filter(e=>!genericObservation(e)).map(e=>({at:e.playback_start_s,end:e.playback_end_s,title:eventTitle(e),description:eventDescription(e),event:e,kind:'event'}));
 if(segments.length){const first=segments[0][0].frame/tr.fps,last=segments.at(-1).at(-1).frame/tr.fps;rows.push({at:first,end:first,title:'First observed',description:'First recorded position in this excerpt',kind:'tracking'});if(last<selected.duration_s-.5)rows.push({at:last,end:last,title:'Last observed',description:'Last recorded position in this excerpt',kind:'tracking'});}
 return rows.sort((a,b)=>a.at-b.at);
}

function thumbnailMarkup(at,extra=''){
 const record=uxData.clips?.[selected?.id],thumb=record?.thumbnail,frames=record?.frames;if(thumb&&frames?.length){const frame=frames.reduce((prev,x)=>Math.abs(x.time_s-at)<Math.abs(prev.time_s-at)?x:prev,frames[0]);return `<span class="chapter-thumb ${extra}" style="background-image:url('${esc(thumb.url)}');background-size:${thumb.columns*100}% ${thumb.rows*100}%;background-position:${thumb.columns>1?(frame.x/thumb.width)/(thumb.columns-1)*100:0}% ${thumb.rows>1?(frame.y/thumb.height)/(thumb.rows-1)*100:0}%"></span>`;}
 return `<span class="chapter-thumb ${extra}" style="background-image:url('${esc(poster(selected))}')"></span>`;
}
function visitorRecords(){
 if(maintained())return MaintainedPlayer.visitors(maintainedState.value);
 const tr=trackData[selected?.id];if(!tr)return [];
 const records=Object.entries(tr.histories||{}).map(([id,segments])=>{
  const first=segments[0][0].frame/tr.fps,last=segments.at(-1).at(-1).frame/tr.fps,observed=Math.min(selected.duration_s,segments.reduce((sum,segment)=>sum+segment.length*tr.sampleStep/tr.fps,0)),events=allEvents().filter(e=>eventActors(e).includes(id));
  return {id,first,last,observed,portrait:events.find(e=>e.person_crop_url)?.person_crop_url||null,events};
 }).sort((a,b)=>a.first-b.first||a.id.localeCompare(b.id,undefined,{numeric:true}));
 return followingLive ? records.filter(visitor=>visitor.first<=video.currentTime+.05) : records;
}
function renderVisitorList(){
 const list=$('#visitorList'),detail=$('#visitorDetail');if(!list||!detail)return;
 list.hidden=!visitorListOpen;detail.hidden=visitorListOpen;
 // A closed list holds nothing: its pictures leave the page with it.
 if(!visitorListOpen){list.replaceChildren();return;}
 const records=visitorRecords();list.innerHTML=`<div class="visitor-list-heading"><h2>Visitors</h2></div>${records.length?records.map(v=>`<button class="visitor-row" data-visitor-id="${esc(v.id)}" aria-label="Follow ${esc(personTitle(v.id))}">${portraitOf(v)?`<img class="visitor-photo" src="${esc(portraitOf(v))}" alt="${esc(personTitle(v.id))}">`:thumbnailMarkup(v.first,'visitor-photo')}<span><strong>${esc(personTitle(v.id))}</strong><small>${maintained()&&!v.visibilityKnown?'First tracked':'First observed'}</small><time>${playerClock(v.first)}</time></span></button>`).join(''):`<p class="sidebar-empty visitor-list-empty">${maintained()?(maintainedState.status==='unavailable'?'Visitor connection unavailable. Reconnecting…':maintainedState.status==='ready'?'Visitors appear as tracking arrives.':'Loading visitors…'):'Visitors will appear here as they arrive.'}</p>`}`;
 list.querySelectorAll('img.visitor-photo').forEach(image=>image.addEventListener('error',()=>{const fallback=document.createElement('span');fallback.className='visitor-photo';image.replaceWith(fallback);},{once:true}));
 list.querySelectorAll('[data-visitor-id]').forEach(button=>button.addEventListener('click',()=>{
  const id=button.dataset.visitorId;if(maintained()){navigateVisitor(id);return;}selectPerson(id);
  if(!tracksAt(video.currentTime).some(p=>String(p.id)===id)){const visitor=visitorRecords().find(v=>v.id===id);if(visitor){video.pause();seek(visitor.first);}}
 }));
}
function refreshVisitorHistory(){
 if(!selected||selectedPerson!==null||!visitorListOpen)return;
 const signature=visitorRecords().map(visitor=>`${visitor.id}:${visitor.first}:${portraits.get(visitor.id)?.count||0}`).join('|');
 if(signature===visitorRosterSignature)return;
 visitorRosterSignature=signature;renderJourney();
}
function observedItemGroups(events){
 const groups=new Map();
 for(const event of events.filter(productInteraction)){
  // One row for each observed item: the bundle names it by its object reference, the maintained record by its item.
  const ref=String(event.object_ref||'').trim(),hasRef=ref&&!['unknown','none','null'].includes(ref.toLowerCase()),key=event.maintained?(event.item_id?`item:${event.item_id}`:event.key):hasRef?`${event.windowIndex}:${ref}`:event.event_id;
  let group=groups.get(key);if(!group){group={description:event.item_description||(hasRef?ref:'Observed item'),events:[],image:null};groups.set(key,group);}
  group.events.push(event);if(event.object_crop_verified&&event.object_crop_url||!group.image)group.image=eventImage(event);
 }
 return [...groups.values()];
}
function renderPersonSummary(rows){
 // The summary holds the visitor's stats and description; the observed items follow it in their own section.
 const target=$('#personSummary'),itemList=$('#visitorItems');if(!target)return;if(journeyTab==='notes')visitBasket?.clear();itemList.replaceChildren();
 if(journeyTab==='notes'){
  const observations=[...new Set(rows.filter(genericObservation).map(eventDescription).filter(Boolean))];
  target.innerHTML=observations.length?`<div class="journey-notes">${observations.length===1?`<p>${esc(observations[0])}</p>`:`<ul>${observations.map(text=>`<li>${esc(text)}</li>`).join('')}</ul>`}</div>`:'<p class="sidebar-empty">Additional observations will appear here.</p>';return;
 }
 // A saved bundle holds every event of its recording, so its count is a number; the maintained reader awaits later reads.
 const visitor=visitorRecords().find(v=>v.id===selectedPerson),items=observedItemGroups(rows),interactionCount=rows.filter(productInteraction).length;
 const description=rows.find(e=>e.person_description)?.person_description||'';
 target.innerHTML=`${visitor?`<div class="visitor-session-stats"><div><strong>${visitor.observed<60?`${visitor.observed.toFixed(1)}s`:playerClock(visitor.observed)}</strong><span>${maintained()&&!visitor.visibilityKnown?'Tracked':'Observed'}</span></div><div><strong>${interactionCount||(maintained()?'—':0)}</strong><span>${interactionCount===1?'Interaction':interactionCount||!maintained()?'Interactions':'Awaiting events'}</span></div></div>`:''}${description?`<p class="visitor-description">${esc(description)}</p>`:''}`;
 itemList.innerHTML=items.length?`<div class="visitor-product-list">${items.map(item=>`<article class="visitor-product">${item.image?`<img src="${esc(item.image)}" alt="Observed item">`:''}<div><strong>${esc(item.description)}</strong><p>${esc([...new Set(item.events.map(eventTitle))].join(' · '))}</p></div></article>`).join('')}</div>`:'';
}
function renderJourney(){
 const target=$('#personJourney');if(!selected)return;clearTrailPreview();const rows=maintained()?maintainedChapters():selectedEvents(),chapters=personChapters();$('#personHeading').textContent=selectedPerson!==null?personTitle(selectedPerson):'Visitors';
 document.querySelectorAll('[data-journey-tab]').forEach(b=>{b.classList.toggle('selected',b.dataset.journeyTab===journeyTab);b.setAttribute('aria-pressed',String(b.dataset.journeyTab===journeyTab));});
 renderVisitorList();renderPersonSummary(rows);
 // The filmstrip under the controls: arrivals while the room is shown, the selected visitor's chapters after that.
 target.innerHTML=chapters.length?chapters.map((chapter,i)=>`<button class="chapter-row" data-chapter="${i}" data-chapter-at="${chapter.at}" ${chapter.personId?`data-room-person="${esc(chapter.personId)}"`:chapter.event?`data-journey="${esc(chapter.event.key)}"`:''}>${chapter.image||eventImage(chapter.event)?`<img class="chapter-thumb" src="${esc(chapter.image||eventImage(chapter.event))}" alt="${chapter.personId?esc(personTitle(chapter.personId)):chapter.event.object_crop_verified?'Item detail':'Action evidence'}" loading="lazy">`:maintained()?`<span class="chapter-thumb" style="background-image:url('${esc(poster(data.clips.find(c=>c.id===chapter.clip_id)))}')"></span>`:thumbnailMarkup(chapter.at)}<span class="chapter-copy"><strong>${esc(chapter.title)}</strong><span class="chapter-description">${esc(brief(chapter.description,105))}</span><time>${playerClock(maintained()?chapter.stream_s:chapter.at)}</time></span></button>`).join(''):`<p class="sidebar-empty">${maintained()&&maintainedState.status!=='ready'?(maintainedState.status==='unavailable'?'Chapter connection unavailable. Reconnecting…':'Loading chapters…'):selectedPerson===null?'Choose a visitor in the video.':'Chapters appear as this visitor acts.'}</p>`;
 target.querySelectorAll('[data-journey]').forEach(b=>{
  b.addEventListener('click',()=>{const e=rows.find(e=>e.key===b.dataset.journey);if(e)selectEvent(e);});
  b.addEventListener('pointerenter',()=>setTrailPreview(b.dataset.journey,'pointer'));b.addEventListener('pointerleave',()=>setTrailPreview(null,'pointer'));
  b.addEventListener('focus',()=>setTrailPreview(b.dataset.journey,'focus'));b.addEventListener('blur',()=>setTrailPreview(null,'focus'));
 });
 target.querySelectorAll('[data-room-person]').forEach(b=>b.addEventListener('click',()=>{if(maintained()){navigateVisitor(b.dataset.roomPerson);return;}selectPerson(b.dataset.roomPerson);video.pause();seek(+b.dataset.chapterAt);}));
 target.querySelectorAll('[data-chapter]:not([data-journey]):not([data-room-person])').forEach(b=>b.addEventListener('click',()=>{selectedEventKey=null;if(maintained())navigateChapter(chapters[+b.dataset.chapter]);else{video.pause();seek(+b.dataset.chapterAt);}}));
 renderDurationTimeline();updateJourneyActive(rows.filter(e=>activeAt(e,video.currentTime)));
 if(decodedFrameTime!==null&&!video.seeking)renderTrailAnnotations(decodedFrameTime);
}

function updateJourneyActive(active){
 if(!selected)return;const t=maintained()?mappedTime():video.currentTime,chapters=personChapters(),current=chapters.reduce((found,ch,i)=>(maintained()?ch.stream_s:ch.at)<=t?i:found,-1),keys=new Set(active.map(e=>e.key));
 $('#personJourney').querySelectorAll('[data-chapter]').forEach(b=>b.classList.toggle('active',+b.dataset.chapter===current));$('#personJourney').querySelectorAll('[data-journey]').forEach(b=>{if(!b.hasAttribute('data-chapter'))b.classList.toggle('active',keys.has(b.dataset.journey));});
}

function selectEvent(e){
 if(!timed(e))return;video.pause();
 {const actors=eventActors(e),primary=actorId(e.primary_person_id);if(actors.length===1)selectPerson(actors[0]);else if(selectedPerson!==null&&!actors.includes(selectedPerson))selectPerson(actors.includes(primary)?primary:null);}
 if(maintained()&&e.clip_id!==selected.id)setClip(e.clip_id);selectedEventKey=e.key;dismissedEvidenceKey=null;seek(e.playback_start_s);showEvidence(e,'manual');
}
function showEvidence(e,origin='auto'){
 evidenceEvent=e;evidenceOrigin=origin;const card=$('#evidenceThumbnail');if(!e.snapshot_url){card.hidden=true;return;}
 $('#evidenceThumbImage').src=e.object_crop_verified&&e.object_crop_url?e.object_crop_url:e.person_crop_url||e.snapshot_url;
 const caption=$('#evidenceThumbCaption'),description=e.item_description||e.evidence||e.person_description||eventLabel(e);
 caption.innerHTML=`<strong>${esc(eventActorLabel(e))} · ${esc(eventLabel(e))}</strong><time>Model event ${clock(e.playback_start_s)}–${clock(e.playback_end_s)}</time><small>${esc(description)}</small><i>${e.object_crop_verified&&e.object_crop_url?'Object view':e.person_crop_url?'Person crop · source image':'Source image'} · ${clock(e.snapshot_time_s??e.playback_start_s)}</i>`;caption.title=description;card.hidden=false;
}
function updateAutomaticEvidence(t){
 if(!selected||selectedPerson===null){$('#evidenceThumbnail').hidden=true;return;}if(video.paused&&evidenceOrigin==='manual'){if(evidenceEvent&&t>=evidenceEvent.playback_start_s&&t<=evidenceEvent.playback_end_s&&(selectedPerson===null||eventActors(evidenceEvent).includes(selectedPerson))){if($('#evidenceThumbnail').hidden&&dismissedEvidenceKey!==`${selected.id}:${evidenceEvent.key}`)showEvidence(evidenceEvent,'manual');return;}evidenceOrigin='auto';}
 const candidates=allEvents().filter(e=>e.eligible_for_overlay!==false&&productInteraction(e)&&e.snapshot_url&&timed(e)&&t>=e.playback_start_s&&t<=e.playback_end_s&&(selectedPerson===null||eventActors(e).includes(selectedPerson))).sort((a,b)=>Math.abs(t-(a.snapshot_time_s??a.playback_start_s))-Math.abs(t-(b.snapshot_time_s??b.playback_start_s)));const e=candidates[0],key=e?`${selected.id}:${e.key}`:null;
 if(e&&key!==dismissedEvidenceKey){if(evidenceEvent?.key!==e.key||$('#evidenceThumbnail').hidden)showEvidence(e,'auto');}else if(evidenceOrigin==='auto')$('#evidenceThumbnail').hidden=true;
}
function expandEvidence(crop=false){
 const e=evidenceEvent;if(!e?.snapshot_url)return;const objectView=crop==='object'&&e.object_crop_verified&&e.object_crop_url;$('#evidenceImage').src=objectView?e.object_crop_url:crop===true&&e.person_crop_url?e.person_crop_url:e.snapshot_url;
 $('#evidenceTitle').textContent=objectView?'Object view · source image':crop===true?'Person view · source image':'Source image';$('#evidenceDescription').textContent=eventLabel(e);
 $('#evidenceMeta').textContent=`${eventActorLabel(e)} · ${clock(e.snapshot_time_s??e.playback_start_s)}${Number.isFinite(e.source_snapshot_time_s)?' · source '+clock(e.source_snapshot_time_s):''}${Number.isInteger(e.snapshot_frame)?' · source frame '+e.snapshot_frame:''}`;
 $('#evidencePersonView').hidden=!e.person_crop_url;$('#evidenceObjectView').hidden=!(e.object_crop_verified&&e.object_crop_url);$('#evidenceOriginalLink').href=e.snapshot_url;if(!$('#evidenceDialog').open)$('#evidenceDialog').showModal();
}
$('#evidenceThumbOpen').addEventListener('click',()=>expandEvidence(evidenceEvent?.object_crop_verified?'object':!!evidenceEvent?.person_crop_url));$('#evidenceObjectView').addEventListener('click',()=>expandEvidence('object'));$('#evidenceThumbClose').addEventListener('click',()=>{dismissedEvidenceKey=evidenceEvent?`${selected.id}:${evidenceEvent.key}`:null;$('#evidenceThumbnail').hidden=true;});
$('#evidenceClose').addEventListener('click',()=>$('#evidenceDialog').close());$('#evidencePersonView').addEventListener('click',()=>expandEvidence(true));$('#evidenceFullView').addEventListener('click',()=>expandEvidence(false));
$('#evidenceDialog').addEventListener('click',e=>{if(e.target===$('#evidenceDialog'))$('#evidenceDialog').close();});

// The replay clock advances independently of media playback. Reviewing a recording
// never freezes the room edge, and separate recordings keep separate tracker IDs.
function updateLiveReplayStatus(){
 const live=$('#liveReplayButton'),catchup=$('#catchupButton')||$('#returnToLive');
 if(live){live.setAttribute('aria-pressed',String(followingLive));live.classList.toggle('at-live-edge',followingLive);}
 if(catchup)catchup.hidden=followingLive||!roomReplayClock;
 if($('#liveReplayStatus'))$('#liveReplayStatus').textContent='Live replay';
 if($('#liveReplayTime')&&roomReplayClock)$('#liveReplayTime').textContent=playerClock(roomReplayClock.edge().elapsed_s);
}
function leaveLiveReplay(){if(!roomReplayClock)return;followingLive=false;liveSyncRequested=false;updateLiveReplayStatus();}
function initializeRoomReplay(roomId=$('#roomSelect')?.value||'shop-aisle'){
 if(maintained()){const clips=data.clips.filter(c=>c.room_id===roomId);if(clips.length&&selected?.room_id!==roomId)setClip(clips[0].id);$('#activeRoomName').textContent=selected?.room_name||'Camera';$('#liveReplayButton').hidden=true;$('#catchupButton').hidden=true;return;}if(!window.RoomLoop)return;
 const clips=data.clips.filter(c=>c.is_primary_trial===true&&c.viewer_enabled!==false&&(c.room_id||'shop-aisle')===roomId);
 if(!clips.length)return;
 if(!roomReplayClocks.has(roomId))roomReplayClocks.set(roomId,RoomLoop.create(clips.map(c=>({id:c.id,duration_s:c.duration_s}))));
 roomReplayClock=roomReplayClocks.get(roomId);followingLive=!playerWorkSuspended();video.muted=true;$('#loop').checked=false;if($('#loopSetting'))$('#loopSetting').checked=false;
 const selectedOption=$('#roomSelect')?.selectedOptions?.[0];if($('#activeRoomName'))$('#activeRoomName').textContent=selectedOption?.textContent||'Shop aisle';
 $('.room-switcher')?.removeAttribute('open');clearInterval(roomReplayInterval);roomReplayInterval=playerWorkSuspended()?null:setInterval(()=>syncLiveReplay(),100);syncLiveReplay(true);
}
function returnToLiveReplay(){if(!roomReplayClock)return;followingLive=true;syncLiveReplay(true);}
function syncLiveReplay(force=false){
 if(playerWorkSuspended()||!roomReplayClock)return;if(force)liveSyncRequested=true;updateLiveReplayStatus();if(!followingLive)return;
 const edge=roomReplayClock.edge();
 if(selected?.id!==edge.clip_id){setClip(edge.clip_id,false,true);return;}
 if(mediaLoading||video.readyState<1||video.seeking)return;
 if(liveSyncRequested||Math.abs(video.currentTime-edge.time_s)>.75){liveSyncRequested=false;seek(edge.time_s,false,true);}
 if(video.paused){const controller=mediaController;video.play().catch(()=>{if(controller!==mediaController)return;followingLive=false;updateLiveReplayStatus();});}
}
const playerLifecycle=window.PlayerLifecycle.create({
 video, context:()=>selected?.id, isActive:()=>$('.camera-player').getClientRects().length>0,
 onSuspend(){
  maintainedReader?.suspend(true);leaveLiveReplay();clearInterval(roomReplayInterval);roomReplayInterval=null;cancelOverlayFrame();
  if(decodedFrameRequest!==null&&video.cancelVideoFrameCallback){video.cancelVideoFrameCallback(decodedFrameRequest);decodedFrameRequest=null;}
  personOutline?.setSuspended(true);markFramePending();
 },
 onResume(){
  maintainedReader?.suspend(false);personOutline?.setSuspended(false);requestDecodedFrame();if(pictureReleased)heldRead();
  clearInterval(roomReplayInterval);roomReplayInterval=roomReplayClock?setInterval(()=>syncLiveReplay(),100):null;
  updateLiveReplayStatus();
 }
});

window.addEventListener('message',event=>{
 if(event.source!==window.parent||event.origin!==location.origin)return;
 if(event.data?.type==='rocoto:player-suspend'){playerViewSuspended=true;playerLifecycle.setSuspended(true);}
 if(event.data?.type==='rocoto:player-resume'){playerViewSuspended=false;playerLifecycle.setSuspended(false);}
});

function requestOverlayFrame(){
 if(playerWorkSuspended()||videoFrameRequest!==null||video.paused)return;
 if('requestVideoFrameCallback' in video)videoFrameRequest=video.requestVideoFrameCallback((_,metadata)=>{videoFrameRequest=null;acceptDecodedFrame(metadata.mediaTime);requestOverlayFrame();});
 else videoFrameRequest=requestAnimationFrame(()=>{videoFrameRequest=null;acceptDecodedFrame(video.currentTime);requestOverlayFrame();});
}
function cancelOverlayFrame(){
 if(videoFrameRequest===null)return;
 if('cancelVideoFrameCallback' in video)video.cancelVideoFrameCallback(videoFrameRequest);else cancelAnimationFrame(videoFrameRequest);
 videoFrameRequest=null;
}
video.addEventListener('timeupdate',updateOverlay);
video.addEventListener('seeking',markFramePending);video.addEventListener('seeked',requestDecodedFrame);video.addEventListener('loadeddata',()=>{const tr=trackData[selected?.id];sizeTrackOverlay(tr?.width||video.videoWidth||1920,tr?.height||video.videoHeight||1080);requestDecodedFrame();});
// The element pauses itself at the end of a recording; only a pause before the end stops the session.
video.addEventListener('play',()=>{sessionPlaying=true;requestOverlayFrame();});video.addEventListener('pause',()=>{if(!video.ended)sessionPlaying=false;cancelOverlayFrame();updateOverlay();});
$('#playhead').addEventListener('input',e=>seek(+e.target.value));
$('#overlayModel').addEventListener('change',e=>{overlayModel=e.target.value;timelineSignature='';renderJourney();updateOverlay();});
video.addEventListener('ended',()=>{if(playerWorkSuspended())return;if(maintained()){if(sessionPlaying)continueSession();return;}if(followingLive){syncLiveReplay(true);return;}if($('#loop').checked){seek(loopLength?loopStart:0);video.play().catch(()=>{});}});
$('#overlays').addEventListener('change',updateOverlay);$('#trackTimes').addEventListener('change',updateOverlay);$('#trackTrails').addEventListener('change',updateOverlay);
function syncExpandedPlayer(){const open=document.fullscreenElement===$('.camera-player')||$('.camera-player').classList.contains('expanded-player');$('#expandPlayback').innerHTML=icon(open?'exitFullscreen':'fullscreen');$('#expandPlayback').setAttribute('aria-label',open?'Close expanded playback':'Expand video with tracker and event overlays');}
$('#expandPlayback').addEventListener('click',async()=>{const wrap=$('.camera-player');if(document.fullscreenElement===wrap){await document.exitFullscreen();}else if(wrap.classList.contains('expanded-player')){wrap.classList.remove('expanded-player');document.body.classList.remove('player-expanded');}else{try{if(!wrap.requestFullscreen)throw Error('Fullscreen unavailable');await wrap.requestFullscreen();}catch{wrap.classList.add('expanded-player');document.body.classList.add('player-expanded');}}syncExpandedPlayer();});
document.addEventListener('fullscreenchange',syncExpandedPlayer);document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('.camera-player').classList.contains('expanded-player')){$('.camera-player').classList.remove('expanded-player');document.body.classList.remove('player-expanded');syncExpandedPlayer();}});

async function loadPlayer(){
 const response=await fetch('/api/retail/viewer/bootstrap',{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw Error('Camera connection unavailable');const boot=MaintainedPlayer.bootstrap(await response.json());readAuthority=boot.authority;
 if(maintained()){$('.room-demo-note').hidden=true;$('#overlayModel').closest('label').hidden=true;$('#loop').checked=false;$('#loopSetting').checked=false;$('#loopSetting').closest('label').hidden=true;data=boot.data;trackData={};overlayModel='maintained';JourneyModel.configure({event_types:{}});window.monitoringProfile={event_types:{}};maintainedReader=MaintainedPlayer.create({onChange:receiveMaintained});const room=$('#roomSelect');room.innerHTML=[...new Map(data.clips.map(c=>[c.room_id,c])).values()].map(c=>`<option value="${esc(c.room_id)}">${esc(c.room_name)}</option>`).join('');}
 else{const [d,traces,profile]=await Promise.all([fetch('data.json').then(r=>{if(!r.ok)throw Error('Could not load results');return r.json();}),fetch('tracks.json').then(r=>{if(!r.ok)throw Error('Could not load tracks');return r.json();}).catch(()=>{trackLoadError=true;return {};}),fetch('monitoring-profile.json').then(r=>{if(!r.ok)throw Error('Could not load monitoring configuration');return r.json();})]);JourneyModel.configure(profile);window.monitoringProfile=profile;data=d;trackData=traces;}
 for(const tr of Object.values(trackData))indexTracks(tr);renderGrid();initWatchUI();const params=new URLSearchParams(location.hash.slice(1)),chosen=data.clips.find(c=>c.id===params.get('clip')),first=chosen||data.clips.find(c=>c.viewer_default)||data.clips.find(c=>c.is_primary_trial===true&&c.viewer_enabled!==false)||data.clips.find(c=>c.viewer_enabled!==false);
 if(first)setClip(first.id,false,!maintained());else $('#pendingPrimary').hidden=false;initializeRoomReplay();ControlRoom.init();
}
loadPlayer().catch(()=>{
 const notice='Camera connection unavailable. Reload to reconnect.';$('#mediaError').hidden=false;$('#mediaError').textContent=notice;$('#mediaLoading').hidden=true;
 // Before a recording opens, the player shell is closed and the pending slot is the painted one.
 if($('#benchmarkWorkspace').hidden){const slot=$('#pendingPrimary p');$('#pendingPrimary').hidden=false;slot.setAttribute('role','alert');slot.textContent=notice;}
});

// Player interaction layer. Media transport, observations, and tracker evidence
// remain in the shared playback clock above.
function icon(name){
 const transport={
  play:'<path d="M8 4.5a1 1 0 0 1 1.5-.86l11 7.5a1 1 0 0 1 0 1.72l-11 7.5A1 1 0 0 1 8 19.5z"/>',
  pause:'<rect x="5" y="4" width="5" height="16" rx="1"/><rect x="14" y="4" width="5" height="16" rx="1"/>',
  next:'<path d="M4 5a1 1 0 0 1 1.54-.84l10 7a1 1 0 0 1 0 1.68l-10 7A1 1 0 0 1 4 19z"/><rect x="18" y="4" width="3" height="16" rx=".8"/>',
  volume:'<path d="M3 9h4l5-4v14l-5-4H3z"/><path d="M15.5 8a6 6 0 0 1 0 8M18.5 5a10 10 0 0 1 0 14" style="fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round"/>',
  muted:'<path d="M3 9h4l5-4v14l-5-4H3z"/><path d="m16 9 6 6m0-6-6 6" style="fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round"/>'
 };
 if(transport[name])return `<svg class="lucide-icon transport-glyph" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true">${transport[name]}</svg>`;
 const ids={next:'skip-forward',volume:'volume-2',muted:'volume-x',fullscreen:'maximize',exitFullscreen:'minimize',theater:'rectangle-horizontal',chevron:'chevron-right',chevronLeft:'chevron-left',close:'x',person:'user-round',spark:'sparkles',camera:'video',chapters:'list-video'};
 return `<svg class="lucide-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="kit/lucide.svg#${ids[name]||name}"></use></svg>`;
}

// Recorded movement of the selected visitor, drawn over the scrubber: higher while they stay in place. The bundle
// reader draws its whole saved recording. The maintained reader draws the visit's own tracker segments as far as
// the playhead, a quarter second at a time, the way its trail, chapters and basket are read.
function motionGroups(tr,t){
 if(selectedPerson===null||!tr)return [];
 if(!maintained())return MotionTimeline.profile(tr,selectedPerson);
 if(!Number.isFinite(t))return [];
 const limit=t*tr.fps+.001,histories=[];
 for(const [id,segments] of Object.entries(tr.histories||{}))for(const segment of segments)if(geometryPerson({id,birth_id:segment[0]?.birth_id},t)===selectedPerson)histories.push(segment.filter(point=>point.frame<=limit));
 return MotionTimeline.profile({...tr,histories:{visit:histories}},'visit');
}
function renderDurationTimeline(){
 if(!selected)return;const tr=trackData[selected.id],reach=maintained()&&Number.isFinite(decodedFrameTime)?Math.floor(decodedFrameTime*4):'',signature=`${selected.id}:${selectedPerson}:${overlayModel}:${tr?.frameNumbers?.length||0}:${maintained()?maintainedRenderKey:''}:${reach}`;if(signature===timelineSignature)return;timelineSignature=signature;
 const duration=selected.duration_s,chapters=personChapters(),groups=motionGroups(tr,decodedFrameTime),paths=MotionTimeline.paths(groups,duration),target=$('#durationLayers');
 target.setAttribute('aria-label','Recorded movement: higher while staying in place');
 target.innerHTML=`<svg class="motion-graph" viewBox="0 0 1000 42" preserveAspectRatio="none" role="img" aria-label="Recorded movement: higher while staying in place"><defs><linearGradient id="motionFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="white" stop-opacity=".65"/><stop offset="1" stop-color="white" stop-opacity=".03"/></linearGradient></defs>${paths.map(p=>`<path class="motion-area" d="${p.fill}" fill="url(#motionFill)"/><path class="motion-line" d="${p.line}" fill="none" stroke="white" stroke-width="1.3" vector-effect="non-scaling-stroke"/>`).join('')}</svg>`;
 target.onpointermove=event=>{const rect=target.getBoundingClientRect(),at=Math.max(0,Math.min(duration,(event.clientX-rect.left)/rect.width*duration)),point=MotionTimeline.at(groups,at);target.title=point?`${playerClock(at)} · ${point.stationary_s.toFixed(1)}s of low movement`:'Recorded movement';};
 $('#seekChapters').innerHTML=[...new Set(chapters.filter(c=>!maintained()||c.clip_id===selected.id).map(c=>c.at))].filter(t=>t>0&&t<duration).map(t=>`<i style="left:${t/duration*100}%"></i>`).join('');$('#mainSeek').setAttribute('aria-valuemax',duration);
}

function updatePlayerUI(t){
 if(!selected)return;const d=selected.duration_s,percent=Math.min(100,Math.max(0,t/d*100));$('#playerTime').textContent=`${playerClock(t)} / ${playerClock(d)}`;$('#seekProgress').style.width=`${percent}%`;$('#seekKnob').style.left=`${percent}%`;$('#mainSeek').setAttribute('aria-valuenow',t.toFixed(2));$('#mainSeek').setAttribute('aria-valuetext',`${playerClock(t)} of ${playerClock(d)}`);
 const chapter=personChapters().filter(c=>c.at<=t).at(-1);$('#currentChapter').innerHTML=`<span>${esc(chapter?.title||'Journey')}</span>${icon('chevron')}`;
 $('.video-wrap').classList.toggle('is-paused',video.paused);const signature=`${video.paused}:${video.muted}:${video.volume}`;if(signature!==playerUiSignature){playerUiSignature=signature;$('#playToggle').innerHTML=icon(video.paused?'play':'pause');$('#playToggle').setAttribute('aria-label',video.paused?'Play (k)':'Pause (k)');$('#muteToggle').innerHTML=icon(video.muted||!video.volume?'muted':'volume');$('#muteToggle').setAttribute('aria-label',video.muted?'Unmute (m)':'Mute (m)');$('#volumeSlider').value=video.muted?0:video.volume;}
 const buffered=video.buffered.length?video.buffered.end(video.buffered.length-1):0;$('#seekBuffer').style.width=`${Math.min(100,buffered/d*100)}%`;
 $('#durationLayers')?.querySelectorAll('[data-duration-at]').forEach(b=>{const a=+b.dataset.durationAt,z=+b.dataset.durationEnd;const active=t>=a&&t<=z;b.classList.toggle('active',active);const value=b.querySelector('b');if(value)value.textContent=`${Math.round(z-a)}s`;});
}
function previewSeek(t,x){
 if(!selected)return;const record=uxData.clips?.[selected.id],thumb=record?.thumbnail,frames=record?.frames;const preview=$('#seekPreview'),image=$('#seekPreviewImage');
 if(thumb&&frames?.length){const frame=frames.filter(f=>f.time_s<=t).at(-1)||frames[0];image.style.backgroundImage=`url("${thumb.url}")`;image.style.backgroundSize=`${thumb.columns*100}% ${thumb.rows*100}%`;image.style.backgroundPosition=`${thumb.columns>1?(frame.x/thumb.width)/(thumb.columns-1)*100:0}% ${thumb.rows>1?(frame.y/thumb.height)/(thumb.rows-1)*100:0}%`;}else{image.style.backgroundImage=poster(selected)?`url("${poster(selected)}")`:'none';image.style.backgroundSize='cover';image.style.backgroundPosition='center';}
 $('#seekPreviewTime').textContent=playerClock(t);$('#seekPreviewLabel').textContent=personChapters().filter(c=>c.at<=t).at(-1)?.title||'Journey';const width=$('#mainSeek').clientWidth;preview.style.left=`${Math.max(95,Math.min(width-95,x))}px`;preview.hidden=false;
}
function resumePlayback(){if(maintained()&&video.ended)continueSession();else video.play().catch(()=>{});}
function togglePlay(){if(pictureReleased){pictureReleased.playing=true;return;}if(video.paused)resumePlayback();else{leaveLiveReplay();video.pause();}updatePlayerUI(video.currentTime);}
function flash(message){const node=$('#keyFeedback');node.textContent=message;node.hidden=false;clearTimeout(flash.timer);flash.timer=setTimeout(()=>node.hidden=true,650);}
let dragSeek=null;
function revealControls(){$('.camera-player').classList.add('controls-visible');}
function initWatchUI(){
 $('#outlineSetting')?.addEventListener('change',e=>{personOutline?.setEnabled(e.target.checked);updateOverlay();});
 document.querySelectorAll('[data-icon]').forEach(node=>node.innerHTML=icon(node.dataset.icon));$('.camera-player').classList.add('controls-visible');$('.video-wrap').classList.add('is-paused');video.controls=false;
 if(!maintained())fetch('ux-data.json').then(r=>r.ok?r.json():null).then(d=>{if(d){uxData=d;renderJourney();}}).catch(()=>{});
 $('#playToggle').addEventListener('click',togglePlay);
 $('#liveReplayButton')?.addEventListener('click',returnToLiveReplay);$('#catchupButton')?.addEventListener('click',returnToLiveReplay);$('#returnToLive')?.addEventListener('click',returnToLiveReplay);$('#roomSelect')?.addEventListener('change',event=>initializeRoomReplay(event.target.value));
 $('.video-wrap').addEventListener('pointerdown',choosePersonAtPointer,true);
 $('#trailAnnotations').addEventListener('pointerdown',event=>{const node=event.target.closest('[data-trail-event]');if(!node||event.button!==0)return;event.preventDefault();event.stopPropagation();activateTrailEvent(node);});
 $('#trailAnnotations').addEventListener('keydown',event=>{if(!['Enter',' '].includes(event.key))return;const node=event.target.closest('[data-trail-event]');if(!node)return;event.preventDefault();event.stopPropagation();activateTrailEvent(node);});
 $('#trailAnnotations').addEventListener('click',event=>{if(event.detail!==0)return;const node=event.target.closest('[data-trail-event]');if(node){event.preventDefault();activateTrailEvent(node);}});
 $('#previousPerson')?.addEventListener('click',()=>cyclePerson(-1));$('#nextPerson')?.addEventListener('click',()=>cyclePerson(1));
 $('#visitorBack')?.addEventListener('click',enterRoomMode);
 $('#contextualActivity').addEventListener('click',event=>{const key=event.target.closest('[data-current-event]')?.dataset.currentEvent,e=selectedEvents().find(e=>e.key===key);if(!e)return;showEvidence(e,'manual');$('#evidenceThumbnail').hidden=true;expandEvidence(e.object_crop_verified?'object':!!e.person_crop_url);});
 $('#muteToggle').addEventListener('click',()=>{video.muted=!video.muted;updatePlayerUI(video.currentTime);});$('#volumeSlider').addEventListener('input',e=>{video.volume=+e.target.value;video.muted=false;updatePlayerUI(video.currentTime);});video.addEventListener('volumechange',()=>updatePlayerUI(video.currentTime));
 $('#nextChapter').addEventListener('click',()=>{const next=personChapters().find(c=>(maintained()?c.stream_s:c.at)>(maintained()?mappedTime():video.currentTime)+.5);if(maintained()&&next)navigateChapter(next);else seek(next?.at??Math.min(selected.duration_s,video.currentTime+10));});
 $('#playbackSpeed').addEventListener('change',e=>{video.playbackRate=+e.target.value;});$('#settingsToggle').addEventListener('click',()=>{const panel=$('#playerSettings');panel.hidden=!panel.hidden;$('#settingsToggle').setAttribute('aria-expanded',String(!panel.hidden));});
 $('#trailSetting').addEventListener('change',e=>{$('#trackTrails').checked=e.target.checked;updateOverlay();});$('#loopSetting').addEventListener('change',e=>$('#loop').checked=e.target.checked);
 $('#captionToggle').addEventListener('click',()=>{const control=$('#overlays');control.checked=!control.checked;$('#captionToggle').setAttribute('aria-pressed',String(control.checked));updateOverlay();});
 $('#theaterToggle').addEventListener('click',()=>{document.body.classList.toggle('theater-mode');$('#theaterToggle').setAttribute('aria-pressed',String(document.body.classList.contains('theater-mode')));});
 const openJourney=()=>{$('#journeyPanel').hidden=false;visitorListOpen=selectedPerson===null;journeyTab='summary';renderJourney();if(innerWidth<900)$('#journeyPanel').scrollIntoView({behavior:'smooth',block:'start'});};$('#currentChapter')?.addEventListener('click',openJourney);$('#reopenJourney')?.addEventListener('click',openJourney);$('#closeJourney')?.addEventListener('click',()=>$('#journeyPanel').hidden=true);
 document.querySelectorAll('[data-journey-tab]').forEach(b=>b.addEventListener('click',()=>{journeyTab=b.dataset.journeyTab;renderJourney();updateOverlay();}));
 $('#downloadEvents').addEventListener('click',()=>{const output={schema_version:1,recording:selected.id,source_recording_id:selected.source_recording_id||selected.id,source_offset_s:selected.source_offset_s??null,model:overlayModel,events:allEvents().map(({windowIndex,eventIndex,key,...e})=>e)},url=URL.createObjectURL(new Blob([JSON.stringify(output,null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download=`${selected.id}-observations.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
 const slider=$('#mainSeek'),position=e=>{const rect=slider.getBoundingClientRect();return Math.min(1,Math.max(0,(e.clientX-rect.left)/rect.width));};
 slider.addEventListener('pointerdown',e=>{if(e.button!==0||!selected)return;e.preventDefault();dragSeek={resume:!video.paused};video.pause();slider.setPointerCapture(e.pointerId);seek(position(e)*selected.duration_s);previewSeek(position(e)*selected.duration_s,e.clientX-slider.getBoundingClientRect().left);revealControls();});
 slider.addEventListener('pointermove',e=>{if(!selected)return;const t=position(e)*selected.duration_s;previewSeek(t,e.clientX-slider.getBoundingClientRect().left);if(dragSeek)seek(t);});
 const finish=e=>{if(!dragSeek)return;const resume=dragSeek.resume;dragSeek=null;try{slider.releasePointerCapture(e.pointerId);}catch{}$('#seekPreview').hidden=true;if(resume)resumePlayback();};slider.addEventListener('pointerup',finish);slider.addEventListener('pointercancel',finish);slider.addEventListener('pointerleave',()=>{if(!dragSeek)$('#seekPreview').hidden=true;});
 slider.addEventListener('keydown',e=>{if(['ArrowRight','ArrowLeft','Home','End'].includes(e.key)){e.preventDefault();e.stopPropagation();seek(e.key==='Home'?0:e.key==='End'?selected.duration_s:video.currentTime+(e.key==='ArrowRight'?5:-5));}});
 $('.video-wrap').addEventListener('pointermove',revealControls);$('.video-wrap').addEventListener('focusin',revealControls);video.addEventListener('play',revealControls);video.addEventListener('pause',()=>{revealControls();updatePlayerUI(video.currentTime);});
 document.addEventListener('click',e=>{if(!e.target.closest('#playerSettings,#settingsToggle')){$('#playerSettings').hidden=true;$('#settingsToggle').setAttribute('aria-expanded','false');}});
 document.addEventListener('keydown',e=>{if(e.defaultPrevented||e.ctrlKey||e.metaKey||e.altKey||e.target.closest('input,textarea,select,[contenteditable="true"]')||$('#evidenceDialog').open)return;const key=e.key.toLowerCase();if(!selected)return;
  if((key===' '&&!e.target.closest('button,a,summary'))||key==='k'){e.preventDefault();togglePlay();}else if(['j','l','arrowleft','arrowright'].includes(key)){e.preventDefault();const delta=key==='j'?-10:key==='l'?10:key==='arrowleft'?-5:5;seek(video.currentTime+delta);flash(`${delta>0?'+':''}${delta} seconds`);}else if(key==='m')$('#muteToggle').click();else if(key==='f')$('#expandPlayback').click();else if(key==='t')$('#theaterToggle').click();else if(key==='c')$('#captionToggle').click();else if(/^[0-9]$/.test(key))seek(selected.duration_s*Number(key)/10);revealControls();
 });
}
