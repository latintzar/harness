"""Actual scoped MCP authority and real funding, with an offline provider snapshot."""
import asyncio
from types import SimpleNamespace
import pytest
from host import Admission
from managed_policy import ManagedPolicy
from managed_mcp import ManagedMCPHost
from test_managed_usage import funded


@pytest.fixture
def anyio_backend():return 'asyncio'


def fixture(tmp_path):
    pool=funded(tmp_path)
    registry=SimpleNamespace(read_names=lambda transport: frozenset({'workspace_context'}))
    agent=SimpleNamespace(state=tmp_path,children={'chat':{'id':'chat','owner_principal_id':'peter','dataset':'company'}},
        active={},tool_registry=registry,automation_store=None,vertical=None,_authority_principal=lambda chat:'peter')
    agent.managed_mcp_host=ManagedMCPHost(tmp_path,public_origin='https://harness.example.test')
    run={'cancel':asyncio.Event(),'deadline':__import__('time').monotonic()+30,'provider_quiesced':True}
    agent.active['chat']=run
    return agent,run,ManagedPolicy(),pool


@pytest.mark.anyio
async def test_policy_uses_managed_hosted_core_and_funded_scoped_children(tmp_path):
    agent,run,policy,pool=fixture(tmp_path);admission=Admission(lambda:True)
    base=policy.base_configuration(agent,'chat',[],'Be helpful')
    assert base['environment']['type']=='openai_hosted'
    assert 'authorization' not in str(base)
    assert base['agent']['instructions'].startswith('Be helpful')
    assert 'Parent function tools belong to the coordinator.' in base['agent']['instructions']
    assert 'advertised Rocoto MCP namespace' in base['agent']['instructions']
    assert 'mcp__rocoto__get_harness' in base['agent']['instructions']
    async with policy.turn(agent,'chat',[],'Be helpful',session_id=None,request_id='request-1',binding_id='binding-1',admission=admission,run=run) as lease:
        assert lease.configuration['spend_control']=={'limit':100}
        assert lease.configuration['agent']['multi_agent']['enabled']
        assert lease.mcp.current()
        lease.mcp.created('session-1')
        lease.revoke()
        assert not lease.mcp.current()
        await lease.finish({'id':'session-1','spend_control':{'limit':100,'consumed':4}},terminal_status='completed')
    assert pool.summary()['reserved_usd']=='1.000000'


@pytest.mark.anyio
async def test_revoked_actor_admits_neither_money_nor_mcp(tmp_path):
    agent,run,policy,pool=fixture(tmp_path)
    with pytest.raises(ValueError):
        async with policy.turn(agent,'chat',[],'Be helpful',session_id=None,request_id='request-1',binding_id='binding-1',admission=Admission(lambda:False),run=run):pass
    assert pool.summary()['reserved_usd']=='0.000000'


@pytest.mark.anyio
async def test_next_turn_increases_only_consumed_cents_and_steering_preserves_primary(tmp_path):
    agent,run,policy,pool=fixture(tmp_path);admission=Admission(lambda:True)
    async with policy.turn(agent,'chat',[],'Be helpful',session_id=None,request_id='request-1',binding_id='binding-1',admission=admission,run=run) as first:
        first.mcp.created('session-1');first.revoke()
        await first.finish({'id':'session-1','spend_control':{'limit':100,'consumed':20}},terminal_status='completed')
    async def snapshot(session_id):return {'id':session_id,'spend_control':{'limit':100,'consumed':20}}
    run['managed_client']=SimpleNamespace(get_session=snapshot)
    async with policy.turn(agent,'chat',[],'Be helpful',session_id='session-1',request_id='request-2',binding_id='binding-1',admission=admission,run=run) as second:
        run['managed_lease']=second
        assert second.configuration['spend_control']['limit']==120
        async with policy.turn(agent,'chat',[],'Be helpful',session_id='session-1',request_id='steer-1',binding_id='binding-1',admission=admission,run=run,steering=True) as steer:
            assert second.mcp.current()
            steer.revoke()
            assert second.mcp.current()
        second.revoke()
    assert pool.summary()['reserved_usd']=='1.200000'


@pytest.mark.anyio
async def test_monitored_turn_persists_target_deadline_and_steering_shares_it(tmp_path):
    import time
    from managed_usage import ManagedUsage
    agent, run, _, pool = fixture(tmp_path)
    run['deadline'] = time.monotonic() + 900
    policy = ManagedPolicy(spending_mode='monitored')
    admission = Admission(lambda: True)
    async with policy.turn(agent, 'chat', [], 'Help', session_id=None, request_id='first',
            binding_id='binding', admission=admission, run=run) as first:
        run['managed_lease'] = first
        first.created('session')
        assert 'spend_control' not in first.configuration
        assert time.time() < first.monitor.deadline_at <= time.time() + 300
        before = first.monitor.receipt()
        async with policy.turn(agent, 'chat', [], 'Help', session_id='session', request_id='steer',
                binding_id='binding', admission=admission, run=run, steering=True) as steering:
            assert steering.monitor is first.monitor
            assert 'spend_control' not in steering.configuration
            assert steering.monitor.receipt() == before
        assert ManagedUsage(pool).monitor('first') == before
        first.revoke(disconnected=True)
    assert pool.summary()['reserved_usd'] == '1.000000'
    run['deadline'] = time.monotonic() + 900
    async with policy.turn(agent, 'chat', [], 'Help', session_id='session', request_id='first',
            binding_id='binding', admission=admission, run=run, recovering=True) as recovered:
        assert recovered.monitor.deadline_at == before['deadline_at']
        assert pool.summary()['reserved_usd'] == '1.000000'
        recovered.revoke()


@pytest.mark.anyio
async def test_monitored_next_turn_reserves_new_target_and_uses_preinput_baseline(tmp_path):
    agent, run, _, pool = fixture(tmp_path)
    policy = ManagedPolicy(spending_mode='monitored')
    admission = Admission(lambda: True)
    async with policy.turn(agent, 'chat', [], 'Help', session_id=None, request_id='first',
            binding_id='binding', admission=admission, run=run) as first:
        first.created('session'); first.revoke()
        await first.finish({'id': 'session', 'usage': None}, terminal_status='completed')
    async def snapshot(session_id):
        return {'id': session_id, 'usage': {'input_tokens': 0, 'output_tokens': 10_000}}
    run['managed_client'] = SimpleNamespace(get_session=snapshot)
    async with policy.turn(agent, 'chat', [], 'Help', session_id='session', request_id='second',
            binding_id='binding', admission=admission, run=run) as second:
        assert second.monitor.baseline == 50
        assert second.monitor.target_cents == 100
        second.revoke()
    assert pool.summary()['reserved_usd'] == '2.000000'
    assert pool.summary()['provider_actual_cost_usd'] == '0.000000'


@pytest.mark.anyio
async def test_expired_monitored_recovery_cannot_renew_deadline_or_authority(tmp_path, monkeypatch):
    import time
    from managed_usage import ManagedUsage
    agent, run, _, pool = fixture(tmp_path)
    policy = ManagedPolicy(spending_mode='monitored')
    admission = Admission(lambda: True)
    async with policy.turn(agent, 'chat', [], 'Help', session_id=None, request_id='first',
            binding_id='binding', admission=admission, run=run) as first:
        first.created('session')
        first.revoke(disconnected=True)
        deadline = first.monitor.deadline_at
    monkeypatch.setattr('managed_policy.time.time', lambda: deadline + 1)
    run['deadline'] = time.monotonic() + 900
    run.pop('spend_monitor')  # A fresh host has only the durable funded request.
    with pytest.raises(ValueError, match='stopped monitored'):
        async with policy.turn(agent, 'chat', [], 'Help', session_id='session', request_id='first',
                binding_id='binding', admission=admission, run=run, recovering=True):
            pytest.fail('Expired recovery gained execution authority')
    assert ManagedUsage(pool).monitor('first')['deadline_at'] == deadline
    assert pool.summary()['reserved_usd'] == '1.000000'
    assert not first.mcp.current()
