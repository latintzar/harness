"""Local MCP reads from the same selected workspace registry as hosted chat."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from public_mcp import HTTPMCPServices, MAX_RESULT, _public
from scope_host import bind_admission
from workspace_identity import workspace_scope


class WorkspaceMCP(FastMCP):
    def __init__(self, services, principal, collection):
        self.services = services
        self.principal = principal
        self.collection = collection
        super().__init__('Rocoto', instructions='Read the tools available in this workspace and preserve their provenance.')

    def visible_names(self):
        declared = getattr(self.services.agent.vertical, 'local_read_names', lambda: None)()
        names = self.services.registry.names('local')
        authority = getattr(self.services.agent.vertical, 'local_authority', None)
        if authority is None or not authority(self.principal, self.collection):
            names = names & self.services.registry.reads
        return names if declared is None else names & declared

    async def list_tools(self):
        return [types.Tool(name=spec['name'], description=spec['description'], inputSchema=spec['inputSchema'],
                annotations=types.ToolAnnotations(readOnlyHint=spec['name'] in self.services.registry.reads, destructiveHint=False))
                for spec in self.services.registry.specs(surface='local') if spec['name'] in self.visible_names()]

    async def call_tool(self, name, arguments):
        try:
            if name not in self.visible_names():
                raise ValueError('Choose a read tool in the host-selected workspace.')
            return await self._read(name, arguments)
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            raise ToolError(str(error)) from None

    async def _read(self, name, arguments):
        grant = SimpleNamespace(company_id=workspace_scope(self.services.state),
            owner_principal_id=self.principal, dataset=self.collection)
        context = await asyncio.to_thread(self.services.admit, grant)
        authority = getattr(self.services.agent.vertical, 'local_authority', None)
        if authority is not None:
            from dataclasses import replace
            context = replace(context, local_authority=lambda: authority(self.principal, self.collection))
        with bind_admission(context.admission):
            result = await self.services.registry.invoke(context, name, arguments, surface='local')
        if isinstance(result, tuple) and len(result) == 3:
            content, _, success = result
            if not success:
                raise ValueError('The workspace read could not complete.')
            from native_mcp import NativeMCP
            return NativeMCP._result(content).content
        local_result = getattr(self.services.agent.vertical, 'local_result', lambda name, value: value)
        encoded = json.dumps(_public(local_result(name, result)), allow_nan=False)
        if len(encoded.encode()) > MAX_RESULT:
            raise ValueError('Choose a smaller workspace result.')
        return [types.TextContent(type='text', text=encoded)]


def make_server(state: Path, *, principal_id=None, dataset=None, derivatives_enabled=False, services=None):
    state = Path(state).absolute()
    if principal_id is not None and (not isinstance(principal_id, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', principal_id)):
        raise ValueError('Choose a host-authenticated principal.')
    if dataset is not None and (not isinstance(dataset, str) or not dataset or len(dataset) > 160
            or any(ord(char) < 32 for char in dataset)):
        raise ValueError('Choose a host-selected collection.')
    if bool(principal_id) != bool(dataset):
        raise ValueError('Bind both a principal and a collection for private saved outputs.')
    if type(derivatives_enabled) is not bool or derivatives_enabled and principal_id is None:
        raise ValueError('Bind a principal and collection before enabling commissioned derivatives.')
    if services is None:
        import pack
        selected = pack.active().import_host()
        factory = getattr(selected, 'local_services', None)
        if factory is None:
            from local_workspace import local_services
            factory = local_services
        services = factory(state, principal_id=principal_id, dataset=dataset,
                           derivatives_enabled=derivatives_enabled)
    if not isinstance(services, HTTPMCPServices) or Path(services.state).absolute() != state:
        raise ValueError('Use the selected workspace services.')
    return WorkspaceMCP(services, principal_id or 'local-operator', dataset or 'company')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--principal-id', help='Host-authorized owner of private saved outputs')
    parser.add_argument('--dataset', help='Host-selected collection for private saved outputs')
    parser.add_argument('--enable-derivatives', action='store_true', help='Use the commissioned media broker')
    args = parser.parse_args()
    make_server(args.state, principal_id=args.principal_id, dataset=args.dataset,
                derivatives_enabled=args.enable_derivatives).run(transport='stdio')
