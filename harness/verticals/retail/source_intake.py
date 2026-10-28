"""Private, resumable source acquisition and durable worker jobs.

This is an internal foundation. It exposes neither HTTP routes nor a media decoder,
remote connector, GPU runner or searchable-index publisher. Callers derive company
state and IntakeActor from authenticated host context; worker leases are host-only.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid


class IntakeError(ValueError):
    """Stable error codes; private bytes and host paths never enter exceptions."""


def _text(value, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum or any(ord(c) < 32 for c in value):
        raise IntakeError("invalid_metadata")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise IntakeError("invalid_checksum")
    return value


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise IntakeError("invalid_size_or_offset")
    return value


@dataclass(frozen=True)
class IntakeActor:
    principal_id: str
    read_only: bool = False

    def __post_init__(self):
        _text(self.principal_id)
        if type(self.read_only) is not bool:
            raise IntakeError("invalid_actor")

    def require_write(self):
        if self.read_only:
            raise IntakeError("read_only_workspace")


@dataclass(frozen=True)
class IntakeLimits:
    max_file_bytes: int = 256 * 1024 * 1024
    max_storage_bytes: int = 2 * 1024 * 1024 * 1024
    max_chunk_bytes: int = 8 * 1024 * 1024
    max_chunks_per_upload: int = 4096
    max_pending_uploads: int = 4
    max_sources: int = 100
    max_assets: int = 10000
    upload_ttl_seconds: int = 24 * 3600

    def __post_init__(self):
        for value in self.__dict__.values():
            if type(value) is not int or value < 1:
                raise IntakeError("invalid_intake_limits")
        if self.max_chunk_bytes > self.max_file_bytes or self.max_file_bytes > self.max_storage_bytes:
            raise IntakeError("invalid_intake_limits")


@dataclass(frozen=True)
class JobLease:
    job_id: str
    asset_id: str
    stage: str
    generation: int
    token: str
    expires_at: float

    def __post_init__(self):
        if (not isinstance(self.job_id, str) or not re.fullmatch(r"job-[a-f0-9]{32}", self.job_id)
                or not isinstance(self.asset_id, str) or not re.fullmatch(r"asset-[a-f0-9]{32}", self.asset_id)
                or self.stage not in STAGES or type(self.generation) is not int or self.generation < 1
                or not isinstance(self.token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.token)
                or not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at)):
            raise IntakeError("invalid_job_lease")


KINDS = {"upload", "sftp", "ftps", "ftp", "object_store", "nas", "shared_link"}
STAGES = ("prepare", "embed", "publish")
ACTIVE_UPLOADS = ("uploading", "sealing", "needs_attention")


class SourceStore:
    def __init__(self, company_state, *, limits=None, clock=time.time):
        self.root = Path(company_state) / "source-intake"
        self.limits, self.clock = limits or IntakeLimits(), clock
        self._directory(self.root)
        self.quarantine, self.blobs = self.root / "quarantine", self.root / "originals"
        self._directory(self.quarantine)
        self._directory(self.blobs)
        self.database = self.root / "intake.sqlite3"
        fd = self._open(self.database, os.O_WRONLY | os.O_CREAT, mode=0o600)
        os.close(fd)
        with self._read() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, collection TEXT NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS source_objects (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                    object_key TEXT NOT NULL, source_version TEXT NOT NULL,
                    UNIQUE(source_id, object_key, source_version));
                CREATE TABLE IF NOT EXISTS upload_source_defaults (
                    owner TEXT NOT NULL, collection TEXT NOT NULL,
                    source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),
                    PRIMARY KEY(owner,collection));
                CREATE TABLE IF NOT EXISTS connection_sources (
                    owner TEXT NOT NULL, collection TEXT NOT NULL, connection_id TEXT NOT NULL,
                    target_sha256 TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),
                    PRIMARY KEY(owner,collection,connection_id,target_sha256));
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id),
                    object_id TEXT NOT NULL UNIQUE REFERENCES source_objects(id), request_key TEXT NOT NULL,
                    filename TEXT NOT NULL, expected_bytes INTEGER NOT NULL, committed_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL, sha256 TEXT, asset_id TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, error TEXT,
                    UNIQUE(owner, request_key));
                CREATE TABLE IF NOT EXISTS upload_chunks (
                    upload_id TEXT NOT NULL REFERENCES uploads(id), offset INTEGER NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(upload_id, offset));
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id),
                    object_id TEXT NOT NULL UNIQUE REFERENCES source_objects(id),
                    sha256 TEXT NOT NULL REFERENCES blobs(sha256), filename TEXT NOT NULL,
                    bytes INTEGER NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), stage TEXT NOT NULL,
                    status TEXT NOT NULL, generation INTEGER NOT NULL, worker TEXT, token_hash TEXT,
                    lease_expires REAL, result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(asset_id,stage));
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), generation INTEGER NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created);
            """)

    @staticmethod
    def _directory(path):
        if path.is_symlink():
            raise IntakeError("invalid_storage")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise IntakeError("invalid_storage")
        path.chmod(0o700)

    @staticmethod
    def _open(path, flags, *, mode=0o600):
        try:
            fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                os.close(fd)
                raise IntakeError("invalid_storage_file")
            return fd
        except OSError:
            raise IntakeError("storage_unavailable") from None

    def _connect(self):
        # Parent directories are private and never mounted into generated code.
        fd = self._open(self.database, os.O_RDONLY)
        os.close(fd)
        db = None
        try:
            db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.Error:
            if db is not None:
                db.close()
            raise IntakeError("storage_unavailable") from None

    @contextmanager
    def _write(self, current=None):
        lock = self._open(self.root / "intake.lock", os.O_WRONLY | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                if current is not None and current() is not True:raise IntakeError("acquisition_stopped")
                yield db
                if current is not None and current() is not True:raise IntakeError("acquisition_stopped")
                db.execute("COMMIT")
            except (OSError, sqlite3.Error):
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise IntakeError("storage_unavailable") from None
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()
        finally:
            os.close(lock)

    @contextmanager
    def _read(self):
        db = self._connect()
        try:
            yield db
        except (OSError, sqlite3.Error):
            raise IntakeError("storage_unavailable") from None
        finally:
            db.close()

    @staticmethod
    def _writer(actor):
        if not isinstance(actor, IntakeActor):
            raise IntakeError("invalid_actor")
        actor.require_write()

    @staticmethod
    def _owned(db, table, identity, actor):
        if not isinstance(actor, IntakeActor):
            raise IntakeError("invalid_actor")
        row = db.execute(f"SELECT * FROM {table} WHERE id=? AND owner=?", (identity, actor.principal_id)).fetchone()
        if row is None:
            raise IntakeError("record_not_found")
        return row

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _public_upload(row):
        return {key: row[key] for key in ("id", "source_id", "object_id", "filename", "expected_bytes",
                "committed_bytes", "status", "sha256", "asset_id", "created", "expires", "error")}

    @staticmethod
    def _public_job(row):
        return {key: row[key] for key in ("id", "asset_id", "stage", "status", "generation", "error", "created", "updated")}

    def create_source(self, actor, *, collection, name, kind="upload"):
        self._writer(actor)
        _text(collection); _text(name, 200)
        if kind not in KINDS:
            raise IntakeError("invalid_source_kind")
        identity = "source-" + uuid.uuid4().hex
        with self._write() as db:
            if db.execute("SELECT count(*) FROM sources").fetchone()[0] >= self.limits.max_sources:
                raise IntakeError("source_limit")
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)", (identity, actor.principal_id, collection, name, kind, self.clock()))
        return self.get_source(actor, identity)

    def lookup_connection_source(self, actor, *, collection, connection_id, target_sha256):
        if not isinstance(actor,IntakeActor):raise IntakeError("invalid_actor")
        _text(collection);_text(connection_id);_sha(target_sha256)
        with self._read() as db:
            row=db.execute("SELECT source_id FROM connection_sources WHERE owner=? AND collection=? AND connection_id=? AND target_sha256=?",
                (actor.principal_id,collection,connection_id,target_sha256)).fetchone()
        return self.get_source(actor,row[0]) if row else None

    def ensure_connection_source(self, actor, *, collection, connection_id, target_sha256, name, kind="sftp", current=None):
        """Host connection identity maps retries to one owner-scoped source."""
        self._writer(actor);_text(collection);_text(connection_id);_sha(target_sha256);_text(name,200)
        if kind not in KINDS-{"upload"}:raise IntakeError("invalid_source_kind")
        with self._write(current) as db:
            row=db.execute("SELECT source_id FROM connection_sources WHERE owner=? AND collection=? AND connection_id=? AND target_sha256=?",
                (actor.principal_id,collection,connection_id,target_sha256)).fetchone()
            if row:
                source=self._owned(db,"sources",row[0],actor)
                if source["kind"]!=kind:raise IntakeError("source_kind_changed")
                identity=row[0]
            else:
                if db.execute("SELECT count(*) FROM sources").fetchone()[0]>=self.limits.max_sources:raise IntakeError("source_limit")
                identity="source-"+uuid.uuid4().hex
                db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)",(identity,actor.principal_id,collection,name,kind,self.clock()))
                db.execute("INSERT INTO connection_sources VALUES (?,?,?,?,?)",(actor.principal_id,collection,connection_id,target_sha256,identity))
        return self.get_source(actor,identity)

    def get_source(self, actor, source_id):
        with self._read() as db:
            value = dict(self._owned(db, "sources", source_id, actor))
            value.pop("owner")
            value["status"] = "configured"
            return value

    def ensure_upload_source(self, actor, *, collection):
        """Host-validated collection; one durable default per uploader/collection."""
        self._writer(actor); _text(collection)
        with self._write() as db:
            row = db.execute("SELECT source_id FROM upload_source_defaults WHERE owner=? AND collection=?",
                             (actor.principal_id, collection)).fetchone()
            if row:
                identity = row["source_id"]
                source = self._owned(db, "sources", identity, actor)
                if source["kind"] != "upload" or source["collection"] != collection:
                    raise IntakeError("invalid_default_source")
            else:
                if db.execute("SELECT count(*) FROM sources").fetchone()[0] >= self.limits.max_sources:
                    raise IntakeError("source_limit")
                identity = "source-" + uuid.uuid4().hex
                db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?)",
                           (identity, actor.principal_id, collection, "Uploads", "upload", self.clock()))
                db.execute("INSERT INTO upload_source_defaults VALUES (?,?,?)",
                           (actor.principal_id, collection, identity))
        return self.get_source(actor, identity)

    def list_sources(self, actor, *, collection=None):
        if not isinstance(actor, IntakeActor):
            raise IntakeError("invalid_actor")
        if collection is not None: _text(collection)
        with self._read() as db:
            values = [dict(row) for row in db.execute(
                "SELECT * FROM sources WHERE owner=? AND (? IS NULL OR collection=?) ORDER BY created,id",
                (actor.principal_id, collection, collection))]
        for value in values:
            value.pop("owner"); value["status"] = "configured"
        return values

    @classmethod
    def _import_view(cls, db, row):
        source = dict(db.execute("SELECT * FROM sources WHERE id=?", (row["source_id"],)).fetchone())
        source.pop("owner"); source["status"] = "configured"
        asset = db.execute("SELECT * FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
        if asset is not None:
            asset = dict(asset); asset.pop("owner")
            asset["jobs"] = [cls._public_job(job) for job in db.execute(
                "SELECT * FROM jobs WHERE asset_id=? ORDER BY created,id", (row["asset_id"],))]
        return {"upload": cls._public_upload(row), "source": source, "asset": asset}

    def get_import(self, actor, upload_id):
        with self._read() as db:
            db.execute("BEGIN")
            return self._import_view(db, self._owned(db, "uploads", upload_id, actor))

    def list_imports(self, actor, *, collection=None, limit=100, offset=0):
        if not isinstance(actor, IntakeActor):
            raise IntakeError("invalid_actor")
        if collection is not None: _text(collection)
        _integer(limit, 1, 200); _integer(offset, 0, self.limits.max_assets)
        with self._read() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT u.* FROM uploads u JOIN sources s ON s.id=u.source_id "
                "WHERE u.owner=? AND (? IS NULL OR s.collection=?) ORDER BY u.created DESC,u.id DESC LIMIT ? OFFSET ?",
                (actor.principal_id, collection, collection, limit, offset)).fetchall()
            return [self._import_view(db, row) for row in rows]

    def cancel_import(self, actor, upload_id):
        """Fence acquisition and every unfinished stage in one transaction.

        Running or uncertain work still needs host reconciliation before it can
        report stopped. This changes durable admission, never claims a provider
        process was interrupted merely because a browser requested cancellation.
        """
        self._writer(actor)
        with self._write() as db:
            row = self._owned(db, "uploads", upload_id, actor)
            if row["status"] == "cancelled":
                return self._import_view(db, row)
            if row["status"] in {"uploading", "needs_attention"} and row["sha256"] is None:
                (self.quarantine / upload_id).unlink(missing_ok=True)
                self._sync_directory(self.quarantine)
                db.execute("UPDATE uploads SET status='cancelled',error=NULL WHERE id=?", (upload_id,))
                db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (upload_id,))
                return self._import_view(db, self._owned(db, "uploads", upload_id, actor))
            if row["status"] != "sealed":
                raise IntakeError("upload_not_open")
            asset = self._owned(db, "assets", row["asset_id"], actor)
            if asset["status"] == "ready":
                raise IntakeError("import_finished")
            unresolved = False
            for job in db.execute("SELECT * FROM jobs WHERE asset_id=?", (row["asset_id"],)).fetchall():
                if job["status"] in {"succeeded", "cancelled"}: continue
                uncertain = job["status"] in {"running", "needs_reconciliation"}
                unresolved = unresolved or uncertain
                if job["status"] == "needs_reconciliation" and job["error"] == "cancel_requested": continue
                status = "needs_reconciliation" if uncertain else "cancelled"
                db.execute("UPDATE jobs SET status=?,generation=generation+1,token_hash=NULL,lease_expires=NULL,"
                           "error='cancel_requested',updated=? WHERE id=?", (status, self.clock(), job["id"]))
                self._event(db, job, "cancel_requested")
            db.execute("UPDATE assets SET status=? WHERE id=?",
                       ("needs_attention" if unresolved else "cancelled", row["asset_id"]))
            return self._import_view(db, self._owned(db, "uploads", upload_id, actor))

    def _quota(self, db):
        stored = db.execute("SELECT coalesce(sum(bytes),0) FROM blobs").fetchone()[0]
        reserved = db.execute("SELECT coalesce(sum(expected_bytes),0) FROM uploads WHERE status IN ('uploading','sealing','needs_attention')").fetchone()[0]
        return stored, reserved

    def begin_upload(self, actor, source_id, *, filename, size, idempotency_key, object_key=None, source_version=None, current=None):
        self._writer(actor)
        _text(filename, 255); _text(idempotency_key)
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise IntakeError("invalid_filename")
        _integer(size, 1, self.limits.max_file_bytes)
        if (object_key is None) != (source_version is None):
            raise IntakeError("source_version_required")
        if object_key is not None:
            _text(object_key, 2048); _text(source_version, 512)
        identity = "upload-" + uuid.uuid4().hex
        with self._write(current) as db:
            self._owned(db, "sources", source_id, actor)
            prior = db.execute("SELECT * FROM uploads WHERE owner=? AND request_key=?", (actor.principal_id, idempotency_key)).fetchone()
            remote = db.execute("SELECT u.* FROM uploads u JOIN source_objects o ON u.object_id=o.id WHERE o.source_id=? AND o.object_key=? AND o.source_version=?",
                                (source_id, object_key, source_version)).fetchone() if object_key is not None else None
            if prior or remote:
                row = prior or remote
                obj = db.execute("SELECT * FROM source_objects WHERE id=?", (row["object_id"],)).fetchone()
                if (row["source_id"] != source_id or row["filename"] != filename or row["expected_bytes"] != size
                        or (object_key is not None and (obj["object_key"], obj["source_version"]) != (object_key, source_version))):
                    raise IntakeError("idempotency_conflict")
                return self._public_upload(row)
            active = db.execute("SELECT count(*) FROM uploads WHERE status IN ('uploading','sealing','needs_attention')").fetchone()[0]
            if active >= self.limits.max_pending_uploads:
                raise IntakeError("upload_capacity")
            if db.execute("SELECT count(*) FROM uploads").fetchone()[0] >= self.limits.max_assets:
                raise IntakeError("asset_limit")
            if sum(self._quota(db)) + size > self.limits.max_storage_bytes:
                raise IntakeError("storage_quota")
            obj, asset = "object-" + uuid.uuid4().hex, "asset-" + uuid.uuid4().hex
            db.execute("INSERT INTO source_objects VALUES (?,?,?,?)", (obj, source_id, object_key or identity, source_version or identity))
            db.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?,0,'uploading',NULL,?,?,?,NULL)",
                       (identity, actor.principal_id, source_id, obj, idempotency_key, filename, size, asset,
                        self.clock(), self.clock() + self.limits.upload_ttl_seconds))
            fd = self._open(self.quarantine / identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._sync_directory(self.quarantine)
            return self._public_upload(self._owned(db, "uploads", identity, actor))

    def get_upload(self, actor, upload_id):
        with self._read() as db:
            return self._public_upload(self._owned(db, "uploads", upload_id, actor))

    def _upload_file(self, row):
        fd = self._open(self.quarantine / row["id"], os.O_RDWR)
        info = os.fstat(fd)
        if info.st_size < row["committed_bytes"]:
            os.close(fd)
            raise IntakeError("committed_bytes_missing")
        if info.st_size > row["committed_bytes"]:
            os.ftruncate(fd, row["committed_bytes"])
            os.fsync(fd)
        return fd

    def append_chunk(self, actor, upload_id, *, offset, data, sha256=None, current=None):
        if not isinstance(data, (bytes, bytearray)):
            raise IntakeError("invalid_chunk")
        return self.append_stream(actor, upload_id, offset=offset, size=len(data), stream=io.BytesIO(data), sha256=sha256, current=current)

    def append_stream(self, actor, upload_id, *, offset, size, stream, sha256=None, current=None):
        """Read one bounded chunk, with at most 64 KiB in the copy buffer.

        The HTTP/remote adapter owns transport deadlines before calling this
        synchronous API; do not pass an unbounded blocking network stream.
        """
        self._writer(actor)
        _integer(offset, 0, self.limits.max_file_bytes)
        _integer(size, 1, self.limits.max_chunk_bytes)
        if sha256 is not None:
            _sha(sha256)
        with self._write(current) as db:
            row = self._owned(db, "uploads", upload_id, actor)
            if row["status"] != "uploading" or row["expires"] <= self.clock():
                raise IntakeError("upload_not_open")
            if offset + size > row["expected_bytes"]:
                raise IntakeError("upload_size_exceeded")
            previous = db.execute("SELECT * FROM upload_chunks WHERE upload_id=? AND offset=?", (upload_id, offset)).fetchone()
            if offset != row["committed_bytes"] and not previous:
                raise IntakeError("offset_mismatch")
            if previous and previous["size"] != size:
                raise IntakeError("chunk_conflict")
            if not previous and db.execute("SELECT count(*) FROM upload_chunks WHERE upload_id=?", (upload_id,)).fetchone()[0] >= self.limits.max_chunks_per_upload:
                raise IntakeError("upload_chunk_limit")
            fd = self._upload_file(row)
            digest = hashlib.sha256()
            received = 0
            try:
                if not previous:
                    os.lseek(fd, offset, os.SEEK_SET)
                while received < size:
                    data = stream.read(min(64 * 1024, size - received))
                    if not isinstance(data, bytes) or not data or len(data) > min(64 * 1024, size - received):
                        raise IntakeError("chunk_length_mismatch")
                    digest.update(data)
                    received += len(data)
                    if not previous:
                        view = memoryview(data)
                        while view:
                            count = os.write(fd, view)
                            if count <= 0:
                                raise IntakeError("storage_unavailable")
                            view = view[count:]
                if stream.read(1):
                    raise IntakeError("chunk_length_mismatch")
                checksum = digest.hexdigest()
                if sha256 is not None and checksum != sha256:
                    raise IntakeError("checksum_mismatch")
                if previous:
                    if checksum != previous["sha256"]:
                        raise IntakeError("chunk_conflict")
                    return self._public_upload(row)
                os.fsync(fd)
                db.execute("INSERT INTO upload_chunks VALUES (?,?,?,?)", (upload_id, offset, size, checksum))
                db.execute("UPDATE uploads SET committed_bytes=? WHERE id=?", (offset + size, upload_id))
                return self._public_upload(self._owned(db, "uploads", upload_id, actor))
            finally:
                os.close(fd)

    def seal_upload(self, actor, upload_id, *, expected_sha256=None, current=None):
        self._writer(actor)
        if expected_sha256 is not None:
            _sha(expected_sha256)
        # Persist a sealing intent before moving bytes. Recovery can identify a
        # promoted blob even when the process crashes before the asset transaction.
        with self._write(current) as db:
            row = self._owned(db, "uploads", upload_id, actor)
            if row["status"] not in {"uploading", "sealing", "sealed"}:
                raise IntakeError("upload_not_open")
            if row["status"] == "uploading":
                if row["expires"] <= self.clock():
                    raise IntakeError("upload_not_open")
                if row["committed_bytes"] != row["expected_bytes"]:
                    raise IntakeError("upload_incomplete")
                with os.fdopen(self._upload_file(row), "rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                if expected_sha256 is not None and checksum != expected_sha256:
                    raise IntakeError("checksum_mismatch")
                db.execute("UPDATE uploads SET status='sealing',sha256=? WHERE id=?", (checksum, upload_id))
            elif expected_sha256 is not None and row["sha256"] != expected_sha256:
                raise IntakeError("checksum_mismatch")
        with self._write() as db:
            row = self._owned(db, "uploads", upload_id, actor)
            self._finish_seal(db, row)
            return self._public_upload(self._owned(db, "uploads", upload_id, actor))

    def _verify_blob(self, path, size, checksum):
        with os.fdopen(self._open(path, os.O_RDONLY), "rb") as stream:
            if os.fstat(stream.fileno()).st_size != size or hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise IntakeError("original_integrity_failed")

    def _finish_seal(self, db, row):
        if row["status"] == "sealed":
            return
        if row["status"] != "sealing":
            raise IntakeError("upload_not_open")
        checksum = _sha(row["sha256"])
        partial, original = self.quarantine / row["id"], self.blobs / checksum
        if original.exists() or original.is_symlink():
            self._verify_blob(original, row["expected_bytes"], checksum)
            if partial.exists():
                self._verify_blob(partial, row["expected_bytes"], checksum)
                partial.unlink()
        else:
            self._verify_blob(partial, row["expected_bytes"], checksum)
            partial.chmod(0o400)
            os.rename(partial, original)
        original.chmod(0o400)
        self._sync_directory(self.blobs)
        self._sync_directory(self.quarantine)
        db.execute("INSERT OR IGNORE INTO blobs VALUES (?,?,?)", (checksum, row["expected_bytes"], self.clock()))
        db.execute("INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?,?,?,'received',?)",
                   (row["asset_id"], row["owner"], row["source_id"], row["object_id"], checksum,
                    row["filename"], row["expected_bytes"], self.clock()))
        self._queue_job(db, row["asset_id"], "prepare")
        db.execute("UPDATE uploads SET status='sealed',error=NULL WHERE id=?", (row["id"],))
        db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (row["id"],))

    def cancel_upload(self, actor, upload_id):
        self._writer(actor)
        with self._write() as db:
            row = self._owned(db, "uploads", upload_id, actor)
            if row["status"] == "cancelled":
                return self._public_upload(row)
            if row["status"] not in {"uploading", "needs_attention"} or row["sha256"] is not None:
                raise IntakeError("source_received_cancel_job_instead")
            (self.quarantine / upload_id).unlink(missing_ok=True)
            self._sync_directory(self.quarantine)
            db.execute("UPDATE uploads SET status='cancelled',error=NULL WHERE id=?", (upload_id,))
            db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (upload_id,))
            return self._public_upload(self._owned(db, "uploads", upload_id, actor))

    def get_asset(self, actor, asset_id):
        with self._read() as db:
            value = dict(self._owned(db, "assets", asset_id, actor))
            value.pop("owner")
            value["jobs"] = [self._public_job(row) for row in db.execute("SELECT * FROM jobs WHERE asset_id=? ORDER BY created", (asset_id,))]
            return value

    def list_uploads(self, actor, source_id):
        with self._read() as db:
            self._owned(db, "sources", source_id, actor)
            return [self._public_upload(row) for row in db.execute("SELECT * FROM uploads WHERE source_id=? ORDER BY created,id", (source_id,))]

    def _queue_job(self, db, asset_id, stage):
        identity = "job-" + uuid.uuid4().hex
        db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?,'queued',0,NULL,NULL,NULL,NULL,NULL,?,?)",
                   (identity, asset_id, stage, self.clock(), self.clock()))

    @staticmethod
    def _result(value):
        if not isinstance(value, dict):
            raise IntakeError("invalid_job_result")
        try:
            encoded = json.dumps(value, allow_nan=False, sort_keys=True)
        except (ValueError, TypeError):
            raise IntakeError("invalid_job_result") from None
        if len(encoded.encode()) > 64 * 1024:
            raise IntakeError("job_result_limit")
        return encoded

    def _event(self, db, row, kind, body=None):
        db.execute("INSERT INTO job_events(job_id,generation,kind,body,created) VALUES (?,?,?,?,?)",
                   (row["id"], row["generation"], kind, self._result(body or {}), self.clock()))

    def _expire_jobs(self, db):
        for row in db.execute("SELECT * FROM jobs WHERE status='running' AND lease_expires<=?", (self.clock(),)).fetchall():
            self._event(db, row, "lease_expired")
            db.execute("UPDATE jobs SET status='needs_reconciliation',generation=generation+1,token_hash=NULL,lease_expires=NULL,updated=? WHERE id=?",
                       (self.clock(), row["id"]))
            db.execute("UPDATE assets SET status='needs_attention' WHERE id=?", (row["asset_id"],))

    def claim_job(self, *, worker_id, lease_seconds=60, stages=STAGES, asset_id=None):
        """Host worker only. Expired attempts require explicit reconciliation."""
        _text(worker_id)
        if asset_id is not None and (not isinstance(asset_id,str) or not re.fullmatch(r"asset-[a-f0-9]{32}",asset_id)):
            raise IntakeError("invalid_asset_id")
        _integer(lease_seconds, 1, 900)
        if not stages or any(stage not in STAGES for stage in stages):
            raise IntakeError("invalid_job_stage")
        with self._write() as db:
            self._expire_jobs(db)
            marks = ",".join("?" for _ in stages)
            row = db.execute(f"SELECT * FROM jobs WHERE status='queued' AND stage IN ({marks}) AND (? IS NULL OR asset_id=?) ORDER BY created,id LIMIT 1", (*stages,asset_id,asset_id)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            generation, expires = row["generation"] + 1, self.clock() + lease_seconds
            db.execute("UPDATE jobs SET status='running',generation=?,worker=?,token_hash=?,lease_expires=?,updated=? WHERE id=?",
                       (generation, worker_id, hashlib.sha256(token.encode()).hexdigest(), expires, self.clock(), row["id"]))
            db.execute("UPDATE assets SET status=? WHERE id=?", ("preparing" if row["stage"] == "prepare" else "indexing", row["asset_id"]))
            return JobLease(row["id"], row["asset_id"], row["stage"], generation, token, expires)

    def _leased(self, db, lease):
        if not isinstance(lease, JobLease):
            raise IntakeError("invalid_job_lease")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (lease.job_id,)).fetchone()
        if (row is None or row["status"] != "running" or row["generation"] != lease.generation
                or row["asset_id"] != lease.asset_id or row["stage"] != lease.stage
                or row["lease_expires"] <= self.clock()
                or not secrets.compare_digest(row["token_hash"], hashlib.sha256(lease.token.encode()).hexdigest())):
            raise IntakeError("stale_job_lease")
        return row

    def heartbeat_job(self, lease, *, lease_seconds=60):
        _integer(lease_seconds, 1, 900)
        with self._write() as db:
            self._leased(db, lease)
            expires = self.clock() + lease_seconds
            db.execute("UPDATE jobs SET lease_expires=?,updated=? WHERE id=?", (expires, self.clock(), lease.job_id))
            return JobLease(lease.job_id, lease.asset_id, lease.stage, lease.generation, lease.token, expires)

    def job_context(self, lease):
        """Private worker input, including durable evidence from earlier stages.

        Callers must keep this response off browser and model routes. Workers
        receive originals through open_job_original, not a mutable host path.
        """
        with self._write() as db:
            self._leased(db, lease)
            asset = dict(db.execute("SELECT * FROM assets WHERE id=?", (lease.asset_id,)).fetchone())
            source = dict(db.execute("SELECT * FROM sources WHERE id=?", (asset["source_id"],)).fetchone())
            occurrence = dict(db.execute("SELECT * FROM source_objects WHERE id=?", (asset["object_id"],)).fetchone())
            previous = {row["stage"]: json.loads(row["result"]) for row in db.execute(
                "SELECT stage,result FROM jobs WHERE asset_id=? AND status='succeeded'", (lease.asset_id,))}
            checkpoint = db.execute("SELECT result FROM jobs WHERE id=?", (lease.job_id,)).fetchone()[0]
            return {"asset": asset, "source": source, "occurrence": occurrence, "completed_stages": previous,
                    "checkpoint": json.loads(checkpoint) if checkpoint else None}

    def checkpoint_job(self, lease, *, checkpoint):
        """Private host attempt identity before work; never a completion receipt."""
        encoded = self._result(checkpoint)
        with self._write() as db:
            self._leased(db, lease)
            db.execute("UPDATE jobs SET result=?,updated=? WHERE id=?", (encoded, self.clock(), lease.job_id))

    @contextmanager
    def open_job_original(self, lease):
        """Host worker gets a verified read-only descriptor, never a public host path."""
        with self._write() as db:
            self._leased(db, lease)
            asset = db.execute("SELECT * FROM assets WHERE id=?", (lease.asset_id,)).fetchone()
            path = self.blobs / _sha(asset["sha256"])
            self._verify_blob(path, asset["bytes"], asset["sha256"])
            stream = os.fdopen(self._open(path, os.O_RDONLY), "rb")
        try:
            yield stream
        finally:
            stream.close()

    def finish_job(self, lease, *, result):
        """Result is trusted worker evidence, not model/browser-supplied metadata.

        Publish completion requires the worker's verified committed generation.
        The caller implements actual validation/Qdrant publication before this API.
        """
        with self._write() as db:
            row = self._leased(db, lease)
            self._finish_job_locked(db, row, result)

    def _finish_job_locked(self, db, row, result):
        encoded = self._result(result)
        if row["stage"] == "publish" and (not isinstance(result.get("generation_id"), str) or not result["generation_id"]):
            raise IntakeError("published_generation_required")
        db.execute("UPDATE jobs SET status='succeeded',token_hash=NULL,lease_expires=NULL,result=?,updated=? WHERE id=?",
                   (encoded, self.clock(), row["id"]))
        self._event(db, row, "succeeded", result)
        position = STAGES.index(row["stage"])
        if position + 1 < len(STAGES):
            self._queue_job(db, row["asset_id"], STAGES[position + 1])
        else:
            db.execute("UPDATE assets SET status='ready' WHERE id=?", (row["asset_id"],))

    def finish_publication(self, lease, *, publish):
        """Fence bounded local publication and its Ready receipt together.

        Lock order: SourceStore -> Cosmos publication -> Qdrant publication.
        publish must never acquire SourceStore again or call a provider/decoder.
        It may commit external local-index effects before raising. The caller then
        marks this attempt uncertain and reconciles its exact immutable evidence.
        The host must bound Qdrant request time and total publication work.
        """
        if not isinstance(lease, JobLease) or lease.stage != "publish" or not callable(publish):
            raise IntakeError("invalid_publication")
        with self._write() as db:
            row = self._leased(db, lease)
            result = publish()
            # Cancellation and generation changes cannot enter this transaction.
            # Confirm its deadline too: an overlong external commit needs explicit
            # reconciliation, even when the local alias has already advanced.
            self._leased(db, lease)
            self._finish_job_locked(db, row, result)
            return result

    def fail_job(self, lease, *, error_code, uncertain=False):
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", error_code) or type(uncertain) is not bool:
            raise IntakeError("invalid_job_failure")
        with self._write() as db:
            row = self._leased(db, lease)
            status = "needs_reconciliation" if uncertain else "failed"
            db.execute("UPDATE jobs SET status=?,generation=generation+1,token_hash=NULL,lease_expires=NULL,error=?,updated=? WHERE id=?",
                       (status, error_code, self.clock(), lease.job_id))
            db.execute("UPDATE assets SET status='needs_attention' WHERE id=?", (row["asset_id"],))
            self._event(db, row, status, {"error": error_code})

    def cancel_job(self, actor, job_id):
        self._writer(actor)
        with self._write() as db:
            row = db.execute("SELECT j.* FROM jobs j JOIN assets a ON j.asset_id=a.id WHERE j.id=? AND a.owner=?", (job_id, actor.principal_id)).fetchone()
            if row is None:
                raise IntakeError("record_not_found")
            if row["status"] == "cancelled" or (row["status"] == "needs_reconciliation" and row["error"] == "cancel_requested"):
                return self._public_job(row)
            if row["status"] == "succeeded":
                raise IntakeError("job_already_completed")
            # Cancellation revokes publication authority. It does not claim a
            # remote process has stopped or release any provider reservation.
            self._event(db, row, "cancel_requested")
            status = "needs_reconciliation" if row["status"] in {"running", "needs_reconciliation"} else "cancelled"
            db.execute("UPDATE jobs SET status=?,generation=generation+1,token_hash=NULL,lease_expires=NULL,error='cancel_requested',updated=? WHERE id=?",
                       (status, self.clock(), job_id))
            db.execute("UPDATE assets SET status=? WHERE id=?", ("needs_attention" if status == "needs_reconciliation" else "cancelled", row["asset_id"]))
            return self._public_job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def reconcile_job(self, job_id, *, expected_generation, outcome, evidence_reference):
        """Trusted worker/operator settles uncertain work after actual stop/billing checks.

        Evidence references are durable record IDs. This method never verifies
        provider billing or grants itself authority to launch a paid retry.
        """
        _text(evidence_reference, 512)
        _integer(expected_generation, 0, 2**63-1)
        if outcome not in {"retry", "failed", "cancelled"}:
            raise IntakeError("invalid_reconciliation")
        with self._write() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["generation"] != expected_generation or row["status"] not in {"needs_reconciliation", "failed"}:
                raise IntakeError("stale_job_generation")
            if row["error"] == "cancel_requested" and outcome == "retry":
                raise IntakeError("cancelled_work_requires_new_intent")
            self._event(db, row, "reconciled", {"outcome": outcome, "evidence_reference": evidence_reference})
            status = "queued" if outcome == "retry" else outcome
            db.execute("UPDATE jobs SET status=?,generation=generation+1,error=NULL,updated=? WHERE id=?", (status, self.clock(), job_id))
            db.execute("UPDATE assets SET status=? WHERE id=?", ("received" if outcome == "retry" else ("cancelled" if outcome == "cancelled" else "needs_attention"), row["asset_id"]))

    def recover(self):
        """Reconcile local transfer crashes; uncertain running jobs remain fenced."""
        report = {"resumed_uploads": 0, "sealed_uploads": 0, "expired_uploads": 0, "needs_attention": 0}
        with self._write() as db:
            for row in db.execute("SELECT * FROM uploads WHERE status IN ('uploading','sealing') OR (status='needs_attention' AND sha256 IS NOT NULL)").fetchall():
                db.execute("SAVEPOINT recover_upload")
                try:
                    if row["sha256"] is not None:
                        db.execute("UPDATE uploads SET status='sealing' WHERE id=?", (row["id"],))
                        self._finish_seal(db, {**dict(row), "status": "sealing"})
                        report["sealed_uploads"] += 1
                    elif row["expires"] <= self.clock():
                        (self.quarantine / row["id"]).unlink(missing_ok=True)
                        db.execute("UPDATE uploads SET status='cancelled',error='upload_expired' WHERE id=?", (row["id"],))
                        db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (row["id"],))
                        report["expired_uploads"] += 1
                    else:
                        os.close(self._upload_file(row))
                        report["resumed_uploads"] += 1
                except (IntakeError, OSError):
                    db.execute("ROLLBACK TO recover_upload")
                    db.execute("UPDATE uploads SET status='needs_attention',error='local_recovery_required' WHERE id=?", (row["id"],))
                    report["needs_attention"] += 1
                finally:
                    db.execute("RELEASE recover_upload")
            # An uncommitted begin_upload can leave only an empty private file.
            known = {row[0] for row in db.execute("SELECT id FROM uploads WHERE status IN ('uploading','sealing','needs_attention')")}
            for path in self.quarantine.iterdir():
                if re.fullmatch(r"upload-[a-f0-9]{32}", path.name) and path.name not in known:
                    path.unlink()
            self._sync_directory(self.quarantine)
            self._expire_jobs(db)
        return report
