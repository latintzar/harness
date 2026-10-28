"""Company image funding, host permissions and actual cover/artifact persistence."""
import asyncio
import base64
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parents[1]))
from access import AccessStore
from agent_auth import AgentAuth
from retail_business import RetailBusinessStore as BusinessStore
from chat_files import ChatFiles
from company_usage import CompanyUsage
from profile_cover_host import install_profile_covers,register_routes,tool_specs
from profile_covers import CoverError
import profile_image_usage as usage


def fixture(tmp_path,*,funded=True,change=None,hook=None):
    state=tmp_path/'company';state.mkdir()
    access=AccessStore(state);access.issue('demo',owner=True,principal_id='alice',display_name='Alex')
    access.issue('demo',principal_id='bob',display_name='Sam')
    index=SimpleNamespace(clips=[{'dataset':'cafe'}],status=lambda:{'datasets':['cafe']})
    business=BusinessStore(state,index);business.update('cafe',0,{'name':'Harbour Café','type':'Coffee shop','location':'Port','description':'A quiet waterfront café'})
    files=ChatFiles(state);children={key:{'id':key,'owner_principal_id':owner,'dataset':'cafe','parent_id':None}for key,owner in [('chat','alice'),('bob-chat','bob')]}
    agent=SimpleNamespace(children=children,_files=lambda:files)
    selected={'state':state,'environment_id':'demo','index':index,'business':business,'agent':agent,
        'auth':AgentAuth(state,mode='openai_api',environ={'ROCOTO_OPENAI_API_KEY':'private-fixture'}),
        'chats':SimpleNamespace(principal=lambda session:session['principal']['id'])}
    pool=CompanyUsage.openai_pool(state)
    if funded:
        with pool.connect()as db:pool._fund(db,'fixture',2_000_000,'prepaid_customer_funds','fixture')
        pool.set_monthly_limit(0,'2')
    output=io.BytesIO();Image.new('RGB',(1536,1024),'cornflowerblue').save(output,format='PNG')
    image=output.getvalue();calls=[];events=[];live=[True]
    async def provider(request):
        assert pool.summary()['reserved_usd']==usage.MAXIMUM_USD
        body=json.loads(request.content);calls.append(body)
        assert body['model']==usage.MODEL and body['quality']=='low'and body['n']==1
        if hook:hook(selected,live)
        payload={'created':1791244800,'data':[{'b64_json':base64.b64encode(image).decode()}],
            'size':'1536x1024','quality':'low','output_format':'png',
            'usage':{'input_tokens':350,'input_tokens_details':{'text_tokens':350,'image_tokens':0},'output_tokens':160,'total_tokens':510}}
        if change:change(payload)
        return httpx.Response(200,headers={'x-request-id':'req_coverfixture'+str(len(calls))},json=payload)
    host=install_profile_covers(selected,access,transport=httpx.MockTransport(provider))
    return SimpleNamespace(state=state,access=access,pool=pool,selected=selected,host=host,files=files,calls=calls,events=events,live=live)


def generate(f,**kwargs):
    fields={'owner':'alice','actor_id':'alice','chat_id':'chat','collection':'cafe','turn_id':'turn-1','current':lambda:f.live[0],'emit':f.events.append,**kwargs}
    args=fields.pop('args',{'target':'business','expected_profile_revision':1})
    return asyncio.run(f.host.tool('generate_profile_cover',args,**fields))


def test_generation_settles_real_ledger_and_persists_private_cover(tmp_path):
    f=fixture(tmp_path);result=generate(f)
    assert result['cover']['target']=='business'and result['model']==usage.MODEL
    assert f.pool.summary()['reserved_usd']=='0.000000'
    assert f.pool.summary()['provider_actual_cost_usd']=='0.006550'
    assert [e['type']for e in f.events]==['profile_cover_progress','profile_cover_progress','profile_cover']
    assert 'Harbour Café'in f.calls[0]['prompt'] and 'Monet'in f.calls[0]['prompt']
    assert 'private-fixture'not in json.dumps(result)
    fresh=install_profile_covers(f.selected,f.access)
    assert fresh.business('bob','cafe',f.selected['business'].get('cafe'))['cover']==result['cover']
    original=f.files.download(result['file']['id'],owner_principal_id='alice',chat_id='chat',dataset='cafe')[1]
    assert original.startswith(b'\x89PNG')
    receipts=list((f.pool.state/'profile-images').glob('*.usage.json'));assert len(receipts)==1
    receipt=json.loads(receipts[0].read_text());assert receipt['kind']=='images_usage'
    assert receipt['endpoint']=='/v1/images/generations'
    assert usage.settle(f.pool,receipt['reservation_id'],receipts[0])['provider_actual_cost_usd']=='0.006550'
    assert generate(f)['cached']is True and len(f.calls)==1


def test_member_cover_is_self_and_profile_response_is_enriched(tmp_path):
    f=fixture(tmp_path);result=generate(f,owner='bob',actor_id='bob',chat_id='bob-chat',args={'target':'member','direction':'Soft seaside light'})
    assert result['cover']['target_id']=='bob'
    rows=f.host.members('alice','cafe',[{'id':'alice'},{'id':'bob'}]);assert rows[0]['cover']is None and rows[1]['cover']==result['cover']
    assert 'Sam'in f.calls[0]['prompt']


@pytest.mark.parametrize('kwargs',[{'actor_id':'bob'},{'owner':'bob'},{'chat_id':'unknown'},
    {'args':{'target':'business','expected_profile_revision':0}},
    {'args':{'target':'member','member_id':'bob'}},
    {'args':{'target':'business','expected_profile_revision':1,'direction':'x'*401}}])
def test_authority_and_input_rejection_before_paid_call(tmp_path,kwargs):
    f=fixture(tmp_path)
    with pytest.raises(ValueError):generate(f,**kwargs)
    assert not f.calls and f.pool.summary()['reserved_usd']=='0.000000'


def test_funding_required_and_child_chats_refused(tmp_path):
    f=fixture(tmp_path,funded=False)
    with pytest.raises(ValueError):generate(f)
    assert not f.calls
    f.selected['agent'].children['chat']['parent_id']='parent'
    with pytest.raises(ValueError):generate(f)
    assert not f.calls


@pytest.mark.parametrize('change',[
    lambda p:p.pop('usage'),
    lambda p:p['usage'].update(output_tokens=-1),
    lambda p:p['usage']['input_tokens_details'].update(image_tokens=1),
    lambda p:p['usage'].update(input_tokens_details={'text_tokens':350,'image_tokens':0,'new_unknown_billable':1}),
])
def test_ambiguous_usage_retains_hold_and_blocks_retry(tmp_path,change):
    f=fixture(tmp_path,change=change)
    with pytest.raises(ValueError):generate(f)
    assert f.pool.summary()['reserved_usd']==usage.MAXIMUM_USD
    assert (f.pool.state/'profile-images/admission-review-required.json').exists()
    assert list((f.pool.state/'profile-images').glob('*.provider-usage.json'))
    with pytest.raises(ValueError):generate(f)
    assert len(f.calls)==1


def test_cancellation_and_membership_revocation_settle_then_skip_publication(tmp_path):
    def stop(selected,live):live[0]=False
    f=fixture(tmp_path,hook=stop)
    with pytest.raises(ValueError):generate(f)
    assert f.pool.summary()['provider_actual_cost_usd']=='0.006550'and f.pool.summary()['reserved_usd']=='0.000000'
    assert f.host.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe')is None
    with f.files.connect()as db:assert db.execute('SELECT COUNT(*) FROM files').fetchone()[0]==0


def test_provider_bound_exceeded_records_cost_and_stops_further_images(tmp_path):
    f=fixture(tmp_path,change=lambda p:p['usage'].update(output_tokens=8193,total_tokens=8543))
    with pytest.raises(ValueError):generate(f)
    assert f.pool.summary()['reserved_usd']=='0.000000'
    assert (f.pool.state/'profile-images/admission-review-required.json').exists()
    assert f.host.store.get(actor_id='alice',target='business',target_id='cafe',collection='cafe')is None


def test_actual_host_routes_serve_saved_cover_and_recheck_membership(tmp_path):
    f=fixture(tmp_path);result=generate(f);app=FastAPI();principal=['bob']
    @app.middleware('http')
    async def session(request,next):
        request.state.workspace=f.selected;request.state.session={'authenticated':True,'principal':{'id':principal[0]}}
        return await next(request)
    register_routes(app);client=TestClient(app)
    response=client.get(result['cover']['url']);assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
    with f.access.connect()as db:db.execute('DELETE FROM principal_environments WHERE principal_id=?',('bob',))
    assert client.get(result['cover']['url']).status_code==403


def test_receipt_mutations_cannot_cross_scope_or_recharge(tmp_path):
    f=fixture(tmp_path);generate(f)
    receipt_path=next((f.pool.state/'profile-images').glob('*.usage.json'));receipt=json.loads(receipt_path.read_text())
    from openai_gateway import _private_json
    for field,value in [('parent_company_scope','other'),('provider_actual_usd','0.000001'),('provider_request_id','req_another')]:
        changed={**receipt,field:value};path=receipt_path.with_name('changed-'+field+'.json');_private_json(path,changed)
        with pytest.raises(ValueError):usage.settle(f.pool,receipt['reservation_id'],path)
    assert f.pool.summary()['provider_actual_cost_usd']=='0.006550'


def test_actual_native_bridge_generates_enriches_and_rejects_children(tmp_path):
    from agent_bridge import CodexVideoAgent,tool_specs as bridge_specs
    from test_server_access import FixtureIndex
    async def scenario():
        f=fixture(tmp_path);agent=CodexVideoAgent(f.state,FixtureIndex(),lambda:{})
        agent.children=f.selected['agent'].children;agent.file_store=f.files;agent.business_store=f.selected['business']
        agent.auth=f.selected['auth'];f.selected['agent']=agent
        install_profile_covers(f.selected,f.access,transport=f.host.transport)
        agent.active['chat']={'cancel':asyncio.Event(),'generation':'turn-bridge','tool_events':asyncio.Queue()}
        for name in ['publish_profile_cover','generate_profile_cover']:
            assert name in {row['name']for row in bridge_specs()}
            assert name not in {row['name']for row in bridge_specs(False)}
        parts,_,ok=await agent._tool('generate_profile_cover',{'target':'business','expected_profile_revision':1},'cafe','chat')
        assert ok,parts
        result=json.loads(parts[0]['text']);assert result['cover']['target']=='business'
        parts,_,ok=await agent._tool('get_business_profile',{},'cafe','chat')
        assert ok and json.loads(parts[0]['text'])['cover']==result['cover']
        event_types=[]
        while not agent.active['chat']['tool_events'].empty():event_types.append(agent.active['chat']['tool_events'].get_nowait()['type'])
        assert event_types==['profile_cover_progress','profile_cover_progress','profile_cover']
        agent.children['child']={'id':'child','dataset':'cafe','owner_principal_id':'alice','parent_id':'chat'}
        agent.active['child']={'cancel':asyncio.Event(),'generation':'turn-child'}
        assert not(await agent._tool('generate_profile_cover',{'target':'member'},'cafe','child'))[2]
        agent.active['chat']['cancel'].set()
        assert not(await agent._tool('generate_profile_cover',{'target':'member'},'cafe','chat'))[2]
        assert len(f.calls)==1
    asyncio.run(scenario())


def test_cancel_before_remote_releases_funds_and_can_be_retried(tmp_path):
    f=fixture(tmp_path)
    def cancel(event):f.live[0]=False
    with pytest.raises(ValueError):generate(f,emit=cancel)
    assert not f.calls and f.pool.summary()['reserved_usd']=='0.000000'
    f.live[0]=True
    assert generate(f)['cover']['target']=='business'


def test_membership_revoked_during_paid_generation_retains_cost_and_private_receipt(tmp_path):
    def revoke(selected,live):
        with sqlite3.connect(selected['state']/'access.sqlite')as db:
            db.execute('DELETE FROM principal_environments WHERE principal_id=?',('alice',))
    f=fixture(tmp_path,hook=revoke)
    with pytest.raises(ValueError):generate(f)
    assert f.pool.summary()['provider_actual_cost_usd']=='0.006550'
    with f.host.store.connect()as db:assert db.execute('SELECT COUNT(*) FROM covers').fetchone()[0]==0
    assert list((f.pool.state/'profile-images').glob('*.usage.json'))


def test_saved_manual_cover_is_preserved_without_image_provider_call(tmp_path):
    f=fixture(tmp_path)
    import hashlib
    output=io.BytesIO();Image.new('RGB',(200,100),'red').save(output,format='PNG');data=output.getvalue();sha=hashlib.sha256(data).hexdigest()
    file=f.files.import_native(io.BytesIO(data),name='chosen-cover.png',size_bytes=len(data),sha256=sha,
        publication_key=sha,provenance={'fixture':'chosen artwork'},owner_principal_id='alice',chat_id='chat',dataset='cafe')
    selected=f.host.store.publish(f.files,f.selected['business'],file_id=file['id'],target='business',owner='alice',actor_id='alice',
        chat_id='chat',collection='cafe',expected_profile_revision=1,current=lambda:True,created=[])
    result=generate(f);assert result['existing']is True and result['cover']==selected
    assert not f.calls and f.pool.summary()['provider_actual_cost_usd']=='0.000000'


def test_real_server_business_and_members_use_private_persisted_covers(tmp_path,monkeypatch):
    import hashlib
    from test_server_access import fixture_app
    app=fixture_app(tmp_path,monkeypatch);headers={'X-Video-Demo':'1'}
    selected=app.state.workspaces['demo'];host=selected['profile_covers']
    files=ChatFiles(selected['state']);selected['agent']._files=lambda:files
    selected['agent'].children['cover-chat']={'id':'cover-chat','owner_principal_id':'alice','dataset':'cafe','parent_id':None}
    output=io.BytesIO();Image.new('RGB',(200,100),'cadetblue').save(output,format='PNG')
    image=output.getvalue();sha=hashlib.sha256(image).hexdigest()
    with TestClient(app)as alice,TestClient(app)as bob:
        for client,principal in [(alice,'alice'),(bob,'bob')]:
            assert client.post('/api/session',json={'code':app.state.access.issue(
                'demo',principal_id=principal,owner=principal=='alice')},headers=headers).status_code==200
        revision=selected['business'].get('cafe')['profile_revision']
        cover_file=files.import_native(io.BytesIO(image),name='profile-cover.png',size_bytes=len(image),sha256=sha,
            publication_key=sha,provenance={'fixture':'chosen artwork'},owner_principal_id='alice',chat_id='cover-chat',dataset='cafe')
        covers={}
        for target in ['business','member']:
            args={'target':target,'file_id':cover_file['id']}
            if target=='business':args['expected_profile_revision']=revision
            covers[target]=asyncio.run(host.tool('publish_profile_cover',args,owner='alice',actor_id='alice',
                chat_id='cover-chat',collection='cafe',turn_id='publication',current=lambda:True))['cover']
        # Reinstallation models a worker reload: profile metadata and sanitized
        # private image bytes must survive without a process-local cache.
        install_profile_covers(selected,app.state.access)
        business=bob.get('/api/business?collection=cafe')
        assert business.status_code==200 and business.json()['cover']==covers['business']
        messaging=alice.get('/api/messaging?collection=cafe')
        assert messaging.status_code==200,messaging.text
        member=next(row for row in messaging.json()['members']if row['id']=='alice')
        assert member['cover']==covers['member']
        for cover in covers.values():
            response=bob.get(cover['url'])
            assert response.status_code==200 and response.content.startswith(b'\x89PNG')
            assert response.headers['cache-control']=='private, no-store'
        with app.state.access.connect()as db:
            db.execute('DELETE FROM principal_environments WHERE principal_id=?',('bob',))
        assert bob.get(covers['business']['url']).status_code==403


@pytest.mark.parametrize('stage',['before_commit','after_commit'])
def test_publication_yields_to_stop_and_drains_cleanup(tmp_path,monkeypatch,stage):
    import threading
    async def scenario():
        f=fixture(tmp_path);entered=threading.Event();release=threading.Event();original=f.host.store.publish
        def held_publication(*args,**kwargs):
            result=original(*args,**kwargs)if stage=='after_commit'else None
            entered.set()
            assert release.wait(3),'Stop must run while image publication is in progress'
            return result if stage=='after_commit'else original(*args,**kwargs)
        monkeypatch.setattr(f.host.store,'publish',held_publication)
        task=asyncio.create_task(f.host.tool('generate_profile_cover',{'target':'business','expected_profile_revision':1},
            owner='alice',actor_id='alice',chat_id='chat',collection='cafe',turn_id='turn-stop',current=lambda:f.live[0],emit=f.events.append))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            f.live[0]=False;task.cancel()
            await asyncio.sleep(0)
            task.cancel() # Repeated stops retain ownership of in-flight cleanup.
        finally:release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert f.pool.summary()['provider_actual_cost_usd']=='0.006550'
        with f.host.store.connect()as db:assert db.execute('SELECT COUNT(*) FROM covers').fetchone()[0]==0
        with f.files.connect()as db:assert db.execute('SELECT COUNT(*) FROM files').fetchone()[0]==0
        assert 'profile_cover'not in[e['type']for e in f.events]
    asyncio.run(scenario())
