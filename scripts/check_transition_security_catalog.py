"""Collect a deterministic PostgreSQL catalog for synthetic J2M access checks."""

CATALOG_QUERY = """
WITH entries(kind, identity, detail) AS (
    SELECT 'relation', c.relname, jsonb_build_array(
        c.relkind, pg_get_userbyid(c.relowner), c.relrowsecurity,
        c.relforcerowsecurity, to_jsonb(c.relacl))
      FROM pg_class c WHERE c.relnamespace='public'::regnamespace
        AND c.relkind IN ('r','p','S','v','m','f')
    UNION ALL
    SELECT 'column', c.relname||'.'||a.attname, jsonb_build_array(
        format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,
        pg_get_expr(d.adbin,d.adrelid),to_jsonb(a.attacl))
      FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
      LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
     WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
       AND a.attnum>0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'sequence', c.relname, jsonb_build_array(
        s.seqstart,s.seqincrement,s.seqmax,s.seqmin,s.seqcache,s.seqcycle,
        ref.relname,a.attname,d.deptype)
      FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
      LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid
          AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
      LEFT JOIN pg_class ref ON ref.oid=d.refobjid
      LEFT JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
     WHERE c.relnamespace='public'::regnamespace
    UNION ALL
    SELECT 'routine', p.oid::regprocedure::text, jsonb_build_array(
        pg_get_userbyid(p.proowner),p.prosecdef,to_jsonb(p.proacl),pg_get_functiondef(p.oid))
      FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.prokind IN ('f','p','w')
    UNION ALL
    SELECT 'trigger', c.relname||'.'||t.tgname,
        jsonb_build_array(t.tgenabled,pg_get_triggerdef(t.oid))
      FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
     WHERE c.relnamespace='public'::regnamespace AND NOT t.tgisinternal
    UNION ALL
    SELECT 'policy', tablename||'.'||policyname, jsonb_build_array(permissive,roles,cmd,qual,with_check)
      FROM pg_policies WHERE schemaname='public'
    UNION ALL
    SELECT 'default-acl', pg_get_userbyid(d.defaclrole)||':'||
        coalesce(n.nspname,'global')||':'||d.defaclobjtype::text, to_jsonb(d.defaclacl)
      FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
     WHERE d.defaclnamespace IN (0,'public'::regnamespace)
    UNION ALL
    SELECT 'schema', nspname, jsonb_build_array(pg_get_userbyid(nspowner),to_jsonb(nspacl))
      FROM pg_namespace WHERE nspname='public'
    UNION ALL
    SELECT 'membership', pg_get_userbyid(member)||':'||pg_get_userbyid(roleid)||':'||
        pg_get_userbyid(grantor), jsonb_build_array(admin_option,inherit_option,set_option)
      FROM pg_auth_members
    UNION ALL
    SELECT 'effective-membership', a.rolname||':'||b.rolname, jsonb_build_array(
        pg_has_role(a.oid,b.oid,'MEMBER'), pg_has_role(a.oid,b.oid,'USAGE'),
        pg_has_role(a.oid,b.oid,'SET'))
      FROM pg_roles a CROSS JOIN pg_roles b
     WHERE a.oid<>b.oid AND NOT a.rolsuper AND pg_has_role(a.oid,b.oid,'MEMBER')
    UNION ALL
    SELECT 'role', rolname, jsonb_build_array(rolsuper,rolinherit,rolcreaterole,
        rolcreatedb,rolcanlogin,rolreplication,rolbypassrls,rolconnlimit)
      FROM pg_roles
)
SELECT coalesce(jsonb_object_agg(kind||':'||identity, detail), '{}'::jsonb) FROM entries;
"""


def verify_catalog(expected, observed):
    """Reject drift without printing ACLs, definitions or role names."""
    if not expected or not observed:
        raise ValueError("Missing security catalog")
    changed = sum(expected[key] != observed[key] for key in expected.keys() & observed.keys())
    missing = len(expected.keys() - observed.keys())
    unexpected = len(observed.keys() - expected.keys())
    if changed or missing or unexpected:
        raise ValueError(f"Security catalog drift: changed={changed}, missing={missing}, unexpected={unexpected}")
