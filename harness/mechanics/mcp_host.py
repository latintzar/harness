"""Authenticated browser grants and host-only company resolution for public MCP."""
from __future__ import annotations
from access import page_request

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chat_permissions import ChatPermissions, workspace_writable
from mcp_access import MCPGrantStore, GrantError
from public_mcp import HTTPMCP, HTTPMCPServices
from workspace_identity import workspace_scope

PRIVATE = {'Cache-Control': 'private, no-store'}


async def _read(function, *args, **kwargs):
    """Retain the transport's admission until a cancelled host read finishes."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


class NewConnection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    dataset: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    label: str = Field(default='MCP connection', min_length=1, max_length=80)
    ttl_seconds: int = Field(default=7*86400, ge=60, le=7*86400)


class MCPHost:
    def __init__(self, state, access, services_for_environment, *, public_origin=''):
        self.state = Path(state)
        self.access = access
        self.services_for_environment = services_for_environment
        self.store = MCPGrantStore(state)
        self.url = public_origin.rstrip('/') + '/mcp' if public_origin else None
        self.transport = HTTPMCP(self.store, self.resolve, resource_url=self.url) if self.url else None

    def _environment(self, company_id, principal):
        # Only host-saved membership rows select paths. Tokens carry an opaque
        # company identity; a guessed environment or filesystem path grants nothing.
        with self.access.connect() as db:
            rows = db.execute('SELECT e.id FROM environments e JOIN principal_environments p '
                'ON p.environment=e.id JOIN principals person ON person.id=p.principal_id '
                'WHERE p.principal_id=? AND p.role IN (?,?)',
                (principal, 'owner', 'operator')).fetchall()
        for row in rows:
            key = row['id']
            root = self.state if key == 'demo' else self.state/'environments'/key
            if root.is_dir() and workspace_scope(root) == company_id:
                return key
        return None

    async def resolve(self, grant):
        environment = await _read(self._environment, grant.company_id, grant.owner_principal_id)
        if environment is None:
            return None
        services = await _read(self.services_for_environment, environment)
        try:
            context = await _read(services.admit, grant)
            if not context.admission.current():
                return None
        except (ValueError, OSError):
            return None
        return services

    def register_routes(self, app):
        def actor(request, *, writable=False):
            session = getattr(request.state, 'session', None)
            principal = ChatPermissions.principal(session)
            if not principal or writable and not workspace_writable(session):
                raise HTTPException(403, 'Use your own workspace to manage MCP access.', headers=PRIVATE)
            selected = request.state.workspace
            if writable:
                origin = request.headers.get('origin')
                if not page_request(request.headers) or origin and urlsplit(origin).netloc != request.headers.get('host'):
                    raise HTTPException(403, 'Open this action from your Rocoto workspace.', headers=PRIVATE)
            return principal, selected

        @app.get('/api/mcp/connections')
        async def connections(request: Request):
            principal, selected = actor(request)
            company = await asyncio.to_thread(workspace_scope, selected['state'])
            records = await asyncio.to_thread(self.store.list, company_id=company, owner_principal_id=principal)
            return JSONResponse({'available': self.transport is not None, 'url': self.url,
                'connections': [row.public() for row in records]}, headers=PRIVATE)

        @app.post('/api/mcp/connections')
        async def create_connection(request: Request):
            principal, selected = actor(request, writable=True)
            if self.transport is None:
                raise HTTPException(503, 'Configure the public Rocoto address to connect an MCP client.', headers=PRIVATE)
            raw = bytearray()
            try:
                async with asyncio.timeout(5):
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > 2048:
                            raise ValueError()
                body = NewConnection.model_validate(json.loads(raw))
            except (ValueError, ValidationError, TimeoutError):
                raise HTTPException(400, 'Choose a collection, connection name and expiry within seven days.', headers=PRIVATE) from None
            company = await asyncio.to_thread(workspace_scope, selected['state'])
            if await asyncio.to_thread(self._environment, company, principal) is None:
                raise HTTPException(403, 'Use an active company membership to connect a client.', headers=PRIVATE)
            if body.dataset not in selected['agent'].vertical.collection_names(principal):
                raise HTTPException(400, 'Choose an available footage collection.', headers=PRIVATE)
            try:
                grant, token = await asyncio.to_thread(self.store.issue, company_id=company,
                    owner_principal_id=principal, **body.model_dump())
            except GrantError as error:
                raise HTTPException(400, str(error), headers=PRIVATE) from None
            # Return once in the authorized private response. Persist only the
            # digest; list/reload and normal chat/tool history never contain it.
            return JSONResponse({'connection': grant.public(), 'url': self.url, 'token': token},
                status_code=201, headers=PRIVATE)

        @app.delete('/api/mcp/connections/{key}')
        async def revoke_connection(key: str, request: Request):
            principal, selected = actor(request, writable=True)
            company = await asyncio.to_thread(workspace_scope, selected['state'])
            try:
                grant = await asyncio.to_thread(self.store.revoke, key, company_id=company, owner_principal_id=principal)
            except GrantError:
                raise HTTPException(404, 'Choose an MCP connection belonging to this account.', headers=PRIVATE) from None
            return JSONResponse({'connection': grant.public()}, headers=PRIVATE)
