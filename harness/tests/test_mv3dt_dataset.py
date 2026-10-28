"""CPU fixtures for explicit stock dataset mapping; fixtures are not real-shop evidence."""

import hashlib
import json
import shutil
import subprocess
import sys

import mv3dt_dataset as dataset
import pytest
import yaml


def _file(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
    return {"path": path, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def _rewrite(root, reference, change):
    body = json.loads((root / reference["path"]).read_text())
    body.update(change)
    return _file(root, reference["path"], body)


@pytest.fixture
def mapped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dataset,
        "probe_video",
        lambda *_: {
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "source_start_s": 0.0,
            "duration_s": 20.0,
        },
    )
    cameras = []
    for name in ("cam_10", "cam_02", "cam_01"):
        camera_id = "physical-" + name
        video = _file(tmp_path, f"videos/{name}.mp4", ("SYNTHETIC CPU FIXTURE " + name).encode())
        calibration = _file(
            tmp_path,
            f"camInfo/{name}.yml",
            yaml.safe_dump(
                {
                    "projectionMatrix_3x4_w2p": [100, 0, 50, 0, 0, 100, 50, 0, 0, 0, 1, 1],
                    "modelInfo": [{"classID": 1, "height": 1.7, "radius": 0.3}],
                }
            ).encode(),
        )
        common = {"camera_id": camera_id, "video_sha256": video["sha256"], "reference": "synthetic/review"}
        calibration_receipt = _file(
            tmp_path,
            f"evidence/{name}-calibration.json",
            common
            | {
                "schema": "rocoto.mv3dt.calibration.v1",
                "calibration_sha256": calibration["sha256"],
                "revision_id": "fixture-r1",
                "world_frame_id": "fixture-room",
                "world_units": "meters",
            },
        )
        clock_receipt = _file(
            tmp_path,
            f"evidence/{name}-clock.json",
            common
            | {
                "schema": "rocoto.mv3dt.clock.v1",
                "common_clock_id": "fixture-clock",
                "source_start_s": 0,
                "source_end_s": 20,
                "common_start_s": 100,
                "common_end_s": 120,
                "max_error_s": 0.001,
                "max_drift_s": 0.001,
                "missing_intervals_s": [],
            },
        )
        pts = [index / 30 for index in range(600)]
        frame_clock_receipt = _file(
            tmp_path,
            f"evidence/{name}-frame-clock.json",
            common
            | {
                "schema": "rocoto.mv3dt.frame-clock.v1",
                "encoded_frame_indices": list(range(600)),
                "decoded_frame_indices": list(range(600)),
                "encoded_frame_times_s": pts,
                "decoded_frame_times_s": [point + 0.25 for point in pts],
                "normalized_frame_times_s": pts,
                "decoded_pts_offset_s": 0.25,
            },
        )
        view_receipt = _file(
            tmp_path,
            f"evidence/{name}-view.json",
            common
            | {
                "schema": "rocoto.mv3dt.view.v1",
                "geometry": "corner",
                "footage_kind": "continuous_video",
                "source_time_s": 2,
                "representative_frame": _file(tmp_path, f"evidence/{name}.jpg", b"fixture"),
            },
        )
        cameras.append(
            {
                "camera_id": camera_id,
                "source_id": "source-" + name,
                "video": video,
                "calibration": calibration,
                "calibration_receipt": calibration_receipt,
                "clock_receipt": clock_receipt,
                "frame_clock_receipt": frame_clock_receipt,
                "view_receipt": view_receipt,
            }
        )
    qualification = _file(
        tmp_path,
        "evidence/qualification.json",
        {
            "schema": "rocoto.mv3dt.qualification.v1",
            "reference": "synthetic/coverage-review",
            "camera_videos": {c["camera_id"]: c["video"]["sha256"] for c in cameras},
            "setting": "real_shop",
            "coverage_regions": [
                {"region_id": "overlap", "visible_in": [cameras[0]["camera_id"], cameras[1]["camera_id"]]},
                {"region_id": "exclusive", "visible_in": [cameras[2]["camera_id"]]},
            ],
        },
    )
    request = {
        "schema": dataset.SCHEMA,
        "dataset_id": "synthetic-fixture",
        "location_id": "fixture-room",
        "detector": "PeopleNetTransformer",
        "duration_policy": {
            "kind": "aligned_full_files",
            "max_skew_s": 0.05,
            "max_duration_difference_s": 0.05,
        },
        "cameras": cameras,
        "qualification_receipt": qualification,
    }
    return tmp_path, request


def test_explicit_mapping_survives_input_order_and_records_stock_sensor_numbers(mapped):
    root, request = mapped
    result = dataset.preflight(root, request)
    assert result["status"] == "ready_for_stock_configuration"
    assert result["target_qualification"]["status"] == "qualified_by_host_evidence"
    rows = result["stock_manifest"]["camera_mappings"]
    assert [r["camera_id"] for r in rows] == ["physical-cam_01", "physical-cam_02", "physical-cam_10"]
    assert [r["stock_sensor_id"] for r in rows] == ["Camera1", "Camera2", "Camera3"]
    assert result["stock_manifest"]["all_cameras_must_see_each_person"] is False
    assert result["stock_manifest"]["run_authorized"] is False
    assert result["synchronized_interval"] == {"clock_id": "fixture-clock", "start_s": 100, "end_s": 120}
    # Order changes neither camera binding nor the materialized stock input manifest.
    request["cameras"].reverse()
    assert dataset.preflight(root, request)["stock_manifest"] == result["stock_manifest"]


def test_stock_ready_is_distinct_from_real_shop_target_qualification(mapped):
    root, request = mapped
    request["qualification_receipt"] = _rewrite(
        root, request["qualification_receipt"], {"setting": "synthetic"}
    )
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is not None
    assert result["target_qualification"]["status"] == "pending_evidence"
    assert "real_shop_review_required" in result["target_qualification"]["pending"]


@pytest.mark.parametrize(
    "field,changes,reason",
    [
        ("clock_receipt", {"common_start_s": 100.2, "common_end_s": 120.2}, "aligned_source_clocks_required"),
        ("clock_receipt", {"max_drift_s": 0.1}, "clock_gaps_or_uncertainty"),
        ("clock_receipt", {"missing_intervals_s": [[2, 3]]}, "clock_gaps_or_uncertainty"),
        ("calibration_receipt", {"world_frame_id": "unrelated-room"}, "shared_calibration_world_frame"),
    ],
)
def test_clock_and_calibration_gaps_leave_stock_manifest_pending(mapped, field, changes, reason):
    root, request = mapped
    camera = request["cameras"][0]
    camera[field] = _rewrite(root, camera[field], changes)
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert any(reason in value for value in result["pending"])


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"geometry": "unknown"}, "corner_or_oblique_view_review_required"),
        ({"footage_kind": "sparse_frames"}, "continuous_source_required"),
    ],
)
def test_unknown_views_and_held_frames_do_not_qualify_as_requested_dataset(mapped, changes, reason):
    root, request = mapped
    camera = request["cameras"][0]
    camera["view_receipt"] = _rewrite(root, camera["view_receipt"], changes)
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is not None
    assert any(reason in value for value in result["target_qualification"]["pending"])


def test_missing_proofs_are_pending_instead_of_inferred(mapped):
    root, request = mapped
    for camera in request["cameras"]:
        camera.pop("clock_receipt")
        camera.pop("view_receipt")
    request.pop("qualification_receipt")
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert len([p for p in result["pending"] if "synchronization" in p]) == 3
    assert result["target_qualification"]["status"] == "pending_evidence"


@pytest.mark.parametrize("hazard", ["yaml_copy", "unmapped_yml", "mismatched_basename", "extra_video"])
def test_stock_inventory_cannot_fall_back_to_sorted_or_duplicate_pairing(mapped, hazard):
    root, request = mapped
    camera = request["cameras"][0]
    if hazard == "yaml_copy":
        source = root / camera["calibration"]["path"]
        source.with_suffix(".yaml").write_bytes(source.read_bytes())
    elif hazard == "unmapped_yml":
        _file(root, "camInfo/extra.yml", b"extra")
    elif hazard == "extra_video":
        _file(root, "videos/extra.mp4", b"extra")
    else:
        old = root / camera["calibration"]["path"]
        camera["calibration"] = _file(root, "camInfo/different.yml", old.read_bytes())
        old.unlink()
    with pytest.raises(ValueError):
        dataset.preflight(root, request)


@pytest.mark.parametrize("hazard", ["bytes", "receipt_camera", "receipt_media", "duplicate_id", "traversal"])
def test_mapping_tampering_is_rejected(mapped, hazard):
    root, request = mapped
    camera = request["cameras"][0]
    if hazard == "bytes":
        (root / camera["video"]["path"]).write_bytes(b"changed")
    elif hazard in {"receipt_camera", "receipt_media"}:
        changes = {"camera_id": "foreign"} if hazard == "receipt_camera" else {"video_sha256": "f" * 64}
        camera["clock_receipt"] = _rewrite(root, camera["clock_receipt"], changes)
    elif hazard == "duplicate_id":
        camera["camera_id"] = request["cameras"][1]["camera_id"]
    else:
        camera["video"]["path"] = "../outside.mp4"
    with pytest.raises(ValueError):
        dataset.preflight(root, request)


def test_raw_target_flag_and_shared_people_are_never_required_or_trusted(mapped):
    root, request = mapped
    request.pop("qualification_receipt")
    request["qualified_real_shop"] = True
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is not None
    assert result["target_qualification"]["status"] == "pending_evidence"
    assert result["stock_manifest"]["all_cameras_must_see_each_person"] is False


def test_clock_receipt_cannot_hide_trimmed_window_or_time_scaling(mapped):
    root, request = mapped
    camera = request["cameras"][0]
    camera["clock_receipt"] = _rewrite(root, camera["clock_receipt"], {"source_end_s": 19})
    with pytest.raises(ValueError, match="unit-rate"):
        dataset.preflight(root, request)


def test_calibration_duplicate_yaml_key_is_rejected(mapped):
    root, request = mapped
    camera = request["cameras"][0]
    path = root / camera["calibration"]["path"]
    camera["calibration"] = _file(root, camera["calibration"]["path"], path.read_bytes() + b"modelInfo: []\n")
    with pytest.raises(ValueError, match="duplicate keys"):
        dataset.preflight(root, request)


def test_actual_cpu_mp4_probe_preserves_original_metadata(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("CPU media probe tools are not installed")
    path = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=160x90:r=10",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        timeout=10,
        capture_output=True,
    )
    assert dataset.probe_video(path, 5) == {
        "width": 160,
        "height": 90,
        "fps": 10.0,
        "source_start_s": 0.0,
        "duration_s": 1.0,
    }


@pytest.mark.parametrize(
    "behavior,reason",
    [
        ("import os; os.write(1,b'x'*70000)", "output budget"),
        ("import time; time.sleep(2)", "time budget"),
    ],
)
def test_actual_probe_process_is_stopped_at_output_or_time_bound(behavior, reason):
    with pytest.raises(ValueError, match=reason):
        dataset._probe_bytes([sys.executable, "-c", behavior], 0.15)


def test_opencv_calibration_header_requires_explicit_normalization(mapped):
    root, request = mapped
    camera = request["cameras"][0]
    path = root / camera["calibration"]["path"]
    camera["calibration"] = _file(
        root, camera["calibration"]["path"], b"%YAML:1.0\n---\n" + path.read_bytes()
    )
    with pytest.raises(ValueError, match="ordinary YAML normalization"):
        dataset.preflight(root, request)


def test_feet_world_coordinates_require_explicit_metric_normalization(mapped):
    root, request = mapped
    for camera in request["cameras"]:
        camera["calibration_receipt"] = _rewrite(root, camera["calibration_receipt"], {"world_units": "feet"})
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert len([p for p in result["pending"] if "metric_calibration" in p]) == 3


def test_different_rates_require_separately_commissioned_file_alignment(mapped, monkeypatch):
    root, request = mapped
    original = dataset.probe_video
    monkeypatch.setattr(
        dataset,
        "probe_video",
        lambda path, timeout: (
            original(path, timeout)
            | {
                "fps": 15.0 if path.stem == "cam_01" else 30.0,
            }
        ),
    )
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert "equal_frame_rate_for_stock_file_path_required" in result["pending"]


def test_pinned_combined_sample_classes_cover_filtered_detectors(mapped):
    root, request = mapped
    camera = request["cameras"][0]
    path = root / camera["calibration"]["path"]
    calibration = yaml.safe_load(path.read_text())
    calibration["modelInfo"] = [{"classID": n, "height": 1.7, "radius": 0.3} for n in range(6)]
    path.write_text(yaml.safe_dump(calibration))
    for detector in ("PeopleNetTransformer", "RTDETR"):
        assert dataset._calibration(path, detector)["model_class_ids"] == list(range(6))


@pytest.mark.parametrize("name", [".hidden.mp4", "camera name.mp4", "cam#1.mp4", "cam%31.mp4", "cam\n1.mp4"])
def test_stock_raw_uri_and_glob_filename_hazards_are_rejected(mapped, name):
    root, request = mapped
    camera = request["cameras"][0]
    previous = root / camera["video"]["path"]
    camera["video"] = _file(root, "videos/" + name, previous.read_bytes())
    previous.unlink()
    with pytest.raises(ValueError, match="plain visible basename"):
        dataset.preflight(root, request)


def test_average_fps_and_endpoints_cannot_replace_exact_frame_clock_audit(mapped):
    root, request = mapped
    for camera in request["cameras"]:
        camera.pop("frame_clock_receipt")
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert len([reason for reason in result["pending"] if "frame_clock_audit_required" in reason]) == 3


def test_equal_average_fps_and_endpoints_with_different_cadence_stays_pending(mapped):
    root, request = mapped
    camera = request["cameras"][0]
    reference = camera["frame_clock_receipt"]
    audit = json.loads((root / reference["path"]).read_text())
    # Both streams keep 600 frames, identical first/last times, duration and average FPS.
    # One interior frame arrives late, followed by a shorter presentation interval.
    for key in ("encoded_frame_times_s", "decoded_frame_times_s", "normalized_frame_times_s"):
        audit[key][150] += 0.01
    camera["frame_clock_receipt"] = _file(root, reference["path"], audit)
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert any("stock_file_cadence_alignment_required" in value for value in result["pending"])


@pytest.mark.parametrize("fault", ["foreign_video", "repeat_pts", "index_gap", "decoded_pts", "running_pts"])
def test_frame_audit_rejects_foreign_or_mismatched_decode_evidence(mapped, fault):
    root, request = mapped
    camera = request["cameras"][0]
    reference = camera["frame_clock_receipt"]
    audit = json.loads((root / reference["path"]).read_text())
    if fault == "foreign_video":
        audit["video_sha256"] = "f" * 64
    elif fault == "repeat_pts":
        audit["encoded_frame_times_s"][150] = audit["encoded_frame_times_s"][149]
    elif fault == "index_gap":
        audit["decoded_frame_indices"][150] += 1
    elif fault == "decoded_pts":
        audit["decoded_frame_times_s"][150] += 0.01
    else:
        audit["normalized_frame_times_s"][150] += 0.01
    camera["frame_clock_receipt"] = _file(root, reference["path"], audit)
    with pytest.raises(ValueError):
        dataset.preflight(root, request)


def test_allowed_timestamp_quantization_still_obeys_stricter_cross_camera_skew(mapped):
    root, request = mapped
    request["duration_policy"]["max_skew_s"] = 0.0001
    for camera in request["cameras"]:
        camera["clock_receipt"] = _rewrite(
            root, camera["clock_receipt"], {"max_error_s": 0, "max_drift_s": 0}
        )
    camera = request["cameras"][0]
    reference = camera["frame_clock_receipt"]
    audit = json.loads((root / reference["path"]).read_text())
    for key in ("encoded_frame_times_s", "decoded_frame_times_s", "normalized_frame_times_s"):
        audit[key][150] += 0.0002
    camera["frame_clock_receipt"] = _file(root, reference["path"], audit)
    result = dataset.preflight(root, request)
    assert result["stock_manifest"] is None
    assert "aligned_source_clocks_required" in result["pending"]
