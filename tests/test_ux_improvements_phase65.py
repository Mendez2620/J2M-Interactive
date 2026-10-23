import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

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
        base = {"birthdate": "1990-01-01", "terms_accepted": True, "privacy_accepted": True}
        response = self.client.post("/join/marca-uno", json={"full_name": "Cliente MX", "email": "mx@example.test", "phone_country": "+52", "phone": "55 1234 5678", **base})
        self.assertEqual(response.status_code, 202)
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        self.assertEqual(self.client.post("/join/marca-uno/verify", json={"code": code}).status_code, 201)
        self.assertEqual(Customer.query.filter_by(email="mx@example.test").one().phone, "+525512345678")
        response = self.client.post("/join/marca-uno", json={"full_name": "Cliente ES", "email": "es@example.test", "phone_country": "+34", "phone": "612 345 678", **base})
        self.assertEqual(response.status_code, 202)
        code = self.app.extensions["recovery_mailer"].outbox[-1]["code"]
        self.assertEqual(self.client.post("/join/marca-uno/verify", json={"code": code}).status_code, 201)
        self.assertEqual(Customer.query.filter_by(email="es@example.test").one().phone, "+34612345678")

    def test_invalid_phone_is_rejected_without_customer(self):
        response = self.client.post("/join/marca-uno", json={"full_name": "Inválido", "email": "bad@example.test", "phone_country": "+52", "phone": "abc123", "birthdate": "1990-01-01", "terms_accepted": True, "privacy_accepted": True})
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
        with open("app/static/js/ajax-search.js", encoding="utf-8") as script:
            ajax_source = script.read()
        self.assertIn('a[href*="page="]', ajax_source)
        self.assertIn("event.target.closest('[data-copy-text]')", ajax_source)
        self.assertIn("window.initializeCsrfForms?.(target)", ajax_source)
        self.assertIn("[data-copy-fallback]", ajax_source)
        self.assertNotIn("target.innerHTML", ajax_source)

    def test_staff_management_uses_one_action_and_no_nested_forms(self):
        self.admin_login()
        staff = self.manager(self.business, "staff-ui", "staff-ui@example.test")
        html = self.client.get(f"/admin/business/{self.business.id}?tab=staff").get_data(as_text=True)
        self.assertIn("Gestionar usuario", html)
        self.assertIn('id="modalToggleButton"', html)
        self.assertIn('id="modalDeleteButton"', html)
        self.assertNotIn('id="modalToggleForm"', html)

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

    def test_ambiguous_login_selection_is_bound_to_auth_version_and_expiry(self):
        first = self.manager(self.business, "first-versioned", "versioned@example.test")
        self.manager(self.other, "second-versioned", "versioned@example.test")
        choose = self.client.post("/manager/login", data={"email": "versioned@example.test", "password": "ManagerPassword123!"})
        self.assertEqual(choose.status_code, 200)
        with self.client.session_transaction() as browser:
            pending = browser["manager_account_choice"]
            self.assertEqual({"staff_id", "auth_version"}, set(pending["accounts"][0]))
            self.assertNotIn("password", pending)
            self.assertNotIn("token", pending)
        first.auth_version += 1
        db.session.commit()
        rejected = self.client.post("/manager/login", data={"account_id": first.id})
        self.assertEqual(rejected.status_code, 401)
        with self.client.session_transaction() as browser:
            self.assertNotIn("manager_staff_id", browser)
            self.assertNotIn("manager_account_choice", browser)

        self.client.post("/manager/login", data={"email": "versioned@example.test", "password": "ManagerPassword123!"})
        with self.client.session_transaction() as browser:
            expired = dict(browser["manager_account_choice"])
            expired["expires_at"] = 0
            browser["manager_account_choice"] = expired
        self.assertEqual(self.client.post("/manager/login", data={"account_id": first.id}).status_code, 401)

    def test_password_reset_revokes_pending_ambiguous_login(self):
        first = self.manager(self.business, "first-reset", "reset-choice@example.test")
        self.manager(self.other, "second-reset", "reset-choice@example.test")
        self.assertEqual(self.client.post("/manager/login", data={
            "email": "reset-choice@example.test", "password": "ManagerPassword123!",
        }).status_code, 200)
        self.admin_login()
        reset = self.client.post(f"/admin/staff/{first.id}/reset-password", json={
            "password": "ReplacementPassword456!", "password_confirmation": "ReplacementPassword456!",
        })
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.client.post("/manager/login", data={"account_id": first.id}).status_code, 401)
        with self.client.session_transaction() as browser:
            self.assertNotIn("manager_staff_id", browser)
            self.assertNotIn("manager_account_choice", browser)

    def test_ambiguous_login_revalidates_status_business_and_discards_old_choice(self):
        first = self.manager(self.business, "first-state", "state@example.test")
        second = self.manager(self.other, "second-state", "state@example.test")
        self.client.post("/manager/login", data={"email": "state@example.test", "password": "ManagerPassword123!"})
        second.is_active = False
        db.session.commit()
        self.assertEqual(self.client.post("/manager/login", data={"account_id": second.id}).status_code, 401)

        second.is_active = True
        db.session.commit()
        self.client.post("/manager/login", data={"email": "state@example.test", "password": "ManagerPassword123!"})
        self.other.web_access_enabled = False
        db.session.commit()
        self.assertEqual(self.client.post("/manager/login", data={"account_id": second.id}).status_code, 401)

        self.other.web_access_enabled = True
        db.session.commit()
        self.client.post("/manager/login", data={"email": "state@example.test", "password": "ManagerPassword123!"})
        self.assertEqual(self.client.post("/manager/login", data={"account_id": 999999}).status_code, 401)
        self.client.post("/manager/login", data={"email": "state@example.test", "password": "ManagerPassword123!"})
        first.role = "cashier"
        db.session.commit()
        self.assertEqual(self.client.post("/manager/login", data={"account_id": first.id}).status_code, 401)
        first.role = "manager"
        db.session.commit()
        self.client.post("/manager/login", data={"email": "state@example.test", "password": "ManagerPassword123!"})
        self.client.post("/manager/login", data={"email": "nobody@example.test", "password": "WrongPassword123!"})
        self.assertEqual(self.client.post("/manager/login", data={"account_id": first.id}).status_code, 401)

    def test_ajax_rows_render_csrf_token_and_toggle_with_protection_enabled(self):
        self.admin_login()
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "ajax-csrf"
        self.app.config["CSRF_ENABLED"] = True
        response = self.client.get("/admin?q=Marca", headers={"X-Requested-With": "XMLHttpRequest"})
        html = response.get_data(as_text=True)
        self.assertIn('name="csrf_token" value="ajax-csrf"', html)
        toggle = self.client.post(
            f"/admin/business/{self.business.id}/toggle-status",
            data={"csrf_token": "ajax-csrf"},
        )
        self.assertEqual(toggle.status_code, 302)
        db.session.refresh(self.business)
        self.assertFalse(self.business.is_active)

    def test_manager_transaction_pagination_preserves_encoded_filters(self):
        manager = self.manager(self.business, "filters", "filters@example.test")
        customer = Customer(business_id=self.business.id, full_name="Filtro")
        db.session.add(customer)
        db.session.flush()
        for index in range(26):
            db.session.add(Transaction(
                business_id=self.business.id, customer_id=customer.id, staff_id=manager.id,
                type="earn", points_amount=1, timestamp=datetime(2026, 10, 1, 12, 0) + timedelta(minutes=index),
            ))
        db.session.commit()
        self.client.post("/manager/login", data={"email": manager.email, "password": "ManagerPassword123!"})
        html = self.client.get("/manager/portal/transactions?type=earn&from=2026-10-01&to=2026-10-02&q=a%26b").get_data(as_text=True)
        marker = 'href="'
        link = html[html.index(marker, html.index("Siguiente") - 400) + len(marker):]
        link = link[:link.index('"')].replace("&amp;", "&")
        params = parse_qs(urlparse(link).query)
        self.assertEqual(params, {"page": ["2"], "q": ["a&b"], "type": ["earn"], "from": ["2026-10-01"], "to": ["2026-10-02"]})

    def test_reset_modal_closes_permissions_and_manages_focus(self):
        self.admin_login()
        self.manager(self.business, "modal-user", "modal@example.test")
        html = self.client.get(f"/admin/business/{self.business.id}?tab=staff").get_data(as_text=True)
        self.assertIn("document.getElementById('permissionsModal').classList.add('hidden')", html)
        self.assertIn("document.getElementById('resetPasswordInput').focus()", html)
        self.assertIn("resetPasswordReturnFocus.focus()", html)
        self.assertIn('name="password_confirmation"', html)
        self.assertIn('role="dialog" aria-modal="true" aria-labelledby="resetPasswordTitle"', html)

    def test_permissions_modal_focus_is_trapped_and_restored(self):
        self.admin_login()
        self.manager(self.business, "focus-user", "focus@example.test")
        html = self.client.get(f"/admin/business/{self.business.id}?tab=staff").get_data(as_text=True)
        self.assertIn("permissionsModal.querySelector('[data-close-permissions]').focus()", html)
        self.assertIn("button.addEventListener('click', closePermissions)", html)
        self.assertIn("if (permissionsReturnFocus?.isConnected) permissionsReturnFocus.focus()", html)
        self.assertIn("event.key !== 'Tab'", html)
        self.assertIn("event.shiftKey && document.activeElement === first", html)
        self.assertIn("document.activeElement === last", html)

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
