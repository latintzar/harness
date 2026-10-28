"""Maintained, tenant-authenticated reads for realtime projections.

This router is deliberately separate from the development video preview.  It
resolves the authenticated supplier session to the imported realtime principal
and then lets the restricted realtime SDK enforce company and source grants.
The wire contains business projection data and bounded freshness, not prompts,
worker configuration, or replay plans.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import db
import sup_api
from realtime.contracts import ExecutionBounds, HostExecutionContext
from realtime.module_store import CoreModuleStore
from realtime.read_contract import (
    MAX_OUTPUT_BYTES as _MAX_OUTPUT_BYTES,
)
from realtime.read_contract import (
    bounded_playhead as _bounded_playhead,
)
from realtime.read_contract import (
    public_projection as _public_projection,
)
from realtime.replay_store import ReplayStore
from realtime.store import AccessDenied, CapacityExceeded, DeadlineExceeded, _identifier

router = APIRouter(prefix="/api/realtime", tags=["realtime"])
_read_dependency = sup_api.require_cap("read")

_READ_DEADLINE_S = 5


async def _principal_id(ctx: dict[str, Any]) -> str:
    """Resolve the session's verified email to the durable imported principal."""

    # Identity import intentionally stores the saved opaque principal id and a NULL
    # operator email. A verified supplier identity must be provisioned separately
    # into operators/team_members before this read surface can resolve it; this
    # path never guesses from display names or accepts a principal from the client.
    email = str(ctx.get("actor_email") or "").strip().lower()
    tenant = str(ctx.get("tenant_slug") or "").strip()
    if not email or not tenant:
        raise HTTPException(status_code=403, detail="customer_scope_required")
    async with db.get_pool().acquire() as con:
        rows = await con.fetch(
            "select o.id from public.operators o "
            "join public.team_members tm on tm.operator_id=o.id "
            "where lower(o.email)=$1 and tm.tenant_slug=$2 limit 2",
            email,
            tenant,
        )
    # The legacy operators email constraint is case-sensitive. A duplicate
    # case-folded login is therefore an ambiguous identity and must fail closed;
    # choosing an arbitrary row would make the authenticated read tenant-unsafe.
    if len(rows) != 1:
        raise HTTPException(status_code=403, detail="principal_unavailable")
    return str(rows[0]["id"])


async def _context(request: Request, ctx: dict[str, Any]) -> HostExecutionContext:
    # Supplier authentication has its own existing lifecycle. This deadline
    # covers identity resolution (including pool acquisition) and the subsequent
    # restricted SDK read; resolving identity never resets the allowance.
    deadline = datetime.now(timezone.utc) + timedelta(seconds=_READ_DEADLINE_S)
    try:
        async with asyncio.timeout(_READ_DEADLINE_S):
            principal_id = await _principal_id(ctx)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="realtime_deadline_exceeded") from exc
    request_id = uuid.uuid4().hex
    conversation_id = f"realtime-read:{ctx['session_id']}"
    return HostExecutionContext(
        company_id=str(ctx["tenant_slug"]),
        principal_id=principal_id,
        request_id=request_id,
        conversation_id=conversation_id,
        capabilities=frozenset({"realtime:read"}),
        deadline_utc=deadline,
        bounds=ExecutionBounds(
            max_events=1000,
            max_payload_bytes=64 * 1024,
            max_payload_depth=8,
            max_output_bytes=_MAX_OUTPUT_BYTES,
        ),
    )


async def _read_context(
    request: Request, ctx: dict[str, Any] = Depends(_read_dependency)
) -> HostExecutionContext:
    return await _context(request, ctx)


def _error(exc: Exception) -> HTTPException:
    # Source, asset, projection, and replay visibility intentionally share 404
    # so callers cannot probe another tenant's identifiers.
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=404, detail="resource_unavailable")
    if isinstance(exc, DeadlineExceeded):
        return HTTPException(status_code=504, detail="realtime_deadline_exceeded")
    if isinstance(exc, CapacityExceeded):
        return HTTPException(status_code=413, detail="response_too_large")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail="invalid_realtime_request")
    return HTTPException(status_code=500, detail="realtime_read_failed")


@router.get("/sources/{source_id}/assets/{asset_id}/snapshot")
async def source_snapshot(
    source_id: str,
    asset_id: str,
    request: Request,
    at_s: str | None = Query(default=None, min_length=1, max_length=80),
    context: HostExecutionContext = Depends(_read_context),
):
    try:
        _identifier(source_id)
        _identifier(asset_id)
        result = await CoreModuleStore().latest(context, source_id, asset_id, at_s=_bounded_playhead(at_s))
        if result is None:
            raise AccessDenied("snapshot is unavailable")
        return JSONResponse(_public_projection(result), headers={"cache-control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/sources/{source_id}/epochs/{epoch_id}/snapshot")
async def epoch_snapshot(
    source_id: str,
    epoch_id: str,
    request: Request,
    at_s: str | None = Query(default=None, min_length=1, max_length=80),
    context: HostExecutionContext = Depends(_read_context),
):
    """Read one admitted continuous session on its mapped stream clock.

    With at_s, the SDK computes a bounded ephemeral view without writing a
    projection. Otherwise it reads the saved head. Both paths recheck current
    camera, subject and catalog access; epoch identity never joins cameras.
    """
    try:
        _identifier(source_id)
        _identifier(epoch_id)
        result = await CoreModuleStore().latest_epoch(
            context, source_id, epoch_id, at_s=_bounded_playhead(at_s)
        )
        if result is None:
            raise AccessDenied("snapshot is unavailable")
        return JSONResponse(
            _public_projection(result, epoch_id=epoch_id), headers={"cache-control": "no-store"}
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/replays/{run_id}")
async def replay_status(
    run_id: str,
    request: Request,
    context: HostExecutionContext = Depends(_read_context),
):
    try:
        _identifier(run_id)
        result = await ReplayStore().get(context, run_id)
        # ReplayStore's cursor and plan digest are orchestration lineage.  The
        # customer read surface only exposes progress and lifecycle state.
        public = {
            "schema": "rocoto.replay-status.v1",
            "run_id": result["run_id"],
            "state": result["state"],
            "revision": result["revision"],
            "duration_s": result["duration_s"],
            "elapsed_s": result["elapsed_s"],
        }
        return JSONResponse(public, headers={"cache-control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc
