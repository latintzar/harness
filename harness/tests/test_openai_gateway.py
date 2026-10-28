"""Real loopback/ASGI transport, mocked upstream, actual durable company ledger."""
import base64
import io
import json
from pathlib import Path
import sys

import httpx
from PIL import Image
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from company_usage import CompanyUsage
from openai_gateway import ResponsesGateway, MODEL, PRICING, prepare_request, usage_receipt


def fund(state,amount=40_000_000):
    pool=CompanyUsage.openai_pool(state)
    with pool.connect() as db:pool._fund(db,'fixture',amount,'prepaid_customer_funds','fixture-sha')
    pool.set_monthly_limit(0,str(amount//1_000_000))
    return pool


def request(**extra):return {'model':MODEL,'input':'Describe visible activity.','stream':False,**extra}


def response(**extra):
    return {'id':'resp_fixture','model':MODEL,'status':'completed','service_tier':'default',
        'usage':{'input_tokens':200,'input_tokens_details':{'cached_tokens':50,'cache_write_tokens':20},'output_tokens':40},
        'output':[{'type':'message','content':[{'type':'output_text','text':'Observed scene.'}]}],**extra}


def test_text_bound_caps_output_and_standard_tier():
    value,bound=prepare_request(request(max_output_tokens=99999,service_tier='auto'))
    assert value['max_output_tokens']==8192 and value['max_tool_calls']==1
    assert value['service_tier']=='default' and value['store'] is False
    assert bound['input_token_bound']>len(json.dumps(value).encode())
    assert bound['assurance']=='calculated_conservative_bound'


@pytest.mark.parametrize('change',[
    {'model':'unpriced-model'},{'tools':[{'type':'code_interpreter'}]},
    {'tools':[{'type':'image_generation'}]},{'service_tier':'fast'},
    {'previous_response_id':'resp_othercompany'},{'conversation':'conv_shared'},
    {'background':True},{'store':True},{'unpriced_option':True},
    {'input':[{'type':'input_file','file_id':'file_othercompany'}]},
    {'input':[{'type':'input_image','image_url':'https://foreign.example/image.jpg'}]},
    {'reasoning':{'effort':'ultra'}},{'prompt_cache_options':{'prewarm':True}},
])
def test_unpriced_or_opaque_routes_rejected(change):
    with pytest.raises(ValueError):prepare_request(request(**change))


def test_inline_image_exact_original_pixels_accounted_without_modification():
    image=Image.new('RGB',(1024,1024),'red');output=io.BytesIO();image.save(output,format='PNG')
    url='data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()
    body=request(input=[{'role':'user','content':[{'type':'input_image','image_url':url,'detail':'original'}]}])
    prepared,bound=prepare_request(body)
    assert prepared['input'][0]['content'][0]['image_url']==url
    assert bound['image_token_bound']==1231 # documented1229 plus2 rounding margin
    assert bound['image_count']==1 and bound['text_bytes']<1000


def test_web_reserves_full_search_and_reinput_and_settles_tool_fee():
    _,plain=prepare_request(request())
    prepared,bound=prepare_request(request(tools=[{'type':'web_search_preview'}]))
    assert prepared['tools']==[{'type':'web_search','return_token_budget':'default'}]
    assert bound['input_token_bound']>=2*bound['initial_input_token_bound']+128000
    assert float(bound['maximum_usd'])>float(plain['maximum_usd'])+3
    res=response(output=[{'type':'web_search_call','id':'ws_1','status':'completed'}])
    receipt=usage_receipt(res,request_id='r',reservation_id='u',pool_scope='s',parent_scope='p',admission=bound)
    assert receipt['additional_usage']==[{'kind':'web_search','quantity':1,'unit_usd':'0.01'}]
    assert receipt['provider_actual_usd']=='0.0136'


@pytest.mark.anyio
async def test_gateway_reserves_before_send_and_settles_usage(tmp_path):
    pool=fund(tmp_path);seen=[]
    async def upstream(req):
        seen.append(req)
        assert pool.summary()['reserved_usd']!='0.000000'
        assert req.url==httpx.URL('https://api.openai.com/v1/responses')
        assert req.headers['authorization']=='Bearer provider-fixture-key'
        return httpx.Response(200,json=response())
    gateway=ResponsesGateway(tmp_path,api_key='provider-fixture-key',run_id='run-1',pool=pool,transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=request(),headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==200 and len(seen)==1
    summary=pool.summary()
    assert summary['reserved_usd']=='0.000000'
    assert summary['provider_actual_cost_usd']=='0.003600'
    assert CompanyUsage(tmp_path).summary()['prepaid_funded_usd']=='0.000000'
    path=next((pool.state/'gateway').glob('*.usage.json'))
    assert path.stat().st_mode&0o777==0o600
    assert 'provider-fixture-key' not in path.read_text()


@pytest.mark.anyio
async def test_gateway_wrong_capability_unfunded_unsupported_path_never_calls_upstream(tmp_path):
    seen=[]
    async def upstream(req):seen.append(req);return httpx.Response(200,json=response())
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='run',transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        assert (await client.post('/v1/responses',json=request())).status_code==401
        headers={'Authorization':'Bearer '+gateway.token}
        assert (await client.post('/v1/responses',json=request(),headers=headers)).status_code==402
        assert (await client.post('/v1/responses/compact',json=request(),headers=headers)).status_code==402
        assert (await client.post('/v1/responses/input_tokens',json=request(),headers=headers)).status_code==404
    await gateway.__aexit__();assert seen==[]


@pytest.mark.anyio
async def test_interrupted_started_request_retains_full_reservation(tmp_path):
    pool=fund(tmp_path)
    async def upstream(req):raise httpx.ReadError('Fixture disconnect')
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='run',transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=request(),headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==502
    assert pool.summary()['reserved_usd']!='0.000000'
    assert pool.summary()['provider_actual_cost_usd']=='0.000000'


@pytest.mark.anyio
async def test_missing_cache_write_usage_stops_next_request_and_keeps_reserve(tmp_path):
    pool=fund(tmp_path);bad=response();del bad['usage']['input_tokens_details']['cache_write_tokens'];seen=[]
    async def upstream(req):seen.append(req);return httpx.Response(200,json=bad)
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='run',transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        headers={'Authorization':'Bearer '+gateway.token}
        assert (await client.post('/v1/responses',json=request(),headers=headers)).status_code==200
        assert (await client.post('/v1/responses',json=request(),headers=headers)).status_code==503
    await gateway.__aexit__()
    assert len(seen)==1 and gateway.breaker.exists()
    assert pool.summary()['reserved_usd']!='0.000000'


@pytest.mark.anyio
async def test_streaming_terminal_usage_across_chunks_settles(tmp_path):
    pool=fund(tmp_path)
    event='event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':response()})+'\n\n'
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for n in range(0,len(event),7):yield event[n:n+7].encode()
    async def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream())
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='run',transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=request(stream=True),headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==200 and result.text==event
    assert pool.summary()['reserved_usd']=='0.000000'


@pytest.mark.anyio
async def test_actual_loopback_lease_binds_local_closes_and_never_exposes_key(tmp_path):
    fund(tmp_path)
    async def upstream(req):return httpx.Response(200,json=response())
    async with ResponsesGateway(tmp_path,api_key='provider-fixture',run_id='run',transport=httpx.MockTransport(upstream)) as gateway:
        assert gateway.base_url.startswith('http://127.0.0.1:')
        assert gateway.cli_config['model_providers.rocoto_metered.requires_openai_auth'] is False
        assert 'model_context_window' not in gateway.cli_config
        assert 'provider-fixture' not in json.dumps(gateway.cli_config)
        async with httpx.AsyncClient(trust_env=False) as client:
            result=await client.post(gateway.base_url+'/responses',json=request(),headers={'Authorization':'Bearer '+gateway.token})
        assert result.status_code==200
    assert gateway.token=='' and gateway._api_key==''


@pytest.fixture
def anyio_backend():return "asyncio"


def test_native_responses_lite_namespaces_are_priced_and_preserve_shape():
    body=request(input=[{'type':'additional_tools','role':'developer','id':'at_fixture','tools':[
        {'type':'namespace','name':'functions','tools':[{'type':'custom','name':'exec','description':'Local fixture'}]}]},
        {'type':'message','role':'user','content':[{'type':'input_text','text':'Scene fixture'}]}],
        client_metadata={'thread_id':'thread_fixture'},reasoning={'effort':'low','context':'all_turns'})
    prepared,bound=prepare_request(body)
    assert prepared['input']==body['input'] and bound['builtin_call_bound']==0
    body['input'][0]['tools'][0]['tools'].append({'type':'code_interpreter'})
    with pytest.raises(ValueError,match='budget route'):prepare_request(body)


@pytest.mark.anyio
async def test_run_bound_shares_atomic_limit_across_gateway_leases(tmp_path):
    pool=fund(tmp_path);calls=[]
    async def upstream(req):
        calls.append(req)
        raise httpx.ReadError('Keep this unknown charge held')
    one=ResponsesGateway(tmp_path,api_key='test',run_id='shared-run',pool=pool,
        transport=httpx.MockTransport(upstream),maximum_run_usd='1')
    two=ResponsesGateway(tmp_path,api_key='test',run_id='shared-run',pool=pool,
        transport=httpx.MockTransport(upstream),maximum_run_usd='1')
    for gateway in [one,two]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
            result=await client.post('/v1/responses',json=request(),headers={'Authorization':'Bearer '+gateway.token})
        await gateway.__aexit__()
    assert result.status_code==402 and len(calls)==1
    assert pool.summary()['reservation_count']==1 # insufficient output headroom stops before admission
    assert float(pool.summary()['reserved_usd'])<1


@pytest.mark.anyio
async def test_opaque_history_uses_provider_count_before_reserving_model(tmp_path):
    pool=fund(tmp_path);paths=[]
    async def upstream(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/input_tokens'):
            assert pool.summary()['reserved_usd']=='0.000000'
            return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':50000})
        assert float(pool.summary()['reserved_usd'])>1.25
        return httpx.Response(200,json=response())
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='opaque-run',transport=httpx.MockTransport(upstream))
    payload=request(input=[{'type':'reasoning','encrypted_content':'opaque-fixture','summary':[]}])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=payload,headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==200 and paths==['/v1/responses/input_tokens','/v1/responses']
    record=json.loads(next((pool.state/'gateway').glob('*.count.json')).read_text())
    assert record['input_tokens']==50000 and record['billing_status']=='not_priced_separately_in_published_rate_card'
    assert pool.summary()['reserved_usd']=='0.000000'


@pytest.mark.anyio
async def test_compaction_preserves_canonical_window_and_accounts_usage(tmp_path):
    pool=fund(tmp_path);paths=[]
    compact={'id':'resp_compact','object':'response.compaction','created_at':123,
        'output':[{'type':'message','role':'user','content':[{'type':'input_text','text':'fixture'}]},
                  {'type':'compaction','id':'cmp_fixture','encrypted_content':'opaque-result'}],
        'usage':response()['usage']}
    async def upstream(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/input_tokens'):
            return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':1500})
        body=json.loads(req.content)
        assert body['service_tier']=='default' and 'max_output_tokens' not in body
        assert pool.summary()['reserved_usd']=='9.637500'
        return httpx.Response(200,json=compact)
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='compact-run',transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses/compact',json={'model':MODEL,'input':'fixture'},
            headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.json()==compact and paths[-1]=='/v1/responses/compact'
    assert pool.summary()['reserved_usd']=='0.000000'
    assert pool.summary()['provider_actual_cost_usd']=='0.003600'


@pytest.mark.anyio
async def test_large_visible_history_counts_instead_of_reducing_native_context(tmp_path):
    pool=fund(tmp_path);paths=[]
    async def upstream(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/input_tokens'):
            return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':250000})
        assert json.loads(req.content)['input']=='a '*160000
        return httpx.Response(200,json=response())
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='large-visible',transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=request(input='a '*160000),headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==200 and paths==['/v1/responses/input_tokens','/v1/responses']
    admission=json.loads(next((pool.state/'gateway').glob('*.admission.json')).read_text())['admission']
    assert admission['provider_count']==250000


@pytest.mark.anyio
async def test_visible_history_recovers_budget_headroom_with_exact_count(tmp_path):
    pool=fund(tmp_path);paths=[]
    async def upstream(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/input_tokens'):
            return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':200})
        return httpx.Response(200,json=response())
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='visible-headroom',maximum_run_usd='.05',
        max_output_tokens=128,transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=request(input='word '*3000),headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==200 and paths==['/v1/responses/input_tokens','/v1/responses']
    assert pool.summary()['reserved_usd']=='0.000000'


@pytest.mark.anyio
async def test_exact_count_still_enforces_run_budget_before_inference(tmp_path):
    pool=fund(tmp_path);paths=[]
    async def upstream(req):
        paths.append(req.url.path)
        return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':10000})
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='headroom-denied',maximum_run_usd='.05',
        max_output_tokens=128,transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses',json=request(input='word '*3000),headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==402 and paths==['/v1/responses/input_tokens']
    assert pool.summary()['reserved_usd']=='0.000000'


@pytest.mark.anyio
@pytest.mark.parametrize('funded,allowed',[(40_000_000,True),(10_000_000,False)])
async def test_native_window_compaction_reserves_full_bound_within_shared_funds(tmp_path,funded,allowed):
    pool=fund(tmp_path,funded);paths=[]
    compact={'id':'resp_full_compact','object':'response.compaction','created_at':123,
        'output':[{'type':'compaction','id':'cmp_full','encrypted_content':'fixture-canonical'}],
        'usage':response()['usage']}
    async def upstream(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/input_tokens'):
            return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':250000})
        assert pool.summary()['reserved_usd']=='15.850000'
        return httpx.Response(200,json=compact)
    gateway=ResponsesGateway(tmp_path,api_key='test',run_id='native-window',transport=httpx.MockTransport(upstream))
    assert str(gateway.maximum_run_usd)=='20'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),base_url='http://127.0.0.1') as client:
        result=await client.post('/v1/responses/compact',json={'model':MODEL,'input':'full-window-fixture'},
            headers={'Authorization':'Bearer '+gateway.token})
    await gateway.__aexit__()
    assert result.status_code==(200 if allowed else 402)
    assert paths==['/v1/responses/input_tokens']+(['/v1/responses/compact'] if allowed else [])
    if allowed:assert result.json()==compact
    else:assert result.json()['error']['code']=='compaction_admission_paused'
    assert pool.summary()['reserved_usd']=='0.000000'
