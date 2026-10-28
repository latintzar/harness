"""Observation-first native tools shared with the scoped read-only MCP surface.

The harness's tools for observations and trackers live here: every capability is a tool first, and
a screen shows what these tools made. A read is listed on every surface at once (the hosted chat,
its native descendants, public MCP and local MCP) through `READ_NAMES`. A write is a tool of the
app's hosted parent conversation, and today that conversation is its one road: a customer's own
agent over MCP reads. Its road to the writes is proposed in MCP-CONTRACT.md (`send_agent_message`
into that conversation) and waits for its build.

Tool families beside the observation tools are registered in `FAMILIES`, at the end of this module;
`registry` checks their names as the module loads.
"""
from copy import copy
from dataclasses import dataclass,replace
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
import re
from types import SimpleNamespace
from monitoring import ProfileDefinition,EventDefinition,is_reference_event

OBSERVATION_READS=frozenset({'list_monitoring_profiles','list_tracker_events','get_tracker_event'})
OBSERVATION_WRITES=frozenset({'save_monitoring_profile','record_tracker_event','promote_chat_to_agent'})

def tool(name,description,properties,required=()):
    return {'type':'function','name':name,'description':description,'inputSchema':{
        'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}

def observation_specs(write=True):
    values=[
        tool('list_monitoring_profiles','Read versioned observation interests and event vocabulary for this business.',{}),
        tool('list_tracker_events','Read committed source-backed annotations first when answering questions about observed activity. Timestamps are source-relative; commit time is separate. Results are observations, not proven intent.',
            {'profile_id':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':500},'after':{'type':'number','minimum':0},'evaluation':{'type':'boolean','description':'Include publisher reference annotations only for explicit evaluation comparisons.'}}),
        tool('get_tracker_event','Read one committed observation with its visit-scoped identity and original evidence link.',{'event_id':{'type':'string'},'evaluation':{'type':'boolean','description':'Read publisher reference annotations for an explicit evaluation comparison.'}},['event_id'])]
    if write:
        definition=ProfileDefinition.model_json_schema();definition['properties'].pop('dataset');definition['required'].remove('dataset')
        definition['properties'].update({'profile_id':{'type':'string'},'expected_revision':{'type':'integer','minimum':1}})
        values.append({'type':'function','name':'save_monitoring_profile','description':'Save or revise the requested tracker observation vocabulary. Read the current profile first. It configures future camera observation interests; saving does not start a GPU or camera feed.','inputSchema':definition})
        values.append({'type':'function','name':'record_tracker_event','description':'Record a directly observed event using this profile’s vocabulary and an accessible original clip. Use exact source timestamps; describe visible evidence and uncertainty. IDs stay within the source recording. This is a model observation, not a human-confirmed fact.','inputSchema':EventDefinition.model_json_schema()})
        from retail_agent_inputs import RetailAgentDefinition as AgentDefinition
        schema=AgentDefinition.model_json_schema();schema['properties'].pop('dataset');schema['required'].remove('dataset')
        schema['required']=list(set(schema['required'])|{'agent_kind'})
        schema['properties']['location_id']={'type':'string','minLength':1,'maxLength':120,'description':'For a Tracker Agent: the location it tracks. Its tracker record is written there, beside the Core Trackers; with one installed location, that one is taken.'}
        values.append({'type':'function','name':'promote_chat_to_agent','description':'When the user asks, make this ordinary conversation a Tracker Agent or Action Agent while keeping its complete history. Saves a draft; activation uses existing controls. Set input_kind=tracker_events for annotation-based summaries/actions. A Tracker Agent gets its tracker record at a location, where its processing script publishes a tile and canvas.','inputSchema':schema})
    return values


def specs(write=True):
    return observation_specs(write) + [spec for family in FAMILIES for spec in family.specs(write)]

def scoped(store,index,*,host=None,chat_id=None):
    result=copy(store)
    def lookup(owner,key):
        source=index.get_clip(key)
        if source is not None:return source
        registered=store.registered_source(owner,key)
        if registered is None:return None
        if host is not None:
            from monitoring import resolve_monitoring_source
            return resolve_monitoring_source(host,owner,key,chat_id=chat_id)
        scopes=getattr(index,'business_scopes',[])
        current=getattr(index,'business_scope_current',None)
        if current is not None and not current():return None
        for scope in scopes:
            if scope['collection']==registered['dataset']:
                return registered if not scope['configured'] or scope['can_query'] and (scope['includes_unassigned'] or registered['video_id'] in scope['source_ids']) else None
        # Host-owned local MCP may have legacy scope without a configured graph.
        from retail_business_scope import legacy_scope_current
        return registered if legacy_scope_current(store.state,registered['dataset']) else None
    result.source_lookup=lookup
    # The view this read is scoped by: a family's read takes its location guard from it.
    result.scope_view=index
    return result

def family_of(name):
    return next((family for family in FAMILIES if name in family.READ_NAMES or name in family.WRITE_NAMES),None)

def declared(family,name,args):
    """A family tool's arguments, checked against the schema the tool itself declares. MCP checks
    them on its way in; the hosted chat hands them over as the model wrote them."""
    from jsonschema import Draft202012Validator
    schema=next(spec['inputSchema'] for spec in family.specs(True) if spec['name']==name)
    if not isinstance(args,dict) or not Draft202012Validator(schema).is_valid(args):
        raise ValueError('Use the declared fields of this tool.')
    return args

def read(store,owner,dataset,name,args,agents=None):
    family=family_of(name)
    if family is not None:
        if name not in family.READ_NAMES:raise ValueError('Choose an available observation read tool.')
        return family.read(ReadContext(store,owner,dataset,agents),name,declared(family,name,args))
    if name=='list_monitoring_profiles':
        # The location's trackers ride with its profiles: Core Trackers (core, locked, without a
        # chat) first, then the customer's own trackers and Tracking Agents.
        from core_trackers import location_trackers
        allowed=lambda record:store.profile_authorized(owner,{'dataset':dataset,'location_id':record['location_id'],'camera_ids':[]})
        return {'profiles':store.list_profiles(owner,dataset),'trackers':location_trackers(store.state,owner,dataset,agents=agents,allowed=allowed)}
    if name=='list_tracker_events':return {'events':store.list_events(owner,dataset,profile_ids=[args['profile_id']] if args.get('profile_id') else None,limit=args.get('limit',200),after=args.get('after',0),include_reference=args.get('evaluation',False))}
    result=store.get_event(owner,args['event_id'])
    if is_reference_event(result) and args.get('evaluation') is not True:raise ValueError('Select evaluation mode to read publisher reference annotations.')
    if result['dataset']!=dataset:raise ValueError('Use an observation in the current business collection.')
    return result

def admit_event_history(agent,chat_id,events):
    host=getattr(agent,'business_scope_host',None)
    if host is None or not events:return
    from business_scope import ScopeActor
    record=agent.children[chat_id];root_id=record.get('native_root_id') or record.get('parent_id') or chat_id
    root=agent.children[root_id];store=host['business_scope'];owner=root['owner_principal_id'];collection=root['dataset']
    actor=ScopeActor(store.company_id,owner)
    scope=store.chat_scope(actor,root_id,collection)
    store.admit_history(actor,root_id,[scope],{collection:sorted({e['recording_id'] for e in events})},legacy=not scope['configured'])

def _admit(family,agent,chat_id,scope,name,args,run,store,origin):
    """What every family tool passes before its handler: a current member, the declared fields, and
    for a write the active parent conversation. Returns the handler's context and its arguments."""
    # Tracker records belong to a person. The Organization conversation acts as the member whose turn this is.
    from scope_host import turn_actor
    actor=turn_actor(agent,chat_id)
    if not actor:raise ValueError('Continue through a current company member’s conversation.')
    args=declared(family,name,args)
    if name in family.READ_NAMES:
        return ReadContext(store,actor,scope,getattr(agent,'automation_store',None)),args
    if origin.get('parent_id') or origin.get('native_root_id') or not run or run.get('cancel') and run['cancel'].is_set():
        raise ValueError('Save tracker changes from the active parent conversation.')
    return WriteContext(store,actor,scope,getattr(agent,'automation_store',None),agent,chat_id,origin,run),args

async def call_async(agent,chat_id,scope,name,args,run,index=None):
    """A family write that waits on the event loop (`ASYNC_NAMES`): the checks of `call`, then the
    family's `acall`. The hosted conversation awaits it, so the wait holds no worker thread and
    follows the turn's stop."""
    import asyncio
    def admitted():
        origin=agent.children.get(chat_id,{})
        if not origin.get('owner_principal_id') or origin.get('dataset')!=scope or not getattr(agent,'monitoring_store',None):
            raise ValueError('Use the current authenticated business conversation.')
        family=family_of(name)
        if family is None or name not in getattr(family,'ASYNC_NAMES',()):raise ValueError('Choose an available observation tool.')
        store=scoped(agent.monitoring_store,index or agent.retrieval,host=getattr(agent,'business_scope_host',None),chat_id=chat_id)
        return family,*_admit(family,agent,chat_id,scope,name,args,run,store,origin)
    family,context,checked=await asyncio.to_thread(admitted)
    return await family.acall(context,name,checked)

def call(agent,chat_id,scope,name,args,run,index=None):
    origin=agent.children.get(chat_id,{})
    from scope_host import turn_actor
    owner=turn_actor(agent,chat_id)
    if not owner or origin.get('dataset')!=scope or not getattr(agent,'monitoring_store',None):
        raise ValueError('Use the current authenticated business conversation.')
    store=scoped(agent.monitoring_store,index or agent.retrieval,host=getattr(agent,'business_scope_host',None),chat_id=chat_id)
    family=family_of(name)
    if family is not None:
        context,args=_admit(family,agent,chat_id,scope,name,args,run,store,origin)
        return family.read(context,name,args) if name in family.READ_NAMES else family.call(context,name,args)
    if name in OBSERVATION_READS:
        result=read(store,owner,scope,name,args,agents=getattr(agent,'automation_store',None))
        if name=='list_tracker_events':admit_event_history(agent,chat_id,result['events'])
        elif name=='get_tracker_event':admit_event_history(agent,chat_id,[result])
        return result
    if origin.get('parent_id') or origin.get('native_root_id') or not run or run.get('cancel') and run['cancel'].is_set():
        raise ValueError('Save tracker changes from the active parent conversation.')
    if name=='save_monitoring_profile':
        body={k:v for k,v in args.items() if k not in {'profile_id','expected_revision'}}
        return store.save_profile(owner,{**body,'dataset':scope},datasets=[scope],profile_id=args.get('profile_id'),expected_revision=args.get('expected_revision'))
    if name=='record_tracker_event':
        profile=store.get_profile(owner,args['profile_id'])
        if profile['dataset']!=scope:raise ValueError('Use a profile in this business collection.')
        return store.commit_event(owner,args,origin='model_observation')
    from monitoring_routes import promotion
    selected={'agent':agent,'agents':agent.automation_store,'index':agent.retrieval,'monitoring':agent.monitoring_store}
    place=args.get('location_id');definition={k:v for k,v in args.items() if k!='location_id'}
    # A Tracking Agent is linked to its tracker record at a location: the location lists one row for
    # the two, and the agent's processing script publishes for that record.
    link=None
    if args['agent_kind']=='tracker' and find_spec('tracker_scripts'):
        link=WriteContext(store,owner,scope,getattr(agent,'automation_store',None),agent,chat_id,origin,run)
        try:company=link.company()
        except ValueError:link=None
    if place is not None:
        if link is None:raise ValueError('A location belongs to a Tracker Agent of the current location workspace.')
        import_module('tracker_scripts').link_check(link,company,place)
    record,child=promotion(selected,chat_id,args['agent_kind'],{**definition,'dataset':scope},allow_active=True)
    if link is not None:
        try:linked=import_module('tracker_scripts').promote_link(link,company,record,place)
        except ValueError as exc:linked={'tracker':None,'next_step':str(exc)}
        record={**record,'tracker':linked['tracker'],**({'tracker_next_step':linked['next_step']} if linked['next_step'] else {})}
    return record


# ── Tool families ─────────────────────────────────────────────────────────────────────────────

from retail_tool_context import ReadContext, WriteContext


# Trackers: definitions, outputs, schedule and runs.
TRACKER_KEY=r'^[a-z][a-z0-9_]{1,39}$'
MAX_OWN_TRACKERS=50
ORGANIZATION_ONLY='Use the Organization conversation for this: it keeps each location’s required coverage.'
MANAGEMENT='Use location management access to change this location’s trackers.'
INSTALL_FIRST='Trackers run at a location once its Core Trackers are installed. Install them from the Organization conversation.'
INSTALLED_BY_MEMBER='The Core Trackers of this location run as the member who installed them. Ask that member to install them again.'
TOOK_OVER='The earlier installation ran as a member whose access to this location changed. These Core Trackers now run as you and read the observations recorded with your access.'
TAKE_OVER='From the Organization conversation, install this location’s Core Trackers to run them as you.'
CAMERA_SCOPE='This output reads cameras beyond your current access.'
SERVICE_RUNS='The tracker service runs this pass at its next sweep, within seconds.'
SERVICE_WAITS='The request waits for the tracker service of this host. The pass runs once the service is on.'

def tracker_specs(write=True):
    location={'location_id':{'type':'string'}}
    key={'type':'string','pattern':TRACKER_KEY}
    values=[
        tool('list_trackers','Read the trackers of this business: Core Trackers first, then the company’s own trackers and Tracking Agents. Each row carries its definition, its schedule, a summary of its current output and its last run. An output is read with the cameras it covers.',location),
        tool('get_tracker','Read one tracker in full: its definition, its current tile and canvas, its processing script record with the script’s revision, policy, health and last test, its view, its schedule and its recent runs. Use the id from list_trackers. rows bounds the canvas rows and tile events returned.',
            {'tracker_id':{'type':'string'},'rows':{'type':'integer','minimum':1,'maximum':500}},['tracker_id']),
        tool('list_tracker_runs','Read a location’s tracker runs, newest first: what started each run, how it ended, which outputs changed and any error. scripts lists each processing-script run of a pass, and each script test as a row of its own with the trigger test: its status, seconds, error, whether an output was published or proposed, and its wake. The schedule names the state of the tracker service; a location whose trackers another member installed shows why they stopped.',
            {**location,'tracker_key':key,'limit':{'type':'integer','minimum':1,'maximum':100}},['location_id'])]
    if write:
        values.extend([
            tool('save_tracker','Add one of the company’s own trackers to a location, or change it. Read list_trackers first; to change a tracker, pass its current revision as expected_revision. Core Trackers follow their recipe and stay as they are.',
                {**location,'key':key,'label':{'type':'string','minLength':1,'maxLength':80},'observes':{'type':'string','minLength':1,'maxLength':500},
                 'event_types':{'type':'array','maxItems':24,'uniqueItems':True,'items':{'type':'string','pattern':r'^[a-z][a-z0-9_]{0,59}$'}},
                 'expected_revision':{'type':'integer','minimum':1}},['location_id','key','label','observes']),
            tool('remove_tracker','Remove one of the company’s own trackers from a location. Core Trackers stay.',{**location,'key':key},['location_id','key']),
            tool('set_tracker_schedule','Set when trackers run. With key: whether one of the company’s own trackers joins the automatic passes. With location_id alone, from the Organization conversation: the location’s automatic passes, the minimum interval between them, the time budget of a pass and a daily time in the location’s own clock. Pass the schedule revision from list_trackers as expected_revision.',
                {**location,'key':key,'automatic':{'type':'boolean'},'min_interval_seconds':{'type':'integer','minimum':30,'maximum':86400},
                 'budget_seconds':{'type':'integer','minimum':5,'maximum':180},
                 'daily_at':{'type':'string','pattern':r'^(([01]\d|2[0-3]):[0-5]\d)?$','description':'HH:MM in the location’s own time. An empty string returns the daily pass to the start of the local day.'},
                 'expected_revision':{'type':'integer','minimum':1}},['location_id','expected_revision']),
            tool('run_trackers','Ask for a pass of a location’s trackers now. The tracker service runs it within seconds, one pass per location at a time; starts in the result says whether the service is on or the request waits for it. Read list_tracker_runs for the result. A pass computes from saved observations and is free of model calls.',
                {**location,'only':{'type':'array','minItems':1,'maxItems':50,'uniqueItems':True,'items':key}},['location_id']),
            tool('install_core_trackers','From the Organization conversation: give a location the Core Trackers of its business type, or bring them to the current recipe. Safe to repeat.',
                {**location,'business_type':{'type':'string','enum':['retail']}},['location_id','business_type'])])
    return values

def _schedule(state,tracker=None,*,service):
    """A location's schedule as a tool returns it; for one of the company's own trackers, its own automatic setting.
    `service` is the tracker service's note for this company: on, off, or waiting for its first sweep."""
    policy=state['policy']
    automatic=policy['automatic'] and (tracker is None or tracker['core'] or tracker['key'] not in policy['paused'])
    return {'automatic':automatic,'min_interval_seconds':policy['min_interval_seconds'],'budget_seconds':policy['budget_seconds'],
        'daily_at':policy['daily_at'],'revision':state['policy_revision'],'requested':state['requested_at'] is not None,
        'running':state['running'],'needs_attention':state['needs_attention'],'service':service}

def _run_rows(context,location_id,rows):
    """A location's run rows for this reader. The counts a pass derived add up every camera of the
    location, so they are read with those cameras, like the outputs."""
    import tracker_runner as runner
    if context.allowed(location_id,runner.output_cameras(context.state,context.owner,context.dataset,location_id)):return rows
    return [{**run,'derived':None} for run in rows]

def _run_of(run,key):
    """One run as it concerns one tracker."""
    mine=lambda rows:[row for row in rows if row.get('key')==key]
    fault=next((row['error'] for row in run['errors'] if row.get('key')==key),None)
    return {**run,'outputs':mine(run['outputs']),'errors':mine(run['errors']),'skipped':mine(run['skipped']),
        'scripts':mine(run.get('scripts') or []),'error':fault or run['error']}

RUN_PARTS=('outputs','errors','skipped','scripts')

def _script(context,row):
    """A tracker's processing script as get_tracker returns it: the record's processor layer, and for
    a saved script its revision, policy, health and last test."""
    if row['core'] or not find_spec('tracker_scripts'):return row['layers']['processor']
    return {**row['layers']['processor'],**import_module('tracker_scripts').describe_saved(context.state,context.owner,row)}

def _trackers(context,location_id=None):
    """The listing rows of `list_monitoring_profiles`, each with its schedule, output summary and last run."""
    import tracker_runner as runner
    from core_trackers import location_trackers,tracker_records
    state,owner,dataset=context.state,context.owner,context.dataset
    runs=runner.TrackerRuns(state);service=runs.service()['state']
    saved={record['id']:record for record in tracker_records(state,owner,dataset)}
    outputs={output['tracker_id']:output for output in runner.current_outputs(state,owner,dataset)}
    places,access={},{}
    def place(key):
        if key not in places:places[key]=(runs.state(owner,dataset,key),runs.last_run(owner,key))
        return places[key]
    def covered(key,cameras=()):
        if (key,cameras) not in access:access[(key,cameras)]=context.allowed(key,cameras)
        return access[(key,cameras)]
    rows=[]
    for row in location_trackers(state,owner,dataset,location_id,agents=context.agents,allowed=lambda record:covered(record['location_id'])):
        if not row.get('location_id'):
            # A Tracking Agent is a conversation: get_agent reads its mode, runs and summary.
            rows.append({**row,'schedule':None,'output':None,'last_run':None});continue
        schedule,last=place(row['location_id']);output=outputs.get(row['id']);tile=(output or {}).get('tile') or {}
        # An output is the record of the cameras it read: a reader whose access covers them reads it.
        readable=output is not None and covered(row['location_id'],tuple(sorted(output.get('camera_ids') or ())))
        if output is not None and not readable:outputs.pop(row['id'])
        rows.append({**row,'revision':saved[row['id']].get('revision'),'schedule':_schedule(schedule,row,service=service),
            'output':{'revision':output['revision'],'state':tile.get('state'),'needs':tile.get('needs'),'value':tile.get('value'),
                'unit':tile.get('unit'),'period':output.get('period'),'summary':tile.get('summary')} if readable else None,
            **({'output_access':CAMERA_SCOPE} if output is not None and not readable else {}),
            'last_run':{k:_run_of(last,row['key'])[k] for k in ('id','trigger','status','started_at','finished_at','error')} if last else None})
    return rows,outputs,runs

def _other_installation(context,location_id,runs):
    """A location the reader manages whose trackers run as another member, once that member's
    passes have stopped: the schedule and the last run of that installation, and the next step."""
    import tracker_runner as runner
    installed=runner.installed_locations(context.state)
    if (context.owner,context.dataset,location_id) in installed or not context.manages(location_id):return None
    for member,collection,place in installed:
        if (collection,place)!=(context.dataset,location_id):continue
        state,last=runs.state(member,collection,place),runs.last_run(member,place)
        if state['needs_attention'] or last and last['error'] in {runner.OWNER_ACCESS,runner.CAMERA_ACCESS}:
            return {'schedule':_schedule(state,service=runs.service()['state']),
                'last_run':{k:last[k] for k in ('trigger','status','started_at','finished_at','error')} if last else None,'next_step':TAKE_OVER}
    return None

def tracker_read(context,name,args):
    if name=='list_tracker_runs':
        import tracker_runner as runner
        location_id=context.require(args['location_id']);key=args.get('tracker_key');runs=runner.TrackerRuns(context.state)
        rows=_run_rows(context,location_id,runs.runs(context.owner,context.dataset,location_id,limit=min(100,args.get('limit',20))))
        if key:rows=[_run_of(run,key) for run in rows if any(row.get('key')==key for part in RUN_PARTS for row in run.get(part) or [])]
        service,other=runs.service(),_other_installation(context,location_id,runs)
        return {'location_id':location_id,'schedule':_schedule(runs.state(context.owner,context.dataset,location_id),service=service['state']),'runs':rows,
            **({'service_fault':service['fault']} if service['fault'] else {}),**({'installed_by_another_member':other} if other else {})}
    if name=='list_trackers':
        if args.get('location_id'):context.require(args['location_id'])
        return {'trackers':_trackers(context,args.get('location_id'))[0]}
    rows,outputs,runs=_trackers(context)
    row=next((row for row in rows if row['id']==args['tracker_id'] and row.get('location_id')),None)
    if row is None:raise ValueError('Choose a tracker from list_trackers. A Tracking Agent opens with get_agent.')
    limit=args.get('rows',20);output=outputs.get(row['id'])
    if type(limit) is not int or not 1<=limit<=500:raise ValueError('Choose between 1 and 500 rows.')
    full=None
    if output:
        # A row of a recording is read with that recording: one outside the reader's sources stays out.
        seen={}
        def readable(entry):
            source=entry.get('source_id') if isinstance(entry,dict) else None
            if source is None:return True
            if source not in seen:seen[source]=context.source(source)
            return seen[source]
        tile,canvas=dict(output['tile']),dict(output['canvas'])
        tile['events']=[entry for entry in tile.get('events',[]) if readable(entry)][:limit]
        canvas['rows']=[entry for entry in canvas.get('rows',[]) if readable(entry)][:limit]
        full={'revision':output['revision'],'period':output.get('period'),'tile':tile,'canvas':canvas}
    recent=[_run_of(run,row['key']) for run in _run_rows(context,row['location_id'],runs.runs(context.owner,context.dataset,row['location_id'],limit=50))
        if any(part.get('key')==row['key'] for name_ in RUN_PARTS for part in run.get(name_) or [])][:10]
    return {**row,'output':full,'script':_script(context,row),'view':{'tile':row['layers']['tile'],'canvas':row['layers']['canvas']},'runs':recent}

def tracker_call(context,name,args):
    import tracker_runner as runner
    from core_trackers import LOCKED,add_tracker,install_core_trackers,installed_trackers,remove_tracker,update_tracker
    owner,dataset,location_id=context.owner,context.dataset,args['location_id']
    company=context.company()
    installed=(owner,dataset,location_id) in runner.installed_locations(context.state)
    if name=='install_core_trackers':
        if not context.organization:raise ValueError(ORGANIZATION_ONLY)
        # One member's installation runs a location. It stays theirs while a pass can act as them; once
        # their management of the location or their access to its cameras has ended, a member who
        # manages the location installs the trackers again and they run as that member.
        took_over=False
        for member,collection,place in runner.installed_locations(context.state):
            if (collection,place)!=(dataset,location_id) or member==owner:continue
            try:runner.pass_access(replace(company,owner=member),location_id)
            except ValueError:took_over=True
            else:raise ValueError(INSTALLED_BY_MEMBER)
        trackers=install_core_trackers(company,location_id,args['business_type'])
        core=[t for t in trackers if t['core']]
        return {'location_id':location_id,'business_type':args['business_type'],'recipe_revision':core[0]['recipe_revision'],
            'trackers':[{'id':t['id'],'key':t['key'],'label':t['label'],'core':t['core']} for t in trackers],
            **({'took_over':TOOK_OVER} if took_over else {})}
    company.locations.authorized(owner,dataset,location_id)
    if not installed:raise ValueError(INSTALL_FIRST)
    ledger=runner.TrackerRuns(context.state)
    schedule=lambda state,tracker=None:_schedule(state,tracker,service=ledger.service()['state'])
    if name=='run_trackers':
        known={t['key'] for t in installed_trackers(company,location_id)}
        if args.get('only') and not set(args['only'])<=known:raise ValueError('Choose trackers of this location.')
        state=ledger.request(owner,dataset,location_id,by=owner,only=args.get('only'));told=schedule(state)
        return {'location_id':location_id,'requested':True,'only':state['requested_only'],
            'starts':SERVICE_RUNS if told['service']=='on' else SERVICE_WAITS,'schedule':told}
    try:company.locations.authorized(owner,dataset,location_id,write=True)
    except ValueError:raise ValueError(MANAGEMENT) from None
    trackers={t['key']:t for t in installed_trackers(company,location_id)}
    public=lambda t:{k:t[k] for k in ('id','key','label','core','chat','locked','location_id','revision') if k in t}|{'observes':t['tracker']['observes'],'event_types':t['tracker']['event_types']}
    if name=='save_tracker':
        key,label,observes=args['key'],' '.join(args['label'].split()),' '.join(args['observes'].split())
        kinds=args.get('event_types')
        if (not re.fullmatch(TRACKER_KEY,key) or not 1<=len(label)<=80 or not 1<=len(observes)<=500
                or kinds is not None and (len(kinds)>24 or any(not isinstance(kind,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,59}',kind) for kind in kinds))):
            raise ValueError('Give the tracker a short key, a label and what it observes.')
        if key in trackers:
            prior=trackers[key]
            if prior['core']:raise ValueError(LOCKED)
            if args.get('expected_revision') is None:raise ValueError('Read the tracker again, then change it with its current revision.')
            return public(update_tracker(company,location_id,key,{'label':label,'tracker':{**prior['tracker'],'observes':observes,
                **({'event_types':list(kinds)} if kinds is not None else {})}},expected_revision=args['expected_revision']))
        if sum(1 for t in trackers.values() if not t['core'])>=MAX_OWN_TRACKERS:raise ValueError('This location holds its full set of own trackers. Remove one to add another.')
        return public(add_tracker(company,location_id,key,label,observes,event_types=kinds or ()))
    if name=='remove_tracker':
        removed=remove_tracker(company,location_id,args['key'])
        return {'removed':True,**public(removed)}
    # set_tracker_schedule
    runs=ledger;key=args.get('key')
    fields={k:args[k] for k in ('automatic','min_interval_seconds','budget_seconds','daily_at') if k in args}
    if fields.get('daily_at')=='':fields['daily_at']=None
    if key is not None:
        tracker=trackers.get(key)
        if tracker is None:raise ValueError('Choose a tracker of this location.')
        if tracker['core']:raise ValueError(LOCKED)
        if set(fields)!={'automatic'}:raise ValueError('A tracker’s own schedule is its automatic setting. The interval, the budget and the daily time belong to the location.')
        paused=set(runs.policy(owner,dataset,location_id)['paused'])
        paused=paused-{key} if fields['automatic'] else paused|{key}
        runs.set_policy(owner,dataset,location_id,{'paused':sorted(paused&set(trackers))},expected_revision=args['expected_revision'])
        return {'location_id':location_id,'key':key,'schedule':schedule(runs.state(owner,dataset,location_id),tracker)}
    if not context.organization:raise ValueError(ORGANIZATION_ONLY)
    if not fields:raise ValueError('Name the schedule fields to change.')
    runs.set_policy(owner,dataset,location_id,fields,expected_revision=args['expected_revision'])
    return {'location_id':location_id,'schedule':schedule(runs.state(owner,dataset,location_id))}

TRACKERS=SimpleNamespace(READ_NAMES=frozenset({'list_trackers','get_tracker','list_tracker_runs'}),
    WRITE_NAMES=frozenset({'save_tracker','remove_tracker','set_tracker_schedule','run_trackers','install_core_trackers'}),
    specs=tracker_specs,read=tracker_read,call=tracker_call, name="trackers", module="monitoring_tools")

# Every tool family beside the observation tools, in listing order. A family is a module, or a
# namespace like TRACKERS above, with five names:
#
#   READ_NAMES, WRITE_NAMES     frozensets of its tool names
#   specs(write)                its tool specifications; the write tools only when `write` is true
#   read(context, name, args)   a read, with a ReadContext: the same code on every surface
#   call(context, name, args)   a write, with a WriteContext: the hosted parent conversation only.
#                               The dispatcher has already refused a participant's turn, a child
#                               conversation, a stopped run and a conversation outside this business
#
# A family whose write waits on something (a sandbox job) may add two names: ASYNC_NAMES, a subset of
# its WRITE_NAMES, and `async acall(context, name, args)`. The hosted conversation awaits those
# through `call_async`, after the same checks.
#
# A read reaches the hosted chat, native MCP, public MCP and local MCP by being in READ_NAMES (local
# MCP also lists one function per read in mcp_server.py). A family module keeps its own imports of
# this module and of agent_bridge inside its functions, because both read FAMILIES while they load.
# Results stay free of principals and file paths; a Core Tracker key answers `core_trackers.LOCKED`.
#
# Three families join when their modules are present in the checkout; each keeps one module:
#   tracker_scripts        get_tracker_script; save_tracker_script, test_tracker_script, publish_tracker_script,
#                          set_tracker_script_policy, approve_tracker_output, write_tracker_text
#   tracker_presentation   save_tracker_view
#   harness_tools          get_harness
# A module that is present and fails to load stops the import, so a broken family is seen at start.
def present(*names):
    return tuple(import_module(name) for name in names if find_spec(name))

FAMILIES=(TRACKERS, import_module('tracker_scripts'), import_module('tracker_presentation'), import_module('harness_tools'))

def registry(families):
    """The read and the write names of every tool here, checked as the module loads.

    A name is one tool: a read or a write, of one family, beside the observation tools. A family
    lists each of its reads to every conversation and each of its writes to the parent conversation
    alone, once. A family that breaks this stops the import, so a name can never be dispatched as a
    read on the open surfaces while it writes, and never takes the place of a tool that exists.
    """
    from tool_families import validate_families
    return validate_families(families, reserved_reads=OBSERVATION_READS,
                             reserved_writes=OBSERVATION_WRITES)

READ_NAMES,WRITE_NAMES=registry(FAMILIES)
NAMES=READ_NAMES|WRITE_NAMES
# The writes the hosted conversation awaits on its event loop.
ASYNC_NAMES=frozenset(name for family in FAMILIES for name in getattr(family,'ASYNC_NAMES',()))


def observation_read(context, name, arguments):
    result = read(context.store, context.owner, context.dataset, name, arguments, context.agents)
    if getattr(context, 'agent', None) is not None:
        events = result['events'] if name == 'list_tracker_events' else [result] if name == 'get_tracker_event' else []
        admit_event_history(context.agent, context.chat_id, events)
    return result


def observation_call(context, name, arguments):
    return call(context.agent, context.chat_id, context.dataset, name, arguments,
                context.run, context.store.scope_view)


OBSERVATIONS = SimpleNamespace(READ_NAMES=OBSERVATION_READS, WRITE_NAMES=OBSERVATION_WRITES,
    specs=observation_specs, read=observation_read, call=observation_call,
    name='observations', module='monitoring_tools')
