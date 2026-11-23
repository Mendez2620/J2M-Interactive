import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import selfhost_proxy_recovery as recovery


class SelfHostProxyRecoveryTests(unittest.TestCase):
    @staticmethod
    def _docker_result(*arguments, **_kwargs):
        if arguments[:2] == ("inspect", "web"):
            return json.dumps([{"Config": {"Image": "approved"}, "State": {"Running": True}}])
        if arguments[:2] == ("ps", "-q"):
            return "worker-id"
        if arguments[:2] == ("inspect", "worker-id"):
            return json.dumps([{
                "Config": {"Labels": {"com.docker.compose.project": "project"}},
                "State": {"Running": True},
            }])
        if arguments[:2] == ("inspect", "proxy"):
            return json.dumps([{"State": {"Running": True}}])
        if arguments[:2] == ("exec", "proxy"):
            return ""
        raise AssertionError(arguments)

    def _recover(self):
        return recovery.recover_proxy(
            origin="https://localhost:8443",
            expected_image="approved",
            web_container="web",
            worker_service="worker-service",
            project="project",
            proxy_container="proxy",
            attempts=2,
            retry_seconds=0,
            request_timeout=1,
        )

    @patch.object(recovery, "_https_ready", return_value=True)
    @patch.object(recovery, "_docker")
    def test_healthy_proxy_is_not_reloaded(self, docker, _ready):
        docker.side_effect = self._docker_result
        self.assertFalse(self._recover())
        self.assertNotIn(("exec", "proxy", "nginx", "-s", "reload"), [call.args for call in docker.call_args_list])

    @patch.object(recovery, "_https_ready", side_effect=[False, False, True])
    @patch.object(recovery, "_docker")
    def test_stale_proxy_is_validated_reloaded_and_retried(self, docker, _ready):
        docker.side_effect = self._docker_result
        self.assertTrue(self._recover())
        calls = [call.args for call in docker.call_args_list]
        self.assertIn(("exec", "proxy", "nginx", "-t"), calls)
        self.assertIn(("exec", "proxy", "nginx", "-s", "reload"), calls)

    @patch.object(recovery, "_https_ready", return_value=False)
    @patch.object(recovery, "_docker")
    def test_reload_failure_is_bounded_and_fail_closed(self, docker, ready):
        docker.side_effect = self._docker_result
        with self.assertRaisesRegex(RuntimeError, "tiempo permitido"):
            self._recover()
        self.assertEqual(ready.call_count, 3)

    @patch.object(recovery, "_https_ready", return_value=True)
    @patch.object(recovery, "_docker")
    def test_duplicate_worker_is_rejected_before_https(self, docker, ready):
        def duplicate(*arguments, **kwargs):
            if arguments[:2] == ("ps", "-q"):
                return "worker-a\nworker-b"
            return self._docker_result(*arguments, **kwargs)

        docker.side_effect = duplicate
        with self.assertRaisesRegex(RuntimeError, "exactamente un worker"):
            self._recover()
        ready.assert_not_called()

    def test_origin_and_limits_are_fail_closed(self):
        for origin in ("http://localhost:8443", "https://example.com:8443", "https://localhost:8443/path"):
            with self.assertRaises(ValueError):
                recovery.recover_proxy(
                    origin=origin,
                    expected_image="approved",
                    web_container="web",
                    worker_service="worker",
                    project="project",
                    proxy_container="proxy",
                )


if __name__ == "__main__":
    unittest.main()
