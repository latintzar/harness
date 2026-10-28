"""Money reservations persist across chats, months and provider billing delays."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from company_usage import CompanyUsage
from compute_guard import OWNER
from query_budget import QueryBudget
from retail_usage import RetailQueryProvider
import cosmos_inspect


def funded(tmp_path,amount='10'):
    store=CompanyUsage(tmp_path/'state',provider=RetailQueryProvider())
    proof=tmp_path/'state'/'funding.json'
    proof.write_text(json.dumps({'schema_version':1,'record_id':'payment-fixture',
        'company_scope':store.summary()['company_scope'],'kind':'prepaid_customer_funds','amount_usd':amount,
        'settled':True,'user_verified':True,'verified_by':OWNER,'receipt_reference':'operator-receipt-fixture'}))
    proof.chmod(0o600)
    store.import_funding_record(proof)
    store.set_monthly_limit(0,amount)
    return store


def approval(tmp_path):
    directory=tmp_path/'compute';directory.mkdir(mode=0o700)
    path=directory/'query-approval.json';now=datetime.now(timezone.utc)
    path.write_text(json.dumps({'schema_version':1,'approval_id':'demo-query-fixture','purpose':'rocoto-video-live-query',
        'provider':'modal','workspace':'peter-17535','verified_by':OWNER,'user_verified':True,
        'verification_source':'user-confirmation-in-this-chat','verification_note':'Fixture owner reading',
        'verified_at':now.isoformat(),'expires_at':(now+timedelta(hours=1)).isoformat(),
        'net_spend_limit_usd':'0','credit_remaining_usd':'23','reserved_credit_usd':'5','approved_run_gross_usd':'18'}))
    path.chmod(0o600)
    return path


def test_new_company_has_no_funding_and_settings_cannot_mint_money(tmp_path):
    store=CompanyUsage(tmp_path/'company')
    assert store.summary()['available_usd']=='0.000000'
    store.set_monthly_limit(0,'100')
    with pytest.raises(ValueError):store.reserve('1','new-chat')
    assert store.summary()['prepaid_funded_usd']=='0.000000'
    with pytest.raises(ValueError):store.set_monthly_limit(0,'1000')


def test_monthly_cap_edit_preserves_host_pricing_when_price_is_omitted(tmp_path):
    store=funded(tmp_path,'10')
    store.set_monthly_limit(1,'10',retail_multiplier='1.5')
    changed=store.set_monthly_limit(2,'8')
    assert changed['retail_multiplier']=='1.5'
    assert changed['monthly_limit_usd']=='8.000000'


def test_stale_provider_snapshot_cannot_lower_a_reconciled_hold(tmp_path):
    store=funded(tmp_path)
    reservation=store.reserve('2','concurrent-provider-sync')['reservation_id']
    store.mark_started(reservation)
    newer={'status':'completed_with_calculated_reserve','held_usd':'.5',
        'provider_evidence':{'reported_gross_usd':'.04'}}
    older={'status':'completed_with_calculated_reserve','held_usd':'.1',
        'provider_evidence':{'reported_gross_usd':'.02'}}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');store._apply_provider(db,reservation,newer)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');store._apply_provider(db,reservation,older)
    assert store.summary()['reserved_usd']=='0.500000'
    assert store.summary()['provider_reported_cost_usd']=='0.040000'


def test_parallel_chats_share_prepaid_balance_and_idempotent_reservation(tmp_path):
    store=funded(tmp_path,'6')
    def reserve(number):
        try:return store.reserve('2',f'chat-{number}')
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations=[r for r in pool.map(reserve,range(8)) if r]
    assert len(reservations)==3
    assert store.summary()['available_usd']=='0.000000'
    one=reservations[0]
    assert store.reserve('2',one['idempotency_key'])['reservation_id']==one['reservation_id']
    with pytest.raises(ValueError):store.reserve('1',one['idempotency_key'])
    store.release_before_remote(one['reservation_id'])
    assert store.summary()['available_usd']=='2.000000'
    store.mark_started(reservations[1]['reservation_id'])
    with pytest.raises(ValueError):store.release_before_remote(reservations[1]['reservation_id'])
    assert CompanyUsage(store.state).summary()['reserved_usd']=='4.000000'


def test_month_rollover_preserves_money_and_pending_holds(tmp_path,monkeypatch):
    store=funded(tmp_path,'10')
    monkeypatch.setattr(store,'_now',lambda:'2026-10-31T23:00:00+00:00')
    reservation=store.reserve('4','october-work')
    store.mark_started(reservation['reservation_id'])
    monkeypatch.setattr(store,'_now',lambda:'2026-11-01T01:00:00+00:00')
    summary=store.summary()
    assert summary['month']=='2026-11'
    assert summary['available_usd']=='6.000000'
    assert summary['reserved_usd']=='4.000000'
    assert summary['monthly_available_usd']=='6.000000'


def test_demo_imports_real_credit_authority_and_existing_provider_holds_once(tmp_path):
    path=approval(tmp_path);budget=QueryBudget(path)
    rid=budget.reserve('2.5',request_id='existing-query')['reservation_id']
    store=CompanyUsage(tmp_path/'demo',provider=RetailQueryProvider())
    store.import_demo_allowance(path)
    store.import_demo_allowance(path)
    summary=store.summary()
    assert summary['provider_credit_funded_usd']=='18.000000'
    assert summary['prepaid_funded_usd']=='0.000000'
    assert summary['retail_charged_usd']=='0.000000'
    assert summary['available_usd']=='15.500000'
    assert summary['pricing_status']=='founder_passthrough_unconfigured'
    assert summary['reservation_count']==1
    assert rid


def test_provider_estimate_cannot_be_settled_or_double_counted(tmp_path):
    path=approval(tmp_path);budget=QueryBudget(path)
    store=CompanyUsage(tmp_path/'demo',provider=RetailQueryProvider());store.import_demo_allowance(path)
    reservation=store.reserve('2.5','fresh-inspection');key=reservation['reservation_id']
    rid=budget.reserve('2.5',request_id='fresh-provider')['reservation_id']
    store.link_provider(key,path,rid);store.mark_started(key);budget.mark_started(rid)
    budget.complete(rid,runtime_estimate_usd='.02')
    source={'query_approval_path':str(path),'provider_reservation_id':rid}
    with pytest.raises(ValueError):store.settle(key,'.02',source)
    store.sync_query_reservation(key,path,rid)
    assert store.summary()['reserved_usd']=='2.500000'
    proof=path.parent/'settled.json'
    proof.write_text(json.dumps({'provider':'modal','workspace':'peter-17535','reservation_id':rid,
        'settled':True,'verified_by':OWNER,'user_verified':True,'app_id':'ap-fixture123',
        'source_url':'https://modal.com/apps/fixture','gross_usd':'.12'}));proof.chmod(0o600)
    budget.reconcile(rid,provider_evidence_path=proof)
    store.sync_query_reservation(key,path,rid)
    store.settle(key,'.12',source)
    summary=store.summary()
    assert summary['provider_actual_cost_usd']=='0.120000'
    assert summary['available_usd']=='17.880000'
    assert summary['retail_charged_usd']=='0.000000'
    assert summary['reserved_usd']=='0.000000'


def test_funding_records_are_scope_bound_and_cannot_be_changed_or_replayed(tmp_path):
    store=funded(tmp_path)
    path=store.state/'funding.json'
    store.import_funding_record(path)
    assert store.summary()['prepaid_funded_usd']=='10.000000'
    other=CompanyUsage(tmp_path/'other')
    with pytest.raises(ValueError):other.import_funding_record(path)
    data=json.loads(path.read_text());data['amount_usd']='100';path.write_text(json.dumps(data))
    with pytest.raises(ValueError):store.import_funding_record(path)


def test_live_reasoner_blocks_unfunded_company_before_provider_access(tmp_path,monkeypatch):
    path=approval(tmp_path);store=CompanyUsage(tmp_path/'company',provider=RetailQueryProvider())
    output=store.state/'result.json'
    monkeypatch.setattr(cosmos_inspect,'create_worker',lambda *a,**kw:pytest.fail('Unfunded compute admitted'))
    with pytest.raises(ValueError,match='Fund'):
        cosmos_inspect.run_inspection([],[],None,output,query_budget=QueryBudget(path),company_usage=store)
    assert not (path.parent/'query-ledger.json').exists()
    assert store.summary()['reservation_count']==0


def test_live_reasoner_binds_company_hold_and_keeps_estimated_cost_pending(tmp_path,monkeypatch):
    import contextlib
    from types import SimpleNamespace
    path=approval(tmp_path);budget=QueryBudget(path);store=CompanyUsage(tmp_path/'demo',provider=RetailQueryProvider())
    store.import_demo_allowance(path)
    workspace=SimpleNamespace(name='peter-17535',hydrate=lambda:None,
        members=SimpleNamespace(list=lambda:[SimpleNamespace(email=OWNER,role='owner')]))
    monkeypatch.setitem(sys.modules,'modal',SimpleNamespace(
        Workspace=SimpleNamespace(from_context=lambda:workspace),enable_output=contextlib.nullcontext))
    class App:
        app_id='ap-testbound123'
        def run(self,**kw):return contextlib.nullcontext()
    class Call:
        object_id='fc-test123'
        def get(self,**kw):
            assert store.summary()['reserved_usd']=='2.500000'
            return {'timing':{'worker_seconds':1},'requests':[]}
    monkeypatch.setattr(cosmos_inspect,'create_worker',lambda *a: (App(),SimpleNamespace(spawn=lambda *a:Call())))
    monkeypatch.setattr(budget,'reconcile_modal',lambda *a: {'status':'awaiting_provider_evidence'})
    result=cosmos_inspect.run_inspection([],[],None,store.state/'result.json',query_budget=budget,company_usage=store)
    assert result['budget']['company_reservation_id']
    assert store.summary()['reserved_usd']=='2.500000'
    assert store.summary()['provider_actual_cost_usd']=='0.000000'
    assert store.summary()['retail_charged_usd']=='0.000000'


def test_prepaid_pricing_is_pinned_and_month_rollover_resets_usage_only(tmp_path,monkeypatch):
    store=funded(tmp_path,'10');store.set_monthly_limit(1,'10',retail_multiplier='1.5')
    path=approval(tmp_path);budget=QueryBudget(path)
    monkeypatch.setattr(store,'_now',lambda:'2026-10-31T23:00:00+00:00')
    reservation=store.reserve('2','priced-work');rid=budget.reserve('2',request_id='priced-query')['reservation_id']
    store.link_provider(reservation['reservation_id'],path,rid)
    assert reservation['maximum_usd']=='3.000000'
    store.mark_started(reservation['reservation_id']);budget.mark_started(rid)
    budget.complete(rid,runtime_estimate_usd='.01')
    proof=path.parent/'settled.json'
    proof.write_text(json.dumps({'provider':'modal','workspace':'peter-17535','reservation_id':rid,
        'settled':True,'verified_by':OWNER,'user_verified':True,'app_id':'ap-price123',
        'source_url':'https://modal.com/apps/fixture','gross_usd':'.12000001'}));proof.chmod(0o600)
    budget.reconcile(rid,provider_evidence_path=proof)
    store.set_monthly_limit(2,'10',retail_multiplier='2')
    store.sync_query_reservation(reservation['reservation_id'],path,rid)
    october=store.summary()
    assert october['provider_actual_cost_usd']=='0.120001'
    assert october['retail_charged_usd']=='0.180002'
    assert october['monthly_used_usd']=='0.180002'
    monkeypatch.setattr(store,'_now',lambda:'2026-11-01T01:00:00+00:00')
    november=store.summary()
    assert november['monthly_used_usd']=='0.000000'
    assert november['available_usd']=='9.819998'
    assert november['retail_charged_usd']=='0.180002'


def test_provider_calculated_reserve_stays_a_hold_and_same_app_cannot_charge_twice(tmp_path):
    from query_budget import BOUNDED_PROFILE
    path=approval(tmp_path);budget=QueryBudget(path);store=CompanyUsage(tmp_path/'demo',provider=RetailQueryProvider())
    store.import_demo_allowance(path)
    company=store.reserve('2','one');rid=budget.reserve('2',request_id='provider-one')['reservation_id']
    store.link_provider(company['reservation_id'],path,rid)
    store.mark_started(company['reservation_id']);budget.mark_started(rid,resource_profile=BOUNDED_PROFILE)
    budget.complete(rid,runtime_estimate_usd='.01')
    now=datetime.now(timezone.utc).timestamp()
    evidence={'workspace':'peter-17535','app_id':'ap-once123','state':'APP_STATE_STOPPED','running_tasks':0,'report_rows':1,
        'created_at':str(now-1),'stopped_at':str(now),'reported_gross_usd':'.02000001',
        'rates_per_hour':{'H100':'3.95','CPU':'.0473','MemoryGiB':'.008'}}
    budget._apply_provider_evidence(rid,'ap-once123',evidence)
    store.sync_query_reservation(company['reservation_id'],path,rid)
    assert store.summary()['provider_actual_cost_usd']=='0.000000'
    assert store.summary()['provider_reported_cost_usd']=='0.020001'
    assert 0<float(store.summary()['reserved_usd'])<2
    proof=path.parent/'settled.json'
    def settle_provider(key):
        proof.write_text(json.dumps({'provider':'modal','workspace':'peter-17535','reservation_id':key,
            'settled':True,'verified_by':OWNER,'user_verified':True,'app_id':'ap-once123',
            'source_url':'https://modal.com/apps/fixture','gross_usd':'.12'}));proof.chmod(0o600)
        budget.reconcile(key,provider_evidence_path=proof)
    settle_provider(rid);store.sync_query_reservation(company['reservation_id'],path,rid)
    other=store.reserve('2','two');rid2=budget.reserve('2',request_id='provider-two')['reservation_id']
    store.link_provider(other['reservation_id'],path,rid2);store.mark_started(other['reservation_id'])
    budget.mark_started(rid2);budget.complete(rid2,runtime_estimate_usd='.01');settle_provider(rid2)
    with pytest.raises(ValueError,match='once'):store.sync_query_reservation(other['reservation_id'],path,rid2)
    assert store.summary()['provider_actual_cost_usd']=='0.120000'
    assert store.summary()['reserved_usd']=='2.000000'


def openai_funded(tmp_path,amount='10'):
    store=CompanyUsage.openai_pool(tmp_path/'company')
    proof=store.state/'funding.json'
    proof.write_text(json.dumps({'schema_version':1,'record_id':'openai-funding-fixture',
        'company_scope':store.company_scope,'parent_company_scope':store.summary()['parent_company_scope'],
        'provider':'openai','kind':'prepaid_customer_funds','amount_usd':amount,
        'settled':True,'user_verified':True,'verified_by':OWNER,'receipt_reference':'actual-receipt-fixture'}))
    proof.chmod(0o600)
    store.import_funding_record(proof);store.set_monthly_limit(0,amount)
    return store


def openai_receipt(store,key,request_id='request-1',response_id='resp_test1',pricing=None):
    pricing=pricing or {'revision':'verified-test-rates','source_url':'https://developers.openai.com/api/docs/pricing',
        'input_usd_per_million':'2','cached_input_usd_per_million':'.2','cache_write_input_usd_per_million':'2.5',
        'output_usd_per_million':'8','tool_usd_per_call':{}}
    store.bind_openai_request(key,request_id=request_id,model='test-model',pricing=pricing)
    store.mark_started(key)
    record={'schema_version':1,'provider':'openai','kind':'responses_usage',
        'parent_company_scope':store.summary()['parent_company_scope'],'pool_scope':store.company_scope,
        'reservation_id':key,'request_id':request_id,'response_id':response_id,'model':'test-model',
        'status':'completed','usage':{'input_tokens':1000,'cached_input_tokens':200,'cache_write_input_tokens':0,'output_tokens':100},
        'pricing':pricing,'additional_usage':[],'provider_actual_usd':'.00244','received_at':datetime.now(timezone.utc).isoformat()}
    path=store.state/(request_id+'.json');path.write_text(json.dumps(record));path.chmod(0o600)
    return path,record


def test_openai_pool_is_separate_and_permanently_bound_to_parent_company(tmp_path):
    parent=CompanyUsage(tmp_path/'company',provider=RetailQueryProvider());parent.import_demo_allowance(approval(tmp_path))
    api=openai_funded(tmp_path)
    summary=api.summary()
    assert summary['provider']=='openai'
    assert summary['parent_company_scope']==parent.company_scope
    assert summary['company_scope']!=parent.company_scope
    assert summary['prepaid_funded_usd']=='10.000000'
    assert summary['provider_credit_funded_usd']=='0.000000'
    assert parent.summary()['provider_credit_funded_usd']=='18.000000'
    assert parent.summary()['prepaid_funded_usd']=='0.000000'
    reopened=CompanyUsage(api.state)
    assert reopened.summary()['parent_company_scope']==parent.company_scope
    with pytest.raises(ValueError):reopened.import_demo_allowance(tmp_path/'compute/query-approval.json')
    other=CompanyUsage.openai_pool(tmp_path/'other')
    with pytest.raises(ValueError):other.import_funding_record(api.state/'funding.json')


def test_openai_completed_usage_settles_exactly_once_and_keeps_immutable_receipt(tmp_path):
    store=openai_funded(tmp_path)
    key=store.reserve('1','chat-1-request-1')['reservation_id']
    path,record=openai_receipt(store,key)
    store.settle_openai(key,path)
    store.settle_openai(key,path)
    summary=store.summary()
    assert summary['provider_actual_cost_usd']=='0.002440'
    assert summary['retail_charged_usd']=='0.002440'
    assert summary['available_usd']=='9.997560'
    assert summary['reserved_usd']=='0.000000'
    record['received_at']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError,match='immutable'):store.settle_openai(key,path)
    with store.connect() as db:
        data=json.loads(db.execute("SELECT data FROM events WHERE kind='settled'").fetchone()['data'])
    assert data['source']['response_id']=='resp_test1'
    assert data['source']['pricing']['revision']=='verified-test-rates'
    assert data['source']['usage']['cached_input_tokens']==200


def test_migrated_api_pool_retains_funds_hold_and_original_usage_receipt(tmp_path):
    store=openai_funded(tmp_path)
    key=store.reserve('1','before-migration')['reservation_id']
    receipt,_=openai_receipt(store,key)
    before=store.summary()
    target=tmp_path/'staging'/'company'
    shutil.copytree(tmp_path/'company',target)
    moved=CompanyUsage.openai_pool(target)
    assert moved.summary()==before
    moved.import_funding_record(moved.state/'funding.json')
    moved.settle_openai(key,moved.state/receipt.name)
    assert moved.summary()['prepaid_funded_usd']=='10.000000'
    assert moved.summary()['provider_actual_cost_usd']=='0.002440'
    assert moved.summary()['available_usd']=='9.997560'
    assert moved.summary()['reserved_usd']=='0.000000'


@pytest.mark.parametrize('change',[
    {'status':'in_progress'}, {'pool_scope':'wrong-company'}, {'parent_company_scope':'wrong-parent'},
    {'request_id':'other-request'}, {'model':'another-model'}, {'provider_actual_usd':'.001'},
    {'usage':{'input_tokens':1000,'cached_input_tokens':1001,'output_tokens':100}},
    {'usage':{'input_tokens':True,'cached_input_tokens':0,'output_tokens':100}},
    {'pricing':{'revision':'changed','source_url':'https://developers.openai.com/api/docs/pricing',
        'input_usd_per_million':'1','cached_input_usd_per_million':'.2','cache_write_input_usd_per_million':'2.5',
        'output_usd_per_million':'8','tool_usd_per_call':{}}},
])
def test_openai_uncertain_or_mismatched_evidence_preserves_maximum_hold(tmp_path,change):
    store=openai_funded(tmp_path);key=store.reserve('1','request')['reservation_id']
    path,record=openai_receipt(store,key);record.update(change);path.write_text(json.dumps(record))
    with pytest.raises(ValueError):store.settle_openai(key,path)
    assert store.summary()['reserved_usd']=='1.000000'
    assert store.summary()['provider_actual_cost_usd']=='0.000000'


def test_openai_same_response_cannot_settle_two_concurrent_reservations(tmp_path):
    store=openai_funded(tmp_path)
    keys=[store.reserve('1',f'request-{i}')['reservation_id'] for i in range(2)]
    receipts=[openai_receipt(store,key,f'request-{i}','resp_shared')[0] for i,key in enumerate(keys)]
    def settle(pair):
        try:store.settle_openai(*pair);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(settle,zip(keys,receipts)))==1
    assert store.summary()['reserved_usd']=='1.000000'
    assert store.summary()['provider_actual_cost_usd']=='0.002440'


def test_openai_request_binding_and_retail_price_are_pinned_before_remote(tmp_path):
    store=openai_funded(tmp_path);store.set_monthly_limit(1,'10',retail_multiplier='1.5')
    key=store.reserve('1','request')['reservation_id']
    path,record=openai_receipt(store,key)
    with pytest.raises(ValueError):
        store.bind_openai_request(key,request_id='changed',model='test-model',pricing=record['pricing'])
    store.set_monthly_limit(2,'10',retail_multiplier='2')
    store.settle_openai(key,path)
    assert store.summary()['retail_charged_usd']=='0.003660'


def test_openai_cache_writes_replace_ordinary_input_and_measured_web_cost_is_added(tmp_path):
    store=openai_funded(tmp_path);key=store.reserve('1','cache-and-web')['reservation_id']
    pricing={'revision':'verified-test-rates','source_url':'https://developers.openai.com/api/docs/pricing',
        'input_usd_per_million':'2','cached_input_usd_per_million':'.2',
        'cache_write_input_usd_per_million':'2.5','output_usd_per_million':'8',
        'tool_usd_per_call':{'web_search':'.01'}}
    path,record=openai_receipt(store,key,pricing=pricing)
    record['usage']['cache_write_input_tokens']=100
    record['additional_usage']=[{'kind':'web_search','quantity':2,'unit_usd':'.01'}]
    record['provider_actual_usd']='.02249'
    # An incomplete terminal response can still carry the complete provider usage.
    record['status']='incomplete'
    path.write_text(json.dumps(record))
    store.settle_openai(key,path)
    assert store.summary()['provider_actual_cost_usd']=='0.022490'
    assert store.summary()['reserved_usd']=='0.000000'


def test_openai_unpriced_tools_and_overlapping_token_buckets_keep_hold(tmp_path):
    store=openai_funded(tmp_path);key=store.reserve('1','uncertain-extra')['reservation_id']
    path,record=openai_receipt(store,key)
    record['additional_usage']=[{'kind':'web_search','quantity':1,'unit_usd':'.01'}]
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):store.settle_openai(key,path)
    record['additional_usage']=[]
    record['usage']['cache_write_input_tokens']=801
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):store.settle_openai(key,path)
    assert store.summary()['reserved_usd']=='1.000000'


def test_openai_remote_attempt_requires_pinned_request_and_ambiguous_call_keeps_hold(tmp_path):
    store=openai_funded(tmp_path);key=store.reserve('1','unbound-request')['reservation_id']
    with pytest.raises(ValueError):store.mark_started(key)
    store.release_before_remote(key)
    assert store.summary()['reserved_usd']=='0.000000'
    next_key=store.reserve('1','started-request')['reservation_id']
    openai_receipt(store,next_key)
    with pytest.raises(ValueError):store.release_before_remote(next_key)
    assert store.summary()['reserved_usd']=='1.000000'
    assert store.summary()['provider_actual_cost_usd']=='0.000000'


@pytest.mark.parametrize('input_tokens,expected,tier',[(1000,'.00244','standard'),(1001,'.004884','long_context')])
def test_openai_long_context_rate_uses_actual_input_and_strict_threshold(tmp_path,input_tokens,expected,tier):
    store=openai_funded(tmp_path);key=store.reserve('1','tiered-request')['reservation_id']
    pricing={'revision':'verified-tiered-rates','source_url':'https://developers.openai.com/api/docs/pricing',
        'input_usd_per_million':'2','cached_input_usd_per_million':'.2',
        'cache_write_input_usd_per_million':'2.5','output_usd_per_million':'8','tool_usd_per_call':{},
        'long_context':{'threshold_tokens':1000,'input_usd_per_million':'4',
            'cached_input_usd_per_million':'.4','cache_write_input_usd_per_million':'5',
            'output_usd_per_million':'16'}}
    path,record=openai_receipt(store,key,pricing=pricing)
    record['usage']['input_tokens']=input_tokens;record['provider_actual_usd']=expected
    path.write_text(json.dumps(record));store.settle_openai(key,path)
    assert float(store.summary()['provider_actual_cost_usd'])==float(expected)
    with store.connect() as db:
        source=json.loads(db.execute("SELECT data FROM events WHERE kind='settled'").fetchone()['data'])['source']
    assert source['pricing_tier']==tier


def test_openai_pool_cannot_admit_modal_billing_links(tmp_path):
    store=openai_funded(tmp_path)
    key=store.reserve('1','api-only')['reservation_id']
    path=approval(tmp_path);budget=QueryBudget(path)
    provider=budget.reserve('1',request_id='modal-only')['reservation_id']
    with pytest.raises(ValueError,match='Modal'):
        store.link_provider(key,path,provider)
    assert store.summary()['reserved_usd']=='1.000000'
