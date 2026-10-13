import copy
import io
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.error import HTTPError
from urllib.request import Request
import alerts

from auth import AuthStore

try:
    from alerts import AlertService, AlertError, send_notification
except ImportError:
    AlertService = None


def sample(cpu=0, memory=0, disks=None):
    return {"timestamp": time.time(), "system": {"hostname": "test-host"},
            "cpu": {"percent": cpu}, "memory": {"percent": memory},
            "disks": disks or []}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(AlertService, "后台告警服务尚未实现")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.auth = AuthStore(self.directory.name)
        self.delivered = []
        self.service = AlertService(self.auth, sender=lambda config, event: self.delivered.append(copy.deepcopy(event)))

    def configure(self, **changes):
        config = {"enabled": True, "channel": "webhook", "credential": "https://example.com/notify?token=private-secret",
                  "duration_seconds": 10, "repeat_seconds": 60,
                  "rules": {"cpu": {"enabled": True, "threshold": 80},
                            "memory": {"enabled": False, "threshold": 90},
                            "disk": {"enabled": False, "threshold": 90}}}
        config.update(changes)
        return self.service.update(config)

    def test_disabled_by_default_and_persistent_redacted_settings(self):
        self.assertFalse(self.service.status()["config"]["enabled"])
        result = self.configure()
        self.assertTrue(result["config"]["credential_configured"])
        self.assertNotIn("private-secret", json.dumps(result))
        reopened = AlertService(self.auth)
        self.assertEqual(reopened.status()["config"]["rules"]["cpu"]["threshold"], 80)
        self.service.update({"credential": "", "duration_seconds": 20})
        self.assertTrue(self.service.status()["config"]["credential_configured"])
        self.service.update({"enabled": False, "clear_credential": True})
        self.assertFalse(self.service.status()["config"]["credential_configured"])

    def test_threshold_is_strict_and_must_last_full_duration(self):
        self.configure()
        self.service.evaluate(sample(cpu=80), now=0)
        self.service.evaluate(sample(cpu=81), now=1)
        self.service.evaluate(sample(cpu=82), now=10)
        self.assertEqual(self.delivered, [])
        self.service.evaluate(sample(cpu=83), now=11)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0]["value"], 83)
        self.assertEqual(self.delivered[0]["threshold"], 80)
        self.assertIn("test-host", self.delivered[0]["message"])

    def test_repeat_interval_and_recovery_restart_duration(self):
        self.configure(duration_seconds=0)
        self.service.evaluate(sample(cpu=90), now=0)
        self.service.evaluate(sample(cpu=90), now=59)
        self.assertEqual(len(self.delivered), 1)
        self.service.evaluate(sample(cpu=90), now=60)
        self.assertEqual(len(self.delivered), 2)
        self.service.evaluate(sample(cpu=80), now=61)
        self.service.evaluate(sample(cpu=90), now=62)
        self.assertEqual(len(self.delivered), 3)
        self.assertEqual(len(self.service.status()["active"]), 1)

    def test_disks_are_independent_and_removed_disk_state_is_cleared(self):
        self.configure(duration_seconds=0, rules={"cpu": {"enabled": False, "threshold": 80},
                       "memory": {"enabled": True, "threshold": 90}, "disk": {"enabled": True, "threshold": 90}})
        self.service.evaluate(sample(memory=95, disks=[{"mount": "/", "percent": 91}, {"mount": "/data", "percent": 92}]), now=0)
        self.assertEqual({e["metric"] for e in self.delivered}, {"memory", "disk"})
        self.assertEqual([e["mount"] for e in self.delivered if e["metric"] == "disk"], ["/", "/data"])
        self.service.evaluate(sample(disks=[{"mount": "/", "percent": 50}]), now=1)
        self.assertEqual(self.service.status()["active"], [])

    def test_stale_data_resets_duration_and_disabling_stops_delivery(self):
        self.configure()
        self.service.evaluate(sample(cpu=90), now=0)
        stale = sample(cpu=90)
        stale["timestamp"] -= 20
        self.service.evaluate(stale, now=15)
        self.service.evaluate(sample(cpu=90), now=16)
        self.assertEqual(self.delivered, [])
        self.service.evaluate(sample(cpu=90), now=26)
        self.assertEqual(len(self.delivered), 1)
        self.service.update({"enabled": False})
        self.service.evaluate(sample(cpu=90), now=100)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.service.status()["active"], [])

    def test_failure_is_redacted_throttled_and_logs_survive_restart(self):
        def fail(config, event):
            raise RuntimeError("private-secret")
        self.service.sender = fail
        self.configure(duration_seconds=0)
        self.service.evaluate(sample(cpu=90), now=0)
        self.service.evaluate(sample(cpu=90), now=59)
        logs = self.service.status()["history"]
        self.assertEqual(len(logs), 1)
        self.assertFalse(logs[0]["success"])
        self.assertNotIn("private-secret", json.dumps(logs))
        self.service.evaluate(sample(cpu=90), now=60)
        self.assertEqual(len(AlertService(self.auth).status()["history"]), 2)

    def test_test_notification_works_while_disabled_and_log_is_bounded(self):
        self.configure(enabled=False)
        self.service.test_notification("test-host")
        self.assertEqual(self.delivered[0]["kind"], "test")
        for _ in range(105):
            self.service.test_notification("test-host")
        self.assertEqual(len(self.service.status()["history"]), 100)

    def test_invalid_configuration_is_rejected_without_overwriting(self):
        self.configure()
        invalid = [{"enabled": "true"}, {"duration_seconds": -1}, {"duration_seconds": True},
                   {"repeat_seconds": 0}, {"repeat_seconds": 86401}, {"channel": "unknown"},
                   {"rules": {"cpu": {"threshold": float("nan")}}},
                   {"rules": {"cpu": {"threshold": 0}}}, {"rules": {"cpu": {"enabled": 1}}},
                   {"credential": "http://example.com"}, {"credential": "https://user:pass@example.com"},
                   {"credential": "https://bad\x7f.example/hook"},
                   {"channel": "wecom", "credential": "https://example.com/cgi-bin/webhook/send?key=abc"},
                   {"channel": "serverchan", "credential": "https://example.com"},
                   {"channel": "serverchan"}, {"clear_credential": True}, {"unexpected": 1}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(AlertError):
                self.service.update(data)
        self.assertEqual(self.service.status()["config"]["channel"], "webhook")

    def test_slow_delivery_does_not_prevent_background_recovery_detection(self):
        entered, release = threading.Event(), threading.Event()
        def slow_sender(config, event):
            entered.set()
            release.wait(5)
        self.service.sender = slow_sender
        self.configure(duration_seconds=0)
        monitor = SimpleNamespace(lock=threading.Lock(), stop_event=threading.Event(), current=sample(cpu=90))
        self.service.start(monitor)
        try:
            self.assertTrue(entered.wait(3), "后台未触发告警")
            with monitor.lock:
                monitor.current = sample(cpu=50)
            deadline = time.monotonic() + 3
            while self.service.status()["active"] and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(self.service.status()["active"], [], "通知请求阻塞了后台规则判断")
        finally:
            release.set()
            monitor.stop_event.set()
            self.service.stop()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(AlertService, "通知传输尚未实现")
        self.event = {"kind": "test", "title": "测试通知", "message": "服务器：test-host"}

    def request(self, channel, credential, response):
        with patch("alerts._post_https", return_value=json.dumps(response).encode()) as post:
            send_notification({"channel": channel, "credential": credential}, self.event)
            request = post.call_args.args[0]
            self.assertEqual(post.call_args.kwargs["timeout"], 5)
            return request

    def test_wecom_and_serverchan_payloads(self):
        request = self.request("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", {"errcode": 0})
        self.assertEqual(json.loads(request.data), {"msgtype": "text", "text": {"content": "服务器：test-host"}})
        request = self.request("serverchan", "SCT1234567890abcdef", {"code": 0, "data": {"errno": 0}})
        self.assertEqual(request.full_url, "https://sctapi.ftqq.com/SCT1234567890abcdef.send")
        self.assertEqual(parse_qs(request.data.decode()), {"title": ["测试通知"], "desp": ["服务器：test-host"]})

    def test_generic_webhook_json_and_business_errors(self):
        request = self.request("webhook", "https://example.com/notify", {"accepted": True})
        self.assertEqual(json.loads(request.data), self.event)
        for channel, credential, response in [
            ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", {"errcode": 93000, "errmsg": "secret"}),
            ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc", {}),
            ("serverchan", "SCT1234567890abcdef", {"code": 0, "data": {"errno": 1}}),
            ("serverchan", "SCT1234567890abcdef", {"code": 400, "message": "secret"})]:
            with self.subTest(channel=channel), self.assertRaises(AlertError) as raised:
                self.request(channel, credential, response)
            self.assertNotIn("secret", str(raised.exception))

    def test_network_errors_redirects_and_invalid_provider_responses_are_safe(self):
        config = {"channel": "wecom", "credential": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"}
        for failure in [TimeoutError('secret'), HTTPError(config['credential'], 302, 'secret', {}, io.BytesIO(b''))]:
            with self.subTest(failure=type(failure).__name__), patch('alerts._post_https', side_effect=failure):
                with self.assertRaises(AlertError) as raised:
                    send_notification(config, self.event)
                self.assertNotIn('secret', str(raised.exception))
        for raw in [b'invalid-json', b'x' * 65537]:
            with self.subTest(size=len(raw)), patch('alerts._post_https', return_value=raw):
                with self.assertRaises(AlertError):
                    send_notification(config, self.event)

    def test_overall_deadline_interrupts_drip_response_and_next_send_can_run(self):
        post = getattr(alerts, '_post_https', None)
        self.assertIsNotNone(post, '通知缺少整体发送期限')

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header('Content-Length', '5')
                self.end_headers()
                try:
                    for _ in range(5):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        if self.path == '/slow':
                            time.sleep(0.08)
                except OSError:
                    pass
            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with patch('alerts.HTTPSConnection', HTTPConnection):
                started = time.monotonic()
                request = Request(f'https://127.0.0.1:{http.server_port}/slow', data=b'{}', method='POST')
                with self.assertRaises(AlertError):
                    post(request, timeout=0.2)
                self.assertLess(time.monotonic() - started, 0.5)
                request = Request(f'https://127.0.0.1:{http.server_port}/fast', data=b'{}', method='POST')
                self.assertEqual(post(request, timeout=1), b'xxxxx')
                # Transport setup or thread creation errors must not consume capacity.
                for _ in range(2):
                    with patch('alerts.HTTPSConnection', side_effect=RuntimeError('setup failed')):
                        with self.assertRaises(RuntimeError):
                            post(request, timeout=1)
                    with patch('alerts.threading.Thread.start', side_effect=RuntimeError('start failed')):
                        with self.assertRaises(RuntimeError):
                            post(request, timeout=1)
                self.assertEqual(post(request, timeout=1), b'xxxxx')
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
