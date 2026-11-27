-- Install as the J2M schema owner, after the internal web role is provisioned.
BEGIN;
GRANT SELECT ON public.businesses, public.staff TO j2m_schema_web;
GRANT SELECT, INSERT ON public.staff_sessions TO j2m_schema_web;
GRANT UPDATE (revoked_at) ON public.staff_sessions TO j2m_schema_web;
GRANT SELECT, INSERT, DELETE ON public.auth_rate_limit_slots TO j2m_schema_web;
GRANT UPDATE (reserved_until, updated_at) ON public.auth_rate_limit_slots TO j2m_schema_web;
COMMIT;
