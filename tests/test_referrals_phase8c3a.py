import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, PlatformAdmin, Promoter, ReferralAttribution,
    ReferralEligibility, ReferralParticipant, ReferralProgramAudit, Staff,
)
from app.config import TestingConfig
from app.routes.staff_portal import _invitation_url
from app.services.referral_participant_service import configure_staff_referrals, resolve_invitation
from app.services.referral_program_service import ReferralProgramError, configure_referral_program


class ReferralsPhase8C3ATestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config["PUBLIC_BASE_URL"] = "https://j2m.example"
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.client = self.app.test_client()
        self.business = Business(name="Uno", slug="uno-8c3", referrals_enabled=True, web_access_enabled=True)
        self.other = Business(name="Dos", slug="dos-8c3", referrals_enabled=True, web_access_enabled=True)
        self.admin = PlatformAdmin(name="Admin", email="admin@j2m.test", is_active=True); self.admin.set_password("PasswordSeguro123!")
        self.manager = self._staff(self.business, "manager", "manager", "manage_referral_participants,refer_customers")
        self.worker = self._staff(self.business, "worker", "cashier", "scanner_operate_web,refer_customers")
        db.session.add_all([self.business, self.other, self.admin]); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.session.execute(text("PRAGMA foreign_keys=OFF")); db.drop_all(); self.ctx.pop()

    def _staff(self, business, username, role, permissions):
        staff = Staff(business=business, username=username, role=role, permissions=permissions, is_active=True, web_access_enabled=True)
        staff.set_password("PasswordSeguro123!"); db.session.add(staff)
        return staff

    def _enable_worker(self):
        return configure_staff_referrals(
            business=self.business, target_staff_id=self.worker.id, enabled=True,
            actor=self.manager, reason="Participación autorizada.", request_id="enable-worker",
        )[0]

    def test_integrated_tab_order_and_no_external_promoter_creation_form(self):
        with self.client.session_transaction() as browser: browser["platform_admin_id"] = self.admin.id
        page = self.client.get(f"/admin/business/{self.business.id}?tab=referrals")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertLess(html.index("tabBtn-customers"), html.index("tabBtn-referrals"))
        self.assertNotIn("Gestionar promotores", html)
        self.assertNotIn("Crear promotor", html)
        self.assertEqual(self.client.post(f"/admin/business/{self.business.id}/promoters", data={}).status_code, 410)

    def test_program_switch_is_audited_idempotent_and_blocks_attribution(self):
        participant = self._enable_worker()
        first = configure_referral_program(business=self.business, enabled=False, actor_type="platform_admin", actor_id=self.admin.id, reason="Pausa preventiva.", request_id="switch-1")
        replay = configure_referral_program(business=self.business, enabled=False, actor_type="platform_admin", actor_id=self.admin.id, reason="Pausa preventiva.", request_id="switch-1")
        self.assertEqual(first, (False, False)); self.assertEqual(replay, (False, True))
        self.assertEqual(ReferralProgramAudit.query.count(), 1)
        self.assertIsNone(resolve_invitation(business_id=self.business.id, code=participant.invitation_code))
        with self.assertRaises(ReferralProgramError):
            configure_referral_program(business=self.business, enabled=True, actor_type="platform_admin", actor_id=self.admin.id, reason="Distinto.", request_id="switch-1")
        self.assertEqual(ReferralAttribution.query.count(), 0)

    def test_staff_portal_shows_only_own_aggregate_invitation_and_qr(self):
        participant = self._enable_worker()
        login = self.client.post("/staff/login?legacy=1", data={
            "identity": "worker", "password": "PasswordSeguro123!",
            "business_slug": self.business.slug,
        })
        self.assertEqual(login.status_code, 302)
        page = self.client.get("/staff/portal/referrals")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("https://j2m.example/join/uno-8c3?ref=", html)
        self.assertIn(participant.invitation_code, html)
        self.assertNotIn("teléfono", html.lower())
        qr = self.client.get("/staff/portal/referrals/qr.svg")
        self.assertEqual(qr.status_code, 200); self.assertEqual(qr.mimetype, "image/svg+xml")

    def test_staff_without_permission_cannot_be_enabled(self):
        denied = self._staff(self.business, "denied", "cashier", "scanner_operate_web"); db.session.commit()
        with self.assertRaisesRegex(Exception, "refer_customers"):
            configure_staff_referrals(business=self.business, target_staff_id=denied.id, enabled=True, actor=self.manager, reason="No permitido.", request_id="denied")

    def test_new_business_defaults_to_referrals_disabled(self):
        new_business = Business(name="Nuevo", slug="nuevo")
        db.session.add(new_business); db.session.commit()
        self.assertFalse(new_business.referrals_enabled)

    def test_paused_historical_promoter_keeps_metrics_but_has_no_link_or_qr(self):
        promoter = Promoter(business_id=self.business.id, name="Histórico", username="historico")
        promoter.set_password("PasswordSeguro123!"); db.session.add(promoter); db.session.flush()
        from app.services.referral_participant_service import ensure_external_promoter
        ensure_external_promoter(promoter); self.business.referrals_enabled = False; db.session.commit()
        login = self.client.post("/promoter/login", data={"business_slug": self.business.slug, "identity": "historico", "password": "PasswordSeguro123!"})
        self.assertEqual(login.status_code, 302)
        page = self.client.get("/promoter/portal").get_data(as_text=True)
        self.assertIn("Programa pausado", page); self.assertNotIn("Descargar QR", page)
        self.assertNotIn(promoter.referral_code, page)
        self.assertEqual(self.client.get("/promoter/referral-qr.svg").status_code, 409)
        self.business.referrals_enabled = True; db.session.commit()
        self.assertIn(promoter.referral_code, self.client.get("/promoter/portal").get_data(as_text=True))

    def test_staff_invitation_origin_is_canonical_and_host_safe(self):
        participant = self._enable_worker()
        with self.app.test_request_context("/", base_url="https://attacker.example"):
            from flask import g
            g.staff_browser = self.worker
            self.app.config.update(APP_ENV="production", PUBLIC_BASE_URL="https://public.example")
            self.assertEqual(_invitation_url(participant), f"https://public.example/join/{self.business.slug}?ref={participant.invitation_code}")
            for invalid in ("", "http://public.example", "https://user:pass@public.example", "https://public.example/path", "https://public.example?q=1"):
                self.app.config["PUBLIC_BASE_URL"] = invalid
                self.assertIsNone(_invitation_url(participant))
            self.app.config.update(APP_ENV="development", PUBLIC_BASE_URL="")
            self.assertTrue(_invitation_url(participant).startswith("http://localhost/"))

    def test_manager_cookie_portal_is_permission_csrf_and_tenant_scoped(self):
        login = self.client.post("/manager/login", data={"identity":"manager", "password":"PasswordSeguro123!", "business_slug":self.business.slug})
        self.assertEqual(login.status_code, 302)
        page = self.client.get("/manager/portal/referrals")
        self.assertEqual(page.status_code, 200); self.assertIn(b"Programa de referidos", page.data)
        self.assertNotIn(self.other.name.encode(), page.data)
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post("/manager/portal/referrals/program", data={"enabled":"false", "reason":"Pausa"}).status_code, 400)
        with self.client.session_transaction() as browser: token = browser["csrf_token"]
        changed = self.client.post("/manager/portal/referrals/program", data={"enabled":"false", "reason":"Pausa autorizada.", "csrf_token":token})
        self.assertEqual(changed.status_code, 302); self.assertFalse(db.session.get(Business, self.business.id).referrals_enabled)

    def test_referral_panel_paginates_and_has_exact_metrics(self):
        participant = self._enable_worker()
        for index in range(31):
            customer = Customer(business_id=self.business.id, full_name=f"Cliente {index}", phone=f"+52550000{index:04d}")
            db.session.add(customer); db.session.flush()
            attribution = ReferralAttribution(business_id=self.business.id, participant_id=participant.id, customer_id=customer.id)
            db.session.add(attribution); db.session.flush()
            db.session.add(ReferralEligibility(business_id=self.business.id, attribution_id=attribution.id, participant_id=participant.id, customer_id=customer.id, status="pending"))
        db.session.commit()
        with self.client.session_transaction() as browser: browser["platform_admin_id"] = self.admin.id
        page = self.client.get(f"/admin/business/{self.business.id}?tab=referrals&referral_page=2")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200); self.assertIn("Página 2 de 2", html)
        self.assertNotIn("Simulados", html)
        self.assertIn('<td class="p-3 font-semibold">Cliente 0</td>', html)
        self.assertNotIn('<td class="p-3 font-semibold">Cliente 30</td>', html)

    def test_concurrent_switch_audits_form_a_consistent_state_chain(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{Path(directory, 'switch.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri): app = create_app("testing")
            with app.app_context():
                db.create_all(); business = Business(name="Concurrente", slug="switch", referrals_enabled=True); db.session.add(business); db.session.commit(); business_id = business.id
            def change(item):
                enabled, request_id = item
                with app.app_context():
                    business = db.session.get(Business, business_id)
                    return configure_referral_program(business=business, enabled=enabled, actor_type="platform_admin", actor_id=1, reason=f"Cambio {request_id}", request_id=request_id)
            with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(change, ((False, "off"), (True, "on"))))
            with app.app_context():
                audits = ReferralProgramAudit.query.order_by(ReferralProgramAudit.id).all()
                state = True
                for audit in audits:
                    self.assertEqual(audit.previous_enabled, state); state = audit.new_enabled
                self.assertEqual(db.session.get(Business, business_id).referrals_enabled, state)


if __name__ == "__main__":
    unittest.main()
