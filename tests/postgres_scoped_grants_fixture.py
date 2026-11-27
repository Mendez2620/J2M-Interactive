"""Draft fixture for scoped J2M service grants; exercised only against the restored synthetic schema."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

# Deliberately explicit: the fixture does not turn newly migrated objects into grants.
OBJECTS = {
    "j2m_schema_web": {
        "tables": {"businesses": "SELECT", "customers": "SELECT,UPDATE",
                   "transactions": "SELECT,INSERT", "loyalty_ledger_entries": "SELECT,INSERT"},
        "sequences": ("transactions_id_seq", "loyalty_ledger_entries_id_seq"),
    },
    "j2m_schema_worker": {
        "tables": {"businesses": "SELECT", "customers": "SELECT",
                   "google_wallet_sync_jobs": "SELECT,UPDATE",
                   "google_wallet_referral_jobs": "SELECT,UPDATE",
                   "google_wallet_write_reservations": "SELECT,INSERT,UPDATE"},
        "sequences": (),
    },
}
MIGRATOR_LOCK = (-1783214642, 9815)


def prove_scoped_grants_and_fence(run, sql, database):
    def execute(statement, role="postgres"):
        return sql(statement, role, database=database)

    for role, grants in OBJECTS.items():
        execute(f"CREATE ROLE {role} LOGIN PASSWORD 'synthetic-only'").check_returncode()
        execute(f"GRANT CONNECT ON DATABASE {database} TO {role}; "
                f"GRANT USAGE ON SCHEMA public TO {role}").check_returncode()
        for table, privileges in grants["tables"].items():
            execute(f"GRANT {privileges} ON public.{table} TO {role}").check_returncode()
        for sequence in grants["sequences"]:
            execute(f"GRANT USAGE ON SEQUENCE public.{sequence} TO {role}").check_returncode()
        # Service accounts must not inherit the owner, a bypass role or public writes.
        properties = execute(f"""
            SELECT json_build_array(rolsuper,rolbypassrls,rolcreaterole,
                pg_has_role(rolname,'j2m_owner','MEMBER'))
            FROM pg_roles WHERE rolname='{role}'
        """).stdout
        if json.loads(properties) != [False, False, False, False]:
            raise RuntimeError("Unexpected synthetic service role privilege")
        if not execute("SET ROLE j2m_owner", role).returncode:
            raise RuntimeError("Synthetic service assumed owner")

    web, worker = OBJECTS
    execute("UPDATE customers SET current_stamps=current_stamps WHERE business_id=1", web).check_returncode()
    if not execute("UPDATE customers SET current_stamps=0", worker).returncode:
        raise RuntimeError("Worker has a balance write privilege")
    execute("INSERT INTO google_wallet_referral_jobs "
            "(business_id,status,run_at,created_at,updated_at) VALUES (1,'pending',now(),now(),now())"
            ).check_returncode()
    # Existing migrations grant no worker RLS policy. A GRANT alone cannot make the queue functional.
    seen = execute("SELECT count(*) FROM google_wallet_referral_jobs", worker)
    if seen.returncode or seen.stdout.strip() != b"0":
        raise RuntimeError("Restricted worker did not fail closed on the protected queue")

    owner = "SET ROLE j2m_owner"
    if execute(f"{owner}; SELECT current_user", "j2m_migrator").stdout.splitlines()[-1] != b"j2m_owner":
        raise RuntimeError("Migrator cannot reach the dedicated owner")
    # Refuse a second concurrent migrator using a transaction advisory lock.
    a, b = MIGRATOR_LOCK
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock = executor.submit(execute, f"BEGIN; SELECT pg_advisory_xact_lock({a},{b}); "
                                        "SELECT pg_sleep(3); COMMIT", "j2m_migrator")
        for _ in range(20):
            if execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                       f"AND classid={a % 2**32} AND objid={b} AND granted").stdout.strip() == b"1":
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Synthetic migrator lock was not observed")
        contender = execute(f"SELECT pg_try_advisory_xact_lock({a},{b})", "j2m_migrator")
        if contender.returncode or contender.stdout.strip() != b"f":
            raise RuntimeError("Concurrent migrator was not excluded")
        lock.result(timeout=12).check_returncode()

    # Direct grants in this fixture are intentionally revoked as a unit.
    roles = ",".join(OBJECTS)
    script = f"""
ALTER ROLE {web} NOLOGIN;
ALTER ROLE {worker} NOLOGIN;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles};
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles};
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {roles};
REVOKE USAGE ON SCHEMA public FROM {roles};
"""
    execute(script).check_returncode()
    for role in OBJECTS:
        if execute("SELECT 1", role).returncode == 0:
            raise RuntimeError("Fenced login reconnected")
    print("Scoped grants and revoked reconnect on the real J2M fixture verified; worker RLS policies still required.")
