"""Host-only import of verified RetailAction recordings into a private owner viewer.

Run on the owning development/demo host. This copies source-anonymized media,
converts the actual NVIDIA tracks, retains review/clock receipts, and admits only
explicitly reviewed observations. Dataset labels remain a separate QA artifact.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import re
import subprocess
from control_room_routes import owner_directory, CODE, GENERATED_CODE_ASSETS
from monitoring import MonitoringStore
from stream_ingestion import StreamIngestion, PRODUCT_EVENTS, canonical
from retail_stream_import import build_import, load, VOCABULARY, SCENE_EVENTS
from retail_dataset_reference import reference_windows


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):digest.update(part)
    return digest.hexdigest()


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,allow_nan=False));temporary.chmod(0o600)
    os.replace(temporary,path)


def source_file(root,relative):
    candidate=Path(root)/relative
    if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('Read artifact files from the verified import directory.')
    return candidate


def make_poster(video,target,at_s):
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ss',str(at_s),'-i',str(video),
        '-frames:v','1','-q:v','3','-y',str(target)],check=True,capture_output=True,timeout=45)


def viewer_tracks(raw):
    if raw.get('identity_scope')!='recording_and_camera_local':
        raise ValueError('Keep the verified tracker identity scope.')
    return {'fps':raw['fps'],'width':raw['width'],'height':raw['height'],
        'source':raw.get('tracker'),'detector':raw.get('detector'),
        'identity_scope':raw['identity_scope'],
        'frames':{str(frame['frame_index']):[{'id':str(track['track_id']),
            'box':track['bbox_xyxy'],'confidence':track.get('confidence')}
            for track in frame['tracks']] for frame in raw['frames']}}


def import_bundle(run_root,state,owner,*,location_id,room_id,camera_ids,reviews=None,
                  collection='retail-demo',location_name='Retail demo',weather=None,cover_image=None,
                  poster_builder=make_poster,visit_scope=None):
    """Import one location while its scheduler waits, including optional tracked visits."""
    from types import SimpleNamespace
    from tracker_runner import host_pass
    company = SimpleNamespace(state=Path(state), owner=MonitoringStore.owner(owner), collection=collection)
    with host_pass(company, location_id) as held:
        result = _import_bundle(run_root, company.state, company.owner, location_id=location_id,
            room_id=room_id, camera_ids=camera_ids, reviews=reviews, collection=collection,
            location_name=location_name, weather=weather, cover_image=cover_image, poster_builder=poster_builder)
        if visit_scope is not None:
            result['tracker_visits'] = ingest_tracker_visits(run_root, company.state, company.owner, visit_scope)
        # The bundle changes inputs and the page; the service still computes the full tracker pass.
        held.done({'status': 'completed'}, page=result, covers_inputs=False)
        return result


def _import_bundle(run_root,state,owner,*,location_id,room_id,camera_ids,reviews=None,
                   collection='retail-demo',location_name='Retail demo',weather=None,cover_image=None,poster_builder=make_poster):
    root=Path(run_root).resolve();state=Path(state);monitor=MonitoringStore(state)
    owner=monitor.owner(owner);destination=owner_directory(state,owner)
    destination.mkdir(parents=True,exist_ok=True);destination.chmod(0o700)
    cover, cover_assets = generated_cover(cover_image)
    records=load(source_file(root,'retail-action-nvidia/manifest.json'))['records']
    definition={'name':'Retail camera observations','dataset':collection,
        'location_id':location_id,'room_id':room_id,
        'criteria':'Directly visible, source-timed observations; review preserves raw model output and source evidence.',
        'camera_ids':sorted(set(camera_ids.values())),
        'vocabulary':[{'type':kind,'description':kind.replace('_',' '),
            'group':'service_maintenance' if kind in SCENE_EVENTS and kind!='other_observation' else ('other' if kind=='other_observation' else 'shopper_journey')}
            for kind in VOCABULARY]}
    profiles=monitor.list_profiles(owner,collection)
    profile=next((p for p in profiles if all(p.get(k)==v for k,v in definition.items())),None)
    if profile is None:profile=monitor.save_profile(owner,definition,datasets=[collection])
    imported=build_import(root,profile_id=profile['id'],profile_revision=profile['revision'],reviews=reviews)
    reference_definition={**definition,'name':'Dataset annotations',
        'criteria':'Publisher labels for dataset-reference playback. Separate from Cosmos predictions and live alerts.',
        'vocabulary':[v for v in definition['vocabulary'] if v['type'] in {'pick_up','put_down','touch'}]}
    reference_profile=next((p for p in profiles if all(p.get(k)==v for k,v in reference_definition.items())),None)
    if reference_profile is None:reference_profile=monitor.save_profile(owner,reference_definition,datasets=[collection])
    references=reference_windows(root,records,reference_profile)
    revision=hashlib.sha256(canonical([records,imported,references]).encode()).hexdigest()[:20]
    relative_base=Path('revisions')/revision
    revision_dir=destination/relative_base
    if not revision_dir.exists():revision_dir.mkdir(parents=True)
    files=[];clips=[];sources=[]
    windows_by_source={}
    for window in imported['windows']:windows_by_source.setdefault(window['source_id'],[]).append(window)
    event_reviews={e['event_key']:e for output in imported['audit'] for e in output['events']}
    for record in records:
        clip=record['clip_id']
        if not isinstance(clip,str) or re.fullmatch(r'\d+-rank[01]',clip) is None:
            raise ValueError('Use the source recording identity from the verified dataset.')
        source_id='retailaction-'+clip
        if source_id not in camera_ids:raise ValueError('Bind each recording view to its authorized camera source.')
        folder=revision_dir/clip;folder.mkdir(exist_ok=True)
        original=source_file(root,f'retail-action-nvidia/{clip}/clean.mp4')
        media=folder/'clean.mp4'
        if not media.exists():shutil.copyfile(original,media);media.chmod(0o600)
        if sha(media)!=sha(original):raise ValueError('Preserve the verified source bytes.')
        raw_tracks=load(source_file(root,f'retail-action-nvidia/{clip}/tracks.json'))
        converted=viewer_tracks(raw_tracks);write_json(folder/'tracks.json',converted)
        shutil.copyfile(source_file(root,f'retail-action-nvidia/{clip}/source_mapping.json'),folder/'source_mapping.json')
        poster=folder/'poster.jpg'
        if not poster.exists():poster_builder(media,poster,min(1,record['duration_s']/2))
        prefix=(relative_base/clip).as_posix()
        for filename in ['clean.mp4','tracks.json','source_mapping.json','poster.jpg']:
            files.append(prefix+'/'+filename);(folder/filename).chmod(0o600)
        descriptor={'id':source_id,'video_id':source_id,'dataset':collection,'start':0,
            'end':record['duration_s'],'sha256':sha(media),'camera_id':camera_ids[source_id],
            'media_url':'/control-room/'+prefix+'/clean.mp4',
            'thumbnail_url':'/control-room/'+prefix+'/poster.jpg',
            'evidence_url':'/control-room/?clip='+source_id}
        # Existing immutable sources may use an earlier bundle revision's path.
        previous=monitor.registered_source(owner,source_id)
        if previous:
            if any(previous.get(k)!=descriptor.get(k) for k in ['sha256','end','dataset','camera_id']):
                raise ValueError('Keep recording content and camera association immutable.')
            descriptor=previous
        else:monitor.register_source(owner,descriptor)
        sources.append(source_id)
        viewer_windows=[]
        for window in windows_by_source.get(source_id,[]):
            events=[]
            for event in window['observations']:
                events.append({'event_id':event['event_key'],'event':event['type'],
                    'person_id':event['track_id'],'description':event['description'],
                    'item_description':event['object_description'],'object_ref':None,
                    'playback_start_s':event['source']['start_s'],'playback_end_s':event['source']['end_s'],
                    'start_s':event['source']['start_s'],'end_s':event['source']['end_s'],
                    'eligible_for_overlay':True,'review_state':'visually_verified',
                    'review':event_reviews[event['event_key']]['review']})
            viewer_windows.append({'offset_s':0,'source_window_start_s':window['start_s'],
                'source_window_end_s':window['end_s'],'processing_seconds':window['processing_seconds'],'events':events})
        reference_events=references['events_by_source'].get(source_id,[])
        for event in reference_events:
            at=(event['start_s']+event['end_s'])/2
            snapshot=folder/(event['event_id']+'.jpg')
            if not snapshot.exists():poster_builder(media,snapshot,at)
            filename=prefix+'/'+snapshot.name;files.append(filename)
            event.update(snapshot_url=filename,snapshot_time_s=at,snapshot_resolution=[record['width'],record['height']],
                snapshot_kind='source_frame',object_crop_verified=False)
            point=event['provenance'].get('interaction_point')
            if point and poster_builder is make_poster and 0<=point.get('x',-1)<=1 and 0<=point.get('y',-1)<=1:
                size=min(180,record['width'],record['height'])
                x=max(0,min(record['width']-size,round(point['x']*record['width']-size/2)))
                y=max(0,min(record['height']-size,round(point['y']*record['height']-size/2)))
                crop=folder/(event['event_id']+'-context.jpg')
                if not crop.exists():
                    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(snapshot),
                        '-vf',f'crop={size}:{size}:{x}:{y}','-frames:v','1','-q:v','2','-y',str(crop)],
                        check=True,capture_output=True,timeout=15)
                filename=prefix+'/'+crop.name;files.append(filename)
                event.update(interaction_crop_url=filename,crop_provenance={'kind':'interaction_context',
                    'basis':'publisher_interaction_point','xyxy_source_pixels':[x,y,x+size,y+size],
                    'source_time_s':at,'object_localization_verified':False})
        clips.append({'id':source_id,'source_recording_id':source_id,
            'title':f'RetailAction {record["source_clip_id"]} · view {record["source_view"][-1]}',
            'location_id':location_id,'location_name':location_name,'room_id':room_id,'room_name':'Shop floor',
            'camera_id':camera_ids[source_id],'duration_s':record['duration_s'],
            'media':prefix+'/clean.mp4','poster':prefix+'/poster.jpg','tracks_url':prefix+'/tracks.json',
            'width':record['width'],'height':record['height'],'fps':record['fps'],
            'temporal_sampling':record['temporal_sampling'],'unique_source_frames':record['unique_source_frames'],
            'source_duration_s':record['source_duration_s'],'capture_start_utc':None,
            'is_primary_trial':True,'viewer_enabled':True,
            'viewer_default':source_id=='retailaction-000027-rank1',
            'models':{'8b':{'windows':viewer_windows,'processed':source_id in windows_by_source},
                'dataset_reference':{'windows':[{'offset_s':0,'events':reference_events}],
                    'provenance':'dataset_reference','label':'Dataset annotations'}},
            'source_provenance':{'dataset':'standard-cognition/RetailAction','face_redaction':'source_anonymized',
                'source_mapping':prefix+'/source_mapping.json','identity_scope':'recording_and_camera_local',
                'scene_capture_location_known':False,'demonstration_location_assignment':True}})
    # Real stream admission persists empty windows as processed coverage and only
    # reviewed events as observations. Downstream inspection remains pending work.
    ingestion=StreamIngestion(state,monitor)
    admitted=[ingestion.accept(owner,window) for window in imported['windows']]
    reference_admitted=[ingestion.accept(owner,window) for window in references['windows']]
    data={'schema_version':1,'clips':clips,'location':{'id':location_id,'name':location_name,**({'weather':weather} if weather else {}),**({'cover_image':cover} if cover else {})},
        'source_dataset':'RetailAction','capture_dates_known':False,'input_kind':'dataset_replay',
        'coverage':{'tracked_views':len(clips),'model_processed_views':len(windows_by_source),**imported['counts'],
            'dataset_reference_events':references['counts']['events'],
            'dataset_reference_associated_events':references['counts']['associated_events']}}
    # Keep what the location's trackers published: their tiles, canvases, catalog, baskets and clips.
    with page_lock(destination):
        kept=carried_page_data(destination)
        data.update(kept['data']);data['clips']=data['clips']+[c for c in kept['clips'] if c['id'] not in {x['id'] for x in clips}]
        write_json(destination/'data.json',data)
    write_json(destination/'tracks.json',{})
    write_json(destination/'ux-data.json',{'clips':{}})
    write_json(destination/'monitoring-profile.json',{'schema_version':1,'profile_id':profile['id'],
        'profile_version':profile['revision'],'event_types':{v['type']:{'label':v['type'].replace('_',' ').capitalize(),
            'group':v['group'],'scope':'scene' if v['type'] in SCENE_EVENTS else 'person',
            'evidence_kind':'interaction' if v['type'] in PRODUCT_EVENTS else 'observation'} for v in profile['vocabulary']}})
    qa=revision_dir/'qa';qa.mkdir(exist_ok=True)
    write_json(qa/'model-output-audit.json',imported)
    write_json(qa/'dataset-reference-provenance.json',references)
    files.append((relative_base/'qa/dataset-reference-provenance.json').as_posix())
    shutil.copyfile(source_file(root,'resumed-stock/config.yaml'),qa/'inference-config.yaml')
    ground_truth=root/'retailaction-ground-truth.json'
    if ground_truth.is_file():
        shutil.copyfile(source_file(root,'retailaction-ground-truth.json'),qa/'publisher-labels.json')
        files.append((relative_base/'qa/publisher-labels.json').as_posix())
    files.extend([(relative_base/'qa/model-output-audit.json').as_posix(),(relative_base/'qa/inference-config.yaml').as_posix()])
    generated_assets = {}
    for relative, source in cover_assets.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        files.append(relative)
        generated_assets[relative] = {'sha256': sha(target), 'bytes': target.stat().st_size, 'kind': 'generated_demo'}
    manifest={'schema_version':1,'owner_principal_id':owner,'revision':revision,
        'generated_assets': generated_assets,
        'source_ids':sources+[key for key in kept['source_ids'] if key not in sources],'location':{'id':location_id,'name':location_name},
        'privacy':{'face_redaction':'source_anonymized','basis':'publisher-anonymized RetailAction source',
            **({'sources':kept['privacy_sources']} if kept['privacy_sources'] else {})},
        'files':sorted(set(files+kept['files']+['data.json','tracks.json','ux-data.json','monitoring-profile.json']))}
    write_json(destination/'manifest.json',manifest)
    return {'bundle_directory':str(destination),'revision':revision,'profile_id':profile['id'],
        'source_ids':sources,'admitted_windows':admitted,'reference_profile_id':reference_profile['id'],
        'reference_windows':reference_admitted,'coverage':data['coverage']}


CORE_PAGE_KEYS=('core_trackers','core_tracker_canvases','products','baskets','currency')
ARCHIVE_CLIP='model_answer_archive'


def carried_page_data(destination):
    """What `publish_core_trackers` wrote into this bundle, so a re-import keeps it."""
    result={'data':{},'clips':[],'files':[],'source_ids':[],'privacy_sources':{}}
    try:
        data=load(Path(destination)/'data.json');manifest=load(Path(destination)/'manifest.json')
    except (OSError,ValueError):
        return result
    result['data']={key:data[key] for key in CORE_PAGE_KEYS if key in data}
    result['clips']=[c for c in data.get('clips',[]) if c.get('source_provenance',{}).get('kind')==ARCHIVE_CLIP]
    result['source_ids']=[c['id'] for c in result['clips']]
    result['files']=[name for name in manifest.get('files',[]) if name.startswith('sources/')]
    result['privacy_sources']=manifest.get('privacy',{}).get('sources',{})
    return result


@contextmanager
def page_lock(destination):
    """One writer of a page's data at a time: a tracker pass, a host import and a saved presentation take turns."""
    import fcntl
    path=Path(destination)/'.page.lock'
    with open(path,'a') as handle:
        path.chmod(0o600)
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


ROWS=30


def reading(output):
    """One period's reading as a tile carries it: its number, its series and its canvas, each list cut to ROWS."""
    tile=output['tile'];canvas={k:(v[:ROWS] if isinstance(v,list) and k not in {'series','history'} else v) for k,v in output['canvas'].items()}
    return {**{k:tile.get(k) for k in ('value','unit','state','needs','series','summary','basis','currency') if k in tile},
        'period_label':tile['period']['label'],'canvas':canvas}


def in_currency(tiles,currency):
    """A company tracker's amount is written in the location's currency, the one the catalog names, when its
    output names none: Home and the canvas then write the same characters."""
    for tile in tiles:
        if currency and tile.get('core') is False and tile.get('kind')=='money' and not tile.get('currency'):tile['currency']=currency
    return tiles


def tracker_page(company,location_id,readings,*,only=None,kept=None):
    """The tiles and the canvases of a location's trackers as the page reads them: (tiles, canvases keyed by tracker).

    A Core Tracker's row is its output's tile with its flags and its week, month and year readings,
    and its canvas is the output's canvas with the flags. A company's tracker carries its saved
    presentation on top: the label, mark and number format of its tile, and the family and blocks
    of its canvas (`core_trackers/presentation.py`). A Tracking Agent's tracker joins once it has an
    output; until then the agent's own tile stands on Home. `only` names the tracker ids wanted;
    `kept` holds the period readings already on the page, for a tracker whose readings are absent.
    """
    import retail_records as records
    from core_trackers import presentation
    from core_trackers.common import outputs
    tiles=[];canvases={};saved=None
    for tracker,output in outputs(company,location_id):
        if only is not None and tracker['id'] not in only:continue
        owner_agent=tracker.get('agent_id') or company.display_owner_agent()
        flags={'core':tracker['core'],'chat':tracker['chat'],'locked':tracker['locked'],
               'display_id':tracker['id'],'owner_principal_id':company.owner,
               **({'owner_agent_id':owner_agent} if owner_agent else {})}
        if tracker['core']:
            if output is None:
                tiles.append({'key':tracker['key'],'label':tracker['label'],'value':None,'unit':None,'state':'needs',
                    'needs':'Needs first run','series':[],'events':[],'period':None,'tracker_id':tracker['id'],**flags})
                continue
            tiles.append({**output['tile'],**flags,'periods':{name:reading(read) for name,read in readings.get(tracker['key'],{}).items()}})
            canvases[tracker['key']]={**output['canvas'],**flags}
            continue
        if saved is None:saved={row['tracker_id']:row for row in records.rows(company,'tracker_presentation',location_id)}
        spec=presentation.presentation_of(tracker,saved.get(tracker['id']),output);agent=tracker.get('agent_id')
        if output is None:
            if agent:continue
            tiles.append(presentation.apply_to_tile({'key':tracker['key'],'label':tracker['label'],'value':None,'unit':None,'state':'needs',
                'needs':'Needs first run','series':[],'events':[],'period':None,'tracker_id':tracker['id'],**flags},spec))
            continue
        periods={name:reading(read) for name,read in readings.get(tracker['key'],{}).items()} or (kept or {}).get(tracker['key'],{})
        tiles.append(presentation.apply_to_tile({**output['tile'],**flags,'periods':periods},spec,output['canvas'],agent_id=agent))
        canvases[tracker['key']]=presentation.canvas_for_page({**output['canvas'],**flags},spec)
    return tiles,canvases


def tracker_row(company,location_id,tracker_id,*,currency=None,kept=None):
    """One company tracker's tile row and canvas as the page carries them: its stored output and its week, month
    and year readings with its presentation applied, and an amount in the location's currency. (None, None) while
    the tracker is still to get a row of its own. A saved presentation and a canvas read ahead of the page both assemble here,
    so a tile and a canvas come out in one period and one number format."""
    readings={}
    try:
        from core_trackers import installed_trackers
        from tracker_scripts import saved_readings
        readings=saved_readings(company,location_id,[t for t in installed_trackers(company,location_id) if t['id']==tracker_id])
    except Exception:
        pass  # The readings already on the page stay with the row.
    tiles,canvases=tracker_page(company,location_id,readings,only={tracker_id},kept=kept)
    if not tiles:return None,None
    in_currency(tiles,currency)
    return tiles[0],canvases.get(tiles[0]['key'])


def location_currency(company,location_id):
    """The currency a location's catalog names, the one `write_page` gives its page."""
    import retail_records as records
    return next((product['price']['currency'] for product in records.catalog(company,location_id) if (product.get('price') or {}).get('currency')),None)


def show_presentation(company,location_id,tracker_id):
    """Carry one company tracker's saved presentation into the owner's published page, without a
    tracker pass: its own row and canvas are assembled again from its stored output and readings.
    Returns whether the page data changed."""
    destination=owner_directory(company.state,company.owner)
    if not (destination/'data.json').is_file():return False
    with page_lock(destination):
        try:data=load(destination/'data.json')
        except (OSError,ValueError):return False
        rows=data.get('core_trackers')
        if not isinstance(data.get('location'),dict) or data['location'].get('id')!=location_id or not isinstance(rows,list):return False
        kept={row['key']:row['periods'] for row in rows if isinstance(row,dict) and row.get('tracker_id')==tracker_id and isinstance(row.get('periods'),dict)}
        tile,canvas=tracker_row(company,location_id,tracker_id,currency=data.get('currency'),kept=kept)
        if tile is None:return False
        before=canonical([rows,data.get('core_tracker_canvases')])
        at=next((index for index,row in enumerate(rows) if isinstance(row,dict) and row.get('tracker_id')==tracker_id),None)
        if at is None:rows.append(tile)
        else:rows[at]=tile
        if canvas is not None:data.setdefault('core_tracker_canvases',{})[tile['key']]=canvas
        if canonical([rows,data.get('core_tracker_canvases')])==before:return False
        write_json(destination/'data.json',data)
    return True


def publish_core_trackers(company,location_id,*,location_name='Retail demo'):
    """Carry a location's trackers in the page data, core first then the company's own.

    Adds `core_trackers` (the tiles), `core_tracker_canvases` (keyed by tracker), `products`
    (the catalog, with its counts for the tiles' period), `baskets` (per visitor) and the catalog
    `currency` to the owner's `data.json`, with the clips of the ingested recordings. Every
    existing key stays as it is.
    """
    destination=owner_directory(company.state,company.owner)
    destination.mkdir(parents=True,exist_ok=True);destination.chmod(0o700)
    # The period switch on Home reads each tracker over the week, the month and the year.
    from core_trackers.run import period_readings
    readings=period_readings(company,location_id)
    with page_lock(destination):
        return write_page(company,location_id,readings,destination,location_name)


def write_page(company,location_id,readings,destination,location_name):
    """The page data of `publish_core_trackers`, assembled and written while its lock is held."""
    import retail_records as records
    from core_trackers.common import read_location,epoch
    try:data=load(destination/'data.json')
    except (OSError,ValueError):
        data={'schema_version':1,'clips':[],'location':{'id':location_id,'name':location_name}}
    # A tracker the location removed takes its presentation with it, so a key used again starts with its own.
    from core_trackers import installed_trackers,presentation
    presentation.forget_removed(company,location_id,installed_trackers(company,location_id))
    tiles,canvases=tracker_page(company,location_id,readings)
    view=read_location(company,location_id)
    from stream_ingestion import PRODUCT_EVENTS
    period=next((tile['period'] for tile in tiles if tile.get('period')),None)
    counts={}
    for event in view.events:
        if not event['product_type'] or event['type'] not in PRODUCT_EVENTS:continue
        if period and not (event['t'] is not None and epoch(period['start'])<=event['t']<epoch(period['end'])):continue
        row=counts.setdefault(event['product_type'],{'interactions':0,'pickups':0,'putbacks':0})
        row['interactions']+=1
        row['pickups']+=event['type'] in {'pick_up','pickup','picked_up_item'}
        row['putbacks']+=event['type'] in {'put_back','put_back_item'}
    count=lambda key:counts.get(key,{'interactions':0,'pickups':0,'putbacks':0})
    # The page paints pictures this service holds: a product shows the shop's own crop of it.
    proofs={r['id']:r for r in records.rows(company,'event_receipt',location_id)}
    relative=lambda url:url.removeprefix('/control-room/') if url else None
    crops={}
    for event in view.events:
        proof=proofs.get(event['id'],{}).get('evidence') or {}
        if event['product_type'] and (proof.get('object_crop_url') or proof.get('thumb')):
            crops.setdefault(event['product_type'],relative(proof.get('object_crop_url') or proof.get('thumb')))
    held=lambda url:url if url and not url.startswith(('http://','https://','//')) else None
    products=[]
    for product in view.catalog:
        attributes=product.get('attributes',{});price=product.get('price') or {}
        products.append({'id':product['id'],'name':product['label'],'brand':attributes.get('brand'),
            'category':attributes.get('category'),'image_url':crops.get(attributes.get('product_type')) or held(attributes.get('image_url') or (product.get('image') or {}).get('url')),
            'catalog_image_url':attributes.get('image_url'),
            'price':float(price['amount']) if price.get('amount') is not None else None,'currency':price.get('currency'),'basis':price.get('basis'),
            'price_source_url':attributes.get('price_source_url'),'product_type':attributes.get('product_type'),
            'confidence':attributes.get('confidence'),**count(attributes.get('product_type'))})
    listed={product['product_type'] for product in products}
    for key in sorted({item['product_type'] for item in view.items}-listed):
        item=next(i for i in view.items if i['product_type']==key)
        products.append({'id':'type-'+key,'name':item['product_type_label'],'brand':None,'category':None,
            'image_url':crops.get(key) or held(next((u for i in view.items if i['product_type']==key for u in i['evidence']),None)),
            'price':None,'currency':None,'basis':None,'price_source_url':None,'product_type':key,'confidence':None,**count(key)})
    currency=next((product['currency'] for product in products if product['currency']),None)
    if currency:data['currency']=currency
    in_currency(tiles,data.get('currency'))
    public=lambda basket:{k:v for k,v in basket.items() if k not in {'data_basis','t','company_id','collection'}}
    newest=sorted((b for b in view.baskets if b['lines'] or b['uncertain']),key=lambda b:(b['t'] or 0,b['id']),reverse=True)[:300]
    data.update(core_trackers=tiles,core_tracker_canvases=canvases,products=products,
        baskets={basket['visit_id']:public(basket) for basket in newest})
    receipts=[r for r in records.rows(company,'recording_receipt',location_id) if r.get('clip')]
    events_by_source={}
    for event in view.events:
        events_by_source.setdefault(event['source_id'],[]).append(event)
    present={clip['id'] for clip in data['clips']}
    for receipt in receipts:
        shown=[]
        for event in sorted(events_by_source.get(receipt['id'],[]),key=lambda e:(e['at_s'],e['id'])):
            proof=proofs.get(event['id'],{}).get('evidence') or {}
            shown.append({'event_id':event['id'],'event':event['type'],'person_id':event['person_id'],
                'description':event['description'],'item_description':event['object_description'],'object_ref':None,
                'playback_start_s':event['at_s'],'playback_end_s':event['end_s'],'start_s':event['at_s'],'end_s':event['end_s'],
                'eligible_for_overlay':True,'review_state':'observation','snapshot_url':relative(proof.get('thumb')),
                'person_crop_url':relative(proof.get('person_crop_url')),'object_crop_url':relative(proof.get('object_crop_url')),
                'snapshot_time_s':proof.get('snapshot_time_s')})
        clip={**receipt['clip'],'location_name':location_name,
            'models':{'8b':{'windows':[{'offset_s':0,'source_window_start_s':0,'source_window_end_s':receipt['duration_s'],'events':shown}],'processed':True}}}
        clip['source_provenance']={**clip['source_provenance'],'kind':ARCHIVE_CLIP}
        if clip['id'] in present:data['clips']=[clip if c['id']==clip['id'] else c for c in data['clips']]
        else:data['clips'].append(clip)
    # The recording with the most to show leads the page.
    shown_events=lambda clip:sum(len(w.get('events',[])) for w in clip.get('models',{}).get('8b',{}).get('windows',[]))
    data['clips'].sort(key=lambda clip:-shown_events(clip))
    # Where the shop stands, for its weather and map.
    config=next((c for c in company.locations._rows(company.owner,'location') if c['location_id']==location_id),None) or {}
    place=next((l for l in company.locations.authorized(company.owner,company.collection,location_id)['locations'] if l['id']==location_id),{})
    point=config.get('coordinates');address=place.get('address') or ''
    data['location']={**data.get('location',{}),'id':location_id,'name':location_name,**({'address':address} if address else {}),
        **({'weather':{'location_id':location_id,'name':location_name,'address':address,'latitude':point['latitude'],
            'longitude':point['longitude'],'timezone':point['timezone']}} if point else {})}
    write_json(destination/'data.json',data)
    for name in ('tracks.json','ux-data.json'):
        if not (destination/name).is_file():write_json(destination/name,{} if name=='tracks.json' else {'clips':{}})
    if not (destination/'monitoring-profile.json').is_file():
        # The page names each event through this registry; a bundle made only by the trackers writes its own.
        write_json(destination/'monitoring-profile.json',{'schema_version':1,'profile_id':'core-trackers','profile_version':1,
            'event_types':{kind:{'label':kind.replace('_',' ').capitalize(),
                'group':'service_maintenance' if kind in SCENE_EVENTS else 'shopper_journey',
                'scope':'scene' if kind in SCENE_EVENTS else 'person',
                'evidence_kind':'interaction' if kind in PRODUCT_EVENTS else 'observation'}
                for kind in sorted({event['type'] for event in view.events})}})
    try:manifest=load(destination/'manifest.json')
    except (OSError,ValueError):
        # A bundle made only of these recordings states their privacy basis as published.
        manifest={'schema_version':1,'owner_principal_id':company.owner,'revision':'core-trackers','generated_assets':{},
            'source_ids':[],'location':{'id':location_id,'name':location_name},
            'privacy':{'face_redaction':'source_as_published','basis':'Licensed footage as its publisher released it.',
                'licence':'CC-BY-SA-4.0','publisher':'Intel'},'files':[]}
    files=[name for receipt in receipts for name in receipt.get('files',[]) if (destination/name).is_file()]
    manifest['files']=sorted(set(manifest['files'])|set(files)|{'data.json','tracks.json','ux-data.json','monitoring-profile.json'})
    manifest['source_ids']=manifest['source_ids']+[r['id'] for r in receipts if r['id'] not in manifest['source_ids']]
    manifest['privacy']={**manifest['privacy'],'sources':{**manifest['privacy'].get('sources',{}),
        **{r['id']:{'face_redaction':'source_as_published','note':r.get('publisher_note')} for r in receipts}}}
    write_json(destination/'manifest.json',manifest)
    last=max((epoch(tile['period']['end']) for tile in tiles if tile.get('period')),default=None)
    return {'bundle_directory':str(destination),'core_trackers':len(tiles),'canvases':len(canvases),'products':len(products),
        'baskets':len(newest),'clips':len(receipts),'period_end':records.iso(last),
        'served_by_page':manifest['privacy']['face_redaction'] in {'source_anonymized','verified_redacted'}
            or (manifest['privacy']['face_redaction']=='source_as_published' and bool(manifest['privacy'].get('licence')))}


def generated_cover(relative):
    """Select a shipped generic illustration, preserving its recorded provenance."""
    if relative is None:
        return None, {}
    if relative not in GENERATED_CODE_ASSETS or not relative.endswith('.png'):
        raise ValueError('Select a shipped generated location illustration.')
    provenance_path = str(Path(relative).with_suffix('.provenance.json'))
    if provenance_path not in GENERATED_CODE_ASSETS:
        raise ValueError('Keep the generated illustration provenance.')
    assets = {name: source_file(CODE, name) for name in [relative, provenance_path]}
    return {'url': '/control-room/' + relative, 'kind': 'generated_demo',
        'provenance': {'kind': 'generated_demo', 'purpose': 'illustrative_location_exterior',
            'record': '/control-room/' + provenance_path, 'sha256': sha(assets[relative])}}, assets


def ingest_tracker_visits(run_root, state, owner, scope):
    """Project actual tracker presence through current source/camera authority."""
    from location_intelligence import LocationIntelligence
    root = Path(run_root).resolve()
    store = LocationIntelligence(state, scope, MonitoringStore(state))
    receipts = []
    for record in load(source_file(root, 'retail-action-nvidia/manifest.json'))['records']:
        clip = record['clip_id']
        if not isinstance(clip, str) or re.fullmatch(r'\d+-rank[01]', clip) is None:
            raise ValueError('Use the verified source recording identity.')
        tracks = source_file(root, f'retail-action-nvidia/{clip}/tracks.json')
        visits = store.ingest_tracks(owner, 'retailaction-' + clip, load(tracks), tracks_sha256=sha(tracks))
        receipts.append({'source_id': 'retailaction-' + clip, 'tracks_sha256': sha(tracks),
            'visit_count': len(visits), 'visit_ids': [visit['id'] for visit in visits],
            'basis': 'first_observed_tracker_presence'})
    return {'tracked_visit_count': sum(row['visit_count'] for row in receipts), 'sources': receipts}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['run-root','state','access-state']:parser.add_argument('--'+key,type=Path,required=True)
    for key in ['environment-id','owner']:parser.add_argument('--'+key,required=True)
    parser.add_argument('--lifecycle',choices=['development','demo'],required=True)
    parser.add_argument('--collection',default='retail-demo');parser.add_argument('--name',default='Retail demo')
    parser.add_argument('--address',default='');parser.add_argument('--reviews',type=Path)
    parser.add_argument('--weather-json',type=Path)
    parser.add_argument('--cover-image', choices=sorted(p for p in GENERATED_CODE_ASSETS if p.endswith('.png')))
    args=parser.parse_args()
    from seed_control_room import seed_location
    from business_scope import ScopeActor
    from retail_business_scope import RetailBusinessScope
    from workspace_identity import workspace_scope
    access=args.access_state/'access.sqlite'
    cover, _ = generated_cover(args.cover_image)
    from location_intelligence import coordinates_from_weather
    weather=load(args.weather_json) if args.weather_json else None
    coordinates=coordinates_from_weather(weather)
    seeded=seed_location(args.state,access,args.environment_id,args.owner,lifecycle=args.lifecycle,
        collection=args.collection,name=args.name,address=args.address,cover_image=cover,coordinates=coordinates)
    def member(owner):
        with sqlite3.connect(access.as_uri()+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT role FROM principal_environments WHERE principal_id=? AND environment=?',(owner,args.environment_id)).fetchone()
        return {'id':owner,'role':row[0]} if row else None
    verified_sources=set()
    for row in load(args.run_root/'retail-action-nvidia/manifest.json')['records']:
        clip=row['clip_id']
        if not re.fullmatch(r'\d+-rank[01]',clip):raise ValueError('Use verified dataset source identities.')
        source_file(args.run_root,f'retail-action-nvidia/{clip}/clean.mp4')
        source_file(args.run_root,f'retail-action-nvidia/{clip}/tracks.json')
        verified_sources.add('retailaction-'+clip)
    scope=RetailBusinessScope(args.state,company_id=workspace_scope(args.state),member=member,
        source_authorized=lambda actor,collection,source: actor.principal_id==args.owner and collection==args.collection and source in verified_sources)
    actor=ScopeActor(scope.company_id,args.owner);view=scope.describe(actor,args.collection);camera_ids={}
    for row in load(args.run_root/'retail-action-nvidia/manifest.json')['records']:
        source='retailaction-'+row['clip_id']
        camera=next((c for c in view['cameras'] if source in c.get('source_ids',[])),None)
        if camera is None:
            view=scope.mutate(actor,args.collection,'cameras',{'name':'RetailAction '+row['clip_id'],
                'location_id':seeded['location_id'],'room_ids':[seeded['room_id']],'source_ids':[source]},view['revision'])
            camera=view['cameras'][-1]
        camera_ids[source]=camera['id']
    result=import_bundle(args.run_root,args.state,args.owner,location_id=seeded['location_id'],
        room_id=seeded['room_id'],camera_ids=camera_ids,reviews=load(args.reviews) if args.reviews else {},
        collection=args.collection,location_name=args.name,weather=weather,
        cover_image=args.cover_image,visit_scope=scope)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
