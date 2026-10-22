import json
import unittest

from app import create_app
from app.extensions import db
from app.models import (AccessAudit, Business, Customer, CustomerRegistrationChallenge,
                        PlatformAdmin, Promoter, PromoterSession, ReferralAttribution)
from app.services.registration_email_service import process_pending_registration_emails


class PromotersPhase78TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(REGISTRATION_REQUEST_LIMIT_EMAIL=30, REGISTRATION_REQUEST_LIMIT_IP=50)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.business = Business(name="Uno", slug="uno")
        self.other = Business(name="Dos", slug="dos")
        admin = PlatformAdmin(name="Admin", email="admin@example.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add_all([self.business, self.other, admin]); db.session.commit()
        self.admin_id = admin.id
        self.promoter = Promoter(business_id=self.business.id, name="Promotora Uno", username="promo", email="promo@example.test", phone="+525500000099")
        self.promoter.set_password("PromotorSeguro123")
        db.session.add(self.promoter); db.session.commit()
        self.payload = {"full_name":"Cliente Referido", "email":"cliente@example.test", "phone_country":"+52", "phone":"55 1234 5678", "birthdate":"1990-01-02", "terms_accepted":True, "privacy_accepted":True, "referral_code":self.promoter.referral_code}

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def register(self, payload=None, slug="uno"):
        response = self.client.post(f"/join/{slug}", json=payload or self.payload)
        process_pending_registration_emails()
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        return response, self.client.post(f"/join/{slug}/verify", json={"code":code})

    def admin_login(self):
        with self.client.session_transaction() as browser:
            browser["platform_admin_id"] = self.admin_id

    def test_attribution_is_created_only_after_verified_registration(self):
        response = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(ReferralAttribution.query.count(), 0)
        process_pending_registration_emails()
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        verified = self.client.post("/join/uno/verify", json={"code":code})
        self.assertEqual(verified.status_code, 201)
        attribution = ReferralAttribution.query.one()
        self.assertEqual((attribution.business_id, attribution.promoter_id), (self.business.id, self.promoter.id))
        self.assertEqual(Customer.query.count(), 1)

    def test_invalid_cross_tenant_suspended_and_self_referrals_are_not_attributed(self):
        cases = [
            ("invalid", "cliente1@example.test", "55 0000 0001"),
        ]
        other_promoter = Promoter(business_id=self.other.id, name="Otro", username="otro")
        other_promoter.set_password("PromotorSeguro123")
        db.session.add(other_promoter); db.session.commit()
        cases.append((other_promoter.referral_code, "cliente2@example.test", "55 0000 0002"))
        self.promoter.is_active = False; db.session.commit()
        cases.append((self.promoter.referral_code, "cliente3@example.test", "55 0000 0003"))
        for code, email, phone in cases:
            payload = dict(self.payload, referral_code=code, email=email, phone=phone)
            _, verified = self.register(payload)
            self.assertEqual(verified.status_code, 201)
        self.promoter.is_active = True; db.session.commit()
        payload = dict(self.payload, email=self.promoter.email, phone="55 0000 0004")
        _, verified = self.register(payload)
        self.assertEqual(verified.status_code, 201)
        self.assertEqual(ReferralAttribution.query.count(), 0)

    def test_suspension_between_request_and_verification_prevents_attribution(self):
        self.client.post("/join/uno", json=self.payload)
        process_pending_registration_emails(); code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        self.promoter.is_active = False; db.session.commit()
        self.assertEqual(self.client.post("/join/uno/verify", json={"code":code}).status_code, 201)
        self.assertEqual(ReferralAttribution.query.count(), 0)

    def test_promoter_portal_is_separate_revocable_and_contains_no_customer_pii(self):
        _, verified = self.register()
        self.assertEqual(verified.status_code, 201)
        login = self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        self.assertEqual(login.status_code, 302)
        page = self.client.get("/promoter/portal")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Referidos verificados", html)
        self.assertNotIn("cliente@example.test", html)
        self.assertNotIn("Cliente Referido", html)
        auth_session = PromoterSession.query.one()
        self.assertIsNone(auth_session.revoked_at)
        self.assertEqual(self.client.post("/promoter/logout").status_code, 302)
        self.assertIsNotNone(db.session.get(PromoterSession, auth_session.id).revoked_at)
        self.assertEqual(self.client.get("/promoter/portal").status_code, 302)

    def test_admin_creation_duplicate_suspension_audit_and_secret_hygiene(self):
        self.admin_login()
        data = {"name":"Nueva Promotora", "username":"nueva", "email":"nueva@example.test", "password":"NuevaSegura123"}
        created = self.client.post(f"/admin/business/{self.business.id}/promoters", data=data)
        self.assertEqual(created.status_code, 302)
        duplicate = self.client.post(f"/admin/business/{self.business.id}/promoters", data=data)
        self.assertEqual(duplicate.status_code, 409)
        created_promoter = Promoter.query.filter_by(username="nueva").one()
        changed = self.client.post(f"/admin/business/{self.business.id}/promoters/{created_promoter.id}", data={"action":"toggle"})
        self.assertEqual(changed.status_code, 302)
        self.assertFalse(created_promoter.is_active)
        serialized = " ".join((audit.previous_state or "") + (audit.new_state or "") for audit in AccessAudit.query.all())
        self.assertNotIn("NuevaSegura123", serialized)
        self.assertNotIn("password_hash", serialized)
        self.assertEqual({audit.action for audit in AccessAudit.query.all()}, {"promoter_created", "promoter_suspended"})

    def test_admin_and_promoter_csrf_protection(self):
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"}).status_code, 400)
        login_page = self.client.get("/promoter/login").get_data(as_text=True)
        token = login_page.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        self.assertEqual(self.client.post("/promoter/login", data={"csrf_token":token, "business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"}).status_code, 302)

    def test_qr_and_public_link_use_stable_random_code_without_pii(self):
        self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        first = self.client.get("/promoter/portal").get_data(as_text=True)
        second = self.client.get("/promoter/portal").get_data(as_text=True)
        self.assertIn(self.promoter.referral_code, first)
        self.assertEqual(first.count(self.promoter.referral_code), second.count(self.promoter.referral_code))
        self.assertNotIn(str(self.promoter.id) + "?ref=", first)
        qr = self.client.get("/promoter/referral-qr.svg")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.mimetype, "image/svg+xml")
        self.assertNotIn(b"cliente@example.test", qr.data)


if __name__ == "__main__":
    unittest.main()
