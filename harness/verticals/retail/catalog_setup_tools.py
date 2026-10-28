"""Native parent-conversation adapters for the normal catalog setup service."""
from retail_settings import value as setting
NAMES=frozenset({'get_catalog_setup','get_product_match','prepare_catalog_photo_upload','import_catalog_products','configure_location'})


def specs(write=True):
    common={'location_id':{'type':'string'}}
    def make(name,description,properties,required):
        return {'type':'function','name':name,'description':description,'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}
    result=[make('get_catalog_setup','Read catalog setup status for this location. Prefer customer POS/catalog file import, then shelf photos, then provisional camera observations.',common,['location_id'])]
    result.append(make('get_product_match','Read the saved product identity candidate, source crop, price provenance and matching evidence for a committed event. This reads existing data and starts no image request.',{'event_id':{'type':'string'}},['event_id']))
    if write:
        from location_intelligence import LocationCoordinates
        result.append(make('configure_location','Save explicitly supplied, source-backed coordinates for this existing location, including timezone and provenance. The current conversation fixes the business collection. Retain supplied uncertainty; this operation performs no geocoding.',{**common,'coordinates':LocationCoordinates.model_json_schema()},['location_id','coordinates']))
        result.append(make('prepare_catalog_photo_upload','When the user chooses shelf photos, create a 15-minute upload-only phone link and local QR. The link grants upload access only. Camera observations remain provisional until supported product matching.',common,['location_id']))
        result.append(make('import_catalog_products','Import explicitly supplied customer catalog or POS rows. Preserve the actual SKU/name/visible identifiers; images and observed camera items keep separate evidence. Use the file upload UI for CSV or JSON files.',{**common,'source_name':{'type':'string'},'rows':{'type':'array','minItems':1,'maxItems':500,'items':{'type':'object','properties':{'sku':{'type':'string'},'name':{'type':'string'},'barcode':{'type':'string'},'description':{'type':'string'},'price':{'type':'string','description':'Exact catalog price in major currency units, such as 2.45.'},'currency':{'type':'string','pattern':'^[A-Z]{3}$'}},'required':['sku','name'],'additionalProperties':False}}},['location_id','source_name','rows']))
    return result


def call(agent,chat_id,collection,name,args,run):
    from scope_host import turn_actor
    host=getattr(agent,'business_scope_host',None)
    if host is None or 'catalog_setup' not in host:raise ValueError('Use the current location workspace.')
    owner=turn_actor(agent,chat_id);record=agent.children.get(chat_id,{})
    if not owner or record.get('dataset')!=collection:raise ValueError('Use the current business conversation.')
    store=host['catalog_setup']
    if name=='get_product_match':
        from execution_policy import RETRIEVAL_VIEW
        view=RETRIEVAL_VIEW.get()
        if view is not None:
            from monitoring_tools import scoped,admit_event_history
            event=scoped(host['location_intelligence'].monitoring,view,host=host,chat_id=chat_id).get_event(owner,args['event_id'])
            if event['dataset']!=collection:raise ValueError('Use product evidence from this business conversation.')
            admit_event_history(agent,chat_id,[event])
        result=host['location_intelligence'].product_match(owner,args['event_id'])
        if result['dataset']!=collection:raise ValueError('Use product evidence from this business conversation.')
        return result
    if name=='get_catalog_setup':return store.overview(owner,collection,args['location_id'])
    if record.get('parent_id') or record.get('native_root_id') or not run or run.get('cancel') and run['cancel'].is_set():
        raise ValueError('Complete catalog setup from the active parent conversation.')
    if name=='configure_location':
        return host['location_intelligence'].configure_location(owner,{'collection':collection,
            'location_id':args['location_id'],'coordinates':args['coordinates']})
    if name=='prepare_catalog_photo_upload':
        import os
        origin=setting('ROCOTO_PUBLIC_ORIGIN','https://video-demo.soyrocoto.com')
        return store.create_handoff(owner,collection,args['location_id'],origin=origin)
    return store.import_products(owner,collection,args['location_id'],args['rows'],source_name=args['source_name'])
