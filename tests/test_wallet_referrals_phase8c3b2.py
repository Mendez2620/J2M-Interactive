import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jwt
from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry,
    ReferralAttribution, ReferralEligibility, ReferralParticipant,
    ReferralParticipantAudit, Transaction,
)
from app.services import AppleWalletService, GoogleWalletService
from app.services.referral_participant_service import (
    ensure_customer_invitation, resolve_invitation,
)


class WalletReferralsPhase8C3B2TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            PUBLIC_BASE_URL="https://j2m.example",
            REGISTRATION_REQUEST_LIMIT_PHONE=50,
            REGISTRATION_REQUEST_LIMIT_IP=50,
            REFERRAL_PUBLIC_MUTATION_LIMIT=10,
            CSRF_ENABLED=True,
        )
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all(); db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.client = self.app.test_client()
        self.business = Business(
            name="Café Seguro", slug="cafe-seguro", loyalty_type="points",
            is_active=True, referrals_enabled=True,
        )
        self.other = Business(
            name="Otro", slug="otro", loyalty_type="stamps",
            is_active=True, referrals_enabled=True,
        )
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.customer = Customer(
            business_id=self.business.id, full_name="Laura Privada",
            phone="+525511111111", current_points=17,
        )
        db.session.add(self.customer); db.session.flush()
        self.participant = ensure_customer_invitation(self.customer)
        db.session.commit()
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "wallet-referral-csrf"

    def tearDown(self):
        db.session.remove(); db.session.execute(text("PRAGMA foreign_keys=OFF"))
        db.drop_all(); self.ctx.pop()

    def _activate(self, request_id="activate-wallet-referral"):
        return self.client.post(
            f"/invite/{self.participant.invitation_code}/consent",
            data={"request_id": request_id, "referral_consent": "on"},
            headers={"X-CSRFToken": "wallet-referral-csrf"},
        )

    def _registration_payload(self, phone, **extra):
        return {
            "full_name": "Cliente Nuevo", "phone_country": "+52", "phone": phone,
            "terms_accepted": True, "privacy_accepted": True, **extra,
        }

    def test_dormant_invitation_is_public_but_reveals_no_pii_and_requires_csrf(self):
        response = self.client.get(f"/invite/{self.participant.invitation_code}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Aceptar y preparar mi invitación", html)
        self.assertNotIn(self.customer.full_name, html)
        self.assertNotIn(self.customer.phone, html)
        self.assertIsNone(resolve_invitation(
            business_id=self.business.id, code=self.participant.invitation_code,
        ))
        denied = self.client.post(
            f"/invite/{self.participant.invitation_code}/consent",
            data={"request_id": "missing-csrf", "referral_consent": "on"},
        )
        self.assertEqual(denied.status_code, 400)
        db.session.refresh(self.participant)
        self.assertFalse(self.participant.is_enabled)

    def test_one_tap_consent_is_audited_and_replay_safe(self):
        first = self._activate()
        self.assertEqual(first.status_code, 200)
        db.session.refresh(self.participant)
        self.assertTrue(self.participant.currently_eligible())
        self.assertEqual(self.participant.consent_version, self.app.config["REFERRAL_CONSENT_VERSION"])
        audit = ReferralParticipantAudit.query.one()
        self.assertEqual(audit.actor_type, "public_wallet_context")
        self.assertNotIn(self.customer.phone, audit.new_state + audit.reason)
        replay = self._activate()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(ReferralParticipantAudit.query.count(), 1)

    def test_share_page_qr_and_join_create_only_pending_attribution(self):
        self.assertEqual(self._activate().status_code, 200)
        page = self.client.get(f"/invite/{self.participant.invitation_code}")
        html = page.get_data(as_text=True)
        self.assertIn("Copiar enlace", html)
        self.assertIn("Compartir por WhatsApp", html)
        self.assertIn(
            f"https://j2m.example/join/{self.business.slug}?ref={self.participant.invitation_code}",
            html.replace("&amp;", "&"),
        )
        qr = self.client.get(f"/invite/{self.participant.invitation_code}/qr.png")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.mimetype, "image/png")
        join = self.client.get(
            f"/join/{self.business.slug}?ref={self.participant.invitation_code}"
        ).get_data(as_text=True)
        context = re.search(r'name="referral_context" value="([^"]*)"', join).group(1)
        created = self.client.post(
            f"/join/{self.business.slug}",
            json=self._registration_payload("55 2222 2222", referral_context=context),
            headers={"X-CSRFToken": "wallet-referral-csrf"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(ReferralAttribution.query.count(), 1)
        self.assertEqual(ReferralEligibility.query.one().status, "pending")
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 0)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)
        db.session.refresh(self.customer)
        self.assertEqual(self.customer.current_points, 17)

    def test_new_registration_consent_is_optional_separate_and_atomic(self):
        consented = self.client.post(
            f"/join/{self.business.slug}",
            json=self._registration_payload("55 3333 3333", referral_consent=True),
            headers={"X-CSRFToken": "wallet-referral-csrf"},
        )
        self.assertEqual(consented.status_code, 201)
        first = Customer.query.filter_by(phone="+525533333333").one()
        enabled = ReferralParticipant.query.filter_by(customer_id=first.id).one()
        self.assertTrue(enabled.currently_eligible())
        plain = self.client.post(
            f"/join/{self.business.slug}",
            json=self._registration_payload("55 4444 4444", marketing_consent=True),
            headers={"X-CSRFToken": "wallet-referral-csrf"},
        )
        self.assertEqual(plain.status_code, 201)
        second = Customer.query.filter_by(phone="+525544444444").one()
        dormant = ReferralParticipant.query.filter_by(customer_id=second.id).one()
        self.assertFalse(dormant.is_enabled)
        self.assertIsNone(dormant.consented_at)
        self.assertTrue(second.marketing_consent)

    def test_revoked_suspended_cross_tenant_and_loyalty_qr_fail_closed(self):
        self.assertEqual(self._activate().status_code, 200)
        self.participant.is_enabled = False
        from datetime import datetime, timezone
        self.participant.revoked_at = datetime.now(timezone.utc)
        db.session.commit()
        page = self.client.get(f"/invite/{self.participant.invitation_code}")
        self.assertIn("fue revocada", page.get_data(as_text=True))
        self.assertEqual(self._activate("cannot-reactivate").status_code, 409)
        self.assertIsNone(resolve_invitation(
            business_id=self.other.id, code=self.participant.invitation_code,
        ))
        loyalty_qr = self.client.get(
            f"/join/{self.business.slug}?ref={self.customer.qr_code_token}"
        ).get_data(as_text=True)
        self.assertIn('name="referral_context" value=""', loyalty_qr)
        self.business.is_active = False; db.session.commit()
        self.assertEqual(
            self.client.get(f"/invite/{self.participant.invitation_code}/qr.png").status_code,
            404,
        )

    def test_wallet_objects_use_personal_link_without_changing_loyalty_barcode(self):
        self.assertEqual(self._activate().status_code, 200)
        google = GoogleWalletService().create_loyalty_object(self.customer)
        target = google["appLinkData"]["webAppLinkInfo"]["appTarget"]["targetUri"]["uri"]
        self.assertEqual(target, f"https://j2m.example/invite/{self.participant.invitation_code}")
        self.assertEqual(google["barcode"]["value"], self.customer.qr_code_token)
        token, _ = GoogleWalletService().create_jwt_pass(self.customer)
        decoded = jwt.decode(token, options={"verify_signature": False})
        self.assertIn("appLinkData", decoded["payload"]["loyaltyObjects"][0])
        apple = AppleWalletService().build_pass_json(self.customer)
        invite = [f for f in apple["storeCard"]["backFields"] if f["key"] == "invite_friends"]
        self.assertEqual(invite[0]["value"], target)
        self.assertEqual(apple["barcodes"][0]["message"], self.customer.qr_code_token)

    def test_wallet_issuance_idempotently_provisions_old_customer_without_consent(self):
        db.session.delete(self.participant); db.session.commit()
        from app.wallet_access import issue_wallet_token
        response = self.client.get(
            f"/api/customers/{self.customer.id}/wallet/google",
            query_string={"token": issue_wallet_token(self.customer)},
        )
        self.assertEqual(response.status_code, 200)
        participant = ReferralParticipant.query.filter_by(customer_id=self.customer.id).one()
        self.assertFalse(participant.is_enabled)
        self.assertIsNone(participant.consented_at)
        self.assertEqual(ReferralParticipantAudit.query.count(), 0)

    def test_concurrent_wallet_issuance_creates_one_invitation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            uri = f"sqlite:///{Path(directory, 'wallet-referral.sqlite').as_posix()}"
            app = create_app("testing")
            app.config.update(SQLALCHEMY_DATABASE_URI=uri, PUBLIC_BASE_URL="https://j2m.example")
            with app.app_context():
                db.create_all()
                business = Business(
                    name="Concurrente", slug="concurrente-wallet",
                    is_active=True, referrals_enabled=True,
                )
                db.session.add(business); db.session.flush()
                customer = Customer(
                    business_id=business.id, full_name="Cliente", phone="+525599999999",
                )
                db.session.add(customer); db.session.commit()
                customer_id = customer.id
                from app.wallet_access import issue_wallet_token
                token = issue_wallet_token(customer)

            def issue(_):
                with app.test_client() as client:
                    return client.get(
                        f"/api/customers/{customer_id}/wallet/google",
                        query_string={"token": token},
                    ).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(issue, range(2)))
            self.assertEqual(statuses, [200, 200])
            with app.app_context():
                self.assertEqual(ReferralParticipant.query.count(), 1)
                participant = ReferralParticipant.query.one()
                self.assertFalse(participant.is_enabled)
                self.assertIsNone(participant.consented_at)
                db.session.remove(); db.drop_all()


if __name__ == "__main__":
    unittest.main()
