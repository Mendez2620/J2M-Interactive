-- Run as J2M schema owner after provisioning the two service roles.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $roles$
DECLARE
    service pg_roles%ROWTYPE;
    service_name text;
BEGIN
    FOREACH service_name IN ARRAY ARRAY['j2m_schema_web', 'j2m_schema_worker']
    LOOP
        SELECT * INTO STRICT service FROM pg_roles WHERE rolname = service_name;
        IF service.rolsuper OR service.rolbypassrls
           OR service.rolcreaterole OR service.rolcreatedb OR service.rolreplication
           OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member = service.oid OR roleid = service.oid)
           OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = service.oid
                      AND relnamespace = 'public'::regnamespace) THEN
            RAISE EXCEPTION 'Unexpected J2M service identity %', service_name;
        END IF;
    END LOOP;
END
$roles$;

GRANT SELECT, INSERT ON public.google_wallet_referral_jobs TO j2m_schema_web;
GRANT SELECT, INSERT ON public.google_wallet_referral_jobs TO j2m_schema_worker;
GRANT UPDATE (status, token, locked_at, attempts, run_at, cursor, outcome, updated_at)
    ON public.google_wallet_referral_jobs TO j2m_schema_worker;
GRANT USAGE ON SEQUENCE public.google_wallet_referral_jobs_id_seq TO j2m_schema_web, j2m_schema_worker;
GRANT SELECT ON public.referral_participants TO j2m_schema_worker;
GRANT SELECT, INSERT ON public.google_wallet_write_reservations TO j2m_schema_web, j2m_schema_worker;
GRANT UPDATE (state, active_slot, updated_at, started_at, remote_resolved_at, remote_status)
    ON public.google_wallet_write_reservations TO j2m_schema_web, j2m_schema_worker;

-- Reconcile only J2M-managed policies. Compare PostgreSQL's catalog output
-- against a transaction-local definition parsed by the same server.
DO $manifest$
DECLARE
    item record;
    actual record;
    expected record;
    reference_table text;
    target_table regclass;
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('google_wallet_referral_jobs', 'j2m_referral_queue_read',
         'SELECT TO j2m_schema_web, j2m_schema_worker USING (true)'),
        ('google_wallet_referral_jobs', 'j2m_referral_queue_enqueue',
         'INSERT TO j2m_schema_web, j2m_schema_worker WITH CHECK
            (status = ''pending'' AND token IS NULL AND locked_at IS NULL
             AND attempts = 0 AND cursor = 0 AND outcome IS NULL)'),
        ('google_wallet_referral_jobs', 'j2m_referral_queue_process',
         'UPDATE TO j2m_schema_worker USING (true) WITH CHECK (true)'),
        ('referral_participants', 'j2m_referral_worker_participants',
         'SELECT TO j2m_schema_worker USING (true)'),
        ('google_wallet_write_reservations', 'j2m_wallet_reservation_read',
         'SELECT TO j2m_schema_web, j2m_schema_worker USING (true)'),
        ('google_wallet_write_reservations', 'j2m_wallet_reservation_create',
         'INSERT TO j2m_schema_web, j2m_schema_worker WITH CHECK (state = ''reserved'' AND active_slot = 1)'),
        ('google_wallet_write_reservations', 'j2m_wallet_reservation_transition',
         'UPDATE TO j2m_schema_web, j2m_schema_worker USING (true) WITH CHECK (true)')
    ) AS definitions(table_name, policy_name, clause)
    LOOP
        target_table := format('public.%I', item.table_name)::regclass;
        IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = target_table) THEN
            RAISE EXCEPTION 'Expected RLS on J2M table %', item.table_name;
        END IF;
        SELECT polcmd, polpermissive, polroles, pg_get_expr(polqual, polrelid) AS predicate,
               pg_get_expr(polwithcheck, polrelid) AS check_predicate
        INTO actual FROM pg_policy WHERE polrelid = target_table AND polname = item.policy_name;
        IF NOT FOUND THEN
            EXECUTE format('CREATE POLICY %I ON %s FOR %s', item.policy_name, target_table, item.clause);
        ELSE
            reference_table := 'j2m_expected_' || item.table_name;
            EXECUTE format('CREATE TEMP TABLE %I (LIKE %s) ON COMMIT DROP', reference_table, target_table);
            EXECUTE format('CREATE POLICY expected ON pg_temp.%I FOR %s', reference_table, item.clause);
            SELECT polcmd, polpermissive, polroles, pg_get_expr(polqual, polrelid) AS predicate,
                   pg_get_expr(polwithcheck, polrelid) AS check_predicate
            INTO STRICT expected FROM pg_policy
            WHERE polrelid = format('pg_temp.%I', reference_table)::regclass AND polname = 'expected';
            IF actual IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'Unexpected definition of J2M policy %', item.policy_name;
            END IF;
            EXECUTE format('DROP TABLE pg_temp.%I', reference_table);
        END IF;
    END LOOP;
END
$manifest$;
COMMIT;
