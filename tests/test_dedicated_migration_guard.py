import unittest

from sqlalchemy.engine import make_url

from scripts.run_certified_migration import checked_environment


class DedicatedMigrationGuardTests(unittest.TestCase):
    def test_only_synthetic_migrator_can_run(self):
        config = {
            "GOOGLE_WALLET_MODE": "disabled",
            "APPLE_WALLET_MODE": "disabled",
            "DATABASE_SSLMODE": "verify-full",
        }
        environment = {"J2M_PROCESS_TYPE": "migrator"}
        url = make_url(
            "postgresql://j2m_migrator@db/full_schema"
            "?sslmode=verify-full&sslrootcert=/run/j2m/supabase-ca.pem"
        )
        self.assertTrue(checked_environment(environment, config, url))
        for changes in (
            {"host": "external"}, {"database": "postgres"}, {"username": "j2m_owner"},
            {"query": {"sslmode": "require"}}, {"port": 6432},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(checked_environment(environment, config, url.set(**changes)))
        for process in ("web", "google-wallet-worker"):
            self.assertFalse(checked_environment({"J2M_PROCESS_TYPE": process}, config, url))
        self.assertFalse(checked_environment({**environment, "PGOPTIONS": "-c role=j2m_owner"}, config, url))
        self.assertFalse(checked_environment(environment, {**config, "GOOGLE_WALLET_MODE": "real"}, url))
