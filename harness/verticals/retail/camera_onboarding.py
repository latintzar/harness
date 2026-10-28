"""RTSP-first camera onboarding data and the private credential handoff.

This module describes setup; it does not open a camera, discover a device or
claim that a stream is connected.  Public setup answers are kept in the draft
and credentials are requested through ``CustomConnectors``' existing secure
credential card.  The resulting request contains field names and status only.
"""
from __future__ import annotations

from datetime import datetime
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ONBOARDING_VERSION = "2026-10-09"
DEFAULT_PROTOCOL = "rtsp"
DEFAULT_WORKFLOW = "nvidia_deepstream_people_tracker"
DEFAULT_PRIVACY = {
    "visitor_identity_scope": "visit",
    "face_blur_default": True,
    "biometric_identity_matching": False,
    "recurring_person_profiles": False,
    "decoder_redaction": "configuration_only",
}

RTSP_CREDENTIAL_FIELDS = (
    {
        "name": "RTSP_USERNAME",
        "label": "RTSP username",
        "description": "The camera or recorder account permitted to read the selected stream.",
    },
    {
        "name": "RTSP_PASSWORD",
        "label": "RTSP password",
        "description": "The password for the selected read-only camera or recorder account.",
    },
)

# These routes remain available for systems where the customer already has a
# supported cloud or recorder integration.  They are deliberately secondary to
# the local RTSP path and do not imply that an adapter is commissioned.
ALTERNATIVE_CONNECTIONS = (
    {"id": "onvif", "label": "ONVIF discovery", "requires": "local_bridge"},
    {"id": "vendor_api", "label": "Vendor API", "requires": "commissioned_vendor_adapter"},
    {"id": "file_import", "label": "Recorded file import", "requires": "authorized_media_export"},
)


def _text(value: str, *, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"Provide {label} of up to {limit:,} characters.")
    return value.strip()


class ClockCalibration(BaseModel):
    """Source clock facts collected before timestamps are compared."""

    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(default=None, max_length=80)
    utc_offset_minutes: int | None = Field(default=None, ge=-14 * 60, le=14 * 60)
    reference_time: datetime | None = None
    method: Literal["operator_reported", "ntp_observed", "probe_pending"] = "probe_pending"

    @model_validator(mode="after")
    def has_clock_input(self):
        if self.method != "probe_pending" and self.timezone is None and self.utc_offset_minutes is None:
            raise ValueError("Give the camera clock timezone or UTC offset.")
        return self


class TopologyCalibration(BaseModel):
    """Human-entered physical placement facts; geometry is confirmed later."""

    model_config = ConfigDict(extra="forbid")
    mount: Literal["ceiling", "wall", "pole", "counter", "other"] = "other"
    direction: str = Field(default="", max_length=160)
    floor: str = Field(default="", max_length=80)
    coverage: list[str] = Field(default_factory=list, max_length=30)
    overlaps_camera_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("coverage", "overlaps_camera_ids")
    @classmethod
    def distinct_ids(cls, values):
        if len(set(values)) != len(values) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Use distinct camera or zone references.")
        return [value.strip() for value in values]


class PrivacyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_identity_scope: Literal["visit"] = "visit"
    face_blur_default: bool = True
    biometric_identity_matching: Literal[False] = False
    recurring_person_profiles: Literal[False] = False
    # This records the delivery boundary honestly.  It is not a decoder
    # implementation switch and must not be presented as completed redaction.
    decoder_redaction: Literal["configuration_only"] = "configuration_only"


class CameraOnboardingDraft(BaseModel):
    """A reviewable camera setup draft with no secret-bearing fields."""

    model_config = ConfigDict(extra="forbid")
    location_id: str = Field(min_length=1, max_length=120)
    camera_name: str = Field(min_length=1, max_length=160)
    room_name: str = Field(min_length=1, max_length=160)
    connection: Literal["rtsp", "onvif", "vendor_api", "file_import"] = DEFAULT_PROTOCOL
    stream_uri: str | None = Field(default=None, max_length=2048)
    transport: Literal["tcp", "udp"] = "tcp"
    topology: TopologyCalibration = Field(default_factory=TopologyCalibration)
    clock: ClockCalibration = Field(default_factory=ClockCalibration)
    workflow: Literal["nvidia_deepstream_people_tracker"] = DEFAULT_WORKFLOW
    privacy: PrivacyConfiguration = Field(default_factory=PrivacyConfiguration)

    @field_validator("location_id", "camera_name", "room_name")
    @classmethod
    def names(cls, value, info):
        return _text(value, label=info.field_name.replace("_", " "), limit=160)

    @field_validator("stream_uri")
    @classmethod
    def public_stream_uri(cls, value):
        if value is None:
            return value
        value = _text(value, label="an RTSP stream address", limit=2048)
        parsed = urlsplit(value)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise ValueError("Use an rtsp:// or rtsps:// stream address.")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ValueError("Keep RTSP credentials in the secure credential card.")
        if parsed.fragment or any(ord(char) < 33 for char in value):
            raise ValueError("Use a valid RTSP stream address.")
        return value

    @model_validator(mode="after")
    def connection_requirements(self):
        if self.connection == "rtsp" and not self.stream_uri:
            raise ValueError("Give the selected RTSP stream address.")
        if self.connection != "rtsp" and self.stream_uri is not None:
            raise ValueError("Keep the stream address with the RTSP connection route.")
        return self


def credential_request_contract(*, connector_id: str, owner: str, chat_id: str, collection: str, reason: str) -> dict:
    """Return metadata for the existing ``request_custom_connector_credentials`` tool.

    This intentionally does not accept or return credential values.  The host
    sends this descriptor to the existing secure modal handler.
    """
    for value, label in ((connector_id, "connector"), (owner, "owner"), (chat_id, "chat"), (collection, "collection")):
        _text(value, label=label, limit=160)
    reason = _text(reason, label="a credential request reason", limit=1000)
    return {
        "handler": "request_custom_connector_credentials",
        "connector_id": connector_id,
        "owner": owner,
        "chat_id": chat_id,
        "collection": collection,
        "reason": reason,
        "credential_entry": "private_modal",
        "fields": [dict(field) for field in RTSP_CREDENTIAL_FIELDS],
    }


def rtsp_connector_package(camera_name: str) -> dict:
    """Build a source-only connector package for the secure credential flow."""
    camera_name = _text(camera_name, label="a camera name", limit=160)
    return {
        "summary": f"Read the {camera_name} RTSP camera stream.",
        "instructions": (
            "Use the customer-selected RTSP address and the private RTSP credential fields. "
            "The address and credentials are host inputs; retain neither in chat text or source files."
        ),
        "secret_fields": [dict(field) for field in RTSP_CREDENTIAL_FIELDS],
        "files": [{
            "path": "README.md",
            "content": "RTSP camera setup is host-managed. The secure credential card supplies the read account.",
        }],
    }


def prepare_rtsp_connector(store, *, owner: str, collection: str, camera_name: str, chat_id: str | None = None) -> dict:
    """Create the prepared connector that the existing secure request handler uses."""
    record = store.create(name=f"{_text(camera_name, label='a camera name', limit=160)} RTSP",
                          description="Customer-authorized RTSP camera read connection.", kind="api",
                          collection=collection, owner=owner, chat_id=chat_id)
    return store.save_package(record["id"], rtsp_connector_package(camera_name), expected_revision=1, owner=owner)


def request_rtsp_credentials(store, connector_id: str, *, owner: str, chat_id: str, collection: str, camera_name: str) -> dict:
    """Open the existing private credential card for a prepared RTSP connector."""
    request = store.request_credentials(connector_id, owner=owner, chat_id=chat_id, collection=collection,
                                        reason=f"Enter the read credentials for { _text(camera_name, label='a camera name', limit=160) }.")
    return {"request": request, "contract": credential_request_contract(
        connector_id=connector_id, owner=owner, chat_id=chat_id, collection=collection,
        reason=request["reason"])}


def public_defaults() -> dict:
    """Return a JSON-safe copy for setup UI and API projections."""
    return {
        "version": ONBOARDING_VERSION,
        "preferred_connection": DEFAULT_PROTOCOL,
        "workflow": DEFAULT_WORKFLOW,
        "privacy": dict(DEFAULT_PRIVACY),
        "alternatives": [dict(item) for item in ALTERNATIVE_CONNECTIONS],
        "credential_entry": "private_modal",
    }


def serialize_draft(draft: CameraOnboardingDraft) -> str:
    """Stable JSON for a review record; secret values cannot enter this model."""
    return json.dumps(draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
