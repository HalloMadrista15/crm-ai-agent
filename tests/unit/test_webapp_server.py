"""Tests the local console server's own routing/validation logic by
actually starting it on 127.0.0.1 with an OS-assigned ephemeral port and
making real local HTTP requests to it. This never touches the network
beyond localhost, never touches CRM/Webitel (endpoints that would need
those are only exercised with inputs that fail validation before reaching
that code, e.g. a missing "url"), and never launches Playwright.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from crm_ai_agent.webapp.server import Handler


class TestConsoleServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path: str, body: dict | None = None):
        data = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(
            self._url(path), data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_index_serves_html(self) -> None:
        with urllib.request.urlopen(self._url("/"), timeout=10) as response:
            self.assertEqual(response.status, 200)
            html = response.read().decode("utf-8")
            self.assertIn("Консоль оператора", html)

    def test_unknown_get_path_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/nope"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_post_path_is_404(self) -> None:
        status, payload = self._post("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_ticket_endpoint_requires_url(self) -> None:
        status, payload = self._post("/api/crm/ticket", {})
        self.assertEqual(status, 400)
        self.assertIn("url", payload["error"])

    def test_webitel_plan_endpoint_requires_both_extensions(self) -> None:
        status, payload = self._post("/api/webitel/plan", {"template_extension": "10078"})
        self.assertEqual(status, 400)

    def test_webitel_execute_endpoint_requires_plan(self) -> None:
        status, payload = self._post("/api/webitel/execute", {})
        self.assertEqual(status, 400)

    def test_login_status_endpoint_returns_boolean_ready_field(self) -> None:
        status, payload = self._post("/api/crm/login/status", {})
        self.assertEqual(status, 200)
        self.assertIn("ready", payload)
        self.assertIsInstance(payload["ready"], bool)


if __name__ == "__main__":
    unittest.main()
