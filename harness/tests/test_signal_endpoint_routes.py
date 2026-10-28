"""Endpoint signing secrets use a current owner's private one-time HTTP action."""
import asyncio
from fastapi.testclient import TestClient
from host import Admission
from tool_families import ToolContext
from workspace_identity import workspace_scope
from test_server_access import fixture_app
from test_workflow_routes import login,HEADERS


def test_endpoint_secret_is_scoped_to_owning_chat_and_revealed_once(tmp_path,monkeypatch):
    app=fixture_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice',owner=True)
        selected=app.state.workspaces['demo'];agent=selected['agent']
        record={'id':'endpoint-chat','owner_principal_id':'alice','dataset':'cafe','messages':[]}
        agent.children[record['id']]=record
        run={'cancel':asyncio.Event()};agent.active[record['id']]=run
        context=ToolContext(state=selected['state'],company_id=workspace_scope(selected['state']),
            actor='alice',data_owner='alice',collection='cafe',chat_id=record['id'],record=record,
            run=run,agent=agent,admission=Admission(lambda:True))
        endpoint=selected['signal_endpoints'].register(context,label='Order received',url='https://example.com/events')
        listing=client.get('/api/wakes?chat_id=endpoint-chat')
        assert listing.status_code==200 and listing.json()['endpoints']==[endpoint]
        assert 'secret' not in endpoint
        path=f"/api/wakes/endpoints/{endpoint['id']}/secret"
        first=client.post(path,json={},headers=HEADERS)
        assert first.status_code==200 and first.headers['cache-control']=='no-store'
        secret=first.json()['secret'];assert len(secret)>20
        assert secret not in str(record) and secret not in listing.text
        assert client.post(path,json={},headers=HEADERS).status_code==409
        login(client,app,'bob')
        assert client.post(path,json={},headers=HEADERS).status_code==409
