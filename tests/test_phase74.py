import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from app import create_app
from app.extensions import db
from app.models import (
    Business, BusinessCategory, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry,
    PlatformAdmin, Staff, StaffSession, Transaction,
)
from app.staff_auth import revoke_staff_sessions


class Phase74TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.business = Business(name="Negocio Web", slug="negocio-web", loyalty_type="stamps", stamps_reward_limit=2, cooldown_minutes=0, is_active=True, web_access_enabled=True, mobile_access_enabled=True)
        self.other = Business(name="Otro Negocio", slug="otro-negocio", loyalty_type="points", points_per_currency=2, cooldown_minutes=0, is_active=True, web_access_enabled=True, mobile_access_enabled=True)
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.cashier = self.make_staff(self.business, "cajero", "cashier", "scanner_operate_web", web=True)
        self.manager = self.make_staff(self.business, "gerente", "manager", "view_stats,scanner_operate_web", web=True, email="gerente@j2m.test")
        self.mobile_only = self.make_staff(self.business, "movil", "cashier", "scanner_operate", web=True)
        self.other_cashier = self.make_staff(self.other, "otro", "cashier", "scanner_operate_web", web=True)
        self.customer = Customer(business_id=self.business.id, full_name="Cliente Sellos", current_stamps=0)
        self.other_customer = Customer(business_id=self.other.id, full_name="Cliente Puntos", current_points=20)
        admin = PlatformAdmin(name="Admin", email="admin74@j2m.test", is_active=True); admin.set_password("AdminSeguro123")
        db.session.add_all([self.customer, self.other_customer, admin]); db.session.commit()
        self.admin_id = admin.id

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.context.pop()

    def make_staff(self, business, username, role, permissions, web, email=None):
        staff = Staff(business_id=business.id, username=username, email=email, role=role, permissions=permissions, is_active=True, web_access_enabled=web, mobile_access_enabled=True)
        staff.set_password("PasswordSeguro123")
        db.session.add(staff)
        return staff

    def staff_login(self, username="cajero", slug="negocio-web", password="PasswordSeguro123"):
        return self.client.post("/staff/login", data={"identity":username, "business_slug":slug, "password":password})

    def manager_login(self):
        return self.client.post("/manager/login", data={"email":"gerente@j2m.test", "password":"PasswordSeguro123"})

    def admin_login(self):
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin_id

    def test_categories_are_independent_and_existing_records_are_unchanged(self):
        names = ["Restaurante", "Cafetería", "Bar", "Tienda", "Servicios", "Otros"]
        for index, name in enumerate(names, 1):
            db.session.add(BusinessCategory(id=index, name=name, slug=name.lower().replace("í", "i").replace("é", "e")))
        self.business.category_id = 1; db.session.commit(); original_ids = [item.id for item in BusinessCategory.query.order_by(BusinessCategory.id)]
        self.admin_login(); html = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("Gestionar categorías", html); self.assertIn('id="categoriesModal"', html)
        self.assertNotIn("Crear categoría sin perder este formulario", html)
        self.assertGreater(html.index('id="categoryForm"'), html.index('id="createBusinessForm"'))
        for name in names: self.assertIn(name, html)
        self.assertEqual([item.id for item in BusinessCategory.query.order_by(BusinessCategory.id)], original_ids)
        self.assertEqual(db.session.get(Business, self.business.id).category_id, 1)
        created = self.client.post("/admin/categories", json={"name":"Gimnasio", "slug":"gimnasio"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.client.post("/admin/categories", json={"name":"Gimnasio 2", "slug":"gimnasio"}).status_code, 409)
        self.assertIn("cloneNode", html); self.assertNotIn("innerHTML", html)

    def test_cashier_web_login_permission_channel_and_status_are_enforced(self):
        success = self.staff_login(); self.assertEqual(success.status_code, 302); self.assertIn("/staff/portal/scanner", success.location)
        self.assertEqual(self.client.get("/staff/portal/scanner").status_code, 200)
        self.client.post("/staff/logout")
        self.assertEqual(self.staff_login("movil").status_code, 401)
        self.cashier.web_access_enabled = False; db.session.commit(); self.assertEqual(self.staff_login().status_code, 401)
        self.cashier.web_access_enabled = True; self.business.web_access_enabled = False; db.session.commit(); self.assertEqual(self.staff_login().status_code, 401)
        self.business.web_access_enabled = True; self.cashier.is_active = False; db.session.commit(); self.assertEqual(self.staff_login().status_code, 401)
        self.cashier.is_active = True; self.business.is_active = False; db.session.commit(); self.assertEqual(self.staff_login().status_code, 401)

    def test_browser_session_expires_and_is_revoked_by_password_or_access_change(self):
        self.staff_login(); session_id = StaffSession.query.filter_by(staff_id=self.cashier.id, channel="browser").one().id
        auth_session = db.session.get(StaffSession, session_id); auth_session.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1); db.session.commit()
        self.assertEqual(self.client.get("/staff/portal/scanner").status_code, 302)
        self.staff_login(); revoke_staff_sessions(self.cashier); self.cashier.set_password("NuevaPasswordSegura123"); db.session.commit()
        self.assertEqual(self.client.get("/staff/portal/scanner").status_code, 302)

    def test_manager_scanner_requires_explicit_web_permission(self):
        self.assertEqual(self.manager_login().status_code, 302)
        page = self.client.get("/manager/portal/scanner"); self.assertEqual(page.status_code, 200); self.assertIn(b"Escanear QR", page.data)
        self.client.post("/manager/logout"); self.manager.permissions = "view_stats,scanner_operate"; db.session.commit(); self.manager_login()
        denied = self.client.get("/manager/portal/scanner"); self.assertEqual(denied.status_code, 302); self.assertIn("/manager/portal/account", denied.location)

    def test_web_scanner_is_tenant_scoped_idempotent_and_uses_ledger_outbox(self):
        self.app.config["GOOGLE_WALLET_MODE"] = "real"
        self.staff_login()
        lookup = self.client.post("/api/web-scanner/lookup", json={"token":self.customer.qr_code_token})
        self.assertEqual(lookup.status_code, 200); self.assertNotIn("qr_code_token", lookup.get_data(as_text=True))
        self.assertEqual(self.client.post("/api/web-scanner/lookup", json={"token":self.other_customer.qr_code_token}).status_code, 404)
        headers = {"Idempotency-Key":"phase74-stamp"}
        first = self.client.post("/api/web-scanner/process", json={"token":self.customer.qr_code_token, "action":"earn_stamp"}, headers=headers)
        replay = self.client.post("/api/web-scanner/process", json={"token":self.customer.qr_code_token, "action":"earn_stamp"}, headers=headers)
        self.assertEqual(first.status_code, 200); self.assertEqual(replay.status_code, 200); self.assertTrue(replay.get_json()["idempotent_replay"])
        self.assertEqual(db.session.get(Customer, self.customer.id).current_stamps, 1)
        self.assertEqual(Transaction.query.filter_by(business_id=self.business.id).count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter_by(business_id=self.business.id, transaction_id=first.get_json()["transaction"]["id"]).count(), 1)
        self.assertEqual(GoogleWalletSyncJob.query.filter_by(business_id=self.business.id).count(), 1)

    def test_web_scanner_points_earn_redeem_and_invalid_tokens(self):
        self.assertEqual(self.staff_login("otro", "otro-negocio").status_code, 302)
        self.assertEqual(self.client.post("/api/web-scanner/lookup", json={"token":"manipulado"}).status_code, 404)
        earn = self.client.post("/api/web-scanner/process", json={"token":self.other_customer.qr_code_token, "action":"earn_points", "purchase_amount":5}, headers={"Idempotency-Key":"phase74-points-earn"})
        redeem = self.client.post("/api/web-scanner/process", json={"token":self.other_customer.qr_code_token, "action":"redeem_points", "amount":4}, headers={"Idempotency-Key":"phase74-points-redeem"})
        self.assertEqual(earn.status_code, 200); self.assertEqual(redeem.status_code, 200)
        self.assertEqual(db.session.get(Customer, self.other_customer.id).current_points, 26)
        insufficient = self.client.post("/api/web-scanner/process", json={"token":self.other_customer.qr_code_token, "action":"redeem_points", "amount":1000}, headers={"Idempotency-Key":"phase74-insufficient"})
        self.assertEqual(insufficient.status_code, 400)

    def test_csrf_and_direct_access_are_protected(self):
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/staff/login", data={"identity":"cajero", "business_slug":"negocio-web", "password":"PasswordSeguro123"}).status_code, 400)
        with self.client.session_transaction() as session: session["csrf_token"] = "phase74-csrf"
        login = self.client.post("/staff/login", data={"identity":"cajero", "business_slug":"negocio-web", "password":"PasswordSeguro123", "csrf_token":"phase74-csrf"})
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.post("/api/web-scanner/lookup", json={"token":self.customer.qr_code_token}).status_code, 400)
        self.client.get("/staff/portal/scanner")
        with self.client.session_transaction() as session: browser_csrf = session["csrf_token"]
        accepted = self.client.post("/api/web-scanner/lookup", json={"token":self.customer.qr_code_token}, headers={"X-CSRFToken":browser_csrf})
        self.assertEqual(accepted.status_code, 200)
        anonymous = self.app.test_client(); self.assertEqual(anonymous.get("/staff/portal/scanner").status_code, 302); self.assertEqual(anonymous.post("/api/web-scanner/lookup", json={"token":"x"}).status_code, 400)

    def test_pwa_manifest_icons_service_worker_and_scanner_frontend_are_safe(self):
        manifest_response = self.client.get("/static/manifest.webmanifest")
        manifest = json.loads(manifest_response.get_data(as_text=True))
        manifest_response.close()
        self.assertEqual(manifest["name"], "J2M Interactive Staff"); self.assertEqual(manifest["display"], "standalone"); self.assertEqual(manifest["scope"], "/")
        root = Path(self.app.root_path) / "static"
        with Image.open(root / "pwa/j2m-staff-192.png") as icon192, Image.open(root / "pwa/j2m-staff-512.png") as icon512:
            self.assertEqual(icon192.size, (192, 192)); self.assertEqual(icon512.size, (512, 512))
        worker = self.client.get("/service-worker.js"); self.assertEqual(worker.status_code, 200); self.assertEqual(worker.headers["Service-Worker-Allowed"], "/")
        source = worker.get_data(as_text=True); worker.close(); self.assertNotIn("/api/", source); self.assertNotIn("localStorage", source); self.assertIn("request.method !== 'GET'", source); self.assertIn("No se guardó ninguna operación", source)
        scanner_js = (root / "js/web-scanner.js").read_text(encoding="utf-8")
        self.assertIn("data-start-camera", scanner_js); self.assertIn("Idempotency-Key", scanner_js); self.assertNotIn("console.log", scanner_js); self.assertNotIn("localStorage", scanner_js)
        login = self.client.get("/staff/login").get_data(as_text=True); self.assertIn("manifest.webmanifest", login); self.assertIn("Añadir a pantalla de inicio", login)


if __name__ == "__main__":
    unittest.main()
