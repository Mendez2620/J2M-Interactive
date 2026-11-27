"""Customer account flows over HTTP using the restricted PostgreSQL web role."""

from base64 import urlsafe_b64encode
import re
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text

from app.extensions import db
from app.models import Customer, CustomerAuthAudit, CustomerCredential, CustomerSession
from run import app

app.config.update(TESTING=True, CSRF_ENABLED=True, PUBLIC_BASE_URL="https://j2m.example",
                  WEBAUTHN_ORIGIN="https://j2m.example", WEBAUTHN_RP_ID="j2m.example")


def post(client, path, *, form=False, **payload):
    with client.session_transaction() as session:
        token = session.get("csrf_token")
    if not token:
        assert client.get("/customer-auth/enroll").status_code == 200
        with client.session_transaction() as session:
            token = session["csrf_token"]
    encoding = {"data" if form else "json": payload}
    return client.post(path, headers={"X-CSRFToken": token}, **encoding)


def cashier(tenant):
    client = app.test_client()
    assert client.get("/staff/login?legacy=1").status_code == 200
    result = post(client, "/staff/login", form=True, identity=f"cashier{tenant}",
                  password="SyntheticPass123", business_slug=f"synthetic-{tenant}")
    assert result.status_code == 302, result.status_code
    return client


with app.app_context():
    assert db.session.scalar(text("SELECT current_user")) == "j2m_schema_web"
registered = app.test_client()
registration = {"full_name": "Synthetic Customer", "phone": "5512345678",
                "terms_accepted": True, "privacy_accepted": True}
assert registered.get("/join/synthetic-1").status_code == 200
assert post(registered, "/join/synthetic-1", **registration).status_code == 201
assert post(registered, "/join/synthetic-1", **registration).status_code == 409
assert post(registered, "/recover/synthetic-1/request", phone="5512345678").status_code == 410
assert post(registered, "/recover/synthetic-1/verify", code="000000").status_code == 410
with app.app_context():
    customer = Customer.query.filter_by(full_name=registration["full_name"], business_id=1).one()
    customer_id = customer.id
    assert customer.current_points == customer.current_stamps == 0

staff, other_staff = cashier(1), cashier(2)
link_path = "/customer-auth/staff/linking"
assert post(other_staff, link_path, customer_id=customer_id,
            human_check_note="Synthetic check").status_code == 404


def enroll(browser, credential_id, *, recovery=False):
    result = post(staff, link_path, customer_id=customer_id, human_check_note="Synthetic check",
                  recovery_mode=recovery)
    assert result.status_code == 201, result.status_code
    enrollment = result.get_json()
    assert browser.get("/customer-auth/enroll").status_code == 200
    claimed = post(browser, "/customer-auth/enroll/claim", form=True,
                   linking_secret=enrollment["linking_secret"])
    assert claimed.status_code == 200, claimed.status_code
    assert post(browser, "/customer-auth/enroll/claim", form=True,
                linking_secret=enrollment["linking_secret"]).status_code == 404
    pairing = re.search(rb'tracking-widest">(\d{6})<', claimed.data)[1].decode()
    approval_path = f"{link_path}/{enrollment['enrollment_id']}/approve"
    assert post(other_staff, approval_path, pairing_code=pairing).status_code == 404
    assert post(staff, approval_path, pairing_code=pairing).status_code == 200
    assert post(browser, "/customer-auth/enroll/options").status_code == 200
    verified = SimpleNamespace(
        credential_id=credential_id, credential_public_key=b"synthetic-public-key", sign_count=0,
        credential_device_type=SimpleNamespace(value="single_device"), credential_backed_up=False,
    )
    with patch("app.services.customer_auth_service.verify_registration_response", return_value=verified) as check:
        assert post(browser, "/customer-auth/enroll/complete", id="synthetic", response={}).status_code == 200
        assert check.call_args.kwargs["require_user_verification"]
    assert post(browser, "/customer-auth/enroll/complete", id="synthetic", response={}).status_code in (404, 409)


enroll(app.test_client(), b"synthetic-first")


def authenticate(browser, credential_id):
    login = "/customer-auth/login/synthetic-1"
    assert browser.get(login).status_code == 200
    assert post(browser, login + "/options").status_code == 200
    payload = {"id": urlsafe_b64encode(credential_id).decode().rstrip("=")}
    assert post(browser, "/customer-auth/login/synthetic-2/complete", **payload).status_code == 409
    with patch("app.services.customer_auth_service.verify_authentication_response",
               return_value=SimpleNamespace(new_sign_count=1)) as check:
        assert post(browser, login + "/complete", **payload).status_code == 200
        assert check.call_args.kwargs["require_user_verification"]
    assert post(browser, login + "/complete", **payload).status_code == 409


old_browser = app.test_client()
authenticate(old_browser, b"synthetic-first")
security = "/customer-auth/portal/synthetic-1/security"
assert old_browser.get(security).status_code == 200
assert old_browser.get("/customer-auth/portal/synthetic-2/security").status_code == 302
enroll(app.test_client(), b"synthetic-replacement", recovery=True)
assert old_browser.get(security).status_code == 302
new_browser = app.test_client()
authenticate(new_browser, b"synthetic-replacement")
revoke = f"/customer-auth/staff/customers/{customer_id}/revoke"
assert post(other_staff, revoke, reason="Synthetic revocation").status_code == 404
assert post(staff, revoke, reason="Synthetic revocation").status_code == 200
assert new_browser.get(security).status_code == 302
with app.app_context():
    assert CustomerCredential.query.filter_by(customer_id=customer_id, is_active=False).count() == 2
    assert CustomerSession.query.filter_by(customer_id=customer_id, revoked_at=None).count() == 0
    actions = {log.action for log in CustomerAuthAudit.query.filter_by(customer_id=customer_id, business_id=1)}
    assert {"linking_authorized", "linking_claimed", "linking_pair_confirmed",
            "credential_registered", "customer_authenticated", "customer_access_revoked"} <= actions
print("Restricted customer registration, assisted recovery, WebAuthn replay denial and persisted audit verified.")
