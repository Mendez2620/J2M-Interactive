"""Synthetic ledger and queue cases; the preview cannot contact Google."""

import os
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from app import create_app
from app.config import TestingConfig
from app.extensions import db
from app.models import Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, Transaction
from app.services.loyalty_transaction_service import process_loyalty_transaction
from app.services.google_wallet_reconciliation_plan import preview_google_wallet_reconciliation


class ReconciliationPreviewCases:
    def setUp(self):
        self.app = self.make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(db.engine.dispose)
        self.addCleanup(db.session.remove)
        db.create_all()
        self.app.config["GOOGLE_WALLET_MODE"] = "disabled"
        b = Business(name="Synthetic A", slug="synthetic-a", loyalty_type="stamps",
                     stamps_reward_limit=8, cooldown_minutes=0)
        other = Business(name="Synthetic B", slug="synthetic-b", cooldown_minutes=0)
        db.session.add_all([b, other])
        db.session.commit()
        self.business_id, self.other_id = b.id, other.id

    def customer(self, business_id=None, **kwargs):
        customer = Customer(business_id=business_id or self.business_id,
                            full_name="Synthetic private name", **kwargs)
        db.session.add(customer)
        db.session.commit()
        return customer.id

    def movement(self, customer_id, *, tx_type="earn", stamps=1, points=0):
        customer = db.session.get(Customer, customer_id)
        result = process_loyalty_transaction(
            business=customer.business, customer_id=customer.id, staff_id=None,
            action="earn_stamp" if tx_type == "earn" else "redeem_stamps",
            tx_type=tx_type, points_amount=points, stamps_amount=stamps, purchase_amount=0,
            request_id=uuid.uuid4().hex, fingerprint=uuid.uuid4().hex,
        )
        return result.transaction.id

    def job(self, customer_id, transaction_id, status, business_id=None):
        customer = db.session.get(Customer, customer_id)
        job = GoogleWalletSyncJob(
            business_id=business_id or customer.business_id, customer_id=customer_id,
            transaction_id=transaction_id, status=status,
        )
        db.session.add(job)
        db.session.commit()
        return job.id

    def preview(self, **kwargs):
        db.session.remove()
        return preview_google_wallet_reconciliation(self.business_id, **kwargs)

    def test_valid_latest_gap_and_failed_job_keep_exact_watermark(self):
        customer = self.customer(current_stamps=2)
        previous = self.movement(customer)
        self.job(customer, previous, "completed")
        latest = self.movement(customer, tx_type="redeem", stamps=2)
        failed = self.customer()
        failed_tx = self.movement(failed)
        failed_job = self.job(failed, failed_tx, "failed")
        self.movement(self.customer(self.other_id))
        result = self.preview()
        self.assertEqual(result.counts, {"eligible": 2})
        by_id = {row.customer_id: row for row in result.candidates}
        self.assertEqual(by_id[customer].transaction_id, latest)
        self.assertEqual(by_id[customer].stamps, 1)
        self.assertIsNone(by_id[customer].job_id)
        self.assertEqual(by_id[failed].job_id, failed_job)

    def test_any_pending_worker_and_latest_completed_are_not_candidates(self):
        pending = self.customer()
        self.job(pending, self.movement(pending), "pending")
        self.movement(pending)
        processing = self.customer()
        self.job(processing, self.movement(processing), "processing")
        completed = self.customer()
        self.job(completed, self.movement(completed), "completed")
        self.customer()
        result = self.preview()
        self.assertEqual(result.counts, {
            "worker_pending": 2, "already_covered": 1, "no_transactions": 1,
        })
        self.assertEqual(result.candidates, ())

    def test_missing_or_inconsistent_ledger_and_foreign_job_fail_closed(self):
        missing = self.customer()
        db.session.add(Transaction(business_id=self.business_id, customer_id=missing,
                                   type="earn", stamps_amount=1))
        db.session.commit()
        incomplete = self.customer()
        self.movement(incomplete)
        db.session.add(Transaction(business_id=self.business_id, customer_id=incomplete,
                                   type="earn", stamps_amount=1))
        db.session.commit()
        changed = self.customer()
        self.movement(changed)
        db.session.get(Customer, changed).current_stamps = 42
        db.session.commit()
        invalid = self.customer()
        self.job(invalid, self.movement(invalid), "failed", business_id=self.other_id)
        result = self.preview()
        self.assertEqual(result.counts, {
            "missing_baseline": 1, "incomplete_ledger": 1,
            "balance_mismatch": 1, "invalid_linkage": 1,
        })
        self.assertEqual(result.candidates, ())

    def test_tampered_delta_and_unknown_status_fail_closed(self):
        bad = self.customer()
        tx = self.movement(bad)
        entry = LoyaltyLedgerEntry.query.filter_by(transaction_id=tx).one()
        entry.stamps_delta = 9
        db.session.commit()
        unknown = self.customer()
        self.job(unknown, self.movement(unknown), "unknown")
        result = self.preview()
        self.assertEqual(result.counts, {"invalid_ledger": 1, "unknown_job_status": 1})

    def test_pagination_bounds_and_history_limit(self):
        first, second, third = [self.customer() for _ in range(3)]
        for customer in (first, second, third):
            self.movement(customer)
        page = self.preview(limit=2)
        self.assertEqual(page.scanned, 2)
        self.assertEqual(page.next_after_customer_id, second)
        self.assertEqual([x.customer_id for x in page.candidates], [first, second])
        last = self.preview(limit=2, after_customer_id=page.next_after_customer_id)
        self.assertEqual([x.customer_id for x in last.candidates], [third])
        self.assertIsNone(last.next_after_customer_id)
        with patch("app.services.google_wallet_reconciliation_plan.MAX_HISTORY", 1):
            self.assertEqual(self.preview().counts, {"history_limit": 3})
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True},
                       {"after_customer_id": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.preview(**kwargs)

    def test_points_and_inactive_business(self):
        b = db.session.get(Business, self.business_id)
        b.loyalty_type = "points"
        db.session.commit()
        customer = self.customer(current_points=1.25)
        self.movement(customer, stamps=0, points=0.125)
        result = self.preview()
        self.assertEqual(result.candidates[0].points, Decimal("1.375000"))
        db.session.get(Business, self.business_id).is_active = False
        db.session.commit()
        self.assertFalse(self.preview().business_active)
        self.assertEqual(self.preview().scanned, 0)

    def test_unflushed_session_data_is_not_written_or_used_in_preview(self):
        customer = self.customer()
        self.movement(customer)
        db.session.remove()
        uncommitted = Transaction(business_id=self.business_id, customer_id=customer,
                                  type="earn", stamps_amount=1)
        db.session.add(uncommitted)
        try:
            result = preview_google_wallet_reconciliation(self.business_id)
            self.assertEqual(result.counts, {"eligible": 1})
            self.assertIsNone(uncommitted.id)
            self.assertIn(uncommitted, db.session.new)
        finally:
            db.session.rollback()
        self.assertEqual(Transaction.query.count(), 1)

    def test_read_only_sql_no_provider(self):
        customer = self.customer()
        self.movement(customer)
        db.session.remove()
        statements = []

        def capture(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement.strip().upper())

        engine = db.engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            with patch("app.services.google_wallet.GoogleWalletService.__init__",
                       side_effect=AssertionError("Provider forbidden")):
                result = self.preview()
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        self.assertEqual(result.counts, {"eligible": 1})
        self.assertTrue(statements)
        self.assertTrue(all(sql.startswith("SELECT") or sql == "BEGIN" for sql in statements))
        self.assertEqual(Transaction.query.count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 2)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)


class ReconciliationPreviewSQLiteTests(ReconciliationPreviewCases, unittest.TestCase):
    def make_app(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        uri = f"sqlite:///{(Path(directory.name) / 'preview.sqlite').as_posix()}"
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri), \
             patch.object(TestingConfig, "SQLALCHEMY_ENGINE_OPTIONS", {}):
            return create_app("testing")


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "synthetic PostgreSQL CI not configured")
class ReconciliationPreviewPostgresTests(ReconciliationPreviewCases, unittest.TestCase):
    def make_app(self):
        url = make_url(os.environ["TEST_POSTGRES_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}
                or url.database != "j2m_test"):
            self.skipTest("requires the dedicated loopback j2m_test PostgreSQL service")
        # SQLAlchemy 2.1 defaults to psycopg 3; the project pins psycopg2.
        url = url.set(drivername="postgresql+psycopg2")
        schema = "wallet_preview_" + uuid.uuid4().hex
        admin = create_engine(url)
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        def remove_schema():
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        self.addCleanup(remove_schema)
        options = {"connect_args": {"options": f"-csearch_path={schema}"}}
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", url.render_as_string(hide_password=False)), \
             patch.object(TestingConfig, "SQLALCHEMY_ENGINE_OPTIONS", options):
            return create_app("testing")


if __name__ == "__main__":
    unittest.main()
