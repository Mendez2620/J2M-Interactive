"""Focused regressions for source-derived routine declarations."""
from copy import deepcopy
import unittest

from scripts.transition_routine_manifest import (
    ROUTINE_ATTRIBUTES, ROUTINE_DEFINITIONS, verify_routine_definitions,
)


def catalog():
    return {name: ["j2m_owner", False, None, "deparsed definition", "\n" + body + "\n",
                   deepcopy(settings), ROUTINE_ATTRIBUTES.copy()]
            for name, (body, settings) in ROUTINE_DEFINITIONS.items()}


class RoutineManifestTests(unittest.TestCase):
    def test_declared_routines(self):
        verify_routine_definitions(catalog())

    def test_rejects_changed_body_or_context(self):
        name = "prevent_customer_auth_audit_mutation()"
        for field, value in ((0, "postgres"), (1, True), (2, ["=X/j2m_owner"]),
                             (4, "BEGIN RETURN NEW; END;"),
                             (5, ["search_path=public"]), (6, ["sql"])):
            with self.subTest(field=field):
                observed = catalog()
                observed[name][field] = value
                with self.assertRaisesRegex(ValueError, "routine definition"):
                    verify_routine_definitions(observed)

    def test_rejects_missing_extra_or_truncated_routines(self):
        name = "prevent_customer_auth_audit_mutation()"
        for change in ("missing", "extra", "truncated"):
            with self.subTest(change=change):
                observed = catalog()
                if change == "missing":
                    observed.pop(name)
                elif change == "extra":
                    observed["unexpected()"] = observed[name]
                else:
                    observed[name] = observed[name][:4]
                with self.assertRaises(ValueError):
                    verify_routine_definitions(observed)

    def test_rejects_changed_tenant_predicate(self):
        observed = catalog()
        name = "validate_referral_incentive_record_subject()"
        observed[name][4] = observed[name][4].replace(
            "p.business_id = e.business_id AND p.id", "p.id", 1)
        with self.assertRaises(ValueError):
            verify_routine_definitions(observed)


if __name__ == "__main__":
    unittest.main()
