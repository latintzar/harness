"""Project public Codex items; authentication and thread admission belong to the host.

Use one PublicActivity per admitted native thread. ``translate_many`` adds child
routes only from the host's verified native-thread -> product-chat registry.
Server request projection never answers a request or grants a permission.
"""
import copy
import json
from datetime import datetime, timezone
from urllib.parse import quote



OUTPUT_LIMIT = 32768
COLLAB_TOOLS = {"spawnAgent": "spawn_agent", "sendInput": "send_message",
    "resumeAgent": "resume_agent", "wait": "wait_agent", "closeAgent": "close_agent",
    "sendMessage": "send_message", "followupTask": "followup_task",
    "interruptAgent": "interrupt_agent", "listAgents": "list_agents"}


def _text(value, limit=OUTPUT_LIMIT):
    return value[:limit] if isinstance(value, str) else ""


def _output(value):
    text = value if isinstance(value, str) else ""
    return {"output": text[:OUTPUT_LIMIT], "output_truncated": len(text) > OUTPUT_LIMIT}


def _arguments(value):
    """Native arguments are public, but a malformed/huge value need not fill chat."""
    if isinstance(value, dict):
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return {}
        if len(encoded) <= OUTPUT_LIMIT:
            return copy.deepcopy(value)
        return {"preview": encoded[:OUTPUT_LIMIT], "truncated": True}
    return {}


def _question_record(params, request_id):
    record = {"id": str(request_id), "native_request_id": request_id,
        "thread_id": params.get("threadId"), "turn_id": params.get("turnId"),
        "item_id": params.get("itemId"), "kind": "question", "status": "pending",
        "blocking": params.get("isBlocking") is True, "questions": []}
    questions = params.get("questions")
    reason = None
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        reason = "invalid_questions"
    elif any(isinstance(question, dict) and question.get("isSecret") is True for question in questions):
        reason = "secure_credentials_required"
    else:
        identities = set()
        for question in questions:
            if (not isinstance(question, dict) or any(not isinstance(question.get(key), str)
                    or not question[key] or len(question[key]) > limit
                    for key, limit in (("id", 200), ("header", 200), ("question", 4000)))
                    or question["id"] in identities):
                reason = "invalid_questions"
                break
            identities.add(question["id"])
            options = question.get("options") or []
            if (not isinstance(options, list) or len(options) > 20 or any(
                    not isinstance(option, dict) or any(not isinstance(option.get(key), str)
                        or len(option[key]) > limit for key, limit in (("label", 500), ("description", 2000)))
                    for option in options)):
                reason = "invalid_questions"
                break
            record["questions"].append({key: question[key] for key in ("id", "header", "question")})
            record["questions"][-1].update(is_other=question.get("isOther") is True,
                options=[{key: option[key] for key in ("label", "description")} for option in options])
    if reason:
        record.update(status="unsupported", resolution_reason=reason, questions=[])
    return record


def context_usage(token_usage):
    last = token_usage.get("last") or {}
    window = token_usage.get("modelContextWindow")
    used = last.get("totalTokens")
    if type(used) is not int or used < 0:
        return None
    if type(window) is not int or window <= 0:
        window = None
    return {"used_tokens": used, "window_tokens": window,
        "input_tokens": last.get("inputTokens"), "output_tokens": last.get("outputTokens"),
        "cached_input_tokens": last.get("cachedInputTokens"),
        "measurement": "last_model_request", "source": "native_thread_token_usage",
        "checked_at": datetime.now(timezone.utc).isoformat()}


def plan_usage(response):
    """Keep native plan windows; exclude account IDs and opaque credit currencies."""
    buckets = response.get("rateLimitsByLimitId") or {"codex": response.get("rateLimits", {})}
    limits = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for label in ("primary", "secondary"):
            window = bucket.get(label)
            if isinstance(window, dict) and type(window.get("usedPercent")) is int:
                limits.append({"id": key + ":" + label, "name": bucket.get("limitName") or key,
                    "window": label, "used_percent": window["usedPercent"],
                    "window_minutes": window.get("windowDurationMins"), "resets_at": window.get("resetsAt")})
    return {"route": "ChatGPT subscription", "source": "native_account_rate_limits",
        "limits": limits, "checked_at": datetime.now(timezone.utc).isoformat()}


class PublicActivity:
    def __init__(self, result_fields=None):
        self.result_fields = result_fields
        self.items = {}
        self.text = {}
        self.requests = {}
        self.tool_names = {}
        self.output_lengths = {}

    def _message(self, identity, text):
        commentary = self.items.get(identity) == "commentary"
        return {"type": "progress" if commentary else "delta", "item_id": identity, "text": text,
                **({"activity": "commentary"} if commentary else {})}

    def translate_many(self, method, params, *, request_id=None, verified_children=None):
        """Expand a native item without deriving product authority from its IDs.

        The caller admits params.threadId and supplies only children already
        verified through native thread/read and bound to this root generation.
        """
        event = self.translate(method, params, request_id=request_id)
        events = [event] if event is not None else []
        item = params.get("item") or {}
        if method not in {"item/started", "item/completed"} or not isinstance(verified_children, dict):
            return events
        states = {}
        if item.get("type") == "subAgentActivity":
            states[item.get("agentThreadId")] = {"started": "running", "completed": "completed",
                "interrupted": "interrupted"}.get(item.get("kind"))
        elif item.get("type") == "collabAgentToolCall":
            for identity in (item.get("receiverThreadIds") or [])[:32]:
                state = (item.get("agentsStates") or {}).get(identity) or {}
                states[identity] = {"pendingInit": "queued", "running": "running",
                    "completed": "completed", "errored": "failed", "interrupted": "interrupted",
                    "shutdown": "stopped", "notFound": "unavailable"}.get(state.get("status"))
        for identity, status in states.items():
            child = verified_children.get(identity)
            if not isinstance(child, dict) or not isinstance(child.get("chat_id"), str):
                continue
            record = {key: copy.deepcopy(child[key]) for key in
                ("chat_id", "parent_id", "title", "status", "task", "progress", "avatar") if key in child}
            if status is not None:
                record["status"] = status
            record["url"] = "/?chat=" + quote(child["chat_id"], safe="")
            events.append({"type": "subchat", **record})
        return events

    def translate(self, method, params, *, request_id=None):
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            kind, identity = item.get("type"), item.get("id")
            if kind == "agentMessage":
                self.items[identity] = item.get("phase")
                if method == "item/completed":
                    full, seen = item.get("text", ""), self.text.get(identity, "")
                    if isinstance(full,str) and full.startswith(seen) and len(full)>len(seen):
                        self.text[identity] = full
                        return self._message(identity, full[len(seen):])
            elif kind == "reasoning":
                return {"type":"progress","item_id":identity,"activity":"thinking",
                        "text":"Thinking.\n" if method=="item/started" else "Reasoning finished.\n"}
            elif kind == "contextCompaction":
                if method == "item/started":
                    return {"type":"tool_start","id":identity,"name":"compact_context","arguments":{}}
                return {"type":"tool_end","id":identity,"name":"compact_context","success":True,
                        "output":"Context compacted; the chat continues with retained history."}
            elif kind == "webSearch":
                action = item.get("action") or {}
                name = {"openPage": "open_web_page", "findInPage": "find_on_page"}.get(action.get("type"), "web_search")
                arguments = {key: action[key] for key in ("query", "queries", "url", "pattern") if key in action}
                if not arguments and item.get("query"):
                    arguments = {"query": item["query"]}
                if method == "item/started":
                    return {"type": "tool_start", "id": identity, "name": name, "arguments": arguments}
                return {"type": "tool_end", "id": identity, "name": name, "success": True,
                    "source": "native_web_search", "results": item.get("results")}
            elif kind in {"commandExecution", "fileChange", "mcpToolCall", "collabAgentToolCall", "subAgentActivity"}:
                arguments, output = {}, ""
                fields = {}
                rendered_name = None
                status = item.get("status")
                success = status == "completed"
                if kind == "commandExecution":
                    actions = item.get("commandActions") or []
                    kinds = {action.get("type") for action in actions if isinstance(action, dict)}
                    name = {"read": "read_files", "search": "search_files", "listFiles": "list_files"}.get(
                        next(iter(kinds)) if len(kinds) == 1 else None, "run_command")
                    arguments = {"command": _text(item.get("command"), 8192), "cwd": _text(item.get("cwd"), 2048),
                        "actions": _arguments({"items": actions}).get("items", [])}
                    output = item.get("aggregatedOutput")
                    if type(item.get("exitCode")) is int and item["exitCode"] != 0:
                        success = False
                elif kind == "fileChange":
                    name = "edit_files"
                    changes = [change for change in (item.get("changes") or [])[:200] if isinstance(change, dict)]
                    arguments = {"paths": [_text(change.get("path"), 2048) for change in changes]}
                    output = "\n\n".join(_text(change.get("path"), 2048) + "\n" + _text(change.get("diff")) for change in changes)
                elif kind == "mcpToolCall":
                    name = "mcp__" + _text(item.get("server"), 200) + "__" + _text(item.get("tool"), 200)
                    arguments = _arguments(item.get("arguments"))
                    result = item.get("result") or {}
                    content = result.get("content") or []
                    output = "\n".join(_text(part.get("text")) for part in content[:100]
                        if isinstance(part, dict) and part.get("type") == "text")
                    error = item.get("error") or {}
                    if error:
                        output = _text(error.get("message"))
                    success = success and not error and result.get("isError") is not True
                    # The isolated launcher reserves this server for the host
                    # workspace MCP. Provider results cannot supply preview authority.
                    if item.get("server") == "rocoto" and self.result_fields is not None:
                        rendered_name = name
                        fields = self.result_fields(item.get("tool"), [
                            {"type": "inputText", "text": part.get("text")}
                            for part in content[:100] if isinstance(part, dict) and part.get("type") == "text"], success)
                elif kind == "collabAgentToolCall":
                    name = COLLAB_TOOLS.get(item.get("tool"), "agent_activity")
                    # Product links and child permissions come only from the verified registry.
                    arguments = {"prompt": _text(item.get("prompt"), 8192)} if item.get("prompt") else {}
                else:
                    name = {"started": "spawn_agent", "interacted": "send_message",
                        "interrupted": "interrupt_agent", "completed": "agent_completed"}.get(item.get("kind"), "agent_activity")
                    success = method == "item/completed"
                if method == "item/started":
                    self.tool_names[identity] = name
                    return {"type": "tool_start", "id": identity, "name": name, "arguments": arguments}
                name = self.tool_names.pop(identity, name)
                event = {"type": "tool_end", "id": identity, "name": name, "success": success,
                    "source": "native_" + kind, **_output(output)}
                if fields and name == rendered_name:
                    event.update(fields)
                    event['tool_fields'] = copy.deepcopy(fields)
                if type(item.get("exitCode")) is int:
                    event["exit_code"] = item["exitCode"]
                if type(item.get("durationMs")) is int and item["durationMs"] >= 0:
                    event["elapsed_ms"] = item["durationMs"]
                return event
        elif method == "item/agentMessage/delta":
            identity = params.get("itemId")
            text = params.get("delta", "")
            self.text[identity] = self.text.get(identity, "") + text
            return self._message(identity, text)
        elif method == "turn/plan/updated":
            lines=[str(step.get("status","pending"))+": "+str(step.get("step",""))[:1000]
                   for step in (params.get("plan") or [])[:20] if isinstance(step,dict)]
            return {"type":"progress","item_id":"plan-"+str(params.get("turnId","")),
                    "activity":"plan","text":"\n".join(lines)+"\n"}
        elif method == "item/mcpToolCall/progress":
            return {"type":"progress","item_id":params.get("itemId"),"activity":"tool_progress",
                    "text":str(params.get("message",""))[:2000]+"\n"}
        elif method == "item/commandExecution/outputDelta":
            identity = params.get("itemId")
            used = self.output_lengths.get(identity, 0)
            text = _text(params.get("delta"), max(0, OUTPUT_LIMIT - used))
            if not text:
                return None
            self.output_lengths[identity] = used + len(text)
            return {"type": "progress", "id": identity, "item_id": identity,
                "activity": "tool_progress", "text": text}
        elif method == "item/tool/requestUserInput":
            if type(request_id) not in (str, int) or not isinstance(params.get("threadId"), str):
                return None
            key = (params["threadId"], type(request_id), request_id)
            if key in self.requests:
                return None
            record = _question_record(params, request_id)
            self.requests[key] = record
            return {"type": "user_input_request", "request": copy.deepcopy(record)}
        elif method == "serverRequest/resolved":
            identity = params.get("requestId")
            if type(identity) not in (str, int):
                return None
            record = self.requests.pop((params.get("threadId"), type(identity), identity), None)
            if record is not None:
                return {"type": "user_input_resolved", "request": {**copy.deepcopy(record), "status": "resolved"}}
        elif method == "thread/tokenUsage/updated":
            context = context_usage(params.get("tokenUsage") or {})
            if context is not None:
                return {"type": "usage", "context": context}
        return None
