"""Shared, fail-closed credit reservations for live Rocoto video questions.

Reserve before creating a Modal app; mark_started before any remote work. A
completed or interrupted attempt retains its maximum until authenticated provider
evidence permits a conservative lifecycle allowance. Runtime estimates and that
allowance remain distinct from settled charges. All chats share one locked ledger.
This broker does not purchase credit, change provider limits, or launch jobs.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from compute_guard import ApprovalError, OWNER, _amount, _read_private, _utc, validate_approval

PURPOSE = "rocoto-video-live-query"
WORKSPACE = "peter-17535"
BOUNDED_PROFILE = "h100-1-cpu2-ram64gib-cached-image"
BOUNDED_PROFILES = {BOUNDED_PROFILE: ("H100", 2, 64),
                    "b200-1-cpu4-ram128gib-cached-image": ("B200", 4, 128)}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _money(value):
    return str(_amount(value, "query cost"))


class QueryBudget:
    """A single owner-authorized query pool, separate from preparation costs.

    ``reserve('2.00')`` returns a dict containing ``reservation_id``. Call
    ``mark_started(id)`` immediately before Modal admission, then ``complete``
    or ``fail``. ``cancel_before_remote`` releases only an unstarted reservation.
    A process crash leaves its reservation held; elapsed time never releases it.
    """

    def __init__(self, approval_path, *, workspace=WORKSPACE):
        self.approval_path = Path(approval_path).absolute()
        self.workspace = workspace
        self.ledger_path = self.approval_path.parent / "query-ledger.json"
        self.lock_path = self.approval_path.parent / "query-ledger.lock"

    @contextmanager
    def _locked(self, *, planned=None):
        # Read ownership/protection on every operation. The authority digest is
        # pinned in the ledger, so refreshing a file cannot reset consumed credit.
        raw = _read_private(self.approval_path)
        authority = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if planned is not None:
            approval = validate_approval(self.approval_path, workspace=self.workspace,
                                         planned_gross_usd=planned, purpose=PURPOSE)
            if approval.digest != digest:
                raise ApprovalError("Retry after the authorization record finishes updating.")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            details = os.fstat(fd)
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                    or details.st_mode & 0o077 or details.st_nlink != 1):
                raise ApprovalError("Query ledger lock requires private owner-held storage.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            if hashlib.sha256(_read_private(self.approval_path)).hexdigest() != digest:
                raise ApprovalError("Retry after the authorization record finishes updating.")
            if self.ledger_path.exists() or self.ledger_path.is_symlink():
                ledger = self._read_ledger()
                if (ledger.get("authorization_sha256") != digest
                        or ledger.get("workspace") != self.workspace
                        or ledger.get("schema_version") != 1):
                    raise ApprovalError("Reconcile the existing query ledger before changing its authorization.")
            else:
                # Creating a pool always requires an unexpired verified authority.
                validate_approval(self.approval_path, workspace=self.workspace,
                                  planned_gross_usd=planned or "0.01", purpose=PURPOSE)
                limit = min(_amount(authority["approved_run_gross_usd"], "query pool"),
                            _amount(authority["credit_remaining_usd"], "credit")
                            - _amount(authority["reserved_credit_usd"], "preparation reserve"))
                if authority.get("gross_usage_limit_usd") is not None:
                    limit = min(limit, _amount(authority["gross_usage_limit_usd"], "gross limit")
                                - _amount(authority["current_gross_usage_usd"], "gross usage"))
                ledger = {"schema_version": 1, "workspace": self.workspace,
                          "authorization_sha256": digest, "approval_id": authority["approval_id"],
                          "expires_at": authority["expires_at"], "limit_usd": str(limit),
                          "created_at": _now(), "reservations": {}}
            yield ledger
            self._write_ledger(ledger)
        finally:
            os.close(fd)

    def _read_ledger(self):
        fd = os.open(self.ledger_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            details = os.fstat(stream.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                    or details.st_mode & 0o077 or details.st_nlink != 1):
                raise ApprovalError("Query ledger requires private owner-held storage.")
            content = stream.read(2_000_001)
        if len(content) > 2_000_000:
            raise ApprovalError("Archive settled query accounting before adding more records.")
        try:
            return json.loads(content)
        except (ValueError, TypeError) as exc:
            raise ApprovalError("Reconcile the unreadable query ledger before execution.") from exc

    def _write_ledger(self, ledger):
        path = self.ledger_path.with_name(".query-ledger-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(ledger, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.ledger_path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _summary(ledger):
        held = sum((_amount(r["held_usd"], "held query amount")
                    for r in ledger["reservations"].values()), Decimal(0))
        limit = _amount(ledger["limit_usd"], "query pool")
        return {"workspace": ledger["workspace"], "limit_usd": str(limit),
                "committed_or_reserved_usd": str(held), "available_usd": str(max(Decimal(0), limit - held)),
                "expires_at": ledger["expires_at"], "reservation_count": len(ledger["reservations"]),
                "accounting": "Running or unverified attempts retain their maximum; verified stopped apps retain a conservative lifecycle cost reserve."}

    def status(self):
        with self._locked() as ledger:
            return self._summary(ledger)

    def reserve(self, planned_gross_usd, *, request_id=None):
        amount = _amount(planned_gross_usd, "planned query maximum")
        if amount <= 0:
            raise ApprovalError("Provide a positive bounded query reservation.")
        rid = request_id or uuid.uuid4().hex
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", rid):
            raise ApprovalError("Use a unique simple query request identifier.")
        self.reconcile_pending()
        with self._locked(planned=amount) as ledger:
            if rid in ledger["reservations"]:
                raise ApprovalError("This query request already has a reservation.")
            available = Decimal(self._summary(ledger)["available_usd"])
            if amount > available:
                raise ApprovalError("Reconcile pending query costs or add owner-authorized credit before more GPU work.")
            entry = {"reservation_id": rid, "status": "reserved", "maximum_usd": str(amount),
                     "held_usd": str(amount), "created_at": _now()}
            ledger["reservations"][rid] = entry
            return dict(entry)

    @staticmethod
    def _entry(ledger, reservation_id):
        try:
            return ledger["reservations"][reservation_id]
        except KeyError as exc:
            raise ApprovalError("Reserve this query before changing its accounting.") from exc

    def mark_started(self, reservation_id, *, resource_profile=None):
        with self._locked(planned="0.01") as ledger:
            entry = self._entry(ledger, reservation_id)
            if entry["status"] != "reserved":
                raise ApprovalError("A query reservation authorizes one remote attempt.")
            entry.update(status="running", started_at=_now(), resource_profile=resource_profile)

    def complete(self, reservation_id, *, runtime_estimate_usd, timing=None, billing_snapshot=None):
        estimate = _money(runtime_estimate_usd)
        with self._locked() as ledger:
            entry = self._entry(ledger, reservation_id)
            if entry["status"] != "running":
                raise ApprovalError("Complete the currently running query attempt.")
            entry.update(status="awaiting_billing", completed_at=_now(), runtime_estimate_usd=estimate,
                         timing=timing or {}, billing_snapshot=billing_snapshot or {})
            # Even an unexpectedly high estimate consumes headroom immediately.
            entry["held_usd"] = str(max(Decimal(entry["maximum_usd"]), Decimal(estimate)))
            return dict(entry)

    def attach_app(self, reservation_id, app_id):
        """Persist app identity before inference so failures can reconcile later."""
        if not re.fullmatch(r"ap-[A-Za-z0-9]+", str(app_id)):
            raise ApprovalError("Attach the provider's valid app identifier.")
        with self._locked() as ledger:
            entry = self._entry(ledger, reservation_id)
            if entry["status"] != "running" or entry.get("app_id") not in (None, app_id):
                raise ApprovalError("Attach the active query's own provider app.")
            if any(row.get("app_id") == app_id for rid, row in ledger["reservations"].items() if rid != reservation_id):
                raise ApprovalError("Each query requires its own provider app.")
            entry["app_id"] = app_id

    def fail(self, reservation_id, *, reason):
        with self._locked() as ledger:
            entry = self._entry(ledger, reservation_id)
            if entry["status"] not in {"running", "reserved"}:
                raise ApprovalError("Record failure for an active query attempt.")
            entry.update(status="interrupted_awaiting_billing", completed_at=_now(), reason=str(reason)[:1000])

    def cancel_before_remote(self, reservation_id):
        with self._locked() as ledger:
            entry = self._entry(ledger, reservation_id)
            if entry["status"] != "reserved":
                raise ApprovalError("A started query keeps its reservation until provider billing settles.")
            entry.update(status="cancelled_before_remote", held_usd="0", completed_at=_now())

    def reconcile_modal(self, reservation_id, app_id):
        """Autonomously reconcile using authenticated provider evidence.

        A stopped app's reported usage can lag. Keep the *sum* of its current
        reported cost and a full-lifecycle resource allowance, with 30 additional
        seconds for shutdown. This deliberately double-counts known compute to
        cover lag. It is a calculated reserve, not a claim of settled billing.

        The caller profile requires a cached image and a declared GPU/CPU/RAM
        hard ceiling. Cold image builds or unconstrained jobs
        keep the original reservation. Provider $0 net spend remains enforced.
        """
        with self._locked() as ledger:
            entry = dict(self._entry(ledger, reservation_id))
        if (entry["status"] not in {"running", "awaiting_billing", "interrupted_awaiting_billing", "completed_with_calculated_reserve"}
                or entry.get("resource_profile") not in BOUNDED_PROFILES
                or not re.fullmatch(r"ap-[A-Za-z0-9]+", str(app_id))):
            raise ApprovalError("Automatic reconciliation needs a completed, bounded cached-image attempt.")
        with self._locked() as ledger:
            current = self._entry(ledger, reservation_id)
            if (current.get("app_id") not in (None, app_id)
                    or any(other.get("app_id") == app_id for key, other in ledger["reservations"].items()
                           if key != reservation_id)):
                raise ApprovalError("Each query reservation must reference its own provider app.")
            current["app_id"] = app_id
        evidence = _modal_app_evidence(self.workspace, app_id)
        return self._apply_provider_evidence(reservation_id, app_id, evidence)

    def reconcile_pending(self):
        """Retry delayed provider reads automatically before later admissions."""
        with self._locked() as ledger:
            pending = [(rid, row["app_id"]) for rid, row in ledger["reservations"].items()
                       if row.get("app_id") and row.get("resource_profile") in BOUNDED_PROFILES
                       and row["status"] in {"running", "awaiting_billing", "interrupted_awaiting_billing"}]
        results = []
        for rid, app_id in pending:
            try:
                results.append(self.reconcile_modal(rid, app_id))
            except Exception as exc:
                # Missing provider evidence keeps every previously held dollar.
                results.append({"reservation_id": rid, "status": "reservation_retained", "error_type": type(exc).__name__})
        return results

    def _apply_provider_evidence(self, reservation_id, app_id, evidence):
        """Pure accounting step; production evidence comes from Modal above."""
        if (evidence.get("workspace") != self.workspace or evidence.get("app_id") != app_id
                or evidence.get("state") != "APP_STATE_STOPPED" or evidence.get("running_tasks") != 0
                or evidence.get("report_rows", 0) < 1):
            raise ApprovalError("Wait for confirmed app shutdown and its provider usage report.")
        created = _amount(evidence["created_at"], "app creation time")
        stopped = _amount(evidence["stopped_at"], "app shutdown time")
        if created <= 0 or stopped <= created:
            raise ApprovalError("Provider lifecycle must cover the completed app.")
        with self._locked() as ledger:
            profile = self._entry(ledger, reservation_id).get("resource_profile")
        if profile not in BOUNDED_PROFILES:
            raise ApprovalError("Reconcile a verified hard resource profile.")
        gpu, cpu, memory_gib = BOUNDED_PROFILES[profile]
        rates = evidence["rates_per_hour"]
        rate = (_amount(rates[gpu], "GPU rate") + cpu * _amount(rates["CPU"], "CPU rate")
                + memory_gib * _amount(rates["MemoryGiB"], "memory rate")) / Decimal(3600)
        if rate <= 0:
            raise ApprovalError("Verify current provider resource rates before reconciliation.")
        reported = _amount(evidence["reported_gross_usd"], "reported app cost")
        if reported <= 0:
            raise ApprovalError("Wait for the app's metered usage to appear before releasing credit.")
        reserve = reported + (stopped - created + Decimal(30)) * rate
        reserve = reserve.quantize(Decimal("0.000001"), rounding="ROUND_CEILING")
        with self._locked() as ledger:
            current = self._entry(ledger, reservation_id)
            if current.get("resource_profile") != profile or current["status"] not in {
                    "running", "awaiting_billing", "interrupted_awaiting_billing", "completed_with_calculated_reserve"}:
                raise ApprovalError("Reconcile the completed bounded query attempt.")
            if created < Decimal(str(_utc(current["started_at"]).timestamp())) - Decimal(5):
                raise ApprovalError("Provider app creation must belong to this query attempt.")
            if current.get("app_id") not in (None, app_id):
                raise ApprovalError("Reconcile the app originally attached to this query.")
            # Re-reading lagging reports can raise, but never lower, this reserve.
            reserve = max(reserve, _amount(current.get("calculated_reserve_usd", "0"), "prior reserve"))
            current.update(status="completed_with_calculated_reserve", held_usd=str(reserve),
                           calculated_reserve_usd=str(reserve), app_id=app_id,
                           provider_evidence=evidence, reconciled_at=_now(),
                           billing_status="provider-reported usage plus calculated lifecycle reserve; final billing may lag")
            return dict(current)

    def reconcile(self, reservation_id, *, provider_evidence_path):
        """Release unused credit only with a private, verified app-charge record.

        Evidence records require provider='modal', workspace, reservation_id,
        app_id, settled=true, gross_usd, verified_by owner, user_verified=true,
        and source_url=https://modal.com/apps/... . Aggregate, lagging workspace
        snapshots do not establish an individual app's settled charge.
        """
        evidence = json.loads(_read_private(Path(provider_evidence_path)))
        if (evidence.get("provider") != "modal" or evidence.get("workspace") != self.workspace
                or evidence.get("reservation_id") != reservation_id or evidence.get("settled") is not True
                or evidence.get("verified_by") != OWNER or evidence.get("user_verified") is not True
                or not re.fullmatch(r"ap-[A-Za-z0-9]+", str(evidence.get("app_id", "")))
                or not str(evidence.get("source_url", "")).startswith("https://modal.com/apps/")):
            raise ApprovalError("Reconcile against verified settled Modal app billing.")
        charge = _money(evidence["gross_usd"])
        with self._locked() as ledger:
            entry = self._entry(ledger, reservation_id)
            if entry["status"] not in {"awaiting_billing", "interrupted_awaiting_billing", "completed_with_calculated_reserve"}:
                raise ApprovalError("Reconcile a completed or interrupted query attempt.")
            entry.update(status="settled", held_usd=charge, settled_gross_usd=charge,
                         billing_evidence=evidence, reconciled_at=_now())
            return dict(entry)


def _modal_app_evidence(workspace_name, app_id):
    """Read provider state, current rates, and per-app costs without mutations."""
    import modal
    from modal.client import _Client
    from modal._utils.async_utils import synchronizer
    from modal_proto import api_pb2

    workspace = modal.Workspace.from_context()
    workspace.hydrate()
    owners = {member.email for member in workspace.members.list() if member.role == "owner"}
    if workspace.name != workspace_name or OWNER not in owners:
        raise ApprovalError("Read billing through the verified Rocoto workspace.")

    async def read_app():
        client = await _Client.from_env()
        response = await asyncio.wait_for(
            client._stub.AppList(api_pb2.AppListRequest(environment_name="main")), timeout=15)
        item = next((item for item in response.apps if item.app_id == app_id), None)
        if item is None or item.description != "rocoto-video-cosmos-inspection":
            raise ApprovalError("Verify the query's own Modal app before releasing its reservation.")
        return {"app_id": item.app_id, "state": api_pb2.AppState.Name(item.state),
                "created_at": str(item.created_at), "stopped_at": str(item.stopped_at),
                "running_tasks": item.n_running_tasks}

    # Reuse Modal's own event loop: its cached client belongs to that loop.
    evidence = synchronizer.create_blocking(read_app)()
    if evidence["state"] != "APP_STATE_STOPPED" or evidence["running_tasks"] != 0:
        raise ApprovalError("The query app must finish shutting down before reconciliation.")
    start = datetime.fromtimestamp(float(evidence["created_at"]), timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Modal excludes partial final intervals. Asking through the next boundary
    # returns the current hour's partial metering; it is explicitly not settled.
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    rows = [row for row in workspace.billing.report(start=start, end=end, resolution="h") if row.object_id == app_id]
    rates = workspace.billing.rates()
    evidence.update(workspace=workspace_name, report_rows=len(rows), sampled_at=_now(),
                    reported_gross_usd=str(sum((row.cost for row in rows), Decimal(0))),
                    cost_by_resource={key: str(sum((row.cost_by_resource.get(key, Decimal(0)) for row in rows), Decimal(0)))
                                      for key in {key for row in rows for key in row.cost_by_resource}},
                    rates_per_hour={"H100": str(rates["gpu_hour_cost_h100"]), "B200": str(rates["gpu_hour_cost_b200"]),
                                    "CPU": str(rates["cpu_hour_cost"]),
                                    "MemoryGiB": str(rates["mem_gib_hour_cost"])},
                    source="Modal AppList, Workspace.billing.report and Workspace.billing.rates")
    return evidence
