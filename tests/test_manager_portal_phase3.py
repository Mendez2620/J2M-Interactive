import io
import unittest
from datetime import datetime, timedelta

import openpyxl

from app import create_app
from app.extensions import db
from app.models import AccessAudit, Business, Customer, PlatformAdmin, Staff, StaffSession, Transaction
from app.staff_auth import revoke_staff_sessions
from app.routes.manager import _portal_login_attempts


class ManagerPortalPhase3TestCase(unittest.TestCase):
    def setUp(self):
        _portal_login_attempts.clear()
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.business = Business(name="Portal Uno", slug="portal-uno", loyalty_type="points", is_active=True, web_access_enabled=True, mobile_access_enabled=True)
        self.other = Business(name="Portal Dos", slug="portal-dos", is_active=True, web_access_enabled=True, mobile_access_enabled=True)
        db.session.add_all([self.business, self.other]); db.session.flush()
        permissions = "view_stats,view_customers,view_transactions,export_reports,view_cashiers,create_cashiers,edit_cashiers,toggle_cashiers,reset_cashier_password"
        self.manager = self.staff(self.business, "manager", "manager", permissions)
        self.limited = self.staff(self.business, "limited", "manager", "view_customers")
        self.cashier = self.staff(self.business, "cashier", "cashier", "scanner_operate")
        self.other_cashier = self.staff(self.other, "othercash", "cashier", "scanner_operate")
        self.customer = Customer(business_id=self.business.id, full_name="Cliente Privado", email="private@example.test", current_points=12)
        self.other_customer = Customer(business_id=self.other.id, full_name="Cliente Ajeno", email="other@example.test")
        db.session.add_all([self.customer, self.other_customer]); db.session.flush()
        db.session.add(Transaction(business_id=self.business.id, customer_id=self.customer.id, staff_id=self.cashier.id, type="earn", points_amount=8, purchase_amount=40))
        admin = PlatformAdmin(name="Global", email="global@example.test", is_active=True); admin.set_password("AdminPassword123"); db.session.add(admin)
        db.session.commit(); self.admin_id = admin.id

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def staff(self, business, username, role, permissions):
        item = Staff(business_id=business.id, username=username, name=username.title(), role=role, is_active=True, web_access_enabled=True, mobile_access_enabled=True, permissions=permissions)
        item.set_password("ManagerPassword123"); db.session.add(item); return item

    def login(self, staff=None, password="ManagerPassword123", slug=None):
        staff = staff or self.manager
        return self.client.post("/manager/login", data={"identity": staff.username, "password": password, "business_slug": slug or staff.business.slug})

    def test_login_cookie_session_role_and_generic_failures(self):
        with self.client.session_transaction() as browser: browser["fixation_marker"] = "remove-me"
        response = self.login()
        self.assertEqual(response.status_code, 302)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Lax", cookie)
        with self.client.session_transaction() as browser:
            self.assertNotIn("access_token", browser); self.assertNotIn("refresh_token", browser)
            self.assertNotIn("fixation_marker", browser)
            session_id = browser["manager_session_id"]
        self.assertEqual(db.session.get(StaffSession, session_id).channel, "browser")
        self.client.get("/manager/logout")
        wrong = self.login(password="incorrect")
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn(self.manager.username.encode(), wrong.data)
        self.assertEqual(self.login(self.cashier).status_code, 401)
        self.manager.is_active = False; db.session.commit(); self.assertEqual(self.login().status_code, 401)
        self.manager.is_active = True; self.business.is_active = False; db.session.commit(); self.assertEqual(self.login().status_code, 401)
        self.business.is_active = True; self.manager.web_access_enabled = False; db.session.commit(); self.assertEqual(self.login().status_code, 401)
        self.manager.web_access_enabled = True; self.business.web_access_enabled = False; db.session.commit(); self.assertEqual(self.login().status_code, 401)
        login_html = self.client.get("/manager/login").get_data(as_text=True)
        self.assertNotIn("localStorage", login_html); self.assertNotIn("sessionStorage", login_html)

    def test_secure_cookie_csrf_logout_and_single_session_revocation(self):
        self.app.config["SESSION_COOKIE_SECURE"] = True
        self.assertIn("Secure", self.login().headers.get("Set-Cookie", ""))
        with self.client.session_transaction() as browser:
            current_id = browser["manager_session_id"]
            browser["csrf_token"] = "portal-csrf"
        second = StaffSession(id="other-browser", staff_id=self.manager.id, channel="browser", refresh_jti_hash="x" * 64, expires_at=datetime.now().replace(microsecond=0) + timedelta(hours=1))
        db.session.add(second); db.session.commit()
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/manager/logout").status_code, 400)
        logout = self.client.post("/manager/logout", headers={"X-CSRFToken": "portal-csrf"})
        self.assertEqual(logout.status_code, 302)
        self.assertIsNotNone(db.session.get(StaffSession, current_id).revoked_at)
        self.assertIsNone(db.session.get(StaffSession, "other-browser").revoked_at)
        self.assertEqual(self.client.post("/manager/login", data={"identity": "manager", "password": "ManagerPassword123", "business_slug": "portal-uno"}).status_code, 400)
        self.assertEqual(self.client.post("/manager/login", data={"identity": "manager", "password": "ManagerPassword123", "business_slug": "portal-uno"}, headers={"X-CSRFToken": "portal-csrf"}).status_code, 302)

    def test_revoked_expired_and_permission_changes_apply_immediately(self):
        self.login()
        self.assertEqual(self.client.get("/manager/portal").status_code, 200)
        with self.client.session_transaction() as browser: session_id = browser["manager_session_id"]
        db.session.get(StaffSession, session_id).expires_at = datetime.now().replace(microsecond=0) - timedelta(seconds=1); db.session.commit()
        self.assertEqual(self.client.get("/manager/portal").status_code, 302)
        self.login(); self.manager.permissions = "view_customers"; revoke_staff_sessions(self.manager); db.session.commit()
        self.assertEqual(self.client.get("/manager/portal").status_code, 302)
        self.assertIn("/manager/login", self.client.get("/manager/portal").headers["Location"])

    def test_modules_enforce_permissions_and_tenant_privacy(self):
        self.login(self.limited)
        self.assertEqual(self.client.get("/manager/portal/customers").status_code, 200)
        self.assertEqual(self.client.get("/manager/portal/transactions").status_code, 302)
        html = self.client.get("/manager/portal/customers?q=Cliente").get_data(as_text=True)
        self.assertIn("Cliente Privado", html); self.assertNotIn("Cliente Ajeno", html); self.assertNotIn("qr_code_token", html)
        self.assertNotIn("Movimientos", html)

    def test_dashboard_real_metrics_empty_state_and_filters(self):
        self.login()
        html = self.client.get("/manager/portal?period=7").get_data(as_text=True)
        self.assertIn("Cliente", html); self.assertIn("8", html); self.assertIn("40.00", html)
        self.assertIn("no representa los ingresos totales", html)
        self.login(self.limited)
        self.assertEqual(self.client.get("/manager/portal").status_code, 302)
        stamp_business = Business(name="Sellos", slug="sellos", loyalty_type="stamps", stamps_reward_limit=5, is_active=True, web_access_enabled=True)
        db.session.add(stamp_business); db.session.flush()
        stamp_manager = self.staff(stamp_business, "stamp-manager", "manager", "view_stats")
        stamp_customer = Customer(business_id=stamp_business.id, full_name="Sellos Cliente", current_stamps=6)
        db.session.add(stamp_customer); db.session.commit()
        self.login(stamp_manager)
        stamp_html = self.client.get("/manager/portal").get_data(as_text=True)
        self.assertIn("6 sellos", stamp_html); self.assertIn("Recompensas disponibles", stamp_html)
        self.assertIn("No hay movimientos recientes", stamp_html)

    def test_customers_transactions_pagination_and_exports(self):
        for index in range(30): db.session.add(Customer(business_id=self.business.id, full_name=f"Cliente {index:02d}"))
        db.session.commit(); self.login()
        page = self.client.get("/manager/portal/customers?page=2")
        self.assertEqual(page.status_code, 200); self.assertIn("Página 2", page.get_data(as_text=True))
        transactions = self.client.get("/manager/portal/transactions?type=earn")
        self.assertEqual(transactions.status_code, 200); self.assertIn(b"Cliente Privado", transactions.data)
        excel = self.client.get("/manager/portal/reports/excel")
        self.assertEqual(excel.status_code, 200); openpyxl.load_workbook(io.BytesIO(excel.data))
        pdf = self.client.get("/manager/portal/reports/pdf")
        self.assertEqual(pdf.status_code, 200); self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_cashier_forms_permissions_audit_csrf_and_tenant_isolation(self):
        self.login()
        with self.client.session_transaction() as browser: browser["csrf_token"] = "cashier-csrf"
        self.app.config["CSRF_ENABLED"] = True
        create = self.client.post("/manager/portal/cashiers/create", data={"username": "portal-cash", "password": "NewCashier123", "csrf_token": "cashier-csrf"})
        self.assertEqual(create.status_code, 302)
        created = Staff.query.filter_by(username="portal-cash").one()
        self.assertFalse(created.web_access_enabled); self.assertFalse(created.mobile_access_enabled)
        self.assertIsNotNone(AccessAudit.query.filter_by(action="staff_created", affected_staff_id=created.id, actor_staff_id=self.manager.id).first())
        tampered = self.client.post(f"/manager/portal/cashiers/{self.other_cashier.id}/toggle", data={"csrf_token": "cashier-csrf"})
        self.assertEqual(tampered.status_code, 404)
        self.assertEqual(self.client.post(f"/manager/portal/cashiers/{created.id}/edit", data={"role": "manager", "csrf_token": "cashier-csrf"}).status_code, 302)
        self.assertEqual(db.session.get(Staff, created.id).role, "cashier")
        self.manager.permissions = "view_cashiers"; db.session.commit()
        self.assertEqual(self.client.post("/manager/portal/cashiers/create", data={"username": "denied", "password": "DeniedPass123", "csrf_token": "cashier-csrf"}).status_code, 302)
        self.assertIsNone(Staff.query.filter_by(username="denied").first())

    def test_super_admin_session_does_not_authorize_portal_and_jwt_api_still_works(self):
        with self.client.session_transaction() as browser: browser["platform_admin_id"] = self.admin_id
        self.assertIn("/manager/login", self.client.get("/manager/portal").headers["Location"])
        jwt_login = self.client.post("/api/scanner/login", json={"username": self.manager.username, "password": "ManagerPassword123", "business_slug": self.business.slug, "channel": "web"})
        self.assertEqual(jwt_login.status_code, 200)
        headers = {"Authorization": f"Bearer {jwt_login.get_json()['access_token']}"}
        self.assertEqual(self.client.get("/manager/dashboard", headers=headers).status_code, 200)


if __name__ == "__main__":
    unittest.main()
