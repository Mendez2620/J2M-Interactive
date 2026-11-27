"""Verify dump coverage against both migrated J2M schemas in the synthetic role harness."""
from difflib import unified_diff
from pathlib import Path
import re
import subprocess
import sys

from postgres_scoped_grants_fixture import prove_scoped_grants_and_fence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.selfhost_backup import verify_transition_accounting

SEED = """
from run import app
from app.extensions import db
from app.models import Business, Customer
from app.services.loyalty_transaction_service import process_loyalty_transaction
with app.app_context():
    for i in (1, 2):
        business = Business(name=f'Synthetic {i}', slug=f'synthetic-{i}', loyalty_type='stamps', cooldown_minutes=0)
        db.session.add(business)
        db.session.flush()
        customer = Customer(business_id=business.id, full_name='Synthetic')
        db.session.add(customer)
        db.session.flush()
        process_loyalty_transaction(business=business, customer_id=customer.id, staff_id=None,
                                    action="earn_stamp", tx_type="earn", points_amount=0,
                                    stamps_amount=1, purchase_amount=0, request_id=f"synthetic-{i}",
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


def exercise_application_schema(network, run, sql):
    sql("CREATE DATABASE full_schema OWNER j2m_owner").check_returncode()

    def app(*args):
        subprocess.run(["docker", "run", "--rm", "--network", network,
                        "--env-file", str(ROOT / ".env.wallet-demo.example"),
                        "-e", "DATABASE_URL=postgresql://j2m_migrator:synthetic-only@db/full_schema",
                        "-e", "PGOPTIONS=-c role=j2m_owner", "-e", "GOOGLE_WALLET_MODE=disabled",
                        "-e", "APPLE_WALLET_MODE=disabled", "j2m-interactive:wallet-demo-synthetic", *args],
                       check=True, timeout=90)

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

    for i, head in enumerate(("9d0e1f2a3b4c", "c3d4e5f6a7b8")):
        app("flask", "--app", "run.py", "db", "upgrade", head)
        if i == 0:
            app("python", "-c", SEED)
        sql(BACKUP_GRANTS, database="full_schema").check_returncode()
        assert sql("SELECT version_num FROM alembic_version",
                   database="full_schema").stdout.strip() == head.encode()
        assert sql("SELECT count(*) FROM customers", database="full_schema").stdout.strip() == b"2"
        assert sql("UPDATE public.customers SET current_stamps=1", "j2m_backup", database="full_schema").returncode
        assert sql("SET ROLE j2m_owner", "j2m_backup", database="full_schema").returncode
        dump = run("env", "PGPASSWORD=synthetic-only", "pg_dump", "-h", "127.0.0.1",
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
            prove_scoped_grants_and_fence(run, sql, destination)
            query("SELECT setval('public.customers_id_seq', 1, true)")
            try:
                verify_transition_accounting(query)
            except RuntimeError as error:
                if "Secuencia" not in str(error):
                    raise
            else:
                raise RuntimeError("A regressed customer sequence was accepted")
            query("SELECT setval('public.customers_id_seq', 2, true)")
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
