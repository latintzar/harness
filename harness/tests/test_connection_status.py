"""Connection status reports the actual probe time and releases timed-out children."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import httpx

sys.path.insert(0, str(Path(__file__).parents[1]))
import server
from test_server_access import fixture_app
from agent_auth import AgentAuth
from test_agent_auth import funded


def test_local_login_mode_never_starts_a_core_process_or_reports_managed_access(tmp_path, monkeypatch):
    monkeypatch.setenv('ROCOTO_AGENT_AUTH_MODE', 'founder_local_chatgpt')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Managed access verification starts no local core.')
    monkeypatch.setattr(server.asyncio, 'create_subprocess_exec', forbidden)
    async def run():
        result = await server.ManagedConnection(tmp_path).status()
        assert not result['managed_authenticated'] and not result['inference_enabled']
        assert result['route'] == 'OpenAI-managed Agents API'
        assert result['execution'] == 'openai_hosted'
    asyncio.run(run())


def test_operator_display_is_separate_from_company_and_host_is_measured(tmp_path, monkeypatch):
    monkeypatch.setenv("ROCOTO_VIDEO_OPERATOR_NAME", "Rocoto operator")
    monkeypatch.setattr(server.socket, "gethostname", lambda:"actual-dev-host")
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post("/api/session/demo", headers={"X-Video-Demo":"1"})
        assert response.json()["operator"] == {"name":"Rocoto operator", "mode":"founder_demo"}
        assert response.json()["environment"]["name"] == "Demo company"
        assert client.get("/api/session").json()["operator"]["name"] == "Rocoto operator"
        status = client.get("/api/status").json()
        assert status["connection"]["host"] == "actual-dev-host"
        assert status["connection"]["managed_authenticated"] is False
        assert status["connection"]["check_status"] == "unavailable"
        assert status["connection"]["checked_at"]


def api_auth(state):
    return AgentAuth(state,mode='openai_api',runtime_host='rocoto-staging',
        environ={'ROCOTO_OPENAI_API_KEY':'private-fixture-key','HTTPS_PROXY':'https://ignored.example'})


def test_api_restart_probes_current_access_and_cache_expiry_detects_revocation(tmp_path):
    funded(tmp_path);calls=[];reply=[200]
    async def provider(request):
        calls.append(request)
        assert request.method=='GET'
        assert str(request.url)=='https://api.openai.com/v1/agents/sessions?limit=1&order=desc'
        assert request.headers['authorization']=='Bearer private-fixture-key'
        assert request.extensions['timeout']['read']==3
        return httpx.Response(reply[0],json={'data':[], 'has_more':False, 'error':'private-fixture-key'})
    async def run():
        # A restarted process has no inference-success flag; access is still checked.
        agent=SimpleNamespace()
        probe=server.ManagedConnection(tmp_path,auth=api_auth(tmp_path),agent=agent,
            api_transport=httpx.MockTransport(provider))
        first=await probe.status()
        assert first['managed_authenticated'] and first['inference_enabled']
        assert first['verification_kind']=='managed_session_read_access' and first['inference_verified'] is False
        assert 'verified access' in first['message']
        reply[0]=401;agent.api_verified_at='prior-success-must-not-authorize-current-access'
        assert await probe.status()==first
        assert len(calls)==1
        probe.checked_monotonic-=16
        failed=await probe.status()
        assert not failed['managed_authenticated'] and not failed['inference_enabled']
        assert failed['check_status']=='unauthorized' and 'Reconnect' in failed['message']
        assert failed['checked_at']!=first['checked_at'] and len(calls)==2
        assert 'private-fixture-key' not in json.dumps(failed)
    asyncio.run(run())


def test_api_timeout_has_bounded_probe_and_truthful_message(tmp_path):
    funded(tmp_path);cancelled=[]
    async def provider(request):
        try:await asyncio.Event().wait()
        finally:cancelled.append(True)
    async def run():
        probe=server.ManagedConnection(tmp_path,auth=api_auth(tmp_path),timeout=.01,
            api_transport=httpx.MockTransport(provider))
        result=await probe.status()
        assert result['check_status']=='timeout' and 'timed out' in result['message']
        assert not result['managed_authenticated'] and not result['inference_enabled']
        assert cancelled==[True]
    asyncio.run(run())


@pytest.mark.parametrize('code,status',[(403,'unauthorized'),(404,'unavailable'),(429,'unavailable'),(503,'unavailable'),(302,'unavailable')])
def test_api_transient_and_denied_access_never_reuse_prior_success_or_follow_redirect(tmp_path,code,status):
    funded(tmp_path);calls=[]
    async def provider(request):
        calls.append(request)
        return httpx.Response(code,json={'error':'private-fixture-key'},headers={'Location':'https://foreign.example'})
    async def run():
        probe=server.ManagedConnection(tmp_path,auth=api_auth(tmp_path),agent=SimpleNamespace(api_verified_at='old'),
            api_transport=httpx.MockTransport(provider))
        result=await probe.status()
        assert result['check_status']==status
        assert not result['managed_authenticated'] and not result['inference_enabled']
        assert 'private-fixture-key' not in json.dumps(result)
        assert len(calls)==1
    asyncio.run(run())


def test_api_access_verification_is_separate_from_company_funding(tmp_path):
    calls=[]
    async def provider(request):calls.append(request);return httpx.Response(200,json={'data':[], 'has_more':False})
    async def run():
        probe=server.ManagedConnection(tmp_path,auth=api_auth(tmp_path),api_transport=httpx.MockTransport(provider))
        result=await probe.status()
        assert result['managed_authenticated'] and result['check_status']=='authenticated'
        assert not result['inference_enabled'] and not result['inference_verified']
        assert 'prepaid company funds' in result['message']
        assert len(calls)==1
    asyncio.run(run())


def test_missing_api_credential_stops_without_network_probe(tmp_path):
    async def forbidden(request):raise AssertionError('Missing configuration must stop before network')
    async def run():
        auth=AgentAuth(tmp_path,mode='openai_api',runtime_host='rocoto-staging',environ={})
        result=await server.ManagedConnection(tmp_path,auth=auth,api_transport=httpx.MockTransport(forbidden)).status()
        assert result['check_status']=='rocoto_api_key_required'
        assert not result['managed_authenticated'] and not result['inference_enabled']
    asyncio.run(run())
