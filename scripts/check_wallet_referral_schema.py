"""Read-only PostgreSQL contract check for the Wallet referral queue."""
import os
import re

from sqlalchemy import create_engine, inspect, text

from app.config import normalize_database_url
from app.models import GoogleWalletReferralJob

EXPECTED_HEAD = "c3d4e5f6a7b8"
STATUSES = {"pending", "processing", "completed", "missing", "failed"}


def _shape(kind, nullable):
    return kind._type_affinity, getattr(kind, "length", None), getattr(kind, "timezone", None), nullable


def validate_schema(connection):
    schema = "public"
    table = GoogleWalletReferralJob.__table__
    inspector = inspect(connection)
    checks = {
        "revision": connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
        == [EXPECTED_HEAD],
        "columns": {c["name"]: _shape(c["type"], c["nullable"])
                    for c in inspector.get_columns(table.name, schema=schema)}
        == {c.name: _shape(c.type, c.nullable) for c in table.columns},
        "primary key": inspector.get_pk_constraint(table.name, schema=schema)["constrained_columns"] == ["id"],
    }
    keys = {(tuple(k["constrained_columns"]), k["referred_schema"], k["referred_table"],
             tuple(k["referred_columns"]), k["options"].get("ondelete"))
            for k in inspector.get_foreign_keys(table.name, schema=schema, postgresql_ignore_search_path=True)}
    checks["foreign keys"] = {
        (("business_id", "customer_id"), schema, "customers", ("business_id", "id"), "CASCADE"),
        (("business_id",), schema, "businesses", ("id",), "CASCADE"),
    } <= keys
    checks["indexes"] = {("status", "run_at", "id"), ("business_id", "status")} <= {
        tuple(i["column_names"]) for i in inspector.get_indexes(table.name, schema=schema)
        if not i["dialect_options"].get("postgresql_where")
    }
    status_check = next((c["sqltext"] for c in inspector.get_check_constraints(table.name, schema=schema)
                         if c["name"] == "ck_wallet_referral_status"), "")
    normalized = re.sub(r"::(?:character varying|text)(?:\[\])?|[()\s]", "", status_check)
    checks["status constraint"] = (
        normalized.startswith("status=ANYARRAY[") and normalized.endswith("]")
        and set(re.findall(r"'([^']+)'", normalized)) == STATUSES
        and re.fullmatch(r"status=ANYARRAY\['[a-z]+'(?:,'[a-z]+')*\]", normalized) is not None
    )
    checks["row-level security"] = connection.scalar(text(
        "SELECT relrowsecurity FROM pg_class WHERE oid='public.google_wallet_referral_jobs'::regclass"
    )) is True
    checks["public privileges"] = not connection.scalar(text("""
        SELECT EXISTS (
            SELECT FROM pg_class c, LATERAL aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
            WHERE c.oid='public.google_wallet_referral_jobs'::regclass AND a.grantee=0
            UNION ALL
            SELECT FROM pg_roles r
            WHERE r.rolname IN ('anon', 'authenticated')
              AND (has_table_privilege(r.oid,'public.google_wallet_referral_jobs',
                 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                 OR has_any_column_privilege(r.oid,'public.google_wallet_referral_jobs',
                 'SELECT,INSERT,UPDATE,REFERENCES'))
        )
    """))
    return [name for name, passed in checks.items() if not passed]


def main():
    engine = None
    try:
        engine = create_engine(normalize_database_url(os.environ["DATABASE_URL"]),
                               connect_args={"connect_timeout": 5, "sslmode": "verify-full"})
        with engine.begin() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '5s'"))
            issues = validate_schema(connection)
    except Exception:
        print("Wallet referral schema could not be verified. Connection details suppressed.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    if issues:
        print("Wallet referral schema incompatible: " + ", ".join(issues))
        return 1
    print("Wallet referral schema verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
