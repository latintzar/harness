"""Host-owned native Codex worker broker and its narrow Unix-socket client.

The web process supplies authenticated scope and revocable run capabilities. Only
the root broker touches Docker, loop devices, or private execution volumes. Native
JSON-RPC is transported unchanged; this module implements no agent/model loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import struct
import time
from typing import AsyncIterator

import native_package_mount as packages

IMAGE = "sha256:8a7f33ac757f7ab8b0559da41b2f4ead3042f8c136911fd55a72a3d684a4feec"
RUNTIME = "rocoto-runsc"
WORKER_UID = WORKER_GID = 10001
VOLUME_BYTES = 256 * 1024 * 1024
OUTPUT_BYTES = 128 * 1024 * 1024
FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 1024
MAX_WORKERS = 2
MAX_RETAINED_WORKSPACES = 16
FRAME_BYTES = 16 * 1024 * 1024
CONTROL_BYTES = 64 * 1024
DEFAULT_SOCKET = "/run/rocoto-native-broker/control.sock"
_FRAME = struct.Struct("!BI")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ExecutorError(RuntimeError):
    """Stable codes only: never echo subprocess stderr, capabilities, or paths."""


@dataclass(frozen=True)
class NativeScope:
    company_id: str
    owner_id: str
    root_chat_id: str

    def __post_init__(self):
        for value in asdict(self).values():
            if not isinstance(value, str) or not value or len(value.encode()) > 512 or "\0" in value:
                raise ExecutorError("invalid_scope")

    @property
    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class NativeLease:
    scope_key: str
    generation: int
    run_id: str
    socket_dir: str

    def __post_init__(self):
        if (not isinstance(self.scope_key, str) or not re.fullmatch(r"[a-f0-9]{64}", self.scope_key)
                or type(self.generation) is not int or self.generation < 1
                or not isinstance(self.run_id, str) or not re.fullmatch(r"[a-f0-9]{48}", self.run_id)
                or not isinstance(self.socket_dir, str)):
            raise ExecutorError("invalid_lease")


@dataclass(frozen=True)
class WorkerSpec:
    key: str
    generation: int
    run_id: str
    volume: Path
    sockets: Path
    packages: Path | None = None
    plugin_config: dict | None = None

    @property
    def name(self):
        return f"rocoto-native-{self.key}-{self.generation}"


async def _command(*argv, timeout=45, input=None):
    try:
        process = await asyncio.create_subprocess_exec(
            *map(str, argv), stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
        try:
            output, _ = await asyncio.wait_for(process.communicate(input), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # It completed between the returncode check and kill.
            await asyncio.shield(process.wait())
            if isinstance(error, asyncio.TimeoutError):
                raise ExecutorError("executor_command_timeout") from None
            raise
    except OSError:
        raise ExecutorError("executor_command_unavailable") from None
    if process.returncode:
        raise ExecutorError("executor_command_failed")
    return output


class Ext4Volumes:
    """A fixed-size filesystem gives workspace + CODEX_HOME a kernel disk ceiling."""

    def __init__(self, root: Path, *, size_bytes=None):
        size_bytes = VOLUME_BYTES if size_bytes is None else size_bytes
        if type(size_bytes) is not int or not 64 * 1024**2 <= size_bytes <= 2 * 1024**3:
            raise ExecutorError("invalid_volume_size")
        self.root, self.size_bytes = root, size_bytes

    async def mount(self, key: str) -> Path:
        root = self.root / key
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        image, mount = root / "volume.ext4", root / "mount"
        mount.mkdir(mode=0o700, exist_ok=True)
        if not image.exists():
            temporary = root / ("volume-" + secrets.token_hex(8) + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.ftruncate(fd, self.size_bytes)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                await _command("/usr/sbin/mkfs.ext4", "-q", "-F", "-m", "0", "-N", "8192", temporary)
                os.replace(temporary, image)
            finally:
                temporary.unlink(missing_ok=True)
        info = image.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size != self.size_bytes:
            raise ExecutorError("invalid_volume")
        if not os.path.ismount(mount):
            await _command("/usr/bin/mount", "-t", "ext4", "-o", "loop,nodev,nosuid", image, mount)
        # Verify the actual mounted device maps this exact broker-owned image.
        mounted = json.loads(await _command("/usr/bin/findmnt", "--json", "--mountpoint", mount,
                                           "--output", "SOURCE,FSTYPE,OPTIONS"))["filesystems"][0]
        loops = json.loads(await _command("/usr/sbin/losetup", "--json", "--output", "NAME,BACK-FILE"))["loopdevices"]
        source = mounted["source"]
        options = set(mounted["options"].split(","))
        if (mounted["fstype"] != "ext4" or not {"nodev", "nosuid"} <= options or
                not any(item["name"] == source and item["back-file"] == str(image) for item in loops)):
            raise ExecutorError("volume_mount_mismatch")
        for name in ("workspace", "codex-state"):
            path = mount / name
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ExecutorError("invalid_volume_directory")
            os.chown(path, WORKER_UID, WORKER_GID)
        return mount

    async def unmount(self, volume: Path):
        if os.path.ismount(volume):
            await _command("/usr/bin/umount", volume)


class DockerBackend:
    """Every Docker argument is selected here; callers cannot supply mounts/commands."""

    def __init__(self, volumes: Ext4Volumes):
        self.volumes = volumes
        self.docker = ("/opt/rocoto-native-executor/docker-29.8.2/docker", "--host",
                       "unix:///run/rocoto-native-docker/docker.sock")

    async def mount(self, key):
        return await self.volumes.mount(key)

    def volume_path(self, key):
        return self.volumes.root / key / "mount"

    async def unmount(self, volume):
        await self.volumes.unmount(volume)

    async def inspect(self, spec):
        # A successful list must prove absence; inspect failure is not absence.
        output = await _command(*self.docker, "ps", "--all", "--quiet", "--filter", "name=^/" + spec.name + "$")
        return bool(output.strip())

    @staticmethod
    def flags(spec):
        flags = ["--name", spec.name, "--label", "com.rocoto.native-broker=true",
                "--runtime", RUNTIME, "--network", "none", "--memory", "1g", "--memory-swap", "1g",
                "--cpus", "1", "--pids-limit", "128", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "10001:10001", "--log-driver", "none",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs", "/home/rocoto:rw,nosuid,nodev,size=16m,uid=10001,gid=10001",
                "--mount", f"type=bind,src={spec.volume}/workspace,dst=/workspace",
                "--mount", f"type=bind,src={spec.volume}/codex-state,dst=/codex-state",
                "--mount", f"type=bind,src={spec.sockets},dst=/run/rocoto,readonly"]
        if spec.packages is not None:
            flags += ["--mount", f"type=bind,src={spec.packages},dst=/codex-state/plugins/cache,readonly"]
        return flags

    async def start(self, spec, capability):
        # The only secret admitted here is a revocable gateway capability. It is
        # written through a private descriptor, never placed on the command line.
        config = {
            "model_provider": "rocoto_metered",
            "model_providers.rocoto_metered.name": "Rocoto funded gateway",
            "model_providers.rocoto_metered.base_url": "http://127.0.0.1:8765/v1",
            "model_providers.rocoto_metered.env_key": "ROCOTO_AGENT_GATEWAY_TOKEN",
            "model_providers.rocoto_metered.wire_api": "responses",
            "model_providers.rocoto_metered.requires_openai_auth": False,
            "model_providers.rocoto_metered.supports_websockets": False,
            "model_providers.rocoto_metered.request_max_retries": 0,
            "model_providers.rocoto_metered.stream_max_retries": 0,
            "features.skip_host_skill_discovery": True,
            "features.apps": False, "features.plugins": bool(spec.plugin_config), "features.hooks": False,
            "features.remote_plugin": False, "features.plugin_sharing": False,
            "features.skill_mcp_dependency_install": False,
            "plugins": spec.plugin_config or {},
            "features.image_generation": False, "features.computer_use": False,
            "features.browser_use": False, "mcp_servers": {},
        }
        args = [part for key, value in config.items() for part in ("-c", key + "=" + packages.toml_value(value))]
        # docker --env-file reads stdin through /dev/stdin. Its process stdin is
        # separate from the subsequent native app-server attach connection.
        await _command(*self.docker, "create", "--interactive", *self.flags(spec),
                       "--env-file", "/dev/stdin", IMAGE, *args,
                       input=("ROCOTO_AGENT_GATEWAY_TOKEN=" + capability + "\n").encode())
        return await asyncio.create_subprocess_exec(
            *self.docker, "start", "--attach", "--interactive", spec.name,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=FRAME_BYTES,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})

    async def pause(self, spec):
        await _command(*self.docker, "pause", spec.name)
        value = await _command(*self.docker, "inspect", "--format", "{{.State.Paused}}", spec.name)
        if value.strip() != b"true":
            raise ExecutorError("worker_not_quiescent")

    async def unpause(self, spec):
        await _command(*self.docker, "unpause", spec.name)

    async def stop(self, spec):
        if await self.inspect(spec):
            await _command(*self.docker, "rm", "--force", spec.name)
        if await self.inspect(spec):
            raise ExecutorError("container_still_present")


def _path_parts(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 2048:
        raise ExecutorError("invalid_output_path")
    parts = value.split("/")
    if len(parts) > 16 or any(not part or part in {".", ".."} or "\0" in part or len(part.encode()) > 240 for part in parts):
        raise ExecutorError("invalid_output_path")
    return parts


def _checked_directory(parent, name, *, uid):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != uid:
        os.close(fd)
        raise ExecutorError("output_owner_mismatch")
    return fd


def snapshot_outputs(workspace: Path, destination: Path, *, uid=WORKER_UID, paths=None):
    """Snapshot owned ordinary files without following a mutable path or symlink."""
    manifest = []
    total = 0
    destination.mkdir(mode=0o700)
    root = -1
    try:
        workspace_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(workspace_fd).st_uid != uid:
                raise ExecutorError("output_owner_mismatch")
            root = _checked_directory(workspace_fd, "outputs", uid=uid)
        finally:
            os.close(workspace_fd)

        def copy_file(parent, name, relative):
            nonlocal total
            if len(manifest) >= MAX_FILES:
                raise ExecutorError("output_file_limit")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1:
                    raise ExecutorError("invalid_output_file")
                if before.st_size > FILE_BYTES or total + before.st_size > OUTPUT_BYTES:
                    raise ExecutorError("output_size_limit")
                output = destination / str(len(manifest))
                digest = hashlib.sha256()
                size = 0
                with output.open("xb") as stream:
                    os.chmod(output, 0o600)
                    while data := os.read(fd, 64 * 1024):
                        size += len(data)
                        if size > FILE_BYTES or total + size > OUTPUT_BYTES:
                            raise ExecutorError("output_size_limit")
                        digest.update(data)
                        stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                after = os.fstat(fd)
                fields = ("st_dev", "st_ino", "st_size", "st_uid", "st_nlink", "st_mtime_ns", "st_ctime_ns")
                if size != before.st_size or any(getattr(before, field) != getattr(after, field) for field in fields):
                    raise ExecutorError("output_changed_during_read")
                total += size
                manifest.append({"path": relative, "size": size, "sha256": digest.hexdigest(), "index": len(manifest)})
            finally:
                os.close(fd)

        visits = 0
        def walk(parent, prefix="", depth=0):
            nonlocal visits
            if depth > 16:
                raise ExecutorError("output_path_limit")
            with os.scandir(parent) as entries:
                for entry in entries:
                    visits += 1
                    if visits > 2048:
                        raise ExecutorError("output_file_limit")
                    relative = prefix + entry.name
                    _path_parts(relative)
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = _checked_directory(parent, entry.name, uid=uid)
                        try:
                            walk(child, relative + "/", depth + 1)
                        finally:
                            os.close(child)
                    else:
                        copy_file(parent, entry.name, relative)

        if paths is None:
            walk(root)
        else:
            if not isinstance(paths, list) or not paths or len(paths) > MAX_FILES or len(set(paths)) != len(paths):
                raise ExecutorError("invalid_output_selection")
            for path in paths:
                parts = _path_parts(path)
                parent = os.dup(root)
                try:
                    for part in parts[:-1]:
                        child = _checked_directory(parent, part, uid=uid)
                        os.close(parent)
                        parent = child
                    copy_file(parent, parts[-1], path)
                finally:
                    os.close(parent)
        value = {"files": manifest, "total_bytes": total}
        if len(json.dumps(value).encode()) > CONTROL_BYTES - 1024:
            raise ExecutorError("output_metadata_limit")
        (destination / "manifest.json").write_text(json.dumps(value))
        return value
    except (OSError, ValueError, TypeError):
        shutil.rmtree(destination)
        raise ExecutorError("output_snapshot_failed") from None
    except BaseException:
        shutil.rmtree(destination)
        raise
    finally:
        if root >= 0:
            os.close(root)


class NativeBroker:
    """One process owns lifecycle. SQLite persists all fences before side effects."""

    def __init__(self, root: Path, run_root: Path, backend, *, app_uid: int, app_gid: int,
                 clock=time.time, socket_owner=WORKER_UID, socket_group=WORKER_GID):
        self.root, self.run_root, self.backend = Path(root), Path(run_root), backend
        self.app_uid, self.app_gid = app_uid, app_gid
        self.socket_owner, self.socket_group, self.clock = socket_owner, socket_group, clock
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run_root.mkdir(mode=0o711, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.run_root, 0o711)  # Explicit under the service's private umask.
        self.db = sqlite3.connect(self.root / "leases.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS leases (
            key TEXT PRIMARY KEY, generation INTEGER NOT NULL, run_id TEXT NOT NULL,
            status TEXT NOT NULL, updated REAL NOT NULL, volume TEXT NOT NULL DEFAULT '')""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS package_selections (
            key TEXT PRIMARY KEY, generation INTEGER NOT NULL, run_id TEXT NOT NULL,
            selection_sha256 TEXT NOT NULL, metadata TEXT NOT NULL, config TEXT NOT NULL,
            status TEXT NOT NULL)""")
        self.lock = asyncio.Lock()
        self.processes = {}
        self.uploads = {}
        self.installer = None

    def _row(self, key):
        return self.db.execute("SELECT * FROM leases WHERE key=?", (key,)).fetchone()

    def _spec(self, row):
        selected = self.db.execute("SELECT * FROM package_selections WHERE key=? AND generation=? AND run_id=? AND status='sealed'",
            (row['key'],row['generation'],row['run_id'])).fetchone()
        return WorkerSpec(row["key"], row["generation"], row["run_id"], Path(row["volume"]), self.run_root / row["run_id"],
            self.root/'packages'/row['run_id'] if selected else None, json.loads(selected['config']) if selected else None)

    def _update(self, key, status, *, volume=None):
        if volume is None:
            self.db.execute("UPDATE leases SET status=?,updated=? WHERE key=?", (status, self.clock(), key))
        else:
            self.db.execute("UPDATE leases SET status=?,updated=?,volume=? WHERE key=?", (status, self.clock(), str(volume), key))

    def _validate(self, scope, lease):
        if not isinstance(lease, NativeLease):
            raise ExecutorError("invalid_lease")
        if lease.scope_key != scope.key:
            raise ExecutorError("scope_mismatch")
        row = self._row(scope.key)
        if not row or row["generation"] != lease.generation or row["run_id"] != lease.run_id:
            raise ExecutorError("stale_generation")
        if lease.socket_dir != str(self.run_root / row["run_id"]):
            raise ExecutorError("lease_mismatch")
        return row

    async def prepare(self, scope: NativeScope):
        async with self.lock:
            row = self._row(scope.key)
            if row and row["status"] != "stopped":
                raise ExecutorError("workspace_busy" if row["status"] != "failed_stop" else "failed_stop_fence")
            if row is None and self.db.execute("SELECT count(*) FROM leases").fetchone()[0] >= MAX_RETAINED_WORKSPACES:
                raise ExecutorError("workspace_storage_capacity")
            count = self.db.execute("SELECT count(*) FROM leases WHERE status!='stopped'").fetchone()[0]
            if count + (self.installer.live_count() if self.installer else 0) >= MAX_WORKERS:
                raise ExecutorError("capacity_reached")
            generation = row["generation"] + 1 if row else 1
            run_id = secrets.token_hex(24)
            self.db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?)",
                            (scope.key, generation, run_id, "preparing", self.clock(), str(self.backend.volume_path(scope.key))))
            try:
                directory = self.run_root / run_id
                directory.mkdir(mode=0o700)
                os.chown(directory, self.app_uid, self.app_gid)
                snapshots = self.root / "snapshots" / scope.key
                if snapshots.exists():
                    shutil.rmtree(snapshots)  # Previous-generation delivery buffers.
                volume = await self.backend.mount(scope.key)
                self._update(scope.key, "prepared", volume=volume)
                return NativeLease(scope.key, generation, run_id, str(directory))
            except BaseException:
                # A partial mount must be reconciled, never erased optimistically.
                self._update(scope.key, "failed_stop")
                raise

    def _freeze_sockets(self, spec):
        directory = spec.sockets.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != self.app_uid:
            raise ExecutorError("invalid_socket_directory")
        # Removing write access first closes socket replacement races with the app.
        os.chown(spec.sockets, 0 if os.geteuid() == 0 else os.geteuid(), self.socket_group)
        os.chmod(spec.sockets, 0o510)
        names = {path.name for path in spec.sockets.iterdir()}
        if names != {"model.sock", "mcp.sock"}:
            raise ExecutorError("required_sockets_missing")
        for name in sorted(names):
            path = spec.sockets / name
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.app_uid or info.st_nlink != 1:
                raise ExecutorError("invalid_run_socket")
            os.chown(path, self.socket_owner, self.socket_group, follow_symlinks=False)
            os.chmod(path, 0o600, follow_symlinks=False)

    async def install_packages(self, scope, lease, reader, writer):
        """Reserve the lease before accepting a bounded stream; no host paths."""
        task=asyncio.current_task()
        key=(scope.key,lease.generation)
        directory=self.root/'packages'/lease.run_id
        async with self.lock:
            row=self._validate(scope,lease)
            if row['status']!='prepared' or self.db.execute('SELECT 1 FROM package_selections WHERE key=?',(scope.key,)).fetchone():
                raise ExecutorError('package_selection_already_bound')
            self._update(scope.key,'uploading')
            self.uploads[key]=task
            self.db.execute('INSERT INTO package_selections VALUES(?,?,?,?,?,?,?)',
                (scope.key,lease.generation,lease.run_id,'','{}','{}','receiving'))
        try:
            async with asyncio.timeout(packages.UPLOAD_SECONDS):
                await BrokerServer._reply(writer,{'status':'receiving','generation':lease.generation})
                kind,body=await _read_package_frame(reader,packages.METADATA_BYTES)
                if kind!=5:raise ExecutorError('invalid_package_frame')
                value=packages.validate_metadata(json.loads(body),scope.company_id,scope.owner_id)
                directory.mkdir(mode=0o700,parents=True)
                for package in value['packages']:
                    tree=directory/packages.MARKETPLACE/package['id']/package['version']
                    tree.mkdir(mode=0o700,parents=True)
                    for name in package['directories']:(tree/name).mkdir(mode=0o700)
                    for item in package['files']:
                        remaining=item['size_bytes'];hasher=hashlib.sha256()
                        with (tree/item['path']).open('xb') as stream:
                            os.fchmod(stream.fileno(),0o700 if item['executable'] else 0o600)
                            while remaining:
                                kind,data=await _read_package_frame(reader,min(packages.CHUNK_BYTES,remaining))
                                if kind!=6 or not data:raise ExecutorError('invalid_package_frame')
                                stream.write(data);hasher.update(data);remaining-=len(data)
                            stream.flush();os.fsync(stream.fileno())
                        if hasher.hexdigest()!=item['sha256']:raise ExecutorError('package_file_mismatch')
                kind,body=await _read_package_frame(reader,0)
                if kind!=7 or body:raise ExecutorError('invalid_package_frame')
                config=packages.native_config(directory,value)
                packages.seal(directory)
                async with self.lock:
                    row=self._validate(scope,lease)
                    if row['status']!='uploading':raise ExecutorError('package_upload_stopped')
                    self.db.execute("UPDATE package_selections SET selection_sha256=?,metadata=?,config=?,status='sealed' WHERE key=?",
                        (value['selection_sha256'],packages.encoded(value).decode(),packages.encoded(config).decode(),scope.key))
                    self._update(scope.key,'prepared')
                return {'status':'sealed','selection_sha256':value['selection_sha256'],'generation':lease.generation,'plugins':config}
        except BaseException:
            async with self.lock:
                row=self._validate(scope,lease)
                if row['status']=='uploading':self._update(scope.key,'failed_stop')
            raise
        finally:
            self.uploads.pop(key,None)

    async def start(self, scope, lease, capability):
        if not isinstance(capability, str) or not _IDENTIFIER.fullmatch(capability) or capability.startswith("sk-"):
            raise ExecutorError("invalid_run_capability")
        async with self.lock:
            row = self._validate(scope, lease)
            if row["status"] != "prepared":
                raise ExecutorError("worker_not_prepared")
            spec = self._spec(row)
            self._update(scope.key, "starting")
            try:
                self._freeze_sockets(spec)
                if spec.packages is not None:packages.ensure_mount_target(spec.volume)
                process = await self.backend.start(spec, capability)
                self.processes[(scope.key, lease.generation)] = process
                self._update(scope.key, "running")
                return process
            except BaseException:
                self._update(scope.key, "failed_stop")
                # Keep the exact generation fenced. Explicit stop/recovery proves
                # container absence and unmount before capacity can be reused.
                raise

    async def _stop_row(self, row):
        spec = self._spec(row)
        self._update(row["key"], "stopping")
        try:
            await self.backend.stop(spec)
            process = self.processes.pop((row["key"], row["generation"]), None)
            if process:
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.kill()  # Only after actual container removal was proven.
                    await process.wait()
            if row["volume"]:
                await self.backend.unmount(spec.volume)
            if spec.sockets.exists():
                os.chmod(spec.sockets, 0o700)
                shutil.rmtree(spec.sockets)
            selected=self.root/'packages'/row['run_id']
            if selected.exists():
                # Root-owned sealed public package copy, after confirmed worker removal.
                for directory,_,_ in os.walk(selected):os.chmod(directory,0o700)
                shutil.rmtree(selected)
            self.db.execute('DELETE FROM package_selections WHERE key=?',(row['key'],))
            self._update(row["key"], "stopped")
        except BaseException:
            self._update(row["key"], "failed_stop")
            raise ExecutorError("failed_stop_fence") from None

    async def stop(self, scope, lease):
        """Caller revokes model/MCP grants first; removal kills the whole process tree."""
        async with self.lock:
            row=self._validate(scope,lease)
            upload=self.uploads.get((scope.key,lease.generation))
            if upload:
                self._update(scope.key,'stopping')
                upload.cancel()
        if upload and upload is not asyncio.current_task():
            await asyncio.gather(upload,return_exceptions=True)
        async with self.lock:
            row = self._validate(scope, lease)
            if row["status"] != "stopped":
                await self._stop_row(row)
            return {"status": "stopped", "generation": lease.generation}

    async def reconcile(self):
        """After broker restart, remove every recorded worker before accepting input."""
        if self.installer:await self.installer.reconcile()
        async with self.lock:
            rows = self.db.execute("SELECT * FROM leases WHERE status!='stopped'").fetchall()
            for row in rows:
                try:
                    await self._stop_row(row)
                except ExecutorError:
                    pass  # The persisted failed-stop fence still consumes capacity.

    async def expire_prepared(self):
        if self.installer:await self.installer.expire()
        async with self.lock:
            rows = self.db.execute("SELECT * FROM leases WHERE status='prepared' AND updated<?",
                                   (self.clock() - 60,)).fetchall()
            for row in rows:
                try:
                    await self._stop_row(row)
                except ExecutorError:
                    pass

    async def snapshot(self, scope, lease, paths=None):
        async with self.lock:
            row = self._validate(scope, lease)
            if row["status"] != "running":
                raise ExecutorError("worker_not_running")
            parent = self.root / "snapshots" / scope.key
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Delivery buffers are recreated from the durable workspace. Clear
            # an interrupted prior copy before allocating another bounded copy.
            prior = sorted(parent.iterdir(), key=lambda item: item.stat().st_mtime_ns, reverse=True)
            retained = False
            for old in prior:
                if not retained and (old / "manifest.json").is_file():
                    retained = True
                else:
                    shutil.rmtree(old)
            identity = secrets.token_hex(24)
            target = parent / identity
            spec = self._spec(row)
            try:
                await self.backend.pause(spec)
            except BaseException:
                self._update(scope.key, "failed_stop")
                raise ExecutorError("worker_pause_failed") from None
            copy_task = asyncio.create_task(asyncio.to_thread(snapshot_outputs, spec.volume / "workspace", target, paths=paths))
            try:
                try:
                    value = await asyncio.shield(copy_task)
                except asyncio.CancelledError:
                    await copy_task  # Finish the bounded read before resuming writers.
                    raise
            finally:
                try:
                    await asyncio.shield(self.backend.unpause(spec))
                except BaseException:
                    self._update(scope.key, "failed_stop")
                    raise ExecutorError("failed_stop_fence") from None
            value.update(snapshot_id=identity, generation=lease.generation)
            (target / "manifest.json").write_text(json.dumps(value))
            for old in parent.iterdir():
                if old != target:
                    shutil.rmtree(old)
            return value

    async def output_file(self, scope, lease, snapshot_id, index):
        async with self.lock:
            self._validate(scope, lease)
            if not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{48}", snapshot_id) or type(index) is not int:
                raise ExecutorError("invalid_snapshot")
            root = self.root / "snapshots" / scope.key / snapshot_id
            try:
                manifest = json.loads((root / "manifest.json").read_text())
                if manifest["generation"] != lease.generation or not 0 <= index < len(manifest["files"]):
                    raise ExecutorError("invalid_snapshot")
                # Opening under the lifecycle lock makes a subsequent snapshot's
                # cleanup harmless; the sealed inode remains valid for this reader.
                return (root / str(index)).open("rb"), manifest["files"][index]
            except (OSError, ValueError, KeyError):
                raise ExecutorError("invalid_snapshot") from None


async def _read_package_frame(reader,limit):
    kind,size=_FRAME.unpack(await reader.readexactly(_FRAME.size))
    if size>limit:raise ExecutorError('package_frame_limit')
    return kind,await reader.readexactly(size)


async def _read_frame(reader):
    kind, size = _FRAME.unpack(await reader.readexactly(_FRAME.size))
    if size > FRAME_BYTES:
        raise ExecutorError("frame_too_large")
    return kind, await reader.readexactly(size)


async def _write_frame(writer, kind, body):
    if len(body) > FRAME_BYTES:
        raise ExecutorError("frame_too_large")
    writer.write(_FRAME.pack(kind, len(body)) + body)
    await writer.drain()


class BrokerServer:
    def __init__(self, broker: NativeBroker, path=DEFAULT_SOCKET):
        self.broker, self.path = broker, str(path)
        self.server = None
        self.connections = set()
        self.ticker = None
        self._lock_fd = None

    async def start(self):
        self._lock_fd = os.open(self.broker.root / "broker.lock", os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise ExecutorError("broker_already_running") from None
        await self.broker.reconcile()
        Path(self.path).parent.mkdir(mode=0o711, parents=True, exist_ok=True)
        Path(self.path).unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._connection, path=self.path, limit=CONTROL_BYTES)
        os.chown(self.path, os.geteuid(), self.broker.app_gid)
        os.chmod(self.path, 0o660)
        async def expire():
            while True:
                await asyncio.sleep(15)
                await self.broker.expire_prepared()
        self.ticker = asyncio.create_task(expire())

    async def close(self):
        if self._lock_fd is None:
            return  # A failed duplicate start never owns another broker's socket.
        if self.ticker:
            self.ticker.cancel()
            await asyncio.gather(self.ticker, return_exceptions=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        await self.broker.reconcile()
        Path(self.path).unlink(missing_ok=True)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        started = None
        try:
            credentials = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != self.broker.app_uid or len(self.connections) > 12:
                raise ExecutorError("broker_access_denied")
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > CONTROL_BYTES:
                raise ExecutorError("request_too_large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ExecutorError("invalid_request")
            scope = NativeScope(**request["scope"])
            operation = request.get("operation")
            if isinstance(operation,str) and operation.startswith('plugin_'):
                if self.broker.installer is None:raise ExecutorError('plugin_installer_uncommissioned')
                await self.broker.installer.handle(operation,scope,request,reader,writer)
                return
            lease = NativeLease(**request["lease"]) if "lease" in request else None
            if operation == "prepare":
                self._fields(request, {"operation", "scope"})
                value = asdict(await self.broker.prepare(scope))
            elif operation == 'install_packages':
                self._fields(request,{'operation','scope','lease'})
                value=await self.broker.install_packages(scope,lease,reader,writer)
            elif operation == "stop":
                self._fields(request, {"operation", "scope", "lease"})
                value = await self.broker.stop(scope, lease)
            elif operation == "snapshot":
                self._fields(request, {"operation", "scope", "lease", "paths"})
                value = await self.broker.snapshot(scope, lease, request.get("paths"))
            elif operation == "read_output":
                self._fields(request, {"operation", "scope", "lease", "snapshot_id", "index"})
                stream, value = await self.broker.output_file(scope, lease, request["snapshot_id"], request["index"])
                try:
                    await self._reply(writer, value)
                    while data := await asyncio.to_thread(stream.read, 64 * 1024):
                        await _write_frame(writer, 2, data)
                    await _write_frame(writer, 4, b'{"returncode":0}')
                finally:
                    stream.close()
                return
            elif operation == "start":
                self._fields(request, {"operation", "scope", "lease", "capability"})
                process = await self.broker.start(scope, lease, request["capability"])
                started = (scope, lease)
                await self._reply(writer, {"status": "running", "generation": lease.generation, "image": IMAGE})
                returncode = await self._relay(process, reader, writer)
                # A terminal frame means actual container absence, not merely a
                # detached/closed Docker CLI. Socket closure is also fail-closed.
                await self.broker.stop(scope, lease)
                started = None
                await _write_frame(writer, 4, json.dumps({"returncode": returncode}).encode())
                return
            else:
                raise ExecutorError("unknown_operation")
            await self._reply(writer, value)
        except (ExecutorError, KeyError, TypeError, ValueError, asyncio.TimeoutError, OSError, asyncio.IncompleteReadError) as error:
            code = str(error) if isinstance(error, (ExecutorError,packages.PackageMountError)) else "invalid_request"
            try:
                await self._reply(writer, {"error": code})
            except (OSError, ConnectionError):
                pass
        finally:
            if started:
                try:
                    await asyncio.shield(self.broker.stop(*started))
                except ExecutorError:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            self.connections.discard(task)

    @staticmethod
    def _fields(value, allowed):
        required = allowed - {"paths"} if value.get("operation") == "snapshot" else allowed
        if set(value) - allowed or required - set(value):
            raise ExecutorError("invalid_request")

    @staticmethod
    async def _reply(writer, value):
        body = json.dumps(value).encode() + b"\n"
        if len(body) > CONTROL_BYTES:
            raise ExecutorError("response_too_large")
        writer.write(body)
        await writer.drain()

    @staticmethod
    async def _relay(process, reader, writer):
        async def stdin():
            while True:
                kind, data = await _read_frame(reader)
                if kind == 1:
                    process.stdin.write(data)
                    await process.stdin.drain()
                elif kind == 0 and not data:
                    process.stdin.write_eof()
                    return
                else:
                    raise ExecutorError("invalid_stream_frame")
        async def output(source, kind):
            while data := await source.read(64 * 1024):
                await _write_frame(writer, kind, data)
        incoming = asyncio.create_task(stdin())
        outputs = [asyncio.create_task(output(process.stdout, 2)), asyncio.create_task(output(process.stderr, 3))]
        exited = asyncio.create_task(process.wait())
        try:
            pending = {incoming, exited, *outputs}
            while exited in pending:
                finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for item in finished:
                    item.result()  # A disconnected client fails, causing real stop.
            await asyncio.gather(*outputs)
            return process.returncode
        finally:
            for item in [incoming, exited, *outputs]:
                item.cancel()
            await asyncio.gather(incoming, exited, *outputs, return_exceptions=True)


class _NativeStdin:
    def __init__(self, writer):
        self.writer = writer

    def write(self, data):
        if len(data) > FRAME_BYTES:
            raise ExecutorError("frame_too_large")
        self.writer.write(_FRAME.pack(1, len(data)) + data)

    async def drain(self):
        await self.writer.drain()

    def write_eof(self):
        self.writer.write(_FRAME.pack(0, 0))

    def close(self):
        self.writer.close()


class NativeProcess:
    """Process-like async stdin/stdout/stderr. Stop is an awaited broker operation."""

    def __init__(self, client, scope, lease, reader, writer):
        self.client, self.scope, self.lease = client, scope, lease
        self.stdin = _NativeStdin(writer)
        self.stdout, self.stderr = asyncio.StreamReader(limit=FRAME_BYTES), asyncio.StreamReader(limit=FRAME_BYTES)
        self.returncode = None
        self._reader, self._writer = reader, writer
        self._done = asyncio.get_running_loop().create_future()
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                kind, body = await _read_frame(self._reader)
                if kind in {2, 3}:
                    target = self.stdout if kind == 2 else self.stderr
                    target.feed_data(body)
                    # StreamReader has no transport here, so enforce bounded
                    # buffering explicitly while retaining ordinary read methods.
                    while len(target._buffer) >= 2 * FRAME_BYTES:
                        await asyncio.sleep(.01)
                elif kind == 4:
                    self.returncode = int(json.loads(body)["returncode"])
                    break
                else:
                    raise ExecutorError("invalid_stream_frame")
        except (asyncio.IncompleteReadError, OSError, ValueError, ExecutorError):
            self.returncode = -1
        finally:
            if self.returncode is None:
                self.returncode = -1
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._writer.close()
            if not self._done.done():
                self._done.set_result(self.returncode)

    async def wait(self):
        return await asyncio.shield(self._done)

    async def stop(self):
        result = await self.client.stop(self.scope, self.lease)
        self._writer.close()
        self._pump_task.cancel()
        await asyncio.gather(self._pump_task, return_exceptions=True)
        return result


class NativeClient:
    def __init__(self, socket_path=DEFAULT_SOCKET):
        self.socket_path = str(socket_path)

    async def _connect(self, operation, scope, lease=None, **values):
        reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=CONTROL_BYTES)
        request = {"operation": operation, "scope": asdict(scope), **values}
        if lease:
            request["lease"] = asdict(lease)
        data = json.dumps(request).encode() + b"\n"
        if len(data) > CONTROL_BYTES:
            writer.close()
            raise ExecutorError("request_too_large")
        try:
            writer.write(data)
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 90))
            if "error" in response:
                raise ExecutorError(response["error"])
            return response, reader, writer
        except BaseException:
            writer.close()
            raise

    async def _request(self, operation, scope, lease=None, **values):
        response, _, writer = await self._connect(operation, scope, lease, **values)
        writer.close()
        await writer.wait_closed()
        return response

    async def prepare(self, scope):
        return NativeLease(**await self._request("prepare", scope))

    async def install_packages(self,scope,lease,selection):
        value=packages.selection_metadata(selection)
        packages.validate_metadata(value,scope.company_id,scope.owner_id)
        metadata=packages.encoded(value)
        if len(metadata)>packages.METADATA_BYTES:raise ExecutorError('package_metadata_limit')
        writer=None
        try:
            async with asyncio.timeout(packages.UPLOAD_SECONDS):
                _,reader,writer=await self._connect('install_packages',scope,lease)
                await _write_frame(writer,5,metadata)
                for package in selection.packages:
                    for item in package.files:
                        with packages.open_source(package.root,item.path) as stream:
                            remaining=item.size_bytes;hasher=hashlib.sha256()
                            while remaining:
                                data=stream.read(min(remaining,packages.CHUNK_BYTES))
                                if not data:raise ExecutorError('package_source_changed')
                                hasher.update(data);remaining-=len(data)
                                await _write_frame(writer,6,data)
                            if stream.read(1) or hasher.hexdigest()!=item.sha256:raise ExecutorError('package_source_changed')
                await _write_frame(writer,7,b'')
                response=json.loads(await reader.readline())
                if response.get('error'):raise ExecutorError(response['error'])
                if response.get('selection_sha256')!=selection.selection_sha256:raise ExecutorError('package_selection_mismatch')
                return response
        except BaseException:
            if writer:writer.close()
            # Cancellation can arrive after broker acceptance. Drain whole-worker
            # cleanup before allowing the caller to report successful Stop.
            cleanup=asyncio.create_task(self.stop(scope,lease))
            while not cleanup.done():
                try:await asyncio.shield(cleanup)
                except asyncio.CancelledError:continue
            cleanup.result()
            raise
        finally:
            if writer:
                writer.close()
                try:await writer.wait_closed()
                except OSError:pass

    async def start(self, scope, lease, gateway_capability):
        _, reader, writer = await self._connect("start", scope, lease, capability=gateway_capability)
        return NativeProcess(self, scope, lease, reader, writer)

    async def stop(self, scope, lease):
        return await self._request("stop", scope, lease)

    async def snapshot(self, scope, lease, paths=None):
        return await self._request("snapshot", scope, lease, paths=paths)

    async def read_output(self, scope, lease, snapshot_id, index) -> AsyncIterator[bytes]:
        _, reader, writer = await self._connect("read_output", scope, lease, snapshot_id=snapshot_id, index=index)
        try:
            while True:
                kind, body = await _read_frame(reader)
                if kind == 4:
                    break
                if kind != 2:
                    raise ExecutorError("invalid_stream_frame")
                yield body
        finally:
            writer.close()
            await writer.wait_closed()
