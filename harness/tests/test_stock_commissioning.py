"""CPU regressions for observed stock-runner failures; these execute no model."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from stock_commissioning import (
    REASONING_SUFFIX,
    RECIPES,
    ReceiptJoin,
    build_config,
    clock_receipt,
    digest,
    finite_segment_preflight,
    reasoning_prompt,
    sampling_evidence,
    sampling_recipe,
    save,
    verify_prepared,
)


def clock(last=8.625):
    # Measured 2026-10-09 avdec clock pattern, not a claim of a new decoder run.
    encoded = [i / 8 for i in range(round(last * 8) + 1)]
    return clock_receipt(
        "a" * 64,
        encoded,
        [t + 0.25 for t in encoded],
        encoded,
        [
            {
                "format": "time",
                "start_ns": 250_000_000,
                "base_ns": 0,
                "time_ns": 0,
                "offset_ns": 0,
                "rate": 1,
                "applied_rate": 1,
            }
        ],
    )


def prepared_row(tmp_path):
    media = tmp_path / "fixture.mp4"
    media.write_bytes(b"CPU fixture, not actual footage")
    preparation = {"model_input_sha256": hashlib.sha256(media.read_bytes()).hexdigest()}
    p = {
        "context": {"start_s": 0, "end_s": 8.75},
        "source_sha256": "b" * 64,
        "clock_basis": "segment_relative",
        "source_offset_s": 0,
        "tracker_labels": {"P0": "0", "PP0": "P0"},
        "tracker_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "input_preparation_sha256": digest(preparation),
    }
    p["input_contract_sha256"] = digest(p)
    p["input_preparation"] = preparation
    p["user_prompt"] = (
        "Host input contract: " + p["input_contract_sha256"] + "\n"
        'Schema: {{"events": [{{"item_claim": null}}]}}\nTimes: {timestamps}\n'
    )
    p["prompt_sha256"] = hashlib.sha256(p["user_prompt"].encode()).hexdigest()
    path = tmp_path / "prepared.json"
    save(path, p)
    return {"stream_id": 0, "media_path": str(media), "prepared_path": str(path)}, p


def test_measured_decoder_offset_normalizes_input_without_changing_source_or_output_times():
    receipt = clock()
    assert receipt["decoded_frame_times_s"][0] == 0.25
    assert receipt["normalized_frame_times_s"] == receipt["encoded_frame_times_s"]
    assert receipt["output_timestamp_repair"] is False


@pytest.mark.parametrize("fault", ["shifted", "count", "duplicate", "rate"])
def test_unproved_clock_transform_stops_preflight(fault):
    r = clock()
    encoded = r["encoded_frame_times_s"]
    decoded = r["decoded_frame_times_s"]
    normalized = list(r["normalized_frame_times_s"])
    segments = r["segments"]
    if fault == "shifted":
        normalized = [t + 0.25 for t in normalized]
    elif fault == "count":
        normalized.pop()
    elif fault == "duplicate":
        normalized[1] = normalized[0]
    else:
        segments[0]["rate"] = 2
    with pytest.raises(ValueError):
        clock_receipt("a" * 64, encoded, decoded, normalized, segments)


@pytest.mark.parametrize("last,segment", [(8.625, 10), (8, 8)])
def test_file_duration_or_exact_endpoint_does_not_imply_stock_segment_completion(last, segment):
    with pytest.raises(ValueError, match="strictly after"):
        finite_segment_preflight(clock(last), segment)


def test_real_frame_after_endpoint_triggers_completed_segment_and_declares_tail_rejection():
    plan = finite_segment_preflight(clock(), 8)
    assert plan["visible_last_s"] == 8
    assert plan["trigger_frame_s"] == 8.125
    assert plan["residual_policy"] == "reject_unprepared_segments"


@pytest.mark.parametrize(
    "recipe,missing",
    [
        ("official-direct-example", "presence_penalty, seed"),
        ("official-reasoning-example", "seed"),
    ],
)
def test_exact_official_recipe_cannot_silently_drop_unsupported_sampling(recipe, missing):
    with pytest.raises(ValueError, match=missing):
        sampling_recipe(recipe)


def test_explicit_unseeded_reasoning_adaptation_keeps_business_schema_and_exact_label_namespace(tmp_path):
    row, p = prepared_row(tmp_path)
    actual = reasoning_prompt(verify_prepared(row))
    assert actual["tracker_labels"] == {"P0": "0", "PP0": "P0"}
    assert actual["input_contract_sha256"] == p["input_contract_sha256"]
    assert actual["user_prompt"].startswith(p["user_prompt"])
    assert actual["user_prompt"].count(REASONING_SUFFIX) == 1
    assert reasoning_prompt(actual) == actual
    assert '"item_claim": null' in actual["user_prompt"].format(timestamps="0.00s")
    config = build_config([row], {}, {0: actual}, recipe_name="reasoning-unseeded-stock", segment_seconds=8)
    assert config["inference"]["system_prompt"] == "You are a helpful assistant."
    stream = config["inference"]["stream_prompts"][0]
    assert stream["repetition_penalty"] == 1
    assert stream["max_tokens"] == 4096
    assert config["segment"]["selection_fps"] == 8
    with pytest.raises(ValueError, match="sampling comes"):
        build_config(
            [row],
            {"inference": {"presence_penalty": 1.5}},
            {0: actual},
            recipe_name="reasoning-unseeded-stock",
            segment_seconds=8,
        )


def joined_fixture(tmp_path):
    row, p = prepared_row(tmp_path)
    p = reasoning_prompt(p)
    c = clock()
    plan = finite_segment_preflight(c, 8)
    join = ReceiptJoin(tmp_path / "out", {0: p}, {0: plan}, {0: c}, "reasoning-unseeded-stock")
    envelope = {"stream_id": 0, "segment_start_s": 0.0, "segment_end_s": 8.0, "result_text": '{"events":[]}'}
    trace = {
        **{k: envelope[k] for k in ("stream_id", "segment_start_s", "segment_end_s")},
        "effective_prompt_config": {"user_prompt": p["user_prompt"]},
        "actual_sampling_params": RECIPES["reasoning-unseeded-stock"],
        "generate_call_sampling_params": RECIPES["reasoning-unseeded-stock"],
        "input_preparation_sha256": p["input_preparation_sha256"],
        "frame_times_s": [i / 8 for i in range(65)],
        "request_outputs": [{"completions": [{"finish_reason": "stop", "token_count": 8}]}],
    }
    return join, envelope, trace


@pytest.mark.parametrize("order", [("trace", "envelope"), ("envelope", "trace")])
def test_callbacks_and_actual_trace_join_in_both_arrival_orders(tmp_path, order):
    join, envelope, trace = joined_fixture(tmp_path)
    values = {"envelope": envelope, "trace": trace}
    assert join.receive(order[0], values[order[0]]) is None
    record = join.receive(order[1], values[order[1]])
    assert record["envelope"]["result_text"] == '{"events":[]}'
    assert len(join.complete) == 1
    assert join.receive(order[1], values[order[1]]) == record


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("tail", "unprepared_residual_segment"),
        ("prompt", "prompt_binding_failed"),
        ("sampling", "effective_sampling_mismatch"),
        ("generate_sampling", "effective_sampling_mismatch"),
        ("clock", "actual_input_clock_mismatch"),
        ("truncation", "generation_incomplete"),
        ("missing_completion", "generation_incomplete"),
    ],
)
def test_bad_or_residual_results_keep_raw_evidence_and_never_become_ready(tmp_path, fault, reason):
    join, envelope, trace = joined_fixture(tmp_path)
    if fault == "tail":
        envelope.update(segment_start_s=8.0, segment_end_s=16.0)
        trace.update(segment_start_s=8.0, segment_end_s=16.0)
    elif fault == "prompt":
        trace["effective_prompt_config"]["user_prompt"] = "wrong global prompt"
    elif fault == "sampling":
        trace["actual_sampling_params"] = {**trace["actual_sampling_params"], "presence_penalty": 1.5}
    elif fault == "generate_sampling":
        trace["generate_call_sampling_params"] = None
    elif fault == "clock":
        trace["frame_times_s"] = [t + 0.25 for t in trace["frame_times_s"]]
    elif fault == "truncation":
        trace["request_outputs"][0]["completions"][0]["finish_reason"] = "length"
    else:
        trace["request_outputs"] = []
    join.receive("envelope", envelope)
    assert join.receive("trace", trace) is None
    assert not join.complete
    assert list((join.root / "envelope").glob("*.json"))
    assert list((join.root / "trace").glob("*.json"))
    assert json.loads(next((join.root / "errors").glob("*.json")).read_text())["kind"] == reason


def test_observer_records_actual_sampling_object_including_unforwarded_defaults(tmp_path):
    from stock_commissioning_observer import Observer

    actual = SimpleNamespace(**RECIPES["reasoning-unseeded-stock"])
    assert sampling_evidence(actual)["seed"] is None
    assert sampling_evidence(actual)["presence_penalty"] == 0
    observer = Observer(tmp_path, {}, None)
    observer.state.active = {}
    frame = SimpleNamespace(
        f_code=SimpleNamespace(co_name="generate", co_filename="/vllm/entrypoints/llm.py"),
        f_locals={"sampling_params": actual},
    )
    observer.state.active = {"stream_id": 0}
    observer.hook(frame, "call", None)
    assert observer.state.active["generate_call_sampling_params"] == RECIPES["reasoning-unseeded-stock"]
    with pytest.raises(ValueError):
        sampling_evidence(SimpleNamespace(temperature=0.6))


def test_launcher_dry_run_uses_cpu_probe_and_does_not_import_model(tmp_path, monkeypatch, capsys):
    import run_stock_commissioning as runner

    row, p = prepared_row(tmp_path)
    manifest = tmp_path / "manifest.json"
    save(manifest, {"inputs": [row]})
    base = tmp_path / "base.json"
    save(base, {"model": {"path": "/fixture/local/model"}})
    monkeypatch.setattr(runner, "probe_clock", lambda _: clock())
    monkeypatch.setattr(
        runner.sys,
        "argv",
        [
            "runner",
            "--manifest",
            str(manifest),
            "--base-config",
            str(base),
            "--output",
            str(tmp_path / "output"),
            "--segment-length-sec",
            "8",
            "--recipe",
            "reasoning-unseeded-stock",
        ],
    )
    runner.main()
    result = json.loads(capsys.readouterr().out)
    assert result["inference_started"] is False
    assert result["segment_plans"]["0"]["trigger_frame_s"] == 8.125
    assert not (tmp_path / "output").exists()
    assert "single-segment=true" in runner.DECODE_CHAIN


@pytest.mark.parametrize("fault", [None, "worker", "parent_group", "deadline", "cleanup", "manifest"])
def test_execution_requires_supervisor_binding_and_explicit_descendant_cleanup(fault):
    from stock_commissioning import validate_supervisor

    args = {
        "worker_pid": 101,
        "worker_pgid": 101,
        "worker_sid": 101,
        "parent_pid": 99,
        "parent_pgid": 99,
        "deadline_epoch": 12345,
        "manifest_sha256": "a" * 64,
    }
    receipt = {
        "schema": "rocoto-isolated-worker-supervisor-v1",
        "worker_pid": 101,
        "worker_pgid": 101,
        "supervisor_pid": 99,
        "deadline_epoch": 12345,
        "manifest_sha256": "a" * 64,
        "cleanup": "kill_process_group_after_exit_or_deadline",
        "descendants_remain_in_group": True,
        "provider_shutdown_receipt_sha256": "b" * 64,
    }
    if fault == "worker":
        receipt["worker_pid"] = 100
    elif fault == "parent_group":
        args["parent_pgid"] = 101
    elif fault == "deadline":
        receipt["deadline_epoch"] += 3600
    elif fault == "cleanup":
        receipt["cleanup"] = "kill_parent_only"
    elif fault == "manifest":
        receipt["manifest_sha256"] = "c" * 64
    if fault:
        with pytest.raises(ValueError, match="isolated supervisor"):
            validate_supervisor(receipt, **args)
    else:
        assert validate_supervisor(receipt, **args) == receipt


def test_missing_actual_sampling_aborts_observer_and_preserves_failure_receipt(tmp_path):
    from stock_commissioning_observer import Observer

    observer = Observer(tmp_path, {}, None)
    observer.state.active = {"stream_id": 0}
    frame = SimpleNamespace(
        f_code=SimpleNamespace(co_name="generate", co_filename="/vllm/entrypoints/llm.py"),
        f_locals={},
    )
    observer.safe_hook(frame, "call", None)
    assert observer.failed.is_set()
    assert observer.state.active is None
    assert json.loads((tmp_path / "observer-failure.json").read_text())["type"] == "ValueError"
