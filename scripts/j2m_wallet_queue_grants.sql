-- Run as J2M schema owner after provisioning the two service roles.
BEGIN;
GRANT SELECT, INSERT ON public.google_wallet_referral_jobs TO j2m_schema_web;
GRANT SELECT, INSERT ON public.google_wallet_referral_jobs TO j2m_schema_worker;
GRANT UPDATE (status, token, locked_at, attempts, run_at, cursor, outcome, updated_at)
    ON public.google_wallet_referral_jobs TO j2m_schema_worker;
GRANT USAGE ON SEQUENCE public.google_wallet_referral_jobs_id_seq TO j2m_schema_web, j2m_schema_worker;
GRANT SELECT ON public.referral_participants TO j2m_schema_worker;
GRANT SELECT, INSERT ON public.google_wallet_write_reservations TO j2m_schema_web, j2m_schema_worker;
GRANT UPDATE (state, active_slot, updated_at, started_at, remote_resolved_at, remote_status)
    ON public.google_wallet_write_reservations TO j2m_schema_web, j2m_schema_worker;

CREATE POLICY j2m_referral_queue_read ON public.google_wallet_referral_jobs
    FOR SELECT TO j2m_schema_web, j2m_schema_worker USING (true);
CREATE POLICY j2m_referral_queue_enqueue ON public.google_wallet_referral_jobs
    FOR INSERT TO j2m_schema_web, j2m_schema_worker
    WITH CHECK (status = 'pending' AND token IS NULL AND locked_at IS NULL
                AND attempts = 0 AND cursor = 0 AND outcome IS NULL);
CREATE POLICY j2m_referral_queue_process ON public.google_wallet_referral_jobs
    FOR UPDATE TO j2m_schema_worker USING (true) WITH CHECK (true);
CREATE POLICY j2m_referral_worker_participants ON public.referral_participants
    FOR SELECT TO j2m_schema_worker USING (true);
CREATE POLICY j2m_wallet_reservation_read ON public.google_wallet_write_reservations
    FOR SELECT TO j2m_schema_web, j2m_schema_worker USING (true);
CREATE POLICY j2m_wallet_reservation_create ON public.google_wallet_write_reservations
    FOR INSERT TO j2m_schema_web, j2m_schema_worker
    WITH CHECK (state = 'reserved' AND active_slot = 1);
CREATE POLICY j2m_wallet_reservation_transition ON public.google_wallet_write_reservations
    FOR UPDATE TO j2m_schema_web, j2m_schema_worker USING (true) WITH CHECK (true);
COMMIT;
