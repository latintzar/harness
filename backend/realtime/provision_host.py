"""Host command: import one saved video company identity through the owner migration role.

The dry run is the default. It runs the complete import inside the owner transaction and rolls
it back, so collisions, drift and the role check answer from the selected database:

    DATABASE_URL=<owner connection> python -B -m realtime.provision_host \\
        --state /var/lib/rocoto-video-preview --environment retail-demo --migration-role rocoto

The real run names the digest the dry run printed and a new private receipt file:

    ... --apply --expect-digest <identity_digest> --receipt /root/identity-import.json

Inputs are the environment's `.workspace-identity.json` and the principals its `access.sqlite`
holds for that environment. Both are read; the access store opens read-only and immutable. The
owner connection comes from the environment, never from an argument. The runtime and control
roles are refused, as `provision.import_workspace_identity` requires.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from realtime.provision import IdentityImportError, import_workspace_identity

SCHEMA = "rocoto.identity-import-host-receipt.v1"
IDENTITY_FILE = ".workspace-identity.json"
IDENTITY_BYTES = 8192
ACCESS_STORE = "access.sqlite"
ACCESS_STORE_BYTES = 256 * 1024**2
RESTRICTED_ROLES = frozenset({"rocoto_rt", "rocoto_ctrl"})
ENVIRONMENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
DIGEST = re.compile(r"[0-9a-f]{64}")


class HostImportRefused(ValueError):
    """A refusal with a stable operator-readable code; it never carries a connection string."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_private(state: Path, parts: tuple[str, ...], *, maximum: int) -> tuple[int, os.stat_result]:
    """Open one private regular file below the state directory, following no symbolic link.

    Every directory on the way belongs to the owner of the state directory, who alone can
    write it. The file belongs to that owner and carries no group or other permission.
    """
    try:
        descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise HostImportRefused("state_unavailable", "Select the service's real state directory.") from None
    try:
        info = os.fstat(descriptor)
        owner = info.st_uid
        if info.st_mode & 0o022:
            raise HostImportRefused("input_custody", "Keep the state directory writable by its owner only.")
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if last else os.O_DIRECTORY)
            try:
                opened = os.open(part, flags, dir_fd=descriptor)
            except OSError:
                raise HostImportRefused(
                    "input_unavailable", "Restore the saved identity inputs of this environment."
                ) from None
            os.close(descriptor)
            descriptor = opened
            info = os.fstat(descriptor)
            if info.st_uid != owner or info.st_mode & (0o077 if last else 0o022):
                raise HostImportRefused(
                    "input_custody", "Keep the identity inputs private to the service identity."
                )
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
            raise HostImportRefused("input_custody", "Use the original single private input file.")
        result, descriptor = descriptor, -1
        return result, info
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_identity(state: Path, environment: str) -> tuple[dict, str]:
    """The saved workspace identity and the SHA-256 of its exact bytes."""
    parts = (IDENTITY_FILE,) if environment == "demo" else ("environments", environment, IDENTITY_FILE)
    descriptor, _ = _open_private(state, parts, maximum=IDENTITY_BYTES)
    with os.fdopen(descriptor, "rb") as stream:
        content = stream.read(IDENTITY_BYTES + 1)
    try:
        record = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise HostImportRefused("invalid_identity", "Restore the original workspace identity.") from None
    if len(content) > IDENTITY_BYTES or not isinstance(record, dict):
        raise HostImportRefused("invalid_identity", "Restore the original workspace identity.")
    return record, hashlib.sha256(content).hexdigest()


def read_principals(state: Path, environment: str) -> tuple[str, list[dict]]:
    """The environment's display name and its principals, from an unchanged read-only store."""
    path = state / ACCESS_STORE
    descriptor, before = _open_private(state, (ACCESS_STORE,), maximum=ACCESS_STORE_BYTES)
    try:
        for suffix in ("-journal", "-wal"):
            with contextlib.suppress(FileNotFoundError):
                if os.lstat(str(path) + suffix).st_size:
                    raise HostImportRefused(
                        "access_store_busy", "Read the access store between its writes; run again."
                    )
        # Read-only and immutable: this command takes no lock and leaves no journal behind.
        store = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
        try:
            named = store.execute("SELECT name FROM environments WHERE id=?", (environment,)).fetchone()
            rows = store.execute(
                "SELECT e.principal_id,p.id,p.display_name,p.created_at,e.role "
                "FROM principal_environments e LEFT JOIN principals p ON p.id=e.principal_id "
                "WHERE e.environment=? ORDER BY e.principal_id",
                (environment,),
            ).fetchall()
        except sqlite3.Error:
            raise HostImportRefused(
                "access_store_unavailable", "Restore the preview's access store."
            ) from None
        finally:
            store.close()
        try:
            current = _identity(os.stat(path, follow_symlinks=False))
        except OSError:
            current = None
        if not _identity(before) == _identity(os.fstat(descriptor)) == current:
            raise HostImportRefused(
                "access_store_changed", "The access store changed during the read; run again."
            )
    finally:
        os.close(descriptor)
    if named is None or not isinstance(named[0], str):
        raise HostImportRefused("environment_unknown", "Select an environment the access store holds.")
    principals = []
    for membership, principal, display_name, created_at, role in rows:
        if principal is None or principal != membership:
            raise HostImportRefused(
                "access_store_inconsistent", "Reconcile memberships that name an absent principal."
            )
        principals.append(
            {"id": principal, "display_name": display_name, "role": role, "created_at": created_at}
        )
    if not principals:
        raise HostImportRefused("environment_empty", "Select an environment with at least one principal.")
    return named[0], principals


def _sha256(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _tool_digests() -> dict[str, str]:
    here = Path(__file__).resolve().parent
    return {
        name: hashlib.sha256((here / name).read_bytes()).hexdigest()
        for name in ("provision.py", "provision_host.py")
    }


async def _connect(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn, timeout=10, command_timeout=30)


def _reserve_receipt(path: Path) -> int:
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise HostImportRefused(
            "receipt_unavailable", "Name a new receipt file in an existing private directory."
        ) from None


def _validate(options, dsn: str) -> None:
    if not isinstance(options.migration_role, str) or options.migration_role in RESTRICTED_ROLES:
        raise HostImportRefused("migration_role_required", "Use the explicit owner migration connection.")
    if not isinstance(options.environment, str) or not ENVIRONMENT.fullmatch(options.environment):
        raise HostImportRefused("invalid_import", "Supply the existing environment alias.")
    if options.apply != bool(options.expect_digest) or options.apply != bool(options.receipt):
        raise HostImportRefused(
            "apply_requires_dry_run",
            "Apply with the dry run's digest and a new receipt file; the dry run takes neither.",
        )
    if options.apply and not DIGEST.fullmatch(options.expect_digest):
        raise HostImportRefused("apply_requires_dry_run", "Supply the 64-character digest of the dry run.")
    if not isinstance(dsn, str) or not dsn.strip():
        raise HostImportRefused("connection_required", "Supply the owner connection in DATABASE_URL.")


async def _import(connection, options, arguments) -> tuple[dict, dict, bool, bool | None]:
    role = await connection.fetchval("select current_user::text")
    if role in RESTRICTED_ROLES or role != options.migration_role:
        raise HostImportRefused("migration_role_required", "Connect as the named owner migration role.")
    from schema_migrations import assert_current

    await assert_current(connection)
    database = await connection.fetchrow(
        "select current_database()::text as name,"
        "(select version_num from public.alembic_version) as schema_revision"
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        imported = await import_workspace_identity(connection, **arguments)
        if options.apply and imported["identity_digest"] != options.expect_digest:
            raise HostImportRefused(
                "digest_mismatch", "The inputs differ from the reviewed dry run; run it again."
            )
    except BaseException:
        await transaction.rollback()
        raise
    if not options.apply:
        await transaction.rollback()
        return imported, dict(database), False, None
    await transaction.commit()
    # The exact retry reads the committed tenant, memberships, binding and alias back.
    verified = False
    with contextlib.suppress(Exception):
        check = connection.transaction()
        await check.start()
        try:
            again = await import_workspace_identity(connection, **arguments)
            verified = (
                again["status"] == "already_imported"
                and again["identity_digest"] == imported["identity_digest"]
            )
        finally:
            await check.rollback()
    return imported, dict(database), True, verified


async def import_identity(options, dsn: str, *, connect=_connect) -> dict:
    """Run the dry run or the real import and return its receipt."""
    _validate(options, dsn)
    state = Path(options.state).absolute()
    saved_identity, identity_sha256 = read_identity(state, options.environment)
    stored_name, principals = read_principals(state, options.environment)
    arguments = {
        "saved_identity": saved_identity,
        "principals": principals,
        "environment_alias": options.environment,
        "display_name": options.display_name or stored_name,
        "migration_source": options.migration_source
        or f"rocoto-video-preview:{options.environment}:workspace-identity:sha256:{identity_sha256}",
        "migration_role": options.migration_role,
    }
    reserved = _reserve_receipt(Path(options.receipt)) if options.apply else None
    committed = False
    try:
        connection = await connect(dsn)
        try:
            imported, database, committed, verified = await _import(connection, options, arguments)
        finally:
            with contextlib.suppress(Exception):
                await connection.close()
        receipt = {
            **imported,
            "schema": SCHEMA,
            "mode": "apply" if options.apply else "dry_run",
            "commit_state": "committed" if committed else "rolled_back_dry_run",
            "verified_by_exact_retry": verified,
            "display_name": arguments["display_name"],
            "principals": [
                {"id": row["id"], "role": row["role"]}
                for row in sorted(principals, key=lambda row: str(row["id"]))
            ],
            "inputs": {
                "workspace_identity_sha256": identity_sha256,
                "principals_sha256": _sha256(sorted(principals, key=lambda row: str(row["id"]))),
            },
            "database": database,
            "tool": {"module": "realtime.provision_host", "sha256": _tool_digests()},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if reserved is not None:
            descriptor, reserved = reserved, None
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
        if verified is False:
            raise HostImportRefused(
                "verification_failed", "The import committed and its exact retry differs; keep the receipt."
            )
        return receipt
    finally:
        if reserved is not None:
            os.close(reserved)
            if not committed:
                # Nothing was imported, so the reserved receipt name is released.
                Path(options.receipt).unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="python -m realtime.provision_host",
        description="Import one saved video company identity; the dry run is the default.",
    )
    command.add_argument("--state", type=Path, required=True, help="The preview service state directory")
    command.add_argument("--environment", required=True, help="The existing environment alias")
    command.add_argument("--migration-role", required=True, help="The owner role DATABASE_URL connects as")
    command.add_argument("--display-name", help="Company name; the environment's stored name by default")
    command.add_argument("--migration-source", help="Retained export reference; derived from the identity")
    command.add_argument("--apply", action="store_true", help="Commit; requires the dry run's digest")
    command.add_argument("--expect-digest", help="identity_digest printed by the dry run")
    command.add_argument("--receipt", help="New private file that receives the committed receipt")
    return command


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    options = parser().parse_args(argv)
    source = os.environ if environ is None else environ
    try:
        receipt = asyncio.run(import_identity(options, source.get("DATABASE_URL", "")))
    except (HostImportRefused, IdentityImportError) as refusal:
        print(f"REFUSING: {refusal.code}: {refusal}", file=sys.stderr)
        return 2
    except Exception as failure:
        # Driver errors can quote connection details; the class and SQLSTATE identify the failure.
        detail = " ".join(filter(None, (type(failure).__name__, getattr(failure, "sqlstate", None))))
        print(f"REFUSING: database_unavailable: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
