-- Install as the J2M schema owner, after the internal web role is provisioned.
BEGIN;
GRANT SELECT ON public.businesses, public.staff TO j2m_schema_web;
GRANT SELECT, INSERT ON public.staff_sessions TO j2m_schema_web;
GRANT UPDATE (revoked_at) ON public.staff_sessions TO j2m_schema_web;
GRANT SELECT, INSERT, DELETE ON public.auth_rate_limit_slots TO j2m_schema_web;
GRANT UPDATE (reserved_until, updated_at) ON public.auth_rate_limit_slots TO j2m_schema_web;
GRANT SELECT ON public.referral_participants TO j2m_schema_web;
DO $policy$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_policies WHERE schemaname = 'public'
          AND tablename = 'referral_participants' AND policyname = 'j2m_referral_web_participants'
    ) THEN
        CREATE POLICY j2m_referral_web_participants ON public.referral_participants
            FOR SELECT TO j2m_schema_web USING (true);
    ELSIF NOT EXISTS (
        SELECT FROM pg_policies WHERE schemaname = 'public'
          AND tablename = 'referral_participants' AND policyname = 'j2m_referral_web_participants'
          AND cmd = 'SELECT' AND roles = ARRAY['j2m_schema_web']::name[]
          AND permissive = 'PERMISSIVE' AND qual = 'true' AND with_check IS NULL
    ) THEN
        RAISE EXCEPTION 'Unexpected cashier participant policy';
    END IF;
END
$policy$;
COMMIT;
