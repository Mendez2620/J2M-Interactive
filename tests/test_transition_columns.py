import unittest
from copy import deepcopy

from scripts.check_transition_security_catalog import TRANSITION_COLUMNS, verify_transition_columns


class TransitionColumnTests(unittest.TestCase):
    def test_source_has_no_transition_columns(self):
        verify_transition_columns("9d0e1f2a3b4c", {})
        with self.assertRaises(ValueError):
            verify_transition_columns("9d0e1f2a3b4c", {"column:google_wallet_sync_jobs.claim_token": []})

    def test_target_rejects_definition_drift(self):
        observed = {
            f"column:{table}.{name}": [kind, required, "", "", default, None]
            for table, columns in TRANSITION_COLUMNS.items()
            for name, (kind, required, default) in columns.items()
        }
        observed["column:google_wallet_sync_jobs.claim_token"] = ["character varying(32)", False, "", "", None, None]
        verify_transition_columns("c3d4e5f6a7b8", observed)
        for field, value in ((0, "text"), (1, False), (4, None)):
            with self.subTest(field=field):
                changed = deepcopy(observed)
                changed["column:google_wallet_referral_jobs.status"][field] = value
                with self.assertRaises(ValueError):
                    verify_transition_columns("c3d4e5f6a7b8", changed)
