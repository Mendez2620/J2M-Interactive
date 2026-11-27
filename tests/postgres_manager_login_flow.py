"""Exercise manager browser authentication over Flask HTTP under the web role."""

import re

from sqlalchemy import text

from app.extensions import db
from run import app


def csrf(response):
    return re.search(rb'name="csrf_token" value="([^"]+)"', response.data)[1].decode()


app.config.update(TESTING=True, CSRF_ENABLED=True)
clients = [app.test_client(), app.test_client()]
for tenant, client in enumerate(clients, 1):
    credentials = {"email": f"manager{tenant}@example.test", "password": "SyntheticPass123"}
    assert client.get("/manager/portal/account").status_code == 302
    credentials["csrf_token"] = csrf(client.get("/manager/login"))
    wrong = client.post("/manager/login", data={**credentials, "business_slug": f"synthetic-{3-tenant}"})
    assert wrong.status_code == 401, wrong.status_code
    assert client.get("/manager/portal/account").status_code == 302
    logged_in = client.post("/manager/login", data=credentials)
    assert logged_in.status_code == 302, logged_in.status_code
    account = client.get("/manager/portal/account")
    assert account.status_code == 200
    assert f"Synthetic {tenant}".encode() in account.data
    assert f"Synthetic {3-tenant}".encode() not in account.data
    with client.session_transaction() as session:
        session_id = session["manager_session_id"]
        logout_csrf = session["csrf_token"]
    with app.app_context():
        row = db.session.execute(text("""
            SELECT staff.business_id, staff_sessions.revoked_at
            FROM staff_sessions JOIN staff ON staff.id = staff_sessions.staff_id
            WHERE staff_sessions.id=:session_id
        """), {"session_id": session_id}).one()
        assert tuple(row) == (tenant, None)
    assert client.post("/manager/logout", data={"csrf_token": logout_csrf}).status_code == 302
    assert client.get("/manager/portal/account").status_code == 302
    with app.app_context():
        assert db.session.scalar(text(
            "SELECT revoked_at IS NOT NULL FROM staff_sessions WHERE id=:id"
        ), {"id": session_id}) is True
