"""Fail-closed admission for one Rocoto Modal embedding experiment.

This local record is an owner's fresh attestation of provider settings, not a
replacement for Modal's enforced net-spend cap. Record actual dashboard readings
only after the owner verifies them. The SDK cannot verify/set that cap itself.

Approval JSON fields (money is decimal USD text): schema_version=1,
approval_id, purpose='rocoto-video-embedding-pilot', provider='modal', workspace,
verified_by='peter@soyrocoto.com', verification_source (the official billing URL),
user_verified=true, verified_at/expires_at (timezone-aware ISO timestamps),
net_spend_limit_usd='0', credit_remaining_usd, reserved_credit_usd,
approved_run_gross_usd. gross_usage_limit_usd/current_gross_usage_usd are optional
additional provider readings: null explicitly means the gross cap is unverified.

Store the record outside the checkout, mode 0600, in a mode-0700 directory owned
by the current user. An approval is valid for at most 24 hours and is single-use.
An interrupted/failed run keeps its reservation. Reconcile its actual usage
before creating another attempt record under the same still-valid owner
authorization. This module creates no approval and makes no provider calls.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re


BILLING_URL = "https://modal.com/settings/usage"
USER_CONFIRMATION = "user-confirmation-in-this-chat"
from private_records import OPERATOR as OWNER, PrivateRecordError as ApprovalError, read_private
PURPOSE = "rocoto-video-embedding-pilot"
ALLOWED_PURPOSES = {PURPOSE, "rocoto-video-reasoning-pilot", "rocoto-video-live-query"}
MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class Approval:
    path: Path
    digest: str
    approval_id: str
    workspace: str
    planned_gross_usd: Decimal
    expires_at: datetime


def _utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ApprovalError("Use a timezone-aware approval timestamp.") from exc


def _amount(value, name: str) -> Decimal:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError) as exc:
        raise ApprovalError(f"Provide a finite, positive-or-zero {name} reading.") from exc


def _code_root() -> Path:
    """Locate source ownership without assuming a particular install depth."""
    module = Path(__file__).resolve()
    for parent in module.parents:
        if (parent / '.git').exists() or (parent / '.release.json').is_file():
            return parent
        if parent.parts[-4:] == ('app', 'src', 'prototypes', 'video'):
            return parent.parents[3]
    if tuple(module.parts[-5:-1]) == ('app', 'src', 'prototypes', 'video'):
        return module.parents[4]
    return module.parent


def _read_private(path: Path) -> bytes:
    return read_private(path, code_root=_code_root())


def validate_approval(path, *, workspace: str, planned_gross_usd, purpose=PURPOSE, now=None) -> Approval:
    """Validate an attested $0 provider cap, credits and gross headroom."""
    now = now or datetime.now(timezone.utc)
    if purpose not in ALLOWED_PURPOSES:
        raise ApprovalError("Select a declared Rocoto video experiment purpose.")
    path = Path(path).absolute()
    raw = _read_private(path)
    try:
        data = json.loads(raw)
        expected = {
            "schema_version": 1, "purpose": purpose, "provider": "modal",
            "workspace": workspace, "verified_by": OWNER,
            "user_verified": True,
        }
        if any(data.get(key) != value or type(data.get(key)) is not type(value)
               for key, value in expected.items()):
            raise ApprovalError("Verify the selected workspace and its owner-held billing settings.")
        if data.get("verification_source") not in (BILLING_URL, USER_CONFIRMATION):
            raise ApprovalError("Record the official billing page or the owner's chat confirmation.")
        if data.get("verification_source") == USER_CONFIRMATION and not data.get("verification_note"):
            raise ApprovalError("Record the owner's confirmed billing readings.")
        approval_id = data["approval_id"]
        if not isinstance(approval_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", approval_id):
            raise ApprovalError("Use a unique approval ID with letters, numbers, underscores or hyphens.")
        verified = _utc(data["verified_at"])
        expires = _utc(data["expires_at"])
        if (verified > now + timedelta(minutes=5) or now - verified > MAX_AGE
                or expires <= now or expires <= verified or expires - verified > MAX_AGE):
            raise ApprovalError("Refresh the billing verification; approval lasts at most 24 hours.")
        if _amount(data["net_spend_limit_usd"], "net spend limit") != 0:
            raise ApprovalError("Verify Modal's enforced net spend limit is exactly $0.")
        planned = _amount(planned_gross_usd, "planned gross budget")
        remaining = _amount(data["credit_remaining_usd"], "remaining applicable credit")
        reserve = _amount(data["reserved_credit_usd"], "credit reserved for later queries")
        approved = _amount(data["approved_run_gross_usd"], "approved run gross budget")
        headroom = remaining - reserve
        if data.get("gross_usage_limit_usd") is not None:
            limit = _amount(data["gross_usage_limit_usd"], "gross usage limit")
            used = _amount(data["current_gross_usage_usd"], "current gross usage")
            headroom = min(headroom, limit - used)
        if planned <= 0 or min(headroom, approved) < planned:
            raise ApprovalError("Verify applicable credit and gross budget cover the planned run.")
        return Approval(path, hashlib.sha256(raw).hexdigest(), approval_id, workspace, planned, expires)
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ApprovalError("Complete the required verified budget approval fields.") from exc


def claim_approval(approval: Approval, *, now=None) -> Path:
    """Atomically consume an approval before image builds or remote execution."""
    now = now or datetime.now(timezone.utc)
    raw = _read_private(approval.path)
    if now >= approval.expires_at or hashlib.sha256(raw).hexdigest() != approval.digest:
        raise ApprovalError("Refresh the changed or expired budget approval before execution.")
    receipt = approval.path.with_name(f"approval-{approval.approval_id}.claimed.json")
    try:
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise ApprovalError("Reconcile this attempt's usage before preparing another budget record.") from exc
    except OSError as exc:
        raise ApprovalError("Budget reservation requires a writable private approval directory.") from exc
    with os.fdopen(fd, "w") as stream:
        json.dump({
            "approval_id": approval.approval_id, "workspace": approval.workspace,
            "approval_sha256": approval.digest, "claimed_at": now.isoformat(),
            "planned_gross_usd": str(approval.planned_gross_usd),
        }, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return receipt
