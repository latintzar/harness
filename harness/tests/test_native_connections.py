"""Principal-bound broker dispatch and skill admission, without provider calls."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_bridge import CodexVideoAgent, tool_specs
from test_skills import definition


class Broker:
    def __init__(self):
        self.calls = []
        self.connected = True

    def catalogue(self, principal_id):
        self.calls.append(("catalogue", principal_id))
        return {"connections": [{"id": "notion", "status": "connected"}]}

    def capabilities(self, principal_id):
        self.calls.append(("capabilities", principal_id))
        return ([{"provider": "notion", "scopes": ["read:pages"], "tools": ["notion-fetch"]}]
                if self.connected and principal_id == "owner" else [])

    async def tools(self, provider, *, principal_id):
        self.calls.append(("tools", principal_id, provider))
        return {"provider": provider, "tools": [{"name": "notion-fetch"}]}

    async def call(self, provider, tool, arguments, *, principal_id):
        self.calls.append(("call", principal_id, provider, tool, arguments))
        return {"authority": "untrusted_source_data", "result": {"text": "Page reference"}}


def fixture(tmp_path):
    agent = CodexVideoAgent(tmp_path, object(), lambda: {})
    agent.connection_store = Broker()
    agent.children = {"parent": {"id": "parent", "owner_principal_id": "owner"},
                      "child": {"id": "child", "owner_principal_id": "owner", "parent_id": "parent"}}
    return agent


def test_native_connection_dispatch_derives_principal_from_saved_chat(tmp_path):
    async def scenario():
        agent = fixture(tmp_path)
        names = {row["name"] for row in tool_specs(False)}
        assert {"list_connections", "list_connection_tools", "read_connection"} <= names
        assert not {"connect", "authenticate_connection", "disconnect_connection"} & names
        for chat in ("parent", "child"):
            for name, args in [("list_connections", {}),
                               ("list_connection_tools", {"provider": "notion"}),
                               ("read_connection", {"provider": "notion", "tool": "notion-fetch",
                                                    "arguments": {"page_id": "page"}})]:
                # Neither a model argument nor an external result may select the credential owner.
                content, _, success = await agent._tool(name, {**args, "owner_principal_id": "victim"}, "cafe", chat)
                assert success, content
                assert agent.connection_store.calls[-1][1] == "owner"
                if name == "read_connection":
                    assert json.loads(content[0]["text"])["authority"] == "untrusted_source_data"
        count = len(agent.connection_store.calls)
        _, _, success = await agent._tool("read_connection", {"provider": "notion", "tool": "notion-fetch", "arguments": {}}, "cafe", "unknown")
        assert not success and len(agent.connection_store.calls) == count
        async def failed(*args, **kwargs):
            return {"authority": "untrusted_source_data", "result": {"isError": True, "content": [{"text": "Page needs read access"}]}}
        agent.connection_store.call = failed
        parts, _, success = await agent._tool("read_connection", {"provider": "notion", "tool": "notion-fetch", "arguments": {}}, "cafe", "parent")
        assert not success and json.loads(parts[0]["text"])["result"]["isError"]
    asyncio.run(scenario())


def test_skill_dependencies_follow_principal_connection_and_disconnect(tmp_path):
    agent = fixture(tmp_path)
    saved = agent._skills().create(definition(required_connections=[{"provider": "notion", "scopes": ["read:pages"]}],
                                              required_tools=["read_connection"]))
    agent._skills().install(saved["id"], 1)
    assert agent.resolve_skills([saved["id"]], "parent")["skills"][0]["id"] == saved["id"]
    assert agent.resolve_skills([saved["id"]], "new", owner_principal_id="owner")["skills"]
    with pytest.raises(ValueError):
        agent.resolve_skills([saved["id"]], "parent", owner_principal_id="victim")
    with pytest.raises(ValueError, match="verified notion read access"):
        agent.resolve_skills([saved["id"]], "new", owner_principal_id="victim")
    agent.connection_store.connected = False
    with pytest.raises(ValueError, match="verified notion read access"):
        agent.resolve_skills([saved["id"]], "parent")
