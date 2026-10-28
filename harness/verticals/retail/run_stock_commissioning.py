"""Bounded launcher for the unchanged official nvvllmvlm plugin. Dry-run first."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from stock_commissioning import (
    PLUGIN_SHA,
    SOURCE_COMMIT,
    ReceiptJoin,
    build_config,
    canonical,
    clock_receipt,
    file_sha,
    finite_segment_preflight,
    load,
    reasoning_prompt,
    sampling_recipe,
    save,
    validate_supervisor,
    verify_prepared,
)

# The normalization occurs on decoded input, before nvvllmvlm sees any frame.
DECODE_CHAIN = "qtdemux ! h264parse ! avdec_h264 ! identity name=sourceclock single-segment=true"


def probe_clock(path):
    """CPU-only decoder preflight before importing vLLM or creating a model."""
    if Path(path).stat().st_size > 64 * 1024**2:
        raise ValueError("Use a bounded prepared source window")
    raw = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ],
        timeout=30,
    )
    if len(raw) > 1_000_000:
        raise ValueError("Prepared frame timing exceeds preflight bounds")
    encoded = [float(f["best_effort_timestamp_time"]) for f in json.loads(raw)["frames"]]
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    pipeline = Gst.parse_launch(
        "filesrc location=" + json.dumps(str(path)) + " ! " + DECODE_CHAIN + " ! fakesink sync=false"
    )
    clock = pipeline.get_by_name("sourceclock")
    before, after, segments, errors = [], [], [], []

    def capture(pad, info, target):
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            event = info.get_event()
            if event.type == Gst.EventType.SEGMENT and target is before:
                segment = event.parse_segment()
                segments.append(
                    {
                        "format": "time" if segment.format == Gst.Format.TIME else "other",
                        "start_ns": segment.start,
                        "base_ns": segment.base,
                        "time_ns": segment.time,
                        "offset_ns": segment.offset,
                        "rate": segment.rate,
                        "applied_rate": segment.applied_rate,
                    }
                )
        elif info.type & Gst.PadProbeType.BUFFER:
            buffer = info.get_buffer()
            if buffer.pts == Gst.CLOCK_TIME_NONE or len(target) >= 1000:
                errors.append("Invalid or excessive decoded frame timing")
                return Gst.PadProbeReturn.DROP
            target.append(buffer.pts / 1e9)
        return Gst.PadProbeReturn.OK

    flags = Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM
    clock.get_static_pad("sink").add_probe(flags, capture, before)
    clock.get_static_pad("src").add_probe(flags, capture, after)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = pipeline.get_bus().timed_pop_filtered(
            30 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS
        )
        if message is None or message.type != Gst.MessageType.EOS or errors:
            raise ValueError("CPU source-clock probe failed; inference remains stopped")
    finally:
        pipeline.set_state(Gst.State.NULL)
    return clock_receipt(file_sha(path), encoded, before, after, segments)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--base-config", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--plugin-dir",
        type=Path,
        default=Path("/workspace/DeepStream/src/apps/reference_apps/deepstream-vllm-plugin"),
    )
    p.add_argument("--segment-length-sec", type=int, choices=range(1, 11), required=True)
    p.add_argument(
        "--recipe",
        required=True,
        choices=["official-direct-example", "official-reasoning-example", "reasoning-unseeded-stock"],
    )
    p.add_argument("--deadline-epoch", type=float)
    p.add_argument("--supervisor-receipt", type=Path)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    sampling_recipe(a.recipe)  # Reject unsupported settings before even decoding input.
    supervisor = None
    if a.execute:
        if a.supervisor_receipt is None or a.deadline_epoch is None:
            raise ValueError("Provide the commissioned isolated supervisor receipt and fixed deadline")
        supervisor = validate_supervisor(
            load(a.supervisor_receipt),
            worker_pid=os.getpid(),
            worker_pgid=os.getpgrp(),
            worker_sid=os.getsid(0),
            parent_pid=os.getppid(),
            parent_pgid=os.getpgid(os.getppid()),
            deadline_epoch=a.deadline_epoch,
            manifest_sha256=file_sha(a.manifest),
        )
    manifest = load(a.manifest)
    rows = manifest["inputs"]
    base = load(a.base_config)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("Use at most 16 prepared source windows")
    if any(row.get("stream_id") != i for i, row in enumerate(rows)):
        raise ValueError("Stream IDs must match actual mux pad order")
    for row in rows:
        row["media_path"] = str(Path(row["media_path"]).resolve())
        row["prepared_path"] = str(Path(row["prepared_path"]).resolve())
    prepared = {r["stream_id"]: reasoning_prompt(verify_prepared(r)) for r in rows}
    clocks = {r["stream_id"]: probe_clock(r["media_path"]) for r in rows}
    plans = {sid: finite_segment_preflight(clock, a.segment_length_sec) for sid, clock in clocks.items()}
    for p in prepared.values():
        if a.segment_length_sec > p["context"]["end_s"] - p["context"]["start_s"]:
            raise ValueError("Prepared source window does not cover this segment")
    config = build_config(rows, base, prepared, recipe_name=a.recipe, segment_seconds=a.segment_length_sec)
    if not a.execute:
        print(
            canonical(
                {
                    "status": "dry_run",
                    "streams": len(rows),
                    "input_preparation_hashes": [p["input_preparation_sha256"] for p in prepared.values()],
                    "business_schema_preserved": True,
                    "recipe": a.recipe,
                    "segment_plans": plans,
                    "source_clocks": clocks,
                    "inference_started": False,
                }
            )
        )
        return
    if a.deadline_epoch is None or not 120 < a.deadline_epoch - time.time() <= 3300:
        raise ValueError("Use the fixed authorized rental deadline, with preservation headroom")
    plugin = a.plugin_dir.resolve()
    if file_sha(plugin / "gstnvvllmvlm.py") != PLUGIN_SHA:
        raise ValueError("Official plugin bytes differ")
    actual = subprocess.check_output(["git", "-C", str(plugin), "rev-parse", "HEAD"], text=True).strip()
    if actual != SOURCE_COMMIT:
        raise ValueError("Official source commit differs")
    subprocess.run(["git", "-C", str(plugin), "diff", "--quiet", "HEAD", "--", "."], check=True)
    a.output = a.output.resolve()
    a.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    import yaml

    cfg = a.output / "config.yaml"
    cfg.write_text(yaml.safe_dump(config, sort_keys=False))
    cfg.chmod(0o600)
    save(a.output / "manifest.json", manifest)
    for sid, value in prepared.items():
        save(a.output / "prepared" / f"{sid}.json", value)
    os.chdir(a.output)
    sys.path.insert(0, str(plugin))
    os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import gi

    gi.require_version("Gst", "1.0")
    import gstnvvllmvlm
    from gi.repository import GLib, Gst
    from stock_commissioning_observer import Observer

    Gst.init(None)
    Gst.Element.register(None, "nvvllmvlm", Gst.Rank.NONE, gstnvvllmvlm.NvVllmVLM)
    sources = [
        "filesrc location="
        + json.dumps(str(Path(r["media_path"]).resolve()))
        + " ! "
        + DECODE_CHAIN.replace("name=sourceclock", "name=sourceclock" + str(r["stream_id"]))
        + " ! nvvideoconvert ! video/x-raw(memory:NVMM),format=NV12 ! queue ! mux.sink_"
        + str(r["stream_id"])
        for r in rows
    ]
    desc = " ".join(sources) + (
        " nvstreammux name=mux width=448 height=448 enable-padding=1 batch-size=1 live-source=0 "
        "batched-push-timeout=4000000 ! nvvideoconvert ! "
        "video/x-raw(memory:NVMM),format=RGB,width=448,height=448 "
        "! nvvllmvlm name=vlm ! fakesink sync=false"
    )
    started = time.time()
    pipeline = Gst.parse_launch(desc)
    built = time.time()
    loop = GLib.MainLoop()
    errors = []
    eos = [False]
    join = ReceiptJoin(a.output, prepared, plans, clocks, a.recipe)
    observer = Observer(a.output, prepared, join)
    save(
        a.output / "runtime.json",
        {
            "pipeline": desc,
            "plugin_sha256": PLUGIN_SHA,
            "source_commit": actual,
            "sdk_modified": False,
            "build_seconds": built - started,
            "deadline_epoch": a.deadline_epoch,
            "recipe": a.recipe,
            "source_clocks": clocks,
            "segment_plans": plans,
            "supervisor_contract": supervisor,
            "residual_policy": "rejected; one-shot worker exits without invoking stock do_stop",
            "model_input_files": [
                {"stream_id": r["stream_id"], "sha256": file_sha(r["media_path"])} for r in rows
            ],
        },
    )

    def result(element, sid, start, end, text):
        try:
            join.receive(
                "envelope",
                {
                    "stream_id": int(sid),
                    "segment_start_s": float(start),
                    "segment_end_s": float(end),
                    "result_text": text,
                },
            )
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))

    pipeline.get_by_name("vlm").connect("vlm-result", result)

    def message(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            errors.append(str(err))
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            eos[0] = True

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", message)

    def tick():
        if observer.failed.is_set():
            errors.append("Required observer evidence failed")
        if (
            errors
            or join.rejected
            or (eos[0] and len(join.complete) >= len(rows))
            or time.time() >= a.deadline_epoch - 120
        ):
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(1, tick)
    observer.install()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except BaseException as exc:
        errors.append(type(exc).__name__ + ": worker interrupted")
    finally:
        # This is an isolated one-shot process, not a resident service. Stock
        # do_stop queues residual frames with the GLOBAL prompt, so preserve
        # receipts and terminate this process instead of running that path.
        # The host's independent provider deadline remains required.
        save(
            a.output / "completion.json",
            {
                "completed_at": time.time(),
                "completed_windows": len(join.complete),
                "expected_windows": len(rows),
                "input_eos": eos[0],
                "errors": errors,
                "rejected_callbacks": len(join.rejected),
                "residuals_processed": False,
                "partial": len(join.complete) != len(rows),
            },
        )
        print(
            canonical(
                {
                    "completed_windows": len(join.complete),
                    "expected_windows": len(rows),
                    "errors": len(errors),
                }
            )
        )
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0 if len(join.complete) == len(rows) and not errors and not join.rejected else 2)


if __name__ == "__main__":
    raise SystemExit(main())
