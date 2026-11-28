import unittest

from scripts.check_transition_relation_manifest import expected_relations
from scripts.check_transition_security_catalog import verify_serial_definitions


class SerialDefinitionTests(unittest.TestCase):
    def test_catalog_must_preserve_definition_default_and_ownership(self):
        for head, count in (("9d0e1f2a3b4c", 30), ("c3d4e5f6a7b8", 31)):
            catalog = {}
            for relation in sorted(expected_relations(head)):
                if not relation.startswith("S:"):
                    continue
                name = relation[2:]
                table = name.removesuffix("_id_seq")
                catalog[f"sequence:{name}"] = [1, 1, 2147483647, 1, 1, False, table, "id", "a"]
                catalog[f"column:{table}.id"] = ["integer", True, "", "", f"nextval('{name}'::regclass)", None]
            with self.subTest(head=head):
                self.assertEqual(len(catalog), 2 * count)
                verify_serial_definitions(head, catalog)
                for key, field, value in (
                    ("sequence:customers_id_seq", 1, 2),
                    ("sequence:customers_id_seq", 6, "businesses"),
                    ("column:customers.id", 4, None),
                ):
                    baseline = catalog[key][field]
                    catalog[key][field] = value
                    with self.assertRaises(ValueError):
                        verify_serial_definitions(head, catalog)
                    catalog[key][field] = baseline
