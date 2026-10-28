"""Operator renewals retain historical spending and retire prior execution authority."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

from company_usage import CompanyUsage, OWNER
from managed_usage import ManagedUsage

NOW='2026-10-13T12:00:00+00:00'


def fixture(tmp_path, monkeypatch, *, managed=False):
    pool=CompanyUsage.openai_pool(tmp_path/'workspace')
    monkeypatch.setattr(pool,'_now',lambda:'2026-10-01T00:00:00+00:00')
    record={'schema_version':1,'record_id':'original','kind':'operator_demo_budget',
        'environment_id':'retail-demo','provider':'openai','company_scope':pool.company_scope,
        'parent_company_scope':pool.summary()['parent_company_scope'],'verified_by':OWNER,
        'user_authorized':True,'authorization_reference':'Synthetic initial allowance',
        'amount_usd':'1','expires_at':'2026-10-02T00:00:00+00:00'}
    if managed:record.update(purpose='managed_agent_chat',allowed_model='gpt-6-astra')
    path=tmp_path/'original.json';path.write_text(json.dumps(record));path.chmod(0o600)
    pool.import_operator_budget(path,lifecycle='development',environment_id='retail-demo')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    replacement={**record,'record_id':'renewal','kind':'operator_demo_budget_renewal',
        'previous_record_sha256':digest,'amount_usd':'5','expires_at':'2026-10-20T12:00:00+00:00',
        'purpose':'managed_agent_chat','allowed_model':'gpt-6-astra'}
    renewal=tmp_path/'renewal.json';renewal.write_text(json.dumps(replacement));renewal.chmod(0o600)
    return pool,renewal,digest


def renew(pool,path,digest,**extra):
    return pool.renew_operator_budget(path,lifecycle='development',environment_id='retail-demo',
        expected_prior_record_sha256=digest,**extra)


def rows(pool):
    with pool.connect() as db:return [tuple(r) for r in db.execute('SELECT * FROM events ORDER BY sequence')]


def test_renewal_preserves_original_events_and_spent_total_without_adding_funds(tmp_path,monkeypatch):
    pool,path,digest=fixture(tmp_path,monkeypatch)
    old=pool.reserve('.001','catalog-original')
    with pool.connect() as db:
        pool._append(db,'settled:'+old['reservation_id'],'settled',512,old['reservation_id'],
            provider_actual=512,retail_charge=0)
    before=rows(pool);monkeypatch.setattr(pool,'_now',lambda:NOW)
    result=renew(pool,path,digest)
    assert rows(pool)[:-1]==before
    assert result['operator_demo_budget_usd']=='5.000000'
    assert result['available_usd']=='4.999488'
    assert result['operator_purpose']=='managed_agent_chat' and result['operator_model']=='gpt-6-astra'
    assert result['monthly_limit_usd']=='1.000000'
    assert result['prepaid_funded_usd']=='0.000000'
    assert renew(pool,path,digest)==result and len(rows(pool))==len(before)+1
    with pytest.raises(ValueError):pool.mark_started(old['reservation_id'])
    pool.set_monthly_limit(result['revision'],'5')
    usage=ManagedUsage(pool)
    admitted=usage.reserve(binding_id='fresh',request_id='new',actor='peter',model='gpt-6-astra',limit_cents=100)
    assert admitted['reservation_id'] and pool.summary()['reserved_usd']=='1.000000'
    assert renew(pool,path,digest)['reserved_usd']=='1.000000' # exact replay stays read-only
    with pytest.raises(ValueError):
        usage.reserve(binding_id='wrong-model',request_id='wrong-model',actor='peter',model='other',limit_cents=100)


@pytest.mark.parametrize('status',['reserved','running','awaiting_provider_final'])
def test_unresolved_old_money_blocks_renewal_even_if_reported_hold_is_zero(tmp_path,monkeypatch,status):
    pool,path,digest=fixture(tmp_path,monkeypatch)
    r=pool.reserve('.1','old-unresolved')
    with pool.connect() as db:
        if status=='running':pool._append(db,'started-test','started',reservation_id=r['reservation_id'])
        if status=='awaiting_provider_final':pool._append(db,'hold-test','provider_hold',0,r['reservation_id'])
    before=rows(pool);monkeypatch.setattr(pool,'_now',lambda:NOW)
    with pytest.raises(ValueError):renew(pool,path,digest)
    assert rows(pool)==before


@pytest.mark.parametrize('change',[
    {'company_scope':'foreign'},{'parent_company_scope':'foreign'},{'provider':'other'},
    {'environment_id':'demo'},{'previous_record_sha256':'0'*64},{'purpose':'catalog_image_inspection'},
    {'allowed_model':None},{'amount_usd':'20.000001'},{'expires_at':'2026-10-13T11:00:00+00:00'},
    {'verified_by':'other'},{'user_authorized':False},
])
def test_foreign_or_incomplete_renewals_leave_original_authority_unchanged(tmp_path,monkeypatch,change):
    pool,path,digest=fixture(tmp_path,monkeypatch)
    record=json.loads(path.read_text());record.update(change);path.write_text(json.dumps(record))
    before=rows(pool);monkeypatch.setattr(pool,'_now',lambda:NOW)
    with pytest.raises(ValueError):renew(pool,path,digest)
    assert rows(pool)==before


def test_active_authority_and_modified_replay_are_refused(tmp_path,monkeypatch):
    pool,path,digest=fixture(tmp_path,monkeypatch)
    with pytest.raises(ValueError):renew(pool,path,digest)
    monkeypatch.setattr(pool,'_now',lambda:NOW);renew(pool,path,digest)
    record=json.loads(path.read_text());record['amount_usd']='6';path.write_text(json.dumps(record))
    before=rows(pool)
    with pytest.raises(ValueError):renew(pool,path,digest)
    assert rows(pool)==before


def test_concurrent_replacements_compare_the_same_prior_authorization(tmp_path,monkeypatch):
    pool,path,digest=fixture(tmp_path,monkeypatch);monkeypatch.setattr(pool,'_now',lambda:NOW)
    other=tmp_path/'other.json';record=json.loads(path.read_text());record['record_id']='other'
    other.write_text(json.dumps(record));other.chmod(0o600)
    def attempt(path):
        try:renew(pool,path,digest);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(attempt,[path,other]))
    assert sum(results)==1
    assert pool.summary()['operator_demo_budget_usd']=='5.000000'


def test_closed_managed_requests_stay_closed_after_renewal_and_new_roles_use_current_authority(tmp_path,monkeypatch):
    pool,path,digest=fixture(tmp_path,monkeypatch,managed=True);usage=ManagedUsage(pool)
    usage.reserve(binding_id='old',request_id='old-request',actor='peter',model='gpt-6-astra',limit_cents=100)
    usage.created('old','old-session')
    usage.finish('old-request',{'id':'old-session'},'completed')
    final={'schema_version':1,'provider':'openai','kind':'agents_session_final',
        'pool_scope':pool.company_scope,'parent_company_scope':pool.summary()['parent_company_scope'],
        'binding_id':'old','session_id':'old-session','session_closed':True,'provider_final':True,
        'verified_by':OWNER,'record_id':'settled-old','actual_usd':'.000512',
        'source_reference':'Synthetic final provider receipt'}
    receipt=tmp_path/'final.json';receipt.write_text(json.dumps(final));receipt.chmod(0o600)
    usage.settle_closed('old',receipt)
    monkeypatch.setattr(pool,'_now',lambda:NOW);renew(pool,path,digest)
    for request,recovering in [('old-request',True),('attempt-to-reopen-session',False)]:
        with pytest.raises(ValueError):
            usage.reserve(binding_id='old',request_id=request,actor='peter',model='gpt-6-astra',
                limit_cents=100,session_id='old-session',recovering=recovering)
    pool.set_monthly_limit(pool.summary()['revision'],'5')
    role={'id':'reader','model':'gpt-6-astra','limit_cents':20,'per_turn':1}
    usage.reserve_parent(roles=[role],binding_id='new',request_id='new-request',actor='peter',
        model='gpt-6-astra',limit_cents=80)
    usage.activate_role(parent_request='new-request',role_id='reader',ordinal=0,binding_id='child',
        request_id='child-request',actor='peter',model='gpt-6-astra',limit_cents=20)
    assert pool.summary()['reserved_usd']=='1.000000'
    assert pool.summary()['available_usd']=='3.999488'


def test_zero_hold_with_unclosed_managed_session_still_blocks_renewal(tmp_path,monkeypatch):
    pool,path,digest=fixture(tmp_path,monkeypatch,managed=True);usage=ManagedUsage(pool)
    request=usage.reserve(binding_id='old',request_id='old-request',actor='peter',model='gpt-6-astra',limit_cents=100)
    # An incomplete legacy reconciliation may clear a hold without closing its provider session.
    with pool.connect() as db:
        pool._append(db,'settled-legacy','settled',0,request['reservation_id'],provider_actual=0,retail_charge=0)
    monkeypatch.setattr(pool,'_now',lambda:NOW)
    assert pool.summary()['reserved_usd']=='0.000000'
    before=rows(pool)
    with pytest.raises(ValueError,match='managed sessions'):renew(pool,path,digest)
    assert rows(pool)==before


def test_new_responses_reservation_starts_only_under_current_model_and_expiry(tmp_path,monkeypatch):
    pool,path,digest=fixture(tmp_path,monkeypatch);monkeypatch.setattr(pool,'_now',lambda:NOW)
    renew(pool,path,digest)
    for key,model in [('current','gpt-6-astra'),('wrong','image-model'),('expires','gpt-6-astra')]:
        request=pool.reserve('.1',key)
        with pool.connect() as db:
            db.execute('INSERT INTO openai_requests VALUES(?,?,?,?)',(request['reservation_id'],key,model,'{}'))
        if key=='current':pool.mark_started(request['reservation_id'])
        else:
            if key=='expires':monkeypatch.setattr(pool,'_now',lambda:'2026-10-21T00:00:00+00:00')
            with pytest.raises(ValueError):pool.mark_started(request['reservation_id'])
