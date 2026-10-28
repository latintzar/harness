"""Selected channel callbacks preserve provider signatures and member identity."""
import hashlib
import hmac
import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import pack
import server
from capability_providers import CapabilityConfiguration, ViberBot
from workspace_identity import workspace_scope


def test_viber_uses_ordinary_messaging_enrollment_and_signed_http_ingress(tmp_path):
    sent=[]
    token='v'*32
    def factory(state, environment):
        config=CapabilityConfiguration('viber',workspace_scope(state),environment,'bot_fixture','Rocoto',
            'https://harness.example.test/api/messaging/webhook/demo/viber',True,True)
        def transport(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200,json={'status':0,'message_token':len(sent)})
        return ViberBot(config,lambda:{'auth_token':token},transport=httpx.MockTransport(transport))
    with pack.use('bare'):
        app=server.create_app(tmp_path/'state', builtin_provider_factories={'viber':factory},
            phone_spend_factory=lambda *args:lambda:{'company_daily_messages':20,'agent_daily_messages':10,'stopped':False})
        selected=app.state.workspaces['demo']
        async def offline_turn(*args,**kwargs):
            yield {'type':'done','status':'completed'}
        selected['agent']._turn=offline_turn
        with TestClient(app) as client:
            headers={'X-Video-Demo':'1'}
            code=app.state.access.issue('demo',owner=True,principal_id='alice',display_name='Alice')
            assert client.post('/api/session',json={'code':code},headers=headers).status_code==200
            response=client.post('/api/messaging/verification',json={'phone':'viber_alice','member_id':'alice',
                'collection':'company','channel':'viber'},headers=headers)
            assert response.status_code==200,response.text
            challenge=response.json()['id']
            code=re.search(r'\b\d{6}\b',sent[-1]['text']).group()
            response=client.post('/api/messaging/verification/confirm',json={'challenge_id':challenge,
                'code':code,'member_id':'alice','collection':'company'},headers=headers)
            assert response.status_code==200,response.text
            raw=json.dumps({'event':'message','timestamp':int(__import__('time').time()*1000),
                'message_token':73,'sender':{'id':'viber_alice'},'message':{'type':'text','text':'Ready for review'}}).encode()
            signature=hmac.new(token.encode(),raw,hashlib.sha256).hexdigest()
            response=client.post('/api/messaging/webhook/demo/viber',content=raw,
                headers={'X-Viber-Content-Signature':signature})
            assert response.status_code==200 and response.json()['accepted']==1,response.text
            forged=client.post('/api/messaging/webhook/demo/viber',content=raw+b' ',
                headers={'X-Viber-Content-Signature':signature})
            assert forged.status_code==403
            assert selected['builtins'].status()['viber']['host_ready']


def test_cold_workspace_callback_verifies_signature_before_loading_runtime(tmp_path):
    token='v'*32
    constructed=[]
    def factory(state, environment):
        constructed.append(environment)
        config=CapabilityConfiguration('viber',workspace_scope(state),environment,'bot_fixture','Rocoto',
            f'https://harness.example.test/api/messaging/webhook/{environment}/viber',True,True)
        return ViberBot(config,lambda:{'auth_token':token})
    with pack.use('bare'):
        app=server.create_app(tmp_path/'state',builtin_provider_factories={'viber':factory})
        app.state.access.create_environment('cold','Cold workspace')
        raw=json.dumps({'event':'webhook','timestamp':int(__import__('time').time()*1000)}).encode()
        signature=hmac.new(token.encode(),raw,hashlib.sha256).hexdigest()
        with TestClient(app) as client:
            assert 'cold' not in app.state.workspaces
            forged=client.post('/api/messaging/webhook/cold/viber',content=raw,
                headers={'X-Viber-Content-Signature':'bad'})
            assert forged.status_code==403 and 'cold' not in app.state.workspaces
            absent=client.post('/api/messaging/webhook/unregistered/viber',content=raw,
                headers={'X-Viber-Content-Signature':signature})
            assert absent.status_code==403 and 'unregistered' not in constructed
            received=client.post('/api/messaging/webhook/cold/viber',content=raw,
                headers={'X-Viber-Content-Signature':signature})
            assert received.status_code==200,received.text
            assert 'cold' in app.state.workspaces


def test_startup_discovers_unprocessed_messaging_without_background_agent_store(tmp_path):
    import asyncio
    import time
    from messaging import Messaging,MessagingActor
    token='v'*32
    sent=[]
    def factory(state, environment):
        config=CapabilityConfiguration('viber',workspace_scope(state),environment,'bot_fixture','Rocoto',
            f'https://harness.example.test/api/messaging/webhook/{environment}/viber',True,True)
        def transport(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200,json={'status':0,'message_token':len(sent)})
        return ViberBot(config,lambda:{'auth_token':token},transport=httpx.MockTransport(transport))
    with pack.use('bare'):
        state=tmp_path/'state'
        app=server.create_app(state,builtin_provider_factories={'viber':factory})
        app.state.access.create_environment('cold','Cold workspace')
        app.state.access.issue('cold',principal_id='alice',display_name='Alice')
        root=state/'environments'/'cold'
        messaging=Messaging(root,company_id=workspace_scope(root),adapters=[factory(root,'cold')],authorized=lambda _:True)
        actor=MessagingActor(workspace_scope(root),'alice','company','saved-chat','alice')
        challenge=asyncio.run(messaging.request_code(actor,'viber_alice',channel='viber'))
        code=re.search(r'\b\d{6}\b',sent[-1]['text']).group()
        messaging.verify_code(actor,challenge['id'],code)
        raw=json.dumps({'event':'message','timestamp':int(time.time()*1000),'message_token':73,
            'sender':{'id':'viber_alice'},'message':{'type':'text','text':'Pending reply'}}).encode()
        signature=hmac.new(token.encode(),raw,hashlib.sha256).hexdigest()
        assert messaging.handle_webhook(raw,{'X-Viber-Content-Signature':signature},channel='viber')['accepted']==1
        assert not (root/'background-agents.sqlite').exists()
        with TestClient(app):
            deadline=time.monotonic()+5
            while 'cold' not in app.state.workspaces and time.monotonic()<deadline:
                time.sleep(.02)
            assert 'cold' in app.state.workspaces
            with app.state.workspaces['cold']['messaging']._db() as db:
                assert db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0]==1
        assert len(sent)==1
