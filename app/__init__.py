import hmac
import os
from flask import Flask
from app.config import config_by_name
from app.extensions import db, migrate, cors
from app.routes import register_routes


def create_app(config_name=None):
    """Application factory for J2M Interactive Loyalty SaaS."""
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    app = Flask(__name__)
    config_class = config_by_name.get(config_name, config_by_name["default"])
    app.config.from_object(config_class)

    if config_name == "production":
        app.config["CORS_ORIGINS"] = os.getenv("CORS_ORIGINS", app.config.get("CORS_ORIGINS", "*"))
        secret = os.getenv("SECRET_KEY", app.config.get("SECRET_KEY") or "")
        app.config["SECRET_KEY"] = secret
        forbidden = {"default-dev-secret-key-j2m", "change-this-secret-key-in-production-j2m-loyalty"}
        if secret in forbidden or len(secret) < 32:
            raise RuntimeError("Producción requiere SECRET_KEY aleatoria de al menos 32 caracteres.")
        app.config["DEBUG"] = False
        jwt_secret = os.getenv("JWT_SECRET_KEY", app.config.get("JWT_SECRET_KEY") or "")
        if jwt_secret in {"default-jwt-secret-key", "change-jwt-secret-key-in-production"} or len(jwt_secret) < 32:
            raise RuntimeError("Producción requiere JWT_SECRET_KEY independiente y aleatoria de al menos 32 caracteres.")
        if hmac.compare_digest(jwt_secret, secret):
            raise RuntimeError("Producción requiere que JWT_SECRET_KEY sea distinta de SECRET_KEY.")
        app.config["JWT_SECRET_KEY"] = jwt_secret
        if app.config.get("CORS_ORIGINS") == "*":
            raise RuntimeError("Producción requiere CORS_ORIGINS explícito.")
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config.get("CORS_ORIGINS", "*")}})
    # Import models so SQLAlchemy / Alembic are aware of them
    from app import models  # noqa: F401

    # Register routes / blueprints
    register_routes(app)

    from app.security import csrf_token, validate_csrf, wants_json

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def protect_admin_forms():
        from flask import jsonify, request
        protected_manager_path = request.path in {"/manager/login", "/manager/logout"} or request.path.startswith("/manager/portal")
        if app.config.get("CSRF_ENABLED", True) and request.method in {"POST", "PUT", "PATCH", "DELETE"} and (request.path.startswith("/admin") or protected_manager_path):
            if not validate_csrf():
                payload = {"status": "error", "message": "Token CSRF inválido o ausente."}
                return jsonify(payload), 400

    @app.errorhandler(413)
    def upload_too_large(_error):
        return {"status": "error", "message": "El archivo excede el tamaño permitido."}, 413

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://unpkg.com")
        if config_name == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    # Register CLI commands
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
    return app
