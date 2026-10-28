"""Hosted API leases isolate credentials; local founder mode remains explicit."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
import httpx

sys.path.insert(0,str(Path(__file__).parents[1]))
from agent_auth import AgentAuth
from company_usage import CompanyUsage


def funded(state):
    usage=CompanyUsage.openai_pool(state)
    with usage.connect() as db:
        usage._fund(db,"test-fixture-receipt",40_000_000,"prepaid_customer_funds","test-record-digest")
    usage.set_monthly_limit(0,"40.000000")
    return usage


def test_unconfigured_never_selects_ambient_credentials(tmp_path):
    auth=AgentAuth(tmp_path,environ={"OPENAI_API_KEY":"foreign-private-key",
        "ROCOTO_OPENAI_API_KEY":"test-rocoto-key"},runtime_host="rocoto-dev")
    assert auth.status()["mode"]=="unconfigured"
    assert auth.status()["inference_enabled"] is False
    with pytest.raises(ValueError):auth.launch()
    assert "private-key" not in json.dumps(auth.status())


def test_local_founder_setting_requires_managed_api_before_launch(tmp_path):
    environment = {'OPENAI_API_KEY': 'foreign-private-key', 'ROCOTO_OPENAI_API_KEY': 'test-rocoto-key',
        'CODEX_ACCESS_TOKEN': 'private-access-token'}
    auth = AgentAuth(tmp_path, mode='founder_local_chatgpt', environ=environment, runtime_host='rocoto-dev')
    assert auth.status()['inference_enabled'] is False
    assert auth.status()['authentication_prepared'] is False
    assert auth.status()['reason'] == 'managed_api_required'
    with pytest.raises(ValueError, match='managed'):
        auth.launch()
    assert 'private' not in json.dumps(auth.status())


@pytest.mark.parametrize("origin,host",[
    ("https://video-demo.soyrocoto.com","rocoto-dev"),(None,"rocoto-staging"),
    ("http://127.0.0.1:8768","rocoto-staging"),("http://localhost.evil.example","rocoto-dev"),
])
def test_hosted_or_staging_founder_mode_stops_before_launch(tmp_path,origin,host):
    auth=AgentAuth(tmp_path,public_origin=origin,mode="founder_local_chatgpt",environ={},runtime_host=host)
    assert auth.status()["reason"]=="hosted_requires_openai_api"
    assert auth.status()["inference_enabled"] is False
    with pytest.raises(ValueError):auth.launch()


def test_api_uses_selected_rocoto_key_and_separate_cash_pool(tmp_path):
    auth=AgentAuth(tmp_path,mode="openai_api",environ={"OPENAI_API_KEY":"ambient-key"},runtime_host="rocoto-staging")
    assert auth.status()["reason"]=="rocoto_api_key_required"
    auth=AgentAuth(tmp_path,mode="openai_api",environ={"ROCOTO_OPENAI_API_KEY":"test-rocoto-key"},runtime_host="rocoto-staging")
    assert auth.status()["reason"]=="prepaid_company_funding_required"
    usage=CompanyUsage(tmp_path)
    with usage.connect() as db:usage._fund(db,"fixture-trial",18_000_000,"provider_trial_credit","trial-digest")
    usage.set_monthly_limit(0,"18.000000")
    assert auth.status()["reason"]=="prepaid_company_funding_required"
    funded(tmp_path)
    assert auth.status()["inference_enabled"] is True
    assert "test-rocoto-key" not in json.dumps(auth.status())
    with pytest.raises(ValueError,match="lease"):auth.launch()


@pytest.mark.anyio
async def test_api_lease_keeps_real_key_in_gateway_and_closes_it(tmp_path):
    funded(tmp_path);calls=[]
    class FakeGateway:
        def __init__(self,state,*,api_key,run_id):
            calls.append((state,api_key,run_id));self.token="local-only-capability"
            self.cli_config={"model_provider":"rocoto_metered"}
        async def __aenter__(self):calls.append("opened");return self
        async def __aexit__(self,*args):calls.append("closed")
    auth=AgentAuth(tmp_path,mode="openai_api",environ={"ROCOTO_OPENAI_API_KEY":"test-private-key",
        "OPENAI_API_KEY":"foreign-key","PATH":"/bin"},runtime_host="rocoto-staging")
    async with auth.lease(run_id="fixture",gateway_factory=FakeGateway) as launch:
        assert launch.child_env=={"PATH":"/bin","ROCOTO_AGENT_GATEWAY_TOKEN":"local-only-capability"}
        assert launch.expected_account_type=="gateway" and not launch.login_status_required
        assert launch.cli_args==["-c",'model_provider="rocoto_metered"']
        assert "test-private-key" not in repr(launch)
    assert calls==[(tmp_path,"test-private-key","fixture"),"opened","closed"]


@pytest.mark.anyio
async def test_founder_lease_refuses_local_execution_before_gateway(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('Local founder mode cannot create a provider gateway')
    auth = AgentAuth(tmp_path, mode='founder_local_chatgpt', environ={}, runtime_host='rocoto-dev')
    with pytest.raises(ValueError, match='managed'):
        async with auth.lease(run_id='local', gateway_factory=forbidden):
            pytest.fail('Local execution was admitted')


@pytest.mark.anyio
async def test_child_leases_share_atomic_root_budget_after_parent_closes(tmp_path):
    from openai_gateway import ResponsesGateway,MODEL
    pool=funded(tmp_path);opened=[];calls=[];release=asyncio.Event()
    async def upstream(request):
        calls.append(request)
        raise httpx.ReadError('Unknown fixture charge remains held')
    def factory(state,*,api_key,run_id):
        opened.append(run_id)
        return ResponsesGateway(state,api_key=api_key,run_id=run_id,
            maximum_run_usd='1',maximum_requests=2,transport=httpx.MockTransport(upstream))
    auth=AgentAuth(tmp_path,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'fixture'},runtime_host='rocoto-staging')
    async def child(label):
        await release.wait()
        async with auth.lease(run_id=label,gateway_factory=factory) as launch:
            url=launch.cli_config['model_providers.rocoto_metered.base_url']
            async with httpx.AsyncClient(trust_env=False) as client:
                result=await client.post(url+'/responses',json={'model':MODEL,'input':'Fixture','stream':False},
                    headers={'Authorization':'Bearer '+launch.child_env['ROCOTO_AGENT_GATEWAY_TOKEN']})
                return result.status_code
    async with auth.lease(run_id='root-question',gateway_factory=factory):
        one=asyncio.create_task(child('child-one'))
        two=asyncio.create_task(child('child-two'))
    # The parent's transport has closed; children still inherit its budget ID.
    release.set()
    results=await asyncio.gather(one,two)
    assert sorted(results)==[402,502]
    assert opened==['root-question']*3 and len(calls)==1
    assert float(pool.summary()['reserved_usd'])<1
    # A separately initiated chat receives its own run limit, same company pool.
    assert await child('independent-question')==502
    assert opened[-1]=='independent-question' and len(calls)==2


@pytest.mark.anyio
async def test_child_budget_binding_rejects_company_change_and_resets(tmp_path):
    one=tmp_path/'one';two=tmp_path/'two';funded(one);funded(two);opened=[]
    class FakeGateway:
        def __init__(self,state,*,api_key,run_id):
            opened.append((state,run_id));self.token='fixture';self.cli_config={}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    def auth(state):return AgentAuth(state,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'fixture'},runtime_host='rocoto-staging')
    async with auth(one).lease(run_id='one-question',gateway_factory=FakeGateway):
        with pytest.raises(ValueError,match='parent company'):
            async with auth(two).lease(run_id='cross-company',gateway_factory=FakeGateway):
                raise AssertionError('Cross-company child admission must stop')
    async with auth(two).lease(run_id='two-question',gateway_factory=FakeGateway):pass
    assert opened==[(one,'one-question'),(two,'two-question')]


def test_zero_cap_and_unreconciled_usage_disable_api(tmp_path):
    pool=funded(tmp_path)
    auth=AgentAuth(tmp_path,mode="openai_api",environ={"ROCOTO_OPENAI_API_KEY":"test-key"},runtime_host="rocoto-staging")
    pool.set_monthly_limit(1,"0")
    assert not auth.status()["inference_enabled"]
    pool.set_monthly_limit(2,"40")
    directory=pool.state/'gateway';directory.mkdir();(directory/'admission-review-required.json').write_text('{}')
    assert auth.status()["reason"]=="api_reconciliation_required"


def test_auth_never_reads_auth_cache(tmp_path,monkeypatch):
    cache=tmp_path/'auth.json';cache.write_text('Private credential fixture')
    original=Path.read_text
    def guard(path,*args,**kwargs):
        assert path!=cache
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',guard)
    assert AgentAuth(tmp_path,environ={},runtime_host='rocoto-staging').status()['inference_enabled'] is False


@pytest.fixture
def anyio_backend():return "asyncio"
