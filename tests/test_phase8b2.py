import unittest
from datetime import datetime, timedelta, timezone

from app import create_app
from app.extensions import db
from app.models import Business, Staff, StaffSession
from app.staff_auth import revoke_staff_sessions


class UnifiedStaffAccessPhase8B2TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            AUTH_RATE_LIMIT_ATTEMPTS=10,
            STAFF_ACCESS_MAX_EMAIL_CANDIDATES=8,
            STAFF_ACCESS_CHOICE_SECONDS=300,
        )
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.client = self.app.test_client()
        self.business = self.business_record("Principal", "principal")
        self.other = self.business_record("Secundario", "secundario")
        self.manager = self.staff(self.business, "gerente", "manager", "gerente@example.test", "Password123!", "view_stats")
        self.cashier = self.staff(self.business, "cajero", "cashier", "cajero@example.test", "Password123!", "scanner_operate_web")

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def business_record(self, name, slug):
        record = Business(name=name, slug=slug, is_active=True, web_access_enabled=True)
        db.session.add(record); db.session.commit(); return record

    def staff(self, business, username, role, email, password, permissions, **overrides):
        record = Staff(
            business_id=business.id, username=username, name=username.title(), role=role,
            email=email, permissions=permissions, is_active=True, web_access_enabled=True,
            mobile_access_enabled=False,
        )
        for key, value in overrides.items(): setattr(record, key, value)
        record.set_password(password); db.session.add(record); db.session.commit(); return record

    def login(self, email, password="Password123!", remember=False):
        return self.client.post("/staff-access", data={
            "email": email, "password": password, "remember": "1" if remember else "0",
        })

    def test_single_manager_and_cashier_open_role_specific_portals(self):
        manager = self.login("GERENTE@example.test")
        self.assertEqual(manager.status_code, 302); self.assertIn("/manager/portal", manager.location)
        with self.client.session_transaction() as browser:
            self.assertEqual(browser["manager_staff_id"], self.manager.id); self.assertNotIn("staff_staff_id", browser)
        self.client.post("/manager/logout")
        cashier = self.login("cajero@example.test")
        self.assertEqual(cashier.status_code, 302); self.assertIn("/staff/portal/scanner", cashier.location)
        with self.client.session_transaction() as browser:
            self.assertEqual(browser["staff_staff_id"], self.cashier.id); self.assertNotIn("manager_staff_id", browser)

    def test_multiple_businesses_and_roles_require_bound_post_auth_choice(self):
        other_cashier = self.staff(self.other, "mixto", "cashier", "shared@example.test", "SharedPassword123!", "scanner_operate_web")
        other_manager = self.staff(self.business, "mixto-manager", "manager", "shared@example.test", "SharedPassword123!", "view_stats")
        response = self.login("shared@example.test", "SharedPassword123!")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(self.business.name, html); self.assertIn(self.other.name, html)
        with self.client.session_transaction() as browser:
            pending = browser["staff_access_choice"]
            self.assertNotIn("password", pending)
            self.assertEqual({"staff_id", "business_id", "role", "auth_version"}, set(pending["accounts"][0]))
        selected = self.client.post("/staff-access", data={"account_id": other_cashier.id})
        self.assertEqual(selected.status_code, 302); self.assertIn("/staff/portal/scanner", selected.location)
        self.assertNotEqual(other_cashier.id, other_manager.id)

    def test_same_email_with_different_passwords_never_reveals_other_account(self):
        first = self.staff(self.other, "first", "manager", "duplicate@example.test", "FirstPassword123!", "view_stats")
        hidden_business = self.business_record("Clave diferente", "clave-diferente")
        self.staff(hidden_business, "second", "manager", "duplicate@example.test", "SecondPassword123!", "view_stats")
        response = self.login("duplicate@example.test", "FirstPassword123!")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as browser: self.assertEqual(browser["manager_staff_id"], first.id)

    def test_invalid_suspended_and_unauthorized_accounts_use_generic_failure(self):
        cases = []
        cases.append(self.login("missing@example.test", "WrongPassword123!"))
        cases.append(self.login(self.manager.email, "WrongPassword123!"))
        self.manager.is_active = False; db.session.commit(); cases.append(self.login(self.manager.email))
        self.manager.is_active = True; self.business.is_active = False; db.session.commit(); cases.append(self.login(self.manager.email))
        self.business.is_active = True; self.cashier.permissions = ""; db.session.commit(); cases.append(self.login(self.cashier.email))
        self.cashier.permissions = "scanner_operate_web"; self.cashier.web_access_enabled = False; db.session.commit(); cases.append(self.login(self.cashier.email))
        for response in cases:
            self.assertEqual(response.status_code, 401)
            self.assertIn("No fue posible iniciar sesión", response.get_data(as_text=True))
            self.assertNotIn(self.business.name, response.get_data(as_text=True))

    def test_selector_rejects_tampering_expiry_and_changed_authorization(self):
        first = self.staff(self.business, "choice-one", "manager", "choice@example.test", "ChoicePassword123!", "view_stats")
        second = self.staff(self.other, "choice-two", "manager", "choice@example.test", "ChoicePassword123!", "view_stats")
        outsider = self.staff(self.other, "outsider", "manager", "outsider@example.test", "ChoicePassword123!", "view_stats")
        self.login("choice@example.test", "ChoicePassword123!")
        self.assertEqual(self.client.post("/staff-access", data={"account_id": outsider.id}).status_code, 401)
        self.login("choice@example.test", "ChoicePassword123!")
        with self.client.session_transaction() as browser:
            pending = dict(browser["staff_access_choice"]); pending["expires_at"] = 0; browser["staff_access_choice"] = pending
        self.assertEqual(self.client.post("/staff-access", data={"account_id": first.id}).status_code, 401)
        self.login("choice@example.test", "ChoicePassword123!")
        second.auth_version += 1; db.session.commit()
        self.assertEqual(self.client.post("/staff-access", data={"account_id": second.id}).status_code, 401)
        self.login("choice@example.test", "ChoicePassword123!")
        self.business.is_active = False; db.session.commit()
        self.assertEqual(self.client.post("/staff-access", data={"account_id": first.id}).status_code, 401)

    def test_candidate_limit_fails_closed_without_partial_account_selection(self):
        self.app.config["STAFF_ACCESS_MAX_EMAIL_CANDIDATES"] = 2
        for index in range(3):
            business = self.business_record(f"Negocio {index}", f"negocio-{index}")
            self.staff(
                business, f"bounded-{index}", "manager", "bounded@example.test",
                "BoundedPassword123!", "view_stats",
            )
        response = self.login("bounded@example.test", "BoundedPassword123!")
        self.assertEqual(response.status_code, 401)
        self.assertIn("No fue posible iniciar sesión", response.get_data(as_text=True))
        with self.client.session_transaction() as browser:
            self.assertNotIn("staff_access_choice", browser)
            self.assertNotIn("manager_staff_id", browser)

    def test_remembered_session_is_revocable_and_bound_to_auth_version(self):
        response = self.login(self.manager.email, remember=True)
        self.assertEqual(response.status_code, 302)
        row = StaffSession.query.filter_by(staff_id=self.manager.id, revoked_at=None).one()
        self.assertGreater((row.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds(), 6 * 86400)
        revoke_staff_sessions(self.manager); db.session.commit()
        self.assertEqual(self.client.get("/manager/portal").status_code, 302)
        self.assertIsNotNone(db.session.get(StaffSession, row.id).revoked_at)

    def test_rate_limit_is_persistent_by_email_and_ip(self):
        self.app.config["AUTH_RATE_LIMIT_ATTEMPTS"] = 2
        self.assertEqual(self.login("limited@example.test", "wrong").status_code, 401)
        self.assertEqual(self.login("limited@example.test", "wrong").status_code, 401)
        limited = self.login("limited@example.test", "wrong")
        self.assertEqual(limited.status_code, 429); self.assertNotIn("existe", limited.get_data(as_text=True).lower())

    def test_legacy_cashier_without_email_remains_available_but_not_enumerated(self):
        legacy = self.staff(self.other, "legacy", "cashier", None, "LegacyPassword123!", "scanner_operate_web")
        self.assertEqual(self.login("legacy", "LegacyPassword123!").status_code, 401)
        page = self.client.get("/staff/login?legacy=1")
        self.assertEqual(page.status_code, 200); self.assertIn(b"Compatibilidad temporal", page.data)
        response = self.client.post("/staff/login", data={
            "identity": legacy.username, "business_slug": legacy.business.slug,
            "password": "LegacyPassword123!",
        })
        self.assertEqual(response.status_code, 302); self.assertIn("/staff/portal/scanner", response.location)

    def test_unified_ui_replaces_role_choice_and_keeps_pwa_contract(self):
        for path in ("/staff-access", "/manager/login", "/staff/login"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('name="email"', html); self.assertIn('name="password"', html)
            self.assertNotIn('name="business_slug"', html); self.assertNotIn("Elige otro acceso", html)
            self.assertIn("max-w-md", html)
        manifest_response = self.client.get("/static/manifest.webmanifest")
        manifest = manifest_response.get_json()
        manifest_response.close()
        self.assertIn("/staff-access", manifest["start_url"])

    def test_csrf_protects_login_and_account_selection(self):
        self.app.config["CSRF_ENABLED"] = True
        client = self.app.test_client()
        self.assertEqual(client.post("/staff-access", data={"email": self.manager.email, "password": "Password123!"}).status_code, 400)
        with client.session_transaction() as browser: browser["csrf_token"] = "phase8b2-csrf"
        second = self.staff(self.other, "manager-csrf", "manager", self.manager.email, "Password123!", "view_stats")
        response = client.post("/staff-access", data={
            "email": self.manager.email, "password": "Password123!", "csrf_token": "phase8b2-csrf",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post("/staff-access", data={"account_id": second.id}).status_code, 400)
        selected = client.post("/staff-access", data={"account_id": second.id, "csrf_token": "phase8b2-csrf"})
        self.assertEqual(selected.status_code, 302)


if __name__ == "__main__":
    unittest.main()
