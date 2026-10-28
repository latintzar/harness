"""Explicit binding of a verified supplier identity to an imported operator.

The realtime import deliberately leaves ``public.operators.email`` NULL.  This
module is the narrow control-plane operation that fills that existing column
after the host has verified an owner context and the supplier identity.  It
does not create operators, memberships, sessions, or a second identity table.

The caller must obtain ``owner_context`` from Rocoto's verified platform-owner
allowlist (the existing ``app.require_owner`` boundary). A tenant ``owner``
membership role, a claimed role, a mapping supplied by a browser, or a model
output is not an authorization proof. The database connection must also be a
control-plane connection with update access to ``public.operators``; runtime/
RLS connections are rejected before any write.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_ENVIRONMENTS = frozenset({"development", "staging", "production"})
_MAX_ID = 256


class IdentityReconciliationError(ValueError):
    """A stable, operator-facing reconciliation failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class IdentityReconciliationConflict(IdentityReconciliationError):
    """The requested binding conflicts with durable identity state."""


@dataclass(frozen=True)
class VerifiedOwner:
    """The small trusted result from Rocoto's platform-owner auth boundary.

    ``VerifiedOwner`` is intentionally constructed by the host after its
    ``app.require_owner`` bearer/OAuth and allowlist checks. A tenant owner
    membership or request claim cannot construct this authorization. The type
    validates shape only; it cannot verify an external token by itself.
    """

    email: str
    subject: str
    environment: str
    role: str = "owner"

    def __post_init__(self) -> None:
        email = _email(self.email)
        _identifier(self.subject, "owner subject")
        if self.environment not in _ENVIRONMENTS:
            raise IdentityReconciliationError(
                "invalid_environment", "Use the selected deployment environment."
            )
        if self.role != "owner":
            raise IdentityReconciliationError(
                "owner_authorization_required", "An owner authorization is required."
            )
        object.__setattr__(self, "email", email)

    @classmethod
    def from_context(cls, context: Mapping[str, Any], *, environment: str) -> "VerifiedOwner":
        """Convert the existing trusted platform-owner ``require_owner`` result.

        This helper deliberately accepts only the platform-owner result and a
        verified subject. A tenant membership role is insufficient. It does
        not accept an email, role, or subject from a request body; the caller
        must pass the result of the existing auth dependency.
        """

        if not isinstance(context, Mapping):
            raise IdentityReconciliationError("owner_authorization_required", "Use a verified owner context.")
        user = context.get("user")
        subject = user.get("id") if isinstance(user, Mapping) else None
        if not subject:
            subject = context.get("subject")
        return cls(
            email=context.get("email", ""),
            subject=subject or "",
            environment=environment,
            role=context.get("role", ""),
        )


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_ID or "\x00" in value:
        raise IdentityReconciliationError("invalid_identity_request", f"Supply a bounded {field}.")
    return value


def _email(value: Any) -> str:
    if not isinstance(value, str):
        raise IdentityReconciliationError("invalid_email", "Supply the verified login email.")
    value = value.strip().lower()
    if len(value) > 320 or not _EMAIL.fullmatch(value):
        raise IdentityReconciliationError("invalid_email", "Supply the verified login email.")
    return value


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= 500:
        raise IdentityReconciliationError("invalid_reason", "Supply a bounded reconciliation reason.")
    if any(ord(char) < 32 for char in value):
        raise IdentityReconciliationError("invalid_reason", "Supply a bounded reconciliation reason.")
    return value.strip()


def _fingerprint(*, company_id: str, operator_id: str, email: str, reason: str) -> str:
    body = {
        "company_id": company_id,
        "operator_id": operator_id,
        "email": email,
        "reason": reason,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def _require_control_connection(connection) -> None:
    """Fail closed when a runtime role reaches this control-plane mutation."""

    checks = await connection.fetchrow(
        "select has_table_privilege(current_user,'public.operators','UPDATE') as can_update, "
        "has_table_privilege(current_user,'public.operator_audit','INSERT') as can_audit, "
        "has_table_privilege(current_user,'public.operator_request','INSERT') as can_request"
    )
    if not checks or not all(checks.values()):
        raise IdentityReconciliationError(
            "control_connection_required", "Use the owner control connection for identity reconciliation."
        )


async def reconcile_verified_login(
    connection,
    *,
    company_id: str,
    operator_id: str,
    verified_email: str,
    owner: VerifiedOwner,
    request_id: str,
    reason: str,
) -> dict[str, Any]:
    """Bind ``verified_email`` to one existing imported operator.

    The caller owns the surrounding transaction and must commit it.  The
    operation is idempotent by ``(owner email, environment, company, request)``
    and records an immutable private audit row.  It preserves every existing
    membership and operator ID.  A target already bound to another email,
    another operator using the email, or a target with memberships in another
    company is rejected.
    """

    company_id = _identifier(company_id, "company ID")
    operator_id = _identifier(operator_id, "operator ID")
    request_id = _identifier(request_id, "request ID")
    email = _email(verified_email)
    reason = _reason(reason)
    if not isinstance(owner, VerifiedOwner):
        raise IdentityReconciliationError(
            "owner_authorization_required", "Use a verified owner authorization."
        )
    if not connection.is_in_transaction():
        raise IdentityReconciliationError(
            "transaction_required", "Open an explicit control transaction first."
        )

    await _require_control_connection(connection)
    fingerprint = _fingerprint(
        company_id=company_id, operator_id=operator_id, email=email, reason=reason
    )
    email_digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    response = {
        "schema": "rocoto.identity-reconciliation.v1",
        "status": "bound",
        "tenant_id": company_id,
        "operator_id": operator_id,
        "email_sha256": email_digest,
        "request_id": request_id,
    }

    # The company lock serializes target membership and idempotency checks. The
    # email lock also serializes two different company requests for the same
    # verified login before the global duplicate query and UPDATE.
    await connection.execute(
        "select pg_advisory_xact_lock(hashtextextended($1, 0))", "identity:" + company_id
    )
    await connection.execute("select pg_advisory_xact_lock(hashtextextended($1, 0))", "email:" + email)

    previous = await connection.fetchrow(
        "select fingerprint,status,response from public.operator_request "
        "where actor_email=$1 and environment=$2 and tenant_slug=$3 and request_id=$4 for update",
        owner.email,
        owner.environment,
        company_id,
        request_id,
    )
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise IdentityReconciliationConflict(
                "request_conflict", "The request ID was already used for different identity data."
            )
        replayed = dict(previous["response"])
        replayed["replayed"] = True
        return replayed

    company = await connection.fetchrow(
        "select t.slug from public.tenants t join realtime.company_binding b on b.tenant_id=t.slug "
        "where t.slug=$1 for update",
        company_id,
    )
    if not company:
        raise IdentityReconciliationError(
            "company_unavailable", "The imported company identity is unavailable."
        )

    target = await connection.fetchrow(
        "select o.id,o.email,tm.role from public.operators o "
        "join public.team_members tm on tm.operator_id=o.id "
        "where tm.tenant_slug=$1 and o.id=$2 for update of o,tm",
        company_id,
        operator_id,
    )
    if not target:
        raise IdentityReconciliationError(
            "operator_unavailable", "The imported operator identity is unavailable."
        )

    memberships = await connection.fetch(
        "select tenant_slug from public.team_members where operator_id=$1 for update", operator_id
    )
    if {row["tenant_slug"] for row in memberships} != {company_id}:
        raise IdentityReconciliationConflict(
            "cross_company_operator", "The imported operator belongs to more than one company."
        )

    other = await connection.fetch(
        "select id from public.operators where lower(email)=$1 and id<>$2 for update", email, operator_id
    )
    if other:
        raise IdentityReconciliationConflict(
            "email_already_bound", "The verified login is already bound to another operator."
        )

    current = (target["email"] or "").strip().lower()
    if current and current != email:
        raise IdentityReconciliationConflict(
            "operator_already_bound", "The imported operator is already bound to another login."
        )

    if not current:
        await connection.execute(
            "update public.operators set email=$2,updated_at=clock_timestamp() where id=$1",
            operator_id,
            email,
        )
    else:
        response["status"] = "already_bound"

    await connection.execute(
        "insert into public.operator_request(actor_email,environment,tenant_slug,request_id," \
        "fingerprint,status,response) values($1,$2,$3,$4,$5,200,$6::jsonb)",
        owner.email,
        owner.environment,
        company_id,
        request_id,
        fingerprint,
        response,
    )
    await connection.execute(
        "insert into public.operator_audit(actor_email,session_id,environment,tenant_slug,action," \
        "target_id,outcome,request_id,detail) values($1,$2,$3,$4,'identity.reconcile',$5,'ok',$6,$7::jsonb)",
        owner.email,
        owner.subject,
        owner.environment,
        company_id,
        operator_id,
        request_id,
        {
            "status": response["status"],
            "reason": reason,
            "verified_email_sha256": email_digest,
            "authorization": "verified_owner_context",
        },
    )
    return response
