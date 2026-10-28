"""Maintained reads behind the existing video workspace session.

ROCOTO_RETAIL_REALTIME_READS selects the authority once at application creation.
The maintained SDK is loaded only in maintained mode. Identity comes exclusively
from the verified host session and retained private workspace identity; the SDK
rechecks current PostgreSQL membership and every source/catalog dependency.
"""

from __future__ import annotations
from retail_settings import value as setting

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

READ_DEADLINE_S = 5


def read_authority(environ=None):
    if environ is None:
        environ = os.environ
    value = setting("ROCOTO_RETAIL_REALTIME_READS", "legacy", environ=environ)
    if value not in {"legacy", "maintained"}:
        raise ValueError("Set ROCOTO_RETAIL_REALTIME_READS to legacy or maintained.")
    return value


def _load_backend():
    """Use the reviewed artifact closure, or the same checkout during development."""
    import layout
    bundled = layout.ROOT / "_maintained_backend"
    root = bundled if bundled.is_dir() else layout.REPO / "backend"
    if not (root / "realtime/read_contract.py").is_file():
        raise RuntimeError("The maintained read runtime is missing from this release.")
    path = str(root)
    if path not in sys.path:
        sys.path.insert(0, path)
    import db

    if Path(db.__file__).resolve() != (root / "db.py").resolve():
        raise RuntimeError("The maintained read runtime must use its packaged database module.")
    return db


@asynccontextmanager
async def maintained_lifespan(app):
    """Open one restricted pool per service lifetime; startup never imports data."""
    if app.state.realtime_read_authority != "maintained":
        yield
        return
    db = _load_backend()
    dsn = os.environ.get("RUNTIME_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("Configure RUNTIME_DATABASE_URL for maintained reads.")
    await db.init_runtime_only(dsn, timeout_s=5, max_size=2)
    try:
        yield
    finally:
        await db.close_runtime_only()


def _saved_company(selected):
    # Reading cannot adopt a missing identity or mint a replacement company.
    from workspace_identity import IDENTITY_FILE, _read

    record = _read(Path(selected["state"]) / IDENTITY_FILE)
    if record is None:
        raise ValueError("Restore the commissioned workspace identity.")
    return record["company_scope"]


async def _context(request):
    from realtime.contracts import ExecutionBounds, HostExecutionContext
    from realtime.read_contract import MAX_OUTPUT_BYTES

    deadline = datetime.now(timezone.utc) + timedelta(seconds=READ_DEADLINE_S)
    session = getattr(request.state, "session", None)
    selected = getattr(request.state, "workspace", None)
    if not session or not session.get("authenticated"):
        raise HTTPException(401, "session_required")
    if not selected:
        raise HTTPException(403, "workspace_required")
    principal = selected["chats"].principal(session)
    if not principal:
        raise HTTPException(403, "principal_unavailable")
    try:
        async with asyncio.timeout(READ_DEADLINE_S):
            company = await asyncio.to_thread(_saved_company, selected)
    except TimeoutError as exc:
        raise HTTPException(504, "realtime_deadline_exceeded") from exc
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(503, "workspace_identity_unavailable") from exc
    request_id = uuid.uuid4().hex
    return HostExecutionContext(
        company_id=company,
        principal_id=principal,
        request_id=request_id,
        conversation_id="video-read:" + request_id,
        capabilities=frozenset({"realtime:read"}),
        deadline_utc=deadline,
        bounds=ExecutionBounds(max_events=1000, max_output_bytes=MAX_OUTPUT_BYTES),
    )


def register_realtime_read_routes(app, *, authority=None):
    from retail_urls import retail_router
    app = retail_router(app)
    app.state.realtime_read_authority = authority if authority is not None else read_authority()
    if app.state.realtime_read_authority not in {"legacy", "maintained"}:
        raise ValueError("Select the declared read authority.")
    if app.state.realtime_read_authority != "maintained":
        return
    _load_backend()
    from realtime.module_store import CoreModuleStore
    from realtime.read_contract import bounded_playhead, public_projection
    from realtime.store import AccessDenied, CapacityExceeded, DeadlineExceeded, _identifier

    async def read(request, source_id, identity, at_s, *, epoch):
        context = await _context(request)
        try:
            _identifier(source_id)
            _identifier(identity)
            store = CoreModuleStore()
            method = store.latest_epoch if epoch else store.latest
            result = await method(context, source_id, identity, at_s=bounded_playhead(at_s))
            if result is None:
                raise AccessDenied("Snapshot is unavailable")
            return JSONResponse(
                public_projection(result, epoch_id=identity if epoch else None),
                headers={"cache-control": "private, no-store"},
            )
        except AccessDenied as exc:
            raise HTTPException(404, "resource_unavailable") from exc
        except DeadlineExceeded as exc:
            raise HTTPException(504, "realtime_deadline_exceeded") from exc
        except CapacityExceeded as exc:
            raise HTTPException(413, "response_too_large") from exc
        except ValueError as exc:
            raise HTTPException(422, "invalid_realtime_request") from exc
        except Exception as exc:
            # The selected authority remains selected during an outage.
            raise HTTPException(503, "realtime_read_unavailable") from exc

    @app.get('/api/retail/visit-basket')
    async def legacy_basket(request: Request):
        await _context(request)
        raise HTTPException(503, "maintained_snapshot_required")

    @app.get('/api/retail/realtime/sources/{source_id}/epochs/{epoch_id}/snapshot')
    async def epoch_snapshot(
        request: Request,
        source_id: str,
        epoch_id: str,
        at_s: str | None = Query(None, min_length=1, max_length=80),
    ):
        return await read(request, source_id, epoch_id, at_s, epoch=True)

    @app.get('/api/retail/realtime/sources/{source_id}/assets/{asset_id}/snapshot')
    async def asset_snapshot(
        request: Request,
        source_id: str,
        asset_id: str,
        at_s: str | None = Query(None, min_length=1, max_length=80),
    ):
        return await read(request, source_id, asset_id, at_s, epoch=False)
