"""Library browsing exposes only the selected tenant's indexed source files."""
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import server
from test_server_access import FixtureAgent


class LibraryIndex:
    def __init__(self, root, company="demo"):
        self.company = company
        self.clips = []
        for number, relative in enumerate(["day-one/entrance.mp4", "day-two/room.mp4"]):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((company + relative).encode())
            self.clips.append(dict(id=f"{company}-clip-{number}", video_id=f"{company}-asset-{number}",
                dataset="cafe", camera=f"Camera {number}", start=0., end=2., sample_time=1.,
                path=str(path), thumbnail_path=str(path), provenance="Released sampled source",
                camera_semantics="Independent recording clock", source_reference="https://example.org/source"))

    def get_clip(self, key):
        return next((row for row in self.clips if row["id"] == key), None)

    def status(self):
        return {"ready": True, "datasets": ["cafe"]}


def fixture_app(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HarnessAgent", FixtureAgent)
    index_factory = lambda path: LibraryIndex(tmp_path/Path(path).parent.name, Path(path).parent.name)
    return server.create_app(tmp_path/"state", LibraryIndex(tmp_path/"demo"),index_factory=index_factory)


def test_library_is_manifest_scoped_and_downloads_available_source(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    (tmp_path/"demo"/"private.txt").write_text("outside the index")
    with TestClient(app) as client:
        assert client.get("/api/library?collection=cafe").status_code == 401
        client.post("/api/session/demo", headers={"X-Video-Demo": "1"})
        listing = client.get("/api/library?collection=cafe")
        assert listing.status_code == 200
        assert [folder["name"] for folder in listing.json()["folders"]] == ["day-one", "day-two"]
        assert listing.json()["assets"] == []
        child = client.get("/api/library", params={"collection":"cafe", "folder":"day-one"}).json()
        assert child["assets"][0]["relative_path"] == "day-one/entrance.mp4"
        assert str(tmp_path) not in json.dumps(child)
        detail = client.get("/api/assets/demo-asset-0?collection=cafe").json()
        assert detail["provenance"] == "Released sampled source"
        assert detail["camera_semantics"] == "Independent recording clock"
        assert detail["clips"][0]["video_url"] == "/clips/demo-clip-0.mp4"
        download = client.get(detail["original_download_url"])
        assert download.content == b"demoday-one/entrance.mp4"
        assert 'attachment' in download.headers["content-disposition"]
        clip = client.get("/api/clip/demo-clip-0").json()
        assert (clip["asset_id"], clip["library_folder"], clip["collection"]) == ("demo-asset-0", "day-one", "cafe")
        for folder in ["..", "../private.txt", "/etc", "day-one/../day-two", "day-one\\.."]:
            assert client.get("/api/library", params={"collection":"cafe", "folder":folder}).status_code == 400
        assert client.get("/api/library?collection=cafe&folder=missing").status_code == 404


def test_other_tenant_and_collection_cannot_resolve_source_asset(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    access = app.state.access
    access.create_environment("company-b", "Company B")
    with TestClient(app) as client:
        client.post("/api/session", json={"code":access.issue("company-b")}, headers={"X-Video-Demo":"1"})
        assert client.get("/api/assets/company-b-asset-0?collection=cafe").status_code == 200
        assert client.get("/api/assets/demo-asset-0").status_code == 404
        assert client.get("/originals/demo-asset-0").status_code == 404
        assert client.get("/api/assets/company-b-asset-0?collection=warehouse").status_code == 404
        assert client.get("/api/library?collection=warehouse").status_code == 404


def test_description_settings_require_exact_saved_evidence_match(tmp_path, monkeypatch):
    app = fixture_app(tmp_path, monkeypatch)
    state = tmp_path/"state"
    (state/"descriptions").mkdir()
    source = {"id":"demo-clip-0", "sha256":"abc", "source_video_id":"demo-asset-0"}
    description = dict(model="nvidia/Cosmos3-Super", model_revision="revision", text="Observed movement",
                       prompt="Describe the movement", source=source, created_at="2026-10-04T10:00:00Z", run_id="result")
    (state/"descriptions/demo-clip-0.json").write_text(json.dumps([description]))
    (state/"reasoner-cache").mkdir()
    result = dict(model=description["model"], model_revision="revision", fps=4, max_new_tokens=4096,
                  requests=[dict(question=description["prompt"], sources=[source], clip_ids=[source["id"]],
                      result={"observations":[{"clip_id":source["id"],"summary":description["text"]}]})])
    cache = state/"reasoner-cache/exact.json"
    cache.write_text(json.dumps(result))
    with TestClient(app) as client:
        client.post("/api/session/demo", headers={"X-Video-Demo":"1"})
        data = client.get("/api/clip/demo-clip-0").json()["descriptions"][0]
        assert data["question"] == description["prompt"]
        assert data["input_settings"]["sampling_fps"] == 4
        assert data["input_settings"]["max_new_tokens"] == 4096
        assert data["input_settings"]["width"] is None
        # New workers record submitted-file measurements beside the exact source hash.
        # They remain available when only the immutable result cache is retained.
        result["requests"][0]["sources"][0].update(submitted_width=1920, submitted_height=1080,
            submitted_avg_frame_rate="999/200", submitted_num_frames="33")
        cache.write_text(json.dumps(result))
        data = client.get("/api/clip/demo-clip-0").json()["descriptions"][0]
        assert data["input_settings"]["width"] == 1920
        assert data["input_settings"]["height"] == 1080
        assert data["input_settings"]["fps"] == 4.995
        assert data["input_settings"]["num_frames"] == 33
        result["requests"][0]["preprocessing"] = [{"clip_id":source["id"],
            "quality_profile":"source-native", "sampling_fps":None, "effective_width":1920,
            "effective_height":1088, "sampled_frame_indices":list(range(33)), "sampled_frame_count":33,
            "padded_frame_count":34, "video_grid_thw":[17,68,120]}]
        cache.write_text(json.dumps(result))
        data = client.get("/api/clip/demo-clip-0").json()["descriptions"][0]
        assert data["input_settings"]["sampling_fps"] is None
        assert data["input_settings"]["effective_height"] == 1088
        assert data["input_settings"]["sampled_frame_count"] == 33
        assert data["input_settings"]["video_grid_thw"] == [17,68,120]
        description["quality_profile"] = "separate-quality-trial"
        (state/"descriptions/demo-clip-0.json").write_text(json.dumps([description]))
        data = client.get("/api/clip/demo-clip-0").json()["descriptions"][0]
        assert data["input_settings"]["width"] is None
        description.pop("quality_profile")
        (state/"descriptions/demo-clip-0.json").write_text(json.dumps([description]))
        result["requests"][0]["question"] = "A different question"
        cache.write_text(json.dumps(result))
        data = client.get("/api/clip/demo-clip-0").json()["descriptions"][0]
        assert data["input_settings"]["sampling_fps"] is None
