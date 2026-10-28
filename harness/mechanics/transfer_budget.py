"""Durable demo egress budget and fail-closed HTTP response middleware.

This is an application cap for the video demo. It does not report provider
traffic or change a hosting-account limit. Reservations are conservative: once
admitted, bytes remain charged even if the client disconnects.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


DAILY_CAP_BYTES = 5_000_000_000
MONTHLY_CAP_BYTES = 50_000_000_000
SCHEMA_VERSION = 1
MAX_RECENT_RESERVATIONS = 512


class TransferBudgetError(RuntimeError):
    """The durable budget cannot safely admit a transfer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TransferBudgetExceeded(TransferBudgetError):
    def __init__(self, period: str):
        super().__init__("transfer_budget_exceeded", f"The demo {period} transfer budget is full.")


class TransferBudget:
    """A small atomic JSON ledger shared by all worker processes."""

    def __init__(self, path: str | os.PathLike[str], *, daily_cap: int = DAILY_CAP_BYTES,
                 monthly_cap: int = MONTHLY_CAP_BYTES):
        if type(daily_cap) is not int or daily_cap <= 0:
            raise ValueError("daily_cap must be a positive integer")
        if type(monthly_cap) is not int or monthly_cap <= 0:
            raise ValueError("monthly_cap must be a positive integer")
        if daily_cap > monthly_cap:
            raise ValueError("daily_cap cannot exceed monthly_cap")
        self.path = Path(path).absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.daily_cap = daily_cap
        self.monthly_cap = monthly_cap

    @staticmethod
    def _periods(now: datetime) -> tuple[str, str]:
        current = now.astimezone(timezone.utc)
        return current.strftime("%Y-%m-%d"), current.strftime("%Y-%m")

    def _blank(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "caps": {"daily_bytes": self.daily_cap, "monthly_bytes": self.monthly_cap},
            "periods": {},
            "recent_reservations": [],
        }

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.lock_path.is_symlink():
                raise TransferBudgetError("ledger_permissions", "The demo transfer lock is unsafe.")
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            lock_stat = os.fstat(fd)
            if lock_stat.st_uid != os.geteuid() or lock_stat.st_mode & 0o077 or not stat.S_ISREG(lock_stat.st_mode):
                os.close(fd)
                raise TransferBudgetError("ledger_permissions", "The demo transfer lock is unsafe.")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r+b", closefd=True) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except TransferBudgetError:
            raise
        except OSError as exc:
            raise TransferBudgetError("ledger_unavailable", "The demo transfer ledger is unavailable.") from exc

    def _read_locked(self) -> dict:
        try:
            if not self.path.is_file():
                raise TransferBudgetError("ledger_missing", "The demo transfer ledger is missing.")
            ledger_stat = self.path.lstat()
            if stat.S_ISLNK(ledger_stat.st_mode) or not stat.S_ISREG(ledger_stat.st_mode):
                raise TransferBudgetError("ledger_permissions", "The demo transfer ledger is unsafe.")
            mode = ledger_stat.st_mode & 0o777
            if ledger_stat.st_uid != os.geteuid() or mode & 0o077:
                raise TransferBudgetError("ledger_permissions", "The demo transfer ledger permissions are unsafe.")
            value = json.loads(self.path.read_text())
        except TransferBudgetError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TransferBudgetError("ledger_corrupt", "The demo transfer ledger is unreadable.") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise TransferBudgetError("ledger_schema", "The demo transfer ledger schema is unsupported.")
        if value.get("caps") != {"daily_bytes": self.daily_cap, "monthly_bytes": self.monthly_cap}:
            raise TransferBudgetError("ledger_caps", "The demo transfer ledger caps do not match configuration.")
        if not isinstance(value.get("periods"), dict) or not isinstance(value.get("recent_reservations"), list):
            raise TransferBudgetError("ledger_shape", "The demo transfer ledger shape is invalid.")
        return value

    def _write_locked(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise TransferBudgetError("ledger_unavailable", "The demo transfer ledger cannot be written.") from exc

    def initialize(self) -> None:
        """Create the ledger once, or validate an existing ledger without resetting it."""
        with self._lock():
            if self.path.exists():
                self._read_locked()
            else:
                self._write_locked(self._blank())

    def reserve(self, byte_count: int, *, now: datetime | None = None, path: str = "",
                method: str = "GET") -> dict:
        """Atomically reserve a response's declared bytes before its headers send."""
        if type(byte_count) is not int or byte_count < 0:
            raise TransferBudgetError("invalid_size", "The response size is invalid.")
        if byte_count > self.daily_cap or byte_count > self.monthly_cap:
            raise TransferBudgetExceeded("single-response")
        now = now or datetime.now(timezone.utc)
        day, month = self._periods(now)
        with self._lock():
            ledger = self._read_locked()
            day_row = ledger["periods"].setdefault(day, {"reserved_bytes": 0, "reservation_count": 0})
            month_row = ledger["periods"].setdefault(month, {"reserved_bytes": 0, "reservation_count": 0})
            if not all(type(row.get("reserved_bytes")) is int and type(row.get("reservation_count")) is int
                       and row["reserved_bytes"] >= 0 and row["reservation_count"] >= 0
                       for row in (day_row, month_row)):
                raise TransferBudgetError("ledger_shape", "The demo transfer ledger usage is invalid.")
            if day_row["reserved_bytes"] + byte_count > self.daily_cap:
                raise TransferBudgetExceeded("daily")
            if month_row["reserved_bytes"] + byte_count > self.monthly_cap:
                raise TransferBudgetExceeded("monthly")
            day_row["reserved_bytes"] += byte_count
            day_row["reservation_count"] += 1
            month_row["reserved_bytes"] += byte_count
            month_row["reservation_count"] += 1
            ledger["recent_reservations"].append({
                "id": uuid.uuid4().hex,
                "at": now.astimezone(timezone.utc).isoformat(),
                "bytes": byte_count,
                "day": day,
                "month": month,
                "method": method[:12],
                "path": path[:240],
                "retained_on_disconnect": True,
            })
            del ledger["recent_reservations"][:-MAX_RECENT_RESERVATIONS]
            self._write_locked(ledger)
            return {"bytes": byte_count, "day": day, "month": month,
                    "daily_reserved_bytes": day_row["reserved_bytes"],
                    "monthly_reserved_bytes": month_row["reserved_bytes"]}

    def status(self, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        day, month = self._periods(now)
        with self._lock():
            ledger = self._read_locked()
            day_row = ledger["periods"].get(day, {"reserved_bytes": 0, "reservation_count": 0})
            month_row = ledger["periods"].get(month, {"reserved_bytes": 0, "reservation_count": 0})
            return {"daily_cap_bytes": self.daily_cap, "monthly_cap_bytes": self.monthly_cap,
                    "daily_reserved_bytes": day_row["reserved_bytes"],
                    "monthly_reserved_bytes": month_row["reserved_bytes"],
                    "day": day, "month": month}


def _headers(message: dict) -> dict[str, str]:
    return {key.decode("latin1").lower(): value.decode("latin1") for key, value in message.get("headers", [])}


class TransferBudgetMiddleware:
    """Reserve eligible outgoing HTTP bodies before ASGI response start."""

    MEDIA_PREFIXES = ("/control-room/", "/clips/", "/downloads/", "/originals/", "/thumbnails/",
                      "/media/", "/assets/", "/api/exports", "/api/files", "/api/attachments")
    MEDIA_TYPES = ("video/", "audio/", "image/", "application/zip", "application/octet-stream")

    def __init__(self, app, *, budget: TransferBudget):
        self.app = app
        self.budget = budget

    @classmethod
    def _eligible(cls, path: str, headers: dict[str, str]) -> bool:
        if path.startswith(cls.MEDIA_PREFIXES):
            return True
        content_type = headers.get("content-type", "").split(";", 1)[0].lower()
        return content_type.startswith(cls.MEDIA_TYPES) or "content-disposition" in headers

    @staticmethod
    async def _deny(send, status: int, code: str) -> None:
        body = json.dumps({"error": code}, separators=(",", ":")).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") == "HEAD":
            return await self.app(scope, receive, send)
        state = {"checked": False, "budgeted": False, "blocked": False, "reserved": 0, "sent": 0}

        async def guarded_send(message):
            if message["type"] == "http.response.start" and not state["checked"]:
                state["checked"] = True
                headers = _headers(message)
                if not self._eligible(scope.get("path", ""), headers):
                    return await send(message)
                status = int(message.get("status", 200))
                if status in {204, 304, 416}:
                    return await send(message)
                raw_length = headers.get("content-length")
                try:
                    byte_count = int(raw_length) if raw_length is not None else None
                except ValueError:
                    byte_count = None
                if byte_count is None or byte_count < 0:
                    state["blocked"] = True
                    return await self._deny(send, 503, "transfer_budget_size_unavailable")
                try:
                    self.budget.reserve(byte_count, path=scope.get("path", ""), method=scope.get("method", "GET"))
                except TransferBudgetExceeded:
                    state["blocked"] = True
                    return await self._deny(send, 509, "transfer_budget_exceeded")
                except TransferBudgetError:
                    state["blocked"] = True
                    return await self._deny(send, 503, "transfer_budget_unavailable")
                state["reserved"] = byte_count
                state["budgeted"] = True
                return await send(message)
            if state["blocked"]:
                return
            if message["type"] == "http.response.body" and state["budgeted"]:
                body = message.get("body", b"")
                remaining = state["reserved"] - state["sent"]
                if len(body) > remaining:
                    if remaining:
                        await send({**message, "body": body[:remaining], "more_body": False})
                    state["blocked"] = True
                    return
                state["sent"] += len(body)
            return await send(message)

        await self.app(scope, receive, guarded_send)
