"""Pure, bounded replay compiler for previously admitted observation records.

The host must read inputs/receipts through its authorized store. This module does
not authenticate records, admit events, sleep, invoke models or deliver effects.
Replay sidecars and the acknowledgement cursor must be persisted by the host
alongside ordinary admission/outbox work before this building block is wired.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from realtime.contracts import CanonicalEvent

MAX_EVENTS = 10_000
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_LOOPS = 10
MAX_DURATION_S = Decimal(3600)


class ReplayError(ValueError):
    pass


def _seconds(value, *, positive=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (positive and result == 0):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ReplayError("Use finite, nonnegative replay seconds.") from None


def _id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise ReplayError("Preserve a bounded opaque identifier.")
    return value


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=lambda v: str(v) if isinstance(v, Decimal) else _unsupported(v),
    )


def _unsupported(value):
    raise TypeError(type(value).__name__)


def _key(prefix, *values):
    return prefix + hashlib.sha256(_json(values).encode()).hexdigest()


@dataclass(frozen=True)
class ReplaySource:
    source_id: str
    source_asset_id: str
    start_s: Decimal
    end_s: Decimal
    offset_s: Decimal = Decimal(0)  # Simulation seconds; never inferred from unknown capture UTC.


@dataclass(frozen=True)
class ReplayReceipt:
    receipt_id: str
    source_id: str
    source_asset_id: str
    profile_revision_id: str
    start_s: Decimal
    end_s: Decimal
    sha256: str


@dataclass(frozen=True)
class EventReference:
    source_id: str
    event_id: str
    event_revision: int


@dataclass(frozen=True)
class EntityReference:
    kind: str
    namespace: str
    entity_id: str


@dataclass(frozen=True)
class AdmittedReplayEvent:
    source_id: str
    sequence: int
    event: CanonicalEvent
    entities: tuple[EntityReference, ...] = ()
    supersedes: tuple[EventReference, ...] = ()
    subject_binding_digest: str | None = None


@dataclass(frozen=True)
class ReplayEntityBinding:
    original: EntityReference
    replay_entity_id: str


@dataclass(frozen=True)
class ReplayContext:
    """Proposed persisted sidecar; kept outside the immutable observation payload."""

    schema: str
    company_id: str
    run_id: str
    loop_id: str
    loop_index: int
    plan_digest: str
    original_event: EventReference
    original_sequence: int
    original_event_sha256: str
    original_receipt_refs: tuple[str, ...]
    receipt_sha256: tuple[str, ...]
    entity_namespace: str
    entity_bindings: tuple[ReplayEntityBinding, ...]
    supersedes: tuple[EventReference, ...]
    original_supersedes: tuple[EventReference, ...]
    external_effects: Literal["disabled"] = "disabled"
    agent_wakes: Literal["disabled"] = "disabled"
    subject_binding_digest: str | None = None


def replay_context_dict(context):
    """Omit absent optional fields to preserve already-compiled plan/event hashes."""
    value = asdict(context)
    if value["subject_binding_digest"] is None:
        value.pop("subject_binding_digest")
    return value


def _planned_dict(event):
    return asdict(event) | {"replay": replay_context_dict(event.replay)}


@dataclass(frozen=True)
class PlannedEvent:
    ordinal: int
    due_s: Decimal
    replay_source_id: str
    event_json: str
    replay: ReplayContext

    def canonical_event(self):
        # Return a fresh model so a consumer cannot mutate the retained plan.
        return CanonicalEvent.model_validate_json(self.event_json)


@dataclass(frozen=True)
class ReplayPlan:
    company_id: str
    run_id: str
    digest: str
    sources: tuple[ReplaySource, ...]
    loops: int
    rate: Decimal
    readiness_lag_s: Decimal
    loop_duration_s: Decimal
    duration_s: Decimal
    events: tuple[PlannedEvent, ...]

    def to_dict(self):
        return json.loads(
            _json(
                {
                    "schema": "rocoto.replay-plan.v1",
                    **asdict(self),
                    "events": [_planned_dict(e) for e in self.events],
                }
            )
        )


@dataclass(frozen=True)
class ReplayCursor:
    """Host stores this with CAS on revision only after the batch is admitted.

    Suggested primary key: (tenant_id, run_id), with plan_digest referencing the
    run's immutable registered plan. A run ID cannot bind a second plan. A crash
    before cursor commit redelivers deterministic event IDs; ordinary admission
    deduplicates them.
    """

    company_id: str
    run_id: str
    plan_digest: str
    next_ordinal: int = 0
    revision: int = 0
    last_due_s: Decimal = Decimal(0)
    schema: str = "rocoto.replay-cursor.v1"

    def to_dict(self):
        return json.loads(_json(asdict(self)))

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ReplayError("Restore the complete persisted replay cursor.")
        values = dict(value)
        values["last_due_s"] = _seconds(values["last_due_s"])
        return cls(**values)


@dataclass(frozen=True)
class ReplayBatch:
    events: tuple[PlannedEvent, ...]
    expected_cursor_revision: int
    candidate_cursor: ReplayCursor


@dataclass(frozen=True)
class ReplayPosition:
    """Read-only seek result; deliberately not an admission cursor."""

    elapsed_s: Decimal
    loop_index: int
    next_due_ordinal: int


def _reference(value):
    if (
        not isinstance(value, EventReference)
        or type(value.event_revision) is not int
        or value.event_revision < 1
    ):
        raise ReplayError("Use an exact source/event/revision reference.")
    _id(value.source_id)
    _id(value.event_id)
    return value


def compile_replay(
    *, company_id, run_id, sources, receipts, records, loops=1, rate=Decimal(1), readiness_lag_s=Decimal(0)
):
    """Compile accepted history; emit only after every actual input window ends.

    Source/event timestamps and raw payload IDs stay unchanged. Explicit entity
    bindings tell downstream projections which replay-local entities to use.
    Every revision chain starts at 1; correction dependencies must be included.
    """
    _id(company_id)
    _id(run_id)
    if type(loops) is not int or not 1 <= loops <= MAX_LOOPS:
        raise ReplayError("Use between 1 and 10 bounded replay loops.")
    rate = _seconds(rate, positive=True)
    lag = _seconds(readiness_lag_s)
    if not Decimal("0.1") <= rate <= 16 or lag > 300:
        raise ReplayError("Use a replay rate from 0.1 to 16 and readiness lag up to 300 seconds.")
    if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= 256:
        raise ReplayError("Provide between 1 and 256 explicit source windows.")
    source_map = {}
    for source in sources:
        if not isinstance(source, ReplaySource):
            raise ReplayError("Use typed replay source windows.")
        source = replace(
            source,
            start_s=_seconds(source.start_s),
            end_s=_seconds(source.end_s, positive=True),
            offset_s=_seconds(source.offset_s),
        )
        _id(source.source_id)
        _id(source.source_asset_id)
        if source.end_s <= source.start_s or source.source_id in source_map:
            raise ReplayError("Use one explicit nonempty window per source.")
        source_map[source.source_id] = source
    loop_duration = max(s.offset_s + (s.end_s - s.start_s) / rate + lag for s in source_map.values())
    if loop_duration * loops > MAX_DURATION_S:
        raise ReplayError("Keep the complete replay within one hour.")
    if not isinstance(receipts, (list, tuple)) or not 1 <= len(receipts) <= MAX_EVENTS:
        raise ReplayError("Provide a bounded saved input-receipt set.")
    receipt_map = {}
    for receipt in receipts:
        if not isinstance(receipt, ReplayReceipt):
            raise ReplayError("Use typed saved input receipts.")
        _id(receipt.receipt_id)
        _id(receipt.profile_revision_id)
        source = source_map.get(receipt.source_id)
        start, end = _seconds(receipt.start_s), _seconds(receipt.end_s, positive=True)
        if (
            source is None
            or receipt.source_asset_id != source.source_asset_id
            or start < source.start_s
            or end > source.end_s
            or end <= start
            or not isinstance(receipt.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt.sha256)
        ):
            raise ReplayError("Bind saved receipt bytes and bounds to the selected source window.")
        key = (receipt.source_id, receipt.receipt_id)
        if key in receipt_map:
            raise ReplayError("Use each input receipt identity once.")
        receipt_map[key] = replace(receipt, start_s=start, end_s=end)
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) * loops <= MAX_EVENTS:
        raise ReplayError("Keep replay output between 1 and 10000 events.")
    canonical = []
    by_ref = {}
    used_sequences = set()
    retained_bytes = 0
    for record in records:
        if (
            not isinstance(record, AdmittedReplayEvent)
            or type(record.sequence) is not int
            or record.sequence < 1
        ):
            raise ReplayError("Use admitted source records with positive sequence identities.")
        if record.sequence in used_sequences:
            raise ReplayError("Admission sequences must identify distinct retained records.")
        used_sequences.add(record.sequence)
        event = CanonicalEvent.model_validate(record.event.model_dump(mode="json", by_alias=True))
        if event.origin_kind != "observation":
            raise ReplayError("This replay compiler accepts admitted observations only.")
        source = source_map.get(record.source_id)
        p = event.provenance
        if (
            source is None
            or p.source_asset_id != source.source_asset_id
            or p.source_window.start_s < source.start_s
            or p.source_window.end_s > source.end_s
        ):
            raise ReplayError("The event must fit its explicitly selected original source window.")
        evidence = []
        for ref in p.actual_receipt_refs:
            receipt = receipt_map.get((record.source_id, ref))
            if (
                receipt is None
                or receipt.profile_revision_id != p.profile_revision_id
                or receipt.start_s > p.source_window.start_s
                or receipt.end_s < p.source_window.end_s
            ):
                raise ReplayError("Preserve the actual input receipts covering the event and profile.")
            evidence.append(receipt)
        if record.subject_binding_digest is not None and not re.fullmatch(
            r"[a-f0-9]{64}", record.subject_binding_digest
        ):
            raise ReplayError("Preserve the exact sealed subject-set digest.")
        if len(record.entities) > 128 or len(record.supersedes) > 32:
            raise ReplayError("Keep explicit entity/correction references bounded.")
        for entity in record.entities:
            if not isinstance(entity, EntityReference):
                raise ReplayError("Use explicit typed entity references; payload IDs are not guessed.")
            _id(entity.kind)
            _id(entity.namespace)
            _id(entity.entity_id)
        if len(set(record.entities)) != len(record.entities):
            raise ReplayError("Keep entity bindings unique.")
        ref = EventReference(record.source_id, event.event_id, event.event_revision)
        if ref in by_ref:
            raise ReplayError("Use each admitted event revision once.")
        body = _json(event.model_dump(mode="json", by_alias=True))
        retained_bytes += len(body.encode()) * loops
        if retained_bytes > MAX_PLAN_BYTES:
            raise ReplayError("Compiled replay exceeds its retained byte budget.")
        entry = {
            "record": record,
            "event": event,
            "body": body,
            "ref": ref,
            "evidence": evidence,
            "due": source.offset_s + (max(r.end_s for r in evidence) - source.start_s) / rate + lag,
        }
        canonical.append(entry)
        by_ref[ref] = entry
    canonical.sort(key=lambda item: item["record"].sequence)
    last_revision = {}
    for entry in canonical:
        ref = entry["ref"]
        identity = (ref.source_id, ref.event_id)
        if ref.event_revision != last_revision.get(identity, 0) + 1:
            raise ReplayError(
                "Include each correction's contiguous original revision history in admission order."
            )
        last_revision[identity] = ref.event_revision
        dependencies = list(entry["record"].supersedes)
        if ref.event_revision > 1:
            dependencies.append(EventReference(ref.source_id, ref.event_id, ref.event_revision - 1))
        entry["dependencies"] = tuple(dict.fromkeys(_reference(dep) for dep in dependencies))
        for dependency in entry["dependencies"]:
            previous = by_ref.get(dependency)
            if previous is None or previous["record"].sequence >= entry["record"].sequence:
                raise ReplayError(
                    "Include earlier admitted correction references; future references are invalid."
                )
            entry["due"] = max(entry["due"], previous["due"])
    plan_events = []
    compiled_bytes = len(_json([asdict(source) for source in source_map.values()]).encode()) + 4096
    for loop in range(loops):
        loop_id = _key("replay-loop-", company_id, run_id, loop)
        namespace = _key("replay-entities-", company_id, run_id, loop)
        for entry in sorted(canonical, key=lambda item: (item["due"], item["record"].sequence)):
            ref = entry["ref"]
            replay_source = _key("replay-source-", company_id, run_id, ref.source_id)
            replay_event = _key("replay-event-", company_id, run_id, loop, ref.source_id, ref.event_id)
            event_body = json.loads(entry["body"])
            event_body["event_id"] = replay_event
            context = ReplayContext(
                schema="rocoto.replay-context.v1",
                company_id=company_id,
                run_id=run_id,
                loop_id=loop_id,
                loop_index=loop,
                plan_digest="",
                original_event=ref,
                original_sequence=entry["record"].sequence,
                original_event_sha256=hashlib.sha256(entry["body"].encode()).hexdigest(),
                original_receipt_refs=tuple(r.receipt_id for r in entry["evidence"]),
                receipt_sha256=tuple(r.sha256 for r in entry["evidence"]),
                entity_namespace=namespace,
                entity_bindings=tuple(
                    ReplayEntityBinding(
                        entity,
                        _key(
                            "replay-entity-",
                            company_id,
                            run_id,
                            loop,
                            ref.source_id,
                            entity.kind,
                            entity.namespace,
                            entity.entity_id,
                        ),
                    )
                    for entity in entry["record"].entities
                ),
                subject_binding_digest=entry["record"].subject_binding_digest,
                original_supersedes=entry["dependencies"],
                supersedes=tuple(
                    EventReference(
                        _key("replay-source-", company_id, run_id, dep.source_id),
                        _key("replay-event-", company_id, run_id, loop, dep.source_id, dep.event_id),
                        dep.event_revision,
                    )
                    for dep in entry["dependencies"]
                ),
            )
            planned = PlannedEvent(
                len(plan_events),
                loop * loop_duration + entry["due"],
                replay_source,
                _json(event_body),
                context,
            )
            # Check each complete sidecar before retaining it; a large entity list
            # must not build an oversized plan before the final manifest check.
            compiled_bytes += len(_json(asdict(planned)).encode()) + 128
            if compiled_bytes > MAX_PLAN_BYTES:
                raise ReplayError("Compiled plan and typed provenance exceed their retained byte budget.")
            plan_events.append(planned)
    ordered_sources = tuple(sorted(source_map.values(), key=lambda source: source.source_id))
    manifest = {
        "schema": "rocoto.replay-plan.v1",
        "company_id": company_id,
        "run_id": run_id,
        "sources": [asdict(s) for s in ordered_sources],
        "loops": loops,
        "rate": rate,
        "lag": lag,
        "events": [_planned_dict(e) for e in plan_events],
    }
    encoded = _json(manifest).encode()
    if len(encoded) > MAX_PLAN_BYTES:
        raise ReplayError("Compiled plan and typed provenance exceed their retained byte budget.")
    digest = hashlib.sha256(encoded).hexdigest()
    events = tuple(replace(e, replay=replace(e.replay, plan_digest=digest)) for e in plan_events)
    return ReplayPlan(
        company_id,
        run_id,
        digest,
        ordered_sources,
        loops,
        rate,
        lag,
        loop_duration,
        loop_duration * loops,
        events,
    )


def initial_cursor(plan):
    return ReplayCursor(plan.company_id, plan.run_id, plan.digest)


def due_events(plan, cursor, elapsed_s, *, limit=100):
    """Return due work plus an uncommitted checkpoint; never advance on reads."""
    if (
        not isinstance(cursor, ReplayCursor)
        or cursor.schema != "rocoto.replay-cursor.v1"
        or (cursor.company_id, cursor.run_id, cursor.plan_digest)
        != (plan.company_id, plan.run_id, plan.digest)
        or type(cursor.next_ordinal) is not int
        or not 0 <= cursor.next_ordinal <= len(plan.events)
        or type(cursor.revision) is not int
        or cursor.revision < 0
    ):
        raise ReplayError("Resume only the matching plan's complete acknowledged cursor.")
    expected_due = plan.events[cursor.next_ordinal - 1].due_s if cursor.next_ordinal else Decimal(0)
    if _seconds(cursor.last_due_s) != expected_due:
        raise ReplayError("The persisted cursor must match its acknowledged event boundary.")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ReplayError("Request between 1 and 1000 due events.")
    elapsed = _seconds(elapsed_s)
    selected = []
    for event in plan.events[cursor.next_ordinal : cursor.next_ordinal + limit]:
        if event.due_s > elapsed:
            break
        selected.append(event)
    candidate = (
        cursor
        if not selected
        else replace(
            cursor,
            next_ordinal=cursor.next_ordinal + len(selected),
            revision=cursor.revision + 1,
            last_due_s=selected[-1].due_s,
        )
    )
    return ReplayBatch(tuple(selected), cursor.revision, candidate)


def seek(plan, elapsed_s):
    """Inspect a playback position without skipping or rewinding committed work."""
    elapsed = _seconds(elapsed_s)
    next_ordinal = next((e.ordinal for e in plan.events if e.due_s > elapsed), len(plan.events))
    loop = min(plan.loops - 1, int(elapsed // plan.loop_duration_s))
    return ReplayPosition(elapsed, loop, next_ordinal)


def restore_plan(value):
    """Restore an immutable database plan and verify its compiler digest/identities."""
    fields = set(ReplayPlan.__dataclass_fields__) | {"schema"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != "rocoto.replay-plan.v1":
        raise ReplayError("Restore the complete compiled replay plan.")
    if len(_json(value).encode()) > MAX_PLAN_BYTES + MAX_EVENTS * 128:
        raise ReplayError("Stored replay plan exceeds its byte bound.")
    _id(value["company_id"])
    _id(value["run_id"])
    if not isinstance(value["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["digest"]):
        raise ReplayError("Restore the saved plan digest.")

    def exact(model, data):
        if not isinstance(data, dict) or set(data) != set(model.__dataclass_fields__):
            raise ReplayError("Restore the complete typed replay sidecar.")
        return dict(data)

    sources = []
    for data in value["sources"]:
        args = exact(ReplaySource, data)
        for key in ("start_s", "end_s", "offset_s"):
            args[key] = _seconds(args[key])
        sources.append(ReplaySource(**args))
    if not 1 <= len(sources) <= 256 or len({s.source_id for s in sources}) != len(sources):
        raise ReplayError("Restore distinct bounded source windows.")
    source_map = {s.source_id: s for s in sources}
    events = []
    for ordinal, data in enumerate(value["events"]):
        args = exact(PlannedEvent, data)
        original_context = args["replay"]
        if isinstance(original_context, dict) and "subject_binding_digest" not in original_context:
            original_context = original_context | {"subject_binding_digest": None}
        context = exact(ReplayContext, original_context)
        digest = context["subject_binding_digest"]
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ReplayError("Restore the sealed subject-set digest.")
        context["original_event"] = _reference(
            EventReference(**exact(EventReference, context["original_event"]))
        )
        for key in ("supersedes", "original_supersedes"):
            context[key] = tuple(_reference(EventReference(**exact(EventReference, r))) for r in context[key])
        entities = []
        for binding in context["entity_bindings"]:
            binding = exact(ReplayEntityBinding, binding)
            binding["original"] = EntityReference(**exact(EntityReference, binding["original"]))
            entities.append(ReplayEntityBinding(**binding))
        context["entity_bindings"] = tuple(entities)
        for key in ("original_receipt_refs", "receipt_sha256"):
            context[key] = tuple(context[key])
        replay = ReplayContext(**context)
        event = CanonicalEvent.model_validate_json(args["event_json"])
        source = source_map.get(replay.original_event.source_id)
        if (
            args["ordinal"] != ordinal
            or type(args["ordinal"]) is not int
            or replay.schema != "rocoto.replay-context.v1"
            or replay.company_id != value["company_id"]
            or replay.run_id != value["run_id"]
            or replay.plan_digest != value["digest"]
            or replay.external_effects != "disabled"
            or replay.agent_wakes != "disabled"
            or type(replay.loop_index) is not int
            or not 0 <= replay.loop_index < value["loops"]
            or source is None
            or event.origin_kind != "observation"
            or event.event_revision != replay.original_event.event_revision
            or event.provenance.source_asset_id != source.source_asset_id
            or tuple(event.provenance.actual_receipt_refs) != replay.original_receipt_refs
            or len(replay.original_receipt_refs) != len(replay.receipt_sha256)
        ):
            raise ReplayError("Stored replay identities or evidence bindings differ from the compiled plan.")
        company, run, loop = value["company_id"], value["run_id"], replay.loop_index
        if (
            args["replay_source_id"] != _key("replay-source-", company, run, source.source_id)
            or event.event_id
            != _key("replay-event-", company, run, loop, source.source_id, replay.original_event.event_id)
            or replay.loop_id != _key("replay-loop-", company, run, loop)
            or replay.entity_namespace != _key("replay-entities-", company, run, loop)
        ):
            raise ReplayError("Stored replay namespace differs from its registered run.")
        for binding in replay.entity_bindings:
            entity = binding.original
            if binding.replay_entity_id != _key(
                "replay-entity-",
                company,
                run,
                loop,
                source.source_id,
                entity.kind,
                entity.namespace,
                entity.entity_id,
            ):
                raise ReplayError("Stored replay entity mapping is inconsistent.")
        original = event.model_dump(mode="json", by_alias=True)
        original["event_id"] = replay.original_event.event_id
        if hashlib.sha256(_json(original).encode()).hexdigest() != replay.original_event_sha256:
            raise ReplayError("Stored replay observation differs from its original evidence digest.")
        args.update(due_s=_seconds(args["due_s"]), replay=replay)
        events.append(PlannedEvent(**args))
    if (
        type(value["loops"]) is not int
        or not 1 <= value["loops"] <= MAX_LOOPS
        or not 1 <= len(events) <= MAX_EVENTS
    ):
        raise ReplayError("Restore the bounded replay loop/event count.")
    rate, lag = _seconds(value["rate"], positive=True), _seconds(value["readiness_lag_s"])
    loop_duration, duration = (
        _seconds(value["loop_duration_s"], positive=True),
        _seconds(value["duration_s"], positive=True),
    )
    if (
        not Decimal("0.1") <= rate <= 16
        or lag > 300
        or duration > MAX_DURATION_S
        or duration != loop_duration * value["loops"]
        or loop_duration != max(s.offset_s + (s.end_s - s.start_s) / rate + lag for s in sources)
        or any(b.due_s < a.due_s for a, b in zip(events, events[1:], strict=False))
        or any(e.due_s > duration for e in events)
    ):
        raise ReplayError("Restore the original bounded replay clock.")
    manifest = {
        "schema": "rocoto.replay-plan.v1",
        "company_id": value["company_id"],
        "run_id": value["run_id"],
        "sources": [asdict(s) for s in sources],
        "loops": value["loops"],
        "rate": rate,
        "lag": lag,
        "events": [_planned_dict(replace(e, replay=replace(e.replay, plan_digest=""))) for e in events],
    }
    if hashlib.sha256(_json(manifest).encode()).hexdigest() != value["digest"]:
        raise ReplayError("Stored replay plan digest does not match its immutable contents.")
    return ReplayPlan(
        value["company_id"],
        value["run_id"],
        value["digest"],
        tuple(sources),
        value["loops"],
        rate,
        lag,
        loop_duration,
        duration,
        tuple(events),
    )
