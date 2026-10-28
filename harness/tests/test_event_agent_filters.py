"""Saved event selections stay exact across empty collections, pagination and claims."""
from retail_agent_inputs import RetailInputProvider
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_test_support import retail_agent_store as AgentStore
from retail_agent_inputs import RetailAgentDefinition as AgentDefinition
from monitoring import MonitoringStore


def setup_events(tmp_path):
    source = {'id':'source', 'video_id':'recording', 'dataset':'shop', 'start':0, 'end':20}
    monitoring = MonitoringStore(tmp_path, source_lookup=lambda owner, key: source if key == 'source' else None)
    profile = monitoring.save_profile('alice', {'name':'Shop events', 'dataset':'shop', 'vocabulary':[
        {'type':'queue_formed', 'description':'A queue forms.'},
        {'type':'pick_up', 'description':'A shopper picks up an item.'},
    ]}, datasets=['shop'])
    def event(kind, key):
        return monitoring.commit_event('alice', {'profile_id':profile['id'], 'profile_revision':1,
            'event_key':key, 'type':kind, 'description':kind,
            'source':{'clip_id':'source', 'start_s':2, 'end_s':3}})
    return monitoring, profile, event


def definition(**more):
    return {'title':'Queue watcher', 'instruction':'Summarize queue observations.', 'dataset':'shop',
        'agent_kind':'tracker', 'input_kind':'tracker_events', 'mode':'ongoing', 'max_runs':3,
        'event_types':['queue_formed'], **more}


def test_saved_event_selection_survives_old_client_edit(tmp_path):
    monitoring, profile, event = setup_events(tmp_path)
    agents = AgentStore(tmp_path, input_provider=RetailInputProvider(monitoring=monitoring))
    saved = agents.create(definition(), ['shop'], owner_principal_id='alice')
    edited = definition(title='Queues at the till')
    edited.pop('event_types')
    changed = agents.update(saved['id'], edited, ['shop'], expected_revision=1)
    assert changed['event_types'] == ['queue_formed']
    assert AgentStore(tmp_path, input_provider=RetailInputProvider(monitoring=monitoring)).get(saved['id'])['event_types'] == ['queue_formed']


@pytest.mark.parametrize('values', [['queue_formed', 'queue_formed'], ['Queue formed'], ['queue/formed']])
def test_event_selection_uses_distinct_event_type_identifiers(values):
    with pytest.raises(ValueError):
        AgentDefinition.model_validate(definition(event_types=values))


def test_empty_selection_does_not_make_unrelated_observations_ready(tmp_path):
    monitoring, profile, event = setup_events(tmp_path)
    event('pick_up', 'unrelated')
    scoped = {**definition(), 'owner_principal_id':'alice', 'profile_ids':[]}
    assert monitoring.events_for(scoped) == []
    assert monitoring.readiness(scoped) is False
    matching = event('queue_formed', 'matching')
    assert monitoring.events_for(scoped) == [matching]
    assert monitoring.readiness(scoped) is True
    assert len(monitoring.events_for({**scoped, 'event_types':[]})) == 2


def test_type_filter_precedes_page_limit(tmp_path):
    monitoring, profile, event = setup_events(tmp_path)
    for index in range(65):
        event('pick_up', f'unrelated-{index}')
    matching = event('queue_formed', 'matching')
    assert monitoring.list_events('alice', 'shop', event_types=['queue_formed'], limit=64, oldest_first=True) == [matching]


def test_claim_and_monitoring_wake_enforce_saved_type_filter(tmp_path):
    monitoring, profile, event = setup_events(tmp_path)
    unrelated = event('pick_up', 'unrelated')
    matching = event('queue_formed', 'matching')
    agents = AgentStore(tmp_path, input_provider=RetailInputProvider(monitoring=monitoring))
    saved = agents.create(definition(), ['shop'], owner_principal_id='alice')
    agents.activate(saved['id'], time.time()+1000)
    assert agents.claim(saved['id'], {unrelated['id']:'source'}, source_records=[unrelated]) is None
    with pytest.raises(ValueError):
        agents.wakes.enqueue(saved['id'], owner='alice', expected_revision=1, provider='monitoring',
            connection_id=profile['id'], event_id=unrelated['id'])
    wake = agents.wakes.enqueue(saved['id'], owner='alice', expected_revision=1, provider='monitoring',
        connection_id=profile['id'], event_id=matching['id'])
    run = agents.claim(saved['id'], {unrelated['id']:'source', matching['id']:'source'},
        source_records=[unrelated, matching], wake_id=wake['id'])
    assert run['observations'] == [matching]
