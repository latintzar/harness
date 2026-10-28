"""Actual skill stores and host integration with isolated HTTP/RPC fixtures."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parents[1]))
import agent_bridge as bridge
import server
from skills import SkillStore
from test_server_access import FixtureIndex
from test_skills import definition as skill_definition

def definition(**changes):
    return skill_definition(**{"required_tools":["get_business_profile"],**changes})

HEADERS={"X-Video-Demo":"1"}


def make_agent(tmp_path):
    tmp_path.mkdir(parents=True,exist_ok=True)
    return bridge.CodexVideoAgent(tmp_path,FixtureIndex(),lambda:{})


def make_app(tmp_path,monkeypatch):
    image=tmp_path/"frame.jpg";image.write_bytes(b"Fixture source metadata")
    index_factory = lambda path:FixtureIndex(Path(path).parent.name,image)
    async def disallow_process(*args,**kwargs):
        pytest.fail("This integration test admits neither a native process nor a provider call")
    monkeypatch.setattr(bridge.asyncio,"create_subprocess_exec",disallow_process)
    return server.create_app(tmp_path/"state",FixtureIndex("demo",image),index_factory=index_factory)


def test_native_create_list_update_tools_use_actual_store_and_preserve_installation(tmp_path):
    async def scenario():
        agent=make_agent(tmp_path)
        agent.children["parent"]={"id":"parent","owner_principal_id":"alice","dataset":"cafe"}
        agent.active["parent"]={"cancel":asyncio.Event()}
        tool_names={item["name"] for item in bridge.tool_specs()}
        assert {"list_skills","create_skill","update_skill"}<=tool_names
        content,results,success=await agent._tool("create_skill",{"definition":definition()},"cafe","parent")
        assert success and results is None
        created=json.loads(content[0]["text"])
        assert created["origin"]=="agent" and created["revision"]==1 and not created["installed"]
        store=SkillStore(tmp_path)
        store.install(created["id"],1)
        content,_,success=await agent._tool("list_skills",{},"cafe","parent")
        assert success
        listed=next(row for row in json.loads(content[0]["text"])["skills"] if row["id"]==created["id"])
        assert listed["definition_sha256"]==created["definition_sha256"] and listed["installed"]
        updated_body=definition(instructions="Inspect the original frames, then describe the visible sequence.")
        args={"skill_id":created["id"],"expected_revision":1,"definition":updated_body}
        content,_,success=await agent._tool("update_skill",args,"cafe","parent")
        assert success
        updated=json.loads(content[0]["text"])
        assert updated["revision"]==2 and updated["installed_revision"]==1
        assert store.list()["skills"][0]["instructions"]==created["instructions"]
        _,_,success=await agent._tool("update_skill",args,"cafe","parent")
        assert not success and len(store.history(created["id"]))==2
        agent.children["child"]={"parent_id":"parent"}
        _,_,success=await agent._tool("create_skill",{"definition":definition()},"cafe","child")
        assert not success
        assert not {"create_skill","update_skill"}&{item["name"] for item in bridge.tool_specs(False)}
    asyncio.run(scenario())


def test_http_catalogue_install_requires_query_access_and_keeps_company_scope(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch)
    access=app.state.access
    access.create_environment("company-a","Company A")
    access.create_environment("company-b","Company B")
    with TestClient(app) as client:
        assert client.get("/api/skills/catalogue").status_code==401
        assert client.post("/api/session/demo",headers=HEADERS).status_code==200
        from skills import BUILTINS
        assert len(client.get("/api/skills/catalogue").json()["skills"])==len(BUILTINS)
        assert client.get("/api/skills").json()=={"skills":[]}
        assert client.post("/api/skills/install",json={"skill_id":"search-planning","expected_revision":1},headers=HEADERS).status_code==403
        assert client.post("/api/session",json={"code":access.issue("company-a")},headers=HEADERS).status_code==200
        saved=SkillStore(tmp_path/"state/environments/company-a").create(definition())
        installed=client.post("/api/skills/install",json={"skill_id":saved["id"],"expected_revision":1},headers=HEADERS)
        assert installed.status_code==200 and installed.json()["installed"]
        assert client.get("/api/skills").json()["skills"][0]["id"]==saved["id"]
        assert client.post("/api/skills/install",json={"skill_id":saved["id"],"expected_revision":2},headers=HEADERS).status_code==400
        assert client.post("/api/session",json={"code":access.issue("company-b")},headers=HEADERS).status_code==200
        assert client.get("/api/skills").json()=={"skills":[]}
        assert saved["id"] not in {row["id"] for row in client.get("/api/skills/catalogue").json()["skills"]}
        assert client.post("/api/skills/install",json={"skill_id":saved["id"]},headers=HEADERS).status_code==400


def test_http_turn_pins_skill_snapshot_before_native_call_and_persists_user_message(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch);agent=app.state.agent
    store=agent._skills();saved=store.create(definition());store.install(saved["id"],1)
    calls=[]
    async def native(message,chat_id,dataset,attachments,clip_attachments,*,resolved_skills,model_context=""):
        calls.append(resolved_skills)
        assert resolved_skills["skills"][0]["instructions"]==saved["instructions"]
        assert saved["instructions"] in agent._skill_input(agent._input(message),resolved_skills)[-1]["text"]
        # A concurrent later install cannot change the already admitted turn.
        store.update(saved["id"],1,{"instructions":"The later revision."})
        store.install(saved["id"],2)
        yield {"type":"delta","text":"Fixture native answer"}
        yield {"type":"done","status":"completed"}
    monkeypatch.setattr(agent,"_turn",native)
    with TestClient(app) as client:
        client.post("/api/session",json={"code":app.state.access.issue("demo")},headers=HEADERS)
        response=client.post("/api/chat",json={"chat_id":"skills-chat","dataset":"cafe",
            "message":"Review the visible actions","skill_ids":[saved["id"]]},headers=HEADERS)
        assert response.status_code==200,response.text
        assert [json.loads(line)["type"] for line in response.text.splitlines()]==["delta","done"]
        record=client.get("/api/chats/skills-chat").json()
    assert len(calls)==1
    user=next(row for row in record["messages"] if row["role"]=="user")
    assert user["content"]=="Review the visible actions"
    assert user["skill_selection"]==calls[0]
    assert user["skill_selection"]["skills"][0]["revision"]==1
    on_disk=json.loads((tmp_path/"state/chats/skills-chat.json").read_text())
    assert on_disk["messages"][0]["skill_selection"]==calls[0]
    assert store.list()["skills"][0]["revision"]==2


def test_dependency_selection_stops_http_and_direct_turn_before_native_execution(tmp_path,monkeypatch):
    app=make_app(tmp_path,monkeypatch);agent=app.state.agent;calls=[]
    item=agent._skills().create(definition(required_connections=[{"provider":"shopify","scopes":["read:orders"]}]))
    agent._skills().install(item["id"],1)
    async def native(*args,**kwargs):
        calls.append(args)
        yield {"type":"done"}
    monkeypatch.setattr(agent,"_turn",native)
    with TestClient(app) as client:
        client.post("/api/session",json={"code":app.state.access.issue("demo")},headers=HEADERS)
        response=client.post("/api/chat",json={"chat_id":"blocked","dataset":"cafe",
            "message":"Review orders","skill_ids":[item["id"]]},headers=HEADERS)
        assert response.status_code==400 and "verified shopify read access" in response.text
        assert "blocked" not in agent.children
    async def direct():return [event async for event in agent.turn("Review","direct","cafe",skill_ids=[item["id"]])]
    assert asyncio.run(direct())[0]["type"]=="error"
    assert calls==[]


def test_actual_native_turn_serializes_admitted_skill_snapshot_into_rpc_input(tmp_path,monkeypatch):
    monkeypatch.setenv("ROCOTO_AGENT_AUTH_MODE", "founder_local_chatgpt")
    async def scenario():
        agent=make_agent(tmp_path);store=agent._skills()
        skill=store.create(definition());store.install(skill["id"],1)
        selected=agent.resolve_skills([skill["id"]],"chat")
        calls=[]
        class Process:
            returncode=0
            async def communicate(self):return b"Logged in using ChatGPT",b""
        async def process(*args,**kwargs):return Process()
        class Connection:
            def __init__(self,process):
                self.events=asyncio.Queue()
                self.events.put_nowait({"method":"turn/completed","params":{"turn":{"status":"completed"}}})
            async def rpc(self,method,params):
                calls.append((method,params))
                if method=="thread/start":return {"thread":{"id":"fixture-native-thread"}}
                if method=="turn/start":return {"turn":{"id":"fixture-native-turn"}}
                return {}
            async def write(self,message):pass
            async def close(self):pass
        monkeypatch.setattr(bridge.shutil,"which",lambda _name:"/fixture/codex")
        monkeypatch.setattr(bridge,"startup_policy",lambda *_args: {})
        monkeypatch.setattr(bridge.asyncio,"create_subprocess_exec",process)
        monkeypatch.setattr(bridge,"AppServerConnection",Connection)
        events=[event async for event in agent._turn("Review this evidence","chat","cafe",resolved_skills=selected)]
        assert events[-1]["type"]=="done"
        inputs=next(params["input"] for method,params in calls if method=="turn/start")
        assert inputs[0]["text"]=="Review this evidence"
        assert json.loads(inputs[-1]["text"].split("\n",1)[1])==selected
        assert next(params for method,params in calls if method=="thread/start")["config"]["web_search"]=="live"
    asyncio.run(scenario())


def test_delegation_inherits_admitted_revision_even_after_new_installation(tmp_path,monkeypatch):
    async def scenario():
        agent=make_agent(tmp_path);store=agent._skills()
        skill=store.create(definition());store.install(skill["id"],1)
        snapshot=agent.resolve_skills([skill["id"]],"parent")
        agent.children["parent"]={"id":"parent","owner_principal_id":"operator","dataset":"cafe"}
        agent.active["parent"]={"skill_selection":snapshot,"cancel":asyncio.Event(),
                                "child_notifications":asyncio.Queue()}
        observed=[]
        async def child_turn(message,chat_id,dataset,*,resolved_skills):
            observed.append(resolved_skills)
            yield {"type":"done","status":"completed"}
        monkeypatch.setattr(agent,"_turn",child_turn)
        content,_,success=await agent._tool("delegate_investigations",{
            "questions":["Inspect the counter activity."]},"cafe","parent")
        assert success
        child_id=json.loads(content[0]["text"])["investigations"][0]["chat_id"]
        store.update(skill["id"],1,{"instructions":"A newly installed workflow."})
        store.install(skill["id"],2)
        await agent.child_tasks[child_id]
        assert observed==[snapshot]
        child=json.loads((tmp_path/"chats"/(child_id+".json")).read_text())
        assert child["skill_selection"]==snapshot
        assert child["messages"][0]["skill_selection"]==snapshot
        assert child["skill_selection"]["skills"][0]["revision"]==1
        assert child["owner_principal_id"]=="operator"
    asyncio.run(scenario())
