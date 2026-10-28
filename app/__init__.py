import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlparse
from datetime import datetime, timezone
from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from app.config import (
    POSTGRES_TLS_MODES, config_by_name, normalize_database_url,
    postgres_engine_options,
)
from app.extensions import db, migrate, cors
from app.routes import register_routes


def create_app(config_name=None):
    """Application factory for J2M Interactive Loyalty SaaS."""
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    app = Flask(__name__)
    config_class = config_by_name.get(config_name, config_by_name["default"])
    app.config.from_object(config_class)
    app.config["APP_ENV"] = config_name
    from app.session_policy import RoleAwareSessionInterface
    app.session_interface = RoleAwareSessionInterface()

    if config_name == "production":
        app.config["CORS_ORIGINS"] = os.getenv("CORS_ORIGINS", app.config.get("CORS_ORIGINS", "*"))
        secret = os.getenv("SECRET_KEY", app.config.get("SECRET_KEY") or "")
        app.config["SECRET_KEY"] = secret
        forbidden = {"default-dev-secret-key-j2m", "change-this-secret-key-in-production-j2m-loyalty"}
        obvious_placeholders = ("change", "replace", "redacted", "placeholder", "example")
        if secret in forbidden or len(secret) < 32 or any(value in secret.lower() for value in obvious_placeholders):
            raise RuntimeError("Producción requiere SECRET_KEY aleatoria de al menos 32 caracteres.")
        if os.getenv("FLASK_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}:
            raise RuntimeError("Producción no permite FLASK_DEBUG habilitado.")
        app.config["DEBUG"] = False
        jwt_secret = os.getenv("JWT_SECRET_KEY", app.config.get("JWT_SECRET_KEY") or "")
        if (jwt_secret in {"default-jwt-secret-key", "change-jwt-secret-key-in-production"}
                or len(jwt_secret) < 32 or any(value in jwt_secret.lower() for value in obvious_placeholders)):
            raise RuntimeError("Producción requiere JWT_SECRET_KEY independiente y aleatoria de al menos 32 caracteres.")
        if hmac.compare_digest(jwt_secret, secret):
            raise RuntimeError("Producción requiere que JWT_SECRET_KEY sea distinta de SECRET_KEY.")
        app.config["JWT_SECRET_KEY"] = jwt_secret
        identity_key = os.getenv("REGISTRATION_IDENTITY_HMAC_KEY", "")
        if (len(identity_key) < 32 or identity_key in {secret, jwt_secret}
                or any(value in identity_key.lower() for value in obvious_placeholders)):
            raise RuntimeError("Producción requiere REGISTRATION_IDENTITY_HMAC_KEY independiente y estable de al menos 32 caracteres.")
        app.config["REGISTRATION_IDENTITY_HMAC_KEY"] = identity_key
        cors_origins = [item.strip() for item in str(app.config.get("CORS_ORIGINS") or "").split(",") if item.strip()]
        if not cors_origins or any(
            item == "*" or (lambda parsed: parsed.scheme != "https" or not parsed.hostname
                            or parsed.username or parsed.password or parsed.query or parsed.fragment
                            or parsed.path not in {"", "/"})(urlparse(item))
            for item in cors_origins
        ):
            raise RuntimeError("Producción requiere CORS_ORIGINS con orígenes HTTPS explícitos.")
        app.config["CORS_ORIGINS"] = cors_origins
        if os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError("Producción requiere SESSION_COOKIE_SECURE=true.")
        raw_database_url = os.getenv("DATABASE_URL", "").strip()
        if not raw_database_url:
            raise RuntimeError("Producción requiere una DATABASE_URL PostgreSQL explícita.")
        normalized_database_url = normalize_database_url(raw_database_url)
        try:
            database_url = make_url(normalized_database_url)
        except (ArgumentError, ValueError):
            raise RuntimeError("DATABASE_URL debe ser una URL PostgreSQL válida en producción.") from None
        if database_url.get_backend_name() != "postgresql":
            raise RuntimeError("Producción requiere PostgreSQL; SQLite no está permitido.")
        configured_sslmode = os.getenv("DATABASE_SSLMODE", "require").strip().lower()
        url_sslmode = str(database_url.query.get("sslmode", "")).strip().lower()
        if configured_sslmode not in POSTGRES_TLS_MODES or (url_sslmode and url_sslmode not in POSTGRES_TLS_MODES):
            raise RuntimeError("Producción requiere TLS PostgreSQL con sslmode require, verify-ca o verify-full.")
        effective_sslmode = url_sslmode or configured_sslmode
        app.config["SQLALCHEMY_DATABASE_URI"] = normalized_database_url
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = postgres_engine_options(effective_sslmode)
        public_base_url = app.config.get("PUBLIC_BASE_URL", "")
        parsed_public_url = urlparse(public_base_url)
        if (not public_base_url or parsed_public_url.scheme != "https"
                or not parsed_public_url.hostname or parsed_public_url.username
                or parsed_public_url.password or parsed_public_url.query
                or parsed_public_url.fragment or parsed_public_url.path not in {"", "/"}):
            raise RuntimeError("PUBLIC_BASE_URL debe utilizar HTTPS en producción.")
        proxy_count = app.config.get("TRUSTED_PROXY_COUNT", 0)
        if proxy_count < 0 or proxy_count > 10:
            raise RuntimeError("TRUSTED_PROXY_COUNT debe estar entre 0 y 10.")
        if proxy_count:
            from werkzeug.middleware.proxy_fix import ProxyFix
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_count, x_proto=proxy_count, x_host=proxy_count)
        upload_backend = os.getenv(
            "UPLOAD_STORAGE_BACKEND", app.config.get("UPLOAD_STORAGE_BACKEND", "disabled")
        ).strip().lower()
        if upload_backend != "disabled":
            raise RuntimeError("Producción requiere UPLOAD_STORAGE_BACKEND=disabled hasta disponer de almacenamiento persistente.")
        app.config["UPLOAD_STORAGE_BACKEND"] = upload_backend
        wallet_mode = os.getenv("GOOGLE_WALLET_MODE", app.config.get("GOOGLE_WALLET_MODE", "disabled")).lower()
        if wallet_mode not in {"disabled", "real"}:
            raise RuntimeError("Producción solo permite GOOGLE_WALLET_MODE=disabled o real.")
        app.config["GOOGLE_WALLET_MODE"] = wallet_mode
        if wallet_mode == "real":
            from app.services.google_wallet import GoogleWalletService
            GoogleWalletService(mode="real")

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}})
    from app.email_service import build_recovery_mailer
    app.extensions["recovery_mailer"] = build_recovery_mailer(app.config)
    # Import models so SQLAlchemy / Alembic are aware of them
    from app import models  # noqa: F401

    # Register routes / blueprints
    register_routes(app)

    from app.security import csrf_token, validate_csrf, wants_json
    from app.iconography import contrast_text_color, icon

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["csp_nonce"] = lambda: getattr(g, "csp_nonce", "")
    app.jinja_env.globals["icon"] = icon
    app.jinja_env.globals["contrast_text_color"] = contrast_text_color
    app.jinja_env.globals["public_join_url"] = lambda slug: (
        f"{app.config.get('PUBLIC_BASE_URL') or request.url_root.rstrip('/')}"
        f"/join/{slug}"
    )

    @app.before_request
    def protect_admin_forms():
        g.request_started_at = time.perf_counter()
        g.request_id = secrets.token_hex(16)
        g.csp_nonce = secrets.token_urlsafe(18)
        protected_manager_path = request.path in {"/manager/login", "/manager/logout"} or request.path.startswith("/manager/portal")
        protected_staff_path = request.path == "/staff-access" or request.path.startswith("/staff/") or request.path.startswith("/api/web-scanner/")
        protected_recovery_path = request.path.startswith("/recover/")
        protected_join_path = request.path.startswith("/join/")
        protected_promoter_path = request.path.startswith("/promoter/")
        if app.config.get("CSRF_ENABLED", True) and request.method in {"POST", "PUT", "PATCH", "DELETE"} and (request.path.startswith("/admin") or protected_manager_path or protected_staff_path or protected_recovery_path or protected_join_path or protected_promoter_path):
            if not validate_csrf():
                if not wants_json() and request.path in {"/staff/logout", "/manager/logout", "/promoter/logout"}:
                    if request.path == "/promoter/logout":
                        from app.promoter_auth import current_promoter
                        active_session = current_promoter()
                        if active_session is None:
                            return redirect(url_for("promoter.promoter_login"), code=303)
                        return render_template(
                            "auth/logout_retry.html",
                            logout_url=url_for("promoter.promoter_logout"),
                            return_url=url_for("promoter.promoter_portal"),
                        ), 400
                    from app.manager_auth import current_manager, current_staff_browser_session

                    is_staff = request.path.startswith("/staff/")
                    active_session = current_staff_browser_session() if is_staff else current_manager()
                    login_endpoint = "staff_portal.staff_login" if is_staff else "manager.manager_login"
                    if active_session is None:
                        return redirect(url_for(login_endpoint), code=303)
                    return render_template(
                        "auth/logout_retry.html",
                        logout_url=url_for(
                            "staff_portal.staff_logout" if is_staff else "manager.manager_logout"
                        ),
                        return_url=url_for(
                            "staff_portal.staff_scanner" if is_staff else "manager.manager_portal"
                        ),
                    ), 400
                payload = {"status": "error", "message": "Token CSRF inválido o ausente."}
                return jsonify(payload), 400

    @app.errorhandler(413)
    def upload_too_large(_error):
        return {"status": "error", "message": "El archivo excede el tamaño permitido."}, 413

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        app.logger.error(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(), "level": "error",
            "component": "flask", "event": "unhandled_error",
            "request_id": getattr(g, "request_id", None),
            "endpoint": request.endpoint, "path": request.path,
        }), exc_info=error)
        return jsonify({"status": "error", "message": "Error interno.",
                        "request_id": getattr(g, "request_id", None)}), 500

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=()")
        nonce = getattr(g, "csp_nonce", "")
        if config_name == "production":
            csp = (f"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
                   f"img-src 'self' data: blob: https:; font-src 'self'; "
                   f"style-src 'self' 'unsafe-inline'; "
                   f"script-src 'self' 'nonce-{nonce}'; script-src-attr 'none'; "
                   f"connect-src 'self'")
        else:
            csp = "default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Request-ID", getattr(g, "request_id", ""))
        if request.path.startswith(("/admin", "/manager", "/staff", "/recover", "/api/scanner", "/api/web-scanner")):
            response.headers.setdefault("Cache-Control", "no-store")
        duration_ms = round((time.perf_counter() - getattr(g, "request_started_at", time.perf_counter())) * 1000, 2)
        if app.config.get("LOG_REQUESTS"):
            app.logger.info(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(), "level": "info",
                "component": "http", "request_id": getattr(g, "request_id", None),
                "endpoint": request.endpoint, "path": request.path,
                "status": response.status_code, "duration_ms": duration_ms,
            }))
        if config_name == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    # Register CLI commands
    from app.staging_preflight import register_staging_preflight
    register_staging_preflight(app)

    @app.cli.command('check-birthdays')
    def check_birthdays():
        """Execute the daily birthday campaign."""
        from app.services.birthday_service import BirthdayService
        sent = BirthdayService.run_daily_campaign()
        print(f'Sent {sent} birthday greetings')

    @app.cli.command("create-platform-admin")
    def create_platform_admin():
        """Create the first global administrator through interactive prompts."""
        import click
        from app.models import PlatformAdmin
        from app.security import valid_email, validate_password
        name = click.prompt("Nombre completo").strip()
        email = click.prompt("Correo electrónico").strip().lower()
        phone = click.prompt("Teléfono", default="", show_default=False).strip() or None
        password = click.prompt("Contraseña", hide_input=True, confirmation_prompt=True)
        if not name or not valid_email(email):
            raise click.ClickException("Nombre y correo válido son obligatorios.")
        password_error = validate_password(password)
        if password_error:
            raise click.ClickException(password_error)
        if PlatformAdmin.query.filter_by(email=email).first():
            raise click.ClickException("Ya existe un administrador con ese correo.")
        admin = PlatformAdmin(name=name, email=email, phone=phone, is_active=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        click.echo(f"Administrador de plataforma creado: {email}")

    @app.cli.command("process-google-wallet-sync")
    def process_google_wallet_sync():
        """Process the persistent Google Wallet outbox once."""
        import json
        from app.services.google_wallet_sync_service import (
            google_wallet_sync_counts, process_pending_google_wallet_jobs,
        )
        processed = process_pending_google_wallet_jobs()
        counts = google_wallet_sync_counts()
        print(json.dumps({"processed": processed, **counts}, sort_keys=True))

    @app.cli.command("run-google-wallet-sync-worker")
    def run_google_wallet_sync_worker_command():
        """Continuously process the Google Wallet outbox in a dedicated service."""
        import signal
        import threading
        from app.services.google_wallet_sync_service import run_google_wallet_sync_worker

        stop_event = threading.Event()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signal_name, lambda _signum, _frame: stop_event.set())
        app.logger.info("google_wallet_worker_started")
        run_google_wallet_sync_worker(
            stop_event=stop_event,
            poll_seconds=app.config["GOOGLE_WALLET_SYNC_POLL_SECONDS"],
            batch_size=app.config["GOOGLE_WALLET_SYNC_BATCH_SIZE"],
            alert_pending=app.config["GOOGLE_WALLET_SYNC_ALERT_PENDING"],
            alert_stale_seconds=app.config["GOOGLE_WALLET_SYNC_ALERT_STALE_SECONDS"],
            logger=app.logger,
        )
        app.logger.info("google_wallet_worker_stopped")

    return app
