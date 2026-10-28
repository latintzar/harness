"""Monitoring HTTP adapter; ordinary workspace/chat authorization remains authoritative."""
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from chat_permissions import workspace_writable
from monitoring import ProfileDefinition, EventDefinition, available_monitoring_datasets, is_reference_event
from background_agents import ensure_agent_conversation
from retail_agent_inputs import RetailAgentDefinition as AgentDefinition

class ProfileUpdate(ProfileDefinition):
    expected_revision:int=Field(ge=1)
class Promotion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    agent_kind:Literal['tracker','action']
    definition:AgentDefinition

PRESENTATION_MOVED='This tracker’s presentation changed while the canvas was open. Pick the canvas again.'
TRACKER_LOCATION='The canvas is saved with this tracker once it belongs to a location. Its chat sets that up.'

class PresentationChange(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int|None=Field(default=None,ge=0)
    tile:dict|None=None
    canvas:dict|None=None

def phone_default(selected,record):
    """Phone chat is a default capability of every Tracking Agent.

    Promotion creates its delivery policy, the creator's participant row and
    enabled binding, and the contact card when the creator has no verified
    number. Promotion succeeds whatever the phone state is: the same records
    appear the first time the host reads the agent's phone block.
    """
    phone=getattr(selected['agent'],'phone_chat',None)
    if phone is None or record.get('agent_kind')!='tracker':return
    import sqlite3
    try:phone.promote(record)
    except (ValueError,OSError,sqlite3.Error):pass

def promotion(selected,chat_id,kind,definition,*,allow_active=False):
    child=selected['agent'].children.get(chat_id)
    if not child or child.get('parent_id') or child.get('kind')=='organization_chat' or child.get('connector_id'):
        raise ValueError('Choose your ordinary saved conversation.')
    if child.get('agent_id'):
        prior=selected['agents'].get(child['agent_id'])
        if prior['agent_kind']==kind:
            phone_default(selected,prior)
            return prior,child
        raise ValueError('Keep the existing agent’s kind and history.')
    if not allow_active and (chat_id in selected['agent'].active or child.get('running')):
        raise ValueError('Finish the current reply before saving this conversation as an agent.')
    if child.get('dataset')!=definition['dataset']:
        raise ValueError('Keep this conversation’s business collection.')
    record=selected['agents'].create({**definition,'agent_kind':kind},available_monitoring_datasets(selected,child['owner_principal_id']),
        owner_principal_id=child['owner_principal_id'],conversation_chat_id=chat_id)
    child=ensure_agent_conversation(selected['agents'],selected['agent'],record)
    phone_default(selected,record)
    return record,child

def observation_detail(selected,record,reader=None,guards=None):
    monitoring=selected['monitoring'];owner=record['owner_principal_id']
    profiles=[monitoring.get_profile(owner,key) for key in record.get('profile_ids',[])]
    incidents=monitoring.events_for(record)
    summary=None
    for run in record.get('runs',[]):
        if run['status']!='completed':continue
        saved=selected['agents'].run_report(record['id'],run['id'])
        # Existing run reports are immutable slices of the continuing conversation.
        messages=saved.get('report_messages',[])
        content=[m.get('content','') for m in messages if m.get('role')=='assistant' and isinstance(m.get('content'),str) and m['content'].strip()]
        if content:
            summary={'text':'\n\n'.join(content),'updated_at':saved.get('completed_at'),'run_id':run['id'],
                'definition_revision':saved['revision']};break
    return {'agent_id':record['id'],'agent_kind':record.get('agent_kind','action'),
        'profiles':profiles,'profile':profiles[0] if profiles else None,'summary':summary,
        'incidents':incidents,'event_count':len(incidents),'more_available':len(incidents)==500,
        **shown_presentation(selected,record,reader or owner,guards)}

def shown_presentation(selected,record,reader,guards=None):
    """What a Tracking Agent's canvas draws beside its events, once its tracker has a record.

    `presentation` is the saved one (or the default at revision 0). `output` is the tracker's
    published output as the page reads it: the tile row Home is showing, with its period readings,
    and the canvas with the presentation applied; `families` says which canvas families that output
    fills. While the owner's page holds this tracker with this presentation, the answer is that
    row and that canvas, whichever output revision the store has reached: a pass that published and
    stopped ahead of the page leaves Home and the canvas on one revision, and both move with the
    page. A tracker the page has yet to hold is assembled as the page would write it, readings and
    currency included. The output is read with the cameras it covers, by the reader's present
    access; `guards` takes that check for the host to ask again while it answers.
    """
    if record.get('agent_kind')!='tracker':return {}
    from pathlib import Path
    from core_trackers import presentation as kit
    from tracker_presentation import tracker_of
    owner,state=record['owner_principal_id'],Path(selected['state'])
    tracker=tracker_of(state,owner,record['dataset'],record['id'])
    if tracker is None:return {}
    output=kit.current_output(state,owner,tracker)
    saved=kit.saved_presentations(state,owner,record['dataset']).get(tracker['id'])
    saved=saved if kit.belongs(saved,tracker) else None
    answer={'tracker_id':tracker['id'],'presentation':kit.presentation_of(tracker,saved)}
    if output is None:return answer
    page=published_page(state,owner)
    row,canvas=page['rows'].get(tracker['id'],(None,None))
    if row is not None and row.get('presentation_revision')!=answer['presentation']['revision']:row=canvas=None
    cameras=sorted({*(output.get('camera_ids') or ()),*((row or {}).get('camera_ids') or ())})
    covered=lambda:selected['monitoring'].profile_authorized(reader,{'dataset':record['dataset'],'location_id':tracker['location_id'],'camera_ids':cameras})
    if not covered():return answer
    if row is None:
        row,canvas=assembled(selected,record,tracker,page)
        if row is None or canvas is None:return answer
    # An unsaved presentation takes its blocks from the fields of the output it draws.
    if saved is None:answer['presentation']=kit.presentation_of(tracker,None,{'tile':row,'canvas':canvas})
    if guards is not None:guards.append(covered)
    return {**answer,'output':{'revision':row['revision'],'tile':row,'canvas':canvas},'families':kit.families_for({'tile':row,'canvas':canvas})}

def assembled(selected,record,tracker,page):
    """A company tracker's row and canvas as the page would carry them now, from its stored output and readings."""
    from pathlib import Path
    import retail_records as records
    from import_control_room_bundle import location_currency,tracker_row
    owner,place=record['owner_principal_id'],tracker['location_id']
    company=records.Company(Path(selected['state']),owner,record['dataset'],selected['business_scope'],selected['monitoring'],selected['location_intelligence'])
    currency=page['currency'] if page['location_id']==place else None
    return tracker_row(company,place,tracker['id'],currency=currency or location_currency(company,place))

_PAGES={}
NO_PAGE={'rows':{},'currency':None,'location_id':None}

def published_page(state,owner):
    """What the owner's published page holds of the company's own trackers, read once per version of the page:
    each tracker's tile row with its canvas, the page's currency and the location it shows."""
    import json
    from control_room_routes import owner_directory
    path=owner_directory(state,owner)/'data.json'
    try:stat=path.stat()
    except OSError:return NO_PAGE
    signature=(stat.st_ino,stat.st_size,stat.st_mtime_ns);held=_PAGES.get(str(path))
    if held is None or held[0]!=signature:
        try:data=json.loads(path.read_text())
        except (OSError,ValueError):return NO_PAGE
        if not isinstance(data,dict):return NO_PAGE
        rows,canvases=data.get('core_trackers'),data.get('core_tracker_canvases')
        location=data.get('location')
        held=(signature,{'rows':{row['tracker_id']:(row,canvases[row['key']]) for row in (rows if isinstance(rows,list) else [])
                if isinstance(row,dict) and row.get('core') is False and row.get('tracker_id') and isinstance(canvases,dict) and isinstance(canvases.get(row.get('key')),dict)},
            'currency':data.get('currency') if isinstance(data.get('currency'),str) else None,
            'location_id':location.get('id') if isinstance(location,dict) else None})
        if len(_PAGES)>=64:_PAGES.clear()
        _PAGES[str(path)]=held
    return held[1]

def register_monitoring_routes(app):
    from retail_urls import retail_router
    app = retail_router(app)
    def actor(request,write=False):
        selected=request.state.workspace;session=request.state.session
        principal=selected['chats'].principal(session)
        if not principal:raise HTTPException(403,'Open your workspace to view observations.')
        if write and not workspace_writable(session):raise HTTPException(403,'Edit observations from your own workspace.')
        return selected,principal
    def allowed(request,record,write=False):
        permitted=request.state.workspace['chats'].can_write if write else request.state.workspace['chats'].can_read
        if not permitted(record,request.state.session):raise HTTPException(404,'Choose a record from your current workspace.')
        request.state.scope_guards.append(lambda:permitted(record,request.state.session))
        if record.get('source'):
            request.state.scope_guards.append(lambda:bool(request.state.workspace['monitoring'].source_lookup(record['owner_principal_id'],record['source']['clip_id'])))
        return record
    def execute(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    def execute_save(fn,*args,**kwargs):
        """A person's save, answered in a person's words. A presentation someone saved in between answers 409, so
        the page reads it and sends the pick once more; a tracker still to get its location answers 409 too, and the
        page keeps what it holds; a canvas the output leaves unfilled answers the sentence its control shows."""
        from core_trackers import presentation as kit
        from tracker_presentation import AGENT_LOCATION
        try:return fn(*args,**kwargs)
        except kit.Unfilled as exc:raise HTTPException(400,exc.problem['needs']) from exc
        except ValueError as exc:
            said=str(exc)
            if said==kit.READ_AGAIN:raise HTTPException(409,PRESENTATION_MOVED) from exc
            if said==AGENT_LOCATION:raise HTTPException(409,TRACKER_LOCATION) from exc
            raise HTTPException(403 if said==kit.MANAGEMENT else 400,said) from exc
    @app.get('/api/retail/monitoring/profiles')
    async def profiles(request:Request,dataset:str|None=None):
        selected,owner=actor(request)
        records=execute(selected['monitoring'].list_profiles,owner,dataset)
        records=[r for r in records if selected['chats'].can_read(r,request.state.session)]
        for r in records:allowed(request,r)
        return {'profiles':records}
    @app.post('/api/retail/monitoring/profiles')
    async def create_profile(body:ProfileDefinition,request:Request):
        selected,owner=actor(request,True)
        allowed(request,{**body.model_dump(),'id':'new-profile','owner_principal_id':owner},True)
        return execute(selected['monitoring'].save_profile,owner,body.model_dump(),datasets=available_monitoring_datasets(selected,owner))
    @app.get('/api/retail/monitoring/profiles/{profile_id}')
    async def profile(profile_id:str,request:Request,revision:int|None=None):
        selected,owner=actor(request)
        return allowed(request,execute(selected['monitoring'].get_profile,owner,profile_id,revision=revision))
    @app.post('/api/retail/monitoring/profiles/{profile_id}')
    async def update_profile(profile_id:str,body:ProfileUpdate,request:Request):
        selected,owner=actor(request,True)
        allowed(request,execute(selected['monitoring'].get_profile,owner,profile_id),True)
        return execute(selected['monitoring'].save_profile,owner,body.model_dump(exclude={'expected_revision'}),
            datasets=available_monitoring_datasets(selected,owner),profile_id=profile_id,expected_revision=body.expected_revision)
    @app.get('/api/retail/monitoring/events')
    async def events(request:Request,dataset:str,profile_id:str|None=None,limit:int=200,after:float=0,evaluation:bool=False):
        selected,owner=actor(request)
        rows=execute(selected['monitoring'].list_events,owner,dataset,profile_ids=[profile_id] if profile_id else None,limit=limit,after=after,include_reference=evaluation)
        rows=[row for row in rows if selected['chats'].can_read(row,request.state.session)]
        for row in rows:allowed(request,row)
        return {'events':rows,'more_available':len(rows)==limit}
    @app.get('/api/retail/monitoring/events/{event_id}')
    async def event(event_id:str,request:Request,evaluation:bool=False):
        selected,owner=actor(request)
        result=execute(selected['monitoring'].get_event,owner,event_id)
        if is_reference_event(result) and not evaluation:raise HTTPException(404,'Select evaluation mode to read publisher reference annotations.')
        return allowed(request,result)
    @app.post('/api/retail/monitoring/events')
    async def commit_event(body:EventDefinition,request:Request):
        selected,owner=actor(request,True)
        allowed(request,execute(selected['monitoring'].get_profile,owner,body.profile_id),True)
        return execute(selected['monitoring'].commit_event,owner,body.model_dump())
    @app.get('/api/agents/{agent_id}/observations')
    async def observations(agent_id:str,request:Request):
        selected,owner=actor(request)
        record=allowed(request,execute(selected['agents'].get,agent_id))
        return execute(observation_detail,selected,record,owner,request.state.scope_guards)
    @app.post('/api/agents/{agent_id}/presentation')
    async def save_presentation(agent_id:str,body:PresentationChange,request:Request):
        """A person's change of a Tracking Agent's tile or canvas: the same save as the harness's
        save_tracker_presentation. The agent's own record stays as it is, so a running agent keeps running."""
        import asyncio
        from tracker_presentation import save_for_agent
        selected,owner=actor(request,True)
        record=allowed(request,execute(selected['agents'].get,agent_id),True)
        if record.get('agent_kind')!='tracker':raise HTTPException(400,'Choose a Tracking Agent.')
        change=body.model_dump(exclude={'expected_revision'},exclude_none=True)
        return await asyncio.to_thread(execute_save,save_for_agent,selected,record,change,expected_revision=body.expected_revision)
    @app.post('/api/chats/{chat_id}/promote-agent')
    async def promote(chat_id:str,body:Promotion,request:Request):
        selected,owner=actor(request,True)
        child=allowed(request,selected['agent'].children.get(chat_id),True)
        if child.get('owner_principal_id')!=owner:raise HTTPException(403,'Save your own conversation as an agent.')
        record,child=execute(promotion,selected,chat_id,body.agent_kind,body.definition.model_dump())
        return {'agent':record,'chat':selected['chats'].get(child,request.state.session)}
