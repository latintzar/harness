"""Durable monitoring contracts and ownership, without provider calls."""
from retail_agent_inputs import RetailInputProvider
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from monitoring import MonitoringStore

SOURCES = {'a': {'id':'a','video_id':'recording-a','dataset':'shop','start':0,'end':20},
           'b': {'id':'b','video_id':'recording-b','dataset':'shop','start':0,'end':20}}
def store(path, sources=None):
    return MonitoringStore(path, source_lookup=lambda owner, key: (SOURCES if sources is None else sources).get(key))
def profile(s, owner='alice'):
    return s.save_profile(owner, {'name':'Product interactions','dataset':'shop',
        'vocabulary':[{'type':'pick_up','description':'Item visibly lifted from its original place.'}]}, datasets=['shop'])
def event(p, source='a', **kw):
    return {'profile_id':p['id'],'profile_revision':p['revision'],'event_key':'e1',
        'type':'pick_up','description':'Picks up a blue packet.','source':{'clip_id':source,'start_s':2,'end_s':3},
        'track_id':'p1', **kw}
def test_profiles_version_and_owner_guard(tmp_path):
    s=store(tmp_path); p=profile(s)
    newer=s.save_profile('alice', {'name':'Pickup watch','dataset':'shop','vocabulary':p['vocabulary']},
        datasets=['shop'], profile_id=p['id'],expected_revision=1)
    assert newer['revision']==2
    assert store(tmp_path).get_profile('alice',p['id'],revision=1)['name']=='Product interactions'
    with pytest.raises(ValueError): s.get_profile('bob',p['id'])
    with pytest.raises(ValueError): s.save_profile('alice',{'name':'X','dataset':'shop','vocabulary':p['vocabulary']},datasets=['shop'],profile_id=p['id'],expected_revision=1)
    assert s.list_profiles('bob')==[]
def test_committed_events_deduplicate_and_keep_recording_scoped_visits(tmp_path):
    s=store(tmp_path);p=profile(s)
    a=s.commit_event('alice',event(p)); assert s.commit_event('alice',event(p))['id']==a['id']
    b=s.commit_event('alice',event(p,source='b'))
    assert a['visit_id'] != b['visit_id']
    assert a['source']['start_s']==2 and a['captured_at'] is None
    assert len(store(tmp_path).list_events('alice','shop'))==2
    with pytest.raises(ValueError): s.commit_event('alice',event(p,description='Different event payload'))
    assert s.list_events('bob','shop')==[]
def test_event_source_vocabulary_and_revision_validation(tmp_path):
    s=store(tmp_path);p=profile(s)
    for changes in ({'type':'theft'},{'profile_revision':99},{'source':{'clip_id':'missing','start_s':2,'end_s':3}},
        {'source':{'clip_id':'a','start_s':19,'end_s':22}}, {'bbox':[.8,.2,.2,.7]}):
        with pytest.raises(ValueError): s.commit_event('alice',event(p,**changes))
    with pytest.raises(ValueError): s.commit_event('bob',event(p))
def test_revocation_hides_source_backed_events(tmp_path):
    visible=dict(SOURCES); s=store(tmp_path,visible);p=profile(s);e=s.commit_event('alice',event(p))
    del visible['a']
    assert s.list_events('alice','shop')==[]
    with pytest.raises(ValueError): s.get_event('alice',e['id'])

def test_event_agent_schedule_has_no_live_camera_or_embedding_gate(tmp_path):
    import asyncio,time
    from background_agents import AgentScheduler
    from agent_test_support import retail_agent_store as AgentStore
    from workspace_capabilities import WorkspaceCapabilities
    class Index:
        clips=list(SOURCES.values())
        def status(self):return {'ready':False,'datasets':['shop']}
        def get_clip(self,key):return SOURCES.get(key)
    class Harness:
        def __init__(self):self.children={};self.child_tasks={};self.calls=[]
        def _save_child(self,c):self.children[c['id']]=c
        async def turn(self,message,chat_id,dataset,**kwargs):
            self.calls.append(message)
            self.children[chat_id]['messages'].append({'role':'assistant','content':'One observed blue packet pickup.'})
            yield {'type':'done','status':'completed'}
    async def scenario():
        s=store(tmp_path);p=profile(s); e=s.commit_event('alice',event(p))
        a=AgentStore(tmp_path,input_provider=RetailInputProvider(monitoring=s,index=Index()));h=Harness();index=Index()
        scheduler=AgentScheduler(a,h,asyncio.Semaphore(1))
        spec={'title':'Product summary','instruction':'Summarize observations.','dataset':'shop','agent_kind':'tracker',
            'input_kind':'tracker_events','mode':'ongoing','max_runs':3,'profile_ids':[p['id']]}
        agent=a.create(spec,['shop'],owner_principal_id='alice')
        capability=WorkspaceCapabilities(inputs=lambda:a.inputs)
        assert capability.execution(agent)['ready'] is True
        assert capability.execution({**agent,'input_kind':'camera_footage'})['ready'] is False
        a.activate(agent['id'],time.time()+1000)
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
        assert len(h.calls)==1 and 'blue packet' in h.calls[0] and e['id'] in h.calls[0]
        run=a.get(agent['id'])['last_run'];assert run['status']=='completed'
        assert a.run_report(agent['id'],run['id'])['observations'][0]['visit_id']==e['visit_id']
        assert h.children[agent['chat_id']]['agent_kind']=='tracker'
        assert a.claim(agent['id'],{e['id']:'a'},source_records=[e],now=time.time()+20) is None
    asyncio.run(scenario())

def test_http_profiles_promotion_and_incident_access(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        result=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        owner=result.json()['principal']['id'];ws=app.state.workspaces['demo']
        old={'id':'my-chat','dataset':'cafe','owner_principal_id':owner,'thread_id':'preserved-thread','messages':[{'role':'user','content':'Watch pickups.'}],'running':False}
        ws['agent'].children[old['id']]=old
        p=client.post('/api/monitoring/profiles',json={'name':'Pickup','dataset':'cafe','vocabulary':[{'type':'pick_up','description':'Visible pickup'}]},headers=HEADERS)
        assert p.status_code==200,p.text
        p=p.json()
        promoted=client.post('/api/chats/my-chat/promote-agent',json={'agent_kind':'tracker','definition':{'title':'Pickups','instruction':'Summarize pickups.','dataset':'cafe','input_kind':'tracker_events','mode':'scheduled','profile_ids':[p['id']]}},headers=HEADERS)
        assert promoted.status_code==200,promoted.text
        value=promoted.json();assert value['chat']['thread_id']=='preserved-thread'
        assert value['chat']['messages']==old['messages'] and value['chat']['agent_kind']=='tracker'
        key=value['agent']['id']
        # Phone chat is the Tracking Agent's default: policy on, the creator as participant with an
        # enabled binding, and the contact card in this same chat while their number is unverified.
        phone=ws['phone'].state(key)
        assert phone['policy']['participant_phone_delivery']=='on' and phone['chat_id']=='my-chat'
        assert [(row['member_id'],row['role'],row['binding']['enabled'],row['binding']['channel']) for row in phone['participants']]==[(owner,'creator',True,None)]
        assert phone['participants'][0]['contact_request']['status']=='open'
        assert value['chat']['messages'][0]=={'role':'user','content':'Watch pickups.'}
        assert [m['contact_request']['member_id'] for m in value['chat']['messages'] if m.get('contact_request')]==[owner]
        detail=client.get('/api/agents/'+key+'/observations').json()
        assert detail['summary'] is None and detail['incidents']==[]
        e=client.post('/api/monitoring/events',json={**event(p), 'source':{'clip_id':'demo-clip','start_s':0,'end_s':1}},headers=HEADERS)
        assert e.status_code==200,e.text
        detail=client.get('/api/agents/'+key+'/observations').json()
        assert detail['event_count']==1 and detail['incidents'][0]['id']==e.json()['id']
        assert client.get('/api/agents/'+key).json()['execution']['ready'] is True
        assert client.post('/api/agents/'+key+'/activate',json={'expected_revision':1},headers=HEADERS).status_code==200
        # A distinct member keeps separate observation/profile/agent authority.
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='another',display_name='Another')},headers=HEADERS)
        assert client.get('/api/monitoring/profiles').json()['profiles']==[]
        assert client.get('/api/monitoring/events?dataset=cafe').json()['events']==[]
        assert client.get('/api/agents/'+key+'/observations').status_code==404

def tracker_with_an_output(client,app,HEADERS,*,located=True,observed=False):
    """A Tracking Agent promoted from a chat; with `located`, its profile names a location of the company."""
    from business_scope import ScopeActor
    result=client.post('/api/session',json={'code':app.state.access.issue('demo',owner=True)},headers=HEADERS)
    owner=result.json()['principal']['id'];ws=app.state.workspaces['demo']
    scope=ws['business_scope'];place=None
    if located:place=scope.mutate(ScopeActor(scope.company_id,owner),'cafe','locations',{'name':'Shop'},0)['locations'][0]['id']
    ws['agent'].children['my-chat']={'id':'my-chat','dataset':'cafe','owner_principal_id':owner,'thread_id':'kept','messages':[{'role':'user','content':'Watch the freezer.'}],'running':False}
    p=client.post('/api/monitoring/profiles',json={'name':'Freezer','dataset':'cafe',**({'location_id':place} if place else {}),
        'vocabulary':[{'type':'door_open','description':'The freezer door stays open.'}]},headers=HEADERS)
    assert p.status_code==200,p.text
    promoted=client.post('/api/chats/my-chat/promote-agent',json={'agent_kind':'tracker','definition':{'title':'Freezer door','instruction':'Count open doors.',
        'dataset':'cafe','input_kind':'tracker_events','mode':'scheduled','profile_ids':[p.json()['id']]}},headers=HEADERS)
    assert promoted.status_code==200,promoted.text
    if observed:
        seen=client.post('/api/monitoring/events',json={'profile_id':p.json()['id'],'profile_revision':1,'event_key':'door-1','type':'door_open',
            'description':'The freezer door stays open.','source':{'clip_id':'demo-clip','start_s':0,'end_s':1}},headers=HEADERS)
        assert seen.status_code==200,seen.text
    return owner,ws,place,promoted.json()['agent']['id']

def publish_tracker_output(ws,owner,place,key,value=14,unit='times',**canvas):
    """One output of a company tracker, written as a processing script's run writes it."""
    from datetime import date
    from pathlib import Path as P
    import retail_records as records
    from core_trackers import common
    company=records.Company(P(ws['state']),owner,'cafe',ws['business_scope'],ws['monitoring'],ws['location_intelligence'])
    points=[{'t':f'2026-10-11T{hour:02d}:00:00Z','value':hour%4} for hour in range(24)]
    return common.publish(common.read_location(company,place),key,common.day_period(date(2026,10,11)),value=value,unit=unit,series=points,trend=points[:7],
        events=[],canvas={'rows':[],'total_rows':0,**canvas},summary='The door stayed open 14 times. [Open the order](https://example.test/pay)')

def test_the_observations_of_a_tracker_carry_its_presentation_and_output(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    from core_trackers import tracker_records
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        owner,ws,place,key=tracker_with_an_output(client,app,HEADERS)
        url='/api/agents/'+key
        # A Tracking Agent whose tracker has no record yet reads as it did: its events alone.
        detail=client.get(url+'/observations').json()
        assert 'presentation' not in detail and 'output' not in detail and detail['incidents']==[]
        # A first save the kit refuses answers one sentence and leaves the agent without a record: a wrong name, a wrong type, a stale revision.
        for body,status,sentence in (({'expected_revision':0,'canvas':{'family':'heat'}},400,'A canvas has a family and its blocks.'),
                ({'expected_revision':0,'canvas':{'family':[]}},400,'A canvas has a family and its blocks.'),({'expected_revision':0,'canvas':{'family':{}}},400,'A canvas has a family and its blocks.'),
                ({'expected_revision':0,'tile':{'label':'Freezer','mark':[],'format':'count'}},400,'Choose the tile mark from the kit: line, bars, ring, arc.'),
                ({'expected_revision':4,'canvas':{'family':'live'}},409,'This tracker’s presentation changed while the canvas was open. Pick the canvas again.')):
            answer=client.post(url+'/presentation',json=body,headers=HEADERS)
            assert (answer.status_code,answer.json()['detail'])==(status,sentence),body
            assert tracker_records(ws['state'],owner,'cafe')==[] and 'presentation' not in client.get(url+'/observations').json()
        # The person's first save gives the agent its tracker record at the location its profile names.
        saved=client.post(url+'/presentation',json={'expected_revision':0,'canvas':{'family':'metric'}},headers=HEADERS)
        assert saved.status_code==200,saved.text
        assert saved.json()['presentation']['revision']==1 and saved.json()['presentation']['saved_by']=='person' and saved.json()['page_updated'] is False
        tracker,=tracker_records(ws['state'],owner,'cafe')
        assert (tracker['agent_id'],tracker['location_id'],tracker['core'],saved.json()['tracker_id'])==(key,place,False,tracker['id'])
        detail=client.get(url+'/observations').json()
        assert detail['presentation']['canvas']['family']=='metric' and detail['presentation']['tile']['label']=='Freezer door' and 'output' not in detail
        # Its first output arrives: the canvas reads the tile row and the canvas of that one revision.
        publish_tracker_output(ws,owner,place,tracker['key'],ratio=.5)
        detail=client.get(url+'/observations').json()
        row,canvas=detail['output']['tile'],detail['output']['canvas']
        assert detail['output']['revision']==row['revision']==canvas['revision']==1 and row['value']==canvas['value']==14
        assert (row['label'],row['kind'],row['mark'],row['unit'],row['agent_id'],row['presentation_revision'])==('Freezer door','count','line','times',key,1)
        assert canvas['presentation']['family']=='metric' and [block['block'] for block in canvas['presentation']['blocks']]==['figure','period','line','summary','instances']
        assert canvas['problems']==[] and {family['id']:family['available'] for family in detail['families']}=={'metric':True,'live':True,'occurrences':True,'state':False,'ranking':False}
        assert canvas['summary']==row['summary']=='The door stayed open 14 times. Open the order', 'text a script wrote reaches the page with its link inert'
        assert owner not in str(detail['output']) and str(ws['state']) not in str(detail)
        # The harness's tool reads the same presentation the person saved.
        import monitoring_tools
        read=monitoring_tools.read(ws['monitoring'],owner,'cafe','get_tracker_presentation',{'tracker_id':key})
        assert read['presentation']==detail['presentation'] and read['output']['revision']==1 and read['kit']['schema']=='rocoto.tracker-presentation-kit.v1'
        # A change the output cannot fill answers one sentence and leaves the saved one in place.
        # A person reads the sentence the canvas control shows for that family; the harness's tool names the binding.
        refused=client.post(url+'/presentation',json={'expected_revision':1,'canvas':{'family':'state'}},headers=HEADERS)
        assert (refused.status_code,refused.json()['detail'])==(400,'Needs shelf states.')
        # A presentation saved in between answers 409 in a person's words: the page reads it and sends the pick once more.
        stale=client.post(url+'/presentation',json={'expected_revision':0,'canvas':{'family':'live'}},headers=HEADERS)
        assert (stale.status_code,stale.json()['detail'])==(409,'This tracker’s presentation changed while the canvas was open. Pick the canvas again.')
        for wrong in ([],{},['metric']):
            answer=client.post(url+'/presentation',json={'expected_revision':1,'canvas':{'family':wrong}},headers=HEADERS)
            assert (answer.status_code,answer.json()['detail'])==(400,'A canvas has a family and its blocks.'),wrong
        assert client.post(url+'/presentation',json={'expected_revision':1,'style':{}},headers=HEADERS).status_code==422
        assert client.get(url+'/observations').json()['presentation']==detail['presentation']
        # Another member reads neither the observations nor the presentation, and saves nothing.
        client.post('/api/session',json={'code':app.state.access.issue('demo',principal_id='another',display_name='Another')},headers=HEADERS)
        assert client.get(url+'/observations').status_code==404
        assert client.post(url+'/presentation',json={'expected_revision':1,'canvas':{'family':'live'}},headers=HEADERS).status_code==404

def test_a_canvas_pick_is_saved_with_the_tracker_and_leaves_the_agent_running(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        owner,ws,place,key=tracker_with_an_output(client,app,HEADERS,observed=True)
        url='/api/agents/'+key
        activated=client.post(url+'/activate',json={'expected_revision':1},headers=HEADERS)
        assert activated.status_code==200,activated.text
        before=ws['agents'].get(key)
        assert before['status']=='active'
        watched=lambda record:{k:record.get(k) for k in ('status','revision','runs_started','active_until','title','instruction','mode','updated_at')}
        for revision,family in enumerate(['live','metric','occurrences']):
            saved=client.post(url+'/presentation',json={'expected_revision':revision,'canvas':{'family':family}},headers=HEADERS)
            assert saved.status_code==200,saved.text
            assert saved.json()['presentation']['canvas']['family']==family
            after=ws['agents'].get(key)
            assert watched(after)==watched(before), 'a saved presentation leaves the agent as it runs'
        assert client.get(url).json()['status']=='active' and client.get(url+'/observations').json()['presentation']['revision']==3
        # The tile part is saved the same way.
        tile=client.post(url+'/presentation',json={'expected_revision':3,'tile':{'label':'Freezer door left open','mark':'bars','format':'count','unit':'times'}},headers=HEADERS)
        assert tile.status_code==200 and tile.json()['presentation']['tile']['mark']=='bars' and watched(ws['agents'].get(key))==watched(before)
        # An Action Agent has no tile; its id answers one sentence.
        ws['agent'].children['other']={'id':'other','dataset':'cafe','owner_principal_id':owner,'messages':[{'role':'user','content':'Message the manager.'}],'running':False}
        action=client.post('/api/chats/other/promote-agent',json={'agent_kind':'action','definition':{'title':'Manager','instruction':'Message the manager.','dataset':'cafe','mode':'ad_hoc'}},headers=HEADERS)
        assert action.status_code==200,action.text
        answer=client.post('/api/agents/'+action.json()['agent']['id']+'/presentation',json={'canvas':{'family':'live'}},headers=HEADERS)
        assert answer.status_code==400 and answer.json()['detail']=='Choose a Tracking Agent.'

def test_a_tracker_whose_location_is_open_answers_one_sentence(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    from business_scope import ScopeActor
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        owner,ws,place,key=tracker_with_an_output(client,app,HEADERS,located=False)
        # The harness's read says the same of a Tracking Agent ahead of its tracker record.
        import monitoring_tools
        with pytest.raises(ValueError,match='A Tracking Agent takes a presentation once its tracker belongs to a location.'):
            monitoring_tools.read(ws['monitoring'],owner,'cafe','get_tracker_presentation',{'tracker_id':key},agents=ws['agents'])
        answer=client.post('/api/agents/'+key+'/presentation',json={'expected_revision':0,'canvas':{'family':'live'}},headers=HEADERS)
        assert (answer.status_code,answer.json()['detail'])==(409,'The canvas is saved with this tracker once it belongs to a location. Its chat sets that up.')
        # A company with one location: the tracker belongs there.
        scope=ws['business_scope'];scope.mutate(ScopeActor(scope.company_id,owner),'cafe','locations',{'name':'Shop'},0)
        answer=client.post('/api/agents/'+key+'/presentation',json={'expected_revision':0,'canvas':{'family':'live'}},headers=HEADERS)
        assert answer.status_code==200,answer.text

def test_native_tools_use_same_store_and_preserve_current_chat(tmp_path):
    import asyncio,json
    from agent_bridge import CodexVideoAgent,tool_specs
    from agent_test_support import retail_agent_store as AgentStore
    class Index:
        clips=list(SOURCES.values())
        def status(self):return {'ready':False,'datasets':['shop']}
        def get_clip(self,key):return SOURCES.get(key)
    async def scenario():
        agent=CodexVideoAgent(tmp_path,Index(),lambda:[])
        agent.monitoring_store=store(tmp_path)
        from tool_test_support import retail_agent
        retail_agent(agent)
        agent.automation_store=AgentStore(tmp_path,input_provider=RetailInputProvider(monitoring=agent.monitoring_store))
        agent.children['chat']={'id':'chat','dataset':'shop','owner_principal_id':'alice','messages':[{'role':'user','content':'Track pickups.'}],'thread_id':'native-kept'}
        agent.active['chat']={'cancel':asyncio.Event()}
        async def call(name,args):
            blocks,_,ok=await agent._tool(name,args,'shop','chat')
            assert ok,blocks
            return json.loads(blocks[0]['text'])
        p=await call('save_monitoring_profile',{'name':'Pickups','vocabulary':[{'type':'pick_up','description':'Visible pickup'}]})
        e=await call('record_tracker_event',event(p))
        result=await call('list_tracker_events',{})
        assert result['events'][0]['id']==e['id']
        promoted=await call('promote_chat_to_agent',{'title':'Pickup tracker','instruction':'Summarize pickups.','agent_kind':'tracker','input_kind':'tracker_events','profile_ids':[p['id']]})
        assert promoted['chat_id']=='chat'
        assert agent.children['chat']['thread_id']=='native-kept'
        assert agent.children['chat']['messages'][0]['content']=='Track pickups.'
        assert agent.children['chat']['agent_kind']=='tracker'
        agent.children['child']={'id':'child','parent_id':'chat','dataset':'shop','owner_principal_id':'alice'}
        agent.active['child']={'cancel':asyncio.Event()}
        assert not (await agent._tool('record_tracker_event',event(p),'shop','child'))[2]
        assert not {'save_monitoring_profile','record_tracker_event','promote_chat_to_agent'} & {s['name'] for s in tool_specs(False)}
    asyncio.run(scenario())

def test_host_event_emission_uses_existing_durable_wake_queue(tmp_path,monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=session['principal']['id'];ws=app.state.workspaces['demo']
        p=ws['monitoring'].save_profile(owner,{'name':'Pickups','dataset':'cafe','vocabulary':[{'type':'pick_up','description':'Visible pickup'}]},datasets=['cafe'])
        agent=ws['agents'].create({'title':'Notify manager','instruction':'Review pickup evidence.','dataset':'cafe','agent_kind':'action','input_kind':'tracker_events','mode':'ongoing','max_runs':3,'profile_ids':[p['id']]},['cafe'],owner_principal_id=owner)
        # An Action Agent holds none of the participant phone records.
        from phone_chat import PhoneChatError
        with pytest.raises(PhoneChatError,match='Choose a Tracking Agent'):ws['phone'].state(agent['id'])
        ws['agents'].activate(agent['id'],time.time()+1000)
        item={**event(p),'source':{'clip_id':'demo-clip','start_s':0,'end_s':1}}
        saved=ws['monitoring'].commit_event(owner,item)
        wake=ws['agents'].wakes.pending(agent['id'],owner=owner)
        assert wake['provider']=='monitoring' and wake['event_id']==saved['id']
        assert ws['monitoring'].commit_event(owner,item)['id']==saved['id']
        assert ws['agents'].wakes.pending(agent['id'],owner=owner)['id']==wake['id']
        ws['agents'].pause(agent['id'])
        assert ws['agents'].wakes.pending(agent['id'],owner=owner) is None

def test_registered_recording_needs_no_embedding_and_retains_immutable_receipt(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=session['principal']['id'];ws=app.state.workspaces['demo']
        ws['index'].clips=[]
        ws['index'].status=lambda:{'ready':False,'datasets':[]}
        descriptor={'id':'recorded-one','video_id':'original-one','dataset':'shop','end':120,
            'sha256':'a'*64,'media_url':'/api/control-room/media/one','evidence_url':'/?home=1&clip=recorded-one'}
        s=ws['monitoring'];source=s.register_source(owner,descriptor)
        assert s.register_source(owner,descriptor)==source
        with pytest.raises(ValueError):s.register_source(owner,{**descriptor,'sha256':'b'*64})
        assert s.registered_source('another','recorded-one') is None
        p=client.post('/api/monitoring/profiles',json={'name':'Pickups','dataset':'shop','vocabulary':[{'type':'pick_up','description':'Visible pickup'}]},headers=HEADERS)
        assert p.status_code==200,p.text
        e=client.post('/api/monitoring/events',json=event(p.json(),source='recorded-one'),headers=HEADERS)
        assert e.status_code==200,e.text
        assert e.json()['source_sha256']=='a'*64 and e.json()['media_url']==descriptor['media_url']
        assert e.json()['evidence_url']==descriptor['evidence_url'] and e.json()['thumbnail_url'] is None
        agent=client.post('/api/agents',json={'title':'Pickup summary','instruction':'Summarize pickups.','dataset':'shop','agent_kind':'tracker','input_kind':'tracker_events','mode':'scheduled'},headers=HEADERS)
        assert agent.status_code==200,agent.text
        assert agent.json()['execution']['ready'] is True
        capabilities=client.get('/api/workspace/capabilities?dataset=shop')
        assert capabilities.status_code==200,capabilities.text
        from monitoring import resolve_monitoring_source
        assert resolve_monitoring_source(ws,owner,'recorded-one')['video_id']=='original-one'
        assert resolve_monitoring_source(ws,'another','recorded-one') is None

def test_registered_recording_respects_current_camera_and_chat_grants(tmp_path):
    from monitoring import resolve_monitoring_source
    class Index:
        def get_clip(self,key):return None
    class Scope:
        company_id='company'
        allowed=['original-one']
        def effective(self,actor,dataset):
            return {'configured':True,'can_query':True,'includes_unassigned':False,'source_ids':self.allowed}
        def chat_scope(self,actor,chat,dataset):
            return {**self.effective(actor,dataset),'source_ids':[] if chat=='other-room' else self.allowed}
    s=MonitoringStore(tmp_path);s.register_source('alice',{'id':'one','video_id':'original-one','dataset':'shop','end':20,'sha256':'a'*64,'media_url':'/api/media/one'})
    scope=Scope();selected={'monitoring':s,'index':Index(),'business_scope':scope}
    assert resolve_monitoring_source(selected,'alice','one')
    assert resolve_monitoring_source(selected,'alice','one',chat_id='other-room') is None
    scope.allowed=[]
    assert resolve_monitoring_source(selected,'alice','one') is None

def test_schedule_edit_keeps_saved_tracker_input_and_profile(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        p=client.post('/api/monitoring/profiles',json={'name':'Pickups','dataset':'cafe','vocabulary':[{'type':'pick_up','description':'Visible pickup'}]},headers=HEADERS).json()
        spec={'title':'Pickup summary','instruction':'Summarize pickups.','dataset':'cafe','agent_kind':'tracker','input_kind':'tracker_events','profile_ids':[p['id']],'mode':'scheduled'}
        created=client.post('/api/agents',json=spec,headers=HEADERS).json()
        # Existing clients send the schedule's original fields only.
        update={k:v for k,v in spec.items() if k not in {'agent_kind','input_kind','profile_ids'}}
        changed=client.post('/api/agents/'+created['id']+'/update',json={**update,'interval_seconds':7200,'expected_revision':1},headers=HEADERS)
        assert changed.status_code==200,changed.text
        record=changed.json()
        assert (record['agent_kind'],record['input_kind'],record['profile_ids'])==('tracker','tracker_events',[p['id']])
        assert record['interval_seconds']==7200 and record['fresh_inspections_per_run']==0


def test_reference_events_require_explicit_evaluation_and_never_emit_production_callbacks(tmp_path):
    from monitoring_tools import read
    s=store(tmp_path);p=profile(s);emitted=[];s.event_sink=emitted.append
    reference=s.commit_event('alice',event(p,event_key='publisher'),origin='dataset_reference')
    assert s.commit_event('alice',event(p,event_key='publisher'),origin='dataset_reference')==reference
    assert emitted==[] and s.list_events('alice','shop')==[]
    definition={'owner_principal_id':'alice','dataset':'shop','profile_ids':[p['id']]}
    assert s.events_for(definition)==[] and not s.readiness(definition)
    assert read(s,'alice','shop','list_tracker_events',{})['events']==[]
    assert read(s,'alice','shop','list_tracker_events',{'evaluation':True})['events']==[reference]
    with pytest.raises(ValueError):read(s,'alice','shop','get_tracker_event',{'event_id':reference['id']})
    assert read(s,'alice','shop','get_tracker_event',{'event_id':reference['id'],'evaluation':True})==reference
    primary=s.commit_event('alice',event(p,event_key='actual'))
    assert emitted==[primary] and s.events_for(definition)==[primary]


@pytest.mark.parametrize('kind',['tracker','action'])
def test_reference_wakes_and_legacy_reference_claims_cannot_start_agents(tmp_path,kind):
    import json,time
    from agent_test_support import retail_agent_store as AgentStore
    s=store(tmp_path);p=profile(s);agents=AgentStore(tmp_path,input_provider=RetailInputProvider(monitoring=s))
    definition=agents.create({'title':'Watch pickups','instruction':'Review actual observations.','dataset':'shop',
        'agent_kind':kind,'input_kind':'tracker_events','mode':'ongoing','max_runs':3,'profile_ids':[p['id']]},['shop'],owner_principal_id='alice')
    agents.activate(definition['id'],time.time()+1000)
    reference=s.commit_event('alice',event(p,event_key='reference'),origin='dataset_reference')
    def enqueue(e):return agents.wakes.enqueue(definition['id'],owner='alice',expected_revision=1,
        provider='monitoring',connection_id=p['id'],event_id=e['id'],text='Review this observation.')
    with pytest.raises(ValueError):enqueue(reference)
    assert agents.wakes.pending(definition['id'],owner='alice') is None
    assert agents.claim(definition['id'],{reference['id']:'a'},source_records=[reference]) is None
    assert agents.get(definition['id'])['runs_started']==0
    primary=s.commit_event('alice',event(p,event_key='old-import'))
    wake=enqueue(primary)
    # Legacy imports may carry the older publisher marker instead of origin.
    legacy={**primary,'review_state':'publisher_annotation'}
    with s.connect() as db:db.execute('UPDATE monitoring_events SET body=? WHERE id=?',(json.dumps(legacy),primary['id']))
    assert s.list_events('alice','shop')==[]
    assert agents.claim(definition['id'],{primary['id']:'a'},source_records=[primary],wake_id=wake['id']) is None
    assert agents.wakes.get(wake['id'],owner='alice')['reason']=='observation_ineligible'
    assert agents.get(definition['id'])['runs_started']==0
    assert s.list_events('alice','shop',include_reference=True)==[legacy,reference]


def test_monitoring_http_keeps_reference_labels_behind_explicit_evaluation(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app,HEADERS
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=session['principal']['id'];ws=app.state.workspaces['demo'];s=ws['monitoring']
        p=s.save_profile(owner,{'name':'Publisher evaluation','dataset':'cafe','vocabulary':[{'type':'pick_up','description':'Reference pickup'}]},datasets=['cafe'])
        saved=s.commit_event(owner,{**event(p),'source':{'clip_id':'demo-clip','start_s':0,'end_s':1}},origin='dataset_reference')
        assert client.get('/api/monitoring/events?dataset=cafe').json()['events']==[]
        assert client.get('/api/monitoring/events?dataset=cafe&evaluation=true').json()['events'][0]['id']==saved['id']
        assert client.get('/api/monitoring/events/'+saved['id']).status_code==404
        assert client.get('/api/monitoring/events/'+saved['id']+'?evaluation=true').status_code==200

def test_a_tracker_alert_reaches_its_participants_phone_beside_the_wake_and_the_replay_lane_stays_silent(tmp_path,monkeypatch):
    """The monitoring event sink is the prototype's processed-alert hook (CHANNELS.md, "Host alert delivery")."""
    import time
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from messaging_provider import Template
    from test_skill_runtime import make_app,HEADERS
    from test_organization_chats import commissioned
    from test_phone_participants import verify_over_http
    app=make_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
        owner=session['principal']['id'];ws=app.state.workspaces['demo']
        _,service,provider,sent,_=commissioned(app,tmp_path)
        provider.config=replace(provider.config,utility_templates=(Template('rocoto_monitoring_alert_v1','en_US','UTILITY','APPROVED','Alert from {{1}}: {{2}} Open {{3}}'),))
        service.spend=lambda:{'company_daily_messages':20,'agent_daily_messages':10,'stopped':False}
        delivery=ws['phone_delivery'];delivery.origin='https://rocoto.example'
        async def asleep():return None
        ws['scheduler'].tick=asleep  # this test admits no model turn: the agent stays asleep and its wake stays queued
        verify_over_http(client,sent,owner,'+34611111111')
        p=ws['monitoring'].save_profile(owner,{'name':'Queue','dataset':'cafe','vocabulary':[{'type':'queue_forms','description':'A queue forms at a till.'}]},datasets=['cafe'])
        spec={'instruction':'Report queue events.','dataset':'cafe','input_kind':'tracker_events','mode':'ongoing','max_runs':3,'profile_ids':[p['id']]}
        def observation(key):
            return {'profile_id':p['id'],'profile_revision':1,'event_key':key,'type':'queue_forms',
                'description':'Six people are waiting at checkout two.','source':{'clip_id':'demo-clip','start_s':0,'end_s':1}}
        tracker=ws['agents'].create({**spec,'title':'Queue watch','agent_kind':'tracker'},['cafe'],owner_principal_id=owner)
        action=ws['agents'].create({**spec,'title':'Notify manager','agent_kind':'action'},['cafe'],owner_principal_id=owner)
        chat=ws['scheduler'].ensure_conversation(tracker)
        # Committed before this agent's phone records exist: read again later, it reaches zero phones.
        delivery.fan_out=lambda *args,**kwargs:[]  # the host reads it for the first time below
        early=ws['monitoring'].commit_event(owner,observation('before-listening'))
        del delivery.fan_out
        time.sleep(.01)
        assert ws['phone'].state(tracker['id'])['participants'][0]['binding']['channel']=='whatsapp'
        def window(stream,kind,key):
            return {'stream_id':stream,'source_id':'demo-clip','profile_id':p['id'],'profile_revision':1,'start_s':0,'end_s':1,
                'input_kind':kind,'observations':[observation(key)],'model':'fixture','sampling':'fixture','processing_seconds':0}
        def wait(check):
            deadline=time.monotonic()+6
            while time.monotonic()<deadline and not check():time.sleep(.05)
            assert check()
        for agent in (tracker,action):ws['agents'].activate(agent['id'],time.time()+1000)
        assert ws['monitoring'].commit_event(owner,observation('before-listening'))['id']==early['id']
        time.sleep(1.2)  # the catch-up pass reads it too
        assert delivery.deliveries(tracker['id'])==[]
        saved=ws['monitoring'].commit_event(owner,observation('queue-1'))
        link='https://rocoto.example/?chat='+tracker['chat_id']+'&alert='+saved['id']
        written=delivery.deliveries(tracker['id'])
        assert [(row['member_id'],row['alert_id'],row['alert_revision'],row['text'],row['link']) for row in written]==[
            (owner,saved['id'],1,'Queue watch: Six people are waiting at checkout two.',link)]
        # Delivery is host work beside the wake: the wake is queued for the sleeping agent and the phone alert leaves.
        assert ws['agents'].wakes.pending(tracker['id'],owner=owner)['provider']=='monitoring'
        wait(lambda:delivery.deliveries(tracker['id'])[0]['status']=='sent')
        assert sent[-1]['to']=='34611111111' and [value['text'] for value in sent[-1]['template']['components'][0]['parameters']]==[
            'Queue watch','Six people are waiting at checkout two.',link]
        assert ws['agents'].get(tracker['id'])['runs']==[] and ws['agents'].wakes.pending(tracker['id'],owner=owner) is not None
        def entries():return [m for m in ws['agent'].children[chat['id']]['messages'] if m.get('alert')]
        wait(lambda:[[item['status'] for item in entry['channel_deliveries']] for entry in entries()]==[['sent']])
        assert entries()[0]['alert']=={'id':saved['id'],'revision':1} and entries()[0]['content']==written[0]['text']
        assert entries()[0]['channel_deliveries'][0]['id']==written[0]['id'] and '+34611111111' not in str(entries())
        # The replayed event reuses its delivery; an Action Agent with the same subscription holds none.
        count=len(sent)
        assert ws['monitoring'].commit_event(owner,observation('queue-1'))['id']==saved['id']
        assert [row['id'] for row in delivery.deliveries(tracker['id'])]==[written[0]['id']] and delivery.deliveries(action['id'])==[]
        # The compiled-data replay lane writes zero deliveries; the same window kind arriving live is an alert.
        replayed=ws['stream_ingestion'].accept(owner,window('replay-1','dataset_replay','replayed'))
        assert replayed['input_kind']=='dataset_replay' and delivery.deliveries(tracker['id'],alert_id=replayed['event_ids'][0])==[]
        live=ws['stream_ingestion'].accept(owner,window('live-1','live','observed-live'))
        assert [row['member_id'] for row in delivery.deliveries(tracker['id'],alert_id=live['event_ids'][0])]==[owner]
        wait(lambda:delivery.deliveries(tracker['id'],alert_id=live['event_ids'][0])[0]['status']=='sent')
        time.sleep(1.2)
        assert len(sent)==count+1 and [row['alert_id'] for row in delivery.deliveries(tracker['id'])]==[saved['id'],live['event_ids'][0]]
        # A paused delivery policy leaves the alert in the app and the wake as it is.
        ws['phone'].set_delivery(tracker['id'],'paused',by=owner)
        paused=ws['monitoring'].commit_event(owner,{**observation('queue-2'),'description':'Two people are waiting.'})
        assert delivery.deliveries(tracker['id'],alert_id=paused['id'])==[]
        time.sleep(1.2)
        assert len(sent)==count+1 and len(ws['monitoring'].list_events(owner,'cafe'))==5


class SecondPerson:
    """Another signed-in person on the SAME client. A second TestClient would run the application on a second
    event loop, and the host's shared tasks belong to the first."""
    def __init__(self,client,headers,code):
        mine=dict(client.cookies)
        client.cookies.clear()  # sign in as a new browser would; the first person's session stays theirs
        assert client.post('/api/session',json={'code':code},headers=headers).status_code==200
        self.client,self.cookies=client,dict(client.cookies)
        self._swap(mine)
    def _swap(self,cookies):
        self.client.cookies.clear()
        for name,value in cookies.items():self.client.cookies.set(name,value)
    def _call(self,method,*args,**kwargs):
        mine=dict(self.client.cookies)
        self._swap(self.cookies)
        try:return getattr(self.client,method)(*args,**kwargs)
        finally:self._swap(mine)
    def post(self,*args,**kwargs):return self._call('post',*args,**kwargs)
    def get(self,*args,**kwargs):return self._call('get',*args,**kwargs)


def tracker_app(tmp_path,monkeypatch):
    """The real application with WhatsApp commissioned on a mock transport, an open spend record and one profile."""
    import time
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from messaging_provider import Template
    from test_skill_runtime import make_app,HEADERS
    from test_organization_chats import commissioned
    app=make_app(tmp_path,monkeypatch)
    client=TestClient(app);client.__enter__()
    session=client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS).json()
    owner=session['principal']['id'];ws=app.state.workspaces['demo']
    _,service,provider,sent,_=commissioned(app,tmp_path)
    provider.config=replace(provider.config,utility_templates=(Template('rocoto_monitoring_alert_v1','en_US','UTILITY','APPROVED','Alert from {{1}}: {{2}} Open {{3}}'),))
    ws['phone_delivery'].origin='https://rocoto.example'
    profile=ws['monitoring'].save_profile(owner,{'name':'Queue','dataset':'cafe','vocabulary':[{'type':'queue_forms','description':'A queue forms at a till.'}]},datasets=['cafe'])
    def observation(key,description='Six people are waiting at checkout two.'):
        return {'profile_id':profile['id'],'profile_revision':1,'event_key':key,'type':'queue_forms',
            'description':description,'source':{'clip_id':'demo-clip','start_s':0,'end_s':1}}
    def window(stream,kind,key):
        return {'stream_id':stream,'source_id':'demo-clip','profile_id':profile['id'],'profile_revision':1,'start_s':0,'end_s':1,
            'input_kind':kind,'observations':[observation(key)],'model':'fixture','sampling':'fixture','processing_seconds':0}
    def tracker(**definition):
        saved=ws['agents'].create({'instruction':'Report queue events.','dataset':'cafe','input_kind':'tracker_events',
            'profile_ids':[profile['id']],'title':'Queue watch','agent_kind':'tracker',**definition},['cafe'],owner_principal_id=owner)
        ws['scheduler'].ensure_conversation(saved)
        ws['phone'].state(saved['id'])
        return saved
    def wait(check,seconds=8):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline and not check():time.sleep(.05)
        return check()
    from types import SimpleNamespace
    return SimpleNamespace(app=app,client=client,owner=owner,ws=ws,service=service,provider=provider,sent=sent,profile=profile,
        observation=observation,window=window,tracker=tracker,wait=wait,delivery=ws['phone_delivery'],headers=HEADERS,
        states=lambda key:[(row['member_id'],row['status'],row['held_reason']) for row in ws['phone_delivery'].deliveries(key)])


@pytest.mark.parametrize('state',['saved_from_home','activated','paused','completed','grant_ended'])
def test_phone_alerts_follow_the_agents_phone_policy_whatever_its_mode_and_activation(tmp_path,monkeypatch,state):
    """Phone chat is a default capability of every Tracking Agent, and delivery is its own control (CHANNELS.md,
    "Delivery leaves the agent's run as it is"): the agent can be a draft, paused, finished or past its run grant."""
    import time
    from test_phone_participants import verify_over_http
    t=tracker_app(tmp_path,monkeypatch)
    try:
        async def asleep():return None
        t.ws['scheduler'].tick=asleep  # no model turn belongs to this rule
        verify_over_http(t.client,t.sent,t.owner,'+34611111111')
        # The Home promotion saves an ad hoc tracker: web/control-room.js posts mode 'ad_hoc'.
        saved=t.tracker(mode='ad_hoc') if state=='saved_from_home' else t.tracker(mode='ongoing',max_runs=3)
        if state!='saved_from_home':t.ws['agents'].activate(saved['id'],time.time()+(.3 if state=='grant_ended' else 1000))
        if state=='paused':t.ws['agents'].pause(saved['id'])
        if state=='completed':
            with t.ws['agents'].connect() as db:  # its run allowance is used up
                record=t.ws['agents']._read(db,'agents',saved['id']);record.update(status='completed',next_run_at=None)
                t.ws['agents']._save(db,'agents',record)
        if state=='grant_ended':time.sleep(.6)
        status=t.ws['agents'].get(saved['id'])['status']
        assert status=={'saved_from_home':'draft','activated':'active','paused':'paused','completed':'completed','grant_ended':'active'}[state]
        assert t.client.get(f"/api/agents/{saved['id']}/phone").json()['delivery']=='on'
        count=len(t.sent)
        event=t.ws['monitoring'].commit_event(t.owner,t.observation('queue-1'))
        assert t.wait(lambda:t.states(saved['id'])==[(t.owner,'sent',None)]),t.states(saved['id'])
        assert len(t.sent)==count+1 and t.sent[-1]['to']=='34611111111'
        assert t.delivery.deliveries(saved['id'])[0]['alert_id']==event['id']
        # The run grant is its own control: a wake is queued for an active grant alone, and delivery started no run.
        with t.ws['agents'].connect() as db:
            wakes=db.execute('SELECT count(*) FROM agent_wakes WHERE agent_id=?',(saved['id'],)).fetchone()[0]
        assert wakes==(1 if state=='activated' else 0) and t.ws['agents'].get(saved['id'])['runs']==[]
        # Its phone policy is the control that stops it.
        t.ws['phone'].set_delivery(saved['id'],'paused',by=t.owner)
        t.ws['monitoring'].commit_event(t.owner,t.observation('queue-2','Two people are waiting.'))
        time.sleep(1.2)
        assert len(t.sent)==count+1 and len(t.states(saved['id']))==1
    finally:
        t.client.__exit__(None,None,None)


def test_an_alert_whose_first_fan_out_failed_is_read_again_by_the_next_pass(tmp_path,monkeypatch):
    """The event commits first and its fan-out follows; a store that was busy then leaves the alert for the catch-up pass."""
    import sqlite3,time
    from test_phone_participants import verify_over_http
    t=tracker_app(tmp_path,monkeypatch)
    try:
        async def asleep():return None
        t.ws['scheduler'].tick=asleep
        verify_over_http(t.client,t.sent,t.owner,'+34611111111')
        saved=t.tracker(mode='ongoing',max_runs=3)
        real,calls=t.delivery.fan_out,[]
        def busy_once(*args,**kwargs):
            calls.append(kwargs['alert_id'])
            if len(calls)==1:raise sqlite3.OperationalError('database is locked')  # the messaging store was busy past its five seconds
            return real(*args,**kwargs)
        t.delivery.fan_out=busy_once
        count=len(t.sent)
        event=t.ws['monitoring'].commit_event(t.owner,t.observation('queue-1'))
        assert len(t.ws['monitoring'].list_events(t.owner,'cafe'))==1  # the commit itself succeeded
        assert t.wait(lambda:t.states(saved['id'])==[(t.owner,'sent',None)]),(calls,t.states(saved['id']))
        time.sleep(2.2)  # two more passes: the receipt keeps them from reading it again
        assert calls==[event['id'],event['id']] and len(t.sent)==count+1
        block=t.client.get(f"/api/agents/{saved['id']}/phone").json()
        assert block['participants'][0]['last_delivery']['status']=='sent'
    finally:
        t.client.__exit__(None,None,None)


def test_the_replay_lane_reaches_zero_phones_also_through_the_turn_it_wakes(tmp_path,monkeypatch):
    """CHANNELS.md "Zero-send cases": a replay-lane alert reaches zero providers. The replayed observation still
    wakes the agent; the woken run's send tools and the observation it records stay inside Rocoto. The stand-in
    is the model's choice of tool; the scheduler, the gate and the event sink are the real ones."""
    import asyncio,json,time
    from types import MethodType
    from fastapi.testclient import TestClient
    from test_phone_participants import verify_over_http
    t=tracker_app(tmp_path,monkeypatch)
    try:
        if True:
            second=SecondPerson(t.client,t.headers,t.app.state.access.issue('demo',principal_id='bob',display_name='Bob'))
            verify_over_http(t.client,t.sent,t.owner,'+34611111111')
            verify_over_http(second,t.sent,'bob','+34622222222')
            results=[]
            async def model_turn(self,message,chat_id,dataset=None,*args,**kwargs):
                # An agent whose instruction asks for a phone alert calls send_alert, then records what it confirmed.
                self.active[chat_id]={'cancel':asyncio.Event(),'generation':'turn-'+str(len(results))}
                try:
                    sent=await self._tool('send_alert',{'purpose':'monitoring','language':'en_US','parameters':['Queue watch','Six people waiting','09:00']},dataset,chat_id)
                    recorded=await self._tool('record_tracker_event',t.observation('derived-'+str(len(results)),'Queue confirmed at checkout two.'),dataset,chat_id)
                    results.append((message,sent,recorded))
                finally:self.active.pop(chat_id,None)
                yield {'type':'done','status':'completed'}
            t.ws['agent']._turn=MethodType(model_turn,t.ws['agent'])
            saved=t.tracker(mode='ongoing',max_runs=3,instruction='Text me when a queue forms.')
            t.ws['phone'].add_participant(saved['id'],'bob',by=t.owner)
            t.ws['agents'].activate(saved['id'],time.time()+1000)
            count=len(t.sent)
            replayed=t.ws['stream_ingestion'].accept(t.owner,t.window('replay-1','dataset_replay','replayed'))
            assert replayed['input_kind']=='dataset_replay' and t.delivery.deliveries(saved['id'])==[]
            assert t.wait(lambda:len(results)==1,10),'the replayed observation still wakes the agent'
            message,sent,recorded=results[0]
            assert replayed['event_ids'][0] in message
            assert (sent[2],sent[0][0]['text'])==(False,'This run reads replayed observations. Phone alerts follow live observations.')
            derived=json.loads(recorded[0][0]['text'])
            assert recorded[2] and derived['event_key']=='derived-0'  # the run keeps its own record of what it confirmed
            time.sleep(2.2)  # the worker and the catch-up pass both run
            assert t.delivery.deliveries(saved['id'])==[] and len(t.sent)==count,'a replay-lane run reached a phone'
            assert t.delivery.lane(derived['id'],lambda:'live')=='replay' and t.delivery.lane(replayed['event_ids'][0],lambda:'live')=='replay'
            # The same window kind arriving live is an alert: the host fans it out to both participants.
            live=t.ws['stream_ingestion'].accept(t.owner,t.window('live-1','live','observed-live'))
            def live_rows():return sorted((row['member_id'],row['status']) for row in t.delivery.deliveries(saved['id'],alert_id=live['event_ids'][0]))
            assert t.wait(lambda:live_rows()==sorted([(t.owner,'sent'),('bob','sent')])),live_rows()
            alerts={row['alert_id'] for row in t.delivery.deliveries(saved['id'])}
            assert replayed['event_ids'][0] not in alerts and derived['id'] not in alerts
    finally:
        t.client.__exit__(None,None,None)


def test_the_creator_is_held_to_the_same_location_check_as_the_participants_they_added(tmp_path,monkeypatch):
    """CHANNELS.md "Admission checks": a location assignment covering the agent's scope, for every participant."""
    import scope_host
    from fastapi.testclient import TestClient
    from test_phone_participants import verify_over_http
    t=tracker_app(tmp_path,monkeypatch)
    try:
        if True:
            second=SecondPerson(t.client,t.headers,t.app.state.access.issue('demo',principal_id='bob',display_name='Bob'))
            async def asleep():return None
            t.ws['scheduler'].tick=asleep
            verify_over_http(t.client,t.sent,t.owner,'+34611111111')
            verify_over_http(second,t.sent,'bob','+34622222222')
            saved=t.tracker(mode='ongoing',max_runs=3)
            t.ws['phone'].add_participant(saved['id'],'bob',by=t.owner)
            # The business graph is configured and neither person's assignments cover this conversation any more.
            covered=[False]
            real=scope_host.history_allowed
            monkeypatch.setattr(scope_host,'history_allowed',lambda *args,**kwargs:real(*args,**kwargs) if covered[0] else False)
            assert t.client.get('/api/chats/'+saved['chat_id']).status_code==404
            count=len(t.sent)
            t.ws['monitoring'].commit_event(t.owner,t.observation('queue-1'))
            assert t.wait(lambda:sorted(t.states(saved['id']))==sorted([(t.owner,'held','access_required'),('bob','held','access_required')])),t.states(saved['id'])
            assert len(t.sent)==count
            # With their assignments back, the next alert reaches both.
            covered[0]=True
            t.ws['monitoring'].commit_event(t.owner,t.observation('queue-2','Two people are waiting.'))
            assert t.wait(lambda:len(t.sent)==count+2),t.states(saved['id'])
            assert sorted(item['to'] for item in t.sent[count:])==['34611111111','34622222222']
    finally:
        t.client.__exit__(None,None,None)
