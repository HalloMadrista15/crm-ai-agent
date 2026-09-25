import json
import tempfile
import unittest
from pathlib import Path

from crm_ai_agent.adapters.webitel_api.session import WebitelSessionError, load_session


def _write_storage_state(path: Path, *, cookies=None, local_storage=None) -> None:
    data = {
        "cookies": cookies if cookies is not None else [
            {"name": "WBTLCSRF", "value": "csrf-value", "domain": "wtl-alm.astana-motors.kz"},
            {"name": "WBTLAUTH", "value": "auth-value", "domain": "wtl-alm.astana-motors.kz"},
        ],
        "origins": [
            {
                "origin": "https://wtl-alm.astana-motors.kz",
                "localStorage": local_storage if local_storage is not None else [
                    {"name": "access-token", "value": "token-value"},
                ],
            }
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


class TestLoadSession(unittest.TestCase):
    def test_loads_cookies_and_access_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storage_state.json"
            _write_storage_state(path)

            session = load_session(path)

            self.assertEqual(session.access_token, "token-value")
            self.assertIn("WBTLAUTH=auth-value", session.cookie_header)
            self.assertIn("WBTLCSRF=csrf-value", session.cookie_header)

    def test_headers_include_access_token_and_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storage_state.json"
            _write_storage_state(path)
            session = load_session(path)

            headers = session.headers()
            self.assertEqual(headers["X-Webitel-Access"], "token-value")
            self.assertIn("WBTLAUTH=auth-value", headers["Cookie"])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(WebitelSessionError):
            load_session(Path("does-not-exist.json"))

    def test_missing_wbtlauth_cookie_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storage_state.json"
            _write_storage_state(path, cookies=[{"name": "WBTLCSRF", "value": "csrf-value"}])
            with self.assertRaises(WebitelSessionError):
                load_session(path)

    def test_missing_access_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "storage_state.json"
            _write_storage_state(path, local_storage=[{"name": "theme", "value": "light"}])
            with self.assertRaises(WebitelSessionError):
                load_session(path)


if __name__ == "__main__":
    unittest.main()
