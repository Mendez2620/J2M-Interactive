import unittest
import re
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, CustomerRecoveryAuthorization,
    CustomerRegistrationChallenge, CustomerRegistrationIdentityClaim,
    Promoter, ReferralAttribution, RegistrationEmailJob,
)
from app.services.referral_participant_service import ensure_external_promoter


class Phase8ATestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            REGISTRATION_REQUEST_LIMIT_PHONE=20,
            REGISTRATION_REQUEST_LIMIT_IP=40,
        )
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.one = Business(name="Uno", slug="uno", loyalty_type="points", referrals_enabled=True)
        self.two = Business(name="Dos", slug="dos", referrals_enabled=True)
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
        self.assertIn("Solicita ayuda directamente al negocio", html)
        self.assertNotIn("Recupérala aquí", html)

    def test_wallet_delivery_can_resume_only_in_creating_session(self):
        created = self.client.post("/join/uno", json=self.payload()).get_json()
        delivery_url = created["wallet_delivery_url"]
        first = self.client.get(delivery_url)
        second = self.client.get(delivery_url)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.get_json()["customer"]["id"], Customer.query.one().id)
        self.assertTrue(first.get_json()["apple_wallet_url"].startswith("/api/customers/1/wallet/apple?token="))
        self.assertTrue(second.get_json()["apple_wallet_url"].startswith("/api/customers/1/wallet/apple?token="))
        self.assertEqual(Customer.query.count(), 1)

        other_browser = self.app.test_client()
        self.assertEqual(other_browser.get(delivery_url).status_code, 403)
        cross_tenant = delivery_url.replace("/join/uno/", "/join/dos/")
        self.assertEqual(self.client.get(cross_tenant).status_code, 403)

    def test_wallet_delivery_expires_and_reload_offers_active_delivery(self):
        created = self.client.post("/join/uno", json=self.payload()).get_json()
        page = self.client.get("/join/uno").get_data(as_text=True)
        self.assertIn(created["wallet_delivery_url"].replace("&", "\\u0026"), page)
        self.assertIn("Continuar con mi tarjeta", page)
        self.assertIn("Registrar otra persona", page)
        self.assertNotIn("¡Hola Cliente Nuevo!", page)
        self.assertNotIn("if (resumeWalletUrl) {", page)
        self.assertIn("continueWallet.addEventListener('click'", page)
        with self.client.session_transaction() as browser:
            delivery = dict(browser["customer_wallet_delivery"])
            delivery["expires_at"] = 0
            browser["customer_wallet_delivery"] = delivery
        self.assertEqual(self.client.get(created["wallet_delivery_url"]).status_code, 403)
        self.assertNotIn("wallet-delivery?credential=", self.client.get("/join/uno").get_data(as_text=True))

    def test_shared_browser_keeps_new_referral_form_and_registers_second_customer(self):
        first_promoter = Promoter(business_id=self.one.id, name="Primero", username="primero", referral_code="PROMO-A")
        second_promoter = Promoter(business_id=self.one.id, name="Segundo", username="segundo", referral_code="PROMO-B")
        first_promoter.set_password("Seguro12345")
        second_promoter.set_password("Seguro12345")
        db.session.add_all([first_promoter, second_promoter]); db.session.flush()
        ensure_external_promoter(first_promoter)
        ensure_external_promoter(second_promoter)
        db.session.commit()

        first_page = self.client.get("/join/uno?ref=PROMO-A").get_data(as_text=True)
        first_context = re.search(r'name="referral_context" value="([^"]*)"', first_page).group(1)
        first = self.client.post("/join/uno", json=self.payload(referral_context=first_context))
        self.assertEqual(first.status_code, 201)

        second_page = self.client.get("/join/uno?ref=PROMO-B").get_data(as_text=True)
        second_context = re.search(r'name="referral_context" value="([^"]*)"', second_page).group(1)
        self.assertNotEqual(first_context, second_context)
        self.assertIn('id="join-form"', second_page)
        self.assertNotIn("Continuar con mi tarjeta", second_page)
        self.assertNotIn("Cliente Nuevo", second_page)
        self.assertNotIn("wallet-delivery?credential=", second_page)

        second = self.client.post("/join/uno", json=self.payload(
            phone="55 1234 5679", full_name="Segunda Persona",
            referral_context=second_context,
        ))
        self.assertEqual(second.status_code, 201)
        self.assertEqual(Customer.query.count(), 2)
        attributions = ReferralAttribution.query.order_by(ReferralAttribution.customer_id).all()
        self.assertEqual([item.promoter_id for item in attributions], [first_promoter.id, second_promoter.id])

    def test_new_customer_action_clears_only_same_tenant_delivery(self):
        created = self.client.post("/join/uno", json=self.payload()).get_json()
        self.assertEqual(self.client.get("/join/uno/wallet-delivery/end").status_code, 405)
        self.assertEqual(self.client.post("/join/dos/wallet-delivery/end").status_code, 200)
        self.assertEqual(self.client.get(created["wallet_delivery_url"]).status_code, 200)
        self.assertEqual(self.client.post("/join/uno/wallet-delivery/end").status_code, 200)
        self.assertEqual(self.client.get(created["wallet_delivery_url"]).status_code, 403)

    def test_suspended_business_blocks_pending_wallet_delivery(self):
        created = self.client.post("/join/uno", json=self.payload()).get_json()
        self.one.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(created["wallet_delivery_url"]).status_code, 403)

    def test_wallet_provider_failure_does_not_consume_delivery_credential(self):
        created = self.client.post("/join/uno", json=self.payload()).get_json()
        with patch("app.routes.customer.AppleWalletService.generate_pkpass", side_effect=RuntimeError("provider unavailable")):
            self.assertEqual(self.client.get(created["apple_wallet_url"]).status_code, 500)
        resumed = self.client.get(created["wallet_delivery_url"])
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["customer"]["id"], Customer.query.one().id)

    def test_duplicate_attempts_persistently_consume_phone_and_ip_limits(self):
        self.app.config.update(REGISTRATION_REQUEST_LIMIT_PHONE=2, REGISTRATION_REQUEST_LIMIT_IP=3)
        self.assertEqual(self.client.post("/join/uno", json=self.payload()).status_code, 201)
        duplicate = self.client.post("/join/uno", json=self.payload())
        limited = self.client.post("/join/uno", json=self.payload())
        self.assertEqual((duplicate.status_code, limited.status_code), (409, 429))
        self.assertNotIn("existe", duplicate.get_data(as_text=True).lower())
        self.assertEqual(Customer.query.count(), 1)
        self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 1)

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
