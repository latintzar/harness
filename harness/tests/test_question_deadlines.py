"""Clarification deadlines and cardinality; local native transport fixtures only."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import native_questions
from native_questions import NativeQuestions
from test_native_questions import opened, params, reply


def clock(monkeypatch):
    current = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
    monkeypatch.setattr(native_questions, "_now", lambda: current[0].isoformat())
    return current


def test_deadline_is_fixed_on_open_and_survives_reload(tmp_path, monkeypatch):
    current = clock(monkeypatch); store = NativeQuestions(tmp_path); record = opened(store)
    assert record["expires_at"] == (current[0] + timedelta(seconds=30)).isoformat()
    current[0] += timedelta(seconds=12)
    assert opened(NativeQuestions(tmp_path))["expires_at"] == record["expires_at"]


def test_single_choice_rejects_many_and_multiselect_requires_explicit_declaration(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store); sent = []
        with pytest.raises(ValueError):
            await reply(store, record, sent, answers={"format":["PDF", "DOCX"]})
        request = params(); request["questions"][0]["isMultiSelect"] = True
        multiple = opened(store, native_request_id=1, params=request)
        assert multiple["questions"][0]["multiple"] is True
        assert (await reply(store, multiple, sent, answers={"format":["PDF", "DOCX"]}))["status"] == "submitted"
        assert len(sent) == 1
    asyncio.run(scenario())


def test_expired_reply_records_timeout_and_releases_native_wait_once(tmp_path, monkeypatch):
    async def scenario():
        current = clock(monkeypatch); store = NativeQuestions(tmp_path); record = opened(store); sent = []
        current[0] += timedelta(seconds=30)
        result = await reply(store, record, sent)
        assert result["status"] == "timed_out" and result["resolution_reason"] == "timed_out"
        assert sent == [(0, {"answers":{}})]
        assert (await reply(store, record, sent))["status"] == "timed_out" and len(sent) == 1
    asyncio.run(scenario())


def test_browser_cannot_force_early_timeout(tmp_path, monkeypatch):
    async def scenario():
        clock(monkeypatch); store = NativeQuestions(tmp_path); record = opened(store); sent = []
        with pytest.raises(ValueError):
            await reply(store, record, sent, action="timeout", answers=None)
        assert sent == [] and store.get(record["id"], owner="alice", root_chat_id="root")["status"] == "pending"
    asyncio.run(scenario())


def test_host_expiry_without_browser_is_owner_generation_scoped(tmp_path, monkeypatch):
    async def scenario():
        current = clock(monkeypatch); store = NativeQuestions(tmp_path); record = opened(store)
        other = opened(store, root_chat_id="other", generation="new")
        sent = []
        async def deliver(identity, response): sent.append((identity, response))
        options = dict(owner="alice", root_chat_id="root", generation="run:1", deliver=deliver, cancelled=asyncio.Event())
        assert await store.expire(**options) == []
        current[0] += timedelta(seconds=30)
        assert await store.expire(**{**options, "owner":"bob"}) == []
        expired = await store.expire(**options)
        assert [item["id"] for item in expired] == [record["id"]]
        assert expired[0]["status"] == "timed_out" and sent == [(0, {"answers":{}})]
        assert await store.expire(**options) == []
        assert store.get(other["id"], owner="alice", root_chat_id="other")["status"] == "pending"
    asyncio.run(scenario())


def test_timeout_and_answer_share_one_durable_delivery_claim(tmp_path, monkeypatch):
    async def scenario():
        current = clock(monkeypatch); store = NativeQuestions(tmp_path); record = opened(store)
        started, release = asyncio.Event(), asyncio.Event(); sent = []
        async def deliver(identity, response):
            sent.append((identity, response)); started.set(); await release.wait()
        pending = asyncio.create_task(reply(store, record, sent, deliver=deliver))
        await started.wait(); current[0] += timedelta(seconds=31)
        assert await store.expire(owner="alice", root_chat_id="root", generation="run:1", deliver=deliver, cancelled=asyncio.Event()) == []
        release.set(); assert (await pending)["status"] == "submitted" and len(sent) == 1
    asyncio.run(scenario())


def test_failed_timeout_delivery_remains_uncertain_without_replay(tmp_path, monkeypatch):
    async def scenario():
        current = clock(monkeypatch); store = NativeQuestions(tmp_path); record = opened(store); attempts = []
        current[0] += timedelta(seconds=31)
        async def deliver(identity, response): attempts.append(identity); raise ConnectionError("private transport detail")
        result = await reply(store, record, [], action="timeout", answers=None, deliver=deliver)
        assert result["status"] == "delivery_uncertain" and 'private' not in str(result)
        assert (await reply(store, record, []))["status"] == "delivery_uncertain" and attempts == [0]
    asyncio.run(scenario())


def test_native_stream_times_out_without_a_browser_then_releases_wait(tmp_path, monkeypatch):
    from test_isolated_turn import tree_fixture, question
    async def scenario():
        current = clock(monkeypatch); agent, run, connection, tree = tree_fixture(tmp_path)
        request = (await tree.event(question()))[-1]["request"]
        current[0] += timedelta(seconds=30)
        stream = tree.stream()
        event = await asyncio.wait_for(anext(stream), 2)
        assert event["type"] == "user_input_resolved" and event["request"]["status"] == "timed_out"
        assert event["request"]["id"] == request["id"]
        assert connection.writes == [{"id":0,"result":{"answers":{}}}]
        run["cancel"].set(); await stream.aclose(); await tree.close()
    asyncio.run(scenario())
