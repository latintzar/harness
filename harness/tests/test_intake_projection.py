"""Host receipts agree in Gallery and native tools without starting execution."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parents[1]))
from source_intake import IntakeActor, SourceStore
from test_skill_runtime import make_app
from test_source_host_routes import commission_fixture
from test_workflow_routes import HEADERS, login


def native(agent, owner='alice', collection='cafe', *, child=False):
    key='intake-'+owner+('-child' if child else '')
    if child:agent.children['parent']={'id':'parent','owner_principal_id':owner,'dataset':collection}
    agent.children[key]={'id':key,'owner_principal_id':owner,'dataset':collection,
                         **({'parent_id':'parent'} if child else {})}
    content, _, success=asyncio.run(agent._tool('get_workspace_capabilities',{},collection,key))
    assert success, content
    return json.loads(content[0]['text'])


def forbid_execution(monkeypatch):
    import source_service
    def forbidden(*args, **kwargs):
        pytest.fail('Readiness started a worker or embedding provider')
    monkeypatch.setattr(source_service,'SourceService',forbidden)
    monkeypatch.setattr(source_service,'SourceEmbedding',forbidden)
    monkeypatch.setattr(source_service,'SourceWorker',forbidden)


def test_browsing_projection_creates_neither_intake_nor_service_store(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    forbid_execution(monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        gallery=client.get('/api/library/imports?collection=cafe').json()
        web=client.get('/api/workspace/capabilities?dataset=cafe').json()
        model=native(app.state.agent)
        assert gallery['intake']==web['intake']==model['intake']
        assert gallery['imports']==web['imports']==model['imports']==[]
        assert model['intake']['admission']=='setup_required'
    assert not (state/'source-intake').exists()
    assert not (state/'source-service').exists()


@pytest.mark.parametrize('terminal',['ready','failed','uncertain','cancelled'])
def test_http_native_share_admission_progress_and_cleanup(tmp_path,monkeypatch,terminal):
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    host=commission_fixture(state);forbid_execution(monkeypatch)
    owner=IntakeActor('alice')
    with TestClient(app) as client:
        login(client,app,'alice')
        def parity(status=None,stage=None,cleanup=False):
            gallery=client.get('/api/library/imports?collection=cafe').json()
            web=client.get('/api/workspace/capabilities?dataset=cafe').json()
            model=native(app.state.agent)
            assert gallery['intake']==web['intake']==model['intake']
            assert gallery['imports']==web['imports']==model['imports']
            if status:
                row=model['imports'][0]
                assert (row['status'],row['stage'],row['cleanup_pending'])==(status,stage,cleanup)
            assert str(state) not in json.dumps(model)
            return model
        assert parity()['intake']['admission']=='ready'
        response=client.post('/api/library/imports',json={'collection':'cafe','filename':'recording.mp4','size':8},
            headers={**HEADERS,'Idempotency-Key':'owned-import'})
        assert response.status_code==201,response.text
        record=response.json()['import'];route='/api/library/imports/'+record['id']
        assert parity('uploading')['intake']['admission']=='reserved'
        (host.root/'heartbeat.json').unlink()
        paused=parity('uploading')['intake']
        assert not paused['worker_ready'] and not paused['can_upload'] and paused['can_manage_imports']
        assert client.patch(route+'/content',content=b'abcdefgh',headers={**HEADERS,
            'Content-Type':'application/octet-stream','Upload-Offset':'0'}).status_code==200
        assert client.post(route+'/complete',json={},headers=HEADERS).status_code==200
        parity('received','prepare')
        store=SourceStore(state)
        lease=store.claim_job(worker_id='synthetic-proof',stages=['prepare'])
        parity('preparing','prepare')
        if terminal=='failed':
            store.fail_job(lease,error_code='invalid_media')
            assert parity('needs_attention')['imports'][0]['error']=='invalid_media'
        elif terminal=='uncertain':
            store.fail_job(lease,error_code='worker_reply_lost',uncertain=True)
            parity('needs_attention',cleanup=True)
        elif terminal=='cancelled':
            store.cancel_import(owner,record['id'])
            parity('cancelling',cleanup=True)
            job=store.get_import(owner,record['id'])['asset']['jobs'][0]
            store.reconcile_job(job['id'],expected_generation=job['generation'],outcome='cancelled',evidence_reference='fixture-stop')
            parity('cancelled')
        else:
            store.finish_job(lease,result={'fixture':'prepared'})
            parity('preparing','embed')
            lease=store.claim_job(worker_id='synthetic-proof',stages=['embed'])
            parity('indexing','embed')
            store.finish_job(lease,result={'fixture':'vectors'})
            parity('indexing','publish')
            lease=store.claim_job(worker_id='synthetic-proof',stages=['publish'])
            store.finish_job(lease,result={'generation_id':'fixture-generation'})
            parity('ready')


def test_owner_collection_observer_and_child_projection(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    commission_fixture(state);store=SourceStore(state)
    for owner,collection in [('alice','cafe'),('alice','company'),('bob','cafe')]:
        actor=IntakeActor(owner);source=store.create_source(actor,collection=collection,name='Source')
        store.begin_upload(actor,source['id'],filename=owner+'-'+collection+'.mp4',size=4,idempotency_key=collection)
    forbid_execution(monkeypatch)
    with TestClient(app) as client:
        login(client,app,'bob') # The native Bob fixture also needs current company membership.
        login(client,app,'alice')
        login(client,app,'owner',owner=True)
        assert client.post('/api/observer',json={'environment_id':'demo','principal_id':'alice'},headers=HEADERS).status_code==200
        gallery=client.get('/api/library/imports?collection=cafe').json()
        observed=client.get('/api/workspace/capabilities?dataset=cafe').json()
        assert observed['imports']==gallery['imports']
        assert [row['filename'] for row in observed['imports']]==['alice-cafe.mp4']
        assert not observed['intake']['can_upload'] and not observed['intake']['can_manage_imports']
        child=native(app.state.agent,child=True)
        assert child['imports']==gallery['imports'] and not child['intake']['can_upload']
        assert not child['intake']['can_manage_imports']
        assert [row['filename'] for row in native(app.state.agent,'bob')['imports']]==['bob-cafe.mp4']
        assert [row['filename'] for row in native(app.state.agent,collection='company')['imports']]==['alice-company.mp4']


def test_actual_native_mcp_reads_the_same_owned_pending_import(tmp_path,monkeypatch):
    from native_mcp import NativeMCP,NativeMCPScope
    from test_native_mcp import client as mcp_client
    from workspace_identity import workspace_scope
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    commission_fixture(state);forbid_execution(monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        response=client.post('/api/library/imports',json={'collection':'cafe','filename':'pending.mp4','size':8},
            headers={**HEADERS,'Idempotency-Key':'protocol'})
        assert response.status_code==201
        expected=client.get('/api/library/imports?collection=cafe').json()
        async def prove():
            agent=app.state.agent
            agent.children['root']={'id':'root','owner_principal_id':'alice','dataset':'cafe'}
            agent.registry['root']='native-root'
            agent.active['root']={'cancel':asyncio.Event()}
            company=workspace_scope(state)
            transport=NativeMCP(agent,NativeMCPScope(company,'alice','cafe','root','native-root'),company_id=company)
            grant=transport.issue()
            try:
                from scope_host import agent_scope
                async with agent_scope(agent,'root','cafe',admit=True),transport.lifespan(),mcp_client(transport,grant.token) as session:
                    result=await session.call_tool('get_workspace_capabilities',{})
                    assert not result.isError
                    actual=json.loads(result.content[0].text)
                    assert actual['intake']==expected['intake'] and actual['imports']==expected['imports']
                    denied=await session.call_tool('get_workspace_capabilities',{'principal':'bob'})
                    assert denied.isError
            finally:agent.active.pop('root',None)
        asyncio.run(prove())


def test_read_only_pagination_and_corrupt_state_are_explicit(tmp_path,monkeypatch):
    from intake_projection import IntakeProjection
    from source_intake import IntakeError
    from workspace_identity import workspace_scope
    state=tmp_path/'company';store=SourceStore(state);owner=IntakeActor('alice')
    source=store.create_source(owner,collection='cafe',name='Own source')
    for i in range(3):
        store.begin_upload(owner,source['id'],filename=f'{i}.mp4',size=4,idempotency_key=str(i))
    view=IntakeProjection(state,company_id=workspace_scope(state),collections=lambda:['cafe'])
    def forbidden(*args,**kwargs):pytest.fail('Readiness initialized SourceStore')
    monkeypatch.setattr(SourceStore,'__init__',forbidden)
    first=view.snapshot('alice','cafe',limit=2)
    second=view.snapshot('alice','cafe',limit=2,offset=first['next_offset'])
    assert len(first['imports'])==2 and len(second['imports'])==1 and second['next_offset'] is None
    assert not ({row['id'] for row in first['imports']}&{row['id'] for row in second['imports']})
    assert view.snapshot('bob','cafe')['imports']==[]
    assert not (state/'source-service').exists()
    with pytest.raises(IntakeError,match='invalid_query'):view.snapshot('alice','cafe',limit=201)
    with pytest.raises(IntakeError,match='read_only_workspace'):view.host.save({})
    store.database.write_bytes(b'corrupt-source-records')
    unavailable=view.snapshot('alice','cafe')
    assert unavailable['intake']['status']=='unavailable'
    assert unavailable['intake']['admission']=='status_unavailable' and unavailable['imports']==[]


def test_removed_or_invalid_configuration_retains_owned_continuation(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    commission_fixture(state);forbid_execution(monkeypatch)
    with TestClient(app) as client:
        login(client,app,'alice')
        response=client.post('/api/library/imports',json={'collection':'cafe','filename':'retained.mp4','size':8},
            headers={**HEADERS,'Idempotency-Key':'retained'})
        assert response.status_code==201
        config=state/'compute/source-intake-service.json'
        config.unlink()
        for invalid in (False,True):
            if invalid:config.write_text('{');config.chmod(0o600)
            gallery=client.get('/api/library/imports?collection=cafe').json()
            model=native(app.state.agent)
            assert model['intake']==gallery['intake'] and model['imports']==gallery['imports']
            assert not model['intake']['can_upload'] and model['intake']['can_manage_imports']
            assert model['imports'][0]['filename']=='retained.mp4'
            assert model['intake']['admission']==('status_unavailable' if invalid else 'setup_required')


def test_pending_admitted_collection_before_index_and_foreign_company(tmp_path,monkeypatch):
    from test_source_service import health
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    host=commission_fixture(state);path=state/'compute/source-intake-service.json'
    config=json.loads(path.read_text());config['collection']='new-recordings'
    path.write_text(json.dumps(config));health(host)
    host.begin(SourceStore(state),IntakeActor('alice'),collection='new-recordings',filename='pending-new.mp4',size=8,idempotency_key='pending')
    forbid_execution(monkeypatch)
    app.state.access.create_environment('foreign','Foreign')
    with TestClient(app) as client:
        login(client,app,'alice')
        gallery=client.get('/api/library/imports?collection=new-recordings')
        capabilities=client.get('/api/workspace/capabilities?dataset=new-recordings')
        assert gallery.status_code==capabilities.status_code==200
        assert gallery.json()['imports']==capabilities.json()['imports']
        assert not capabilities.json()['footage']['search_ready']
        assert capabilities.json()['imports'][0]['filename']=='pending-new.mp4'
        login(client,app,'bob')
        assert client.get('/api/library/imports?collection=new-recordings').status_code==404
        assert client.get('/api/workspace/capabilities?dataset=new-recordings').status_code==400
        assert client.post('/api/session',json={'code':app.state.access.issue('foreign',principal_id='alice')},headers=HEADERS).status_code==200
        foreign=client.get('/api/workspace/capabilities?dataset=cafe').json()
        assert foreign['imports']==[] and foreign['intake']['admission']=='setup_required'
        assert 'pending-new.mp4' not in json.dumps(foreign)
        assert not (state/'environments/foreign/source-intake').exists()


def test_allowance_refusal_is_metadata_and_does_not_claim_it(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch);state=tmp_path/'state'
    host=commission_fixture(state);forbid_execution(monkeypatch)
    approval=Path(host.configuration().approval_path)
    original=approval.read_bytes();approval.write_text('{}')
    with TestClient(app) as client:
        login(client,app,'alice')
        data=client.get('/api/workspace/capabilities?dataset=cafe').json()
        assert data['intake']['admission']=='allowance_required'
        assert data['intake']['worker_ready'] and not data['intake']['can_upload']
        assert str(approval) not in json.dumps(data) and not host.records()
        assert approval.read_text()=='{}'
        approval.write_bytes(original)
        assert client.get('/api/workspace/capabilities?dataset=cafe').json()['intake']['can_upload']
    assert not list(approval.parent.glob('*.claimed.json'))
