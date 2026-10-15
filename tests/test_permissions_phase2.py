import unittest

from app import create_app
from app.extensions import db
from app.models import AccessAudit, Business, Customer, PlatformAdmin, Staff


class PermissionsPhase2TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.business = Business(name="Uno", slug="uno", is_active=True, web_access_enabled=True, mobile_access_enabled=True)
        self.other = Business(name="Dos", slug="dos", is_active=True, web_access_enabled=True, mobile_access_enabled=True)
        db.session.add_all([self.business, self.other])
        db.session.flush()
        self.admin = PlatformAdmin(name="Admin", email="admin@example.test", is_active=True)
        self.admin.set_password("AdminPassword123")
        self.manager = self.make_staff(self.business, "manager", "manager", "view_stats")
        self.cashier = self.make_staff(self.business, "cashier", "cashier", "scanner_operate")
        self.other_cashier = self.make_staff(self.other, "other", "cashier", "scanner_operate")
        db.session.add(self.admin)
        db.session.add(Customer(business_id=self.business.id, full_name="Cliente", email="private@example.test"))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def make_staff(self, business, username, role, permissions):
        staff = Staff(business_id=business.id, username=username, role=role, is_active=True,
                      web_access_enabled=True, mobile_access_enabled=True, permissions=permissions)
        staff.set_password("StaffPassword123")
        db.session.add(staff)
        return staff

    def admin_login(self, csrf=False):
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin.id
            if csrf:
                session["csrf_token"] = "phase2-csrf"

    def token(self, staff, channel="web"):
        response = self.client.post("/api/scanner/login", json={
            "username": staff.username, "password": "StaffPassword123",
            "business_slug": staff.business.slug, "channel": channel,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return {"Authorization": f"Bearer {response.get_json()['access_token']}"}

    def permissions_url(self, staff=None, business=None):
        staff = staff or self.manager
        business = business or self.business
        return f"/admin/business/{business.id}/staff/{staff.id}/permissions"

    def test_admin_saves_profiles_channels_and_recovers_configuration(self):
        self.admin_login()
        basic = self.client.put(self.permissions_url(), json={"profile": "basic", "permissions": [], "web_access_enabled": True, "mobile_access_enabled": False})
        self.assertEqual(basic.status_code, 200)
        self.assertEqual(basic.get_json()["staff"]["permissions"], ["view_stats"])
        operational = self.client.put(self.permissions_url(), json={"profile": "operational", "permissions": [], "web_access_enabled": False, "mobile_access_enabled": True})
        self.assertEqual(operational.status_code, 200)
        recovered = self.client.get(self.permissions_url()).get_json()
        self.assertEqual(recovered["profile"], "operational")
        self.assertFalse(recovered["staff"]["web_access_enabled"])
        self.assertTrue(recovered["staff"]["mobile_access_enabled"])
        self.assertNotIn("reset_cashier_password", recovered["staff"]["permissions"])
        self.assertNotIn("scanner_operate", recovered["staff"]["permissions"])

    def test_custom_permissions_validation_role_tenant_and_csrf(self):
        self.admin_login()
        valid = self.client.put(self.permissions_url(), json={"profile": "custom", "permissions": ["view_stats", "reset_cashier_password", "scanner_operate"], "web_access_enabled": True, "mobile_access_enabled": True})
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.get_json()["staff"]["permissions"], ["reset_cashier_password", "scanner_operate", "view_stats"])
        self.assertEqual(self.client.get(self.permissions_url()).get_json()["profile"], "custom")
        for permissions in (["unknown"], ["view_stats", "view_stats"]):
            response = self.client.put(self.permissions_url(), json={"profile": "custom", "permissions": permissions, "web_access_enabled": True, "mobile_access_enabled": True})
            self.assertEqual(response.status_code, 400)
        incompatible = self.client.put(self.permissions_url(self.cashier), json={"profile": "custom", "permissions": ["view_stats"], "web_access_enabled": True, "mobile_access_enabled": True})
        self.assertEqual(incompatible.status_code, 400)
        cross_tenant = self.client.put(self.permissions_url(self.other_cashier), json={"profile": "custom", "permissions": ["scanner_operate"], "web_access_enabled": False, "mobile_access_enabled": True})
        self.assertEqual(cross_tenant.status_code, 404)
        self.app.config["CSRF_ENABLED"] = True
        missing = self.client.put(self.permissions_url(), json={"profile": "basic", "permissions": [], "web_access_enabled": True, "mobile_access_enabled": False})
        self.assertEqual(missing.status_code, 400)
        self.admin_login(csrf=True)
        accepted = self.client.put(self.permissions_url(), json={"profile": "basic", "permissions": [], "web_access_enabled": True, "mobile_access_enabled": False}, headers={"X-CSRFToken": "phase2-csrf"})
        self.assertEqual(accepted.status_code, 200)

    def test_admin_auth_session_revocation_and_audit(self):
        headers = self.token(self.manager)
        self.admin_login()
        updated = self.client.put(self.permissions_url(), json={"profile": "custom", "permissions": ["view_transactions"], "web_access_enabled": True, "mobile_access_enabled": False})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.get("/manager/dashboard", headers=headers).status_code, 401)
        audit = AccessAudit.query.filter_by(affected_staff_id=self.manager.id, action="permissions_updated").one()
        self.assertEqual(audit.platform_admin_id, self.admin.id)
        manager_id = self.manager.id
        self.assertEqual(self.client.post(f"/admin/staff/{manager_id}/delete", headers={"Accept": "application/json"}).status_code, 200)
        self.assertIsNotNone(AccessAudit.query.filter_by(affected_staff_id=manager_id).first())

    def test_anonymous_and_manager_cannot_modify_permissions(self):
        payload = {"profile": "basic", "permissions": [], "web_access_enabled": True, "mobile_access_enabled": False}
        self.assertEqual(self.client.put(self.permissions_url(), json=payload).status_code, 401)
        headers = self.token(self.manager)
        self.assertEqual(self.client.put(self.permissions_url(), json=payload, headers=headers).status_code, 401)

    def test_manager_cashier_crud_authorization_isolation_and_customer_privacy(self):
        self.manager.permissions = "view_customers,view_cashiers,create_cashiers,edit_cashiers,toggle_cashiers,reset_cashier_password"
        db.session.commit()
        headers = self.token(self.manager)
        customers = self.client.get(f"/manager/business/{self.business.id}/customers", headers=headers)
        self.assertEqual(customers.status_code, 200)
        self.assertNotIn("qr_code_token", customers.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/manager/business/{self.other.id}/customers", headers=headers).status_code, 403)
        created = self.client.post(f"/manager/business/{self.business.id}/staff", headers=headers, json={"username": "newcash", "password": "NewCashier123", "role": "cashier"})
        self.assertEqual(created.status_code, 201)
        staff_id = created.get_json()["cashier"]["id"]
        self.assertEqual(self.client.patch(f"/manager/business/{self.business.id}/staff/{staff_id}", headers=headers, json={"name": "Nuevo", "role": "manager"}).status_code, 400)
        self.assertEqual(self.client.post(f"/manager/business/{self.business.id}/staff/{staff_id}/toggle-status", headers=headers).status_code, 200)
        reset = self.client.post(f"/manager/business/{self.business.id}/staff/{staff_id}/reset-password", headers=headers, json={"password": "ChangedPass123", "password_confirmation": "ChangedPass123"})
        self.assertEqual(reset.status_code, 200)
        self.assertTrue(db.session.get(Staff, staff_id).check_password("ChangedPass123"))
        self.assertIsNotNone(AccessAudit.query.filter_by(affected_staff_id=staff_id, action="password_reset").first())
        self.assertEqual(self.client.post(f"/manager/business/{self.other.id}/staff/{self.other_cashier.id}/toggle-status", headers=headers).status_code, 403)

    def test_permissions_are_enforced_and_legacy_manage_is_limited(self):
        self.manager.permissions = "manage_cashiers"
        db.session.commit()
        headers = self.token(self.manager)
        self.assertEqual(self.client.get(f"/manager/business/{self.business.id}/staff", headers=headers).status_code, 200)
        self.assertEqual(self.client.patch(f"/manager/business/{self.business.id}/staff/{self.cashier.id}", headers=headers, json={"name": "No autorizado"}).status_code, 403)
        self.assertEqual(self.client.post(f"/manager/business/{self.business.id}/staff/{self.cashier.id}/toggle-status", headers=headers).status_code, 403)
        self.assertEqual(self.client.post(f"/manager/business/{self.business.id}/staff/{self.cashier.id}/reset-password", headers=headers, json={"password": "ChangedPass123", "password_confirmation": "ChangedPass123"}).status_code, 403)
        cashier_headers = self.token(self.cashier, "web")
        self.assertEqual(self.client.post(f"/manager/business/{self.business.id}/staff", headers=cashier_headers, json={"username": "forbidden", "password": "Forbidden123"}).status_code, 403)

    def test_sensitive_admin_actions_are_audited_without_secrets(self):
        self.admin_login()
        created = self.client.post(f"/admin/business/{self.business.id}/staff", json={
            "username": "audited", "name": "Audited", "role": "cashier", "password": "SecretValue123",
        })
        self.assertEqual(created.status_code, 201)
        staff_id = created.get_json()["staff"]["id"]
        self.assertEqual(self.client.post(f"/admin/business/{self.business.id}/access/web", headers={"Accept": "application/json"}).status_code, 200)
        self.assertEqual(self.client.post(f"/admin/staff/{staff_id}/access/mobile", headers={"Accept": "application/json"}).status_code, 200)
        self.assertEqual(self.client.post(f"/admin/staff/{staff_id}/reset-password", json={"password": "AnotherSecret123", "password_confirmation": "AnotherSecret123"}).status_code, 200)
        self.assertEqual(self.client.post(f"/admin/staff/{staff_id}/delete", headers={"Accept": "application/json"}).status_code, 200)
        actions = {audit.action for audit in AccessAudit.query.filter_by(business_id=self.business.id).all()}
        self.assertTrue({"staff_created", "business_channel_updated", "staff_channel_updated", "password_reset", "staff_deleted"} <= actions)
        business_audit = AccessAudit.query.filter_by(action="business_channel_updated").one()
        self.assertIsNone(business_audit.affected_staff_id)
        serialized = " ".join((audit.previous_state or "") + (audit.new_state or "") for audit in AccessAudit.query.all())
        self.assertNotIn("SecretValue123", serialized)
        self.assertNotIn("AnotherSecret123", serialized)
        self.assertNotIn("password_hash", serialized)

    def test_admin_modal_is_rendered_without_dynamic_event_attributes(self):
        self.admin_login()
        html = self.client.get(f"/admin/business/{self.business.id}?tab=staff").get_data(as_text=True)
        self.assertIn("Gestionar permisos", html)
        self.assertIn("permissionsModal", html)
        self.assertIn("data-manage-permissions", html)
        self.assertNotIn("onclick=\"openPermissions", html)


if __name__ == "__main__":
    unittest.main()
