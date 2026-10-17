import unittest
from datetime import datetime, timedelta

from app import create_app
from app.extensions import db
from app.models import Business, Customer, PlatformAdmin, Staff, Transaction


class UXImprovementsPhase65TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.business = Business(name="Marca Uno", slug="marca-uno", web_access_enabled=True)
        self.other = Business(name="Marca Ajena", slug="marca-ajena", web_access_enabled=True)
        db.session.add_all([self.business, self.other]); db.session.flush()
        admin = PlatformAdmin(name="Admin", email="admin@example.test", is_active=True); admin.set_password("AdminPassword123!")
        db.session.add(admin); db.session.commit(); self.admin_id = admin.id

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def admin_login(self):
        with self.client.session_transaction() as browser: browser["platform_admin_id"] = self.admin_id

    def manager(self, business, username, email, password="ManagerPassword123!"):
        staff = Staff(business_id=business.id, username=username, email=email, role="manager", is_active=True,
                      web_access_enabled=True, permissions="view_stats,view_customers,view_transactions")
        staff.set_password(password); db.session.add(staff); db.session.commit(); return staff

    def test_public_join_normalizes_supported_phone_numbers(self):
        response = self.client.post("/join/marca-uno", json={"full_name": "Cliente MX", "email": "mx@example.test", "phone_country": "+52", "phone": "55 1234 5678"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Customer.query.filter_by(email="mx@example.test").one().phone, "+525512345678")
        response = self.client.post("/join/marca-uno", json={"full_name": "Cliente ES", "email": "es@example.test", "phone_country": "+34", "phone": "612 345 678"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Customer.query.filter_by(email="es@example.test").one().phone, "+34612345678")

    def test_invalid_phone_is_rejected_without_customer(self):
        response = self.client.post("/join/marca-uno", json={"full_name": "Inválido", "email": "bad@example.test", "phone_country": "+52", "phone": "abc123"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(Customer.query.filter_by(email="bad@example.test").first())

    def test_admin_links_ajax_assets_and_local_icons(self):
        self.admin_login(); self.app.config["PUBLIC_BASE_URL"] = "https://loyalty.example"
        html = self.client.get("/admin?q=Marca").get_data(as_text=True)
        self.assertIn("https://loyalty.example/join/marca-uno", html)
        self.assertIn('data-ajax-target="#business-results"', html)
        self.assertIn("data-copy-text", html)
        self.assertNotIn("cdnjs.cloudflare.com", html)
        self.assertEqual(self.client.get("/static/icons.svg").status_code, 200)

    def test_customization_defaults_persistence_and_validation(self):
        self.admin_login()
        response = self.client.post(f"/admin/business/{self.business.id}/update", json={
            "visual_theme": "elegant", "font_style": "classic", "text_size": "large",
            "primary_color": "#123456", "secondary_color": "#FEDCBA",
        })
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.business)
        self.assertEqual((self.business.visual_theme, self.business.font_style, self.business.text_size), ("elegant", "classic", "large"))
        self.assertEqual(self.client.post(f"/admin/business/{self.business.id}/update", json={"visual_theme": "arbitrary"}).status_code, 400)
        db.session.refresh(self.business); self.assertEqual(self.business.visual_theme, "elegant")

    def test_email_login_ambiguity_is_revealed_only_after_password(self):
        first = self.manager(self.business, "one", "shared@example.test")
        second = self.manager(self.other, "two", "shared@example.test")
        third_business = Business(name="Cuenta con otra clave", slug="third", web_access_enabled=True); db.session.add(third_business); db.session.commit()
        self.manager(third_business, "three", "shared@example.test", "DifferentPassword123!")
        wrong = self.client.post("/manager/login", data={"email": "shared@example.test", "password": "wrong"})
        self.assertEqual(wrong.status_code, 401); self.assertNotIn(self.business.name.encode(), wrong.data)
        choose = self.client.post("/manager/login", data={"email": "SHARED@example.test", "password": "ManagerPassword123!"})
        self.assertEqual(choose.status_code, 200); self.assertIn(self.business.name.encode(), choose.data); self.assertIn(self.other.name.encode(), choose.data); self.assertNotIn(third_business.name.encode(), choose.data)
        selected = self.client.post("/manager/login", data={"account_id": first.id})
        self.assertEqual(selected.status_code, 302)
        with self.client.session_transaction() as browser: self.assertEqual(browser["manager_staff_id"], first.id)

    def test_manager_ajax_results_remain_tenant_scoped(self):
        manager = self.manager(self.business, "tenant-manager", "tenant@example.test")
        db.session.add_all([Customer(business_id=self.business.id, full_name="Propio"), Customer(business_id=self.other.id, full_name="Ajeno")]); db.session.commit()
        self.client.post("/manager/login", data={"email": manager.email, "password": "ManagerPassword123!"})
        response = self.client.get("/manager/portal/customers?q=o", headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 200); self.assertIn(b"Propio", response.data); self.assertNotIn(b"Ajeno", response.data)

    def test_dashboard_supports_24_hours_and_zero_buckets(self):
        manager = self.manager(self.business, "stats", "stats@example.test")
        customer = Customer(business_id=self.business.id, full_name="Cliente"); db.session.add(customer); db.session.flush()
        db.session.add(Transaction(business_id=self.business.id, customer_id=customer.id, staff_id=manager.id, type="earn", points_amount=1, timestamp=datetime.now() - timedelta(hours=2)))
        db.session.commit(); self.client.post("/manager/login", data={"email": manager.email, "password": "ManagerPassword123!"})
        html = self.client.get("/manager/portal?period=1").get_data(as_text=True)
        self.assertIn("24 horas", html); self.assertIn("Gráfica de transacciones", html)


if __name__ == "__main__":
    unittest.main()
