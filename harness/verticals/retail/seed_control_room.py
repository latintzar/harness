"""Explicit host commissioning of the retail demo through existing company scope.

This command creates geography/configuration only. Dataset adapters register actual
recordings and feed the stream ingestion service separately. It invents no footage,
calendar history, catalog matches or business performance.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sqlite3
from business_scope import ScopeActor
from retail_business_scope import RetailBusinessScope
from workspace_identity import workspace_scope
from monitoring import MonitoringStore
from location_intelligence import LocationIntelligence


def seed_location(state,access_path,environment_id,owner,*,lifecycle,collection='retail-demo',name='Retail demo',address='',cover_image=None,coordinates=None):
    if coordinates is not None:
        from location_intelligence import LocationCoordinates
        coordinates=LocationCoordinates.model_validate(coordinates).model_dump(mode='json')
    if lifecycle not in {'development','demo'}:raise ValueError('Use the development or demo runtime.')
    state=Path(state);access_path=Path(access_path)
    if not access_path.is_file():raise ValueError('Use the company access registry.')
    with sqlite3.connect(access_path.as_uri()+'?mode=ro',uri=True) as db:
        if not db.execute('SELECT 1 FROM environments WHERE id=?',(environment_id,)).fetchone():
            raise ValueError('Commission the company through its existing access registry first.')
    def member(key):
        with sqlite3.connect(access_path.as_uri()+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT role FROM principal_environments WHERE principal_id=? AND environment=?',(key,environment_id)).fetchone()
        return {'id':key,'role':row[0]} if row else None
    if (member(owner) or {}).get('role')!='owner':raise ValueError('Use the existing company owner for commissioning.')
    expected=access_path.parent if environment_id=='demo' else access_path.parent/'environments'/environment_id
    if state.resolve()!=expected.resolve():raise ValueError('Use the state directory belonging to the selected company.')
    scope=RetailBusinessScope(state,company_id=workspace_scope(state),member=member)
    actor=ScopeActor(scope.company_id,owner)
    view=scope.describe(actor,collection)
    location=next((v for v in view['locations'] if v['name']==name and v['address']==address),None)
    if location is None:
        named=[v for v in view['locations'] if v['name']==name] if address else []
        if len(named)==1:
            # The one shop of this name gains its address.
            view=scope.mutate(actor,collection,'locations',{'id':named[0]['id'],'name':name,'address':address},view['revision'])
            location=next(v for v in view['locations'] if v['id']==named[0]['id'])
        else:
            view=scope.mutate(actor,collection,'locations',{'name':name,'address':address},view['revision'])
            location=view['locations'][-1]
    room=next((r for r in view['rooms'] if r['location_id']==location['id'] and r['name']=='Shop floor'),None)
    if room is None:
        view=scope.mutate(actor,collection,'rooms',{'name':'Shop floor','location_id':location['id'],'function_ids':[]},view['revision'])
        room=view['rooms'][-1]
    monitor=MonitoringStore(state)
    store=LocationIntelligence(state,scope,monitor)
    configured=store.configure_location(owner,{'collection':collection,'location_id':location['id'],**({'cover_image':cover_image} if cover_image is not None else {}),**({'coordinates':coordinates} if coordinates is not None else {})})
    return {'collection':collection,'location_id':location['id'],'room_id':room['id'],'configuration':configured,'basis':'host_commissioned_location','environment_id':environment_id}


if __name__=='__main__':
    import json
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--access-state',required=True,type=Path)
    parser.add_argument('--environment-id',required=True)
    parser.add_argument('--owner',required=True)
    parser.add_argument('--lifecycle',choices=['development','demo'],required=True)
    parser.add_argument('--collection',default='retail-demo')
    parser.add_argument('--name',default='Retail demo')
    parser.add_argument('--address',default='')
    parser.add_argument('--cover-image')
    args=parser.parse_args()
    cover={'url':args.cover_image,'kind':'generated_demo','provenance':{'kind':'generated_demo','purpose':'illustrative_location_exterior'}} if args.cover_image else None
    result=seed_location(args.state,args.access_state/'access.sqlite',args.environment_id,args.owner,lifecycle=args.lifecycle,collection=args.collection,name=args.name,address=args.address,cover_image=cover)
    print(json.dumps(result))
