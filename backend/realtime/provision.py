"""Explicit host migration of saved video identity into the existing tenant registry.

This module is not an HTTP/SDK mutation. The operator supplies a verified export,
its retained migration reference, and an already-open privileged transaction.
Shape validation is not authentication; database role verification is mandatory.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

MAX_PRINCIPALS = 250
_IDENTITY_FIELDS = {"schema_version", "company_scope", "onboarding_seed", "origin", "created_at"}
_PRIVILEGE_SQL = """
select current_user::text as role_name,
       (select rolsuper from pg_roles where rolname=current_user) as is_superuser,
       (select count(*)=5 and coalesce(bool_and(pg_has_role(current_user,c.relowner,'USAGE')),false)
        from pg_class c where c.oid=any(array[
          to_regclass('public.tenants'),to_regclass('public.operators'),
          to_regclass('public.team_members'),to_regclass('realtime.company_binding'),
          to_regclass('realtime.environment_alias')])) as owns_tables
"""


class IdentityImportError(ValueError):
    """A typed failure with a stable operator-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class IdentityImportConflict(IdentityImportError):
    """An existing identity or import receipt requires explicit reconciliation."""


def _text(value, field, maximum=256):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise IdentityImportError("invalid_import", f"Supply a bounded {field} from the verified export.")
    return value


def _timestamp(value):
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError
            result = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value)
        else:
            raise ValueError
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        raise IdentityImportError(
            "invalid_import", "Preserve an explicit, valid creation timestamp."
        ) from None


def _manifest(saved_identity, principals, environment_alias, display_name, migration_source):
    if not isinstance(saved_identity, Mapping) or set(saved_identity) != _IDENTITY_FIELDS:
        raise IdentityImportError("invalid_identity", "Supply the complete saved workspace identity.")
    identity = dict(saved_identity)
    company = identity["company_scope"]
    if (
        type(identity["schema_version"]) is not int
        or identity["schema_version"] != 1
        or not isinstance(company, str)
        or not re.fullmatch(r"[0-9a-f]{64}", company)
    ):
        raise IdentityImportError("invalid_identity", "Preserve the saved 64-character company identity.")
    seed = _text(identity["onboarding_seed"], "onboarding seed", 4096)
    if identity["origin"] == "legacy_path_adoption":
        valid_seed = hashlib.sha256(seed.encode()).hexdigest() == company
    else:
        valid_seed = identity["origin"] == "new_company" and seed == "workspace-" + company
    if not valid_seed:
        raise IdentityImportError("invalid_identity", "Restore the original workspace identity and seed.")
    _timestamp(identity["created_at"])
    if not isinstance(identity["created_at"], str):
        raise IdentityImportError("invalid_identity", "Preserve the saved identity timestamp string.")
    if not isinstance(environment_alias, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,63}", environment_alias
    ):
        raise IdentityImportError("invalid_import", "Supply the existing environment alias.")
    _text(display_name, "company display name", 160)
    _text(migration_source, "retained migration source reference", 1024)
    if (
        not isinstance(principals, Sequence)
        or isinstance(principals, (str, bytes))
        or not 1 <= len(principals) <= MAX_PRINCIPALS
    ):
        raise IdentityImportError("invalid_import", "Supply between 1 and 250 verified principal rows.")
    rows = []
    seen = set()
    for raw in principals:
        if (
            not isinstance(raw, Mapping)
            or not {"id", "display_name", "role"} <= raw.keys()
            or set(raw) - {"id", "display_name", "role", "created_at"}
        ):
            raise IdentityImportError(
                "invalid_import", "Supply saved principal IDs, names and exact membership roles."
            )
        key = _text(raw["id"], "principal ID", 80)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", key) or key.startswith(
            ("organization-", "orgchat-")
        ):
            raise IdentityImportError(
                "invalid_import", "Import human principals; retain service actors separately."
            )
        if key in seen:
            raise IdentityImportConflict(
                "duplicate_principal", "Reconcile duplicate principal rows before import."
            )
        seen.add(key)
        if not isinstance(raw["role"], str) or raw["role"] not in {"owner", "operator", "browse"}:
            raise IdentityImportError("invalid_import", "Preserve the saved owner, operator or browse role.")
        rows.append(
            {
                "id": key,
                "display_name": _text(raw["display_name"], "principal display name", 80),
                "role": raw["role"],
                "created_at": _timestamp(raw["created_at"]) if "created_at" in raw else None,
            }
        )
    return {
        "schema": "rocoto.identity-import.v1",
        "saved_identity": identity,
        "principals": sorted(rows, key=lambda row: row["id"]),
        "environment_alias": environment_alias,
        "display_name": display_name,
        "migration_source": migration_source,
    }


def _conflict(code, message):
    raise IdentityImportConflict(code, message)


async def import_workspace_identity(
    connection,
    *,
    saved_identity,
    principals,
    environment_alias,
    display_name,
    migration_source,
    migration_role,
):
    """Import once or validate an exact retry, without committing the caller's transaction.

    ``identity_digest`` binds the whole import manifest, including principal roles
    and its retained export reference. Existing unrelated rows fail closed. A later
    membership change must use the normal authorized service, not re-run this import.
    The returned receipt becomes durable only when the caller commits.
    """
    manifest = _manifest(saved_identity, principals, environment_alias, display_name, migration_source)
    _text(migration_role, "explicit migration database role", 63)
    if migration_role in {"rocoto_rt", "rocoto_ctrl"}:
        raise IdentityImportError("migration_role_required", "Use the explicit owner migration connection.")
    if not connection.is_in_transaction():
        raise IdentityImportError("transaction_required", "Open an explicit migration transaction first.")
    role = await connection.fetchrow(_PRIVILEGE_SQL)
    if (
        not role
        or role["role_name"] in {"rocoto_rt", "rocoto_ctrl"}
        or role["role_name"] != migration_role
        or not (role["is_superuser"] or role["owns_tables"])
    ):
        raise IdentityImportError(
            "migration_role_required", "Verify the owner migration role for every target table."
        )
    company = manifest["saved_identity"]["company_scope"]
    scope = "t." + company
    digest = hashlib.sha256(
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    rows = manifest["principals"]
    try:
        # A savepoint preserves all-or-nothing behavior even if the caller catches a conflict.
        async with connection.transaction():
            await connection.execute("select pg_advisory_xact_lock(74184761562702)")
            binding = await connection.fetchrow(
                "select tenant_id,scope::text as scope,identity_digest,import_reference "
                "from realtime.company_binding where tenant_id=$1 for update",
                company,
            )
            tenant = await connection.fetchrow(
                "select slug,name from public.tenants where slug=$1 for update", company
            )
            if binding:
                if (
                    binding["identity_digest"] != digest
                    or binding["import_reference"] != migration_source
                    or binding["scope"] != scope
                    or tenant is None
                ):
                    _conflict(
                        "identity_conflict",
                        "Reconcile the existing tenant import provenance before retrying.",
                    )
            elif tenant:
                _conflict(
                    "tenant_collision",
                    "Verify the existing tenant identity through an explicit reconciliation.",
                )
            existing = await connection.fetch(
                "select id,name,email from public.operators where id=any($1::text[]) for update",
                [row["id"] for row in rows],
            )
            operators = {row["id"]: row for row in existing}
            members = await connection.fetch(
                "select operator_id,role from public.team_members where tenant_slug=$1 for update", company
            )
            memberships = {row["operator_id"]: row["role"] for row in members}
            if binding:
                if set(memberships) != {row["id"] for row in rows}:
                    _conflict(
                        "membership_drift",
                        "Retain the current membership state and reconcile this import retry.",
                    )
                for row in rows:
                    current = operators.get(row["id"])
                    if current is None or memberships.get(row["id"]) != row["role"]:
                        _conflict(
                            "membership_drift",
                            "Retain current principal and role state before reconciling a retry.",
                        )
                alias = await connection.fetchrow(
                    "select alias from realtime.environment_alias where tenant_id=$1 and alias=$2",
                    company,
                    environment_alias,
                )
                if not alias:
                    _conflict("alias_drift", "Reconcile the stored environment binding before retrying.")
                status = "already_imported"
            else:
                if operators:
                    _conflict(
                        "principal_collision",
                        "Verify existing principal identities before importing their membership.",
                    )
                if memberships:
                    _conflict(
                        "membership_collision",
                        "Reconcile existing membership rows before importing this tenant.",
                    )
                await connection.execute(
                    "insert into public.tenants(slug,name) values($1,$2)", company, display_name
                )
                for row in rows:
                    created = datetime.fromisoformat(row["created_at"]) if row["created_at"] else None
                    await connection.execute(
                        "insert into public.operators(id,name,email,created_at) "
                        "values($1,$2,NULL,coalesce($3::timestamptz,now()))",
                        row["id"],
                        row["display_name"],
                        created,
                    )
                    await connection.execute(
                        "insert into public.team_members(operator_id,tenant_slug,role) values($1,$2,$3)",
                        row["id"],
                        company,
                        row["role"],
                    )
                await connection.execute(
                    "insert into realtime.company_binding(tenant_id,scope,identity_digest,import_reference) "
                    "values($1,$2::ltree,$3,$4)",
                    company,
                    scope,
                    digest,
                    migration_source,
                )
                await connection.execute(
                    "insert into realtime.environment_alias(tenant_id,alias) values($1,$2)",
                    company,
                    environment_alias,
                )
                status = "imported"
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise IdentityImportConflict(
                "concurrent_identity_collision",
                "Reconcile the concurrently registered identity before retrying.",
            ) from None
        raise
    return {
        "schema": "rocoto.identity-import-receipt.v1",
        "status": status,
        "tenant_id": company,
        "scope": scope,
        "environment_alias": environment_alias,
        "identity_digest": digest,
        "import_reference": migration_source,
        "principal_count": len(rows),
        "migration_role": migration_role,
        "commit_state": "pending_caller_commit",
    }
