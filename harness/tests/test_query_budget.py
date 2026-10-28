import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compute_guard import ApprovalError, OWNER
from query_budget import BOUNDED_PROFILE, QueryBudget


def reserve_in_process(path, number, queue):
    try:
        QueryBudget(path).reserve("2.00", request_id=f"parallel-{number:04}")
        queue.put(True)
    except ApprovalError:
        queue.put(False)


class QueryBudgetTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.approval = self.root / "approval.json"
        now = datetime.now(timezone.utc)
        self.data = {"schema_version": 1, "approval_id": "live-queries-test", "purpose": "rocoto-video-live-query",
                     "provider": "modal", "workspace": "peter-17535", "verified_by": OWNER,
                     "verification_source": "user-confirmation-in-this-chat", "verification_note": "test owner reading",
                     "user_verified": True, "verified_at": now.isoformat(),
                     "expires_at": (now + timedelta(hours=1)).isoformat(), "net_spend_limit_usd": "0",
                     "credit_remaining_usd": "11", "reserved_credit_usd": "5", "approved_run_gross_usd": "6"}
        self.save_authority()
        self.budget = QueryBudget(self.approval)

    def save_authority(self):
        self.approval.write_text(json.dumps(self.data))
        self.approval.chmod(0o600)

    def tearDown(self):
        self.directory.cleanup()

    def test_parallel_chats_cannot_oversubscribe(self):
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        processes = [context.Process(target=reserve_in_process, args=(str(self.approval), i, queue)) for i in range(8)]
        for process in processes:
            process.start()
        answers = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(answers), 3)
        self.assertEqual(self.budget.status()["available_usd"], "0")

    def test_billing_lag_and_failure_keep_maximum(self):
        one = self.budget.reserve("2")["reservation_id"]
        self.budget.mark_started(one)
        self.budget.complete(one, runtime_estimate_usd="0.05", timing={"worker_seconds": 20})
        two = self.budget.reserve("2")["reservation_id"]
        self.budget.mark_started(two)
        self.budget.fail(two, reason="connection interrupted")
        self.assertEqual(self.budget.status()["available_usd"], "2")
        with self.assertRaises(ApprovalError):
            self.budget.cancel_before_remote(one)

    def test_only_unstarted_reservation_can_cancel(self):
        reservation = self.budget.reserve("2")["reservation_id"]
        self.budget.cancel_before_remote(reservation)
        self.assertEqual(self.budget.status()["available_usd"], "6")
        with self.assertRaises(ApprovalError):
            self.budget.mark_started(reservation)

    def test_authority_change_cannot_reset_consumption(self):
        self.budget.reserve("2")
        self.data["approved_run_gross_usd"] = "100"
        self.save_authority()
        with self.assertRaises(ApprovalError):
            self.budget.reserve("2")

    def test_expiry_and_paid_cap_fail_closed(self):
        for key, value in [("net_spend_limit_usd", "1"), ("expires_at", "2020-01-01T00:00:00+00:00")]:
            original = self.data[key]
            self.data[key] = value
            self.save_authority()
            with self.assertRaises(ApprovalError):
                self.budget.reserve("2")
            self.data[key] = original
        self.save_authority()

    def test_unknown_request_and_replayed_admission_fail(self):
        with self.assertRaises(ApprovalError):
            self.budget.mark_started("unknown-1234")
        reservation = self.budget.reserve("2", request_id="unique-query-1234")["reservation_id"]
        self.budget.mark_started(reservation)
        with self.assertRaises(ApprovalError):
            self.budget.mark_started(reservation)
        with self.assertRaises(ApprovalError):
            self.budget.reserve("2", request_id=reservation)

    def test_settled_private_evidence_releases_unused_reservation(self):
        rid = self.budget.reserve("2")["reservation_id"]
        self.budget.mark_started(rid)
        self.budget.complete(rid, runtime_estimate_usd="0.2")
        evidence = self.root / "settled.json"
        evidence.write_text(json.dumps({"provider": "modal", "workspace": "peter-17535", "reservation_id": rid,
                                        "settled": True, "verified_by": OWNER, "user_verified": True,
                                        "app_id": "ap-example123", "source_url": "https://modal.com/apps/example",
                                        "gross_usd": "0.12"}))
        evidence.chmod(0o600)
        self.budget.reconcile(rid, provider_evidence_path=evidence)
        self.assertEqual(self.budget.status()["available_usd"], "5.88")

    def provider_evidence(self, *, stopped=True, rows=1):
        now = datetime.now(timezone.utc).timestamp()
        return {"workspace": "peter-17535", "app_id": "ap-query123", "state": "APP_STATE_STOPPED" if stopped else "APP_STATE_STOPPING",
                "running_tasks": 0 if stopped else 1, "report_rows": rows,
                "created_at": str(now - 2), "stopped_at": str(now - 1), "reported_gross_usd": ".02",
                "rates_per_hour": {"H100": "3.95", "CPU": ".0473", "MemoryGiB": ".008"}}

    def test_automatic_provider_reconciliation_keeps_calculated_allowance(self):
        rid = self.budget.reserve("2")["reservation_id"]
        self.budget.mark_started(rid, resource_profile=BOUNDED_PROFILE)
        self.budget.complete(rid, runtime_estimate_usd=".01")
        with patch("query_budget._modal_app_evidence", return_value=self.provider_evidence()):
            result = self.budget.reconcile_modal(rid, "ap-query123")
        self.assertEqual(result["status"], "completed_with_calculated_reserve")
        self.assertGreater(float(result["held_usd"]), .02)
        self.assertLess(float(result["held_usd"]), 2)
        self.assertIn("calculated", result["billing_status"])

    def test_automatic_reconciliation_requires_confirmed_stop_and_report(self):
        rid = self.budget.reserve("2")["reservation_id"]
        self.budget.mark_started(rid, resource_profile=BOUNDED_PROFILE)
        self.budget.complete(rid, runtime_estimate_usd=".01")
        for evidence in [self.provider_evidence(stopped=False), self.provider_evidence(rows=0)]:
            with patch("query_budget._modal_app_evidence", return_value=evidence), self.assertRaises(ApprovalError):
                self.budget.reconcile_modal(rid, "ap-query123")
        self.assertEqual(self.budget.status()["available_usd"], "4")
        with patch("query_budget._modal_app_evidence", return_value=self.provider_evidence()):
            self.budget.reconcile_pending()
        self.assertGreater(float(self.budget.status()["available_usd"]), 5)

    def test_automatic_reconciliation_requires_actual_hard_limit_profile(self):
        rid = self.budget.reserve("2")["reservation_id"]
        self.budget.mark_started(rid)
        self.budget.complete(rid, runtime_estimate_usd=".01")
        with self.assertRaises(ApprovalError):
            self.budget.reconcile_modal(rid, "ap-query123")
        self.assertEqual(self.budget.status()["available_usd"], "4")

    def test_failed_super_attempt_retains_app_and_reconciles_its_resource_ceiling(self):
        rid = self.budget.reserve('3.50')['reservation_id']
        self.budget.mark_started(rid, resource_profile='b200-1-cpu4-ram128gib-cached-image')
        self.budget.attach_app(rid, 'ap-query123')
        self.budget.fail(rid, reason='RemoteFailure')
        self.assertEqual(self.budget.status()['available_usd'], '2.50')
        evidence = self.provider_evidence()
        evidence['rates_per_hour']['B200'] = '6.25'
        with patch('query_budget._modal_app_evidence', return_value=evidence):
            result = self.budget.reconcile_pending()[0]
        self.assertEqual(result['app_id'], 'ap-query123')
        self.assertEqual(result['status'], 'completed_with_calculated_reserve')
        self.assertAlmostEqual(float(result['held_usd']), .02 + 31 * 7.4632 / 3600, places=5)

    def test_crashed_local_caller_reconciles_only_after_remote_shutdown(self):
        rid = self.budget.reserve('2')['reservation_id']
        self.budget.mark_started(rid, resource_profile=BOUNDED_PROFILE)
        self.budget.attach_app(rid, 'ap-query123')
        with patch('query_budget._modal_app_evidence', return_value=self.provider_evidence(stopped=False)):
            self.budget.reconcile_pending()
        self.assertEqual(self.budget.status()['available_usd'], '4')
        with patch('query_budget._modal_app_evidence', return_value=self.provider_evidence()):
            self.budget.reconcile_pending()
        self.assertGreater(float(self.budget.status()['available_usd']), 5)


if __name__ == "__main__":
    unittest.main()
