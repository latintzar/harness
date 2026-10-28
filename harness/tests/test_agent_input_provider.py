"""Generic saved agents run through the same store and scheduler with no retail source."""
from pathlib import Path
import os
import subprocess
import sys


def test_scheduler_imports_and_runs_without_a_retail_import_path(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r'''
import asyncio, json, sys, time
import layout
layout.install('bare')
from background_agents import AgentStore, AgentScheduler
class Agent:
    def __init__(self):
        self.children = {}; self.child_tasks = {}; self.active = {}; self.turns = []
    def _save_child(self, child):
        self.children[child['id']] = child
    async def turn(self, message, chat_id, dataset, **options):
        self.turns.append((message, chat_id, dataset, options))
        self.children[chat_id]['messages'].extend([
            {'role':'user','content':message}, {'role':'assistant','content':'Prepared the workspace summary.'}])
        yield {'type':'done','status':'completed'}
async def proof():
    store = AgentStore(sys.argv[1])
    agent = Agent()
    scheduler = AgentScheduler(store, agent, slots=asyncio.Semaphore(1))
    saved = store.create({'title':'Workspace summary','instruction':'Summarize our workspace.',
        'dataset':'workspace'}, ['workspace'], owner_principal_id='owner')
    assert saved['input_kind'] == 'none'
    store.activate(saved['id'], time.time()+60)
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks.values())
    completed = store.get(saved['id'])
    assert completed['status'] == 'completed' and len(agent.turns) == 1
    assert completed['last_run']['source_count'] == 0
    assert completed['last_run']['owner_principal_id'] == 'owner'
    scheduled = store.create({'title':'Inbox summary','instruction':'Summarize incoming updates.',
        'dataset':'workspace','mode':'scheduled','interval_seconds':60,'max_runs':2},
        ['workspace'],owner_principal_id='owner')
    store.activate(scheduled['id'],time.time()+60)
    wake=store.wakes.enqueue(scheduled['id'],owner='owner',expected_revision=1,
        provider='email',connection_id='inbox',event_id='reply-1',kind='channel_reply',
        sender_id='sender-1',channel_id='thread-1',text='The delivery arrived.')
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks.values())
    assert len(agent.turns)==2 and agent.turns[-1][0]=='The delivery arrived.'
    assert 'email' in agent.turns[-1][3]['model_context']
    assert agent.children[scheduled['chat_id']]['messages'][0]['origin']['event_id']=='reply-1'
    assert store.wakes.get(wake['id'],owner='owner')['status']=='completed'
    assert 'retail' not in {part for entry in sys.path for part in entry.split('/')}
    assert not {'monitoring','watch_screening','source_visibility','cosmos_inspect'} & sys.modules.keys()
    await scheduler.close()
asyncio.run(proof())
'''
    env = {**os.environ, 'ROCOTO_VERTICAL':'bare', 'PYTHONPATH':str(root),
           'PYTHONDONTWRITEBYTECODE':'1'}
    env.pop('ROCOTO_PACK', None)
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'state')],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_installation_is_sealed_before_recovery(tmp_path):
    import pytest
    from agent_inputs import NoInputProvider
    from background_agents import AgentStore
    store = AgentStore(tmp_path)
    provider = NoInputProvider()
    store.install_inputs(provider)
    assert store.definition_model is provider.definition_model
    with pytest.raises(ValueError, match='once'):
        store.install_inputs(NoInputProvider())
    restarted = AgentStore(tmp_path)
    restarted.recover()
    with pytest.raises(ValueError, match='before scheduler recovery'):
        restarted.install_inputs(NoInputProvider())


def test_no_source_provider_rejects_undeclared_input_and_source_payloads(tmp_path):
    import pytest
    import time
    from background_agents import AgentStore
    store = AgentStore(tmp_path)
    definition = {'title':'Summary', 'instruction':'Summarize.', 'dataset':'company'}
    with pytest.raises(ValueError, match='declared input provider'):
        store.create({**definition, 'input_kind':'camera_footage'}, ['company'])
    saved = store.create(definition, ['company'], owner_principal_id='owner')
    store.activate(saved['id'], time.time()+60)
    with pytest.raises(ValueError, match='admit source inputs'):
        store.claim(saved['id'], {'source':'recording'})
    assert store.get(saved['id'])['runs_started'] == 0


def test_provider_metadata_cannot_replace_run_authority(tmp_path):
    import pytest
    import time
    from agent_inputs import NoInputProvider, InputSelection
    from background_agents import AgentStore
    class InvalidInput(NoInputProvider):
        def select(self, *args):
            return InputSelection(fields={'owner_principal_id':'another-person'}, allow_empty=True)
    store = AgentStore(tmp_path, input_provider=InvalidInput())
    saved = store.create({'title':'Summary', 'instruction':'Summarize.', 'dataset':'company'},
                         ['company'], owner_principal_id='owner')
    store.activate(saved['id'], time.time()+60)
    with pytest.raises(ValueError, match='execution authority'):
        store.claim(saved['id'], {})
    assert store.get(saved['id'])['runs_started'] == 0
    assert store.get(saved['id'])['runs'] == []


def test_declared_resource_profile_keeps_shared_budget_and_grant_limits(tmp_path):
    import pytest
    import time
    from decimal import Decimal
    from agent_inputs import NoInputProvider
    from background_agents import AgentStore
    class BudgetedInput(NoInputProvider):
        def resource_ceiling(self):return Decimal('0.25')
        def resource_allowance(self, definition):return 1
    store = AgentStore(tmp_path, input_provider=BudgetedInput())
    saved = store.create({'title':'Summary', 'instruction':'Summarize.', 'dataset':'company'},
                         ['company'], owner_principal_id='owner')
    store.activate(saved['id'], time.time()+60)
    run = store.claim(saved['id'], {})
    with pytest.raises(ValueError, match='resource profile'):
        store.admit_resource(run['id'], '0.26')
    store.admit_resource(run['id'], '0.20')
    with pytest.raises(ValueError, match='allowance is used'):
        store.admit_resource(run['id'], '0.01')
    detail = store.run_report(saved['id'], run['id'])
    assert detail['resource_uses'] == 1
    assert detail['credit_committed_ceiling_usd'] == '0.20'
    store.pause(saved['id'])
    with pytest.raises(ValueError, match='grant has ended'):
        store.admit_resource(run['id'], '0.01')


def test_no_source_jobs_keep_cadence_revision_recovery_and_owner_bound_wakes(tmp_path):
    import pytest
    import time
    from background_agents import AgentStore
    store = AgentStore(tmp_path)
    now = time.time()
    saved = store.create({'title':'Summary', 'instruction':'Summarize.', 'dataset':'company',
                          'mode':'scheduled', 'interval_seconds':60, 'max_runs':3},
                         ['company'], owner_principal_id='owner')
    store.activate(saved['id'], now+600, now=now)
    first = store.claim(saved['id'], {}, now=now)
    store.finish(first['id'], 'completed', now=now)
    assert store.claim(saved['id'], {}, now=now+1) is None
    with pytest.raises(ValueError, match='owning principal'):
        store.wakes.enqueue(saved['id'], owner='another', expected_revision=1,
                            provider='email', connection_id='mail', event_id='message-1')
    with pytest.raises(ValueError, match='reviewed agent revision'):
        store.wakes.enqueue(saved['id'], owner='owner', expected_revision=2,
                            provider='email', connection_id='mail', event_id='message-1')
    wake = store.wakes.enqueue(saved['id'], owner='owner', expected_revision=1,
        provider='email', connection_id='mail', event_id='message-1', text='The figures arrived.')
    second = store.claim(saved['id'], {}, now=now+1, wake_id=wake['id'])
    assert second['wake']['text'] == 'The figures arrived.'
    assert second['source_count'] == 0
    store.recover()
    assert store.get(saved['id'])['status'] == 'paused'
    assert store.run_report(saved['id'], second['id'])['status'] == 'interrupted'
    assert store.wakes.get(wake['id'], owner='owner')['status'] == 'interrupted'


def test_only_declared_classes_can_be_saved_or_resumed(tmp_path):
    import pytest
    import time
    from background_agents import AgentStore
    body={'title':'Plan work', 'instruction':'Plan the workspace work.', 'dataset':'company'}
    bare=AgentStore(tmp_path/'bare')
    with pytest.raises(ValueError, match='class declared'):
        bare.create({**body, 'agent_kind':'tracker'}, ['company'])
    classes=[{'kind':'planner','authority':'granted_effects','default':True}]
    store=AgentStore(tmp_path/'business', agent_classes=classes)
    saved=store.create(body,['company'],owner_principal_id='owner')
    assert saved['agent_kind']=='planner'
    store.activate(saved['id'],time.time()+600)
    running=store.claim(saved['id'],{})
    # A newly selected descriptor excludes the old class; recovery closes its grant.
    restarted=AgentStore(tmp_path/'business')
    restarted.recover()
    assert restarted.get(saved['id'])['status']=='needs_attention'
    assert restarted.run_report(saved['id'],running['id'])['status']=='interrupted'
    with pytest.raises(ValueError,match='class declared'):
        restarted.activate(saved['id'],time.time()+600)
    with pytest.raises(ValueError,match='class declared'):
        restarted.update(saved['id'],{**body,'agent_kind':'planner'},['company'])
    with pytest.raises(ValueError,match='class declared'):
        restarted.claim(saved['id'],{})
    with pytest.raises(ValueError,match='before scheduler recovery'):
        restarted.install_classes(classes)
