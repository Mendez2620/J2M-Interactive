"""Run the explicitly scoped J2M upgrade in an isolated certification fixture."""

import os
import sys

from alembic import command
from flask import current_app
from sqlalchemy import text

from scripts.historical_transition_evidence import FINAL_HEAD, INITIAL_HEAD

LOCK_KEY = -178321464298153
LOGIN = "j2m_migrator"
OWNER = "j2m_owner"
DATABASE = "full_schema"


def checked_environment(environment, config, url):
    return (
        environment.get("J2M_PROCESS_TYPE") == "migrator"
        and not any(name.upper().startswith("PG") for name in environment)
        and config.get("APPLE_WALLET_MODE") == config.get("GOOGLE_WALLET_MODE") == "disabled"
        and url.get_backend_name() == "postgresql"
        and url.host == "db"
        and url.database == DATABASE
        and url.username == LOGIN
        and url.port in (None, 5432)
        and dict(url.query) == {"sslmode": "verify-full", "sslrootcert": "/run/j2m/supabase-ca.pem"}
        and config.get("DATABASE_SSLMODE") == "verify-full"
    )


def _identity(connection):
    return tuple(connection.execute(text(
        "SELECT session_user, current_user, current_database(), pg_backend_pid()"
    )).one())


def _revision(connection):
    rows = connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
    if len(rows) != 1:
        raise RuntimeError("Unexpected Alembic revision set")
    return rows[0]


def rehearse_upgrade(engine, config, *, environment):
    if not checked_environment(environment, config, engine.url):
        raise ValueError("Unapproved migration parameters")
    # Never return a connection with a session-level role or lock to the pool.
    with engine.connect() as connection:
        try:
            login, current, database, pid = _identity(connection)
            if ((login, current, database) != (LOGIN, LOGIN, DATABASE)
                    or not connection.connection.driver_connection.info.ssl_in_use):
                raise RuntimeError("Unapproved migration login or transport")
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
                raise RuntimeError("Migration lock already held")
            connection.execute(text("SET ROLE j2m_owner"))
            connection.commit()
            if _identity(connection) != (LOGIN, OWNER, DATABASE, pid):
                raise RuntimeError("Migration identity changed")
            if _revision(connection) != INITIAL_HEAD:
                # Includes a confirmed target after an interrupted response.
                # A separate live inspection must resolve the outcome.
                raise RuntimeError("Source revision no longer eligible")
            connection.commit()
            cfg = current_app.extensions["migrate"].migrate.get_config()
            cfg.attributes["j2m_migration_connection"] = connection
            command.upgrade(cfg, FINAL_HEAD)
            if (_identity(connection) != (LOGIN, OWNER, DATABASE, pid)
                    or _revision(connection) != FINAL_HEAD):
                raise RuntimeError("Target revision or identity unconfirmed")
            if not connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}):
                raise RuntimeError("Migration lock release unconfirmed")
            connection.commit()
        finally:
            connection.invalidate()


def main():
    try:
        from run import app
        from app.extensions import db
        with app.app_context():
            rehearse_upgrade(db.engine, app.config, environment=os.environ)
    except Exception as exc:
        print(f"Synthetic migration blocked ({type(exc).__name__}); inspect the live head before retrying.",
              file=sys.stderr)
        return 1
    print("Synthetic migration verified with dedicated login, owner and session lock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
