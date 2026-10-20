import base64
import io
import unittest

from app import create_app
from app.extensions import db
from app.models import Business, BusinessCategory, PlatformAdmin


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class AdminBusinessPhase73TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        admin = PlatformAdmin(name="Admin", email="admin73@j2m.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin_id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def create(self, slug, category_marker="missing", **extra):
        payload = {"name": f"Negocio {slug}", "slug": slug, "loyalty_type": "stamps", "stamps_reward_limit": "8"}
        if category_marker != "missing":
            payload["category_id"] = category_marker
        payload.update(extra)
        return self.client.post("/admin/business/create", json=payload)

    def test_absent_empty_and_whitespace_category_become_sql_null(self):
        for index, value in enumerate(("missing", "", "   ")):
            with self.subTest(value=value):
                response = self.create(f"sin-categoria-{index}", value)
                self.assertEqual(response.status_code, 201)
                business = db.session.get(Business, response.get_json()["business"]["id"])
                self.assertIsNone(business.category_id)
                self.assertEqual(business.stamps_reward_limit, 8)

    def test_category_must_be_existing_positive_integer(self):
        category = BusinessCategory(name="Restaurante", slug="restaurante")
        db.session.add(category)
        db.session.commit()
        valid = self.create("categoria-valida", str(category.id))
        self.assertEqual(valid.status_code, 201)
        self.assertEqual(valid.get_json()["business"]["category_id"], category.id)
        for index, value in enumerate(("abc", "1.5", "-1", "99999", True, 1.0, [], {})):
            with self.subTest(value=value):
                response = self.create(f"categoria-invalida-{index}", value)
                self.assertEqual(response.status_code, 400)
                self.assertIsNone(Business.query.filter_by(slug=f"categoria-invalida-{index}").first())

    def test_multipart_empty_category_is_null_and_duplicate_slug_is_conflict(self):
        response = self.client.post("/admin/business/create", data={"name":"Multipart", "slug":"multipart", "category_id":" ", "stamps_reward_limit":"8"}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(db.session.get(Business, response.get_json()["business"]["id"]).category_id)
        duplicate = self.create("multipart")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(Business.query.filter_by(slug="multipart").count(), 1)
        self.assertIsNotNone(db.session.get(PlatformAdmin, self.admin_id))

    def test_category_creation_validation_duplicates_and_authentication(self):
        created = self.client.post("/admin/categories", json={"name":"Cafetería", "slug":"cafeteria"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["category"]["name"], "Cafetería")
        compatible = self.client.post("/admin/categories", json={"name":"Servicios Profesionales"})
        self.assertEqual(compatible.status_code, 201)
        self.assertEqual(compatible.get_json()["category"]["slug"], "servicios-profesionales")
        self.assertEqual(self.client.post("/admin/categories", json={"name":"Otra", "slug":"cafeteria"}).status_code, 409)
        self.assertEqual(self.client.post("/admin/categories", json={"name":"Inválida", "slug":"../mala"}).status_code, 400)
        anonymous = self.app.test_client().post("/admin/categories", json={"name":"Bar", "slug":"bar"})
        self.assertEqual(anonymous.status_code, 401)

    def test_csrf_protects_category_and_business_creation(self):
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/admin/categories", json={"name":"Bar", "slug":"bar"}).status_code, 400)
        self.assertEqual(self.create("sin-csrf").status_code, 400)
        with self.client.session_transaction() as session:
            session["csrf_token"] = "phase73-token"
        headers = {"X-CSRFToken":"phase73-token"}
        self.assertEqual(self.client.post("/admin/categories", json={"name":"Bar", "slug":"bar"}, headers=headers).status_code, 201)
        self.assertEqual(self.client.post("/admin/business/create", json={"name":"Con CSRF", "slug":"con-csrf"}, headers=headers).status_code, 201)

    def test_colors_images_and_disabled_upload_ui(self):
        invalid_color = self.create("color-invalido", primary_color="#12345G")
        self.assertEqual(invalid_color.status_code, 400)
        valid_color = self.create("color-valido", primary_color="#aabbcc", secondary_color="#ffffff")
        self.assertEqual(valid_color.status_code, 201)
        self.assertEqual(valid_color.get_json()["business"]["primary_color"], "#AABBCC")
        bad_image = self.client.post("/admin/business/create", data={"name":"Imagen mala", "slug":"imagen-mala", "logo_file":(io.BytesIO(b"not-image"), "logo.png")}, content_type="multipart/form-data")
        self.assertEqual(bad_image.status_code, 400)
        good_image = self.client.post("/admin/business/create", data={"name":"Imagen buena", "slug":"imagen-buena", "logo_file":(io.BytesIO(PNG_1PX), "logo.png")}, content_type="multipart/form-data")
        self.assertEqual(good_image.status_code, 201)

        self.app.config["UPLOAD_STORAGE_BACKEND"] = "disabled"
        html = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("almacenamiento persistente", html)
        self.assertNotIn('name="logo_file"', html)
        blocked = self.client.post("/admin/business/create", data={"name":"Bloqueado", "slug":"bloqueado", "logo_file":(io.BytesIO(PNG_1PX), "logo.png")}, content_type="multipart/form-data")
        self.assertEqual(blocked.status_code, 503)

    def test_dashboard_exposes_safe_category_file_and_hex_controls(self):
        html = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("Sin categoría", html)
        self.assertIn("createCategoryButton", html)
        self.assertIn("Ningún archivo seleccionado", html)
        self.assertIn('data-color-picker', html)
        self.assertEqual(html.count('name="primary_color"'), 1)
        self.assertNotIn("innerHTML", html)

    def test_update_rejects_invalid_category_without_partial_changes(self):
        business = Business(name="Original", slug="original")
        db.session.add(business)
        db.session.commit()
        response = self.client.post(f"/admin/business/{business.id}/update", json={"name":"No persistir", "category_id":"inexistente"})
        self.assertEqual(response.status_code, 400)
        db.session.refresh(business)
        self.assertEqual(business.name, "Original")
        self.assertIsNone(business.category_id)


if __name__ == "__main__":
    unittest.main()
