"""Real Unix protocol and fake daemon tests. No model/provider calls."""
import asyncio
from dataclasses import replace
import hashlib
import functools
import time
import tempfile
import json
import os
from pathlib import Path
import socket
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import native_executor as native
from native_executor import (BrokerServer, DockerBackend, ExecutorError, NativeBroker,
                             NativeClient, NativeScope, snapshot_outputs)

@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix="nxb-") as directory:
        yield Path(directory)


def async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


SCOPE = NativeScope("company-a", "alice", "root-chat-a")
TOKEN = "fixture_" + "x" * 48


class FakeBackend:
    def __init__(self, root):
        self.root = root
        self.running = {}
        self.fail_stop = False
        self.fail_start = False
        self.fail_mount = False
        self.started = []
        self.removed = []

    def volume_path(self, key):
        return self.root / key

    async def mount(self, key):
        path = self.volume_path(key)
        for directory in (path / "workspace", path / "codex-state"):
            directory.mkdir(parents=True, exist_ok=True)
        if self.fail_mount:
            raise ExecutorError("fixture_partial_mount")
        return path

    async def unmount(self, volume):
        pass

    async def inspect(self, spec):
        return spec.name in self.running

    async def start(self, spec, capability):
        self.started.append(spec)
        if self.fail_start:
            self.running[spec.name] = None  # Docker create succeeded, attach failed.
            raise ExecutorError("fixture_attach_failure")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c",
            "import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.running[spec.name] = process
        return process

    async def pause(self, spec):
        pass

    async def unpause(self, spec):
        pass

    async def stop(self, spec):
        if self.fail_stop:
            raise ExecutorError("fixture_daemon_unavailable")
        process = self.running.pop(spec.name, None)
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        self.removed.append(spec.name)


def broker(tmp_path, backend=None, **kwargs):
    backend = backend or FakeBackend(tmp_path / "volumes")
    return NativeBroker(tmp_path / "state", tmp_path / "runs", backend,
                        app_uid=os.getuid(), app_gid=os.getgid(), socket_owner=os.getuid(),
                        socket_group=os.getgid(), **kwargs)


def sockets(lease):
    listeners = []
    for name in ("model.sock", "mcp.sock"):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(Path(lease.socket_dir) / name))
        listener.listen()
        listeners.append(listener)
    return listeners


def test_scope_paths_are_host_hashed_and_partitioned():
    assert len(SCOPE.key) == 64
    assert SCOPE.key != replace(SCOPE, owner_id="bob").key
    assert SCOPE.key != replace(SCOPE, company_id="b").key
    assert SCOPE.key != replace(SCOPE, root_chat_id="b").key
    assert len(NativeScope("../../", "../../", "/etc").key) == 64
    with pytest.raises(ExecutorError, match="invalid_scope"):
        NativeScope("", "a", "b")


def test_run_parent_remains_traversable_under_service_private_umask(tmp_path):
    previous = os.umask(0o077)
    try:
        service = broker(tmp_path)
    finally:
        os.umask(previous)
    assert service.root.stat().st_mode & 0o777 == 0o700
    assert service.run_root.stat().st_mode & 0o777 == 0o711


@async_test
async def test_capacity_generation_stop_and_native_state_persist(tmp_path):
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    listeners = sockets(lease)
    process = await service.start(SCOPE, lease, TOKEN)
    workspace = service.backend.volume_path(SCOPE.key) / "workspace"
    (workspace / "report").write_text("persistent")
    other = replace(SCOPE, owner_id="bob")
    other_lease = await service.prepare(other)
    with pytest.raises(ExecutorError, match="capacity_reached"):
        await service.prepare(replace(SCOPE, owner_id="charlie"))
    with pytest.raises(ExecutorError, match="scope_mismatch"):
        await service.stop(other, lease)
    await service.stop(SCOPE, lease)
    assert process.returncode is not None
    assert not service.backend.running
    resumed = await service.prepare(SCOPE)
    assert resumed.generation == lease.generation + 1
    assert (workspace / "report").read_text() == "persistent"
    with pytest.raises(ExecutorError, match="stale_generation"):
        await service.stop(SCOPE, lease)
    await service.stop(SCOPE, resumed)
    await service.stop(other, other_lease)
    for listener in listeners:
        listener.close()


@async_test
async def test_stop_failure_fences_capacity_until_proven_removed(tmp_path):
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    service.backend.fail_stop = True
    with pytest.raises(ExecutorError, match="failed_stop_fence"):
        await service.stop(SCOPE, lease)
    with pytest.raises(ExecutorError, match="failed_stop_fence"):
        await service.prepare(SCOPE)
    assert service._row(SCOPE.key)["status"] == "failed_stop"
    service.backend.fail_stop = False
    await service.stop(SCOPE, lease)
    assert service._row(SCOPE.key)["status"] == "stopped"


@async_test
async def test_retained_storage_is_bounded_separately_from_active_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "MAX_RETAINED_WORKSPACES", 1)
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    await service.stop(SCOPE, lease)
    with pytest.raises(ExecutorError, match="workspace_storage_capacity"):
        await service.prepare(replace(SCOPE, root_chat_id="other-chat"))
    resumed = await service.prepare(SCOPE)
    await service.stop(SCOPE, resumed)


@async_test
async def test_failed_partial_create_and_restart_reconcile_before_new_generation(tmp_path):
    backend = FakeBackend(tmp_path / "volumes")
    first = broker(tmp_path, backend)
    lease = await first.prepare(SCOPE)
    listeners = sockets(lease)
    backend.fail_start = True
    with pytest.raises(ExecutorError, match="fixture_attach_failure"):
        await first.start(SCOPE, lease, TOKEN)
    assert backend.running
    restarted = broker(tmp_path, backend)
    await restarted.reconcile()
    assert not backend.running
    assert restarted._row(SCOPE.key)["status"] == "stopped"
    for listener in listeners:
        listener.close()


@async_test
async def test_partial_mount_is_recorded_for_reconciliation(tmp_path):
    service = broker(tmp_path)
    service.backend.fail_mount = True
    with pytest.raises(ExecutorError, match="fixture_partial_mount"):
        await service.prepare(SCOPE)
    assert service._row(SCOPE.key)["volume"] == str(service.backend.volume_path(SCOPE.key))
    await service.reconcile()
    assert service._row(SCOPE.key)["status"] == "stopped"


@async_test
async def test_expired_prepare_frees_capacity_and_stale_token_rejects(tmp_path):
    now = [1000]
    service = broker(tmp_path, clock=lambda: now[0])
    lease = await service.prepare(SCOPE)
    now[0] += 61
    await service.expire_prepared()
    assert service._row(SCOPE.key)["status"] == "stopped"
    for capability in ["sk-" + "x" * 50, "bad\nTOKEN=x", "short", {"token": TOKEN}]:
        with pytest.raises(ExecutorError, match="invalid_run_capability"):
            await service.start(SCOPE, lease, capability)


@async_test
async def test_socket_mount_contains_only_exact_owned_sockets(tmp_path):
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    listeners = sockets(lease)
    (Path(lease.socket_dir) / "secret.txt").write_text("fixture")
    with pytest.raises(ExecutorError, match="required_sockets_missing"):
        await service.start(SCOPE, lease, TOKEN)
    assert not service.backend.started
    await service.stop(SCOPE, lease)
    for listener in listeners:
        listener.close()


@async_test
async def test_socket_symlink_and_path_override_rejected(tmp_path):
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    (Path(lease.socket_dir) / "model.sock").symlink_to("/etc/passwd")
    (Path(lease.socket_dir) / "mcp.sock").symlink_to("/etc/passwd")
    with pytest.raises(ExecutorError, match="lease_mismatch"):
        await service.start(SCOPE, replace(lease, socket_dir="/tmp"), TOKEN)
    with pytest.raises(ExecutorError, match="invalid_run_socket"):
        await service.start(SCOPE, lease, TOKEN)
    await service.stop(SCOPE, lease)


def output_tree(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "outputs" / "nested").mkdir(parents=True)
    (workspace / "outputs" / "nested" / "report.txt").write_text("Verified output.")
    return workspace


def test_snapshot_preserves_exact_bytes_and_seals_original_changes(tmp_path):
    workspace = output_tree(tmp_path)
    destination = tmp_path / "snapshot"
    value = snapshot_outputs(workspace, destination, uid=os.getuid())
    item = value["files"][0]
    assert item == {"path": "nested/report.txt", "size": 16,
                    "sha256": hashlib.sha256(b"Verified output.").hexdigest(), "index": 0}
    (workspace / "outputs" / item["path"]).write_text("Replaced later")
    assert (destination / "0").read_bytes() == b"Verified output."
    assert (destination / "0").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo", "owner"])
def test_snapshot_rejects_special_files_links_and_wrong_owner(tmp_path, kind):
    workspace = output_tree(tmp_path)
    output = workspace / "outputs" / "bad"
    uid = os.getuid()
    if kind == "symlink":
        output.symlink_to("/etc/passwd")
    elif kind == "directory_symlink":
        output.symlink_to("/tmp", target_is_directory=True)
    elif kind == "hardlink":
        os.link(workspace / "outputs/nested/report.txt", output)
    elif kind == "fifo":
        os.mkfifo(output)
    else:
        uid += 1
    with pytest.raises(ExecutorError):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=uid)
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize("path", ["../report", "/etc/passwd", "nested/../report", "nested//report", "bad\0file"])
def test_snapshot_selection_rejects_traversal(tmp_path, path):
    workspace = output_tree(tmp_path)
    with pytest.raises(ExecutorError, match="invalid_output_path"):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=os.getuid(), paths=[path])


def test_snapshot_file_and_total_limits(tmp_path, monkeypatch):
    workspace = output_tree(tmp_path)
    monkeypatch.setattr(native, "FILE_BYTES", 8)
    with pytest.raises(ExecutorError, match="output_size_limit"):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=os.getuid())
    monkeypatch.setattr(native, "FILE_BYTES", 100)
    monkeypatch.setattr(native, "OUTPUT_BYTES", 24)
    (workspace / "outputs" / "second").write_bytes(b"x" * 16)
    with pytest.raises(ExecutorError, match="output_size_limit"):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=os.getuid())


def test_snapshot_file_count_and_manifest_limits(tmp_path, monkeypatch):
    workspace = output_tree(tmp_path)
    (workspace / "outputs/second.txt").write_text("other")
    monkeypatch.setattr(native, "MAX_FILES", 1)
    with pytest.raises(ExecutorError, match="output_file_limit"):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=os.getuid())
    monkeypatch.setattr(native, "MAX_FILES", 10)
    monkeypatch.setattr(native, "CONTROL_BYTES", 1100)
    with pytest.raises(ExecutorError, match="output_metadata_limit"):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=os.getuid())


def test_snapshot_detects_same_size_mutation_during_read(tmp_path, monkeypatch):
    workspace = output_tree(tmp_path)
    source = workspace / "outputs/nested/report.txt"
    original_read = os.read
    mutated = False
    def read(fd, count):
        nonlocal mutated
        data = original_read(fd, count)
        if data and not mutated:
            mutated = True
            time.sleep(.01)
            source.write_bytes(b"x" * 16)
        return data
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ExecutorError, match="output_changed_during_read"):
        snapshot_outputs(workspace, tmp_path / "snapshot", uid=os.getuid())


def test_docker_launch_profile_cannot_be_changed_by_scope(tmp_path):
    spec = native.WorkerSpec(SCOPE.key, 1, "r" * 48, tmp_path / "volume", tmp_path / "sockets")
    flags = DockerBackend.flags(spec)
    assert flags[flags.index("--runtime") + 1] == "rocoto-runsc"
    assert flags[flags.index("--network") + 1] == "none"
    assert flags[flags.index("--memory") + 1] == "1g"
    assert flags[flags.index("--memory-swap") + 1] == "1g"
    assert flags[flags.index("--pids-limit") + 1] == "128"
    assert flags.count("--mount") == 3
    assert "--privileged" not in flags and "--device" not in flags
    assert native.IMAGE.startswith("sha256:")


@async_test
async def test_real_unix_client_relays_json_lines_and_stop_removes_worker(tmp_path):
    service = broker(tmp_path)
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    client = NativeClient(server.path)
    listeners = []
    try:
        lease = await client.prepare(SCOPE)
        listeners = sockets(lease)
        process = await client.start(SCOPE, lease, TOKEN)
        with pytest.raises(ExecutorError, match="worker_not_prepared"):
            await client.start(SCOPE, lease, TOKEN)
        assert service.backend.running
        line = json.dumps({"id": 1, "method": "initialize", "params": {}}).encode() + b"\n"
        process.stdin.write(line)
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 3) == line
        assert await process.stop() == {"status": "stopped", "generation": 1}
        assert not service.backend.running
        assert service._row(SCOPE.key)["status"] == "stopped"
    finally:
        await server.close()
        for listener in listeners:
            listener.close()


@async_test
async def test_client_disconnect_stops_real_worker_not_just_transport(tmp_path):
    service = broker(tmp_path)
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    client = NativeClient(server.path)
    lease = await client.prepare(SCOPE)
    listeners = sockets(lease)
    try:
        process = await client.start(SCOPE, lease, TOKEN)
        process.stdin.close()
        for _ in range(100):
            if service._row(SCOPE.key)["status"] == "stopped":
                break
            await asyncio.sleep(.01)
        assert not service.backend.running
        assert service._row(SCOPE.key)["status"] == "stopped"
        await process.wait()
    finally:
        await server.close()
        for listener in listeners:
            listener.close()


@async_test
async def test_broker_protocol_rejects_extra_mount_or_executable_fields(tmp_path):
    service = broker(tmp_path)
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(json.dumps({"operation": "prepare", "scope": native.asdict(SCOPE),
                                 "mount": "/", "executable": "/bin/sh"}).encode() + b"\n")
        await writer.drain()
        assert json.loads(await reader.readline()) == {"error": "invalid_request"}
        assert service._row(SCOPE.key) is None
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


@pytest.mark.parametrize("operation", ["start", "stop", "snapshot", "read_output"])
@async_test
async def test_missing_lease_returns_stable_error_without_lifecycle_side_effect(tmp_path, operation):
    service = broker(tmp_path)
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(json.dumps({"operation": operation, "scope": native.asdict(SCOPE)}).encode() + b"\n")
        await writer.drain()
        assert json.loads(await reader.readline()) == {"error": "invalid_request"}
        assert service._row(SCOPE.key) is None
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


@async_test
async def test_cancelled_privileged_command_is_killed_and_reaped_before_return(monkeypatch):
    entered = asyncio.Event()
    actions = []
    class Process:
        returncode = None
        async def communicate(self, data):
            entered.set()
            await asyncio.Future()
        def kill(self):
            actions.append("kill")
            self.returncode = -9
        async def wait(self):
            actions.append("reap")
            return self.returncode
    async def create(*args, **kwargs):
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(native._command("synthetic-command"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert actions == ["kill", "reap"]


@async_test
async def test_command_timeout_handles_completion_race_and_reaps(monkeypatch):
    actions = []
    class Process:
        returncode = None
        async def communicate(self, data):
            await asyncio.Future()
        def kill(self):
            actions.append("kill")
            self.returncode = 0
            raise ProcessLookupError()
        async def wait(self):
            actions.append("reap")
            return 0
    async def create(*args, **kwargs):
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(ExecutorError, match="executor_command_timeout"):
        await native._command("synthetic-command", timeout=.001)
    assert actions == ["kill", "reap"]


@async_test
async def test_peer_uid_must_match_explicit_application_identity(tmp_path):
    service = broker(tmp_path)
    service.app_uid = os.getuid() + 1
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    try:
        with pytest.raises(ExecutorError, match="broker_access_denied"):
            await NativeClient(server.path).prepare(SCOPE)
    finally:
        await server.close()


@async_test
async def test_stdout_eof_reports_after_container_removal(tmp_path):
    service = broker(tmp_path)
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    client = NativeClient(server.path)
    lease = await client.prepare(SCOPE)
    listeners = sockets(lease)
    try:
        process = await client.start(SCOPE, lease, TOKEN)
        process.stdin.write_eof()
        await process.stdin.drain()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert not service.backend.running
        assert service._row(SCOPE.key)["status"] == "stopped"
    finally:
        await server.close()
        for listener in listeners:
            listener.close()


@async_test
async def test_snapshot_download_scope_and_generation_are_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "WORKER_UID", os.getuid())
    # snapshot_outputs default binds at definition; replace only the fixture UID.
    original = native.snapshot_outputs
    monkeypatch.setattr(native, "snapshot_outputs", lambda *args, **kw: original(*args, **kw, uid=os.getuid()))
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    listeners = sockets(lease)
    await service.start(SCOPE, lease, TOKEN)
    workspace = service.backend.volume_path(SCOPE.key) / "workspace"
    (workspace / "outputs").mkdir()
    (workspace / "outputs/report.txt").write_text("hello")
    try:
        result = await service.snapshot(SCOPE, lease)
        stream, meta = await service.output_file(SCOPE, lease, result["snapshot_id"], 0)
        with stream:
            assert stream.read() == b"hello"
        assert meta["path"] == "report.txt"
        with pytest.raises(ExecutorError, match="scope_mismatch"):
            await service.output_file(replace(SCOPE, owner_id="bob"), lease, result["snapshot_id"], 0)
        await service.stop(SCOPE, lease)
        later = await service.prepare(SCOPE)
        with pytest.raises(ExecutorError, match="invalid_snapshot"):
            await service.output_file(SCOPE, later, result["snapshot_id"], 0)
        await service.stop(SCOPE, later)
    finally:
        for listener in listeners:
            listener.close()


@async_test
async def test_single_broker_process_and_pause_failure_fences(tmp_path):
    service = broker(tmp_path)
    server = BrokerServer(service, tmp_path / "control.sock")
    await server.start()
    duplicate = BrokerServer(broker(tmp_path), tmp_path / "other.sock")
    with pytest.raises(ExecutorError, match="broker_already_running"):
        await duplicate.start()
    await duplicate.close()
    assert Path(server.path).exists()
    lease = await service.prepare(SCOPE)
    listeners = sockets(lease)
    try:
        await service.start(SCOPE, lease, TOKEN)
        async def failed_pause(spec):
            raise ExecutorError("fixture_pause_error")
        service.backend.pause = failed_pause
        with pytest.raises(ExecutorError, match="worker_pause_failed"):
            await service.snapshot(SCOPE, lease)
        assert service._row(SCOPE.key)["status"] == "failed_stop"
        await service.stop(SCOPE, lease)
    finally:
        await server.close()
        for listener in listeners:
            listener.close()


@async_test
async def test_snapshot_failure_resumes_worker_and_unpause_failure_fences(tmp_path, monkeypatch):
    service = broker(tmp_path)
    lease = await service.prepare(SCOPE)
    listeners = sockets(lease)
    transitions = []
    async def pause(spec):
        transitions.append("paused")
    async def unpause(spec):
        transitions.append("resumed")
    service.backend.pause, service.backend.unpause = pause, unpause
    def fail_snapshot(*args, **kwargs):
        raise ExecutorError("fixture_copy_error")
    monkeypatch.setattr(native, "snapshot_outputs", fail_snapshot)
    try:
        await service.start(SCOPE, lease, TOKEN)
        with pytest.raises(ExecutorError, match="fixture_copy_error"):
            await service.snapshot(SCOPE, lease)
        assert transitions == ["paused", "resumed"]
        assert service._row(SCOPE.key)["status"] == "running"
        async def failed_unpause(spec):
            raise ExecutorError("fixture_resume_error")
        service.backend.unpause = failed_unpause
        with pytest.raises(ExecutorError, match="failed_stop_fence"):
            await service.snapshot(SCOPE, lease)
        assert service._row(SCOPE.key)["status"] == "failed_stop"
        await service.stop(SCOPE, lease)
    finally:
        for listener in listeners:
            listener.close()
