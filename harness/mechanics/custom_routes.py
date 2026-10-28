"""Owner controls over exact proposed targets and host-tested connector versions."""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


PRIVATE = {'Cache-Control':'private, no-store'}


async def body(request, fields):
    raw = bytearray()
    try:
        async with asyncio.timeout(10):
            async for part in request.stream():
                raw.extend(part)
                if len(raw)>4096:
                    raise ValueError()
        value = json.loads(raw)
        if not isinstance(value,dict) or set(value)!=set(fields):
            raise ValueError()
        return value
    except (ValueError, TypeError, TimeoutError):
        raise HTTPException(400,'Use the current connector action.',headers=PRIVATE) from None


def register_custom_routes(app, identity, *, owner_for_connector=None):
    async def selected(request, key, *, write=False):
        owner,_ = await owner_for_connector(request,key,writable=write) if owner_for_connector else identity(request,writable=write)
        workspace = request.state.workspace
        try:
            record = await asyncio.to_thread(workspace['custom_connectors'].get,key,owner=owner)
        except ValueError:
            raise HTTPException(404,'Choose a connector from this workspace.',headers=PRIVATE) from None
        return workspace, {'owner':owner,'collection':record['collection']}

    @app.get('/api/connections/custom/{connector_id}/execution')
    async def execution(connector_id: str, request: Request):
        workspace, scope = await selected(request,connector_id)
        value = await asyncio.to_thread(workspace['custom_setup'].describe,connector_id,**scope)
        if workspace.get('custom_coordinator'):
            value.update(await workspace['custom_coordinator'].status(connector_id,**scope))
        return JSONResponse(value,headers=PRIVATE)

    @app.post('/api/connections/custom/{connector_id}/execution/target')
    async def target(connector_id: str, request: Request):
        workspace, scope = await selected(request,connector_id,write=True)
        value = await body(request,{'proposal_id'})
        if not isinstance(value['proposal_id'],str) or len(value['proposal_id'])>100:
            raise HTTPException(400,'Review the current access proposal.',headers=PRIVATE)
        try:
            result = await asyncio.to_thread(workspace['custom_setup'].approve,connector_id,value['proposal_id'],**scope)
        except ValueError:
            raise HTTPException(409,'Review access for the current connector version.',headers=PRIVATE) from None
        return JSONResponse(result,headers=PRIVATE)

    @app.post('/api/connections/custom/{connector_id}/execution/activate')
    async def activate(connector_id: str, request: Request):
        workspace, scope = await selected(request,connector_id,write=True)
        value = await body(request,{'test_id','expected_active_generation'})
        if not isinstance(value['test_id'],str) or len(value['test_id'])>100 or type(value['expected_active_generation']) is not int:
            raise HTTPException(400,'Choose a verified connector version.',headers=PRIVATE)
        manager = workspace['custom_execution']
        try:
            tested = await asyncio.to_thread(manager.test_result,value['test_id'],**scope)
            if tested['connector_id']!=connector_id:
                raise ValueError()
            result = await asyncio.to_thread(manager.activate,value['test_id'],
                expected_active_generation=value['expected_active_generation'],**scope)
        except ValueError:
            raise HTTPException(409,'Run the current connector checks before activating.',headers=PRIVATE) from None
        return JSONResponse(result,headers=PRIVATE)

    @app.post('/api/connections/custom/{connector_id}/execution/test')
    async def test(connector_id: str, request: Request):
        workspace, scope = await selected(request,connector_id,write=True)
        value = await body(request,{'expected_revision'})
        if type(value['expected_revision']) is not int:
            raise HTTPException(400,'Choose the current connector version.',headers=PRIVATE)
        coordinator = workspace.get('custom_coordinator')
        if coordinator is None:
            raise HTTPException(409,'Custom execution is being commissioned. Continue preparing the connector.',headers=PRIVATE)
        try:
            result = await coordinator.test(connector_id,expected_revision=value['expected_revision'],
                cancelled=asyncio.Event(),**scope)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            result = {'status':'stopped'}
        except ValueError:
            raise HTTPException(409,'Review the connector setup and run its checks again.',headers=PRIVATE) from None
        return JSONResponse(result,headers=PRIVATE)

    @app.post('/api/connections/custom/{connector_id}/execution/revoke')
    async def revoke(connector_id: str, request: Request):
        workspace, scope = await selected(request,connector_id,write=True)
        await body(request,set())
        coordinator = workspace.get('custom_coordinator')
        if coordinator is not None:
            result = await coordinator.revoke(connector_id,**scope)
        else:
            result = await asyncio.to_thread(workspace['custom_execution'].revoke,connector_id,**scope)
        return JSONResponse(result,headers=PRIVATE)

    @app.post('/api/connections/custom/{connector_id}/execution/stop')
    async def stop(connector_id: str, request: Request):
        workspace, scope = await selected(request,connector_id,write=True)
        await body(request,set())
        coordinator = workspace.get('custom_coordinator')
        if coordinator is None:
            raise HTTPException(409,'Connection checks are being prepared.',headers=PRIVATE)
        result = await coordinator.stop(connector_id,**scope)
        return JSONResponse(result,headers=PRIVATE)
