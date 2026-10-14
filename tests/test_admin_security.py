import base64
import io
import unittest

from app import create_app
from app.extensions import db
from app.models import Business, PlatformAdmin, Staff


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class AdminSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.business = Business(name="Tenant Uno", slug="tenant-uno")
        self.other_business = Business(name="Tenant Dos", slug="tenant-dos")
        db.session.add_all([self.business, self.other_business])
        admin = PlatformAdmin(name="Global Admin", email="global@j2m.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self):
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin_id

    def test_anonymous_admin_access_is_rejected(self):
        response = self.client.get("/admin/dashboard", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 401)

    def test_authenticated_admin_pages_render(self):
        self.login()
        dashboard = self.client.get("/admin")
        detail = self.client.get(f"/admin/business/{self.business.id}")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Ajustes", dashboard.data)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Restablecer", detail.data)

    def test_login_rejects_wrong_password_and_inactive_account(self):
        bad = self.client.post("/admin/login", data={"email": "global@j2m.test", "password": "incorrecta"})
        self.assertEqual(bad.status_code, 401)
        admin = db.session.get(PlatformAdmin, self.admin_id)
        admin.is_active = False
        db.session.commit()
        inactive = self.client.post("/admin/login", data={"email": admin.email, "password": "AdminSeguro123"})
        self.assertEqual(inactive.status_code, 401)

    def test_tenant_staff_cannot_become_platform_admin(self):
        staff = Staff(business_id=self.business.id, username="tenant-admin", role="admin", is_active=True)
        staff.set_password("TenantSeguro123")
        db.session.add(staff)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["staff_id"] = staff.id
        response = self.client.get("/admin/dashboard", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 401)

    def test_admin_creation_edit_duplicate_and_password_validation(self):
        self.login()
        weak = self.client.post("/admin/users/create", json={"name": "Dos", "email": "dos@j2m.test", "password": "debil"})
        self.assertEqual(weak.status_code, 400)
        created = self.client.post("/admin/users/create", json={"name": "Dos", "email": "dos@j2m.test", "phone": "555", "password": "SeguraNueva123", "password_confirmation": "SeguraNueva123"})
        self.assertEqual(created.status_code, 201)
        admin_id = created.get_json()["admin"]["id"]
        duplicate = self.client.post("/admin/users/create", json={"name": "Duplicado", "email": "dos@j2m.test", "password": "SeguraNueva123"})
        self.assertEqual(duplicate.status_code, 409)
        old_hash = db.session.get(PlatformAdmin, admin_id).password_hash
        edited = self.client.post(f"/admin/users/edit/{admin_id}", json={"name": "Dos Editado", "email": "dos@j2m.test", "phone": "777", "password": ""})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(db.session.get(PlatformAdmin, admin_id).password_hash, old_hash)

    def test_staff_password_reset_preserves_identity_and_tenant(self):
        self.login()
        staff = Staff(business_id=self.other_business.id, username="cajero", role="cashier", is_active=True)
        staff.set_password("AnteriorSeguro123")
        db.session.add(staff)
        db.session.commit()
        staff_id, business_id = staff.id, staff.business_id
        response = self.client.post(f"/admin/staff/{staff_id}/reset-password", json={"password": "NuevaSegura456", "password_confirmation": "NuevaSegura456"})
        self.assertEqual(response.status_code, 200)
        same_staff = db.session.get(Staff, staff_id)
        self.assertEqual(same_staff.business_id, business_id)
        self.assertTrue(same_staff.check_password("NuevaSegura456"))

    def test_valid_image_upload_and_dangerous_file_rejection(self):
        self.login()
        valid = self.client.post("/admin/business/create", data={"name": "Con Logo", "slug": "con-logo", "loyalty_type": "stamps", "stamps_reward_limit": "5", "logo_file": (io.BytesIO(PNG_1PX), "logo.png")}, content_type="multipart/form-data")
        self.assertEqual(valid.status_code, 201)
        created = valid.get_json()["business"]
        self.assertTrue(created["logo_path"].startswith("uploads/"))
        updated = self.client.post(f"/admin/business/{created['id']}/update", data={"cover_file": (io.BytesIO(PNG_1PX), "cover.png")}, content_type="multipart/form-data", headers={"Accept": "application/json"})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["business"]["cover_path"].startswith("uploads/"))
        dangerous = self.client.post("/admin/business/create", data={"name": "Malo", "slug": "malo", "logo_file": (io.BytesIO(b"MZ executable"), "payload.png")}, content_type="multipart/form-data")
        self.assertEqual(dangerous.status_code, 400)

    def test_oversized_image_is_rejected(self):
        self.login()
        oversized = PNG_1PX + (b"0" * (5 * 1024 * 1024))
        response = self.client.post("/admin/business/create", data={"name": "Grande", "slug": "grande", "logo_file": (io.BytesIO(oversized), "large.png")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)

    def test_loyalty_validation_for_create_and_update(self):
        self.login()
        invalid_stamps = self.client.post("/admin/business/create", json={"name": "Invalid", "slug": "invalid", "loyalty_type": "stamps", "stamps_reward_limit": 0})
        self.assertEqual(invalid_stamps.status_code, 400)
        invalid_points = self.client.post(f"/admin/business/{self.business.id}/update", json={"loyalty_type": "points", "points_per_currency": 0})
        self.assertEqual(invalid_points.status_code, 400)
        self.assertEqual(db.session.get(Business, self.business.id).loyalty_type, "stamps")

    def test_csrf_is_enforced_when_enabled(self):
        self.login()
        self.app.config["CSRF_ENABLED"] = True
        rejected = self.client.post("/admin/users/create", json={"name": "Sin CSRF", "email": "csrf@j2m.test", "password": "SeguraNueva123"})
        self.assertEqual(rejected.status_code, 400)
        with self.client.session_transaction() as session:
            session["csrf_token"] = "known-token"
        accepted = self.client.post("/admin/users/create", json={"name": "Con CSRF", "email": "csrf@j2m.test", "password": "SeguraNueva123"}, headers={"X-CSRFToken": "known-token"})
        self.assertEqual(accepted.status_code, 201)


if __name__ == "__main__":
    unittest.main()
