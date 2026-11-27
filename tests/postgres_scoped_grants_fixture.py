"""Draft fixture for scoped J2M service grants; exercised only against the restored synthetic schema."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
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
                   "google_wallet_sync_jobs": "SELECT,UPDATE"},
        "sequences": (),
    },
}
MIGRATOR_LOCK = (-1783214642, 9815)


def prove_scoped_grants_and_fence(sql, database, service_flow, connection):
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
    execute("GRANT UPDATE (referrals_enabled) ON public.businesses TO j2m_schema_web").check_returncode()
    execute((Path(__file__).resolve().parents[1] / "scripts/j2m_wallet_queue_grants.sql").read_text()).check_returncode()
    execute("INSERT INTO referral_participants (business_id, customer_id, participant_type, "
            "invitation_code, is_enabled, created_at, updated_at) VALUES "
            "(1,1,'customer_ambassador','synthetic-one',false,now(),now()),"
            "(2,2,'customer_ambassador','synthetic-two',false,now(),now())").check_returncode()
    accounting = "SELECT current_stamps FROM customers ORDER BY id; SELECT count(*) FROM transactions; " \
                 "SELECT count(*) FROM loyalty_ledger_entries"
    before = execute(accounting).stdout
    service_flow(web)
    service_flow(worker)
    assert execute(accounting).stdout == before
    assert execute("UPDATE customers SET current_stamps=0", worker).returncode
    assert execute("UPDATE google_wallet_referral_jobs SET status='failed'", web).returncode
    assert execute("UPDATE google_wallet_referral_jobs SET business_id=2", worker).returncode
    assert execute("INSERT INTO google_wallet_referral_jobs (business_id,customer_id,run_at,created_at,updated_at) "
                   "VALUES (2,1,now(),now(),now())", worker).returncode

    owner = "SET ROLE j2m_owner"
    if execute(f"{owner}; SELECT current_user", "j2m_migrator").stdout.splitlines()[-1] != b"j2m_owner":
        raise RuntimeError("Migrator cannot reach the dedicated owner")
    # Hold the migrator lock on a dedicated connection across commits.
    a, b = MIGRATOR_LOCK
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock = executor.submit(execute, f"BEGIN; SELECT pg_advisory_lock({a},{b}); "
                                        f"COMMIT; BEGIN; SELECT pg_sleep(3); COMMIT; SELECT pg_advisory_unlock({a},{b})", "j2m_migrator")
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
REVOKE UPDATE (referrals_enabled) ON public.businesses FROM {web};
REVOKE UPDATE (status, token, locked_at, attempts, run_at, cursor, outcome, updated_at)
    ON public.google_wallet_referral_jobs FROM {worker};
REVOKE UPDATE (state, active_slot, updated_at, started_at, remote_resolved_at, remote_status)
    ON public.google_wallet_write_reservations FROM {roles};
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles};
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles};
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {roles};
REVOKE USAGE ON SCHEMA public FROM {roles};
"""
    peer = connection(worker, database)
    try:
        peer.stdin.write(b"SELECT current_user;\n")
        peer.stdin.flush()
        assert peer.stdout.readline().strip() == worker.encode()
        execute(script).check_returncode()
        _, errors = peer.communicate(
            b"INSERT INTO google_wallet_referral_jobs DEFAULT VALUES;\n"
            b"UPDATE google_wallet_referral_jobs SET status='pending';\n"
            b"DELETE FROM google_wallet_referral_jobs;\n"
            b"TRUNCATE google_wallet_referral_jobs;\n"
            b"SELECT nextval('google_wallet_referral_jobs_id_seq');\n"
            b"SET ROLE j2m_owner;\n", timeout=8)
        assert errors.count(b"permission denied") == 6
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=8)
    for role in OBJECTS:
        if execute("SELECT 1", role).returncode == 0:
            raise RuntimeError("Fenced login reconnected")
    print("Scoped grants and revoked reconnect on the real J2M fixture verified; referral queue and reservation operations verified.")
