"""Synthetic ledger and queue cases; the preview cannot contact Google."""

import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url

from app import create_app
from app.config import TestingConfig
from app.extensions import db
from app.models import AccessAudit, Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, PlatformAdmin, Transaction
from app.services.loyalty_transaction_service import process_loyalty_transaction
from app.services.google_wallet_reconciliation_plan import (
    execute_google_wallet_reconciliation, execute_google_wallet_reconciliation_batch,
    preview_google_wallet_reconciliation,
)
from app.services.google_wallet_sync_service import _customer_lock
from app.services.google_wallet_reconciliation_control import run_reviewed_reconciliation


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

    def _execute(self, candidate, service, *, business_id=None):
        db.session.remove()
        self.app.config["GOOGLE_WALLET_MODE"] = "real"
        return execute_google_wallet_reconciliation(
            candidate, business_id=business_id or self.business_id, service=service,
        )

    def test_executor_records_only_confirmed_transaction_and_is_idempotent(self):
        customer = self.customer(current_stamps=2)
        transaction = self.movement(customer)
        candidate = self.preview().candidates[0]
        calls = []

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                calls.append((record.id, business_id, record.current_stamps))
                return {"status": "success", "reason": "balance_updated"}

        provider = Provider()
        self.assertEqual(self._execute(candidate, provider),
                         {"status": "success", "reason": "balance_updated"})
        self.assertEqual(calls, [(customer, self.business_id, 3)])
        job = GoogleWalletSyncJob.query.filter_by(transaction_id=transaction).one()
        self.assertEqual((job.status, job.business_id, job.customer_id),
                         ("completed", self.business_id, customer))
        self.assertEqual(self._execute(candidate, provider)["reason"], "stale_candidate")
        self.assertEqual(len(calls), 1)
        self.assertEqual(Transaction.query.count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 2)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 1)

    def test_executor_completes_existing_failed_job_without_duplicate(self):
        customer = self.customer()
        transaction = self.movement(customer)
        job_id = self.job(customer, transaction, "failed")
        db.session.get(GoogleWalletSyncJob, job_id).attempts = 3
        db.session.commit()
        candidate = self.preview().candidates[0]

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                return {"status": "success", "reason": "already_current"}

        self.assertEqual(self._execute(candidate, Provider())["status"], "success")
        job = db.session.get(GoogleWalletSyncJob, job_id)
        self.assertEqual((job.status, job.attempts), ("completed", 3))
        self.assertEqual(GoogleWalletSyncJob.query.count(), 1)

    def test_executor_never_queues_missing_or_uncertain_remote_object(self):
        customer = self.customer()
        self.movement(customer)
        candidate = self.preview().candidates[0]

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                return _self.response

        provider = Provider()
        for response, expected in (
            ({"status": "skipped", "reason": "object_not_found", "status_code": 404}, "skipped"),
            ({"status": "retry", "reason": "balance_unconfirmed"}, "retry"),
            ({"status": "error", "status_code": 403, "private": "not for logs"}, "error"),
            ({"status": "success"}, "retry"),
            (None, "retry"),
        ):
            with self.subTest(response=response):
                provider.response = response
                result = self._execute(candidate, provider)
                self.assertEqual(result["status"], expected)
                self.assertNotIn("private", result)
                self.assertEqual(GoogleWalletSyncJob.query.count(), 0)

    def test_executor_rejects_stale_foreign_disabled_and_busy_candidates(self):
        customer = self.customer()
        self.movement(customer)
        candidate = self.preview().candidates[0]

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                raise AssertionError("No remote call")

        provider = Provider()
        self.assertEqual(execute_google_wallet_reconciliation(
            candidate, business_id=self.business_id, service=provider,
        )["status"], "disabled")
        self.assertEqual(self._execute(candidate, provider, business_id=self.other_id)["reason"],
                         "invalid_candidate")
        with _customer_lock(customer) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self._execute(candidate, provider)["reason"], "customer_busy")
        self.movement(customer)
        self.assertEqual(self._execute(candidate, provider)["reason"], "stale_candidate")
        self.assertEqual(GoogleWalletSyncJob.query.count(), 1)  # normal real-mode movement

    def test_executor_never_completes_movement_arriving_during_remote_call(self):
        customer = self.customer()
        first = self.movement(customer)
        candidate = self.preview().candidates[0]
        movements = []
        test = self

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                movements.append(test.movement(customer))
                db.session.remove()
                return {"status": "success", "reason": "balance_updated"}

        result = self._execute(candidate, Provider())
        self.assertEqual(result, {"status": "retry", "reason": "changed_during_provider_call"})
        self.assertIsNone(GoogleWalletSyncJob.query.filter_by(transaction_id=first).first())
        newer = GoogleWalletSyncJob.query.filter_by(transaction_id=movements[0]).one()
        self.assertEqual(newer.status, "pending")
        self.assertEqual(Transaction.query.count(), 2)
        self.assertEqual(db.session.get(Customer, customer).current_stamps, 2)

    def test_executor_local_completion_failure_is_not_reported_as_success(self):
        customer = self.customer()
        self.movement(customer)
        candidate = self.preview().candidates[0]

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                return {"status": "success", "reason": "already_current"}

        with patch.object(db.engine, "begin",
                          side_effect=OperationalError("write", {}, RuntimeError("private"))):
            result = self._execute(candidate, Provider())
        self.assertEqual(result, {"status": "retry", "reason": "local_completion_unconfirmed"})
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)
        self.assertEqual(self._execute(candidate, Provider())["status"], "success")

    def test_batch_validates_all_candidates_and_stops_after_provider_failure(self):
        ids = [self.customer() for _ in range(3)]
        for customer in ids:
            self.movement(customer)
        other = self.customer(self.other_id)
        self.movement(other)
        candidates = self.preview().candidates
        foreign = preview_google_wallet_reconciliation(self.other_id).candidates[0]
        self.app.config["GOOGLE_WALLET_MODE"] = "real"
        calls = []

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                calls.append(record.id)
                if record.id == ids[1]:
                    return {"status": "retry", "status_code": 429}
                return {"status": "success", "reason": "already_current"}

        provider = Provider()
        for invalid in ((), (candidates[0], candidates[0]), (candidates[0], foreign),
                        tuple(candidates[0] for _ in range(26))):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                execute_google_wallet_reconciliation_batch(
                    invalid, business_id=self.business_id, service=provider,
                )
        self.assertEqual(calls, [])
        report = execute_google_wallet_reconciliation_batch(
            tuple(reversed(candidates)), business_id=self.business_id, service=provider,
        )
        self.assertEqual(calls, ids[:2])
        self.assertEqual(report, {"requested": 3, "processed": 2, "remaining": 1,
                                  "counts": {"success": 1, "retry": 1},
                                  "stop_reason": "provider_unconfirmed"})
        self.assertEqual(GoogleWalletSyncJob.query.count(), 1)

    def reviewed_fixture(self):
        customer = self.customer()
        self.movement(customer)
        admin = PlatformAdmin(name="Synthetic admin", email="admin@example.invalid",
                              password_hash="not-a-real-password-hash", is_active=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        candidates = self.preview().candidates
        self.app.config["GOOGLE_WALLET_MODE"] = "real"
        return candidates, admin_id

    def test_reviewed_operation_requires_gate_active_admin_and_confirmation(self):
        candidates, admin_id = self.reviewed_fixture()

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                raise AssertionError("Provider forbidden")

        args = dict(business_id=self.business_id, admin_id=admin_id,
                    request_id=str(uuid.uuid4()), confirm=True, service=Provider())
        self.assertEqual(run_reviewed_reconciliation(candidates, **args)["status"], "disabled")
        self.app.config["GOOGLE_WALLET_RECONCILIATION_ENABLED"] = True
        with self.assertRaises(ValueError):
            run_reviewed_reconciliation(candidates, **{**args, "confirm": False})
        db.session.get(PlatformAdmin, admin_id).is_active = False
        db.session.commit()
        db.session.remove()
        self.assertEqual(run_reviewed_reconciliation(candidates, **args),
                         {"status": "error", "reason": "admin_inactive"})
        self.assertEqual(AccessAudit.query.count(), 0)

    def test_reviewed_operation_records_each_attempt_and_replays_without_provider(self):
        candidates, admin_id = self.reviewed_fixture()
        self.app.config["GOOGLE_WALLET_RECONCILIATION_ENABLED"] = True
        calls = []
        test = self

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                with db.engine.connect() as connection:
                    actions = connection.execute(text("SELECT action FROM access_audits ORDER BY id")).scalars().all()
                test.assertEqual(actions, ["wallet_reconciliation_started", "wallet_reconciliation_item_started"])
                calls.append(record.id)
                return {"status": "skipped", "reason": "object_not_found", "status_code": 404}

        args = dict(business_id=self.business_id, admin_id=admin_id,
                    request_id=str(uuid.uuid4()), confirm=True, service=Provider())
        report = run_reviewed_reconciliation(candidates, **args)
        self.assertEqual(report["counts"], {"skipped": 1})
        self.assertFalse(report["replayed"])
        self.assertEqual(run_reviewed_reconciliation(candidates, **args), {**report, "replayed": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(AccessAudit.query.count(), 4)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)
        for row in AccessAudit.query.all():
            self.assertEqual(row.platform_admin_id, admin_id)
            self.assertNotIn("Synthetic private name", row.new_state)

    def test_reviewed_operation_interrupted_audit_cannot_automatically_replay(self):
        candidates, admin_id = self.reviewed_fixture()
        self.app.config["GOOGLE_WALLET_RECONCILIATION_ENABLED"] = True
        calls = []

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(_self, record, *, business_id):
                calls.append(record.id)
                return {"status": "retry", "reason": "balance_unconfirmed"}

        from app.services import google_wallet_reconciliation_control as control
        original = control._append

        def interrupt(connection, action, **kwargs):
            if action == control._ITEM_FINISH:
                raise OperationalError("write", {}, RuntimeError("private"))
            return original(connection, action, **kwargs)

        args = dict(business_id=self.business_id, admin_id=admin_id,
                    request_id=str(uuid.uuid4()), confirm=True, service=Provider())
        with patch.object(control, "_append", side_effect=interrupt):
            self.assertEqual(run_reviewed_reconciliation(candidates, **args)["status"], "unconfirmed")
        self.assertEqual(run_reviewed_reconciliation(candidates, **args),
                         {"status": "unconfirmed", "reason": "request_incomplete"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(AccessAudit.query.count(), 2)
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0)

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
    def test_referral_business_cursor_survives_worker_restart(self):
        import copy
        from datetime import datetime, timezone
        from app.models import GoogleWalletReferralJob, ReferralParticipant
        from app.services.google_wallet import GoogleWalletService
        from app.services.google_wallet_referral_queue import process_wallet_referral_jobs
        from app.services.referral_participant_service import (
            configure_customer_ambassador, ensure_customer_invitation,
        )
        from app.services.referral_program_service import configure_referral_program
        from scripts.inspect_wallet_referral_history import inspect_history

        self.app.config.update(GOOGLE_WALLET_MODE="real", GOOGLE_ISSUER_ID="123456",
                               PUBLIC_BASE_URL="https://j2m.example")
        # Seed the initially enabled state, before the events under test.
        db.session.execute(Business.__table__.update().where(Business.id == self.business_id)
                           .values(referrals_enabled=True))
        db.session.commit()

        def consent(customer_id, enabled):
            configure_customer_ambassador(
                customer=db.session.get(Customer, customer_id), enabled=enabled,
                consent_version="v1", consent_evidence="synthetic consent",
                actor_type="customer", actor_id=customer_id, reason="Synthetic choice",
                request_id=f"combined-{customer_id}-{enabled}", authorization_verified=True,
            )

        expected = []
        for _ in range(51):
            customer_id = self.customer()
            expected.append(customer_id)
            participant = ensure_customer_invitation(db.session.get(Customer, customer_id))
            participant.is_enabled = True
            participant.consent_version = "v1"
            participant.consented_at = datetime.now(timezone.utc)
            participant.consent_evidence_hash = "a" * 64
        foreign_id = self.customer(self.other_id)
        foreign = ensure_customer_invitation(db.session.get(Customer, foreign_id))
        db.session.commit()
        foreign_participant_id = foreign.id

        def configure(enabled):
            configure_referral_program(
                business=db.session.get(Business, self.business_id), enabled=enabled,
                actor_type="platform_admin", actor_id=1, reason="Synthetic pagination",
                request_id=f"synthetic-pagination-{enabled}",
            )

        configure(False)
        job = GoogleWalletReferralJob.query.filter_by(customer_id=None).one()
        job_id = job.id
        reading_page, release_page = Event(), Event()
        engine = db.engine
        db.session.remove()

        def pause_claimed_worker(_conn, _cursor, statement, _params, _context, _executemany):
            if "FROM referral_participants" in statement and "ORDER BY" in statement:
                reading_page.set()
                if not release_page.wait(10):
                    raise TimeoutError("Synthetic expansion was not released")

        def expand():
            with self.app.app_context():
                return process_wallet_referral_jobs(limit=1)

        event.listen(engine, "before_cursor_execute", pause_claimed_worker)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                winner = pool.submit(expand)
                try:
                    self.assertTrue(reading_page.wait(10))
                    self.assertEqual(pool.submit(expand).result(timeout=10), 0)
                finally:
                    release_page.set()
                self.assertEqual(winner.result(timeout=10), 1)
        finally:
            event.remove(engine, "before_cursor_execute", pause_claimed_worker)
        self.assertEqual(db.session.get(GoogleWalletReferralJob, job_id).cursor, expected[24])
        db.session.remove()
        configure(True)
        revoked = {expected[1], expected[47]}
        for subject in revoked:
            consent(subject, False)
        added = self.customer()
        expected.append(added)
        consent(added, True)
        db.session.remove()
        self.assertEqual(process_wallet_referral_jobs(limit=1), 1)
        self.assertEqual(db.session.get(GoogleWalletReferralJob, job_id).cursor, expected[49])
        db.session.remove()
        self.assertEqual(process_wallet_referral_jobs(limit=1), 1)
        self.assertEqual(db.session.get(GoogleWalletReferralJob, job_id).status, "completed")
        children = GoogleWalletReferralJob.query.filter(GoogleWalletReferralJob.customer_id.is_not(None))
        self.assertEqual(children.count(), len(expected) + len(revoked) + 1)

        class Response:
            content = b"synthetic"

            def __init__(self, payload):
                self.payload = payload
                self.status_code = 200 if payload is not None else 404

            def json(self):
                return copy.deepcopy(self.payload)

        initial = {}
        owned = {"id": "invite_friends", "uri": "https://j2m.example/invite/synthetic"}
        foreign_link = {"id": "menu", "uri": "https://example.test/menu"}
        missing = expected[-2]
        for subject in [*expected, foreign_id]:
            if subject == missing:
                continue
            tenant = self.other_id if subject == foreign_id else self.business_id
            class_id = f"123456.business_{tenant}"
            object_id = f"{class_id}_customer_{subject}"
            initial[object_id] = {
                "id": object_id, "classId": class_id, "barcode": {"value": "unchanged"},
                "loyaltyPoints": {"label": "Sellos", "balance": {"int": 1}},
                "textModulesData": [{"id": "brand", "body": "unchanged"}],
                "linksModuleData": {"uris": [foreign_link, owned]},
            }
        remote = copy.deepcopy(initial)
        calls = []
        test = self

        class Transport:
            def request(self, method, url, *, json, **kwargs):
                key = url.rsplit("/", 1)[-1]
                calls.append((method, key))
                if method == "GET":
                    return Response(remote.get(key))
                test.assertEqual(method, "PATCH")
                test.assertIn(key, remote)
                test.assertLessEqual(set(json), {"linksModuleData", "appLinkData"})
                remote[key].update(copy.deepcopy(json))
                return Response(remote[key])

        service = GoogleWalletService(mode="real", credentials_info={
            "type": "service_account", "project_id": "synthetic",
            "client_email": "wallet@synthetic.iam.gserviceaccount.com",
            "private_key": GoogleWalletService._generate_dev_private_key(),
            "token_uri": "https://example.invalid/token",
        }, http_session=Transport())
        for _ in range(20):
            if process_wallet_referral_jobs(service=service, limit=25) == 0:
                break
        else:
            self.fail("Referral queue did not drain")
        self.assertEqual(GoogleWalletReferralJob.query.filter_by(status="pending").count(), 0)
        self.assertEqual(GoogleWalletReferralJob.query.filter_by(status="failed").count(), 0)
        self.assertTrue(calls)
        for key, before in initial.items():
            card = remote[key]
            subject = int(key.rsplit("_", 1)[-1])
            if subject == foreign_id:
                self.assertEqual(card, before)
                self.assertFalse(any(resource == key for _, resource in calls))
                continue
            self.assertTrue(any(resource == key for _, resource in calls))
            for field in ("barcode", "loyaltyPoints", "textModulesData", "id", "classId"):
                self.assertEqual(card[field], before[field])
            links = card["linksModuleData"]["uris"]
            self.assertIn(foreign_link, links)
            invitation = [link for link in links if link["id"] == "invite_friends"]
            self.assertEqual(len(invitation), int(subject not in revoked))
            if invitation:
                participant = ReferralParticipant.query.filter_by(customer_id=subject).one()
                self.assertEqual(invitation[0]["uri"], f"https://j2m.example/invite/{participant.invitation_code}")
        self.assertEqual(GoogleWalletReferralJob.query.filter_by(customer_id=missing, status="missing").count(), 2)
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 0)

        first = inspect_history(db.session, self.business_id, limit=2)
        second = inspect_history(db.session, self.business_id, limit=2, after=first["next_after"])
        self.assertEqual([p["state"] for p in first["participants"]], ["active", "revoked"])
        self.assertGreater(second["participants"][0]["id"], first["next_after"])
        self.assertNotIn(foreign_participant_id, [p["id"] for p in first["participants"] + second["participants"]])
        self.assertEqual(inspect_history(db.session, self.other_id)["participants"],
                         [{"id": foreign_participant_id, "state": "no_consent"}])

    def test_referral_revocation_waits_for_inflight_write_and_removes_only_owned_links(self):
        self.check_referral_revocation_during_write(uncertain=False)

    def test_uncertain_referral_write_blocks_remote_removal_after_local_revocation(self):
        self.check_referral_revocation_during_write(uncertain=True)

    def test_existing_independent_card_is_patched_while_other_referral_is_uncertain(self):
        self.check_referral_revocation_during_write(uncertain=True, independent_exists=True)

    def check_referral_revocation_during_write(self, *, uncertain, independent_exists=False):
        from datetime import timedelta
        from requests import Timeout
        from app.models import GoogleWalletReferralJob
        from app.services.google_wallet import GoogleWalletService
        from app.services.google_wallet_referral_queue import process_wallet_referral_jobs, utcnow
        from app.services.referral_participant_service import configure_customer_ambassador

        self.app.config.update(GOOGLE_WALLET_MODE="real", GOOGLE_ISSUER_ID="123456",
                               PUBLIC_BASE_URL="https://j2m.example")
        business = db.session.get(Business, self.business_id)
        business.referrals_enabled = True
        db.session.commit()
        process_wallet_referral_jobs(limit=1)
        customer_id = self.customer()
        target = f"123456.business_{self.business_id}_customer_{customer_id}"

        def change(enabled, subject_id=customer_id):
            configure_customer_ambassador(
                customer=db.session.get(Customer, subject_id), enabled=enabled,
                consent_version="v1", consent_evidence="synthetic consent",
                actor_type="customer", actor_id=subject_id, reason="Synthetic choice",
                request_id=f"referral-{subject_id}-{enabled}", authorization_verified=True,
            )

        change(True)
        started, release = Event(), Event()
        calls = []
        independent_reads = []
        independent_writes = []
        independent_remote = {}
        foreign = {"id": "menu", "uri": "https://foreign.example/menu"}
        remote = {"id": target, "classId": target.split("_customer_")[0],
                  "barcode": {"value": "unchanged"}, "loyaltyPoints": {"label": "Sellos"},
                  "linksModuleData": {"uris": [foreign]}}
        engine = db.engine

        class Response:
            status_code = 200
            content = b"synthetic"

            def __init__(self, payload=None):
                self.payload = payload

            def json(self):
                return dict(remote if self.payload is None else self.payload)

        class Transport:
            def request(self, method, url, *, json, **kwargs):
                if not url.endswith(target):
                    if independent_exists:
                        if method == "PATCH":
                            independent_writes.append(method)
                            independent_remote.update(json)
                        else:
                            self_test.assertEqual(method, "GET")
                            independent_reads.append(True)
                        return Response(independent_remote)
                    self_test.assertEqual(method, "GET")
                    independent_reads.append(True)
                    return type("MissingResponse", (), {"status_code": 404})()
                if method == "PATCH":
                    calls.append(json)
                    if len(calls) == 1:
                        started.set()
                        self_test.assertTrue(release.wait(15))
                    remote.update(json)
                    if uncertain and len(calls) == 1:
                        raise Timeout("Synthetic response loss")
                else:
                    self_test.assertEqual(method, "GET")
                return Response()

        self_test = self
        credentials = {
            "type": "service_account", "project_id": "synthetic",
            "client_email": "wallet@synthetic.iam.gserviceaccount.com",
            "private_key": GoogleWalletService._generate_dev_private_key(),
            "token_uri": "https://example.invalid/token",
        }

        def worker():
            with self.app.app_context():
                return process_wallet_referral_jobs(service=GoogleWalletService(
                    mode="real", credentials_info=credentials, http_session=Transport(),
                ))

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker)
            try:
                self.assertTrue(started.wait(10))
                change(False)
                independent_id = self.customer()
                independent_remote.update(
                    id=f"{target.split('_customer_')[0]}_customer_{independent_id}",
                    classId=target.split("_customer_")[0],
                )
                change(True, independent_id)
                with engine.begin() as connection:
                    pid = connection.execute(text(
                        "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                        "WHERE l.locktype='advisory' AND l.granted AND l.objsubid=2 "
                        "AND l.classid=1246907724 AND l.objid=:id AND a.datname=current_database() "
                        "AND a.application_name=:app"
                    ), {"id": customer_id, "app": self.synthetic_schema}).scalar_one()
                    self.assertTrue(connection.execute(text(
                        "SELECT pg_terminate_backend(:pid, 5000)"
                    ), {"pid": pid}).scalar_one())
                worker()
                self.assertEqual(len(calls), 1)
                self.assertEqual(independent_reads, [True])
                self.assertEqual(GoogleWalletReferralJob.query.filter_by(customer_id=independent_id).one().status,
                                 "completed" if independent_exists else "missing")
                self.assertEqual(GoogleWalletReferralJob.query.filter_by(status="pending").count(), 1)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15), 1)
        with patch("app.services.google_wallet_referral_queue.utcnow", return_value=utcnow() + timedelta(hours=2)):
            worker()
        if uncertain:
            self.assertEqual(len(calls), 1)
            self.assertEqual(GoogleWalletReferralJob.query.filter_by(status="pending", outcome="blocked").count(), 2)
            self.assertIsNotNone(remote["appLinkData"])
            if independent_exists:
                self.assertEqual(independent_writes, ["PATCH"])
                self.assertIn("appLinkData", independent_remote)
        else:
            self.assertEqual(remote["linksModuleData"]["uris"], [foreign])
            self.assertIsNone(remote["appLinkData"])
        self.assertEqual(remote["barcode"], {"value": "unchanged"})
        self.assertEqual(remote["loyaltyPoints"], {"label": "Sellos"})
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 0)

    def test_normal_worker_claims_only_returned_rows_and_fences_stale_owner(self):
        from datetime import timedelta
        from threading import Barrier
        from app.services.google_wallet_sync_service import _claim_sync_jobs, _now, _record_result

        customer = self.customer()
        job = self.job(customer, self.movement(customer), "pending")
        barrier = Barrier(2, timeout=10)
        timestamp = _now()

        def claim(token):
            with self.app.app_context():
                barrier.wait()
                acquired = _claim_sync_jobs([job], timestamp, token)
                db.session.commit()
                return token, acquired

        tokens = ["a" * 32, "b" * 32]
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, tokens))
        self.assertEqual(sorted(len(ids) for _, ids in outcomes), [0, 1])
        first = next(token for token, ids in outcomes if ids)
        successor = "c" * 32
        later = timestamp + timedelta(minutes=6)
        self.assertEqual(_claim_sync_jobs([job], later, successor), [job])
        db.session.commit()
        _record_result([job], {"status": "success"}, later, claim=first)
        db.session.commit()
        row = db.session.get(GoogleWalletSyncJob, job)
        self.assertEqual((row.status, row.claim_token), ("processing", successor))
        _record_result([job], {"status": "success"}, later, claim=successor)
        db.session.commit()
        self.assertEqual((row.status, row.claim_token), ("completed", None))

    def test_delayed_reconciliation_cannot_overtake_new_purchase_after_lock_loss(self):
        self.check_delayed_wallet_write(redeem=False)

    def test_delayed_worker_write_does_not_cover_later_redemption_after_lock_loss(self):
        self.check_delayed_wallet_write(redeem=True)

    def check_delayed_wallet_write(self, *, redeem):
        from app.services.google_wallet import GoogleWalletService
        from app.services.google_wallet_sync_service import process_pending_google_wallet_jobs

        customer = self.customer(current_stamps=4)
        if redeem:
            self.app.config["GOOGLE_WALLET_MODE"] = "real"
        first_tx = self.movement(customer)
        candidate = None if redeem else self.preview().candidates[0]
        business_id = self.business_id
        self.app.config.update(GOOGLE_WALLET_MODE="real", GOOGLE_ISSUER_ID="123456")
        credentials = {
            "type": "service_account", "project_id": "synthetic",
            "client_email": "wallet@synthetic.iam.gserviceaccount.com",
            "private_key": GoogleWalletService._generate_dev_private_key(),
            "token_uri": "https://example.invalid/token",
        }
        started, release = Event(), Event()
        writes = []
        balances = {}
        engine = db.engine

        class Response:
            status_code = 200
            content = b"synthetic"

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body

        class Provider:
            def request(self, method, url, *, json, timeout, **kwargs):
                identifier = url.rsplit("/", 1)[-1]
                if method == "PATCH":
                    balance = json["loyaltyPoints"]["balance"]["string"]
                    writes.append((identifier, balance))
                    if balance == "5 / 8":
                        started.set()
                        if not release.wait(20):
                            raise RuntimeError("Synthetic provider timed out")
                    balances[identifier] = json["loyaltyPoints"]
                elif method != "GET":
                    raise AssertionError("Reconciliation must not issue a card")
                return Response({
                    "id": identifier, "classId": f"123456.business_{business_id}",
                    "state": "active",
                    "loyaltyPoints": balances.get(identifier, {
                        "label": "Sellos", "balance": {"string": "4 / 8"},
                    }),
                })

        provider = Provider()

        def service():
            return GoogleWalletService(mode="real", credentials_info=credentials, http_session=provider)

        def reconcile():
            with self.app.app_context():
                if redeem:
                    return process_pending_google_wallet_jobs(service=service())
                return execute_google_wallet_reconciliation(candidate, business_id=business_id, service=service())

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reconcile)
            try:
                self.assertTrue(started.wait(15), future.exception() if future.done() else None)
                with engine.begin() as connection:
                    pid = connection.execute(text(
                        "SELECT locks.pid FROM pg_locks AS locks JOIN pg_stat_activity AS activity "
                        "ON activity.pid=locks.pid WHERE locktype='advisory' AND granted "
                        "AND classid=1246907724 AND objid=:customer AND objsubid=2 "
                        "AND activity.datname=current_database() AND activity.application_name=:app"
                    ), {"customer": customer, "app": self.synthetic_schema}).scalar_one()
                    self.assertTrue(connection.execute(text(
                        "SELECT pg_terminate_backend(:pid, 5000)"
                    ), {"pid": pid}).scalar_one())
                later_tx = self.movement(customer, tx_type="redeem" if redeem else "earn")
                later_job = GoogleWalletSyncJob.query.filter_by(transaction_id=later_tx).one().id
                independent = self.customer()
                independent_tx = self.movement(independent)
                self.assertEqual(
                    GoogleWalletSyncJob.query.filter_by(transaction_id=independent_tx).one().status,
                    "pending",
                )
                db.session.remove()
                process_pending_google_wallet_jobs(service=service())
                target = f"123456.business_{business_id}_customer_{customer}"
                self.assertEqual([value for key, value in writes if key == target], ["5 / 8"])
                self.assertEqual(len(writes), 2)
            finally:
                release.set()
            initial_result = future.result(timeout=15)
            if redeem:
                self.assertEqual(initial_result, 1)
            else:
                self.assertEqual(initial_result["reason"], "changed_during_provider_call")

        db.session.expire_all()
        job = db.session.get(GoogleWalletSyncJob, later_job)
        self.assertEqual(job.status, "pending")
        first_job = GoogleWalletSyncJob.query.filter_by(transaction_id=first_tx).first()
        if redeem:
            self.assertEqual(first_job.status, "completed")
        else:
            self.assertIsNone(first_job)
        job.next_attempt_at = None
        db.session.commit()
        process_pending_google_wallet_jobs(service=service())
        self.assertEqual(balances[target]["balance"]["string"], "4 / 8" if redeem else "6 / 8")
        self.assertEqual(Transaction.query.filter_by(customer_id=customer).count(), 2)
        self.assertEqual(GoogleWalletSyncJob.query.filter_by(customer_id=customer).count(), 2 if redeem else 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter_by(customer_id=customer).count(), 3)
        self.assertEqual(db.session.get(GoogleWalletSyncJob, later_job).status, "completed")

    def make_app(self):
        url = make_url(os.environ["TEST_POSTGRES_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}
                or url.database != "j2m_test" or url.query):
            self.skipTest("requires the dedicated loopback j2m_test PostgreSQL service")
        # SQLAlchemy 2.1 defaults to psycopg 3; the project pins psycopg2.
        url = url.set(drivername="postgresql+psycopg2")
        schema = "wallet_preview_" + uuid.uuid4().hex
        self.synthetic_schema = schema
        admin = create_engine(url)
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        def remove_schema():
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        self.addCleanup(remove_schema)
        options = {"connect_args": {"options": f"-csearch_path={schema}", "application_name": schema}}
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", url.render_as_string(hide_password=False)), \
             patch.object(TestingConfig, "SQLALCHEMY_ENGINE_OPTIONS", options):
            return create_app("testing")


if __name__ == "__main__":
    unittest.main()
