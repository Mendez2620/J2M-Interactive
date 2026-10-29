import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, LoyaltyLedgerEntry, Promoter, ReferralAttribution,
    ReferralEligibility, ReferralEligibilityDecision, Staff, Transaction,
)
from app.staff_auth import issue_token_pair
from app.config import TestingConfig
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError


class ReferralEligibilityPhase8C1TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.base_time = datetime.now(timezone.utc).replace(microsecond=0)
        self.business = Business(name="Uno", slug="uno", loyalty_type="points", is_active=True, web_access_enabled=True)
        self.other = Business(name="Dos", slug="dos", loyalty_type="points", is_active=True, web_access_enabled=True)
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.manager = self._staff(self.business, "manager", "review_referrals")
        self.denied = self._staff(self.business, "denied", "view_stats")
        self.other_manager = self._staff(self.other, "other-manager", "review_referrals")
        self.promoter = self._promoter(self.business, "promoter")
        self.other_promoter = self._promoter(self.other, "other-promoter")
        self.customer = Customer(business_id=self.business.id, full_name="Cliente", phone="+525511111111", created_at=self.base_time)
        self.other_customer = Customer(business_id=self.other.id, full_name="Otro", phone="+525522222222", created_at=self.base_time)
        db.session.add_all([self.customer, self.other_customer]); db.session.flush()
        self.attribution, self.eligibility = self._eligibility(
            self.business, self.promoter, self.customer
        )
        self.other_attribution, self.other_eligibility = self._eligibility(
            self.other, self.other_promoter, self.other_customer
        )
        self.purchase = Transaction(
            business_id=self.business.id, customer_id=self.customer.id,
            staff_id=self.manager.id, type="earn", points_amount=5,
            purchase_amount=100, request_id="purchase-one", request_fingerprint="p" * 64,
            timestamp=self.base_time + timedelta(seconds=2),
        )
        db.session.add(self.purchase); db.session.commit()
        self.headers = self._auth(self.manager)

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _staff(self, business, username, permissions):
        item = Staff(
            business_id=business.id, username=username, role="manager", is_active=True,
            web_access_enabled=True, mobile_access_enabled=False, permissions=permissions,
        )
        item.set_password("PasswordSeguro123!"); db.session.add(item); db.session.flush()
        return item

    def _promoter(self, business, username):
        item = Promoter(business_id=business.id, name=username, username=username, is_active=True)
        item.set_password("PasswordSeguro123!"); db.session.add(item); db.session.flush()
        return item

    def _eligibility(self, business, promoter, customer):
        attribution = ReferralAttribution(
            business_id=business.id, promoter_id=promoter.id, customer_id=customer.id,
            created_at=self.base_time + timedelta(seconds=1),
        )
        db.session.add(attribution); db.session.flush()
        eligibility = ReferralEligibility(
            business_id=business.id, attribution_id=attribution.id,
            promoter_id=promoter.id, customer_id=customer.id,
        )
        db.session.add(eligibility); db.session.flush()
        return attribution, eligibility

    def _auth(self, staff):
        access, _ = issue_token_pair(staff, channel="web")
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "phase8c1-csrf"
        return {"Authorization": f"Bearer {access}", "X-CSRFToken": "phase8c1-csrf"}

    def _decide(self, eligibility=None, headers=None, **changes):
        payload = {
            "status": "verified", "transaction_id": self.purchase.id,
            "evidence_reference": "ticket-2026-0001",
            "evidence_type": "recorded_amount",
            "reason": "Compra presencial revisada contra el comprobante.",
        }
        payload.update(changes)
        target = eligibility or self.eligibility
        return self.client.post(
            f"/manager/business/{target.business_id}/referrals/{target.id}/decision",
            json=payload, headers={**(headers or self.headers), "Idempotency-Key": changes.get("request_id", "decision-one")},
        )

    def test_manual_verification_is_audited_and_has_no_loyalty_side_effects(self):
        balances = (self.customer.current_points, self.customer.current_stamps)
        transaction_count = Transaction.query.count()
        ledger_count = LoyaltyLedgerEntry.query.count()
        response = self._decide()
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.refresh(self.eligibility)
        self.assertEqual(self.eligibility.status, "verified")
        self.assertEqual(self.eligibility.source_transaction_id, self.purchase.id)
        self.assertEqual(self.eligibility.evidence_type, "recorded_amount")
        self.assertNotEqual(self.eligibility.evidence_reference_hash, "ticket-2026-0001")
        decision = ReferralEligibilityDecision.query.one()
        self.assertEqual((decision.actor_staff_id, decision.from_status, decision.to_status),
                         (self.manager.id, "pending", "verified"))
        self.assertNotIn("ticket-2026-0001", decision.evidence_reference_hash)
        self.assertEqual((self.customer.current_points, self.customer.current_stamps), balances)
        self.assertEqual(Transaction.query.count(), transaction_count)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), ledger_count)

    def test_idempotency_and_controlled_transitions(self):
        first = self._decide()
        replay = self._decide()
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        self.assertTrue(replay.get_json()["idempotent_replay"])
        self.assertEqual(ReferralEligibilityDecision.query.count(), 1)
        conflict = self._decide(reason="Motivo diferente")
        self.assertEqual(conflict.status_code, 409)
        revoked = self._decide(
            status="revoked", transaction_id=None, evidence_reference=None, evidence_type=None,
            reason="Evidencia posterior de reverso.", request_id="decision-two",
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(self.eligibility.status, "revoked")
        self.assertEqual(ReferralEligibilityDecision.query.count(), 2)
        self.assertEqual(self._decide(status="verified", request_id="decision-three").status_code, 409)

    def test_purchase_is_evidence_not_an_automatic_transition(self):
        self.assertEqual(self.eligibility.status, "pending")
        invalid = Transaction(
            business_id=self.business.id, customer_id=self.customer.id,
            staff_id=self.manager.id, type="redeem", purchase_amount=100,
        )
        db.session.add(invalid); db.session.commit()
        response = self._decide(transaction_id=invalid.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.eligibility.status, "pending")
        self.assertEqual(ReferralEligibilityDecision.query.count(), 0)

    def test_only_first_post_attribution_purchase_can_be_verified(self):
        before = Transaction(
            business_id=self.business.id, customer_id=self.customer.id,
            staff_id=self.manager.id, type="earn", points_amount=2, purchase_amount=20,
            timestamp=self.base_time,
        )
        db.session.add(before); db.session.commit()
        self.assertEqual(self._decide(transaction_id=before.id).status_code, 409)
        self.assertEqual(self._decide().status_code, 200)

    def test_later_purchase_cannot_replace_first_eligible_purchase(self):
        first = Transaction(
            business_id=self.business.id, customer_id=self.customer.id,
            staff_id=self.manager.id, type="earn", points_amount=3, purchase_amount=30,
            timestamp=self.base_time + timedelta(seconds=1, microseconds=1),
        )
        db.session.add(first); db.session.commit()
        self.assertEqual(self._decide().status_code, 409)
        self.assertEqual(self._decide(
            transaction_id=first.id, request_id="first-purchase-decision"
        ).status_code, 200)

    def test_purchase_at_exact_attribution_timestamp_is_eligible(self):
        self.purchase.timestamp = self.attribution.created_at
        db.session.commit()

        response = self._decide(request_id="exact-attribution-boundary")

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.eligibility.source_transaction_id, self.purchase.id)

    def test_stamp_program_accepts_zero_amount_only_with_manual_receipt(self):
        self.business.loyalty_type = "stamps"
        self.purchase.points_amount = 0
        self.purchase.stamps_amount = 1
        self.purchase.purchase_amount = 0
        db.session.commit()
        self.assertEqual(self._decide().status_code, 400)
        self.assertEqual(self._decide(
            evidence_type="manual_receipt", evidence_reference="x",
            request_id="invalid-manual-receipt",
        ).status_code, 400)
        accepted = self._decide(
            evidence_type="manual_receipt", request_id="manual-stamp-receipt"
        )
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        self.assertEqual(self.eligibility.evidence_type, "manual_receipt")

    def test_permission_csrf_tenant_and_suspension_fail_closed(self):
        denied_headers = self._auth(self.denied)
        self.assertEqual(self._decide(headers=denied_headers).status_code, 403)
        without_csrf = {"Authorization": self.headers["Authorization"]}
        self.assertEqual(self._decide(headers=without_csrf).status_code, 400)
        self.assertEqual(self._decide(eligibility=self.other_eligibility).status_code, 403)
        self.promoter.is_active = False; db.session.commit()
        self.assertEqual(self._decide().status_code, 409)
        self.promoter.is_active = True; self.business.is_active = False; db.session.commit()
        self.assertIn(self._decide().status_code, {403})
        self.assertEqual(self.eligibility.status, "pending")

    def test_duplicate_economic_evidence_and_transaction_are_rejected(self):
        self.assertEqual(self._decide().status_code, 200)
        second_customer = Customer(
            business_id=self.business.id, full_name="Segundo", phone="+525533333333"
        )
        second_promoter = self._promoter(self.business, "second-promoter")
        db.session.add(second_customer); db.session.flush()
        _, second = self._eligibility(self.business, second_promoter, second_customer)
        second_purchase = Transaction(
            business_id=self.business.id, customer_id=second_customer.id,
            staff_id=self.manager.id, type="earn", purchase_amount=50,
        )
        db.session.add(second_purchase); db.session.commit()
        same_reference = self._decide(
            eligibility=second, transaction_id=second_purchase.id,
            request_id="second-decision",
        )
        self.assertEqual(same_reference.status_code, 409)
        self.assertEqual(second.status, "pending")

    def test_history_contains_no_promoter_or_customer_pii(self):
        self.assertEqual(self._decide().status_code, 200)
        serialized = " ".join(
            str(value) for row in ReferralEligibilityDecision.query.all()
            for value in (row.reason, row.evidence_reference_hash, row.request_fingerprint)
        )
        self.assertNotIn(self.customer.full_name, serialized)
        self.assertNotIn(self.customer.phone, serialized)
        self.assertNotIn(self.promoter.name, serialized)

    def test_key_rotation_keeps_old_evidence_duplicates_detectable(self):
        self.assertEqual(self._decide().status_code, 200)
        stored_hash = self.eligibility.evidence_reference_hash
        self.app.config["SECRET_KEY"] = "rotated-session-secret-that-does-not-affect-evidence"
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "phase8c1-csrf"
        replay = self._decide()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.eligibility.evidence_reference_hash, stored_hash)
        old_key = self.app.config["REFERRAL_EVIDENCE_HMAC_KEYS"]
        self.app.config["REFERRAL_EVIDENCE_HMAC_KEYS"] = (
            "new-testing-referral-evidence-key-at-least-32-bytes", old_key,
        )
        second_customer = Customer(
            business_id=self.business.id, full_name="Segundo", phone="+525566666666",
            created_at=self.base_time,
        )
        second_promoter = self._promoter(self.business, "rotated-promoter")
        db.session.add(second_customer); db.session.flush()
        _, second = self._eligibility(self.business, second_promoter, second_customer)
        second_purchase = Transaction(
            business_id=self.business.id, customer_id=second_customer.id,
            staff_id=self.manager.id, type="earn", points_amount=4, purchase_amount=40,
            timestamp=self.base_time + timedelta(seconds=2),
        )
        db.session.add(second_purchase); db.session.commit()
        response = self._decide(
            eligibility=second, transaction_id=second_purchase.id, request_id="rotated-key",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(second.status, "pending")

    def test_history_blocks_deletes_and_is_append_only(self):
        self.assertEqual(self._decide().status_code, 200)
        decision = ReferralEligibilityDecision.query.one()
        decision.reason = "Intento de edición"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        for model, identifier in (
            (ReferralAttribution, self.attribution.id),
            (ReferralEligibility, self.eligibility.id),
            (Customer, self.customer.id),
            (Promoter, self.promoter.id),
            (Business, self.business.id),
        ):
            with self.subTest(model=model.__name__):
                db.session.delete(db.session.get(model, identifier))
                with self.assertRaises((IntegrityError, SQLAlchemyError)):
                    db.session.commit()
                db.session.rollback()
                self.assertEqual(ReferralEligibilityDecision.query.count(), 1)

    def test_manager_browser_session_can_call_internal_portal_endpoint(self):
        login = self.client.post("/manager/login", data={
            "identity": self.manager.username, "password": "PasswordSeguro123!",
            "business_slug": self.business.slug,
        })
        self.assertEqual(login.status_code, 302)
        with self.client.session_transaction() as browser:
            csrf = browser["csrf_token"]
        self.app.config["CSRF_ENABLED"] = True
        missing_csrf = self.client.post(
            f"/manager/portal/referrals/{self.eligibility.id}/decision",
            json={"status": "in_review", "reason": "Revisión iniciada."},
            headers={"Idempotency-Key": "portal-missing-csrf"},
        )
        self.assertEqual(missing_csrf.status_code, 400)
        response = self.client.post(
            f"/manager/portal/referrals/{self.eligibility.id}/decision",
            json={
                "status": "verified", "transaction_id": self.purchase.id,
                "evidence_reference": "portal-ticket-0001",
                "evidence_type": "recorded_amount", "reason": "Comprobante revisado.",
            },
            headers={"X-CSRFToken": csrf, "Idempotency-Key": "portal-decision"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_concurrent_decisions_have_one_winner_and_one_audit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'eligibility.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all()
                business = Business(
                    name="Concurrente", slug="concurrente", loyalty_type="points",
                    web_access_enabled=True,
                )
                db.session.add(business); db.session.flush()
                manager = Staff(
                    business_id=business.id, username="manager", role="manager", is_active=True,
                    web_access_enabled=True, permissions="review_referrals",
                )
                manager.set_password("PasswordSeguro123!")
                promoter = Promoter(business_id=business.id, name="Promo", username="promo")
                promoter.set_password("PasswordSeguro123!")
                customer = Customer(business_id=business.id, full_name="Cliente", phone="+525544444444")
                db.session.add_all([manager, promoter, customer]); db.session.flush()
                attribution = ReferralAttribution(
                    business_id=business.id, promoter_id=promoter.id, customer_id=customer.id,
                )
                db.session.add(attribution); db.session.flush()
                eligibility = ReferralEligibility(
                    business_id=business.id, attribution_id=attribution.id,
                    promoter_id=promoter.id, customer_id=customer.id,
                )
                purchase = Transaction(
                    business_id=business.id, customer_id=customer.id, staff_id=manager.id,
                    type="earn", points_amount=8, purchase_amount=80,
                )
                db.session.add_all([eligibility, purchase]); db.session.commit()
                access, _ = issue_token_pair(manager, channel="web")
                ids = (business.id, eligibility.id, purchase.id)

            def decide(index):
                client = app.test_client()
                with client.session_transaction() as browser:
                    browser["csrf_token"] = "concurrent-csrf"
                return client.post(
                    f"/manager/business/{ids[0]}/referrals/{ids[1]}/decision",
                    json={
                        "status": "verified", "transaction_id": ids[2],
                        "evidence_reference": f"ticket-{index}",
                        "evidence_type": "recorded_amount", "reason": "Revisión concurrente.",
                    },
                    headers={
                        "Authorization": f"Bearer {access}", "X-CSRFToken": "concurrent-csrf",
                        "Idempotency-Key": f"concurrent-{index}",
                    },
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = sorted(executor.map(decide, range(2)))
            with app.app_context():
                self.assertEqual(statuses, [200, 409])
                self.assertEqual(ReferralEligibility.query.one().status, "verified")
                self.assertEqual(ReferralEligibilityDecision.query.count(), 1)
                db.session.remove()


if __name__ == "__main__":
    unittest.main()
