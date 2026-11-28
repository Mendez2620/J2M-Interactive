"""Regression tests for incoming as well as outgoing role membership drift."""
from copy import deepcopy
import unittest

from scripts.transition_membership_manifest import (
    EXPECTED_MEMBERSHIPS, verify_fixture_memberships,
)


class TransitionMembershipTests(unittest.TestCase):
    def test_approved_memberships_and_unrelated_roles(self):
        catalog = deepcopy(EXPECTED_MEMBERSHIPS)
        catalog["membership:unrelated:other:postgres"] = [False, True, True]
        catalog["role:j2m_owner"] = []
        verify_fixture_memberships(catalog)

    def test_rejects_incoming_memberships_from_unrelated_login(self):
        for role in ("j2m_owner", "j2m_backup", "j2m_migrator", "j2m_schema_web", "j2m_schema_worker"):
            for kind, suffix, value in (
                ("membership", ":postgres", [False, True, True]),
                ("effective-membership", "", [True, True, True]),
            ):
                with self.subTest(role=role, kind=kind):
                    catalog = deepcopy(EXPECTED_MEMBERSHIPS)
                    catalog[f"{kind}:unexpected_login:{role}{suffix}"] = value
                    with self.assertRaisesRegex(ValueError, "role membership"):
                        verify_fixture_memberships(catalog)

    def test_rejects_unexpected_outgoing_membership(self):
        catalog = deepcopy(EXPECTED_MEMBERSHIPS)
        catalog["membership:j2m_backup:unrelated:postgres"] = [False, True, True]
        with self.assertRaises(ValueError):
            verify_fixture_memberships(catalog)

    def test_rejects_missing_or_changed_approved_membership(self):
        key = "membership:j2m_migrator:j2m_owner:postgres"
        for change in ("missing", "admin", "inherit", "set"):
            with self.subTest(change=change):
                catalog = deepcopy(EXPECTED_MEMBERSHIPS)
                if change == "missing":
                    del catalog[key]
                else:
                    index = {"admin": 0, "inherit": 1, "set": 2}[change]
                    catalog[key][index] = not catalog[key][index]
                with self.assertRaises(ValueError):
                    verify_fixture_memberships(catalog)
