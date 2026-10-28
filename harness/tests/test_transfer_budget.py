import asyncio
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from transfer_budget import (
    TransferBudget,
    TransferBudgetError,
    TransferBudgetExceeded,
    TransferBudgetMiddleware,
)


def _reserve_worker(path, output):
    try:
        TransferBudget(path, daily_cap=10, monthly_cap=20).reserve(6, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    except Exception as exc:  # process boundary transports only the class name
        output.put(type(exc).__name__)
    else:
        output.put("ok")


def _run_asgi(middleware, path, *, headers=None, body=b"payload", status=200, media_type="video/mp4"):
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", media_type.encode()),
                                (b"content-length", str(len(body)).encode()),
                                *((k.encode(), v.encode()) for k, v in (headers or {}).items())]})
        await send({"type": "http.response.body", "body": body})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    wrapped = TransferBudgetMiddleware(app, budget=middleware)
    asyncio.run(wrapped({"type": "http", "method": "GET", "path": path, "headers": []}, receive, lambda message: _append_async(messages, message)))
    return messages


async def _append_async(messages, message):
    messages.append(message)


def test_initialize_is_durable_and_reservation_is_retained(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=100, monthly_cap=200)
    budget.initialize()
    result = budget.reserve(25, now=datetime(2026, 10, 9, tzinfo=timezone.utc), path="/clips/a.mp4")
    assert result["daily_reserved_bytes"] == 25
    assert budget.status(now=datetime(2026, 10, 9, tzinfo=timezone.utc))["monthly_reserved_bytes"] == 25
    assert json.loads((tmp_path / "transfer.json").read_text())["recent_reservations"][0]["retained_on_disconnect"] is True


def test_daily_and_monthly_caps_are_enforced(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=10, monthly_cap=20)
    budget.initialize()
    budget.reserve(7, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    with pytest.raises(TransferBudgetExceeded):
        budget.reserve(4, now=datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert budget.status(now=datetime(2026, 10, 10, tzinfo=timezone.utc))["daily_reserved_bytes"] == 0
    budget.reserve(10, now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    with pytest.raises(TransferBudgetExceeded):
        budget.reserve(4, now=datetime(2026, 10, 10, tzinfo=timezone.utc))


def test_atomic_reservation_across_processes(tmp_path):
    path = tmp_path / "transfer.json"
    TransferBudget(path, daily_cap=10, monthly_cap=20).initialize()
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    workers = [context.Process(target=_reserve_worker, args=(path, output)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
    assert sorted(output.get(timeout=1) for _ in workers) == ["TransferBudgetExceeded", "ok"]


def test_corrupt_or_missing_ledger_fails_closed(tmp_path):
    path = tmp_path / "transfer.json"
    budget = TransferBudget(path, daily_cap=10, monthly_cap=20)
    with pytest.raises(TransferBudgetError):
        budget.reserve(1)
    path.write_text("{}")
    with pytest.raises(TransferBudgetError):
        budget.reserve(1)
    path.unlink()
    target = tmp_path / "other-ledger.json"
    TransferBudget(target, daily_cap=10, monthly_cap=20).initialize()
    path.symlink_to(target)
    with pytest.raises(TransferBudgetError):
        budget.reserve(1)


def test_media_response_reserves_before_start_and_denies_at_cap(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=5, monthly_cap=20)
    budget.initialize()
    first = _run_asgi(budget, "/clips/a.mp4", body=b"12345")
    assert first[0]["status"] == 200
    second = _run_asgi(budget, "/clips/b.mp4", body=b"1")
    assert second[0]["status"] == 509
    assert json.loads(second[1]["body"]) == {"error": "transfer_budget_exceeded"}


def test_unknown_media_length_and_missing_ledger_fail_closed(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=5, monthly_cap=20)
    budget.initialize()

    async def unknown_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"video/mp4")]})
        await send({"type": "http.response.body", "body": b"later"})

    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    asyncio.run(TransferBudgetMiddleware(unknown_app, budget=budget)(
        {"type": "http", "method": "GET", "path": "/media/a.mp4", "headers": []}, receive, lambda message: _append_async(messages, message)))
    assert messages[0]["status"] == 503

    missing = TransferBudget(tmp_path / "missing.json", daily_cap=5, monthly_cap=20)
    denied = _run_asgi(missing, "/control-room/video.mp4", body=b"x")
    assert denied[0]["status"] == 503


def test_non_media_response_does_not_consume_media_budget(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=1, monthly_cap=2)
    budget.initialize()
    messages = _run_asgi(budget, "/api/session", body=b"123456", media_type="application/json")
    assert messages[0]["status"] == 200
    assert budget.status()["daily_reserved_bytes"] == 0


def test_head_and_invalid_multipart_range_do_not_reserve_body_bytes(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=5, monthly_cap=20)
    budget.initialize()
    head = _run_asgi(budget, "/clips/a.mp4", body=b"12345", status=200)
    # Helper issues GET; exercise the HEAD path directly.
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"video/mp4"), (b"content-length", b"5")]})
        await send({"type": "http.response.body", "body": b"12345"})
    asyncio.run(TransferBudgetMiddleware(app, budget=budget)(
        {"type": "http", "method": "HEAD", "path": "/clips/a.mp4", "headers": []}, receive,
        lambda message: _append_async(messages, message)))
    assert budget.status()["daily_reserved_bytes"] == 5  # only the GET helper reserved

    messages = []
    async def range_error(scope, receive, send):
        await send({"type": "http.response.start", "status": 416,
                    "headers": [(b"content-type", b"video/mp4"), (b"content-range", b"bytes */5")]})
        await send({"type": "http.response.body", "body": b""})
    asyncio.run(TransferBudgetMiddleware(range_error, budget=budget)(
        {"type": "http", "method": "GET", "path": "/clips/a.mp4", "headers": [(b"range", b"bytes=1-2,4-5")]}, receive,
        lambda message: _append_async(messages, message)))
    assert messages[0]["status"] == 416
    assert budget.status()["daily_reserved_bytes"] == 5


def test_bounded_stream_is_charged_and_oversized_body_is_cut(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=10, monthly_cap=20)
    budget.initialize()
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def streamed(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"video/mp4"), (b"content-length", b"4")]})
        await send({"type": "http.response.body", "body": b"12", "more_body": True})
        await send({"type": "http.response.body", "body": b"3456", "more_body": True})
    asyncio.run(TransferBudgetMiddleware(streamed, budget=budget)(
        {"type": "http", "method": "GET", "path": "/media/a.mp4", "headers": []}, receive,
        lambda message: _append_async(messages, message)))
    assert budget.status()["daily_reserved_bytes"] == 4
    assert b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body") == b"1234"
    assert messages[-1].get("more_body") is False


def test_month_rollover_starts_new_month_counter_without_resetting_old_ledger(tmp_path):
    budget = TransferBudget(tmp_path / "transfer.json", daily_cap=20, monthly_cap=20)
    budget.initialize()
    budget.reserve(20, now=datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc))
    assert budget.status(now=datetime(2026, 10, 31, tzinfo=timezone.utc))["monthly_reserved_bytes"] == 20
    assert budget.status(now=datetime(2026, 11, 1, tzinfo=timezone.utc))["monthly_reserved_bytes"] == 0
    budget.reserve(20, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    ledger = json.loads((tmp_path / "transfer.json").read_text())
    assert ledger["periods"]["2026-10"]["reserved_bytes"] == 20
    assert ledger["periods"]["2026-11"]["reserved_bytes"] == 20
