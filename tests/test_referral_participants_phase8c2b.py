import re
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry,
    ReferralAttribution, ReferralEligibility, ReferralParticipant,
    ReferralParticipantAudit, ReferralIncentiveRule, Staff, Transaction,
)
from app.services.referral_participant_service import (
    ReferralParticipantError, configure_customer_ambassador,
    configure_staff_referrals, resolve_invitation,
)
from app.staff_auth import issue_token_pair
from app.services.referral_incentive_service import preview_incentive


class ReferralParticipantsPhase8C2BTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(REGISTRATION_REQUEST_LIMIT_PHONE=50, REGISTRATION_REQUEST_LIMIT_IP=50)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all(); db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.client = self.app.test_client()
        self.business = Business(
            name="Uno", slug="uno-8c2b", loyalty_type="points",
            is_active=True, web_access_enabled=True,
        )
        self.other = Business(
            name="Dos", slug="dos-8c2b", loyalty_type="stamps",
            is_active=True, web_access_enabled=True,
        )
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.manager = self._staff(
            self.business, "manager", "manager",
            "manage_referral_participants,refer_customers",
        )
        self.worker = self._staff(
            self.business, "worker", "cashier", "refer_customers"
        )
        self.denied = self._staff(self.business, "denied", "cashier", "scanner_operate")
        self.customer = Customer(
            business_id=self.business.id, full_name="Laura",
            phone="+525511111111",
        )
        db.session.add(self.customer); db.session.commit()
        access, _ = issue_token_pair(self.manager, channel="web")
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "phase8c2b-csrf"
        self.headers = {
            "Authorization": f"Bearer {access}",
            "X-CSRFToken": "phase8c2b-csrf",
            "Idempotency-Key": "enable-worker",
        }

    def tearDown(self):
        db.session.remove(); db.session.execute(text("PRAGMA foreign_keys=OFF")); db.drop_all(); self.ctx.pop()

    def _staff(self, business, username, role, permissions):
        staff = Staff(
            business_id=business.id, username=username, role=role,
            permissions=permissions, is_active=True, web_access_enabled=True,
        )
        staff.set_password("PasswordSeguro123!")
        db.session.add(staff); db.session.flush()
        return staff

    def _enable_worker(self):
        return self.client.post(
            f"/manager/business/{self.business.id}/staff/{self.worker.id}/referrals",
            json={"enabled": True, "reason": "Participación autorizada."},
            headers=self.headers,
        )

    def _registration_payload(self, context):
        return {
            "full_name": "Carlos", "phone_country": "+52", "phone": "55 2222 2222",
            "terms_accepted": True, "privacy_accepted": True,
            "referral_context": context,
        }

    def test_staff_enable_is_explicit_audited_idempotent_and_self_enable_is_forbidden(self):
        first = self._enable_worker()
        replay = self._enable_worker()
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        participant = ReferralParticipant.query.filter_by(staff_id=self.worker.id).one()
        self.assertTrue(participant.is_enabled)
        self.assertGreaterEqual(len(participant.invitation_code), 20)
        self.assertEqual(ReferralParticipantAudit.query.count(), 1)
        self.assertTrue(replay.get_json()["idempotent_replay"])
        self.assertIsNone(ReferralParticipant.query.filter_by(staff_id=self.denied.id).first())
        self.headers["Idempotency-Key"] = "self-enable"
        denied = self.client.post(
            f"/manager/business/{self.business.id}/staff/{self.manager.id}/referrals",
            json={"enabled": True, "reason": "No permitido."}, headers=self.headers,
        )
        self.assertEqual(denied.status_code, 403)

    def test_staff_invitation_registers_pending_attribution_without_economic_effects(self):
        self.assertEqual(self._enable_worker().status_code, 200)
        participant = ReferralParticipant.query.filter_by(staff_id=self.worker.id).one()
        page = self.client.get(
            f"/join/{self.business.slug}?ref={participant.invitation_code}"
        ).get_data(as_text=True)
        context = re.search(r'name="referral_context" value="([^"]*)"', page).group(1)
        response = self.client.post(
            f"/join/{self.business.slug}", json=self._registration_payload(context)
        )
        self.assertEqual(response.status_code, 201)
        attribution = ReferralAttribution.query.one()
        eligibility = ReferralEligibility.query.one()
        self.assertEqual(attribution.participant_id, participant.id)
        self.assertIsNone(attribution.promoter_id)
        self.assertEqual(eligibility.status, "pending")
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 0)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)

    def test_disabled_suspended_permissionless_and_cross_tenant_staff_codes_fail_closed(self):
        self._enable_worker(); participant = ReferralParticipant.query.filter_by(staff_id=self.worker.id).one()
        self.assertIsNotNone(resolve_invitation(business_id=self.business.id, code=participant.invitation_code))
        self.assertIsNone(resolve_invitation(business_id=self.other.id, code=participant.invitation_code))
        self.worker.permissions = "scanner_operate"; db.session.commit()
        self.assertIsNone(resolve_invitation(business_id=self.business.id, code=participant.invitation_code))
        self.worker.permissions = "refer_customers"; self.worker.is_active = False; db.session.commit()
        self.assertIsNone(resolve_invitation(business_id=self.business.id, code=participant.invitation_code))
        self.worker.is_active = True; participant.is_enabled = False; db.session.commit()
        self.assertIsNone(resolve_invitation(business_id=self.business.id, code=participant.invitation_code))

    def test_customer_ambassador_requires_strong_authorization_and_specific_consent(self):
        with self.assertRaises(ReferralParticipantError):
            configure_customer_ambassador(
                customer=self.customer, enabled=True, consent_version="v1",
                consent_evidence="evidence-bound-to-future-auth",
                actor_type="customer", actor_id=self.customer.id,
                reason="Consentimiento voluntario.", request_id="customer-consent",
                authorization_verified=False,
            )
        self.assertIsNone(ReferralParticipant.query.filter_by(customer_id=self.customer.id).first())
        participant, _ = configure_customer_ambassador(
            customer=self.customer, enabled=True, consent_version="v1",
            consent_evidence="evidence-bound-to-future-auth",
            actor_type="customer", actor_id=self.customer.id,
            reason="Consentimiento voluntario.", request_id="customer-consent",
            authorization_verified=True,
        )
        self.assertTrue(participant.currently_eligible())
        configure_customer_ambassador(
            customer=self.customer, enabled=False, consent_version=None,
            consent_evidence=None, actor_type="customer", actor_id=self.customer.id,
            reason="Revocación solicitada.", request_id="customer-revoke",
            authorization_verified=True,
        )
        self.assertFalse(participant.currently_eligible())
        self.assertEqual(ReferralParticipantAudit.query.count(), 2)

    def test_customer_public_emission_endpoint_does_not_exist(self):
        response = self.client.post(
            f"/join/{self.business.slug}/ambassador",
            json={"customer_id": self.customer.id},
        )
        self.assertEqual(response.status_code, 404)

    def test_staff_rule_preview_is_dry_run_and_uses_typed_identity(self):
        self._enable_worker(); participant = ReferralParticipant.query.filter_by(staff_id=self.worker.id).one()
        referred = Customer(
            business_id=self.business.id, full_name="Referido", phone="+525533333333",
            current_points=0, current_stamps=0,
        )
        db.session.add(referred); db.session.flush()
        attribution = ReferralAttribution(
            business_id=self.business.id, participant_id=participant.id,
            customer_id=referred.id,
        )
        db.session.add(attribution); db.session.flush()
        purchase = Transaction(
            business_id=self.business.id, customer_id=referred.id,
            staff_id=self.manager.id, type="earn", points_amount=1,
            purchase_amount=100,
        )
        db.session.add(purchase); db.session.flush()
        eligibility = ReferralEligibility(
            business_id=self.business.id, attribution_id=attribution.id,
            participant_id=participant.id, customer_id=referred.id,
            status="verified", source_transaction_id=purchase.id,
            evidence_reference_hash="e" * 64,
        )
        rule = ReferralIncentiveRule(
            business_id=self.business.id, beneficiary_type="staff",
            loyalty_program="points", reward_type="points",
            reward_amount=Decimal("5.000000"),
            eligible_condition="first_verified_purchase", status="draft", version=1,
            created_by_type="staff", created_by_id=self.manager.id,
            updated_by_type="staff", updated_by_id=self.manager.id,
        )
        db.session.add_all([eligibility, rule]); db.session.commit()
        before = (Transaction.query.count(), LoyaltyLedgerEntry.query.count(), GoogleWalletSyncJob.query.count())
        result = preview_incentive(
            business=self.business, eligibility_id=eligibility.id, rule_id=rule.id,
        )
        self.assertEqual(result.outcome, "eligible_for_proposal")
        self.assertEqual(result.beneficiary_id, self.worker.id)
        self.assertEqual(
            (Transaction.query.count(), LoyaltyLedgerEntry.query.count(), GoogleWalletSyncJob.query.count()),
            before,
        )


if __name__ == "__main__":
    unittest.main()
