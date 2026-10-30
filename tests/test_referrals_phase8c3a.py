import unittest

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import (
    Business, PlatformAdmin, ReferralAttribution, ReferralParticipant,
    ReferralProgramAudit, Staff,
)
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


if __name__ == "__main__":
    unittest.main()
