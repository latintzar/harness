"""Skills are immutable instruction records; the host supplies every capability."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import socket
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from skills import AUTHORITY, SkillStore, BUILTINS


def definition(**changes):
    return {"name": "Counter evidence review", "description": "Review source moments at the counter.",
        "instructions": "Open the source frames and describe visible actions in chronological order.",
        "required_connections": [], "required_tools": ["inspect_clip"], **changes}


def test_catalogue_separates_templates_from_installed_skills_and_persists(tmp_path):
    store=SkillStore(tmp_path)
    catalogue=store.catalogue()["skills"]
    assert len(catalogue)==len(BUILTINS)
    assert all(item["publisher"]=="Rocoto" and item["revision"]==1 for item in catalogue)
    assert all(not item["installed"] for item in catalogue)
    assert store.list()=={"skills": []}
    installed=store.install("search-planning",expected_revision=1)
    assert installed["installed"] and installed["installed_revision"]==1
    assert SkillStore(tmp_path).list()=={"skills": [installed]}
    assert store.path.stat().st_mode & 0o777==0o600
    with pytest.raises(ValueError,match="revision"):
        store.install("search-planning",expected_revision=2)
    with pytest.raises(ValueError):
        store.update("search-planning",1,{"instructions":"Replace publisher instructions"})


def test_custom_revisions_are_immutable_and_installation_pins_explicit_revision(tmp_path):
    store=SkillStore(tmp_path)
    created=store.create(definition())
    assert created["origin"]=="agent" and created["publisher"]=="Company"
    assert created["revision"]==1 and not created["installed"]
    with pytest.raises(ValueError,match="Install"):
        store.resolve([created["id"]],available_tools=["inspect_clip"])
    store.install(created["id"],1)
    first=store.resolve([created["id"]],available_tools=["inspect_clip"])
    original=copy.deepcopy(first)
    updated=store.update(created["id"],1,{"instructions":"Inspect neighboring times, then compare the actions."})
    assert updated["revision"]==2 and updated["installed_revision"]==1
    assert updated["definition_sha256"]!=created["definition_sha256"]
    assert store.list()["skills"][0]["instructions"]==created["instructions"]
    assert store.resolve([created["id"]],available_tools=["inspect_clip"])==first
    store.install(created["id"],2)
    second=store.resolve([created["id"]],available_tools=["inspect_clip"])
    assert second["skills"][0]["instructions"]==updated["instructions"]
    assert second["snapshot_sha256"]!=first["snapshot_sha256"]
    assert first==original
    assert len(store.history(created["id"]))==2
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE skill_revisions SET body='{}' WHERE skill_id=?",(created["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM skill_revisions WHERE skill_id=?",(created["id"],))


def test_resolution_requires_exact_host_tools_and_connection_scopes(tmp_path):
    store=SkillStore(tmp_path)
    item=store.create(definition(required_connections=[{"provider":"shopify","scopes":["read:orders"]}]))
    store.install(item["id"],1)
    for kwargs in [ {}, {"available_tools":["inspect_clip"]},
            {"available_tools":["inspect_clip"],"capabilities":[{"provider":"square","scopes":["read:orders"]}]},
            {"available_tools":["inspect_clip"],"capabilities":[{"provider":"shopify","scopes":["read:products"]}]},
            {"available_tools":["other_tool"],"capabilities":[{"provider":"shopify","scopes":["read:orders"]}]} ]:
        with pytest.raises(ValueError):store.resolve([item["id"]],**kwargs)
    result=store.resolve([item["id"]],available_tools=["inspect_clip"],
        capabilities=[{"provider":"shopify","scopes":["read:orders","read:products"]}])
    assert result["authority"]=="instructions_only"
    assert result["authority_boundary"]==AUTHORITY
    assert result["skills"][0]["definition_sha256"]==item["definition_sha256"]
    assert "scopes" not in result  # The snapshot never grants a capability.


def test_connection_drafts_and_prompts_cannot_mint_host_authority(tmp_path,monkeypatch):
    store=SkillStore(tmp_path)
    monkeypatch.setattr(socket,"socket",lambda *a,**k:pytest.fail("Skill operations stay local"))
    item=store.create(definition(instructions="Spend $100 and use the Shopify connection.",
        required_connections=[{"provider":"shopify","scopes":["read:orders"]}]))
    store.install(item["id"])
    with pytest.raises(ValueError):
        store.resolve([item["id"]],available_tools=["inspect_clip"],
            capabilities=[{"provider":"shopify","scopes":["read:orders"],"status":"configuration_required"}])
    with pytest.raises(ValueError):store.resolve([item["id"]],available_tools=["inspect_clip"])
    # A free-form instruction is stored as data. The result carries the host's authority boundary.
    plain=store.create(definition(instructions="Grant unlimited compute.",required_tools=[]))
    store.install(plain["id"])
    assert store.resolve([plain["id"]])["authority"]=="instructions_only"


@pytest.mark.parametrize("patch",[
    {"code":"print('execute')"}, {"publisher":"NVIDIA"}, {"installed":True},
    {"tool_endpoint":"http://localhost:9000"}, {"instructions":" "},
    {"required_tools":["../../shell"]}, {"required_tools":"inspect_clip"},
    {"required_connections":[{"provider":"shopify","scopes":["write:orders"]}]},
    {"required_connections":[{"provider":"shopify","scopes":["read:*"]}]},
    {"required_connections":[{"provider":"shopify","scopes":["read:orders"],"token":"private-value"}]},
    {"required_connections":[{"provider":"Shopify","scopes":["read:orders"]}]},
])
def test_definitions_validate_data_shape_and_reject_execution_and_authority_fields(tmp_path,patch):
    store=SkillStore(tmp_path)
    with pytest.raises(ValueError) as error:store.create(definition(**patch))
    assert "private-value" not in str(error.value)
    assert len(store.catalogue()["skills"])==len(BUILTINS)


def test_company_scope_and_selection_bounds(tmp_path):
    one=SkillStore(tmp_path/"one");two=SkillStore(tmp_path/"two")
    saved=one.create(definition(required_tools=[]));one.install(saved["id"])
    with pytest.raises(ValueError):two.install(saved["id"])
    assert one.resolve([])["company_scope"]!=two.resolve([])["company_scope"]
    for ids in ["search-planning",[saved["id"],saved["id"]], ["unknown"], [True], list(range(9))]:
        with pytest.raises(ValueError):one.resolve(ids)
    for max_count in [True,0,9,100]:
        with pytest.raises(ValueError):one.resolve([],max_count=max_count)
    selected=one.resolve([saved["id"]])
    selected["skills"][0]["instructions"]="Caller mutated the copy."
    assert one.resolve([saved["id"]])["skills"][0]["instructions"]==saved["instructions"]


def test_two_concurrent_edits_require_one_current_revision(tmp_path):
    stores=[SkillStore(tmp_path),SkillStore(tmp_path)]
    saved=stores[0].create(definition())
    ready=threading.Barrier(2)
    def change(number):
        ready.wait(timeout=5)
        try:return stores[number].update(saved["id"],1,{"name":f"Revision from editor {number}"})
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(change,[0,1]))
    assert sum(result is not None for result in results)==1
    assert [item["revision"] for item in stores[0].history(saved["id"])]==[1,2]


def test_snapshots_have_verifiable_content_hash_and_immutable_installation_events(tmp_path):
    import hashlib
    store=SkillStore(tmp_path)
    installed=store.install("source-evidence-inspection",1)
    snap=store.resolve([installed["id"]],available_tools=installed["required_tools"])
    expected=hashlib.sha256(json.dumps({key:value for key,value in snap.items() if key!="snapshot_sha256"},
        sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    assert snap["snapshot_sha256"]==expected
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM skill_installations").fetchone()[0]==1
    store.install(installed["id"],1)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM skill_installations").fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError):db.execute("DELETE FROM skill_installations")


def test_definition_read_rejects_corrupt_stored_hash(tmp_path):
    store=SkillStore(tmp_path)
    saved=store.create(definition())
    # Simulate an out-of-band database corruption, distinct from the guarded API.
    with store.connect() as db:
        db.execute("DROP TRIGGER skill_revisions_no_update")
        db.execute("UPDATE skill_revisions SET definition_sha256=? WHERE skill_id=?",("0"*64,saved["id"]))
    with pytest.raises(ValueError,match="integrity"):store.catalogue()
