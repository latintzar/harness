"""Business reference profiles persist separately from footage and connection authority."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from retail_business import RetailBusinessStore as BusinessStore
from investigation_plans import InvestigationPlans
from test_investigation_plans import Index, document


def profile(name="Reference café"):
    return {"name":name,"type":"cafe","website":"https://example.org/cafe",
        "location":"Tokyo","description":"Public reference for an onboarding example",
        "operations":["Counter service"],"source_urls":["https://example.org/cafe/about"],
        "assumptions":["The imported footage is independently sourced."]}


def test_business_profile_and_history_persist_with_explicit_unconfirmed_provenance(tmp_path):
    index=Index(tmp_path);state=tmp_path/"state";store=BusinessStore(state,index)
    initial=store.get("cafe")
    assert initial["revision"]==0
    saved=store.update("cafe",0,profile(),origin="agent_research")
    restored=BusinessStore(state,index).get("cafe")
    assert restored==saved
    assert restored["onboarding_chat_id"]==initial["onboarding_chat_id"]
    assert restored["provenance"]["epistemic_status"]=="agent_researched_unconfirmed"
    assert restored["footage_relationship"]["status"]=="unestablished"
    updated=store.update("cafe",1,profile("Operator reference"),origin="operator")
    assert updated["provenance"]["epistemic_status"]=="operator_provided"
    assert store.history("cafe")[0]["profile"]["name"]=="Reference café"
    assert store.history("cafe")[1]["profile"]["name"]=="Operator reference"
    with pytest.raises(ValueError,match="revision"):
        store.update("cafe",1,profile())
    assert len(store.history("cafe"))==2


def test_business_state_and_onboarding_chat_are_tenant_and_collection_scoped(tmp_path):
    index=Index(tmp_path)
    one=BusinessStore(tmp_path/"tenant-a",index);two=BusinessStore(tmp_path/"tenant-b",index)
    one.update("cafe",0,profile())
    assert two.get("cafe")["profile"]["name"]==""
    assert one.get("warehouse")["profile"]["name"]==""
    assert len({one.get("cafe")["onboarding_chat_id"],two.get("cafe")["onboarding_chat_id"],
                one.get("warehouse")["onboarding_chat_id"]})==3
    with pytest.raises(ValueError):one.get("other-company")
    # A committed profile remains available while its footage catalog refreshes.
    index.clips=[]
    assert one.get("cafe")["profile"]["name"]=="Reference café"
    with pytest.raises(ValueError):two.get("cafe")


@pytest.mark.parametrize("bad",[
    {"website":"https://admin:secret@example.org"},
    {"website":"https://example.org/?token=private"},
    {"website":"http://127.0.0.1:8768"},
    {"website":"http://169.254.169.254/latest"},
    {"website":"http://camera.local/stream"},
    {"website":"https://example.org/#access_token=private"},
    {"source_urls":["rtsp://camera.internal/live"]},
    {"description":"Camera is at https://admin:secret@example.org"},
    {"description":"api_key=private-value"},
    {"description":"sk-thisIsAPretendSecretToken123456"},
    {"password":"private-value"},
])
def test_profiles_reject_credentials_and_private_references_without_echoing_them(tmp_path,bad):
    store=BusinessStore(tmp_path/"state",Index(tmp_path))
    with pytest.raises(ValueError) as error:
        store.update("cafe",0,{**profile(),**bad})
    assert "private-value" not in str(error.value)
    assert store.get("cafe")["revision"]==0
    assert store.history("cafe")==[]


def test_connection_and_settings_are_local_drafts_and_cannot_admit_external_work(tmp_path,monkeypatch):
    store=BusinessStore(tmp_path/"state",Index(tmp_path))
    monkeypatch.setattr(socket,"socket",lambda *a,**kw:pytest.fail("Settings must remain local"))
    saved=store.update_settings("cafe",0,{"initial_history_hours":24})
    assert saved["settings"]=={"initial_history_hours":24,"analysis_window_seconds":8}
    saved=store.save_connection("cafe",{"expected_revision":1,"provider":"Existing recorder",
        "type":"recorder_export","source_description":"Operator will select an exported source folder",
        "history_hours":24})
    assert saved["connections"][0]["status"]=="configuration_required"
    assert saved["connections"][0]["ingestion_started"] is False
    assert saved["revision"]==2
    for body in [{"initial_history_hours":49},{"initial_history_hours":-1},{"gpu_credits":100},
                 {"initial_history_hours":True}]:
        with pytest.raises(ValueError):store.update_settings("cafe",2,body)
    for body in [{"api_key":"private-value"},{"source_description":"rtsp://user:password@192.168.1.1/live"},
                 {"status":"connected"},{"history_hours":49}]:
        with pytest.raises(ValueError):store.save_connection("cafe",{"expected_revision":2,
            "provider":"Recorder","type":"camera_stream","source_description":"Pending setup",**body})
    assert store.get("cafe")["revision"]==2


def test_investigation_context_snapshots_business_revision_and_keeps_source_provenance(tmp_path):
    index=Index(tmp_path);state=tmp_path/"state"
    business=BusinessStore(state,index)
    business.update("cafe",0,profile(),origin="agent_research")
    plans=InvestigationPlans(state,index);plan=plans.save("cafe","chat-a",document())
    business.update("cafe",1,profile("Later reference"))
    snapshot=plans.get(plan["id"],"cafe","chat-a")["context_snapshot"]
    assert snapshot["business_profile"]["name"]=="Reference café"
    assert snapshot["business_profile_revision"]==1
    assert snapshot["business_profile_provenance"]["epistemic_status"]=="agent_researched_unconfirmed"
    assert snapshot["business_footage_relationship"]["status"]=="unestablished"
    assert snapshot["sources"][0]["source_reference"]=="https://example.org/source"
    assert plans.context("cafe")["business_profile"]["name"]=="Later reference"


def test_parallel_profile_edits_require_one_current_revision(tmp_path):
    index=Index(tmp_path);state=tmp_path/"state"
    stores=[BusinessStore(state,index),BusinessStore(state,index)]
    ready=threading.Barrier(2)
    def save(number):
        ready.wait(timeout=3)
        try:
            return stores[number].update("cafe",0,profile(str(number)))
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(save,[0,1]))
    assert len([result for result in results if result])==1
    assert stores[0].get("cafe")["revision"]==1
    assert len(stores[0].history("cafe"))==1


def test_canonical_company_onboards_before_any_footage(tmp_path):
    index=Index(tmp_path);index.clips=[]
    store=BusinessStore(tmp_path/'empty-tenant',index)
    assert store.get('company')['revision']==0
    saved=store.update('company',0,profile(),origin='operator')
    assert BusinessStore(tmp_path/'empty-tenant',index).get('company')==saved
    with pytest.raises(ValueError):store.get('arbitrary-collection')
