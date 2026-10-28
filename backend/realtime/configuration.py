"""Typed Rocoto Real-time configuration; activation requires actual source evidence.

This service compiles business criteria and retains policy. It does not schedule
inference, execute scripts, wake agents or deliver notifications. The native
harness and external access channel will share these methods through host grants.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from realtime.contracts import AgentWakeMode, CanonicalEvent, SourceWindow
from realtime.store import (
    AccessDenied,
    CapacityExceeded,
    Conflict,
    RealTimeStore,
    _digest,
    _encoded,
    _identifier,
)

PLAYBOOK = json.loads((Path(__file__).parent / "playbooks/retail-v1.json").read_text())
PLAYBOOK_SHA256 = _digest(PLAYBOOK)
COMPILER_REVISION = "retail-configuration.v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Switches(Strict):
    observation_enabled: bool = True
    processing_enabled: bool = True
    agent_wake_mode: AgentWakeMode = "disabled"
    delivery_enabled: bool = False

    @model_validator(mode="after")
    def maintained_core(self):
        if not self.observation_enabled or not self.processing_enabled:
            raise ValueError("Core observation and processing remain enabled in configuration edits")
        return self


class Parameters(Strict):
    queue_zone_ids: tuple[str, ...] = Field(default=(), max_length=8)
    checkout_zone_ids: tuple[str, ...] = Field(default=(), max_length=8)
    shelf_zone_ids: tuple[str, ...] = Field(default=(), max_length=8)
    examination_minimum_s: Decimal = Field(default=Decimal(2), ge=0, le=120)
    queue_alert_after_s: Decimal = Field(default=Decimal(120), ge=1, le=3600)

    @field_validator("queue_zone_ids", "checkout_zone_ids", "shelf_zone_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(set(values)) != len(values) or any(not 1 <= len(v) <= 256 or "\x00" in v for v in values):
            raise ValueError("Use distinct bounded zone identifiers")
        return tuple(sorted(values))


class Interest(Strict):
    """A custom event criterion, not a prompt or executable processor."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    subject: Literal["person", "object", "scene"]
    starts_when: str = Field(min_length=1, max_length=500)
    ends_when: str | None = Field(default=None, max_length=500)

    @property
    def event_type(self):
        return "custom." + self.id


class Intent(Strict):
    mode: Literal["configure", "edit_only"] = "edit_only"
    expected_revision: int = Field(ge=1, le=2_147_483_646, strict=True)
    parameters: Parameters = Field(default_factory=Parameters)
    add_ons: tuple[Interest, ...] = Field(default=(), max_length=32)
    switches: Switches = Field(default_factory=Switches)

    @model_validator(mode="after")
    def unique_interests(self):
        if len({x.id for x in self.add_ons}) != len(self.add_ons):
            raise ValueError("Each Add-on has one distinct identifier")
        return self


class Acknowledgement(Strict):
    ack_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00]+$")
    revision: int = Field(ge=2, strict=True)
    profile_revision_id: str = Field(min_length=1, max_length=256)
    receipt_id: str = Field(min_length=1, max_length=256)


def _clock(body):
    continuity = body.get("continuity")
    if continuity:
        return "epoch", continuity["epoch_id"], SourceWindow.model_validate(continuity["stream_window"])
    return "asset", body["source_asset_id"], SourceWindow.model_validate(body["source_window"])


def _core_events():
    return sorted({name for core in PLAYBOOK["core"] for name in core["event_types"]})


async def _zone_available(con, context, source_id, zone_id, kind):
    from realtime.subjects import SubjectStore

    subjects = SubjectStore()
    try:
        zone = await subjects._read(con, context, zone_id)
        if (
            zone["source_id"] != source_id
            or zone["kind"] != "zone"
            or zone["current_status"] != "active"
            or zone["attributes"].get("zone_type") != kind
        ):
            return False
        camera = await subjects._read(con, context, zone["attributes"]["camera_subject_id"])
        return (
            camera["source_id"] == source_id
            and camera["kind"] == "camera"
            and camera["current_status"] == "active"
        )
    except AccessDenied:
        return False


def _support(parameters, *, actual_input, tracker=False):
    available = {"camera": actual_input, "person_tracking": tracker}
    available.update(
        {kind + "_zone": bool(parameters[kind + "_zone_ids"]) for kind in ("queue", "checkout", "shelf")}
    )
    return [
        {
            "id": c["id"],
            "status": "ready" if all(available[k] for k in c["required_inputs"]) else "pending",
            "missing_inputs": [k for k in c["required_inputs"] if not available[k]],
        }
        for c in PLAYBOOK["core"]
    ]


async def validate_admission(con, context, source_id, event) -> str | None:
    """Enforce actual acknowledged configuration only for managed observations.

    Source and receipt authorization/coverage are checked by RealTimeStore first.
    Epoch clocks and file clocks are deliberately incomparable. A straddling
    event requires the producer to split observations at the actual boundary.
    """
    head = await con.fetchval(
        "select revision from realtime.configuration_head where tenant_id=$1 and source_id=$2",
        context.company_id,
        source_id,
    )
    if head is None:
        return None
    # Corrections retain the authority under which this exact evidence was
    # admitted. Readiness changing later must not prevent a retraction. The
    # caller has already rechecked current source grants and actual receipts.
    if event.event_revision > 1:
        previous = await con.fetchval(
            "select envelope from realtime.event where tenant_id=$1 and source_id=$2 "
            "and event_id=$3 and event_revision=$4 and replay_context is null",
            context.company_id,
            source_id,
            event.event_id,
            event.event_revision - 1,
        )
        if previous:
            previous = CanonicalEvent.model_validate(previous)
            if previous.event_type == event.event_type and previous.provenance == event.provenance:
                return None
    p = event.provenance
    kind, clock_id, window = _clock(p.model_dump(mode="json"))
    active = await con.fetchrow(
        "select * from realtime.configuration_ack where tenant_id=$1 and source_id=$2 "
        "and clock_kind=$3 and clock_id=$4 and boundary_s<=$5 order by boundary_s desc limit 1",
        context.company_id,
        source_id,
        kind,
        clock_id,
        window.start_s,
    )
    if not active or active["profile_revision_id"] != p.profile_revision_id:
        return "configuration_not_acknowledged"
    boundary = await con.fetchval(
        "select min(boundary_s) from realtime.configuration_ack where tenant_id=$1 and source_id=$2 "
        "and clock_kind=$3 and clock_id=$4 and boundary_s>$5",
        context.company_id,
        source_id,
        kind,
        clock_id,
        window.start_s,
    )
    if boundary is not None and window.end_s > boundary:
        return "configuration_boundary_crossed"
    core = next((c for c in PLAYBOOK["core"] if event.event_type in c["event_types"]), None)
    if core:
        support = next(c for c in active["body"]["core_support"] if c["id"] == core["id"])
        if support["status"] != "ready":
            return "configuration_inputs_pending"
        for required in core["required_inputs"]:
            if required.endswith("_zone"):
                kind = required.removesuffix("_zone")
                for zone_id in active["body"]["parameters"][kind + "_zone_ids"]:
                    if not await _zone_available(con, context, source_id, zone_id, kind):
                        return "configuration_zone_unavailable"
    for receipt_id in p.actual_receipt_refs:
        receipt = await con.fetchval(
            "select body from realtime.input_receipt where tenant_id=$1 and source_id=$2 and receipt_id=$3",
            context.company_id,
            source_id,
            receipt_id,
        )
        if (
            receipt.get("configuration_revision") != active["revision"]
            or receipt.get("configuration_sha256") != active["body"]["configuration_sha256"]
        ):
            return "configuration_input_mismatch"
    return None


class ConfigurationService:
    def __init__(self, store=None):
        self.store = store or RealTimeStore()

    async def _head(self, con, context, source_id):
        return await con.fetchrow(
            "select * from realtime.configuration_head where tenant_id=$1 and source_id=$2",
            context.company_id,
            source_id,
        )

    async def _camera(self, con, context, source_id):
        _identifier(source_id)
        source = await self.store._source(con, context, source_id)
        if source["kind"] != "camera":
            raise Conflict("Configuration requires a camera source")
        return source

    async def _capacity(self, con, context, body, records=1):
        limits = await self.store._lock_budget(con, context)
        encoded = _encoded(body, min(131072, context.bounds.max_payload_bytes))
        usage = await self.store._usage(con, context)
        size = await con.fetchval("select octet_length($1::jsonb::text)", body)
        self.store._check_bytes(limits, usage, size + records * 2048)
        count = await con.fetchval(
            "select record_count from realtime.configuration_usage($1)", context.company_id
        )
        if count + records > 4096:
            raise CapacityExceeded("Company configuration history budget reached")
        return encoded

    async def _validate(self, con, context, source_id, intent):
        for kind in ("queue", "checkout", "shelf"):
            for zone_id in getattr(intent.parameters, kind + "_zone_ids"):
                if not await _zone_available(con, context, source_id, zone_id, kind):
                    raise Conflict("Configuration references an unavailable zone")
        return {
            "valid": True,
            "core_support": _support(intent.parameters.model_dump(mode="json"), actual_input=False),
        }

    def _compile(self, intent, validation):
        criteria = {
            "core": PLAYBOOK["core"],
            "parameters": intent.parameters.model_dump(mode="json"),
            "add_ons": [x.model_dump(mode="json") for x in sorted(intent.add_ons, key=lambda x: x.id)],
        }
        private = {
            "compiler_revision": COMPILER_REVISION,
            "playbook_sha256": PLAYBOOK_SHA256,
            "evidence_rules": PLAYBOOK["evidence_rules"],
            "business_criteria": criteria,
            "event_types": sorted(_core_events() + [x.event_type for x in intent.add_ons]),
        }
        digest = _digest(private)
        public = {
            "name": "Rocoto Real-time",
            "playbook_revision": PLAYBOOK["revision"],
            "core": criteria["core"],
            "add_ons": criteria["add_ons"],
            "parameters": criteria["parameters"],
            "switches": intent.switches.model_dump(mode="json"),
            "validation": validation,
        }
        return {
            "profile_revision_id": "cfg-" + digest,
            "configuration_sha256": digest,
            "private": private,
            "public": public,
        }

    async def requirements(self, context, source_id):
        async with self.store._transaction(context, "realtime:read") as con:
            await self._camera(con, context, source_id)
            head = await self._head(con, context, source_id)
            result = {
                "name": "Rocoto Real-time",
                "revision": head["revision"] if head else 1,
                "playbook_revision": PLAYBOOK["revision"],
                "core": PLAYBOOK["core"],
                "add_on_limit": 32,
                "parameter_limits": {"examination_minimum_s": [0, 120], "queue_alert_after_s": [1, 3600]},
                "desired": head["public_body"] if head else None,
            }
            return self._bounded(context, result)

    @staticmethod
    def _bounded(context, body):
        _encoded(body, context.bounds.max_output_bytes)
        return body

    async def propose(self, context, source_id, proposal_id, raw_intent):
        _identifier(proposal_id)
        intent = Intent.model_validate(raw_intent)
        request = intent.model_dump(mode="json")
        digest = _digest(request)
        async with self.store._transaction(context, "realtime:configure") as con:
            await self._camera(con, context, source_id)
            await self.store._lock_budget(con, context)
            prior = await con.fetchrow(
                "select * from realtime.configuration_proposal where tenant_id=$1 and source_id=$2 and "
                "proposal_id=$3",
                context.company_id,
                source_id,
                proposal_id,
            )
            if prior:
                if prior["digest"] != digest:
                    raise Conflict("Proposal identity already has different content")
                return self._bounded(context, prior["body"])
            head = await self._head(con, context, source_id)
            if intent.expected_revision != (head["revision"] if head else 1):
                raise Conflict("Configuration revision changed")
            validation = await self._validate(con, context, source_id, intent)
            body = {
                "proposal_id": proposal_id,
                "intent": request,
                "validation": validation,
                "playbook_revision": PLAYBOOK["revision"],
                "playbook_sha256": PLAYBOOK_SHA256,
            }
            await self._capacity(con, context, body)
            await con.execute(
                "insert into realtime.configuration_proposal(tenant_id,source_id,proposal_id,"
                "expected_revision,"
                "digest,body,conversation_id) values($1,$2,$3,$4,$5,$6,$7)",
                context.company_id,
                source_id,
                proposal_id,
                intent.expected_revision,
                digest,
                body,
                context.conversation_id,
            )
            return self._bounded(context, body)

    async def validate(self, context, source_id, proposal_id):
        async with self.store._transaction(context, "realtime:configure") as con:
            await self._camera(con, context, source_id)
            row = await self._proposal(con, context, source_id, proposal_id)
            head = await self._head(con, context, source_id)
            if row["expected_revision"] != (head["revision"] if head else 1):
                raise Conflict("Configuration revision changed")
            return await self._validate(con, context, source_id, Intent.model_validate(row["body"]["intent"]))

    async def _proposal(self, con, context, source_id, proposal_id):
        row = await con.fetchrow(
            "select * from realtime.configuration_proposal where tenant_id=$1 and source_id=$2 and "
            "proposal_id=$3",
            context.company_id,
            source_id,
            proposal_id,
        )
        if not row:
            raise AccessDenied("Proposal is unavailable in this grant")
        if row["body"]["playbook_sha256"] != PLAYBOOK_SHA256:
            raise Conflict("Refresh this proposal with the current maintained playbook")
        return row

    async def activate(self, context, source_id, proposal_id):
        """Set desired configuration atomically. There is deliberately no enqueue."""
        async with self.store._transaction(context, "realtime:configure") as con:
            await self._camera(con, context, source_id)
            await self.store._lock_budget(con, context)
            row = await self._proposal(con, context, source_id, proposal_id)
            prior = await con.fetchrow(
                "select revision,body from realtime.configuration_revision where tenant_id=$1 and "
                "source_id=$2 and proposal_id=$3",
                context.company_id,
                source_id,
                proposal_id,
            )
            if prior:
                return self._bounded(context, {"revision": prior["revision"], **prior["body"]["public"]})
            head = await self._head(con, context, source_id)
            if row["expected_revision"] != (head["revision"] if head else 1):
                raise Conflict("Configuration revision changed")
            intent = Intent.model_validate(row["body"]["intent"])
            validation = await self._validate(con, context, source_id, intent)
            compiled = self._compile(intent, validation)
            revision = row["expected_revision"] + 1
            public = {**compiled["public"], "desired_profile_id": compiled["profile_revision_id"]}
            body = {**compiled, "public": public, "intent": intent.model_dump(mode="json")}
            await self._capacity(con, context, {"revision": body, "head": public}, records=2)
            await self.store._register_profile(
                con, context, source_id, compiled["profile_revision_id"], compiled["private"]["event_types"]
            )
            # Recheck after profile bytes enter retained usage in this same transaction.
            await self._capacity(con, context, {"revision": body, "head": public}, records=2)
            await con.execute(
                "insert into realtime.configuration_revision(tenant_id,source_id,revision,proposal_id,"
                "profile_revision_id,digest,body,conversation_id) "
                "values($1,$2,$3,$4,$5,$6,$7,$8)",
                context.company_id,
                source_id,
                revision,
                proposal_id,
                compiled["profile_revision_id"],
                _digest(body),
                body,
                context.conversation_id,
            )
            await con.execute(
                "insert into realtime.configuration_head(tenant_id,source_id,revision,desired_profile_id,"
                "public_body) values($1,$2,$3,$4,$5) "
                "on conflict(tenant_id,source_id) do update set "
                "revision=excluded.revision,desired_profile_id=excluded.desired_profile_id,"
                "public_body=excluded.public_body",
                context.company_id,
                source_id,
                revision,
                compiled["profile_revision_id"],
                public,
            )
            return self._bounded(context, {"revision": revision, **public})

    async def _worker(self, con, context, source_id):
        await self._camera(con, context, source_id)
        if not await con.fetchval(
            "select realtime.configuration_worker($1,$2)", context.company_id, source_id
        ):
            raise AccessDenied("Activation requires a commissioned source service grant")

    async def worker_configuration(self, context, source_id):
        """Private adapter read. Never expose this shape through customer tools."""
        async with self.store._transaction(context, "realtime:acknowledge") as con:
            await self._worker(con, context, source_id)
            row = await con.fetchrow(
                "select r.revision,r.body from realtime.configuration_revision r join "
                "realtime.configuration_head h "
                "using(tenant_id,source_id,revision) where r.tenant_id=$1 and r.source_id=$2",
                context.company_id,
                source_id,
            )
            if not row:
                raise Conflict("Request configuration before preparing source input")
            body = row["body"]
            return self._bounded(
                context,
                {
                    "revision": row["revision"],
                    "profile_revision_id": body["profile_revision_id"],
                    "configuration_sha256": body["configuration_sha256"],
                    "compiled": body["private"],
                },
            )

    async def acknowledge(self, context, source_id, raw_ack):
        ack = Acknowledgement.model_validate(raw_ack)
        request = ack.model_dump(mode="json")
        async with self.store._transaction(context, "realtime:acknowledge") as con:
            await self._worker(con, context, source_id)
            await self.store._lock_budget(con, context)
            prior = await con.fetchrow(
                "select body from realtime.configuration_ack where tenant_id=$1 and source_id=$2 and "
                "ack_id=$3",
                context.company_id,
                source_id,
                ack.ack_id,
            )
            if prior:
                if prior["body"]["request"] != request:
                    raise Conflict("Acknowledgement identity already has different content")
                return self._bounded(context, prior["body"])
            head = await self._head(con, context, source_id)
            if (
                not head
                or head["revision"] != ack.revision
                or head["desired_profile_id"] != ack.profile_revision_id
            ):
                raise Conflict("Acknowledgement must match the desired configuration revision")
            compiled = await con.fetchval(
                "select body from realtime.configuration_revision where tenant_id=$1 and source_id=$2 "
                "and revision=$3",
                context.company_id,
                source_id,
                ack.revision,
            )
            receipt = await con.fetchrow(
                "select digest,body from realtime.input_receipt where tenant_id=$1 and source_id=$2 and "
                "receipt_id=$3",
                context.company_id,
                source_id,
                ack.receipt_id,
            )
            if not receipt:
                raise Conflict("Acknowledgement requires retained actual input evidence")
            b = receipt["body"]
            if (
                b.get("kind") != "actual_input"
                or b.get("profile_revision_id") != ack.profile_revision_id
                or b.get("configuration_revision") != ack.revision
                or b.get("configuration_sha256") != compiled["configuration_sha256"]
                or not re.fullmatch(r"[a-f0-9]{64}", str(b.get("source_sha256", "")))
            ):
                raise Conflict("Actual input evidence differs from desired configuration")
            from realtime.continuity import ContinuityStore

            await ContinuityStore(self.store).validate_input(con, context, source_id, b)
            kind, clock_id, window = _clock(b)
            last = await con.fetchval(
                "select max(boundary_s) from realtime.configuration_ack where tenant_id=$1 and "
                "source_id=$2 and clock_kind=$3 and clock_id=$4",
                context.company_id,
                source_id,
                kind,
                clock_id,
            )
            if last is not None and window.start_s <= last:
                raise Conflict("Acknowledgement must advance its exact source clock boundary")
            # Prevent a late acknowledgement rewriting already admitted history.
            later = await con.fetchval(
                "select 1 from realtime.event where tenant_id=$1 and source_id=$2 and replay_context is null "
                "and envelope->'provenance'->>'kind'='observation' and "
                "case when $3='epoch' then envelope->'provenance'->'continuity'->>'epoch_id'=$4 "
                "and (envelope->'provenance'->'continuity'->'stream_window'->>'end_s')::numeric>$5 "
                "else envelope->'provenance'->'continuity' is null "
                "and envelope->'provenance'->>'source_asset_id'=$4 "
                "and (envelope->'provenance'->'source_window'->>'end_s')::numeric>$5 end limit 1",
                context.company_id,
                source_id,
                kind,
                clock_id,
                window.start_s,
            )
            if later:
                raise Conflict("Acknowledge the boundary before admitting its observations")
            parameters = compiled["public"]["parameters"]
            await self._validate(con, context, source_id, Intent.model_validate(compiled["intent"]))
            body = {
                "parameters": parameters,
                "core_support": _support(
                    parameters,
                    actual_input=True,
                    tracker=bool(b.get("continuity") and b.get("tracker_snapshot")),
                ),
                "request": request,
                "receipt_sha256": receipt["digest"],
                "configuration_sha256": compiled["configuration_sha256"],
                "clock": {"kind": kind, "id": clock_id, "boundary_s": str(window.start_s)},
            }
            await self._capacity(con, context, body)
            await con.execute(
                "insert into realtime.configuration_ack(tenant_id,source_id,ack_id,revision,"
                "profile_revision_id,receipt_id,clock_kind,clock_id,boundary_s,digest,body,conversation_id) "
                "values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                context.company_id,
                source_id,
                ack.ack_id,
                ack.revision,
                ack.profile_revision_id,
                ack.receipt_id,
                kind,
                clock_id,
                window.start_s,
                _digest(body),
                body,
                context.conversation_id,
            )
            return self._bounded(context, body)

    async def status(self, context, source_id, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= min(100, context.bounds.max_events):
            raise ValueError("Use a bounded acknowledgement history limit")
        async with self.store._transaction(context, "realtime:read") as con:
            await self._camera(con, context, source_id)
            head = await self._head(con, context, source_id)
            acknowledgements = await con.fetch(
                "select revision,profile_revision_id,body from realtime.configuration_ack where "
                "tenant_id=$1 and source_id=$2 "
                "order by created_at desc,ack_id desc limit $3",
                context.company_id,
                source_id,
                limit,
            )
            result = {
                "revision": head["revision"] if head else 1,
                "desired": head["public_body"] if head else None,
                "acknowledgements": [
                    {
                        "revision": x["revision"],
                        "profile_revision_id": x["profile_revision_id"],
                        "clock": x["body"]["clock"],
                        "core_support": x["body"]["core_support"],
                    }
                    for x in acknowledgements
                ],
            }
            return self._bounded(context, result)
