import unittest

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, CustomerRecoveryAuthorization,
    CustomerRegistrationChallenge, CustomerRegistrationIdentityClaim,
    RegistrationEmailJob,
)


class Phase8ATestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            REGISTRATION_REQUEST_LIMIT_PHONE=20,
            REGISTRATION_REQUEST_LIMIT_IP=40,
        )
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.one = Business(name="Uno", slug="uno", loyalty_type="points")
        self.two = Business(name="Dos", slug="dos")
        db.session.add_all([self.one, self.two]); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    @staticmethod
    def payload(phone="55 1234 5678", **changes):
        data = {
            "full_name": "Cliente Nuevo", "phone_country": "+52", "phone": phone,
            "terms_accepted": True, "privacy_accepted": True,
        }
        data.update(changes)
        return data

    def test_direct_registration_has_no_email_or_otp_dependency(self):
        response = self.client.post("/join/uno", json=self.payload(email="ignored@example.test"))
        self.assertEqual(response.status_code, 201)
        customer = Customer.query.one()
        self.assertIsNone(customer.email)
        self.assertIsNone(customer.email_verified_at)
        self.assertEqual(customer.phone, "+525512345678")
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])
        self.assertIn("apple_wallet_url", response.get_json())

    def test_public_form_has_no_email_code_or_required_birthdate(self):
        html = self.client.get("/join/uno").get_data(as_text=True)
        self.assertNotIn('name="email"', html)
        self.assertNotIn("verification-form", html)
        self.assertNotIn('name="birthdate" required', html)

    def test_duplicate_is_rejected_without_mutation_and_claim_is_tenant_scoped(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload()).status_code, 201)
        customer = Customer.query.one(); original_token = customer.qr_code_token
        self.assertEqual(self.client.post("/join/uno", json=self.payload(full_name="Atacante")).status_code, 409)
        db.session.refresh(customer)
        self.assertEqual((customer.full_name, customer.qr_code_token), ("Cliente Nuevo", original_token))
        self.assertEqual(self.client.post("/join/dos", json=self.payload()).status_code, 201)
        self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 2)

    def test_validation_consent_and_optional_fields(self):
        for field in ("full_name", "phone", "terms_accepted", "privacy_accepted"):
            data = self.payload(phone=f"55 1000 0{len(field):03d}"); data.pop(field)
            self.assertEqual(self.client.post("/join/uno", json=data).status_code, 400)
        response = self.client.post("/join/uno", json=self.payload(
            "55 9999 0001", birthdate="1998-05-15", marketing_consent=True,
        ))
        self.assertEqual(response.status_code, 201)
        customer = Customer.query.one()
        self.assertTrue(customer.marketing_consent)
        self.assertIsNotNone(customer.terms_content_hash)
        self.assertIsNotNone(customer.privacy_content_hash)

    def test_legacy_email_is_preserved_and_verify_endpoint_is_retired(self):
        legacy = Customer(
            business_id=self.one.id, full_name="Histórico",
            email="historico@example.test", phone="+525500000001",
        )
        db.session.add(legacy); db.session.commit()
        self.assertEqual(self.client.post("/join/uno", json=self.payload("55 0000 0002")).status_code, 201)
        db.session.refresh(legacy)
        self.assertEqual(legacy.email, "historico@example.test")
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "12345678"}).status_code, 410)

    def test_public_recovery_is_disabled_without_disclosing_customer_data(self):
        customer = Customer(
            business_id=self.one.id, full_name="Titular", phone="+525500000010",
            current_points=17,
        )
        db.session.add(customer); db.session.commit()
        for slug in ("uno", "dos"):
            response = self.client.post(f"/recover/{slug}/request", json={
                "phone": customer.phone, "full_name": customer.full_name,
                "card_token": customer.qr_code_token,
            })
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.get_json()["status"], "unavailable")
        self.assertEqual(CustomerRecoveryAuthorization.query.count(), 0)
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])
        page = self.client.get("/recover/uno").get_data(as_text=True)
        self.assertNotIn("Titular", page)
        self.assertNotIn(customer.qr_code_token, page)
        self.assertIn("Recuperación pública no disponible", page)

    def test_recovery_never_accepts_name_phone_qr_or_legacy_otp(self):
        customer = Customer(business_id=self.one.id, full_name="Titular", phone="+525500000020")
        db.session.add(customer); db.session.commit()
        for payload in ({"phone": customer.phone}, {"full_name": customer.full_name}, {"card_token": customer.qr_code_token}, {"email": "legacy@example.test"}):
            response = self.client.post("/recover/uno/request", json=payload)
            self.assertEqual(response.status_code, 410)
        self.assertEqual(self.client.post("/recover/uno/verify", json={"code": "12345678"}).status_code, 410)
        self.assertEqual(CustomerRecoveryAuthorization.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
