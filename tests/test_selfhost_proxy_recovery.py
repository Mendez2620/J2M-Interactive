import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import selfhost_proxy_recovery as recovery


SHA_NEW = "b" * 40
SHA_OLD = "a" * 40
NETWORK = "project_j2m-private"


class _FakeHTTPResponse:
    def __init__(self, url, revision, status=200):
        self.status = status
        self.url = url
        self.headers = {"X-J2M-Revision": revision}

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SelfHostProxyRecoveryTests(unittest.TestCase):
    @staticmethod
    def _docker_result(*arguments, **_kwargs):
        network_info = {NETWORK: {"IPAddress": "172.23.0.3"}}
        if arguments[:2] == ("inspect", "web"):
            return json.dumps([{
                "Config": {
                    "Image": "approved",
                    "Labels": {
                        "com.docker.compose.project": "project",
                        "com.docker.compose.service": "web",
                    },
                },
                "NetworkSettings": {"Networks": network_info},
                "State": {"Running": True},
            }])
        if arguments[:2] == ("ps", "-q"):
            return "worker-id"
        if arguments[:2] == ("inspect", "worker-id"):
            return json.dumps([{
                "Config": {"Labels": {"com.docker.compose.project": "project"}},
                "State": {"Running": True},
            }])
        if arguments[:2] == ("inspect", "proxy"):
            return json.dumps([{
                "State": {"Running": True},
                "NetworkSettings": {"Networks": network_info},
            }])
        if arguments[:2] == ("exec", "proxy"):
            return ""
        raise AssertionError(arguments)

    def _recover(self):
        return recovery.recover_proxy(
            origin="https://localhost:8443",
            expected_image="approved",
            expected_revision=SHA_NEW,
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
    def test_healthy_approved_backend_is_not_reloaded(self, docker, ready):
        docker.side_effect = self._docker_result
        self.assertFalse(self._recover())
        self.assertEqual(ready.call_args.args[-1], SHA_NEW)
        self.assertNotIn(("exec", "proxy", "nginx", "-s", "reload"),
                         [call.args for call in docker.call_args_list])

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

    @patch.object(recovery, "_https_ready", return_value=True)
    @patch.object(recovery, "_docker")
    def test_wrong_web_project_is_rejected_before_https(self, docker, ready):
        def foreign_web(*arguments, **kwargs):
            result = self._docker_result(*arguments, **kwargs)
            if arguments[:2] == ("inspect", "web"):
                parsed = json.loads(result)
                parsed[0]["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
                return json.dumps(parsed)
            return result

        docker.side_effect = foreign_web
        with self.assertRaisesRegex(RuntimeError, "proyecto o red"):
            self._recover()
        ready.assert_not_called()

    @patch.object(recovery, "_https_ready", return_value=True)
    @patch.object(recovery, "_docker")
    def test_wrong_proxy_network_is_rejected_before_https(self, docker, ready):
        def detached_proxy(*arguments, **kwargs):
            result = self._docker_result(*arguments, **kwargs)
            if arguments[:2] == ("inspect", "proxy"):
                parsed = json.loads(result)
                parsed[0]["NetworkSettings"]["Networks"] = {}
                return json.dumps(parsed)
            return result

        docker.side_effect = detached_proxy
        with self.assertRaisesRegex(RuntimeError, "red autorizada"):
            self._recover()
        ready.assert_not_called()

    def test_http_200_from_old_revision_is_never_accepted(self):
        def old_response(url, **_kwargs):
            return _FakeHTTPResponse(url, SHA_OLD)

        with patch.object(recovery, "urlopen", side_effect=old_response):
            self.assertFalse(recovery._https_ready(
                "https://localhost:8443", object(), 1, SHA_NEW
            ))

    def test_http_200_without_revision_is_never_accepted(self):
        with patch.object(recovery, "urlopen",
                          side_effect=lambda url, **kw: _FakeHTTPResponse(url, "")):
            self.assertFalse(recovery._https_ready(
                "https://localhost:8443", object(), 1, SHA_NEW
            ))

    def test_http_200_from_expected_revision_is_accepted_on_both_routes(self):
        requested = []
        def correct_response(url, **_kwargs):
            requested.append(url)
            return _FakeHTTPResponse(url, SHA_NEW)

        with patch.object(recovery, "urlopen", side_effect=correct_response):
            self.assertTrue(recovery._https_ready(
                "https://localhost:8443", object(), 1, SHA_NEW
            ))
        self.assertEqual(requested, [
            "https://localhost:8443/api/health",
            "https://localhost:8443/api/ready",
        ])


    @patch.object(recovery, "_legacy_https_ready", return_value=True)
    @patch.object(recovery, "_https_ready")
    @patch.object(recovery, "_docker")
    def test_bootstrap_legacy_rollback_forces_reload_without_sha_header(self, docker, normal_ready, legacy_ready):
        def legacy_runtime(*arguments, **kwargs):
            if arguments[:2] == ("network", "inspect"):
                return json.dumps([{"Containers": {"web-id": {"Name": "web"}}}])
            if arguments[:2] == ("inspect", "web"):
                item = json.loads(self._docker_result(*arguments, **kwargs))
                item[0]["Config"]["Image"] = "j2m-interactive:" + recovery.LEGACY_BOOTSTRAP_SHA
                item[0]["NetworkSettings"]["Networks"][NETWORK]["Aliases"] = ["web"]
                return json.dumps(item)
            return self._docker_result(*arguments, **kwargs)

        docker.side_effect = legacy_runtime
        self.assertTrue(recovery.recover_proxy(
            origin="https://localhost:8443",
            expected_image="j2m-interactive:" + recovery.LEGACY_BOOTSTRAP_SHA,
            expected_revision=recovery.LEGACY_BOOTSTRAP_SHA,
            web_container="web",
            worker_service="worker-service",
            project="project",
            proxy_container="proxy",
            network_name=NETWORK,
            attempts=2,
            retry_seconds=0,
            request_timeout=1,
            legacy_bootstrap_rollback=True,
        ))
        self.assertIn(("exec", "proxy", "nginx", "-s", "reload"),
                      [call.args for call in docker.call_args_list])
        normal_ready.assert_not_called()
        legacy_ready.assert_called_once()

    def test_bootstrap_legacy_rollback_rejects_any_other_image(self):
        with self.assertRaisesRegex(ValueError, "solo permite"):
            recovery.recover_proxy(
                origin="https://localhost:8443",
                expected_image="j2m-interactive:" + SHA_NEW,
                expected_revision=SHA_NEW,
                web_container="web",
                worker_service="worker-service",
                project="j2m-cutover-ready",
                proxy_container="proxy",
                legacy_bootstrap_rollback=True,
            )

    @patch.object(recovery, "urlopen")
    def test_legacy_https_requires_unmarked_200_without_redirects(self, opened):
        opened.side_effect = lambda url, **kw: _FakeHTTPResponse(url, "")
        self.assertTrue(recovery._legacy_https_ready(
            "https://localhost:8443", object(), 1
        ))
        opened.side_effect = lambda url, **kw: _FakeHTTPResponse(url, SHA_NEW)
        self.assertFalse(recovery._legacy_https_ready(
            "https://localhost:8443", object(), 1
        ))

    def test_origin_and_limits_are_fail_closed(self):
        for origin in ("http://localhost:8443", "https://example.com:8443",
                       "https://localhost:8443/path"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                recovery.recover_proxy(
                    origin=origin,
                    expected_image="approved",
                    expected_revision=SHA_NEW,
                    web_container="web",
                    worker_service="worker",
                    project="project",
                    proxy_container="proxy",
                )
        with self.assertRaisesRegex(ValueError, "SHA completo"):
            recovery.recover_proxy(
                origin="https://localhost:8443",
                expected_image="approved",
                expected_revision="invalid",
                web_container="web",
                worker_service="worker",
                project="project",
                proxy_container="proxy",
            )


if __name__ == "__main__":
    unittest.main()
