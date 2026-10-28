"""Managed session limits use the existing money ledger and survive ambiguous retries."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from company_usage import CompanyUsage
from managed_usage import ManagedUsage


def funded(state, cents=500):
    pool=CompanyUsage.openai_pool(state)
    with pool.connect() as db: pool._fund(db,'fixture',cents*10000,'prepaid_customer_funds','fixture')
    pool.set_monthly_limit(0,str(Decimal(cents)/100))
    return pool


def reserve(usage, request='request-1', **extra):
    return usage.reserve(binding_id='binding-1',request_id=request,actor='peter',model='gpt-6-astra',
                         limit_cents=100,**extra)


def test_new_turn_holds_only_additional_session_allowance(tmp_path):
    pool=funded(tmp_path);usage=ManagedUsage(pool)
    first=reserve(usage)
    assert first['limit_cents']==100 and pool.summary()['reserved_usd']=='1.000000'
    usage.created('binding-1','session-1')
    usage.finish('request-1',{'id':'session-1','spend_control':{'limit':100,'consumed':37}},'completed')
    second=usage.reserve(binding_id='binding-1',request_id='request-2',actor='peter',model='gpt-6-astra',limit_cents=137,session_id='session-1')
    assert second['limit_cents']==137 and pool.summary()['reserved_usd']=='1.370000'
    assert pool.summary()['provider_actual_cost_usd']=='0.000000'


def test_recovery_reuses_original_hold_and_bound_actor(tmp_path):
    pool=funded(tmp_path);usage=ManagedUsage(pool);first=reserve(usage)
    usage.created('binding-1','session-1')
    recovered=ManagedUsage(CompanyUsage.agent_pool(tmp_path))
    assert reserve(recovered,recovering=True,session_id='session-1')==first|{'session_id':'session-1'}
    assert pool.summary()['reserved_usd']=='1.000000'
    with pytest.raises(ValueError):
        recovered.reserve(binding_id='binding-1',request_id='request-1',actor='other',model='gpt-6-astra',limit_cents=100,recovering=True,session_id='session-1')
    with pytest.raises(ValueError):reserve(recovered,'new-request',recovering=True,session_id='session-1')


def test_same_request_cannot_extend_budget_or_change_session(tmp_path):
    usage=ManagedUsage(funded(tmp_path));reserve(usage)
    for change in ({'limit_cents':200},{'binding_id':'other'},{'session_id':'foreign'}):
        args=dict(binding_id='binding-1',request_id='request-1',actor='peter',model='gpt-6-astra',limit_cents=100)
        args.update(change)
        with pytest.raises(ValueError):usage.reserve(**args)


def test_missing_funding_is_atomic_and_simultaneous_requests_keep_total_hold(tmp_path):
    pool=funded(tmp_path,100);usage=ManagedUsage(pool)
    def attempt(i):
        try:return usage.reserve(binding_id=f'binding-{i}',request_id=f'request-{i}',actor='peter',model='gpt-6-astra',limit_cents=100)
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(attempt,[1,2]))
    assert sum(result is not None for result in results)==1
    assert pool.summary()['reserved_usd']=='1.000000'


def test_terminal_estimate_and_missing_snapshot_keep_full_hold(tmp_path):
    pool=funded(tmp_path);usage=ManagedUsage(pool);reserve(usage)
    usage.finish('request-1',None,None)
    assert pool.summary()['reserved_usd']=='1.000000'
    usage.created('binding-1','session-1')
    with pytest.raises(ValueError):usage.finish('request-1',{'id':'foreign'},'completed')
    usage.finish('request-1',{'id':'session-1','spend_control':{'limit':100,'consumed':0}},'completed')
    assert pool.summary()['reserved_usd']=='1.000000'
    with pytest.raises(ValueError):reserve(usage,recovering=True,session_id='session-1')


def test_existing_operator_allowance_remains_usable_when_fully_reserved(tmp_path):
    pool=CompanyUsage.native_agent_pool(tmp_path)
    with pool.connect() as db:
        pool._fund(db,'fixture',1_000_000,'operator_demo_budget','fixture',
                   expires_at='2099-01-01T00:00:00+00:00',purpose='native_agent_chat',allowed_model='gpt-6-astra')
    pool.set_monthly_limit(0,'1');usage=ManagedUsage(pool)
    reserve(usage);usage.created('binding-1','session-1')
    usage.finish('request-1',{'id':'session-1'},'completed')
    assert pool.summary()['available_usd']=='0.000000'
    reserve(usage,'request-2',session_id='session-1')
    assert pool.summary()['reserved_usd']=='1.000000'


def test_expired_operator_allocation_cannot_restore_execution(tmp_path):
    pool=CompanyUsage.native_agent_pool(tmp_path)
    with pool.connect() as db:
        pool._fund(db,'fixture',1_000_000,'operator_demo_budget','fixture',
                   expires_at='2099-01-01T00:00:00+00:00',purpose='native_agent_chat',allowed_model='gpt-6-astra')
    pool.set_monthly_limit(0,'1');usage=ManagedUsage(pool);reserve(usage)
    pool._now=lambda:'2099-01-02T00:00:00+00:00'
    with pytest.raises(ValueError):reserve(usage,recovering=True)
    assert pool.summary()['reserved_usd']=='1.000000'


def final_receipt(pool, tmp_path, **changes):
    import json
    from company_usage import OWNER
    record = {'schema_version':1, 'provider':'openai', 'kind':'agents_session_final',
        'pool_scope':pool.company_scope, 'parent_company_scope':pool.summary()['parent_company_scope'],
        'binding_id':'binding-1', 'session_id':'session-1', 'session_closed':True, 'provider_final':True,
        'verified_by':OWNER, 'record_id':'final-1', 'actual_usd':'0.420000',
        'source_reference':'synthetic finalized provider invoice'}
    record.update(changes)
    path = tmp_path / 'final.json'
    path.write_text(json.dumps(record));path.chmod(0o600)
    return path


def test_final_receipt_settles_all_session_holds_once_and_closes_execution(tmp_path):
    pool=funded(tmp_path);usage=ManagedUsage(pool);reserve(usage)
    usage.created('binding-1','session-1')
    usage.finish('request-1',{'id':'session-1','spend_control':{'limit':100,'consumed':37}},'completed')
    usage.reserve(binding_id='binding-1',request_id='request-2',actor='peter',model='gpt-6-astra',
                  limit_cents=137,session_id='session-1')
    proof=final_receipt(pool,tmp_path)
    usage.settle_closed('binding-1',proof)
    first=pool.summary()
    assert first['reserved_usd']=='0.000000' and first['provider_actual_cost_usd']=='0.420000'
    usage.settle_closed('binding-1',proof)
    assert pool.summary()==first
    with pytest.raises(ValueError):reserve(usage,'request-3',session_id='session-1')
    with pytest.raises(ValueError):usage.capacity('binding-1','session-1')
    proof=final_receipt(pool,tmp_path,actual_usd='0.43')
    with pytest.raises(ValueError):usage.settle_closed('binding-1',proof)
    assert pool.summary()==first


@pytest.mark.parametrize('change', [
    {'provider_final':False}, {'session_closed':False}, {'session_id':'foreign'},
    {'pool_scope':'foreign'}, {'parent_company_scope':'foreign'}, {'verified_by':'foreign'},
])
def test_incomplete_or_foreign_final_receipts_preserve_entire_hold(tmp_path,change):
    pool=funded(tmp_path);usage=ManagedUsage(pool);reserve(usage);usage.created('binding-1','session-1')
    proof=final_receipt(pool,tmp_path,**change)
    with pytest.raises(ValueError):usage.settle_closed('binding-1',proof)
    assert pool.summary()['reserved_usd']=='1.000000'
    assert pool.summary()['provider_actual_cost_usd']=='0.000000'


def test_final_operator_receipt_consumes_allowance_without_customer_charge(tmp_path):
    pool=CompanyUsage.native_agent_pool(tmp_path)
    with pool.connect() as db:
        pool._fund(db,'fixture',1_000_000,'operator_demo_budget','fixture',
                   expires_at='2099-01-01T00:00:00+00:00',purpose='native_agent_chat',allowed_model='gpt-6-astra')
    pool.set_monthly_limit(0,'1');usage=ManagedUsage(pool);reserve(usage)
    usage.created('binding-1','session-1')
    usage.settle_closed('binding-1',final_receipt(pool,tmp_path))
    summary=pool.summary()
    assert summary['provider_actual_cost_usd']=='0.420000'
    assert summary['retail_charged_usd']=='0.000000'
    assert summary['reserved_usd']=='0.000000'
    assert summary['available_usd']=='0.580000'


def test_parent_and_role_slots_reserve_atomically_before_any_provider_input(tmp_path):
    pool = funded(tmp_path, 100)
    usage = ManagedUsage(pool)
    roles = [{'id': 'reader', 'model': 'gpt-6-astra', 'limit_cents': 20, 'per_turn': 1}]
    usage.reserve_parent(binding_id='parent', request_id='parent-request', actor='peter',
        model='gpt-6-astra', limit_cents=80, roles=roles)
    assert pool.summary()['reserved_usd'] == '1.000000'
    usage.activate_role(parent_request='parent-request', role_id='reader', ordinal=0,
        binding_id='child', request_id='child-request', actor='peter', model='gpt-6-astra', limit_cents=20)
    assert usage.request('child-request')['limit_cents'] == 20
    assert pool.summary()['reserved_usd'] == '1.000000'
    with pytest.raises(ValueError):
        usage.activate_role(parent_request='parent-request', role_id='reader', ordinal=0,
            binding_id='another', request_id='another-request', actor='peter', model='gpt-6-astra', limit_cents=20)


def test_insufficient_parent_total_rolls_back_every_reservation(tmp_path):
    pool = funded(tmp_path, 99)
    usage = ManagedUsage(pool)
    with pytest.raises(ValueError):
        usage.reserve_parent(binding_id='parent', request_id='parent-request', actor='peter',
            model='gpt-6-astra', limit_cents=80,
            roles=[{'id': 'reader', 'model': 'gpt-6-astra', 'limit_cents': 20, 'per_turn': 1}])
    assert usage.request('parent-request') is None
    assert pool.summary()['reserved_usd'] == '0.000000'


def test_unused_role_slot_release_cannot_release_a_started_or_uncertain_child(tmp_path):
    pool = funded(tmp_path)
    usage = ManagedUsage(pool)
    roles = [{'id': 'reader', 'model': 'gpt-6-astra', 'limit_cents': 20, 'per_turn': 2}]
    usage.reserve_parent(binding_id='parent', request_id='parent-request', actor='peter',
        model='gpt-6-astra', limit_cents=60, roles=roles)
    usage.activate_role(parent_request='parent-request', role_id='reader', ordinal=0,
        binding_id='child', request_id='child-request', actor='peter', model='gpt-6-astra', limit_cents=20)
    usage.created('parent', 'session-parent')
    usage.finish('parent-request', {'id': 'session-parent'}, 'completed')
    usage.release_unused_roles('parent-request')
    assert pool.summary()['reserved_usd'] == '0.800000'
    with pytest.raises(ValueError):
        usage.activate_role(parent_request='parent-request', role_id='reader', ordinal=1,
            binding_id='later', request_id='later-request', actor='peter', model='gpt-6-astra', limit_cents=20)
