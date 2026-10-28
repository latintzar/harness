"""Bounded replay admission plus core projection orchestration.

This worker deliberately stops at durable module snapshots.  Replay admission
keeps its own lane and outbox entries; this helper never claims or completes
that outbox and never wakes agents or delivers external effects.
"""

from __future__ import annotations

from typing import Any

from realtime.contracts import CanonicalEvent, HostExecutionContext
from realtime.module_store import CoreModuleStore
from realtime.replay import ReplayError
from realtime.replay_store import ReplayStore
from realtime.store import AccessDenied, _identifier

MAX_WORKER_EVENTS = 100
MAX_WORKER_PAIRS = 32


def _pairs(plan: dict[str, Any]) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for entry in plan.get("events", []):
        if not isinstance(entry, dict):
            raise ReplayError("Stored replay plan contains an invalid event entry")
        source_id = entry.get("replay_source_id")
        if not isinstance(source_id, str):
            raise ReplayError("Stored replay plan contains an invalid replay source")
        _identifier(source_id)
        event_json = entry.get("event_json")
        if not isinstance(event_json, str):
            raise ReplayError("Stored replay plan contains an invalid event payload")
        event = CanonicalEvent.model_validate_json(event_json)
        found.add((source_id, event.provenance.source_asset_id))
        if len(found) > MAX_WORKER_PAIRS:
            raise ReplayError("Replay projection fanout exceeds the worker bound")
    return sorted(found)


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded receipt without copying the potentially large snapshot."""

    return {
        key: result[key]
        for key in (
            "status",
            "projection_id",
            "module_revision",
            "source_id",
            "source_asset_id",
            "original_source_id",
            "lane",
            "playhead_s",
            "digest",
            "awaiting_bindings",
            "freshness",
        )
        if key in result
    }


class ReplayWorker:
    """Run one saved replay checkpoint and refresh its changed source assets."""

    def __init__(self, replay: ReplayStore | None = None, modules: CoreModuleStore | None = None):
        self.replay = replay or ReplayStore()
        self.modules = modules or CoreModuleStore(self.replay.store)

    async def run_once(
        self, context: HostExecutionContext, run_id: str, *, limit: int = 100
    ) -> dict[str, Any]:
        _identifier(run_id)
        if type(limit) is not int or not 1 <= limit <= min(MAX_WORKER_EVENTS, context.bounds.max_events):
            raise ReplayError("Replay worker limit must be between 1 and 100.")
        required = {"realtime:read", "realtime:ingest", "realtime:process"}
        if not required <= context.capabilities:
            raise AccessDenied("Replay worker requires read, ingest, and process capabilities")

        # The plan is loaded from the durable run identity.  No source or asset
        # list is accepted from this worker's caller.
        plan = await self.replay.inspect_plan(context, run_id)
        pairs = _pairs(plan)
        tick = await self.replay.tick(context, run_id, limit=limit)
        projections = []
        for source_id, asset_id in pairs:
            projection = await self.modules.refresh(context, source_id, asset_id)
            projections.append(_summary(projection))
        return {
            "schema": "rocoto.replay-worker.v1",
            "run_id": run_id,
            "plan_digest": tick["plan_digest"],
            "state": tick["state"],
            "revision": tick["revision"],
            "admitted": tick["admitted"],
            "refreshed": len(projections),
            "projections": projections,
            "external_effects": "disabled",
            "agent_wakes": "disabled",
        }


async def run_once(context: HostExecutionContext, run_id: str, *, limit: int = 100) -> dict[str, Any]:
    """Convenience boundary for a scheduled ordinary-host invocation."""

    return await ReplayWorker().run_once(context, run_id, limit=limit)
