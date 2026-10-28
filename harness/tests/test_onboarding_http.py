"""Onboarding, scoped attachments and public metadata routes use actual stores, no LLM."""
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).parents[1]))
from test_server_access import FixtureIndex
from server import create_app
from agent_bridge import CodexVideoAgent

HEADERS={'X-Video-Demo':'1'}


def app_with_sources(tmp_path):
    image=tmp_path/'frame.jpg';image.write_bytes(b'fixture')
    return create_app(tmp_path/'state',FixtureIndex('demo',image))


def test_public_catalogue_and_business_read_but_mutations_require_code(tmp_path):
    app=app_with_sources(tmp_path)
    with TestClient(app) as client:
        client.post('/api/session/demo',headers=HEADERS)
        assert {item['id'] for item in client.get('/api/agents/catalogue').json()['templates']} == {'queue-watch', 'empty-shelf-watch', 'shop-summary'}
        assert client.get('/api/business?collection=cafe').json()['revision']==0
        assert client.post('/api/business/onboarding',json={'collection':'cafe'},headers=HEADERS).status_code==403
        assert client.post('/api/usage/settings',json={'expected_revision':0,'monthly_limit_usd':'10'},headers=HEADERS).status_code==403
        assert client.get('/api/usage').json()['company']['available_usd']=='0.000000'


def test_onboarding_identity_revision_and_draft_connection_are_durable(tmp_path):
    app=app_with_sources(tmp_path)
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        first=client.post('/api/business/onboarding',json={'collection':'cafe'},headers=HEADERS)
        assert first.status_code==200 and first.json()['kind']=='onboarding'
        assert first.json()['id']==client.post('/api/business/onboarding',json={'collection':'cafe'},headers=HEADERS).json()['id']
        assert client.get('/api/chats/'+first.json()['id']).status_code==200
        update={'collection':'cafe','expected_revision':0,'profile':{'name':'Test business'}}
        assert client.post('/api/business/profile',json=update,headers=HEADERS).json()['revision']==1
        assert client.post('/api/business/profile',json=update,headers=HEADERS).status_code==400
        result=client.post('/api/business/connections',json={'collection':'cafe','connection':{
            'expected_revision':1,'provider':'Axis','type':'camera_stream','source_description':'Counter camera','history_hours':48}},headers=HEADERS)
        assert result.status_code==200
        assert result.json()['connections'][0]['status']=='configuration_required'
        assert result.json()['connections'][0]['ingestion_started'] is False


def test_gallery_attachments_reject_wrong_scope_before_native_process(tmp_path):
    app=app_with_sources(tmp_path)
    agent=app.state.agent
    assert 'demo-clip' in str(agent._input('Review this',[],['demo-clip'],'cafe'))
    with pytest.raises(ValueError):agent._input('Review',[],['demo-clip'],'warehouse')
    with pytest.raises(ValueError):agent._input('Review',[],['foreign'],'cafe')
    with TestClient(app) as client:
        client.post('/api/session',json={'code':app.state.access.issue('demo')},headers=HEADERS)
        with patch.object(agent,'turn') as native:
            result=client.post('/api/chat',json={'chat_id':'my-chat','dataset':'cafe','message':'Review','clip_attachments':['foreign']},headers=HEADERS)
            assert result.status_code==400
            native.assert_not_called()
