"""Host-reviewed migration of admitted stream history through the maintained SDK.

This adapter reads immutable SQLite records, never model callbacks or evaluation
labels. Export is read-only; import requires an authenticated host context and a
separately reviewed checksum. It neither provisions tenants nor enables workers.
The retained export is the private raw-receipt artifact referenced by DB receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from realtime.contracts import CanonicalEvent
from realtime.store import AccessDenied, CapacityExceeded, Conflict, RealTimeStore, _digest
from realtime.subjects import ATTRIBUTES, SubjectBinding, SubjectRegistration, SubjectStore

SCHEMA = "rocoto.admitted-stream-export.v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_WINDOWS = 16
MAX_EVENTS = 1000
MAX_NATIVE_BYTES = 4_000_000
EVENT_FIELDS = (
    "profile_id",
    "profile_revision",
    "event_key",
    "type",
    "description",
    "source",
    "track_id",
    "object_ref",
    "object_description",
    "bbox",
)


def canonical(value):
    # Match the original adapter's canonical encoding, including Unicode escapes.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def raw_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def checked_json(value, maximum=MAX_NATIVE_BYTES):
    if not isinstance(value, str) or len(value.encode()) > maximum:
        raise CapacityExceeded("Retained export record exceeds its byte allowance")
    return json.loads(value)


def identity_record(value):
    keys = {"schema_version", "company_scope", "onboarding_seed", "origin", "created_at"}
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != 1:
        raise Conflict("Supply the saved, host-verified workspace identity")
    company, seed = value["company_scope"], value["onboarding_seed"]
    if not isinstance(company, str) or not re.fullmatch(r"[a-f0-9]{64}", company):
        raise Conflict("Preserve the exact workspace company scope")
    if value["origin"] == "new_company":
        valid = seed == "workspace-" + company
    elif value["origin"] == "legacy_path_adoption":
        valid = isinstance(seed, str) and raw_digest(seed) == company
    else:
        valid = False
    if not valid or datetime.fromisoformat(value["created_at"]).utcoffset() is None:
        raise Conflict("Workspace identity provenance does not match its company scope")
    return value


def _read_db(path):
    if not path.is_file() or path.is_symlink():
        raise Conflict("Select a regular host-owned snapshot database")
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
    con.execute("begin")
    return con


def _has_table(con, name):
    return (
        con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
        is not None
    )


def _reference(event):
    return any(
        event.get(k) == v
        for k, v in (
            ("origin", "dataset_reference"),
            ("observation_origin", "dataset_reference"),
            ("review_state", "publisher_annotation"),
        )
    )


def _profile_key(profile_id, revision):
    # A revision-qualified storage key; the original opaque ID/revision are also retained verbatim.
    if not isinstance(profile_id, str) or not profile_id or type(revision) is not int or revision < 1:
        raise Conflict("Retain the original profile identity and revision")
    result = profile_id + "@" + str(revision)
    if len(result) > 256:
        raise Conflict("Profile revision identifier exceeds the SDK reference bound")
    return result


def export_snapshot(state, *, identity_path, owner, source_ids, export_reference):
    """Read committed native admissions from a host-selected private snapshot.

    The company identity path is explicit because commissioning snapshots may
    deliberately omit account state. Environment/display names confer no scope.
    """
    state, identity_path = Path(state), Path(identity_path)
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,149}", owner):
        raise ValueError("Use the authenticated saved owner identity")
    if (
        not isinstance(source_ids, (list, tuple))
        or not 1 <= len(source_ids) <= 16
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("Select up to 16 distinct registered sources")
    if identity_path.is_symlink() or identity_path.stat().st_size > 8192:
        raise Conflict("Supply a bounded regular workspace identity record")
    identity = identity_record(checked_json(identity_path.read_text(), 8192))
    if not isinstance(export_reference, str) or not 1 <= len(export_reference) <= 512:
        raise ValueError("Supply a retained host export reference")
    stream = _read_db(state / "stream-ingestion.sqlite")
    monitoring = _read_db(state / "monitoring.sqlite")
    location = (
        _read_db(state / "location-intelligence.sqlite")
        if (state / "location-intelligence.sqlite").exists()
        else None
    )
    try:
        body = {
            "identity": identity,
            "owner": owner,
            "source_ids": list(source_ids),
            "export_reference": export_reference,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "sources": [],
            "profiles": [],
            "windows": [],
            "visits": [],
            "inventory": {
                "reference_windows_excluded": 0,
                "pending_windows_excluded": 0,
                "unreceipted_pipeline_windows_excluded": 0,
                "quarantined_native_attempts_excluded": 0,
            },
        }
        profiles, wanted_visits = {}, set()
        for sid in source_ids:
            if _has_table(stream, "cosmos_bridge_attempts"):
                body["inventory"]["quarantined_native_attempts_excluded"] += stream.execute(
                    "select count(*) from cosmos_bridge_attempts "
                    "where owner=? and source_id=? and status='quarantined'",
                    (owner, sid),
                ).fetchone()[0]
            source = monitoring.execute(
                "select body from monitoring_sources where owner=? and id=?", (owner, sid)
            ).fetchone()
            if source is None:
                raise AccessDenied("Selected source belongs to the supplied saved owner")
            body["sources"].append(checked_json(source["body"]))
            rows = stream.execute(
                "select * from stream_windows where owner=? and source=? order by start,end,id", (owner, sid)
            )
            for row in rows:
                if row["status"] != "committed":
                    body["inventory"]["pending_windows_excluded"] += 1
                    continue
                spec = checked_json(row["body"])
                if spec.get("observation_origin") != "pipeline_observation":
                    body["inventory"]["reference_windows_excluded"] += 1
                    continue
                model = stream.execute(
                    "select * from stream_model_outputs where window_id=? and owner=?", (row["id"], owner)
                ).fetchone()
                adapter = stream.execute(
                    "select * from stream_response_adapters where window_id=? and owner=?", (row["id"], owner)
                ).fetchone()
                admitted = bool(
                    adapter
                    and _has_table(stream, "cosmos_bridge_attempts")
                    and stream.execute(
                        "select 1 from cosmos_bridge_attempts where owner=? and source_id=? "
                        "and status='admitted' and body=?",
                        (owner, sid, adapter["body"]),
                    ).fetchone()
                )
                if not model or not adapter or not admitted:
                    body["inventory"]["unreceipted_pipeline_windows_excluded"] += 1
                    continue
                event_ids = checked_json(row["event_ids"])
                events = []
                for event_id in event_ids:
                    saved = monitoring.execute(
                        "select * from monitoring_events where id=? and owner=? and source_id=?",
                        (event_id, owner, sid),
                    ).fetchone()
                    if saved is None:
                        raise Conflict("Committed stream window references an unavailable saved event")
                    event = checked_json(saved["body"])
                    if _reference(event) or event.get("origin") != "pipeline_observation":
                        raise Conflict("Primary stream admission contains a reference observation")
                    events.append({"record": event, "fingerprint": saved["fingerprint"]})
                    if event.get("visit_id"):
                        wanted_visits.add(event["visit_id"])
                profile_id, revision = spec["profile_id"], spec["profile_revision"]
                key = _profile_key(profile_id, revision)
                profile = monitoring.execute(
                    "select body from profile_revisions where id=? and revision=?", (profile_id, revision)
                ).fetchone()
                if profile is None:
                    raise Conflict("Retain the admitted profile revision")
                profiles[key] = checked_json(profile["body"])
                body["windows"].append(
                    {
                        "record": dict(row),
                        "model_output": dict(model),
                        "adapter": dict(adapter),
                        "events": events,
                    }
                )
                if (
                    len(body["windows"]) > MAX_WINDOWS
                    or sum(len(w["events"]) for w in body["windows"]) > MAX_EVENTS
                ):
                    raise CapacityExceeded("Export selected history in bounded batches")
                if len(canonical(body).encode()) > MAX_BYTES:
                    raise CapacityExceeded("Export exceeds its retained byte allowance")
        body["profiles"] = [profiles[key] for key in sorted(profiles)]
        if location:
            for key in sorted(wanted_visits):
                row = location.execute(
                    "select body from records where owner=? and kind='tracked_visit' and id=?", (owner, key)
                ).fetchone()
                if row:
                    body["visits"].append(checked_json(row["body"]))
        sections = {
            key: digest(body[key]) for key in ("identity", "sources", "profiles", "windows", "visits")
        }
        result = {"schema": SCHEMA, "body": body, "section_sha256": sections}
        result["sha256"] = digest(result)
        validate_export(result, expected_sha256=result["sha256"])
        return result
    finally:
        stream.close()
        monitoring.close()
        if location:
            location.close()


def validate_export(manifest, *, expected_sha256):
    """Validate a reviewed neutral artifact; its checksum is integrity, not authorization."""
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "body", "section_sha256", "sha256"}:
        raise Conflict("Use the versioned admitted-stream export")
    if len(canonical(manifest).encode()) > MAX_BYTES:
        raise CapacityExceeded("Export exceeds its byte allowance")
    if (
        manifest["schema"] != SCHEMA
        or manifest["sha256"] != expected_sha256
        or digest({k: v for k, v in manifest.items() if k != "sha256"}) != expected_sha256
    ):
        raise Conflict("Export differs from the separately reviewed checksum")
    body = manifest["body"]
    if set(body) != {
        "identity",
        "owner",
        "source_ids",
        "export_reference",
        "exported_at",
        "sources",
        "profiles",
        "windows",
        "visits",
        "inventory",
    }:
        raise Conflict("Retain the complete bounded export contract")
    identity_record(body["identity"])
    if set(manifest["section_sha256"]) != {"identity", "sources", "profiles", "windows", "visits"}:
        raise Conflict("Retain every section checksum")
    if any(digest(body[k]) != value for k, value in manifest["section_sha256"].items()):
        raise Conflict("Export section differs from its recorded checksum")
    if (
        not 1 <= len(body["sources"]) <= 16
        or len(body["windows"]) > MAX_WINDOWS
        or len(body["visits"]) > MAX_EVENTS
    ):
        raise CapacityExceeded("Export record counts exceed the import allowance")
    sources = {s["id"]: s for s in body["sources"]}
    if len(sources) != len(body["sources"]) or set(sources) != set(body["source_ids"]):
        raise Conflict("Keep each registered source exactly once")
    if len({s.get("camera_id") for s in sources.values()}) != len(sources):
        raise Conflict("Shared-camera recordings require a reviewed multi-source subject migration")
    for source in sources.values():
        if not all(
            isinstance(source.get(k), str) and 1 <= len(source[k]) <= 160
            for k in ("id", "video_id", "camera_id")
        ):
            raise Conflict("Preserve registered source, asset and camera identifiers")
        if not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", "")):
            raise Conflict("Preserve registered source byte hashes")
    if len({v["id"] for v in body["visits"]}) != len(body["visits"]):
        raise Conflict("Keep each saved visit identity exactly once")
    profiles = {_profile_key(p["id"], p["revision"]): p for p in body["profiles"]}
    if len(profiles) != len(body["profiles"]):
        raise Conflict("Keep each profile revision exactly once")
    for profile in profiles.values():
        if profile.get("owner_principal_id") != body["owner"]:
            raise AccessDenied("Profile revision belongs to the saved owner")
    event_ids, windows = set(), set()
    for bundle in body["windows"]:
        row, model, adapter = bundle["record"], bundle["model_output"], bundle["adapter"]
        spec = checked_json(row["body"])
        sid = row["source"]
        source = sources.get(sid)
        if (
            not source
            or row["status"] != "committed"
            or row["owner"] != body["owner"]
            or row["id"] in windows
        ):
            raise Conflict("Import distinct committed source windows for the saved owner")
        windows.add(row["id"])
        if (
            spec["observation_origin"] != "pipeline_observation"
            or spec["source_id"] != sid
            or raw_digest(row["body"]) != row["fingerprint"]
        ):
            raise Conflict("Retain the exact primary observation window")
        if (row["start"], row["end"]) != (spec["start_s"], spec["end_s"]) or not source["start"] <= row[
            "start"
        ] < row["end"] <= source["end"]:
            raise Conflict("Preserve the original registered source clock")
        if (
            model["owner"] != body["owner"]
            or adapter["owner"] != body["owner"]
            or model["window_id"] != row["id"]
            or adapter["window_id"] != row["id"]
        ):
            raise Conflict("Raw receipts retain their source window and owner")
        if (
            raw_digest(model["raw"]) != model["response_sha256"]
            or model["source_sha256"] != source["sha256"]
            or raw_digest(adapter["body"]) != adapter["receipt_sha256"]
        ):
            raise Conflict("Original model, adapter or source receipt hash differs")
        native = checked_json(adapter["body"])
        prepared = native["prepared"]
        if (
            digest(native["envelope"]) != native["envelope_sha256"]
            or digest(native["input_trace"]) != native["input_trace_sha256"]
        ):
            raise Conflict("Preserve the original native response and actual input trace")
        profile = profiles.get(_profile_key(spec["profile_id"], spec["profile_revision"]))
        if (
            profile is None
            or prepared["profile_sha256"] != digest(profile)
            or prepared["source_sha256"] != source["sha256"]
        ):
            raise Conflict("Native input must match the saved source and profile")
        if any(
            prepared["context"].get(k) != spec[k]
            for k in ("source_id", "profile_id", "profile_revision", "start_s", "end_s")
        ):
            raise Conflict("Native preparation preserves the host source/profile clock contract")
        tracker = prepared["tracker_receipt"]
        if (
            digest(tracker) != prepared["tracker_sha256"]
            or tracker.get("source_id") != sid
            or tracker.get("source_sha256") != source["sha256"]
            or tracker.get("camera_id") != source["camera_id"]
            or tracker.get("identity_scope") != "recording_and_camera_local"
        ):
            raise Conflict("Tracker receipt preserves recording and camera-local identity")
        raw = checked_json(model["raw"])
        if raw.get("observations") != spec["observations"] or raw.get("item_claims", []) != spec.get(
            "item_claims", []
        ):
            raise Conflict("Canonical stream observations differ from the admitted response")
        saved_ids = [e["record"]["id"] for e in bundle["events"]]
        if saved_ids != checked_json(row["event_ids"]) or len(saved_ids) != len(set(saved_ids)):
            raise Conflict("Preserve the exact committed event references")
        by_key = {e["event_key"]: e for e in spec["observations"]}
        if len(by_key) != len(bundle["events"]):
            raise Conflict("Each admitted observation retains its saved event")
        for saved in bundle["events"]:
            event = saved["record"]
            if (
                event["id"] in event_ids
                or _reference(event)
                or event.get("origin") != "pipeline_observation"
                or event.get("owner_principal_id") != body["owner"]
            ):
                raise Conflict("Import distinct primary admitted observations")
            event_ids.add(event["id"])
            original = {key: event.get(key) for key in EVENT_FIELDS}
            if (
                original != by_key.get(event["event_key"])
                or raw_digest(json.dumps([original, event["origin"]], sort_keys=True)) != saved["fingerprint"]
            ):
                raise Conflict("Saved event differs from its admitted fingerprint")
            if (
                event["source"]["clip_id"] != sid
                or event["source_sha256"] != source["sha256"]
                or event["camera_id"] != source["camera_id"]
            ):
                raise Conflict("Event preserves the host source identity")
            if event.get("captured_at") is not None and not (
                source.get("capture_start_utc") or source.get("source_capture_start_utc")
            ):
                raise Conflict("Calendar timestamps require a recorded source clock")
            if not spec["start_s"] <= event["source"]["start_s"] < event["source"]["end_s"] <= spec["end_s"]:
                raise Conflict("Use saved nonempty event intervals; point events require a reviewed adapter")
    if len(event_ids) > MAX_EVENTS:
        raise CapacityExceeded("Admitted event count exceeds the import allowance")
    return body


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def plan_import(manifest, *, expected_sha256):
    """Create inspectable SDK commands; empty exports remain explicitly empty."""
    body = validate_export(manifest, expected_sha256=expected_sha256)
    sources = {s["id"]: s for s in body["sources"]}
    visits = {v["id"]: v for v in body["visits"]}
    plan = {
        "company_id": body["identity"]["company_scope"],
        "owner": body["owner"],
        "sha256": expected_sha256,
        "sources": body["sources"],
        "profiles": [],
        "receipts": [],
        "subjects": [],
        "events": [],
        "unresolved_actors": [],
    }
    seen_subjects, seen_profiles = set(), set()
    profiles = {_profile_key(p["id"], p["revision"]): p for p in body["profiles"]}
    for bundle in body["windows"]:
        row, adapter = bundle["record"], bundle["adapter"]
        spec, native = checked_json(row["body"]), checked_json(adapter["body"])
        source = sources[row["source"]]
        revision = _profile_key(spec["profile_id"], spec["profile_revision"])
        if (source["id"], revision) not in seen_profiles:
            plan["profiles"].append(
                {
                    "source_id": source["id"],
                    "revision_id": revision,
                    "event_types": [v["type"] for v in profiles[revision]["vocabulary"]],
                }
            )
            seen_profiles.add((source["id"], revision))
        receipt_id = "stream-input-" + row["id"]
        receipt = {
            "kind": "actual_input",
            "source_asset_id": source["video_id"],
            "profile_revision_id": revision,
            "source_window": {"start_s": spec["start_s"], "end_s": spec["end_s"]},
            "source_sha256": source["sha256"],
            "received_at": _utc(row["received"]),
            "legacy_profile_id": spec["profile_id"],
            "legacy_profile_revision": spec["profile_revision"],
            "model_response_sha256": bundle["model_output"]["response_sha256"],
            "native_receipt_sha256": adapter["receipt_sha256"],
            "native_input_trace_sha256": native["input_trace_sha256"],
            "native_receipt_ref": "sha256:" + adapter["receipt_sha256"],
            "profile_sha256": digest(profiles[revision]),
            "legacy_window_id": row["id"],
        }
        plan["receipts"].append({"source_id": source["id"], "receipt_id": receipt_id, "body": receipt})
        camera = source["camera_id"]
        if camera not in seen_subjects:
            registration = {
                "subject_id": camera,
                "source_id": source["id"],
                "kind": "camera",
                "namespace": "camera",
                "attributes": {"camera_id": camera},
                "provenance": {"reference": body["export_reference"], "sha256": digest(source)},
            }
            plan["subjects"].append(SubjectRegistration.model_validate(registration).model_dump(mode="json"))
            seen_subjects.add(camera)
        for saved in bundle["events"]:
            original = saved["record"]
            visit = visits.get(original.get("visit_id"))
            bindings = []
            tracker = native["prepared"]["tracker_receipt"]
            exact = bool(
                visit
                and visit.get("source_id") == source["id"]
                and visit.get("camera_id") == camera
                and visit.get("recording_id") == source["video_id"]
                and visit.get("person_id") == original.get("track_id")
                and visit.get("provenance", {}).get("source_sha256") == source["sha256"]
                and visit.get("provenance", {}).get("identity_scope") == "recording_and_camera_local"
            )
            observed = any(
                original["source"]["start_s"] <= f["video_time_s"] < original["source"]["end_s"]
                and any(str(t["track_id"]) == original.get("track_id") for t in f.get("tracks", []))
                for f in tracker["frames"]
            )
            if exact and observed:
                if visit["id"] not in seen_subjects:
                    registration = {
                        "subject_id": visit["id"],
                        "source_id": source["id"],
                        "kind": "visit",
                        "namespace": "recording:" + source["video_id"],
                        "attributes": {
                            "camera_subject_id": camera,
                            "source_asset_id": source["video_id"],
                            "tracker_namespace": str(
                                visit["provenance"].get("tracker") or "recording-local-tracker"
                            ),
                            "tracker_id": visit["person_id"],
                            "visit_boundary_id": visit["id"],
                        },
                        "provenance": {
                            "reference": body["export_reference"],
                            "sha256": _digest(receipt),
                            "receipt_id": receipt_id,
                            "source_start_s": original["source"]["start_s"],
                            "source_end_s": original["source"]["end_s"],
                        },
                    }
                    plan["subjects"].append(
                        SubjectRegistration.model_validate(registration).model_dump(mode="json")
                    )
                    seen_subjects.add(visit["id"])
                bindings = [
                    SubjectBinding(role="actor", subject_id=visit["id"], subject_revision=1).model_dump()
                ]
            elif original.get("track_id") is not None:
                plan["unresolved_actors"].append(original["id"])
            event = CanonicalEvent.model_validate(
                {
                    "event_id": original["id"],
                    "event_revision": 1,
                    "event_type": original["type"],
                    "origin_kind": "observation",
                    "occurred_at_utc": original.get("captured_at"),
                    "ingested_at_utc": _utc(original["committed_at"]),
                    "provenance": {
                        "kind": "observation",
                        "source_asset_id": source["video_id"],
                        "source_window": {
                            "start_s": original["source"]["start_s"],
                            "end_s": original["source"]["end_s"],
                        },
                        "actual_receipt_refs": [receipt_id],
                        "profile_revision_id": revision,
                    },
                    "payload": {
                        "description": original["description"],
                        "object_description": original.get("object_description"),
                        "legacy_event_key": original["event_key"],
                        "legacy_fingerprint": saved["fingerprint"],
                        "source_observation_origin": "pipeline_observation",
                        "observation_local": {
                            "track_id": original.get("track_id"),
                            "object_ref": original.get("object_ref"),
                            "bbox": original.get("bbox"),
                        },
                        "input_kind": spec["input_kind"],
                    },
                }
            )
            plan["events"].append(
                {
                    "source_id": source["id"],
                    "event": event.model_dump(mode="json", by_alias=True),
                    "bindings": bindings,
                }
            )
    return plan


async def import_export(
    context, manifest, *, expected_sha256, dry_run=True, retained_export_path=None, store=None
):
    """Apply idempotent SDK phases after host identity commissioning and review.

    Event admission and its sealed subject associations share one transaction.
    Earlier source/profile/receipt phases remain safe to retry after interruption.
    """
    plan = plan_import(manifest, expected_sha256=expected_sha256)
    if context.company_id != plan["company_id"] or context.principal_id != plan["owner"]:
        raise AccessDenied(
            "Reviewed export and authenticated host context must select the same company and owner"
        )
    receipt = {
        "schema": "rocoto.stream-import-receipt.v1",
        "export_sha256": expected_sha256,
        "company_id": context.company_id,
        "owner": context.principal_id,
        "dry_run": dry_run,
        "counts": {k: len(plan[k]) for k in ("sources", "profiles", "receipts", "subjects", "events")},
        "unresolved_actors": plan["unresolved_actors"],
        "accepted": 0,
        "duplicates": 0,
    }
    if dry_run:
        return receipt
    if retained_export_path is None:
        raise Conflict("Retain the reviewed raw-receipt export before importing history")
    retained = Path(retained_export_path)
    if retained.is_symlink() or not retained.is_file() or retained.stat().st_size > MAX_BYTES:
        raise Conflict("Use a bounded regular retained export artifact")
    validate_export(checked_json(retained.read_text(), MAX_BYTES), expected_sha256=expected_sha256)
    receipt["retained_export_path"] = str(retained.resolve())
    store = store or RealTimeStore()
    subjects = SubjectStore(store)
    for source in plan["sources"]:
        await store.register_source(context, source["id"], metadata={"legacy_registered_source": source})
    for profile in plan["profiles"]:
        await store.register_profile(
            context, profile["source_id"], profile["revision_id"], profile["event_types"]
        )
    for item in plan["receipts"]:
        await store.record_receipt(context, item["source_id"], item["receipt_id"], item["body"])
    for subject in plan["subjects"]:
        subject["attributes"] = (
            ATTRIBUTES[subject["kind"]].model_validate(subject["attributes"]).model_dump(mode="json")
        )
        async with store._transaction(context, "realtime:read") as con:
            exists = await con.fetchval(
                "select 1 from realtime.subject where tenant_id=$1 and subject_id=$2",
                context.company_id,
                subject["subject_id"],
            )
        if exists:
            prior = await subjects.get(context, subject["subject_id"], 1)
            if any(prior[key] != subject[key] for key in ("source_id", "namespace", "kind", "attributes")):
                raise Conflict("Imported subject identity collides with retained history")
        else:
            await subjects.register(context, subject)
    for item in plan["events"]:
        event = CanonicalEvent.model_validate(item["event"])
        async with store._transaction(context, "realtime:ingest") as con:
            result = await store._admit(con, context, item["source_id"], event)
            if result.status not in {"accepted", "duplicate"}:
                raise Conflict("Reviewed event failed admission; its event and bindings were rolled back")
            await subjects._bind_event(
                con, context, item["source_id"], event.event_id, event.event_revision, item["bindings"]
            )
            receipt["accepted" if result.status == "accepted" else "duplicates"] += 1
    return receipt


def write_export(path, manifest):
    """Retain private raw admission evidence before using the import service."""
    import os

    encoded = canonical(manifest).encode()
    validate_export(manifest, expected_sha256=manifest["sha256"])
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    return {"path": str(path.resolve()), "bytes": len(encoded), "sha256": manifest["sha256"]}


def main():
    """Read-only source export CLI; database import stays an authenticated host call."""
    import argparse

    parser = argparse.ArgumentParser(description="Export admitted private stream history for review")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = export_snapshot(
        args.state,
        identity_path=args.identity,
        owner=args.owner,
        source_ids=args.source,
        export_reference=args.reference,
    )
    result = write_export(args.output, manifest)
    result["counts"] = {k: len(manifest["body"][k]) for k in ("sources", "profiles", "windows", "visits")}
    result["excluded"] = manifest["body"]["inventory"]
    print(json.dumps(result))


if __name__ == "__main__":
    main()
