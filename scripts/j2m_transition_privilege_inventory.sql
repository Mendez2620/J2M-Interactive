-- Reproducible inventory for the isolated harness. No row data or function bodies.
WITH inspected_roles AS (
    SELECT * FROM pg_roles WHERE rolname IN
      ('j2m_owner', 'j2m_migrator', 'j2m_schema_web', 'j2m_schema_worker', 'j2m_backup')
), relations AS (
    SELECT c.* FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
)
SELECT jsonb_build_object(
    'roles', (SELECT jsonb_agg(jsonb_build_array(rolname, rolcanlogin, rolinherit, rolsuper,
                   rolcreaterole, rolcreatedb, rolreplication, rolbypassrls,
                   has_database_privilege(oid, current_database(), 'CONNECT'),
                   has_schema_privilege(oid, 'public', 'USAGE')) ORDER BY rolname)
              FROM inspected_roles),
    'membership', (SELECT jsonb_agg(jsonb_build_array(r.rolname, parent.rolname, m.admin_option)
                                    ORDER BY r.rolname, parent.rolname)
                   FROM pg_auth_members m JOIN inspected_roles r ON r.oid = m.member
                   JOIN pg_roles parent ON parent.oid = m.roleid),
    'schema', (SELECT jsonb_build_array(pg_get_userbyid(nspowner), nspacl)
               FROM pg_namespace WHERE oid = 'public'::regnamespace),
    'relations', (SELECT jsonb_agg(jsonb_build_array(relname, relkind, pg_get_userbyid(relowner),
                                    relacl, relrowsecurity, relforcerowsecurity) ORDER BY relname)
                  FROM relations),
    'columns', (SELECT jsonb_agg(jsonb_build_array(r.relname, a.attname, a.attacl)
                                 ORDER BY r.relname, a.attname)
                FROM pg_attribute a JOIN relations r ON r.oid = a.attrelid
                WHERE a.attnum > 0 AND NOT a.attisdropped AND a.attacl IS NOT NULL),
    'functions', (SELECT jsonb_agg(jsonb_build_array(proname, pg_get_function_identity_arguments(oid),
                                    pg_get_userbyid(proowner), proacl, prosecdef, proconfig)
                                    ORDER BY proname, pg_get_function_identity_arguments(oid))
                  FROM pg_proc WHERE pronamespace = 'public'::regnamespace),
    'defaults', (SELECT jsonb_agg(jsonb_build_array(pg_get_userbyid(defaclrole), defaclobjtype,
                                   defaclnamespace = 0, defaclacl)
                                   ORDER BY pg_get_userbyid(defaclrole), defaclobjtype, defaclnamespace)
                 FROM pg_default_acl WHERE defaclnamespace IN (0, 'public'::regnamespace)),
    'triggers', (SELECT jsonb_agg(jsonb_build_array(r.relname, t.tgname, t.tgenabled,
                                   t.tgfoid::regprocedure::text) ORDER BY r.relname, t.tgname)
                 FROM pg_trigger t JOIN relations r ON r.oid = t.tgrelid WHERE NOT t.tgisinternal),
    'policies', (SELECT jsonb_agg(to_jsonb(p) ORDER BY tablename, policyname)
                 FROM pg_policies p WHERE schemaname = 'public')
);
