import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, Promoter,
    ReferralAttribution, ReferralEligibility, ReferralIncentiveRecord,
    ReferralIncentiveRule, ReferralIncentiveRuleAudit, Staff, Transaction,
)
from app.staff_auth import issue_token_pair
from app.config import TestingConfig


class ReferralIncentivesPhase8C2TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.client = self.app.test_client()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.points = Business(
            name="Puntos", slug="puntos-8c2", loyalty_type="points",
            is_active=True, web_access_enabled=True,
        )
        self.stamps = Business(
            name="Sellos", slug="sellos-8c2", loyalty_type="stamps",
            is_active=True, web_access_enabled=True,
        )
        db.session.add_all([self.points, self.stamps]); db.session.flush()
        self.manager = self._staff(self.points, "manager", "manage_referral_incentives")
        self.denied = self._staff(self.points, "denied", "view_stats")
        self.other_manager = self._staff(self.stamps, "other", "manage_referral_incentives")
        self.promoter = self._promoter(self.points, "promoter")
        self.customer = Customer(
            business_id=self.points.id, full_name="Cliente", phone="+525511111111",
            current_points=11, current_stamps=0, created_at=self.now,
        )
        db.session.add(self.customer); db.session.flush()
        self.attribution = ReferralAttribution(
            business_id=self.points.id, promoter_id=self.promoter.id,
            customer_id=self.customer.id, created_at=self.now,
        )
        db.session.add(self.attribution); db.session.flush()
        self.purchase = Transaction(
            business_id=self.points.id, customer_id=self.customer.id,
            staff_id=self.manager.id, type="earn", points_amount=5,
            purchase_amount=100, timestamp=self.now + timedelta(seconds=1),
        )
        db.session.add(self.purchase); db.session.flush()
        self.eligibility = ReferralEligibility(
            business_id=self.points.id, attribution_id=self.attribution.id,
            promoter_id=self.promoter.id, customer_id=self.customer.id,
            status="verified", source_transaction_id=self.purchase.id,
            evidence_reference_hash="a" * 64, evidence_type="recorded_amount",
            evidence_key_id="b" * 16, reviewed_by_staff_id=self.manager.id,
            reviewed_at=self.now, version=1,
        )
        db.session.add(self.eligibility); db.session.commit()
        self.headers = self._auth(self.manager)

    def tearDown(self):
        db.session.remove()
        db.session.execute(text("PRAGMA foreign_keys=OFF"))
        db.drop_all()
        self.ctx.pop()

    def _staff(self, business, username, permissions):
        item = Staff(
            business_id=business.id, username=username, role="manager", is_active=True,
            web_access_enabled=True, permissions=permissions,
        )
        item.set_password("PasswordSeguro123!")
        db.session.add(item); db.session.flush()
        return item

    def _promoter(self, business, username):
        item = Promoter(
            business_id=business.id, name=username, username=username, is_active=True,
        )
        item.set_password("PasswordSeguro123!")
        db.session.add(item); db.session.flush()
        return item

    def _auth(self, staff):
        access, _ = issue_token_pair(staff, channel="web")
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "phase8c2-csrf"
        return {
            "Authorization": f"Bearer {access}", "X-CSRFToken": "phase8c2-csrf",
        }

    def _payload(self, **changes):
        payload = {
            "beneficiary_type": "external_promoter",
            "loyalty_program": "points", "reward_type": "points",
            "reward_amount": "5.000000",
            "eligible_condition": "first_verified_purchase",
            "period_limit": 10, "period_unit": "month",
            "valid_from": None, "valid_until": None, "status": "draft",
            "reason": "Configuración inicial para revisión manual.",
        }
        payload.update(changes)
        return payload

    def _create_rule(self, request_id="create-rule", headers=None, **changes):
        return self.client.post(
            f"/manager/business/{self.points.id}/referral-incentive-rules",
            json=self._payload(**changes),
            headers={**(headers or self.headers), "Idempotency-Key": request_id},
        )

    def _preview(self, rule, eligibility=None, headers=None):
        target = eligibility or self.eligibility
        return self.client.post(
            f"/manager/business/{target.business_id}/referral-eligibilities/"
            f"{target.id}/incentive-preview/{rule.id}",
            json={}, headers=headers or self.headers,
        )

    def test_rule_creation_is_disabled_by_design_audited_and_idempotent(self):
        response = self._create_rule()
        self.assertEqual(response.status_code, 201, response.get_json())
        rule = ReferralIncentiveRule.query.one()
        audit = ReferralIncentiveRuleAudit.query.one()
        self.assertEqual((rule.status, rule.version), ("draft", 1))
        self.assertEqual((audit.actor_id, audit.action), (self.manager.id, "created"))
        replay = self._create_rule()
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotent_replay"])
        self.assertEqual(ReferralIncentiveRule.query.count(), 1)
        self.assertEqual(ReferralIncentiveRuleAudit.query.count(), 1)
        conflict = self._create_rule(reward_amount="6", request_id="create-rule")
        self.assertEqual(conflict.status_code, 409)

    def test_omitted_status_defaults_to_disabled_and_rules_can_be_listed(self):
        payload = self._payload()
        payload.pop("status")
        response = self.client.post(
            f"/manager/business/{self.points.id}/referral-incentive-rules",
            json=payload, headers={**self.headers, "Idempotency-Key": "default-disabled"},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["rule"]["status"], "disabled")
        listing = self.client.get(
            f"/manager/business/{self.points.id}/referral-incentive-rules",
            headers=self.headers,
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.get_json()["rules"]), 1)
        self.assertNotIn("reason", listing.get_json()["rules"][0])

    def test_rules_reject_program_mismatch_activation_and_invalid_amounts(self):
        for changes in (
            {"loyalty_program": "stamps"}, {"reward_type": "stamps"},
            {"status": "active"}, {"reward_amount": "NaN"},
            {"reward_amount": 0}, {"reward_amount": "0.0000001"},
            {"eligible_condition": "registration"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self._create_rule(request_id=f"bad-{len(str(changes))}", **changes).status_code,
                    400,
                )
        self.assertEqual(ReferralIncentiveRule.query.count(), 0)

    def test_stamp_rules_require_integer_stamps_and_matching_program(self):
        headers = self._auth(self.other_manager)
        payload = self._payload(
            loyalty_program="stamps", reward_type="stamps", reward_amount="2",
        )
        accepted = self.client.post(
            f"/manager/business/{self.stamps.id}/referral-incentive-rules",
            json=payload, headers={**headers, "Idempotency-Key": "stamp-rule"},
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())
        payload["reward_amount"] = "1.5"
        rejected = self.client.post(
            f"/manager/business/{self.stamps.id}/referral-incentive-rules",
            json=payload, headers={**headers, "Idempotency-Key": "fractional-stamps"},
        )
        self.assertEqual(rejected.status_code, 400)

    def test_dry_run_distinguishes_all_core_outcomes_without_side_effects(self):
        self.assertEqual(self._create_rule().status_code, 201)
        rule = ReferralIncentiveRule.query.one()
        balances = (self.customer.current_points, self.customer.current_stamps)
        counts = (
            Transaction.query.count(), LoyaltyLedgerEntry.query.count(),
            GoogleWalletSyncJob.query.count(), ReferralIncentiveRecord.query.count(),
        )
        eligible = self._preview(rule)
        self.assertEqual(eligible.get_json()["outcome"], "eligible_for_proposal")
        self.assertTrue(eligible.get_json()["dry_run"])
        self.assertEqual((self.customer.current_points, self.customer.current_stamps), balances)
        self.assertEqual((
            Transaction.query.count(), LoyaltyLedgerEntry.query.count(),
            GoogleWalletSyncJob.query.count(), ReferralIncentiveRecord.query.count(),
        ), counts)
        rule.status = "disabled"; db.session.commit()
        self.assertEqual(self._preview(rule).get_json()["outcome"], "rule_disabled")
        rule.status = "draft"; self.eligibility.status = "pending"; db.session.commit()
        self.assertEqual(self._preview(rule).get_json()["outcome"], "pending_verification")
        self.eligibility.status = "revoked"; db.session.commit()
        self.assertEqual(self._preview(rule).get_json()["outcome"], "revoked")

    def test_duplicate_and_period_limit_are_detected_from_separate_records(self):
        self.assertEqual(self._create_rule(period_limit=1, period_unit="month").status_code, 201)
        rule = ReferralIncentiveRule.query.one()
        record = ReferralIncentiveRecord(
            business_id=self.points.id, eligibility_id=self.eligibility.id,
            rule_id=rule.id, rule_version=rule.version,
            beneficiary_type="external_promoter", beneficiary_id=self.promoter.id,
            event_key="first-purchase:1", status="proposed",
            reason="Propuesta manual sin concesión.", audit_reference="review-1",
            created_by_type="staff", created_by_id=self.manager.id,
        )
        db.session.add(record); db.session.commit()
        self.assertEqual(self._preview(rule).get_json()["outcome"], "conflict_or_duplicate")
        duplicate = ReferralIncentiveRecord(
            business_id=self.points.id, eligibility_id=self.eligibility.id,
            rule_id=rule.id, rule_version=rule.version,
            beneficiary_type="external_promoter", beneficiary_id=self.promoter.id,
            event_key="different", status="proposed", reason="Duplicado",
            audit_reference="review-2", created_by_type="staff",
            created_by_id=self.manager.id,
        )
        db.session.add(duplicate)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        reversal = ReferralIncentiveRecord(
            business_id=self.points.id, eligibility_id=self.eligibility.id,
            rule_id=rule.id, rule_version=rule.version,
            beneficiary_type="external_promoter", beneficiary_id=self.promoter.id,
            event_key="reverse:first-purchase:1", status="voided",
            reverses_record_id=record.id, reason="Reverso compensatorio auditable.",
            audit_reference="review-reversal", created_by_type="staff",
            created_by_id=self.manager.id,
        )
        db.session.add(reversal); db.session.commit()
        self.assertEqual(ReferralIncentiveRecord.query.count(), 2)

    def test_incompatible_future_beneficiaries_are_not_inferred(self):
        self.assertEqual(self._create_rule(beneficiary_type="staff").status_code, 201)
        rule = ReferralIncentiveRule.query.one()
        response = self._preview(rule)
        self.assertEqual(response.get_json()["outcome"], "not_eligible")
        self.assertIsNone(response.get_json()["beneficiary_id"])

    def test_tenant_permission_csrf_and_suspensions_fail_closed(self):
        self.assertEqual(self._create_rule(headers=self._auth(self.denied)).status_code, 403)
        no_csrf = {"Authorization": self.headers["Authorization"], "Idempotency-Key": "no-csrf"}
        self.assertEqual(self._create_rule(headers=no_csrf).status_code, 400)
        wrong_tenant = self.client.post(
            f"/manager/business/{self.stamps.id}/referral-incentive-rules",
            json=self._payload(), headers={**self.headers, "Idempotency-Key": "wrong-tenant"},
        )
        self.assertEqual(wrong_tenant.status_code, 403)
        self.points.is_active = False; db.session.commit()
        self.assertEqual(self._create_rule(request_id="suspended").status_code, 403)

    def test_rule_updates_use_version_cas_and_preserve_audit_snapshots(self):
        self.assertEqual(self._create_rule().status_code, 201)
        rule = ReferralIncentiveRule.query.one()
        payload = {
            "version": 1, "reward_amount": "7", "reason": "Ajuste revisado.",
        }
        url = f"/manager/business/{self.points.id}/referral-incentive-rules/{rule.id}"
        first = self.client.patch(
            url, json=payload, headers={**self.headers, "Idempotency-Key": "update-rule"},
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        replay = self.client.patch(
            url, json=payload, headers={**self.headers, "Idempotency-Key": "update-rule"},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotent_replay"])
        stale = self.client.patch(
            url, json={**payload, "reward_amount": "8"},
            headers={**self.headers, "Idempotency-Key": "stale-update"},
        )
        self.assertEqual(stale.status_code, 409)
        db.session.refresh(rule)
        self.assertEqual((rule.version, rule.reward_amount), (2, Decimal("7.000000")))
        self.assertEqual(ReferralIncentiveRuleAudit.query.count(), 2)

    def test_concurrent_rule_updates_have_one_version_winner(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'rules.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all()
                business = Business(
                    name="Concurrente", slug="concurrente-8c2", loyalty_type="points",
                    is_active=True, web_access_enabled=True,
                )
                db.session.add(business); db.session.flush()
                manager = Staff(
                    business_id=business.id, username="manager", role="manager",
                    is_active=True, web_access_enabled=True,
                    permissions="manage_referral_incentives",
                )
                manager.set_password("PasswordSeguro123!")
                db.session.add(manager); db.session.commit()
                access, _ = issue_token_pair(manager, channel="web")
                ids = (business.id, manager.id)
            client = app.test_client()
            with client.session_transaction() as browser:
                browser["csrf_token"] = "concurrent-csrf"
            payload = self._payload()
            created = client.post(
                f"/manager/business/{ids[0]}/referral-incentive-rules",
                json=payload, headers={
                    "Authorization": f"Bearer {access}", "X-CSRFToken": "concurrent-csrf",
                    "Idempotency-Key": "create-concurrent",
                },
            )
            rule_id = created.get_json()["rule"]["id"]

            def update_amount(index):
                local = app.test_client()
                with local.session_transaction() as browser:
                    browser["csrf_token"] = "concurrent-csrf"
                return local.patch(
                    f"/manager/business/{ids[0]}/referral-incentive-rules/{rule_id}",
                    json={
                        "version": 1, "reward_amount": str(6 + index),
                        "reason": "Actualización concurrente controlada.",
                    },
                    headers={
                        "Authorization": f"Bearer {access}",
                        "X-CSRFToken": "concurrent-csrf",
                        "Idempotency-Key": f"concurrent-{index}",
                    },
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = sorted(pool.map(update_amount, (1, 2)))
            self.assertEqual(statuses, [200, 409])
            with app.app_context():
                rule = db.session.get(ReferralIncentiveRule, rule_id)
                self.assertEqual(rule.version, 2)
                self.assertEqual(ReferralIncentiveRuleAudit.query.count(), 2)

    def test_history_is_append_only_and_prevents_parent_deletion(self):
        self.assertEqual(self._create_rule().status_code, 201)
        audit = ReferralIncentiveRuleAudit.query.one()
        audit.reason = "Intento de edición"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        rule = ReferralIncentiveRule.query.one()
        db.session.delete(rule)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(ReferralIncentiveRuleAudit.query.count(), 1)

    def test_browser_session_endpoint_uses_permission_and_csrf(self):
        login = self.client.post("/manager/login", data={
            "identity": self.manager.username, "password": "PasswordSeguro123!",
            "business_slug": self.points.slug,
        })
        self.assertEqual(login.status_code, 302)
        self.app.config["CSRF_ENABLED"] = True
        missing = self.client.post("/manager/portal/referral-incentive-rules", json=self._payload())
        self.assertEqual(missing.status_code, 400)
        with self.client.session_transaction() as browser:
            csrf = browser["csrf_token"]
        accepted = self.client.post(
            "/manager/portal/referral-incentive-rules", json=self._payload(),
            headers={"X-CSRFToken": csrf, "Idempotency-Key": "portal-create"},
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())


if __name__ == "__main__":
    unittest.main()
