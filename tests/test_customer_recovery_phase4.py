import io
import hashlib
import secrets
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.config import TestingConfig
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
        return response, None

    def verify(self, code, slug="cafe-uno", client=None):
        return (client or self.client).post(f"/recover/{slug}/verify", json={"code": code})

    def authorize(self, customer=None, business=None):
        customer = customer or self.customer
        business = business or self.business
        token = secrets.token_urlsafe(32)
        authorization = CustomerRecoveryAuthorization(
            business_id=business.id, customer_id=customer.id,
            token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=utcnow() + timedelta(minutes=10),
        )
        db.session.add(authorization); db.session.commit()
        with self.client.session_transaction() as browser:
            browser["customer_recovery_authorization_id"] = authorization.id
            browser["customer_recovery_authorization_token"] = token

    def test_request_is_generic_for_existing_missing_and_other_tenant(self):
        existing, code = self.request_code()
        self.assertIsNone(code)
        existing_payload = existing.get_json()
        missing, _ = self.request_code("missing@example.test")
        wrong_tenant, _ = self.request_code("other@example.test")
        self.assertEqual(existing_payload, missing.get_json())
        self.assertEqual(existing_payload, wrong_tenant.get_json())
        self.assertEqual(existing.status_code, 410)
        self.assertEqual(len(self.outbox), 0)
        self.assertEqual(CustomerRecoveryChallenge.query.count(), 0)
        for response in (existing, missing, wrong_tenant):
            body = response.get_data(as_text=True)
            self.assertNotIn("Cliente Existente", body)
            self.assertNotIn("stable-recovery-qr", body)

    def test_historical_authorization_is_revocable_and_cannot_be_replayed(self):
        self.authorize()
        with self.client.session_transaction() as browser:
            authorization_id = browser["customer_recovery_authorization_id"]
            authorization_token = browser["customer_recovery_authorization_token"]
        self.assertIn(b"Cliente Existente", self.client.get("/recover/cafe-uno").data)
        self.assertEqual(self.client.post("/recover/cafe-uno/end").status_code, 302)
        self.assertIsNotNone(db.session.get(CustomerRecoveryAuthorization, authorization_id).revoked_at)
        with self.client.session_transaction() as browser:
            browser["customer_recovery_authorization_id"] = authorization_id
            browser["customer_recovery_authorization_token"] = authorization_token
        self.assertNotIn(b"Cliente Existente", self.client.get("/recover/cafe-uno").data)
        self.assertEqual(CustomerRecoveryAuthorization.query.count(), 1)

    def test_legacy_codes_and_public_identity_fields_never_authorize(self):
        for code in ("00000000", "12345678", "", None):
            self.assertEqual(self.verify(code).status_code, 410)
        for payload in ({"email": self.customer.email}, {"phone": self.customer.phone},
                        {"full_name": self.customer.full_name}, {"card_token": self.customer.qr_code_token}):
            self.assertEqual(self.client.post("/recover/cafe-uno/request", json=payload).status_code, 410)
        self.assertEqual(CustomerRecoveryChallenge.query.count(), 0)
        self.assertEqual(CustomerRecoveryAuthorization.query.count(), 0)

    def test_disabled_recovery_never_calls_mailer_or_creates_partial_state(self):
        ambiguous_email = "duplicate@example.test"
        db.session.add_all([
            Customer(business_id=self.other.id, full_name="Uno", email=ambiguous_email),
            Customer(business_id=self.other.id, full_name="Dos", email=ambiguous_email),
        ]); db.session.commit()
        mailer = self.app.extensions["recovery_mailer"]
        with patch.object(mailer, "send_recovery_code") as send:
            for email in (ambiguous_email, "other@example.test", "missing@example.test"):
                response = self.client.post("/recover/cafe-dos/request", json={"email": email})
                self.assertEqual(response.status_code, 410)
            send.assert_not_called()
        self.assertEqual(self.outbox, [])
        self.assertEqual(CustomerRecoveryChallenge.query.count(), 0)
        self.assertEqual(CustomerRecoveryAuthorization.query.count(), 0)

    def _concurrent_recovery_app(self, directory, email_limit, ip_limit):
        uri = f"sqlite:///{Path(directory, 'concurrent-recovery.sqlite').as_posix()}"
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
            app = create_app("testing")
        app.config.update(
            RECOVERY_REQUEST_LIMIT_EMAIL=email_limit,
            RECOVERY_REQUEST_LIMIT_IP=ip_limit,
        )
        with app.app_context():
            db.create_all()
            business = Business(name="Concurrente", slug="concurrente", loyalty_type="points", is_active=True)
            db.session.add(business); db.session.flush()
            for index in range(8):
                db.session.add(Customer(
                    business_id=business.id, full_name=f"Cliente {index}",
                    email=f"client{index}@example.test",
                ))
            db.session.commit()
        return app

    def test_concurrent_public_recovery_requests_never_create_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._concurrent_recovery_app(directory, email_limit=2, ip_limit=20)

            def request_once(index):
                with app.test_client() as client:
                    return client.post(
                        "/recover/concurrente/request",
                        json={"email": "client0@example.test"},
                        environ_base={"REMOTE_ADDR": f"192.0.2.{index + 1}"},
                    ).status_code

            with ThreadPoolExecutor(max_workers=8) as executor:
                payloads = list(executor.map(request_once, range(8)))
            self.assertEqual(payloads, [410] * 8)
            self.assertEqual(len(app.extensions["recovery_mailer"].outbox), 0)
            with app.app_context():
                self.assertEqual(CustomerRecoveryAuthorization.query.count(), 0)
                self.assertEqual(CustomerRecoveryChallenge.query.count(), 0)
                db.session.remove()
                db.engine.dispose()

    def test_concurrent_mixed_identities_remain_indistinguishable(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self._concurrent_recovery_app(directory, email_limit=3, ip_limit=2)

            def request_once(index):
                with app.test_client() as client:
                    return client.post(
                        "/recover/concurrente/request",
                        json={"email": f"client{index}@example.test"},
                        environ_base={"REMOTE_ADDR": "198.51.100.9"},
                    ).get_json()

            with ThreadPoolExecutor(max_workers=8) as executor:
                payloads = list(executor.map(request_once, range(8)))
            self.assertTrue(all(payload == payloads[0] for payload in payloads))
            self.assertEqual(len(app.extensions["recovery_mailer"].outbox), 0)
            with app.app_context():
                self.assertEqual(CustomerRecoveryAuthorization.query.count(), 0)
                db.session.remove()
                db.engine.dispose()

    def test_no_private_access_before_verification_or_across_businesses(self):
        page = self.client.get("/recover/cafe-uno")
        self.assertNotIn(b"Cliente Existente", page.data)
        self.assertEqual(self.client.get("/recover/cafe-uno/wallet/apple").status_code, 302)
        self.assertEqual(self.verify("12345678", "cafe-dos").status_code, 410)
        self.assertEqual(self.client.get("/recover/cafe-dos/wallet/apple").status_code, 302)
        self.assertNotIn(b"Cliente Ajeno", self.client.get("/recover/cafe-dos").data)

    def test_authorization_expiry_revocation_headers_and_secure_cookie(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True
        self.authorize()
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
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.client.post("/recover/cafe-uno/verify", data={"code": "12345678"}).status_code, 400)
        self.assertEqual(self.client.post("/recover/cafe-uno/verify", data={"code": "12345678", "csrf_token": "recovery-csrf"}).status_code, 410)

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
        self.authorize(customer=self.other_customer, business=self.other)
        self.assertIn(b"7", self.client.get("/recover/cafe-dos").data)
        self.assertEqual(db.session.get(Customer, self.other_customer.id).current_stamps, 7)

    def test_disabled_google_wallet_is_not_offered_after_recovery(self):
        self.authorize()
        self.app.config["GOOGLE_WALLET_MODE"] = "disabled"
        page = self.client.get("/recover/cafe-uno")
        self.assertNotIn(b"Guardar en Google Wallet", page.data)
        response = self.client.get("/recover/cafe-uno/wallet/google")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"credencial", response.data.lower())

    def test_join_link_and_phone_only_account_do_not_expose_secrets(self):
        join = self.client.get("/join/cafe-uno").get_data(as_text=True)
        self.assertIn("¿Ya tienes una tarjeta? Solicita ayuda directamente al negocio.", join)
        self.assertNotIn("Recupérala aquí", join)
        self.assertNotIn("customer-token-display", join)
        recovery = self.client.get("/recover/cafe-uno").get_data(as_text=True)
        self.assertNotIn("localStorage", recovery)
        self.assertNotIn("sessionStorage", recovery)
        response = self.client.post("/recover/cafe-uno/request", json={"email": ""})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(len(self.outbox), 0)
        self.assertNotIn("Solo Teléfono", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
