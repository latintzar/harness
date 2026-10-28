"""Search real Cosmos video vectors with their matching CPU-only text tower.

GPU ingestion and local text search use the exact same pinned checkpoint.
The local process loads a small TorchScript text export, never the vision model.
Import accepts actual worker output plus its source manifest, validates clip
hashes, and keeps the original source clock for evidence playback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import io
import json
import math
import os
import re
import stat
from pathlib import Path
import time
import tempfile
import uuid
import zipfile

import numpy as np

from retrieval import DEFAULT_STATE, RetrievalIndex, _slug

MODEL_ID = "nvidia/Cosmos-Embed1-224p"
MODEL_REVISION = "787e0b996f5260a71ad474a283c90539a2e12986"
DIMENSIONS = 256
GENERATION_FILE = "index-generation.json"
GENERATION_SCHEMA = 2
GENERATIONS_DIRECTORY = "generations"
GENERATION_FILES = ("clips.json", "embeddings.npy", "index-report.json")


def _file_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _atomic_write(path, content):
    """Publish one durable file; unique temporary names allow concurrent import preparation."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def generation_paths(state, marker=None):
    """Resolve committed files; callers retain one marker for a coherent view.

    Schema 1 uses root files. Schema 2 uses immutable, host-named directories.
    Reading a path alone does not verify the generation; use load_generation
    when consuming records/vectors rather than measuring a file's byte size.
    """
    state = Path(state)
    if marker is None:
        path = state / GENERATION_FILE
        marker = json.loads(path.read_bytes()) if path.exists() else None
    if marker is not None and not isinstance(marker, dict):
        raise ValueError("Cosmos generation needs a supported schema and generation ID")
    if marker is None or marker.get("schema_version") == 1:
        root = state
    elif (marker.get("schema_version") == GENERATION_SCHEMA
            and isinstance(marker.get("generation_id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", marker["generation_id"])):
        root = state / GENERATIONS_DIRECTORY / marker["generation_id"]
        # These directories belong only to the host importer, never the sandbox.
        for path in (root.parent, root):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Cosmos generation directory must be an immutable host directory")
    else:
        raise ValueError("Cosmos generation needs a supported schema and generation ID")
    return {name: root / name for name in GENERATION_FILES}


def _validate_payload(clips, embeddings, report, marker=None):
    if (not isinstance(clips, list) or not clips or not isinstance(report, dict)
            or not isinstance(embeddings, np.ndarray) or embeddings.dtype != np.dtype(np.float32)
            or embeddings.shape != (len(clips), DIMENSIONS) or not np.isfinite(embeddings).all()
            or np.any(np.linalg.norm(embeddings, axis=1) < 1e-8)):
        raise ValueError("Cosmos generation needs matching clip rows and finite 256-dimensional vectors")
    if (report.get("model") != MODEL_ID or report.get("model_revision") != MODEL_REVISION
            or report.get("embedding_dimensions") != DIMENSIONS
            or report.get("clip_count") != len(clips)
            or marker is not None and marker.get("clip_count") != len(clips)):
        raise ValueError("Cosmos generation report must match its model revision, dimensions and clip count")
    if any(not isinstance(clip, dict) or not isinstance(clip.get("id"), str) or not clip["id"]
            or clip.get("model") != MODEL_ID or clip.get("model_revision") != MODEL_REVISION for clip in clips):
        raise ValueError("Cosmos generation clip metadata must match its model revision")
    if len({clip["id"] for clip in clips}) != len(clips):
        raise ValueError("Cosmos generation clip IDs must be unique")
    # Persist portable JSON only; rejected data never changes the commit marker.
    json.dumps([clips, report], allow_nan=False)


def load_generation(state, *, allow_legacy=True):
    """Read and verify one complete committed snapshot without loading a model.

    Return records, vectors, report, marker, marker identity. Missing indexes
    return empty records. Unmarked legacy files are read only when explicitly
    allowed and are hash-identified for compare-and-swap migration.
    """
    state = Path(state)
    marker_path = state / GENERATION_FILE
    try:
        identity = _file_identity(marker_path)
        marker = json.loads(marker_path.read_bytes())
    except FileNotFoundError:
        if not allow_legacy:
            raise ValueError("Cosmos committed generation is required") from None
        identity, marker = None, None
    try:
        if marker is not None:
            if (not isinstance(marker, dict) or marker.get("schema_version") not in (1, GENERATION_SCHEMA)
                    or not isinstance(marker.get("generation_id"), str) or not marker["generation_id"]):
                raise ValueError("Cosmos generation needs a supported schema and generation ID")
            if (marker.get("model") != MODEL_ID or marker.get("model_revision") != MODEL_REVISION
                    or marker.get("dimensions") != DIMENSIONS):
                raise ValueError("Cosmos generation model revision and dimensions must match the text tower")
            proofs = marker.get("files")
            if not isinstance(proofs, dict) or set(proofs) != set(GENERATION_FILES):
                raise ValueError("Cosmos generation must verify clips, vectors and report together")
        paths = generation_paths(state, marker)
        if marker is None and not any(path.exists() for path in paths.values()):
            return [], np.empty((0, DIMENSIONS), dtype=np.float32), {}, None, None
        before = {name: _file_identity(path) for name, path in paths.items()}
        contents = {}
        for filename, path in paths.items():
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Cosmos generation needs regular committed files")
            content = path.read_bytes()
            if marker is not None:
                proof = proofs[filename]
                if (not isinstance(proof, dict) or proof.get("bytes") != len(content)
                        or proof.get("sha256") != hashlib.sha256(content).hexdigest()):
                    raise ValueError(f"Cosmos generation SHA256 differs for {filename}")
            contents[filename] = content
        clips = json.loads(contents["clips.json"])
        report = json.loads(contents["index-report.json"])
        embeddings = np.load(io.BytesIO(contents["embeddings.npy"]), allow_pickle=False)
        _validate_payload(clips, embeddings, report, marker)
        if any(_file_identity(path) != before[name] for name, path in paths.items()):
            raise ValueError("Cosmos generation changed while loading; retry after publication")
        if identity is not None and _file_identity(marker_path) != identity:
            raise ValueError("Cosmos generation changed while loading; retry after publication")
        if identity is None:
            if marker_path.exists():
                raise ValueError("Cosmos generation changed while loading; retry after publication")
            digest = hashlib.sha256(b"".join(contents[name] for name in GENERATION_FILES)).hexdigest()
            marker = {"schema_version": 0, "generation_id": "legacy-" + digest}
        return clips, embeddings, report, marker, identity
    except (OSError, EOFError) as exc:
        raise ValueError("Cosmos generation is incomplete; retain the previous index and retry") from exc


@contextmanager
def _publication_lock(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    if state.is_symlink():
        raise ValueError("Cosmos state must be a host directory")
    state.chmod(0o700)
    descriptor = os.open(state / ".index-publish.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Cosmos publication lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield state
    finally:
        os.close(descriptor)


def _stage_generation_locked(state, records, vectors, report):
    vectors = np.asarray(vectors, dtype=np.float32)
    _validate_payload(records, vectors, report)
    stream = io.BytesIO()
    np.save(stream, vectors, allow_pickle=False)
    files = {"clips.json": json.dumps(records, indent=2, allow_nan=False).encode(), "embeddings.npy": stream.getvalue(),
             "index-report.json": json.dumps(report, indent=2, allow_nan=False).encode()}
    marker = {"schema_version": GENERATION_SCHEMA, "generation_id": uuid.uuid4().hex,
              "model": MODEL_ID, "model_revision": MODEL_REVISION, "dimensions": DIMENSIONS,
              "clip_count": len(records),
              "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                        for name, content in files.items()}}
    generations = state / GENERATIONS_DIRECTORY
    if generations.is_symlink():
        raise ValueError("Cosmos generation directory must be a host directory")
    generations.mkdir(mode=0o700, exist_ok=True)
    directory = generations / marker["generation_id"]
    directory.mkdir(mode=0o700)
    # The old files remain intact across every failure, including a cold restart.
    # Unreferenced directories after interruption are safe to collect separately.
    for filename, content in files.items():
        _atomic_write(directory / filename, content)
        (directory / filename).chmod(0o400)
    _sync_directory(directory)
    directory.chmod(0o500)
    _sync_directory(generations)
    _sync_directory(state)
    return marker


def _promote_generation_locked(state, marker):
    _atomic_write(state / GENERATION_FILE, json.dumps(marker, indent=2).encode())
    _sync_directory(state)
    return marker


def _publish_generation_locked(state, records, vectors, report):
    return _promote_generation_locked(state, _stage_generation_locked(state, records, vectors, report))


def _publish_generation(state, records, vectors, report):
    """Explicit full replacement used by the historical offline import command."""
    with _publication_lock(state) as state:
        return _publish_generation_locked(state, records, vectors, report)


def _merge_metadata(records, vectors, asset_metadata):
    if not isinstance(asset_metadata, dict) or not isinstance(records, list) or not records:
        raise ValueError("Provide validated records and host-issued source assets")
    result = copy.deepcopy(records)
    vectors = np.asarray(vectors, dtype=np.float32)
    report = {"model": MODEL_ID, "model_revision": MODEL_REVISION,
              "embedding_dimensions": DIMENSIONS, "clip_count": len(result)}
    _validate_payload(result, vectors, report)
    if np.any(np.abs(np.linalg.norm(vectors, axis=1) - 1) > .001):
        raise ValueError("Imported Cosmos vectors must be normalized")
    referenced = {clip.get("video_id") for clip in result}
    if set(asset_metadata) != referenced:
        raise ValueError("Each imported record must bind exactly one host-issued asset")
    for clip in result:
        asset_id = clip["video_id"]
        asset = asset_metadata[asset_id]
        if (not isinstance(asset_id, str) or not re.fullmatch(r"asset-[a-f0-9]{32}", asset_id)
                or not isinstance(asset, dict) or asset.get("asset_id") != asset_id
                or not isinstance(asset.get("source_id"), str) or not re.fullmatch(r"source-[a-f0-9]{32}", asset["source_id"])
                or not isinstance(asset.get("object_id"), str) or not re.fullmatch(r"object-[a-f0-9]{32}", asset["object_id"])
                or not isinstance(asset.get("original_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", asset["original_sha256"])
                or not isinstance(asset.get("collection"), str) or not asset["collection"]
                or clip.get("dataset") != asset["collection"]):
            raise ValueError("Imported records must match their host-issued asset provenance and collection")
        if (not isinstance(clip.get("camera"), str) or not clip["camera"]
                or not isinstance(clip.get("path"), str) or not Path(clip["path"]).is_absolute()
                or not isinstance(clip.get("embedded_clip_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", clip["embedded_clip_sha256"])):
            raise ValueError("Imported records need verified media paths, camera names and embedded clip hashes")
        try:
            if any(type(clip[key]) not in (int, float) for key in ("start", "end", "duration")):
                raise ValueError()
            start, end, duration = (float(clip[key]) for key in ("start", "end", "duration"))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Imported records need their verified source clock") from None
        if not all(math.isfinite(x) for x in (start, end, duration)) or start < 0 or not start < end <= duration + .1:
            raise ValueError("Imported records need finite source intervals within the original")
        for key, value in {"asset_id": asset_id, "source_id": asset["source_id"],
                "source_object_id": asset["object_id"], "original_sha256": asset["original_sha256"]}.items():
            if key in clip and clip[key] != value:
                raise ValueError("Imported record conflicts with host-issued asset provenance")
            clip[key] = value
    return result, vectors


def _combined_report(records, prior, *, parent_generation):
    videos = {}
    for clip in records:
        videos.setdefault(clip["video_id"], {"video_id": clip["video_id"], "dataset": clip["dataset"],
            "camera": clip.get("camera", ""), "duration_seconds": clip.get("duration", clip["end"])})
    report = copy.deepcopy(prior)
    # Per-job timing stays with the immutable prior report and intake job log.
    # It would misdescribe a later union as one worker's run.
    report.pop("import_seconds", None)
    report.pop("worker_timings", None)
    report.update(model=MODEL_ID, model_revision=MODEL_REVISION, embedding_dimensions=DIMENSIONS,
        clip_count=len(records), video_count=len(videos), videos=list(videos.values()),
        footage_seconds=round(sum(clip["end"] - clip["start"] for clip in records), 6),
        archive_seconds=round(sum(video["duration_seconds"] for video in videos.values()), 6),
        text_query_device="cpu", query_provider_spend_usd=0, parent_generation_id=parent_generation,
        import_mode="additive")
    return report


def merge_index(state, records, vectors, *, asset_metadata, expected_generation=None, replace_clip_ids=(), before_commit=None):
    """Commit additive source imports under one load/merge/publication lock.

    Inputs are verified HOST worker records and source-store metadata, never
    browser/model assertions. Equal rows/vectors are idempotent. Replacement is
    explicit and requires compare-and-swap against a concrete prior generation.
    A host before_commit(snapshot, promote) callback runs under this publication
    lock, even for an exact retry. It stages/verifies local Qdrant first, then
    invokes promote exactly once while holding its Qdrant publication lock.
    Failed staging leaves the old public marker intact. The caller also holds
    its intake job fence; neither callback may call providers/decoders or acquire
    SourceStore/index locks again. An interrupted alias/marker/Ready sequence is
    reconciled against immutable evidence; existing queries pin concrete indexes.
    """
    records, vectors = _merge_metadata(records, vectors, asset_metadata)
    replacements = set(replace_clip_ids)
    incoming_ids = {record["id"] for record in records}
    if replacements - incoming_ids or replacements and expected_generation is None:
        raise ValueError("Explicit replacements need incoming rows and an expected generation")
    with _publication_lock(state) as state:
        old_records, old_vectors, report, marker, _ = load_generation(state)
        actual = marker["generation_id"] if marker else None
        if expected_generation is not None and expected_generation != actual:
            raise ValueError("Cosmos generation changed; reload before replacement or conditional import")
        positions = {record["id"]: position for position, record in enumerate(old_records)}
        sources = {record["video_id"]: record for record in old_records}
        if replacements - positions.keys():
            raise ValueError("Replace only clip IDs from the expected committed generation")
        combined, matrix, added, replaced = list(old_records), list(old_vectors), 0, 0
        for record, vector in zip(records, vectors):
            source = sources.get(record["video_id"])
            if source is not None:
                for field in ("dataset", "asset_id", "source_id", "source_object_id", "original_sha256", "path", "duration"):
                    if source.get(field) != record.get(field):
                        raise ValueError("Source occurrence metadata must stay immutable across imported intervals")
            sources[record["video_id"]] = record
            position = positions.get(record["id"])
            if position is None:
                positions[record["id"]] = len(combined)
                combined.append(record); matrix.append(vector); added += 1
            elif combined[position] == record and np.array_equal(matrix[position], vector):
                continue
            elif record["id"] in replacements:
                prior = combined[position]
                for field in ("video_id", "dataset", "asset_id", "source_id", "source_object_id", "original_sha256"):
                    if prior.get(field) != record.get(field):
                        raise ValueError("Replacement cannot change the source occurrence or its original hash")
                combined[position], matrix[position] = record, vector
                replaced += 1
            else:
                raise ValueError("An existing clip ID has different metadata or vectors; explicit replacement required")
        changed = bool(added or replaced)
        matrix = np.asarray(matrix, dtype=np.float32)
        if changed:
            report = _combined_report(combined, report, parent_generation=actual)
            marker = _stage_generation_locked(state, combined, matrix, report)
        receipt = {"generation_id": marker["generation_id"], "clip_count": len(combined),
                   "added": added, "replaced": replaced, "changed": changed}
        promoted = False
        def promote():
            nonlocal promoted
            if promoted: raise ValueError("Promote an index generation exactly once")
            if changed: _promote_generation_locked(state, marker)
            promoted = True
        if before_commit is None:
            promote()
        else:
            before_commit({"records": combined, "vectors": matrix, "report": report,
                           "marker": marker, "receipt": receipt}, promote)
            if not promoted: raise ValueError("Publication callback must confirm generation promotion")
        return receipt


class CosmosRetrievalIndex(RetrievalIndex):
    def __init__(self, state=DEFAULT_STATE / "cosmos"):
        self._generation_identity = None
        super().__init__(state)
        if len(self.clips) and self.embeddings.shape[1] != DIMENSIONS:
            raise ValueError("Cosmos-Embed1-224p requires 256-dimensional video vectors")
        if self.report and (self.report.get("model") != MODEL_ID or self.report.get("model_revision") != MODEL_REVISION):
            raise ValueError("Cosmos index model revision must match the text tower")

    def use_text_encoder(self, source):
        """Host-only sharing of the pinned CPU model, preserving company indexes.

        The known host asset owner loads one model/tokenizer under its existing
        lock. Only query encoding and model readiness delegate; source records,
        vectors, state paths, Qdrant collections and search remain on this index.
        Configure before admitting the company's first query.
        """
        if not isinstance(source, CosmosRetrievalIndex):
            raise ValueError("Choose the commissioned Cosmos text encoder.")
        source = getattr(source, "_text_encoder_source", source)
        if source is self:
            raise ValueError("Keep one host owner for the shared text encoder.")
        with self._lock:
            if self._model is not None:
                raise ValueError("Select shared model assets before loading the company encoder.")
            self._text_encoder_source = source
            self._encode_text = source._encode_text
            self._encoder_ready = source._encoder_ready
            self._encoder_verified = source._encoder_verified

    def _reload(self):
        if (self.state / GENERATION_FILE).exists():
            self.refresh()
        else:
            super()._reload()

    def refresh(self):
        """Load a newly committed generation, preserving the index and encoder on failure.

        Return True after a complete swap, False for an unchanged marker or a
        legacy index without one. Invalid, incomplete or concurrently replaced
        generations raise ValueError and can be retried after the import settles.
        """
        with self._lock:
            marker_path = self.state / GENERATION_FILE
            try:
                identity = _file_identity(marker_path)
            except FileNotFoundError:
                return False
            if identity == self._generation_identity:
                return False
            clips, embeddings, report, _, loaded_identity = load_generation(self.state, allow_legacy=False)
            if loaded_identity != identity:
                raise ValueError("Cosmos generation changed while loading; retry after publication")
            self.clips, self.embeddings, self.report = clips, embeddings, report
            self._generation_identity = identity
            return True

    def _encoder_ready(self):
        return all(path.exists() for path in (
            self.state / "text-encoder.pt", self.state / "text-encoder.json",
            self.state / "text-tokenizer/tokenizer_config.json",
        ))

    def _encoder_verified(self):
        proof = self.state / "text-validation.json"
        if not self._encoder_ready() or not proof.exists():
            return False
        data = json.loads(proof.read_text())
        stat = (self.state / "text-encoder.pt").stat()
        return (data.get("model_revision") == MODEL_REVISION and data.get("encoder_bytes") == stat.st_size
                and data.get("encoder_mtime_ns") == stat.st_mtime_ns)

    def _load_model(self):
        if self._model is not None:
            return
        if not self._encoder_ready():
            raise ValueError("The matching Cosmos text encoder is being prepared")
        import torch
        from transformers import AutoTokenizer

        metadata = json.loads((self.state / "text-encoder.json").read_text())
        if (metadata.get("model") != MODEL_ID or metadata.get("model_revision") != MODEL_REVISION
                or metadata.get("dimensions") != DIMENSIONS or metadata.get("max_length", 128) != 128):
            raise ValueError("Text encoder revision and dimensions must match the Cosmos video index")
        torch.set_num_threads(2)
        encoder = torch.jit.load(str(self.state / "text-encoder.pt"), map_location="cpu").eval()
        tokenizer = AutoTokenizer.from_pretrained(self.state / "text-tokenizer", local_files_only=True)
        self._model, self._processor = encoder, tokenizer

    def _encode_text(self, query):
        import torch
        with self._lock, torch.inference_mode():
            self._load_model()
            tokens = self._processor([query], return_tensors="pt", padding="max_length", truncation=True, max_length=128)
            vector = self._model(tokens["input_ids"], tokens["attention_mask"])
            if tuple(vector.shape) != (1, DIMENSIONS) or not torch.isfinite(vector).all():
                raise ValueError("Cosmos text export must produce one finite 256-dimensional vector")
            if not self._encoder_verified():
                stat = (self.state / "text-encoder.pt").stat()
                (self.state / "text-validation.json").write_text(json.dumps({
                    "model_revision": MODEL_REVISION, "dimensions": DIMENSIONS,
                    "encoder_bytes": stat.st_size, "encoder_mtime_ns": stat.st_mtime_ns,
                    "verification": "Serialized text encoder loaded and produced a finite local CPU query vector",
                }, indent=2))
            return torch.nn.functional.normalize(vector.float(), dim=-1)[0].numpy()

    def search(self, query, limit=6, dataset=None):
        results = super().search(query, limit, dataset)
        for result in results:
            result["model"] = MODEL_ID
            result["source_url"] = f"/?clip={result['id']}"
            result["video_url"] = f"/clips/{result['id']}.mp4"
            result["thumbnail_url"] = result["thumbnail"]
            result["playback_start"] = 0
        return results

    def status(self):
        with self._lock:
            return {**super().status(), "model": MODEL_ID, "model_revision": MODEL_REVISION,
                    "encoder_ready": self._encoder_verified(), "ready": bool(self.clips) and self._encoder_verified(),
                    "text_query_device": "cpu", "query_provider_spend_usd": 0}

    def build(self, *args, **kwargs):
        raise ValueError("Import the verified GPU worker output with prepare_index")


def install_text_assets(encoder_path, tokenizer_zip, metadata_path, state=DEFAULT_STATE / "cosmos"):
    """Install a local export without downloading or executing model source code."""
    import shutil

    state = Path(state)
    metadata = json.loads(Path(metadata_path).read_text())
    if (metadata.get("model") != MODEL_ID or metadata.get("model_revision") != MODEL_REVISION
            or metadata.get("dimensions") != DIMENSIONS):
        raise ValueError("Text encoder metadata must identify the pinned Cosmos model revision")
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    tokenizer_dir = state / "text-tokenizer"
    tokenizer_dir.mkdir(exist_ok=True)
    allowed = {"tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt",
               "added_tokens.json", "config.json"}
    with zipfile.ZipFile(tokenizer_zip) as archive:
        members = archive.infolist()
        if sum(member.file_size for member in members) > 16 * 1024 * 1024:
            raise ValueError("Tokenizer archive exceeds the expected export size")
        for member in members:
            if member.is_dir():
                continue
            parts = Path(member.filename).parts
            if Path(member.filename).is_absolute() or ".." in parts or Path(member.filename).name not in allowed:
                raise ValueError("Tokenizer archive must contain only tokenizer assets")
            # The exporter may add a single containing directory.
            (tokenizer_dir / Path(member.filename).name).write_bytes(archive.read(member))
    if not (tokenizer_dir / "tokenizer_config.json").exists():
        raise ValueError("Tokenizer export needs tokenizer_config.json")
    source = Path(encoder_path)
    if source.resolve() != (state / "text-encoder.pt").resolve():
        shutil.copyfile(source, state / "text-encoder.pt")
    (state / "text-encoder.json").write_text(json.dumps(metadata, indent=2))


def prepare_index(embedding_files, manifest_files, state=DEFAULT_STATE / "cosmos", baseline_state=None,
                  excluded_datasets=("smartspaces-retail",)):
    """Convert authenticated worker artifacts to a searchable source-video index.

The known synthetic retail fixture is excluded by default. Every imported vector
must match an input clip hash and the pinned Cosmos model revision.
"""
    import cv2

    clock = time.perf_counter()
    state = Path(state)
    manifests = {}
    for path in manifest_files:
        content = json.loads(Path(path).read_text())
        entries = content if isinstance(content, list) else content.get("clips", content.get("assets", []))
        for entry in entries:
            key = str(Path(entry["clip"]).resolve())
            if key in manifests:
                raise ValueError("Each worker clip must have exactly one source-manifest record")
            manifests[key] = entry
    baseline = {}
    if baseline_state is not None:
        prior = generation_paths(baseline_state)["clips.json"]
        if prior.exists():
            for clip in json.loads(prior.read_text()):
                baseline.setdefault(str(Path(clip["path"]).resolve()), clip)
    accepted, timings, excluded_count = [], [], 0
    for file in embedding_files:
        payload = json.loads(Path(file).read_text())
        if payload.get("model") != MODEL_ID or payload.get("model_revision") != MODEL_REVISION:
            raise ValueError("Worker model revision must match Cosmos-Embed1-224p")
        if payload.get("dimensions") != DIMENSIONS:
            raise ValueError("Worker vectors must have 256 dimensions")
        timings.append(payload.get("timing", {}))
        for row in payload.get("clips", []):
            prepared_path = Path(row["path"]).resolve(strict=True)
            entry = manifests.get(str(prepared_path))
            if entry is None:
                raise ValueError("Every embedding must resolve to a source manifest")
            source = Path(entry["source"]).resolve(strict=True)
            original = baseline.get(str(source), {})
            dataset = str(entry.get("dataset", original.get("dataset", source.parent.name)))
            if dataset in excluded_datasets:
                excluded_count += 1
                continue
            source_dataset = dataset
            if dataset == "cafe-preview":
                dataset = "cafe"
            digest = hashlib.sha256(prepared_path.read_bytes()).hexdigest()
            if digest != row["sha256"]:
                raise ValueError("Prepared clip SHA256 differs from the embedded input")
            vector = np.asarray(row["embedding"], dtype=np.float32)
            if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-8:
                raise ValueError("Video vectors must contain 256 finite, nonzero values")
            start, end = float(entry["start"]), float(entry["end"])
            if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end <= start:
                raise ValueError("Source intervals must use increasing, finite timestamps")
            worker_duration = float(row["duration_seconds"])
            if not math.isfinite(worker_duration) or abs(worker_duration - (end - start)) > 0.25:
                raise ValueError("Worker clip duration differs from its source interval")
            video_id = original.get("video_id", _slug(f"{source.stem}-{hashlib.sha256(str(source).encode()).hexdigest()[:8]}"))
            accepted.append({"source": source, "entry": entry, "original": original, "dataset": dataset,
                             "source_dataset": source_dataset,
                             "video_id": video_id, "vector": vector / np.linalg.norm(vector),
                             "sha256": digest, "start": start, "end": end,
                             "frames_per_clip": payload.get("frames_per_clip", 8)})
    if not accepted:
        raise ValueError("Provide actual Cosmos embeddings for at least one selected real source")
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    thumbnails = state / "thumbnails"
    thumbnails.mkdir(exist_ok=True)
    records, vectors, sources, seen = [], [], {}, set()
    for row in accepted:
        start, end, source = row["start"], row["end"], row["source"]
        clip_id = f"{row['video_id']}-cosmos-{round(start * 1000):09d}-{round(end * 1000):09d}"
        if clip_id in seen:
            raise ValueError("Duplicate source intervals in the Cosmos import")
        seen.add(clip_id)
        capture = cv2.VideoCapture(str(source))
        fps, count = capture.get(cv2.CAP_PROP_FPS), int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0 or count <= 0:
            capture.release()
            raise ValueError("Original evidence video needs readable frame timestamps")
        duration = count / fps
        if end > duration + 0.1:
            capture.release()
            raise ValueError("The indexed interval exceeds its original evidence video")
        frame_number = min(count - 1, int((start + end) * 0.5 * fps))
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ok, frame = capture.read()
        actual_frame = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))) - 1)
        capture.release()
        if not ok:
            raise ValueError("The original source midpoint must decode for evidence playback")
        height, width = frame.shape[:2]
        scale = min(640 / width, 360 / height, 1)
        thumbnail = thumbnails / f"{clip_id}.jpg"
        if not cv2.imwrite(str(thumbnail), cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))):
            raise OSError("Evidence thumbnail could not be written")
        original, entry = row["original"], row["entry"]
        record = {"id": clip_id, "video_id": row["video_id"], "dataset": row["dataset"],
                  "camera": str(entry.get("camera", original.get("camera", source.stem))),
                  "start": start, "end": end, "duration": duration,
                  "sample_time": round(actual_frame / fps, 6), "sample_frame": actual_frame, "fps": fps,
                  "path": str(source), "thumbnail_path": str(thumbnail),
                  "model": MODEL_ID, "model_revision": MODEL_REVISION,
                  "embedded_clip_sha256": row["sha256"], "frames_embedded": row["frames_per_clip"],
                  "source_reference": entry.get("source_url", original.get("source_reference")),
                  "source_dataset": row["source_dataset"], "provenance": entry.get("provenance", original.get("provenance")),
                  "camera_semantics": entry.get("camera_semantics", original.get("camera_semantics", "Independent source clock; cross-camera links remain unverified"))}
        records.append(record)
        vectors.append(row["vector"])
        sources.setdefault(row["video_id"], {"video_id": row["video_id"], "dataset": row["dataset"],
                                            "camera": record["camera"], "duration_seconds": duration})
    report = {"model": MODEL_ID, "model_revision": MODEL_REVISION, "embedding_dimensions": DIMENSIONS,
              "retrieval_kind": "Cosmos eight-frame video-text similarity", "text_query_device": "cpu",
              "video_count": len(sources), "clip_count": len(records), "videos": list(sources.values()),
              "footage_seconds": round(sum(c["end"] - c["start"] for c in records), 6),
              "archive_seconds": round(sum(s["duration_seconds"] for s in sources.values()), 6),
              "excluded_clip_count": excluded_count, "excluded_datasets": list(excluded_datasets),
              "import_seconds": round(time.perf_counter() - clock, 3), "worker_timings": timings,
              "query_provider_spend_usd": 0,
              "limits": ["Similarity ranks candidates; visual inspection verifies claims",
                         "Coverage is the imported source intervals", "Camera identities remain unverified"]}
    _publish_generation(state, records, vectors, report)
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "cosmos")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("import")
    prepare.add_argument("--embeddings", action="append", required=True, type=Path)
    prepare.add_argument("--manifest", action="append", required=True, type=Path)
    prepare.add_argument("--baseline-state", type=Path, default=DEFAULT_STATE)
    install = commands.add_parser("install-text")
    install.add_argument("--encoder", type=Path, required=True)
    install.add_argument("--tokenizer-zip", type=Path, required=True)
    install.add_argument("--metadata", type=Path, required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--dataset")
    search.add_argument("--limit", type=int, default=6)
    commands.add_parser("status")
    args = parser.parse_args()
    if args.command == "import":
        result = prepare_index(args.embeddings, args.manifest, args.state, args.baseline_state)
    elif args.command == "install-text":
        install_text_assets(args.encoder, args.tokenizer_zip, args.metadata, args.state)
        result = CosmosRetrievalIndex(args.state).status()
    else:
        index = CosmosRetrievalIndex(args.state)
        result = index.search(args.query, args.limit, args.dataset) if args.command == "search" else index.status()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
