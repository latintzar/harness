"""Private commissioning helpers. No provider or GPU side effects on import."""

import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path

PLUGIN_SHA = "935313052ba60c38ce51ca7394aad1f5f1d9eeb4b52cf0f0fa3da14dea7ac9db"
SOURCE_COMMIT = "0a63ef8be554346820e01c96eaea390d04962939"
REASONING = "You are a helpful assistant."


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def load(path, limit=4_000_000):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("Receipt exceeds its byte limit")
    return json.loads(path.read_text())


def save(path, value, immutable=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = (canonical(value) + "\n").encode()
    if path.exists() and immutable:
        if path.read_bytes() != body:
            raise ValueError("Immutable receipt conflict: " + path.name)
        return
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        if immutable:
            try:
                os.link(tmp, path)
            except FileExistsError:
                if path.read_bytes() != body:
                    raise ValueError("Immutable receipt conflict") from None
        else:
            os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def result_key(stream_id, start, end):
    if type(stream_id) is not int or not finite(start) or not finite(end) or not 0 <= start < end <= 60:
        raise ValueError("Invalid stock stream/segment key")
    return digest([stream_id, float(start), float(end)])[:32]


def verify_prepared(row):
    prepared = load(row["prepared_path"])
    preparation = prepared["input_preparation"]
    if file_sha(row["media_path"]) != preparation["model_input_sha256"]:
        raise ValueError("Prepared input bytes changed")
    if digest(preparation) != prepared["input_preparation_sha256"]:
        raise ValueError("Input preparation receipt changed")
    if hashlib.sha256(prepared["user_prompt"].encode()).hexdigest() != prepared["prompt_sha256"]:
        raise ValueError("Prepared prompt changed")
    keys = (
        "context",
        "source_sha256",
        "clock_basis",
        "source_offset_s",
        "tracker_labels",
        "tracker_sha256",
        "profile_sha256",
        "input_preparation_sha256",
    )
    if (
        digest({k: prepared[k] for k in keys}) != prepared["input_contract_sha256"]
        or "Host input contract: " + prepared["input_contract_sha256"] + "\n" not in prepared["user_prompt"]
    ):
        raise ValueError("Host input contract changed")
    if prepared["clock_basis"] != "segment_relative":
        raise ValueError("This runner admits explicit reset-PTS windows only")
    if prepared["source_offset_s"] != prepared["context"]["start_s"]:
        raise ValueError("Source window offset changed")
    duration = prepared["context"]["end_s"] - prepared["context"]["start_s"]
    if not 0 < duration <= 10:
        raise ValueError("Each logical input is one bounded source window")
    return prepared


# These are published example recipes, not claims of demonstrated model quality.
# Exact seeds are part of parity. Stock does not forward seed or presence_penalty.
REASONING_SUFFIX = (
    "Answer the question using the following format:\n\n"
    "<think>\nYour reasoning.\n</think>\n\n"
    "Write your final answer immediately after the </think> tag."
)
SAMPLING_FIELDS = (
    "temperature",
    "max_tokens",
    "top_p",
    "top_k",
    "repetition_penalty",
    "presence_penalty",
    "seed",
)
STOCK_FORWARD = frozenset(SAMPLING_FIELDS) - {"presence_penalty", "seed"}
STOCK_DEFAULTS = {"presence_penalty": 0.0, "seed": None}
RECIPES = {
    "official-direct-example": {
        "temperature": 0.7,
        "max_tokens": 4096,
        "top_p": 0.8,
        "top_k": 20,
        "repetition_penalty": 1.0,
        "presence_penalty": 1.5,
        "seed": 3407,
    },
    "official-reasoning-example": {
        "temperature": 0.6,
        "max_tokens": 4096,
        "top_p": 0.95,
        "top_k": 20,
        "repetition_penalty": 1.0,
        "presence_penalty": 0.0,
        "seed": 1234,
    },
    "reasoning-unseeded-stock": {
        "temperature": 0.6,
        "max_tokens": 4096,
        "top_p": 0.95,
        "top_k": 20,
        "repetition_penalty": 1.0,
        "presence_penalty": 0.0,
        "seed": None,
    },
}


def sampling_recipe(name, requested=None):
    """Reject unsupported/different settings; never silently drop a recipe knob."""
    if name not in RECIPES:
        raise ValueError("Unknown named commissioning recipe")
    recipe = dict(RECIPES[name])
    if requested is not None and requested != recipe:
        raise ValueError("Sampling settings differ from the named recipe")
    unsupported = [
        key for key, value in recipe.items() if key not in STOCK_FORWARD and STOCK_DEFAULTS.get(key) != value
    ]
    if unsupported:
        raise ValueError("Pinned stock plugin cannot express: " + ", ".join(unsupported))
    return recipe


def sampling_evidence(params):
    """Snapshot actual vLLM object attributes, including defaults not forwarded."""
    if params is None or any(not hasattr(params, key) for key in SAMPLING_FIELDS):
        raise ValueError("Actual SamplingParams evidence is incomplete")
    result = {key: getattr(params, key) for key in SAMPLING_FIELDS}
    canonical(result)
    return result


def reasoning_prompt(prepared):
    """Keep the business schema and host binding; append only the user directive."""
    result = json.loads(canonical(prepared))
    if REASONING_SUFFIX not in result["user_prompt"]:
        result["user_prompt"] += "\n" + REASONING_SUFFIX + "\n"
    result["prompt_sha256"] = hashlib.sha256(result["user_prompt"].encode()).hexdigest()
    return result


def clock_receipt(model_input_sha256, encoded_pts, decoded_pts, running_pts, segments):
    """Verify actual before/after identity probes against encoded presentation PTS.

    No model timestamp is repaired. Gst identity(single-segment=true) converts
    decoded buffer PTS through its observed TIME segment before stock sees them.
    """
    for times in (encoded_pts, decoded_pts, running_pts):
        if (
            not isinstance(times, list)
            or not 2 <= len(times) <= 1000
            or any(not finite(t) or t < 0 for t in times)
            or any(b <= a for a, b in zip(times, times[1:], strict=False))
        ):
            raise ValueError("Use bounded, ordered actual decoded presentation times")
    if (
        not isinstance(segments, list)
        or not segments
        or any(
            segment.get("rate") != 1 or segment.get("applied_rate") != 1 or segment.get("format") != "time"
            for segment in segments
        )
    ):
        raise ValueError("Only observed forward TIME segments at normal speed are supported")
    if len(encoded_pts) != len(decoded_pts) or len(encoded_pts) != len(running_pts):
        raise ValueError("Decoder changed the actual frame count")
    if abs(encoded_pts[0]) > 1e-6 or abs(running_pts[0]) > 1e-6:
        raise ValueError("Prepare a source-bound reset-PTS input before inference")
    if any(abs(a - b) > 1e-6 for a, b in zip(encoded_pts, running_pts, strict=True)):
        raise ValueError("Decoded running time differs from prepared encoded source clock")
    return {
        "model_input_sha256": model_input_sha256,
        "normalizer": "Gst identity single-segment=true",
        "segments": segments,
        "encoded_frame_times_s": encoded_pts,
        "decoded_frame_times_s": decoded_pts,
        "normalized_frame_times_s": running_pts,
        "output_timestamp_repair": False,
    }


def finite_segment_preflight(clock, segment_seconds):
    """Stock finalizes at current_pts-1ns; an endpoint frame alone cannot flush."""
    if type(segment_seconds) is not int or not 1 <= segment_seconds <= 10:
        raise ValueError("Use a supported integer finite-file segment length")
    times = clock["normalized_frame_times_s"]
    end_ns = segment_seconds * 1_000_000_000
    if not any(round(t * 1e9) > end_ns for t in times):
        raise ValueError("Finite input needs an observed frame strictly after the segment endpoint")
    if times[-1] >= 2 * segment_seconds:
        raise ValueError("Prepare one complete segment plus its bounded real trigger/tail frames")
    selected = [t for t in times if t <= segment_seconds]
    return {
        "segment_start_s": 0.0,
        "segment_end_s": float(segment_seconds),
        "visible_first_s": selected[0],
        "visible_last_s": selected[-1],
        "trigger_frame_s": next(t for t in times if t > segment_seconds),
        "residual_policy": "reject_unprepared_segments",
        "source_clock_sha256": digest(clock),
    }


def build_config(rows, base, prepared, *, recipe_name, segment_seconds):
    recipe = sampling_recipe(recipe_name)
    if not 1 <= len(rows) <= 16:
        raise ValueError("Use at most 16 prepared windows")
    # No hidden base sampling overrides can pretend to reach the stock plugin.
    if base.get("inference"):
        raise ValueError("Supply model/video base settings; sampling comes from the named recipe")
    config = json.loads(canonical(base))
    overrides = {}
    for index, row in enumerate(rows):
        if row["stream_id"] != index:
            raise ValueError("Stream IDs must match the actual mux pad order")
        overrides[index] = {
            "user_prompt": prepared[index]["user_prompt"],
            "system_prompt": REASONING,
            **{key: value for key, value in recipe.items() if key in STOCK_FORWARD},
        }
    config.setdefault("model", {}).update(
        max_model_len=24576, gpu_memory_utilization=0.7, gpu_id=0, video_mode=1, tensor_format="pytorch"
    )
    config["segment"] = {
        "length_sec": segment_seconds,
        "overlap_sec": 0,
        "subsample_interval": 1,
        "selection_fps": 8,
    }
    config["inference"] = {
        "user_prompt": "Unprepared residual segment.",
        "system_prompt": REASONING,
        "max_tokens": 1,
        "stream_prompts": overrides,
    }
    config["pipeline"] = {"queue_maxsize": len(rows), "max_wait_timeout": 30}
    return config


class ReceiptJoin:
    """Save raw callbacks first; readiness is not business-event admission."""

    def __init__(self, folder, prepared, plans, clocks, recipe_name):
        self.root = Path(folder)
        self.prepared = prepared
        self.plans = plans
        self.clocks = clocks
        self.recipe = sampling_recipe(recipe_name)
        self.lock = threading.RLock()
        self.complete = set()
        self.rejected = set()

    def receive(self, kind, value):
        if kind not in ("envelope", "trace"):
            raise ValueError("Unsupported receipt kind")
        sid = value["stream_id"]
        key = result_key(sid, value["segment_start_s"], value["segment_end_s"])
        if sid not in self.prepared:
            raise ValueError("Unknown actual input stream")
        with self.lock:
            save(self.root / kind / (key + ".json"), value)
            plan = self.plans[sid]
            if any(value[field] != plan[field] for field in ("segment_start_s", "segment_end_s")):
                return self.reject(key, "unprepared_residual_segment")
            envelope = self.root / "envelope" / (key + ".json")
            trace = self.root / "trace" / (key + ".json")
            if not envelope.exists() or not trace.exists():
                return None
            e, t = load(envelope), load(trace)
            p = self.prepared[sid]
            if t.get("effective_prompt_config", {}).get("user_prompt") != p["user_prompt"]:
                return self.reject(key, "prompt_binding_failed")
            if (
                t.get("actual_sampling_params") != self.recipe
                or t.get("generate_call_sampling_params") != self.recipe
            ):
                return self.reject(key, "effective_sampling_mismatch")
            if t.get("input_preparation_sha256") != p["input_preparation_sha256"] or t.get("observer_error"):
                return self.reject(key, "input_trace_failed")
            times = t.get("frame_times_s")
            source_times = self.clocks[sid]["normalized_frame_times_s"]
            if (
                not isinstance(times, list)
                or not times
                or times[0] != 0
                or any(not finite(x) or x < 0 or x > plan["segment_end_s"] for x in times)
                or any(b <= a for a, b in zip(times, times[1:], strict=False))
                or any(not any(abs(ti - si) <= 1e-6 for si in source_times) for ti in times)
            ):
                return self.reject(key, "actual_input_clock_mismatch")
            completions = [
                completion
                for result in t.get("request_outputs", [])
                for completion in result.get("completions", [])
            ]
            if not completions or any(
                completion.get("finish_reason") != "stop" for completion in completions
            ):
                return self.reject(key, "generation_incomplete")
            record = {
                "receipt_key": key,
                "stream_id": sid,
                "prepared": p,
                "envelope": e,
                "input_trace": t,
                "source_clock": self.clocks[sid],
                "segment_preflight": plan,
                "envelope_sha256": digest(e),
                "input_trace_sha256": digest(t),
            }
            save(self.root / "ready" / (key + ".json"), record)
            self.complete.add(key)
            return record

    def reject(self, key, reason):
        save(self.root / "errors" / (key + ".json"), {"kind": reason, "receipt_key": key})
        self.rejected.add(key)
        return None


def validate_supervisor(
    receipt, *, worker_pid, worker_pgid, worker_sid, parent_pid, parent_pgid, deadline_epoch, manifest_sha256
):
    """Require an explicit host-owned teardown contract for this isolated worker.

    This binds a commissioned supervisor's receipt to its live child. It does not
    independently prove that a provider watchdog or cgroup was installed.
    """
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "rocoto-isolated-worker-supervisor-v1"
        or receipt.get("worker_pid") != worker_pid
        or receipt.get("worker_pgid") != worker_pgid
        or worker_pgid != worker_pid
        or worker_sid != worker_pid
        or receipt.get("supervisor_pid") != parent_pid
        or parent_pgid == worker_pgid
        or receipt.get("deadline_epoch") != deadline_epoch
        or receipt.get("manifest_sha256") != manifest_sha256
        or receipt.get("cleanup") != "kill_process_group_after_exit_or_deadline"
        or receipt.get("descendants_remain_in_group") is not True
        or not isinstance(receipt.get("provider_shutdown_receipt_sha256"), str)
        or len(receipt["provider_shutdown_receipt_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in receipt["provider_shutdown_receipt_sha256"])
    ):
        raise ValueError("Commission an isolated supervisor with descendant cleanup before execution")
    return dict(receipt)
