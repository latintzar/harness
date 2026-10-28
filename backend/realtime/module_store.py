"""Durable source-scoped snapshots for maintained core processing modules.

The store is deliberately an explicit refresh boundary.  It reads only admitted
observations from one source and asset, selects the latest event revision before
the reducer receives the facts, and requires sealed subject bindings.  It does
not fan out work to other modules or agents.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from realtime.contracts import CanonicalEvent, HostExecutionContext
from realtime.modules import (
    EPOCH_MODULE_REVISION,
    MODULE_REVISION,
    project_continuous_observations,
    project_observations,
)
from realtime.store import AccessDenied, CapacityExceeded, RealTimeStore, _identifier
from realtime.subjects import SubjectStore

MAX_HISTORY_EVENTS = 1000
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_MODULE_PROJECTIONS = 200000


def _json_bytes(value: Any, limit: int) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("projection contains non-JSON data") from exc
    if len(encoded) > limit:
        raise CapacityExceeded("projection exceeds its byte budget")
    return encoded


def _wire(value: Any) -> Any:
    """Round-trip JSON values so asyncpg/Decimal values cannot escape the API."""

    return json.loads(_json_bytes(value, MAX_HISTORY_BYTES))


def _bounded_digest(value: Any) -> str:
    """Hash the full bounded wire, without the small event helper limit."""

    return hashlib.sha256(_json_bytes(value, MAX_HISTORY_BYTES * 2)).hexdigest()


def _playhead(value: Any) -> Decimal | None:
    if value is None:
        return None
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("playhead must be a finite nonnegative source second")
    return parsed


class CoreModuleStore:
    """Persist one append-only revision-qualified snapshot per refresh."""

    def __init__(self, store: RealTimeStore | None = None):
        self.store = store or RealTimeStore()
        self.subjects = SubjectStore(self.store)

    @staticmethod
    def _lane(source: dict[str, Any]) -> str:
        return "replay" if source["kind"] == "replay" else "live"

    @staticmethod
    def _original_source(source: dict[str, Any], source_id: str) -> str:
        metadata = source.get("metadata") or {}
        original = metadata.get("original_source_id") if source["kind"] == "replay" else source_id
        return original if isinstance(original, str) and original else source_id

    @staticmethod
    def _context_key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _epoch_asset_id(epoch_id: str) -> str:
        _identifier(epoch_id)
        digest = _bounded_digest({"domain": "rocoto.module-epoch-storage.v1", "epoch_id": epoch_id})
        return "epoch:" + digest

    async def _history(
        self,
        con,
        context,
        source_id: str,
        source_asset_id: str | None,
        max_events: int,
        *,
        epoch_id: str | None = None,
    ):
        """Read a bounded cursor after latest-revision selection.

        Recording reads select one artifact. Epoch reads select only admitted
        observation events carrying the requested typed continuity epoch; their
        local artifact clocks are normalized by the pure reducer later.
        """
        if (source_asset_id is None) == (epoch_id is None):
            raise ValueError("Select exactly one recording asset or continuity epoch")
        predicate = (
            "and envelope->'provenance'->'continuity'->>'epoch_id'=$3"
            if epoch_id is not None
            else "and envelope->'provenance'->>'source_asset_id'=$3"
        )
        query = (
            "with latest as ("
            "select e.*,row_number() over(partition by e.event_id "
            "order by e.event_revision desc) as revision_rank "
            "from realtime.event e where e.tenant_id=$1 and e.source_id=$2) "
            "select sequence,event_id,event_revision,envelope,replay_context,committed_at "
            "from latest where revision_rank=1 and envelope->>'origin_kind'='observation' "
            + predicate
            + " order by sequence limit $4"
        )
        selected = source_asset_id if source_asset_id is not None else epoch_id
        rows = []
        retained = 0
        async for row in con.cursor(
            query, context.company_id, source_id, selected, max_events + 1, prefetch=32
        ):
            retained += len(_json_bytes(row["envelope"], MAX_HISTORY_BYTES))
            if retained > MAX_HISTORY_BYTES:
                raise CapacityExceeded("selected observation history exceeds the 16 MiB module budget")
            rows.append(row)
            if len(rows) > max_events:
                raise CapacityExceeded("selected observation history exceeds the event budget")
        return rows

    @staticmethod
    def _binding_manifest(
        bindings: list[dict[str, Any]], current_revisions: dict[str, int]
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": binding["role"],
                "subject_id": binding["subject"]["subject_id"],
                "subject_revision": binding["subject"]["revision"],
                "subject_status": binding["subject"]["status"],
                "current_subject_revision": current_revisions.get(
                    binding["subject"]["subject_id"], binding["subject"]["revision"]
                ),
                "current_status": binding["subject"]["current_status"],
            }
            for binding in bindings
        ]

    def _result(self, row: dict[str, Any], context: HostExecutionContext, *, status: str = "ready"):
        result = {
            "status": status,
            "projection_id": row.get("projection_id"),
            "module_revision": row["module_revision"],
            "source_id": row["source_id"],
            "source_asset_id": row["source_asset_id"],
            "original_source_id": row["original_source_id"],
            "lane": row["lane"],
            "playhead_s": None if row["playhead_s"] is None else str(row["playhead_s"]),
            "digest": row.get("digest"),
            "input_manifest": row.get("input_manifest"),
            "replay_contexts": row.get("replay_contexts") or [],
            "freshness": row.get("freshness") or {},
            "snapshot": row.get("snapshot"),
            "created_at": row["created_at"].astimezone(timezone.utc).isoformat()
            if row.get("created_at")
            else None,
        }
        _json_bytes(result, min(context.bounds.max_output_bytes, MAX_HISTORY_BYTES))
        return result

    async def _collect(
        self,
        con,
        context,
        source_id: str,
        asset_id: str,
        playhead: Decimal | None,
        *,
        epoch_id: str | None = None,
    ) -> dict[str, Any]:
        source = await self.store._source(con, context, source_id)
        max_events = min(MAX_HISTORY_EVENTS, context.bounds.max_events)
        rows = await self._history(
            con,
            context,
            source_id,
            None if epoch_id is not None else asset_id,
            max_events,
            epoch_id=epoch_id,
        )
        facts: list[dict[str, Any]] = []
        awaiting: list[dict[str, Any]] = []
        contexts: dict[str, Any] = {}
        event_rows: list[dict[str, Any]] = []
        latest_ingested = None
        latest_source_end = None
        for row in rows:
            event = CanonicalEvent.model_validate(row["envelope"])
            if getattr(event, "event_state", "active") == "retracted":
                # A retraction is the admitted correction and does not wait for
                # a new subject-binding seal.
                bindings = []
            else:
                bindings = await self.subjects._event_bindings(
                    con, context, source_id, event.event_id, event.event_revision
                )
            current_revisions: dict[str, int] = {}
            if bindings is not None and bindings:
                subject_ids = [binding["subject"]["subject_id"] for binding in bindings]
                revision_rows = await con.fetch(
                    "select subject_id,max(revision) as current_revision from realtime.subject_revision "
                    "where tenant_id=$1 and subject_id=any($2::text[]) group by subject_id",
                    context.company_id,
                    subject_ids,
                )
                current_revisions = {row["subject_id"]: row["current_revision"] for row in revision_rows}
            event_row = {
                "event_id": event.event_id,
                "event_revision": event.event_revision,
                "sequence": row["sequence"],
                "bindings": (
                    None if bindings is None else self._binding_manifest(bindings, current_revisions)
                ),
            }
            if epoch_id is not None:
                continuity = event.provenance.continuity
                if continuity is None or continuity.epoch_id != epoch_id:
                    raise ValueError(
                        "Epoch history contains an event without its requested continuity mapping"
                    )
                event_row.update(
                    {
                        "epoch_id": continuity.epoch_id,
                        "mapping_id": continuity.mapping_id,
                        "source_asset_id": event.provenance.source_asset_id,
                        "source_window": event.provenance.source_window.model_dump(mode="json"),
                        "stream_window": continuity.stream_window.model_dump(mode="json"),
                    }
                )
            event_rows.append(event_row)
            if bindings is None:
                awaiting.append({"event_id": event.event_id, "event_revision": event.event_revision})
            else:
                facts.append({"event": event, "bindings": bindings})
            if row["replay_context"] is not None:
                key = self._context_key(row["replay_context"])
                contexts[key] = _wire(row["replay_context"])
            end_s = (
                event.provenance.continuity.stream_window.end_s
                if epoch_id is not None and event.provenance.continuity is not None
                else event.provenance.source_window.end_s
            )
            latest_source_end = end_s if latest_source_end is None else max(latest_source_end, end_s)
            if latest_ingested is None or event.ingested_at_utc > latest_ingested:
                latest_ingested = event.ingested_at_utc

        lane = self._lane(source)
        replay_contexts = [contexts[key] for key in sorted(contexts)]
        original_source_id = self._original_source(source, source_id)
        freshness = {
            "source_active": bool(source["active"]),
            "source_created_at": source["created_at"].astimezone(timezone.utc).isoformat(),
            "loaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "history_event_count": len(rows),
            "sealed_event_count": len(facts),
            "awaiting_binding_count": len(awaiting),
            "latest_source_end_s": None if latest_source_end is None else str(latest_source_end),
            "latest_ingested_at_utc": None if latest_ingested is None else latest_ingested.isoformat(),
        }
        manifest: dict[str, Any] = {
            "schema": "rocoto.module-input.v1",
            "module_revision": EPOCH_MODULE_REVISION if epoch_id is not None else MODULE_REVISION,
            "source_id": source_id,
            "source_asset_id": asset_id,
            "original_source_id": original_source_id,
            "lane": lane,
            "playhead_s": None if playhead is None else str(playhead),
            "events": event_rows,
            "replay_contexts": replay_contexts,
        }
        if epoch_id is not None:
            manifest["continuity"] = {"epoch_id": epoch_id, "clock": "stream"}
            freshness["continuity_epoch_id"] = epoch_id
        return {
            "source": source,
            "rows": rows,
            "facts": facts,
            "awaiting": awaiting,
            "lane": lane,
            "replay_contexts": replay_contexts,
            "original_source_id": original_source_id,
            "freshness": freshness,
            "manifest": manifest,
            "epoch_id": epoch_id,
            "module_revision": manifest["module_revision"],
        }

    @staticmethod
    def _pending(collected: dict[str, Any], playhead: Decimal | None) -> dict[str, Any]:
        return {
            "status": "awaiting_bindings",
            "module_revision": collected["module_revision"],
            "source_id": collected["manifest"]["source_id"],
            "source_asset_id": collected["manifest"]["source_asset_id"],
            "original_source_id": collected["original_source_id"],
            "lane": collected["lane"],
            "playhead_s": None if playhead is None else str(playhead),
            "awaiting_bindings": collected["awaiting"],
            "input_manifest": collected["manifest"],
            "replay_contexts": collected["replay_contexts"],
            "freshness": collected["freshness"],
        }

    @staticmethod
    def _empty(collected: dict[str, Any], playhead: Decimal | None) -> dict[str, Any]:
        return {
            "status": "empty",
            "module_revision": collected["module_revision"],
            "source_id": collected["manifest"]["source_id"],
            "source_asset_id": collected["manifest"]["source_asset_id"],
            "original_source_id": collected["original_source_id"],
            "lane": collected["lane"],
            "playhead_s": None if playhead is None else str(playhead),
            "input_manifest": collected["manifest"],
            "replay_contexts": collected["replay_contexts"],
            "freshness": collected["freshness"],
        }

    def _ephemeral_result(
        self, collected: dict[str, Any], context: HostExecutionContext, playhead: Decimal | None
    ) -> dict[str, Any]:
        if collected["awaiting"]:
            result = self._pending(collected, playhead)
        elif collected["lane"] == "replay" and not collected["replay_contexts"]:
            result = self._empty(collected, playhead)
        else:
            result = {
                "status": "ephemeral",
                "projection_id": None,
                "module_revision": collected["module_revision"],
                "source_id": collected["manifest"]["source_id"],
                "source_asset_id": collected["manifest"]["source_asset_id"],
                "original_source_id": collected["original_source_id"],
                "lane": collected["lane"],
                "playhead_s": None if playhead is None else str(playhead),
                "digest": None,
                "input_manifest": collected["manifest"],
                "replay_contexts": collected["replay_contexts"],
                "freshness": collected["freshness"],
                "snapshot": (
                    project_continuous_observations(
                        collected["facts"], at_s=playhead, epoch_id=collected["epoch_id"]
                    )
                    if collected.get("epoch_id") is not None
                    else project_observations(collected["facts"], at_s=playhead)
                ),
                "created_at": None,
            }
        _json_bytes(result, min(context.bounds.max_output_bytes, MAX_HISTORY_BYTES))
        return result

    async def project_at(
        self, context: HostExecutionContext, source_id: str, asset_id: str, at_s: Any
    ) -> dict[str, Any]:
        """Read an ephemeral source-time projection without creating a row."""

        _identifier(source_id)
        _identifier(asset_id)
        playhead = _playhead(at_s)
        if playhead is None:
            raise ValueError("project_at requires an explicit source playhead")
        async with self.store._transaction(context, "realtime:read") as con:
            collected = await self._collect(con, context, source_id, asset_id, playhead)
            return self._ephemeral_result(collected, context, playhead)

    async def _verify_epoch(self, con, context, source_id: str, epoch_id: str) -> None:
        row = await con.fetchrow(
            "select e.epoch_id from realtime.tracker_epoch e "
            "join realtime.subject camera on camera.tenant_id=e.tenant_id "
            "and camera.source_id=e.source_id and camera.subject_id=e.camera_subject_id "
            "where e.tenant_id=$1 and e.source_id=$2 and e.epoch_id=$3 "
            "and camera.kind='camera' and (select status from realtime.subject_revision latest "
            "where latest.tenant_id=camera.tenant_id and latest.source_id=camera.source_id "
            "and latest.subject_id=camera.subject_id order by revision desc limit 1)='active'",
            context.company_id,
            source_id,
            epoch_id,
        )
        if row is None:
            raise AccessDenied("tracker epoch is unavailable in this camera source")

    async def _with_playback(self, con, context, source_id, epoch_id, result, playhead=None):
        from realtime.continuity import ContinuityStore

        result["playback"] = await ContinuityStore(self.store)._playback(
            con, context, source_id, epoch_id, playhead
        )
        _json_bytes(result, min(context.bounds.max_output_bytes, MAX_HISTORY_BYTES))
        return result

    async def project_epoch_at(
        self, context: HostExecutionContext, source_id: str, epoch_id: str, at_s: Any
    ) -> dict[str, Any]:
        """Read an epoch projection at stream time without creating a row."""
        _identifier(source_id)
        _identifier(epoch_id)
        playhead = _playhead(at_s)
        if playhead is None:
            raise ValueError("project_epoch_at requires an explicit stream playhead")
        asset_id = self._epoch_asset_id(epoch_id)
        async with self.store._transaction(context, "realtime:read") as con:
            await self.store._source(con, context, source_id)
            await self._verify_epoch(con, context, source_id, epoch_id)
            collected = await self._collect(con, context, source_id, asset_id, playhead, epoch_id=epoch_id)
            return await self._with_playback(
                con,
                context,
                source_id,
                epoch_id,
                self._ephemeral_result(collected, context, playhead),
                playhead,
            )

    async def refresh_epoch(
        self,
        context: HostExecutionContext,
        source_id: str,
        epoch_id: str,
        at_s: Any = None,
    ) -> dict[str, Any]:
        """Persist one tracker epoch ordered by its host stream clock."""
        _identifier(source_id)
        _identifier(epoch_id)
        playhead = _playhead(at_s)
        if playhead is not None:
            return await self.project_epoch_at(context, source_id, epoch_id, playhead)
        if not {"realtime:read", "realtime:process"} <= context.capabilities:
            raise AccessDenied("epoch refresh requires read and process capabilities")
        asset_id = self._epoch_asset_id(epoch_id)
        async with self.store._transaction(context, "realtime:process") as con:
            source = await self.store._source(con, context, source_id)
            if source["kind"] != "camera":
                raise AccessDenied("continuity projections require a camera source")
            await self._verify_epoch(con, context, source_id, epoch_id)
            if not await con.fetchval(
                "select realtime.source_processable($1,$2)", context.company_id, source_id
            ):
                raise AccessDenied("source grant does not permit module processing")
            limits = await self.store._lock_budget(con, context)
            collected = await self._collect(con, context, source_id, asset_id, None, epoch_id=epoch_id)
            if collected["awaiting"]:
                pending = self._pending(collected, None)
                _json_bytes(pending, min(context.bounds.max_output_bytes, MAX_HISTORY_BYTES))
                return await self._with_playback(con, context, source_id, epoch_id, pending)
            snapshot = project_continuous_observations(collected["facts"], at_s=None, epoch_id=epoch_id)
            manifest = collected["manifest"]
            freshness = collected["freshness"]
            _json_bytes(manifest, MAX_HISTORY_BYTES)
            _json_bytes(snapshot, MAX_HISTORY_BYTES)
            _json_bytes(freshness, 8192)
            digest = _bounded_digest(
                {
                    "module_revision": EPOCH_MODULE_REVISION,
                    "source_id": source_id,
                    "source_asset_id": asset_id,
                    "epoch_id": epoch_id,
                    "original_source_id": collected["original_source_id"],
                    "lane": collected["lane"],
                    "playhead_s": None,
                    "manifest": manifest,
                    "snapshot": snapshot,
                }
            )
            prior = await con.fetchrow(
                "select * from realtime.module_projection where tenant_id=$1 and digest=$2",
                context.company_id,
                digest,
            )
            if prior:
                return await self._with_playback(
                    con, context, source_id, epoch_id, self._result(dict(prior), context)
                )
            usage = await self.store._usage(con, context)
            needed = await con.fetchval(
                "select octet_length($1::jsonb::text)+octet_length($2::jsonb::text)+"
                "octet_length($3::jsonb::text)+octet_length($4::jsonb::text)+2048",
                manifest,
                snapshot,
                freshness,
                collected["replay_contexts"],
            )
            self.store._check_bytes(limits, usage, needed)
            module_usage = await con.fetchrow("select * from realtime.module_usage($1)", context.company_id)
            if module_usage["projection_count"] >= min(
                MAX_MODULE_PROJECTIONS, max(1024, limits["max_events"] * 4)
            ):
                raise CapacityExceeded("core module projection budget reached")
            row = await con.fetchrow(
                "insert into realtime.module_projection(tenant_id,source_id,source_asset_id,"
                "original_source_id,lane,module_revision,playhead_s,digest,input_manifest,"
                "snapshot,freshness,replay_contexts) "
                "values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) returning *",
                context.company_id,
                source_id,
                asset_id,
                collected["original_source_id"],
                collected["lane"],
                EPOCH_MODULE_REVISION,
                None,
                digest,
                manifest,
                snapshot,
                freshness,
                collected["replay_contexts"],
            )
            return await self._with_playback(
                con, context, source_id, epoch_id, self._result(dict(row), context)
            )

    async def latest_epoch(
        self, context: HostExecutionContext, source_id: str, epoch_id: str, at_s: Any = None
    ) -> dict[str, Any] | None:
        """Read the stored head for an epoch namespace."""
        _identifier(source_id)
        _identifier(epoch_id)
        playhead = _playhead(at_s)
        if playhead is not None:
            return await self.project_epoch_at(context, source_id, epoch_id, playhead)
        asset_id = self._epoch_asset_id(epoch_id)
        async with self.store._transaction(context, "realtime:read") as con:
            await self.store._source(con, context, source_id)
            identity = await con.fetchrow(
                "select projection_id from realtime.module_projection where tenant_id=$1 and source_id=$2 "
                "and source_asset_id=$3 and module_revision=$4 and playhead_s is null "
                "order by projection_id desc limit 1",
                context.company_id,
                source_id,
                asset_id,
                EPOCH_MODULE_REVISION,
            )
            if identity is None:
                exists = await con.fetchval(
                    "select 1 from realtime.tracker_epoch "
                    "where tenant_id=$1 and source_id=$2 and epoch_id=$3",
                    context.company_id,
                    source_id,
                    epoch_id,
                )
                if not exists:
                    return None
                await self._verify_epoch(con, context, source_id, epoch_id)
                collected = await self._collect(con, context, source_id, asset_id, None, epoch_id=epoch_id)
                result = self._ephemeral_result(collected, context, None)
            else:
                row = await self._fetch_bounded_projection(con, context, identity["projection_id"])
                if row is None:
                    return None
                result = self._result(dict(row), context)
            return await self._with_playback(con, context, source_id, epoch_id, result)

    async def refresh(
        self,
        context: HostExecutionContext,
        source_id: str,
        asset_id: str,
        at_s: Any = None,
    ) -> dict[str, Any]:
        """Persist the canonical head; playhead reads use :meth:`project_at`."""

        _identifier(source_id)
        _identifier(asset_id)
        playhead = _playhead(at_s)
        if playhead is not None:
            return await self.project_at(context, source_id, asset_id, playhead)
        if "realtime:read" not in context.capabilities or "realtime:process" not in context.capabilities:
            raise AccessDenied("module refresh requires read and process capabilities")
        async with self.store._transaction(context, "realtime:process") as con:
            await self.store._source(con, context, source_id)
            if not await con.fetchval(
                "select realtime.source_processable($1,$2)", context.company_id, source_id
            ):
                raise AccessDenied("source grant does not permit module processing")
            # Admission and projection writes share this company lock.  It must
            # precede history selection, otherwise a concurrent refresh can
            # race its idempotency check against a second insert.
            limits = await self.store._lock_budget(con, context)
            collected = await self._collect(con, context, source_id, asset_id, None)
            if collected["awaiting"]:
                pending = self._pending(collected, None)
                _json_bytes(pending, min(context.bounds.max_output_bytes, MAX_HISTORY_BYTES))
                return pending
            if collected["lane"] == "replay" and not collected["replay_contexts"]:
                empty = self._empty(collected, None)
                _json_bytes(empty, min(context.bounds.max_output_bytes, MAX_HISTORY_BYTES))
                return empty
            snapshot = project_observations(collected["facts"], at_s=None)
            manifest = collected["manifest"]
            freshness = collected["freshness"]
            _json_bytes(manifest, MAX_HISTORY_BYTES)
            _json_bytes(snapshot, MAX_HISTORY_BYTES)
            _json_bytes(freshness, 8192)
            digest = _bounded_digest(
                {
                    "module_revision": MODULE_REVISION,
                    "source_id": source_id,
                    "source_asset_id": asset_id,
                    "original_source_id": collected["original_source_id"],
                    "lane": collected["lane"],
                    "playhead_s": None,
                    "manifest": manifest,
                    "snapshot": snapshot,
                }
            )
            prior = await con.fetchrow(
                "select * from realtime.module_projection where tenant_id=$1 and digest=$2",
                context.company_id,
                digest,
            )
            if prior:
                return self._result(dict(prior), context)
            usage = await self.store._usage(con, context)
            needed = await con.fetchval(
                "select octet_length($1::jsonb::text)+octet_length($2::jsonb::text)+"
                "octet_length($3::jsonb::text)+octet_length($4::jsonb::text)+2048",
                manifest,
                snapshot,
                freshness,
                collected["replay_contexts"],
            )
            self.store._check_bytes(limits, usage, needed)
            module_usage = await con.fetchrow("select * from realtime.module_usage($1)", context.company_id)
            if module_usage["projection_count"] >= min(
                MAX_MODULE_PROJECTIONS, max(1024, limits["max_events"] * 4)
            ):
                raise CapacityExceeded("core module projection budget reached")
            row = await con.fetchrow(
                "insert into realtime.module_projection(tenant_id,source_id,source_asset_id,"
                "original_source_id,lane,module_revision,playhead_s,digest,input_manifest,"
                "snapshot,freshness,replay_contexts) "
                "values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) returning *",
                context.company_id,
                source_id,
                asset_id,
                collected["original_source_id"],
                collected["lane"],
                MODULE_REVISION,
                None,
                digest,
                manifest,
                snapshot,
                freshness,
                collected["replay_contexts"],
            )
            return self._result(dict(row), context)

    async def _authorize_manifest(self, con, context, source_id: str, manifest: dict[str, Any]):
        """Recheck every retained subject dependency through its current grant.

        A camera grant does not authorize a cached catalog-source association.
        Resolve the exact bound revision using the same subject reader as a
        fresh projection, including original replay and catalog association
        checks. Deduplicate references before the bounded lookups.
        """
        events = manifest.get("events")
        if not isinstance(events, list) or len(events) > min(MAX_HISTORY_EVENTS, context.bounds.max_events):
            raise CapacityExceeded("module projection exceeds the event budget")
        references = set()
        for event in events:
            bindings = event.get("bindings")
            if not isinstance(bindings, list) or len(bindings) > 32:
                raise AccessDenied("Module projection requires its retained subject bindings")
            for binding in bindings:
                subject_id, revision = binding.get("subject_id"), binding.get("subject_revision")
                _identifier(subject_id)
                if type(revision) is not int or revision < 1:
                    raise AccessDenied("Module projection requires exact subject revisions")
                references.add((subject_id, revision))
        for subject_id, revision in sorted(references):
            await self.subjects._read(con, context, subject_id, revision)
        continuity = manifest.get("continuity")
        if continuity:
            await self._verify_epoch(con, context, source_id, continuity["epoch_id"])

    async def _fetch_bounded_projection(self, con, context, projection_id: int):
        """Preflight size and current dependency grants before fetching snapshot JSON."""

        sized = await con.fetchrow(
            "select projection_id,octet_length(input_manifest::text)+"
            "octet_length(snapshot::text)+octet_length(freshness::text)+"
            "octet_length(replay_contexts::text)+4096 as bytes "
            "from realtime.module_projection where tenant_id=$1 and projection_id=$2",
            context.company_id,
            projection_id,
        )
        if not sized:
            return None
        if sized["bytes"] > context.bounds.max_output_bytes:
            raise CapacityExceeded("module projection exceeds the response byte budget")
        manifest = await con.fetchrow(
            "select source_id,input_manifest from realtime.module_projection "
            "where tenant_id=$1 and projection_id=$2",
            context.company_id,
            projection_id,
        )
        if manifest is None:
            return None
        await self._authorize_manifest(con, context, manifest["source_id"], manifest["input_manifest"])
        return await con.fetchrow(
            "select * from realtime.module_projection where tenant_id=$1 and projection_id=$2",
            context.company_id,
            projection_id,
        )

    async def get(self, context: HostExecutionContext, projection_id: int) -> dict[str, Any]:
        """Read one projection through its current source grant."""

        if type(projection_id) is not int or projection_id < 1:
            raise ValueError("projection_id must be a positive integer")
        async with self.store._transaction(context, "realtime:read") as con:
            row = await self._fetch_bounded_projection(con, context, projection_id)
            if not row:
                raise AccessDenied("module projection is unavailable in this source grant")
            return self._result(dict(row), context)

    async def latest(
        self,
        context: HostExecutionContext,
        source_id: str,
        asset_id: str,
        at_s: Any = None,
    ) -> dict[str, Any] | None:
        """Read the canonical stored head; source-time reads are ephemeral."""

        _identifier(source_id)
        _identifier(asset_id)
        playhead = _playhead(at_s)
        if playhead is not None:
            return await self.project_at(context, source_id, asset_id, playhead)
        async with self.store._transaction(context, "realtime:read") as con:
            await self.store._source(con, context, source_id)
            identity = await con.fetchrow(
                "select projection_id from realtime.module_projection where tenant_id=$1 and source_id=$2 "
                "and source_asset_id=$3 and module_revision=$4 and playhead_s is null "
                "order by projection_id desc limit 1",
                context.company_id,
                source_id,
                asset_id,
                MODULE_REVISION,
            )
            if identity is None:
                return None
            row = await self._fetch_bounded_projection(con, context, identity["projection_id"])
            return None if row is None else self._result(dict(row), context)
