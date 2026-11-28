"""Verify dump coverage against both migrated J2M schemas in the synthetic role harness."""
from difflib import unified_diff
from pathlib import Path
import os
import re
import subprocess
import sys
import json

from postgres_scoped_grants_fixture import prove_scoped_grants_and_fence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.selfhost_backup import verify_transition_accounting
from scripts.check_transition_relation_manifest import RELATION_QUERY, verify_relation_manifest

SEED = """
from run import app
from app.extensions import db
from app.models import Business, Customer, Staff, PlatformAdmin
from app.services.loyalty_transaction_service import process_loyalty_transaction
with app.app_context():
    admin = PlatformAdmin(name="Synthetic platform admin", email="platform@example.test", is_active=True)
    admin.set_password("SyntheticPlatform123")
    db.session.add(admin)
    for i in (1, 2):
        business = Business(name=f'Synthetic {i}', slug=f'synthetic-{i}', loyalty_type='points' if i == 1 else 'stamps',
                            points_per_currency=1, stamps_reward_limit=2, cooldown_minutes=0,
                            web_access_enabled=True)
        db.session.add(business)
        db.session.flush()
        manager = Staff(business_id=business.id, username=f'manager{i}', email=f'manager{i}@example.test',
                        role='manager', web_access_enabled=True)
        manager.set_password("SyntheticPass123")
        cashier = Staff(business_id=business.id, username=f'cashier{i}', role='cashier',
                        web_access_enabled=True, permissions="scanner_operate_web,authorize_customer_identity")
        cashier.set_password("SyntheticPass123")
        db.session.add_all([manager, cashier])
        customer = Customer(business_id=business.id, full_name='Synthetic')
        db.session.add(customer)
        db.session.flush()
        process_loyalty_transaction(business=business, customer_id=customer.id, staff_id=None,
                                    action="earn_points" if i == 1 else "earn_stamp", tx_type="earn",
                                    points_amount=1 if i == 1 else 0, stamps_amount=0 if i == 1 else 1, purchase_amount=0, request_id=f"synthetic-{i}",
                                    fingerprint=f"synthetic-{i}")
    db.session.commit()
"""

BACKUP_GRANTS = """
DO $$
DECLARE item record;
BEGIN
  GRANT USAGE ON SCHEMA public TO j2m_backup;
  FOR item IN SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('GRANT SELECT ON public.%I TO j2m_backup', item.tablename);
    IF item.rowsecurity AND NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=item.tablename
          AND policyname='synthetic_backup_coverage') THEN
      EXECUTE format('CREATE POLICY synthetic_backup_coverage ON public.%I '
                     'FOR SELECT TO j2m_backup USING (true)', item.tablename);
    END IF;
  END LOOP;
  GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO j2m_backup;
END $$;
"""

DATA_QUERIES = """
SELECT format('SELECT %L,coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text),''[]''::jsonb) FROM public.%I x;',
              tablename,tablename) FROM pg_tables WHERE schemaname='public' ORDER BY tablename;
SELECT format('SELECT %L,last_value,is_called FROM public.%I;',sequencename,sequencename)
FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename;
"""


def exercise_application_schema(network, run, sql, connection, pool_control, ca):
    sql("CREATE DATABASE full_schema OWNER j2m_owner").check_returncode()

    root = ca.parent
    media = root / "media"
    media.mkdir()
    layer = root / "synthetic-compose.json"
    layer.write_text(json.dumps({
        "networks": {"j2m-private": {"external": True, "name": network, "driver": None}},
        "services": {"web": {"volumes": [{"type": "bind", "source": str(media),
                                           "target": "/app/instance/media", "read_only": True}]}},
    }), encoding="utf-8")
    env = {
        **os.environ,
        "J2M_IMAGE": os.environ.get("J2M_ROLE_FIXTURE_IMAGE", "j2m-interactive:wallet-demo-synthetic"),
        "J2M_ENV_FILE": str(ROOT / ".env.wallet-demo.example"),
        "J2M_CLOUDFLARED_ENV_FILE": str(ROOT / ".env.cloudflared.example"),
        "J2M_DATABASE_CA_FILE": str(ca),
    }
    project = network.removesuffix("-internal")
    services = {"j2m_migrator": ("migrator", "MIGRATOR"),
                "j2m_schema_web": ("web", "WEB"),
                "j2m_schema_worker": ("google-wallet-worker", "WORKER")}
    base_env = (ROOT / ".env.wallet-demo.example").read_text(encoding="utf-8").splitlines()
    base_env = [line for line in base_env if not line.startswith(
        ("DATABASE_URL=", "DATABASE_SSLMODE=", "GOOGLE_WALLET_MODE=", "APPLE_WALLET_MODE="))]
    for _, suffix in services.values():
        env[f"J2M_{suffix}_ENV_FILE"] = str(root / f"{suffix}.env")

    def app(*args, role="j2m_migrator", database="full_schema", ca_override=None):
        service, _ = services[role]
        for login, (_, suffix) in services.items():
            path = root / f"{suffix}.env"
            path.write_text("\n".join([
                *base_env, "GOOGLE_WALLET_MODE=disabled", "APPLE_WALLET_MODE=disabled",
                "DATABASE_SSLMODE=verify-full",
                f"DATABASE_URL=postgresql://{login}:synthetic-{login}@db/{database}"
                "?sslmode=verify-full&sslrootcert=/run/j2m/supabase-ca.pem",
                "",
            ]), encoding="utf-8")
        # Bootstrap only the disposable source from an empty catalog.
        # The transition itself uses the standalone guarded migrator.
        dedicated = args[:3] == ("python", "-m", "scripts.run_certified_migration")
        options = (["-e", "PGOPTIONS=-c role=j2m_owner"]
                   if role == "j2m_migrator" and not dedicated else [])
        compose = ["docker", "compose", "-p", project, "-f", "compose.selfhost.yml",
                   "-f", "compose.selfhost.identities.yml", "-f", str(layer), "--profile", "migrate"]
        process_env = {**env, "J2M_DATABASE_CA_FILE": str(ca_override or ca)}
        return subprocess.run([*compose, "run", "--rm", "--no-deps", "-T", *options, service, *args],
                              cwd=ROOT, env=process_env, check=ca_override is None,
                              capture_output=ca_override is not None, timeout=90)

    def snapshot(database):
        commands = sql(DATA_QUERIES, database=database)
        commands.check_returncode()
        rows = run("psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database,
                   input=commands.stdout, check=True).stdout
        schema = run("pg_dump", "-U", "postgres", "-d", database, "--schema-only",
                     "--no-owner", "--no-acl", "-n", "public", check=True).stdout
        # PostgreSQL deparses a text[] cast differently after a dump/parse cycle.
        lines = []
        for line in schema.splitlines():
            if line.startswith((b"\\restrict ", b"\\unrestrict ")):
                continue
            line = re.sub(rb"\(\(ARRAY\[([^\]]*)\]\)::text\[\]\)",
                          lambda match: b"(ARRAY[" + match[1].replace(b"::character varying", b"::text") + b"])",
                          line)
            line = re.sub(rb"\(('(?:[^']|'')*')::character varying\)::text", rb"\1::text", line)
            lines.append(line)
        return rows, lines

    def check_tls(role, database):
        expected_role = "j2m_owner" if role == "j2m_migrator" else role
        probe = (
            "from run import app; from app.extensions import db; from sqlalchemy import text; "
            "ctx=app.app_context(); ctx.push(); conn=db.session.connection(); "
            "assert conn.connection.driver_connection.info.ssl_in_use; "
            'observed=tuple(conn.execute(text("SELECT current_user,session_user,current_database()")).one()); '
            f"assert observed == {(expected_role, role, database)!r}"
        )
        app("python", "-c", probe, role=role, database=database)

    for i, head in enumerate(("9d0e1f2a3b4c", "c3d4e5f6a7b8")):
        if i == 0:
            app("flask", "--app", "run.py", "db", "upgrade", head)
            check_tls("j2m_migrator", "full_schema")
            from nginx_recovery_ci import write_certificates

            wrong = root / "wrong-ca"
            wrong.mkdir()
            wrong_ca, _, _ = write_certificates(wrong, hostnames=("db",), authority_name="Untrusted synthetic CI CA")
            denied = app("python", "-c", "from run import app; from app.extensions import db; "
                         "from sqlalchemy import text; app.app_context().push(); "
                         "db.session.execute(text('SELECT 1'))", ca_override=wrong_ca)
            if not denied.returncode or b"certificate verify failed" not in denied.stderr.lower():
                raise RuntimeError("Connection accepted an unrelated CA or failed for the wrong reason")
            app("python", "-c", SEED)
        else:
            app("python", "-m", "scripts.run_certified_migration")
        sql(BACKUP_GRANTS, database="full_schema").check_returncode()
        assert sql("SELECT version_num FROM alembic_version",
                   database="full_schema").stdout.strip() == head.encode()
        observed_relations = sql(RELATION_QUERY, database="full_schema")
        observed_relations.check_returncode()
        verify_relation_manifest(head, json.loads(observed_relations.stdout))
        assert sql("SELECT count(*) FROM customers", database="full_schema").stdout.strip() == b"2"
        assert sql("UPDATE public.customers SET current_stamps=1", "j2m_backup", database="full_schema").returncode
        assert sql("SET ROLE j2m_owner", "j2m_backup", database="full_schema").returncode
        backup_tls = ("env", "PGPASSWORD=synthetic-only", "PGSSLMODE=verify-full",
                      "PGSSLROOTCERT=/tmp/ca.crt")
        backup_identity = run(*backup_tls, "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                              "-h", "db", "-U", "j2m_backup", "-d", "full_schema", "-c",
                              "SELECT current_user,session_user,ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
                              check=True)
        assert backup_identity.stdout.strip() == b"j2m_backup|j2m_backup|t"
        dump = run(*backup_tls, "pg_dump", "-h", "db",
                   "-U", "j2m_backup", "--enable-row-security", "--no-owner", "--no-acl",
                   "-Fc", "-n", "public", "full_schema", check=True).stdout
        destination = f"full_restore_{i}"
        sql(f"CREATE DATABASE {destination}").check_returncode()
        sql("DROP SCHEMA public", database=destination).check_returncode()
        restore = run("pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "-U", "postgres",
                      "-d", destination, input=dump)
        if restore.returncode:
            raise RuntimeError("Synthetic schema restore failed: " + restore.stderr.decode()[:600])
        origin, restored = snapshot("full_schema"), snapshot(destination)
        if origin[0] != restored[0] or origin[1] != restored[1]:
            diff = list(unified_diff([line.decode() for line in origin[1]], [line.decode() for line in restored[1]], n=0))
            raise RuntimeError(f"Restored J2M fixture differs (data matched={origin[0] == restored[0]}): " + "\n".join(diff[:20]))
        def query(statement):
            result = sql(statement, database=destination)
            result.check_returncode()
            return result.stdout.decode().strip()

        verify_transition_accounting(query)
        if i == 1:
            sql(BACKUP_GRANTS, database=destination).check_returncode()
            flow = (ROOT / "tests/postgres_referral_worker_flow.py").read_text()
            checked = set()

            def run_service(role, source=flow):
                if role not in checked:
                    check_tls(role, destination)
                    checked.add(role)
                app("python", "-c", source, role=role, database=destination)

            prove_scoped_grants_and_fence(
                sql, destination, run_service,
                connection, pool_control,
            )
            verify_transition_accounting(query)
            # The lazy baseline is valid for an unused zero-balance account,
            # but must never certify an unexplained positive or negative balance.
            customer_id = query("""
                INSERT INTO customers (business_id,full_name,marketing_consent,
                    current_points,current_stamps,qr_code_token,created_at)
                VALUES (1,'Synthetic unused',false,0,0,'synthetic-unused',now())
                RETURNING id
            """).splitlines()[0]
            assert customer_id.isdigit()
            verify_transition_accounting(query)
            for points, stamps in ((1, 0), (0, 1), (-1, 0)):
                query(f"UPDATE customers SET current_points={points}, current_stamps={stamps} WHERE id={customer_id}")
                try:
                    verify_transition_accounting(query)
                except RuntimeError as error:
                    if "cadena contable" not in str(error):
                        raise
                else:
                    raise RuntimeError("An unexplained customer balance was accepted")
            query(f"DELETE FROM customers WHERE id={customer_id}")
            verify_transition_accounting(query)
            query("SELECT setval('public.customers_id_seq', 1, true)")
            try:
                verify_transition_accounting(query)
            except RuntimeError as error:
                if "Secuencia" not in str(error):
                    raise
            else:
                raise RuntimeError("A regressed customer sequence was accepted")
            query("SELECT setval('public.customers_id_seq', 2, true)")
            customer_flow = (ROOT / "tests/postgres_customer_http_flow.py").read_text()
            app("python", "-c", customer_flow, role="j2m_schema_web", database=destination)
            verify_transition_accounting(query)
            query("INSERT INTO transactions (business_id,customer_id,type,points_amount,"
                  "stamps_amount,purchase_amount,timestamp) VALUES (1,1,'earn',0,1,0,now())")
            try:
                verify_transition_accounting(query)
            except RuntimeError as error:
                if "cadena contable" not in str(error):
                    raise
            else:
                raise RuntimeError("A movement without a ledger entry was accepted")

        print(f"Full J2M {head}: public schema, data and sequences matched after restore.")
