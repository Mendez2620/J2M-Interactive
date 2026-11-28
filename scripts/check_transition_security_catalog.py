"""Collect a deterministic PostgreSQL catalog for synthetic J2M access checks."""

CATALOG_QUERY = """
WITH entries(kind, identity, detail) AS (
    SELECT 'relation', c.relname::text, jsonb_build_array(
        c.relkind, pg_get_userbyid(c.relowner), c.relrowsecurity,
        c.relforcerowsecurity, to_jsonb(c.relacl))
      FROM pg_class c WHERE c.relnamespace='public'::regnamespace
        AND c.relkind IN ('r','p','S','v','m','f')
    UNION ALL
    SELECT 'column', c.relname::text||'.'||a.attname::text, jsonb_build_array(
        format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,
        pg_get_expr(d.adbin,d.adrelid),to_jsonb(a.attacl))
      FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
      LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
     WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
       AND a.attnum>0 AND NOT a.attisdropped
    UNION ALL
    SELECT 'sequence', c.relname::text, jsonb_build_array(
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
        pg_get_userbyid(p.proowner),p.prosecdef,to_jsonb(p.proacl),pg_get_functiondef(p.oid),
        p.prosrc,to_jsonb(p.proconfig),jsonb_build_array(
            l.lanname,format_type(p.prorettype,NULL),p.prokind,p.provolatile,p.proisstrict,
            p.proparallel,p.proleakproof,p.proretset,p.procost,p.prorows,p.prosupport::oid=0))
      FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
     WHERE p.pronamespace='public'::regnamespace AND p.prokind IN ('f','p','w')
    UNION ALL
    SELECT 'trigger', c.relname::text||'.'||t.tgname::text,
        jsonb_build_array(t.tgenabled,t.tgtype,t.tgfoid::regprocedure::text,
                          t.tgqual IS NULL,pg_get_triggerdef(t.oid))
      FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
     WHERE c.relnamespace='public'::regnamespace AND NOT t.tgisinternal
    UNION ALL
    SELECT 'policy', tablename::text||'.'||policyname::text, jsonb_build_array(permissive,roles,cmd,qual,with_check)
      FROM pg_policies WHERE schemaname='public'
    UNION ALL
    SELECT 'default-acl', pg_get_userbyid(d.defaclrole)||':'||
        coalesce(n.nspname,'global')||':'||d.defaclobjtype::text, to_jsonb(d.defaclacl)
      FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
     WHERE d.defaclnamespace IN (0,'public'::regnamespace)
    UNION ALL
    SELECT 'schema', nspname::text, jsonb_build_array(pg_get_userbyid(nspowner),to_jsonb(nspacl))
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
    SELECT 'role', rolname::text, jsonb_build_array(rolsuper,rolinherit,rolcreaterole,
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

# PostgreSQL tgtype masks: BEFORE (2), EACH ROW (1), and event bits.
TRIGGERS = {
    "referral_eligibility_decisions.trg_referral_decisions_append_only":
        (27, "prevent_referral_decision_mutation()"),
    "referral_incentive_rule_audits.trg_referral_rule_audits_append_only":
        (27, "prevent_referral_incentive_history_mutation()"),
    "referral_incentive_records.trg_referral_incentive_records_append_only":
        (27, "prevent_referral_incentive_history_mutation()"),
    "referral_incentive_records.trg_referral_incentive_record_subject":
        (7, "validate_referral_incentive_record_subject()"),
    "referral_participant_audits.trg_referral_participant_audits_append_only":
        (27, "prevent_referral_participant_audit_mutation()"),
    "referral_program_audits.trg_referral_program_audits_append_only":
        (27, "prevent_referral_program_audit_mutation()"),
    "customer_auth_audits.trg_customer_auth_audits_append_only":
        (27, "prevent_customer_auth_audit_mutation()"),
}


# Independently declared from a1b2c3d4e5f6, b2c3d4e5f6a7 and c3d4e5f6a7b8.
# Each entry carries (PostgreSQL type, NOT NULL, canonical default).
TIMESTAMP = "timestamp without time zone"
TRANSITION_COLUMNS = {
    "google_wallet_write_reservations": {
        "operation_id": ("character varying(32)", True, None),
        "owner_token": ("character varying(32)", True, None),
        "issuer": ("character varying(128)", True, None),
        "kind": ("character varying(16)", True, None),
        "resource_id": ("character varying(255)", True, None),
        "business_id": ("integer", True, None),
        "customer_id": ("integer", False, None),
        "state": ("character varying(16)", True, None),
        "active_slot": ("integer", False, None),
        "created_at": (TIMESTAMP, True, "now()"),
        "updated_at": (TIMESTAMP, True, None),
        "started_at": (TIMESTAMP, False, None),
        "remote_resolved_at": (TIMESTAMP, False, None),
        "remote_status": ("integer", False, None),
    },
    "platform_admin_sessions": {
        "token_digest": ("character varying(64)", True, None),
        "admin_id": ("integer", True, None),
        "password_digest": ("character varying(64)", True, None),
        "expires_at": (TIMESTAMP, True, None),
        "revoked_at": (TIMESTAMP, False, None),
    },
    "admin_wallet_reviews": {
        "id": ("character varying(32)", True, None),
        "session_digest": ("character varying(64)", True, None),
        "business_id": ("integer", True, None),
        "candidate_ids": ("json", True, None),
        "snapshot": ("json", True, None),
        "fingerprint": ("character varying(64)", True, None),
        "created_at": (TIMESTAMP, True, None),
        "expires_at": (TIMESTAMP, True, None),
        "state": ("character varying(16)", True, "'prepared'::character varying"),
        "confirmed_at": (TIMESTAMP, False, None),
        "claimed_at": (TIMESTAMP, False, None),
        "owner": ("character varying(32)", False, None),
        "finished_at": (TIMESTAMP, False, None),
        "result": ("json", False, None),
    },
    "google_wallet_referral_jobs": {
        "id": ("integer", True, "nextval('google_wallet_referral_jobs_id_seq'::regclass)"),
        "business_id": ("integer", True, None),
        "customer_id": ("integer", False, None),
        "cursor": ("integer", True, "0"),
        "scheduled_expiry": ("boolean", True, "false"),
        "status": ("character varying(16)", True, "'pending'::character varying"),
        "token": ("character varying(32)", False, None),
        "attempts": ("integer", True, "0"),
        "run_at": (TIMESTAMP, True, None),
        "locked_at": (TIMESTAMP, False, None),
        "created_at": (TIMESTAMP, True, None),
        "updated_at": (TIMESTAMP, True, None),
        "outcome": ("character varying(24)", False, None),
    },
}


def verify_transition_columns(revision, catalog):
    if revision not in {"9d0e1f2a3b4c", "c3d4e5f6a7b8"}:
        raise ValueError("Unknown transition revision")
    prefixes = tuple(f"column:{table}." for table in TRANSITION_COLUMNS)
    claim = "column:google_wallet_sync_jobs.claim_token"
    expected = {}
    if revision == "c3d4e5f6a7b8":
        expected = {
            f"column:{table}.{column}": [type_, not_null, "", "", default, None]
            for table, columns in TRANSITION_COLUMNS.items()
            for column, (type_, not_null, default) in columns.items()
        }
        expected[claim] = ["character varying(32)", False, "", "", None, None]
    observed = {key: row for key, row in catalog.items() if key == claim or key.startswith(prefixes)}
    if observed != expected:
        raise ValueError("Unexpected transition column definition")


def verify_serial_definitions(revision, catalog):
    """Validate the SERIAL declarations emitted for the Alembic integer primary keys."""
    from scripts.check_transition_relation_manifest import expected_relations

    sequences = {
        relation.removeprefix("S:") for relation in expected_relations(revision)
        if relation.startswith("S:")
    }
    expected = {}
    for sequence in sequences:
        table = sequence.removesuffix("_id_seq")
        expected[f"sequence:{sequence}"] = [1, 1, 2147483647, 1, 1, False, table, "id", "a"]
        if catalog.get(f"column:{table}.id") != [
            "integer", True, "", "", f"nextval('{sequence}'::regclass)", None,
        ]:
            raise ValueError("Unexpected serial column definition")
    observed = {key: value for key, value in catalog.items() if key.startswith("sequence:")}
    if observed != expected:
        raise ValueError("Unexpected sequence definition or ownership dependency")


def verify_fixture_security(revision, catalog):
    """Check source-derived security invariants, independent of before/after drift checks."""
    from scripts.check_transition_relation_manifest import verify_relation_manifest

    relations = {key.removeprefix("relation:"): row for key, row in catalog.items()
                 if key.startswith("relation:")}
    verify_relation_manifest(revision, [f"{row[0]}:{name}" for name, row in relations.items()])
    verify_transition_columns(revision, catalog)
    verify_serial_definitions(revision, catalog)
    expected_rls = LEGACY_RLS
    if revision == "c3d4e5f6a7b8":
        expected_rls |= TARGET_RLS_ADDITIONS
    actual_rls = {name for name, row in relations.items() if row[2]}
    if actual_rls != expected_rls:
        raise ValueError("Unexpected row-level security matrix")
    for kind, owner, _, force_rls, acl in relations.values():
        owner_permissions = {"r": "arwdDxtm", "S": "rwU"}.get(kind)
        expected_acl = {f"j2m_owner={owner_permissions}/j2m_owner", "j2m_backup=r/j2m_owner"}
        if (owner_permissions is None or owner != "j2m_owner" or force_rls or acl is None
                or len(acl) != 2 or set(acl) != expected_acl):
            raise ValueError("Unexpected relation owner or direct ACL")
    policies = {key: row for key, row in catalog.items() if key.startswith("policy:")}
    expected_policies = {
        f"policy:{table}.synthetic_backup_coverage": ["PERMISSIVE", ["j2m_backup"], "SELECT", "true", None]
        for table in expected_rls
    }
    if policies != expected_policies:
        raise ValueError("Unexpected row-level policy definition")
    for role, inherit, login in (("j2m_owner", True, False), ("j2m_backup", True, True),
                                  ("j2m_migrator", False, True)):
        if catalog.get(f"role:{role}") != [False, inherit, False, False, login, False, False, -1]:
            raise ValueError("Unexpected fixture role attributes")
    for key, row in catalog.items():
        if key.startswith("column:") and row[-1]:
            raise ValueError("Unexpected column grant before service provisioning")
        if key.startswith("default-acl:"):
            raise ValueError("Unexpected fixture default grant")
    routines = {key.removeprefix("routine:"): row for key, row in catalog.items()
                if key.startswith("routine:")}
    if routines.keys() != ROUTINES or any(row[:3] != ["j2m_owner", False, None] for row in routines.values()):
        raise ValueError("Unexpected routine owner, identity or execution context")
    from scripts.transition_routine_manifest import verify_routine_definitions
    verify_routine_definitions(routines)
    triggers = {key.removeprefix("trigger:"): row[:4] for key, row in catalog.items()
                if key.startswith("trigger:")}
    if triggers != {name: ["O", mask, routine, True] for name, (mask, routine) in TRIGGERS.items()}:
        raise ValueError("Unexpected fixture trigger definition")
    from scripts.transition_membership_manifest import verify_fixture_memberships
    verify_fixture_memberships(catalog)
