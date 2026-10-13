import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from auth import AuthStore
from alerts import AlertService
from server import Monitor, make_handler


class AlertAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.auth = AuthStore(cls.directory.name)
        cls.admin, cls.admin_token = cls.auth.setup("alerts_admin", "alerts-admin-password")
        cls.auth.create_user(cls.admin["user"]["id"], "alerts_member", "alerts-member-password", "viewer")
        cls.viewer, cls.viewer_token = cls.auth.login("alerts_member", "alerts-member-password")
        cls.monitor = Monitor()
        cls.monitor.sample()
        cls.delivered = []
        cls.alerts = AlertService(cls.auth, sender=lambda config, event: cls.delivered.append(event))
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.monitor, cls.auth, alerts=cls.alerts))
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def request(self, method="GET", path="/api/admin/alerts", data=None, role="admin", csrf=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if role:
            token = self.admin_token if role == "admin" else self.viewer_token
            session = self.admin if role == "admin" else self.viewer
            headers["Cookie"] = f"monilite_session={token}"
            if csrf:
                headers["X-CSRF-Token"] = session["csrf_token"]
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, method=method, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None)
        try:
            response = urlopen(request)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def setUp(self):
        self.auth.limiter.attempts.clear()

    def test_admin_can_save_and_read_redacted_persistent_config(self):
        status, result = self.request("PATCH", data={"enabled": False, "channel": "webhook", "credential": "https://example.com/hook?token=api-secret"})
        self.assertEqual(status, 200, result)
        status, result = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(result["config"]["credential_configured"])
        self.assertNotIn("api-secret", json.dumps(result))

    def test_routes_require_admin_and_mutations_require_csrf_and_origin(self):
        for role, expected in [(None, 401), ("viewer", 403)]:
            for method, path, data in [("GET", "/api/admin/alerts", None), ("PATCH", "/api/admin/alerts", {}), ("POST", "/api/admin/alerts/test", {})]:
                with self.subTest(role=role, method=method):
                    self.assertEqual(self.request(method, path, data, role=role)[0], expected)
        self.assertEqual(self.request("PATCH", data={}, csrf=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/admin/alerts/test", {}, csrf=False)[0], 403)
        self.assertEqual(self.request("PATCH", data={}, origin="https://elsewhere.example")[0], 403)

    def test_validation_and_unconfigured_test_notification(self):
        self.assertEqual(self.request("PATCH", data={"repeat_seconds": 0})[0], 400)
        self.assertEqual(self.request("PATCH", data={"enabled": False, "clear_credential": True})[0], 200)
        status, result = self.request("POST", "/api/admin/alerts/test", {})
        self.assertEqual(status, 400, result)
        self.assertIn("凭据", result["error"])

    def test_test_notification_is_recorded_and_rate_limited(self):
        self.request("PATCH", data={"enabled": False, "channel": "webhook", "credential": "https://example.com/test"})
        for _ in range(3):
            status, result = self.request("POST", "/api/admin/alerts/test", {})
            self.assertEqual(status, 200, result)
            self.assertTrue(result["success"])
        self.assertEqual(self.delivered[-1]["kind"], "test")
        status, result = self.request("POST", "/api/admin/alerts/test", {})
        self.assertEqual(status, 429)
        self.assertIn("1 分钟", result["error"])
        self.assertTrue(self.request()[1]["history"][0]["success"])

    def test_delivery_failure_returns_safe_error_and_persists_failure(self):
        def failure(config, event):
            raise RuntimeError("secret-token")
        sender = self.alerts.sender
        self.alerts.sender = failure
        try:
            self.request("PATCH", data={"enabled": False, "channel": "webhook", "credential": "https://example.com/test"})
            status, result = self.request("POST", "/api/admin/alerts/test", {})
            self.assertEqual(status, 502)
            self.assertFalse(result["success"])
            self.assertNotIn("secret-token", json.dumps(result))
            self.assertFalse(self.request()[1]["history"][0]["success"])
        finally:
            self.alerts.sender = sender


if __name__ == "__main__":
    unittest.main()
