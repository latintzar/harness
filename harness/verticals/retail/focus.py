"""CPU image tracking for an operator/agent-selected subject in a bounded clip.

MIL supplies an update success flag, not a calibrated confidence. The sidecar keeps
that distinction explicit. Appearance similarity is a drift check, not identity.
"""
from datetime import datetime, timezone
from imported_media import require_sandbox_derivation

import hashlib
import json
import math
from pathlib import Path
import tempfile
import threading
import time

import cv2
import numpy as np


class FocusTracker:
    def __init__(self, state, *, sample_fps=5, max_width=480, max_seconds=30):
        self.directory = Path(state) / "focus"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sample_fps = sample_fps
        self.max_width = max_width
        self.max_seconds = max_seconds
        self.lock = threading.Lock()

    @staticmethod
    def _histogram(frame, box):
        x, y, width, height = (int(value) for value in box)
        patch = frame[y:y + height, x:x + width]
        if not patch.size:
            return None
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        return cv2.normalize(hist, hist, 1, 0, cv2.NORM_L1)

    @staticmethod
    def _features(gray, box):
        x, y, width, height = (int(value) for value in box)
        mask = np.zeros(gray.shape, np.uint8)
        mask[y:y + height, x:x + width] = 255
        return cv2.goodFeaturesToTrack(gray, maxCorners=70, qualityLevel=.02, minDistance=2, mask=mask, blockSize=3)

    @staticmethod
    def _flow(previous, current, points, box):
        if points is None or len(points) < 4:
            return None, None, 0.
        moved, forward, _ = cv2.calcOpticalFlowPyrLK(previous, current, points, None, winSize=(15, 15), maxLevel=3)
        if moved is None:
            return None, None, 0.
        returned, backward, _ = cv2.calcOpticalFlowPyrLK(current, previous, moved, None, winSize=(15, 15), maxLevel=3)
        if returned is None:
            return None, None, 0.
        good = (forward.ravel() == 1) & (backward.ravel() == 1) & (np.linalg.norm(points - returned, axis=2).ravel() < 1.5)
        before, after = points[good], moved[good]
        if len(before) < 4:
            return None, None, 0.
        transform, inliers = cv2.estimateAffinePartial2D(before, after, method=cv2.RANSAC, ransacReprojThreshold=2., maxIters=100)
        if transform is None or inliers is None or np.mean(inliers) < .6:
            return None, None, 0.
        accepted = inliers.ravel().astype(bool)
        displacement = np.median((after - before)[accepted], axis=0).ravel()
        x, y, width, height = box
        candidate = (round(x + displacement[0]), round(y + displacement[1]), width, height)
        return candidate, after[accepted], float(np.mean(inliers))

    def build(self, record, normalized_bbox, anchor_seconds=0):
        require_sandbox_derivation(record, "focus_tracking")
        try:
            box = [float(value) for value in normalized_bbox]
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a normalized box [x, y, width, height].") from exc
        if len(box) != 4 or not all(math.isfinite(value) for value in box) or min(box) < 0 or min(box[2:]) <= 0 or box[0] + box[2] > 1 or box[1] + box[3] > 1:
            raise ValueError("Choose a normalized box inside the video frame.")
        start, end = float(record["start"]), float(record["end"])
        duration, anchor = end - start, float(anchor_seconds)
        if not math.isfinite(duration) or not 0 < duration <= self.max_seconds or start < 0:
            raise ValueError(f"Choose a source interval of up to {self.max_seconds} seconds.")
        if not math.isfinite(anchor) or not 0 <= anchor < duration:
            raise ValueError("Choose an anchor time inside the clip, measured from its beginning.")
        path = Path(record["path"])
        stat = path.stat()
        key = hashlib.sha256(json.dumps({"clip": record["id"], "path": str(path), "mtime": stat.st_mtime_ns,
            "size": stat.st_size, "start": start, "end": end, "box": box, "anchor": anchor,
            "fps": self.sample_fps, "width": self.max_width, "version": 2}, sort_keys=True).encode()).hexdigest()[:32]
        destination = self.directory / f"{key}.json"
        with self.lock:
            if destination.exists():
                return json.loads(destination.read_text())
            result = self._track(path, record["id"], start, duration, box, anchor)
            result["sidecar_path"] = str(destination)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, suffix=".tmp", delete=False) as file:
                json.dump(result, file)
                temporary = Path(file.name)
            temporary.chmod(0o600)
            temporary.replace(destination)
        return result

    def _track(self, path, clip_id, start, duration, box, anchor):
        began = time.monotonic()
        capture = cv2.VideoCapture(str(path))
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0:
            capture.release()
            raise ValueError("Choose a playable video source for tracking.")
        original_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        original_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        start_frame = round(start * fps)
        capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frames, times = [], []
        step, next_sample = fps / self.sample_fps, 0.
        try:
            for index in range(math.ceil(duration * fps)):
                ok, frame = capture.read()
                if not ok:
                    break
                if index + .5 < next_sample:
                    continue
                target_width = min(self.max_width, frame.shape[1])
                frame = cv2.resize(frame, (target_width, round(frame.shape[0] * target_width / frame.shape[1])), interpolation=cv2.INTER_AREA)
                frames.append(frame)
                times.append(round(index / fps, 4))
                next_sample += step
        finally:
            capture.release()
        if not frames:
            raise ValueError("The chosen interval needs readable video frames.")
        anchor_index = min(range(len(times)), key=lambda index: abs(times[index] - anchor))
        height, width = frames[0].shape[:2]
        pixel_box = (round(box[0] * width), round(box[1] * height), max(1, round(box[2] * width)), max(1, round(box[3] * height)))
        pixel_box = (pixel_box[0], pixel_box[1], min(pixel_box[2], width - pixel_box[0]), min(pixel_box[3], height - pixel_box[1]))
        if min(pixel_box[2:]) < 4:
            raise ValueError("Choose a box at least four tracking pixels wide and high.")
        reference = self._histogram(frames[anchor_index], pixel_box)
        samples = [{"time": value, "bbox": None, "confidence": None, "status": "untracked"} for value in times]
        samples[anchor_index].update(bbox=box, status="anchor", appearance_similarity=1.)
        for indices in (range(anchor_index + 1, len(frames)), range(anchor_index - 1, -1, -1)):
            tracker = cv2.TrackerMIL_create()
            tracker.init(frames[anchor_index], pixel_box)
            previous = cv2.cvtColor(frames[anchor_index], cv2.COLOR_BGR2GRAY)
            previous_box = pixel_box
            points = self._features(previous, previous_box)
            lost = False
            for index in indices:
                if lost:
                    samples[index]["status"] = "lost"
                    continue
                ok, tracked = tracker.update(frames[index])
                current = cv2.cvtColor(frames[index], cv2.COLOR_BGR2GRAY)
                flow_box, followed, flow_support = self._flow(previous, current, points, previous_box)
                if flow_box is not None:
                    tracked, ok = flow_box, True
                x, y, box_width, box_height = (int(value) for value in tracked)
                valid = ok and min(box_width, box_height) >= 4 and x >= 0 and y >= 0 and x + box_width <= width and y + box_height <= height
                similarity = 0.
                if valid:
                    histogram = self._histogram(frames[index], tracked)
                    similarity = max(0., 1. - cv2.compareHist(reference, histogram, cv2.HISTCMP_BHATTACHARYYA))
                # A large appearance change ends this direction instead of displaying a stale box.
                if not valid or similarity < (.45 if flow_box is not None else .6):
                    samples[index].update(status="lost", appearance_similarity=round(similarity, 4))
                    lost = True
                    continue
                samples[index].update(bbox=[round(x / width, 6), round(y / height, 6), round(box_width / width, 6), round(box_height / height, 6)],
                    status="tracked", appearance_similarity=round(similarity, 4), flow_inlier_fraction=round(flow_support, 4))
                previous, previous_box = current, tracked
                points = followed if followed is not None and len(followed) >= 8 else self._features(current, tracked)
        tracked_count = sum(sample["bbox"] is not None for sample in samples)
        return {"clip_id": clip_id, "method": "opencv-lk-mil", "status": "tracked" if tracked_count == len(samples) else "partial",
            "bbox_format": "xywh_normalized", "sample_fps": self.sample_fps, "duration_seconds": duration,
            "anchor_seconds": times[anchor_index], "source_start": start, "source_size": [original_width, original_height],
            "tracking_size": [width, height], "samples": samples, "confidence": None,
            "quality_note": "Image tracking from a selected anchor; appearance similarity is an uncalibrated drift check.",
            "created_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": round(time.monotonic() - began, 3)}
