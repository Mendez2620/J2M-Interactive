import importlib
import os
import unittest
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.services.google_wallet_write_reservation import Reservation, reserve, transition


class WalletReservationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        self.initialize_schema()

    def initialize_schema(self):
        migration = importlib.import_module(
            "migrations.versions.a1b2c3d4e5f6_wallet_write_reservations"
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE businesses (id integer PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE customers (id integer PRIMARY KEY, business_id integer NOT NULL, "
                "UNIQUE (business_id, id))"
            )
            connection.exec_driver_sql("INSERT INTO businesses VALUES (1), (2)")
            connection.exec_driver_sql("INSERT INTO customers VALUES (11, 1), (22, 2)")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        self.resource = dict(kind="loyaltyObject", issuer="issuer", resource_id="issuer.business_1_customer_11",
                             business_id=1, customer_id=11)

    def test_uncertainty_blocks_other_operations_until_owner_completes(self):
        owner = reserve(self.engine, **self.resource)
        self.assertTrue(transition(self.engine, owner, previous="reserved", state="started"))
        self.assertTrue(transition(self.engine, owner, previous="started", state="uncertain"))
        self.assertIsNone(reserve(self.engine, **self.resource))
        other = reserve(self.engine, **dict(self.resource, resource_id="issuer.business_2_customer_22",
                                            business_id=2, customer_id=22))
        self.assertIsNotNone(other)
        forged = Reservation(owner.operation_id, other.owner_token)
        self.assertFalse(transition(self.engine, forged, previous="uncertain", state="confirmed"))
        self.assertTrue(transition(self.engine, owner, previous="uncertain", state="confirmed"))
        self.assertIsNone(reserve(self.engine, **self.resource))
        self.assertTrue(transition(self.engine, owner, previous="confirmed", state="completed"))
        self.assertNotEqual(reserve(self.engine, **self.resource).operation_id, owner.operation_id)

    def test_rejects_cross_tenant_customer(self):
        with self.assertRaises(ValueError):
            reserve(self.engine, **dict(self.resource, customer_id=22,
                                        resource_id="issuer.business_1_customer_22"))

    def test_rejects_noncanonical_resource_and_missing_business(self):
        with self.assertRaisesRegex(ValueError, "resource"):
            reserve(self.engine, **dict(self.resource, resource_id="issuer.other"))
        with self.assertRaisesRegex(ValueError, "ownership"):
            reserve(self.engine, kind="loyaltyClass", issuer="issuer",
                    resource_id="issuer.business_3", business_id=3)

    def test_downgrade_preserves_reservation_history(self):
        migration = importlib.import_module(
            "migrations.versions.a1b2c3d4e5f6_wallet_write_reservations"
        )
        owner = reserve(self.engine, **self.resource)
        self.assertTrue(transition(self.engine, owner, previous="reserved", state="not_sent"))
        with self.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with self.assertRaisesRegex(RuntimeError, "history prevents downgrade"):
                    migration.downgrade()
            self.assertEqual(connection.exec_driver_sql(
                "SELECT COUNT(*) FROM google_wallet_write_reservations"
            ).scalar_one(), 1)


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "synthetic PostgreSQL not configured")
class PostgresWalletReservationTests(WalletReservationTests):
    def setUp(self):
        url = make_url(os.environ["TEST_POSTGRES_URL"]).set(drivername="postgresql+psycopg2")
        if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "j2m_test" or url.query:
            self.fail("Reservation regression requires local synthetic PostgreSQL")
        schema = "wallet_reservation_" + uuid4().hex
        control = create_engine(url, poolclass=NullPool)
        self.addCleanup(control.dispose)
        with control.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
        self.addCleanup(self.drop_schema, control, schema)
        self.engine = create_engine(url, poolclass=NullPool,
                                    connect_args={"options": f"-csearch_path={schema}"})
        self.addCleanup(self.engine.dispose)
        self.initialize_schema()

    @staticmethod
    def drop_schema(engine, schema):
        with engine.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")

    def test_terminated_advisory_session_does_not_release_started_operation(self):
        owner = reserve(self.engine, **self.resource)
        with self.engine.connect() as original:
            pid = original.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            original.exec_driver_sql("SELECT pg_advisory_lock(1246907724, 11)")
            self.assertTrue(transition(self.engine, owner, previous="reserved", state="started"))
            with self.engine.begin() as killer:
                self.assertTrue(killer.exec_driver_sql(
                    "SELECT pg_terminate_backend(%s, 5000)", (pid,)
                ).scalar_one())
            original.invalidate()
        with self.engine.connect() as successor:
            self.assertTrue(successor.exec_driver_sql(
                "SELECT pg_try_advisory_lock(1246907724, 11)"
            ).scalar_one())
            self.assertIsNone(reserve(self.engine, **self.resource))
        self.assertTrue(transition(self.engine, owner, previous="started", state="confirmed"))
        self.assertTrue(transition(self.engine, owner, previous="confirmed", state="completed"))
        self.assertIsNotNone(reserve(self.engine, **self.resource))
