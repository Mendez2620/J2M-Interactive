"""Independent role membership contract for the disposable transition fixture."""

EXPECTED_MEMBERSHIPS = {
    "membership:j2m_migrator:j2m_owner:postgres": [False, False, True],
    "effective-membership:j2m_migrator:j2m_owner": [True, False, True],
    "effective-membership:j2m_owner:pg_database_owner": [True, True, True],
    "effective-membership:j2m_migrator:pg_database_owner": [True, False, True],
}


def verify_fixture_memberships(catalog):
    """Reject undeclared incoming and outgoing J2M role memberships."""
    observed = {}
    for key, value in catalog.items():
        kind, _, identities = key.partition(":")
        if kind not in {"membership", "effective-membership"}:
            continue
        # Check both ends, not just the member. An unrelated login granted
        # j2m_owner or a service role is also a path into J2M privileges.
        if any(identity.startswith("j2m_") for identity in identities.split(":")):
            observed[key] = value
    if observed != EXPECTED_MEMBERSHIPS:
        raise ValueError("Unexpected fixture role membership")
