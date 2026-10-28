"""Real SDK response publication is bound to the admitted location revision."""
import threading
import pytest
from test_public_mcp import setup,legacy,issue,client,headers,rpc,URL
from retail_scope_views import selected_index_view

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
@pytest.mark.parametrize('after_checks',[1,2])
async def test_partial_revocation_after_read_cannot_publish_old_mcp_evidence(tmp_path,monkeypatch,after_checks):
    app,store,services,raw,access=setup(tmp_path,[legacy('secret-location'),legacy('retained-location')])
    grant,token=issue(store,services)
    revision=[0];read_done=threading.Event();checks=[0]
    def scope_filter(view,principal):
        pinned=revision[0]
        value=selected_index_view(view,[i for i,row in enumerate(view.clips) if not pinned or row['id']=='retained-location'])
        value.business_scope_current=lambda:revision[0]==pinned
        return value
    services.agent.retrieval.scope_filter=scope_filter
    original_read=app._read;original_resolve=app.resolve_scope
    def read(*args):
        result=original_read(*args);read_done.set();return result
    def resolve(value):
        if read_done.is_set():
            checks[0]+=1
            if checks[0]>=after_checks:revision[0]=1
        return original_resolve(value)
    monkeypatch.setattr(app,'_read',read);app.resolve_scope=resolve
    async with app.lifespan(),client(app,token) as (_,sid,http):
        response=await http.post(URL,headers=headers(token,sid()),json=rpc('tools/call',{'name':'list_cameras','arguments':{}}))
        assert checks[0]>=after_checks
        assert response.status_code==401,response.text
        assert 'secret-location' not in response.text
        assert not app.calls

@pytest.mark.anyio
async def test_grant_revoked_after_headers_suppresses_body(tmp_path):
    app,store,services,_,_=setup(tmp_path)
    grant,token=issue(store,services);armed=[False]
    async def wrapped(scope,receive,send):
        async def boundary(message):
            if armed[0] and message['type']=='http.response.start' and message['status']==200:
                armed[0]=False
                store.revoke(grant.id,company_id=grant.company_id,owner_principal_id=grant.owner_principal_id)
            await send(message)
        await app(scope,receive,boundary)
    async with app.lifespan(),client(wrapped,token) as (_,sid,http):
        armed[0]=True
        response=await http.post(URL,headers=headers(token,sid()),json=rpc('tools/call',{'name':'list_cameras','arguments':{}}))
        assert not armed[0] and store.authenticate(token) is None
        assert response.status_code==200 and response.content==b''
        assert not app.calls
