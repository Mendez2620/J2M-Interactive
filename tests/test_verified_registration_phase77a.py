import unittest
import re
import time
import tempfile
import logging
from http.cookies import SimpleCookie
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta

from app import create_app
from app.extensions import db
from app.email_service import MailDeliveryError
from app.models import (Business, Customer, CustomerRegistrationChallenge,
                        CustomerRegistrationIdentityClaim, RegistrationEmailJob)
from app.config import TestingConfig
from app.services.registration_email_service import (
    _claim_job, _finish, _now, process_pending_registration_emails,
    run_registration_email_worker,
)


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
        process_pending_registration_emails()
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
        job = RegistrationEmailJob.query.one()
        self.assertIsNone(job.encrypted_code)
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
        process_pending_registration_emails()
        failed_job = RegistrationEmailJob.query.order_by(RegistrationEmailJob.id.desc()).first()
        self.assertEqual(failed_job.status, "pending")
        self.assertEqual(failed_job.attempts, 1)
        mailer.send_registration_code = original
        failed_job.next_attempt_at = None; db.session.commit()
        process_pending_registration_emails()
        self.assertEqual(db.session.get(RegistrationEmailJob, failed_job.id).status, "completed")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Customer.query.count(), 0)

    def test_mail_retries_stop_at_configured_limit(self):
        self.app.config["REGISTRATION_EMAIL_MAX_ATTEMPTS"] = 2
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
        mailer = self.app.extensions["recovery_mailer"]
        with patch.object(mailer, "send_registration_code", side_effect=MailDeliveryError("smtp unavailable")) as send:
            process_pending_registration_emails()
            job = RegistrationEmailJob.query.one()
            self.assertEqual((job.status, job.attempts), ("pending", 1))
            job.next_attempt_at = None; db.session.commit()
            process_pending_registration_emails()
            self.assertEqual((job.status, job.attempts), ("failed", 2))
            process_pending_registration_emails()
            self.assertEqual(send.call_count, 2)

    def test_new_existing_and_limited_requests_have_identical_public_response(self):
        self.app.config["REGISTRATION_RESPONSE_MIN_MS"] = 20
        started = time.monotonic(); fresh = self.client.post("/join/uno", json=self.payload); fresh_elapsed = time.monotonic() - started
        business = Business.query.filter_by(slug="uno").one()
        db.session.add(Customer(business_id=business.id, full_name="Existe", email="existe@example.test", phone="+525500000001")); db.session.commit()
        duplicate_payload = dict(self.payload); duplicate_payload.update(email="existe@example.test", phone="55 0000 0001")
        started = time.monotonic(); duplicate = self.client.post("/join/uno", json=duplicate_payload); duplicate_elapsed = time.monotonic() - started
        self.app.config["REGISTRATION_REQUEST_LIMIT_IP"] = 0
        limited_payload = dict(self.payload); limited_payload.update(email="limited@example.test", phone="55 0000 0002")
        started = time.monotonic(); limited = self.client.post("/join/uno", json=limited_payload); limited_elapsed = time.monotonic() - started
        self.assertEqual((fresh.status_code, fresh.get_json()), (duplicate.status_code, duplicate.get_json()))
        self.assertEqual((fresh.status_code, fresh.get_json()), (limited.status_code, limited.get_json()))
        self.assertTrue(all(value >= 0.01 for value in (fresh_elapsed, duplicate_elapsed, limited_elapsed)))
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])

    def test_registration_eligibility_is_not_observable_in_session_cookie(self):
        business = Business.query.filter_by(slug="uno").one()
        db.session.add_all([
            Customer(
                business_id=business.id, full_name="Correo existente",
                email="existing@example.test", phone="+525500000001",
            ),
            Customer(
                business_id=business.id, full_name="Teléfono existente",
                email="phone-owner@example.test", phone="+525500000002",
            ),
        ])
        db.session.commit()

        cases = [
            ("new", dict(self.payload, email="new@example.test", phone="55 0000 0010")),
            ("existing_email", dict(self.payload, email="existing@example.test", phone="55 0000 0011")),
            ("existing_phone", dict(self.payload, email="other@example.test", phone="55 0000 0002")),
        ]
        observed = []
        clients = {}
        for index, (name, payload) in enumerate(cases, start=1):
            client = self.app.test_client()
            response = client.post(
                "/join/uno", json=payload,
                environ_base={"REMOTE_ADDR": f"198.51.100.{index}"},
            )
            clients[name] = client
            observed.append((name, response))

        original_email_limit = self.app.config["REGISTRATION_REQUEST_LIMIT_EMAIL"]
        self.app.config["REGISTRATION_REQUEST_LIMIT_EMAIL"] = 0
        limited_client = self.app.test_client()
        limited = limited_client.post(
            "/join/uno",
            json=dict(self.payload, email="limited@example.test", phone="55 0000 0012"),
            environ_base={"REMOTE_ADDR": "198.51.100.4"},
        )
        self.app.config["REGISTRATION_REQUEST_LIMIT_EMAIL"] = original_email_limit
        clients["limited"] = limited_client
        observed.append(("limited", limited))

        cookie_name = self.app.config["SESSION_COOKIE_NAME"]
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        cookie_lengths, cookie_attributes, session_shapes = [], [], []
        challenge_ids = {}
        public_headers = []
        for name, response in observed:
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json(), observed[0][1].get_json())
            header = response.headers.get("Set-Cookie")
            self.assertIsNotNone(header)
            parsed = SimpleCookie(); parsed.load(header)
            morsel = parsed[cookie_name]
            decoded = serializer.loads(morsel.value)
            challenge_id = decoded["customer_registration_challenge_id"]
            challenge_proof = decoded["customer_registration_challenge_proof"]
            self.assertRegex(challenge_id, r"^[0-9a-f]{36}$")
            self.assertRegex(challenge_proof, r"^[0-9a-f]{64}$")
            challenge_ids[name] = challenge_id
            cookie_lengths.append(len(morsel.value))
            cookie_attributes.append(tuple(morsel[key] for key in ("path", "secure", "httponly", "samesite")))
            session_shapes.append(tuple(sorted(decoded)))
            public_headers.append(tuple(sorted(
                (key, value) for key, value in response.headers.items()
                if key.lower() not in {"set-cookie", "x-request-id"}
            )))

        # ItsDangerous may compress random payloads by a few bytes. Real HMACs
        # and random decoy proofs have the same entropy and size distribution.
        self.assertLessEqual(max(cookie_lengths) - min(cookie_lengths), 4)
        self.assertEqual(len(set(cookie_attributes)), 1)
        self.assertEqual(len(set(session_shapes)), 1)
        self.assertEqual(len(set(public_headers)), 1)
        persisted_ids = {item.id for item in CustomerRegistrationChallenge.query.all()}
        self.assertEqual(persisted_ids, {challenge_ids["new"]})
        self.assertEqual(RegistrationEmailJob.query.count(), 1)

        for name in ("existing_email", "existing_phone", "limited"):
            self.assertNotIn(challenge_ids[name], persisted_ids)
            rejected = clients[name].post("/join/uno/verify", json={"code": "00000000"})
            self.assertEqual(rejected.status_code, 400)
        self.assertEqual(Customer.query.count(), 2)

        process_pending_registration_emails()
        self.assertEqual(len(self.app.extensions["recovery_mailer"].outbox), 1)
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox[0]["to"], "new@example.test")

    def test_accepted_request_replaces_old_challenge_without_cookie_signal(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
        real_challenge = CustomerRegistrationChallenge.query.one()
        with self.client.session_transaction() as browser:
            self.assertEqual(browser["customer_registration_challenge_id"], real_challenge.id)

        business = Business.query.filter_by(slug="uno").one()
        db.session.add(Customer(
            business_id=business.id, full_name="Existente",
            email="duplicate@example.test", phone="+525500000099",
        ))
        db.session.commit()
        duplicate = dict(self.payload, email="duplicate@example.test", phone="55 0000 0098")
        response = self.client.post("/join/uno", json=duplicate)
        self.assertEqual(response.status_code, 202)
        with self.client.session_transaction() as browser:
            decoy_id = browser["customer_registration_challenge_id"]
            decoy_proof = browser["customer_registration_challenge_proof"]
        self.assertRegex(decoy_id, r"^[0-9a-f]{36}$")
        self.assertNotEqual(decoy_id, real_challenge.id)
        self.assertIsNone(db.session.get(CustomerRegistrationChallenge, decoy_id))
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "00000000"}).status_code, 400)

        with self.client.session_transaction() as browser:
            browser["customer_registration_challenge_id"] = real_challenge.id
            browser["customer_registration_challenge_proof"] = decoy_proof
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": "00000000"}).status_code, 400)

    def test_http_request_never_waits_for_or_invokes_smtp(self):
        mailer = self.app.extensions["recovery_mailer"]
        original = mailer.send_registration_code

        def slow_send(*args):
            time.sleep(0.3)
            return original(*args)

        mailer.send_registration_code = slow_send
        started = time.monotonic()
        response = self.client.post("/join/uno", json=self.payload)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 202)
        self.assertLess(elapsed, 0.15)
        self.assertEqual(mailer.outbox, [])
        self.assertEqual(RegistrationEmailJob.query.filter_by(status="pending").count(), 1)
        process_pending_registration_emails()
        self.assertEqual(len(mailer.outbox), 1)

    def test_phone_collision_and_unverified_historical_email_send_nothing(self):
        business = Business.query.filter_by(slug="uno").one()
        db.session.add_all([
            Customer(business_id=business.id, full_name="Teléfono", email="old-phone@example.test", phone="+525512345678"),
            Customer(business_id=business.id, full_name="Correo", email="historical@example.test", phone="+525500000099", email_verified_at=None),
        ])
        db.session.commit()
        phone_payload = dict(self.payload, email="new@example.test")
        email_payload = dict(self.payload, email="historical@example.test", phone="55 0000 0098")
        self.assertEqual(self.client.post("/join/uno", json=phone_payload).status_code, 202)
        self.assertEqual(self.client.post("/join/uno", json=email_payload).status_code, 202)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])

    def test_expired_job_is_cancelled_without_sending_and_survives_worker_restart(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
        challenge = CustomerRegistrationChallenge.query.one()
        challenge.expires_at -= timedelta(hours=1); db.session.commit()
        process_pending_registration_emails()
        job = RegistrationEmailJob.query.one()
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(self.app.extensions["recovery_mailer"].outbox, [])

        payload = dict(self.payload, email="restart@example.test", phone="55 9999 9999")
        self.assertEqual(self.client.post("/join/uno", json=payload).status_code, 202)
        pending = RegistrationEmailJob.query.filter_by(status="pending").one()
        pending.status = "processing"
        pending.locked_at = pending.created_at - timedelta(hours=1)
        db.session.commit()
        process_pending_registration_emails()
        self.assertEqual(db.session.get(RegistrationEmailJob, pending.id).status, "completed")
        self.assertEqual(len(self.app.extensions["recovery_mailer"].outbox), 1)

    def test_resend_cancels_old_job_and_only_latest_code_is_delivered(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
        first_challenge = CustomerRegistrationChallenge.query.one()
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
        process_pending_registration_emails()
        jobs = RegistrationEmailJob.query.order_by(RegistrationEmailJob.id).all()
        self.assertEqual([job.status for job in jobs], ["cancelled", "completed"])
        self.assertEqual(len(self.app.extensions["recovery_mailer"].outbox), 1)
        delivered_code = self.app.extensions["recovery_mailer"].outbox[0]["code"]
        db.session.refresh(first_challenge)
        self.assertIsNotNone(first_challenge.consumed_at)
        self.assertEqual(self.client.post("/join/uno/verify", json={"code": delivered_code}).status_code, 201)

    def test_two_workers_claim_one_job_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'registration-workers.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                app = create_app("testing")
            with app.app_context():
                db.create_all(); db.session.add(Business(name="Workers", slug="workers")); db.session.commit()
            self.assertEqual(app.test_client().post("/join/workers", json=self.payload).status_code, 202)

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
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
        job = RegistrationEmailJob.query.one()
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
            if commits == 2:
                raise RuntimeError("forced commit failure")
            return original_commit()

        with patch.object(db.session, "commit", side_effect=fail_final_commit):
            with self.assertRaises(RuntimeError):
                self.client.post("/join/uno", json=self.payload)
        db.session.rollback()
        self.assertEqual(CustomerRegistrationChallenge.query.count(), 0)
        self.assertEqual(RegistrationEmailJob.query.count(), 0)

    def test_worker_logs_never_include_mail_payload_or_identity(self):
        self.assertEqual(self.client.post("/join/uno", json=self.payload).status_code, 202)
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
        self.assertEqual(result.exit_code, 0)
        self.assertIn('"processed": 0', result.output)
        self.assertNotIn("@", result.output)

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
            with app.app_context():
                process_pending_registration_emails()
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
                with app.app_context():
                    process_pending_registration_emails()
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
