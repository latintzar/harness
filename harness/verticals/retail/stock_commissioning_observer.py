"""Read-only profiler; observes NVIDIA/vLLM calls without replacing any callable."""

import hashlib
import sys
import threading
import time
from pathlib import Path

from stock_commissioning import sampling_evidence, save


class Observer:
    def __init__(self, folder, prepared, joiner):
        self.folder = Path(folder)
        self.state = threading.local()
        self.count = 0
        self.prepared = prepared
        self.joiner = joiner
        self.failed = threading.Event()

    def simple(self, v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        if isinstance(v, (list, tuple)):
            return [self.simple(x) for x in v]
        if isinstance(v, dict):
            return {str(k): self.simple(x) for k, x in v.items()}
        if hasattr(v, "tolist"):
            return v.tolist()
        if hasattr(v, "__dict__"):
            return self.simple(vars(v))
        return str(v)

    def hook(self, frame, event, arg):
        name = frame.f_code.co_name
        file = frame.f_code.co_filename
        if name == "_run_vlm_batch" and file.endswith("gstnvvllmvlm.py"):
            if event == "call":
                seg = frame.f_locals.get("segment")
                self.state.active = {
                    "stream_id": int(seg.stream_id),
                    "segment_start_s": seg.start_pts_ns / 1e9,
                    "segment_end_s": seg.end_pts_ns / 1e9,
                    "started_at_unix": time.time(),
                    "processors": [],
                }
                return
            if event == "return" and getattr(self.state, "active", None):
                record = self.state.active
                record["input_preparation_sha256"] = self.prepared[record["stream_id"]][
                    "input_preparation_sha256"
                ]
                record["stock_run_vlm_batch_elapsed_s_before_observation"] = (
                    time.time() - record["started_at_unix"]
                )
                self.state.active = None
                try:
                    loc = frame.f_locals
                    record["serialized_prompt"] = loc.get("inputs", {}).get("prompt")
                    record["reasoning_addon_present"] = (
                        "Write your final answer immediately after the </think> tag."
                        in str(record["serialized_prompt"])
                    )
                    record["system_prompt_present"] = "<|im_start|>system" in str(record["serialized_prompt"])
                    record["effective_prompt_config"] = self.simple(loc.get("prompt_config"))
                    record["sampling_params_kwargs"] = self.simple(loc.get("sampling_params_dict"))
                    record["actual_sampling_params"] = sampling_evidence(loc.get("sampling_params"))
                    record["video_metadata"] = self.simple(loc.get("video_metadata"))
                    record["frame_times_s"] = loc.get("frame_times")
                    record["stock_tensor_shape"] = list(loc["batch_tensor"].shape)
                    record["stock_tensor_dtype"] = str(loc["batch_tensor"].dtype)
                    video = loc["inputs"]["multi_modal_data"]["video"][0].detach().cpu().numpy()
                    record["actual_video_argument_shape"] = list(video.shape)
                    record["frame_hashes_sha256"] = [hashlib.sha256(v.tobytes()).hexdigest() for v in video]
                    record["unique_frame_count"] = len(set(record["frame_hashes_sha256"]))
                    record["mean_absolute_adjacent_frame_difference"] = [
                        float(abs(video[i].astype("float32") - video[i - 1].astype("float32")).mean())
                        for i in range(1, len(video))
                    ]
                    record["per_channel_mean"] = [float(video[:, i].mean()) for i in range(video.shape[1])]
                    record["request_outputs"] = []
                    for out in loc.get("outputs", []):
                        record["request_outputs"].append(
                            {
                                "prompt_token_count": len(out.prompt_token_ids or []),
                                "metrics": self.simple(out.metrics),
                                "completions": [
                                    {
                                        "token_count": len(x.token_ids),
                                        "finish_reason": x.finish_reason,
                                        "stop_reason": self.simple(x.stop_reason),
                                        "reasoning_open_tag": "<think>" in x.text,
                                        "reasoning_close_tag": "</think>" in x.text,
                                        "reasoning_character_count": len(x.text.split("</think>", 1)[0])
                                        if "</think>" in x.text
                                        else None,
                                        "final_character_count": len(x.text.rsplit("</think>", 1)[-1])
                                        if "</think>" in x.text
                                        else len(x.text),
                                    }
                                    for x in out.outputs
                                ],
                            }
                        )
                    self.count += 1
                    from PIL import Image

                    sheet = Image.new("RGB", (448 * 3, 448))
                    for k, i in enumerate([0, len(video) // 2, len(video) - 1]):
                        sheet.paste(
                            Image.fromarray(video[i].transpose(1, 2, 0)).resize((448, 448)), (448 * k, 0)
                        )
                    sheet.save(self.folder / f"actual-model-frames-{self.count:03d}.jpg", quality=92)
                except Exception as exc:
                    record["observer_error"] = repr(exc)
                self.joiner.receive("trace", record)
                return
        active = getattr(self.state, "active", None)
        if not active:
            return
        if name == "generate" and file.endswith("/entrypoints/llm.py"):
            if event == "call":
                active["llm_generate_started_at_unix"] = time.time()
                active["generate_call_sampling_params"] = sampling_evidence(
                    frame.f_locals.get("sampling_params")
                )
            elif event == "return" and "llm_generate_started_at_unix" in active:
                active["llm_generate_elapsed_s"] = time.time() - active["llm_generate_started_at_unix"]
        if (
            event == "return"
            and ("/transformers/" in file or "/vllm/" in file)
            and name in ["__call__", "preprocess", "_call_hf_processor", "_preprocess"]
        ):
            try:
                if hasattr(arg, "keys") and "video_grid_thw" in arg:
                    entry = {
                        "function": name,
                        "source": file,
                        "video_grid_thw": self.simple(arg["video_grid_thw"]),
                    }
                    for key in ["video_metadata", "second_per_grid_ts"]:
                        if key in arg:
                            entry[key] = self.simple(arg[key])
                    if "pixel_values_videos" in arg:
                        pv = arg["pixel_values_videos"]
                        entry["pixel_values_videos_shape"] = list(pv.shape)
                        entry["pixel_values_videos_dtype"] = str(pv.dtype)
                        sample = pv[:: max(1, len(pv) // 128)].float()
                        entry["normalized_pixel_sample"] = {
                            "min": float(sample.min()),
                            "max": float(sample.max()),
                            "mean": float(sample.mean()),
                            "std": float(sample.std()),
                        }
                    active["processors"].append(entry)
            except Exception:
                pass

    def safe_hook(self, frame, event, arg):
        try:
            self.hook(frame, event, arg)
        except Exception as exc:
            self.failed.set()
            self.state.active = None
            save(
                self.folder / "observer-failure.json",
                {"type": type(exc).__name__, "message": str(exc)[:1000]},
                immutable=False,
            )

    def install(self):
        sys.setprofile(self.safe_hook)
        threading.setprofile(self.safe_hook)
