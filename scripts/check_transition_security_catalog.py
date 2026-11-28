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
# Declarative privileges for the migrated schema before fixture service grants.
# Source: Alembic DDL plus the explicit SELECT-only fixture backup grants.
LEGACY_RLS = frozenset("""
apple_pass_update_sequence apple_pass_credentials apple_wallet_publications apple_wallet_push_jobs
customer_linking_enrollments customer_credentials customer_sessions customer_auth_challenges customer_auth_audits
customer_referral_activations google_wallet_publications google_wallet_publication_jobs
referral_eligibilities referral_eligibility_decisions referral_incentive_rules
referral_incentive_rule_audits referral_incentive_records referral_participants
referral_participant_audits referral_program_audits
""".split())
TARGET_RLS_ADDITIONS = frozenset("""
admin_wallet_reviews google_wallet_referral_jobs google_wallet_write_reservations platform_admin_sessions
""".split())
ROUTINES = frozenset("""
prevent_referral_decision_mutation() prevent_referral_incentive_history_mutation()
validate_referral_incentive_record_subject() prevent_referral_participant_audit_mutation()
prevent_referral_program_audit_mutation() prevent_customer_auth_audit_mutation()
""".split())


def verify_fixture_security(revision, catalog):
    """Check source-derived security invariants, independent of before/after drift checks."""
    from scripts.check_transition_relation_manifest import verify_relation_manifest

    relations = {key.removeprefix("relation:"): row for key, row in catalog.items()
                 if key.startswith("relation:")}
    verify_relation_manifest(revision, [f"{row[0]}:{name}" for name, row in relations.items()])
    expected_rls = LEGACY_RLS
    if revision == "c3d4e5f6a7b8":
        expected_rls |= TARGET_RLS_ADDITIONS
    actual_rls = {name for name, row in relations.items() if row[2]}
    if actual_rls != expected_rls:
        raise ValueError("Unexpected row-level security matrix")
    for kind, owner, _, force_rls, acl in relations.values():
        granted = [] if acl is None else [grant for grant in acl if not grant.startswith("j2m_owner=")]
        if kind not in {"r", "S"} or owner != "j2m_owner" or force_rls or granted != ["j2m_backup=r/j2m_owner"]:
            raise ValueError("Unexpected relation owner or direct ACL")
    for key, row in catalog.items():
        if key.startswith("column:") and row[-1]:
            raise ValueError("Unexpected column grant before service provisioning")
        if key.startswith("default-acl:"):
            raise ValueError("Unexpected fixture default grant")
    routines = {key.removeprefix("routine:"): row for key, row in catalog.items()
                if key.startswith("routine:")}
    if routines.keys() != ROUTINES or any(row[0] != "j2m_owner" or row[1] for row in routines.values()):
        raise ValueError("Unexpected routine owner, identity or execution context")
    membership = {key: value for key, value in catalog.items()
                  if key.startswith(("membership:j2m_", "effective-membership:j2m_"))}
    if membership != {
        "membership:j2m_migrator:j2m_owner:postgres": [False, False, True],
        "effective-membership:j2m_migrator:j2m_owner": [True, False, True],
        "effective-membership:j2m_owner:pg_database_owner": [True, True, True],
        "effective-membership:j2m_migrator:pg_database_owner": [True, False, True],
    }:
        raise ValueError("Unexpected fixture role membership")
