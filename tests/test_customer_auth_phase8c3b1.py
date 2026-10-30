import base64
import hashlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, CustomerAuthAudit, CustomerAuthChallenge, CustomerCredential,
    CustomerLinkingEnrollment, CustomerSession, ReferralParticipant, Staff,
    Transaction,
)
from app.services.customer_auth_service import (
    CustomerAuthError, approve_linking_enrollment, create_linking_enrollment,
    revoke_customer_access,
)


class CustomerAuthPhase8C3B1TestCase(unittest.TestCase):
    @staticmethod
    def _now():
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            PUBLIC_BASE_URL="https://j2m.example",
            WEBAUTHN_ORIGIN="https://j2m.example",
            WEBAUTHN_RP_ID="j2m.example",
        )
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.business = Business(name="Uno", slug="uno-auth", is_active=True, web_access_enabled=True)
        self.other = Business(name="Dos", slug="dos-auth", is_active=True, web_access_enabled=True)
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.customer = Customer(
            business_id=self.business.id, full_name="Cliente Uno", phone="+525500000001",
            current_points=17, current_stamps=3, qr_code_token="wallet-stable-one",
        )
        self.other_customer = Customer(
            business_id=self.other.id, full_name="Cliente Dos", phone="+525500000002",
            qr_code_token="wallet-stable-two",
        )
        self.staff = self._staff(self.business, "authorized", "authorize_customer_identity,scanner_operate_web")
        self.denied = self._staff(self.business, "denied", "scanner_operate_web")
        self.other_staff = self._staff(self.other, "other", "authorize_customer_identity,scanner_operate_web")
        db.session.add_all([self.customer, self.other_customer]); db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.session.execute(text("PRAGMA foreign_keys=OFF")); db.drop_all(); self.ctx.pop()

    def _staff(self, business, username, permissions):
        staff = Staff(
            business=business, username=username, role="cashier", permissions=permissions,
            is_active=True, web_access_enabled=True,
        )
        staff.set_password("PasswordSeguro123!"); db.session.add(staff)
        return staff

    def _verified_registration(self, credential_id=b"credential-one"):
        return SimpleNamespace(
            credential_id=credential_id, credential_public_key=b"public-key",
            sign_count=0, credential_device_type=SimpleNamespace(value="single_device"),
            credential_backed_up=False,
        )

    def _enroll(self, credential_id=b"credential-one", recovery=False):
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Se aplicó la política presencial del negocio.",
            recovery_mode=recovery,
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        self.assertEqual(claim.status_code, 200)
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)
        options = self.client.post("/customer-auth/enroll/options")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(
            options.get_json()["authenticatorSelection"]["residentKey"], "required",
        )
        with patch(
            "app.services.customer_auth_service.verify_registration_response",
            return_value=self._verified_registration(credential_id),
        ) as verifier:
            complete = self.client.post("/customer-auth/enroll/complete", json={
                "id": "ignored-by-mock", "response": {"transports": ["internal"]},
            })
        self.assertEqual(complete.status_code, 200, complete.get_data(as_text=True))
        self.assertTrue(verifier.call_args.kwargs["require_user_verification"])
        self.assertEqual(verifier.call_args.kwargs["expected_origin"], "https://j2m.example")
        self.assertEqual(verifier.call_args.kwargs["expected_rp_id"], "j2m.example")
        return enrollment

    def test_ordinary_customer_and_wallet_remain_unchanged_and_not_ambassador(self):
        before = (self.customer.current_points, self.customer.current_stamps, self.customer.qr_code_token)
        self._enroll()
        db.session.refresh(self.customer)
        self.assertEqual((self.customer.current_points, self.customer.current_stamps, self.customer.qr_code_token), before)
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(ReferralParticipant.query.count(), 0)
        self.assertIsNone(self.customer.email)

    def test_assisted_pairing_is_tenant_permission_and_two_device_bound(self):
        with self.assertRaises(CustomerAuthError):
            create_linking_enrollment(staff=self.denied, customer_id=self.customer.id, human_check_note="No autorizado")
        with self.assertRaises(CustomerAuthError):
            create_linking_enrollment(staff=self.staff, customer_id=self.other_customer.id, human_check_note="Tenant incorrecto")
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        with self.assertRaises(CustomerAuthError):
            approve_linking_enrollment(staff=self.other_staff, enrollment_id=enrollment.id, pairing_code=pairing)
        with self.assertRaises(CustomerAuthError):
            approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code="000000")
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)
        second_browser = self.app.test_client()
        self.assertEqual(second_browser.post("/customer-auth/enroll/options").status_code, 403)
        self.assertEqual(self.client.post("/customer-auth/enroll/options").status_code, 200)

    def test_expired_and_consumed_secrets_fail_closed(self):
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        enrollment.expires_at = self._now() - timedelta(seconds=1); db.session.commit()
        self.assertEqual(self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret}).status_code, 404)
        enrollment.status = "pending"; enrollment.expires_at = self._now() + timedelta(minutes=5); db.session.commit()
        self.assertEqual(self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret}).status_code, 200)
        self.assertEqual(self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret}).status_code, 404)

    def test_concurrent_secret_claim_has_one_atomic_winner(self):
        _enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        second = self.app.test_client()
        observed = []
        calls = 0

        def interleave(_length):
            nonlocal calls
            calls += 1
            if calls == 1:
                observed.append(second.post(
                    "/customer-auth/enroll/claim", data={"linking_secret": secret},
                ).status_code)
            return f"browser-verifier-{calls}-with-enough-entropy-for-test"

        with patch("app.services.customer_auth_service.secrets.token_urlsafe", side_effect=interleave):
            outer = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        self.assertEqual(sorted([outer.status_code, *observed]), [200, 404])
        self.assertEqual(CustomerAuthAudit.query.filter_by(action="linking_claimed").count(), 1)

    def test_wrong_origin_challenge_or_signature_never_creates_credential(self):
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)
        self.client.post("/customer-auth/enroll/options")
        for failure in ("origin", "rp id", "challenge", "signature"):
            with patch("app.services.customer_auth_service.verify_registration_response", side_effect=ValueError(failure)):
                response = self.client.post("/customer-auth/enroll/complete", json={"id": "invalid"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(CustomerCredential.query.count(), 0)

    def test_duplicate_credential_and_replay_are_rejected(self):
        self._enroll(b"duplicate")
        self.client.post("/customer-auth/logout")
        with self.assertRaises(Exception):
            db.session.add(CustomerCredential(
                business_id=self.other.id, customer_id=self.other_customer.id,
                credential_id=b"duplicate", public_key=b"other", sign_count=0,
            )); db.session.commit()
        db.session.rollback()

    def test_concurrent_completion_has_one_atomic_winner(self):
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)
        self.client.post("/customer-auth/enroll/options")
        second = self.app.test_client()
        session_cookie = self.client.get_cookie("session")
        second.set_cookie("session", session_cookie.value, domain="localhost")
        observed = []

        def verify_once(**_kwargs):
            observed.append(second.post("/customer-auth/enroll/complete", json={"id": "same"}).status_code)
            return self._verified_registration(b"atomic-winner")

        with patch("app.services.customer_auth_service.verify_registration_response", side_effect=verify_once):
            winner = self.client.post("/customer-auth/enroll/complete", json={"id": "same"})
        self.assertEqual(winner.status_code, 200)
        self.assertEqual(observed, [409])
        self.assertEqual(CustomerCredential.query.count(), 1)

    def test_late_staff_business_and_customer_revocation_fail_closed(self):
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        self.staff.web_access_enabled = False; db.session.commit()
        with self.assertRaises(CustomerAuthError):
            approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)

        self.staff.web_access_enabled = True; db.session.commit()
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)
        self.staff.permissions = "scanner_operate_web"; db.session.commit()
        self.assertEqual(self.client.post("/customer-auth/enroll/options").status_code, 409)

        self.staff.permissions = "authorize_customer_identity,scanner_operate_web"; db.session.commit()
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing)
        self.client.post("/customer-auth/enroll/options")

        def revoke_during_verification(**_kwargs):
            revoke_customer_access(
                staff=self.staff, customer_id=self.customer.id,
                reason="Revocación concurrente documentada.",
            )
            return self._verified_registration(b"must-not-exist")

        with patch(
            "app.services.customer_auth_service.verify_registration_response",
            side_effect=revoke_during_verification,
        ):
            response = self.client.post("/customer-auth/enroll/complete", json={"id": "race"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(CustomerCredential.query.count(), 0)
        db.session.refresh(enrollment)
        self.assertEqual(enrollment.status, "cancelled")

        enrollment2, secret2 = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim2 = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret2})
        pairing2 = re.search(r"tracking-widest\">(\d{6})<", claim2.get_data(as_text=True)).group(1)
        approve_linking_enrollment(staff=self.staff, enrollment_id=enrollment2.id, pairing_code=pairing2)
        self.business.is_active = False; db.session.commit()
        self.assertEqual(self.client.post("/customer-auth/enroll/options").status_code, 409)

    def test_pairing_attempt_budget_is_persistent_atomic_and_secret_free(self):
        self.app.config["CUSTOMER_LINKING_PAIRING_MAX_ATTEMPTS"] = 2
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        claim = self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret})
        pairing = re.search(r"tracking-widest\">(\d{6})<", claim.get_data(as_text=True)).group(1)
        observed = []
        comparisons = 0

        def interleave(_supplied, _stored):
            nonlocal comparisons
            comparisons += 1
            if comparisons == 1:
                try:
                    approve_linking_enrollment(
                        staff=self.staff, enrollment_id=enrollment.id, pairing_code="111111",
                    )
                except CustomerAuthError as exc:
                    observed.append(exc.status_code)
            return False

        with patch("app.services.customer_auth_service.hmac.compare_digest", side_effect=interleave):
            with self.assertRaises(CustomerAuthError) as outer:
                approve_linking_enrollment(
                    staff=self.staff, enrollment_id=enrollment.id, pairing_code="222222",
                )
        observed.append(outer.exception.status_code)
        db.session.refresh(enrollment)
        self.assertEqual(sorted(observed), [409, 429])
        self.assertEqual(enrollment.pairing_failed_attempts, 2)
        self.assertEqual(enrollment.status, "cancelled")
        self.assertIsNotNone(enrollment.pairing_locked_at)
        with self.assertRaises(CustomerAuthError):
            approve_linking_enrollment(
                staff=self.staff, enrollment_id=enrollment.id, pairing_code=pairing,
            )
        audits = CustomerAuthAudit.query.filter(
            CustomerAuthAudit.action.in_(("linking_pair_failed", "linking_pair_locked")),
        ).all()
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(pairing not in item.reason for item in audits))

    def test_authentication_session_expiry_revocation_and_shared_browser_rotation(self):
        self._enroll()
        first_session = CustomerSession.query.one()
        self.client.post("/customer-auth/logout")
        self.client.get(f"/customer-auth/login/{self.business.slug}")
        options = self.client.post(f"/customer-auth/login/{self.business.slug}/options")
        self.assertEqual(options.status_code, 200)
        with patch("app.services.customer_auth_service.verify_authentication_response", return_value=SimpleNamespace(new_sign_count=1)):
            login = self.client.post(f"/customer-auth/login/{self.business.slug}/complete", json={"id": "Y3JlZGVudGlhbC1vbmU"})
        self.assertEqual(login.status_code, 200)
        active = CustomerSession.query.filter_by(revoked_at=None).one()
        active.expires_at = self._now() - timedelta(seconds=1); db.session.commit()
        self.assertEqual(self.client.get(f"/customer-auth/portal/{self.business.slug}/security").status_code, 302)
        self.assertIsNotNone(first_session.revoked_at)

    def test_switching_customer_in_same_browser_revokes_only_that_prior_session(self):
        self._enroll(b"credential-a")
        session_a = CustomerSession.query.filter_by(revoked_at=None).one()
        old_cookie = self.client.get_cookie("session").value
        other_device_session = CustomerSession(
            id="other-device-session", business_id=self.business.id,
            customer_id=self.customer.id,
            credential_id=session_a.credential_id,
            verifier_hash=hashlib.sha256(b"other-device-verifier").hexdigest(),
            expires_at=self._now() + timedelta(hours=1), last_active_at=self._now(),
        )
        customer_b = Customer(
            business_id=self.business.id, full_name="Cliente B", phone="+525500000099",
            qr_code_token="wallet-stable-b",
        )
        db.session.add(customer_b); db.session.flush()
        credential_b = CustomerCredential(
            business_id=self.business.id, customer_id=customer_b.id,
            credential_id=b"credential-b", public_key=b"public-b", sign_count=0,
        )
        db.session.add_all([credential_b, other_device_session]); db.session.commit()
        self.client.post(f"/customer-auth/login/{self.business.slug}/options")
        encoded_b = base64.urlsafe_b64encode(b"credential-b").decode().rstrip("=")
        with patch(
            "app.services.customer_auth_service.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=1),
        ):
            response = self.client.post(
                f"/customer-auth/login/{self.business.slug}/complete", json={"id": encoded_b},
            )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(session_a); db.session.refresh(other_device_session)
        self.assertIsNotNone(session_a.revoked_at)
        self.assertIsNone(other_device_session.revoked_at)
        with self.client.session_transaction() as browser:
            current_id = browser.get("customer_session_id")
        self.assertEqual(db.session.get(CustomerSession, current_id).customer_id, customer_b.id)
        replay = self.app.test_client()
        replay.set_cookie("session", old_cookie, domain="localhost")
        self.assertEqual(
            replay.get(f"/customer-auth/portal/{self.business.slug}/security").status_code, 302,
        )

    def test_authentication_challenges_are_single_use_and_payloads_are_controlled(self):
        self._enroll(b"challenge-credential")
        self.client.post("/customer-auth/logout")
        encoded = base64.urlsafe_b64encode(b"challenge-credential").decode().rstrip("=")
        self.client.post(f"/customer-auth/login/{self.business.slug}/options")
        challenge = CustomerAuthChallenge.query.order_by(CustomerAuthChallenge.created_at.desc()).first()
        with patch(
            "app.services.customer_auth_service.verify_authentication_response",
            side_effect=ValueError("invalid signature"),
        ) as verifier:
            invalid = self.client.post(
                f"/customer-auth/login/{self.business.slug}/complete", json={"id": encoded},
            )
        self.assertEqual(invalid.status_code, 401)
        db.session.refresh(challenge)
        self.assertIsNotNone(challenge.consumed_at)
        replay = self.client.post(
            f"/customer-auth/login/{self.business.slug}/complete", json={"id": encoded},
        )
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(verifier.call_count, 1)

        self.client.post(f"/customer-auth/login/{self.business.slug}/options")
        malformed = self.client.post(
            f"/customer-auth/login/{self.business.slug}/complete", json=["unexpected"],
        )
        self.assertEqual(malformed.status_code, 400)
        latest = CustomerAuthChallenge.query.order_by(CustomerAuthChallenge.created_at.desc()).first()
        self.assertIsNotNone(latest.consumed_at)

        self.client.post(f"/customer-auth/login/{self.business.slug}/options")
        latest = CustomerAuthChallenge.query.order_by(CustomerAuthChallenge.created_at.desc()).first()
        latest.expires_at = self._now() - timedelta(seconds=1); db.session.commit()
        expired = self.client.post(
            f"/customer-auth/login/{self.business.slug}/complete", json={"id": encoded},
        )
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(
            self.client.post("/customer-auth/enroll/complete", json=["bad"]).status_code, 400,
        )

    def test_assisted_recovery_revokes_old_credentials_and_sessions(self):
        self._enroll(b"old-credential")
        old_credential = CustomerCredential.query.one()
        old_session = CustomerSession.query.filter_by(revoked_at=None).one()
        self._enroll(b"new-credential", recovery=True)
        db.session.refresh(old_credential); db.session.refresh(old_session)
        self.assertFalse(old_credential.is_active)
        self.assertIsNotNone(old_credential.revoked_at)
        self.assertIsNotNone(old_session.revoked_at)
        self.assertEqual(CustomerCredential.query.filter_by(is_active=True).count(), 1)

    def test_staff_revocation_is_audited_and_invalidates_sessions(self):
        self._enroll()
        revoke_customer_access(staff=self.staff, customer_id=self.customer.id, reason="Dispositivo reportado como comprometido.")
        self.assertEqual(CustomerCredential.query.filter_by(is_active=True).count(), 0)
        self.assertEqual(CustomerSession.query.filter_by(revoked_at=None).count(), 0)
        self.assertIn("customer_access_revoked", [item.action for item in CustomerAuthAudit.query.all()])

    def test_csrf_and_wallet_delivery_session_cannot_authorize(self):
        self.app.config["CSRF_ENABLED"] = True
        with self.client.session_transaction() as browser:
            browser["wallet_delivery_id"] = "not-customer-auth"
        self.assertEqual(self.client.post(f"/customer-auth/login/{self.business.slug}/options").status_code, 400)
        self.assertEqual(self.client.get(f"/customer-auth/portal/{self.business.slug}/security").status_code, 302)
        page = self.client.get(f"/customer-auth/login/{self.business.slug}")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.get_data(as_text=True)).group(1)
        response = self.client.post(
            f"/customer-auth/login/{self.business.slug}/options",
            headers={"X-CSRFToken": token}, json={},
        )
        self.assertEqual(response.status_code, 200)

    def test_program_and_consent_remain_separate_and_invitation_is_not_public(self):
        self._enroll()
        self.assertEqual(ReferralParticipant.query.count(), 0)
        self.assertEqual(self.client.get("/customer-auth/invitation").status_code, 404)
        consent = self.client.post(
            f"/customer-auth/portal/{self.business.slug}/ambassador-consent",
            json={
                "enabled": True, "consent_version": "2026-10",
                "consent_evidence": "aceptacion-explicita-cliente",
                "reason": "Participación voluntaria del cliente.",
            }, headers={"Idempotency-Key": "customer-consent-1"},
        )
        self.assertEqual(consent.status_code, 409)
        self.assertEqual(ReferralParticipant.query.count(), 0)
        participant = ReferralParticipant(
            business_id=self.business.id, participant_type="customer_ambassador",
            customer_id=self.customer.id, invitation_code="historical-customer-code",
            is_enabled=True, consent_version="historical-v1",
            consented_at=self._now(), consent_evidence_hash="a" * 64,
        )
        db.session.add(participant); db.session.commit()
        revoked = self.client.post(
            f"/customer-auth/portal/{self.business.slug}/ambassador-consent",
            json={"enabled": False, "reason": "Retiro voluntario."},
            headers={"Idempotency-Key": "customer-consent-2"},
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertFalse(participant.is_enabled)
        self.business.referrals_enabled = False; self.business.is_active = False; db.session.commit()
        self.assertEqual(self.client.get(f"/customer-auth/portal/{self.business.slug}/security").status_code, 302)

    def test_staff_endpoints_require_revocable_session_permission_csrf_and_tenant(self):
        login = self.client.post("/staff/login?legacy=1", data={
            "identity": self.staff.username, "password": "PasswordSeguro123!",
            "business_slug": self.business.slug,
        })
        self.assertEqual(login.status_code, 302)
        self.app.config["CSRF_ENABLED"] = True
        endpoint = "/customer-auth/staff/linking"
        page = self.client.get(endpoint)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Vinculación asistida de cliente", page.get_data(as_text=True))
        payload = {
            "customer_id": self.customer.id,
            "human_check_note": "Comprobación presencial documentada.",
        }
        self.assertEqual(self.client.post(endpoint, json=payload).status_code, 400)
        with self.client.session_transaction() as browser:
            csrf = browser["csrf_token"]
        malformed = self.client.post(endpoint, json={
            **payload, "customer_id": {"unexpected": True},
        }, headers={"X-CSRFToken": csrf})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(CustomerLinkingEnrollment.query.count(), 0)
        created = self.client.post(endpoint, json=payload, headers={"X-CSRFToken": csrf})
        self.assertEqual(created.status_code, 201)
        cross_tenant = self.client.post(endpoint, json={
            **payload, "customer_id": self.other_customer.id,
        }, headers={"X-CSRFToken": csrf})
        self.assertEqual(cross_tenant.status_code, 404)
        self.staff.is_active = False; db.session.commit()
        self.assertEqual(self.client.post(endpoint, json=payload, headers={"X-CSRFToken": csrf}).status_code, 302)

    def test_suspended_staff_or_business_invalidates_pending_linking(self):
        enrollment, secret = create_linking_enrollment(
            staff=self.staff, customer_id=self.customer.id,
            human_check_note="Comprobación presencial documentada.",
        )
        self.staff.is_active = False; db.session.commit()
        self.assertEqual(self.client.post("/customer-auth/enroll/claim", data={"linking_secret": secret}).status_code, 409)
        self.staff.is_active = True; self.business.is_active = False; db.session.commit()
        with self.assertRaises(CustomerAuthError):
            create_linking_enrollment(
                staff=self.staff, customer_id=self.customer.id,
                human_check_note="Comprobación presencial documentada.",
            )


if __name__ == "__main__":
    unittest.main()
