"""Small, versioned contracts shared by realtime workers and the host.

These models validate data shape and boundedness.  They do not authenticate a
principal, grant capabilities, or turn customer supplied JSON into host
authority.  Callers must obtain :class:`HostExecutionContext` from the trusted
host boundary before invoking an operation.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

EVENT_SCHEMA = "rocoto.event.v1"
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PAYLOAD_DEPTH = 8
MAX_EVENT_BYTES = 256 * 1024
MAX_REFERENCE_LENGTH = 256

AgentWakeMode = Literal["disabled", "manual", "event", "batch", "schedule"]
MatchStatus = Literal["pending", "matched", "unmatched", "unknown"]
OriginKind = Literal["observation", "business", "processing"]
OpaqueId = str


def _opaque_id(value: str) -> str:
    """Validate an opaque existing identifier without assigning it semantics."""

    if "\x00" in value:
        raise ValueError("identifier contains a NUL character")
    return value


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit UTC offset")
    return value.astimezone(timezone.utc)


def _finite_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("source seconds must be finite")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionBounds(ContractModel):
    """Hard limits carried with a host execution request."""

    max_events: int = Field(default=1000, ge=1, le=100_000)
    max_payload_bytes: int = Field(default=MAX_PAYLOAD_BYTES, ge=1, le=MAX_PAYLOAD_BYTES)
    max_payload_depth: int = Field(default=MAX_PAYLOAD_DEPTH, ge=1, le=32)
    max_output_bytes: int = Field(default=1_048_576, ge=1, le=16 * 1024 * 1024)


class HostExecutionContext(ContractModel):
    """Opaque host request context; validation here is not authentication."""

    company_id: OpaqueId = Field(min_length=1, max_length=256)
    principal_id: OpaqueId = Field(min_length=1, max_length=256)
    request_id: OpaqueId = Field(min_length=1, max_length=256)
    conversation_id: OpaqueId = Field(min_length=1, max_length=256)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    deadline_utc: datetime
    bounds: ExecutionBounds = Field(default_factory=ExecutionBounds)

    _validate_ids = field_validator(
        "company_id", "principal_id", "request_id", "conversation_id", mode="after"
    )(_opaque_id)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, values: frozenset[str]) -> frozenset[str]:
        for capability in values:
            if not capability or "\x00" in capability or len(capability) > 128:
                raise ValueError("capabilities must be non-empty bounded strings")
        return values

    @field_validator("deadline_utc", mode="after")
    @classmethod
    def _validate_deadline(cls, value: datetime) -> datetime:
        return _utc_datetime(value)


class SourceWindow(ContractModel):
    """Half-open source-media interval in decimal source seconds."""

    start_s: Decimal = Field(ge=Decimal("0"))
    end_s: Decimal = Field(gt=Decimal("0"))

    @field_validator("start_s", "end_s", mode="after")
    @classmethod
    def _validate_finite(cls, value: Decimal) -> Decimal:
        return _finite_decimal(value)

    @model_validator(mode="after")
    def _validate_order(self) -> "SourceWindow":
        if self.end_s <= self.start_s:
            raise ValueError("source window must be half-open with end_s > start_s")
        return self


class TrackingContinuity(ContractModel):
    """Host-bound mapping from one immutable artifact into its tracker epoch."""

    epoch_id: OpaqueId = Field(min_length=1, max_length=256)
    mapping_id: OpaqueId = Field(min_length=1, max_length=256)
    stream_window: SourceWindow
    _validate_ids = field_validator("epoch_id", "mapping_id", mode="after")(_opaque_id)


class ObservationProvenance(ContractModel):
    kind: Literal["observation"] = "observation"
    source_asset_id: OpaqueId = Field(min_length=1, max_length=256)
    source_window: SourceWindow
    actual_receipt_refs: list[OpaqueId] = Field(min_length=1, max_length=32)
    profile_revision_id: OpaqueId = Field(min_length=1, max_length=256)
    continuity: TrackingContinuity | None = None

    @model_serializer(mode="wrap")
    def _preserve_recording_wire(self, handler):
        value = handler(self)
        if value.get("continuity") is None:
            value.pop("continuity", None)
        return value

    _validate_ids = field_validator("source_asset_id", "profile_revision_id", mode="after")(_opaque_id)

    @field_validator("actual_receipt_refs")
    @classmethod
    def _validate_receipts(cls, values: list[str]) -> list[str]:
        if any(not value or "\x00" in value or len(value) > MAX_REFERENCE_LENGTH for value in values):
            raise ValueError("receipt references must be non-empty bounded opaque identifiers")
        return values


class BusinessProvenance(ContractModel):
    """Connector/business lineage; it intentionally has no media fields."""

    kind: Literal["business"] = "business"
    connector_ref: OpaqueId = Field(min_length=1, max_length=256)
    external_identity: OpaqueId = Field(min_length=1, max_length=256)
    effective_at_utc: datetime

    _validate_ids = field_validator("connector_ref", "external_identity", mode="after")(_opaque_id)

    @field_validator("effective_at_utc", mode="after")
    @classmethod
    def _validate_effective_at(cls, value: datetime) -> datetime:
        return _utc_datetime(value)


# Public alias makes connector wording explicit to callers while retaining a
# stable business origin kind in the envelope.
ConnectorProvenance = BusinessProvenance


class InputEventReference(ContractModel):
    event_id: OpaqueId = Field(min_length=1, max_length=256)
    event_revision: int = Field(ge=1, le=2_147_483_647)

    _validate_id = field_validator("event_id", mode="after")(_opaque_id)


class ProcessingProvenance(ContractModel):
    kind: Literal["processing"] = "processing"
    processing_revision_id: OpaqueId = Field(min_length=1, max_length=256)
    input_event_refs: list[InputEventReference] = Field(min_length=1, max_length=10_000)

    _validate_ids = field_validator("processing_revision_id", mode="after")(_opaque_id)

    @field_validator("input_event_refs")
    @classmethod
    def _validate_event_refs(cls, values: list[InputEventReference]) -> list[InputEventReference]:
        keys = [(value.event_id, value.event_revision) for value in values]
        if len(keys) != len(set(keys)):
            raise ValueError("processing references must identify distinct event revisions")
        return values


Provenance = Union[ObservationProvenance, BusinessProvenance, ProcessingProvenance]


def _validate_json_value(value: Any, *, depth: int = 1, ancestors: set[int] | None = None) -> None:
    """Validate JSON shape, finite numbers, depth, and cycles before encoding."""

    if depth > MAX_PAYLOAD_DEPTH:
        raise ValueError(f"payload exceeds maximum depth of {MAX_PAYLOAD_DEPTH}")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload contains a non-finite number")
        return
    if not isinstance(value, (list, dict)):
        raise ValueError("payload must contain JSON-compatible values")

    active = ancestors if ancestors is not None else set()
    marker = id(value)
    if marker in active:
        raise ValueError("payload contains a cyclic container")
    active.add(marker)
    try:
        children = value if isinstance(value, list) else value.items()
        for item in children:
            if isinstance(value, dict):
                key, child = item
                if not isinstance(key, str):
                    raise ValueError("payload object keys must be strings")
            else:
                child = item
            _validate_json_value(child, depth=depth + 1, ancestors=active)
    finally:
        active.remove(marker)


def _encoded_json(value: Any, *, label: str) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc


class CanonicalEvent(ContractModel):
    """Bounded event envelope with exclusive provenance by origin kind."""

    schema_version: Literal["rocoto.event.v1"] = Field(EVENT_SCHEMA, alias="schema")
    event_id: OpaqueId = Field(min_length=1, max_length=256)
    event_revision: int = Field(ge=1, le=2_147_483_647)
    event_type: str = Field(min_length=1, max_length=128)
    origin_kind: OriginKind
    event_state: Literal["active", "retracted"] = "active"
    occurred_at_utc: datetime | None = None
    ingested_at_utc: datetime
    provenance: Provenance = Field(discriminator="kind")
    payload: dict[str, Any]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    _validate_event_id = field_validator("event_id", mode="after")(_opaque_id)

    @field_validator("occurred_at_utc", "ingested_at_utc", mode="after")
    @classmethod
    def _validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc_datetime(value)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        if "\x00" in value or value.strip() != value:
            raise ValueError("event_type must be a bounded registered name")
        return value

    @model_serializer(mode="wrap")
    def _serialize_event(self, handler):
        value = handler(self)
        # v1 active events predate the explicit retraction field. Preserve their
        # immutable wire digest, including retries from newer host versions.
        if value.get("event_state") == "active":
            value.pop("event_state")
        return value

    @model_validator(mode="after")
    def _validate_envelope(self) -> "CanonicalEvent":
        if self.event_state == "retracted" and self.event_revision < 2:
            raise ValueError("a retraction must follow an admitted event revision")
        if self.origin_kind == "business" and self.occurred_at_utc is None:
            raise ValueError("business events require their actual effective UTC time")
        if self.provenance.kind != self.origin_kind:
            raise ValueError("provenance kind must match origin_kind")
        _validate_json_value(self.payload)
        encoded = _encoded_json(self.payload, label="payload")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} UTF-8 bytes")
        envelope = _encoded_json(self.model_dump(mode="json", by_alias=True), label="event envelope")
        if len(envelope) > MAX_EVENT_BYTES:
            raise ValueError(f"event envelope exceeds {MAX_EVENT_BYTES} UTF-8 bytes")
        return self


class ExecutionPolicy(ContractModel):
    """Independent execution switches; changing policy does not execute it."""

    observation_enabled: bool = False
    processing_enabled: bool = False
    agent_wake_mode: AgentWakeMode = "disabled"
    delivery_enabled: bool = False
    expected_revision: int = Field(ge=1, le=2_147_483_647)


class ConfigurationOperation(ContractModel):
    """A hosted configuration/edit operation, with no implicit run operation."""

    mode: Literal["configure", "edit_only"]
    expected_revision: int = Field(ge=1, le=2_147_483_647)
    changes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("changes")
    @classmethod
    def _validate_changes(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_json_value(value)
        encoded = _encoded_json(value, label="configuration changes")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"configuration changes exceed {MAX_PAYLOAD_BYTES} UTF-8 bytes")
        return value
