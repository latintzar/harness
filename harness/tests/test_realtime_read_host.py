"""Full native middleware and lifespan proof for explicit maintained read selection."""

import hashlib
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
import realtime_read_routes as adapter  # noqa: E402
from test_server_access import fixture_app  # noqa: E402

BOOTSTRAP = "/api/control-room/bootstrap"
ADDRESS = "/control-room/maintained/{}/0/0/media"
CHUNK = 65536


def _maintained_host(tmp_path, monkeypatch, calls):
    runtime = adapter._load_backend()

    async def initialize(dsn, **limits):
        calls.append(("open", dsn, limits))

    async def close():
        calls.append(("close",))

    monkeypatch.setattr(runtime, "init_runtime_only", initialize)
    monkeypatch.setattr(runtime, "close_runtime_only", close)
    monkeypatch.setenv("ROCOTO_VIDEO_REALTIME_READS", "maintained")
    monkeypatch.setenv("RUNTIME_DATABASE_URL", "private-test-dsn")
    return fixture_app(tmp_path, monkeypatch)


def _sign_in(app, browser, principal):
    code = app.state.access.issue("demo", owner=True, principal_id=principal)
    response = browser.post("/api/session", json={"code": code}, headers={"X-Video-Demo": "1"})
    assert response.status_code == 200


def _publish(state, principal, body):
    """Seal one recording and its catalog in the workspace's private playback store."""
    root = state / "playback-media" / hashlib.sha256(principal.encode()).hexdigest()
    for directory in (root.parent, root):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
    digest = hashlib.sha256(body).hexdigest()
    company = json.loads((state / ".workspace-identity.json").read_text())["company_scope"]
    manifest = json.dumps({
        "schema_version": 1,
        "owner_principal_id": principal,
        "company_id": company,
        "face_redaction": "source_anonymized",
        "sources": [{
            "source_id": "camera",
            "epoch_id": "e1",
            "label": "Camera one",
            "artifacts": [{
                "mapping_id": "e1-m1",
                "media": {"path": "camera.mp4", "sha256": digest, "size_bytes": len(body)},
                "width": 640,
                "height": 360,
                "fps": 8,
                "duration_s": 10,
            }],
        }],
    }).encode()
    for name, content in (("camera.mp4", body), ("manifest.json", manifest)):
        (root / name).write_bytes(content)
        (root / name).chmod(0o400)
    lineage = {
        "source_id": "camera",
        "epoch_id": "e1",
        "tracker_namespace": "e1",
        "camera_subject_id": "cam",
        "mapping_id": "e1-m1",
        "source_asset_id": "e1-a1",
        "source_sha256": digest,
        "source_window": {"start_s": "0", "end_s": "10"},
        "stream_window": {"start_s": "0", "end_s": "10"},
    }
    return company, hashlib.sha256(manifest).hexdigest(), lineage


def test_full_host_selects_maintained_before_legacy_basket_and_closes_pool(tmp_path, monkeypatch):
    calls = []
    app = _maintained_host(tmp_path, monkeypatch, calls)
    matches = [
        route for route in app.routes if getattr(route, "path", None) == "/api/control-room/visit-basket"
    ]
    assert matches[0].endpoint.__module__ == "realtime_read_routes"
    # Configuration changes apply at the next service boot, never mid-request.
    monkeypatch.setenv("ROCOTO_VIDEO_REALTIME_READS", "legacy")
    with TestClient(app) as browser:
        assert browser.get("/api/control-room/visit-basket").status_code == 401
        assert browser.get(BOOTSTRAP).status_code == 401
        assert browser.get(ADDRESS.format("0" * 64)).status_code == 401
        _sign_in(app, browser, "host-owner")
        for _ in range(2):
            response = browser.get("/api/control-room/visit-basket")
            assert response.status_code == 503
            assert response.json()["detail"] == "maintained_snapshot_required"
        # An uncommissioned catalog is an unavailable view, in the maintained shape.
        for path in (BOOTSTRAP, "/api/control-room"):
            response = browser.get(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "playback_unavailable"}
        assert browser.get(ADDRESS.format("0" * 64)).status_code == 404
        assert browser.get("/control-room/data.json").status_code == 404
        assert browser.get("/control-room/manifest.json").status_code == 404
        assert calls == [("open", "private-test-dsn", {"timeout_s": 5, "max_size": 2})]
    assert calls[-1] == ("close",)


def test_full_host_streams_maintained_media_under_current_authority(tmp_path, monkeypatch):
    """Native middleware, transfer admission and the endpoint's per-chunk decision together.

    This suite runs with no database. The PostgreSQL lineage answer is substituted here;
    backend/tests/test_video_playback_media.py proves it against real PostgreSQL/RLS.
    """
    app = _maintained_host(tmp_path, monkeypatch, [])
    from realtime.media import PlaybackMediaStore
    from realtime.store import AccessDenied

    body = bytes(range(256)) * 800
    company, revision, lineage = _publish(tmp_path / "state", "host-owner", body)
    authority = {"remaining": None}

    async def bindings(self, context, references):
        assert (context.company_id, context.principal_id) == (company, "host-owner")
        assert context.capabilities == frozenset({"realtime:read"})
        assert references == [{"source_id": "camera", "epoch_id": "e1", "mapping_id": "e1-m1"}]
        if authority["remaining"] is not None:
            if not authority["remaining"]:
                raise AccessDenied("source is unavailable in this grant")
            authority["remaining"] -= 1
        return [lineage]

    monkeypatch.setattr(PlaybackMediaStore, "bindings", bindings)
    budget = app.state.transfer_budget

    def reserved():
        return budget.status()["daily_reserved_bytes"]

    with TestClient(app) as browser:
        _sign_in(app, browser, "host-owner")
        value = browser.get(BOOTSTRAP).json()
        assert value["read_authority"] == "maintained"
        media = value["sources"][0]["artifacts"][0]["media_url"]
        assert media == ADDRESS.format(revision)
        assert browser.get("/api/control-room").json() == {"ready": True, "bundle_url": "/control-room/"}
        before = reserved()
        whole = browser.get(media)
        assert whole.status_code == 200 and whole.content == body
        assert whole.headers["cache-control"] == "private, no-store"
        assert whole.headers["x-content-type-options"] == "nosniff"
        assert reserved() - before == len(body)
        part = browser.get(media, headers={"Range": "bytes=2-8"})
        assert part.status_code == 206 and part.content == body[2:9]
        assert part.headers["content-range"] == f"bytes 2-8/{len(body)}"
        assert reserved() - before == len(body) + 7
        assert browser.get(media, headers={"Range": f"bytes={len(body)}-"}).status_code == 416
        assert reserved() - before == len(body) + 7
        # Current authority admits the request and its first chunk, then is revoked.
        authority["remaining"] = 2
        cut = browser.get(media)
        assert cut.status_code == 200
        assert cut.headers["content-length"] == str(len(body))
        assert cut.content == body[:CHUNK]
        assert browser.get(media).status_code == 404
        assert browser.get(BOOTSTRAP).status_code == 404
        assert browser.get("/control-room/data.json").status_code == 404


def test_default_host_does_not_initialize_maintained_pool(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("The legacy host loaded maintained database code")

    monkeypatch.delenv("ROCOTO_VIDEO_REALTIME_READS", raising=False)
    monkeypatch.setattr(adapter, "_load_backend", forbidden)
    app = fixture_app(tmp_path, monkeypatch)
    with TestClient(app) as browser:
        assert app.state.realtime_read_authority == "legacy"
        code = app.state.access.issue("demo", principal_id="legacy-member")
        assert (
            browser.post("/api/session", json={"code": code}, headers={"X-Video-Demo": "1"}).status_code
            == 200
        )
        assert browser.get("/api/realtime/sources/camera/epochs/e1/snapshot").status_code == 404
        response = browser.get(BOOTSTRAP)
        assert response.json() == {
            "schema": "rocoto.player-bootstrap.v1",
            "read_authority": "legacy",
            "sources": [],
        }
        assert response.headers["cache-control"] == "private, no-store"
        assert browser.get(ADDRESS.format("0" * 64)).status_code == 404
