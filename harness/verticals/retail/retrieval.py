"""Private, CPU-only visual retrieval experiment with timestamped video evidence.

CLIP embeds one representative frame per interval. This is a sampled image
retrieval baseline, not a Cosmos video benchmark or an event/identity detector.
Media, embeddings and generated thumbnails live outside the Git checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from imported_media import ImportedMediaError, imported_record

MODEL_ID = "openai/clip-vit-base-patch32"
MODEL_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
DEFAULT_STATE = Path("/home/peter/.local/state/rocoto/video-mvp")
MODEL_CACHE = "/home/peter/.cache/huggingface/hub"


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-") or "video"


def load_sources(sources: list[str], manifests: list[str]) -> list[dict[str, Any]]:
    """Read local paths only. Descriptions in manifests never become embeddings."""
    records: list[dict[str, Any]] = []
    for manifest in manifests:
        source = Path(manifest).resolve()
        content = json.loads(source.read_text())
        entries = content if isinstance(content, list) else content.get("assets", content.get("videos", []))
        for item in entries:
            record = dict(item)
            video_path = Path(record["path"])
            record["path"] = str(video_path.resolve() if video_path.is_absolute() else (source.parent / video_path).resolve())
            record.setdefault("dataset", source.parent.name)
            record.setdefault("camera", record.get("place", video_path.stem))
            records.append(record)
    for directory in sources:
        root = Path(directory).resolve()
        if root.is_file():
            files = [root]
            dataset = root.parent.name
        else:
            files = sorted(p for p in root.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".webm"})
            dataset = root.name
        for path in files:
            records.append({"path": str(path), "dataset": dataset, "camera": path.stem})
    if not records:
        raise ValueError("Provide a video directory or a manifest containing local video paths")
    unique = {}
    for record in records:
        unique[record["path"]] = record
    return list(unique.values())


class RetrievalIndex:
    def __init__(self, state: str | Path = DEFAULT_STATE):
        self.state = Path(state)
        self._lock = threading.RLock()
        self._model = None
        self._processor = None
        self.clips: list[dict[str, Any]] = []
        self.embeddings = np.empty((0, 512), dtype=np.float32)
        self.report: dict[str, Any] = {}
        self._reload()

    def _reload(self) -> None:
        clips_path = self.state / "clips.json"
        vectors_path = self.state / "embeddings.npy"
        if clips_path.exists() != vectors_path.exists():
            raise ValueError("Index is incomplete; rebuild both clip metadata and embeddings")
        if clips_path.exists():
            clips = json.loads(clips_path.read_text())
            vectors = np.load(vectors_path, allow_pickle=False)
            if vectors.ndim != 2 or len(clips) != len(vectors) or not np.isfinite(vectors).all():
                raise ValueError("Embedding rows must match the saved clip metadata")
            self.clips = clips
            self.embeddings = vectors.astype(np.float32, copy=False)
        if (self.state / "index-report.json").exists():
            self.report = json.loads((self.state / "index-report.json").read_text())

    def _load_model(self) -> None:
        if self._model is not None:
            return
        import torch
        from huggingface_hub import snapshot_download
        from transformers import CLIPModel, CLIPProcessor

        torch.set_num_threads(2)
        # Fetch an explicit, pinned snapshot with the Hub client, then load locally.
        # This also avoids Transformers' older HTTP calls against newer Hub clients.
        files = ["config.json", "pytorch_model.bin", "preprocessor_config.json",
                 "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
                 "vocab.json", "merges.txt"]
        model_path = Path(MODEL_CACHE) / "models--openai--clip-vit-base-patch32" / "snapshots" / MODEL_REVISION
        if not all((model_path / name).is_file() for name in files):
            model_path = Path(snapshot_download(
                MODEL_ID, revision=MODEL_REVISION, cache_dir=MODEL_CACHE,
                allow_patterns=files, max_workers=2, token=False,
            ))
        self._model = CLIPModel.from_pretrained(
            model_path, local_files_only=True, use_safetensors=False,
        ).to("cpu").eval()
        self._processor = CLIPProcessor.from_pretrained(
            model_path, local_files_only=True, use_fast=False,
        )

    def _encode_images(self, images: list[Any]) -> np.ndarray:
        import torch
        with self._lock, torch.inference_mode():
            self._load_model()
            inputs = self._processor(images=images, return_tensors="pt")
            vectors = self._model.get_image_features(**inputs)
            vectors = torch.nn.functional.normalize(vectors, dim=-1)
            return vectors.cpu().numpy().astype(np.float32)

    def _encode_text(self, query: str) -> np.ndarray:
        import torch
        with self._lock, torch.inference_mode():
            self._load_model()
            inputs = self._processor(text=[query], return_tensors="pt", padding=True, truncation=True, max_length=77)
            vectors = self._model.get_text_features(**inputs)
            vectors = torch.nn.functional.normalize(vectors, dim=-1)
            return vectors[0].cpu().numpy().astype(np.float32)

    def build(self, sources: list[dict[str, Any]], interval: float = 2.0) -> dict[str, Any]:
        import cv2
        from PIL import Image

        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Sampling interval must be a positive number")
        start_clock = time.perf_counter()
        self.state.mkdir(parents=True, exist_ok=True)
        thumbnails = self.state / "thumbnails"
        thumbnails.mkdir(exist_ok=True)
        records = []
        vectors = []
        video_reports = []
        seen_ids: set[str] = set()
        with self._lock:
            for source in sources:
                path = Path(source["path"]).resolve(strict=True)
                digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
                video_id = _slug(str(source.get("id", f"{path.stem}-{digest}")))
                if video_id in seen_ids:
                    raise ValueError(f"Duplicate video id: {video_id}")
                seen_ids.add(video_id)
                capture = cv2.VideoCapture(str(path))
                fps = float(capture.get(cv2.CAP_PROP_FPS))
                frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
                if not capture.isOpened() or not math.isfinite(fps) or fps <= 0 or frame_count <= 0:
                    capture.release()
                    raise ValueError(f"Video frame timing is unavailable: {path.name}")
                duration = frame_count / fps
                dataset = str(source.get("dataset", path.parent.name))
                camera = str(source.get("camera", path.stem))
                video_clock = time.perf_counter()
                video_rows = 0
                try:
                    for window in range(math.ceil(duration / interval)):
                        clip_start = window * interval
                        clip_end = min(clip_start + interval, duration)
                        # Ignore sub-frame residue caused by approximate container duration.
                        if clip_end - clip_start < 0.5 / fps:
                            continue
                        frame_index = min(frame_count - 1, int((clip_start + clip_end) * 0.5 * fps))
                        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                        ok, bgr = capture.read()
                        if not ok:
                            raise ValueError(f"Could not decode {path.name} at frame {frame_index}")
                        actual_index = int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))) - 1
                        sample_time = max(0, actual_index) / fps
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        image = Image.fromarray(rgb)
                        embedding = self._encode_images([image])[0]
                        embedding = embedding / max(float(np.linalg.norm(embedding)), 1e-12)
                        clip_id = f"{video_id}-{window:06d}"
                        thumbnail_path = thumbnails / f"{clip_id}.jpg"
                        preview = image.copy()
                        preview.thumbnail((640, 360))
                        preview.save(thumbnail_path, quality=85)
                        record = {
                            "id": clip_id, "video_id": video_id, "dataset": dataset,
                            "camera": camera, "start": round(clip_start, 6), "end": round(clip_end, 6),
                            "sample_time": round(sample_time, 6), "sample_frame": actual_index,
                            "fps": fps, "duration": duration, "path": str(path),
                            "thumbnail_path": str(thumbnail_path), "model": MODEL_ID,
                            "source_reference": source.get("source_url"),
                            "camera_semantics": source.get("camera_semantics", "Independent video; cross-camera identity is unverified"),
                        }
                        records.append(record)
                        vectors.append(embedding)
                        video_rows += 1
                finally:
                    capture.release()
                video_reports.append({"video_id": video_id, "dataset": dataset, "camera": camera,
                                      "duration_seconds": duration, "frames_embedded": video_rows,
                                      "processing_seconds": round(time.perf_counter() - video_clock, 3)})
                print(json.dumps({"indexed": video_id, "frames": video_rows}), flush=True)
            if not vectors:
                raise ValueError("The supplied media produced zero indexable frames")
            matrix = np.asarray(vectors, dtype=np.float32)
            report = {
                "model": MODEL_ID, "model_revision": MODEL_REVISION, "device": "cpu",
                "cpu_threads": 2, "image_batch_size": 1, "embedding_dimensions": int(matrix.shape[1]),
                "sampling_interval_seconds": interval, "frames_per_window": 1,
                "retrieval_kind": "Sampled image-text similarity using CLIP",
                "video_count": len(video_reports), "clip_count": len(records),
                "footage_seconds": round(sum(v["duration_seconds"] for v in video_reports), 3),
                "build_seconds": round(time.perf_counter() - start_clock, 3),
                "provider_spend_usd": 0, "videos": video_reports,
                "limits": ["Frame similarity is a retrieval score, not event confidence",
                           "Action order and events between sampled frames require clip inspection",
                           "Independent camera results; identity links remain unverified"],
            }
            with (self.state / "embeddings.tmp").open("wb") as handle:
                np.save(handle, matrix, allow_pickle=False)
            (self.state / "clips.tmp").write_text(json.dumps(records, indent=2))
            os.replace(self.state / "embeddings.tmp", self.state / "embeddings.npy")
            os.replace(self.state / "clips.tmp", self.state / "clips.json")
            (self.state / "index-report.json").write_text(json.dumps(report, indent=2))
            self.clips, self.embeddings, self.report = records, matrix, report
            return report

    def search(self, query: str, limit: int = 6, dataset: str | None = None) -> list[dict[str, Any]]:
        query = query.strip()
        if not query or not self.clips or limit <= 0:
            return []
        limit = min(int(limit), 50)
        with self._lock:
            eligible = [i for i, clip in enumerate(self.clips) if dataset is None or clip["dataset"] == dataset]
            if not eligible:
                return []
            text_vector = self._encode_text(query)
            scores = self.embeddings[eligible] @ text_vector
            ranked = np.argsort(-scores, kind="stable")[:limit]
            results = []
            for rank in ranked:
                record = self.clips[eligible[int(rank)]]
                result = {key: record[key] for key in ("id", "video_id", "dataset", "camera", "start", "end")}
                result.update({"score": round(float(scores[rank]), 6), "model": MODEL_ID,
                               "sample_time": record["sample_time"], "verification": "candidate",
                               "thumbnail": f"/thumbnails/{record['id']}.jpg",
                               "source_url": f"/media/{record['video_id']}#t={record['start']},{record['end']}"})
                results.append(result)
            return results

    def get_clip(self, clip_id: str) -> dict[str, Any] | None:
        """Private server lookup; includes filesystem paths, never a public API body."""
        return next((dict(c) for c in self.clips if c["id"] == clip_id), None)

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        """Private server lookup for controlled media serving."""
        return next((dict(c) for c in self.clips if c["video_id"] == video_id), None)

    def inspect_clip(self, clip_id: str) -> dict[str, Any]:
        """Decode up to three frames for the agent to inspect; makes no verdict."""
        clip = self.get_clip(clip_id)
        if clip is None:
            raise KeyError(clip_id)
        if imported_record(clip):
            resolver = getattr(self, "imported_media", None)
            if resolver is None:
                raise ImportedMediaError("imported_media_resolver_unavailable")
            return resolver.frame_input(clip)
        import cv2
        output_dir = self.state / "inspection"
        output_dir.mkdir(exist_ok=True)
        capture = cv2.VideoCapture(clip["path"])
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not capture.isOpened() or fps <= 0 or count <= 0:
            capture.release()
            raise ValueError("Evidence media could not be decoded")
        frames = []
        seen = set()
        try:
            for fraction in (0.1, 0.5, 0.9):
                stamp = clip["start"] + fraction * (clip["end"] - clip["start"])
                index = min(count - 1, int(stamp * fps))
                if index in seen:
                    continue
                seen.add(index)
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = capture.read()
                if not ok:
                    continue
                actual = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))) - 1)
                path = output_dir / f"{clip_id}-{actual}.jpg"
                if not cv2.imwrite(str(path), frame):
                    raise OSError("Evidence frame could not be saved")
                frames.append({"time": round(actual / fps, 6), "frame": actual, "path": str(path)})
        finally:
            capture.release()
        return {"clip": clip, "frames": frames, "verification": "Awaiting visual inspection"}

    def status(self) -> dict[str, Any]:
        return {**self.report, "ready": bool(self.clips), "model": MODEL_ID,
                "model_loaded": self._model is not None, "clip_count": len(self.clips),
                "datasets": sorted({clip["dataset"] for clip in self.clips})}


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source", action="append", default=[])
    build.add_argument("--manifest", action="append", default=[])
    build.add_argument("--interval", type=float, default=2)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--dataset")
    search.add_argument("--limit", type=int, default=6)
    commands.add_parser("status")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("clip_id")
    args = parser.parse_args()
    index = RetrievalIndex(args.state)
    if args.command == "build":
        result = index.build(load_sources(args.source, args.manifest), interval=args.interval)
    elif args.command == "search":
        result = index.search(args.query, args.limit, args.dataset)
    elif args.command == "inspect":
        result = index.inspect_clip(args.clip_id)
    else:
        result = index.status()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
