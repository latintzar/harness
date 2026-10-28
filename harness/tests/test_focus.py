"""Focus follows measured image motion and preserves the original evidence interval."""
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from focus import FocusTracker


def moving_video(path):
    rng = np.random.default_rng(7)
    target = rng.integers(60, 245, (24, 20, 3), dtype=np.uint8)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (128, 96))
    for index in range(25):
        frame = np.full((96, 128, 3), 20, dtype=np.uint8)
        frame[30:54, 14 + index:34 + index] = target
        writer.write(frame)
    writer.release()


def test_tracks_motion_in_both_directions_and_caches(tmp_path):
    source = tmp_path / "moving.mp4"
    moving_video(source)
    record = {"id": "moving", "path": str(source), "start": 1., "end": 4.}
    tracker = FocusTracker(tmp_path / "state")
    # Anchor at source time 2 seconds: target x = 24 pixels.
    result = tracker.build(record, [24 / 128, 30 / 96, 20 / 128, 24 / 96], anchor_seconds=1)
    samples = [sample for sample in result["samples"] if sample.get("bbox")]
    assert result["duration_seconds"] == 3
    assert samples[0]["time"] == 0
    assert samples[-1]["time"] > 2.5
    assert samples[-1]["bbox"][0] - samples[0]["bbox"][0] > .045
    assert all(sample["confidence"] is None for sample in samples)
    assert all(0 <= value <= 1 for sample in samples for value in sample["bbox"])
    assert json.loads(Path(result["sidecar_path"]).read_text()) == result
    assert tracker.build(record, [24 / 128, 30 / 96, 20 / 128, 24 / 96], 1) == result


@pytest.mark.parametrize("box", [[.9, .1, .2, .2], [-.1, 0, .1, .1], [0, 0, 0, .1], [0, 0, float("nan"), .1]])
def test_rejects_invalid_normalized_boxes(tmp_path, box):
    with pytest.raises(ValueError, match="box"):
        FocusTracker(tmp_path).build({"id": "bad", "path": "missing", "start": 0, "end": 2}, box)


def test_rejects_anchor_outside_clip(tmp_path):
    with pytest.raises(ValueError, match="anchor"):
        FocusTracker(tmp_path).build({"id": "bad", "path": "missing", "start": 0, "end": 2}, [.1, .1, .2, .2], 3)


def test_disappearing_subject_ends_visible_tracking(tmp_path):
    source = tmp_path / "disappearing.mp4"
    rng = np.random.default_rng(9)
    patch = rng.integers(50, 250, (30, 24, 3), dtype=np.uint8)
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 5, (128, 96))
    for index in range(15):
        frame = np.zeros((96, 128, 3), dtype=np.uint8)
        if index < 6:
            frame[30:60, 40:64] = patch
        writer.write(frame)
    writer.release()
    result = FocusTracker(tmp_path).build({"id": "disappearing", "path": str(source), "start": 0, "end": 3}, [40/128, 30/96, 24/128, 30/96])
    assert result["status"] == "partial"
    assert result["samples"][-1]["bbox"] is None
    assert result["samples"][-1]["status"] == "lost"
