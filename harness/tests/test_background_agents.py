"""Execution grants, incremental coverage and restart accounting, without GPU calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from background_agents import AgentStore, AgentScheduler
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from agent_inputs import NoInputProvider
from retail_agent_inputs import RetailInputProvider, RetailAgentDefinition, evidence_scope
from test_server_access import fixture_app
from test_watch_screening import Index as WatchIndex


def definition(**changes):
    return {"title":"Counter handoffs", "instruction":"Find visible cup handoffs and explain the evidence.",
            "dataset":"cafe", "mode":"scheduled", "interval_seconds":60, "max_runs":3,
            "input_kind":"none", **changes}



RESOURCE_ADMISSION = ContextVar('fixture_resource_admission', default=None)


class BoundedInput(NoInputProvider):
    """A declared provider's resource allowance shared by all children of one run."""
    def resource_ceiling(self):
        return Decimal('2.50')

    def resource_allowance(self, definition):
        return 1

    @contextmanager
    def context(self, agent, run, snapshot=None):
        token = RESOURCE_ADMISSION.set(lambda amount:self.store.admit_resource(run['id'], amount))
        try:
            with super().context(agent, run, snapshot) as message:
                yield message
        finally:
            RESOURCE_ADMISSION.reset(token)


def observation_fixture(state):
    from monitoring import MonitoringStore
    index = WatchIndex([('clip-a', [1, 0]), ('clip-b', [1, 0])])
    sources = {row['id']:row for row in index.clips}
    monitoring = MonitoringStore(state, source_lookup=lambda owner, key:sources.get(key))
    profile = monitoring.save_profile('person-a', {'name':'Handoffs', 'dataset':'cafe',
        'vocabulary':[{'type':'handoff', 'description':'A visible object handoff.'}]}, datasets=['cafe'])
    store = AgentStore(state, input_provider=RetailInputProvider(monitoring=monitoring, index=index))
    def commit(key, clip='clip-a'):
        return monitoring.commit_event('person-a', {'profile_id':profile['id'], 'profile_revision':1,
            'event_key':key, 'type':'handoff', 'description':'An object changes hands.',
            'source':{'clip_id':clip, 'start_s':1, 'end_s':2}})
    def create(**changes):
        return store.create(definition(mode='ongoing', interval_seconds=10,
            input_kind='tracker_events', **changes), ['cafe'], owner_principal_id='person-a')
    def claim(key, **options):
        rows = monitoring.events_for(store.get(key))
        return store.claim(key, {row['id']:row['source']['clip_id'] for row in rows},
            source_records=rows, **options)
    return store, index, commit, create, claim

def active(store, **changes):
    record = store.create(definition(**changes), ["cafe"])
    store.activate(record["id"], time.time()+1000)
    return record["id"]


def test_draft_never_claims_and_invalid_delivery_is_rejected(tmp_path):
    store = AgentStore(tmp_path)
    record = store.create(definition(), ["cafe"])
    assert store.claim(record["id"], {}) is None
    assert store.get(record["id"])["runs"] == []
    with pytest.raises(ValueError):
        store.create(definition(output="webhook", destination="https://example.com"), ["cafe"])


def test_ongoing_watermark_advances_only_after_success(tmp_path):
    store,index,commit,create,claim = observation_fixture(tmp_path)
    commit('first'); key=create()['id']; now=time.time()
    store.activate(key,now+1000,now=now)
    first=claim(key,now=now);store.finish(first['id'],'completed',now=now)
    assert claim(key,now=now+11) is None
    commit('second','clip-b')
    second=claim(key,now=now+22)
    assert second['source_ids']==['clip-b']
    store.finish(second['id'],'failed','Lost connection',now=now+23)
    assert store.get(key)['status']=='needs_attention'
    store.activate(key,now+500,now=now+30)
    third=claim(key,now=now+31)
    assert third['source_ids']==['clip-b']


def test_run_once_reviews_committed_observations_for_an_ongoing_agent(tmp_path):
    store,index,commit,create,claim = observation_fixture(tmp_path)
    commit('first');key=create()['id']
    store.activate(key,time.time()+1000,once=True)
    manual=claim(key)
    assert manual['source_ids']==['clip-a'] and manual['trigger']=='manual'
    store.finish(manual['id'],'completed')
    assert store.get(key)['status']=='paused'


def test_scheduler_coalesces_missed_ticks_and_respects_company_slot(tmp_path):
    store = AgentStore(tmp_path)
    first, second = active(store, mode="scheduled", interval_seconds=60), active(store)
    now = time.time()
    run = store.claim(first, {}, now=now)
    assert store.claim(first, {}, now=now+300) is None
    assert store.claim(second, {}, now=now+300) is None
    store.finish(run["id"], "completed", now=now+300)
    assert store.claim(first, {}, now=now+359) is None
    assert store.claim(first, {}, now=now+361) is not None


def test_expiry_restart_and_spent_attempts_require_explicit_resumption(tmp_path):
    store = AgentStore(tmp_path, input_provider=BoundedInput())
    key = active(store, max_runs=1)
    run = store.claim(key, {})
    store.admit_resource(run["id"], "2.50")
    reopened = AgentStore(tmp_path, input_provider=BoundedInput())
    reopened.recover()
    result = reopened.get(key)
    assert result["status"] == "paused"
    assert result["last_run"]["status"] == "recovering"
    assert reopened.claim(key, {}) is None
    assert result["last_run"]["credit_committed_ceiling_usd"] == "2.50"
    with pytest.raises(ValueError, match="Revise"):
        reopened.activate(key, time.time()+1000)
    expired = active(store)
    assert store.claim(expired, {}, now=time.time()+1001) is None
    assert store.get(expired)["status"] == "paused"



def test_parallel_children_share_one_resource_grant(tmp_path):
    store = AgentStore(tmp_path, input_provider=BoundedInput())
    key = active(store)
    run = store.claim(key, {})
    def admission(_):
        try:
            store.admit_resource(run["id"], "2.50")
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=4) as workers:
        assert sum(workers.map(admission, range(4))) == 1
    assert store.get(key)["last_run"]["resource_uses"] == 1
    store.pause(key)
    with pytest.raises(ValueError, match="grant"):
        store.admit_resource(run["id"], "2.50")



def test_editing_requires_pause_and_preserves_the_run_snapshot(tmp_path):
    store = AgentStore(tmp_path)
    key = active(store)
    run = store.claim(key, {})
    with pytest.raises(ValueError, match="Pause"):
        store.update(key, definition(title="Other question"), ["cafe"])
    store.finish(run["id"], "completed")
    store.pause(key)
    record = store.update(key, definition(title="Other question"), ["cafe"])
    assert record["revision"] == 2 and record["status"] == "draft"
    with store.connect() as db:
        prior = store._read(db, "runs", run["id"])
    assert prior["definition"]["title"] == "Counter handoffs"
    assert prior["revision"] == 1


def test_stale_agent_edits_are_rejected_atomically(tmp_path):
    store=AgentStore(tmp_path)
    record=store.create(definition(),['cafe'])
    def update(number):
        try:return store.update(record['id'],definition(title=f'Revision {number}'),['cafe'],expected_revision=1)
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(update,[1,2]))
    assert len([item for item in results if item])==1
    assert store.get(record['id'])['revision']==2


def test_host_agent_ownership_and_selected_skills_survive_revision_and_run(tmp_path):
    store=AgentStore(tmp_path)
    record=store.create(definition(skill_ids=['video-investigation']),['cafe'],owner_principal_id='person-a')
    assert record['owner_principal_id']=='person-a'
    assert store.list(owner_principal_id='person-b')==[]
    updated=store.update(record['id'],definition(skill_ids=['video-investigation'],title='Revised'),['cafe'],expected_revision=1)
    assert updated['owner_principal_id']=='person-a'
    store.activate(record['id'],time.time()+100)
    run=store.claim(record['id'],{})
    assert run['owner_principal_id']=='person-a'
    assert run['definition']['skill_ids']==['video-investigation']
    snapshots=[{'id':'video-investigation','revision':1,'instructions':'Approved fixture instructions'}]
    store.pin_skills(run['id'],{'skills':snapshots,'snapshot_sha256':'a'*64,
        'company_scope':'fixture-company','authority':'instructions_only'})
    snapshots[0]['instructions']='Mutated caller input'
    assert store.run_report(record['id'],run['id'])['skill_snapshots'][0]['instructions']=='Approved fixture instructions'
    assert store.run_report(record['id'],run['id'])['skill_context']['snapshot_sha256']=='a'*64
    with pytest.raises(ValueError):store.pin_skills(run['id'],snapshots)
    with pytest.raises(ValueError):store.create(definition(owner_principal_id='person-b'),['cafe'])
    with pytest.raises(ValueError):store.create(definition(skill_ids=['one','one']),['cafe'])


def test_scheduler_passes_host_selected_skill_ids_and_owner_to_native_turn(tmp_path):
    class Harness:
        def __init__(self):self.children,self.child_tasks={},{}
        def _save_child(self,child):pass
        async def turn(self,message,chat_id,dataset,*,skill_ids,**options):
            assert skill_ids==['video-investigation']
            assert self.children[chat_id]['owner_principal_id']=='person-a'
            yield {'type':'done','status':'completed'}
    async def prove():
        store=AgentStore(tmp_path);harness=Harness()
        scheduler=AgentScheduler(store,harness,asyncio.Semaphore(2))
        record=store.create(definition(mode='ad_hoc',skill_ids=['video-investigation']),['cafe'],owner_principal_id='person-a')
        store.activate(record['id'],time.time()+100)
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
        assert store.get(record['id'])['last_run']['status']=='completed'
    asyncio.run(prove())


def test_saved_evidence_filters_real_capture_times_and_records_archive_fallback():
    from datetime import datetime,timezone
    iso=lambda value:datetime.fromtimestamp(value,timezone.utc).isoformat()
    spec=RetailAgentDefinition.model_validate(definition()).model_dump()
    now=time.time()
    records=[{'id':'old','capture_start_utc':iso(now-1000),'capture_end_utc':iso(now-900)},
             {'id':'recent','capture_start_utc':iso(now-30),'capture_end_utc':iso(now-20)}]
    selected,scope=evidence_scope(spec,{'a':'old','b':'recent'},records,now)
    assert list(selected.values())==['recent']
    assert scope['basis']=='capture_utc' and scope['lookback_seconds']==60
    selected,scope=evidence_scope(spec,{'a':'old','b':'recent'},
        [{'id':'old','start':0,'end':8},{'id':'recent','start':8,'end':16}],now+61)
    assert set(selected.values())=={'old','recent'}
    assert scope['basis']=='source_relative_archive' and scope['utc_window_applied'] is False
    assert scope['capture_timestamps_missing']==2


def test_naive_capture_times_are_not_promoted_to_utc():
    spec=RetailAgentDefinition.model_validate(definition()).model_dump()
    selected,scope=evidence_scope(spec,{'a':'clip'},
        [{'id':'clip','capture_start_utc':'2026-10-04T10:00:00',
          'capture_end_utc':'2026-10-04T10:00:08'}],time.time())
    assert selected=={'a':'clip'} and scope['basis']=='source_relative_archive'


def test_http_code_gate_scoped_drafts_and_activation(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    headers = {"X-Video-Demo":"1"}
    with TestClient(app) as client:
        client.post("/api/session/demo", headers=headers)
        assert client.get("/api/agents").status_code == 403
        assert client.post("/api/agents", json=definition(), headers=headers).status_code == 403
        code = app.state.access.issue("demo")
        client.post("/api/session", json={"code":code}, headers=headers)
        response = client.post("/api/agents", json=definition(mode='ad_hoc', fresh_inspections_per_run=0), headers=headers)
        assert response.status_code == 200
        key = response.json()["id"]
        assert response.json()["status"] == "draft"
        assert client.get("/api/agents").json()["agents"][0]["id"] == key
        assert client.post(f"/api/agents/{key}/activate", json={"expected_revision":99}, headers=headers).status_code == 400
        assert client.post(f"/api/agents/{key}/activate", json={"expected_revision":1}, headers=headers).json()["status"] == "active"
        assert client.post(f"/api/agents/{key}/pause", headers=headers).json()["status"] == "paused"
        app.state.access.create_environment("company-b", "Company B")
        code = app.state.access.issue("company-b")
        client.post("/api/session", json={"code":code}, headers=headers)
        assert client.get(f"/api/agents/{key}").status_code == 404
        assert client.get("/api/agents").json()["agents"] == []


def test_background_execution_inherits_budget_and_survives_browser_absence(tmp_path):
    class Harness:
        def __init__(self):
            self.children, self.child_tasks = {}, {}
        def _save_child(self, child):
            pass
        async def turn(self, *args, **options):
            assert RESOURCE_ADMISSION.get() is not None
            async def child():
                RESOURCE_ADMISSION.get()("2.50")
            await asyncio.create_task(child())
            await asyncio.sleep(.01)
            yield {"type":"done", "status":"completed"}
    async def prove():
        store = AgentStore(tmp_path, input_provider=BoundedInput())
        scheduler = AgentScheduler(store, Harness(), asyncio.Semaphore(2))
        key = active(store, mode="ad_hoc")
        await scheduler.tick()
        await asyncio.gather(*scheduler.tasks.values())
        result = store.get(key)
        assert result["status"] == "completed"
        assert result["last_run"]["resource_uses"] == 1
        assert RESOURCE_ADMISSION.get() is None
    asyncio.run(prove())



def test_cancel_before_worker_starts_leaves_terminal_run(tmp_path):
    class Harness:
        async def control(self, *args):
            raise ValueError("Worker is starting")
    async def prove():
        store = AgentStore(tmp_path)
        scheduler = AgentScheduler(store, Harness(), asyncio.Semaphore(2))
        key = active(store, mode="ad_hoc")
        await scheduler.tick()
        await scheduler.pause(key)
        assert store.get(key)["status"] == "paused"
        assert store.get(key)["last_run"]["status"] == "interrupted"
    asyncio.run(prove())


def test_pause_cancels_active_work_without_waiting_for_native_stop_rpc(tmp_path):
    class Harness:
        def __init__(self):
            self.children, self.child_tasks = {}, {}
            self.started, self.stopped = asyncio.Event(), asyncio.Event()
        def _save_child(self, child):
            pass
        async def control(self, *args):
            await asyncio.Event().wait()
        async def turn(self, *args, **options):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()
            yield {"type":"done", "status":"completed"}
    async def prove():
        store = AgentStore(tmp_path)
        harness = Harness()
        scheduler = AgentScheduler(store, harness, asyncio.Semaphore(2))
        key = active(store, mode="ad_hoc")
        await scheduler.tick()
        await harness.started.wait()
        await asyncio.wait_for(scheduler.pause(key), timeout=.5)
        assert harness.stopped.is_set()
        assert store.get(key)["last_run"]["status"] == "interrupted"
    asyncio.run(prove())


def test_finished_report_is_an_immutable_snapshot(tmp_path):
    store = AgentStore(tmp_path)
    key = active(store, mode="ad_hoc")
    run = store.claim(key, {})
    messages = [{"role":"assistant", "content":"Observed cup handoff", "results":[{"id":"clip-a"}]}]
    store.finish(run["id"], "completed", report_messages=messages)
    original = store.run_report(key, run["id"])
    messages[0]["content"] = "A later followup"
    store.freeze_report(run["id"], messages)
    frozen = store.run_report(key, run["id"])
    assert frozen["report_messages"][0]["content"] == "Observed cup handoff"
    assert frozen["report_sha256"] == original["report_sha256"]


def test_completed_agent_chat_requires_a_linked_continuation(tmp_path, monkeypatch):
    from test_server_access import FixtureAgent
    app = fixture_app(tmp_path, monkeypatch)
    headers = {"X-Video-Demo":"1"}
    with TestClient(app) as client:
        session=client.post("/api/session", json={"code":app.state.access.issue("demo")}, headers=headers).json()
        agent = FixtureAgent.instances["demo"]
        agent.active = {}
        agent.children["demo-chat"].update(kind="agent_run", dataset="cafe",owner_principal_id=session['principal']['id'])
        response = client.post("/api/chat", json={"chat_id":"demo-chat", "message":"Followup", "dataset":"cafe"}, headers=headers)
        assert response.status_code == 409 and "linked chat" in response.json()["detail"]


def test_linked_followup_receives_frozen_evidence_and_keeps_original_report(tmp_path, monkeypatch):
    from test_server_access import FixtureAgent
    app = fixture_app(tmp_path, monkeypatch)
    captured = []
    async def followup(self, message, chat_id, dataset, attachments, clip_attachments=None, **kwargs):
        captured.append((message, chat_id, kwargs.get("model_context", "")))
        yield {"type":"done", "status":"completed", "thread_id":"new-thread"}
    monkeypatch.setattr(FixtureAgent, "turn", followup, raising=False)
    monkeypatch.setattr(FixtureAgent, "_input", lambda *args: [], raising=False)
    monkeypatch.setattr(FixtureAgent, "_save_child", lambda *args: None, raising=False)
    headers = {"X-Video-Demo":"1"}
    with TestClient(app) as client:
        session=client.post("/api/session", json={"code":app.state.access.issue("demo")}, headers=headers).json()
        store = AgentStore(tmp_path / "state")
        key=store.create(definition(mode='ad_hoc'),['cafe'],owner_principal_id=session['principal']['id'])['id']
        store.activate(key,time.time()+1000)
        run = store.claim(key, {})
        store.finish(run["id"], "completed", report_messages=[{"role":"assistant", "content":"Observed cup handoff", "results":[{"id":"clip-a"}]}])
        agent = FixtureAgent.instances["demo"]
        agent.registry={}
        agent.active = {}
        agent.children[run["chat_id"]] = {"kind":"agent_run", "dataset":"cafe", "running":False,
            "agent_id":key, "run_id":run["id"], "id":run["chat_id"], "messages":[],
            'owner_principal_id':session['principal']['id']}
        response = client.post("/api/chat", json={"chat_id":"new-conversation", "message":"Show the handoff",
            "dataset":"cafe", "followup_run_chat_id":run["chat_id"]}, headers=headers)
        assert response.status_code == 200
        assert captured[0][:2] == ("Show the handoff", "new-conversation")
        assert "Observed cup handoff" in captured[0][2]
        report = client.get(run["report_url"]).json()
        assert report["messages"][0]["content"] == "Observed cup handoff"
        assert agent.children[run["chat_id"]]["messages"] == []


def test_ongoing_observation_context_contains_only_new_committed_events(tmp_path):
    import json
    captured=[]
    class Harness:
        def __init__(self):self.children,self.child_tasks={},{}
        def _save_child(self,child):pass
        async def turn(self,message,chat_id,dataset,**options):
            rows=json.loads(message.rsplit('\n',1)[-1])
            captured.extend(rows)
            assert [row['event_key'] for row in rows]==['new']
            yield {'type':'done','status':'completed'}
    async def prove():
        store,index,commit,create,claim=observation_fixture(tmp_path)
        scheduler=AgentScheduler(store,Harness(),asyncio.Semaphore(2))
        old=commit('old');key=create()['id'];store.activate(key,time.time()+1000)
        first=claim(key);store.finish(first['id'],'completed',now=time.time()-20)
        new=commit('new','clip-b')
        await scheduler.tick();await asyncio.gather(*scheduler.tasks.values())
        assert store.get(key)['last_run']['status']=='completed'
        assert captured==[new] and old not in captured
    asyncio.run(prove())


def test_legacy_agent_claim_backfills_new_definition_fields(tmp_path):
    store=AgentStore(tmp_path);key=active(store)
    with store.connect() as db:
        legacy=store._read(db,'agents',key)
        for field in ['description','skill_ids','agent_kind','input_kind','signal_chain']:
            legacy.pop(field,None)
        store._save(db,'agents',legacy)
    run=store.claim(key,{})
    assert run['definition']['description']=='' and run['definition']['skill_ids']==[]
    assert run['definition']['agent_kind']=='action' and run['definition']['input_kind']=='none'
    assert run['definition']['signal_chain']==[{'kind':'deliver'}]
    assert run['definition']['instruction']==definition()['instruction']

def test_agent_keeps_one_conversation_across_runs_and_revisions(tmp_path):
    store = AgentStore(tmp_path)
    key = active(store)
    conversation = store.get(key)['chat_id']
    first = store.claim(key, {})
    store.finish(first['id'], 'completed', now=time.time()-120)
    second = store.claim(key, {})
    assert first['chat_id'] == second['chat_id'] == conversation
    store.finish(second['id'], 'completed')
    store.pause(key)
    assert store.update(key, definition(title='Changed title'), ['cafe'])['chat_id'] == conversation
    assert AgentStore(tmp_path).get(key)['chat_id'] == conversation


def test_legacy_agent_adopts_latest_run_chat_without_rewriting_reports(tmp_path):
    import json
    store = AgentStore(tmp_path)
    key = active(store)
    run = store.claim(key, {})
    store.finish(run['id'], 'completed', report_messages=[{'role':'assistant','content':'Historical evidence'}])
    with store.connect() as db:
        original = store._read(db, 'agents', key)
        original.pop('chat_id', None)
        store._save(db, 'agents', original)
        legacy = store._read(db, 'runs', run['id'])
        legacy['chat_id'] = 'agent-run-legacy'
        store._save(db, 'runs', legacy)
    reopened = AgentStore(tmp_path)
    assert reopened.get(key)['chat_id'] == 'agent-run-legacy'
    assert reopened.run_report(key, run['id']) == legacy


def test_agent_setup_chat_binding_is_unique_and_scope_stays_fixed(tmp_path):
    store = AgentStore(tmp_path)
    record = store.create(definition(), ['cafe'], owner_principal_id='person-a', conversation_chat_id='setup-chat')
    assert record['chat_id'] == 'setup-chat'
    with pytest.raises(ValueError):
        store.create(definition(), ['cafe'], owner_principal_id='person-a', conversation_chat_id='setup-chat')
    with pytest.raises(ValueError):
        store.create(definition(), ['cafe'], conversation_chat_id='../unsafe')
    with pytest.raises(ValueError):
        store.update(record['id'], definition(dataset='retail'), ['cafe','retail'])


def test_scheduler_appends_to_conversation_and_freezes_only_current_run(tmp_path):
    class Harness:
        def __init__(self):
            self.children, self.child_tasks, self.active = {}, {}, {}
            self.calls = []
        def _save_child(self, child): pass
        async def turn(self, message, chat_id, dataset, **options):
            child = self.children[chat_id]
            self.calls.append((chat_id, list(child['messages'])))
            child['messages'].extend([{'role':'user','content':message}, {'role':'assistant','content':f'Report {len(self.calls)}'}])
            yield {'type':'done','status':'completed'}
    async def prove():
        store = AgentStore(tmp_path); harness = Harness()
        scheduler = AgentScheduler(store,harness,asyncio.Semaphore(2))
        key = active(store)
        child = scheduler.ensure_conversation(key)
        child['messages'].append({'role':'user','content':'Keep checking the front counter.'})
        first = store.claim(key, {})
        await scheduler._execute(first)
        with store.connect() as db:
            record = store._read(db,'agents',key); record['next_run_at'] = time.time()-1; store._save(db,'agents',record)
        second = store.claim(key, {})
        await scheduler._execute(second)
        assert harness.calls[0][0] == harness.calls[1][0] == store.get(key)['chat_id']
        assert any(item.get('content') == 'Report 1' for item in harness.calls[1][1])
        assert len(child['messages']) == 5
        assert store.run_report(key,first['id'])['report_messages'][-1]['content'] == 'Report 1'
        report = store.run_report(key,second['id'])
        assert report['message_start'] == 3
        assert [item['content'] for item in report['report_messages']][-1] == 'Report 2'
        assert len(report['report_messages']) == 2
        assert child['kind'] == 'agent_conversation'
        assert child['running'] is False
    asyncio.run(prove())


def test_scheduler_preserves_legacy_chat_and_rejects_foreign_identity(tmp_path):
    class Harness:
        def __init__(self): self.children, self.child_tasks = {}, {}
        def _save_child(self, child): pass
    store = AgentStore(tmp_path); harness = Harness()
    record = store.create(definition(), ['cafe'], owner_principal_id='person-a')
    harness.children[record['chat_id']] = {'id':record['chat_id'],'kind':'agent_run','agent_id':record['id'],
        'dataset':'cafe','owner_principal_id':'person-a','messages':[{'role':'assistant','content':'Prior answer'}]}
    scheduler = AgentScheduler(store,harness,asyncio.Semaphore(2))
    child = scheduler.ensure_conversation(record)
    assert child['messages'][0]['content'] == 'Prior answer'
    assert child['kind'] == 'agent_conversation'
    child['owner_principal_id'] = 'person-b'
    with pytest.raises(ValueError): scheduler.ensure_conversation(record)

@pytest.mark.parametrize('designated', [False, True])
def test_proposal_uses_designated_chat_or_creates_a_separate_conversation(tmp_path, designated):
    import json
    from agent_bridge import HarnessAgent
    async def prove():
        from host import BareWorkspace, Collections
        from types import SimpleNamespace
        agent = HarnessAgent(tmp_path)
        collections=Collections();collections.register(lambda principal:['cafe']);collections.seal()
        agent.vertical=BareWorkspace({'collections':collections,
            'business_scope':SimpleNamespace(member=lambda actor:{'role':'operator'} if actor=='person-a' else None)})
        agent.automation_store = AgentStore(tmp_path)
        from workspace_capabilities import WorkspaceCapabilities
        agent.workspace_capabilities=WorkspaceCapabilities(inputs=lambda:agent.automation_store.inputs)
        agent.children['origin-chat'] = {'id':'origin-chat','dataset':'cafe','kind':'chat','messages':[],
            'owner_principal_id':'person-a', 'parent_id':None,
            **({'agent_setup':{'mode':'ad_hoc'}} if designated else {})}
        agent.active['origin-chat'] = {'cancel':asyncio.Event()}
        args = definition(mode='ad_hoc'); args.pop('dataset')
        content, _, success = await agent._tool('propose_agent', args, 'cafe', 'origin-chat')
        assert success
        record = json.loads(content[0]['text'])
        assert (record['chat_id'] == 'origin-chat') is designated
        target = agent.children[record['chat_id']]
        assert target['kind'] == 'agent_conversation' and target['agent_id'] == record['id']
        assert target['owner_principal_id'] == 'person-a' and target['dataset'] == 'cafe'
        if not designated:
            assert 'agent_setup' not in agent.children['origin-chat']
            assert target['messages'][0]['content'] == args['instruction']
        event = agent.active['origin-chat']['tool_events'].get_nowait()
        assert event['type'] == 'agent' and event['agent']['chat_id'] == record['chat_id']
        answer = {}; agent._record_event(answer, event)
        agent._record_event(answer, {**event, 'agent':{**event['agent'], 'title':'Updated'}})
        assert len(answer['agents']) == 1 and answer['agents'][0]['title'] == 'Updated'
    asyncio.run(prove())


def test_recovery_clears_active_run_marker_before_manual_conversation_reply(tmp_path):
    class Harness:
        def __init__(self):self.children,self.child_tasks={},{ }
        def _save_child(self,child):pass
    store=AgentStore(tmp_path);key=active(store);run=store.claim(key,{})
    harness=Harness()
    harness.children[run['chat_id']]={'id':run['chat_id'],'agent_id':key,'dataset':'cafe',
        'owner_principal_id':None,'messages':[], 'active_run_id':run['id'],'running':True}
    scheduler=AgentScheduler(store,harness,asyncio.Semaphore(2))
    child=scheduler.ensure_conversation(key)
    assert 'active_run_id' not in child and child['running'] is False
    assert store.get(key)['last_run']['status']=='interrupted'


def test_recovery_freezes_empty_report_for_shared_run_cancelled_before_dispatch(tmp_path):
    class Harness:
        def __init__(self): self.children,self.child_tasks={},{ }
        def _save_child(self, child): pass
    store=AgentStore(tmp_path);key=active(store);run=store.claim(key,{})
    harness=Harness()
    history=[{'role':'user','content':'An earlier question'}, {'role':'assistant','content':'An earlier answer'}]
    harness.children[run['chat_id']]={'id':run['chat_id'],'agent_id':key,'dataset':'cafe',
        'owner_principal_id':None,'messages':history,'kind':'agent_conversation','running':False}
    scheduler=AgentScheduler(store,harness,asyncio.Semaphore(2))
    report=store.run_report(key,run['id'])
    assert report['status']=='interrupted' and report['report_messages']==[]
    assert scheduler.ensure_conversation(key)['messages']==history
