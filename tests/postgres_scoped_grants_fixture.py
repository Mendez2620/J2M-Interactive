"""Scoped J2M service-grant rehearsal against disposable PostgreSQL schemas."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

# Deliberately explicit: the fixture does not turn newly migrated objects into grants.
OBJECTS = {
    "j2m_schema_web": {
        "tables": {"alembic_version": "SELECT", "businesses": "SELECT", "customers": "SELECT",
                   "transactions": "SELECT,INSERT", "loyalty_ledger_entries": "SELECT,INSERT"},
        "sequences": ("transactions_id_seq", "loyalty_ledger_entries_id_seq"),
    },
    "j2m_schema_worker": {
        "tables": {"businesses": "SELECT", "customers": "SELECT",
                   "google_wallet_sync_jobs": "SELECT,UPDATE"},
        "sequences": (),
    },
}
CUSTOMER_TABLES = (
    "customer_linking_enrollments", "customer_credentials", "customer_sessions",
    "customer_auth_challenges", "customer_auth_audits", "customer_registration_identity_claims",
    "customer_recovery_rate_limits", "customers", "referral_participants",
)
CUSTOMER_UPDATES = {
    "businesses": ("name",),  # SELECT FOR UPDATE during registration.
    "customer_recovery_rate_limits": ("reserved_until", "updated_at"),
    "customer_linking_enrollments": (
        "status", "browser_binding_hash", "pairing_code_hash", "claimed_at",
        "approved_at", "pairing_failed_attempts", "pairing_locked_at",
        "webauthn_challenge", "consumed_at", "secret_hash",
    ),
    "customer_credentials": ("sign_count", "last_used_at", "is_active", "revoked_at", "revoked_reason"),
    "customer_sessions": ("revoked_at", "last_active_at"),
    "customer_auth_challenges": ("consumed_at",),
}
CUSTOMER_SEQUENCES = ("customers_id_seq", "referral_participants_id_seq",
                      "customer_credentials_id_seq", "customer_auth_audits_id_seq")
MIGRATOR_LOCK = (-1783214642, 9815)


def prove_scoped_grants_and_fence(sql, database, service_flow, connection, pool_control, *, nominal=False):
    if database not in {"full_schema", "full_restore_1"}:
        raise ValueError("Unexpected synthetic writer database")
    def execute(statement, role="postgres"):
        return sql(statement, role, database=database)

    # Authentication takes a row lock (SELECT FOR UPDATE); PostgreSQL requires
    # UPDATE on at least one column. Restrict this to the non-security name
    # column rather than granting changes to credentials or account status.
    def admin_grants():
        execute("""
            GRANT SELECT ON public.platform_admins, public.business_categories,
                public.google_wallet_sync_jobs TO j2m_schema_web;
            GRANT UPDATE (name) ON public.platform_admins TO j2m_schema_web;
            GRANT SELECT, INSERT ON public.platform_admin_sessions TO j2m_schema_web;
            GRANT UPDATE (revoked_at) ON public.platform_admin_sessions TO j2m_schema_web;
        """).check_returncode()

    def customer_grants():
        for table in CUSTOMER_TABLES:
            execute(f"GRANT SELECT, INSERT ON public.{table} TO j2m_schema_web").check_returncode()
        for table, columns in CUSTOMER_UPDATES.items():
            execute(f"GRANT UPDATE ({','.join(columns)}) ON public.{table} TO j2m_schema_web").check_returncode()
        for sequence in CUSTOMER_SEQUENCES:
            execute(f"GRANT USAGE ON SEQUENCE public.{sequence} TO j2m_schema_web").check_returncode()

    def customer_policies():
        for table in CUSTOMER_TABLES:
            protected = execute(f"SELECT relrowsecurity FROM pg_class WHERE oid='public.{table}'::regclass")
            protected.check_returncode()
            if protected.stdout.strip() != b"t":
                continue
            execute(f"CREATE POLICY fixture_customer_read ON public.{table} "
                    "FOR SELECT TO j2m_schema_web USING (true)").check_returncode()
            predicate = ("participant_type = 'customer_ambassador' AND NOT is_enabled"
                         if table == "referral_participants" else "true")
            execute(f"CREATE POLICY fixture_customer_insert ON public.{table} "
                    f"FOR INSERT TO j2m_schema_web WITH CHECK ({predicate})").check_returncode()
            if table in CUSTOMER_UPDATES:
                execute(f"CREATE POLICY fixture_customer_update ON public.{table} "
                        "FOR UPDATE TO j2m_schema_web USING (true) WITH CHECK (true)").check_returncode()

    def fixture_grants():
        statements = []
        for role, grants in OBJECTS.items():
            statements.extend(f"GRANT {privileges} ON public.{table} TO {role}"
                              for table, privileges in grants["tables"].items())
            statements.extend(f"GRANT USAGE ON SEQUENCE public.{sequence} TO {role}"
                              for sequence in grants["sequences"])
        return "; ".join(statements)

    for role in OBJECTS:
        if execute(f"SELECT count(*) FROM pg_roles WHERE rolname='{role}'").stdout.strip() == b"0":
            execute(f"CREATE ROLE {role} LOGIN PASSWORD 'synthetic-{role}'").check_returncode()
        execute(f"GRANT CONNECT ON DATABASE {database} TO {role}; "
                f"GRANT USAGE ON SCHEMA public TO {role}").check_returncode()
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

    execute(fixture_grants()).check_returncode()
    web, worker = OBJECTS
    execute("GRANT UPDATE (current_points, current_stamps, last_earn_at) ON public.customers TO j2m_schema_web").check_returncode()
    execute("GRANT UPDATE (referrals_enabled) ON public.businesses TO j2m_schema_web").check_returncode()
    manifest = (Path(__file__).resolve().parents[1] / "scripts/j2m_wallet_queue_grants.sql").read_text()
    execute(manifest).check_returncode()
    execute(manifest).check_returncode()
    execute("ALTER POLICY j2m_referral_queue_read ON public.google_wallet_referral_jobs "
            "USING (false)").check_returncode()
    rejected = execute(manifest)
    assert rejected.returncode and b"Unexpected definition of J2M policy" in rejected.stderr
    assert execute("SELECT qual FROM pg_policies WHERE policyname='j2m_referral_queue_read'").stdout.strip() == b"false"
    execute("ALTER POLICY j2m_referral_queue_read ON public.google_wallet_referral_jobs "
            "USING (true)").check_returncode()
    execute(manifest).check_returncode()
    execute("INSERT INTO referral_participants (business_id, customer_id, participant_type, "
            "invitation_code, is_enabled, created_at, updated_at) VALUES "
            "(1,1,'customer_ambassador','synthetic-one',false,now(),now()),"
            "(2,2,'customer_ambassador','synthetic-two',false,now(),now())").check_returncode()
    accounting = """
        SELECT id, current_points, current_stamps FROM public.customers ORDER BY id;
        SELECT to_jsonb(t) FROM public.transactions t ORDER BY id;
        SELECT to_jsonb(l) FROM public.loyalty_ledger_entries l ORDER BY id;
    """
    before = execute(accounting).stdout
    execute("GRANT j2m_owner TO j2m_schema_worker").check_returncode()
    rejected = execute(manifest)
    assert rejected.returncode and b"Unexpected J2M service identity" in rejected.stderr
    execute("REVOKE j2m_owner FROM j2m_schema_worker").check_returncode()
    root = Path(__file__).resolve().parents[1]
    execute((root / "scripts/j2m_staff_login_grants.sql").read_text()).check_returncode()
    execute("""
        CREATE POLICY synthetic_platform_session_read ON public.platform_admin_sessions
            FOR SELECT TO j2m_schema_web USING (true);
        CREATE POLICY synthetic_platform_session_create ON public.platform_admin_sessions
            FOR INSERT TO j2m_schema_web WITH CHECK (true);
        CREATE POLICY synthetic_platform_session_revoke ON public.platform_admin_sessions
            FOR UPDATE TO j2m_schema_web USING (true) WITH CHECK (true);
    """).check_returncode()
    admin_grants()
    customer_grants()
    customer_policies()
    inventory_sql = (root / "scripts/j2m_transition_privilege_inventory.sql").read_text()
    inventory_result = execute(inventory_sql)
    inventory_result.check_returncode()
    assert execute(inventory_sql).stdout == inventory_result.stdout
    inventory = json.loads(inventory_result.stdout)
    assert {row[0] for row in inventory["roles"]} == {
        "j2m_owner", "j2m_migrator", "j2m_schema_web", "j2m_schema_worker", "j2m_backup",
    }
    assert any(row["policyname"] == "j2m_referral_queue_process" for row in inventory["policies"])
    assert any(row[0:2] == ["staff_sessions", "revoked_at"] for row in inventory["columns"])
    # Compare effective service grants, not just catalog ACLs. A table-level
    # grant implies the corresponding column grants and is checked separately.
    expected_tables = {
        web: {**OBJECTS[web]["tables"], "staff": "SELECT",
              "staff_sessions": "SELECT,INSERT", "auth_rate_limit_slots": "SELECT,INSERT,DELETE",
              "referral_participants": "SELECT", "google_wallet_referral_jobs": "SELECT,INSERT",
              "google_wallet_write_reservations": "SELECT,INSERT",
              "platform_admins": "SELECT", "business_categories": "SELECT",
              "google_wallet_sync_jobs": "SELECT", "platform_admin_sessions": "SELECT,INSERT"},
        worker: {**OBJECTS[worker]["tables"], "referral_participants": "SELECT",
                 "google_wallet_referral_jobs": "SELECT,INSERT",
                 "google_wallet_write_reservations": "SELECT,INSERT"},
    }
    expected_tables[web].update(dict.fromkeys(CUSTOMER_TABLES, "SELECT,INSERT"))
    expected_table_rows = {
        (role, table, privilege) for role, grants in expected_tables.items()
        for table, privileges in grants.items() for privilege in privileges.split(",")
    }
    expected_sequence_rows = {
        (role, sequence, "USAGE") for role, grants in OBJECTS.items()
        for sequence in (*grants["sequences"], "google_wallet_referral_jobs_id_seq")
    }
    expected_sequence_rows.update((web, sequence, "USAGE") for sequence in CUSTOMER_SEQUENCES)
    extra_updates = {
        web: {"customers": ("current_points", "current_stamps", "last_earn_at"),
              "businesses": ("referrals_enabled",), "staff_sessions": ("revoked_at",),
              "auth_rate_limit_slots": ("reserved_until", "updated_at"),
              "platform_admins": ("name",), "platform_admin_sessions": ("revoked_at",)},
        worker: {"google_wallet_referral_jobs": ("status", "token", "locked_at", "attempts",
                                                  "run_at", "cursor", "outcome", "updated_at")},
    }
    for table, names in CUSTOMER_UPDATES.items():
        extra_updates[web][table] = (*extra_updates[web].get(table, ()), *names)
    for role in OBJECTS:
        extra_updates[role]["google_wallet_write_reservations"] = (
            "state", "active_slot", "updated_at", "started_at", "remote_resolved_at", "remote_status")
    columns = json.loads(execute("""
        SELECT jsonb_agg(jsonb_build_array(c.relname, a.attname))
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
          AND a.attnum>0 AND NOT a.attisdropped
    """).stdout)
    expected_column_rows = {
        (role, table, column, privilege) for role, name, privilege in expected_table_rows
        for table, column in columns if table == name
        and privilege in {"SELECT", "INSERT", "UPDATE", "REFERENCES"}
    }
    expected_column_rows.update(
        (role, table, column, "UPDATE") for role, grants in extra_updates.items()
        for table, names in grants.items() for column in names
    )

    def assert_service_manifest(observed):
        for key, expected in (("effective_tables", expected_table_rows),
                              ("effective_columns", expected_column_rows),
                              ("effective_sequences", expected_sequence_rows)):
            actual = {tuple(row) for row in observed[key] if row[0] in OBJECTS}
            if actual != expected:
                raise AssertionError(f"Unexpected {key}: extra={sorted(actual-expected)}, missing={sorted(expected-actual)}")
        if any(row[0] in OBJECTS for row in observed["effective_memberships"]):
            raise AssertionError("Unexpected service role membership")

    assert_service_manifest(inventory)
    for grant, revoke in (
        (f"GRANT UPDATE (current_stamps) ON public.customers TO {worker}",
         f"REVOKE UPDATE (current_stamps) ON public.customers FROM {worker}"),
        ("GRANT DELETE ON public.customers TO PUBLIC", "REVOKE DELETE ON public.customers FROM PUBLIC"),
        (f"GRANT UPDATE ON SEQUENCE public.transactions_id_seq TO {worker}",
         f"REVOKE UPDATE ON SEQUENCE public.transactions_id_seq FROM {worker}"),
        (f"GRANT j2m_owner TO {worker}", f"REVOKE j2m_owner FROM {worker}"),
    ):
        execute(grant).check_returncode()
        try:
            assert_service_manifest(json.loads(execute(inventory_sql).stdout))
        except AssertionError:
            pass
        else:
            raise RuntimeError("Privilege drift was accepted")
        finally:
            execute(revoke).check_returncode()
        assert_service_manifest(json.loads(execute(inventory_sql).stdout))

    service_flow(web, (root / "tests/postgres_manager_login_flow.py").read_text())
    service_flow(web, (root / "tests/postgres_platform_admin_flow.py").read_text())
    assert execute("UPDATE platform_admins SET is_active=false", web).returncode
    assert execute("UPDATE platform_admins SET password_hash='invalid'", web).returncode
    assert execute("SELECT * FROM platform_admin_sessions", worker).returncode
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
    execute("""
        CREATE FUNCTION public.synthetic_fence_write() RETURNS void LANGUAGE SQL SECURITY DEFINER
        SET search_path = pg_catalog AS $$
            UPDATE public.customers SET current_stamps = 99 WHERE id = 1;
        $$;
        REVOKE EXECUTE ON FUNCTION public.synthetic_fence_write() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.synthetic_fence_write() TO j2m_schema_worker;
    """).check_returncode()
    revoke_customer_updates = "; ".join(
        f"REVOKE UPDATE ({','.join(columns)}) ON public.{table} FROM {web}"
        for table, columns in CUSTOMER_UPDATES.items()
    )
    script = f"""
BEGIN;
{revoke_customer_updates};
ALTER ROLE {web} NOLOGIN;
ALTER ROLE {worker} NOLOGIN;
REVOKE UPDATE (revoked_at) ON public.staff_sessions FROM {web};
REVOKE UPDATE (name) ON public.platform_admins FROM {web};
REVOKE UPDATE (revoked_at) ON public.platform_admin_sessions FROM {web};
REVOKE UPDATE (reserved_until, updated_at) ON public.auth_rate_limit_slots FROM {web};
REVOKE UPDATE (current_points, current_stamps, last_earn_at) ON public.customers FROM {web};
REVOKE UPDATE (referrals_enabled) ON public.businesses FROM {web};
REVOKE UPDATE (status, token, locked_at, attempts, run_at, cursor, outcome, updated_at)
    ON public.google_wallet_referral_jobs FROM {worker};
REVOKE UPDATE (state, active_slot, updated_at, started_at, remote_resolved_at, remote_status)
    ON public.google_wallet_write_reservations FROM {roles};
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles};
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles};
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {roles};
SET LOCAL lock_timeout = '8s';
LOCK TABLE public.google_wallet_referral_jobs IN ACCESS EXCLUSIVE MODE;
COMMIT;
"""
    def send(peer, statement):
        peer.stdin.write((statement + "\n").encode())
        peer.stdin.flush()

    def kill(peer):
        if peer.poll() is None:
            peer.kill()
            assert peer.wait(timeout=8) != 0

    def terminate_backend(pid):
        execute(f"SELECT pg_terminate_backend(pid, 3000) FROM pg_stat_activity WHERE pid={pid}").check_returncode()
        assert execute(f"SELECT count(*) FROM pg_stat_activity WHERE pid={pid}").stdout.strip() == b"0"

    def await_lock(pid):
        for _ in range(40):
            if execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND "
                       f"pid={pid} AND relation='public.google_wallet_referral_jobs'::regclass").stdout.strip() == b"1":
                return
            time.sleep(0.1)
        raise RuntimeError("Fence did not wait for the pooled transaction")

    def direct_blocked():
        for role in OBJECTS:
            connection_check = execute("SELECT 1", role)
            write_check = execute("UPDATE public.customers SET current_stamps=0", role)
            assert connection_check.returncode and write_check.returncode
            assert any(message in connection_check.stderr for message in
                       (b"permission denied for database", b"not permitted to log in"))

    def pool_blocked():
        newcomer = connection(worker, database)
        _, rejected = newcomer.communicate(b"SELECT 1;\n", timeout=8)
        assert newcomer.returncode and b"disabled" in rejected.lower()

    pool_control(f"ENABLE {database}").check_returncode()
    peer = connection(worker, database)
    try:
        peer.stdin.write(b"BEGIN; SELECT current_user,pg_backend_pid(); COMMIT; SELECT pg_backend_pid();\n")
        peer.stdin.flush()
        current, pid = peer.stdout.readline().strip().split(b"|")
        assert current == worker.encode() and peer.stdout.readline().strip() == pid
        pool_control(f"DISABLE {database}").check_returncode()
        pool_blocked()
        # Database CONNECT is an independent gate even if the role remains LOGIN.
        execute(f"GRANT CONNECT ON DATABASE {database} TO j2m_backup,j2m_migrator; "
                f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC,{roles}").check_returncode()
        direct_blocked()
        peer.stdin.write(b"BEGIN; SELECT count(*) FROM public.google_wallet_referral_jobs;\n")
        peer.stdin.flush()
        assert peer.stdout.readline().strip().isdigit()
        if not nominal:
            coordinator = connection("postgres", database, direct=True)
            send(coordinator, "SELECT pg_backend_pid();")
            fence_pid = int(coordinator.stdout.readline())
            try:
                send(coordinator, " ".join(script.splitlines()))
                await_lock(fence_pid)
                kill(coordinator)
                terminate_backend(fence_pid)
                # The aborted fence must not be mistaken for a committed revocation.
                state = execute(f"SELECT rolcanlogin,has_any_column_privilege(rolname,"
                                f"'public.google_wallet_referral_jobs','UPDATE') FROM pg_roles WHERE rolname='{worker}'")
                assert state.stdout.strip() == b"t|t"
                assert execute(accounting).stdout == before
                direct_blocked()
                pool_blocked()
            finally:
                kill(coordinator)
        with ThreadPoolExecutor(max_workers=1) as executor:
            fence = executor.submit(execute, "SELECT pg_backend_pid(); " + script)
            for _ in range(40):
                if execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND "
                           "relation='public.google_wallet_referral_jobs'::regclass").stdout.strip() == b"1":
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Replacement fence did not drain the pooled transaction")
            send(peer, "ROLLBACK;")
            fence.result(timeout=12).check_returncode()
        _, errors = peer.communicate(
            b"INSERT INTO public.google_wallet_referral_jobs DEFAULT VALUES;\n"
            b"UPDATE public.google_wallet_referral_jobs SET status='pending';\n"
            b"DELETE FROM public.google_wallet_referral_jobs;\n"
            b"TRUNCATE public.google_wallet_referral_jobs;\n"
            b"SELECT nextval('public.google_wallet_referral_jobs_id_seq');\n"
            b"SET ROLE j2m_owner;\n"
            b"SELECT public.synthetic_fence_write();\n", timeout=8)
        assert errors.count(b"permission denied") == 7
        assert execute(accounting).stdout == before
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=8)
    for role in OBJECTS:
        effective = execute(f"""
            SELECT json_build_array(
                has_schema_privilege('{role}', 'public', 'USAGE'),
                has_any_column_privilege('{role}', 'public.google_wallet_referral_jobs', 'UPDATE'),
                has_sequence_privilege('{role}', 'public.google_wallet_referral_jobs_id_seq', 'USAGE'),
                has_function_privilege('{role}', 'public.synthetic_fence_write()', 'EXECUTE'),
                pg_has_role('{role}', 'j2m_owner', 'MEMBER'))
        """)
        effective.check_returncode()
        assert json.loads(effective.stdout) == [True, False, False, False, False]
        if execute("SELECT 1", role).returncode == 0:
            raise RuntimeError("Fenced login reconnected")
    print("Scoped grants and revoked reconnect on the real J2M fixture verified; referral queue and reservation operations verified.")

    backup = execute("SELECT count(*) FROM public.loyalty_ledger_entries", "j2m_backup")
    backup.check_returncode()
    assert backup.stdout == execute("SELECT count(*) FROM public.loyalty_ledger_entries").stdout
    assert execute("UPDATE public.customers SET current_stamps=0", "j2m_backup").returncode
    assert execute(accounting).stdout == before

    # Drop cached PostgreSQL connections before restoring write privileges.
    # NOLOGIN prevents another direct connection until grants are certified.
    active = f"FROM pg_stat_activity WHERE usename IN ('{web}', '{worker}')"
    execute(f"SELECT pg_terminate_backend(pid, 3000) {active}").check_returncode()
    assert execute(f"SELECT count(*) {active}").stdout.strip() == b"0"
    def restore_grants():
        execute(fixture_grants()).check_returncode()
        execute(f"GRANT UPDATE (current_points, current_stamps, last_earn_at) ON public.customers TO {web}").check_returncode()
        execute(f"GRANT UPDATE (referrals_enabled) ON public.businesses TO {web}").check_returncode()
        execute(manifest).check_returncode()
        execute((root / "scripts/j2m_staff_login_grants.sql").read_text()).check_returncode()
        admin_grants()
        customer_grants()
        restored_inventory = execute(inventory_sql)
        restored_inventory.check_returncode()
        assert_service_manifest(json.loads(restored_inventory.stdout))

    def refence():
        pool_control(f"DISABLE {database}").check_returncode()
        execute(script).check_returncode()
        assert execute(f"SELECT count(*) {active}").stdout.strip() == b"0"
        direct_blocked()
        pool_blocked()
        assert execute(accounting).stdout == before
        assert execute(f"SELECT count(*) FROM pg_roles WHERE rolname IN ('{web}','{worker}') "
                       "AND NOT rolcanlogin").stdout.strip() == b"2"
        assert execute(f"SELECT has_any_column_privilege('{worker}',"
                       "'public.google_wallet_referral_jobs','UPDATE')").stdout.strip() == b"f"

    def interrupted(command, *, pool=False, transaction=False):
        coordinator = connection("j2m_pool_admin", "pgbouncer") if pool else connection(
            "postgres", database, direct=True)
        pid = None
        try:
            if not pool:
                send(coordinator, "SELECT pg_backend_pid();")
                pid = int(coordinator.stdout.readline())
            if transaction:
                send(coordinator, f"BEGIN; {command}; SELECT 'held';")
            else:
                send(coordinator, command)
                if not pool:
                    send(coordinator, "SELECT 'held';")
            if not pool:
                assert coordinator.stdout.readline().strip() == b"held"
            else:
                send(coordinator, r"\echo pool-command-returned")
                assert coordinator.stdout.readline().strip() == b"pool-command-returned"
            kill(coordinator)
            if pid is not None:
                terminate_backend(pid)
            direct_blocked()
            assert execute(accounting).stdout == before
        finally:
            kill(coordinator)

    login = f"ALTER ROLE {web} LOGIN; ALTER ROLE {worker} LOGIN;"
    if not nominal:
        interrupted(fixture_grants(), transaction=True)
        assert execute(f"SELECT has_table_privilege('{web}','public.transactions','INSERT')").stdout.strip() == b"f"
        refence()
        restore_grants()
        interrupted(fixture_grants() + ";")
        assert execute(f"SELECT has_table_privilege('{web}','public.transactions','INSERT')").stdout.strip() == b"t"
        pool_blocked()
        refence()
        restore_grants()
        interrupted(login)
        pool_blocked()
        refence()
        restore_grants()
        execute(login).check_returncode()
        interrupted(f"ENABLE {database};", pool=True)
        newcomer = connection(worker, database)
        _, error = newcomer.communicate(b"SELECT 1;\n", timeout=8)
        assert newcomer.returncode and b"permission denied for database" in error
        refence()
        print("Killed a blocked fence and four reopening coordinators; connection gate and accounting verified.")


    restore_grants()
    execute(login).check_returncode()
    pool_control(f"ENABLE {database}").check_returncode()
    direct_blocked()
    execute(f"GRANT CONNECT ON DATABASE {database} TO {roles}").check_returncode()
    for _ in range(5):
        reopened = connection(worker, database)
        result, errors = reopened.communicate(b"SELECT current_user,current_database();\n", timeout=8)
        if not reopened.returncode and not errors and result.strip() == f"{worker}|{database}".encode():
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Reopened pool did not establish an authenticated writer connection")
    service_flow(web, (root / "tests/postgres_manager_login_flow.py").read_text())
    service_flow(web, (root / "tests/postgres_platform_admin_flow.py").read_text())
    assert execute(accounting).stdout == before
    print("PgBouncer session affinity, drainage, denial, and verified synthetic reopening passed.")
    service_flow(web, (root / "tests/postgres_loyalty_http_flow.py").read_text())
