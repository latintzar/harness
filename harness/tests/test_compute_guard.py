"""Budget preflight tests use local fixtures and never contact Modal."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compute_guard import ApprovalError, claim_approval, validate_approval


class ComputeGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o700)
        self.path = self.folder / "approval.json"
        self.now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        self.data = {
            "schema_version": 1,
            "approval_id": "pilot-20261004-1",
            "purpose": "rocoto-video-embedding-pilot",
            "provider": "modal",
            "workspace": "peter-17535",
            "verified_by": "peter@soyrocoto.com",
            "verification_source": "https://modal.com/settings/usage",
            "user_verified": True,
            "verified_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(hours=1)).isoformat(),
            "net_spend_limit_usd": "0",
            "credit_remaining_usd": "23.38",
            "reserved_credit_usd": "18",
            "gross_usage_limit_usd": "25",
            "current_gross_usage_usd": "7.21",
            "approved_run_gross_usd": "3",
        }
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def check(self):
        return validate_approval(self.path, workspace="peter-17535", planned_gross_usd="3", now=self.now)

    def test_fresh_verified_zero_spend_approval_can_be_claimed_once(self):
        approval = self.check()
        receipt = claim_approval(approval, now=self.now)
        self.assertEqual(json.loads(receipt.read_text())["planned_gross_usd"], "3")
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ApprovalError):
            claim_approval(approval, now=self.now)

    def test_missing_approval_stops_before_execution(self):
        self.path.unlink()
        with self.assertRaises(ApprovalError):
            self.check()

    def test_standalone_and_packaged_layout_keep_approvals_outside_code(self):
        import compute_guard
        for module in ['/tmp/rocoto-proof/compute_guard.py',
                       '/opt/rocoto/current/app/src/prototypes/video/compute_guard.py',
                       '/opt/rocoto/current/app/src/prototypes/video/verticals/retail/compute_guard.py']:
            with self.subTest(module=module), patch.object(compute_guard, '__file__', module):
                self.assertEqual(self.check().approval_id, self.data['approval_id'])
                root = compute_guard._code_root()
                if '/app/src/prototypes/video/' in module:
                    self.assertEqual(root, Path('/opt/rocoto/current'))
                with self.assertRaises(ApprovalError):
                    compute_guard._read_private(root / 'approval.json')

    def test_billing_or_identity_mismatch_fails_closed(self):
        for key, value in [
            ("workspace", "another-company"), ("provider", "other"),
            ("verified_by", "someone@example.com"), ("user_verified", False),
            ("verification_source", "https://example.com/usage"),
            ("net_spend_limit_usd", "0.01"), ("credit_remaining_usd", "2.99"),
            ("reserved_credit_usd", "23"),
            ("approved_run_gross_usd", "2.99"), ("gross_usage_limit_usd", "10"),
        ]:
            with self.subTest(key=key):
                original = self.data[key]
                self.data[key] = value
                self.write()
                with self.assertRaises(ApprovalError):
                    self.check()
                self.data[key] = original

    def test_expired_future_and_overlong_approvals_fail(self):
        for verified, expires in [
            (self.now - timedelta(hours=2), self.now),
            (self.now + timedelta(minutes=10), self.now + timedelta(hours=1)),
            (self.now, self.now + timedelta(hours=25)),
            (self.now - timedelta(hours=25), self.now + timedelta(minutes=1)),
        ]:
            with self.subTest(verified=verified, expires=expires):
                self.data.update(verified_at=verified.isoformat(), expires_at=expires.isoformat())
                self.write()
                with self.assertRaises(ApprovalError):
                    self.check()

    def test_nonfinite_and_negative_amounts_fail(self):
        for value in ["NaN", "Infinity", "-1", True, "wrong"]:
            with self.subTest(value=value):
                self.data["credit_remaining_usd"] = value
                self.write()
                with self.assertRaises(ApprovalError):
                    self.check()

    def test_user_readback_can_leave_provider_gross_cap_unknown(self):
        self.data.update(
            verification_source="user-confirmation-in-this-chat",
            verification_note="Owner confirmed net spend $0 and remaining credit $23.",
            gross_usage_limit_usd=None,
            current_gross_usage_usd=None,
            credit_remaining_usd="23",
            approved_run_gross_usd="5",
        )
        self.write()
        self.assertEqual(self.check().workspace, "peter-17535")

    def test_reasoning_approval_requires_matching_declared_caller(self):
        self.data["purpose"] = "rocoto-video-reasoning-pilot"
        self.write()
        with self.assertRaises(ApprovalError):
            self.check()
        approval = validate_approval(
            self.path, workspace="peter-17535", planned_gross_usd="3",
            purpose="rocoto-video-reasoning-pilot", now=self.now,
        )
        self.assertEqual(approval.workspace, "peter-17535")

    def test_private_file_and_directory_required(self):
        self.path.chmod(0o644)
        with self.assertRaises(ApprovalError):
            self.check()
        self.path.chmod(0o600)
        self.folder.chmod(0o755)
        with self.assertRaises(ApprovalError):
            self.check()

    def test_symlink_is_rejected(self):
        original = self.path.with_name("real.json")
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaises(ApprovalError):
            self.check()

    def test_modified_approval_cannot_be_claimed(self):
        approval = self.check()
        self.data["credit_remaining_usd"] = "100"
        self.write()
        with self.assertRaises(ApprovalError):
            claim_approval(approval, now=self.now)


if __name__ == "__main__":
    unittest.main()
