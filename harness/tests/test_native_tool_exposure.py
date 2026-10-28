"""Installed-runtime proof through a loopback-only fake Responses provider.

No real credential is inherited, no provider is contacted, and generated tool
calls target only pytest's temporary directory. This checks actual model-visible
schemas AND native dispatch, rather than trusting feature-list labels.
"""
import asyncio
from copy import deepcopy
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy.migration.legacy.native_policy import cli_args, startup_policy, thread_params, policy_digest


def test_catalogue_changes_only_file_tool_capabilities(monkeypatch, tmp_path):
    original = {"models": [{"slug": "fixture", "shell_type": "unified_exec",
        "apply_patch_tool_type": "freeform", "context_window": 123,
        "input_modalities": ["text", "image"], "base_instructions": "Preserve me",
        "reasoning_levels": ["high"], "tool_mode": "code_mode_only", "experimental_supported_tools": ["clock", "send_user_message_async"]}]}
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(original))
    monkeypatch.setattr(subprocess, "run", run)
    config = startup_policy("/fixture/codex", tmp_path, {"PATH": "/fixture"})
    path = Path(config["model_catalog_json"])
    expected = deepcopy(original)
    expected["models"][0].update(shell_type="disabled", apply_patch_tool_type=None, experimental_supported_tools=["clock"])
    assert json.loads(path.read_text()) == expected
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert calls[0][0] == ["/fixture/codex", "debug", "models", "--bundled"]
    assert calls[0][1]["env"] == {"PATH": "/fixture"}
    assert startup_policy("/fixture/codex", tmp_path)["model_catalog_json"] == str(path)
    # Model metadata changes also change the persistent native policy fingerprint.
    original["models"][0]["context_window"] += 1
    changed = startup_policy("/fixture/codex", tmp_path)
    assert policy_digest(changed) != policy_digest(config)


@pytest.mark.parametrize("catalogue", [{}, {"models": []}, {"models": [{}]},
    {"models": [{"slug": "fixture", "shell_type": "disabled"}]}])
def test_missing_catalogue_fails_before_publication(monkeypatch, tmp_path, catalogue):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(catalogue)))
    with pytest.raises(ValueError):
        startup_policy("/fixture/codex", tmp_path)
    assert not (tmp_path / "native-policy").exists()


class LocalResponses:
    def __init__(self, injected=()):
        self.requests = []
        self.injected = list(injected)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                owner.requests.append(json.loads(data))
                assert self.path == "/v1/responses"
                output = owner.injected if len(owner.requests) == 1 else []
                response = {"id": "fixture-response", "object": "response", "status": "completed",
                    "output": output, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
                events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
                events.extend({"type": "response.output_item.done", "output_index": i, "item": item}
                              for i, item in enumerate(output))
                events.append({"type": "response.completed", "response": response})
                body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)


async def captured_turn(binary, root, transport, model, thread_id=None, effort=None):
    isolated_home = root / "codex-home"
    isolated_home.mkdir(exist_ok=True)
    # Deliberate allowlist: never inherit a real API key, auth file or proxy.
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "CODEX_HOME": str(isolated_home),
           "ROCOTO_NATIVE_FIXTURE_KEY": "loopback-fixture-only"}
    runtime = startup_policy(binary, root, env)
    provider = {"model": model, "model_provider": "fixture",
        "model_providers.fixture.name": "Loopback tool-exposure fixture",
        "model_providers.fixture.base_url": f"http://127.0.0.1:{transport.server.server_port}/v1",
        "model_providers.fixture.env_key": "ROCOTO_NATIVE_FIXTURE_KEY",
        "model_providers.fixture.wire_api": "responses",
        "model_providers.fixture.requires_openai_auth": False,
        "model_providers.fixture.supports_websockets": False}
    if effort is not None:
        provider["model_reasoning_effort"] = effort
    process = await asyncio.create_subprocess_exec(binary, *cli_args(runtime), *cli_args(provider),
        "app-server", "--listen", "stdio://", env=env, cwd=root,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
    counter = 0
    async def read():
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        assert line, "Native fixture process ended before its response"
        message = json.loads(line)
        # Dynamic requests would require an explicit host handler. This fixture
        # injects only forbidden builtins and should receive none of them.
        assert not (message.get("method") and "id" in message), message.get("method")
        return message
    async def rpc(method, params):
        nonlocal counter
        counter += 1
        process.stdin.write((json.dumps({"id": counter, "method": method, "params": params}) + "\n").encode())
        await process.stdin.drain()
        while True:
            message = await read()
            if message.get("id") == counter:
                assert "error" not in message, message.get("error")
                return message["result"]
    try:
        await rpc("initialize", {"clientInfo": {"name": "rocoto_policy_fixture", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        tools = [{"name": "scoped_clip_lookup", "description": "Read authorized fixture clips",
                  "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
        params = thread_params(root, "Use authorized evidence tools.", tools)
        params.update(model=model, modelProvider="fixture")
        params["config"].update(runtime)
        if thread_id:
            params.pop("dynamicTools")
            params["threadId"] = thread_id
        started = await rpc("thread/resume" if thread_id else "thread/start", params)
        thread_id = started["thread"]["id"]
        await rpc("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "Inspect authorized fixture evidence."}]})
        while True:
            message = await read()
            if message.get("method") == "turn/completed":
                assert message["params"]["turn"]["status"] == "completed", message
                break
        return thread_id
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


def installed_codex():
    binary = shutil.which("codex")
    if not binary:
        pytest.skip("Installed native CLI supplies the capability exposure proof")
    return binary


def assert_scoped_tool_set(request):
    if "tools" in request:
        exposed = {(tool["type"], tool.get("name")) for tool in request["tools"]}
        assert exposed == {("function", "scoped_clip_lookup"), ("web_search", None)}
        return
    # Astra's code-mode wrapper has no Node, filesystem or network. Its only
    # nested capabilities are the registered scoped tool and native clock.
    groups = [tool for item in request["input"] if item.get("type") == "additional_tools"
              for tool in item["tools"]]
    names = {(group["name"], item["name"]) for group in groups for item in group["tools"]}
    assert names == {("functions", "exec"), ("functions", "wait"), ("clock", "sleep")}
    executor = next(item for group in groups for item in group["tools"] if item["name"] == "exec")
    declarations = [line for line in executor["description"].splitlines() if line.startswith("### `")]
    assert declarations == ["### `scoped_clip_lookup`", "### `clock__curr_time`"]


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-6-astra"])
def test_installed_fresh_and_resumed_threads_preserve_only_scoped_tools_and_wrappers(tmp_path, model):
    binary = installed_codex()
    with LocalResponses() as transport:
        thread_id = asyncio.run(captured_turn(binary, tmp_path, transport, model))
        # A new native process restores persisted tools and the hardened catalogue.
        assert asyncio.run(captured_turn(binary, tmp_path, transport, model, thread_id)) == thread_id
    assert len(transport.requests) == 2
    for request in transport.requests:
        assert_scoped_tool_set(request)


def test_unadvertised_local_tools_are_rejected_before_execution(tmp_path):
    binary = installed_codex()
    patch_target = tmp_path / "forbidden-patch.txt"
    shell_target = tmp_path / "forbidden-shell.txt"
    injected = [{"id": "fixture-patch", "call_id": "fixture-patch", "type": "custom_tool_call",
        "name": "apply_patch", "input": "*** Begin Patch\n*** Add File: " + str(patch_target) + "\n+fixture\n*** End Patch\n"}]
    arguments = {
        "exec_command": {"cmd": "touch " + str(shell_target)},
        "shell": {"command": ["touch", str(shell_target)]},
        "write_stdin": {"session_id": 1, "chars": "fixture"},
        "view_image": {"path": str(tmp_path / "fixture-image.png")},
        "read_file": {"path": str(tmp_path / "fixture-private.txt")},
    }
    injected.extend({"id": "fixture-" + name, "call_id": "fixture-" + name,
        "type": "function_call", "name": name, "arguments": json.dumps(args)}
        for name, args in arguments.items())
    with LocalResponses(injected) as transport:
        asyncio.run(captured_turn(binary, tmp_path, transport, "gpt-5.4"))
    assert len(transport.requests) == 2
    assert not patch_target.exists()
    assert not shell_target.exists()
    outputs = {item.get("call_id"): item.get("output") for item in transport.requests[-1]["input"]
               if item.get("type") in {"custom_tool_call_output", "function_call_output"}}
    for call in injected:
        assert "unsupported" in outputs[call["call_id"]].lower()
        assert call["name"] in outputs[call["call_id"]]
    for request in transport.requests:
        assert_scoped_tool_set(request)


def test_astra_code_mode_has_only_scoped_registry_and_rejects_host_tools(tmp_path):
    binary = installed_codex()
    source = '''
text(ALL_TOOLS.map(tool => tool.name));
for (const name of ['exec_command', 'shell', 'write_stdin', 'apply_patch', 'view_image', 'read_file', 'spawn_agent', 'collaboration__spawn_agent']) {
    try { await tools[name]({}); text({name, refused: false}); }
    catch (error) { text({name, refused: true, error: String(error)}); }
}
'''
    injected = [{"id": "fixture-code", "call_id": "fixture-code", "type": "custom_tool_call",
                 "namespace": "functions", "name": "exec", "input": source}]
    with LocalResponses(injected) as transport:
        asyncio.run(captured_turn(binary, tmp_path, transport, "gpt-6-astra"))
    assert len(transport.requests) == 2
    outputs = [item for item in transport.requests[-1]["input"] if item.get("type") == "custom_tool_call_output"]
    assert outputs, transport.requests[-1]["input"][-2:]
    output = outputs[-1]["output"]
    if isinstance(output, list):
        output = "\n".join(part.get("text", "") for part in output)
    assert '"scoped_clip_lookup"' in output
    assert '"clock__curr_time"' in output
    registry = next(json.loads(line) for line in output.splitlines() if line.startswith('["'))
    assert set(registry) == {'scoped_clip_lookup', 'clock__curr_time'}
    assert '"refused":false' not in output
    for name in ['exec_command', 'shell', 'write_stdin', 'apply_patch', 'view_image', 'read_file', 'spawn_agent', 'collaboration__spawn_agent']:
        assert f'"name":"{name}","refused":true' in output, output
    for request in transport.requests:
        assert_scoped_tool_set(request)


@pytest.mark.parametrize("error", [OSError("fixture details"), subprocess.CalledProcessError(1, ["codex"], output="fixture details"), subprocess.TimeoutExpired("codex", 15)])
def test_catalogue_subprocess_failures_are_sanitized(monkeypatch, tmp_path, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="^The installed Codex model catalogue could not be loaded$"):
        startup_policy("/fixture/codex", tmp_path)
    assert not (tmp_path / "native-policy").exists()
