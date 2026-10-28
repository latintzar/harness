"""Native chat funding stays separate from consented image-inspection money."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,sys
import httpx
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from agent_auth import AgentAuth
from company_usage import CompanyUsage
from compute_guard import OWNER
from openai_gateway import ResponsesGateway, MODEL, PRICING
from catalog_image_model import FundedItemModel


def allocate(state, *, native=False, purpose=None, model=None, amount='2'):
    pool=(CompanyUsage.native_agent_pool if native else CompanyUsage.openai_pool)(state)
    record={'schema_version':1,'record_id':'authorized-native' if native else 'two-images',
        'kind':'operator_demo_budget','environment_id':'retail-demo','provider':'openai',
        'company_scope':pool.company_scope,'parent_company_scope':pool.summary()['parent_company_scope'],
        'verified_by':OWNER,'user_authorized':True,'authorization_reference':'test fixture approval',
        'amount_usd':amount,'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    if purpose is not None:record['purpose']=purpose
    if model is not None:record['allowed_model']=model
    path=pool.state/'approval.json';path.write_text(json.dumps(record));path.chmod(0o600)
    pool.import_operator_budget(path,lifecycle='development',environment_id='retail-demo')
    return pool,path


def auth(state):
    return AgentAuth(state,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'fixture'},runtime_host='rocoto-staging')


def test_image_allowance_cannot_enable_native_chat_or_direct_gateway(tmp_path):
    image,_=allocate(tmp_path,amount='1')
    assert auth(tmp_path).status()['inference_enabled'] is False
    with pytest.raises(ValueError,match='dedicated'):
        ResponsesGateway(tmp_path,api_key='fixture',run_id='unauthorized')
    assert image.summary()['reservation_count']==0


@pytest.mark.parametrize('purpose,model',[(None,None),('catalog_image_inspection',MODEL),('native_agent_chat','gpt-5.6-luna')])
def test_native_allocation_requires_exact_purpose_and_model(tmp_path,purpose,model):
    with pytest.raises(ValueError,match='explicitly'):
        allocate(tmp_path,native=True,purpose=purpose,model=model)
    assert auth(tmp_path).status()['inference_enabled'] is False


def test_native_approval_cannot_be_imported_into_image_pool(tmp_path):
    with pytest.raises(ValueError,match='dedicated'):
        allocate(tmp_path,purpose='native_agent_chat',model=MODEL)


def test_dedicated_allowance_enables_only_native_and_expiry_does_not_fallback(tmp_path,monkeypatch):
    image,_=allocate(tmp_path,amount='1')
    native,path=allocate(tmp_path,native=True,purpose='native_agent_chat',model=MODEL)
    status=auth(tmp_path).status()
    assert status['inference_enabled'] and status['billing_status']=='operator_native_request_metering'
    assert native.company_scope!=image.company_scope
    assert native.summary()['parent_company_scope']==image.summary()['parent_company_scope']
    assert FundedItemModel(tmp_path,'fixture').pool.company_scope==image.company_scope
    with pytest.raises(ValueError,match='company'):
        ResponsesGateway(tmp_path,api_key='fixture',run_id='wrong-pool',pool=image)
    native.import_operator_budget(path,lifecycle='development',environment_id='retail-demo')
    assert native.summary()['available_usd']=='2.000000'
    later=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    monkeypatch.setattr(CompanyUsage,'_now',staticmethod(lambda:later))
    assert not auth(tmp_path).status()['inference_enabled']
    assert CompanyUsage.agent_pool(tmp_path).company_scope==native.company_scope
    assert image.summary()['reservation_count']==0


def test_native_money_cannot_bind_image_model(tmp_path):
    native,_=allocate(tmp_path,native=True,purpose='native_agent_chat',model=MODEL)
    reservation=native.reserve('.01','wrong-model')
    with pytest.raises(ValueError,match='authorized chat model'):
        native.bind_openai_request(reservation['reservation_id'],request_id='wrong-model',model='gpt-5.6-luna',pricing=PRICING)
    with native.connect() as db:
        assert native._reservation(db,reservation['reservation_id'])['status']=='reserved'


@pytest.mark.anyio
async def test_native_gateway_reserves_only_approved_chat_pool(tmp_path):
    image,_=allocate(tmp_path,amount='1')
    native,_=allocate(tmp_path,native=True,purpose='native_agent_chat',model=MODEL)
    calls=[]
    def upstream(request):
        calls.append(request)
        raise httpx.ReadError('Unknown fixture charge stays reserved')
    async with ResponsesGateway(tmp_path,api_key='fixture',run_id='bounded-chat',
            transport=httpx.MockTransport(upstream),maximum_run_usd='.5') as gateway:
        async with httpx.AsyncClient(trust_env=False) as client:
            response=await client.post(gateway.base_url+'/responses',json={'model':MODEL,'input':'Hello','stream':False,'max_output_tokens':64},
                headers={'Authorization':'Bearer '+gateway.token})
        assert response.status_code==502
    assert len(calls)==1
    assert native.summary()['reservation_count']==1
    assert image.summary()['reservation_count']==0
    assert image.summary()['available_usd']=='1.000000'


@pytest.fixture
def anyio_backend():return 'asyncio'
