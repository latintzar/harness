"""Host import of retained complete tracker runs as historical tracker reports.

One retained run root holds one completed stock tracker execution per camera view.
This command reads that evidence, verifies it, plans the exact records and, on an
explicit apply with the separately reviewed plan checksum, writes them through the
maintained SDK as the restricted runtime role. A dry run opens no database. The
import admits zero action events and registers zero observed items.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from pydantic import ValidationError

import db
from realtime.continuity import (
    ArtifactMapping,
    ContinuityStore,
    EpochEnd,
    EpochStart,
    TrackerSnapshot,
    TrackSegment,
    reported_birth_id,
    reported_frame_s,
)
from realtime.contracts import HostExecutionContext
from realtime.import_stream import identity_record
from realtime.store import AccessDenied, Conflict, RealtimeError, RealTimeStore, _digest, _encoded
from realtime.subjects import ATTRIBUTES, SubjectRegistration, SubjectStore

PLAN_SCHEMA = "rocoto.tracker-run-import-plan.v1"
RECEIPT_SCHEMA = "rocoto.tracker-run-import-receipt.v1"
SIDECAR_SCHEMA = "rocoto.tracker-kitti-directory.v1"
SOURCE_SCHEMA = "rocoto.historical-tracker-source.v1"
VIEWS = "retail-action-nvidia"
PRODUCER = "retail_prepare_track.py"
TEMPLATE = "tracker-template.txt"
INVENTORY = "source-inventory-proof.json"
FRAME_FILE = "00_000_{:06d}.txt"
TRACKER_LIBRARY = "libnvds_nvmultiobjecttracker.so"
PROFILE = "tracking-reported@1"
EVENT_TYPES = ["tracking.reported"]
CAPABILITIES = frozenset({"realtime:read", "realtime:configure", "realtime:ingest"})
CLIP = re.compile(r"[0-9]{1,16}-rank[01]")
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
TRACKER_ID = re.compile(r"0|[1-9][0-9]{0,19}")
KITTI_COLUMNS = 17
MAX_VIEWS = 256
MAX_MEDIA = 256 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
MAX_TRACKS = 64 * 1024 * 1024
MAX_FRAME = 256 * 1024
VIEW_DEADLINE_S = 600
# Every retained row of one source, counted before and after its import.
ROWS = (
    ("sources", "source", ""),
    ("profiles", "profile_revision", ""),
    ("receipts", "input_receipt", ""),
    ("cameras", "subject", " and kind='camera'"),
    ("visits", "subject", " and kind='visit'"),
    ("observed_items", "subject", " and kind='observed_item'"),
    ("subject_revisions", "subject_revision", ""),
    ("epochs", "tracker_epoch", ""),
    ("closures", "tracker_epoch_end", ""),
    ("mappings", "source_artifact_mapping", ""),
    ("reported_segments", "track_segment", " and state='reported'"),
    ("other_segments", "track_segment", " and state<>'reported'"),
    ("events", "event", ""),
    ("quarantined", "quarantine", ""),
    ("event_bindings", "event_subject_binding", ""),
)


class Refused(Conflict):
    """Retained evidence, the reviewed plan or a retained record differs from this import."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _need(condition, code, message):
    if not condition:
        raise Refused(code, message)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _whole(value, low, high):
    return type(value) is int and low <= value <= high


def _entry(root, relative):
    """One retained path below the resolved run root, reached through real directories."""
    path = root / relative
    try:
        info = os.lstat(path)
        direct = path.resolve(strict=True) == path
    except OSError:
        info, direct = None, False
    _need(info is not None and direct, "evidence_missing", f"Retain {relative} as a real path of the run")
    return path, info


def _read(root, relative, limit=MAX_TEXT):
    path, info = _entry(root, relative)
    _need(stat.S_ISREG(info.st_mode), "evidence_missing", f"Retain {relative} as a regular file")
    _need(info.st_size <= limit, "evidence_too_large", f"{relative} exceeds its byte allowance")
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    _need(len(data) <= limit, "evidence_too_large", f"{relative} exceeds its byte allowance")
    return data


def _media(root, relative):
    path, info = _entry(root, relative)
    _need(stat.S_ISREG(info.st_mode), "evidence_missing", f"Retain {relative} as a regular file")
    _need(0 < info.st_size <= MAX_MEDIA, "evidence_too_large", f"{relative} exceeds its byte allowance")
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
            size += len(part)
    return digest.hexdigest(), size


def _json(data, relative):
    try:
        return json.loads(data)
    except ValueError:
        raise Refused("evidence_format", f"{relative} holds one JSON document") from None


def sidecar_digest(files):
    """One sealed digest of a per-frame tracker directory, from (name, bytes) pairs.

    The digest is the SHA-256 of a UTF-8 listing: the schema line, then one line per
    file in ascending byte order of its name, `<sha256 of the file bytes>  <name>`,
    every line ending in a line feed. It equals `sha256sum` over the sorted directory
    with the schema line in front, so a host can reproduce it with standard tools.
    """
    listing = [SIDECAR_SCHEMA]
    for name, data in sorted(files, key=lambda item: os.fsencode(item[0])):
        listing.append(_sha(data) + "  " + name)
    return _sha(("\n".join(listing) + "\n").encode())


def _frame_rows(data, relative):
    """The tracker rows of one frame file, in the producer's own record shape."""
    refusal = Refused("sidecar_format", f"{relative} holds one 17-column Person row per reported track")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise refusal from None
    rows, seen = [], set()
    for line in text.splitlines():
        cells = line.split()
        if (
            len(cells) != KITTI_COLUMNS
            or cells[0] != "Person"
            or not TRACKER_ID.fullmatch(cells[1])
            or cells[1] in seen
        ):
            raise refusal
        try:
            numbers = [float(cell) for cell in cells[2:]]
        except ValueError:
            raise refusal from None
        if not all(math.isfinite(number) for number in numbers):
            raise refusal
        seen.add(cells[1])
        rows.append(
            {
                "track_id": int(cells[1]),
                "class_name": "person",
                "bbox_xyxy": numbers[3:7],
                "confidence": numbers[-1],
            }
        )
    return rows


def _sidecar(root, relative, count):
    path, info = _entry(root, relative)
    _need(stat.S_ISDIR(info.st_mode), "evidence_missing", f"Retain {relative} as a directory")
    names = sorted(os.listdir(path), key=os.fsencode)
    _need(
        names == [FRAME_FILE.format(index) for index in range(count)],
        "frame_count_mismatch",
        f"{relative} holds exactly one tracker file for each of the {count} frames",
    )
    files = [(name, _read(root, f"{relative}/{name}", MAX_FRAME)) for name in names]
    return sidecar_digest(files), [_frame_rows(data, f"{relative}/{name}") for name, data in files]


def _reports(frames):
    """Each raw tracker ID with its runs of consecutive reported frames, end exclusive."""
    reported = {}
    for index, rows in enumerate(frames):
        for row in rows:
            runs = reported.setdefault(str(row["track_id"]), [])
            if runs and runs[-1][1] == index:
                runs[-1][1] = index + 1
            else:
                runs.append([index, index + 1])
    return [(tracker_id, reported[tracker_id]) for tracker_id in sorted(reported, key=int)]


def _recording(record, listed, clip):
    """The producer's own run receipt: one complete recording on an exact frame clock."""
    ok = isinstance(record, dict) and record == listed
    ok = ok and all(
        name in record
        for name in ("source_clip_id", "source_view", "duration_s", "known_track_ids", "tracking_exit")
    )
    ok = (
        ok and record.get("clip_id") == clip and f"{record['source_clip_id']}-{record['source_view']}" == clip
    )
    ok = ok and _whole(record.get("fps"), 1, 120) and _whole(record.get("frame_count"), 1, 100_000)
    ok = ok and _whole(record.get("width"), 2, 4096) and _whole(record.get("height"), 2, 2160)
    ok = ok and _whole(record.get("unique_source_frames"), 1, 100_000)
    ok = (
        ok
        and isinstance(record.get("temporal_sampling"), str)
        and 1 <= len(record["temporal_sampling"]) <= 128
    )
    _need(ok, "producer_receipt_mismatch", f"{clip}: the run receipt and the run manifest describe this view")
    _need(
        type(record["tracking_exit"]) is int and record["tracking_exit"] == 0,
        "tracker_exit_nonzero",
        f"{clip}: the tracker execution ended with exit status zero",
    )
    fps, count, stated = Decimal(record["fps"]), record["frame_count"], record["duration_s"]
    clock = SimpleNamespace(source_window=SimpleNamespace(start_s=Decimal(0)), fps=fps)
    duration = reported_frame_s(clock, count)
    _need(
        type(stated) in (int, float)
        and math.isfinite(stated)
        and Decimal(str(stated)) == duration
        and duration * fps == count
        and duration <= 3600,
        "frame_clock_mismatch",
        f"{clip}: duration equals frame count over frame rate on a bounded clock",
    )
    return clock, duration


def _application(data, clip, record):
    """The retained application configuration names this view's media, sidecar and one pass."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(data.decode("utf-8"))
    except (configparser.Error, UnicodeDecodeError):
        raise Refused("evidence_format", f"{clip}: the application configuration is one INI file") from None

    def value(section, name):
        return parser.get(section, name, fallback="")

    uri = value("source0", "uri")
    ok = (
        [name for name in parser.sections() if re.fullmatch(r"source[0-9]+", name)] == ["source0"]
        and value("source0", "enable") == "1"
        and value("source0", "num-sources") == "1"
        and uri.startswith("file:///")
        and PurePosixPath(uri[len("file://") :]).parts[-2:] == (clip, "clean.mp4")
        and PurePosixPath(value("application", "kitti-track-output-dir")).parts[-2:]
        == (clip, "tracker-kitti")
        and value("tracker", "enable") == "1"
        and PurePosixPath(value("tracker", "ll-lib-file")).name == TRACKER_LIBRARY
        and bool(PurePosixPath(value("tracker", "ll-config-file")).name)
        and value("tests", "file-loop") == "0"
        and value("streammux", "width") == str(record["width"])
        and value("streammux", "height") == str(record["height"])
    )
    _need(
        ok,
        "application_config_mismatch",
        f"{clip}: the application configuration tracks this view's media once into its own sidecar",
    )
    return PurePosixPath(value("tracker", "ll-config-file")).name


def _end_of_stream(data, clip):
    lines = data.decode("utf-8", "replace").splitlines()
    ends = [index for index, line in enumerate(lines) if "Received EOS. Exiting" in line]
    _need(
        len(ends) == 1 and any(line.strip() == "App run successful" for line in lines[ends[0] + 1 :]),
        "end_of_stream_missing",
        f"{clip}: the execution log holds its end-of-stream line and a successful exit",
    )


def _tracks(value, clip, record, frames):
    """The producer's JSON conversion agrees with the per-frame sidecar, row for row."""
    fps = record["fps"]
    expected = [
        {"frame_index": index, "video_time_s": index / fps, "tracks": rows}
        for index, rows in enumerate(frames)
    ]
    ok = (
        isinstance(value, dict)
        and value.get("frames") == expected
        and value.get("schema_version") == 1
        and value.get("clip_id") == clip
        and value.get("identity_scope") == "recording_and_camera_local"
        and all(value.get(name) == record[name] for name in ("fps", "width", "height"))
        and all(
            isinstance(value.get(name), str) and 1 <= len(value[name]) <= 160
            for name in ("detector", "tracker")
        )
        and record["known_track_ids"] == sorted({row["track_id"] for rows in frames for row in rows})
    )
    _need(ok, "tracks_mismatch", f"{clip}: the converted tracks equal the per-frame tracker sidecar")


def _sample_mapping(value, clip, record):
    """Every encoded frame names the publisher frame it holds, on the encoded clock."""
    ok = isinstance(value, dict) and set(value) == {
        "source_frame_timestamps_s",
        "frames",
        "temporal_sampling",
    }
    times = value["source_frame_timestamps_s"] if ok else []
    frames = value["frames"] if ok else []
    ok = (
        ok
        and value["temporal_sampling"] == record["temporal_sampling"]
        and isinstance(times, list)
        and len(times) == record["unique_source_frames"]
        and all(type(time) in (int, float) and math.isfinite(time) for time in times)
        and isinstance(frames, list)
        and len(frames) == record["frame_count"]
    )
    previous = 0
    for index, frame in enumerate(frames if ok else []):
        chosen = frame.get("source_frame_index") if isinstance(frame, dict) else None
        ok = (
            type(chosen) is int
            and previous <= chosen < len(times)
            and frame
            == {
                "frame_index": index,
                "video_time_s": index / record["fps"],
                "source_frame_index": chosen,
                "source_time_s": times[chosen],
            }
        )
        if not ok:
            break
        previous = chosen
    _need(ok, "sample_mapping_mismatch", f"{clip}: the source sample mapping covers every encoded frame")


def _inventory(root):
    data = _read(root, INVENTORY)
    value = _json(data, INVENTORY)
    rows = value.get("recordings") if isinstance(value, dict) else None
    ok = isinstance(rows, list) and all(
        isinstance(row, dict) and isinstance(row.get("source_id"), str) for row in rows
    )
    _need(
        ok and len({row["source_id"] for row in rows}) == len(rows),
        "evidence_format",
        f"{INVENTORY} lists each recording once",
    )
    return {row["source_id"]: row for row in rows}, _sha(data)


def identifiers(clip):
    """The stable identifiers of one view. Content hashes stay out of every identifier."""
    source = "retailaction-" + clip
    report = source + "-tracker-report"
    return {
        "source_id": source,
        "source_asset_id": source,
        "camera_subject_id": source + "-camera",
        "epoch_id": report,
        "tracker_namespace": report,
        "mapping_id": report + "-m1",
        "receipt_id": report + "-r1",
        "profile_revision_id": PROFILE,
    }


def _verify_view(root, clip, listed, shared):
    """Read and verify one view's retained evidence. Every check precedes any record."""
    base, source_id = f"{VIEWS}/{clip}", identifiers(clip)["source_id"]
    receipt_bytes = _read(root, base + "/receipt.json")
    record = _json(receipt_bytes, base + "/receipt.json")
    clock, duration = _recording(record, listed, clip)
    count = record["frame_count"]
    media_sha, media_bytes = _media(root, base + "/clean.mp4")
    inventory = shared["inventory"].get(source_id)
    _need(
        isinstance(inventory, dict)
        and inventory.get("sha256") == media_sha
        and inventory.get("bytes") == media_bytes
        and inventory.get("face_redaction") in {"source_anonymized", "verified_redacted"},
        "media_hash_mismatch",
        f"{clip}: the media bytes equal the retained source inventory",
    )
    application = _read(root, base + "/app.txt")
    low_level = _application(application, clip, record)
    log = _read(root, base + "/tracking.log")
    _end_of_stream(log, clip)
    sidecar_sha, frames = _sidecar(root, base + "/tracker-kitti", count)
    tracks_bytes = _read(root, base + "/tracks.json", MAX_TRACKS)
    converted = _json(tracks_bytes, base + "/tracks.json")
    _tracks(converted, clip, record, frames)
    mapping_bytes = _read(root, base + "/source_mapping.json")
    _sample_mapping(_json(mapping_bytes, base + "/source_mapping.json"), clip, record)
    return {
        "record": record,
        "clock": clock,
        "duration": duration,
        "frames": frames,
        "converted": converted,
        "face_redaction": inventory["face_redaction"],
        "low_level": low_level,
        "media_sha": media_sha,
        "media_bytes": media_bytes,
        "sidecar_sha": sidecar_sha,
        "application_sha": _sha(application),
        "log_sha": _sha(log),
        "mapping_sha": _sha(mapping_bytes),
        "receipt_sha": _sha(receipt_bytes),
        "tracks_sha": _sha(tracks_bytes),
    }


def _plan_view(root, clip, listed, shared):
    """State the exact SDK records one verified view becomes."""
    seen = _verify_view(root, clip, listed, shared)
    ids, record, clock, duration = identifiers(clip), seen["record"], seen["clock"], seen["duration"]
    reference = f"{shared['reference']}/{VIEWS}/{clip}"
    source_id, asset, namespace = ids["source_id"], ids["source_asset_id"], ids["tracker_namespace"]
    fps, count, frames = record["fps"], record["frame_count"], seen["frames"]
    media_sha, sidecar_sha, application_sha = seen["media_sha"], seen["sidecar_sha"], seen["application_sha"]

    def evidence(name, sha256):
        return {"reference": f"{reference}/{name}", "sha256": sha256}

    window = {"start_s": "0", "end_s": str(duration)}
    sidecar = evidence("tracker-kitti", sidecar_sha)
    reports, intervals = [], []
    for tracker_id, runs in _reports(frames):
        first = runs[0][0]
        birth = reported_birth_id(namespace, sidecar_sha, tracker_id, first)
        visit = f"{namespace}-v{tracker_id}"
        segments = []
        for start, end in runs:
            span = {
                "start_s": str(reported_frame_s(clock, start)),
                "end_s": str(reported_frame_s(clock, end)),
            }
            intervals.append(
                {
                    "tracker_id": tracker_id,
                    "birth_id": birth,
                    "source_window": span,
                    "state": "reported",
                    "first_reported_frame": first,
                }
            )
            segments.append(
                {
                    "segment_id": f"{visit}-s{start}",
                    "first_frame": start,
                    "end_frame": end,
                    "source_window": span,
                }
            )
        reports.append(
            {
                "tracker_id": tracker_id,
                "first_reported_frame": first,
                "first_reported_s": str(reported_frame_s(clock, first)),
                "first_reported_end_s": str(reported_frame_s(clock, first + 1)),
                "birth_id": birth,
                "visit_subject_id": visit,
                "segments": segments,
            }
        )
    try:
        snapshot = TrackerSnapshot.model_validate(
            {
                "epoch_id": ids["epoch_id"],
                "tracker_namespace": namespace,
                "configuration_sha256": application_sha,
                "evidence": sidecar,
                "tracks": intervals,
            }
        ).model_dump(mode="json")
        epoch = EpochStart.model_validate(
            {
                "epoch_id": ids["epoch_id"],
                "camera_subject_id": ids["camera_subject_id"],
                "tracker_namespace": namespace,
                "implementation": "nvidia_deepstream_nvtracker",
                "configuration_sha256": application_sha,
                "started_s": "0",
                "evidence": evidence("receipt.json", seen["receipt_sha"]),
                "historical_run": {
                    "basis": "complete_recording_kitti",
                    "source_asset_id": asset,
                    "source_sha256": media_sha,
                    "source_window": window,
                    "frame_count": count,
                    "fps": str(fps),
                    "application_config": evidence("app.txt", application_sha),
                    "execution_log": evidence("tracking.log", seen["log_sha"]),
                    "tracker_sidecar": sidecar,
                    "source_mapping": evidence("source_mapping.json", seen["mapping_sha"]),
                    "producer": {
                        "reference": f"{shared['reference']}/{PRODUCER}",
                        "sha256": shared["producer_sha256"],
                    },
                    "configuration_basis": "retained_application_config",
                    "visibility_basis": "unclassified_tracker_report",
                    "snapshot_sha256": _digest(snapshot),
                },
            }
        ).model_dump(mode="json")
        historical = epoch["historical_run"]
        mapping = ArtifactMapping.model_validate(
            {
                "mapping_id": ids["mapping_id"],
                "epoch_id": ids["epoch_id"],
                "source_asset_id": asset,
                "source_sha256": media_sha,
                "source_window": window,
                "stream_window": window,
                "evidence": historical["source_mapping"],
            }
        ).model_dump(mode="json")
        receipt = {
            "kind": "actual_input",
            "input_kind": "historical_tracker_report",
            "source_asset_id": asset,
            "profile_revision_id": PROFILE,
            "source_window": window,
            "source_sha256": media_sha,
            "continuity": {
                "epoch_id": ids["epoch_id"],
                "mapping_id": ids["mapping_id"],
                "stream_window": window,
            },
            "tracker_snapshot": snapshot,
            "retained_run": {
                "reference": reference,
                "producer_receipt_sha256": seen["receipt_sha"],
                "producer_template_sha256": shared["template_sha256"],
                "source_inventory_sha256": shared["inventory_sha256"],
                "tracks_sha256": seen["tracks_sha"],
            },
        }
        receipt_sha = _digest(receipt)

        def subject(registration):
            body = SubjectRegistration.model_validate(registration).model_dump(mode="json")
            attributes = ATTRIBUTES[body["kind"]].model_validate(body["attributes"])
            return body | {"attributes": attributes.model_dump(mode="json")}

        camera = subject(
            {
                "subject_id": ids["camera_subject_id"],
                "source_id": source_id,
                "namespace": "camera",
                "kind": "camera",
                "attributes": {"camera_id": source_id},
                "provenance": evidence("receipt.json", seen["receipt_sha"]),
            }
        )
        visits = [
            subject(
                {
                    "subject_id": report["visit_subject_id"],
                    "source_id": source_id,
                    "namespace": namespace,
                    "kind": "visit",
                    "attributes": {
                        "camera_subject_id": ids["camera_subject_id"],
                        "source_asset_id": asset,
                        "tracker_namespace": namespace,
                        "tracker_id": report["tracker_id"],
                        "visit_boundary_id": report["birth_id"],
                        "basis": "first_reported",
                        "tracker_epoch_id": ids["epoch_id"],
                    },
                    "provenance": {
                        "reference": sidecar["reference"],
                        "sha256": receipt_sha,
                        "receipt_id": ids["receipt_id"],
                        "source_start_s": report["first_reported_s"],
                        "source_end_s": report["first_reported_end_s"],
                    },
                }
            )
            for report in reports
        ]
        segments = [
            TrackSegment.model_validate(
                {
                    "segment_id": segment["segment_id"],
                    "epoch_id": ids["epoch_id"],
                    "mapping_id": ids["mapping_id"],
                    "visit_subject_id": report["visit_subject_id"],
                    "receipt_id": ids["receipt_id"],
                    "receipt_sha256": receipt_sha,
                    "tracker_id": report["tracker_id"],
                    "birth_id": report["birth_id"],
                    "source_window": segment["source_window"],
                    "state": "reported",
                }
            ).model_dump(mode="json")
            for report in reports
            for segment in report["segments"]
        ]
        closure = EpochEnd.model_validate(
            {
                "epoch_id": ids["epoch_id"],
                "ended_s": str(duration),
                "reason": "eos",
                "evidence": historical["execution_log"],
            }
        ).model_dump(mode="json")
    except ValidationError as error:
        raise Refused(
            "plan_invalid", f"{clip}: the report exceeds the storage contract ({error.error_count()})"
        ) from None
    metadata = {
        "historical_tracker_run": {
            "schema": SOURCE_SCHEMA,
            "reference": reference,
            "dataset_clip_id": record["source_clip_id"],
            "dataset_view": record["source_view"],
            "source_asset_id": asset,
            "source_sha256": media_sha,
            "source_bytes": seen["media_bytes"],
            "face_redaction": seen["face_redaction"],
            "width": record["width"],
            "height": record["height"],
            "fps": fps,
            "frame_count": count,
            "duration_s": str(duration),
            "temporal_sampling": record["temporal_sampling"],
            "unique_source_frames": record["unique_source_frames"],
            "identity_scope": seen["converted"]["identity_scope"],
            "detector": seen["converted"]["detector"],
            "tracker": seen["converted"]["tracker"],
            "tracker_low_level_config": seen["low_level"],
        }
    }
    commands = {
        "source": {"source_id": source_id, "kind": "camera", "metadata": metadata},
        "profile": {"source_id": source_id, "revision_id": PROFILE, "event_types": EVENT_TYPES},
        "camera": camera,
        "epoch": epoch,
        "mapping": mapping,
        "receipt": receipt,
        "visits": visits,
        "segments": segments,
        "closure": closure,
    }
    sealed = {
        "media_sha256": media_sha,
        "media_bytes": seen["media_bytes"],
        "application_config_sha256": application_sha,
        "execution_log_sha256": seen["log_sha"],
        "tracker_sidecar_sha256": sidecar_sha,
        "source_mapping_sha256": seen["mapping_sha"],
        "producer_sha256": shared["producer_sha256"],
        "producer_template_sha256": shared["template_sha256"],
        "producer_receipt_sha256": seen["receipt_sha"],
        "source_inventory_sha256": shared["inventory_sha256"],
        "tracks_sha256": seen["tracks_sha"],
        "snapshot_sha256": historical["snapshot_sha256"],
        "receipt_sha256": receipt_sha,
    }
    return {
        "clip": clip,
        "ids": ids,
        "sealed": sealed,
        "recording": {
            "frame_count": count,
            "fps": fps,
            "duration_s": str(duration),
            "width": record["width"],
            "height": record["height"],
        },
        "counts": {
            "frames": count,
            "tracker_ids": len(reports),
            "visits": len(visits),
            "reported_segments": len(segments),
            "events": 0,
            "observed_items": 0,
        },
        "tracks": reports,
        "commands": commands,
        "sha256": _sha(
            _encoded({"clip": clip, "ids": ids, "sealed": sealed, "commands": commands}, MAX_TRACKS)
        ),
    }


def plan_run(run_root, *, reference, company_id, principal_id, clips=None):
    """Verify the retained run and plan every record. Reads files only."""
    _need(
        isinstance(reference, str) and REFERENCE.fullmatch(reference) is not None,
        "plan_invalid",
        "Name the retained run with one bounded reference label",
    )
    _need(
        all(isinstance(value, str) and 1 <= len(value) <= 256 for value in (company_id, principal_id)),
        "plan_invalid",
        "Supply the saved company and principal identities",
    )
    try:
        root = Path(run_root).resolve(strict=True)
    except OSError:
        raise Refused("evidence_missing", "Select the retained run root") from None
    manifest = _json(_read(root, VIEWS + "/manifest.json"), VIEWS + "/manifest.json")
    records = manifest.get("records") if isinstance(manifest, dict) else None
    _need(isinstance(records, list), "evidence_format", "The run manifest lists its view records")
    listed = {}
    for record in records:
        clip = record.get("clip_id") if isinstance(record, dict) else None
        _need(
            isinstance(clip, str) and CLIP.fullmatch(clip) is not None and clip not in listed,
            "view_unknown",
            "The run manifest names each recording view once",
        )
        listed[clip] = record
    present = set(os.listdir(root / VIEWS)) - {"manifest.json"}
    _need(present <= set(listed), "view_unknown", "Every retained view folder belongs to the run manifest")
    if clips is None:
        selected = sorted(listed)
    else:
        _need(
            isinstance(clips, (list, tuple)) and len(set(clips)) == len(clips) and set(clips) <= set(listed),
            "view_unknown",
            "Select distinct views of the run manifest",
        )
        selected = sorted(clips)
    _need(1 <= len(selected) <= MAX_VIEWS, "view_unknown", "Select between 1 and 256 views")
    inventory, inventory_sha = _inventory(root)
    shared = {
        "reference": reference,
        "producer_sha256": _sha(_read(root, PRODUCER)),
        "template_sha256": _sha(_read(root, TEMPLATE)),
        "inventory": inventory,
        "inventory_sha256": inventory_sha,
    }
    views = [_plan_view(root, clip, listed[clip], shared) for clip in selected]
    totals = {
        "views": len(views),
        **{
            name: sum(view["counts"][name] for view in views)
            for name in ("frames", "tracker_ids", "visits", "reported_segments", "events", "observed_items")
        },
        "media_bytes": sum(view["sealed"]["media_bytes"] for view in views),
    }
    sealed = {
        "schema": PLAN_SCHEMA,
        "reference": reference,
        "company_id": company_id,
        "principal_id": principal_id,
        "views": [[view["clip"], view["sha256"]] for view in views],
    }
    return {
        "schema": PLAN_SCHEMA,
        "reference": reference,
        "run_root": str(root),
        "company_id": company_id,
        "principal_id": principal_id,
        "totals": totals,
        "views": views,
        "sha256": _sha(_encoded(sealed, MAX_TEXT)),
    }


def summary(plan):
    """The reviewable plan: identifiers, sealed hashes, counts and visitor boundaries per view."""
    return {
        "schema": PLAN_SCHEMA,
        "reference": plan["reference"],
        "run_root": plan["run_root"],
        "company_id": plan["company_id"],
        "principal_id": plan["principal_id"],
        "plan_sha256": plan["sha256"],
        "totals": plan["totals"],
        "views": [
            {
                name: view[name]
                for name in ("clip", "ids", "sealed", "recording", "counts", "tracks", "sha256")
            }
            for view in plan["views"]
        ],
    }


def require_reviewed(plan, expected_sha256):
    """The checksum is integrity of the reviewed dry run; authority stays with the database."""
    _need(
        isinstance(expected_sha256, str) and expected_sha256 == plan["sha256"],
        "plan_checksum_mismatch",
        "The retained run differs from the separately reviewed dry-run checksum",
    )


def _context(plan):
    return HostExecutionContext(
        company_id=plan["company_id"],
        principal_id=plan["principal_id"],
        request_id="tracker-run-import-" + uuid.uuid4().hex,
        conversation_id="tracker-run-import:" + plan["sha256"][:16],
        capabilities=CAPABILITIES,
        deadline_utc=datetime.now(timezone.utc) + timedelta(seconds=VIEW_DEADLINE_S),
    )


async def _authority(context, store):
    """The restricted role, a commissioned company and a principal that configures and ingests."""
    async with store._transaction(context, "realtime:read") as con:
        role = await con.fetchval("select current_user::text")
        granted = await con.fetchval(
            "select realtime.actor_capabilities($1,$2)", context.company_id, context.principal_id
        )
    if not CAPABILITIES.issubset(granted):
        raise AccessDenied("Historical import requires a principal that configures and ingests sources")
    return role


async def _retained(context, store, view):
    """Compare every record this view already holds with its plan, before any write."""
    commands, ids = view["commands"], view["ids"]
    company, source_id = context.company_id, ids["source_id"]
    subjects = {item["subject_id"]: item for item in (commands["camera"], *commands["visits"])}
    segments = {item["segment_id"]: item for item in commands["segments"]}
    expected = {
        "source": {"kind": "camera", "metadata": commands["source"]["metadata"]},
        "profile": EVENT_TYPES,
        "epoch": _digest(commands["epoch"]),
        "mapping": _digest(commands["mapping"]),
        "receipt": view["sealed"]["receipt_sha256"],
        "closure": _digest(commands["closure"]),
        **{"subject " + key: [source_id, _digest(item)] for key, item in subjects.items()},
        **{"segment " + key: _digest(item) for key, item in segments.items()},
    }
    found = {}
    async with store._transaction(context, "realtime:read") as con:
        source = await con.fetchrow(
            "select kind,metadata from realtime.source where tenant_id=$1 and source_id=$2",
            company,
            source_id,
        )
        if source:
            found["source"] = {"kind": source["kind"], "metadata": source["metadata"]}
        for name, statement, key in (
            (
                "profile",
                "select event_types from realtime.profile_revision "
                "where tenant_id=$1 and source_id=$2 and revision_id=$3",
                PROFILE,
            ),
            (
                "epoch",
                "select digest from realtime.tracker_epoch "
                "where tenant_id=$1 and source_id=$2 and epoch_id=$3",
                ids["epoch_id"],
            ),
            (
                "mapping",
                "select digest from realtime.source_artifact_mapping "
                "where tenant_id=$1 and source_id=$2 and mapping_id=$3",
                ids["mapping_id"],
            ),
            (
                "receipt",
                "select digest from realtime.input_receipt "
                "where tenant_id=$1 and source_id=$2 and receipt_id=$3",
                ids["receipt_id"],
            ),
            (
                "closure",
                "select digest from realtime.tracker_epoch_end "
                "where tenant_id=$1 and source_id=$2 and epoch_id=$3",
                ids["epoch_id"],
            ),
        ):
            value = await con.fetchval(statement, company, source_id, key)
            if value is not None:
                found[name] = value
        for row in await con.fetch(
            "select subject_id,source_id,digest from realtime.subject_revision "
            "where tenant_id=$1 and subject_id=any($2::text[]) and revision=1",
            company,
            list(subjects),
        ):
            found["subject " + row["subject_id"]] = [row["source_id"], row["digest"]]
        for row in await con.fetch(
            "select segment_id,digest from realtime.track_segment "
            "where tenant_id=$1 and source_id=$2 and segment_id=any($3::text[])",
            company,
            source_id,
            list(segments),
        ):
            found["segment " + row["segment_id"]] = row["digest"]
    differing = sorted(name for name, value in found.items() if value != expected[name])
    _need(
        not differing,
        "retained_record_conflict",
        f"{view['clip']}: retained records differ from this run ({', '.join(differing[:4])})",
    )
    return "new" if not found else "complete" if len(found) == len(expected) else "partial"


async def _rows(context, store, source_id):
    counts = {}
    async with store._transaction(context, "realtime:read") as con:
        for name, table, extra in ROWS:
            # Table and filter are constants of this module; values stay query parameters.
            counts[name] = await con.fetchval(
                f"select count(*) from realtime.{table} where tenant_id=$1 and source_id=$2{extra}",
                context.company_id,
                source_id,
            )
    return counts


async def _import_view(context, store, view):
    """One view through the maintained SDK. Every call returns its retained record on a retry."""
    commands, ids, sealed = view["commands"], view["ids"], view["sealed"]
    source_id = ids["source_id"]
    subjects, continuity = SubjectStore(store), ContinuityStore(store)
    source = await store.register_source(context, source_id, "camera", commands["source"]["metadata"])
    await store.register_profile(context, source_id, PROFILE, EVENT_TYPES)
    camera = await subjects.register(context, commands["camera"])
    epoch = await continuity.start_epoch(context, source_id, commands["epoch"])
    mapping = await continuity.map_artifact(context, source_id, commands["mapping"])
    receipt_sha = await store.record_receipt(context, source_id, ids["receipt_id"], commands["receipt"])
    _need(
        receipt_sha == sealed["receipt_sha256"],
        "retained_record_conflict",
        "The tracker receipt keeps its hash",
    )
    visits = [await subjects.register(context, visit) for visit in commands["visits"]]
    segments = [await continuity.record_segment(context, source_id, item) for item in commands["segments"]]
    closure = await continuity.end_epoch(context, source_id, commands["closure"])
    playback = await continuity.playback(context, source_id, ids["epoch_id"])
    planned = {report["visit_subject_id"]: report for report in view["tracks"]}
    people = {person["visit_subject_id"]: person for person in playback["visitors"]}
    artifacts = [(item["mapping_id"], item["source_sha256"]) for item in playback["artifacts"]]
    _need(
        artifacts == [(ids["mapping_id"], sealed["media_sha256"])]
        and set(people) <= set(planned)
        and all(
            person["basis"] == "first_reported"
            and person["first_observed"] is None
            and person["birth_id"] == planned[key]["birth_id"]
            and person["first_reported"]
            == {
                "source_asset_id": ids["source_asset_id"],
                "source_s": planned[key]["first_reported_s"],
                "stream_s": planned[key]["first_reported_s"],
            }
            for key, person in people.items()
        ),
        "readback_mismatch",
        f"{view['clip']}: playback returns the planned first-reported visitors",
    )
    return {
        "records": {
            "source": {
                "source_id": source["source_id"],
                "kind": source["kind"],
                "metadata_sha256": _digest(source["metadata"]),
            },
            "profile": {"revision_id": PROFILE, "event_types": EVENT_TYPES},
            "camera": {key: camera[key] for key in ("subject_id", "revision", "status")},
            "epoch": {"epoch_id": epoch["epoch_id"], "digest": _digest(epoch)},
            "mapping": {"mapping_id": mapping["mapping_id"], "digest": _digest(mapping)},
            "receipt": {"receipt_id": ids["receipt_id"], "sha256": receipt_sha},
            "visits": [
                {key: visit[key] for key in ("subject_id", "revision", "status")}
                | {key: report[key] for key in ("tracker_id", "first_reported_frame", "birth_id")}
                for visit, report in zip(visits, view["tracks"], strict=True)
            ],
            "segments": [{"segment_id": item["segment_id"], "digest": _digest(item)} for item in segments],
            "closure": {"epoch_id": closure["epoch_id"], "digest": _digest(closure)},
        },
        "written": {
            "camera": camera["needs_refresh"],
            "visits": sum(visit["needs_refresh"] for visit in visits),
        },
        "readback": {
            "selectable_visitors": len(people),
            "first_reported": sum(person["first_reported"] is not None for person in people.values()),
            "first_observed": sum(person["first_observed"] is not None for person in people.values()),
        },
    }


async def import_run(plan, *, store=None):
    """Import every planned view, or refuse the whole run before its first write.

    The principal's authority and every already retained record are checked for all
    views first. A failure after writing began returns a receipt with status `failed`;
    the same run is safe to apply again.
    """
    store = store or RealTimeStore()
    started = datetime.now(timezone.utc)
    role = await _authority(_context(plan), store)
    states = [await _retained(_context(plan), store, view) for view in plan["views"]]
    views, error = [], None
    for view, state in zip(plan["views"], states, strict=True):
        context = _context(plan)
        try:
            before = await _rows(context, store, view["ids"]["source_id"])
            result = await _import_view(context, store, view)
            after = await _rows(context, store, view["ids"]["source_id"])
        except Exception as failure:
            error = {
                "clip": view["clip"],
                "type": type(failure).__name__,
                "code": getattr(failure, "code", None),
                "message": str(failure)[:500],
            }
            break
        views.append(
            {name: view[name] for name in ("clip", "ids", "sealed", "recording", "counts", "sha256")}
            | {"state_before": state, "rows_before": before, "rows_after": after}
            | result
        )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "failed" if error else "imported",
        "dry_run": False,
        "reference": plan["reference"],
        "run_root": plan["run_root"],
        "company_id": plan["company_id"],
        "principal_id": plan["principal_id"],
        "plan_sha256": plan["sha256"],
        "database_role": role,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "totals": plan["totals"],
        "retained_rows": {name: sum(view["rows_after"][name] for view in views) for name, _, _ in ROWS},
        "views": views,
    }
    if error:
        receipt["error"] = error
    return receipt


def _identity(path):
    try:
        info = os.lstat(path)
        bounded = stat.S_ISREG(info.st_mode) and info.st_size <= 8192
        return identity_record(json.loads(Path(path).read_bytes())) if bounded else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _receipt_path(value, root):
    """A new file in an existing folder outside the repository and outside the retained run."""
    path = Path(value).absolute()
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        parent = None
    code = Path(__file__).resolve().parents[1]
    _need(
        parent is not None
        and parent.is_dir()
        and all(base != parent and base not in parent.parents for base in (root, code))
        and not any((folder / ".git").exists() for folder in (parent, *parent.parents)),
        "receipt_location",
        "Keep the receipt in an existing folder outside the repository and the retained run",
    )
    _need(
        not os.path.lexists(parent / path.name),
        "receipt_exists",
        "Each import keeps its own new receipt file",
    )
    return parent / path.name


def _write_receipt(path, receipt):
    encoded = (json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


async def _apply(plan, dsn):
    """Open only the restricted pool for this command's lifetime."""
    try:
        await db.init_runtime_only(dsn)
    except Exception as failure:
        detail = str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__
        raise Refused(
            "database_connection", "The restricted runtime connection is unavailable: " + detail
        ) from None
    try:
        return await import_run(plan)
    finally:
        await db.close_runtime_only()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m realtime.import_tracker_run",
        description="Import retained complete tracker runs as historical tracker reports. "
        "The default is a dry run that prints the plan and opens no database.",
    )
    parser.add_argument("--run-root", type=Path, required=True, help="the retained run root, read only")
    parser.add_argument("--reference", required=True, help="the retained run's reference label")
    parser.add_argument("--identity", type=Path, required=True, help="the saved workspace identity file")
    parser.add_argument("--principal", required=True, help="the saved principal that owns the import")
    parser.add_argument("--view", action="append", help="one clip of the run manifest; default every view")
    parser.add_argument("--apply", action="store_true", help="write through RUNTIME_DATABASE_URL")
    parser.add_argument("--expected-plan-sha256", help="plan_sha256 of the reviewed dry run")
    parser.add_argument("--receipt", type=Path, help="new receipt file outside the repository")
    args = parser.parse_args(argv)
    if args.apply != bool(args.expected_plan_sha256) or args.apply != bool(args.receipt):
        parser.error("--apply, --expected-plan-sha256 and --receipt travel together")
    try:
        identity = _identity(args.identity)
        _need(
            identity is not None, "identity_invalid", "Supply the saved workspace identity as a bounded file"
        )
        plan = plan_run(
            args.run_root,
            reference=args.reference,
            company_id=identity["company_scope"],
            principal_id=args.principal,
            clips=args.view,
        )
        if not args.apply:
            print(json.dumps(summary(plan) | {"dry_run": True}, indent=2))
            return 0
        require_reviewed(plan, args.expected_plan_sha256)
        target = _receipt_path(args.receipt, Path(plan["run_root"]))
        dsn = os.environ.get("RUNTIME_DATABASE_URL", "").strip()
        _need(bool(dsn), "database_connection", "Supply the restricted connection in RUNTIME_DATABASE_URL")
        receipt = asyncio.run(_apply(plan, dsn))
    except RealtimeError as refusal:
        code = getattr(refusal, "code", type(refusal).__name__)
        print(json.dumps({"refused": code, "detail": str(refusal)}), file=sys.stderr)
        return 2
    _write_receipt(target, receipt)
    result = {name: receipt[name] for name in ("status", "plan_sha256", "totals", "retained_rows")}
    print(json.dumps(result | {"receipt": str(target), "error": receipt.get("error")}, indent=2))
    return 0 if receipt["status"] == "imported" else 1


if __name__ == "__main__":
    raise SystemExit(main())
