"""Isolated native socket route retains funded accounting and exact tool discovery."""
import asyncio
import json
import os
from pathlib import Path
import sys
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_auth import AgentAuth
from openai_gateway import ResponsesGateway, MODEL, prepare_request, usage_receipt
from deploy.migration.legacy.native_policy import isolated_thread_params
from test_openai_gateway import fund, request, response

TOOL = {"type":"tool_search","execution":"client","description":"Discover scoped MCP tools.",
        "parameters":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"number"}},"required":["query"],"additionalProperties":False}}
CALL = {"type":"tool_search_call","id":"tsc_1","call_id":"call_1","execution":"client","status":"completed","arguments":{"query":"rocoto search","limit":1}}
DEFERRED = {"type":"namespace","name":"mcp__rocoto","tools":[{"type":"function","name":"search_video","defer_loading":True,"parameters":{"type":"object","properties":{"query":{"type":"string"}}}}]}
OUTPUT = {"type":"tool_search_output","id":"tso_1","call_id":"call_1","execution":"client","status":"completed","tools":[DEFERRED]}


def test_client_tool_search_requires_explicit_native_admission_and_settles_tokens():
    with pytest.raises(ValueError): prepare_request(request(tools=[TOOL]))
    prepared, bound = prepare_request(request(tools=[TOOL],input=[CALL,OUTPUT]), allow_native_tool_search=True)
    assert prepared['tools']==[TOOL] and prepared['input']==[CALL,OUTPUT]
    assert bound['builtin_call_bound']==0 and bound['client_tool_search'] is True
    receipt = usage_receipt(response(output=[CALL]), request_id='r',reservation_id='v',pool_scope='s',parent_scope='p',admission=bound)
    assert receipt['additional_usage']==[] and receipt['provider_actual_usd']=='0.0036'


@pytest.mark.parametrize('tool', [{**TOOL,'execution':'server'}, {**TOOL,'server_url':'https://other.example'}, {**TOOL,'parameters':[]}, {**TOOL,'type':'code_interpreter'}])
def test_deferred_tool_search_never_opens_hosted_tools(tool):
    with pytest.raises(ValueError):prepare_request(request(tools=[tool]),allow_native_tool_search=True)


def test_discovery_history_cannot_smuggle_provider_tools_or_server_execution():
    for item in [{**CALL,'execution':'server'},{**OUTPUT,'tools':[{'type':'image_generation'}]}, {**CALL,'arguments':{'query':'x','limit':999}}]:
        with pytest.raises(ValueError):prepare_request(request(tools=[TOOL],input=[item]),allow_native_tool_search=True)
    with pytest.raises(ValueError):prepare_request(request(tools=[TOOL,TOOL]),allow_native_tool_search=True)


@pytest.mark.anyio
async def test_actual_uds_listener_binds_only_run_socket_and_preserves_reservations(tmp_path):
    pool=fund(tmp_path);run=tmp_path/'run';run.mkdir(mode=0o700);seen=[]
    async def upstream(req):
        seen.append(req);assert req.headers['authorization']=='Bearer provider-private'
        assert pool.summary()['reserved_usd']!='0.000000'
        return httpx.Response(200,json=response(output=[CALL]))
    gateway=ResponsesGateway(tmp_path,api_key='provider-private',run_id='root',pool=pool,
        unix_socket=run/'model.sock',transport=httpx.MockTransport(upstream),allow_native_tool_search=True)
    async with gateway:
        assert gateway.base_url=='http://127.0.0.1:8765/v1'
        assert (run/'model.sock').stat().st_mode&0o777==0o600
        transport=httpx.AsyncHTTPTransport(uds=str(run/'model.sock'))
        async with httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:8765') as client:
            assert (await client.post('/v1/responses',json=request(tools=[TOOL]))).status_code==401
            headers={'Authorization':'Bearer '+gateway.token}
            result=await client.post('/v1/responses',json=request(tools=[TOOL]),headers=headers)
            assert result.status_code==200
            gateway.revoke()
            assert (await client.post('/v1/responses',json=request(),headers=headers)).status_code==401
    assert not (run/'model.sock').exists() and len(seen)==1
    assert pool.summary()['reserved_usd']=='0.000000'
    assert pool.summary()['provider_actual_cost_usd']=='0.003600'


@pytest.mark.anyio
async def test_uds_refuses_existing_file_and_non_private_directory(tmp_path):
    pool=fund(tmp_path);run=tmp_path/'run';run.mkdir(mode=0o700);path=run/'model.sock';path.write_text('keep')
    gateway=ResponsesGateway(tmp_path,api_key='private',run_id='root',pool=pool,unix_socket=path)
    with pytest.raises(ValueError):
        async with gateway:pass
    assert path.read_text()=='keep'
    path.unlink();run.chmod(0o755)
    gateway=ResponsesGateway(tmp_path,api_key='private',run_id='root',pool=pool,unix_socket=path)
    with pytest.raises(ValueError):
        async with gateway:pass


@pytest.mark.anyio
async def test_uds_disconnect_retains_hold_and_same_root_other_gateway_obeys_cap(tmp_path):
    pool=fund(tmp_path);run=tmp_path/'run';run.mkdir(mode=0o700);seen=[]
    async def upstream(req):seen.append(req);raise httpx.ReadError('fixture-disconnect')
    async with ResponsesGateway(tmp_path,api_key='private',run_id='root',pool=pool,unix_socket=run/'model.sock',
             maximum_requests=1,transport=httpx.MockTransport(upstream)) as gateway:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(run/'model.sock')),base_url='http://127.0.0.1:8765') as client:
            headers={'Authorization':'Bearer '+gateway.token}
            assert (await client.post('/v1/responses',json=request(),headers=headers)).status_code==502
    async with ResponsesGateway(tmp_path,api_key='private',run_id='root',pool=pool,
             maximum_requests=1,transport=httpx.MockTransport(upstream)) as gateway:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.post(gateway.base_url+'/responses',json=request(),headers={'Authorization':'Bearer '+gateway.token})).status_code==402
    assert len(seen)==1 and pool.summary()['reserved_usd']!='0.000000'


@pytest.mark.anyio
async def test_isolated_auth_environment_has_only_run_capability(tmp_path):
    fund(tmp_path);run=tmp_path/'run';run.mkdir(mode=0o700)
    auth=AgentAuth(tmp_path,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'private','AWS_SECRET_ACCESS_KEY':'other-company','HOME':'/host/private','PATH':'/host/bin'},runtime_host='rocoto-staging')
    async with auth.lease(run_id='root',unix_socket=run/'model.sock') as launch:
        assert set(launch.child_env)=={'ROCOTO_AGENT_GATEWAY_TOKEN'}
        assert launch.isolated is True and launch.gateway is not None
        assert launch.cli_config['model_providers.rocoto_metered.base_url']=='http://127.0.0.1:8765/v1'
        assert 'private' not in repr(launch) and 'other-company' not in repr(launch)


def test_isolated_policy_preserves_native_base_and_drops_custom_delegation():
    tools=[{'name':'search_video'},{'name':'delegate_investigations'},{'name':'wait_investigations'}]
    params=isolated_thread_params('Rocoto product rules.',tools)
    assert 'baseInstructions' not in params and params['developerInstructions']=='Rocoto product rules.'
    assert params['cwd']=='/workspace' and params['sandbox']=='danger-full-access'
    assert [tool['name'] for tool in params['dynamicTools']]==['search_video']
    assert params['config']['features.multi_agent_v2'] is True
    assert params['config']['features.default_mode_request_user_input'] is True
    assert params['config']['features.plugins'] is False


@pytest.fixture
def anyio_backend():return 'asyncio'
