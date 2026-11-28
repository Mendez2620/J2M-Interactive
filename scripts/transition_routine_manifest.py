"""Independent routine declarations from the Alembic upgrade DDL for both heads."""

# Keep the stored function body intact, except surrounding whitespace. Do not
# collapse internal whitespace or literals: that could mask a changed body.
ROUTINE_DEFINITIONS = {
    # 1b2c3d4e5f6a: upgrade DDL.
    "prevent_referral_decision_mutation()": (
        "BEGIN\n"
        "                RAISE EXCEPTION 'referral eligibility decisions are append-only';\n"
        "            END;",
        None,
    ),
    # 2c3d4e5f6a7b: upgrade DDL.
    "prevent_referral_incentive_history_mutation()": (
        "BEGIN\n"
        "                RAISE EXCEPTION 'referral incentive history is append-only';\n"
        "            END;",
        None,
    ),
    # 3d4e5f6a7b8c: upgrade DDL.
    "prevent_referral_participant_audit_mutation()": (
        "BEGIN\n"
        "                RAISE EXCEPTION 'referral participant audit is append-only';\n"
        "            END;",
        ["search_path=pg_catalog, public"],
    ),
    # 3d4e5f6a7b8c: upgrade DDL.
    "validate_referral_incentive_record_subject()": (
        "BEGIN\n"
        "            IF NOT EXISTS (\n"
        "                SELECT 1\n"
        "                FROM referral_eligibilities e\n"
        "                JOIN referral_participants p\n"
        "                  ON p.business_id = e.business_id AND p.id = e.participant_id\n"
        "                WHERE e.business_id = NEW.business_id\n"
        "                  AND e.id = NEW.eligibility_id\n"
        "                  AND e.attribution_id = NEW.attribution_id\n"
        "                  AND e.source_transaction_id = NEW.source_transaction_id\n"
        "                  AND p.participant_type = NEW.beneficiary_type\n"
        "                  AND CASE NEW.beneficiary_type\n"
        "                        WHEN 'external_promoter' THEN p.promoter_id\n"
        "                        WHEN 'staff' THEN p.staff_id\n"
        "                        WHEN 'customer_ambassador' THEN p.customer_id\n"
        "                      END = NEW.beneficiary_id\n"
        "            ) THEN RAISE EXCEPTION 'referral incentive subject mismatch'; END IF;\n"
        "            RETURN NEW;\n"
        "        END;",
        ["search_path=pg_catalog, public"],
    ),
    # 4e5f6a7b8c9d: upgrade DDL.
    "prevent_referral_program_audit_mutation()": (
        "BEGIN\n"
        "                RAISE EXCEPTION 'referral program audit is append-only';\n"
        "            END;",
        ["search_path=pg_catalog, public"],
    ),
    # 5f6a7b8c9d0e: upgrade DDL.
    "prevent_customer_auth_audit_mutation()": (
        "BEGIN RAISE EXCEPTION 'customer auth audit is append-only'; END;",
        ["search_path=pg_catalog, public"],
    ),
}

# Language, return type, kind, volatility, strictness, parallel safety,
# leakproof, set-returning, cost, rows, absence of a planner support function.
ROUTINE_ATTRIBUTES = ["plpgsql", "trigger", "f", "v", False, "u", False, False, 100, 0, True]


def verify_routine_definitions(routines):
    """Reject undeclared or changed routines without disclosing their bodies."""
    if routines.keys() != ROUTINE_DEFINITIONS.keys():
        raise ValueError("Unexpected routine identity")
    for name, row in routines.items():
        body, settings = ROUTINE_DEFINITIONS[name]
        if (not isinstance(row, list) or len(row) != 7
                or row[:3] != ["j2m_owner", False, None]
                or not isinstance(row[4], str) or row[4].strip() != body
                or row[5] != settings or row[6] != ROUTINE_ATTRIBUTES):
            raise ValueError("Unexpected routine definition or execution context")
