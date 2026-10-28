"""Durable observation replay through the ordinary scoped admission service.

Start reads admitted database records, never caller-authored events. Preparation
is resumable; a tick commits event, replay provenance, outbox and cursor together.
Clock movement comes only from PostgreSQL. The live processing lane stays separate.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal

from realtime.contracts import CanonicalEvent
from realtime.replay import (
    MAX_EVENTS,
    MAX_PLAN_BYTES,
    AdmittedReplayEvent,
    ReplayCursor,
    ReplayError,
    ReplayReceipt,
    ReplaySource,
    compile_replay,
    due_events,
    initial_cursor,
    replay_context_dict,
    restore_plan,
)
from realtime.store import AccessDenied, CapacityExceeded, Conflict, RealTimeStore, _digest, _identifier
from realtime.subjects import SubjectStore

MAX_SOURCES = 16
MAX_PLANS = 4
MAX_BATCH_BYTES = 1_048_576


def _wire(value):
    return json.loads(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=lambda v: str(v) if isinstance(v, Decimal) else _unsupported(v),
        )
    )


def _unsupported(value):
    raise TypeError(type(value).__name__)


def _bytes(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())


class ReplayStore:
    def __init__(self, store=None):
        self.store = store or RealTimeStore()
        self.subjects = SubjectStore(self.store)

    @staticmethod
    def _request(sources, loops, rate, readiness_lag_s):
        if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= MAX_SOURCES:
            raise ReplayError("Select between 1 and 16 registered original source windows.")
        normalized = []
        for source in sources:
            if not isinstance(source, ReplaySource):
                raise ReplayError("Select typed source windows rather than supplying events.")
            _identifier(source.source_id)
            _identifier(source.source_asset_id)
            values = []
            for value in (source.start_s, source.end_s, source.offset_s):
                parsed = Decimal(str(value))
                if not parsed.is_finite() or parsed < 0:
                    raise ReplayError("Use finite nonnegative source clock bounds.")
                values.append(parsed)
            if values[1] <= values[0]:
                raise ReplayError("Select a nonempty original source interval.")
            normalized.append(ReplaySource(source.source_id, source.source_asset_id, *values))
        if len({s.source_id for s in normalized}) != len(normalized):
            raise ReplayError("Select one explicit window per source.")
        if type(loops) is not int or not 1 <= loops <= 10:
            raise ReplayError("Use between 1 and 10 replay loops.")
        rate, lag = Decimal(str(rate)), Decimal(str(readiness_lag_s))
        if (
            not rate.is_finite()
            or not lag.is_finite()
            or not Decimal("0.1") <= rate <= 16
            or not 0 <= lag <= 300
        ):
            raise ReplayError("Use bounded replay speed and readiness lag.")
        duration = max(s.offset_s + (s.end_s - s.start_s) / rate + lag for s in normalized) * loops
        if duration > 3600:
            raise ReplayError("Keep the complete replay within one hour.")
        normalized.sort(key=lambda s: s.source_id)
        request = _wire(
            {
                "schema": "rocoto.replay-request.v1",
                "sources": [asdict(s) for s in normalized],
                "loops": loops,
                "rate": rate,
                "readiness_lag_s": lag,
            }
        )
        return tuple(normalized), request

    async def _load_inputs(self, con, context, sources, loops):
        records, receipts = [], {}
        read_sources = {s.source_id for s in sources}
        retained_bytes = 0
        max_records = min(MAX_EVENTS // loops, context.bounds.max_events)
        for source in sources:
            original = await self.store._source(con, context, source.source_id)
            if original["kind"] != "camera":
                raise ReplayError(
                    "Select camera observations; other source kinds require separate replay semantics."
                )
            query = (
                "select sequence,envelope from realtime.event where tenant_id=$1 and source_id=$2 "
                "and replay_context is null and envelope->>'origin_kind'='observation' "
                "and envelope->'provenance'->>'source_asset_id'=$3 "
                "and (envelope->'provenance'->'source_window'->>'start_s')::numeric >= $4 "
                "and (envelope->'provenance'->'source_window'->>'end_s')::numeric <= $5 "
                "order by sequence limit $6"
            )
            async for row in con.cursor(
                query,
                context.company_id,
                source.source_id,
                source.source_asset_id,
                source.start_s,
                source.end_s,
                max_records + 1,
                prefetch=8,
            ):
                retained_bytes += _bytes(row["envelope"])
                if len(records) >= max_records or retained_bytes > MAX_PLAN_BYTES:
                    raise CapacityExceeded("selected admitted history exceeds the replay preparation budget")
                event = CanonicalEvent.model_validate(row["envelope"])
                entities = await self.subjects._replay_entities(
                    con, context, source.source_id, event.event_id, event.event_revision
                )
                for entity in entities or ():
                    subject = await self.subjects._read(con, context, entity.entity_id)
                    read_sources.add(subject["source_id"])
                if len(read_sources) > MAX_SOURCES:
                    raise CapacityExceeded("Replay subject dependencies exceed the source grant budget")
                binding_digest = await con.fetchval(
                    "select digest from realtime.event_subject_set where tenant_id=$1 and source_id=$2 "
                    "and event_id=$3 and event_revision=$4",
                    context.company_id,
                    source.source_id,
                    event.event_id,
                    event.event_revision,
                )
                records.append(
                    AdmittedReplayEvent(
                        source.source_id,
                        row["sequence"],
                        event,
                        entities=entities or (),
                        subject_binding_digest=binding_digest,
                    )
                )
                # Only revision-chain references are canonical today. Payload identifiers
                # or similarly named correction fields are deliberately not interpreted.
                for receipt_id in event.provenance.actual_receipt_refs:
                    key = (source.source_id, receipt_id)
                    if key in receipts:
                        continue
                    saved = await con.fetchrow(
                        "select body,digest from realtime.input_receipt "
                        "where tenant_id=$1 and source_id=$2 and receipt_id=$3",
                        context.company_id,
                        source.source_id,
                        receipt_id,
                    )
                    if saved is None or _digest(saved["body"]) != saved["digest"]:
                        raise Conflict("retained original input receipt is unavailable or inconsistent")
                    retained_bytes += _bytes(saved["body"])
                    if retained_bytes > MAX_PLAN_BYTES or len(receipts) >= MAX_EVENTS:
                        raise CapacityExceeded(
                            "selected original receipts exceed the replay preparation budget"
                        )
                    body = saved["body"]
                    receipts[key] = ReplayReceipt(
                        receipt_id,
                        source.source_id,
                        body["source_asset_id"],
                        body["profile_revision_id"],
                        Decimal(body["source_window"]["start_s"]),
                        Decimal(body["source_window"]["end_s"]),
                        saved["digest"],
                    )
        return records, list(receipts.values()), sorted(read_sources)

    async def start(self, context, run_id, *, sources, loops=1, rate=1, readiness_lag_s=0):
        """Persist one immutable plan, commission its sources, then start the DB clock."""
        _identifier(run_id)
        sources, request = self._request(sources, loops, rate, readiness_lag_s)
        request_digest = _digest(request)
        if not {"realtime:read", "realtime:ingest"} <= context.capabilities:
            raise AccessDenied("replay preparation requires host read and ingestion grants")
        async with self.store._transaction(context, "realtime:configure") as con:
            caps = await con.fetchval(
                "select realtime.actor_capabilities($1,$2)", context.company_id, context.principal_id
            )
            if "realtime:ingest" not in caps:
                raise AccessDenied("current membership does not permit replay ingestion")
            limits = await self.store._lock_budget(con, context)
            for source in sources:
                await self.store._source(con, context, source.source_id)
            prior = await con.fetchrow(
                "select * from realtime.replay_run where tenant_id=$1 and run_id=$2 for update",
                context.company_id,
                run_id,
            )
            if prior:
                if prior["request_digest"] != request_digest:
                    raise Conflict("a replay run retains its original source selection and clock settings")
                plan = restore_plan(prior["plan"])
            else:
                usage = await con.fetchrow("select * from realtime.replay_usage($1)", context.company_id)
                if usage["plan_count"] >= MAX_PLANS:
                    raise CapacityExceeded("retained replay plan allowance reached")
                records, receipts, read_sources = await self._load_inputs(con, context, sources, loops)
                plan = compile_replay(
                    company_id=context.company_id,
                    run_id=run_id,
                    sources=sources,
                    receipts=receipts,
                    records=records,
                    loops=loops,
                    rate=rate,
                    readiness_lag_s=readiness_lag_s,
                )
                payload, cursor = plan.to_dict(), initial_cursor(plan).to_dict()
                needed = await con.fetchval(
                    "select octet_length($1::jsonb::text)+octet_length($2::jsonb::text)+1024", payload, cursor
                )
                self.store._check_bytes(limits, await self.store._usage(con, context), needed)
                await con.execute(
                    "insert into realtime.replay_run(tenant_id,run_id,principal_id,"
                    "request_digest,plan_digest,"
                    "source_ids,plan,cursor,duration_s) values($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    context.company_id,
                    run_id,
                    context.principal_id,
                    request_digest,
                    plan.digest,
                    read_sources,
                    payload,
                    cursor,
                    plan.duration_s,
                )
        await self._prepare(context, run_id, plan)
        return await self.get(context, run_id)

    async def _prepare(self, context, run_id, plan):
        # This phase is resumable across separately committed SDK registrations.
        async with self.store._transaction(context, "realtime:configure") as con:
            row = await con.fetchrow(
                "select state from realtime.replay_run where tenant_id=$1 and run_id=$2",
                context.company_id,
                run_id,
            )
            if not row:
                raise AccessDenied("replay is unavailable under the current source grants")
            if row["state"] != "preparing":
                return
        copies = {}
        for entry in plan.events:
            source_id = entry.replay.original_event.source_id
            copied = copies.setdefault(
                source_id, {"target": entry.replay_source_id, "receipts": {}, "profiles": set()}
            )
            copied["profiles"].add(entry.canonical_event().provenance.profile_revision_id)
            for receipt_id, digest in zip(
                entry.replay.original_receipt_refs, entry.replay.receipt_sha256, strict=True
            ):
                copied["receipts"][receipt_id] = digest
        for source in plan.sources:
            if source.source_id not in copies:
                continue
            copied = copies[source.source_id]
            await self.store.register_source(
                context,
                copied["target"],
                kind="replay",
                metadata={
                    "replay_run_id": run_id,
                    "plan_digest": plan.digest,
                    "original_source_id": source.source_id,
                    "original_source_asset_id": source.source_asset_id,
                    "external_effects": "disabled",
                    "agent_wakes": "disabled",
                },
            )
            for profile_id in sorted(copied["profiles"]):
                async with self.store._transaction(context, "realtime:read") as con:
                    await self.store._source(con, context, source.source_id)
                    event_types = await con.fetchval(
                        "select event_types from realtime.profile_revision "
                        "where tenant_id=$1 and source_id=$2 and revision_id=$3",
                        context.company_id,
                        source.source_id,
                        profile_id,
                    )
                if not event_types:
                    raise Conflict("original profile revision is unavailable for replay preparation")
                await self.store.register_profile(context, copied["target"], profile_id, event_types)
            for receipt_id, expected_digest in sorted(copied["receipts"].items()):
                async with self.store._transaction(context, "realtime:read") as con:
                    await self.store._source(con, context, source.source_id)
                    row = await con.fetchrow(
                        "select body,digest from realtime.input_receipt "
                        "where tenant_id=$1 and source_id=$2 and receipt_id=$3",
                        context.company_id,
                        source.source_id,
                        receipt_id,
                    )
                if row is None or row["digest"] != expected_digest or _digest(row["body"]) != expected_digest:
                    raise Conflict("original receipt identity changed during replay preparation")
                await self.store.record_receipt(context, copied["target"], receipt_id, row["body"])
        async with self.store._transaction(context, "realtime:ingest") as con:
            row = await con.fetchrow(
                "select state,plan_digest from realtime.replay_run "
                "where tenant_id=$1 and run_id=$2 for update",
                context.company_id,
                run_id,
            )
            if not row or row["plan_digest"] != plan.digest:
                raise AccessDenied("replay source authorization or plan identity changed")
            if row["state"] == "preparing":
                await con.execute(
                    "update realtime.replay_run set state='running',clock_started_at=clock_timestamp(),"
                    "revision=revision+1,updated_at=clock_timestamp() where tenant_id=$1 and run_id=$2",
                    context.company_id,
                    run_id,
                )

    @staticmethod
    def _elapsed(row, now):
        elapsed = Decimal(row["elapsed_s"])
        if row["state"] == "running":
            elapsed += max(Decimal(0), Decimal(str((now - row["clock_started_at"]).total_seconds())))
        return min(elapsed, Decimal(row["duration_s"]))

    async def _row(self, con, context, run_id, *, lock=False, include_plan=False):
        columns = (
            "*"
            if include_plan
            else (
                "tenant_id,run_id,principal_id,plan_digest,state,elapsed_s,duration_s,clock_started_at,revision,cursor"
            )
        )
        row = await con.fetchrow(
            "select " + columns + ",clock_timestamp() as database_now from realtime.replay_run "
            "where tenant_id=$1 and run_id=$2" + (" for update" if lock else ""),
            context.company_id,
            run_id,
        )
        if not row:
            raise AccessDenied("replay is unavailable under the current principal and source grants")
        return row

    @staticmethod
    def _status(row):
        return {
            "schema": "rocoto.replay-status.v1",
            "run_id": row["run_id"],
            "plan_digest": row["plan_digest"],
            "state": row["state"],
            "revision": row["revision"],
            "duration_s": str(row["duration_s"]),
            "elapsed_s": str(ReplayStore._elapsed(row, row["database_now"])),
            "cursor": row["cursor"],
            "external_effects": "disabled",
            "agent_wakes": "disabled",
        }

    async def get(self, context, run_id):
        async with self.store._transaction(context, "realtime:read") as con:
            return self._status(await self._row(con, context, run_id))

    async def inspect_plan(self, context, run_id):
        async with self.store._transaction(context, "realtime:read") as con:
            row = await con.fetchrow(
                "select case when octet_length(plan::text)<=$3 then plan else null end as plan "
                "from realtime.replay_run where tenant_id=$1 and run_id=$2",
                context.company_id,
                run_id,
                context.bounds.max_output_bytes,
            )
            if row is None:
                raise AccessDenied("replay is unavailable under the current source grants")
            if row["plan"] is None:
                raise CapacityExceeded("compiled plan exceeds this response byte allowance")
            return restore_plan(row["plan"]).to_dict()

    async def tick(self, context, run_id, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= min(100, context.bounds.max_events):
            raise ReplayError("Request at most 100 events within the host tick allowance.")
        async with self.store._transaction(context, "realtime:ingest") as con:
            # Follow the same company-before-run lock order as start; retries and
            # concurrent ticks cannot deadlock against a new plan admission.
            await self.store._lock_budget(con, context)
            row = await self._row(con, context, run_id, lock=True, include_plan=True)
            if row["state"] != "running":
                return {**self._status(row), "admitted": 0}
            plan = restore_plan(row["plan"])
            cursor = ReplayCursor.from_dict(row["cursor"])
            elapsed = self._elapsed(row, row["database_now"])
            due = due_events(plan, cursor, elapsed, limit=limit)
            count, size = 0, 0
            for entry in due.events:
                wire_context = _wire(replay_context_dict(entry.replay))
                size += len(entry.event_json.encode()) + _bytes(wire_context)
                if size > min(MAX_BATCH_BYTES, context.bounds.max_output_bytes):
                    if count == 0:
                        raise CapacityExceeded("first replay event exceeds this tick's byte allowance")
                    break
                await self.store._source(con, context, entry.replay.original_event.source_id)
                result = await self.store._admit(
                    con, context, entry.replay_source_id, entry.canonical_event(), replay_context=wire_context
                )
                if result.status not in {"accepted", "duplicate"}:
                    raise Conflict("replay admission failed; the batch and cursor remain unchanged")
                await self.subjects._copy_replay_event(
                    con,
                    context,
                    entry.replay_source_id,
                    entry.canonical_event().event_id,
                    entry.canonical_event().event_revision,
                )
                count += 1
            if count:
                candidate = due_events(plan, cursor, elapsed, limit=count).candidate_cursor
                state = (
                    "complete"
                    if candidate.next_ordinal == len(plan.events) and elapsed >= plan.duration_s
                    else "running"
                )
                await con.execute(
                    "update realtime.replay_run set cursor=$3,state=$4,"
                    "elapsed_s=case when $4='complete' then duration_s else elapsed_s end,"
                    "clock_started_at=case when $4='complete' then null else clock_started_at end,"
                    "revision=revision+1,updated_at=clock_timestamp() where tenant_id=$1 and run_id=$2",
                    context.company_id,
                    run_id,
                    candidate.to_dict(),
                    state,
                )
            elif cursor.next_ordinal == len(plan.events) and elapsed >= plan.duration_s:
                await con.execute(
                    "update realtime.replay_run set state='complete',elapsed_s=duration_s,"
                    "clock_started_at=null,"
                    "revision=revision+1,updated_at=clock_timestamp() where tenant_id=$1 and run_id=$2",
                    context.company_id,
                    run_id,
                )
            current = await self._row(con, context, run_id)
            return {**self._status(current), "admitted": count}

    async def pause(self, context, run_id, *, expected_revision):
        return await self._set_paused(context, run_id, expected_revision, True)

    async def resume(self, context, run_id, *, expected_revision):
        return await self._set_paused(context, run_id, expected_revision, False)

    async def _set_paused(self, context, run_id, expected_revision, paused):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ReplayError("Use the current replay control revision.")
        async with self.store._transaction(context, "realtime:configure") as con:
            row = await self._row(con, context, run_id, lock=True)
            if row["revision"] != expected_revision:
                raise Conflict("replay controls changed; reload the current revision")
            desired = "paused" if paused else "running"
            if row["state"] not in {"paused", "running"} or row["state"] == desired:
                return self._status(row)
            elapsed = self._elapsed(row, row["database_now"])
            await con.execute(
                "update realtime.replay_run set state=$3,elapsed_s=$4,"
                "clock_started_at=case when $3='running' then clock_timestamp() else null end,"
                "revision=revision+1,updated_at=clock_timestamp() where tenant_id=$1 and run_id=$2",
                context.company_id,
                run_id,
                desired,
                elapsed,
            )
            return self._status(await self._row(con, context, run_id))
