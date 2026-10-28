"""Tenant-scoped admission and durable work. The host authenticates every context.

No model, broker or UI may write tables directly. This boundary rechecks membership
and the source grant on every transaction, including retries after revocation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import db
from realtime.contracts import CanonicalEvent, HostExecutionContext, SourceWindow


class RealtimeError(ValueError):
    """A rejected operation with no downstream effect."""


class AccessDenied(RealtimeError):
    pass


class Conflict(RealtimeError):
    pass


class CapacityExceeded(RealtimeError):
    pass


class DeadlineExceeded(CapacityExceeded):
    """The complete scoped operation exhausted its host time budget."""


@dataclass(frozen=True)
class AdmissionResult:
    status: str
    reason: str | None = None
    sequence: int | None = None


def scope_for_company(company_id: str) -> str:
    """Preserve safe canonical labels; otherwise use a collision-resistant label.

    A persisted company binding must match; this function cannot provision one.
    In particular a display/environment alias cannot select a company.
    """
    label = (
        company_id
        if re.fullmatch(r"[a-z0-9_]{1,256}", company_id)
        else ("h_" + hashlib.sha256(company_id.encode()).hexdigest())
    )
    return "t." + label


def _encoded(value: Any, limit: int = 262144) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if len(encoded) > limit:
        raise CapacityExceeded("record exceeds its byte budget")
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or "\x00" in value:
        raise ValueError("expected a bounded opaque identifier")
    return value


class RealTimeStore:
    """Shared SDK for camera adapters, replay, hosted agents and MCP services."""

    @asynccontextmanager
    async def _transaction(self, context: HostExecutionContext, capability: str):
        if capability not in context.capabilities:
            raise AccessDenied("operation is outside the host capability grant")
        remaining = (context.deadline_utc - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise AccessDenied("host request deadline expired")
        # Convert the trusted wall-clock allowance once; elapsed work uses a monotonic clock.
        # The existing 30-second statement ceiling also bounds one complete transaction.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(30.0, remaining)
        budget = asyncio.timeout_at(deadline)
        try:
            async with budget, db.scoped_tx(scope_for_company(context.company_id)) as con:
                role = await con.fetchrow(
                    "select current_user as name,rolsuper,rolbypassrls "
                    "from pg_roles where rolname=current_user"
                )
                if role["name"] != "rocoto_rt" or role["rolsuper"] or role["rolbypassrls"]:
                    raise AccessDenied("Real-time requires the restricted runtime database role")
                await con.execute("select set_config('rocoto.principal',$1,true)", context.principal_id)
                await con.execute("select set_config('rocoto.request_id',$1,true)", context.request_id)
                remaining_ms = max(1, int((deadline - loop.time()) * 1000))
                await con.execute("select set_config('statement_timeout',$1,true)", str(remaining_ms))
                capabilities = await con.fetchval(
                    "select realtime.actor_capabilities($1,$2)", context.company_id, context.principal_id
                )
                if capability not in capabilities:
                    raise AccessDenied("current membership does not grant this operation")
                binding = await con.fetchrow(
                    "select tenant_id from realtime.company_binding where tenant_id=$1", context.company_id
                )
                if not binding:
                    raise AccessDenied("company identity is not commissioned for this scope")
                yield con
                # A host callback that delays or catches cancellation still cannot commit late.
                if loop.time() >= deadline:
                    raise DeadlineExceeded("operation deadline exceeded")
        except TimeoutError as error:
            if budget.expired():
                raise DeadlineExceeded("operation deadline exceeded") from error
            raise

    async def _source(self, con, context, source_id, *, writing=False):
        source = await con.fetchrow(
            "select * from realtime.source where tenant_id=$1 and source_id=$2 and active",
            context.company_id,
            source_id,
        )
        if not source:
            raise AccessDenied("source is unavailable in this grant")
        if writing:
            caps = await con.fetchval(
                "select realtime.actor_capabilities($1,$2)", context.company_id, context.principal_id
            )
            if "realtime:configure" not in caps and not await con.fetchval(
                "select can_ingest from realtime.resource_grant where tenant_id=$1 and "
                "principal_id=$2 and source_id=$3",
                context.company_id,
                context.principal_id,
                source_id,
            ):
                raise AccessDenied("source grant does not permit ingestion")
        return source

    async def register_source(self, context, source_id, kind="camera", metadata=None):
        _identifier(source_id)
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise ValueError("source metadata must be an object")
        _encoded(metadata, 60000)
        if kind not in {"camera", "connector", "processing", "replay"}:
            raise ValueError("unsupported source kind")
        async with self._transaction(context, "realtime:configure") as con:
            limits = await self._lock_budget(con, context)
            existing = await con.fetchrow(
                "select * from realtime.source where tenant_id=$1 and source_id=$2",
                context.company_id,
                source_id,
            )
            if existing:
                if existing["kind"] != kind or existing["metadata"] != metadata:
                    raise Conflict("source identity already has different metadata")
                return dict(existing)
            usage = await self._usage(con, context)
            self._check_bytes(
                limits, usage, await con.fetchval("select octet_length($1::jsonb::text)+512", metadata)
            )
            if usage["source_count"] >= 256:
                raise CapacityExceeded("company source budget reached")
            await con.execute(
                "insert into realtime.source(tenant_id,source_id,kind,metadata) values($1,$2,$3,$4) "
                "on conflict do nothing",
                context.company_id,
                source_id,
                kind,
                metadata,
            )
            row = await con.fetchrow(
                "select * from realtime.source where tenant_id=$1 and source_id=$2",
                context.company_id,
                source_id,
            )
            if row["kind"] != kind or row["metadata"] != metadata:
                raise Conflict("source identity already has different metadata")
            return dict(row)

    async def register_profile(self, context, source_id, revision_id, event_types):
        async with self._transaction(context, "realtime:configure") as con:
            return await self._register_profile(con, context, source_id, revision_id, event_types)

    async def _register_profile(self, con, context, source_id, revision_id, event_types):
        _identifier(revision_id)
        if not isinstance(event_types, (list, tuple)):
            raise ValueError("event types must be a list")
        names = sorted(set(event_types))
        if not 1 <= len(names) <= 256 or any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", n) for n in names):
            raise ValueError("profile needs bounded registered event names")
        await self._source(con, context, source_id)
        limits = await self._lock_budget(con, context)
        prior = await con.fetchval(
            "select event_types from realtime.profile_revision "
            "where tenant_id=$1 and source_id=$2 and revision_id=$3",
            context.company_id,
            source_id,
            revision_id,
        )
        if prior:
            if prior != names:
                raise Conflict("profile revision is immutable")
            return
        usage = await self._usage(con, context)
        self._check_bytes(
            limits, usage, await con.fetchval("select octet_length($1::text[]::text)+512", names)
        )
        if usage["profile_count"] >= 2048:
            raise CapacityExceeded("company profile revision budget reached")
        await con.execute(
            "insert into realtime.profile_revision(tenant_id,source_id,revision_id,event_types) "
            "values($1,$2,$3,$4) on conflict do nothing",
            context.company_id,
            source_id,
            revision_id,
            names,
        )
        existing = await con.fetchval(
            "select event_types from realtime.profile_revision "
            "where tenant_id=$1 and source_id=$2 and revision_id=$3",
            context.company_id,
            source_id,
            revision_id,
        )
        if existing != names:
            raise Conflict("profile revision is immutable")

    async def record_receipt(self, context, source_id, receipt_id, body):
        _identifier(receipt_id)
        if body.get("kind") != "actual_input":
            raise ValueError("receipt must describe actual worker input")
        _identifier(body.get("source_asset_id"))
        _identifier(body.get("profile_revision_id"))
        SourceWindow.model_validate(body.get("source_window"))
        digest = _digest(body)
        async with self._transaction(context, "realtime:ingest") as con:
            await self._source(con, context, source_id, writing=True)
            if not await con.fetchval(
                "select 1 from realtime.profile_revision where tenant_id=$1 and source_id=$2 and "
                "revision_id=$3",
                context.company_id,
                source_id,
                body["profile_revision_id"],
            ):
                raise Conflict("receipt references an unregistered profile revision")
            # Serialize mapping registration with receipts that might predate a mapping.
            limits = await self._lock_budget(con, context)
            from realtime.continuity import ContinuityStore

            source = await self._source(con, context, source_id)
            if source["kind"] == "replay" and body.get("continuity") is not None:
                raise Conflict("Epoch continuity replay requires a commissioned compiler mapping")
            await ContinuityStore(self).validate_input(con, context, source_id, body)
            # Retained receipts are bounded before worker/broker acknowledgement.
            prior = await con.fetchval(
                "select digest from realtime.input_receipt where tenant_id=$1 and source_id=$2 and "
                "receipt_id=$3",
                context.company_id,
                source_id,
                receipt_id,
            )
            if prior:
                if prior != digest:
                    raise Conflict("receipt identity conflicts with retained input")
                return digest
            usage = await self._usage(con, context)
            self._check_bytes(
                limits, usage, await con.fetchval("select octet_length($1::jsonb::text)+512", body)
            )
            if usage["receipt_count"] >= 5000:
                raise CapacityExceeded("retained input receipt budget reached")
            await con.execute(
                "insert into realtime.input_receipt(tenant_id,source_id,receipt_id,digest,body) "
                "values($1,$2,$3,$4,$5)",
                context.company_id,
                source_id,
                receipt_id,
                digest,
                body,
            )
            return digest

    async def _lock_budget(self, con, context):
        # A row lock serializes admissions without changing the identity/budget itself.
        return await con.fetchrow(
            "select max_events,max_outbox,max_bytes from realtime.company_binding where "
            "tenant_id=$1 for update",
            context.company_id,
        )

    async def _usage(self, con, context):
        return await con.fetchrow("select * from realtime.retained_usage($1)", context.company_id)

    @staticmethod
    def _check_bytes(limits, usage, added):
        if usage["retained_bytes"] + added > limits["max_bytes"]:
            raise CapacityExceeded("company retained-data budget reached")

    async def _evidence_reason(self, con, context, source_id, event, replay_context=None):
        p = event.provenance
        if p.kind == "observation":
            registered = await con.fetchval(
                "select event_types from realtime.profile_revision "
                "where tenant_id=$1 and source_id=$2 and revision_id=$3",
                context.company_id,
                source_id,
                p.profile_revision_id,
            )
            if not registered or event.event_type not in registered:
                return "unregistered_event_type"
            for receipt_id in p.actual_receipt_refs:
                body = await con.fetchval(
                    "select body from realtime.input_receipt where tenant_id=$1 and source_id=$2 "
                    "and receipt_id=$3",
                    context.company_id,
                    source_id,
                    receipt_id,
                )
                if not body:
                    return "missing_actual_input_receipt"
                window = SourceWindow.model_validate(body["source_window"])
                if (
                    body["profile_revision_id"] != p.profile_revision_id
                    or body["source_asset_id"] != p.source_asset_id
                    or window.start_s > p.source_window.start_s
                    or window.end_s < p.source_window.end_s
                ):
                    return "source_evidence_mismatch"
            from realtime.continuity import ContinuityStore

            reason = await ContinuityStore(self).event_reason(con, context, source_id, event, replay_context)
            if reason:
                return reason
            from realtime.configuration import validate_admission

            reason = await validate_admission(con, context, source_id, event)
            if reason:
                return reason
        elif p.kind == "business":
            if not await con.fetchval(
                "select 1 from realtime.source where tenant_id=$1 and source_id=$2 and kind='connector'",
                context.company_id,
                source_id,
            ):
                return "business_origin_requires_connector_source"
            if p.connector_ref != source_id:
                return "connector_identity_mismatch"
        else:
            lineage = await con.fetchrow(
                "select count(*) as found, bool_or(e.replay_context is not null) as has_replay, "
                "bool_or(e.replay_context is null) as has_live, "
                "bool_or((e.replay_context->>'run_id') is distinct from $4 or "
                "(e.replay_context->>'loop_id') is distinct from $5 or "
                "(e.replay_context->>'plan_digest') is distinct from $6) as different_replay "
                "from realtime.event e join unnest($2::text[],$3::integer[]) as ref(id,revision) "
                "on e.event_id=ref.id and e.event_revision=ref.revision where e.tenant_id=$1",
                context.company_id,
                [ref.event_id for ref in p.input_event_refs],
                [ref.event_revision for ref in p.input_event_refs],
                (replay_context or {}).get("run_id"),
                (replay_context or {}).get("loop_id"),
                (replay_context or {}).get("plan_digest"),
            )
            if lineage["found"] != len(p.input_event_refs):
                return "missing_processing_input_revision"
            if lineage["has_replay"]:
                if replay_context is None or lineage["has_live"] or lineage["different_replay"]:
                    return "processing_replay_lineage_mismatch"
            elif replay_context is not None:
                return "processing_replay_lineage_mismatch"
            if not await con.fetchval(
                "select 1 from realtime.profile_revision where tenant_id=$1 and source_id=$2 "
                "and revision_id=$3 and $4=any(event_types)",
                context.company_id,
                source_id,
                p.processing_revision_id,
                event.event_type,
            ):
                return "unregistered_processing_revision"
        return None

    async def admit(self, context, source_id, event: CanonicalEvent) -> AdmissionResult:
        async with self._transaction(context, "realtime:ingest") as con:
            return await self._admit(con, context, source_id, event)

    async def _admit(
        self, con, context, source_id, event: CanonicalEvent, replay_context=None
    ) -> AdmissionResult:
        # Revalidate at the durable boundary even if an in-process caller used model_construct.
        event = CanonicalEvent.model_validate(event.model_dump(mode="json", by_alias=True))
        envelope = event.model_dump(mode="json", by_alias=True)
        if len(_encoded(event.payload)) > context.bounds.max_payload_bytes:
            raise CapacityExceeded("payload exceeds this host request budget")

        def depth(value):
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            return 1 + max((depth(child) for child in children), default=0)

        if depth(event.payload) > context.bounds.max_payload_depth:
            raise CapacityExceeded("payload exceeds this host request depth budget")
        if replay_context is not None:
            if not isinstance(replay_context, dict) or replay_context.get("company_id") != context.company_id:
                raise AccessDenied("replay context must belong to this company")
            if (
                replay_context.get("external_effects") != "disabled"
                or replay_context.get("agent_wakes") != "disabled"
            ):
                raise AccessDenied("compiled replay keeps delivery and agent wakes disabled")
            for key in ("run_id", "loop_id", "plan_digest", "entity_namespace"):
                _identifier(replay_context.get(key))
            _encoded(replay_context, 65536)
        digest = _digest(
            envelope if replay_context is None else {"event": envelope, "replay": replay_context}
        )
        source = await self._source(con, context, source_id, writing=True)
        if (source["kind"] == "replay") != (replay_context is not None):
            raise Conflict("replay sources require their persisted run context")
        limits = await self._lock_budget(con, context)
        prior = await con.fetchrow(
            "select digest,sequence,source_id from realtime.event where tenant_id=$1 and "
            "event_id=$2 and event_revision=$3",
            context.company_id,
            event.event_id,
            event.event_revision,
        )
        if prior:
            if prior["digest"] != digest or prior["source_id"] != source_id:
                raise Conflict("event identity/revision conflicts with retained evidence")
            return AdmissionResult("duplicate", sequence=prior["sequence"])
        external_identity = event.provenance.external_identity if event.origin_kind == "business" else None
        if external_identity:
            bound_event = await con.fetchval(
                "select event_id from realtime.event where tenant_id=$1 and source_id=$2 "
                "and external_identity=$3 limit 1",
                context.company_id,
                source_id,
                external_identity,
            )
            if bound_event and bound_event != event.event_id:
                raise Conflict("connector fact already belongs to another canonical event")
        reason = await self._evidence_reason(con, context, source_id, event, replay_context)
        latest = await con.fetchrow(
            "select event_revision,source_id,external_identity,envelope,replay_context from realtime.event "
            "where tenant_id=$1 and event_id=$2 "
            "order by event_revision desc limit 1",
            context.company_id,
            event.event_id,
        )
        if latest and latest["source_id"] != source_id:
            raise Conflict("event source cannot change across revisions")
        if latest and latest["external_identity"] != external_identity:
            raise Conflict("external identity cannot change across event revisions")
        if latest:
            previous = CanonicalEvent.model_validate(latest["envelope"])
            if previous.origin_kind != event.origin_kind:
                raise Conflict("event origin cannot change across revisions")
            if event.origin_kind == "observation" and (
                previous.provenance.source_asset_id != event.provenance.source_asset_id
            ):
                raise Conflict("observation source asset cannot change across revisions")
            old_replay = latest["replay_context"] or {}
            new_replay = replay_context or {}
            if any(
                old_replay.get(key) != new_replay.get(key)
                for key in ("run_id", "loop_id", "plan_digest", "entity_namespace")
            ):
                raise Conflict("event replay lineage cannot change across revisions")
        if event.event_revision != (latest["event_revision"] + 1 if latest else 1):
            reason = "nonsequential_event_revision"
        if reason:
            existing = await con.fetchval(
                "select 1 from realtime.quarantine where tenant_id=$1 and source_id=$2 and digest=$3",
                context.company_id,
                source_id,
                digest,
            )
            usage = await self._usage(con, context)
            if not existing:
                self._check_bytes(
                    limits,
                    usage,
                    await con.fetchval("select octet_length($1::jsonb::text)+512", envelope),
                )
                if usage["quarantine_count"] >= 500:
                    raise CapacityExceeded("quarantine budget reached")
            await con.execute(
                "insert into realtime.quarantine(tenant_id,source_id,digest,reason,envelope) "
                "values($1,$2,$3,$4,$5) on conflict do nothing",
                context.company_id,
                source_id,
                digest,
                reason,
                envelope,
            )
            return AdmissionResult("quarantined", reason=reason)
        usage = await self._usage(con, context)
        self._check_bytes(
            limits,
            usage,
            await con.fetchval(
                "select octet_length($1::jsonb::text)+coalesce(octet_length($2::jsonb::text),0)+1024",
                envelope,
                replay_context,
            ),
        )
        if usage["event_count"] >= limits["max_events"]:
            raise CapacityExceeded("retained event budget reached")
        if usage["pending_count"] >= limits["max_outbox"]:
            raise CapacityExceeded("pending processing budget reached")
        sequence = await con.fetchval(
            "insert into "
            "realtime.event(tenant_id,source_id,event_id,event_revision,"
            "event_type,occurred_at,digest,envelope,external_identity,replay_context) "
            "values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) returning sequence",
            context.company_id,
            source_id,
            event.event_id,
            event.event_revision,
            event.event_type,
            event.occurred_at_utc,
            digest,
            envelope,
            external_identity,
            replay_context,
        )
        await con.execute(
            "insert into realtime.outbox(tenant_id,source_id,event_id,event_revision,lane) "
            "values($1,$2,$3,$4,$5)",
            context.company_id,
            source_id,
            event.event_id,
            event.event_revision,
            "replay" if replay_context is not None else "live",
        )
        return AdmissionResult("accepted", sequence=sequence)

    async def read_events(self, context, source_id, after_sequence=0, limit=100):
        if not 1 <= limit <= min(1000, context.bounds.max_events) or after_sequence < 0:
            raise ValueError("invalid bounded cursor")
        async with self._transaction(context, "realtime:read") as con:
            await self._source(con, context, source_id)
            rows = await con.fetch(
                "with candidates as (select sequence,octet_length(envelope::text)+"
                "coalesce(octet_length(replay_context::text),0)+512 as bytes "
                "from realtime.event where tenant_id=$1 and source_id=$2 and sequence>$3 "
                "order by sequence limit $4), "
                "sized as(select sequence,sum(bytes) over(order by sequence) as total, "
                "row_number() over(order by sequence) as ordinal from candidates) "
                "select e.sequence,e.source_id,e.committed_at,s.total, "
                "case when s.total<=$5 then e.replay_context else null end as replay_context, "
                "case when s.total<=$5 then e.envelope else null end as envelope "
                "from sized s join realtime.event e on e.tenant_id=$1 and e.sequence=s.sequence "
                "where s.total<=$5 or s.ordinal=1 order by e.sequence",
                context.company_id,
                source_id,
                after_sequence,
                limit,
                context.bounds.max_output_bytes,
            )
            if rows and rows[0]["envelope"] is None:
                raise CapacityExceeded("first event exceeds response budget")
            return [{k: v for k, v in dict(row).items() if k != "total"} for row in rows]

    async def claim_outbox(self, context, limit=10, lease_seconds=30, lane="live"):
        if lane not in {"live", "replay"}:
            raise ValueError("unsupported processing lane")
        if not 1 <= limit <= min(100, context.bounds.max_events) or not 1 <= lease_seconds <= 300:
            raise ValueError("lease bounds exceeded")
        async with self._transaction(context, "realtime:process") as con:
            # An exhausted lease is retained visibly; retries never spin forever.
            await con.execute(
                "update realtime.outbox set state='failed',lease_token=null,lease_until=null "
                "where tenant_id=$1 and state='leased' and lease_until<=now() and attempts>=5 "
                "and realtime.source_processable(tenant_id,source_id)",
                context.company_id,
            )
            rows = await con.fetch(
                "select outbox_id from realtime.outbox where tenant_id=$1 and attempts<5 and lane=$3 "
                "and realtime.source_processable(tenant_id,source_id) "
                "and ((state='pending' and available_at<=now()) or (state='leased' and lease_until<=now())) "
                "order by outbox_id for update skip locked limit $2",
                context.company_id,
                limit,
                lane,
            )
            claimed = []
            for row in rows:
                record = await con.fetchrow(
                    "update realtime.outbox set state='leased',attempts=attempts+1,lease_token=$3,"
                    "lease_until=now()+$4*interval '1 second' where tenant_id=$1 and outbox_id=$2 "
                    "returning *",
                    context.company_id,
                    row["outbox_id"],
                    str(uuid.uuid4()),
                    lease_seconds,
                )
                if record is not None:
                    claimed.append(dict(record))
            return claimed

    async def finish_outbox(self, context, outbox_id, lease_token):
        async with self._transaction(context, "realtime:process") as con:
            result = await con.execute(
                "update realtime.outbox set "
                "state='complete',completed_at=now(),lease_token=null,lease_until=null "
                "where tenant_id=$1 and outbox_id=$2 and state='leased' and lease_token=$3 and "
                "lease_until>now()",
                context.company_id,
                outbox_id,
                lease_token,
            )
            return result == "UPDATE 1"
