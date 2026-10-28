"""Synthetic company data supplied entirely by the selected vertical folder."""
from contextlib import asynccontextmanager, closing
from pathlib import Path
import sqlite3
import sys

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from host import BareWorkspace
from tool_families import ToolContext

READ_NAMES = frozenset({'manufacturer_orders'})
WRITE_NAMES = frozenset()


def specs(write=True):
    return [{'name': 'manufacturer_orders', 'description': 'Read this company’s current orders.',
        'inputSchema': {'type': 'object', 'properties': {
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
            'additionalProperties': False}}]


def read(context, name, arguments):
    context.require_current()
    selected = context.extension.selected
    company = selected['business_scope'].company_id
    if (name != 'manufacturer_orders' or context.collection != 'company'
            or context.company_id != company or Path(context.state) != Path(selected['state'])):
        raise ValueError('Read the selected company’s orders.')
    path = Path(selected['state']) / 'manufacturer.sqlite'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT id,status FROM orders WHERE company_id=? ORDER BY id LIMIT ?',
                          (company, arguments.get('limit', 50))).fetchall()
    context.require_current()
    return {'orders': [{'id': row[0], 'status': row[1]} for row in rows]}


FAMILY = sys.modules[__name__]


def install_workspace(host, selected):
    path = Path(selected['state']) / 'manufacturer.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS orders (company_id TEXT NOT NULL, id TEXT NOT NULL, '
                   'status TEXT NOT NULL, PRIMARY KEY(company_id,id))')
    path.chmod(0o600)
    return BareWorkspace(selected)


def register_routes(app, host, http):
    asset = '/static/manufacturer/orders.js'
    host.options['page_assets'] = {'scripts': [asset], 'styles': []}
    host.options['public_request'] = lambda request: request.method in {'GET', 'HEAD'} and request.url.path == asset

    @app.get(asset)
    async def orders_script():
        return FileResponse(Path(__file__).parent / 'web/orders.js', media_type='application/javascript')

    @app.get('/api/manufacturer/orders')
    async def orders(request: Request, limit: int = Query(default=50, ge=1, le=100)):
        principal, _ = http.connection_identity(request)
        selected = request.state.workspace
        extension = selected['vertical']
        try:
            context = ToolContext(state=selected['state'], company_id=selected['business_scope'].company_id,
                actor=principal, data_owner=principal, collection='company', extension=extension,
                admission=extension.read_admission(principal, 'company'))
            return await selected['agent'].tool_registry.invoke(context, 'manufacturer_orders', {'limit': limit})
        except ValueError as error:
            raise HTTPException(403, str(error)) from None


@asynccontextmanager
async def lifespan(app, host):
    yield
