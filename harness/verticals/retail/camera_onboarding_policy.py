"""Retail camera setup and private credential entry policy."""
CAMERA_ONBOARDING_POLICY = {
    "preferred_connection": "rtsp",
    "workflow": "nvidia_deepstream_people_tracker",
    "credential_handler": "request_custom_connector_credentials",
    "credential_entry": "private_modal",
    "vendor_routes": ("onvif", "vendor_api", "file_import"),
    "privacy_defaults": {
        "visitor_identity_scope": "visit",
        "face_blur_default": True,
        "biometric_identity_matching": False,
        "recurring_person_profiles": False,
        "decoder_redaction": "configuration_only",
    },
}


def camera_onboarding_policy():
    """Return a defensive public copy of the RTSP-first setup policy."""
    from copy import deepcopy
    return deepcopy(CAMERA_ONBOARDING_POLICY)


