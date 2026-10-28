"""A script's bounded wake reaches its own Tracking Agent even without a new observation."""
from retail_agent_inputs import RetailInputProvider
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from background_agents import AgentScheduler
from agent_test_support import retail_agent_store as AgentStore
from core_trackers import link_tracking_agent, remove_tracker
from monitoring import monitoring_profile_authorized
from tracker_scripts import wake_agent


class Index:
    clips = []
    def status(self):
        return {'ready':False, 'datasets':['retail-demo']}
    def get_clip(self, key):
        return None


class Harness:
    def __init__(self, host):
        self.business_scope_host = host
        self.children = {}
        self.child_tasks = {}
        self.calls = []
    def _save_child(self, child):
        self.children[child['id']] = child
    async def turn(self, message, chat_id, dataset, **options):
        self.calls.append((message, options))
        self.children[chat_id]['messages'].extend([
            {'role':'user', 'content':message}, {'role':'assistant', 'content':'Script reviewed.'}])
        yield {'type':'done', 'status':'completed'}


@pytest.fixture
def setup(tmp_path):
    from access import AccessStore
    from seed_control_room import seed_location
    from retail_records import company_from_access
    access = AccessStore(tmp_path)
    state = access.create_environment('shop', 'Corner shop')
    access.issue('shop', owner=True, principal_id='peter')
    seeded = seed_location(state, tmp_path/'access.sqlite', 'shop', 'peter', lifecycle='demo')
    company = company_from_access(state, tmp_path, 'shop', 'peter', 'retail-demo')
    monitoring = company.monitoring
    host = {'state':state, 'index':Index(), 'monitoring':monitoring, 'business_scope':company.scope}
    monitoring.profile_authorized = lambda owner, profile: monitoring_profile_authorized(host, owner, profile)
    from tracker_runner import script_wake_authorizer
    agents = AgentStore(state, input_provider=RetailInputProvider(monitoring=monitoring,index=host['index']), wake_authorizer=script_wake_authorizer(host))
    harness = Harness(host)
    scheduler = AgentScheduler(agents, harness, asyncio.Semaphore(1))
    definition = agents.create({'title':'Freezer watch', 'instruction':'Maintain the freezer script.', 'dataset':'retail-demo',
        'agent_kind':'tracker', 'input_kind':'tracker_events', 'mode':'ongoing', 'max_runs':3},
        ['retail-demo'], owner_principal_id='peter')
    agents.activate(definition['id'], time.time()+1000)
    tracker = link_tracking_agent(company, seeded['location_id'], definition)
    return SimpleNamespace(company=company, agents=agents, definition=definition, tracker=tracker,
        scheduler=scheduler, harness=harness, monitoring=monitoring, access=access, state=state, root=tmp_path)


def wake(setup):
    result = wake_agent(setup.company, setup.tracker, event_id='script-run-1', text='The freezer script stopped; read its saved run.')
    assert result['queued'], result
    return result['id']


def tick(setup):
    async def run():
        await setup.scheduler.tick()
        await asyncio.gather(*setup.scheduler.tasks.values())
    asyncio.run(run())


def test_script_wake_runs_once_with_zero_observations(setup):
    identifier = wake(setup)
    assert setup.monitoring.readiness(setup.definition) is False
    tick(setup)
    assert len(setup.harness.calls) == 1
    message, options = setup.harness.calls[0]
    assert message == 'The freezer script stopped; read its saved run.'
    assert 'tracker_script' in options['model_context']
    run = setup.agents.get(setup.definition['id'])['last_run']
    assert run['status'] == 'completed' and run['source_count'] == 0
    assert setup.agents.run_report(setup.definition['id'], run['id'])['observations'] == []
    assert setup.agents.wakes.get(identifier, owner='peter')['status'] == 'completed'
    tick(setup)
    assert len(setup.harness.calls) == 1


def test_script_wake_can_claim_after_all_observations_are_consumed(setup):
    monitor = setup.monitoring
    source = {'id':'source', 'video_id':'recording', 'dataset':'retail-demo', 'start':0, 'end':20}
    monitor.source_lookup = lambda owner, key: source if key == 'source' else None
    profile = monitor.save_profile('peter', {'name':'Door events', 'dataset':'retail-demo',
        'location_id':setup.tracker['location_id'], 'vocabulary':[{'type':'door_open', 'description':'The freezer door opens.'}]},
        datasets=['retail-demo'])
    observation = monitor.commit_event('peter', {'profile_id':profile['id'], 'profile_revision':1,
        'event_key':'open-1', 'type':'door_open', 'description':'The door opens.', 'source':{'clip_id':'source','start_s':1,'end_s':2}})
    first = setup.agents.claim(setup.definition['id'], {observation['id']:'source'}, source_records=[observation])
    setup.agents.finish(first['id'], 'completed')
    identifier = wake(setup)
    next_run = setup.agents.claim(setup.definition['id'], {observation['id']:'source'}, source_records=[observation], wake_id=identifier)
    assert next_run is not None and next_run['source_count'] == 0
    assert next_run['wake']['id'] == identifier


@pytest.mark.parametrize('change', ['removed_tracker', 'revoked_membership', 'revoked_profile_access', 'revoked_camera_access', 'paused_agent'])
def test_script_wake_preserves_current_authority(setup, change):
    identifier = wake(setup)
    if change == 'removed_tracker':
        remove_tracker(setup.company, setup.tracker['location_id'], setup.tracker['key'])
    elif change == 'revoked_membership':
        with sqlite3.connect(setup.root/'access.sqlite') as db:
            db.execute("DELETE FROM principal_environments WHERE principal_id='peter' AND environment='shop'")
    elif change == 'revoked_profile_access':
        setup.monitoring.profile_authorized = lambda owner, profile: False
    elif change == 'revoked_camera_access':
        scope = setup.company.scope
        view = scope.describe(setup.company.actor, 'retail-demo')
        scope.mutate(setup.company.actor, 'retail-demo', 'cameras', {'name':'Freezer camera',
            'location_id':setup.tracker['location_id'], 'room_ids':[view['rooms'][0]['id']], 'source_ids':[]}, view['revision'])
        setup.monitoring.profile_authorized = lambda owner, profile: not profile.get('camera_ids')
    else:
        setup.agents.pause(setup.definition['id'])
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier) is None
    tick(setup)
    assert setup.harness.calls == []


def test_unrelated_host_event_does_not_gain_a_script_only_turn(setup):
    row = setup.agents.wakes.enqueue(setup.definition['id'], owner='peter', expected_revision=1,
        provider='other_adapter', connection_id=setup.tracker['id'], event_id='unrelated-1', text='New evidence')
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=row['id']) is None
    tick(setup)
    assert setup.harness.calls == []


def test_script_wake_obeys_the_chats_selected_location(setup):
    from seed_control_room import seed_location
    other = seed_location(setup.state, setup.root/'access.sqlite', 'shop', 'peter', lifecycle='demo', name='Other shop')
    scope = setup.company.scope
    view = scope.describe(setup.company.actor, 'retail-demo')
    scope.bind_chat(setup.company.actor, setup.definition['chat_id'], 'retail-demo',
        [other['location_id']], view['revision'])
    wake(setup)
    tick(setup)
    assert setup.harness.calls == []


def test_script_wake_obeys_expiry_and_does_not_reenable_archive_agents(setup):
    identifier = wake(setup)
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier, now=time.time()+2000) is None
    with setup.agents.connect() as db:
        legacy = setup.agents._read(db, 'agents', setup.definition['id'])
        legacy.update(input_kind='camera_footage', status='active', expires_at=time.time()+1000, next_run_at=0)
        setup.agents._save(db, 'agents', legacy)
    another = wake_agent(setup.company, setup.tracker, event_id='script-run-2', text='Review the script.')
    assert another['queued']
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=another['id']) is None
    tick(setup)
    assert setup.harness.calls == []


def test_input_optional_wake_fails_closed_without_its_adapter(setup):
    identifier = wake(setup)
    setup.agents.wake_authorizer = None
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier) is None
    tick(setup)
    assert setup.harness.calls == []


def test_a_removed_trackers_wake_cannot_run_for_its_replacement(setup):
    identifier = wake(setup)
    remove_tracker(setup.company, setup.tracker['location_id'], setup.tracker['key'])
    replacement = link_tracking_agent(setup.company, setup.tracker['location_id'], setup.definition)
    assert replacement['id'] == setup.tracker['id']
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier) is None
    tick(setup)
    assert setup.harness.calls == []


def test_script_source_access_is_rechecked_at_dispatch(setup):
    identifier = wake(setup)
    run = setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier)
    assert run is not None
    setup.monitoring.profile_authorized = lambda owner, profile: False
    asyncio.run(setup.scheduler._execute(run))
    assert setup.harness.calls == []
    assert setup.agents.get(setup.definition['id'])['last_run']['status'] == 'interrupted'


def test_script_wake_binds_its_words_to_the_conversations_scope(setup):
    wake(setup)
    tick(setup)
    history = setup.company.scope.history(setup.definition['chat_id'])
    assert history is not None
    assert history['scopes']['retail-demo']['location_ids'] == [setup.tracker['location_id']]


def camera_in_the_chat(setup):
    scope = setup.company.scope
    view = scope.describe(setup.company.actor, 'retail-demo')
    view = scope.mutate(setup.company.actor, 'retail-demo', 'cameras', {'name':'Freezer camera',
        'location_id':setup.tracker['location_id'], 'room_ids':[view['rooms'][0]['id']], 'source_ids':[]}, view['revision'])
    camera = view['cameras'][-1]
    scope.bind_chat(setup.company.actor, setup.definition['chat_id'], 'retail-demo',
        [setup.tracker['location_id']], view['revision'])
    return camera


def test_queued_script_words_keep_their_original_cameras_after_a_move(setup):
    from seed_control_room import seed_location
    camera = camera_in_the_chat(setup)
    identifier = wake(setup)
    other = seed_location(setup.state, setup.root/'access.sqlite', 'shop', 'peter', lifecycle='demo', name='Other shop')
    scope = setup.company.scope
    view = scope.describe(setup.company.actor, 'retail-demo')
    scope.mutate(setup.company.actor, 'retail-demo', 'cameras', {'id':camera['id'], 'name':camera['name'], 'source_ids':camera['source_ids'],
        'location_id':other['location_id'], 'room_ids':[other['room_id']]}, view['revision'])
    assert setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier) is None
    tick(setup)
    assert setup.harness.calls == []


def test_script_history_retains_camera_authority(setup):
    from scope_host import history_within_view
    camera = camera_in_the_chat(setup)
    wake(setup)
    tick(setup)
    history = setup.company.scope.history(setup.definition['chat_id'])
    assert history['scopes']['retail-demo']['camera_ids'] == [camera['id']]
    scope = setup.company.scope.chat_scope(setup.company.actor, setup.definition['chat_id'], 'retail-demo')
    view = SimpleNamespace(business_scopes=[{**scope, 'camera_ids':[]}], clips=[])
    assert history_within_view(setup.harness.business_scope_host, setup.harness.children[setup.definition['chat_id']], view) is False


@pytest.mark.parametrize('change', ['pause', 'expiry', 'revision'])
def test_script_dispatch_rechecks_the_execution_grant(setup, change):
    identifier = wake(setup)
    run = setup.agents.claim(setup.definition['id'], {}, source_records=[], wake_id=identifier)
    with setup.agents.connect() as db:
        definition = setup.agents._read(db, 'agents', setup.definition['id'])
        if change == 'pause':
            definition['status'] = 'paused'
        elif change == 'expiry':
            definition['expires_at'] = time.time()-1
        else:
            definition['revision'] += 1
        setup.agents._save(db, 'agents', definition)
    asyncio.run(setup.scheduler._execute(run))
    assert setup.harness.calls == []


def test_unattended_script_producer_can_queue_a_current_standing_grant(setup):
    setup.agents.pause(setup.definition['id'])
    setup.agents.authority=lambda definition,actor:actor==setup.company.owner and bool(setup.company.scope.authority(setup.company.actor))
    setup.agents.activate(setup.definition['id'],standing=True,actor_principal_id=setup.company.owner)
    identifier=wake(setup)
    tick(setup)
    assert setup.agents.wakes.get(identifier,owner=setup.company.owner)['status']=='completed'
    assert setup.agents.get(setup.definition['id'])['status']=='active'
