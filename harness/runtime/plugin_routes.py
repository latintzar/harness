"""Private, host-commissioned native package selection and durable removal.

Register before the host's /api/plugins/{plugin_id} metadata route. The host owns
workspace selection, a pinned NativePackageStore, installer commissioning and
runtime revocation. This module never accepts source URLs, paths or principals.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from chat_permissions import ChatPermissions, workspace_writable
from native_packages import NativePackageStore, PackageError
from native_install_source import source_spec

PRIVATE_HEADERS = {'Cache-Control': 'private, no-store'}
MAX_REQUEST_BYTES = 1024
BODY_TIMEOUT = 5
INSTALL_TIMEOUT = 420
REVOKE_TIMEOUT = 45
# Immediate global admission, held through thread draining and runtime cleanup.
_REQUEST_SLOTS = threading.BoundedSemaphore(4)


def _deny(status, code):
    raise HTTPException(status, detail=code, headers=PRIVATE_HEADERS)


async def _offload(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Finite native install transactions own subprocess cleanup. A caller
        # disappearing must not free the global slot or company fence early.
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if task.done() and not task.cancelled(): task.exception()
        raise


async def _run_store(method, *args, **kwargs):
    async def invoke():
        async with asyncio.timeout(INSTALL_TIMEOUT):
            return await method(*args, **kwargs)
    return await _offload(lambda: asyncio.run(invoke()))


@asynccontextmanager
async def _admitted():
    if not _REQUEST_SLOTS.acquire(blocking=False): _deny(429, 'plugin_requests_busy')
    try: yield
    finally: _REQUEST_SLOTS.release()


def _header(request, name):
    values = request.headers.getlist(name)
    if len(values) > 1 or (values and len(values[0]) > 256): _deny(400, 'invalid_headers')
    return values[0] if values else None


def _identity(request, *, write=False):
    session = getattr(request.state, 'session', None)
    selected = getattr(request.state, 'workspace', None)
    if not session or not session.get('authenticated'): _deny(401, 'sign_in_required')
    principal = ChatPermissions.principal(session)
    if not isinstance(principal, str) or not principal or not isinstance(selected, dict):
        _deny(403, 'workspace_access_required')
    if request.query_params: _deny(400, 'invalid_query')
    if write:
        if not workspace_writable(session): _deny(403, 'read_only_workspace')
        if _header(request, 'x-video-demo') != '1': _deny(403, 'csrf_required')
        origin = _header(request, 'origin')
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                    or parsed.netloc != request.headers.get('host') or parsed.path or parsed.query or parsed.fragment):
                _deny(403, 'same_origin_required')
    return selected, principal


async def _empty_body(request, *, required):
    if _header(request, 'content-encoding') not in {None, 'identity'}: _deny(415, 'unsupported_content_encoding')
    if _header(request, 'transfer-encoding') is not None: _deny(400, 'use_content_length')
    raw_length = _header(request, 'content-length')
    if raw_length is not None and not re.fullmatch(r'[0-9]{1,10}', raw_length): _deny(400, 'invalid_headers')
    length = int(raw_length) if raw_length is not None else None
    if length is not None and length > MAX_REQUEST_BYTES: _deny(413, 'request_size_limit')
    content_type = (_header(request, 'content-type') or '').split(';', 1)[0].strip().lower()
    if (required or length not in {None, 0}) and content_type != 'application/json': _deny(415, 'use_application_json')
    raw = bytearray()
    try:
        async with asyncio.timeout(BODY_TIMEOUT):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_REQUEST_BYTES: _deny(413, 'request_size_limit')
                raw.extend(chunk)
    except TimeoutError: _deny(408, 'request_timeout')
    except ClientDisconnect: _deny(400, 'incomplete_request')
    if length is not None and len(raw) != length: _deny(400, 'request_length_mismatch')
    if not raw and not required: return
    try:
        value = json.loads(raw)
        if type(value) is not dict or value: raise ValueError()
    except (ValueError, TypeError, UnicodeError, RecursionError): _deny(400, 'empty_object_required')


def _plugin_id(identifier):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}', identifier): _deny(404, 'plugin_not_found')
    return identifier


def _installable(store):
    # Reuse the pinned archive validator without constructing an agent runtime.
    result = []
    for entry in store.entries.values():
        if entry.get('supported_api') is not True: continue
        try: source_spec(entry, store.snapshot_catalogue)
        except (PackageError, ValueError, TypeError, KeyError, AttributeError): continue
        result.append(entry['id'])
    return sorted(result)


def _private_fd(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        os.close(fd)
        raise OSError('invalid private file')
    os.fchmod(fd, 0o600)
    return fd


class PendingRemovals:
    """Small durable metadata journal; contains hashes and published plugin IDs."""
    def __init__(self, store):
        self.root = store.root
        self.path = self.root / 'pending-removals.sqlite3'
        fd = _private_fd(self.path); os.close(fd)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pending(owner TEXT NOT NULL,plugin_id TEXT NOT NULL,PRIMARY KEY(owner,plugin_id))')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db: yield db
        finally: db.close()

    @staticmethod
    def _owner(principal): return hashlib.sha256(principal.encode()).hexdigest()

    def list(self, principal):
        with self._db() as db:
            return [row[0] for row in db.execute('SELECT plugin_id FROM pending WHERE owner=? ORDER BY plugin_id', (self._owner(principal),))]

    def begin(self, principal, plugin_id):
        owner = self._owner(principal)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM pending WHERE owner=? AND plugin_id=?', (owner, plugin_id)).fetchone(): return
            if (db.execute('SELECT COUNT(*) FROM pending').fetchone()[0] >= 512
                    or db.execute('SELECT COUNT(*) FROM pending WHERE owner=?', (owner,)).fetchone()[0] >= 64):
                _deny(429, 'plugin_removal_capacity')
            db.execute('INSERT INTO pending VALUES(?,?)', (owner, plugin_id))

    def clear(self, principal, plugin_id):
        with self._db() as db:
            db.execute('DELETE FROM pending WHERE owner=? AND plugin_id=?', (self._owner(principal), plugin_id))

    def lock(self):
        fd = _private_fd(self.root / 'http-mutation.lock')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd); _deny(409, 'plugin_mutation_busy')
        except BaseException:
            os.close(fd); raise
        return fd


@asynccontextmanager
async def _mutation(journal):
    # Nonblocking process-shared fence covers pending checks, selection mutation
    # and runtime revocation, including threads draining after HTTP cancellation.
    fd = journal.lock()
    try: yield
    finally: os.close(fd)


def register_plugin_routes(app: FastAPI, *, store_for_workspace, installation_ready=None, installation_capabilities=None, revoke_plugin=None):
    """Host callbacks receive only its authenticated request.state.workspace.

    installation_capabilities is async and receives selected, principal_id and
    store; it returns {available, installable_ids} from verified native readiness.
    installation_ready(selected)->bool remains a legacy host/test override.
    Both default closed. revoke_plugin is async:
    (selected, *, principal_id, plugin_id)->{'cleanup_confirmed':True}. Omission
    retains pending removal until an explicit host cleanup confirmation arrives.
    Register once before a generic /api/plugins/{plugin_id} metadata route.
    """
    if getattr(app.state, 'plugin_routes_registered', False): raise ValueError('plugin_routes_already_registered')
    app.state.plugin_routes_registered = True

    async def context(request, *, write=False):
        selected, principal = _identity(request, write=write)
        try:
            store = await _offload(store_for_workspace, selected)
            if not isinstance(store, NativePackageStore): _deny(503, 'plugin_store_unavailable')
            # A miswired host cannot return another selected company's store.
            if Path(selected['state']).absolute() != store.state: _deny(503, 'plugin_store_unavailable')
            journal = await _offload(PendingRemovals, store)
        except (PackageError, OSError, sqlite3.Error, KeyError, TypeError): _deny(503, 'plugin_store_unavailable')
        return selected, principal, store, journal

    async def ready(selected):
        if installation_ready is None: return False
        try: return await _offload(installation_ready, selected) is True
        except Exception: return False

    async def capabilities(selected, principal, store):
        if installation_capabilities is None:
            available = await ready(selected)
            return {'available': available, 'installable_ids': await _offload(_installable, store) if available else []}
        try:
            value = await installation_capabilities(selected, principal_id=principal, store=store)
            if not isinstance(value, dict) or value.get('available') is not True:
                return {'available': False, 'installable_ids': []}
            identifiers = value.get('installable_ids')
            if (not isinstance(identifiers, list) or len(identifiers) > 64
                    or any(not isinstance(item, str) for item in identifiers)):
                return {'available': False, 'installable_ids': []}
            eligible = await _offload(_installable, store)
            return {'available': True, 'installable_ids': sorted(set(identifiers).intersection(eligible))}
        except Exception:
            return {'available': False, 'installable_ids': []}

    @app.get('/api/plugins/installed')
    async def installed(request: Request):
        async with _admitted():
            selected, principal, store, journal = await context(request)
            try:
                value = await _offload(store.list, principal_id=principal)
                pending = await _offload(journal.list, principal)
            except (PackageError, OSError, sqlite3.Error): _deny(503, 'plugin_store_unavailable')
            installation = await capabilities(selected, principal, store)
            return JSONResponse({**value, **installation, 'installation_available': installation['available'],
                                 'cleanup_pending_ids': pending}, headers=PRIVATE_HEADERS)

    @app.post('/api/plugins/{plugin_id}/install')
    async def install(plugin_id: str, request: Request):
        async with _admitted():
            selected, principal = _identity(request, write=True)
            _plugin_id(plugin_id)
            await _empty_body(request, required=True)
            if installation_capabilities is None and not await ready(selected): _deny(503, 'plugin_installation_unavailable')
            _, _, store, journal = await context(request, write=True)
            installation = await capabilities(selected, principal, store)
            if not installation['available']: _deny(503, 'plugin_installation_unavailable')
            if plugin_id not in installation['installable_ids']: _deny(409, 'plugin_setup_required')
            current, current_principal = _identity(request, write=True)
            if current is not selected or current_principal != principal: _deny(403, 'workspace_scope_changed')
            try:
                async with _mutation(journal):
                    if plugin_id in await _offload(journal.list, principal): _deny(409, 'plugin_removal_pending')
                    value = await _run_store(store.install, plugin_id, principal_id=principal)
            except HTTPException: raise
            except Exception: _deny(503, 'plugin_installation_failed')
            return JSONResponse(value, headers=PRIVATE_HEADERS)

    @app.delete('/api/plugins/{plugin_id}/installation')
    async def remove(plugin_id: str, request: Request):
        async with _admitted():
            _plugin_id(plugin_id)
            selected, principal, store, journal = await context(request, write=True)
            await _empty_body(request, required=False)
            removed = False
            try:
                async with _mutation(journal):
                    # Bound intents to real public packages or owned historical
                    # selections/pending removals. Browser IDs cannot fill storage.
                    known = plugin_id in store.entries or plugin_id in await _offload(journal.list, principal)
                    if not known:
                        owned = await _offload(store.list, principal_id=principal)
                        known = any(row['id'] == plugin_id for row in owned['plugins'])
                    if not known: _deny(404, 'plugin_not_found')
                    await _offload(journal.begin, principal, plugin_id)
                    await _run_store(store.uninstall, plugin_id, principal_id=principal)
                    removed = True
                    if revoke_plugin is None: raise RuntimeError('cleanup unavailable')
                    async with asyncio.timeout(REVOKE_TIMEOUT):
                        receipt = await revoke_plugin(selected, principal_id=principal, plugin_id=plugin_id)
                    if not isinstance(receipt, dict) or receipt.get('cleanup_confirmed') is not True:
                        raise RuntimeError('cleanup unconfirmed')
                    await _offload(journal.clear, principal, plugin_id)
            except HTTPException: raise
            except Exception:
                return JSONResponse({'removed': removed, 'plugin_id': plugin_id, 'cleanup_confirmed': False,
                                     'detail': 'plugin_cleanup_pending' if removed else 'plugin_removal_pending'},
                                    status_code=503, headers=PRIVATE_HEADERS)
            return JSONResponse({'removed': True, 'plugin_id': plugin_id, 'cleanup_confirmed': True}, headers=PRIVATE_HEADERS)
