import logging
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import create_app
from app.config import TestingConfig
from app.extensions import db
from app.services import google_wallet_sync_service as worker


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "synthetic PostgreSQL not configured")
class WorkerTransactionCleanupTest(unittest.TestCase):
    # Use the same dedicated local test database guard as the PostgreSQL HTTP tests.
    def setUp(self):
        url = make_url(os.environ["TEST_POSTGRES_URL"])
        self.assertEqual(url.get_backend_name(), "postgresql")
        self.assertIn(url.host, {"localhost", "127.0.0.1"})
        self.assertEqual(url.database, "j2m_test")
        self.assertFalse(url.query)
        self.config_url = url.set(drivername="postgresql+psycopg2")
        self.schema = "worker_cycle_" + uuid4().hex
        self.role = self.schema + "_role"
        self.observer = create_engine(self.config_url)
        self.addCleanup(self.observer.dispose)
        with self.observer.begin() as sql:
            sql.exec_driver_sql(f"CREATE SCHEMA {self.schema}")
            sql.exec_driver_sql(f"CREATE ROLE {self.role} LOGIN PASSWORD 'synthetic-worker-cycle'")

        def cleanup():
            with self.observer.begin() as sql:
                sql.exec_driver_sql(f"DROP SCHEMA {self.schema} CASCADE")
                sql.exec_driver_sql(f"DROP ROLE {self.role}")

        self.addCleanup(cleanup)
        owner = self.make_app()
        with owner.app_context():
            db.create_all()
            db.engine.dispose()
        with self.observer.begin() as sql:
            sql.exec_driver_sql(f"SET LOCAL search_path={self.schema}")
            sql.exec_driver_sql("CREATE TABLE committed_cycle (n integer)")
            sql.exec_driver_sql(f"GRANT USAGE ON SCHEMA {self.schema} TO {self.role}")
            sql.exec_driver_sql(f"GRANT SELECT ON google_wallet_sync_jobs, google_wallet_referral_jobs, google_wallet_write_reservations TO {self.role}")
            sql.exec_driver_sql(f"GRANT SELECT (id,status,locked_at) ON google_wallet_publication_jobs TO {self.role}")
            sql.exec_driver_sql(f"GRANT INSERT ON committed_cycle TO {self.role}")
        self.app = self.make_app(self.role)
        with self.app.app_context():
            self.addCleanup(db.engine.dispose)

    def make_app(self, role=None):
        options = f"-csearch_path={self.schema}"
        url = self.config_url.set(username=role, password='synthetic-worker-cycle') if role else self.config_url
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI",
                          url.render_as_string(hide_password=False)), \
             patch.object(TestingConfig, "SQLALCHEMY_ENGINE_OPTIONS", {
                 "connect_args": {"options": options, "application_name": self.schema if role else "fixture-owner"},
             }):
            app = create_app("testing")
        app.config.update(GOOGLE_WALLET_MODE="disabled", APPLE_WALLET_MODE="disabled",
                          GOOGLE_WALLET_RECONCILIATION_ENABLED=False)
        return app

    def observe(self, expected_commits, *, require_unlocked=True):
        with self.observer.begin() as sql:
            state = sql.execute(text("""
                SELECT a.state, c.relname FROM pg_stat_activity a
                LEFT JOIN pg_locks l ON l.pid=a.pid AND l.mode='AccessShareLock' AND l.granted
                LEFT JOIN pg_class c ON c.oid=l.relation
                WHERE a.datname=current_database() AND a.application_name=:name
            """), {"name": self.schema}).all()
            observed = {name for _, name in state}
            restricted = {"google_wallet_sync_jobs", "google_wallet_publication_jobs"}
            if require_unlocked:
                self.assertNotIn("idle in transaction", [value for value, _ in state])
                self.assertFalse(observed & restricted)
                for relation in sorted(restricted):
                    sql.exec_driver_sql(f"LOCK TABLE {self.schema}.{relation} IN ACCESS EXCLUSIVE MODE NOWAIT")
            else:
                self.assertIn("idle in transaction", [value for value, _ in state])
                self.assertLessEqual(restricted, observed)
            self.assertEqual(sql.exec_driver_sql(f"SELECT count(*) FROM {self.schema}.committed_cycle").scalar(),
                             expected_commits)
            for table in ("customers", "transactions", "loyalty_ledger_entries", "google_wallet_sync_jobs"):
                self.assertEqual(0, sql.exec_driver_sql(f"SELECT count(*) FROM {self.schema}.{table}").scalar())

    def exercise(self, *, omit_cleanup=False, fail_metrics=False, fail_processing=False, external_stop=False):
        waits = []
        calls = []
        stop = threading.Event()
        disabled = SimpleNamespace(mode="disabled")
        process_jobs = worker.process_pending_google_wallet_jobs
        metrics = worker.google_wallet_sync_metrics
        logger = logging.getLogger("synthetic.worker")

        def process(**kwargs):
            calls.append(len(calls) + 1)
            self.assertEqual(db.session.scalar(text("SELECT current_user")), self.role)
            db.session.execute(text("INSERT INTO committed_cycle VALUES (:cycle)"), {"cycle": calls[-1]})
            db.session.commit()
            if fail_processing and len(calls) == 1:
                raise RuntimeError("synthetic processing failure")
            return process_jobs(**kwargs)

        def read_metrics():
            result = metrics()
            if fail_metrics and len(calls) == 1:
                raise RuntimeError("synthetic metrics failure")
            return result

        def wait(_seconds):
            waits.append(len(calls))
            self.observe(len(calls), require_unlocked=not omit_cleanup)
            if external_stop:
                stop.set()

        with (
            self.app.app_context(),
            patch.object(worker, "process_pending_google_wallet_jobs", side_effect=process),
            patch.object(worker, "google_wallet_sync_metrics", side_effect=read_metrics),
            patch.object(logger, "info", wraps=logger.info) as log_metrics,
            patch.object(db.session, "remove", wraps=db.session.remove) as cleanup,
        ):
            if omit_cleanup:
                cleanup.side_effect = lambda: None
            worker.run_google_wallet_sync_worker(service=disabled, stop_event=stop, sleep_fn=wait,
                                                max_cycles=2, logger=logger)
            self.assertEqual(cleanup.call_count, len(calls))
            self.assertEqual(log_metrics.call_count, len(calls) - int(fail_metrics or fail_processing))
            self.assertEqual(waits, [1])
            self.assertEqual(calls, [1] if external_stop else [1, 2])
        self.observe(len(calls))

    def test_regression_baseline_retains_transaction_when_cycle_cleanup_is_omitted(self):
        self.exercise(omit_cleanup=True, external_stop=True)

    def test_success_releases_locks_before_wait_and_runs_next_cycle(self):
        self.exercise()

    def test_metrics_error_is_logged_and_cycle_cleanup_still_runs(self):
        with self.assertLogs("synthetic.worker", "ERROR") as logs:
            self.exercise(fail_metrics=True)
        self.assertEqual(logs.output, ["ERROR:synthetic.worker:google_wallet_worker_cycle_failed error_type=RuntimeError"])

    def test_processing_error_is_logged_and_cycle_cleanup_still_runs(self):
        with self.assertLogs("synthetic.worker", "ERROR") as logs:
            self.exercise(fail_processing=True)
        self.assertEqual(logs.output, ["ERROR:synthetic.worker:google_wallet_worker_cycle_failed error_type=RuntimeError"])

    def test_external_stop_leaves_no_transaction(self):
        self.exercise(external_stop=True)
