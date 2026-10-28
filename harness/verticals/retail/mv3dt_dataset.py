"""CPU-only, host-owned dataset preflight for the pinned stock MV3DT file workflow.

Run: python mv3dt_dataset.py --dataset-root PATH --request request.json --output receipt.json
A ready receipt permits stock configuration preparation, not inference or identity fusion.
Clock/calibration/view attestations are retained host evidence, never inferred from filenames,
matching frame counts, or projection matrices. This module neither edits nor retimes source files.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import selectors
import stat
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import yaml
from stock_commissioning import SOURCE_COMMIT, digest, load, save

SCHEMA = "rocoto.mv3dt.dataset.v1"
MAX_BYTES = 8 * 1024**3
MAX_CAMERAS = 12
DETECTORS = {"PeopleNetTransformer": {1}, "PeopleNet2.6.3": {0}, "RTDETR": set(range(6))}


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Expected a bounded camera/source/evidence identifier")
    return value


def _number(value, *, minimum=0, maximum=3600):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Expected a finite bounded source-clock measurement")
    return value


class _Files:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.deadline = time.monotonic() + 60
        self.total = 0
        self.seen = {}

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Dataset preflight time budget exceeded")
        return remaining

    def resolve(self, relative):
        if not isinstance(relative, str) or len(relative) > 512:
            raise ValueError("Expected a bounded dataset-relative path")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Evidence paths must remain within the selected dataset")
        resolved = (self.root / path).resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError("Evidence paths must resolve to a file within the selected dataset")
        return resolved

    def verify(self, reference, *, maximum=1_000_000):
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValueError("Each file needs its explicit path and SHA256")
        expected = reference["sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Expected an exact SHA256")
        path = self.resolve(reference["path"])
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise ValueError("Dataset file exceeds its byte budget or is empty")
        if path not in self.seen:
            self.total += before.st_size
            if self.total > MAX_BYTES:
                raise ValueError("Dataset exceeds the total hashing byte budget")
            hashed = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.remaining()
                    hashed.update(chunk)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("Dataset file changed while hashing")
            self.seen[path] = hashed.hexdigest()
        if self.seen[path] != expected:
            raise ValueError("Dataset file SHA256 mismatch: " + reference["path"])
        return path

    def receipt(self, reference, camera, kind):
        if reference is None:
            return None
        body = load(self.verify(reference), limit=1_000_000)
        if (
            body.get("schema") != f"rocoto.mv3dt.{kind}.v1"
            or body.get("camera_id") != camera["camera_id"]
            or body.get("video_sha256") != camera["video"]["sha256"]
        ):
            raise ValueError("Evidence receipt must bind the exact camera and video")
        _identifier(body.get("reference"))
        return body


def _probe_bytes(command, timeout):
    """Bound actual pipe reads, including malformed files with excessive video tracks."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ValueError("Media probe time budget exceeded")
                    chunk = os.read(process.stdout.fileno(), 65537 - len(output))
                    output.extend(chunk)
                    if len(output) > 65536:
                        raise ValueError("Media probe exceeds its output budget")
                    if not chunk:
                        break
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise ValueError("Media probe could not read the selected MP4")
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def probe_video(path, timeout):
    """Read bounded MP4 metadata with ffprobe; this does not run a decoder/model."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=width,height,avg_frame_rate,start_time,duration:format=format_name",
        "-of",
        "json",
        str(path),
    ]
    body = json.loads(_probe_bytes(command, min(timeout, 5)))
    if "mp4" not in body.get("format", {}).get("format_name", "").split(","):
        raise ValueError("Stock file workflow requires MP4 media")
    streams = body.get("streams", [])
    if len(streams) != 1:
        raise ValueError("Each camera mapping requires exactly one video stream")
    stream = streams[0]
    return {
        "width": stream["width"],
        "height": stream["height"],
        "fps": float(Fraction(stream["avg_frame_rate"])),
        "source_start_s": float(stream["start_time"]),
        "duration_s": float(stream["duration"]),
    }


class _CalibrationLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("Calibration aliases require explicit normalization before preflight")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("Calibration contains duplicate keys")
        return super().construct_mapping(node, deep=deep)


def _calibration(path, detector):
    text = path.read_text()
    if text.lstrip().startswith("%YAML:1.0"):
        raise ValueError("Stock camInfo requires ordinary YAML normalization of the OpenCV header")
    try:
        body = yaml.load(text, Loader=_CalibrationLoader)
    except yaml.YAMLError as error:
        raise ValueError("Stock camInfo requires ordinary YAML normalization") from error
    matrix = body.get("projectionMatrix_3x4_w2p") if isinstance(body, dict) else None
    if not isinstance(matrix, list) or len(matrix) != 12:
        raise ValueError("Stock calibration requires its explicit 3x4 projection matrix")
    for value in matrix:
        _number(value, minimum=-1e12, maximum=1e12)

    # Rank three is necessary, not evidence that the physical calibration is correct.
    def determinant(columns):
        a, b, c = [[matrix[r * 4 + col] for col in columns] for r in range(3)]
        return (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        )

    if not any(abs(determinant(cols)) > 1e-12 for cols in itertools.combinations(range(4), 3)):
        raise ValueError("Calibration projection matrix is degenerate")
    models = body.get("modelInfo")
    if not isinstance(models, list) or not 1 <= len(models) <= 7:
        raise ValueError("Calibration requires bounded modelInfo")
    class_ids = [model.get("classID") for model in models]
    if (
        any(type(value) is not int or not 0 <= value <= 6 for value in class_ids)
        or len(set(class_ids)) != len(class_ids)
        or not DETECTORS[detector] <= set(class_ids)
    ):
        raise ValueError("Calibration modelInfo must cover the selected detector's active stock classes")
    for model in models:
        _number(model.get("height"), minimum=0.01, maximum=10)
        _number(model.get("radius"), minimum=0.01, maximum=10)
    return {"projection_matrix_sha256": digest(matrix), "model_class_ids": sorted(class_ids)}


def _clock(body, media, policy):
    _identifier(body.get("common_clock_id"))
    values = {
        key: _number(body.get(key), maximum=1e12)
        for key in (
            "source_start_s",
            "source_end_s",
            "common_start_s",
            "common_end_s",
            "max_error_s",
            "max_drift_s",
        )
    }
    duration = values["source_end_s"] - values["source_start_s"]
    if duration <= 0 or abs(values["common_end_s"] - values["common_start_s"] - duration) > 0.000001:
        raise ValueError("Clock receipt requires an explicit unit-rate source-to-common mapping")
    if (
        abs(values["source_start_s"] - media["source_start_s"]) > 0.001
        or abs(duration - media["duration_s"]) > 0.001
    ):
        raise ValueError("Clock receipt must cover the exact full video; trimming requires new evidence")
    gaps = body.get("missing_intervals_s")
    if not isinstance(gaps, list) or len(gaps) > 1000:
        raise ValueError("Clock receipt must explicitly record known missing intervals")
    for gap in gaps:
        if not isinstance(gap, list) or len(gap) != 2:
            raise ValueError("Invalid missing-frame interval")
        start, end = [_number(t, maximum=1e12) for t in gap]
        if not values["source_start_s"] <= start < end <= values["source_end_s"]:
            raise ValueError("Missing-frame interval is outside the source recording")
    ready = not gaps and values["max_error_s"] + values["max_drift_s"] <= policy["max_skew_s"]
    return values | {"common_clock_id": body["common_clock_id"], "ready": ready}


def _frame_clock(body, media):
    """Validate retained full-file probes; never synthesize timing from average FPS.

    Field names follow stock_commissioning.clock_receipt. That finite-window helper
    requires reset PTS and <=1,000 frames; this file audit preserves original PTS,
    explicit decode offset and ordinal correspondence, bounded to 30,000 frames.
    The host supplies an observed audit. This function runs no decoder or normalizer.
    """
    encoded = body.get("encoded_frame_times_s")
    decoded = body.get("decoded_frame_times_s")
    running = body.get("normalized_frame_times_s")
    if not isinstance(encoded, list) or not 2 <= len(encoded) <= 30000:
        raise ValueError("Frame-clock audit requires bounded complete presentation times")
    count = len(encoded)
    for times in (encoded, decoded, running):
        if not isinstance(times, list) or len(times) != count:
            raise ValueError("Frame-clock audit must preserve the complete decoded frame count")
        for point in times:
            _number(point, maximum=1e12)
        if any(b <= a for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("Frame-clock audit requires strictly ordered presentation times")
    for key in ("encoded_frame_indices", "decoded_frame_indices"):
        indices = body.get(key)
        if (
            not isinstance(indices, list)
            or len(indices) != count
            or any(type(value) is not int or value != index for index, value in enumerate(indices))
        ):
            raise ValueError("Frame-clock audit must retain exact complete encoded/decoded ordinals")
    offset = _number(body.get("decoded_pts_offset_s"), minimum=-1e12, maximum=1e12)
    start = media["source_start_s"]
    for source, before, after in zip(encoded, decoded, running, strict=True):
        if abs(before - offset - source) > 1e-6 or abs(after - (source - start)) > 1e-6:
            raise ValueError("Frame-clock audit decode/source mapping differs from retained PTS")
    period = 1 / media["fps"]
    tolerance = min(0.001, period / 100)
    grid_error = max(abs(point - (start + index * period)) for index, point in enumerate(encoded))
    tail_error = abs(encoded[-1] + period - (start + media["duration_s"]))
    ready = grid_error <= tolerance and tail_error <= tolerance
    return {
        "frame_count": count,
        "first_source_pts_s": encoded[0],
        "last_source_pts_s": encoded[-1],
        "cadence_status": "uniform_full_file" if ready else "alignment_required",
        "max_grid_error_s": grid_error,
        "tail_error_s": tail_error,
        "tolerance_s": tolerance,
        "audit_sha256": digest(body),
    }


def _qualification(files, request, pending):
    cameras = request["cameras"]
    if not 3 <= len(cameras) <= 4:
        pending.append("requested_three_or_four_camera_sample_required")
    reference = request.get("qualification_receipt")
    evidence = None
    if reference is None:
        pending.append("real_shop_and_coverage_review_required")
    else:
        evidence = load(files.verify(reference))
        expected = {c["camera_id"]: c["video"]["sha256"] for c in cameras}
        if (
            evidence.get("schema") != "rocoto.mv3dt.qualification.v1"
            or evidence.get("camera_videos") != expected
        ):
            raise ValueError("Dataset review must bind every exact camera and video")
        _identifier(evidence.get("reference"))
        if evidence.get("setting") not in {"real_shop", "synthetic", "other", "unknown"}:
            raise ValueError("Unknown reviewed setting")
        if evidence["setting"] != "real_shop":
            pending.append("real_shop_review_required")
        regions = evidence.get("coverage_regions")
        if not isinstance(regions, list) or len(regions) > 100:
            raise ValueError("Dataset review requires bounded explicit coverage regions")
        counts, region_ids = [], set()
        for region in regions:
            region_id = _identifier(region.get("region_id"))
            visible = region.get("visible_in")
            if (
                region_id in region_ids
                or not isinstance(visible, list)
                or not visible
                or len(set(visible)) != len(visible)
                or not set(visible) <= set(expected)
            ):
                raise ValueError("Coverage review must retain explicit known camera references")
            region_ids.add(region_id)
            counts.append(len(visible))
        if not any(count > 1 for count in counts) or 1 not in counts:
            pending.append("overlapping_and_exclusive_regions_review_required")
    return {
        "status": "pending_evidence" if pending else "qualified_by_host_evidence",
        "pending": sorted(set(pending)),
        "evidence_file": reference,
        "evidence": evidence,
    }


def preflight(dataset_root, request):
    """Validate explicit host mappings; absent qualification evidence stays pending."""
    if not isinstance(request, dict) or request.get("schema") != SCHEMA:
        raise ValueError("Expected the versioned MV3DT dataset request")
    for key in ("dataset_id", "location_id"):
        _identifier(request.get(key))
    detector = request.get("detector")
    if detector not in DETECTORS:
        raise ValueError("Select an explicit supported stock detector")
    cameras = request.get("cameras")
    if not isinstance(cameras, list) or not 2 <= len(cameras) <= MAX_CAMERAS:
        raise ValueError("A dataset has two to twelve explicitly mapped cameras")
    policy = request.get("duration_policy", {})
    if policy.get("kind") != "aligned_full_files":
        raise ValueError("This preflight supports aligned full files; it never trims or shifts sources")
    for key in ("max_skew_s", "max_duration_difference_s"):
        _number(policy.get(key), maximum=1)
    files = _Files(dataset_root)
    pending, results, clocks, frames, sizes = [], [], [], set(), set()
    frame_rates = set()
    target_pending, media_paths = [], set()
    identifiers, sources, videos, calibrations = set(), set(), set(), set()
    for camera in cameras:
        camera_id, source_id = [_identifier(camera.get(k)) for k in ("camera_id", "source_id")]
        video, calibration = camera.get("video"), camera.get("calibration")
        video_path = files.verify(video, maximum=4 * 1024**3)
        relative = Path(video["path"])
        if relative.parent != Path("videos") or relative.suffix != ".mp4":
            raise ValueError("Stock videos must be directly under videos/ with the .mp4 extension")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,123}\.mp4", relative.name):
            raise ValueError("Stock filenames require a plain visible basename before configuration")
        if camera_id in identifiers or source_id in sources or video["path"] in videos:
            raise ValueError("Camera, source and video mappings must each be one-to-one")
        if video_path in media_paths:
            raise ValueError("A source video cannot represent two physical cameras")
        identifiers.add(camera_id)
        sources.add(source_id)
        videos.add(video["path"])
        media_paths.add(video_path)
        media = probe_video(video_path, files.remaining())
        if any(type(media[k]) is not int or not 1 <= media[k] <= 16384 for k in ("width", "height")):
            raise ValueError("Media dimensions exceed the file-workflow limits")
        _number(media["fps"], minimum=0.1, maximum=240)
        _number(media["source_start_s"])
        _number(media["duration_s"], minimum=0.001)
        sizes.add((media["width"], media["height"]))
        frame_rates.add(media["fps"])
        row = {
            "camera_id": camera_id,
            "source_id": source_id,
            "video": video,
            "media": media,
            "evidence_files": {
                key: camera.get(key)
                for key in (
                    "calibration_receipt",
                    "clock_receipt",
                    "frame_clock_receipt",
                    "view_receipt",
                )
            },
        }
        if calibration is None:
            pending.append(f"{camera_id}:calibration_required")
        else:
            calibration_path = files.verify(calibration)
            expected = f"camInfo/{relative.stem}.yml"
            if calibration["path"] != expected or calibration["path"] in calibrations:
                raise ValueError("Explicit calibration pairing requires the exact video basename and .yml")
            calibrations.add(calibration["path"])
            row["calibration"] = calibration | _calibration(calibration_path, detector)
        evidence = files.receipt(camera.get("calibration_receipt"), camera, "calibration")
        if evidence is None or calibration is None:
            pending.append(f"{camera_id}:calibration_review_required")
        else:
            if evidence.get("calibration_sha256") != calibration["sha256"]:
                raise ValueError("Calibration review must bind the selected calibration bytes")
            _identifier(evidence.get("revision_id"))
            _identifier(evidence.get("world_frame_id"))
            if evidence.get("world_units") not in {"meters", "feet"}:
                raise ValueError("Calibration review requires explicit supported world units")
            frames.add((evidence["world_frame_id"], evidence["world_units"]))
            if evidence["world_units"] != "meters":
                pending.append(f"{camera_id}:metric_calibration_normalization_required")
            row["calibration_evidence"] = evidence
        clock = files.receipt(camera.get("clock_receipt"), camera, "clock")
        if clock is None:
            pending.append(f"{camera_id}:synchronization_evidence_required")
        else:
            clock_values = _clock(clock, media, policy)
            clocks.append(clock_values)
            row["clock_evidence"] = clock
            if not clock_values["ready"]:
                pending.append(f"{camera_id}:clock_gaps_or_uncertainty")
        frame_clock = files.receipt(camera.get("frame_clock_receipt"), camera, "frame-clock")
        if frame_clock is None:
            pending.append(f"{camera_id}:frame_clock_audit_required")
        else:
            audit = _frame_clock(frame_clock, media)
            row["frame_clock_audit"] = audit
            if clock is not None:
                clock_values["frame_grid_error_s"] = audit["max_grid_error_s"]
            if audit["cadence_status"] != "uniform_full_file":
                pending.append(f"{camera_id}:stock_file_cadence_alignment_required")
        view = files.receipt(camera.get("view_receipt"), camera, "view")
        if view is None:
            target_pending.append(f"{camera_id}:view_and_continuity_review_required")
        else:
            files.verify(view.get("representative_frame"), maximum=8_000_000)
            point = _number(view.get("source_time_s"))
            if not media["source_start_s"] <= point < media["source_start_s"] + media["duration_s"]:
                raise ValueError("Representative frame time must be inside its exact source recording")
            if view.get("geometry") not in {"high_oblique", "corner", "top_down", "other", "unknown"}:
                raise ValueError("Unknown view qualification value")
            if view.get("footage_kind") not in {"continuous_video", "sparse_frames", "unknown"}:
                raise ValueError("Unknown source continuity qualification")
            if view["geometry"] not in {"high_oblique", "corner"}:
                target_pending.append(f"{camera_id}:corner_or_oblique_view_review_required")
            if view["footage_kind"] != "continuous_video":
                target_pending.append(f"{camera_id}:continuous_source_required")
            row["view_evidence"] = view
        results.append(row)
    # Both stock consumers must see precisely the same camera inventory. The tracker scans
    # .yml only; generate_pub_sub_configs.py scans .yml AND .yaml and assigns sorted indices.
    for directory, suffixes, selected in (
        ("videos", {".mp4"}, videos),
        ("camInfo", {".yml", ".yaml"}, calibrations),
    ):
        folder = files.root / directory
        inventory = (
            {str(p.relative_to(files.root)) for p in folder.iterdir() if p.suffix.lower() in suffixes}
            if folder.exists()
            else set()
        )
        if inventory != selected:
            raise ValueError("Stock input inventory has unmapped files or ambiguous calibration extensions")
    if len(sizes) != 1:
        pending.append("equal_video_resolution_required")
    # This pinned file/BEV path groups per-camera metadata by frame ID.
    # Different source rates require a separately commissioned alignment adapter.
    if len(frame_rates) != 1:
        pending.append("equal_frame_rate_for_stock_file_path_required")
    if len(frames) > 1:
        pending.append("shared_calibration_world_frame_and_units_required")
    synchronized = None
    if len(clocks) == len(cameras):
        if len({c["common_clock_id"] for c in clocks}) != 1:
            pending.append("shared_source_clock_evidence_required")
        else:
            for a, b in itertools.combinations(clocks, 2):
                uncertainty = (
                    a["max_error_s"]
                    + b["max_error_s"]
                    + a["max_drift_s"]
                    + b["max_drift_s"]
                    + a.get("frame_grid_error_s", 0)
                    + b.get("frame_grid_error_s", 0)
                )
                if (
                    max(abs(a[k] - b[k]) for k in ("common_start_s", "common_end_s")) + uncertainty
                    > policy["max_skew_s"]
                ):
                    pending.append("aligned_source_clocks_required")
                if (
                    abs((a["source_end_s"] - a["source_start_s"]) - (b["source_end_s"] - b["source_start_s"]))
                    > policy["max_duration_difference_s"]
                ):
                    pending.append("aligned_file_durations_required")
            start, end = max(c["common_start_s"] for c in clocks), min(c["common_end_s"] for c in clocks)
            if end > start:
                synchronized = {"clock_id": clocks[0]["common_clock_id"], "start_s": start, "end_s": end}
            else:
                pending.append("overlapping_common_clock_interval_required")
    ordered = sorted(results, key=lambda row: row["video"]["path"])
    for index, row in enumerate(ordered, 1):
        row.update(stock_camera_index=index, stock_sensor_id=f"Camera{index}")
    qualification = _qualification(files, request, target_pending)
    pending = sorted(set(pending))
    if pending:
        qualification["status"] = "pending_evidence"
        qualification["pending"].append("technical_stock_inputs_required")
    manifest = (
        None
        if pending
        else {
            "schema": "rocoto.mv3dt.stock-inputs.v1",
            "stock_contract_commit": SOURCE_COMMIT,
            "dataset_root": str(files.root),
            "detector": detector,
            "camera_mappings": ordered,
            "synchronized_interval": synchronized,
            "neighbor_graph_policy": "stock_calibration_then_review",
            "all_cameras_must_see_each_person": False,
            "run_authorized": False,
            "launch_revalidation_required": True,
        }
    )
    return {
        "schema": "rocoto.mv3dt.preflight.v1",
        "dataset_id": request["dataset_id"],
        "location_id": request["location_id"],
        "request_sha256": digest(request),
        "status": "pending_evidence" if pending else "ready_for_stock_configuration",
        "pending": pending,
        "camera_count": len(cameras),
        "cameras": ordered,
        "duration_policy": policy,
        "synchronized_interval": synchronized,
        "stock_manifest": manifest,
        "qualification_basis": "host_attestations_and_local_file_checks",
        "run_verified": False,
        "calibration_accuracy_measured": False,
        "neighbor_graph_review_required": True,
        "target_qualification": qualification,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = preflight(args.dataset_root, load(args.request, limit=1_000_000))
    save(args.output, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "camera_count": result["camera_count"],
                "pending": result["pending"],
                "target_qualification": result["target_qualification"]["status"],
            }
        )
    )
    return 2 if result["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
