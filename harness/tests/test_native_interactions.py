"""Installed 0.153.4 public item shapes; no provider calls or native authority inferred."""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from native_events import PublicActivity


def envelope(item, thread="root", turn="turn"):
    return {"threadId": thread, "turnId": turn, "item": item}


def command(**overrides):
    return {"type": "commandExecution", "id": "cmd", "command": "rg cafe report.md", "cwd": "/workspace",
            "status": "inProgress", "commandActions": [{"type": "search", "command": "rg cafe report.md", "query": "cafe", "path": "report.md"}],
            "aggregatedOutput": None, "exitCode": None, "durationMs": None, **overrides}


def test_command_identity_actions_and_failure_are_native():
    stream = PublicActivity()
    start = stream.translate("item/started", envelope(command()))
    assert start["type"] == "tool_start" and start["name"] == "search_files"
    assert start["arguments"]["command"] == "rg cafe report.md"
    delta = stream.translate("item/commandExecution/outputDelta", {"threadId": "root", "turnId": "turn", "itemId": "cmd", "delta": "found\n"})
    assert delta["activity"] == "tool_progress" and delta["id"] == "cmd"
    end = stream.translate("item/completed", envelope(command(status="failed", exitCode=2, aggregatedOutput="Bad pattern")))
    assert end["id"] == start["id"] == "cmd"
    assert end["success"] is False and end["output"] == "Bad pattern" and end["exit_code"] == 2


@pytest.mark.parametrize("actions,name", [([], "run_command"), ([{"type": "read", "name": "report.md", "path": "report.md", "command": "cat report.md"}], "read_files"), ([{"type": "listFiles", "command": "ls", "path": "."}], "list_files")])
def test_command_display_uses_parsed_native_actions(actions, name):
    assert PublicActivity().translate("item/started", envelope(command(commandActions=actions)))["name"] == name


def test_completed_command_with_nonzero_exit_code_is_unsuccessful():
    event = PublicActivity().translate("item/completed", envelope(command(status="completed", exitCode=1)))
    assert event["success"] is False
    assert event["exit_code"] == 1


def test_file_changes_use_bounded_diff_and_truthful_declined_status():
    stream = PublicActivity()
    item = {"type": "fileChange", "id": "patch", "status": "inProgress", "changes": [{"path": "/workspace/report.md", "kind": {"type": "update", "move_path": None}, "diff": "+Summary"}]}
    start = stream.translate("item/started", envelope(item))
    assert start["name"] == "edit_files" and start["arguments"]["paths"] == ["/workspace/report.md"]
    end = stream.translate("item/completed", envelope({**item, "status": "declined"}))
    assert end["success"] is False and "+Summary" in end["output"]


def test_mcp_call_preserves_namespace_and_omits_image_payload():
    stream = PublicActivity()
    item = {"type": "mcpToolCall", "id": "mcp", "server": "rocoto", "tool": "search_video", "arguments": {"query": "delivery"}, "status": "inProgress", "result": None, "error": None, "durationMs": None}
    start = stream.translate("item/started", envelope(item))
    assert start["name"] == "mcp__rocoto__search_video" and start["arguments"] == {"query": "delivery"}
    end = stream.translate("item/completed", envelope({**item, "status": "completed", "result": {"content": [{"type": "text", "text": "Found a delivery"}, {"type": "image", "data": "PRIVATE_IMAGE_BASE64", "mimeType": "image/png"}], "structuredContent": {"data": "opaque"}}}))
    assert end["success"] is True and end["output"] == "Found a delivery"
    assert "PRIVATE_IMAGE_BASE64" not in json.dumps(end) and "opaque" not in json.dumps(end)
    failed = stream.translate("item/completed", envelope({**item, "id": "other", "status": "failed", "error": {"message": "Access expired"}}))
    assert not failed["success"] and failed["output"] == "Access expired"


def test_native_tool_output_is_bounded_and_marked():
    stream = PublicActivity()
    item = command(status="completed", aggregatedOutput="a" * 100000, exitCode=0)
    event = stream.translate("item/completed", envelope(item))
    assert len(event["output"]) <= 32768 and event["output_truncated"] is True


def test_native_v2_subagent_items_require_host_verified_routes():
    stream = PublicActivity()
    item = {"type": "subAgentActivity", "id": "spawn", "kind": "started", "agentThreadId": "native-child", "agentPath": "/root/malicious-name"}
    assert all(e["type"] != "subchat" for e in stream.translate_many("item/started", envelope(item)))
    verified = {"native-child": {"chat_id": "owned-product-chat", "parent_id": "owned-root", "title": "Inspect deliveries", "status": "running", "url": "https://hostile.example/"}}
    events = stream.translate_many("item/started", envelope(item), verified_children=verified)
    child = next(e for e in events if e["type"] == "subchat")
    assert child["chat_id"] == "owned-product-chat" and child["title"] == "Inspect deliveries"
    assert child["url"] == "/?chat=owned-product-chat" and child["status"] == "running"
    assert "malicious-name" not in json.dumps(child)
    ended = stream.translate_many("item/completed", envelope({**item, "kind": "completed", "id": "finish"}), verified_children=verified)
    assert next(e for e in ended if e["type"] == "subchat")["status"] == "completed"
    # Completion of the spawn item is not completion of the child.
    spawned = stream.translate_many("item/completed", envelope(item), verified_children=verified)
    assert next(e for e in spawned if e["type"] == "subchat")["status"] == "running"


def test_legacy_collaboration_updates_only_verified_children():
    stream = PublicActivity()
    item = {"type": "collabAgentToolCall", "id": "wait", "tool": "wait", "status": "completed", "senderThreadId": "root", "receiverThreadIds": ["owned", "foreign"], "agentsStates": {"owned": {"status": "completed", "message": "Done"}, "foreign": {"status": "running"}}, "prompt": "reference"}
    events = stream.translate_many("item/completed", envelope(item), verified_children={"owned": {"chat_id": "owned-chat", "parent_id": "root-chat", "title": "Inspection"}})
    children = [e for e in events if e["type"] == "subchat"]
    assert len(children) == 1 and children[0]["chat_id"] == "owned-chat" and children[0]["status"] == "completed"
    assert events[0]["name"] == "wait_agent" and events[0]["success"] is True


def question(**overrides):
    return {"threadId": "root", "turnId": "turn", "itemId": "question", "isBlocking": False, "autoResolutionMs": None,
            "questions": [{"id": "format", "header": "Format", "question": "Choose a format.", "isOther": True, "isSecret": False, "options": [{"label": "PDF", "description": "Portable"}, {"label": "DOCX", "description": "Editable"}]}], **overrides}


def test_question_and_resolution_preserve_zero_id_without_inventing_submission():
    stream = PublicActivity()
    assert stream.translate("item/tool/requestUserInput", question()) is None  # No transport request identity.
    event = stream.translate("item/tool/requestUserInput", question(), request_id=0)
    assert event["type"] == "user_input_request"
    record = event["request"]
    assert record["id"] == "0" and record["native_request_id"] == 0 and record["blocking"] is False
    assert record["status"] == "pending" and record["questions"][0]["is_other"] is True
    resolved = stream.translate("serverRequest/resolved", {"threadId": "root", "requestId": 0})
    assert resolved["type"] == "user_input_resolved" and resolved["request"]["status"] == "resolved"
    assert record["status"] == "pending"  # Previously emitted event is immutable.
    assert "answers" not in resolved["request"]
    assert stream.translate("serverRequest/resolved", {"threadId": "root", "requestId": 0}) is None


def test_question_identity_is_scoped_and_unknown_resolutions_ignored():
    stream = PublicActivity()
    stream.translate("item/tool/requestUserInput", question(), request_id="same")
    assert stream.translate("serverRequest/resolved", {"threadId": "foreign", "requestId": "same"}) is None
    assert stream.translate("serverRequest/resolved", {"threadId": "root", "requestId": "same"})["request"]["status"] == "resolved"


def test_secret_question_never_uses_ordinary_answer_widget():
    stream = PublicActivity()
    fields = [{"id": "api_key", "header": "Key", "question": "Provide a key", "isSecret": True}]
    record = stream.translate("item/tool/requestUserInput", question(questions=fields), request_id=1)["request"]
    assert record["status"] == "unsupported" and record["resolution_reason"] == "secure_credentials_required"
    assert record["questions"] == [] and "values" not in record


@pytest.mark.parametrize("questions", [[], [{"id": "q"}], [{"id": "a", "header": "H", "question": "Q"}] * 4])
def test_malformed_question_becomes_explicit_unsupported_state(questions):
    record = PublicActivity().translate("item/tool/requestUserInput", question(questions=questions), request_id=2)["request"]
    assert record["status"] == "unsupported" and record["resolution_reason"] == "invalid_questions"


def test_unknown_requests_and_hidden_reasoning_are_not_projected():
    stream = PublicActivity()
    assert stream.translate("item/reasoning/summaryTextDelta", {"delta": "hidden"}) is None
    assert stream.translate("item/permissions/requestApproval", {"threadId": "root"}, request_id=7) is None
