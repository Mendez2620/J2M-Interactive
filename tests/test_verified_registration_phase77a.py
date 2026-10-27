import unittest
import hashlib
import re
import time
import tempfile
import logging
from http.cookies import SimpleCookie
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from datetime import date, timedelta

from app import create_app
from app.extensions import db
from app.email_service import MailDeliveryError
from app.models import (Business, Customer, CustomerRegistrationChallenge,
                        CustomerRegistrationIdentityClaim, RegistrationEmailJob)
from app.config import TestingConfig
from app.services.registration_email_service import (
    _claim_job, _finish, _now, process_pending_registration_emails,
    run_registration_email_worker, encrypt_registration_code,
)


class VerifiedRegistrationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(REGISTRATION_REQUEST_LIMIT_PHONE=20, REGISTRATION_REQUEST_LIMIT_IP=50)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.add_all([Business(name="Uno", slug="uno"), Business(name="Dos", slug="dos")]); db.session.commit()
        self.payload = {"full_name": "Ana Prueba", "email": "ana@example.test", "phone_country": "+52", "phone": "55 1234 5678", "birthdate": "1992-04-05", "terms_accepted": True, "privacy_accepted": True}

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def request_code(self, payload=None, slug="uno"):
        response = self.client.post(f"/join/{slug}", json=payload or self.payload)
        process_pending_registration_emails()
        code = self.app.extensions["recovery_mailer"].outbox[-1].get("code") if self.app.extensions["recovery_mailer"].outbox else None
        return response, code

    def legacy_job(self, *, email="legacy@example.test", phone="+525500000001", expired=False):
        now = _now()
        business = Business.query.filter_by(slug="uno").one()
        challenge = CustomerRegistrationChallenge(
            business_id=business.id, full_name="Histórica", email=email,
            email_hash=hashlib.sha256(email.encode()).hexdigest(), phone=phone,
            phone_hash=hashlib.sha256(phone.encode()).hexdigest(), birthdate=date(1990, 1, 1),
            code_hash="a" * 64, terms_version="legacy", privacy_version="legacy",
            consent_ip_hash="b" * 64, consent_user_agent_hash="c" * 64,
            expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=10),
        )
        db.session.add(challenge); db.session.flush()
        job = RegistrationEmailJob(
            business_id=business.id, challenge_id=challenge.id,
            encrypted_code=encrypt_registration_code("12345678"),
        )
        db.session.add(job); db.session.commit()
        return challenge, job

    def test_customer_and_wallet_are_created_atomically_without_email_verification(self):
        with patch.object(self.app.extensions["recovery_mailer"], "send_registration_code") as send:
            response = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(response.status_code, 201)
        customer = Customer.query.one()
        self.assertIsNone(customer.email)
        self.assertIsNone(customer.email_verified_at)
        self.assertIsNotNone(customer.terms_accepted_at)
        self.assertEqual(len(customer.terms_content_hash), 64)
        self.assertEqual(len(customer.privacy_content_hash), 64)
        self.assertIn("wallet", response.get_json()["apple_wallet_url"])
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        send.assert_not_called()

    def test_required_validation_and_impossible_dates(self):
        for changes in ({"birthdate": "2035-01-01"}, {"birthdate": "2020-02-31"},
                        {"privacy_accepted": False}, {"terms_accepted": False},
                        {"phone": ""}, {"full_name": ""}):
            payload = dict(self.payload); payload.update(changes)
            self.assertEqual(self.client.post("/join/uno", json=payload).status_code, 400)
        optional = dict(self.payload); optional.pop("email"); optional["birthdate"] = ""
        self.assertEqual(self.client.post("/join/uno", json=optional).status_code, 201)
        self.assertIsNone(Customer.query.one().birthdate)

    def test_direct_registration_is_single_identity_and_tenant_scoped(self):
        first = self.client.post("/join/uno", json=self.payload)
        replay = self.client.post("/join/uno", json=self.payload)
        other = self.client.post("/join/dos", json=self.payload)
        self.assertEqual((first.status_code, replay.status_code, other.status_code), (201, 409, 201))
        self.assertEqual(Customer.query.count(), 2)
        self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 2)
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)

    def test_duplicates_do_not_modify_existing_customer(self):
        business = Business.query.filter_by(slug="uno").one()
        original = Customer(business_id=business.id, full_name="Existente", email="historico@example.test", phone="+525512345678")
        db.session.add(original); db.session.commit()
        response = self.client.post("/join/uno", json=dict(self.payload, full_name="Atacante"))
        self.assertEqual(response.status_code, 409)
        db.session.refresh(original)
        self.assertEqual((original.full_name, original.email), ("Existente", "historico@example.test"))
        self.assertEqual(Customer.query.count(), 1)

    def test_optional_marketing_consent_is_recorded_separately(self):
        payload = dict(self.payload); payload["marketing_consent"] = True
        response = self.client.post("/join/uno", json=payload)
        self.assertEqual(response.status_code, 201)
        customer = Customer.query.one()
        self.assertTrue(customer.marketing_consent)
        self.assertIsNotNone(customer.marketing_consent_at)
        self.assertEqual(customer.terms_version, self.app.config["TERMS_VERSION"])

    def test_obsolete_verify_is_closed_and_csrf_protects_direct_registration(self):
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "11111111"}).status_code, 410)
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 400)
        page = self.client.get("/join/uno").get_data(as_text=True)
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
        allowed = self.client.post("/join/uno", json=self.payload, headers={"X-CSRFToken": token})
        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(Customer.query.count(), 1)

    def test_database_claim_prevents_duplicate_identity_completion(self):
        business = Business.query.filter_by(slug="uno").one()
        owner = Customer(business_id=business.id, full_name="Dueña", phone="+525512345678")
        db.session.add(owner); db.session.flush()
        from app.routes.business import _identity_hmac
        db.session.add(CustomerRegistrationIdentityClaim(
            business_id=business.id, identity_type="phone",
            identity_hash=_identity_hmac("+525512345678"), customer_id=owner.id,
        ))
        db.session.commit()
        result = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(Customer.query.count(), 1)

    def test_rate_limit_and_mailer_are_independent_from_registration(self):
        self.app.config["REGISTRATION_REQUEST_LIMIT_IP"] = 1
        mailer = self.app.extensions["recovery_mailer"]
        with patch.object(mailer, "send_registration_code", side_effect=MailDeliveryError("fallo")) as send:
            first = self.client.post("/join/uno", json=self.payload)
            second = self.client.post("/join/uno", json=dict(self.payload, phone="55 2222 2222"))
        self.assertEqual((first.status_code, second.status_code), (201, 429))
        self.assertEqual(Customer.query.count(), 1)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        send.assert_not_called()

    def test_registration_never_creates_mail_retry_jobs(self):
        mailer = self.app.extensions["recovery_mailer"]
        with patch.object(mailer, "send_registration_code", side_effect=MailDeliveryError("smtp unavailable")) as send:
            self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 201)
            process_pending_registration_emails()
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        send.assert_not_called()

    def test_new_duplicate_and_limited_requests_have_explicit_safe_statuses(self):
        fresh = self.client.post("/join/uno", json=self.payload)
        duplicate = self.client.post("/join/uno", json=self.payload)
        self.app.config["REGISTRATION_REQUEST_LIMIT_IP"] = 0
        limited = self.client.post("/join/uno", json=dict(self.payload, phone="55 0000 0002"))
        self.assertEqual((fresh.status_code, duplicate.status_code, limited.status_code), (201, 409, 429))
        for response in (duplicate, limited):
            body = response.get_data(as_text=True)
            self.assertNotIn("SELECT ", body)
            self.assertNotIn("customer_registration", body)
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])

    def test_registration_does_not_store_identity_or_challenge_in_session_cookie(self):
        response = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(response.status_code, 201)
        header = response.headers.get("Set-Cookie", "")
        self.assertNotIn("customer_registration_challenge", header)
        self.assertNotIn("ana@example.test", header)
        with self.client.session_transaction() as browser:
            self.assertNotIn("customer_registration_challenge_id", browser)
            self.assertNotIn("customer_registration_challenge_proof", browser)
        duplicate = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(duplicate.status_code, 409)
        self.assertNotIn("Set-Cookie", duplicate.headers)

    def test_direct_registration_never_replaces_or_creates_legacy_challenge(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 201)
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 409)
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        with self.client.session_transaction() as browser:
            self.assertNotIn("customer_registration_challenge_id", browser)

    def test_http_request_never_waits_for_or_invokes_smtp(self):
        mailer = self.app.extensions["recovery_mailer"]
        def slow_send(*_args):
            time.sleep(0.3)
        with patch.object(mailer, "send_registration_code", side_effect=slow_send) as send:
            started = time.monotonic()
            response = self.client.post("/join/uno", json=self.payload)
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 201)
        self.assertLess(elapsed, 0.25)
        self.assertEqual(mailer.outbox, [])
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        send.assert_not_called()

    def test_phone_collision_and_unverified_historical_email_are_preserved(self):
        business = Business.query.filter_by(slug="uno").one()
        phone_owner = Customer(business_id=business.id, full_name="Teléfono", email="old-phone@example.test", phone="+525512345678")
        historical = Customer(business_id=business.id, full_name="Correo", email="historical@example.test", phone="+525500000099", email_verified_at=None)
        db.session.add_all([phone_owner, historical]); db.session.commit()
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 409)
        new_payload = dict(self.payload, email="historical@example.test", phone="55 0000 0098")
        self.assertEqual(self.client.post("/join/uno", json=new_payload).status_code, 201)
        db.session.refresh(historical)
        self.assertEqual(historical.email, "historical@example.test")
        self.assertEqual(Customer.query.filter_by(phone="+525500000098").one().email, None)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)

    def test_expired_job_is_cancelled_without_sending_and_survives_worker_restart(self):
        _challenge, job = self.legacy_job(expired=True)
        process_pending_registration_emails()
        db.session.refresh(job)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])

        _challenge, pending = self.legacy_job(email="restart@example.test", phone="+525599999999")
        pending.status = "processing"
        pending.locked_at = pending.created_at - timedelta(hours=1)
        db.session.commit()
        process_pending_registration_emails()
        self.assertEqual(db.session.get(RegistrationEmailJob, pending.id).status, "completed")
        self.assertEqual(len(self.app.extensions["recovery_mailer"].outbox), 1)

    def test_resend_cancels_old_job_and_only_latest_code_is_delivered(self):
        first_challenge, _first_job = self.legacy_job()
        first_challenge.consumed_at = _now(); db.session.commit()
        self.legacy_job(email="latest@example.test", phone="+525500000002")
        process_pending_registration_emails()
        jobs = RegistrationEmailJob.query.order_by(RegistrationEmailJob.id).all()
        self.assertEqual([job.status for job in jobs], ["cancelled", "completed"])
        self.assertEqual(len(self.app.extensions["recovery_mailer"].outbox), 1)
        db.session.refresh(first_challenge)
        self.assertIsNotNone(first_challenge.consumed_at)
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "12345678"}).status_code, 410)

    def test_two_workers_claim_one_job_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'registration-workers.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all()
                business = Business(name="Workers", slug="workers")
                db.session.add(business); db.session.flush()
                challenge = CustomerRegistrationChallenge(
                    business_id=business.id, full_name="Histórica", email="legacy@example.test",
                    email_hash="a" * 64, phone="+525500000001", phone_hash="b" * 64,
                    birthdate=date(1990, 1, 1), code_hash="c" * 64,
                    terms_version="legacy", privacy_version="legacy",
                    consent_ip_hash="d" * 64, consent_user_agent_hash="e" * 64,
                    expires_at=_now() + timedelta(minutes=10),
                )
                db.session.add(challenge); db.session.flush()
                db.session.add(RegistrationEmailJob(
                    business_id=business.id, challenge_id=challenge.id,
                    encrypted_code=encrypt_registration_code("12345678"),
                ))
                db.session.commit()

            def work(_):
                with app.app_context():
                    return process_pending_registration_emails()

            with ThreadPoolExecutor(max_workers=2) as executor:
                processed = list(executor.map(work, range(2)))
            with app.app_context():
                self.assertEqual(RegistrationEmailJob.query.filter_by(status="completed").count(), 1)
                db.session.remove()
            self.assertEqual(sum(processed), 1)
            self.assertEqual(len(app.extensions["recovery_mailer"].outbox), 1)

    def test_stale_worker_cannot_finish_newer_lease(self):
        _challenge, job = self.legacy_job()
        now = _now()
        lease_a = _claim_job(job.id, now=now, lease_token="a" * 64)
        self.assertEqual(lease_a, "a" * 64)
        RegistrationEmailJob.query.filter_by(id=job.id, lease_token=lease_a).update({
            "locked_at": now - timedelta(seconds=self.app.config["REGISTRATION_EMAIL_STALE_SECONDS"] + 1)
        }, synchronize_session=False)
        db.session.commit()
        lease_b = _claim_job(job.id, now=now, lease_token="b" * 64)
        self.assertEqual(lease_b, "b" * 64)

        self.assertFalse(_finish(job.id, lease_a, "completed", now=now))
        current = db.session.get(RegistrationEmailJob, job.id)
        self.assertEqual((current.status, current.lease_token), ("processing", lease_b))
        self.assertIsNotNone(current.encrypted_code)

        self.assertTrue(_finish(job.id, lease_b, "completed", now=now))
        current = db.session.get(RegistrationEmailJob, job.id)
        self.assertEqual(current.status, "completed")
        self.assertIsNone(current.lease_token)
        self.assertIsNone(current.encrypted_code)

    def test_challenge_and_outbox_roll_back_together(self):
        original_commit = db.session.commit
        commits = 0

        def fail_final_commit():
            nonlocal commits
            commits += 1
            if commits == 1:
                raise RuntimeError("forced commit failure")
            return original_commit()

        with patch.object(db.session, "commit", side_effect=fail_final_commit):
            with self.assertRaises(RuntimeError):
                self.client.post("/join/uno", json=self.payload)
        db.session.rollback()
        self.assertEqual(Customer.query.count(), 0)
        self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 0)
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)

    def test_worker_logs_never_include_mail_payload_or_identity(self):
        self.legacy_job(email="ana@example.test")
        logger = logging.getLogger("registration-email-test")
        with patch("app.services.registration_email_service.process_pending_registration_emails",
                   side_effect=RuntimeError("ana@example.test 12345678 smtp-password")), \
             self.assertLogs(logger, level="ERROR") as captured:
            run_registration_email_worker(max_cycles=1, logger=logger)
        output = " ".join(captured.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("ana@example.test", output)
        self.assertNotIn("12345678", output)
        self.assertNotIn("smtp-password", output)

    def test_worker_cli_handles_empty_queue_without_sensitive_output(self):
        result = self.app.test_cli_runner().invoke(args=["process-registration-emails"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("No such command", result.output)
        self.assertNotIn("@", result.output)

    def test_suspension_after_code_request_blocks_completion(self):
        business = Business.query.filter_by(slug="uno").one()
        business.is_active = False; db.session.commit()
        result = self.client.post("/join/uno", json=self.payload)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(Customer.query.count(), 0)
        self.assertNotIn("wallet", result.get_data(as_text=True).lower())

    def test_secret_key_rotation_does_not_change_identity_claim_hashes(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 201)
        hashes = {claim.identity_hash for claim in CustomerRegistrationIdentityClaim.query.all()}
        self.app.config["SECRET_KEY"] = "rotated-session-secret-key-with-more-than-32-characters"
        from app.routes.business import _identity_hmac
        self.assertIn(_identity_hmac("+525512345678"), hashes)

    def test_concurrent_direct_registration_creates_one_customer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'registration.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all(); db.session.add(Business(name="Concurrente", slug="concurrente")); db.session.commit()
            def register_once(_):
                client = app.test_client()
                return client.post("/join/concurrente", json=self.payload).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(register_once, range(2)))
            with app.app_context():
                self.assertEqual(Customer.query.count(), 1)
                self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 1)
                self.assertEqual(RegistrationEmailJob.query.count(), 0)
                db.session.remove()
            self.assertEqual(statuses.count(201), 1)
            self.assertEqual(statuses.count(409), 1)

    def test_concurrent_payloads_cannot_claim_same_phone_twice(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'registration-phone.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all(); db.session.add(Business(name="Concurrente", slug="concurrente")); db.session.commit()

            payloads = [dict(self.payload, full_name=f"Persona {index}", email=f"persona{index}@example.test") for index in range(2)]
            def register_once(payload):
                client = app.test_client()
                return client.post("/join/concurrente", json=payload).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(executor.map(register_once, payloads))
            with app.app_context():
                self.assertEqual(Customer.query.count(), 1)
                self.assertEqual(CustomerRegistrationIdentityClaim.query.count(), 1)
                self.assertIn(Customer.query.one().full_name, {"Persona 0", "Persona 1"})
                db.session.remove()
            self.assertEqual(statuses.count(201), 1)
            self.assertEqual(statuses.count(409), 1)


if __name__ == "__main__":
    unittest.main()
