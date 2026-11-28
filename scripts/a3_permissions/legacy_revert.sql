-- Proposed increment only. Requires separate authorization, independently verified project, and A2 already confirmed.
BEGIN;
SET LOCAL lock_timeout='2s';
SET LOCAL statement_timeout='20s';
SET LOCAL idle_in_transaction_session_timeout='60s';
SET LOCAL search_path=pg_catalog;

DO $guard$
BEGIN
    IF current_database()<>'postgres' OR session_user<>'postgres' OR current_user<>'postgres'
       OR current_setting('server_version_num')::int NOT BETWEEN 170000 AND 179999
       OR (SELECT array_agg(version_num::text) FROM public.alembic_version) IS DISTINCT FROM ARRAY['9d0e1f2a3b4c']
       OR (SELECT array_agg(oid ORDER BY CASE rolname WHEN 'j2m_owner' THEN 1 WHEN 'j2m_migrator' THEN 2
                    WHEN 'j2m_web' THEN 3 WHEN 'j2m_worker' THEN 4 WHEN 'j2m_backup' THEN 5 ELSE 6 END)
             FROM pg_roles WHERE rolname IN ('j2m_owner','j2m_migrator','j2m_web','j2m_worker','j2m_backup','j2m_recovery')) IS DISTINCT FROM ARRAY[18919,18921,18923,18925,18927,18929]::oid[]
       OR EXISTS (SELECT FROM pg_roles WHERE rolname IN ('j2m_owner','j2m_migrator','j2m_web','j2m_worker','j2m_backup','j2m_recovery') AND
            (rolcanlogin OR rolinherit OR rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls))
       OR EXISTS (SELECT FROM pg_roles r JOIN pg_stat_activity a ON a.usesysid=r.oid WHERE r.rolname IN ('j2m_owner','j2m_migrator','j2m_web','j2m_worker','j2m_backup','j2m_recovery'))
       OR EXISTS (
            SELECT FROM pg_roles r JOIN pg_auth_members m ON m.member=r.oid
            WHERE r.rolname IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery')
       )
       OR EXISTS (
           SELECT FROM pg_roles c CROSS JOIN pg_roles r
           WHERE c.rolname IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery')
             AND r.oid<>c.oid AND NOT r.rolsuper
             AND (pg_has_role(r.oid,c.oid,'SET') OR pg_has_role(r.oid,c.oid,'USAGE'))
       ) THEN RAISE EXCEPTION 'Unexpected A2/A3 scope, role or revision'; END IF;
END;
$guard$;

CREATE TEMP VIEW a3_catalog_acl AS
WITH catalog AS (
    SELECT c.relname::text obj, ''::text col,
           CASE WHEN c.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END kind,
           a.grantee,a.grantor,a.privilege_type priv,a.is_grantable
    FROM pg_class c CROSS JOIN LATERAL aclexplode(
        coalesce(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 's'::"char" ELSE 'r'::"char" END,c.relowner))) a
    WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','S','v','m','f')
    UNION ALL
    SELECT c.relname::text,b.attname::text,'TABLE', a.grantee,a.grantor,a.privilege_type,a.is_grantable
    FROM pg_class c JOIN pg_attribute b ON b.attrelid=c.oid AND b.attnum>0 AND NOT b.attisdropped
    CROSS JOIN LATERAL aclexplode(b.attacl) a WHERE c.relnamespace='public'::regnamespace
)
SELECT CASE WHEN grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee)::text END role_name,
       kind,obj,col,priv,pg_get_userbyid(grantor)::text grantor,is_grantable FROM catalog;

CREATE TEMP TABLE a3_external ON COMMIT DROP AS
SELECT * FROM pg_temp.a3_catalog_acl WHERE role_name NOT IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery');

CREATE TEMP VIEW a3_catalog_policy AS
SELECT CASE WHEN c.relnamespace=pg_my_temp_schema() THEN substring(c.relname::text FROM 8) ELSE c.relname::text END obj,
       p.polname::text policy,p.polcmd,p.polpermissive,p.polroles,
       pg_get_expr(p.polqual,p.polrelid) qual,pg_get_expr(p.polwithcheck,p.polrelid) with_check,
       c.relnamespace=pg_my_temp_schema() AS reference
FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
WHERE c.relnamespace='public'::regnamespace OR
      (c.relnamespace=pg_my_temp_schema() AND left(c.relname::text,7)='a3_ref_');
CREATE TEMP TABLE a3_ref_apple_pass_credentials (LIKE public.apple_pass_credentials) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_apple_pass_update_sequence (LIKE public.apple_pass_update_sequence) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_apple_wallet_publications (LIKE public.apple_wallet_publications) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_apple_wallet_push_jobs (LIKE public.apple_wallet_push_jobs) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_customer_auth_audits (LIKE public.customer_auth_audits) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_customer_auth_challenges (LIKE public.customer_auth_challenges) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_customer_credentials (LIKE public.customer_credentials) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_customer_linking_enrollments (LIKE public.customer_linking_enrollments) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_customer_referral_activations (LIKE public.customer_referral_activations) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_customer_sessions (LIKE public.customer_sessions) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_google_wallet_publication_jobs (LIKE public.google_wallet_publication_jobs) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_google_wallet_publications (LIKE public.google_wallet_publications) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_eligibilities (LIKE public.referral_eligibilities) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_eligibility_decisions (LIKE public.referral_eligibility_decisions) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_incentive_records (LIKE public.referral_incentive_records) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_incentive_rule_audits (LIKE public.referral_incentive_rule_audits) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_incentive_rules (LIKE public.referral_incentive_rules) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_participant_audits (LIKE public.referral_participant_audits) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_participants (LIKE public.referral_participants) ON COMMIT DROP;
CREATE TEMP TABLE a3_ref_referral_program_audits (LIKE public.referral_program_audits) ON COMMIT DROP;
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_apple_pass_credentials AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_apple_pass_update_sequence AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_apple_wallet_publications AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_apple_wallet_push_jobs AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_customer_auth_audits AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_web_insert ON pg_temp.a3_ref_customer_auth_audits AS PERMISSIVE FOR INSERT TO "j2m_web" WITH CHECK (true);
CREATE POLICY j2m_a2_web_read ON pg_temp.a3_ref_customer_auth_audits AS PERMISSIVE FOR SELECT TO "j2m_web" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_customer_auth_challenges AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_web_insert ON pg_temp.a3_ref_customer_auth_challenges AS PERMISSIVE FOR INSERT TO "j2m_web" WITH CHECK (true);
CREATE POLICY j2m_a2_web_read ON pg_temp.a3_ref_customer_auth_challenges AS PERMISSIVE FOR SELECT TO "j2m_web" USING (true);
CREATE POLICY j2m_a2_web_update ON pg_temp.a3_ref_customer_auth_challenges AS PERMISSIVE FOR UPDATE TO "j2m_web" USING (true) WITH CHECK (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_customer_credentials AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_web_insert ON pg_temp.a3_ref_customer_credentials AS PERMISSIVE FOR INSERT TO "j2m_web" WITH CHECK (true);
CREATE POLICY j2m_a2_web_read ON pg_temp.a3_ref_customer_credentials AS PERMISSIVE FOR SELECT TO "j2m_web" USING (true);
CREATE POLICY j2m_a2_web_update ON pg_temp.a3_ref_customer_credentials AS PERMISSIVE FOR UPDATE TO "j2m_web" USING (true) WITH CHECK (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_customer_linking_enrollments AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_web_insert ON pg_temp.a3_ref_customer_linking_enrollments AS PERMISSIVE FOR INSERT TO "j2m_web" WITH CHECK (true);
CREATE POLICY j2m_a2_web_read ON pg_temp.a3_ref_customer_linking_enrollments AS PERMISSIVE FOR SELECT TO "j2m_web" USING (true);
CREATE POLICY j2m_a2_web_update ON pg_temp.a3_ref_customer_linking_enrollments AS PERMISSIVE FOR UPDATE TO "j2m_web" USING (true) WITH CHECK (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_customer_referral_activations AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_customer_sessions AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_web_insert ON pg_temp.a3_ref_customer_sessions AS PERMISSIVE FOR INSERT TO "j2m_web" WITH CHECK (true);
CREATE POLICY j2m_a2_web_read ON pg_temp.a3_ref_customer_sessions AS PERMISSIVE FOR SELECT TO "j2m_web" USING (true);
CREATE POLICY j2m_a2_web_update ON pg_temp.a3_ref_customer_sessions AS PERMISSIVE FOR UPDATE TO "j2m_web" USING (true) WITH CHECK (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_google_wallet_publication_jobs AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_google_wallet_publications AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_eligibilities AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_eligibility_decisions AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_incentive_records AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_incentive_rule_audits AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_incentive_rules AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_participant_audits AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_participants AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a2_web_insert ON pg_temp.a3_ref_referral_participants AS PERMISSIVE FOR INSERT TO "j2m_web" WITH CHECK (participant_type = 'customer_ambassador' AND NOT is_enabled);
CREATE POLICY j2m_a2_web_read ON pg_temp.a3_ref_referral_participants AS PERMISSIVE FOR SELECT TO "j2m_web" USING (true);
CREATE POLICY j2m_a2_backup_read ON pg_temp.a3_ref_referral_program_audits AS PERMISSIVE FOR SELECT TO "j2m_backup" USING (true);
CREATE POLICY j2m_a3_update ON pg_temp.a3_ref_referral_participants AS PERMISSIVE FOR UPDATE TO j2m_web USING (true) WITH CHECK (false);
CREATE POLICY j2m_a3_select ON pg_temp.a3_ref_referral_eligibilities AS PERMISSIVE FOR SELECT TO j2m_web USING (true);
CREATE POLICY j2m_a3_insert ON pg_temp.a3_ref_referral_eligibilities AS PERMISSIVE FOR INSERT TO j2m_web WITH CHECK (status = 'pending');
CREATE POLICY j2m_a3_select ON pg_temp.a3_ref_referral_incentive_rules AS PERMISSIVE FOR SELECT TO j2m_web USING (true);
CREATE POLICY j2m_a3_insert ON pg_temp.a3_ref_referral_incentive_rules AS PERMISSIVE FOR INSERT TO j2m_web WITH CHECK (status IN ('draft','disabled'));
CREATE POLICY j2m_a3_update ON pg_temp.a3_ref_referral_incentive_rules AS PERMISSIVE FOR UPDATE TO j2m_web USING (status IN ('draft','disabled')) WITH CHECK (status IN ('draft','disabled'));
CREATE POLICY j2m_a3_select ON pg_temp.a3_ref_referral_incentive_rule_audits AS PERMISSIVE FOR SELECT TO j2m_web USING (true);
CREATE POLICY j2m_a3_insert ON pg_temp.a3_ref_referral_incentive_rule_audits AS PERMISSIVE FOR INSERT TO j2m_web WITH CHECK (true);

DO $verify_before$
BEGIN
    IF EXISTS (
        WITH expected(role_name,kind,obj,col,priv) AS (VALUES ('j2m_backup','SEQUENCE','access_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_devices_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_pass_credentials_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_pass_update_sequence_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_wallet_publications_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_wallet_push_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','business_categories_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','businesses_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customer_auth_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customer_credentials_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customer_referral_activations_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customers_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','google_wallet_publication_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','google_wallet_publications_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','google_wallet_sync_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','loyalty_ledger_entries_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','platform_admins_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','promoters_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_attributions_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_eligibilities_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_eligibility_decisions_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_incentive_records_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_incentive_rule_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_incentive_rules_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_participant_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_participants_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_program_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','registration_email_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','staff_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','transactions_id_seq','','SELECT'),
('j2m_backup','TABLE','access_audits','','SELECT'),
('j2m_backup','TABLE','alembic_version','','SELECT'),
('j2m_backup','TABLE','apple_devices','','SELECT'),
('j2m_backup','TABLE','apple_pass_credentials','','SELECT'),
('j2m_backup','TABLE','apple_pass_update_sequence','','SELECT'),
('j2m_backup','TABLE','apple_wallet_publications','','SELECT'),
('j2m_backup','TABLE','apple_wallet_push_jobs','','SELECT'),
('j2m_backup','TABLE','auth_rate_limit_slots','','SELECT'),
('j2m_backup','TABLE','business_categories','','SELECT'),
('j2m_backup','TABLE','businesses','','SELECT'),
('j2m_backup','TABLE','customer_auth_audits','','SELECT'),
('j2m_backup','TABLE','customer_auth_challenges','','SELECT'),
('j2m_backup','TABLE','customer_credentials','','SELECT'),
('j2m_backup','TABLE','customer_linking_enrollments','','SELECT'),
('j2m_backup','TABLE','customer_recovery_authorizations','','SELECT'),
('j2m_backup','TABLE','customer_recovery_challenges','','SELECT'),
('j2m_backup','TABLE','customer_recovery_rate_limits','','SELECT'),
('j2m_backup','TABLE','customer_referral_activations','','SELECT'),
('j2m_backup','TABLE','customer_registration_challenges','','SELECT'),
('j2m_backup','TABLE','customer_registration_identity_claims','','SELECT'),
('j2m_backup','TABLE','customer_sessions','','SELECT'),
('j2m_backup','TABLE','customers','','SELECT'),
('j2m_backup','TABLE','google_wallet_publication_jobs','','SELECT'),
('j2m_backup','TABLE','google_wallet_publications','','SELECT'),
('j2m_backup','TABLE','google_wallet_sync_jobs','','SELECT'),
('j2m_backup','TABLE','loyalty_ledger_entries','','SELECT'),
('j2m_backup','TABLE','platform_admins','','SELECT'),
('j2m_backup','TABLE','promoter_sessions','','SELECT'),
('j2m_backup','TABLE','promoters','','SELECT'),
('j2m_backup','TABLE','referral_attributions','','SELECT'),
('j2m_backup','TABLE','referral_eligibilities','','SELECT'),
('j2m_backup','TABLE','referral_eligibility_decisions','','SELECT'),
('j2m_backup','TABLE','referral_incentive_records','','SELECT'),
('j2m_backup','TABLE','referral_incentive_rule_audits','','SELECT'),
('j2m_backup','TABLE','referral_incentive_rules','','SELECT'),
('j2m_backup','TABLE','referral_participant_audits','','SELECT'),
('j2m_backup','TABLE','referral_participants','','SELECT'),
('j2m_backup','TABLE','referral_program_audits','','SELECT'),
('j2m_backup','TABLE','registration_email_jobs','','SELECT'),
('j2m_backup','TABLE','staff','','SELECT'),
('j2m_backup','TABLE','staff_sessions','','SELECT'),
('j2m_backup','TABLE','transactions','','SELECT'),
('j2m_web','SEQUENCE','access_audits_id_seq','','USAGE'),
('j2m_web','SEQUENCE','business_categories_id_seq','','USAGE'),
('j2m_web','SEQUENCE','businesses_id_seq','','USAGE'),
('j2m_web','SEQUENCE','customer_auth_audits_id_seq','','USAGE'),
('j2m_web','SEQUENCE','customer_credentials_id_seq','','USAGE'),
('j2m_web','SEQUENCE','customers_id_seq','','USAGE'),
('j2m_web','SEQUENCE','loyalty_ledger_entries_id_seq','','USAGE'),
('j2m_web','SEQUENCE','referral_attributions_id_seq','','USAGE'),
('j2m_web','SEQUENCE','referral_eligibilities_id_seq','','USAGE'),
('j2m_web','SEQUENCE','referral_incentive_rule_audits_id_seq','','USAGE'),
('j2m_web','SEQUENCE','referral_incentive_rules_id_seq','','USAGE'),
('j2m_web','SEQUENCE','referral_participants_id_seq','','USAGE'),
('j2m_web','SEQUENCE','staff_id_seq','','USAGE'),
('j2m_web','SEQUENCE','transactions_id_seq','','USAGE'),
('j2m_web','TABLE','access_audits','','INSERT'),
('j2m_web','TABLE','access_audits','id','SELECT'),
('j2m_web','TABLE','alembic_version','','SELECT'),
('j2m_web','TABLE','auth_rate_limit_slots','','DELETE'),
('j2m_web','TABLE','auth_rate_limit_slots','','INSERT'),
('j2m_web','TABLE','auth_rate_limit_slots','','SELECT'),
('j2m_web','TABLE','auth_rate_limit_slots','reserved_until','UPDATE'),
('j2m_web','TABLE','auth_rate_limit_slots','updated_at','UPDATE'),
('j2m_web','TABLE','business_categories','','INSERT'),
('j2m_web','TABLE','business_categories','','SELECT'),
('j2m_web','TABLE','businesses','','INSERT'),
('j2m_web','TABLE','businesses','','SELECT'),
('j2m_web','TABLE','businesses','name','UPDATE'),
('j2m_web','TABLE','businesses','referrals_enabled','UPDATE'),
('j2m_web','TABLE','customer_auth_audits','','INSERT'),
('j2m_web','TABLE','customer_auth_audits','','SELECT'),
('j2m_web','TABLE','customer_auth_challenges','','INSERT'),
('j2m_web','TABLE','customer_auth_challenges','','SELECT'),
('j2m_web','TABLE','customer_auth_challenges','consumed_at','UPDATE'),
('j2m_web','TABLE','customer_credentials','','INSERT'),
('j2m_web','TABLE','customer_credentials','','SELECT'),
('j2m_web','TABLE','customer_credentials','is_active','UPDATE'),
('j2m_web','TABLE','customer_credentials','last_used_at','UPDATE'),
('j2m_web','TABLE','customer_credentials','revoked_at','UPDATE'),
('j2m_web','TABLE','customer_credentials','revoked_reason','UPDATE'),
('j2m_web','TABLE','customer_credentials','sign_count','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','','INSERT'),
('j2m_web','TABLE','customer_linking_enrollments','','SELECT'),
('j2m_web','TABLE','customer_linking_enrollments','approved_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','browser_binding_hash','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','claimed_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','consumed_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','pairing_code_hash','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','pairing_failed_attempts','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','pairing_locked_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','secret_hash','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','status','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','webauthn_challenge','UPDATE'),
('j2m_web','TABLE','customer_recovery_rate_limits','','INSERT'),
('j2m_web','TABLE','customer_recovery_rate_limits','','SELECT'),
('j2m_web','TABLE','customer_recovery_rate_limits','reserved_until','UPDATE'),
('j2m_web','TABLE','customer_recovery_rate_limits','updated_at','UPDATE'),
('j2m_web','TABLE','customer_registration_identity_claims','','INSERT'),
('j2m_web','TABLE','customer_registration_identity_claims','','SELECT'),
('j2m_web','TABLE','customer_sessions','','INSERT'),
('j2m_web','TABLE','customer_sessions','','SELECT'),
('j2m_web','TABLE','customer_sessions','last_active_at','UPDATE'),
('j2m_web','TABLE','customer_sessions','revoked_at','UPDATE'),
('j2m_web','TABLE','customers','','INSERT'),
('j2m_web','TABLE','customers','','SELECT'),
('j2m_web','TABLE','customers','current_points','UPDATE'),
('j2m_web','TABLE','customers','current_stamps','UPDATE'),
('j2m_web','TABLE','customers','last_earn_at','UPDATE'),
('j2m_web','TABLE','google_wallet_sync_jobs','','SELECT'),
('j2m_web','TABLE','loyalty_ledger_entries','','INSERT'),
('j2m_web','TABLE','loyalty_ledger_entries','','SELECT'),
('j2m_web','TABLE','platform_admins','','SELECT'),
('j2m_web','TABLE','platform_admins','name','UPDATE'),
('j2m_web','TABLE','promoter_sessions','','INSERT'),
('j2m_web','TABLE','promoter_sessions','','SELECT'),
('j2m_web','TABLE','promoter_sessions','revoked_at','UPDATE'),
('j2m_web','TABLE','promoters','','SELECT'),
('j2m_web','TABLE','referral_attributions','','INSERT'),
('j2m_web','TABLE','referral_attributions','id','SELECT'),
('j2m_web','TABLE','referral_eligibilities','','INSERT'),
('j2m_web','TABLE','referral_eligibilities','','SELECT'),
('j2m_web','TABLE','referral_incentive_rule_audits','','INSERT'),
('j2m_web','TABLE','referral_incentive_rule_audits','','SELECT'),
('j2m_web','TABLE','referral_incentive_rules','','INSERT'),
('j2m_web','TABLE','referral_incentive_rules','','SELECT'),
('j2m_web','TABLE','referral_incentive_rules','beneficiary_type','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','eligible_condition','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','loyalty_program','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','period_limit','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','period_unit','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','reward_amount','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','reward_type','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','status','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','updated_at','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','updated_by_id','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','updated_by_type','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','valid_from','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','valid_until','UPDATE'),
('j2m_web','TABLE','referral_incentive_rules','version','UPDATE'),
('j2m_web','TABLE','referral_participants','','INSERT'),
('j2m_web','TABLE','referral_participants','','SELECT'),
('j2m_web','TABLE','referral_participants','updated_at','UPDATE'),
('j2m_web','TABLE','staff','','INSERT'),
('j2m_web','TABLE','staff','','SELECT'),
('j2m_web','TABLE','staff','email','UPDATE'),
('j2m_web','TABLE','staff','name','UPDATE'),
('j2m_web','TABLE','staff','phone','UPDATE'),
('j2m_web','TABLE','staff_sessions','','INSERT'),
('j2m_web','TABLE','staff_sessions','','SELECT'),
('j2m_web','TABLE','staff_sessions','revoked_at','UPDATE'),
('j2m_web','TABLE','transactions','','INSERT'),
('j2m_web','TABLE','transactions','','SELECT'),
('j2m_worker','TABLE','businesses','','SELECT'),
('j2m_worker','TABLE','customers','','SELECT'),
('j2m_worker','TABLE','google_wallet_publication_jobs','id','SELECT'),
('j2m_worker','TABLE','google_wallet_publication_jobs','locked_at','SELECT'),
('j2m_worker','TABLE','google_wallet_publication_jobs','status','SELECT'),
('j2m_worker','TABLE','google_wallet_sync_jobs','','SELECT'),
('j2m_worker','TABLE','google_wallet_sync_jobs','attempts','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','last_status_code','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','locked_at','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','next_attempt_at','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','status','UPDATE')),
             actual AS (SELECT role_name,kind,obj,col,priv FROM pg_temp.a3_catalog_acl
                WHERE role_name IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery') AND grantor=current_user AND NOT is_grantable)
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) OR EXISTS (SELECT FROM pg_temp.a3_catalog_acl WHERE role_name IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery')
                  AND (grantor<>current_user OR is_grantable))
      OR EXISTS (SELECT FROM pg_temp.a3_catalog_acl WHERE role_name='PUBLIC')
      OR EXISTS (
          SELECT FROM pg_proc p
          CROSS JOIN pg_roles r
          WHERE p.pronamespace='public'::regnamespace
            AND r.rolname IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery')
            AND (p.prorettype<>'trigger'::regtype OR p.prosecdef)
            AND has_function_privilege(r.oid,p.oid,'EXECUTE')
      )
      OR EXISTS (
        WITH keys(obj,policy) AS (VALUES ('apple_pass_credentials','j2m_a2_backup_read'),
('apple_pass_update_sequence','j2m_a2_backup_read'),
('apple_wallet_publications','j2m_a2_backup_read'),
('apple_wallet_push_jobs','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_web_insert'),
('customer_auth_audits','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_backup_read'),
('customer_auth_challenges','j2m_a2_web_insert'),
('customer_auth_challenges','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_web_update'),
('customer_credentials','j2m_a2_backup_read'),
('customer_credentials','j2m_a2_web_insert'),
('customer_credentials','j2m_a2_web_read'),
('customer_credentials','j2m_a2_web_update'),
('customer_linking_enrollments','j2m_a2_backup_read'),
('customer_linking_enrollments','j2m_a2_web_insert'),
('customer_linking_enrollments','j2m_a2_web_read'),
('customer_linking_enrollments','j2m_a2_web_update'),
('customer_referral_activations','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_web_insert'),
('customer_sessions','j2m_a2_web_read'),
('customer_sessions','j2m_a2_web_update'),
('google_wallet_publication_jobs','j2m_a2_backup_read'),
('google_wallet_publications','j2m_a2_backup_read'),
('referral_eligibilities','j2m_a2_backup_read'),
('referral_eligibilities','j2m_a3_insert'),
('referral_eligibilities','j2m_a3_select'),
('referral_eligibility_decisions','j2m_a2_backup_read'),
('referral_incentive_records','j2m_a2_backup_read'),
('referral_incentive_rule_audits','j2m_a2_backup_read'),
('referral_incentive_rule_audits','j2m_a3_insert'),
('referral_incentive_rule_audits','j2m_a3_select'),
('referral_incentive_rules','j2m_a2_backup_read'),
('referral_incentive_rules','j2m_a3_insert'),
('referral_incentive_rules','j2m_a3_select'),
('referral_incentive_rules','j2m_a3_update'),
('referral_participant_audits','j2m_a2_backup_read'),
('referral_participants','j2m_a2_backup_read'),
('referral_participants','j2m_a2_web_insert'),
('referral_participants','j2m_a2_web_read'),
('referral_participants','j2m_a3_update'),
('referral_program_audits','j2m_a2_backup_read'))
        SELECT FROM keys k JOIN pg_class c ON c.relname=k.obj AND c.relnamespace='public'::regnamespace
        WHERE NOT c.relrowsecurity OR c.relforcerowsecurity
      )
      OR EXISTS (
        WITH keys(obj,policy) AS (VALUES ('apple_pass_credentials','j2m_a2_backup_read'),
('apple_pass_update_sequence','j2m_a2_backup_read'),
('apple_wallet_publications','j2m_a2_backup_read'),
('apple_wallet_push_jobs','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_web_insert'),
('customer_auth_audits','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_backup_read'),
('customer_auth_challenges','j2m_a2_web_insert'),
('customer_auth_challenges','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_web_update'),
('customer_credentials','j2m_a2_backup_read'),
('customer_credentials','j2m_a2_web_insert'),
('customer_credentials','j2m_a2_web_read'),
('customer_credentials','j2m_a2_web_update'),
('customer_linking_enrollments','j2m_a2_backup_read'),
('customer_linking_enrollments','j2m_a2_web_insert'),
('customer_linking_enrollments','j2m_a2_web_read'),
('customer_linking_enrollments','j2m_a2_web_update'),
('customer_referral_activations','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_web_insert'),
('customer_sessions','j2m_a2_web_read'),
('customer_sessions','j2m_a2_web_update'),
('google_wallet_publication_jobs','j2m_a2_backup_read'),
('google_wallet_publications','j2m_a2_backup_read'),
('referral_eligibilities','j2m_a2_backup_read'),
('referral_eligibilities','j2m_a3_insert'),
('referral_eligibilities','j2m_a3_select'),
('referral_eligibility_decisions','j2m_a2_backup_read'),
('referral_incentive_records','j2m_a2_backup_read'),
('referral_incentive_rule_audits','j2m_a2_backup_read'),
('referral_incentive_rule_audits','j2m_a3_insert'),
('referral_incentive_rule_audits','j2m_a3_select'),
('referral_incentive_rules','j2m_a2_backup_read'),
('referral_incentive_rules','j2m_a3_insert'),
('referral_incentive_rules','j2m_a3_select'),
('referral_incentive_rules','j2m_a3_update'),
('referral_participant_audits','j2m_a2_backup_read'),
('referral_participants','j2m_a2_backup_read'),
('referral_participants','j2m_a2_web_insert'),
('referral_participants','j2m_a2_web_read'),
('referral_participants','j2m_a3_update'),
('referral_program_audits','j2m_a2_backup_read')),
        expected AS (
            SELECT obj,policy,polcmd,polpermissive,polroles,qual,with_check
            FROM pg_temp.a3_catalog_policy JOIN keys USING (obj,policy) WHERE reference
        ), actual AS (
            SELECT obj,policy,polcmd,polpermissive,polroles,qual,with_check
            FROM pg_temp.a3_catalog_policy WHERE NOT reference
        )
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) THEN RAISE EXCEPTION 'Unexpected A2/A3 ACL or policy set (before)'; END IF;
END;
$verify_before$;
DROP POLICY j2m_a3_insert ON public.referral_incentive_rule_audits;
DROP POLICY j2m_a3_select ON public.referral_incentive_rule_audits;
DROP POLICY j2m_a3_update ON public.referral_incentive_rules;
DROP POLICY j2m_a3_insert ON public.referral_incentive_rules;
DROP POLICY j2m_a3_select ON public.referral_incentive_rules;
DROP POLICY j2m_a3_insert ON public.referral_eligibilities;
DROP POLICY j2m_a3_select ON public.referral_eligibilities;
DROP POLICY j2m_a3_update ON public.referral_participants;
REVOKE SELECT (id) ON TABLE public.access_audits FROM j2m_web;
REVOKE UPDATE (revoked_at) ON TABLE public.promoter_sessions FROM j2m_web;
REVOKE SELECT (id) ON TABLE public.referral_attributions FROM j2m_web;
REVOKE UPDATE (beneficiary_type) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (eligible_condition) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (loyalty_program) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (period_limit) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (period_unit) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (reward_amount) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (reward_type) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (status) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (updated_at) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (updated_by_id) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (updated_by_type) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (valid_from) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (valid_until) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (version) ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE UPDATE (updated_at) ON TABLE public.referral_participants FROM j2m_web;
REVOKE UPDATE (email) ON TABLE public.staff FROM j2m_web;
REVOKE UPDATE (name) ON TABLE public.staff FROM j2m_web;
REVOKE UPDATE (phone) ON TABLE public.staff FROM j2m_web;
REVOKE SELECT (id) ON TABLE public.google_wallet_publication_jobs FROM j2m_worker;
REVOKE SELECT (locked_at) ON TABLE public.google_wallet_publication_jobs FROM j2m_worker;
REVOKE SELECT (status) ON TABLE public.google_wallet_publication_jobs FROM j2m_worker;
REVOKE USAGE ON SEQUENCE public.access_audits_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.business_categories_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.businesses_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.referral_attributions_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.referral_eligibilities_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.referral_incentive_rule_audits_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.referral_incentive_rules_id_seq FROM j2m_web;
REVOKE USAGE ON SEQUENCE public.staff_id_seq FROM j2m_web;
REVOKE INSERT ON TABLE public.access_audits FROM j2m_web;
REVOKE INSERT ON TABLE public.business_categories FROM j2m_web;
REVOKE INSERT ON TABLE public.businesses FROM j2m_web;
REVOKE INSERT ON TABLE public.promoter_sessions FROM j2m_web;
REVOKE SELECT ON TABLE public.promoter_sessions FROM j2m_web;
REVOKE SELECT ON TABLE public.promoters FROM j2m_web;
REVOKE INSERT ON TABLE public.referral_attributions FROM j2m_web;
REVOKE INSERT ON TABLE public.referral_eligibilities FROM j2m_web;
REVOKE SELECT ON TABLE public.referral_eligibilities FROM j2m_web;
REVOKE INSERT ON TABLE public.referral_incentive_rule_audits FROM j2m_web;
REVOKE SELECT ON TABLE public.referral_incentive_rule_audits FROM j2m_web;
REVOKE INSERT ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE SELECT ON TABLE public.referral_incentive_rules FROM j2m_web;
REVOKE INSERT ON TABLE public.staff FROM j2m_web;

DO $verify_after$
BEGIN
    IF EXISTS (
        WITH expected(role_name,kind,obj,col,priv) AS (VALUES ('j2m_backup','SEQUENCE','access_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_devices_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_pass_credentials_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_pass_update_sequence_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_wallet_publications_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','apple_wallet_push_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','business_categories_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','businesses_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customer_auth_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customer_credentials_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customer_referral_activations_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','customers_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','google_wallet_publication_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','google_wallet_publications_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','google_wallet_sync_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','loyalty_ledger_entries_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','platform_admins_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','promoters_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_attributions_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_eligibilities_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_eligibility_decisions_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_incentive_records_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_incentive_rule_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_incentive_rules_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_participant_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_participants_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','referral_program_audits_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','registration_email_jobs_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','staff_id_seq','','SELECT'),
('j2m_backup','SEQUENCE','transactions_id_seq','','SELECT'),
('j2m_backup','TABLE','access_audits','','SELECT'),
('j2m_backup','TABLE','alembic_version','','SELECT'),
('j2m_backup','TABLE','apple_devices','','SELECT'),
('j2m_backup','TABLE','apple_pass_credentials','','SELECT'),
('j2m_backup','TABLE','apple_pass_update_sequence','','SELECT'),
('j2m_backup','TABLE','apple_wallet_publications','','SELECT'),
('j2m_backup','TABLE','apple_wallet_push_jobs','','SELECT'),
('j2m_backup','TABLE','auth_rate_limit_slots','','SELECT'),
('j2m_backup','TABLE','business_categories','','SELECT'),
('j2m_backup','TABLE','businesses','','SELECT'),
('j2m_backup','TABLE','customer_auth_audits','','SELECT'),
('j2m_backup','TABLE','customer_auth_challenges','','SELECT'),
('j2m_backup','TABLE','customer_credentials','','SELECT'),
('j2m_backup','TABLE','customer_linking_enrollments','','SELECT'),
('j2m_backup','TABLE','customer_recovery_authorizations','','SELECT'),
('j2m_backup','TABLE','customer_recovery_challenges','','SELECT'),
('j2m_backup','TABLE','customer_recovery_rate_limits','','SELECT'),
('j2m_backup','TABLE','customer_referral_activations','','SELECT'),
('j2m_backup','TABLE','customer_registration_challenges','','SELECT'),
('j2m_backup','TABLE','customer_registration_identity_claims','','SELECT'),
('j2m_backup','TABLE','customer_sessions','','SELECT'),
('j2m_backup','TABLE','customers','','SELECT'),
('j2m_backup','TABLE','google_wallet_publication_jobs','','SELECT'),
('j2m_backup','TABLE','google_wallet_publications','','SELECT'),
('j2m_backup','TABLE','google_wallet_sync_jobs','','SELECT'),
('j2m_backup','TABLE','loyalty_ledger_entries','','SELECT'),
('j2m_backup','TABLE','platform_admins','','SELECT'),
('j2m_backup','TABLE','promoter_sessions','','SELECT'),
('j2m_backup','TABLE','promoters','','SELECT'),
('j2m_backup','TABLE','referral_attributions','','SELECT'),
('j2m_backup','TABLE','referral_eligibilities','','SELECT'),
('j2m_backup','TABLE','referral_eligibility_decisions','','SELECT'),
('j2m_backup','TABLE','referral_incentive_records','','SELECT'),
('j2m_backup','TABLE','referral_incentive_rule_audits','','SELECT'),
('j2m_backup','TABLE','referral_incentive_rules','','SELECT'),
('j2m_backup','TABLE','referral_participant_audits','','SELECT'),
('j2m_backup','TABLE','referral_participants','','SELECT'),
('j2m_backup','TABLE','referral_program_audits','','SELECT'),
('j2m_backup','TABLE','registration_email_jobs','','SELECT'),
('j2m_backup','TABLE','staff','','SELECT'),
('j2m_backup','TABLE','staff_sessions','','SELECT'),
('j2m_backup','TABLE','transactions','','SELECT'),
('j2m_web','SEQUENCE','customer_auth_audits_id_seq','','USAGE'),
('j2m_web','SEQUENCE','customer_credentials_id_seq','','USAGE'),
('j2m_web','SEQUENCE','customers_id_seq','','USAGE'),
('j2m_web','SEQUENCE','loyalty_ledger_entries_id_seq','','USAGE'),
('j2m_web','SEQUENCE','referral_participants_id_seq','','USAGE'),
('j2m_web','SEQUENCE','transactions_id_seq','','USAGE'),
('j2m_web','TABLE','alembic_version','','SELECT'),
('j2m_web','TABLE','auth_rate_limit_slots','','DELETE'),
('j2m_web','TABLE','auth_rate_limit_slots','','INSERT'),
('j2m_web','TABLE','auth_rate_limit_slots','','SELECT'),
('j2m_web','TABLE','auth_rate_limit_slots','reserved_until','UPDATE'),
('j2m_web','TABLE','auth_rate_limit_slots','updated_at','UPDATE'),
('j2m_web','TABLE','business_categories','','SELECT'),
('j2m_web','TABLE','businesses','','SELECT'),
('j2m_web','TABLE','businesses','name','UPDATE'),
('j2m_web','TABLE','businesses','referrals_enabled','UPDATE'),
('j2m_web','TABLE','customer_auth_audits','','INSERT'),
('j2m_web','TABLE','customer_auth_audits','','SELECT'),
('j2m_web','TABLE','customer_auth_challenges','','INSERT'),
('j2m_web','TABLE','customer_auth_challenges','','SELECT'),
('j2m_web','TABLE','customer_auth_challenges','consumed_at','UPDATE'),
('j2m_web','TABLE','customer_credentials','','INSERT'),
('j2m_web','TABLE','customer_credentials','','SELECT'),
('j2m_web','TABLE','customer_credentials','is_active','UPDATE'),
('j2m_web','TABLE','customer_credentials','last_used_at','UPDATE'),
('j2m_web','TABLE','customer_credentials','revoked_at','UPDATE'),
('j2m_web','TABLE','customer_credentials','revoked_reason','UPDATE'),
('j2m_web','TABLE','customer_credentials','sign_count','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','','INSERT'),
('j2m_web','TABLE','customer_linking_enrollments','','SELECT'),
('j2m_web','TABLE','customer_linking_enrollments','approved_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','browser_binding_hash','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','claimed_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','consumed_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','pairing_code_hash','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','pairing_failed_attempts','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','pairing_locked_at','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','secret_hash','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','status','UPDATE'),
('j2m_web','TABLE','customer_linking_enrollments','webauthn_challenge','UPDATE'),
('j2m_web','TABLE','customer_recovery_rate_limits','','INSERT'),
('j2m_web','TABLE','customer_recovery_rate_limits','','SELECT'),
('j2m_web','TABLE','customer_recovery_rate_limits','reserved_until','UPDATE'),
('j2m_web','TABLE','customer_recovery_rate_limits','updated_at','UPDATE'),
('j2m_web','TABLE','customer_registration_identity_claims','','INSERT'),
('j2m_web','TABLE','customer_registration_identity_claims','','SELECT'),
('j2m_web','TABLE','customer_sessions','','INSERT'),
('j2m_web','TABLE','customer_sessions','','SELECT'),
('j2m_web','TABLE','customer_sessions','last_active_at','UPDATE'),
('j2m_web','TABLE','customer_sessions','revoked_at','UPDATE'),
('j2m_web','TABLE','customers','','INSERT'),
('j2m_web','TABLE','customers','','SELECT'),
('j2m_web','TABLE','customers','current_points','UPDATE'),
('j2m_web','TABLE','customers','current_stamps','UPDATE'),
('j2m_web','TABLE','customers','last_earn_at','UPDATE'),
('j2m_web','TABLE','google_wallet_sync_jobs','','SELECT'),
('j2m_web','TABLE','loyalty_ledger_entries','','INSERT'),
('j2m_web','TABLE','loyalty_ledger_entries','','SELECT'),
('j2m_web','TABLE','platform_admins','','SELECT'),
('j2m_web','TABLE','platform_admins','name','UPDATE'),
('j2m_web','TABLE','referral_participants','','INSERT'),
('j2m_web','TABLE','referral_participants','','SELECT'),
('j2m_web','TABLE','staff','','SELECT'),
('j2m_web','TABLE','staff_sessions','','INSERT'),
('j2m_web','TABLE','staff_sessions','','SELECT'),
('j2m_web','TABLE','staff_sessions','revoked_at','UPDATE'),
('j2m_web','TABLE','transactions','','INSERT'),
('j2m_web','TABLE','transactions','','SELECT'),
('j2m_worker','TABLE','businesses','','SELECT'),
('j2m_worker','TABLE','customers','','SELECT'),
('j2m_worker','TABLE','google_wallet_sync_jobs','','SELECT'),
('j2m_worker','TABLE','google_wallet_sync_jobs','attempts','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','last_status_code','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','locked_at','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','next_attempt_at','UPDATE'),
('j2m_worker','TABLE','google_wallet_sync_jobs','status','UPDATE')),
             actual AS (SELECT role_name,kind,obj,col,priv FROM pg_temp.a3_catalog_acl
                WHERE role_name IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery') AND grantor=current_user AND NOT is_grantable)
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) OR EXISTS (SELECT FROM pg_temp.a3_catalog_acl WHERE role_name IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery')
                  AND (grantor<>current_user OR is_grantable))
      OR EXISTS (SELECT FROM pg_temp.a3_catalog_acl WHERE role_name='PUBLIC')
      OR EXISTS (
          SELECT FROM pg_proc p
          CROSS JOIN pg_roles r
          WHERE p.pronamespace='public'::regnamespace
            AND r.rolname IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery')
            AND (p.prorettype<>'trigger'::regtype OR p.prosecdef)
            AND has_function_privilege(r.oid,p.oid,'EXECUTE')
      )
      OR EXISTS (
        WITH keys(obj,policy) AS (VALUES ('apple_pass_credentials','j2m_a2_backup_read'),
('apple_pass_update_sequence','j2m_a2_backup_read'),
('apple_wallet_publications','j2m_a2_backup_read'),
('apple_wallet_push_jobs','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_web_insert'),
('customer_auth_audits','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_backup_read'),
('customer_auth_challenges','j2m_a2_web_insert'),
('customer_auth_challenges','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_web_update'),
('customer_credentials','j2m_a2_backup_read'),
('customer_credentials','j2m_a2_web_insert'),
('customer_credentials','j2m_a2_web_read'),
('customer_credentials','j2m_a2_web_update'),
('customer_linking_enrollments','j2m_a2_backup_read'),
('customer_linking_enrollments','j2m_a2_web_insert'),
('customer_linking_enrollments','j2m_a2_web_read'),
('customer_linking_enrollments','j2m_a2_web_update'),
('customer_referral_activations','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_web_insert'),
('customer_sessions','j2m_a2_web_read'),
('customer_sessions','j2m_a2_web_update'),
('google_wallet_publication_jobs','j2m_a2_backup_read'),
('google_wallet_publications','j2m_a2_backup_read'),
('referral_eligibilities','j2m_a2_backup_read'),
('referral_eligibility_decisions','j2m_a2_backup_read'),
('referral_incentive_records','j2m_a2_backup_read'),
('referral_incentive_rule_audits','j2m_a2_backup_read'),
('referral_incentive_rules','j2m_a2_backup_read'),
('referral_participant_audits','j2m_a2_backup_read'),
('referral_participants','j2m_a2_backup_read'),
('referral_participants','j2m_a2_web_insert'),
('referral_participants','j2m_a2_web_read'),
('referral_program_audits','j2m_a2_backup_read'))
        SELECT FROM keys k JOIN pg_class c ON c.relname=k.obj AND c.relnamespace='public'::regnamespace
        WHERE NOT c.relrowsecurity OR c.relforcerowsecurity
      )
      OR EXISTS (
        WITH keys(obj,policy) AS (VALUES ('apple_pass_credentials','j2m_a2_backup_read'),
('apple_pass_update_sequence','j2m_a2_backup_read'),
('apple_wallet_publications','j2m_a2_backup_read'),
('apple_wallet_push_jobs','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_backup_read'),
('customer_auth_audits','j2m_a2_web_insert'),
('customer_auth_audits','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_backup_read'),
('customer_auth_challenges','j2m_a2_web_insert'),
('customer_auth_challenges','j2m_a2_web_read'),
('customer_auth_challenges','j2m_a2_web_update'),
('customer_credentials','j2m_a2_backup_read'),
('customer_credentials','j2m_a2_web_insert'),
('customer_credentials','j2m_a2_web_read'),
('customer_credentials','j2m_a2_web_update'),
('customer_linking_enrollments','j2m_a2_backup_read'),
('customer_linking_enrollments','j2m_a2_web_insert'),
('customer_linking_enrollments','j2m_a2_web_read'),
('customer_linking_enrollments','j2m_a2_web_update'),
('customer_referral_activations','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_backup_read'),
('customer_sessions','j2m_a2_web_insert'),
('customer_sessions','j2m_a2_web_read'),
('customer_sessions','j2m_a2_web_update'),
('google_wallet_publication_jobs','j2m_a2_backup_read'),
('google_wallet_publications','j2m_a2_backup_read'),
('referral_eligibilities','j2m_a2_backup_read'),
('referral_eligibility_decisions','j2m_a2_backup_read'),
('referral_incentive_records','j2m_a2_backup_read'),
('referral_incentive_rule_audits','j2m_a2_backup_read'),
('referral_incentive_rules','j2m_a2_backup_read'),
('referral_participant_audits','j2m_a2_backup_read'),
('referral_participants','j2m_a2_backup_read'),
('referral_participants','j2m_a2_web_insert'),
('referral_participants','j2m_a2_web_read'),
('referral_program_audits','j2m_a2_backup_read')),
        expected AS (
            SELECT obj,policy,polcmd,polpermissive,polroles,qual,with_check
            FROM pg_temp.a3_catalog_policy JOIN keys USING (obj,policy) WHERE reference
        ), actual AS (
            SELECT obj,policy,polcmd,polpermissive,polroles,qual,with_check
            FROM pg_temp.a3_catalog_policy WHERE NOT reference
        )
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) THEN RAISE EXCEPTION 'Unexpected A2/A3 ACL or policy set (after)'; END IF;
END;
$verify_after$;

DO $external$
BEGIN
    IF EXISTS (
        (SELECT * FROM pg_temp.a3_catalog_acl WHERE role_name NOT IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery') EXCEPT SELECT * FROM pg_temp.a3_external)
        UNION ALL (SELECT * FROM pg_temp.a3_external EXCEPT SELECT * FROM pg_temp.a3_catalog_acl WHERE role_name NOT IN ('j2m_web','j2m_worker','j2m_backup','j2m_recovery'))
    ) THEN RAISE EXCEPTION 'External ACL changed'; END IF;
END;
$external$;
DROP VIEW pg_temp.a3_catalog_policy, pg_temp.a3_catalog_acl;
COMMIT;
