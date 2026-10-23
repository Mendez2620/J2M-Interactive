import unittest
import re
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta

from app import create_app
from app.extensions import db
from app.email_service import MailDeliveryError
from app.models import (Business, Customer, CustomerRegistrationChallenge,
                        CustomerRegistrationIdentityClaim)
from app.config import TestingConfig


class VerifiedRegistrationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(REGISTRATION_REQUEST_LIMIT_EMAIL=20, REGISTRATION_REQUEST_LIMIT_IP=50)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.add_all([Business(name="Uno", slug="uno"), Business(name="Dos", slug="dos")]); db.session.commit()
        self.payload = {"full_name": "Ana Prueba", "email": "ana@example.test", "phone_country": "+52", "phone": "55 1234 5678", "birthdate": "1992-04-05", "terms_accepted": True, "privacy_accepted": True}

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def request_code(self, payload=None, slug="uno"):
        response = self.client.post(f"/join/{slug}", json=payload or self.payload)
        code = self.app.extensions["recovery_mailer"].outbox[-1].get("code") if self.app.extensions["recovery_mailer"].outbox else None
        return response, code

    def test_customer_and_wallet_are_created_only_after_email_verification(self):
        response, code = self.request_code()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "accepted")
        self.assertEqual(Customer.query.count(), 0)
        challenge = CustomerRegistrationChallenge.query.one()
        self.assertNotEqual(challenge.code_hash, code)
        self.assertNotIn(code, challenge.code_hash)
        result = self.client.post("/join/uno/verify", json={"code": code})
        self.assertEqual(result.status_code, 201)
        customer = Customer.query.one()
        self.assertIsNotNone(customer.email_verified_at)
        self.assertIsNotNone(customer.terms_accepted_at)
        self.assertLess(customer.terms_accepted_at, customer.email_verified_at)
        self.assertEqual(len(customer.terms_content_hash), 64)
        self.assertEqual(len(customer.privacy_content_hash), 64)
        self.assertFalse(customer.marketing_consent)
        self.assertIn("wallet", result.get_json()["apple_wallet_url"])
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": code}).status_code, 400)
        self.assertEqual(Customer.query.count(), 1)

    def test_required_validation_and_impossible_dates(self):
        for changes in ({"birthdate": ""}, {"birthdate": "2035-01-01"}, {"birthdate": "2020-02-31"}, {"privacy_accepted": False}, {"phone": ""}, {"email": "bad"}):
            payload = dict(self.payload); payload.update(changes)
            self.assertEqual(self.client.post("/join/uno", json=payload).status_code, 400)
        self.assertEqual(Customer.query.count(), 0)

    def test_expiry_attempts_single_use_and_tenant_isolation(self):
        _, code = self.request_code()
        challenge = CustomerRegistrationChallenge.query.one()
        challenge.expires_at -= timedelta(hours=1); db.session.commit()
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": code}).status_code, 400)
        self.assertEqual(self.client.post("/join/dos/verify", json={"code": code}).status_code, 400)
        self.assertEqual(Customer.query.count(), 0)

    def test_duplicates_are_generic_and_do_not_modify_existing_customer(self):
        business = Business.query.filter_by(slug="uno").one()
        original = Customer(business_id=business.id, full_name="Existente", email=self.payload["email"], phone="+525500000000")
        db.session.add(original); db.session.commit()
        response, code = self.request_code()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "accepted")
        self.assertIsNone(code)
        self.assertEqual(Customer.query.count(), 1)
        self.assertEqual(Customer.query.one().full_name, "Existente")

    def test_optional_marketing_consent_is_recorded_separately(self):
        payload = dict(self.payload); payload["marketing_consent"] = True
        _, code = self.request_code(payload)
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": code}).status_code, 201)
        customer = Customer.query.one()
        self.assertTrue(customer.marketing_consent)
        self.assertIsNotNone(customer.marketing_consent_at)
        self.assertEqual(customer.terms_version, self.app.config["TERMS_VERSION"])

    def test_attempt_limit_and_csrf_protection(self):
        _, code = self.request_code()
        self.app.config["REGISTRATION_MAX_ATTEMPTS"] = 2
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "11111111"}).status_code, 400)
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "22222222"}).status_code, 400)
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": code}).status_code, 400)
        self.assertEqual(Customer.query.count(), 0)

        self.app.config["CSRF_ENABLED"] = True
        denied = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(denied.status_code, 400)
        page = self.client.get("/join/uno").get_data(as_text=True)
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
        allowed = self.client.post("/join/uno", json=self.payload, headers={"X-CSRFToken": token})
        self.assertEqual(allowed.status_code, 202)

    def test_database_claim_prevents_concurrent_identity_completion(self):
        _, code = self.request_code()
        challenge = CustomerRegistrationChallenge.query.one()
        db.session.add(CustomerRegistrationIdentityClaim(
            business_id=challenge.business_id, identity_type="email",
            identity_hash=challenge.email_hash,
        ))
        db.session.commit()
        result = self.client.post("/join/uno/verify", json={"code": code})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(Customer.query.count(), 0)
        db.session.refresh(challenge)
        self.assertIsNotNone(challenge.consumed_at)

    def test_rate_limit_and_mail_failure_never_create_customer(self):
        self.app.config["REGISTRATION_REQUEST_LIMIT_EMAIL"] = 1
        first, _ = self.request_code()
        self.assertEqual(first.status_code, 202)
        second, _ = self.request_code()
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.get_json()["message"], first.get_json()["message"])
        self.assertEqual(len(self.app.extensions["recovery_mailer"].outbox), 1)
        self.assertEqual(Customer.query.count(), 0)

        mailer = self.app.extensions["recovery_mailer"]
        original = mailer.send_registration_code
        mailer.send_registration_code = lambda *args: (_ for _ in ()).throw(MailDeliveryError("fallo"))
        payload = dict(self.payload); payload.update(email="otra@example.test", phone="55 2222 2222")
        response = self.client.post("/join/uno", json=payload)
        mailer.send_registration_code = original
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Customer.query.count(), 0)

    def test_new_existing_and_limited_requests_have_identical_public_response(self):
        self.app.config["REGISTRATION_RESPONSE_MIN_MS"] = 20
        started = time.monotonic(); fresh, _ = self.request_code(); fresh_elapsed = time.monotonic() - started
        business = Business.query.filter_by(slug="uno").one()
        db.session.add(Customer(business_id=business.id, full_name="Existe", email="existe@example.test", phone="+525500000001")); db.session.commit()
        duplicate_payload = dict(self.payload); duplicate_payload.update(email="existe@example.test", phone="55 0000 0001")
        started = time.monotonic(); duplicate, _ = self.request_code(duplicate_payload); duplicate_elapsed = time.monotonic() - started
        self.app.config["REGISTRATION_REQUEST_LIMIT_IP"] = 0
        limited_payload = dict(self.payload); limited_payload.update(email="limited@example.test", phone="55 0000 0002")
        started = time.monotonic(); limited, _ = self.request_code(limited_payload); limited_elapsed = time.monotonic() - started
        self.assertEqual((fresh.status_code, fresh.get_json()), (duplicate.status_code, duplicate.get_json()))
        self.assertEqual((fresh.status_code, fresh.get_json()), (limited.status_code, limited.get_json()))
        self.assertTrue(all(value >= 0.01 for value in (fresh_elapsed, duplicate_elapsed, limited_elapsed)))
        notices = [item for item in self.app.extensions["recovery_mailer"].outbox if item.get("purpose") == "registration_notice"]
        self.assertEqual(len(notices), 1)
        self.assertNotIn("code", notices[0])

    def test_suspension_after_code_request_blocks_completion(self):
        _, code = self.request_code()
        business = Business.query.filter_by(slug="uno").one()
        business.is_active = False; db.session.commit()
        result = self.client.post("/join/uno/verify", json={"code": code})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(Customer.query.count(), 0)
        self.assertNotIn("wallet", result.get_data(as_text=True).lower())

    def test_secret_key_rotation_does_not_change_identity_claim_hashes(self):
        _, code = self.request_code()
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": code}).status_code, 201)
        hashes = {claim.identity_hash for claim in CustomerRegistrationIdentityClaim.query.all()}
        self.app.config["SECRET_KEY"] = "rotated-session-secret-key-with-more-than-32-characters"
        from app.routes.business import _identity_hmac
        self.assertIn(_identity_hmac("ana@example.test"), hashes)
        self.assertIn(_identity_hmac("+525512345678"), hashes)

    def test_concurrent_consumption_of_same_code_creates_one_customer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'registration.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all(); db.session.add(Business(name="Concurrente", slug="concurrente")); db.session.commit()
            source = app.test_client()
            response = source.post("/join/concurrente", json=self.payload)
            self.assertEqual(response.status_code, 202)
            code = app.extensions["recovery_mailer"].outbox[-1]["code"]
            with source.session_transaction() as browser:
                challenge_id = browser["customer_registration_challenge_id"]

            def verify_once(_):
                client = app.test_client()
                with client.session_transaction() as browser:
                    browser["customer_registration_challenge_id"] = challenge_id
                return client.post("/join/concurrente/verify", json={"code": code}).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(verify_once, range(2)))
            with app.app_context():
                self.assertEqual(Customer.query.count(), 1)
                self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 2)
                db.session.remove()
            self.assertEqual(statuses.count(201), 1)
            self.assertEqual(len([status for status in statuses if status in {400, 409}]), 1)

    def test_concurrent_verification_cannot_claim_same_phone_twice(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'registration-phone.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all(); db.session.add(Business(name="Concurrente", slug="concurrente")); db.session.commit()

            registrations = []
            for index in range(2):
                client = app.test_client()
                payload = dict(self.payload, email=f"persona{index}@example.test")
                response = client.post("/join/concurrente", json=payload)
                self.assertEqual(response.status_code, 202)
                code = app.extensions["recovery_mailer"].outbox[-1]["code"]
                with client.session_transaction() as browser:
                    challenge_id = browser["customer_registration_challenge_id"]
                registrations.append((challenge_id, code))

            def verify_once(registration):
                challenge_id, code = registration
                client = app.test_client()
                with client.session_transaction() as browser:
                    browser["customer_registration_challenge_id"] = challenge_id
                return client.post("/join/concurrente/verify", json={"code": code}).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(verify_once, registrations))
            with app.app_context():
                self.assertEqual(Customer.query.count(), 1)
                self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 2)
                db.session.remove()
            self.assertEqual(statuses.count(201), 1)
            self.assertEqual(statuses.count(409), 1)


if __name__ == "__main__":
    unittest.main()
