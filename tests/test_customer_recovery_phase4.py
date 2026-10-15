import io
import unittest
import zipfile
from datetime import timedelta

from app import create_app
from app.email_service import MailDeliveryError, SMTPRecoveryMailer
from app.extensions import db
from app.models import (
    AppleDevice, Business, Customer, CustomerRecoveryAuthorization,
    CustomerRecoveryChallenge, Transaction,
)
from app.models.customer_recovery import utcnow
from app.services import AppleWalletService


class FailingMailer:
    def send_recovery_code(self, *_args):
        raise MailDeliveryError("simulated")


class CustomerRecoveryPhase4TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.business = Business(name="Café Uno", slug="cafe-uno", loyalty_type="points", is_active=True, primary_color="#123456")
        self.other = Business(name="Café Dos", slug="cafe-dos", loyalty_type="stamps", is_active=True)
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.customer = Customer(business_id=self.business.id, full_name="Cliente Existente", email="client@example.test", current_points=42, qr_code_token="stable-recovery-qr")
        self.other_customer = Customer(business_id=self.other.id, full_name="Cliente Ajeno", email="other@example.test", current_stamps=7)
        self.phone_only = Customer(business_id=self.business.id, full_name="Solo Teléfono", phone="5550101")
        db.session.add_all([self.customer, self.other_customer, self.phone_only]); db.session.flush()
        self.transaction = Transaction(business_id=self.business.id, customer_id=self.customer.id, type="earn", points_amount=42)
        self.device = AppleDevice(customer_id=self.customer.id, device_library_identifier="device-stable", push_token="push-secret", pass_type_identifier="pass.test", serial_number="serial-stable")
        db.session.add_all([self.transaction, self.device]); db.session.commit()
        self.original = (self.customer.id, self.customer.qr_code_token, self.customer.current_points, self.customer.transactions.count(), self.customer.apple_devices.count())

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    @property
    def outbox(self):
        return self.app.extensions["recovery_mailer"].outbox

    def request_code(self, email="client@example.test", slug="cafe-uno", client=None):
        client = client or self.client
        response = client.post(f"/recover/{slug}/request", json={"email": email})
        code = self.outbox[-1]["code"] if self.outbox else None
        return response, code

    def verify(self, code, slug="cafe-uno", client=None):
        return (client or self.client).post(f"/recover/{slug}/verify", json={"code": code})

    def authorize(self):
        response, code = self.request_code()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.verify(code).status_code, 200)

    def test_request_is_generic_for_existing_missing_and_other_tenant(self):
        existing, code = self.request_code()
        self.assertIsNotNone(code)
        challenge = CustomerRecoveryChallenge.query.order_by(CustomerRecoveryChallenge.created_at.desc()).first()
        self.assertNotEqual(challenge.code_hash, code)
        existing_payload = existing.get_json()
        outbox_size = len(self.outbox)
        missing, _ = self.request_code("missing@example.test")
        wrong_tenant, _ = self.request_code("other@example.test")
        self.assertEqual(existing_payload, missing.get_json())
        self.assertEqual(existing_payload, wrong_tenant.get_json())
        self.assertEqual(len(self.outbox), outbox_size)
        for response in (existing, missing, wrong_tenant):
            body = response.get_data(as_text=True)
            self.assertNotIn("Cliente Existente", body)
            self.assertNotIn("stable-recovery-qr", body)
            self.assertNotIn(code, body)

    def test_correct_code_is_single_use_and_previous_challenge_is_invalidated(self):
        _, first_code = self.request_code()
        first_id = CustomerRecoveryChallenge.query.order_by(CustomerRecoveryChallenge.created_at.desc()).first().id
        _, second_code = self.request_code()
        self.assertIsNotNone(db.session.get(CustomerRecoveryChallenge, first_id).consumed_at)
        self.assertEqual(self.verify(first_code).status_code, 400)
        self.assertEqual(self.verify(second_code).status_code, 200)
        authorization = CustomerRecoveryAuthorization.query.one()
        self.assertNotIn(second_code, authorization.token_hash)
        with self.client.session_transaction() as browser:
            browser["customer_recovery_challenge_id"] = CustomerRecoveryChallenge.query.order_by(CustomerRecoveryChallenge.created_at.desc()).first().id
            browser.pop("customer_recovery_authorization_id", None)
            browser.pop("customer_recovery_authorization_token", None)
        self.assertIn(self.verify(second_code).status_code, {400, 409})
        self.assertEqual(CustomerRecoveryAuthorization.query.count(), 1)

    def test_wrong_expired_and_max_attempt_codes_are_rejected(self):
        _, code = self.request_code()
        challenge = CustomerRecoveryChallenge.query.order_by(CustomerRecoveryChallenge.created_at.desc()).first()
        for _ in range(5):
            self.assertEqual(self.verify("00000000").status_code, 400)
        self.assertEqual(db.session.get(CustomerRecoveryChallenge, challenge.id).attempts, 5)
        self.assertEqual(self.verify(code).status_code, 400)
        _, expired_code = self.request_code()
        challenge = CustomerRecoveryChallenge.query.order_by(CustomerRecoveryChallenge.created_at.desc()).first()
        challenge.expires_at = utcnow() - timedelta(seconds=1); db.session.commit()
        self.assertEqual(self.verify(expired_code).status_code, 400)

    def test_request_limits_ambiguous_email_and_missing_mailer_fail_closed(self):
        for _ in range(3):
            self.request_code()
        sent = len(self.outbox)
        limited, _ = self.request_code()
        self.assertEqual(limited.status_code, 200)
        self.assertEqual(len(self.outbox), sent)

        ambiguous_email = "duplicate@example.test"
        db.session.add_all([
            Customer(business_id=self.other.id, full_name="Uno", email=ambiguous_email),
            Customer(business_id=self.other.id, full_name="Dos", email=ambiguous_email),
        ]); db.session.commit()
        previous = len(self.outbox)
        self.request_code(ambiguous_email, "cafe-dos")
        self.assertEqual(len(self.outbox), previous)
        self.assertIsNone(CustomerRecoveryChallenge.query.order_by(CustomerRecoveryChallenge.created_at.desc()).first().customer_id)

        self.app.extensions["recovery_mailer"] = SMTPRecoveryMailer({})
        response = self.client.post("/recover/cafe-dos/request", json={"email": "other@example.test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomerRecoveryChallenge.query.filter_by(customer_id=self.other_customer.id).count(), 0)
        self.app.extensions["recovery_mailer"] = FailingMailer()
        response = self.client.post("/recover/cafe-dos/request", json={"email": "other@example.test"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomerRecoveryChallenge.query.filter_by(customer_id=self.other_customer.id).count(), 0)

    def test_no_private_access_before_verification_or_across_businesses(self):
        page = self.client.get("/recover/cafe-uno")
        self.assertNotIn(b"Cliente Existente", page.data)
        self.assertEqual(self.client.get("/recover/cafe-uno/wallet/apple").status_code, 302)
        _, code = self.request_code()
        self.assertEqual(self.verify(code, "cafe-dos").status_code, 400)
        self.assertEqual(self.client.get("/recover/cafe-dos/wallet/apple").status_code, 302)
        self.assertNotIn(b"Cliente Ajeno", self.client.get("/recover/cafe-dos").data)

    def test_authorization_expiry_revocation_headers_and_secure_cookie(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True
        _, code = self.request_code()
        verified = self.verify(code)
        self.assertIn("Secure", verified.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", verified.headers.get("Set-Cookie", ""))
        page = self.client.get("/recover/cafe-uno")
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
        authorization = CustomerRecoveryAuthorization.query.one()
        authorization.expires_at = utcnow() - timedelta(seconds=1); db.session.commit()
        self.assertNotIn(b"Cliente Existente", self.client.get("/recover/cafe-uno").data)

        self.authorize()
        with self.client.session_transaction() as browser:
            authorization_id = browser["customer_recovery_authorization_id"]
        db.session.get(CustomerRecoveryAuthorization, authorization_id).revoked_at = utcnow(); db.session.commit()
        self.assertEqual(self.client.get("/recover/cafe-uno/wallet/apple").status_code, 302)

        self.authorize()
        self.assertEqual(self.client.post("/recover/cafe-uno/end").status_code, 302)
        self.assertEqual(self.client.get("/recover/cafe-uno/wallet/apple").status_code, 302)

    def test_csrf_protects_mutations(self):
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/recover/cafe-uno/request", data={"email": "client@example.test"}).status_code, 400)
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "recovery-csrf"
        response = self.client.post("/recover/cafe-uno/request", data={"email": "client@example.test", "csrf_token": "recovery-csrf"})
        self.assertEqual(response.status_code, 302)
        code = self.outbox[-1]["code"]
        self.assertEqual(self.client.post("/recover/cafe-uno/verify", data={"code": code}).status_code, 400)
        self.assertEqual(self.client.post("/recover/cafe-uno/verify", data={"code": code, "csrf_token": "recovery-csrf"}).status_code, 302)

    def test_wallet_recovery_preserves_customer_identity_balances_and_history(self):
        serial_before = AppleWalletService().get_serial_number(self.customer)
        self.authorize()
        page = self.client.get("/recover/cafe-uno")
        self.assertIn(b"Cliente Existente", page.data)
        self.assertIn(b"42", page.data)
        apple = self.client.get("/recover/cafe-uno/wallet/apple")
        self.assertEqual(apple.status_code, 200)
        self.assertEqual(apple.mimetype, "application/vnd.apple.pkpass")
        with zipfile.ZipFile(io.BytesIO(apple.data)) as archive:
            self.assertIn("pass.json", archive.namelist())
        google = self.client.get("/recover/cafe-uno/wallet/google")
        self.assertEqual(google.status_code, 302)
        self.assertTrue(google.headers["Location"].startswith("https://pay.google.com/gp/v/save/"))
        customer = db.session.get(Customer, self.customer.id)
        self.assertEqual((customer.id, customer.qr_code_token, customer.current_points, customer.transactions.count(), customer.apple_devices.count()), self.original)
        self.assertEqual(AppleWalletService().get_serial_number(customer), serial_before)
        self.assertEqual(Customer.query.count(), 3)

        self.other_customer.email = "stamps@example.test"; db.session.commit()
        response, code = self.request_code("stamps@example.test", "cafe-dos")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.verify(code, "cafe-dos").status_code, 200)
        self.assertIn(b"7", self.client.get("/recover/cafe-dos").data)
        self.assertEqual(db.session.get(Customer, self.other_customer.id).current_stamps, 7)

    def test_join_link_and_phone_only_account_do_not_expose_secrets(self):
        join = self.client.get("/join/cafe-uno").get_data(as_text=True)
        self.assertIn("¿Ya tienes una tarjeta? Recupérala aquí", join)
        self.assertNotIn("customer-token-display", join)
        recovery = self.client.get("/recover/cafe-uno").get_data(as_text=True)
        self.assertNotIn("localStorage", recovery)
        self.assertNotIn("sessionStorage", recovery)
        response = self.client.post("/recover/cafe-uno/request", json={"email": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.outbox), 0)
        self.assertNotIn("Solo Teléfono", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
