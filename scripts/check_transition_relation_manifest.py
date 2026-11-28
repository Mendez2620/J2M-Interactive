"""Reconcile J2M relation names against the declared legacy and target schema."""

import json
from pathlib import Path


RELATION_QUERY = """
SELECT COALESCE(json_agg(relkind::text || ':' || relname ORDER BY relkind, relname), '[]'::json)
FROM pg_class c
WHERE relnamespace='public'::regnamespace AND relkind IN ('r','p','S','v','m','f')
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend d
    WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'
  )
"""


def expected_relations(revision):
    manifest = json.loads(Path(__file__).with_name("j2m_transition_relation_manifest.json").read_text(encoding="utf-8"))
    expected = set(manifest["legacy"])
    if revision == manifest["target"]:
        expected.update(manifest["additions"])
    elif revision != manifest["initial"]:
        raise ValueError("Unsupported revision")
    return expected


def verify_relation_manifest(revision, observed):
    expected = expected_relations(revision)
    actual = set(observed)
    if len(actual) != len(observed) or actual != expected:
        raise ValueError(
            f"Relation drift: missing={len(expected - actual)}, unexpected={len(actual - expected)}"
        )
