"""Saved conversation controls and retained retail scope regressions.

Managed Stop, disconnect, durable effects and children are covered by
test_managed_runtime, test_managed_turn and test_delegated_jobs. The removed
local app-server loop has no active transport tests.
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))
import agent_bridge as bridge


class MemoryPath:
    def write_text(self, *args): pass
    def chmod(self, *args): pass


class FakeConnection:
    def __init__(self, process=None):
        self.events = asyncio.Queue()
        self.events.put_nowait({"method": "turn/started"})
        self.events.put_nowait({"id": 7, "method": "item/tool/call", "params": {"tool": "inspect_clip", "arguments": {"clip_id": "x"}}})

    async def rpc(self, method, params, *, guard=None):
        if guard is not None and not guard():
            raise RuntimeError("This native request has ended.")
        if method == "thread/start": return {"thread": {"id": "thread"}}
        if method == "turn/start": return {"turn": {"id": "turn"}}
        return {}

    async def write(self, *args): pass
    async def close(self): pass


def agent_fixture():
    agent = object.__new__(bridge.CodexVideoAgent)
    agent.active, agent.scopes, agent.registry, agent.children, agent.child_tasks = {}, {}, {}, {}, {}
    agent.scope_path = MemoryPath()
    agent.state = Path("/tmp")
    agent.auth = bridge.AgentAuth(agent.state, mode="founder_local_chatgpt", runtime_host="local-test")
    agent._remember = lambda *args: None
    agent.toolsets = {}
    agent._remember_toolset = lambda *args: None
    agent._input = lambda *args: []
    agent._save_child = lambda child: None
    agent.child_slots = asyncio.Semaphore(3)
    return agent


def test_saved_run_events_preserve_tool_completion_children_and_updated_focus():
    answer = {"content":"", "results":[], "tools":[]}
    for event in [
        {"type":"tool_start", "name":"inspect_clip", "arguments":{"clip_id":"x"}},
        {"type":"tool_end", "name":"inspect_clip", "success":True},
        {"type":"results", "results":[{"id":"x", "source_url":"/?clip=x"}]},
        {"type":"results", "results":[{"id":"x", "focus_id":"specific-subject"}]},
        {"type":"subchat", "chat_id":"child", "title":"Independent investigation"},
        {"type":"delta", "text":"Observed answer"},
        {"type":"done", "status":"completed"},
    ]:
        bridge.CodexVideoAgent._record_event(answer, event)
    assert answer["tools"][0]["status"] == "complete"
    assert answer["subchats"][0]["chat_id"] == "child"
    assert answer["results"] == [{"id":"x", "source_url":"/?clip=x", "focus_id":"specific-subject"}]
    assert answer["content"] == "Observed answer" and answer["status"] == "completed"


def active_run():
    ready = asyncio.Event()
    ready.set()
    return {"ready": ready, "turn_id": "turn", "thread_id": "thread", "connection": FakeConnection(), "cancel": asyncio.Event()}












def test_focus_result_keeps_the_exact_subject_id_without_private_paths(tmp_path):
    async def scenario():
        agent = agent_fixture()
        agent.state = tmp_path
        (tmp_path / "focus").mkdir()
        record = {"id": "clip", "dataset": "cafe"}
        agent.retrieval = SimpleNamespace(get_clip=lambda _: record)
        focus_id = "a" * 32
        agent.focus_tracker = SimpleNamespace(build=lambda *args: {
            "clip_id": "clip", "sidecar_path": str(tmp_path / "focus" / (focus_id + ".json"))})
        content, results, success = await agent._tool("focus_clip", {
            "clip_id": "clip", "bbox": [.1, .1, .2, .3], "anchor_seconds": 0}, "cafe")
        assert success
        data = json.loads(content[0]["text"])
        assert data["focus_id"] == results[0]["focus_id"] == focus_id
        assert "sidecar_path" not in data
        assert str(tmp_path) not in str(content)
    asyncio.run(scenario())






def test_scoped_run_direct_tools_and_exports_cannot_read_other_windows(tmp_path):
    from execution_policy import RETRIEVAL_VIEW
    async def scenario():
        agent=agent_fixture();agent.state=tmp_path
        record={'id':'other','dataset':'cafe','video_id':'video','start':0,'end':8}
        agent.retrieval=SimpleNamespace(get_clip=lambda _:record)
        scoped=SimpleNamespace(get_clip=lambda _:None,clips=[])
        token=RETRIEVAL_VIEW.set(scoped)
        try:
            for name,args in [('inspect_clip',{'clip_id':'other'}),('focus_clip',{'clip_id':'other'}),('related_clips',{'clip_id':'other'}),('cosmos_inspect',{'clip_ids':['other'],'question':'Describe'})]:
                _,_,success=await agent._tool(name,args,'cafe')
                assert not success,name
            seen=[]
            class Export:
                index=agent.retrieval
                async def prepare(self,document,**kwargs):
                    seen.append(self.index)
                    assert self.index.get_clip('other') is None
                    raise ValueError('Choose admitted footage')
            agent.export_store=Export();agent.children['parent']={'owner_principal_id':'owner'}
            _,_,success=await agent._tool('prepare_export',{'document':{'items':[{'clip_id':'other'}]}},'cafe','parent')
            assert not success and seen==[scoped] and agent.export_store.index is agent.retrieval
        finally: RETRIEVAL_VIEW.reset(token)
    asyncio.run(scenario())
