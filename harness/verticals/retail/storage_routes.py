"""Owner-only controls for immutable SFTP folder proposals and acquisition."""
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from chat_permissions import workspace_writable
from custom_coordinator import offload
from custom_routes import body, PRIVATE


def register_storage_routes(app,identity,runtime_for_workspace,*,owner_for_connector=None):
    from retail_urls import retail_router
    app = retail_router(app)
    async def selected(request,key,*,write=False):
        owner,_=await owner_for_connector(request,key,writable=write) if owner_for_connector else identity(request,writable=write)
        workspace=request.state.workspace
        try:
            record=await offload(workspace['custom_connectors'].get,key,owner=owner)
            if record['kind']!='sftp':raise ValueError()
            runtime=await offload(runtime_for_workspace,workspace)
        except ValueError:raise HTTPException(404,'Choose a stored-recording connection.',headers=PRIVATE)from None
        return runtime,{'owner':owner,'collection':record['collection']}
    def public(value,request):
        if not workspace_writable(request.state.session):value={**value,'can_run':False,'can_pause':False}
        return value
    @app.get('/api/retail/storage/{connector_id}')
    async def status(connector_id:str,request:Request):
        runtime,scope=await selected(request,connector_id)
        return JSONResponse(public(await offload(runtime.describe,connector_id,**scope),request),headers=PRIVATE)
    @app.post('/api/retail/storage/{connector_id}/approve')
    async def approve(connector_id:str,request:Request):
        runtime,scope=await selected(request,connector_id,write=True)
        value=await body(request,{'proposal_id'})
        if not isinstance(value['proposal_id'],str)or len(value['proposal_id'])>100:
            raise HTTPException(400,'Review the current folder proposal.',headers=PRIVATE)
        try:result=await runtime.approve(connector_id,value['proposal_id'],**scope)
        except (ValueError,ImportError):raise HTTPException(409,'Review the current folder setup and worker availability.',headers=PRIVATE)from None
        return JSONResponse(result,headers=PRIVATE)
    @app.post('/api/retail/storage/{connector_id}/run')
    async def run(connector_id:str,request:Request):
        runtime,scope=await selected(request,connector_id,write=True)
        value=await body(request,{'expected_generation'})
        if type(value['expected_generation'])is not int:
            raise HTTPException(400,'Choose the current folder version.',headers=PRIVATE)
        try:result=await runtime.run(connector_id,expected_generation=value['expected_generation'],**scope)
        except (ValueError,ImportError):raise HTTPException(409,'Review folder access, worker readiness and the import allowance.',headers=PRIVATE)from None
        return JSONResponse(result,headers=PRIVATE)
    @app.post('/api/retail/storage/{connector_id}/pause')
    async def pause(connector_id:str,request:Request):
        runtime,scope=await selected(request,connector_id,write=True)
        value=await body(request,{'expected_generation'})
        if type(value['expected_generation'])is not int:
            raise HTTPException(400,'Choose the current folder version.',headers=PRIVATE)
        try:result=await runtime.pause(connector_id,expected_generation=value['expected_generation'],**scope)
        except ValueError:raise HTTPException(409,'Refresh the folder connection before pausing.',headers=PRIVATE)from None
        return JSONResponse(result,status_code=200 if result['acquisition']['cleanup_confirmed']else 202,headers=PRIVATE)
