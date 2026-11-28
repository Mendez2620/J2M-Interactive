"""Exercise platform admin authentication with the restricted web role."""
import hashlib
import re

from sqlalchemy import text

from app.extensions import db
from run import app


def csrf(response):
    match = re.search(rb'name="csrf_token"\s+value="([^"]+)"', response.data)
    assert match, response.status_code
    return match[1].decode()


app.config.update(TESTING=True, CSRF_ENABLED=True)
client = app.test_client()
assert client.get("/admin/dashboard", headers={"Accept": "application/json"}).status_code == 401
credentials = {"email": "platform@example.test", "password": "SyntheticPlatform123",
               "csrf_token": csrf(client.get("/admin/login"))}
assert client.post("/admin/login", data={**credentials, "password": "invalid"}).status_code == 401
assert client.post("/admin/login", data=credentials).status_code == 302
response = client.get("/admin/dashboard", headers={"Accept": "application/json"})
assert response.status_code == 200, response.status_code
assert response.get_json()["metrics"]["total_businesses"] == 2
with client.session_transaction() as session:
    admin_id = session["platform_admin_id"]
    token_digest = hashlib.sha256(session["platform_admin_session"].encode()).hexdigest()
    logout_csrf = session["csrf_token"]
with app.app_context():
    row = db.session.execute(text("""
        SELECT admin_id, revoked_at FROM platform_admin_sessions
        WHERE token_digest=:digest
    """), {"digest": token_digest}).one()
    assert tuple(row) == (admin_id, None)
assert client.post("/admin/logout", data={"csrf_token": logout_csrf}).status_code == 302
assert client.get("/admin/dashboard", headers={"Accept": "application/json"}).status_code == 401
with app.app_context():
    assert db.session.scalar(text("""
        SELECT revoked_at IS NOT NULL FROM platform_admin_sessions
        WHERE token_digest=:digest
    """), {"digest": token_digest}) is True

tenant = app.test_client()
assert tenant.post("/manager/login", data={
    "email": "manager1@example.test", "password": "SyntheticPass123",
    "csrf_token": csrf(tenant.get("/manager/login")),
}).status_code == 302
assert tenant.get("/admin/dashboard", headers={"Accept": "application/json"}).status_code == 401
with tenant.session_transaction() as session:
    tenant_csrf = session["csrf_token"]
assert tenant.post("/manager/logout", data={"csrf_token": tenant_csrf}).status_code == 302
print("Restricted platform admin login, dashboard, tenant denial and persisted logout verified.")
