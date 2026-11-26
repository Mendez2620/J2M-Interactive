import importlib
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from app.services.google_wallet_write_reservation import Reservation, reserve, transition


class WalletReservationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        migration = importlib.import_module(
            "migrations.versions.a1b2c3d4e5f6_wallet_write_reservations"
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE businesses (id integer PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE customers (id integer PRIMARY KEY, business_id integer NOT NULL)"
            )
            connection.exec_driver_sql("INSERT INTO businesses VALUES (1), (2)")
            connection.exec_driver_sql("INSERT INTO customers VALUES (11, 1), (22, 2)")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        self.resource = dict(kind="loyaltyObject", issuer="issuer", resource_id="issuer.card",
                             business_id=1, customer_id=11)

    def test_uncertainty_blocks_other_operations_until_owner_completes(self):
        owner = reserve(self.engine, **self.resource)
        self.assertTrue(transition(self.engine, owner, previous="reserved", state="started"))
        self.assertTrue(transition(self.engine, owner, previous="started", state="uncertain"))
        self.assertIsNone(reserve(self.engine, **self.resource))
        other = reserve(self.engine, **dict(self.resource, resource_id="issuer.other"))
        self.assertIsNotNone(other)
        forged = Reservation(owner.operation_id, other.owner_token)
        self.assertFalse(transition(self.engine, forged, previous="uncertain", state="confirmed"))
        self.assertTrue(transition(self.engine, owner, previous="uncertain", state="confirmed"))
        self.assertIsNone(reserve(self.engine, **self.resource))
        self.assertTrue(transition(self.engine, owner, previous="confirmed", state="completed"))
        self.assertNotEqual(reserve(self.engine, **self.resource).operation_id, owner.operation_id)

    def test_rejects_cross_tenant_customer(self):
        with self.assertRaises(ValueError):
            reserve(self.engine, **dict(self.resource, customer_id=22))
