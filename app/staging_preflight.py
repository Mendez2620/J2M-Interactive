"""Read-only staging configuration diagnostics.

The preflight deliberately avoids contacting external providers. Database
connectivity and the Alembic revision are checked only with an explicit flag.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

import click
from flask import current_app
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import POSTGRES_TLS_MODES
from app.extensions import db


EXPECTED_ALEMBIC_HEAD = "f9a0b1c2d3e4"
_PLACEHOLDERS = ("change", "replace", "redacted", "placeholder", "example", "tu_clave", "your_")


def _placeholder(value):
    lowered = str(value or "").strip().lower()
    return not lowered or any(marker in lowered for marker in _PLACEHOLDERS)


def _origin_is_https(value):
    parsed = urlparse(str(value or "").strip())
    return bool(
        parsed.scheme == "https" and parsed.hostname and not parsed.username
        and not parsed.password and not parsed.query and not parsed.fragment
        and parsed.path in {"", "/"}
    )


def _cors_origins(value):
    items = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _result(results, status, check, message):
    results.append({"status": status, "check": check, "message": message})


def collect_preflight(config, *, check_database=False):
    """Return sanitized diagnostics without changing application state."""
    results = []
    environment = config.get("APP_ENV")
    _result(results, "ok" if environment == "production" else "error", "environment",
            "FLASK_ENV usa el perfil de producción." if environment == "production"
            else "FLASK_ENV debe ser production para staging.")

    secret_names = (
        "SECRET_KEY", "JWT_SECRET_KEY", "REGISTRATION_IDENTITY_HMAC_KEY",
        "REGISTRATION_EMAIL_ENCRYPTION_KEY",
    )
    secrets = [str(config.get(name) or "") for name in secret_names]
    for name, value in zip(secret_names, secrets):
        valid = len(value) >= 32 and not _placeholder(value)
        _result(results, "ok" if valid else "error", name,
                "Configurado con longitud suficiente y sin placeholder evidente."
                if valid else "Falta una clave robusta y estable, sin placeholders.")
    if all(secrets) and len(set(secrets)) == len(secrets):
        _result(results, "ok", "secret_separation", "Las claves de propósitos incompatibles son distintas.")
    else:
        _result(results, "error", "secret_separation", "Cada propósito criptográfico requiere una clave distinta.")

    database_uri = str(config.get("SQLALCHEMY_DATABASE_URI") or "")
    try:
        database_url = make_url(database_uri)
        postgres = database_url.get_backend_name() == "postgresql"
        url_sslmode = str(database_url.query.get("sslmode", "")).lower()
    except Exception:
        postgres, url_sslmode = False, ""
    sslmode = url_sslmode or str(config.get("DATABASE_SSLMODE") or "require").lower()
    _result(results, "ok" if postgres else "error", "database",
            "DATABASE_URL usa PostgreSQL." if postgres else "Staging requiere una DATABASE_URL PostgreSQL explícita.")
    _result(results, "ok" if sslmode in POSTGRES_TLS_MODES else "error", "database_tls",
            "PostgreSQL exige TLS." if sslmode in POSTGRES_TLS_MODES else "DATABASE_SSLMODE debe exigir TLS.")

    public_url = config.get("PUBLIC_BASE_URL")
    _result(results, "ok" if _origin_is_https(public_url) else "error", "public_base_url",
            "PUBLIC_BASE_URL es un origen HTTPS canónico." if _origin_is_https(public_url)
            else "PUBLIC_BASE_URL debe ser un origen HTTPS exacto.")
    origins = _cors_origins(config.get("CORS_ORIGINS"))
    cors_ok = bool(origins) and all(_origin_is_https(item) for item in origins)
    _result(results, "ok" if cors_ok else "error", "cors",
            "CORS contiene únicamente orígenes HTTPS explícitos." if cors_ok
            else "CORS_ORIGINS requiere orígenes HTTPS explícitos y nunca '*'.")
    _result(results, "ok" if config.get("SESSION_COOKIE_SECURE") else "error", "session_cookie",
            "La cookie de sesión exige HTTPS." if config.get("SESSION_COOKIE_SECURE")
            else "SESSION_COOKIE_SECURE debe estar habilitado.")
    proxy_count = config.get("TRUSTED_PROXY_COUNT")
    _result(results, "ok" if isinstance(proxy_count, int) and 0 <= proxy_count <= 10 else "error",
            "trusted_proxy_count", "La cantidad de proxies confiables está acotada."
            if isinstance(proxy_count, int) and 0 <= proxy_count <= 10 else "TRUSTED_PROXY_COUNT debe estar entre 0 y 10.")

    mail_backend = str(config.get("RECOVERY_MAIL_BACKEND") or "").lower()
    if mail_backend == "smtp":
        smtp_ok = bool(config.get("SMTP_HOST") and config.get("SMTP_FROM")
                       and isinstance(config.get("SMTP_PORT"), int) and 0 < config.get("SMTP_PORT") < 65536)
        _result(results, "ok" if smtp_ok else "error", "smtp",
                "SMTP tiene host, puerto y remitente configurados; no se realizó ninguna conexión."
                if smtp_ok else "SMTP requiere host, puerto válido y remitente antes de staging.")
        username, password = config.get("SMTP_USERNAME"), config.get("SMTP_PASSWORD")
        auth_status = "ok" if username and password else ("warning" if not username and not password else "error")
        _result(results, auth_status, "smtp_auth",
                "Las credenciales SMTP están presentes y permanecen ocultas."
                if auth_status == "ok" else "SMTP no usa autenticación; confirme que el relay privado lo permite."
                if auth_status == "warning" else "SMTP_USERNAME y SMTP_PASSWORD deben configurarse juntos.")
        if not config.get("SMTP_USE_TLS"):
            _result(results, "warning", "smtp_tls", "SMTP_USE_TLS está deshabilitado; confirmar transporte privado o habilitar TLS.")
    else:
        _result(results, "error", "smtp", "Staging requiere RECOVERY_MAIL_BACKEND=smtp.")

    wallet_mode = str(config.get("GOOGLE_WALLET_MODE") or "disabled").lower()
    if wallet_mode == "disabled":
        _result(results, "ok", "google_wallet", "Google Wallet está deshabilitado y se considera integración opcional.")
    elif wallet_mode == "real":
        issuer_ok = bool(str(config.get("GOOGLE_ISSUER_ID") or "").isdigit())
        try:
            account = json.loads(config.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "")
            account_ok = account.get("type") == "service_account" and bool(account.get("client_email") and account.get("private_key"))
        except (TypeError, ValueError, AttributeError):
            account_ok = False
        _result(results, "ok" if issuer_ok and account_ok else "error", "google_wallet",
                "Google Wallet tiene estructura de credenciales completa; no se contactó Google."
                if issuer_ok and account_ok else "Google Wallet real requiere issuer y JSON de cuenta de servicio válidos.")
    else:
        _result(results, "error", "google_wallet", "GOOGLE_WALLET_MODE debe ser disabled o real en staging.")

    apple_values = [config.get(name) for name in (
        "APPLE_PASS_TYPE_ID", "APPLE_TEAM_ID", "APPLE_CERT_PATH", "APPLE_KEY_PATH", "APPLE_WWDR_PATH",
    )]
    if not any(apple_values):
        _result(results, "warning", "apple_wallet", "Apple Wallet no está configurado; permanece como integración opcional pendiente.")
    elif all(apple_values):
        identity_ok = not _placeholder(apple_values[0]) and not _placeholder(apple_values[1])
        web_url_ok = _origin_is_https(config.get("APPLE_WEB_SERVICE_URL"))
        missing_files = [path for path in apple_values[2:] if not Path(str(path)).is_file()]
        status = "error" if not identity_ok or not web_url_ok else ("warning" if missing_files else "ok")
        _result(results, status, "apple_wallet",
                "Apple Wallet contiene placeholders o una URL pública no canónica."
                if status == "error" else
                "Apple Wallet está configurado, pero faltan archivos montados en este proceso."
                if missing_files else "Apple Wallet tiene identificadores y archivos disponibles; no se contactó Apple.")
    else:
        _result(results, "error", "apple_wallet", "La configuración parcial de Apple Wallet debe completarse o retirarse.")

    _result(results, "ok" if config.get("UPLOAD_STORAGE_BACKEND") == "disabled" else "error", "uploads",
            "Uploads locales están deshabilitados." if config.get("UPLOAD_STORAGE_BACKEND") == "disabled"
            else "Staging no permite almacenamiento local efímero.")
    worker_values = {
        "registration_worker": (
            config.get("REGISTRATION_EMAIL_POLL_SECONDS"), config.get("REGISTRATION_EMAIL_BATCH_SIZE"),
            config.get("REGISTRATION_EMAIL_MAX_ATTEMPTS"), config.get("REGISTRATION_EMAIL_STALE_SECONDS"),
            config.get("REGISTRATION_EMAIL_RETENTION_SECONDS"),
        ),
        "google_wallet_worker": (
            config.get("GOOGLE_WALLET_SYNC_POLL_SECONDS"), config.get("GOOGLE_WALLET_SYNC_BATCH_SIZE"),
            config.get("GOOGLE_WALLET_SYNC_ALERT_PENDING"), config.get("GOOGLE_WALLET_SYNC_ALERT_STALE_SECONDS"),
        ),
    }
    for check, values in worker_values.items():
        valid = all(isinstance(value, int) and value > 0 for value in values)
        _result(results, "ok" if valid else "error", check,
                "Los parámetros del worker son positivos." if valid else "Los parámetros del worker deben ser enteros positivos.")
    auth_values = (config.get("JWT_ACCESS_TOKEN_EXPIRES"), config.get("JWT_REFRESH_TOKEN_EXPIRES"),
                   config.get("BROWSER_SESSION_ABSOLUTE_SECONDS"), config.get("REMEMBER_SESSION_IDLE_SECONDS"))
    auth_valid = all(isinstance(value, int) and value > 0 for value in auth_values)
    _result(results, "ok" if auth_valid else "error", "session_lifetimes",
            "Las duraciones de tokens y sesiones son positivas." if auth_valid
            else "Las duraciones de tokens y sesiones deben ser enteros positivos.")

    if check_database:
        try:
            db.session.execute(text("SELECT 1"))
            revisions = {row[0] for row in db.session.execute(text("SELECT version_num FROM alembic_version"))}
            _result(results, "ok", "database_connectivity", "La consulta de solo lectura respondió correctamente.")
            _result(results, "ok" if revisions == {EXPECTED_ALEMBIC_HEAD} else "error", "alembic_revision",
                    "La base está en la revisión Alembic esperada." if revisions == {EXPECTED_ALEMBIC_HEAD}
                    else "La revisión Alembic no coincide con el head esperado; no se aplicaron migraciones.")
        except SQLAlchemyError:
            db.session.rollback()
            _result(results, "error", "database_connectivity", "No fue posible completar la comprobación de solo lectura.")
    else:
        _result(results, "warning", "database_connectivity", "No se comprobó la base; use --check-database para una lectura explícita.")
    return results


def register_staging_preflight(app):
    @app.cli.command("staging-preflight")
    @click.option("--check-database", is_flag=True, help="Ejecuta SELECT 1 y consulta alembic_version sin escribir.")
    @click.option("--json-output", is_flag=True, help="Emite JSON sanitizado para automatización.")
    def staging_preflight(check_database, json_output):
        """Validate staging readiness without mutating data or contacting providers."""
        results = collect_preflight(current_app.config, check_database=check_database)
        if json_output:
            click.echo(json.dumps({"checks": results}, sort_keys=True))
        else:
            labels = {"ok": "CORRECTO", "warning": "ADVERTENCIA", "error": "ERROR"}
            for result in results:
                click.echo(f"[{labels[result['status']]}] {result['check']}: {result['message']}")
        if any(result["status"] == "error" for result in results):
            raise click.ClickException("Preflight bloqueado: corrija los errores antes de staging.")
