import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Phase8B1InterfaceTestCase(unittest.TestCase):
    def read(self, relative_path):
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_manager_shell_has_sidebar_drawer_bottom_navigation_and_safe_areas(self):
        template = self.read("app/templates/manager/base.html")
        for marker in (
            'id="managerDrawer"', "data-open-manager-drawer", "data-manager-backdrop",
            "mobile-bottom", "env(safe-area-inset-bottom)", "aria-label=\"Acciones principales\"", "inert",
        ):
            self.assertIn(marker, template)
        self.assertNotIn("overflow-x-auto gap-2 bg-slate-950", template)
        drawer_script = self.read("app/static/js/manager-drawer.js")
        self.assertIn("event.key === 'Escape'", drawer_script)
        self.assertIn("event.key !== 'Tab'", drawer_script)
        self.assertIn('input:not([type="hidden"])', drawer_script)
        self.assertIn("drawer.inert = true", drawer_script)

    def test_manager_drawer_keyboard_behavior(self):
        result = subprocess.run(
            ["node", "tests/js/phase8b1_manager_drawer.test.js"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_navigation_remains_permission_gated_and_logout_is_post_csrf(self):
        template = self.read("app/templates/manager/base.html")
        for permission in (
            "view_stats", "view_customers", "view_transactions", "export_reports",
            "scanner_operate_web", "view_cashiers",
        ):
            self.assertIn(permission, template)
        self.assertGreaterEqual(template.count('method="POST" action="{{ url_for(\'manager.manager_logout\') }}"'), 2)
        self.assertGreaterEqual(template.count('name="csrf_token"'), 2)

    def test_scanner_redesign_preserves_behavior_contract(self):
        template = self.read("app/templates/components/web_scanner.html")
        for marker in (
            "data-web-scanner", "data-start-camera", "data-stop-camera", "data-camera-select",
            "data-customer-panel", "data-confirm-panel", "data-confirm-operation",
            "data-success-panel", "data-reset-scanner", "data-scan-next",
        ):
            self.assertIn(marker, template)
        self.assertIn("La cámara se activa solo cuando tú lo indicas", template)
        self.assertIn("ningún saldo cambiará", template)
        for step in ("scan", "review", "confirm"):
            self.assertIn(f'data-scanner-step="{step}"', template)
        scanner_script = self.read("app/static/js/web-scanner.js")
        self.assertIn("function updateProgress()", scanner_script)
        self.assertIn("aria-current", scanner_script)

    def test_staff_mobile_navigation_keeps_post_logout_and_safe_area(self):
        template = self.read("app/templates/staff/scanner.html")
        self.assertIn("env(safe-area-inset-bottom)", template)
        self.assertIn("staff_portal.staff_scanner", template)
        self.assertGreaterEqual(template.count("staff_portal.staff_logout"), 2)
        self.assertGreaterEqual(template.count('name="csrf_token"'), 2)

    def test_all_static_icon_references_exist_in_local_catalog(self):
        paths = [
            "app/templates/manager/base.html", "app/templates/components/web_scanner.html",
            "app/templates/staff/scanner.html", "app/templates/manager/dashboard.html",
            "app/templates/manager/reports.html",
        ]
        requested = set()
        for path in paths:
            requested.update(re.findall(r"icon\(['\"]([^'\"]+)", self.read(path)))
        available = set(re.findall(r'<symbol id="([^"]+)"', self.read("app/static/icons.svg")))
        self.assertEqual(requested - available, set())

    def test_future_phase_numbers_are_reconciled_without_losing_backlog(self):
        document = self.read("docs/future-phases-8b-8e.md")
        for heading in ("8B.1", "8B.2", "8C", "8D", "8E", "Backlog técnico adicional"):
            self.assertIn(heading, document)
        self.assertIn("Recuperación asistida auditada", document)
        self.assertIn("protección contra enumeración", document)


if __name__ == "__main__":
    unittest.main()
