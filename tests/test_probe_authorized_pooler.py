from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from scripts import probe_authorized_pooler as p


class AuthorizedPoolerTests(unittest.TestCase):
    def test_requires_authorization_before_reading_config(self):
        with patch("sys.argv", ["probe", "--expected-host", "x",
                               "--expected-project-ref", "x"]), \
                patch.object(p, "read_config") as config, patch("builtins.print"):
            self.assertEqual(p.main(), 2)
            config.assert_not_called()

    def test_exact_destination_and_tls(self):
        ref = "a" * 20
        host = "aws-0-example.pooler.supabase.com"
        with TemporaryDirectory() as work, patch.dict(p.os.environ, {}, clear=True):
            ca = Path(work) / "ca.crt"
            ca.write_text("synthetic")
            config = {"DATABASE_URL": f"postgresql://postgres.{ref}:private@{host}:5432/postgres",
                      "DATABASE_SSL_ROOT_CERT": str(ca)}
            params = p.connection_parameters(config, host, ref)
            self.assertEqual(params["sslmode"], "verify-full")
            self.assertEqual(params["port"], 5432)
            for bad in (config["DATABASE_URL"].replace("5432", "6543"),
                        config["DATABASE_URL"] + "?hostaddr=127.0.0.1",
                        config["DATABASE_URL"].replace(host, "example.invalid")):
                with self.assertRaises(ValueError):
                    p.connection_parameters({**config, "DATABASE_URL": bad}, host, ref)

    def test_two_sessions_acquire_exclude_and_release(self):
        owner, peer = MagicMock(), MagicMock()
        connect = MagicMock(side_effect=[owner, peer])
        with patch.object(p, "scalar", side_effect=[10, 11, True, 10, False, 11,
                                                    True, True, True]) as scalar:
            result = p.check_session_affinity(connect, {})
        self.assertEqual(result, {"session_affinity_verified": True,
                                  "diagnostic_cleanup_uncertain": False})
        keys = [call.args[2][0] for call in scalar.call_args_list if len(call.args) == 3]
        self.assertEqual(len(set(keys)), 1)
        self.assertLess(keys[0], 0)
        owner.close.assert_called_once()
        peer.close.assert_called_once()

    def test_affinity_loss_reports_uncertain_cleanup_and_closes(self):
        owner, peer = MagicMock(), MagicMock()
        with patch.object(p, "scalar", side_effect=[10, 11, True, 12, 12]):
            result = p.check_session_affinity(MagicMock(side_effect=[owner, peer]), {})
        self.assertEqual(result, {"session_affinity_verified": False,
                                  "diagnostic_cleanup_uncertain": True})
        owner.close.assert_called_once()
        peer.close.assert_called_once()


    def test_candidate_role_requires_explicit_tls_and_exact_username(self):
        ref = "a" * 20
        host = "aws-0-example.pooler.supabase.com"
        with TemporaryDirectory() as work, patch.dict(p.os.environ, {}, clear=True):
            ca = Path(work) / "ca.crt"
            ca.write_text("synthetic")
            config = {"DATABASE_URL": f"postgresql://j2m_web.{ref}:private@{host}:5432/postgres"
                                      "?sslmode=verify-full&sslrootcert=/run/j2m/supabase-ca.pem"}
            params = p.connection_parameters(config, host, ref, expected_role="j2m_web", ca_file=ca)
            self.assertEqual(params["user"], "j2m_web." + ref)
            self.assertEqual(params["sslrootcert"], str(ca.resolve()))
            for role, cert in (("postgres", ca), ("j2m_worker", ca), ("j2m_web", None), ("service_role", ca)):
                with self.subTest(role=role, cert=cert is not None), self.assertRaises(ValueError):
                    p.connection_parameters(config, host, ref, expected_role=role, ca_file=cert)
            with self.assertRaises(ValueError):
                p.connection_parameters({"DATABASE_URL": config["DATABASE_URL"].replace("verify-full", "require")},
                                        host, ref, expected_role="j2m_web", ca_file=ca)

    def test_wrong_server_identity_aborts_before_lock(self):
        owner, peer = MagicMock(), MagicMock()
        with patch.object(p, "scalar", side_effect=[10, 11, False]) as scalar:
            result = p.check_session_affinity(MagicMock(side_effect=[owner, peer]), {},
                                              expected_role="j2m_web")
        self.assertFalse(result["session_affinity_verified"])
        self.assertFalse(result["diagnostic_cleanup_uncertain"])
        self.assertFalse(any("advisory" in call.args[1] for call in scalar.call_args_list))
        owner.close.assert_called_once()
        peer.close.assert_called_once()

    def test_candidate_identity_is_checked_again_after_commit(self):
        owner, peer = MagicMock(), MagicMock()
        with patch.object(p, "scalar", side_effect=[10, 11, True, True, True, 10, True,
                                                    False, 11, True, True, True, True]) as scalar:
            result = p.check_session_affinity(MagicMock(side_effect=[owner, peer]), {},
                                              expected_role="j2m_worker")
        self.assertTrue(result["session_affinity_verified"])
        checks = [call for call in scalar.call_args_list if "current_user" in call.args[1]]
        self.assertEqual(len(checks), 4)
        self.assertTrue(all(call.args[2] == ("j2m_worker", "j2m_worker") for call in checks))
