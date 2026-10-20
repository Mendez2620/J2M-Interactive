import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import jwt
import requests
from cryptography.hazmat.primitives import serialization

from app import create_app
from app.extensions import db
from app.models import Business, Customer, GoogleWalletSyncJob, Transaction
from app.services.google_wallet import (
    GoogleWalletAPIError, GoogleWalletConfigurationError, GoogleWalletService,
)
from app.wallet_access import issue_wallet_token
from app.services.google_wallet_sync_service import (
    _customer_lock, _now, google_wallet_sync_counts,
    process_pending_google_wallet_jobs,
)
from app.services.loyalty_transaction_service import process_loyalty_transaction


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode() if payload is not None else b""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class GoogleWalletRealTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(PUBLIC_BASE_URL="https://staging.example.test", GOOGLE_ISSUER_ID="3388000000023203929")
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.business = Business(name="Café Seguro", slug="cafe-seguro", logo_url="https://cdn.example.test/logo.png",
                                 loyalty_type="stamps", stamps_reward_limit=8, primary_color="#123456")
        db.session.add(self.business); db.session.commit()
        self.customer = Customer(business_id=self.business.id, full_name="Persona Privada",
                                 email="private@example.test", phone="+525500000000", current_stamps=2)
        db.session.add(self.customer); db.session.commit()
        private_key = GoogleWalletService._generate_dev_private_key()
        self.credentials = {
            "type": "service_account", "project_id": "fake-wallet-project",
            "private_key_id": "fake-key-id", "private_key": private_key,
            "client_email": "wallet-test@fake-wallet-project.iam.gserviceaccount.com",
            "client_id": "123456789", "token_uri": "https://oauth2.googleapis.com/token",
        }

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.context.pop()

    def service(self, responses):
        return GoogleWalletService(mode="real", credentials_info=self.credentials,
                                   http_session=FakeSession(responses))

    def test_disabled_mode_and_missing_or_invalid_credentials_fail_closed(self):
        disabled = GoogleWalletService(mode="disabled")
        with self.assertRaisesRegex(GoogleWalletConfigurationError, "deshabilitado"):
            disabled.create_jwt_pass(self.customer)
        with self.assertRaises(GoogleWalletConfigurationError):
            GoogleWalletService(mode="real", credentials_info={})
        invalid = dict(self.credentials, private_key="not-a-private-key")
        with self.assertRaisesRegex(GoogleWalletConfigurationError, "inválida"):
            GoogleWalletService(mode="real", credentials_info=invalid)

    def test_real_mode_creates_resources_and_signs_existing_object_jwt(self):
        session = FakeSession([
            FakeResponse(404), FakeResponse(200, {"id": "class"}),
            FakeResponse(404), FakeResponse(200, {"id": "object"}),
        ])
        service = GoogleWalletService(mode="real", credentials_info=self.credentials, http_session=session)
        token, save_url = service.create_jwt_pass(self.customer)
        private_key = serialization.load_pem_private_key(self.credentials["private_key"].encode(), None)
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        decoded = jwt.decode(token, public_key, algorithms=["RS256"], audience="google")
        self.assertEqual(decoded["iss"], self.credentials["client_email"])
        self.assertEqual(decoded["origins"], ["https://staging.example.test"])
        self.assertEqual(decoded["aud"], "google")
        self.assertEqual(decoded["typ"], "savetowallet")
        self.assertIsInstance(decoded["iat"], int)
        self.assertEqual(decoded["payload"], {"loyaltyObjects": [{
            "id": service.get_object_id(self.customer),
            "classId": service.get_class_id(self.business),
        }]})
        self.assertTrue(save_url.endswith(token))
        self.assertLess(len(save_url), 2000)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET", "POST"])

    def test_real_jwt_rejects_non_https_or_non_origin_values(self):
        service = self.service([])
        for origins in (["http://staging.example.test"], ["https://staging.example.test/path"], []):
            with self.subTest(origins=origins), self.assertRaises(GoogleWalletConfigurationError):
                service.create_jwt_pass(self.customer, origins=origins)

    def test_conflict_is_idempotently_resolved_and_identity_is_stable(self):
        original = self.customer.qr_code_token
        service = self.service([FakeResponse(404), FakeResponse(409), FakeResponse(200, {"id": "ok"})])
        class_body = service.create_or_update_class(self.business)
        service._upsert("loyaltyClass", class_body["id"], class_body)
        self.assertEqual([call[0] for call in service.http.calls], ["GET", "POST", "PATCH"])
        object_id = service.get_object_id(self.customer)
        self.customer.qr_code_token = "rotated-private-token"
        self.assertEqual(service.get_object_id(self.customer), object_id)
        self.assertEqual(service.create_loyalty_object(self.customer)["barcode"]["value"],
                         "rotated-private-token")
        self.customer.qr_code_token = original

    def test_object_minimizes_personal_data_and_tenants_do_not_collide(self):
        service = self.service([])
        payload = service.create_loyalty_object(self.customer)
        encoded = json.dumps(payload)
        self.assertNotIn(self.customer.email, encoded)
        self.assertNotIn(self.customer.phone, encoded)
        self.assertNotIn(self.customer.full_name, encoded)
        other = Business(name="Otro", slug="otro", logo_url="https://cdn.example.test/other.png")
        db.session.add(other); db.session.commit()
        other_customer = Customer(business_id=other.id, full_name="Otra persona")
        db.session.add(other_customer); db.session.commit()
        self.assertNotEqual(service.get_object_id(self.customer), service.get_object_id(other_customer))

    def test_updates_points_stamps_and_classifies_api_failures(self):
        success = self.service([FakeResponse(200, {"ok": True})])
        result = success.update_loyalty_object(self.customer)
        self.assertEqual(result["status"], "success")
        self.assertEqual(success.http.calls[0][2]["loyaltyPoints"]["balance"]["string"], "2 / 8")
        self.business.loyalty_type = "points"; self.customer.current_points = 50
        points = self.service([FakeResponse(200, {})]); points.update_loyalty_object(self.customer)
        self.assertEqual(points.http.calls[0][2]["loyaltyPoints"]["balance"]["string"], "50 pts")
        for status, expected in ((400, "error"), (401, "error"), (403, "error"), (429, "retry")):
            with self.subTest(status=status):
                self.assertEqual(self.service([FakeResponse(status)]).update_loyalty_object(self.customer)["status"], expected)
        timeout = self.service([requests.Timeout()]).update_loyalty_object(self.customer)
        self.assertEqual(timeout["status"], "retry")

    def test_missing_object_is_recreated_before_reporting_sync_success(self):
        service = self.service([
            FakeResponse(404),
            FakeResponse(200, {"id": "class"}),
            FakeResponse(200, {"id": "class"}),
            FakeResponse(404),
            FakeResponse(200, {"id": "object"}),
        ])
        result = service.update_loyalty_object(self.customer)
        self.assertEqual(result["status"], "success")
        self.assertEqual([call[0] for call in service.http.calls], ["PATCH", "GET", "PATCH", "GET", "POST"])

    def test_disabled_endpoint_is_generic_and_wallet_token_remains_required(self):
        self.app.config["GOOGLE_WALLET_MODE"] = "disabled"
        url = f"/api/customers/{self.customer.id}/wallet/google"
        self.assertEqual(self.app.test_client().get(url).status_code, 403)
        response = self.app.test_client().get(url, query_string={"token": issue_wallet_token(self.customer)})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("credential", response.get_data(as_text=True).lower())

    def test_errors_do_not_log_credentials_tokens_or_private_data(self):
        service = self.service([FakeResponse(403)])
        with self.assertLogs("app.services.google_wallet", level="WARNING") as captured:
            result = service.update_loyalty_object(self.customer)
        output = " ".join(captured.output)
        self.assertEqual(result["status"], "error")
        for secret in (self.credentials["private_key"], self.credentials["client_email"], self.customer.qr_code_token):
            self.assertNotIn(secret, output)

    def test_persistent_outbox_retries_without_changing_ledger_balance(self):
        transaction = Transaction(business_id=self.business.id, customer_id=self.customer.id,
                                  type="earn", stamps_amount=1, request_id="wallet-outbox")
        db.session.add(transaction); db.session.flush()
        job = GoogleWalletSyncJob(business_id=self.business.id, customer_id=self.customer.id,
                                  transaction_id=transaction.id)
        db.session.add(job); db.session.commit()

        class RetryService:
            def update_loyalty_object(self, customer):
                return {"status": "retry", "status_code": 429}

        self.assertEqual(process_pending_google_wallet_jobs(service=RetryService()), 1)
        db.session.refresh(job)
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.attempts, 1)
        self.assertEqual(self.customer.current_stamps, 2)

        job.next_attempt_at = None; db.session.commit()
        class SuccessService:
            def update_loyalty_object(self, customer):
                return {"status": "success"}

        self.assertEqual(process_pending_google_wallet_jobs(service=SuccessService()), 1)
        db.session.refresh(job)
        self.assertEqual(job.status, "completed")

    def _job(self, request_id, tx_type="earn"):
        transaction = Transaction(
            business_id=self.business.id, customer_id=self.customer.id,
            type=tx_type, stamps_amount=1, request_id=request_id,
        )
        db.session.add(transaction); db.session.flush()
        job = GoogleWalletSyncJob(
            business_id=self.business.id, customer_id=self.customer.id,
            transaction_id=transaction.id,
        )
        db.session.add(job); db.session.commit()
        return job

    def test_outbox_watermark_keeps_newer_job_pending_and_sends_current_balance(self):
        old_job = self._job("old-two-stamps")
        sent = []

        class CaptureService:
            def update_loyalty_object(_self, customer):
                sent.append(customer.current_stamps)
                if len(sent) == 1:
                    customer.current_stamps = 3
                    transaction = Transaction(
                        business_id=self.business.id, customer_id=customer.id,
                        type="earn", stamps_amount=1, request_id="new-three-stamps",
                    )
                    db.session.add(transaction); db.session.flush()
                    db.session.add(GoogleWalletSyncJob(
                        business_id=self.business.id, customer_id=customer.id,
                        transaction_id=transaction.id,
                    ))
                    db.session.flush()
                return {"status": "success"}

        self.assertEqual(process_pending_google_wallet_jobs(limit=1, service=CaptureService()), 1)
        new_job = GoogleWalletSyncJob.query.filter(
            GoogleWalletSyncJob.id != old_job.id
        ).one()
        db.session.refresh(old_job)
        self.assertEqual(old_job.status, "completed")
        self.assertEqual(new_job.status, "pending")
        self.assertEqual(sent, [2])
        self.assertEqual(process_pending_google_wallet_jobs(limit=1, service=CaptureService()), 1)
        db.session.refresh(new_job)
        self.assertEqual(new_job.status, "completed")
        self.assertEqual(sent[-1], 3)

    def test_customer_fence_rejects_second_worker_and_recovers_stale_claim(self):
        stale_job = self._job("stale-worker")
        stale_job.status = "processing"
        stale_job.locked_at = _now() - timedelta(minutes=6)
        db.session.commit()
        with _customer_lock(self.customer.id) as first:
            self.assertTrue(first)
            with _customer_lock(self.customer.id) as second:
                self.assertFalse(second)
        db.session.rollback()

        class SuccessService:
            def update_loyalty_object(_self, customer):
                return {"status": "success"}

        self.assertEqual(process_pending_google_wallet_jobs(service=SuccessService()), 1)
        db.session.refresh(stale_job)
        self.assertEqual(stale_job.status, "completed")

    def test_worker_exception_and_temporary_and_permanent_errors_are_visible(self):
        failed_call = self._job("worker-exception")

        class RaisingService:
            def update_loyalty_object(_self, customer):
                raise RuntimeError("external failure")

        self.assertEqual(process_pending_google_wallet_jobs(service=RaisingService()), 1)
        db.session.refresh(failed_call)
        self.assertEqual((failed_call.status, failed_call.attempts), ("pending", 1))
        failed_call.next_attempt_at = None; db.session.commit()

        class PermanentService:
            def update_loyalty_object(_self, customer):
                return {"status": "error", "status_code": 403}

        self.assertEqual(process_pending_google_wallet_jobs(service=PermanentService()), 1)
        db.session.refresh(failed_call)
        self.assertEqual((failed_call.status, failed_call.last_status_code), ("failed", 403))
        self.assertEqual(google_wallet_sync_counts()["failed"], 1)

    def test_worker_cli_reports_only_safe_aggregate_counts(self):
        result = self.app.test_cli_runner().invoke(args=["process-google-wallet-sync"])
        self.assertEqual(result.exit_code, 0)
        output = json.loads(result.output.strip())
        self.assertEqual(output, {
            "completed": 0, "failed": 0, "pending": 0, "processed": 0,
        })
        self.assertNotIn(self.customer.email, result.output)
        self.assertNotIn(self.customer.qr_code_token, result.output)

    def test_rapid_earn_and_redeem_are_coalesced_to_latest_confirmed_balance(self):
        self._job("rapid-earn", "earn")
        self._job("rapid-redeem", "redeem")
        self.customer.current_stamps = 1; db.session.commit()
        sent = []

        class CaptureService:
            def update_loyalty_object(_self, customer):
                sent.append(customer.current_stamps)
                return {"status": "success"}

        self.assertEqual(process_pending_google_wallet_jobs(service=CaptureService()), 2)
        self.assertEqual(sent, [1])
        self.assertEqual(GoogleWalletSyncJob.query.filter_by(status="pending").count(), 0)

    def test_business_transaction_and_outbox_commit_together(self):
        self.app.config["GOOGLE_WALLET_MODE"] = "real"
        result = process_loyalty_transaction(
            business=self.business, customer_id=self.customer.id, staff_id=None,
            action="earn_stamp", tx_type="earn", points_amount=0,
            stamps_amount=1, purchase_amount=0,
            request_id="wallet-transactional-outbox", fingerprint="f" * 64,
        )
        job = GoogleWalletSyncJob.query.filter_by(transaction_id=result.transaction.id).one()
        self.assertEqual(job.business_id, self.business.id)
        self.assertEqual(job.customer_id, self.customer.id)
        self.assertEqual(job.status, "pending")
        self.assertEqual(result.customer.current_stamps, 3)


if __name__ == "__main__":
    unittest.main()
