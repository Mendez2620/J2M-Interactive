import hashlib
import re
import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import (AccessAudit, Business, Customer, CustomerRegistrationChallenge,
                        PlatformAdmin, Promoter, PromoterSession, ReferralAttribution)
from app.services.registration_email_service import process_pending_registration_emails
from app.config import TestingConfig
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from app.routes.promoter import _referral_url


class PromotersPhase78TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(REGISTRATION_REQUEST_LIMIT_EMAIL=30, REGISTRATION_REQUEST_LIMIT_IP=50)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.business = Business(name="Uno", slug="uno")
        self.other = Business(name="Dos", slug="dos")
        admin = PlatformAdmin(name="Admin", email="admin@example.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add_all([self.business, self.other, admin]); db.session.commit()
        self.admin_id = admin.id
        self.promoter = Promoter(business_id=self.business.id, name="Promotora Uno", username="promo", email="promo@example.test", phone="+525500000099")
        self.promoter.set_password("PromotorSeguro123")
        db.session.add(self.promoter); db.session.commit()
        self.payload = {"full_name":"Cliente Referido", "email":"cliente@example.test", "phone_country":"+52", "phone":"55 1234 5678", "birthdate":"1990-01-02", "terms_accepted":True, "privacy_accepted":True}

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def register(self, payload=None, slug="uno"):
        response = self.client.post(f"/join/{slug}", json=payload or self.referred_payload(slug))
        process_pending_registration_emails()
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        return response, self.client.post(f"/join/{slug}/verify", json={"code":code})

    def referred_payload(self, slug="uno", promoter=None):
        promoter = promoter or self.promoter
        page = self.client.get(f"/join/{slug}?ref={promoter.referral_code}").get_data(as_text=True)
        context = re.search(r'name="referral_context" value="([^"]*)"', page).group(1)
        return dict(self.payload, referral_context=context)

    def admin_login(self):
        with self.client.session_transaction() as browser:
            browser["platform_admin_id"] = self.admin_id

    def test_attribution_is_created_only_after_verified_registration(self):
        response = self.client.post("/join/uno", json=self.referred_payload())
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
        payload = self.referred_payload()
        payload.update(email=self.promoter.email, phone="55 0000 0004")
        _, verified = self.register(payload)
        self.assertEqual(verified.status_code, 201)
        self.assertEqual(ReferralAttribution.query.count(), 0)

    def test_suspension_between_request_and_verification_prevents_attribution(self):
        self.client.post("/join/uno", json=self.referred_payload())
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

    def test_session_verifier_is_required_constant_length_and_revocable(self):
        self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        auth_session = PromoterSession.query.one()
        with self.client.session_transaction() as browser:
            verifier = browser["promoter_session_verifier"]
            self.assertEqual(auth_session.verifier_hash, hashlib.sha256(verifier.encode()).hexdigest())
            browser["promoter_session_verifier"] = "manipulado"
        self.assertEqual(self.client.get("/promoter/portal").status_code, 302)

        self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        with self.client.session_transaction() as browser:
            browser.pop("promoter_session_verifier")
        self.assertEqual(self.client.get("/promoter/portal").status_code, 302)

        self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        latest = PromoterSession.query.order_by(PromoterSession.created_at.desc()).first()
        latest.expires_at -= timedelta(hours=2); db.session.commit()
        self.assertEqual(self.client.get("/promoter/portal").status_code, 302)

    def test_suspension_and_password_reset_revoke_all_promoter_sessions(self):
        self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        auth_session = PromoterSession.query.one()
        admin_client = self.app.test_client()
        with admin_client.session_transaction() as browser:
            browser["platform_admin_id"] = self.admin_id
        response = admin_client.post(
            f"/admin/business/{self.business.id}/promoters/{self.promoter.id}", data={"action":"toggle"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(PromoterSession, auth_session.id).revoked_at)
        self.assertEqual(self.client.get("/promoter/portal").status_code, 302)
        self.promoter.is_active = True; db.session.commit()
        self.client.post("/promoter/login", data={"business_slug":"uno", "identity":"promo", "password":"PromotorSeguro123"})
        password_session = PromoterSession.query.order_by(PromoterSession.created_at.desc()).first()
        reset = admin_client.post(
            f"/admin/business/{self.business.id}/promoters/{self.promoter.id}",
            data={"action":"password", "password":"PromotorNuevo123"},
        )
        self.assertEqual(reset.status_code, 302)
        self.assertIsNotNone(db.session.get(PromoterSession, password_session.id).revoked_at)

    def test_admin_qr_is_tenant_scoped_and_host_header_cannot_poison_links(self):
        other_promoter = Promoter(business_id=self.other.id, name="Otro", username="otro")
        other_promoter.set_password("PromotorSeguro123")
        db.session.add(other_promoter); db.session.commit(); self.admin_login()
        denied = self.client.get(f"/admin/business/{self.business.id}/promoters/{other_promoter.id}/qr.svg")
        self.assertEqual(denied.status_code, 404)
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        self.client.set_cookie("session", serializer.dumps({"platform_admin_id":self.admin_id}), domain="attacker.example")
        page = self.client.get(
            f"/admin/business/{self.business.id}/promoters", base_url="http://attacker.example"
        ).get_data(as_text=True)
        self.assertNotIn("attacker.example", page)
        self.assertIn(f"http://localhost/join/uno?ref={self.promoter.referral_code}", page)

    def test_production_referral_url_requires_canonical_https_origin(self):
        self.app.config.update(APP_ENV="production", PUBLIC_BASE_URL="http://inseguro.example")
        with self.app.test_request_context("/", headers={"Host":"attacker.example"}):
            with self.assertRaises(RuntimeError):
                _referral_url(self.promoter)
        self.app.config["PUBLIC_BASE_URL"] = "https://j2m.example"
        with self.app.test_request_context("/", headers={"Host":"attacker.example"}):
            self.assertEqual(
                _referral_url(self.promoter),
                f"https://j2m.example/join/uno?ref={self.promoter.referral_code}",
            )

    def test_signed_referral_context_cannot_be_replaced_during_resend(self):
        other = Promoter(business_id=self.business.id, name="Otra", username="otra")
        other.set_password("PromotorSeguro123")
        db.session.add(other); db.session.commit()
        first = self.referred_payload(promoter=self.promoter)
        self.assertEqual(self.client.post("/join/uno", json=first).status_code, 202)
        altered = dict(self.payload, referral_context=self.referred_payload(promoter=other)["referral_context"])
        self.assertEqual(self.client.post("/join/uno", json=altered).status_code, 202)
        challenge = CustomerRegistrationChallenge.query.order_by(CustomerRegistrationChallenge.created_at.desc()).first()
        self.assertEqual(challenge.promoter_id, self.promoter.id)

    def test_new_identity_in_shared_browser_uses_its_own_referrer(self):
        other = Promoter(business_id=self.business.id, name="Otra", username="otra")
        other.set_password("PromotorSeguro123")
        db.session.add(other); db.session.commit()
        self.assertEqual(self.client.post("/join/uno", json=self.referred_payload()).status_code, 202)
        second = dict(
            self.payload,
            email="segunda@example.test",
            phone="55 9999 1111",
            referral_context=self.referred_payload(promoter=other)["referral_context"],
        )
        self.assertEqual(self.client.post("/join/uno", json=second).status_code, 202)
        process_pending_registration_emails()
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": code}).status_code, 201)
        attribution = ReferralAttribution.query.one()
        self.assertEqual(attribution.promoter_id, other.id)

    def test_referral_lock_requires_same_complete_identity(self):
        other = Promoter(business_id=self.business.id, name="Otra", username="otra")
        other.set_password("PromotorSeguro123")
        db.session.add(other); db.session.commit()
        other_context = self.referred_payload(promoter=other)["referral_context"]
        cases = (
            {"email": self.payload["email"], "phone": "55 9999 0001"},
            {"email": "otra-identidad@example.test", "phone": self.payload["phone"]},
        )
        for index, changed in enumerate(cases):
            client = self.app.test_client()
            first_context = re.search(
                r'name="referral_context" value="([^"]*)"',
                client.get(f"/join/uno?ref={self.promoter.referral_code}").get_data(as_text=True),
            ).group(1)
            initial = dict(self.payload, email=f"base{index}@example.test", phone=f"55 0000 010{index}", referral_context=first_context)
            self.assertEqual(client.post("/join/uno", json=initial).status_code, 202)
            changed = dict(changed, full_name="Identidad Nueva", birthdate="1990-01-02",
                           terms_accepted=True, privacy_accepted=True, referral_context=other_context)
            if index == 0:
                changed["email"] = initial["email"]
            else:
                changed["phone"] = initial["phone"]
            self.assertEqual(client.post("/join/uno", json=changed).status_code, 202)
            latest = CustomerRegistrationChallenge.query.order_by(CustomerRegistrationChallenge.created_at.desc()).first()
            self.assertEqual(latest.promoter_id, other.id)

    def test_expired_consumed_exhausted_or_untrusted_challenge_does_not_lock_referral(self):
        other = Promoter(business_id=self.business.id, name="Otra", username="otra")
        other.set_password("PromotorSeguro123")
        db.session.add(other); db.session.commit()
        states = ("expired", "consumed", "exhausted", "invalid-proof")
        for state in states:
            client = self.app.test_client()
            page = client.get(f"/join/uno?ref={self.promoter.referral_code}").get_data(as_text=True)
            first_context = re.search(r'name="referral_context" value="([^"]*)"', page).group(1)
            payload = dict(self.payload, email=f"{state}@example.test", phone=f"55 1000 000{states.index(state)}", referral_context=first_context)
            self.assertEqual(client.post("/join/uno", json=payload).status_code, 202)
            challenge = CustomerRegistrationChallenge.query.order_by(CustomerRegistrationChallenge.created_at.desc()).first()
            if state == "expired":
                challenge.expires_at -= timedelta(hours=1)
            elif state == "consumed":
                challenge.consumed_at = challenge.created_at
            elif state == "exhausted":
                challenge.attempts = self.app.config["REGISTRATION_MAX_ATTEMPTS"]
            else:
                with client.session_transaction() as browser:
                    browser["customer_registration_challenge_proof"] = "0" * 64
            db.session.commit()
            other_page = client.get(f"/join/uno?ref={other.referral_code}").get_data(as_text=True)
            other_context = re.search(r'name="referral_context" value="([^"]*)"', other_page).group(1)
            self.assertEqual(client.post("/join/uno", json=dict(payload, referral_context=other_context)).status_code, 202)
            latest = CustomerRegistrationChallenge.query.order_by(CustomerRegistrationChallenge.created_at.desc()).first()
            self.assertEqual(latest.promoter_id, other.id)

    def test_tampered_context_and_unsigned_code_are_ignored(self):
        tampered = dict(
            self.payload,
            referral_code=self.promoter.referral_code,
            referral_context=self.referred_payload()["referral_context"] + "alterado",
        )
        _, verified = self.register(tampered)
        self.assertEqual(verified.status_code, 201)
        self.assertEqual(ReferralAttribution.query.count(), 0)

    def test_admin_period_statistics_and_privacy(self):
        _, verified = self.register()
        self.assertEqual(verified.status_code, 201)
        attribution = ReferralAttribution.query.one()
        attribution.created_at -= timedelta(days=20); db.session.commit(); self.admin_login()
        seven = self.client.get(f"/admin/business/{self.business.id}/promoters?period=7").get_data(as_text=True)
        all_time = self.client.get(f"/admin/business/{self.business.id}/promoters?period=all").get_data(as_text=True)
        self.assertIn("Periodo seleccionado</p><p class=\"mt-1 text-3xl font-black text-indigo-700\">0", seven)
        self.assertIn("Periodo seleccionado</p><p class=\"mt-1 text-3xl font-black text-indigo-700\">1", all_time)
        for private in ("cliente@example.test", "Cliente Referido", "+525512345678"):
            self.assertNotIn(private, all_time)

    def test_database_rejects_cross_tenant_attribution(self):
        customer = Customer(business_id=self.other.id, full_name="Histórico", email="historico@example.test")
        db.session.add(customer); db.session.commit()
        db.session.add(ReferralAttribution(
            business_id=self.business.id, promoter_id=self.promoter.id, customer_id=customer.id
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_concurrent_verification_creates_one_attribution(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'referrals.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all()
                business = Business(name="Concurrente", slug="concurrente")
                db.session.add(business); db.session.flush()
                promoter = Promoter(business_id=business.id, name="Promotor", username="promo")
                promoter.set_password("PromotorSeguro123"); db.session.add(promoter); db.session.commit()
                referral_code = promoter.referral_code
            source = app.test_client()
            page = source.get(f"/join/concurrente?ref={referral_code}").get_data(as_text=True)
            context = re.search(r'name="referral_context" value="([^"]*)"', page).group(1)
            payload = dict(self.payload, referral_context=context)
            self.assertEqual(source.post("/join/concurrente", json=payload).status_code, 202)
            with app.app_context(): process_pending_registration_emails()
            code = app.extensions["recovery_mailer"].outbox[-1]["code"]
            with source.session_transaction() as browser:
                challenge_id = browser["customer_registration_challenge_id"]
                proof = browser["customer_registration_challenge_proof"]
            def verify(_index):
                client = app.test_client()
                with client.session_transaction() as browser:
                    browser["customer_registration_challenge_id"] = challenge_id
                    browser["customer_registration_challenge_proof"] = proof
                return client.post("/join/concurrente/verify", json={"code":code}).status_code
            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(verify, range(2)))
            with app.app_context():
                self.assertEqual(Customer.query.count(), 1)
                self.assertEqual(ReferralAttribution.query.count(), 1)
                db.session.remove()
            self.assertIn(201, statuses)


if __name__ == "__main__":
    unittest.main()
