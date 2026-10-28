"""Source-backed, non-biometric identities and exact event-subject associations.

The authenticated host supplies typed registrations. Descriptive event payloads
never create identities. Revisions and bindings are append-only and every lookup
rechecks the original source grant, including copied replay history.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from realtime.replay import EntityReference
from realtime.store import AccessDenied, CapacityExceeded, Conflict, RealTimeStore, _digest, _encoded

Kind = Literal["camera", "zone", "visit", "observed_item", "catalog_product"]
Role = Literal["camera", "zone", "actor", "recipient", "item", "product_candidate", "product_confirmed"]
KINDS = {
    "camera": "camera",
    "zone": "zone",
    "actor": "visit",
    "recipient": "visit",
    "item": "observed_item",
    "product_candidate": "catalog_product",
    "product_confirmed": "catalog_product",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CameraAttributes(Strict):
    camera_id: str = Field(min_length=1, max_length=256)
    label: str | None = Field(default=None, max_length=256)


class ZoneAttributes(Strict):
    camera_subject_id: str = Field(min_length=1, max_length=256)
    zone_id: str = Field(min_length=1, max_length=256)
    zone_type: Literal["queue", "shelf", "checkout", "other"]
    label: str | None = Field(default=None, max_length=256)


class VisitAttributes(Strict):
    camera_subject_id: str = Field(min_length=1, max_length=256)
    source_asset_id: str = Field(min_length=1, max_length=256)
    tracker_namespace: str = Field(min_length=1, max_length=256)
    tracker_id: str = Field(min_length=1, max_length=256)
    visit_boundary_id: str = Field(min_length=1, max_length=256)
    basis: Literal["first_observed", "first_reported"] = "first_observed"
    tracker_epoch_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_serializer(mode="wrap")
    def _preserve_recording_identity(self, handler):
        value = handler(self)
        if value.get("tracker_epoch_id") is None:
            value.pop("tracker_epoch_id", None)
        return value


class ItemAttributes(Strict):
    visit_subject_id: str = Field(min_length=1, max_length=256)
    source_asset_id: str = Field(min_length=1, max_length=256)
    instance_ref: str = Field(min_length=1, max_length=256)
    identity_basis: Literal["object_tracker", "human_review", "vision_estimate"]
    quantity: int | None = Field(default=None, ge=1, le=1000, strict=True)
    quantity_basis: Literal["single_sale_unit", "counted_sale_units", "estimated"] | None = None
    description: str | None = Field(default=None, max_length=1024)
    label: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def quantity_evidence(self):
        if (self.quantity is None) != (self.quantity_basis is None):
            raise ValueError("Quantity and its evidence basis travel together")
        if self.quantity_basis == "single_sale_unit" and self.quantity != 1:
            raise ValueError("A single sale unit has quantity one")
        return self


class ProductAttributes(Strict):
    catalogue_sku_id: str | None = Field(default=None, min_length=1, max_length=256)
    provisional_ref: str | None = Field(default=None, min_length=1, max_length=256)
    label: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def identity(self):
        if (self.catalogue_sku_id is None) == (self.provisional_ref is None):
            raise ValueError("Supply exactly one catalog identity or provisional reference")
        return self


ATTRIBUTES = {
    "camera": CameraAttributes,
    "zone": ZoneAttributes,
    "visit": VisitAttributes,
    "observed_item": ItemAttributes,
    "catalog_product": ProductAttributes,
}


class SubjectProvenance(Strict):
    reference: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    receipt_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_start_s: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_end_s: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def interval(self):
        if (self.source_start_s is None) != (self.source_end_s is None):
            raise ValueError("Supply both source clock endpoints")
        if self.source_start_s is not None and self.source_end_s <= self.source_start_s:
            raise ValueError("Use a nonempty half-open source interval")
        if self.receipt_id is not None and self.source_start_s is None:
            raise ValueError("Receipt evidence includes its source interval")
        return self


class SubjectRegistration(Strict):
    subject_id: str = Field(min_length=1, max_length=256)
    source_id: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=256)
    kind: Kind
    attributes: dict
    provenance: SubjectProvenance
    status: Literal["active", "closed", "retracted"] = "active"

    @model_validator(mode="after")
    def typed(self):
        ATTRIBUTES[self.kind].model_validate(self.attributes)
        if self.kind in {"visit", "observed_item"} and self.provenance.receipt_id is None:
            raise ValueError("Tracked subjects require an actual source receipt")
        if self.status == "closed" and self.kind != "visit":
            raise ValueError("Closed is a visit lifecycle state")
        return self


class SubjectBinding(Strict):
    role: Role
    subject_id: str = Field(min_length=1, max_length=256)
    subject_revision: int = Field(ge=1, le=10000, strict=True)


def _identity(record):
    attrs = ATTRIBUTES[record.kind].model_validate(record.attributes).model_dump(mode="json")
    immutable = {
        k: v
        for k, v in attrs.items()
        if k not in {"label", "description", "quantity", "quantity_basis", "identity_basis"}
    }
    if attrs.get("tracker_epoch_id"):
        # The birth file remains evidence, while the same live tracker spans later files.
        immutable.pop("source_asset_id", None)
    parent = attrs.get("camera_subject_id") or attrs.get("visit_subject_id")
    return attrs, parent, _digest([record.source_id, record.kind, record.namespace, immutable])


class SubjectStore:
    def __init__(self, store=None):
        self.store = store or RealTimeStore()

    async def _read(self, con, context, subject_id, revision=None):
        row = await con.fetchrow(
            "select s.*,r.revision,r.attributes,r.provenance,r.status,r.replay_context,"
            "case when (select status from realtime.subject_revision original where "
            "original.tenant_id=s.tenant_id and original.subject_id=s.original_subject_id "
            "order by revision desc limit 1)='retracted' then 'retracted' else "
            "(select status from realtime.subject_revision latest where latest.tenant_id=s.tenant_id "
            "and latest.subject_id=s.subject_id order by revision desc limit 1) end current_status "
            "from realtime.subject s join realtime.subject_revision r using(tenant_id,source_id,subject_id) "
            "where s.tenant_id=$1 and s.subject_id=$2 and ($3::integer is null or r.revision=$3) "
            "order by r.revision desc limit 1",
            context.company_id,
            subject_id,
            revision,
        )
        if not row:
            raise AccessDenied("Subject is unavailable in this source grant")
        result = {
            k: row[k]
            for k in (
                "subject_id",
                "kind",
                "source_id",
                "namespace",
                "revision",
                "status",
                "current_status",
                "attributes",
                "provenance",
                "replay_context",
            )
        } | {"company_id": context.company_id}
        sku = result["attributes"].get("catalogue_sku_id")
        if sku and not await con.fetchval(
            "select realtime.catalogue_subject_visible($1,$2)", context.company_id, sku
        ):
            raise AccessDenied("Catalog identity requires its current company association")
        _encoded(result, context.bounds.max_output_bytes)
        return result

    async def get(self, context, subject_id, revision=None):
        async with self.store._transaction(context, "realtime:read") as con:
            return await self._read(con, context, subject_id, revision)

    async def _budget(self, con, context, additional_bytes):
        limits = await self.store._lock_budget(con, context)
        usage = await self.store._usage(con, context)
        self.store._check_bytes(limits, usage, additional_bytes)
        count = await con.fetchval(
            "select revision_count from realtime.subject_usage($1)", context.company_id
        )
        if count >= min(limits["max_events"] * 4, 200000):
            raise CapacityExceeded("Subject revision budget reached")

    async def register(self, context, registration, *, expected_revision=0):
        record = SubjectRegistration.model_validate(registration)
        if type(expected_revision) is not int or not 0 <= expected_revision < 10000:
            raise ValueError("Expected revision is a bounded integer")
        capability = "realtime:ingest" if record.kind in {"visit", "observed_item"} else "realtime:configure"
        async with self.store._transaction(context, capability) as con:
            await self.store._source(con, context, record.source_id, writing=True)
            source = await self.store._source(con, context, record.source_id)
            if record.kind != "catalog_product" and source["kind"] not in {"camera", "replay"}:
                raise Conflict("Tracked places and subjects require a camera source")
            if source["kind"] == "replay":
                raise Conflict("Replay identities are copied from admitted original bindings")
            attrs, parent, identity_digest = _identity(record)
            body = record.model_dump(mode="json") | {"attributes": attrs}
            digest = _digest(body)
            await self.store._lock_budget(con, context)
            existing = await con.fetchrow(
                "select * from realtime.subject where tenant_id=$1 and subject_id=$2",
                context.company_id,
                record.subject_id,
            )
            if existing and existing["identity_digest"] != identity_digest:
                raise Conflict("Subject identity is immutable")
            previous = await con.fetchrow(
                "select revision,digest,status,attributes from realtime.subject_revision where tenant_id=$1 "
                "and subject_id=$2 order by revision desc limit 1",
                context.company_id,
                record.subject_id,
            )
            if (
                previous
                and attrs.get("tracker_epoch_id")
                and previous["attributes"].get("source_asset_id") != attrs["source_asset_id"]
            ):
                raise Conflict("Visit birth artifact is immutable")
            revision = expected_revision + 1
            retry = await con.fetchval(
                "select digest from realtime.subject_revision where tenant_id=$1 and subject_id=$2 "
                "and revision=$3",
                context.company_id,
                record.subject_id,
                revision,
            )
            if retry:
                if retry != digest:
                    raise Conflict("Subject revision conflicts with its retained command")
                return await self._read(con, context, record.subject_id, revision) | {"needs_refresh": False}
            await self._budget(con, context, len(_encoded(body)) + 2048)
            if (previous["revision"] if previous else 0) != expected_revision:
                raise Conflict("Subject revision changed")
            if previous and previous["status"] != "active" and record.status != "retracted":
                raise Conflict("A finished visit or retracted subject retains its original boundary")
            duplicate = await con.fetchval(
                "select subject_id from realtime.subject where tenant_id=$1 and identity_digest=$2",
                context.company_id,
                identity_digest,
            )
            if duplicate and duplicate != record.subject_id:
                raise Conflict("Source identity already has an explicit subject identifier")
            parent_record = None
            if parent:
                parent_record = await self._read(con, context, parent)
                required = "visit" if record.kind == "observed_item" else "camera"
                if (
                    parent_record["source_id"] != record.source_id
                    or parent_record["kind"] != required
                    or parent_record["current_status"] == "retracted"
                ):
                    raise Conflict("Subject parent must belong to the same source and expected kind")
                if (
                    record.kind == "observed_item"
                    and attrs["source_asset_id"] != parent_record["attributes"]["source_asset_id"]
                    and not parent_record["attributes"].get("tracker_epoch_id")
                ):
                    raise Conflict("Item and visit preserve the same recording")
            await self._evidence(con, context, record, attrs, parent_record)
            if not existing:
                await con.execute(
                    "insert into realtime.subject(tenant_id,source_id,subject_id,kind,namespace,"
                    "identity_digest,"
                    "parent_subject_id) values($1,$2,$3,$4,$5,$6,$7)",
                    context.company_id,
                    record.source_id,
                    record.subject_id,
                    record.kind,
                    record.namespace,
                    identity_digest,
                    parent,
                )
            await con.execute(
                "insert into realtime.subject_revision(tenant_id,source_id,subject_id,revision,digest,"
                "status,attributes,provenance) values($1,$2,$3,$4,$5,$6,$7,$8)",
                context.company_id,
                record.source_id,
                record.subject_id,
                revision,
                digest,
                record.status,
                attrs,
                record.provenance.model_dump(mode="json"),
            )
            return await self._read(con, context, record.subject_id, revision) | {"needs_refresh": True}

    async def _evidence(self, con, context, record, attrs, parent_record=None):
        p = record.provenance
        receipt_body = None
        if p.receipt_id:
            receipt_row = await con.fetchrow(
                "select body,digest from realtime.input_receipt where tenant_id=$1 and source_id=$2"
                " and receipt_id=$3",
                context.company_id,
                record.source_id,
                p.receipt_id,
            )
            receipt = receipt_row["body"] if receipt_row else None
            receipt_body = receipt
            if receipt_row and receipt_row["digest"] != p.sha256:
                raise Conflict("Subject provenance must preserve the actual receipt hash")
            if not receipt or receipt.get("source_asset_id") != attrs.get("source_asset_id"):
                raise Conflict("Subject evidence must identify the registered source recording")
            if (
                record.kind == "visit"
                and attrs.get("basis") == "first_reported"
                and not attrs.get("tracker_epoch_id")
            ):
                raise Conflict("First-reported visits require their historical tracker epoch")
            if record.kind == "visit" and bool(receipt.get("continuity")) != bool(
                attrs.get("tracker_epoch_id")
            ):
                raise Conflict("Visit and actual input must preserve the same epoch qualification")
            window = receipt["source_window"]
            if (
                not Decimal(window["start_s"])
                <= p.source_start_s
                < p.source_end_s
                <= Decimal(window["end_s"])
            ):
                raise Conflict("Subject evidence falls outside the actual source receipt")
        if (
            record.kind == "observed_item"
            and parent_record is not None
            and parent_record["attributes"].get("basis") == "first_reported"
        ):
            # A reported visit has no observed interval for an item to share.
            raise Conflict("Observed items require observed tracker coverage of their visit")
        if (
            record.kind == "observed_item"
            and parent_record is not None
            and parent_record["attributes"].get("tracker_epoch_id")
            and attrs["source_asset_id"] != parent_record["attributes"].get("source_asset_id")
        ):
            if receipt_body is None:
                raise Conflict("Cross-file item requires an actual continuity receipt")
            continuity = receipt_body.get("continuity")
            if not continuity:
                raise Conflict("Cross-file item requires an epoch-qualified receipt")
            from realtime.continuity import ContinuityStore
            from realtime.contracts import SourceWindow

            if continuity.get("epoch_id") != parent_record["attributes"]["tracker_epoch_id"]:
                raise Conflict("Cross-file item must preserve the visit tracker epoch")
            snapshot = receipt_body.get("tracker_snapshot")
            if snapshot is None:
                raise Conflict("Cross-file item requires an observed tracker snapshot")
            await ContinuityStore(self.store)._track_evidence(
                con,
                context,
                record.source_id,
                p.receipt_id,
                p.sha256,
                parent_record["attributes"]["tracker_id"],
                parent_record["attributes"]["visit_boundary_id"],
                SourceWindow(start_s=p.source_start_s, end_s=p.source_end_s),
                "observed",
            )
            if not await con.fetchval(
                "select 1 from realtime.track_segment where tenant_id=$1 and source_id=$2 "
                "and visit_subject_id=$3 and epoch_id=$4 and mapping_id=$5 and receipt_id=$6 "
                "and state='observed' and (body->'source_window'->>'start_s')::numeric <= $7 "
                "and (body->'source_window'->>'end_s')::numeric >= $8",
                context.company_id,
                record.source_id,
                parent_record["subject_id"],
                continuity["epoch_id"],
                continuity["mapping_id"],
                p.receipt_id,
                p.source_start_s,
                p.source_end_s,
            ):
                raise Conflict("Cross-file item requires observed tracker coverage")
            if snapshot.get("tracker_namespace") != parent_record["attributes"]["tracker_namespace"]:
                raise Conflict("Cross-file item must preserve the visit tracker namespace")
        if record.kind == "visit" and attrs.get("tracker_epoch_id"):
            from realtime.continuity import ContinuityStore

            await ContinuityStore(self.store).validate_visit(con, context, record, attrs)
        sku = attrs.get("catalogue_sku_id")
        if sku and not await con.fetchval(
            "select realtime.catalogue_subject_visible($1,$2)", context.company_id, sku
        ):
            raise AccessDenied("Catalog product belongs to an authorized company catalog")

    async def bind_event(self, context, source_id, event_id, event_revision, bindings):
        async with self.store._transaction(context, "realtime:ingest") as con:
            return await self._bind_event(con, context, source_id, event_id, event_revision, bindings)

    async def _bind_event(self, con, context, source_id, event_id, event_revision, bindings):
        if not isinstance(bindings, (list, tuple)) or len(bindings) > 32:
            raise ValueError("Use up to 32 explicit event bindings")
        parsed = [SubjectBinding.model_validate(b) for b in bindings]
        rows = sorted((b.model_dump(mode="json") for b in parsed), key=lambda b: (b["role"], b["subject_id"]))
        if len({(b.role, b.subject_id) for b in parsed}) != len(parsed):
            raise ValueError("Each role-subject association appears once")
        for role in ("actor", "recipient", "camera", "zone", "product_confirmed"):
            if sum(b.role == role for b in parsed) > 1:
                raise ValueError("Role requires one explicit association")
        await self.store._source(con, context, source_id, writing=True)
        await self.store._lock_budget(con, context)
        event = await con.fetchrow(
            "select envelope,replay_context from realtime.event where tenant_id=$1 and source_id=$2 "
            "and event_id=$3 and event_revision=$4",
            context.company_id,
            source_id,
            event_id,
            event_revision,
        )
        if not event:
            raise AccessDenied("Bindings require an admitted event in this source grant")
        digest = _digest(rows)
        prior = await con.fetchval(
            "select digest from realtime.event_subject_set where tenant_id=$1 and event_id=$2"
            " and event_revision=$3",
            context.company_id,
            event_id,
            event_revision,
        )
        if prior:
            if prior != digest:
                raise Conflict("An event revision retains its exact subject binding set")
            return await self._event_bindings(con, context, source_id, event_id, event_revision)
        await self._budget(con, context, len(_encoded(rows)) + len(rows) * 512 + 1024)
        for binding in parsed:
            subject = await self._read(con, context, binding.subject_id, binding.subject_revision)
            await self._validate_binding(con, context, source_id, event, binding, subject)
        await con.execute(
            "insert into realtime.event_subject_set(tenant_id,source_id,event_id,event_revision,"
            "digest,binding_count) "
            "values($1,$2,$3,$4,$5,$6)",
            context.company_id,
            source_id,
            event_id,
            event_revision,
            digest,
            len(rows),
        )
        for b in parsed:
            await con.execute(
                "insert into realtime.event_subject_binding(tenant_id,source_id,event_id,"
                "event_revision,role,"
                "subject_id,subject_revision) values($1,$2,$3,$4,$5,$6,$7)",
                context.company_id,
                source_id,
                event_id,
                event_revision,
                b.role,
                b.subject_id,
                b.subject_revision,
            )
        return await self._event_bindings(con, context, source_id, event_id, event_revision)

    async def _validate_binding(self, con, context, source_id, event, binding, subject):
        if KINDS[binding.role] != subject["kind"] or subject["current_status"] == "retracted":
            raise Conflict("Binding requires an available subject of the declared kind")
        if subject["kind"] != "catalog_product" and subject["source_id"] != source_id:
            raise Conflict("Tracked subjects retain their exact source")
        if binding.role == "product_confirmed" and not subject["attributes"].get("catalogue_sku_id"):
            raise Conflict("Confirmed catalog links require a registered catalog identity")
        replay = event["replay_context"]
        if bool(replay) != bool(subject["replay_context"]):
            raise Conflict("Live and replay subject namespaces remain separate")
        if replay and any(
            replay[k] != subject["replay_context"].get(k) for k in ("run_id", "loop_id", "plan_digest")
        ):
            raise Conflict("Replay subject belongs to another run or loop")
        provenance = event["envelope"]["provenance"]
        if (
            subject["kind"] == "visit"
            and provenance.get("continuity")
            and not subject["attributes"].get("tracker_epoch_id")
        ):
            raise Conflict("Epoch event requires an epoch-qualified visit")
        if subject["kind"] == "visit" and subject["attributes"].get("tracker_epoch_id"):
            from realtime.continuity import ContinuityStore

            await ContinuityStore(self.store).validate_binding(con, context, source_id, event, subject)
            return
        asset = subject["attributes"].get("source_asset_id")
        if asset and provenance.get("source_asset_id") != asset:
            raise Conflict("Event and tracked subject preserve the same source asset")

    async def _event_bindings(self, con, context, source_id, event_id, event_revision):
        await self.store._source(con, context, source_id)
        sealed = await con.fetchval(
            "select binding_count from realtime.event_subject_set where tenant_id=$1 and source_id=$2 "
            "and event_id=$3 and event_revision=$4",
            context.company_id,
            source_id,
            event_id,
            event_revision,
        )
        if sealed is None:
            return None
        rows = await con.fetch(
            "select role,subject_id,subject_revision from realtime.event_subject_binding where tenant_id=$1 "
            "and source_id=$2 and event_id=$3 and event_revision=$4 order by role,subject_id",
            context.company_id,
            source_id,
            event_id,
            event_revision,
        )
        if len(rows) != sealed:
            raise AccessDenied("Subject binding set requires all original source grants")
        result = [
            {
                "role": row["role"],
                "subject": await self._read(con, context, row["subject_id"], row["subject_revision"]),
            }
            for row in rows
        ]
        _encoded(result, context.bounds.max_output_bytes)
        return result

    async def event_bindings(self, context, source_id, event_id, event_revision):
        async with self.store._transaction(context, "realtime:read") as con:
            return await self._event_bindings(con, context, source_id, event_id, event_revision)

    async def replay_entities(self, context, source_id, event_id, event_revision):
        async with self.store._transaction(context, "realtime:read") as con:
            return await self._replay_entities(con, context, source_id, event_id, event_revision)

    async def _replay_entities(self, con, context, source_id, event_id, event_revision):
        bindings = await self._event_bindings(con, context, source_id, event_id, event_revision)
        if bindings is None:
            return None
        found = {}
        for binding in bindings:
            subject = binding["subject"]
            if subject["attributes"].get("tracker_epoch_id"):
                raise Conflict("Epoch continuity replay requires a commissioned compiler mapping")
            while subject["subject_id"] not in found:
                found[subject["subject_id"]] = EntityReference(
                    subject["kind"], subject["namespace"], subject["subject_id"]
                )
                parent = subject["attributes"].get("camera_subject_id") or subject["attributes"].get(
                    "visit_subject_id"
                )
                if not parent:
                    break
                subject = await self._read(con, context, parent)
        return tuple(found[key] for key in sorted(found))

    async def copy_replay_event(self, context, source_id, event_id, event_revision):
        async with self.store._transaction(context, "realtime:ingest") as con:
            return await self._copy_replay_event(con, context, source_id, event_id, event_revision)

    async def _copy_replay_event(self, con, context, source_id, event_id, event_revision):
        """Copy and seal with event admission/cursor in the caller's transaction."""
        await self.store._source(con, context, source_id, writing=True)
        await self.store._lock_budget(con, context)
        replay = await con.fetchval(
            "select replay_context from realtime.event where tenant_id=$1 and source_id=$2 "
            "and event_id=$3 and event_revision=$4",
            context.company_id,
            source_id,
            event_id,
            event_revision,
        )
        if not replay:
            raise Conflict("Replay bindings require an admitted replay context")
        expected_digest = replay.get("subject_binding_digest")
        if expected_digest is None:
            return None  # Retained legacy/unbound plans keep their explicit unknown association.
        original = replay["original_event"]
        actual_digest = await con.fetchval(
            "select digest from realtime.event_subject_set where tenant_id=$1 and source_id=$2 "
            "and event_id=$3 and event_revision=$4",
            context.company_id,
            original["source_id"],
            original["event_id"],
            original["event_revision"],
        )
        if actual_digest != expected_digest:
            raise Conflict("Original subject set differs from the compiled replay")
        original_bindings = await self._event_bindings(
            con, context, original["source_id"], original["event_id"], original["event_revision"]
        )
        if original_bindings is None:
            raise Conflict("Original event subjects await an explicit binding set")
        mapping = {}
        for b in replay["entity_bindings"]:
            ref = b["original"]
            key = (ref["kind"], ref["namespace"], ref["entity_id"])
            if key in mapping:
                raise Conflict("Replay entity bindings require unique explicit references")
            mapping[key] = b["replay_entity_id"]
        copied = {}

        async def copy(subject):
            old_id = subject["subject_id"]
            if old_id in copied:
                return copied[old_id]
            if subject["current_status"] == "retracted":
                raise Conflict("Original subject was retracted")
            key = (subject["kind"], subject["namespace"], old_id)
            new_id = mapping.get(key)
            if not new_id:
                raise Conflict("Compiled replay lacks an explicit subject binding")
            attrs = dict(subject["attributes"])
            parent = None
            for field in ("camera_subject_id", "visit_subject_id"):
                if attrs.get(field):
                    original_parent = await self._read(con, context, attrs[field])
                    parent = await copy(original_parent)
                    attrs[field] = parent
            meta = {k: replay[k] for k in ("run_id", "loop_id", "plan_digest")}
            meta.update(
                original_source_id=subject["source_id"],
                original_subject_id=old_id,
                original_subject_revision=subject["revision"],
            )
            body = {
                "attributes": attrs,
                "provenance": subject["provenance"],
                "replay_context": meta,
                "status": subject["status"],
            }
            digest = _digest(body)
            prior = await con.fetchval(
                "select digest from realtime.subject_revision where tenant_id=$1 and"
                " subject_id=$2 and revision=$3",
                context.company_id,
                new_id,
                subject["revision"],
            )
            if prior:
                if prior != digest:
                    raise Conflict("Replay subject revision conflicts with retained provenance")
            else:
                await self._budget(con, context, len(_encoded(body)) + 2048)
                registry_exists = await con.fetchval(
                    "select 1 from realtime.subject where tenant_id=$1 and subject_id=$2",
                    context.company_id,
                    new_id,
                )
                if not registry_exists:
                    await con.execute(
                        "insert into realtime.subject(tenant_id,source_id,subject_id,kind,namespace,"
                        "identity_digest,"
                        "parent_subject_id,original_source_id,original_subject_id) "
                        "values($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                        context.company_id,
                        source_id,
                        new_id,
                        subject["kind"],
                        replay["entity_namespace"],
                        _digest([source_id, replay["entity_namespace"], subject["kind"], old_id]),
                        parent,
                        subject["source_id"],
                        old_id,
                    )
                await con.execute(
                    "insert into realtime.subject_revision(tenant_id,source_id,subject_id,"
                    "revision,digest,status,"
                    "attributes,provenance,replay_context) values($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    context.company_id,
                    source_id,
                    new_id,
                    subject["revision"],
                    digest,
                    subject["status"],
                    attrs,
                    subject["provenance"],
                    meta,
                )
            copied[old_id] = new_id
            return new_id

        bindings = []
        for b in original_bindings:
            bindings.append(
                SubjectBinding(
                    role=b["role"],
                    subject_id=await copy(b["subject"]),
                    subject_revision=b["subject"]["revision"],
                )
            )
        return await self._bind_event(con, context, source_id, event_id, event_revision, bindings)
