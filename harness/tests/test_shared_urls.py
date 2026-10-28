"""Canonical shared paths retain session, signature and file boundaries."""
import json
import re
from fastapi.testclient import TestClient
import pack
import server
from access import COOKIE, LEGACY_COOKIE


def test_shared_page_assets_health_and_legacy_aliases(tmp_path):
    with pack.use('bare'):
        app = server.create_app(tmp_path)
        with TestClient(app) as client:
            health = client.get('/healthz')
            assert health.json() == {'status':'ok','vertical':'bare','release':None,
                                    'core':'openai_managed_agents_api','execution':'openai_hosted'}
            page = client.get('/').text
            assets = re.findall(r'(?:src|href)="(/static/[^"#]+)"', page)
            assert assets and not any('/static/retail/' in path for path in assets)
            for path in assets:
                response = client.get(path)
                assert response.status_code == 200, path
                assert response.content
            assert client.get('/app.js').content == client.get('/static/harness/app.js').content
            for path in ('/static/harness/server.py','/static/harness/assets/private.json',
                         '/static/vendor/private.js','/static/harness/%2e%2e/server.py'):
                assert client.get(path).status_code == 404


def test_page_header_session_cookie_and_company_switch_compatibility(tmp_path):
    with pack.use('bare'):
        app = server.create_app(tmp_path)
        with TestClient(app) as client:
            code = app.state.access.issue('demo',owner=True,principal_id='alice',display_name='Alice')
            login = client.post('/api/session',json={'code':code},headers={'X-Rocoto-Page':'1'})
            assert login.status_code == 200
            token = client.cookies.get(COOKIE)
            assert token
            client.cookies.clear();client.cookies.set(LEGACY_COOKIE,token)
            assert client.get('/api/session').json()['authenticated']
            assert client.post('/api/session/company',json={'environment_id':'demo'},headers={'X-Rocoto-Page':'1'}).status_code == 200
            assert client.post('/api/environment',json={'environment_id':'demo'},headers={'X-Video-Demo':'1'}).status_code == 200
            assert client.post('/api/session/company',json={'environment_id':'demo'}).status_code == 403
            assert client.delete('/api/session',headers={'X-Rocoto-Page':'1'}).status_code == 200
            assert not client.get('/api/session').json()['authenticated']


def test_shared_bridge_requires_managed_core_even_with_legacy_launcher(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from agent_bridge import HarnessAgent
    async def forbidden(*args, **kwargs):raise AssertionError('A web turn cannot launch a local core')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',forbidden)
    agent = HarnessAgent(tmp_path)
    agent.isolated_launcher = SimpleNamespace(enabled=True)
    async def run():return [event async for event in agent._turn('Hello','chat','company')]
    events = asyncio.run(run())
    assert events == [{'type':'error','message':'Connect this workspace to the OpenAI-managed Agents API.'}]


def test_page_marker_prefers_canonical_and_refuses_duplicate_values():
    from starlette.datastructures import Headers
    from access import page_request
    for raw in ([('x-rocoto-page','1')],[('x-video-demo','1')],
                [('x-rocoto-page','1'),('x-video-demo','0')]):
        assert page_request(Headers(raw=[(k.encode(),v.encode()) for k,v in raw]))
    for raw in ([],[('x-rocoto-page','')],[('x-rocoto-page',''),('x-video-demo','1')],
                [('x-rocoto-page','0'),('x-video-demo','1')],
                [('x-rocoto-page','1'),('x-rocoto-page','1')],
                [('x-video-demo','1'),('x-video-demo','1')]):
        assert not page_request(Headers(raw=[(k.encode(),v.encode()) for k,v in raw]))
