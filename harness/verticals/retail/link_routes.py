"""Authenticated browser adapter for LinkIntake; host creates and owns services."""
from __future__ import annotations
from access import page_request
import inspect
import re
from urllib.parse import urlsplit
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse
from pydantic import Field
from chat_permissions import ChatPermissions,workspace_writable
from source_intake import IntakeActor,IntakeError
from source_routes import StrictBody,SourceRoutePolicy,_header,_json_body,_deny,_public,PRIVATE_HEADERS
from link_intake import LinkIntake,LinkError,offload

class CreateLink(StrictBody):
 collection:str=Field(min_length=1,max_length=128)
 url:str=Field(min_length=1,max_length=8192)
 idempotency_key:str=Field(min_length=1,max_length=160)
class Empty(StrictBody):pass


def public(value):
 result=dict(value)
 if result.get('import'):result['import']=_public(result['import'])
 return result

async def call(fn,*args,**kwargs):
 try:
  return await fn(*args,**kwargs)if inspect.iscoroutinefunction(fn)else await offload(fn,*args,**kwargs)
 except(IntakeError,LinkError)as error:
  code=str(error)
  if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',code):code='invalid_link'
  status=404 if code=='record_not_found'else 403 if code in{'read_only_workspace','workspace_access_required'}else 503 if code=='source_service_unavailable'else 429 if code in{'download_capacity','link_record_limit','download_attempt_limit'}else 409 if code=='idempotency_conflict'else 400
  _deny(status,code)
 except(OSError,ValueError):_deny(503,'link_service_unavailable')


def register_link_routes(app,*,service_for_workspace):
 from retail_urls import retail_router
 app = retail_router(app)
 if getattr(app.state,'link_routes_registered',False):raise ValueError('link_routes_already_registered')
 app.state.link_routes_registered=True
 receiving=0
 async def context(request,write=False):
  session=getattr(request.state,'session',None);selected=getattr(request.state,'workspace',None)
  if not session:_deny(401,'sign_in_required')
  principal=ChatPermissions.principal(session)
  if not principal or not isinstance(selected,dict):_deny(403,'workspace_access_required')
  writable=workspace_writable(session)
  if write:
   if not writable:_deny(403,'read_only_workspace')
   if not page_request(request.headers):_deny(403,'csrf_required')
   origin=_header(request,'origin')
   if origin and urlsplit(origin).netloc!=request.headers.get('host'):_deny(403,'same_origin_required')
  service=await call(service_for_workspace,selected)
  if not isinstance(service,LinkIntake):_deny(503,'link_service_unavailable')
  await call(service.ready)
  actor=IntakeActor(principal,read_only=not writable)
  policy=await call(service.host.policy)
  datasets=selected['index'].status().get('datasets',[])
  collections={v for v in datasets if isinstance(v,str)}
  if not collections and policy.empty_workspace_collection:collections.add(policy.empty_workspace_collection)
  collections.update(await call(service.host.collections,actor))
  return service,actor,policy,collections
 async def row(service,actor,identity,collections):
  value=await call(service.get,actor,identity)
  if value['collection']not in collections:_deny(404,'collection_not_found')
  return value
 def response(value,code=200):return JSONResponse({'link':public(value)},status_code=code,headers=PRIVATE_HEADERS)
 @app.get('/api/retail/source-links')
 async def listing(request:Request):
  service,actor,policy,collections=await context(request)
  if set(request.query_params)-{'collection'}or len(request.query_params.getlist('collection'))>1:_deny(400,'invalid_query')
  collection=request.query_params.get('collection')
  if collection is not None and collection not in collections:_deny(404,'collection_not_found')
  values=await call(service.list,actor,collection=collection);grants=await call(service.host.capabilities,actor)
  return JSONResponse({'links':[public(v)for v in values if v['collection']in collections],
   'intake':{'available':service.enabled and policy.intake_enabled and policy.worker_ready,
    'can_create':service.enabled and policy.intake_enabled and policy.worker_ready and not actor.read_only and grants.get('can_upload',False),
    'can_manage':not actor.read_only and bool(values),'collections':sorted(collections),
    'default_collection':policy.empty_workspace_collection if policy.empty_workspace_collection in collections else None}},headers=PRIVATE_HEADERS)
 @app.post('/api/retail/source-links')
 async def create(request:Request):
  nonlocal receiving
  service,actor,policy,collections=await context(request,True)
  if receiving>=4:_deny(429,'link_request_capacity')
  receiving+=1
  try:body=await _json_body(request,CreateLink,policy)
  finally:receiving-=1
  if body.collection not in collections:_deny(404,'collection_not_found')
  if not(service.enabled and policy.intake_enabled and policy.worker_ready):_deny(503,'source_service_unavailable')
  value=await call(service.create,actor,**body.model_dump())
  await call(service.start,actor,value['id'])
  return response(value,202)
 @app.get('/api/retail/source-links/{identity}')
 async def detail(identity:str,request:Request):
  service,actor,_,collections=await context(request)
  return response(await row(service,actor,identity,collections))
 @app.post('/api/retail/source-links/{identity}/retry')
 async def retry(identity:str,request:Request):
  service,actor,policy,collections=await context(request,True)
  await _json_body(request,Empty,policy,empty=True)
  await row(service,actor,identity,collections)
  return response(await call(service.start,actor,identity),202)
 @app.delete('/api/retail/source-links/{identity}')
 async def cancel(identity:str,request:Request):
  service,actor,_,collections=await context(request,True)
  await row(service,actor,identity,collections)
  value=await call(service.cancel,actor,identity)
  return response(value,202 if value['status']=='cancelling'else 200)
