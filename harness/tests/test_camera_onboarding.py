"""RTSP-first onboarding keeps setup public and credentials in the secure card."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from camera_onboarding import (
    CameraOnboardingDraft,
    credential_request_contract,
    prepare_rtsp_connector,
    public_defaults,
    request_rtsp_credentials,
    serialize_draft,
)
from custom_connectors import CustomConnectors
from camera_onboarding_policy import camera_onboarding_policy


def draft(**changes):
    value = {
        "location_id": "location-1",
        "camera_name": "Front corner",
        "room_name": "Shop floor",
        "stream_uri": "rtsps://camera.local:7441/live/main?channel=1",
        "topology": {"mount": "ceiling", "direction": "toward entry", "coverage": ["entry"]},
        "clock": {"timezone": "Europe/Sofia", "method": "operator_reported"},
    }
    value.update(changes)
    return value


def test_rtsp_is_default_and_calibration_is_retained():
    result = CameraOnboardingDraft.model_validate(draft())
    assert result.connection == "rtsp"
    assert result.transport == "tcp"
    assert result.workflow == "nvidia_deepstream_people_tracker"
    assert result.topology.coverage == ["entry"]
    assert result.clock.timezone == "Europe/Sofia"
    assert result.privacy.visitor_identity_scope == "visit"
    assert result.privacy.face_blur_default is True
    assert result.privacy.decoder_redaction == "configuration_only"
    assert "RTSP_PASSWORD" not in serialize_draft(result)


def test_stream_uri_rejects_embedded_credentials_and_non_rtsp():
    for uri in ["rtsps://user:password@camera.local/live", "https://camera.local/live"]:
        with pytest.raises(ValueError):
            CameraOnboardingDraft.model_validate(draft(stream_uri=uri))
    with pytest.raises(ValueError):
        CameraOnboardingDraft.model_validate(draft(connection="onvif", stream_uri="rtsps://camera.local/live"))


def test_secure_request_contract_has_metadata_only():
    contract = credential_request_contract(
        connector_id="custom-camera", owner="alice", chat_id="setup-chat", collection="cafe",
        reason="Enter the camera read account.",
    )
    assert contract["handler"] == "request_custom_connector_credentials"
    assert contract["credential_entry"] == "private_modal"
    assert [field["name"] for field in contract["fields"]] == ["RTSP_USERNAME", "RTSP_PASSWORD"]
    assert "private-password" not in json.dumps(contract)


def test_prepared_connector_routes_credentials_through_existing_store(tmp_path):
    store = CustomConnectors(tmp_path)
    record = prepare_rtsp_connector(store, owner="alice", collection="cafe", camera_name="Front corner",
                                    chat_id="setup-chat")
    assert record["status"] == "prepared"
    assert {field["name"] for field in record["secret_fields"]} == {"RTSP_USERNAME", "RTSP_PASSWORD"}
    result = request_rtsp_credentials(store, record["id"], owner="alice", chat_id="setup-chat",
                                      collection="cafe", camera_name="Front corner")
    assert result["request"]["status"] == "pending"
    assert result["contract"]["handler"] == "request_custom_connector_credentials"
    serialized = json.dumps(result["request"])
    assert "private-password" not in serialized and "private-username" not in serialized


def test_defaults_keep_vendor_routes_as_secondary_choices():
    defaults = public_defaults()
    assert defaults["preferred_connection"] == "rtsp"
    assert defaults["workflow"] == "nvidia_deepstream_people_tracker"
    assert {item["id"] for item in defaults["alternatives"]} == {"onvif", "vendor_api", "file_import"}
    policy = camera_onboarding_policy()
    policy["privacy_defaults"]["face_blur_default"] = False
    assert camera_onboarding_policy()["privacy_defaults"]["face_blur_default"] is True
