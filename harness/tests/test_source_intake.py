"""Real private files/SQLite fixtures, including crash and concurrent writers."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from source_intake import IntakeActor, IntakeError, IntakeLimits, SourceStore

ALICE = IntakeActor("alice")
BOB = IntakeActor("bob")
OBSERVER = IntakeActor("alice", read_only=True)


def make_store(tmp_path, **kwargs):
    limits = IntakeLimits(max_file_bytes=1024, max_storage_bytes=4096, max_chunk_bytes=256)
    return SourceStore(tmp_path, limits=kwargs.pop("limits", limits), **kwargs)


def source(store, actor=ALICE):
    return store.create_source(actor, collection="warehouse", name="Camera exports")


def upload(store, origin, data=b"synthetic video bytes", actor=ALICE, key="one", **kwargs):
    row = store.begin_upload(actor, origin["id"], filename="capture.mp4", size=len(data), idempotency_key=key, **kwargs)
    store.append_chunk(actor, row["id"], offset=0, data=data)
    return store.seal_upload(actor, row["id"])


def test_real_chunks_seal_hash_modes_and_metadata_remain_private(tmp_path):
    store = make_store(tmp_path)
    origin = source(store)
    data = b"synthetic" * 40
    start = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=len(data), idempotency_key="request-a")
    assert start["status"] == "uploading" and start["committed_bytes"] == 0
    first = store.append_chunk(ALICE, start["id"], offset=0, data=data[:180])
    assert first["committed_bytes"] == 180
    store.append_stream(ALICE, start["id"], offset=180, size=len(data)-180, stream=io.BytesIO(data[180:]))
    sealed = store.seal_upload(ALICE, start["id"], expected_sha256=hashlib.sha256(data).hexdigest())
    assert sealed["status"] == "sealed"
    assert sealed == store.seal_upload(ALICE, start["id"])
    asset = store.get_asset(ALICE, sealed["asset_id"])
    assert asset["status"] == "received" and len(asset["jobs"]) == 1
    assert asset["jobs"][0]["status"] == "queued" and asset["jobs"][0]["stage"] == "prepare"
    original = store.blobs / sealed["sha256"]
    assert original.read_bytes() == data
    assert original.stat().st_mode & 0o777 == 0o400
    assert store.database.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert not (store.quarantine / start["id"]).exists()
    assert str(tmp_path) not in json.dumps([origin, sealed, asset])


def test_idempotent_exact_chunk_retry_and_conflicting_retry(tmp_path):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key="same-request")
    assert store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key="same-request") == begin
    first = store.append_chunk(ALICE, begin["id"], offset=0, data=b"abcd")
    assert store.append_chunk(ALICE, begin["id"], offset=0, data=b"abcd") == first
    for data in [b"bad!", b"abc"]:
        with pytest.raises(IntakeError, match="chunk_conflict"):
            store.append_chunk(ALICE, begin["id"], offset=0, data=data)
    with pytest.raises(IntakeError, match="offset_mismatch"):
        store.append_chunk(ALICE, begin["id"], offset=5, data=b"x")
    with pytest.raises(IntakeError, match="idempotency_conflict"):
        store.begin_upload(ALICE, origin["id"], filename="other.mp4", size=8, idempotency_key="same-request")
    assert (store.quarantine / begin["id"]).read_bytes() == b"abcd"


def test_restart_discards_uncommitted_trailing_bytes_after_fsync_failure(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key="crash")
    fsync = os.fsync
    def failed_sync(fd):
        raise OSError("synthetic filesystem interruption")
    monkeypatch.setattr(os, "fsync", failed_sync)
    with pytest.raises(IntakeError, match="storage_unavailable"):
        store.append_chunk(ALICE, begin["id"], offset=0, data=b"bad!")
    monkeypatch.setattr(os, "fsync", fsync)
    assert (store.quarantine / begin["id"]).read_bytes() == b"bad!"
    restarted = make_store(tmp_path)
    assert restarted.get_upload(ALICE, begin["id"])["committed_bytes"] == 0
    assert restarted.recover()["resumed_uploads"] == 1
    assert (store.quarantine / begin["id"]).read_bytes() == b""
    restarted.append_chunk(ALICE, begin["id"], offset=0, data=b"abcdefgh")
    assert restarted.seal_upload(ALICE, begin["id"])["sha256"] == hashlib.sha256(b"abcdefgh").hexdigest()


@pytest.mark.parametrize("body,declared", [(b"short", 8), (b"too-long", 3)])
def test_bad_chunk_length_rolls_back_offset_then_retries(tmp_path, body, declared):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=12, idempotency_key="length")
    with pytest.raises(IntakeError, match="chunk_length_mismatch"):
        store.append_stream(ALICE, begin["id"], offset=0, size=declared, stream=io.BytesIO(body))
    assert store.get_upload(ALICE, begin["id"])["committed_bytes"] == 0
    store.append_chunk(ALICE, begin["id"], offset=0, data=b"complete1234")
    assert store.seal_upload(ALICE, begin["id"])["status"] == "sealed"


def test_chunk_and_complete_checksum_mismatch_keep_receipt_unpublished(tmp_path):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=4, idempotency_key="checksum")
    with pytest.raises(IntakeError, match="checksum_mismatch"):
        store.append_chunk(ALICE, begin["id"], offset=0, data=b"abcd", sha256="0"*64)
    assert store.get_upload(ALICE, begin["id"])["committed_bytes"] == 0
    store.append_chunk(ALICE, begin["id"], offset=0, data=b"abcd")
    with pytest.raises(IntakeError, match="checksum_mismatch"):
        store.seal_upload(ALICE, begin["id"], expected_sha256="0"*64)
    assert store.get_upload(ALICE, begin["id"])["status"] == "uploading"
    assert store.claim_job(worker_id="worker") is None


def test_sealing_crash_after_blob_move_is_recovered_exactly_once(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=4, idempotency_key="sealing")
    store.append_chunk(ALICE, begin["id"], offset=0, data=b"abcd")
    def crash(*args):
        raise RuntimeError("synthetic crash after durable rename")
    monkeypatch.setattr(store, "_queue_job", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        store.seal_upload(ALICE, begin["id"])
    assert store.get_upload(ALICE, begin["id"])["status"] == "sealing"
    assert not (store.quarantine / begin["id"]).exists()
    assert len(list(store.blobs.iterdir())) == 1
    restarted = make_store(tmp_path)
    assert restarted.recover()["sealed_uploads"] == 1
    restarted.recover()
    result = restarted.get_asset(ALICE, begin["asset_id"])
    assert len(result["jobs"]) == 1
    assert restarted.get_upload(ALICE, begin["id"])["status"] == "sealed"


def test_owner_and_observer_boundaries_every_public_mutation(tmp_path):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=4, idempotency_key="ownership")
    with pytest.raises(IntakeError, match="record_not_found"):
        store.get_source(BOB, origin["id"])
    with pytest.raises(IntakeError, match="record_not_found"):
        store.get_upload(BOB, begin["id"])
    with pytest.raises(IntakeError, match="record_not_found"):
        store.begin_upload(BOB, origin["id"], filename="b.mp4", size=4, idempotency_key="bob")
    with pytest.raises(IntakeError, match="record_not_found"):
        store.append_chunk(BOB, begin["id"], offset=0, data=b"abcd")
    for action in [lambda: source(store, OBSERVER),
                   lambda: store.begin_upload(OBSERVER, origin["id"], filename="a.mp4", size=4, idempotency_key="o"),
                   lambda: store.append_chunk(OBSERVER, begin["id"], offset=0, data=b"abcd"),
                   lambda: store.seal_upload(OBSERVER, begin["id"]),
                   lambda: store.cancel_upload(OBSERVER, begin["id"])]:
        with pytest.raises(IntakeError, match="read_only_workspace"):
            action()
    assert store.get_upload(OBSERVER, begin["id"])["id"] == begin["id"]
    assert not (store.quarantine / begin["id"]).read_bytes()


@pytest.mark.parametrize("filename", ["../private", "/etc/passwd", "folder/file.mp4", "folder\\file.mp4", ".", "..", "bad\0name"])
def test_client_filenames_never_become_host_paths(tmp_path, filename):
    store = make_store(tmp_path)
    with pytest.raises(IntakeError):
        store.begin_upload(ALICE, source(store)["id"], filename=filename, size=4, idempotency_key="a")
    assert not list(store.quarantine.iterdir())


def test_storage_reservation_precedes_bytes_and_cancellation_releases_only_pending(tmp_path):
    limits = IntakeLimits(max_file_bytes=8, max_chunk_bytes=4, max_storage_bytes=12)
    store = make_store(tmp_path, limits=limits)
    origin = source(store)
    first = store.begin_upload(ALICE, origin["id"], filename="a.mp4", size=8, idempotency_key="one")
    with pytest.raises(IntakeError, match="storage_quota"):
        store.begin_upload(ALICE, origin["id"], filename="b.mp4", size=8, idempotency_key="two")
    assert len(list(store.quarantine.iterdir())) == 1
    assert store.cancel_upload(ALICE, first["id"])["status"] == "cancelled"
    assert store.cancel_upload(ALICE, first["id"])["status"] == "cancelled"
    with pytest.raises(IntakeError, match="upload_not_open"):
        store.append_chunk(ALICE, first["id"], offset=0, data=b"abcd")
    second = store.begin_upload(ALICE, origin["id"], filename="b.mp4", size=8, idempotency_key="two")
    assert second["status"] == "uploading"


def test_same_bytes_deduplicate_but_distinct_source_occurrences_survive(tmp_path):
    store = make_store(tmp_path)
    a, b = source(store), source(store)
    first = upload(store, a, key="one", object_key="channel/a.mp4", source_version="version1")
    second = upload(store, b, key="two", object_key="channel/a.mp4", source_version="version1")
    assert first["sha256"] == second["sha256"]
    assert first["asset_id"] != second["asset_id"] and first["object_id"] != second["object_id"]
    assert len(list(store.blobs.iterdir())) == 1
    same = store.begin_upload(ALICE, a["id"], filename="capture.mp4", size=21, idempotency_key="another-key",
                              object_key="channel/a.mp4", source_version="version1")
    assert same["id"] == first["id"]
    newer = upload(store, a, data=b"changed version", key="new", object_key="channel/a.mp4", source_version="version2")
    assert newer["asset_id"] != first["asset_id"]
    assert len(list(store.blobs.iterdir())) == 2


def test_concurrent_request_and_chunk_retry_produce_one_receipt(tmp_path):
    origin = source(make_store(tmp_path))
    def begin(_):
        return make_store(tmp_path).begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key="concurrent")
    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(begin, range(10)))
    assert len({row["id"] for row in rows}) == 1
    def write(_):
        return make_store(tmp_path).append_chunk(ALICE, rows[0]["id"], offset=0, data=b"abcdefgh")
    with ThreadPoolExecutor(max_workers=5) as pool:
        writes = list(pool.map(write, range(10)))
    assert {row["committed_bytes"] for row in writes} == {8}
    assert (make_store(tmp_path).quarantine / rows[0]["id"]).read_bytes() == b"abcdefgh"


def test_concurrent_quota_preadmission_is_serialized(tmp_path):
    limits = IntakeLimits(max_file_bytes=8, max_chunk_bytes=8, max_storage_bytes=8)
    origin = source(make_store(tmp_path, limits=limits))
    def begin(number):
        try:
            return make_store(tmp_path, limits=limits).begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key=str(number))["id"]
        except IntakeError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(begin, range(4)))
    assert results.count("storage_quota") == 3


def test_expired_upload_and_missing_committed_bytes_reconcile_honestly(tmp_path):
    clock = [1000]
    limits = IntakeLimits(max_file_bytes=8, max_chunk_bytes=8, max_storage_bytes=16, upload_ttl_seconds=10)
    store = make_store(tmp_path, limits=limits, clock=lambda: clock[0])
    origin = source(store)
    first = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key="expiry")
    clock[0] = 1011
    with pytest.raises(IntakeError, match="upload_not_open"):
        store.append_chunk(ALICE, first["id"], offset=0, data=b"a")
    assert store.recover()["expired_uploads"] == 1
    assert not (store.quarantine / first["id"]).exists()
    second = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=8, idempotency_key="missing")
    store.append_chunk(ALICE, second["id"], offset=0, data=b"abcd")
    (store.quarantine / second["id"]).write_bytes(b"a")
    assert store.recover()["needs_attention"] == 1
    assert store.get_upload(ALICE, second["id"])["committed_bytes"] == 4
    assert store.get_upload(ALICE, second["id"])["status"] == "needs_attention"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_quarantine_replacement_does_not_read_or_modify_another_file(tmp_path, kind):
    store = make_store(tmp_path)
    begin = store.begin_upload(ALICE, source(store)["id"], filename="clip.mp4", size=8, idempotency_key="replacement")
    target = tmp_path / "private"
    target.write_bytes(b"PRIVATE")
    target.chmod(0o600)
    path = store.quarantine / begin["id"]
    path.unlink()
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises(IntakeError):
        store.append_chunk(ALICE, begin["id"], offset=0, data=b"evil")
    assert target.read_bytes() == b"PRIVATE"


def test_worker_chain_ready_only_after_publish_evidence(tmp_path):
    store = make_store(tmp_path)
    sealed = upload(store, source(store))
    for stage in ("prepare", "embed", "publish"):
        lease = store.claim_job(worker_id="worker")
        assert lease.stage == stage
        with store.open_job_original(lease) as reader:
            assert reader.read() == b"synthetic video bytes"
        assert store.get_asset(ALICE, sealed["asset_id"])["status"] != "ready"
        if stage == "publish":
            with pytest.raises(IntakeError, match="published_generation_required"):
                store.finish_job(lease, result={})
        store.finish_job(lease, result={"generation_id": "verified-generation"} if stage == "publish" else {"artifact_id": "private-worker-result"})
        with pytest.raises(IntakeError, match="stale_job_lease"):
            store.finish_job(lease, result={"generation_id": "late"})
    assert store.get_asset(ALICE, sealed["asset_id"])["status"] == "ready"
    assert store.claim_job(worker_id="worker") is None


def test_expired_worker_requires_reconciliation_and_old_generation_cannot_finish(tmp_path):
    now = [1000]
    store = make_store(tmp_path, clock=lambda: now[0])
    sealed = upload(store, source(store))
    lease = store.claim_job(worker_id="worker", lease_seconds=10)
    now[0] += 11
    assert store.claim_job(worker_id="replacement") is None
    job = store.get_asset(ALICE, sealed["asset_id"])["jobs"][0]
    assert job["status"] == "needs_reconciliation"
    with pytest.raises(IntakeError, match="stale_job_lease"):
        store.finish_job(lease, result={})
    store.reconcile_job(job["id"], expected_generation=job["generation"], outcome="retry", evidence_reference="verified-local-stop-receipt")
    second = store.claim_job(worker_id="replacement")
    assert second.generation > lease.generation and second.token != lease.token
    store.finish_job(second, result={})


def test_running_cancellation_revokes_publication_without_claiming_remote_stop(tmp_path):
    store = make_store(tmp_path)
    sealed = upload(store, source(store))
    lease = store.claim_job(worker_id="worker")
    with pytest.raises(IntakeError, match="read_only_workspace"):
        store.cancel_job(OBSERVER, lease.job_id)
    with pytest.raises(IntakeError, match="record_not_found"):
        store.cancel_job(BOB, lease.job_id)
    cancel = store.cancel_job(ALICE, lease.job_id)
    assert cancel["status"] == "needs_reconciliation" and cancel["error"] == "cancel_requested"
    assert store.cancel_job(ALICE, lease.job_id) == cancel
    with pytest.raises(IntakeError, match="stale_job_lease"):
        store.finish_job(lease, result={})
    with pytest.raises(IntakeError, match="cancelled_work_requires_new_intent"):
        store.reconcile_job(lease.job_id, expected_generation=cancel["generation"], outcome="retry", evidence_reference="stop")
    store.reconcile_job(lease.job_id, expected_generation=cancel["generation"], outcome="cancelled", evidence_reference="provider-stopped-reservation-held")
    assert store.get_asset(ALICE, sealed["asset_id"])["status"] == "cancelled"
    assert store.claim_job(worker_id="worker") is None


def test_queued_cancel_is_terminal_and_sealed_storage_remains(tmp_path):
    store = make_store(tmp_path)
    sealed = upload(store, source(store))
    job = store.get_asset(ALICE, sealed["asset_id"])["jobs"][0]
    assert store.cancel_job(ALICE, job["id"])["status"] == "cancelled"
    assert store.cancel_job(ALICE, job["id"])["status"] == "cancelled"
    assert (store.blobs / sealed["sha256"]).exists()
    with pytest.raises(IntakeError, match="source_received_cancel_job_instead"):
        store.cancel_upload(ALICE, sealed["id"])


def test_heartbeat_and_lease_forgery_or_other_store_are_rejected(tmp_path):
    now = [1000]
    store = make_store(tmp_path / "company-a", clock=lambda: now[0])
    upload(store, source(store))
    lease = store.claim_job(worker_id="worker", lease_seconds=5)
    other = make_store(tmp_path / "company-b", clock=lambda: now[0])
    with pytest.raises(IntakeError, match="stale_job_lease"):
        other.finish_job(lease, result={})
    with pytest.raises(IntakeError, match="stale_job_lease"):
        store.finish_job(replace(lease, token="x"*43), result={})
    now[0] += 4
    refreshed = store.heartbeat_job(lease, lease_seconds=10)
    now[0] += 2
    store.finish_job(refreshed, result={})


def test_concurrent_worker_claim_is_single_and_failure_records_only_code(tmp_path):
    store = make_store(tmp_path)
    sealed = upload(store, source(store))
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda n: make_store(tmp_path).claim_job(worker_id=str(n)), range(10)))
    leases = [lease for lease in results if lease]
    assert len(leases) == 1
    with pytest.raises(IntakeError, match="invalid_job_failure"):
        store.fail_job(leases[0], error_code="Error including private-url?secret=123")
    store.fail_job(leases[0], error_code="provider_interrupted", uncertain=True)
    job = store.get_asset(ALICE, sealed["asset_id"])["jobs"][0]
    assert job["status"] == "needs_reconciliation"
    assert store.claim_job(worker_id="later") is None


def test_tiny_chunk_metadata_is_bounded_and_reclaimed_when_terminal(tmp_path):
    store = make_store(tmp_path, limits=IntakeLimits(max_file_bytes=16, max_storage_bytes=32,
            max_chunk_bytes=8, max_chunks_per_upload=2))
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="tiny.mp4", size=3, idempotency_key="tiny")
    store.append_chunk(ALICE, begin["id"], offset=0, data=b"a")
    store.append_chunk(ALICE, begin["id"], offset=1, data=b"b")
    # Replaying existing chunks consumes no metadata capacity.
    store.append_chunk(ALICE, begin["id"], offset=0, data=b"a")
    with pytest.raises(IntakeError, match="upload_chunk_limit"):
        store.append_chunk(ALICE, begin["id"], offset=2, data=b"c")
    store.cancel_upload(ALICE, begin["id"])
    with store._read() as db:
        assert db.execute("SELECT count(*) FROM upload_chunks").fetchone()[0] == 0
    upload(store, origin, data=b"sealed", key="sealed")
    with store._read() as db:
        assert db.execute("SELECT count(*) FROM upload_chunks").fetchone()[0] == 0


def test_failed_recovery_rolls_back_partial_database_promotion_then_retries(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    origin = source(store)
    begin = store.begin_upload(ALICE, origin["id"], filename="clip.mp4", size=5, idempotency_key="savepoint")
    store.append_chunk(ALICE, begin["id"], offset=0, data=b"bytes")
    queue = store._queue_job
    def failed_queue(*args):
        raise IntakeError("synthetic_storage_failure")
    monkeypatch.setattr(store, "_queue_job", failed_queue)
    with pytest.raises(IntakeError):
        store.seal_upload(ALICE, begin["id"])
    assert store.recover()["needs_attention"] == 1
    with store._read() as db:
        assert db.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
    monkeypatch.setattr(store, "_queue_job", queue)
    assert store.recover()["sealed_uploads"] == 1
    assert store.get_upload(ALICE, begin["id"])["status"] == "sealed"


def test_malformed_host_actor_or_lease_has_stable_error(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(IntakeError, match="invalid_actor"):
        store.create_source(None, collection="cafe", name="Upload")
    with pytest.raises(IntakeError, match="invalid_job_lease"):
        store.finish_job(None, result={})


def test_worker_stage_evidence_is_durable_private_and_lease_bound(tmp_path):
    store = make_store(tmp_path)
    sealed = upload(store, source(store), object_key="remote-recording", source_version="version-1")
    prepare = store.claim_job(worker_id="prepare")
    context = store.job_context(prepare)
    assert context["source"]["collection"] == "warehouse"
    assert context["occurrence"]["source_version"] == "version-1"
    assert context["completed_stages"] == {}
    store.finish_job(prepare, result={"manifest_id": "private-worker-manifest-1"})
    restarted = make_store(tmp_path)
    embed = restarted.claim_job(worker_id="embed")
    assert restarted.job_context(embed)["completed_stages"] == {"prepare": {"manifest_id": "private-worker-manifest-1"}}
    assert "private-worker-manifest" not in json.dumps(restarted.get_asset(ALICE, sealed["asset_id"]))
    with pytest.raises(IntakeError, match="stale_job_lease"):
        restarted.job_context(prepare)
