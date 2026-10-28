"""A retail location's trackers, records, outputs, schedule and runs behind the company wall.

The authenticated host supplies every context. Each operation rechecks current membership as the
restricted database role, and row security reads a location together with its cameras. Processing
and ingestion also take the grant on each camera of the location. Tracker revisions and the output
ledger are append-only. The current output, the derived records and the newest runs are updated in
place, so the tables stay bounded and every row stays. A location has one holder at a time: the
pass that claimed it commits its records, outputs, cursor and finished work once, in a single
transaction, and an exact retry of that commit gets the first answer.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from realtime.contracts import HostExecutionContext
from realtime.store import AccessDenied, CapacityExceeded, Conflict, RealTimeStore, _encoded, _identifier

HOME_SCHEMA = "rocoto.location-home.v1"
PARTS = ("definition", "script", "presentation")
TRACKER_KINDS = ("core_tracker", "company_tracker", "tracking_agent")
TRACKER_STATES = ("active", "paused", "retired")
TRIGGERS = ("events", "day", "manual", "host")
FINISHED = ("completed", "failed", "budget_exceeded")
KEY = re.compile(r"[a-z][a-z0-9_-]{0,79}")
SHA256 = re.compile(r"[a-f0-9]{64}")

# What each record kind holds, by the basis that writes it. A row keeps the basis it was written with.
# The table check location_record_kind_basis of migration 0012 holds the same pairs.
RECORD_KINDS = {
    "observed_item": {"derived"},
    "basket": {"derived"},
    "shelf_state": {"derived"},
    "queue_interval": {"derived"},
    "page": {"derived"},
    "sale": {"derived", "ingested"},
    "sale_line": {"derived", "ingested"},
    "product": {"derived", "ingested", "configured"},
    "recording": {"ingested"},
    "event_evidence": {"ingested"},
    "item_grounding": {"ingested"},
    "basket_evidence": {"ingested"},
    "tracked_visit": {"ingested"},
    "zone": {"configured"},
}
# `put_records` writes these two bases; derived rows are a run's, written by `commit`.
RECORD_CAPABILITY = {"ingested": "realtime:ingest", "configured": "realtime:configure"}
# A write to a location takes its capability on the company and the matching grant on each camera.
CAMERA_GRANT = {
    "realtime:process": "Processing a location takes a process grant on each of its cameras",
    "realtime:ingest": "Ingesting for a location takes an ingest grant on each of its cameras",
    "realtime:configure": "Location is unavailable in this grant",
}

# The same bounds as the table checks of migration 0012, measured the way PostgreSQL prints jsonb.
REVISION_BYTES = 262144
TILE_BYTES = 262144
CANVAS_BYTES = 1048576
PERIODS_BYTES = 1048576
PAGE_BYTES = 1048576
RECORD_BYTES = 65536
INPUT_BYTES = 8192
SUMMARY_BYTES = 4096
POLICY_BYTES = 4096
SOURCE_BYTES = 4096
PERIOD_BYTES = 1024
RUN_OUTPUT_BYTES = 32768
RUN_ERROR_BYTES = 16384
RUN_SKIPPED_BYTES = 8192
RUN_DERIVED_BYTES = 4096
ERROR_CHARACTERS = 300
DOCUMENT_BYTES = 16 * 1024 * 1024

MAX_LOCATIONS = 64
MAX_LOCATION_SOURCES = 64
MAX_TRACKERS = 128
MAX_RECORDS = 100_000
MAX_RECORD_BATCH = 20_000
MAX_CLAIMED_EVENTS = 1000

# The run schedule follows the preview's runner: one holder per location, back-off after a failure.
KEEP_RUNS = 200
LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 3600
MANUAL_FLOOR_SECONDS = 5
FAILURE_CEILING_SECONDS = 3600
ATTENTION_AFTER = 5

HELD = "Another pass holds this location; this run's changes stay out"
LOCKED = "Core Trackers come with every location of this business type. Add your own tracker beside them."
INTERRUPTED = "The pass stopped before it finished. The next pass runs the same work again."
CURSOR = "The input cursor is an event sequence of this location"
FIRST_RUN = "Needs first run"


class CoreLocked(Conflict):
    """A Core Tracker follows its maintained recipe."""


class SchedulePolicy(BaseModel):
    """When a location's trackers run by themselves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    automatic: bool = Field(default=True, strict=True)
    min_interval_seconds: int = Field(default=60, ge=30, le=86400, strict=True)
    budget_seconds: int = Field(default=60, ge=5, le=180, strict=True)
    daily_at: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    paused: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("paused")
    @classmethod
    def _tracker_keys(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not KEY.fullmatch(key) for key in value):
            raise ValueError("List the paused trackers by their keys")
        return sorted(value)


def _size(value: Any) -> int:
    """UTF-8 bytes of a document as PostgreSQL prints jsonb.

    PostgreSQL puts a space after each colon and comma, as `json.dumps` does, and prints every
    number in full: 1e+300 takes 301 digits there. Text that holds NUL has no place in jsonb.
    """
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("document contains non-JSON data") from exc
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if "\x00" in item:
                raise ValueError("document text holds a NUL character")
        elif isinstance(item, float):
            printed = format(Decimal(repr(item)), "f")
            size += len(printed) - len(repr(item)) - (item == 0 and printed.startswith("-"))
        elif isinstance(item, dict):
            pending.extend(key for key in item if isinstance(key, str))
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return size


def _document(value: Any, limit: int, name: str) -> int:
    if not isinstance(value, dict):
        raise ValueError(f"{name} is an object")
    size = _size(value)
    if size > limit:
        raise CapacityExceeded(f"{name} exceeds its byte budget")
    return size


def _listing(value: Any, limit: int, name: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} is a list")
    if _size(list(value)) > limit:
        raise CapacityExceeded(f"{name} exceeds its byte budget")
    return list(value)


def _sha(value: Any) -> str:
    return hashlib.sha256(_encoded(value, DOCUMENT_BYTES)).hexdigest()


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    rounded = datetime.fromtimestamp(round(moment.timestamp(), 3), timezone.utc)
    return rounded.isoformat().replace("+00:00", "Z")


def _day(value: Any) -> date | None:
    if value is None or type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Give the local day as YYYY-MM-DD")
    return date.fromisoformat(value)


def _stamp(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or "\x00" in value:
        raise ValueError("The input stamp is a bounded opaque string")
    return value


def _keys(value: Any) -> list[str] | None:
    if not value:
        return None
    if (
        isinstance(value, str)
        or len(value) > 100
        or any(not isinstance(key, str) or not KEY.fullmatch(key) for key in value)
    ):
        raise ValueError("Name the trackers by their keys")
    return sorted(set(value))


def _moment(value: Any) -> str | None:
    """An ISO 8601 time with its offset, as a record's body carries it."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _run(row) -> dict[str, Any]:
    """A run as the tools and the page read it: no principal, no stamp, no lease.

    A run still stored as running whose lease ran out reads as interrupted from the lease's end.
    Its holder can still close it while every other pass stays away.
    """
    stalled = row["status"] == "running" and not row.get("held", True)
    return {
        "id": row["run_id"],
        "location_id": row["location_id"],
        "trigger": row["trigger"],
        "status": "interrupted" if stalled else row["status"],
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["lease_end"] if stalled else row["finished_at"]),
        "day": row["day"].isoformat() if row["day"] else None,
        "period": row["period"],
        "wall_ms": row["wall_ms"],
        "derived": row["derived"],
        "outputs": row["outputs"],
        "errors": row["errors"],
        "skipped": row["skipped"],
        "error": INTERRUPTED if stalled else row["error"],
        "page_published": row["published"],
        "only": row["requested_only"],
    }


def _tracker(row) -> dict[str, Any]:
    return {
        "tracker_id": row["tracker_id"],
        "location_id": row["location_id"],
        "key": row["key"],
        "core": row["core"],
        "kind": row["kind"],
        "chat_id": row["chat_id"],
        "state": row["state"],
        "head_revision": row["head_revision"],
        "definition_revision": row["definition_revision"],
        "script_revision": row["script_revision"],
        "presentation_revision": row["presentation_revision"],
        "definition": row["definition"],
        "script": row["script"],
        "presentation": row["presentation"],
    }


def _order(tracker: dict[str, Any]) -> tuple:
    order = tracker["definition"].get("order")
    return (not tracker["core"], order if type(order) is int else 0, tracker["key"])


def _flags(tracker: dict[str, Any]) -> dict[str, bool]:
    definition = tracker["definition"]
    chat, locked = definition.get("chat"), definition.get("locked")
    return {
        "core": tracker["core"],
        "chat": chat if type(chat) is bool else not tracker["core"],
        "locked": locked if type(locked) is bool else tracker["core"],
    }


_OUTPUT = ("tracker_id", "digest", "revision", "period", "tile", "canvas", "input", "summary")
_TRACKERS = (
    "select t.tracker_id,t.location_id,t.key,t.core,t.kind,t.chat_id,t.state,t.head_revision,"
    "t.definition_revision,t.script_revision,t.presentation_revision,"
    "d.body as definition,d.digest as definition_digest,s.body as script,p.body as presentation "
    "from realtime.location_tracker t "
    "join realtime.location_tracker_revision d on d.tenant_id=t.tenant_id and d.tracker_id=t.tracker_id "
    "and d.part='definition' and d.revision=t.definition_revision "
    "left join realtime.location_tracker_revision s on s.tenant_id=t.tenant_id and s.tracker_id=t.tracker_id "
    "and s.part='script' and s.revision=t.script_revision "
    "left join realtime.location_tracker_revision p on p.tenant_id=t.tenant_id and p.tracker_id=t.tracker_id "
    "and p.part='presentation' and p.revision=t.presentation_revision "
    "where t.tenant_id=$1 "
)
_SCHEDULE = (
    "select processed_stamp,processed_day,attempt_stamp,failures,"
    "coalesce(extract(epoch from next_after),0)::float8 as next_after,"
    "coalesce(extract(epoch from manual_after),0)::float8 as manual_after,"
    "extract(epoch from requested_at)::float8 as requested_at,requested_only,"
    "(lease_token is not null and lease_until>coalesce(to_timestamp($3::float8),now())) as running,"
    "last_run_id,case when last_status='running' and not(lease_token is not null "
    "and lease_until>coalesce(to_timestamp($3::float8),now())) then 'interrupted' else last_status end "
    "as last_status,policy,policy_revision "
    "from realtime.location where tenant_id=$1 and location_id=$2"
)
# A run with the lease it holds: `held` while the lease is the run's own and still runs.
_RUNS = (
    "select r.*,(l.lease_run_id=r.run_id and l.lease_until>now()) is true as held,"
    "case when l.lease_run_id=r.run_id then greatest(r.started_at,l.lease_until) end as lease_end "
    "from realtime.location_run r join realtime.location l "
    "on l.tenant_id=r.tenant_id and l.location_id=r.location_id where r.tenant_id=$1 "
)


class LocationStore:
    """Store of record for a location's trackers. Every method takes the host's verified context."""

    def __init__(self, store: RealTimeStore | None = None):
        self.store = store or RealTimeStore()

    # ── locations and their cameras ────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _tx(self, context: HostExecutionContext, capability: str):
        """One scoped transaction; a refusal of the tables arrives as the SDK's own error."""
        try:
            async with self.store._transaction(context, capability) as con:
                yield con
        except asyncpg.CheckViolationError as error:
            if str(error.constraint_name).endswith("_bytes"):
                raise CapacityExceeded("document exceeds its byte budget") from error
            raise
        except asyncpg.DataError as error:
            raise ValueError("document holds a value outside what PostgreSQL stores") from error

    async def _location(self, con, context, location_id, *, lock=False, reach=None):
        """The location row behind the wall. `reach` names the capability a write takes on each camera."""
        _identifier(location_id)
        read = "select * from realtime.location where tenant_id=$1 and location_id=$2"
        locked = " for update" if lock else ""
        row = await con.fetchrow(read + ("" if reach else locked), context.company_id, location_id)
        if not row:
            raise AccessDenied("Location is unavailable in this grant")
        if reach:
            if not await con.fetchval(
                "select true from realtime.location_reach($3) r where r.tenant_id=$1 and r.location_id=$2",
                context.company_id,
                location_id,
                reach,
            ):
                raise AccessDenied(CAMERA_GRANT[reach])
            if lock:
                row = await con.fetchrow(read + locked, context.company_id, location_id)
                if not row:
                    raise AccessDenied("Location is unavailable in this grant")
        return row

    async def _budget(self, con, context, limits, added: int) -> None:
        usage = await self.store._usage(con, context)
        self.store._check_bytes(limits, usage, added)

    @staticmethod
    async def _bump(con, context, location_id) -> None:
        """A tracker change moves what a commit expects and what the page shows."""
        await con.execute(
            "update realtime.location set run_seq=run_seq+1,page_seq=page_seq+1,updated_at=now() "
            "where tenant_id=$1 and location_id=$2",
            context.company_id,
            location_id,
        )

    @staticmethod
    def _identity(row) -> dict[str, Any]:
        return {
            "location_id": row["location_id"],
            "collection": row["collection"],
            "business_type": row["business_type"],
            "run_seq": row["run_seq"],
            "through_sequence": row["through_sequence"],
        }

    async def register_location(
        self,
        context: HostExecutionContext,
        location_id: str,
        *,
        collection: str,
        business_type: str = "retail",
    ) -> dict[str, Any]:
        """Give a location its PostgreSQL identity. An exact retry returns the same row."""
        _identifier(location_id)
        if not isinstance(collection, str) or not 1 <= len(collection) <= 64 or "\x00" in collection:
            raise ValueError("A location belongs to one bounded collection name")
        if business_type != "retail":
            raise ValueError("Choose a business type with a maintained tracker recipe")
        async with self._tx(context, "realtime:configure") as con:
            limits = await self.store._lock_budget(con, context)
            held = await con.fetchval(
                "select location_count from realtime.location_usage($1)", context.company_id
            )
            made = await con.execute(
                "insert into realtime.location(tenant_id,location_id,collection,business_type) "
                "values($1,$2,$3,$4) on conflict do nothing",
                context.company_id,
                location_id,
                collection,
                business_type,
            )
            row = await self._location(con, context, location_id)
            if (row["collection"], row["business_type"]) != (collection, business_type):
                raise Conflict("Location identity already has a different collection or business type")
            if made == "INSERT 0 1":
                # The new row is counted now; a refusal here rolls it back.
                if held >= MAX_LOCATIONS:
                    raise CapacityExceeded("Company location budget reached")
                await self._budget(con, context, limits, 0)
            return self._identity(row)

    async def bind_source(
        self,
        context: HostExecutionContext,
        location_id: str,
        source_id: str,
        *,
        camera_id: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind one camera source to its location. A camera belongs to one location."""
        _identifier(source_id)
        _identifier(camera_id)
        body = {} if body is None else body
        size = _document(body, SOURCE_BYTES, "camera binding")
        async with self._tx(context, "realtime:configure") as con:
            limits = await self.store._lock_budget(con, context)
            await self._location(con, context, location_id)
            source = await self.store._source(con, context, source_id)
            if source["kind"] != "camera":
                raise Conflict("A location holds camera sources")
            read = (
                "select location_id,source_id,camera_id,body from realtime.location_source "
                "where tenant_id=$1 and source_id=$2"
            )
            bound = await con.fetchrow(read, context.company_id, source_id)
            if not bound:
                await self._budget(con, context, limits, size + 1024)
                held = await con.fetchval(
                    "select count(*) from realtime.location_source where tenant_id=$1 and location_id=$2",
                    context.company_id,
                    location_id,
                )
                if held >= MAX_LOCATION_SOURCES:
                    raise CapacityExceeded("Location camera budget reached")
                await con.execute(
                    "insert into realtime.location_source(tenant_id,location_id,source_id,camera_id,body) "
                    "values($1,$2,$3,$4,$5) on conflict do nothing",
                    context.company_id,
                    location_id,
                    source_id,
                    camera_id,
                    body,
                )
                bound = await con.fetchrow(read, context.company_id, source_id)
            if (
                not bound
                or bound["location_id"] != location_id
                or bound["camera_id"] != camera_id
                or bound["body"] != body
            ):
                raise Conflict("Camera source already has a different location binding")
            return dict(bound)

    async def locations(self, context: HostExecutionContext) -> list[dict[str, Any]]:
        """The company's locations this principal reads, with how many trackers each holds."""
        async with self._tx(context, "realtime:read") as con:
            rows = await con.fetch(
                "select l.location_id,l.collection,l.business_type,l.run_seq,l.through_sequence,"
                "(select count(*) from realtime.location_tracker t where t.tenant_id=l.tenant_id "
                "and t.location_id=l.location_id and t.state<>'retired') as tracker_count "
                "from realtime.location l where l.tenant_id=$1 order by l.location_id limit $2",
                context.company_id,
                MAX_LOCATIONS,
            )
            return [self._identity(row) | {"tracker_count": row["tracker_count"]} for row in rows]

    # ── trackers and their revisions ───────────────────────────────────────────────────────────

    @staticmethod
    def _definition(definition) -> dict[str, Any]:
        if not isinstance(definition, dict):
            raise ValueError("A tracker definition is an object")
        tracker_id, key, core = definition.get("id"), definition.get("key"), definition.get("core")
        _identifier(tracker_id)
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("A tracker key starts with a letter and uses lowercase letters, digits, _ and -")
        if type(core) is not bool:
            raise ValueError("A tracker definition states core as true or false")
        agent = definition.get("agent")
        named = agent.get("kind") if isinstance(agent, dict) else None
        kind = named or ("core_tracker" if core else "company_tracker")
        if kind not in TRACKER_KINDS or core != (kind == "core_tracker"):
            raise ValueError(
                "A Core Tracker carries the kind core_tracker; a company's tracker carries its own"
            )
        chat_id = definition.get("chat_id")
        if core and (chat_id is not None or definition.get("chat") is True):
            raise CoreLocked(LOCKED)
        if chat_id is not None:
            _identifier(chat_id)
        return {
            "tracker_id": tracker_id,
            "key": key,
            "core": core,
            "kind": kind,
            "chat_id": chat_id,
            "body": definition,
            "size": _document(definition, REVISION_BYTES, "tracker definition"),
            "digest": _sha(definition),
        }

    async def _trackers(self, con, context, location_id, include_retired) -> list[dict[str, Any]]:
        rows = await con.fetch(
            _TRACKERS + "and t.location_id=$2 and ($3 or t.state<>'retired')",
            context.company_id,
            location_id,
            include_retired,
        )
        return sorted((_tracker(row) for row in rows), key=_order)

    async def _one_tracker(self, con, context, tracker_id):
        _identifier(tracker_id)
        row = await con.fetchrow(_TRACKERS + "and t.tracker_id=$2", context.company_id, tracker_id)
        if not row:
            raise AccessDenied("Tracker is unavailable in this grant")
        return row

    async def install_trackers(
        self, context: HostExecutionContext, location_id: str, definitions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Record tracker definitions for a location. An exact retry returns the same rows.

        A Core Tracker follows its maintained recipe: a changed Core definition becomes its next
        definition revision and its head. A company's tracker changes through `save_revision`
        and `activate`.
        """
        if not isinstance(definitions, (list, tuple)) or not 1 <= len(definitions) <= MAX_TRACKERS:
            raise ValueError("Install a bounded list of tracker definitions")
        wanted = [self._definition(definition) for definition in definitions]
        if len({w["tracker_id"] for w in wanted}) != len(wanted) or len({w["key"] for w in wanted}) != len(
            wanted
        ):
            raise ValueError("Each tracker has its own identifier and key")
        async with self._tx(context, "realtime:configure") as con:
            limits = await self.store._lock_budget(con, context)
            await self._location(con, context, location_id, lock=True)
            held = {
                row["tracker_id"]: row
                for row in await con.fetch(
                    _TRACKERS + "and t.tracker_id=any($2::text[])",
                    context.company_id,
                    [w["tracker_id"] for w in wanted],
                )
            }
            keys = {
                row["key"]: row["tracker_id"]
                for row in await con.fetch(
                    "select key,tracker_id from realtime.location_tracker "
                    "where tenant_id=$1 and location_id=$2",
                    context.company_id,
                    location_id,
                )
            }
            fresh, recipe = [], []
            for item in wanted:
                row = held.get(item["tracker_id"])
                if row is None:
                    if keys.get(item["key"], item["tracker_id"]) != item["tracker_id"]:
                        raise Conflict("Choose a new tracker name for this location")
                    fresh.append(item)
                elif (row["location_id"], row["key"], row["core"], row["kind"], row["chat_id"]) != (
                    location_id,
                    item["key"],
                    item["core"],
                    item["kind"],
                    item["chat_id"],
                ):
                    raise Conflict("Tracker identity is immutable")
                elif row["definition_digest"] != item["digest"]:
                    if not row["core"]:
                        raise Conflict("Save a revision and activate it to change this tracker")
                    recipe.append(item)
            if len(keys) + len(fresh) > MAX_TRACKERS:
                raise CapacityExceeded("Location tracker budget reached")
            if fresh or recipe:
                await self._budget(con, context, limits, sum(w["size"] + 4096 for w in fresh + recipe))
            for item in fresh:
                made = await con.execute(
                    "insert into realtime.location_tracker(tenant_id,tracker_id,location_id,key,core,kind,"
                    "chat_id,definition_revision) values($1,$2,$3,$4,$5,$6,$7,1) on conflict do nothing",
                    context.company_id,
                    item["tracker_id"],
                    location_id,
                    item["key"],
                    item["core"],
                    item["kind"],
                    item["chat_id"],
                )
                if made != "INSERT 0 1":
                    raise Conflict("Tracker identity is immutable")
                await self._revision(con, context, location_id, item, "definition", 1)
            for item in recipe:
                revision = 1 + await con.fetchval(
                    "select max(revision) from realtime.location_tracker_revision where tenant_id=$1 "
                    "and tracker_id=$2 and part='definition'",
                    context.company_id,
                    item["tracker_id"],
                )
                await self._revision(con, context, location_id, item, "definition", revision)
                await con.execute(
                    "update realtime.location_tracker set definition_revision=$3,"
                    "head_revision=head_revision+1,updated_at=now() where tenant_id=$1 and tracker_id=$2",
                    context.company_id,
                    item["tracker_id"],
                    revision,
                )
            if fresh or recipe:
                await self._bump(con, context, location_id)
            return await self._trackers(con, context, location_id, False)

    @staticmethod
    async def _revision(con, context, location_id, item, part, revision) -> None:
        await con.execute(
            "insert into realtime.location_tracker_revision(tenant_id,tracker_id,location_id,core,part,"
            "revision,digest,body,conversation_id) values($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            context.company_id,
            item["tracker_id"],
            location_id,
            item["core"],
            part,
            revision,
            item["digest"],
            item["body"],
            context.conversation_id,
        )

    async def save_revision(
        self,
        context: HostExecutionContext,
        tracker_id: str,
        part: str,
        body: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Append the next revision of one part of a company's tracker.

        `expected_revision` is the newest revision of that part the caller read, 0 for a first one.
        The saved revision takes effect when `activate` names it.
        """
        if part not in PARTS:
            raise ValueError("A tracker has a definition, a script and a presentation")
        if type(expected_revision) is not int or not 0 <= expected_revision < 100000:
            raise ValueError("Expected revision is a bounded integer")
        size, digest = _document(body, REVISION_BYTES, "tracker " + part), _sha(body)
        async with self._tx(context, "realtime:configure") as con:
            limits = await self.store._lock_budget(con, context)
            tracker = await self._one_tracker(con, context, tracker_id)
            if tracker["core"]:
                raise CoreLocked(LOCKED)
            if part == "definition" and (
                body.get("id", tracker_id) != tracker_id
                or body.get("key", tracker["key"]) != tracker["key"]
                or body.get("core", False) is not False
            ):
                raise Conflict("Tracker identity is immutable")
            revision = expected_revision + 1
            saved = {
                row["revision"]: row["digest"]
                for row in await con.fetch(
                    "select revision,digest from realtime.location_tracker_revision where tenant_id=$1 "
                    "and tracker_id=$2 and part=$3 order by revision desc limit 2",
                    context.company_id,
                    tracker_id,
                    part,
                )
            }
            result = {"tracker_id": tracker_id, "part": part, "revision": revision, "digest": digest}
            if revision in saved:
                if saved[revision] != digest:
                    raise Conflict("Tracker revision changed")
                return result | {"created": False}
            if max(saved, default=0) != expected_revision:
                raise Conflict("Tracker revision changed")
            await self._budget(con, context, limits, size + 1024)
            item = {"tracker_id": tracker_id, "core": False, "digest": digest, "body": body}
            await self._revision(con, context, tracker["location_id"], item, part, revision)
            return result | {"created": True}

    async def activate(
        self,
        context: HostExecutionContext,
        tracker_id: str,
        *,
        expected_head: int,
        definition: int | None = None,
        script: int | None = None,
        presentation: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Point a company's tracker at saved revisions or change its state, as one head move.

        `expected_head` is the head revision the caller read; a head that moved since is refused.
        """
        chosen = {"definition": definition, "script": script, "presentation": presentation}
        if type(expected_head) is not int or expected_head < 1:
            raise ValueError("Expected head is the head revision the caller read")
        if any(value is not None and (type(value) is not int or value < 1) for value in chosen.values()):
            raise ValueError("Name each part by a saved revision number")
        if state is not None and state not in TRACKER_STATES:
            raise ValueError("A tracker is active, paused or retired")
        if state is None and all(value is None for value in chosen.values()):
            raise ValueError("Name a saved revision or a state to activate")
        async with self._tx(context, "realtime:configure") as con:
            await self.store._lock_budget(con, context)
            tracker = await self._one_tracker(con, context, tracker_id)
            if tracker["core"]:
                raise CoreLocked(LOCKED)
            if tracker["head_revision"] != expected_head:
                raise Conflict("Tracker head changed")
            for part, revision in chosen.items():
                if revision is not None and not await con.fetchval(
                    "select 1 from realtime.location_tracker_revision where tenant_id=$1 and tracker_id=$2 "
                    "and part=$3 and revision=$4",
                    context.company_id,
                    tracker_id,
                    part,
                    revision,
                ):
                    raise Conflict("Activate a saved revision of this tracker")
            moved = await con.execute(
                "update realtime.location_tracker set definition_revision=coalesce($4,definition_revision),"
                "script_revision=coalesce($5,script_revision),"
                "presentation_revision=coalesce($6,presentation_revision),state=coalesce($7,state),"
                "head_revision=head_revision+1,updated_at=now() "
                "where tenant_id=$1 and tracker_id=$2 and head_revision=$3",
                context.company_id,
                tracker_id,
                expected_head,
                definition,
                script,
                presentation,
                state,
            )
            if moved != "UPDATE 1":
                raise Conflict("Tracker head changed")
            await self._bump(con, context, tracker["location_id"])
            return _tracker(await self._one_tracker(con, context, tracker_id))

    async def trackers(
        self, context: HostExecutionContext, location_id: str, *, include_retired: bool = False
    ) -> list[dict[str, Any]]:
        """The location's trackers with their active revision bodies: Core first, then by key."""
        async with self._tx(context, "realtime:read") as con:
            await self._location(con, context, location_id)
            result = await self._trackers(con, context, location_id, bool(include_retired))
            _encoded(result, context.bounds.max_output_bytes)
            return result

    # ── records ────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _records(kind, rows, basis, occurred_key=None, source_key=None) -> list[dict[str, Any]]:
        if kind not in RECORD_KINDS or basis not in RECORD_KINDS[kind]:
            raise ValueError("Choose a record kind and the basis that writes it")
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_RECORD_BATCH:
            raise ValueError("Write a bounded list of records")
        limit = PAGE_BYTES if kind == "page" else RECORD_BYTES
        prepared, seen = [], set()
        for body in rows:
            size = _document(body, limit, "record")
            record_id = _identifier(body.get("id"))
            if record_id in seen:
                raise ValueError("Each record has its own identifier")
            seen.add(record_id)
            source_id = body.get(source_key) if source_key else None
            prepared.append(
                {
                    "id": record_id,
                    "digest": _sha(body),
                    "body": body,
                    "size": size,
                    "occurred_at": _moment(body.get(occurred_key)) if occurred_key else None,
                    "source_id": source_id
                    if isinstance(source_id, str) and 1 <= len(source_id) <= 256
                    else None,
                }
            )
        return prepared

    async def _write_records(self, con, context, limits, location_id, kind, basis, prepared, replace_all):
        """Insert new rows, rewrite changed ones, and turn the rows that left a rebuild inactive."""
        ids = [row["id"] for row in prepared]
        known = {
            row["record_id"]: row
            for row in await con.fetch(
                "select record_id,basis,digest,active from realtime.location_record "
                "where tenant_id=$1 and location_id=$2 and kind=$3 and record_id=any($4::text[])",
                context.company_id,
                location_id,
                kind,
                ids,
            )
        }
        if any(row["basis"] != basis for row in known.values()):
            raise Conflict("A record keeps the basis it was written with")
        fresh = [row for row in prepared if row["id"] not in known]
        moved = [
            row
            for row in prepared
            if row["id"] in known
            and (known[row["id"]]["digest"] != row["digest"] or not known[row["id"]]["active"])
        ]
        written = 0
        if fresh or moved:
            await self._budget(con, context, limits, sum(row["size"] + 1024 for row in fresh + moved))
            held = await con.fetchval(
                "select record_count from realtime.location_usage($1)", context.company_id
            )
            if held + len(fresh) > MAX_RECORDS:
                raise CapacityExceeded("Company location record budget reached")
            rows = await con.fetch(
                "insert into realtime.location_record as r(tenant_id,location_id,kind,record_id,basis,"
                "source_id,occurred_at,digest,body) "
                "select $1::text,$2::text,$3::text,x->>'id',$4::text,x->>'source_id',"
                "(x->>'occurred_at')::timestamptz,"
                "x->>'digest',x->'body' from jsonb_array_elements($5::jsonb) as x "
                "on conflict(tenant_id,location_id,kind,record_id) do update set "
                "source_id=excluded.source_id,occurred_at=excluded.occurred_at,digest=excluded.digest,"
                "body=excluded.body,active=true,updated_at=now(),"
                "principal_id=current_setting('rocoto.principal',true),"
                "request_id=current_setting('rocoto.request_id',true) "
                "where r.basis=excluded.basis returning r.record_id",
                context.company_id,
                location_id,
                kind,
                basis,
                [
                    {k: row[k] for k in ("id", "digest", "body", "occurred_at", "source_id")}
                    for row in fresh + moved
                ],
            )
            if len(rows) != len(fresh) + len(moved):
                raise Conflict("A record keeps the basis it was written with")
            written = len(rows)
        if replace_all:
            left = await con.execute(
                "update realtime.location_record set active=false,updated_at=now() where tenant_id=$1 "
                "and location_id=$2 and kind=$3 and basis=$4 and active and not(record_id=any($5::text[]))",
                context.company_id,
                location_id,
                kind,
                basis,
                ids,
            )
            written += int(left.rsplit(" ", 1)[1])
        return written

    async def put_records(
        self,
        context: HostExecutionContext,
        location_id: str,
        kind: str,
        rows: list[dict[str, Any]],
        *,
        basis: str,
        replace_all: bool = False,
        occurred_key: str | None = None,
        source_key: str | None = None,
    ) -> int:
        """Write ingested or configured records of one kind; returns how many rows changed.

        Each row is its body with an `id`, its own within the location. An unchanged row stays as it
        is. `replace_all` turns the location's other rows of that kind and basis inactive.
        `occurred_key` and `source_key` name the body fields that carry the row's time and source.
        Ingested rows take an ingest grant on each camera of the location. Derived rows are a run's:
        `commit` writes them.
        """
        if basis not in RECORD_CAPABILITY:
            raise ValueError("Choose a record kind and the basis that writes it")
        prepared = self._records(kind, rows, basis, occurred_key, source_key)
        async with self._tx(context, RECORD_CAPABILITY[basis]) as con:
            limits = await self.store._lock_budget(con, context)
            await self._location(con, context, location_id, reach=RECORD_CAPABILITY[basis])
            return await self._write_records(
                con, context, limits, location_id, kind, basis, prepared, bool(replace_all)
            )

    # ── one run: read a snapshot, commit its changes ───────────────────────────────────────────

    async def load(self, context: HostExecutionContext, location_id: str) -> dict[str, Any]:
        """Everything one tracker run reads, bounded by the context's event and byte limits.

        The snapshot names `run_seq` (what `commit` expects; it moves with every commit and every
        tracker change) and `events_through` (the highest event sequence it read, the run's next
        `through_sequence`).
        """
        budget = context.bounds.max_output_bytes
        async with self._tx(context, "realtime:read") as con:
            # The location row is read first: a commit or a tracker change after this point moves
            # run_seq, and this run's commit is refused.
            location = await self._location(con, context, location_id)
            sources = await con.fetch(
                "select source_id,camera_id,body from realtime.location_source "
                "where tenant_id=$1 and location_id=$2 order by source_id",
                context.company_id,
                location_id,
            )
            trackers = await self._trackers(con, context, location_id, False)
            used = _size(trackers)
            outputs = {}
            for row in await con.fetch(
                "select tracker_id,revision,digest,period,tile,canvas,input,"
                "octet_length(tile::text)+octet_length(canvas::text) as bytes "
                "from realtime.location_tracker_output where tenant_id=$1 and location_id=$2",
                context.company_id,
                location_id,
            ):
                used += row["bytes"]
                outputs[row["tracker_id"]] = {
                    k: row[k] for k in ("revision", "digest", "period", "tile", "canvas", "input")
                }
            through = await con.fetchval(
                "select coalesce(max(e.sequence),0) from realtime.event e join realtime.location_source s "
                "on s.tenant_id=e.tenant_id and s.source_id=e.source_id "
                "where e.tenant_id=$1 and s.location_id=$2 and e.replay_context is null",
                context.company_id,
                location_id,
            )
            events: list[dict[str, Any]] = []
            async for row in con.cursor(
                "with latest as (select e.sequence,e.source_id,e.event_id,e.event_revision,e.envelope,"
                "row_number() over(partition by e.event_id order by e.event_revision desc) as revision_rank "
                "from realtime.event e join realtime.location_source s "
                "on s.tenant_id=e.tenant_id and s.source_id=e.source_id "
                "where e.tenant_id=$1 and s.location_id=$2 and e.replay_context is null and e.sequence<=$3) "
                "select l.sequence,l.source_id,l.envelope,octet_length(l.envelope::text) as bytes,"
                "a.subject_id,a.attributes,a.status from latest l left join lateral ("
                "select b.subject_id,r.attributes,(select c.status from realtime.subject_revision c "
                "where c.tenant_id=b.tenant_id and c.subject_id=b.subject_id "
                "order by c.revision desc limit 1) as status "
                "from realtime.event_subject_binding b join realtime.subject_revision r "
                "on r.tenant_id=b.tenant_id and r.subject_id=b.subject_id and r.revision=b.subject_revision "
                "where b.tenant_id=$1 and b.event_id=l.event_id and b.event_revision=l.event_revision "
                "and b.role='actor' order by b.subject_id limit 1) a on true "
                "where l.revision_rank=1 and coalesce(l.envelope->>'event_state','active')='active' "
                "order by l.sequence limit $4",
                context.company_id,
                location_id,
                through,
                context.bounds.max_events + 1,
                prefetch=64,
            ):
                used += row["bytes"]
                if len(events) >= context.bounds.max_events or used > budget:
                    raise CapacityExceeded("Location history exceeds this run's event or byte budget")
                actor = None
                if row["subject_id"] is not None:
                    actor = {
                        "subject_id": row["subject_id"],
                        "attributes": row["attributes"],
                        "status": row["status"],
                    }
                events.append(
                    {
                        "sequence": row["sequence"],
                        "source_id": row["source_id"],
                        "envelope": row["envelope"],
                        "actor": actor,
                    }
                )
            records: dict[str, list] = {}
            async for row in con.cursor(
                "select kind,body,octet_length(body::text) as bytes from realtime.location_record "
                "where tenant_id=$1 and location_id=$2 and active order by kind,record_id",
                context.company_id,
                location_id,
                prefetch=256,
            ):
                used += row["bytes"]
                if used > budget:
                    raise CapacityExceeded("Location records exceed this run's byte budget")
                records.setdefault(row["kind"], []).append(row["body"])
            return self._identity(location) | {
                "events_through": through,
                "sources": [
                    {
                        "source_id": row["source_id"],
                        "camera_id": row["camera_id"],
                        "camera_name": row["body"].get("camera_name"),
                        "room_ids": row["body"].get("room_ids") or [],
                    }
                    for row in sources
                ],
                "trackers": trackers,
                "outputs": outputs,
                "events": events,
                "records": records,
            }

    @staticmethod
    def _output(value) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) - {
            "tracker_id",
            "digest",
            "revision",
            "period",
            "tile",
            "canvas",
            "input",
            "summary",
        }:
            raise ValueError("An output names its tracker, digest, period, tile, canvas and input")
        tracker_id, digest, revision = value.get("tracker_id"), value.get("digest"), value.get("revision")
        _identifier(tracker_id)
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("An output digest is a SHA-256 of its tile and canvas")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("An output revision is a positive integer")
        tile: Any = value.get("tile")
        canvas, period, given = value.get("canvas"), value.get("period"), value.get("input")
        given = {} if given is None else given
        size = (
            _document(tile, TILE_BYTES, "tile")
            + _document(canvas, CANVAS_BYTES, "canvas")
            + _document(given, INPUT_BYTES, "output input")
            + _document(period, PERIOD_BYTES, "output period")
        )
        summary = value.get("summary")
        if summary is None:
            summary = {
                k: tile[k]
                for k in ("key", "label", "value", "unit", "state", "needs", "summary")
                if k in tile
            }
        size += _document(summary, SUMMARY_BYTES, "output summary")
        return {
            "tracker_id": tracker_id,
            "digest": digest,
            "revision": revision,
            "period": period,
            "tile": tile,
            "canvas": canvas,
            "input": given,
            "summary": summary,
            "size": size + 2048,
        }

    async def commit(
        self,
        context: HostExecutionContext,
        location_id: str,
        changes: dict[str, Any],
        *,
        run_id: str,
        lease_token: str,
        expected_run_seq: int,
    ) -> dict[str, Any]:
        """Commit one run in one transaction: all of it lands, or the location stays as it was.

        `run_id` and `lease_token` are the claim's: the pass that holds the location commits, and
        every other pass gets a `Conflict`. `expected_run_seq` is the snapshot's `run_seq`; a commit
        or a tracker change since that read is a `Conflict`. A run commits once, and an exact retry
        of its commit returns the first answer.

        `changes` holds `records` ({kind: {"rows", "replace_all", "occurred_key", "source_key"}}, written
        as derived), `outputs` (each with `tracker_id`, `digest`, `period`, `tile`, `canvas`, `input`),
        `periods` ({tracker_id: readings}), `through_sequence` (an event sequence of this location) and
        `outbox` (rows from `claim_events`). An output whose digest equals the current one stays as it
        is; a new digest updates the current output in place and adds one ledger row. The answer
        names the new `run_seq`, the page's `revision` and `changed`: whether the page shows
        something new.
        """
        _identifier(run_id)
        if not isinstance(lease_token, str):
            raise ValueError("Give the lease token the claim returned")
        if not isinstance(changes, dict) or set(changes) - {
            "records",
            "outputs",
            "periods",
            "through_sequence",
            "outbox",
        }:
            raise ValueError("Commit records, outputs, periods, through_sequence and outbox")
        if type(expected_run_seq) is not int or expected_run_seq < 0:
            raise ValueError("Expected run sequence is the value the run read")
        records = {}
        for kind, spec in (changes.get("records") or {}).items():
            if not isinstance(spec, dict) or set(spec) - {
                "rows",
                "replace_all",
                "occurred_key",
                "source_key",
            }:
                raise ValueError("Each record kind carries rows and replace_all")
            records[kind] = (
                self._records(
                    kind, spec.get("rows") or [], "derived", spec.get("occurred_key"), spec.get("source_key")
                ),
                bool(spec.get("replace_all")),
            )
        outputs = [self._output(value) for value in changes.get("outputs") or ()]
        if len({output["tracker_id"] for output in outputs}) != len(outputs) or len(outputs) > MAX_TRACKERS:
            raise ValueError("A run publishes one output per tracker")
        periods = changes.get("periods") or {}
        if (
            not isinstance(periods, dict)
            or len(periods) > MAX_TRACKERS
            or any(not isinstance(tracker_id, str) for tracker_id in periods)
        ):
            raise ValueError("Period readings are keyed by tracker")
        period_bytes = sum(_document(value, PERIODS_BYTES, "period readings") for value in periods.values())
        through = changes.get("through_sequence", 0)
        if type(through) is not int or not 0 <= through < 2**63:
            raise ValueError(CURSOR)
        outbox = changes.get("outbox") or []
        if (
            not isinstance(outbox, (list, tuple))
            or len(outbox) > MAX_CLAIMED_EVENTS
            or any(
                not isinstance(row, dict)
                or type(row.get("outbox_id")) is not int
                or not isinstance(row.get("lease_token"), str)
                for row in outbox
            )
        ):
            raise ValueError("Finish the outbox rows this run claimed")
        # What this run commits, as one digest: an exact retry is recognised by it.
        content = hashlib.sha256()
        parts: list[Any] = [
            [
                kind,
                replace_all,
                [[row[key] for key in ("id", "digest", "occurred_at", "source_id")] for row in rows],
            ]
            for kind, (rows, replace_all) in sorted(records.items())
        ]
        parts += [[output[key] for key in _OUTPUT] for output in outputs]
        parts += [list(entry) for entry in sorted(periods.items())]
        parts.append([through, expected_run_seq, [[row["outbox_id"], row["lease_token"]] for row in outbox]])
        for part in parts:
            content.update(_encoded(part, 2**62) + b"\n")
        digest = content.hexdigest()
        async with self._tx(context, "realtime:process") as con:
            limits = await self.store._lock_budget(con, context)
            location = await self._location(con, context, location_id, lock=True, reach="realtime:process")
            # The fence: the location's lease is this run's own. An expired lease that every other
            # pass left alone still is.
            run = await con.fetchrow(
                "select commit_digest,committed from realtime.location_run where tenant_id=$1 "
                "and location_id=$2 and run_id=$3 and status='running'",
                context.company_id,
                location_id,
                run_id,
            )
            if not run or location["lease_token"] != lease_token or location["lease_run_id"] != run_id:
                raise Conflict(HELD)
            if run["commit_digest"] is not None:
                if run["commit_digest"] != digest:
                    raise Conflict("A run commits once; its first commit stands")
                return run["committed"]
            if location["run_seq"] != expected_run_seq:
                raise Conflict("Location changed since this run read it")
            if through > await con.fetchval(
                "select coalesce(max(e.sequence),0) from realtime.event e join realtime.location_source s "
                "on s.tenant_id=e.tenant_id and s.source_id=e.source_id "
                "where e.tenant_id=$1 and s.location_id=$2 and e.replay_context is null",
                context.company_id,
                location_id,
            ):
                raise ValueError(CURSOR)
            changed, written = False, {}
            for kind, (prepared, replace_all) in records.items():
                written[kind] = await self._write_records(
                    con, context, limits, location_id, kind, "derived", prepared, replace_all
                )
                changed = changed or written[kind] > 0
            keys = {
                row["tracker_id"]: row["key"]
                for row in await con.fetch(
                    "select tracker_id,key from realtime.location_tracker "
                    "where tenant_id=$1 and location_id=$2",
                    context.company_id,
                    location_id,
                )
            }
            current = {
                row["tracker_id"]: row
                for row in await con.fetch(
                    "select tracker_id,revision,digest from realtime.location_tracker_output "
                    "where tenant_id=$1 and location_id=$2",
                    context.company_id,
                    location_id,
                )
            }
            if outputs or periods:
                await self._budget(con, context, limits, sum(o["size"] for o in outputs) + period_bytes)
            published = []
            for output in outputs:
                tracker_id, prior = output["tracker_id"], current.get(output["tracker_id"])
                if tracker_id not in keys:
                    raise Conflict("An output belongs to a tracker of this location")
                revision = prior["revision"] if prior else 0
                moved = prior is None or prior["digest"] != output["digest"]
                if moved:
                    revision += 1
                    if output["revision"] not in (None, revision):
                        raise Conflict("An output revision follows the current one")
                    document = (output["digest"], output["period"], output["tile"], output["canvas"])
                    await con.execute(
                        "insert into realtime.location_tracker_output(tenant_id,tracker_id,location_id,"
                        "revision,digest,period,tile,canvas,input) values($1,$2,$3,$4,$5,$6,$7,$8,$9) "
                        "on conflict(tenant_id,location_id,tracker_id) do update set "
                        "revision=excluded.revision,digest=excluded.digest,period=excluded.period,"
                        "tile=excluded.tile,canvas=excluded.canvas,input=excluded.input,updated_at=now(),"
                        "principal_id=current_setting('rocoto.principal',true),"
                        "request_id=current_setting('rocoto.request_id',true)",
                        context.company_id,
                        tracker_id,
                        location_id,
                        revision,
                        *document,
                        output["input"],
                    )
                    await con.execute(
                        "insert into realtime.location_tracker_output_revision(tenant_id,tracker_id,"
                        "location_id,revision,digest,period,summary,input) values($1,$2,$3,$4,$5,$6,$7,$8)",
                        context.company_id,
                        tracker_id,
                        location_id,
                        revision,
                        output["digest"],
                        output["period"],
                        output["summary"],
                        output["input"],
                    )
                    current[tracker_id] = {"revision": revision, "digest": output["digest"]}
                    changed = True
                published.append(
                    {
                        "tracker_id": tracker_id,
                        "key": keys[tracker_id],
                        "revision": revision,
                        "digest": output["digest"],
                        "changed": moved,
                    }
                )
            for tracker_id, readings in periods.items():
                if tracker_id not in current:
                    raise Conflict("Period readings belong to a published output")
                moved = await con.execute(
                    "update realtime.location_tracker_output set periods=$4,updated_at=now() "
                    "where tenant_id=$1 and location_id=$2 and tracker_id=$3 and periods<>$4::jsonb",
                    context.company_id,
                    location_id,
                    tracker_id,
                    readings,
                )
                changed = changed or moved == "UPDATE 1"
            # run_seq moves with every commit, so a snapshot read before this one commits nothing;
            # the page's revision moves when the page shows something new.
            row = await con.fetchrow(
                "update realtime.location set run_seq=run_seq+1,page_seq=page_seq+$3,"
                "through_sequence=greatest(through_sequence,$4),updated_at=now() "
                "where tenant_id=$1 and location_id=$2 returning run_seq,page_seq,through_sequence",
                context.company_id,
                location_id,
                1 if changed else 0,
                through,
            )
            if outbox:
                # Last on purpose: a lease that changed hands rolls the whole run back.
                finished = await con.fetch(
                    "update realtime.outbox o set state='complete',completed_at=now(),lease_token=null,"
                    "lease_until=null from unnest($3::bigint[],$4::text[]) as c(outbox_id,lease_token),"
                    "realtime.location_source s where o.tenant_id=$1 and o.outbox_id=c.outbox_id "
                    "and o.lease_token=c.lease_token and o.state='leased' and o.lease_until>now() "
                    "and o.lane='live' and s.tenant_id=o.tenant_id and s.source_id=o.source_id "
                    "and s.location_id=$2 returning o.outbox_id",
                    context.company_id,
                    location_id,
                    [entry["outbox_id"] for entry in outbox],
                    [entry["lease_token"] for entry in outbox],
                )
                if len(finished) != len(outbox):
                    raise Conflict("Claimed events changed hands; the next run reads them again")
            answer = {
                "location_id": location_id,
                "run_seq": row["run_seq"],
                "revision": str(row["page_seq"]),
                "through_sequence": row["through_sequence"],
                "changed": changed,
                "records": written,
                "outputs": published,
                "finished": len(outbox),
            }
            await con.execute(
                "update realtime.location_run set commit_digest=$4,committed=$5 "
                "where tenant_id=$1 and location_id=$2 and run_id=$3",
                context.company_id,
                location_id,
                run_id,
                digest,
                answer,
            )
            return answer

    async def home(
        self, context: HostExecutionContext, location_id: str, *, known_revision: str | int | None = None
    ) -> dict[str, Any]:
        """The page keys of a location with their revision. No event is read.

        The revision moves when a commit or a tracker change altered what the page shows. A caller
        that names the revision it holds, as `home` returned it, gets `{"revision", "unchanged": True}`
        while the location stays at that revision; any other value gets the whole page.
        """
        known = None
        if type(known_revision) is int:
            known = known_revision if 0 <= known_revision < 2**63 else None
        elif isinstance(known_revision, str) and re.fullmatch(r"0|[1-9][0-9]{0,17}", known_revision):
            known = int(known_revision)
        async with self._tx(context, "realtime:read") as con:
            # One statement reads the revision with the trackers and outputs it names.
            rows = await con.fetch(
                "select l.page_seq,t.tracker_id,t.key,t.core,d.body as definition,"
                "(o.tracker_id is not null) as published,"
                "case when $3::bigint is distinct from l.page_seq then o.tile end as tile,"
                "case when $3::bigint is distinct from l.page_seq then o.canvas end as canvas,"
                "case when $3::bigint is distinct from l.page_seq then o.periods end as periods "
                "from realtime.location l left join realtime.location_tracker t "
                "on t.tenant_id=l.tenant_id and t.location_id=l.location_id and t.state<>'retired' "
                "left join realtime.location_tracker_revision d on d.tenant_id=t.tenant_id "
                "and d.tracker_id=t.tracker_id and d.part='definition' and d.revision=t.definition_revision "
                "left join realtime.location_tracker_output o on o.tenant_id=t.tenant_id "
                "and o.location_id=t.location_id and o.tracker_id=t.tracker_id "
                "where l.tenant_id=$1 and l.location_id=$2",
                context.company_id,
                _identifier(location_id),
                known,
            )
            if not rows:
                raise AccessDenied("Location is unavailable in this grant")
            revision = str(rows[0]["page_seq"])
            if known == rows[0]["page_seq"]:
                return {"schema": HOME_SCHEMA, "revision": revision, "unchanged": True}
            page = await con.fetchval(
                "select body from realtime.location_record where tenant_id=$1 and location_id=$2 "
                "and kind='page' and basis='derived' and active order by record_id limit 1",
                context.company_id,
                location_id,
            )
        tiles, canvases = [], {}
        for row in sorted((row for row in rows if row["tracker_id"] is not None), key=_order):
            flags = _flags(row)
            if not row["published"]:
                tiles.append(
                    {
                        "key": row["key"],
                        "label": row["definition"].get("label"),
                        "value": None,
                        "unit": None,
                        "state": "needs",
                        "needs": FIRST_RUN,
                        "series": [],
                        "events": [],
                        "period": None,
                        "tracker_id": row["tracker_id"],
                        **flags,
                    }
                )
                continue
            tiles.append({**row["tile"], **flags, "periods": row["periods"]})
            canvases[row["key"]] = {**row["canvas"], **flags}
        page = page or {}
        result = {
            "schema": HOME_SCHEMA,
            "revision": revision,
            "location": {"id": location_id},
            "currency": page.get("currency"),
            "core_trackers": tiles,
            "core_tracker_canvases": canvases,
            "products": page.get("products") or [],
        }
        _encoded(result, context.bounds.max_output_bytes)
        return result

    async def claim_events(
        self,
        context: HostExecutionContext,
        *,
        location_id: str | None = None,
        limit: int = 100,
        lease_seconds: int = 120,
    ) -> dict[str, list[dict[str, Any]]]:
        """Lease pending live outbox rows of location cameras, grouped by location.

        `commit` finishes the rows a run hands back. A lease that expires returns its row to the
        next claim; five expired attempts leave a retained failed job, as in `claim_outbox`.
        """
        if location_id is not None:
            _identifier(location_id)
        if type(limit) is not int or not 1 <= limit <= min(MAX_CLAIMED_EVENTS, context.bounds.max_events):
            raise ValueError("lease bounds exceeded")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease bounds exceeded")
        async with self._tx(context, "realtime:process") as con:
            await con.execute(
                "update realtime.outbox set state='failed',lease_token=null,lease_until=null "
                "where tenant_id=$1 and state='leased' and lease_until<=now() and attempts>=5 "
                "and realtime.source_processable(tenant_id,source_id)",
                context.company_id,
            )
            due = await con.fetch(
                "select o.outbox_id,s.location_id from realtime.outbox o join realtime.location_source s "
                "on s.tenant_id=o.tenant_id and s.source_id=o.source_id "
                "where o.tenant_id=$1 and o.attempts<5 and o.lane='live' "
                "and ($3::text is null or s.location_id=$3) "
                "and realtime.source_processable(o.tenant_id,o.source_id) "
                "and ((o.state='pending' and o.available_at<=now()) "
                "or (o.state='leased' and o.lease_until<=now())) "
                "order by o.outbox_id for update of o skip locked limit $2",
                context.company_id,
                limit,
                location_id,
            )
            place = {row["outbox_id"]: row["location_id"] for row in due}
            leased = await con.fetch(
                "update realtime.outbox set state='leased',attempts=attempts+1,lease_token=$3,"
                "lease_until=now()+$4*interval '1 second' where tenant_id=$1 and outbox_id=any($2::bigint[]) "
                "returning outbox_id,source_id,event_id,event_revision,attempts,lease_token,lease_until",
                context.company_id,
                list(place),
                str(uuid.uuid4()),
                lease_seconds,
            )
            claimed: dict[str, list[dict[str, Any]]] = {}
            for row in sorted(leased, key=lambda row: row["outbox_id"]):
                claimed.setdefault(place[row["outbox_id"]], []).append(dict(row))
            return claimed

    async def input_stamp(self, context: HostExecutionContext, location_id: str) -> str:
        """A digest of what a run of this location reads. It moves when an input is added or changed.

        Inputs are the live events of the location's cameras with their subject bindings, the ingested
        and configured records, the cameras and the tracker heads. What a run writes stays out. The
        database answers with counts and high-water marks in one call, so a probe can ask often.
        Every write of a record draws a higher mark from one sequence, so the stamp moves with each
        committed write in whatever order the writers began and committed.
        """
        _identifier(location_id)
        async with self._tx(context, "realtime:read") as con:
            marks = await con.fetchval(
                "select realtime.location_inputs($1,$2)", context.company_id, location_id
            )
            if marks is None:
                raise AccessDenied("Location is unavailable in this grant")
            return _sha(marks)

    # ── schedule and runs ──────────────────────────────────────────────────────────────────────

    async def _state(self, con, context, location_id, now=None) -> dict[str, Any]:
        row = await con.fetchrow(_SCHEDULE, context.company_id, _identifier(location_id), now)
        if not row:
            raise AccessDenied("Location is unavailable in this grant")
        policy = SchedulePolicy.model_validate(row["policy"]).model_dump(mode="json")
        return {
            "processed_stamp": row["processed_stamp"],
            "processed_day": row["processed_day"].isoformat() if row["processed_day"] else None,
            "attempt_stamp": row["attempt_stamp"],
            "failures": row["failures"],
            "needs_attention": row["failures"] >= ATTENTION_AFTER,
            "next_after": row["next_after"],
            "manual_after": row["manual_after"],
            "requested_at": row["requested_at"],
            "requested_only": row["requested_only"],
            "running": row["running"],
            "last_run_id": row["last_run_id"],
            "last_status": row["last_status"],
            "policy": policy,
            "policy_revision": row["policy_revision"],
        }

    async def state(
        self, context: HostExecutionContext, location_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """The location's schedule as the probe and the tools read it. Times are epoch seconds."""
        if now is not None and type(now) not in (int, float):
            raise ValueError("Give the time as epoch seconds")
        async with self._tx(context, "realtime:read") as con:
            return await self._state(con, context, location_id, now)

    async def policy(self, context: HostExecutionContext, location_id: str) -> dict[str, Any]:
        """The location's run policy with its revision."""
        state = await self.state(context, location_id)
        return {**state["policy"], "revision": state["policy_revision"]}

    async def set_policy(
        self,
        context: HostExecutionContext,
        location_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Change the location's run policy. `expected_revision` is the revision the caller read."""
        if not isinstance(changes, dict):
            raise ValueError("Give the schedule fields to change")
        async with self._tx(context, "realtime:configure") as con:
            row = await self._location(con, context, location_id, lock=True)
            if type(expected_revision) is not int or expected_revision != row["policy_revision"]:
                raise Conflict("Read the schedule again, then change it with its current revision.")
            policy = SchedulePolicy.model_validate({**row["policy"], **changes}).model_dump(mode="json")
            _document(policy, POLICY_BYTES, "schedule policy")
            await con.execute(
                "update realtime.location set policy=$3,policy_revision=policy_revision+1,updated_at=now() "
                "where tenant_id=$1 and location_id=$2",
                context.company_id,
                location_id,
                policy,
            )
            return {**policy, "revision": expected_revision + 1}

    async def request(
        self, context: HostExecutionContext, location_id: str, *, by: str, only: list[str] | None = None
    ) -> dict[str, Any]:
        """Ask for a pass now. Requests made before the pass starts become that one pass."""
        _identifier(by)
        chosen = _keys(only)
        async with self._tx(context, "realtime:process") as con:
            await self._location(con, context, location_id, lock=True, reach="realtime:process")
            await con.execute(
                "update realtime.location set requested_at=now(),requested_by=$3,requested_only=$4,"
                "updated_at=now() where tenant_id=$1 and location_id=$2",
                context.company_id,
                location_id,
                by,
                chosen,
            )
            return await self._state(con, context, location_id)

    async def claim(
        self,
        context: HostExecutionContext,
        location_id: str,
        *,
        trigger: str,
        stamp: str,
        day: str | date | None,
        lease_seconds: int = LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Take the location's lease and open a run. None while another pass holds the lease.

        The lease runs on database time. A pass that stopped mid-way frees its location when its
        lease expires: its run closes as interrupted at the lease's end and this pass does the work
        again. The run takes the place of the location's 200th-newest one. The claim's `id` and
        `lease_token` are what `commit` and `finish` take.
        """
        if trigger not in TRIGGERS:
            raise ValueError("Use a known run trigger")
        stamp, local_day = _stamp(stamp), _day(day)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError("lease bounds exceeded")
        async with self._tx(context, "realtime:process") as con:
            await self._location(con, context, location_id, reach="realtime:process")
            row = await con.fetchrow(
                "select lease_token,lease_run_id,lease_until,(lease_until>now()) as held,requested_by,"
                "requested_only,run_count,extract(epoch from now())::float8 as started "
                "from realtime.location where tenant_id=$1 and location_id=$2 for update",
                context.company_id,
                location_id,
            )
            if not row:
                raise AccessDenied("Location is unavailable in this grant")
            if row["lease_token"] and row["held"]:
                return None
            if row["lease_token"]:
                await con.execute(
                    "update realtime.location_run set status='interrupted',"
                    "finished_at=greatest(started_at,$5),error=$4 "
                    "where tenant_id=$1 and location_id=$2 and run_id=$3 and status='running'",
                    context.company_id,
                    location_id,
                    row["lease_run_id"],
                    INTERRUPTED,
                    row["lease_until"],
                )
            manual = trigger == "manual"
            run_id, token, ordinal = "trun-" + uuid.uuid4().hex, secrets.token_hex(16), row["run_count"] + 1
            only = row["requested_only"] if manual else None
            await con.execute(
                "insert into realtime.location_run as r(tenant_id,location_id,slot,ordinal,run_id,trigger,"
                "status,input_stamp,day,requested_by,requested_only) "
                "values($1,$2,$3,$4,$5,$6,'running',$7,$8,$9,$10) "
                "on conflict(tenant_id,location_id,slot) do update set ordinal=excluded.ordinal,"
                "run_id=excluded.run_id,trigger=excluded.trigger,status='running',started_at=now(),"
                "finished_at=null,input_stamp=excluded.input_stamp,day=excluded.day,wall_ms=null,period=null,"
                "derived=null,outputs='[]'::jsonb,errors='[]'::jsonb,skipped='[]'::jsonb,error=null,"
                "published=false,requested_by=excluded.requested_by,requested_only=excluded.requested_only,"
                "commit_digest=null,committed=null,principal_id=current_setting('rocoto.principal',true),"
                "request_id=current_setting('rocoto.request_id',true)",
                context.company_id,
                location_id,
                (ordinal - 1) % KEEP_RUNS,
                ordinal,
                run_id,
                trigger,
                stamp,
                local_day,
                row["requested_by"] if manual else None,
                only,
            )
            await con.execute(
                "update realtime.location set lease_token=$3,lease_until=now()+$4*interval '1 second',"
                "lease_run_id=$5,last_run_id=$5,last_status='running',run_count=$6,"
                "requested_at=case when $7 then null else requested_at end,"
                "requested_by=case when $7 then null else requested_by end,"
                "requested_only=case when $7 then null else requested_only end,updated_at=now() "
                "where tenant_id=$1 and location_id=$2",
                context.company_id,
                location_id,
                token,
                lease_seconds,
                run_id,
                ordinal,
                manual,
            )
            return {
                "id": run_id,
                "lease_token": token,
                "trigger": trigger,
                "started_at": row["started"],
                "stamp": stamp,
                "day": local_day.isoformat() if local_day else None,
                "only": only,
            }

    async def finish(
        self,
        context: HostExecutionContext,
        run_id: str,
        lease_token: str,
        *,
        status: str,
        stamp: str | None = None,
        day: str | date | None = None,
        period: dict[str, Any] | None = None,
        derived: dict[str, Any] | None = None,
        outputs=(),
        errors=(),
        skipped=(),
        wall_ms: int = 0,
        error: str | None = None,
        published: bool = False,
    ) -> dict[str, Any]:
        """Close a run and free its lease; the holder's token is the key.

        A completed pass that names its `stamp` records that stamp and `day` as processed. A failed
        pass backs off and leaves what was processed as it is. With another token the run and the
        lease stay as they are and the run is returned as it stands.
        """
        if status not in FINISHED:
            raise ValueError("Use a known run status")
        _identifier(run_id)
        if not isinstance(lease_token, str):
            raise ValueError("Give the lease token the claim returned")
        stamp, local_day = (None if stamp is None else _stamp(stamp)), _day(day)
        if type(wall_ms) is not int or not 0 <= wall_ms <= 86400000:
            raise ValueError("Give the wall time in milliseconds")
        if period is not None:
            _document(period, PERIOD_BYTES, "run period")
        if derived is not None:
            _document(derived, RUN_DERIVED_BYTES, "run derived counts")
        outputs = _listing(outputs, RUN_OUTPUT_BYTES, "run outputs")
        skipped = _listing(skipped, RUN_SKIPPED_BYTES, "run skipped trackers")
        errors = [
            {**entry, "error": str(entry.get("error", ""))[:ERROR_CHARACTERS]}
            if isinstance(entry, dict)
            else entry
            for entry in errors
        ]
        errors = _listing(errors, RUN_ERROR_BYTES, "run errors")
        error = None if error is None else str(error)[:ERROR_CHARACTERS]
        read = _RUNS + "and r.run_id=$2"
        async with self._tx(context, "realtime:process") as con:
            run = await con.fetchrow(read, context.company_id, run_id)
            if not run:
                raise Conflict("Choose a run of this location.")
            row = await self._location(con, context, run["location_id"], lock=True, reach="realtime:process")
            run = await con.fetchrow(read, context.company_id, run_id)
            if (
                not run
                or run["status"] != "running"
                or row["lease_token"] != lease_token
                or row["lease_run_id"] != run_id
            ):
                # The lease moved on; the pass that holds it owns the location's state.
                if not run:
                    raise Conflict("Choose a run of this location.")
                return _run(run)
            run = await con.fetchrow(
                "update realtime.location_run set status=$3,finished_at=now(),period=$4,wall_ms=$5,"
                "derived=$6,outputs=$7,errors=$8,skipped=$9,error=$10,published=$11 "
                "where tenant_id=$1 and run_id=$2 returning *",
                context.company_id,
                run_id,
                status,
                period,
                wall_ms,
                derived,
                outputs,
                errors,
                skipped,
                error,
                bool(published),
            )
            policy = SchedulePolicy.model_validate(row["policy"])
            wall = wall_ms / 1000
            completed = status == "completed" and stamp is not None
            failed = status != "completed" and run["trigger"] != "host"
            failures = min(row["failures"] + 1, 1000000) if failed else row["failures"]
            if completed:
                wait = max(policy.min_interval_seconds, 10 * wall)
            else:
                wait = min(FAILURE_CEILING_SECONDS, 60 * 2 ** min(failures, 10))
            await con.execute(
                "update realtime.location set lease_token=null,lease_until=null,lease_run_id=null,"
                "last_status=$3,manual_after=now()+$4*interval '1 second',"
                "processed_stamp=case when $5 then $6 else processed_stamp end,"
                "processed_day=case when $5 then coalesce($7,processed_day) else processed_day end,"
                "attempt_stamp=case when $5 then null when $8 then $9 else attempt_stamp end,"
                "failures=case when $5 then 0 else $10 end,"
                "next_after=case when $5 or $8 then now()+$11*interval '1 second' else next_after end,"
                "updated_at=now() where tenant_id=$1 and location_id=$2",
                context.company_id,
                run["location_id"],
                status,
                float(max(MANUAL_FLOOR_SECONDS, 10 * wall)),
                completed,
                stamp,
                local_day,
                failed,
                stamp or run["input_stamp"],
                failures,
                float(wait),
            )
            return _run(run)

    async def runs(
        self, context: HostExecutionContext, location_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """The location's runs, newest first. The newest 200 are kept."""
        if type(limit) is not int or not 1 <= limit <= KEEP_RUNS:
            raise ValueError("Choose a bounded number of runs")
        async with self._tx(context, "realtime:read") as con:
            await self._location(con, context, location_id)
            rows = await con.fetch(
                _RUNS + "and r.location_id=$2 order by r.ordinal desc limit $3",
                context.company_id,
                location_id,
                limit,
            )
            return [_run(row) for row in rows]

    async def last_run(
        self, context: HostExecutionContext, location_id: str | None = None
    ) -> dict[str, Any] | None:
        """The newest run of one location, or of the company's locations this principal reads."""
        if location_id is not None:
            _identifier(location_id)
        async with self._tx(context, "realtime:read") as con:
            row = await con.fetchrow(
                _RUNS + "and ($2::text is null or r.location_id=$2) "
                "order by r.started_at desc,r.ordinal desc limit 1",
                context.company_id,
                location_id,
            )
            return _run(row) if row else None
