import json
import unittest
from unittest.mock import patch

from sqlalchemy import event

from app import create_app
from app.extensions import db
from app.models import Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, Transaction
from app.services.google_wallet_reconciliation_audit import google_wallet_reconciliation_audit


class GoogleWalletReconciliationAuditTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.business = Business(name="Synthetic One", slug="synthetic-one")
        self.other = Business(name="Synthetic Two", slug="synthetic-two")
        db.session.add_all([self.business, self.other])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def customer(self, business):
        customer = Customer(business_id=business.id, full_name="Synthetic private name")
        db.session.add(customer)
        db.session.commit()
        return customer

    def transaction(self, customer, status=None, *, ledger=True, job_business=None):
        tx = Transaction(business_id=customer.business_id, customer_id=customer.id,
                         type="earn", stamps_amount=1)
        db.session.add(tx)
        db.session.flush()
        if ledger:
            db.session.add(LoyaltyLedgerEntry(
                business_id=customer.business_id, customer_id=customer.id,
                transaction_id=tx.id, operation_type="earn",
                points_before=0, points_after=0, stamps_before=0, stamps_after=1,
                stamps_delta=1,
            ))
        if status is not None:
            db.session.add(GoogleWalletSyncJob(
                business_id=job_business.id if job_business else customer.business_id,
                customer_id=customer.id, transaction_id=tx.id, status=status,
            ))
        db.session.commit()
        return tx

    def test_latest_transaction_only_and_tenant_isolation(self):
        missed = self.customer(self.business)
        self.transaction(missed, "completed")
        self.transaction(missed)
        for status in ("pending", "processing", "failed", "completed"):
            self.transaction(self.customer(self.business), status)
        self.transaction(self.customer(self.other))
        self.customer(self.business)  # No transaction: outside this inventory.

        result = google_wallet_reconciliation_audit(self.business.id)
        self.assertEqual(result["customers_with_transactions"], 5)
        self.assertEqual(result["counts"], {
            "missing_job": 1, "pending": 1, "processing": 1,
            "failed": 1, "completed": 1, "missing_ledger": 0,
            "invalid_linkage": 0, "unknown_job_status": 0,
        })
        self.assertFalse(result["remote_issuance_verified"])
        self.assertFalse(result["remote_balance_verified"])
        other = google_wallet_reconciliation_audit(self.other.id)
        self.assertEqual(other["customers_with_transactions"], 1)
        self.assertEqual(other["counts"]["missing_job"], 1)

    def test_invalid_or_unverified_records_are_not_candidates(self):
        self.transaction(self.customer(self.business), ledger=False)
        self.transaction(self.customer(self.business), "completed", job_business=self.other)
        self.transaction(self.customer(self.business), "unexpected")
        self.business.is_active = False
        db.session.commit()

        result = google_wallet_reconciliation_audit(self.business.id)
        self.assertFalse(result["business_active"])
        self.assertEqual(result["counts"]["missing_ledger"], 1)
        self.assertEqual(result["counts"]["invalid_linkage"], 1)
        self.assertEqual(result["counts"]["unknown_job_status"], 1)
        self.assertEqual(result["counts"]["missing_job"], 0)

    def test_cli_is_read_only_and_does_not_instantiate_provider(self):
        self.transaction(self.customer(self.business))
        business_id = self.business.id
        engine = db.engine
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.strip().upper())

        event.listen(engine, "before_cursor_execute", capture)
        try:
            with patch("app.services.google_wallet.GoogleWalletService.__init__",
                       side_effect=AssertionError("Provider must not be instantiated")):
                result = self.app.test_cli_runner().invoke(
                    args=["audit-google-wallet-reconciliation", "--business-id", str(business_id)]
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(statements)
        self.assertTrue(all(sql.startswith("SELECT") for sql in statements))
        report = json.loads(result.output)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["counts"]["missing_job"], 1)
        self.assertNotIn("Synthetic private name", result.output)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)
        self.assertEqual(Transaction.query.count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 1)

    def test_cli_requires_explicit_valid_business(self):
        runner = self.app.test_cli_runner()
        for arguments in ([], ["--business-id", "0"], ["--business-id", "999999999"]):
            with self.subTest(arguments=arguments):
                result = runner.invoke(args=["audit-google-wallet-reconciliation", *arguments])
                self.assertNotEqual(result.exit_code, 0)
        for value in (None, True, 0, -1, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                google_wallet_reconciliation_audit(value)

    def test_empty_business_has_zero_counts(self):
        report = google_wallet_reconciliation_audit(self.business.id)
        self.assertEqual(report["customers_with_transactions"], 0)
        self.assertTrue(all(count == 0 for count in report["counts"].values()))


if __name__ == "__main__":
    unittest.main()
