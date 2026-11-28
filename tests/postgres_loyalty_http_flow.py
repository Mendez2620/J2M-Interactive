"""Run loyalty HTTP writes with the restricted PostgreSQL web identity."""

from concurrent.futures import ThreadPoolExecutor
import re
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import text

from app.extensions import db
from app.models import Customer, LoyaltyLedgerEntry, Transaction
from app.services.loyalty_transaction_service import reconcile_customer_balance
from run import app

app.config.update(TESTING=True, CSRF_ENABLED=True)
assert app.config["APPLE_WALLET_MODE"] == app.config["GOOGLE_WALLET_MODE"] == "disabled"


def scanner(tenant, ip="127.0.0.1"):
    client = app.test_client()
    login = client.get("/staff/login?legacy=1")
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', login.data)[1].decode()
    response = client.post("/staff/login", data={
        "csrf_token": csrf, "identity": f"cashier{tenant}", "password": "SyntheticPass123",
        "business_slug": f"synthetic-{tenant}",
    }, environ_overrides={"REMOTE_ADDR": ip})
    assert response.status_code == 302, response.status_code
    with client.session_transaction() as session:
        return client, {"X-CSRFToken": session["csrf_token"]}


def process(client, headers, token, action, key, **values):
    return client.post("/api/web-scanner/process", headers={**headers, "Idempotency-Key": key},
                       json={"token": token, "action": action, **values})


with app.app_context():
    assert db.session.scalar(text("SELECT current_user")) == "j2m_schema_web"
    tokens = {tenant: db.session.get(Customer, tenant).qr_code_token for tenant in (1, 2)}

points, points_headers = scanner(1)
stamps, stamps_headers = scanner(2)
for client, headers, other, action in ((points, points_headers, 2, "earn_points"),
                                       (stamps, stamps_headers, 1, "earn_stamp")):
    lookup = client.post("/api/web-scanner/lookup", headers=headers, json={"token": tokens[other]})
    assert lookup.status_code == 404, lookup.status_code
    denied = process(client, headers, tokens[other], action, "cross-tenant", purchase_amount=10)
    assert denied.status_code == 404, denied.status_code

gate = Barrier(2)


def concurrent_earn(client, headers):
    gate.wait(timeout=10)
    response = process(client, headers, tokens[2], "earn_stamp", "same-stamp")
    assert response.status_code == 200, response.status_code
    return bool(response.get_json().get("idempotent_replay"))


parallel_sessions = [(stamps, stamps_headers), scanner(2, "127.0.0.2")]
with ThreadPoolExecutor(max_workers=2) as executor:
    results = [executor.submit(concurrent_earn, *session) for session in parallel_sessions]
    assert sum(future.result(timeout=20) for future in results) == 1
assert process(stamps, stamps_headers, tokens[2], "redeem_stamp", "redeem-stamp").status_code == 200
assert process(points, points_headers, tokens[1], "earn_points", "purchase",
               purchase_amount=20).status_code == 200
assert process(points, points_headers, tokens[1], "redeem_points", "redeem-points",
               amount=5).status_code == 200

with patch("app.services.loyalty_transaction_service.LoyaltyLedgerEntry", side_effect=RuntimeError("synthetic")):
    try:
        process(points, points_headers, tokens[1], "earn_points", "rolled-back", purchase_amount=7)
    except RuntimeError as exc:
        assert str(exc) == "synthetic"
    else:
        raise AssertionError("Expected injected ledger failure")

with app.app_context():
    assert Transaction.query.count() == 6
    assert LoyaltyLedgerEntry.query.count() == 8
    assert Transaction.query.filter(Transaction.request_id.in_(["cross-tenant", "rolled-back"])).count() == 0
    assert Transaction.query.filter_by(request_id="same-stamp").count() == 1
    assert db.session.get(Customer, 1).current_points == 16
    assert db.session.get(Customer, 2).current_stamps == 0
    for tenant in (1, 2):
        assert reconcile_customer_balance(db.session.get(Customer, tenant))["consistent"]
print("Restricted cross-tenant scanner denial, purchase, earn/redeem, idempotency and rollback verified.")
