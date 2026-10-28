"""Demo guidance follows available footage and never becomes customer context."""
from fastapi.testclient import TestClient

from test_server_access import fixture_app


def test_customer_with_cafe_dataset_receives_no_demo_questions(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    app.state.access.create_environment("customer", "Demo")
    with TestClient(app) as client:
        client.post("/api/session", json={"code": app.state.access.issue("customer")},
                    headers={"X-Video-Demo": "1"})
        assert client.get("/api/status").json()["demo_guidance"] == {}
        assert client.get("/api/workspace/capabilities?dataset=cafe").json()["demo_context"] is None


def test_demo_guidance_tracks_present_collections_and_verified_company(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    # Commission source data on the host publisher. Public ReadyIndex snapshots
    # are immutable selections and cannot mutate the underlying company corpus.
    source_index = app.state.index._raw
    source_index.clips.append({**source_index.clips[0], "id": "factory-clip",
                                 "video_id": "factory-source", "dataset": "steel-process"})
    monkeypatch.setattr(app.state.index, "status", lambda: {
        "ready": True, "datasets": sorted({clip["dataset"] for clip in app.state.index.clips})})
    with TestClient(app) as client:
        client.post("/api/session/demo", headers={"X-Video-Demo": "1"})
        guidance = client.get("/api/status").json()["demo_guidance"]
        assert set(guidance) == {"cafe", "steel-process"}
        # A similarly named collection alone does not establish the source company.
        assert guidance["steel-process"]["name"] == "Factory"
        source_index.clips[-1]["source_reference"] = (
            "https://commons.wikimedia.org/wiki/File:Proizvodnja_vro%C4%8De_valjane_"
            "debele_plo%C4%8Devine_v_Acroni_SIJ_Jesenice.ogv")
        guidance = client.get("/api/status").json()["demo_guidance"]
        assert guidance["steel-process"]["name"] == "SIJ Acroni"
        assert guidance["steel-process"]["company_url"] == "https://www.acroni.si/en/about-us"
        assert guidance["steel-process"]["brand_image"] == "/assets/demo-companies/sij-acroni.png"
        assert client.get("/api/workspace/capabilities?dataset=steel-process").json()["demo_context"]["name"] == "SIJ Acroni"
        session = client.get("/api/session").json()
        collections = next(e for e in session["environments"] if e["id"] == "demo")["collections"]
        assert next(c for c in collections if c["id"] == "steel-process")["name"] == "SIJ Acroni"
        assert all(item["label"] and item["prompt"] for item in guidance["cafe"]["questions"])
        source_index.clips.append({**source_index.clips[-1], "id": "another-factory",
                                     "video_id": "another-factory", "source_reference": "https://example.com/another-factory"})
        mixed = client.get("/api/status").json()["demo_guidance"]["steel-process"]
        assert mixed["name"] == "Factory" and "company_url" not in mixed and "source_url" not in mixed
        assert "brand_image" not in mixed
        source_index.clips = source_index.clips[:1]
        assert set(client.get("/api/status").json()["demo_guidance"]) == {"cafe"}
