import io
import json
import unittest
import zipfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import create_app
from app.extensions import db
from app.models import (
    AppleDevice, ApplePassCredential, AppleWalletPublication, AppleWalletPushJob,
    Business, Customer, PlatformAdmin,
)
from app.services import AppleWalletService
from app.services.apple_wallet_update_service import (
    cancel_apple_publication, enqueue_apple_publication, process_pending_apple_wallet_jobs,
)


class AppleWalletUpdatesPhase8C3C2TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(CSRF_ENABLED=True, APPLE_WALLET_MODE="mock")
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        db.create_all()
        self.business = Business(name="Apple Café", slug="apple-cafe", loyalty_type="points", is_active=True)
        self.other = Business(name="Otro", slug="otro", loyalty_type="points", is_active=True)
        db.session.add_all([self.business, self.other])
        db.session.flush()
        self.customer = Customer(business_id=self.business.id, full_name="Cliente Uno", current_points=12)
        self.other_customer = Customer(business_id=self.other.id, full_name="Cliente Dos")
        self.admin = PlatformAdmin(name="Admin", email="admin@j2m.test", is_active=True)
        self.admin.set_password("AdminPassword123!")
        db.session.add_all([self.customer, self.other_customer, self.admin])
        db.session.commit()
        self.service = AppleWalletService()
        self.credential = self.service.ensure_pass_credential(self.customer)
        self.token = self.service.authentication_token(self.customer, self.credential)
        self.device = AppleDevice(
            customer_id=self.customer.id, device_library_identifier="device_library_123",
            pass_type_identifier=self.service.pass_type_id,
            serial_number=self.service.get_serial_number(self.customer), push_token="push_token_123456",
        )
        db.session.add(self.device)
        db.session.commit()
        with self.client.session_transaction() as browser:
            browser["platform_admin_id"] = self.admin.id
            browser["csrf_token"] = "apple-publish-csrf"

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def auth(self):
        return {"Authorization": f"ApplePass {self.token}"}

    def test_passkit_token_is_stable_and_not_loyalty_qr(self):
        payload = self.service.build_pass_json(self.customer)
        self.assertEqual(payload["authenticationToken"], self.token)
        self.assertNotEqual(payload["authenticationToken"], self.customer.qr_code_token)
        self.assertEqual(payload["barcodes"][0]["message"], self.customer.qr_code_token)
        self.assertEqual(self.service.authentication_token(self.customer), self.token)
        url = f"/api/apple/v1/passes/{self.service.pass_type_id}/{self.credential.serial_number}"
        self.assertEqual(self.client.get(url, headers={"Authorization": f"ApplePass {self.customer.qr_code_token}"}).status_code, 401)

    def test_update_marker_and_download_confirmation(self):
        list_url = f"/api/apple/v1/devices/{self.device.device_library_identifier}/registrations/{self.service.pass_type_id}"
        first = self.client.get(list_url)
        self.assertEqual(first.status_code, 200)
        marker = first.get_json()["lastUpdated"]
        self.assertEqual(self.client.get(list_url, query_string={"passesUpdatedSince": marker}).status_code, 204)
        self.credential.update_version += 1
        db.session.commit()
        self.assertEqual(self.client.get(list_url, query_string={"passesUpdatedSince": marker}).status_code, 200)
        deliver = self.client.get(
            f"/api/apple/v1/passes/{self.service.pass_type_id}/{self.credential.serial_number}", headers=self.auth(),
        )
        self.assertEqual(deliver.status_code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(deliver.data)))
        self.assertEqual(db.session.get(AppleDevice, self.device.id).last_downloaded_version, self.credential.update_version)

    def test_registration_rejects_cross_tenant_and_invalid_input(self):
        url = f"/api/apple/v1/devices/device_library_new/registrations/{self.service.pass_type_id}/{self.credential.serial_number}"
        self.assertEqual(self.client.post(url, json=[], headers=self.auth()).status_code, 400)
        self.assertEqual(self.client.post(url, json={"pushToken": 123}, headers=self.auth()).status_code, 400)
        other_service = AppleWalletService()
        other_credential = other_service.ensure_pass_credential(self.other_customer)
        db.session.commit()
        self.assertEqual(self.client.post(
            f"/api/apple/v1/devices/device_library_new/registrations/{self.service.pass_type_id}/{other_credential.serial_number}",
            json={"pushToken": "push_token_123456"}, headers=self.auth(),
        ).status_code, 401)

    def test_persistent_publication_mock_and_permanent_apns_failure(self):
        publication = enqueue_apple_publication(
            business=self.business, admin=self.admin, reason="Actualizar diseño",
        )
        self.assertEqual(publication.total_jobs, 1)
        result = process_pending_apple_wallet_jobs(service=Mock(send_push_notification=Mock(return_value={"ok": True, "status_code": 200})))
        self.assertEqual(result, 1)
        self.assertEqual(db.session.get(AppleWalletPublication, publication.id).status, "completed")
        self.assertEqual(AppleWalletPushJob.query.one().status, "completed")

        publication2 = enqueue_apple_publication(
            business=self.business, admin=self.admin, reason="Republicar", republish=True,
        )
        process_pending_apple_wallet_jobs(service=Mock(send_push_notification=Mock(return_value={"ok": False, "status_code": 410, "permanent": True})))
        self.assertEqual(db.session.get(AppleWalletPublication, publication2.id).status, "failed")
        self.assertFalse(db.session.get(AppleDevice, self.device.id).is_active)

    def test_cancellation_during_slow_apns_cannot_be_overwritten_by_old_worker(self):
        publication = enqueue_apple_publication(
            business=self.business, admin=self.admin, reason="Publicación cancelable",
        )

        def cancel_during_http(_push_token):
            cancel_apple_publication(db.session.get(AppleWalletPublication, publication.id), self.admin)
            return {"ok": True, "status_code": 200}

        process_pending_apple_wallet_jobs(service=Mock(send_push_notification=cancel_during_http))
        self.assertEqual(db.session.get(AppleWalletPublication, publication.id).status, "cancelled")
        self.assertEqual(AppleWalletPushJob.query.filter_by(publication_id=publication.id).one().status, "cancelled")
        self.assertEqual(db.session.get(AppleDevice, self.device.id).last_notified_version, 0)

    def test_newer_design_is_revalidated_before_apns_and_old_job_is_superseded(self):
        publication = enqueue_apple_publication(
            business=self.business, admin=self.admin, reason="Versión anterior",
        )
        self.business.wallet_design_version += 1
        db.session.commit()
        sender = Mock(send_push_notification=Mock(return_value={"ok": True, "status_code": 200}))
        process_pending_apple_wallet_jobs(service=sender)
        sender.send_push_notification.assert_not_called()
        self.assertEqual(db.session.get(AppleWalletPublication, publication.id).status, "superseded")
        self.assertEqual(AppleWalletPushJob.query.one().status, "superseded")

    def test_admin_publication_requires_admin_csrf_confirmation_and_tenant(self):
        url = f"/admin/business/{self.business.id}/wallet/apple/publications"
        payload = {"confirm": True, "reason": "Publicar Apple", "republish": False}
        self.assertEqual(self.client.post(url, json=payload).status_code, 400)
        self.assertEqual(self.client.post(url, json={"reason": "x"},
                                          headers={"X-CSRFToken": "apple-publish-csrf"}).status_code, 400)
        anonymous = self.app.test_client()
        with anonymous.session_transaction() as browser:
            browser["csrf_token"] = "anonymous"
        self.assertEqual(anonymous.post(url, json=payload, headers={"X-CSRFToken": "anonymous"}).status_code, 401)
        response = self.client.post(url, json=payload, headers={"X-CSRFToken": "apple-publish-csrf"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(AppleWalletPublication.query.one().business_id, self.business.id)
        self.assertEqual(self.client.post(
            f"/admin/business/{self.other.id}/wallet/apple/publications/{response.get_json()['publication_id']}/cancel",
            headers={"X-CSRFToken": "apple-publish-csrf"},
        ).status_code, 404)

    def test_real_mode_never_falls_back_to_dummy_signature_or_fake_push(self):
        self.app.config["APPLE_WALLET_MODE"] = "real"
        self.app.config["APPLE_WEB_SERVICE_URL"] = "https://wallet.example.test/api/apple/v1"
        service = AppleWalletService(cert_path="missing.pem", key_path="missing-key.pem", wwdr_path="missing-wwdr.pem",
                                     web_service_url="https://wallet.example.test/api/apple/v1")
        with self.assertRaises(RuntimeError):
            service.generate_pkpass(self.customer)
        self.assertFalse(service.send_push_notification("push_token_123456")["ok"])

    def test_apns_http2_contract_has_no_private_payload(self):
        self.app.config["APPLE_WALLET_MODE"] = "real"
        service = AppleWalletService(cert_path="cert.pem", key_path="key.pem")
        response = Mock(status_code=200)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.return_value = response
        ssl_context = Mock()
        with patch("app.services.apple_wallet.os.path.isfile", return_value=True), \
                patch("builtins.open", mock_open(read_data=b"cert")), \
                patch("app.services.apple_wallet.x509.load_pem_x509_certificate", return_value=Mock()), \
                patch.object(service, "_validate_pass_certificate"), \
                patch("app.services.apple_wallet.ssl.create_default_context", return_value=ssl_context), \
                patch("app.services.apple_wallet.httpx.Client", return_value=client):
            result = service.send_push_notification("push_token_123456")
        self.assertTrue(result["ok"])
        ssl_context.load_cert_chain.assert_called_once_with("cert.pem", "key.pem", password=None)
        args, kwargs = client.post.call_args
        self.assertEqual(kwargs["content"], b"{}")
        self.assertEqual(kwargs["headers"]["apns-topic"], service.pass_type_id)
        self.assertNotIn(self.customer.full_name, repr(kwargs))

    def test_pkpass_uses_real_detached_signature_with_test_certificates(self):
        def certificate(common_name, key, *, pass_type=None):
            attributes = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
            if pass_type:
                attributes.append(x509.NameAttribute(NameOID.USER_ID, pass_type))
            subject = x509.Name(attributes)
            now = datetime.now(timezone.utc)
            return (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                    .sign(key, hashes.SHA256()))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            wwdr_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            cert = certificate("Test Pass", key, pass_type=self.service.pass_type_id)
            wwdr = certificate("Test WWDR", wwdr_key)
            (root / "key.pem").write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            (root / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (root / "wwdr.pem").write_bytes(wwdr.public_bytes(serialization.Encoding.PEM))
            service = AppleWalletService(cert_path=str(root / "cert.pem"), key_path=str(root / "key.pem"),
                                         wwdr_path=str(root / "wwdr.pem"))
            with zipfile.ZipFile(service.generate_pkpass(self.customer)) as archive:
                signature = archive.read("signature")
            self.assertFalse(signature.startswith(b"PKCS7_DEV_DUMMY_SIGNATURE_"))
            subjects = x509.load_der_x509_certificate(cert.public_bytes(serialization.Encoding.DER)).subject
            self.assertIn(self.service.pass_type_id, [item.value for item in subjects])


if __name__ == "__main__":
    unittest.main()
