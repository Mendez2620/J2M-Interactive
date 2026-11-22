"""Docker/PostgreSQL integration probe for the authenticated DB+media restore path."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql


def _database_url(source_url: str, database: str) -> str:
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("TEST_POSTGRES_URL no es una conexión PostgreSQL sintética válida.")
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + database,
                       parsed.query, parsed.fragment))


def main() -> int:
    repository = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repository))
    source_url = os.environ["TEST_POSTGRES_URL"]
    database_name = "j2m_integral_" + secrets.token_hex(8)
    admin_url = _database_url(source_url, "postgres")
    isolated_url = _database_url(source_url, database_name)
    db = None

    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

        # Prove this is a fresh database, not the service database previously
        # exercised by the unit suite, then migrate it independently.
        with psycopg2.connect(isolated_url) as isolated:
            with isolated.cursor() as cursor:
                cursor.execute("SELECT current_database(), to_regclass('public.businesses')")
                current_database, businesses_table = cursor.fetchone()
                if current_database != database_name or businesses_table is not None:
                    raise RuntimeError("La base del ensayo integral no está aislada o vacía.")
        migration_env = os.environ.copy()
        migration_env.update(DATABASE_URL=isolated_url, DATABASE_SSLMODE="disable")
        subprocess.run([sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
                       cwd=repository, env=migration_env, check=True,
                       stdout=subprocess.DEVNULL)

        os.environ.update(DATABASE_URL=isolated_url, DATABASE_SSLMODE="disable",
                          J2M_SYNTHETIC_BACKUP_TEST="1")
        from app import create_app
        from app.extensions import db as app_db
        from app.models import Business
        from scripts.selfhost_backup import integral_backup, restore_integral_isolated

        db = app_db
        app = create_app("development")
        with tempfile.TemporaryDirectory() as temporary, app.app_context():
            root = Path(temporary)
            media = root / "media"
            output = root / "backups"
            business = Business(name="Integral Restore CI", slug="integral-restore-ci")
            db.session.add(business)
            db.session.commit()
            current_key = f"businesses/{business.id}/logo/" + "a" * 32 + ".png"
            old_key = f"businesses/{business.id}/logo/" + "b" * 32 + ".png"
            for key, content in ((current_key, b"current"), (old_key, b"historical")):
                path = media / Path(*key.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            marker = media / Path(*old_key.split("/"))
            marker.with_name(f".{marker.name}.retained.json").write_text(json.dumps({
                "schema": 1, "storage_key": old_key, "business_id": business.id,
                "superseded_by_design_version": 2,
                "retired_at": "2026-11-21T00:00:00+00:00",
            }), encoding="utf-8")
            business.logo_path = current_key
            db.session.commit()

            source_port = urlsplit(isolated_url).port or 5432
            env_file = root / "synthetic.env"
            env_file.write_text(
                "DATABASE_URL=postgresql://postgres:postgres@j2m-backup-source:"
                f"{source_port}/{database_name}?sslmode=disable\n", encoding="utf-8",
            )
            key_file = root / "synthetic.dpapi"
            synthetic_key = bytes(range(32))
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=synthetic_key):
                index = integral_backup(env_file, media, output, key_file, retries=1,
                                        allow_insecure_synthetic=True, timeout=180)
                report = restore_integral_isolated(
                    index, key_file, "j2m-interactive:" + "0" * 39 + "1", timeout=180,
                )
            if report.businesses != 1:
                raise RuntimeError("La restauración integral no recuperó el tenant sintético exacto.")
            db.session.remove()
            db.engine.dispose()
        return 0
    finally:
        if db is not None:
            try:
                db.session.remove()
            except Exception:
                pass
        with admin.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           "WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(
                sql.Identifier(database_name)))
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
