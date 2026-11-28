import json
from pathlib import Path
import unittest

from scripts.check_transition_security_catalog import verify_historical_columns
from scripts.historical_transition_evidence import FINAL_HEAD, INITIAL_HEAD


class HistoricalColumnTests(unittest.TestCase):
    def test_exact_historical_columns_at_both_heads(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] /
                               "scripts/j2m_historical_column_manifest.json").read_text(encoding="utf-8"))
        origin = {f"column:{table}.{column}": fields
                  for table, columns in manifest["tables"].items() for column, fields in columns.items()}
        self.assertEqual(len(origin), 493)
        for head in (INITIAL_HEAD, FINAL_HEAD):
            catalog = origin.copy()
            if head == FINAL_HEAD:
                catalog["column:google_wallet_sync_jobs.claim_token"] = ["character varying(32)", False, "", "", None, None]
            verify_historical_columns(head, catalog)
            key = "column:customers.id"
            for label, row in (("missing", None), ("wrong type", ["bigint", *origin[key][1:]]),
                               ("wrong nullable", [origin[key][0], False, *origin[key][2:]]),
                               ("wrong default", [*origin[key][:4], "1", None])):
                with self.subTest(head=head, drift=label):
                    changed = catalog.copy()
                    changed.pop(key)
                    if row is not None:
                        changed[key] = row
                    with self.assertRaisesRegex(ValueError, "Historical column drift"):
                        verify_historical_columns(head, changed)
            with self.assertRaises(ValueError):
                verify_historical_columns(head, {**catalog, "column:customers.unexpected": origin[key]})
