"""Native questions keep transport identity private and consume each reply once."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from native_questions import NativeQuestions


def params(**changes):
    return {"threadId":"native-root", "turnId":"native-turn", "itemId":"item", "isBlocking":False,
        "questions":[{"id":"format", "header":"Format", "question":"Choose a format.",
            "isOther":True, "options":[{"label":"PDF", "description":"Portable"},
                {"label":"DOCX", "description":"Editable"}]}], **changes}


def opened(store, **changes):
    kwargs = dict(root_chat_id="root", actor_chat_id="root", actor_title="Chat", owner="alice",
        generation="run:1", native_request_id=0, params=params())
    kwargs.update(changes)
    return store.open(**kwargs)


async def reply(store, request, sent, **changes):
    async def deliver(native_id, payload):
        sent.append((native_id, payload))
    kwargs = dict(owner="alice", root_chat_id="root", generation="run:1", action="submit",
        answers={"format":["PDF"]}, deliver=deliver, cancelled=asyncio.Event())
    kwargs.update(changes)
    return await store.answer(request["id"], **kwargs)


def test_zero_native_id_private_public_id_and_exact_once_durable_submission(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path)
        record = opened(store)
        assert record["id"] != "0" and record["status"] == "pending"
        assert all(key not in record for key in ("native_request_id", "thread_id", "turn_id", "generation", "owner"))
        assert "native-root" not in json.dumps(record)
        assert opened(store) == record
        sent = []
        result = await reply(store, record, sent)
        assert result["status"] == "submitted"
        assert sent == [(0, {"answers":{"format":{"answers":["PDF"]}}})]
        assert await reply(NativeQuestions(tmp_path), record, sent) == result
        assert len(sent) == 1
        assert store.resolve_native("root", "run:1", 0)["status"] == "submitted"
    asyncio.run(scenario())


def test_zero_string_is_distinct_and_roots_can_share_native_request_ids(tmp_path):
    store = NativeQuestions(tmp_path)
    numeric = opened(store)
    text = opened(store, native_request_id="0")
    other = opened(store, root_chat_id="other", actor_chat_id="other")
    assert len({numeric["id"], text["id"], other["id"]}) == 3
    assert store.resolve_native("root", "run:1", "0")["id"] == text["id"]
    assert store.get(numeric["id"], owner="alice", root_chat_id="root")["status"] == "pending"


@pytest.mark.parametrize("change", [dict(owner="bob"), dict(root_chat_id="foreign"), dict(generation="other")])
def test_foreign_owner_root_or_generation_cannot_deliver(tmp_path, change):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store); sent = []
        with pytest.raises(ValueError):
            await reply(store, record, sent, **change)
        assert sent == []
    asyncio.run(scenario())


@pytest.mark.parametrize("answers", [{}, {"other":["PDF"]}, {"format":[]}, {"format":[""]},
    {"format":"PDF"}, {"format":["PDF", "PDF"]}, {"format":["x"*4001]}, {"format":[True]}])
def test_invalid_answers_never_consume_request(tmp_path, answers):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store); sent = []
        with pytest.raises(ValueError):
            await reply(store, record, sent, answers=answers)
        assert sent == []
        assert store.get(record["id"], owner="alice", root_chat_id="root")["status"] == "pending"
    asyncio.run(scenario())


def test_option_rules_freeform_and_explicit_skip(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); sent = []
        selected = params(); selected["questions"][0]["isOther"] = False
        record = opened(store, params=selected)
        with pytest.raises(ValueError):
            await reply(store, record, sent, answers={"format":["Plain text"]})
        record2 = opened(store, native_request_id=1)
        assert (await reply(store, record2, sent, answers={"format":["Plain text"]}))["status"] == "submitted"
        assert (await reply(store, record, sent, action="skip", answers=None))["status"] == "skipped"
        assert sent[-1] == (0, {"answers":{}})
        assert store.resolve_native("root", "run:1", 0)["status"] == "skipped"
    asyncio.run(scenario())


def test_secret_question_is_unsupported_and_never_keeps_fields(tmp_path):
    store = NativeQuestions(tmp_path)
    record = opened(store, params=params(questions=[{"id":"SECRET_LABEL", "header":"Secret",
        "question":"SECRET_PROMPT", "isSecret":True}]))
    assert record["status"] == "unsupported" and record["questions"] == []
    assert record["resolution_reason"] == "secure_credentials_required"
    assert "SECRET_PROMPT" not in json.dumps(record)
    assert "SECRET_LABEL" not in store.database.read_bytes().decode(errors="ignore")


def test_child_question_is_attributed_but_only_parent_route_is_authorized(tmp_path):
    store = NativeQuestions(tmp_path)
    record = opened(store, actor_chat_id="child", actor_title="Check deliveries")
    assert record["actor_chat_id"] == "child" and record["actor_title"] == "Check deliveries"
    with pytest.raises(ValueError):
        store.get(record["id"], owner="alice", root_chat_id="child")


def test_no_live_run_recovery_is_owned_and_preserves_submitted_outcome(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store)
        with pytest.raises(ValueError):
            store.interrupt(record["id"], owner="bob", root_chat_id="root")
        await reply(store, record, [])
        assert store.interrupt(record["id"], owner="alice", root_chat_id="root")["status"] == "submitted"
        waiting = opened(store, native_request_id=1)
        result = store.interrupt(waiting["id"], owner="alice", root_chat_id="root")
        assert result["status"] == "interrupted" and result["resolution_reason"] == "runtime_ended"
    asyncio.run(scenario())


def test_restart_and_stop_retire_pending_requests_without_guessing_answers(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store); sent = []
        store.retire_generation("root", "run:1", reason="stopped")
        result = await reply(store, record, sent)
        assert result["status"] == "interrupted" and sent == []
        newer = opened(store, generation="run:2")
        restored = NativeQuestions(tmp_path)
        retired = restored.recover()
        assert [row["id"] for row in retired] == [newer["id"]]
        assert retired[0]["resolution_reason"] == "runtime_restarted"
    asyncio.run(scenario())


def test_cancelled_before_reply_closes_request_without_delivery(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store); sent = []
        cancelled = asyncio.Event(); cancelled.set()
        assert (await reply(store, record, sent, cancelled=cancelled))["status"] == "interrupted"
        assert not sent
    asyncio.run(scenario())


def test_concurrent_duplicate_submission_consumes_one_transport_write(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store)
        started, release = asyncio.Event(), asyncio.Event(); sent = []
        async def deliver(native_id, payload):
            sent.append((native_id, deepcopy(payload))); started.set(); await release.wait()
        first = asyncio.create_task(reply(store, record, [], deliver=deliver))
        await started.wait()
        with pytest.raises(ValueError):
            await reply(NativeQuestions(tmp_path), record, sent, answers={"format":["DOCX"]})
        with pytest.raises(ValueError):
            await reply(NativeQuestions(tmp_path), record, sent, action="skip", answers=None)
        second = await reply(NativeQuestions(tmp_path), record, sent)
        assert second["status"] == "delivering" and len(sent) == 1
        release.set()
        assert (await first)["status"] == "submitted"
    asyncio.run(scenario())


def test_disconnect_after_write_is_uncertain_and_never_replayed(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store); sent = []
        async def deliver(native_id, payload):
            sent.append((native_id, payload)); raise ConnectionError("disconnected")
        result = await reply(store, record, [], deliver=deliver)
        assert result["status"] == "delivery_uncertain"
        result2 = await reply(store, record, sent)
        assert result2["status"] == "delivery_uncertain" and len(sent) == 1
    asyncio.run(scenario())


def test_stop_racing_delivery_stays_interrupted(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store)
        async def deliver(*args):
            store.retire_generation("root", "run:1", reason="stopped")
        result = await reply(store, record, [], deliver=deliver)
        assert result["status"] == "interrupted"
        assert store.resolve_native("root", "run:1", 0)["status"] == "interrupted"
    asyncio.run(scenario())


def test_conflicting_replay_or_native_request_identity_rejected(tmp_path):
    async def scenario():
        store = NativeQuestions(tmp_path); record = opened(store)
        with pytest.raises(ValueError):
            opened(store, actor_chat_id="different")
        with pytest.raises(ValueError):
            opened(store, params=params(isBlocking=True))
        await reply(store, record, [])
        with pytest.raises(ValueError):
            await reply(store, record, [], answers={"format":["DOCX"]})
        for invalid in (True, None, [], "x"*257):
            with pytest.raises(ValueError):opened(store, native_request_id=invalid)
    asyncio.run(scenario())
