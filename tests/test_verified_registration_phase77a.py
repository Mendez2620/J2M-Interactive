import unittest
import re
from datetime import timedelta

from app import create_app
from app.extensions import db
from app.email_service import MailDeliveryError
from app.models import (Business, Customer, CustomerRegistrationChallenge,
                        CustomerRegistrationIdentityClaim)


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
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"] if self.app.extensions["recovery_mailer"].outbox else None
        return response, code

    def test_customer_and_wallet_are_created_only_after_email_verification(self):
        response, code = self.request_code()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "verification_required")
        self.assertEqual(Customer.query.count(), 0)
        challenge = CustomerRegistrationChallenge.query.one()
        self.assertNotEqual(challenge.code_hash, code)
        self.assertNotIn(code, challenge.code_hash)
        result = self.client.post("/join/uno/verify", json={"code": code})
        self.assertEqual(result.status_code, 201)
        customer = Customer.query.one()
        self.assertIsNotNone(customer.email_verified_at)
        self.assertIsNotNone(customer.terms_accepted_at)
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


if __name__ == "__main__":
    unittest.main()
