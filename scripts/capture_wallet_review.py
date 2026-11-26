"""Capture the panel using a loopback-only synthetic application (Playwright)."""

from datetime import timedelta
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from app import create_app
from app.extensions import db
from app.models import Business, Customer, PlatformAdmin
from app.models.platform_admin_session import AdminWalletReview
from app.services.loyalty_transaction_service import process_loyalty_transaction
from app.services.platform_admin_session import now


def main():
    app = create_app("testing")
    app.config.update(SESSION_COOKIE_SECURE=False, GOOGLE_WALLET_MODE="disabled")
    with app.app_context():
        if db.engine.url.get_backend_name() != "sqlite" or db.engine.url.database != ":memory:":
            raise RuntimeError("Capture requires the synthetic in-memory database")
        db.create_all()
        admin = PlatformAdmin(name="Administración de prueba", email="wallet-demo@example.invalid")
        admin.set_password("SyntheticCapture123")
        business = Business(name="Cafetería de prueba", slug="wallet-demo", loyalty_type="stamps",
                            stamps_reward_limit=8)
        db.session.add_all([admin, business])
        db.session.commit()
        business_id = business.id
        for index in range(2):
            customer = Customer(business_id=business_id, full_name=f"Cliente de prueba {index + 1}")
            db.session.add(customer)
            db.session.commit()
            process_loyalty_transaction(
                business=business, customer_id=customer.id, staff_id=None, action="earn_stamp",
                tx_type="earn", points_amount=0, stamps_amount=index + 1, purchase_amount=0,
                request_id=f"capture-{index}", fingerprint=f"capture-{index}",
            )
    server = make_server("127.0.0.1", 0, app)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    directory = Path("docs/evidence/wallet-reconciliation")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge")
            for width in (1440, 768, 390, 320):
                app.config.update(GOOGLE_WALLET_MODE="disabled", GOOGLE_WALLET_RECONCILIATION_ENABLED=False)
                page = browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())

                def capture(state):
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, state)
                    assert not errors, errors
                    page.screenshot(path=directory / f"{width}-{state}.png", full_page=True)

                page.goto(origin + "/admin/login")
                page.locator('input[name="email"]').fill("wallet-demo@example.invalid")
                page.locator('input[name="password"]').fill("SyntheticCapture123")
                page.get_by_role("button", name="Iniciar sesión").click()
                page.goto(origin + f"/admin/business/{business_id}/wallet/reconciliation")
                candidates = page.get_by_role("checkbox", name="Cliente")
                assert candidates.count() == 2 and not candidates.first.is_checked()
                capture("diagnostico")
                candidates.first.focus()
                page.keyboard.press("Space")
                assert candidates.first.is_checked()
                candidates.last.check()
                assert "2 clientes" in page.locator("[data-wallet-selection-count]").inner_text()
                capture("seleccion")
                page.get_by_role("button", name="Revisar selección").click()
                capture("deshabilitada")
                app.config.update(GOOGLE_WALLET_MODE="real", GOOGLE_WALLET_RECONCILIATION_ENABLED=True)
                page.reload()
                confirmation = page.get_by_role("checkbox", name="Confirmo esta selección")
                assert confirmation.is_visible() and not confirmation.is_checked()
                capture("confirmacion")
                review_id = page.url.rsplit("/", 1)[-1]
                for state, processed in (("confirmado", 2), ("parcial", 1)):
                    with app.app_context():
                        review = db.session.get(AdminWalletReview, review_id)
                        review.state = "finished"
                        review.confirmed_at = review.created_at
                        review.owner = "0" * 32
                        review.claimed_at = review.created_at
                        review.finished_at = now() + timedelta(seconds=1)
                        review.result = {
                            "status": "finished", "requested": 2, "processed": processed,
                            "remaining": 2 - processed, "counts": {"success": processed},
                            "stop_reason": "authorization_expired" if processed == 1 else None,
                        }
                        db.session.commit()
                    page.goto(origin + f"/admin/business/{business_id}/wallet/reconciliation")
                    page.get_by_text("Resultado completo" if processed == 2 else "Resultado parcial").first.wait_for()
                    capture(state)
                page.close()
            browser.close()
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


if __name__ == "__main__":
    main()
