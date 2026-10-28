"""Bounded Cosmos-Embed1 pilot; use Python, rather than ``modal run``.

    python modal_embed.py --approval /private/approval.json \
        --clips /private/clip1.mp4 /private/clip2.mp4 \
        --queries 'a person dancing' --output /private/embeddings.json --check

Remove --check only for an authorized run after the owner verifies Modal billing.
Admission and its one-use reservation happen before app.run(), image building,
model download or a GPU allocation. There is no deployed endpoint or scheduler.

Gross reservation: $1.50 per cached-image attempt (a budget, not measured throughput).
The initial sixteen-clip pilot took 20.6 worker seconds; a larger batch is bounded.
One H100, <=160 clips of <=10 seconds, <=16 queries, 600-second function timeout,
300-second startup timeout, two-second scale-down, one input, zero task retries.
GPU+2 CPU+32 GiB at the published 2026-10-04 standard rates for 902 seconds is
about $1.08; the reservation adds room for image building and setup. Image-build
cost is not a measured upper bound. The verified provider usage and $0 net-spend
caps are the ultimate enforcement; the local approval is only an attestation.

The public, ungated 224p model is pinned below. Its 2.39 GB weights plus runtime
memory fit the selected H100's generous capacity; a smaller GPU is unmeasured.
NVIDIA's model card tests BF16 on Ampere/Hopper. This code follows its eight-frame
recipe; frame sampling/window length remain experiment settings.
Source: https://huggingface.co/nvidia/Cosmos-Embed1-224p
License: https://huggingface.co/nvidia/Cosmos-Embed1-224p/blob/main/LICENSE
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

from compute_guard import ApprovalError, OWNER, claim_approval, validate_approval


MODEL = "nvidia/Cosmos-Embed1-224p"
MODEL_REVISION = "787e0b996f5260a71ad474a283c90539a2e12986"
WORKSPACE = "peter-17535"
PLANNED_GROSS_USD = "1.50"
MAX_CLIPS = 160
MAX_QUERIES = 16
MAX_DURATION_SECONDS = 10.25  # Container/codec rounding around a ten-second cut.
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _load_clips(paths):
    if not 1 <= len(paths) <= MAX_CLIPS:
        raise ValueError(f"Select 1–{MAX_CLIPS} short clips for the initial experiment.")
    clips, total = [], 0
    for value in paths:
        path = Path(value).expanduser().resolve(strict=True)
        size = path.stat().st_size
        total += size
        if not path.is_file() or size <= 0 or total > MAX_TOTAL_BYTES:
            raise ValueError("The initial clip batch supports up to 64 MiB of regular video files.")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        )
        metadata = json.loads(probe.stdout)
        duration = float(metadata["format"]["duration"])
        video = metadata["streams"][0]
        if (not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS
                or not 0 < video["width"] <= 3840 or not 0 < video["height"] <= 2160):
            raise ValueError("Prepare clips up to ten seconds and 3840×2160 before GPU admission.")
        content = path.read_bytes()
        if len(content) != size:
            raise ValueError("Finish preparing the clips before starting this batch.")
        clips.append({
            "path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
            "duration_seconds": duration, "content": content,
        })
    return clips


def _embed_batch(clips, queries, export_text=True):
    """One remote input; imports and all model work stay inside the GPU worker."""
    import os
    import io
    import tempfile
    import time
    import zipfile
    import cv2
    import numpy as np
    import torch
    from transformers import AutoModel, AutoProcessor

    started = time.monotonic()
    torch.set_num_threads(2)
    if not 1 <= len(clips) <= 160 or len(queries) > 16:
        raise ValueError("Batch limits exceeded.")
    if sum(len(clip["content"]) for clip in clips) > 64 * 1024 * 1024:
        raise ValueError("Input byte limit exceeded.")
    model_id = "nvidia/Cosmos-Embed1-224p"
    revision = "787e0b996f5260a71ad474a283c90539a2e12986"
    model = AutoModel.from_pretrained(
        model_id, revision=revision, trust_remote_code=True, torch_dtype=torch.bfloat16,
    ).to("cuda").eval()
    processor = AutoProcessor.from_pretrained(model_id, revision=revision, trust_remote_code=True)
    loaded_at = time.monotonic()
    results = []
    for clip in clips:
        with tempfile.NamedTemporaryFile(suffix=".mp4") as source:
            source.write(clip["content"])
            source.flush()
            capture = cv2.VideoCapture(source.name)
            try:
                count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
                if count <= 0:
                    raise ValueError("Video decoder requires a readable frame count.")
                frames = []
                for index in np.linspace(0, count - 1, 8, dtype=int):
                    capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
                    ok, frame = capture.read()
                    if not ok:
                        raise ValueError("All eight requested source frames must decode.")
                    frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            finally:
                capture.release()
        # The checkpoint processor divides by 255; pass uint8, not pre-normalized floats.
        batch = np.transpose(np.expand_dims(np.stack(frames), 0), (0, 1, 4, 2, 3))
        with torch.inference_mode():
            inputs = processor(videos=batch).to("cuda", dtype=torch.bfloat16)
            vector = model.get_video_embeddings(**inputs).visual_proj[0].float().cpu().numpy()
        if vector.shape != (256,) or not np.isfinite(vector).all():
            raise ValueError("Model must return 256 finite embedding values.")
        results.append({key: clip[key] for key in ("path", "sha256", "duration_seconds")}
                       | {"embedding": vector.tolist()})
    query_results = []
    if queries:
        with torch.inference_mode():
            inputs = processor(text=queries).to("cuda", dtype=torch.bfloat16)
            vectors = model.get_text_embeddings(**inputs).text_proj.float().cpu().numpy()
        if vectors.shape != (len(queries), 256) or not np.isfinite(vectors).all():
            raise ValueError("Model must return 256 finite values per query.")
        query_results = [{"text": query, "embedding": vector.tolist()}
                         for query, vector in zip(queries, vectors)]
    result = {
        "model": model_id, "model_revision": revision, "dimensions": 256,
        "frames_per_clip": 8, "clips": results, "queries": query_results,
        "timing": {"model_load_seconds": loaded_at - started,
                   "worker_seconds": time.monotonic() - started},
        "measurement_scope": "Worker times exclude image building, GPU startup and local transfer.",
    }
    if not export_text:
        result["text_export"] = {"skipped": "Reuse the previously validated encoder from this exact revision."}
        return result
    # Export the aligned text path for arbitrary local queries. A failed export
    # preserves completed video vectors and records the failure for repair.
    export_started = time.monotonic()
    try:
        class TextEncoder(torch.nn.Module):
            def __init__(self, bert, projection):
                super().__init__()
                self.bert = bert
                self.projection = projection

            def forward(self, input_ids, attention_mask):
                hidden = self.bert(
                    input_ids=input_ids, attention_mask=attention_mask.float(), return_dict=False,
                )[0][:, 0, :]
                return torch.nn.functional.normalize(self.projection(hidden), dim=-1)

        encoder = TextEncoder(model.qformer.bert.cpu().float(), model.text_proj.cpu().float()).eval()
        examples = processor(text=[queries[0] if queries else "a person near a shelf"])
        sample = (examples["input_ids"].cpu().long(), examples["attention_mask"].cpu().long())
        check_inputs = processor(text=["a forklift moving near a worker", "people sitting at cafe tables"])
        check = (check_inputs["input_ids"].cpu().long(), check_inputs["attention_mask"].cpu().long())
        with torch.inference_mode():
            traced = torch.jit.freeze(torch.jit.trace(encoder, sample, check_trace=False, strict=False).eval())
            expected = encoder(*check)
            actual = traced(*check)
            cosine = torch.nn.functional.cosine_similarity(expected, actual).min().item()
            if actual.shape != (2, 256) or not torch.isfinite(actual).all() or cosine < 0.99999:
                raise ValueError("Text export validation requires matching CPU eager and traced vectors.")
            sample_cpu = traced(*sample)[0].numpy()
        precision_cosine = None
        if query_results:
            gpu_vector = np.asarray(query_results[0]["embedding"])
            precision_cosine = float(np.dot(sample_cpu, gpu_vector) / (np.linalg.norm(sample_cpu) * np.linalg.norm(gpu_vector)))
            if precision_cosine < 0.99:
                raise ValueError("CPU text export requires alignment with the original GPU text vector.")
        with tempfile.TemporaryDirectory() as export_dir:
            target = os.path.join(export_dir, "text-encoder.pt")
            traced.save(target)
            # The remote-code package contains a hyphen and numeric revision.
            # Freezing inlines its module types; verify the saved archive loads.
            restored = torch.jit.load(target, map_location="cpu").eval()
            with torch.inference_mode():
                restored_vectors = restored(*check)
                reload_cosine = torch.nn.functional.cosine_similarity(expected, restored_vectors).min().item()
                if reload_cosine < 0.99999:
                    raise ValueError("Saved text encoder must preserve aligned query vectors.")
            with open(target, "rb") as source:
                result["text_encoder_bytes"] = source.read()
            processor.tokenizer.save_pretrained(export_dir)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                for name in os.listdir(export_dir):
                    if name != "text-encoder.pt":
                        zipped.write(os.path.join(export_dir, name), arcname=name)
            result["text_tokenizer_bytes"] = archive.getvalue()
        result["text_export"] = {
            "format": "torchscript", "dtype": "float32", "input_tokens": 128,
            "input_dtypes": ["int64", "int64"], "dimensions": 256,
            "eager_traced_cosine_min": cosine, "gpu_cpu_cosine": precision_cosine,
            "saved_reload_cosine_min": reload_cosine,
            "seconds": time.monotonic() - export_started,
        }
    except Exception as exc:
        result.pop("text_encoder_bytes", None)
        result.pop("text_tokenizer_bytes", None)
        result["text_export"] = {"error": type(exc).__name__ + ": " + str(exc)}
    result["timing"]["worker_seconds"] = time.monotonic() - started
    return result


def _create_worker(modal, *, small_batch=False):
    image = (
        modal.Image.from_registry("pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime")
        .pip_install(
            "transformers==4.51.3", "huggingface-hub==0.36.0", "einops==0.8.1",
            "torchvision==0.23.0", "opencv-python-headless==4.12.0.88", "numpy==2.2.6",
        )
        .add_local_file(__file__, "/root/modal_embed.py")
        .add_local_file(Path(__file__).with_name("compute_guard.py"), "/root/compute_guard.py")
    )
    # Source imports work across local Python 3.12 and this image's Python 3.11;
    # serializing a local Python code object would require matching interpreters.
    from importlib import import_module
    implementation = import_module("modal_embed")._embed_batch
    app = modal.App("rocoto-video-embedding-pilot")
    worker = app.function(
        image=image, gpu="H100", cpu=2, memory=32768,
        min_containers=0, max_containers=1, buffer_containers=0,
        scaledown_window=2, timeout=300 if small_batch else 600,
        startup_timeout=180 if small_batch else 300, retries=0,
        single_use_containers=True, serialized=False, include_source=False,
    )(implementation)
    return app, worker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--clips", nargs="+", required=True)
    parser.add_argument("--queries", nargs="*", default=[])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="Verify prerequisites using read-only provider calls.")
    parser.add_argument("--small-batch", action="store_true", help="Up to 48 clips, $1 reservation, shorter timeout; reuse the exported text encoder.")
    args = parser.parse_args(argv)
    try:
        planned_gross = "1.00" if args.small_batch else PLANNED_GROSS_USD
        approval = validate_approval(args.approval, workspace=WORKSPACE, planned_gross_usd=planned_gross)
        if args.small_batch and len(args.clips) > 48:
            raise ValueError("The shorter run supports up to 48 clips.")
        if len(args.queries) > MAX_QUERIES or any(not query.strip() or len(query) > 500 for query in args.queries):
            raise ValueError("Use up to 16 short text queries (500 characters each).")
        clips = _load_clips(args.clips)
        output = args.output.expanduser().absolute()
        if output.exists() or output.is_symlink():
            raise ValueError("Choose a new private output filename for this experiment.")
        if (not output.parent.is_dir() or output.parent.stat().st_uid != os.geteuid()
                or output.parent.stat().st_mode & 0o077):
            raise ValueError("Prepare an owner-only output directory (0700).")
        import modal
        workspace = modal.Workspace.from_context()
        workspace.hydrate()
        owners = {member.email for member in workspace.members.list() if member.role == "owner"}
        if workspace.name != approval.workspace or OWNER not in owners:
            raise ApprovalError("Authenticate the verified Rocoto Modal workspace before execution.")
        plan = {
            "workspace": workspace.name, "model": MODEL, "revision": MODEL_REVISION,
            "clip_count": len(clips), "query_count": len(args.queries),
            "planned_gross_usd": planned_gross, "net_spend_limit_usd": "0",
            "budget_verification": "owner-attested dashboard readings; provider enforces limits",
            "gpu": "H100", "max_containers": 1, "function_timeout_seconds": 300 if args.small_batch else 600,
            "startup_timeout_seconds": 180 if args.small_batch else 300, "retries": 0,
        }
        if args.check:
            print(json.dumps({"preflight": "passed", "remote_execution": False, "plan": plan}, indent=2))
            return 0
        receipt = claim_approval(approval)
        started = time.monotonic()
        app, worker = _create_worker(modal, small_batch=args.small_batch)
        with modal.enable_output():
            with app.run(environment_name="main"):
                result = worker.remote(clips, args.queries, not args.small_batch)
        result["timing"]["local_elapsed_seconds"] = time.monotonic() - started
        result["budget"] = plan | {"approval_id": approval.approval_id, "receipt": str(receipt)}
        for key, filename in [("text_encoder_bytes", "text-encoder.pt"),
                              ("text_tokenizer_bytes", "text-tokenizer.zip")]:
            if key in result:
                export_path = output.parent / filename
                fd = os.open(export_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(result.pop(key))
                result["text_export"][key.replace("_bytes", "_path")] = str(export_path)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"output": str(output), "clips": len(result["clips"]), "dimensions": 256}))
        return 0
    except (ApprovalError, ValueError, OSError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Preflight/run stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
