"""Deployment selects native execution; customers cannot choose its scope or flag."""
import pytest
from fastapi.testclient import TestClient
from test_server_access import fixture_app, FixtureAgent
from workspace_identity import workspace_scope


@pytest.mark.parametrize('flag', [None, '0', 'true', '1'])
def test_native_executor_opt_in_uses_durable_company_identity(tmp_path, monkeypatch, flag):
    if flag is None:monkeypatch.delenv('ROCOTO_NATIVE_EXECUTOR_ENABLED',raising=False)
    else:monkeypatch.setenv('ROCOTO_NATIVE_EXECUTOR_ENABLED',flag)
    app=fixture_app(tmp_path,monkeypatch)
    launcher=getattr(app.state.agent,'isolated_launcher',None)
    if flag!='1':
        assert launcher is None
        return
    assert launcher.enabled and launcher.company_id==workspace_scope(tmp_path/'state')
    assert launcher.auth is app.state.agent.auth
    access=app.state.access;access.create_environment('customer-a','Customer A')
    with TestClient(app) as client:
        response=client.post('/api/session',json={'code':access.issue('customer-a')},headers={'X-Video-Demo':'1'})
        assert response.status_code==200
        assert client.get('/api/status').status_code==200
    customer=FixtureAgent.instances['customer-a'].isolated_launcher
    assert customer.enabled and customer.company_id==workspace_scope(tmp_path/'state/environments/customer-a')
    assert customer.company_id!=launcher.company_id
