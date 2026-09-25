"""Offline tests for WebitelApiEntityResolver: urllib.request.urlopen is
mocked, so nothing here ever touches the real network or the real Webitel
system. This only proves the request-building and response-parsing logic
is correct against realistic response shapes (modeled on the real API
structure confirmed via a shared Postman collection, 2026-09-11) — it does
not prove the real API still matches that shape today.
"""

import json
import unittest
from io import BytesIO
from unittest.mock import patch

from crm_ai_agent.adapters.webitel_api.entity_resolver import WebitelApiEntityResolver, WebitelApiError
from crm_ai_agent.adapters.webitel_api.session import WebitelApiSession
from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError

SESSION = WebitelApiSession(cookie_header="WBTLAUTH=fake; WBTLCSRF=fake", access_token="fake-token")


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._buf = BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _resolver() -> WebitelApiEntityResolver:
    return WebitelApiEntityResolver(base_url="https://wtl-alm.astana-motors.kz", session=SESSION)


class TestWebitelApiEntityResolverResolve(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_resolve_unique_extension_match(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {"items": [{"id": "1138", "extension": "10078", "name": "madi muratbay"}]}
        )
        result = _resolver().resolve("10078")
        self.assertEqual(result.match_count, 1)
        self.assertEqual(result.entity_id, "10078")

    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_resolve_filters_out_substring_matches(self, mock_urlopen) -> None:
        """The real search endpoint does substring matching (e.g. querying
        "1007" would also return "10078", "10079"), so a raw result count
        would overstate ambiguity or miss it entirely — this proves the
        exact-extension filter used in the project owner's own Postman
        scripts is applied here too."""
        mock_urlopen.return_value = _FakeResponse(
            {
                "items": [
                    {"id": "1138", "extension": "10078", "name": "madi muratbay"},
                    {"id": "1139", "extension": "100781", "name": "someone else"},
                ]
            }
        )
        result = _resolver().resolve("10078")
        self.assertEqual(result.match_count, 1)
        self.assertEqual(result.entity_id, "10078")

    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_resolve_zero_matches(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse({"items": []})
        result = _resolver().resolve("99999")
        self.assertEqual(result.match_count, 0)
        self.assertIsNone(result.entity_id)

    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_resolve_handles_bare_list_response(self, mock_urlopen) -> None:
        """Some collection scripts guard with `Array.isArray(data) ? data :
        (data.items || [])`, implying the API sometimes returns a bare
        array rather than {items: [...]}. Both shapes must work."""
        mock_urlopen.return_value = _FakeResponse(
            [{"id": "1138", "extension": "10078", "name": "madi muratbay"}]
        )
        result = _resolver().resolve("10078")
        self.assertEqual(result.match_count, 1)


class TestWebitelApiEntityResolverGetCurrentState(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_reads_roles_license_and_group(self, mock_urlopen) -> None:
        responses = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078", "name": "madi muratbay"}]}),
            _FakeResponse(
                {
                    "id": "1138",
                    "extension": "10078",
                    "roles": [
                        {"id": "3", "name": "sysadmin"},
                        {"id": "28", "name": "users"},
                        {"id": "761", "name": "admins-cc"},
                    ],
                    "license": [
                        {"id": "05f2f0e6-bf94-4d84-b53a-d2d05a617035"},
                        {"id": "36d8c4ca-6f1a-4125-97a3-c2f2be6a8375"},
                    ],
                    "profile": {"group": "7719499780"},
                }
            ),
        ]
        mock_urlopen.side_effect = responses

        state = _resolver().get_current_state("10078")

        self.assertEqual(state["roles"], "sysadmin,users,admins-cc")
        self.assertEqual(
            state["license"], "05f2f0e6-bf94-4d84-b53a-d2d05a617035,36d8c4ca-6f1a-4125-97a3-c2f2be6a8375"
        )
        self.assertEqual(state["group"], "7719499780")

    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_missing_group_defaults_to_empty_string(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse({"id": "1138", "roles": [], "license": [], "profile": {}}),
        ]
        state = _resolver().get_current_state("10078")
        self.assertEqual(state["group"], "")

    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_unknown_extension_raises_subject_not_found(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse({"items": []})
        with self.assertRaises(SubjectNotFoundError):
            _resolver().get_current_state("99999")


class TestWebitelApiEntityResolverErrors(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.entity_resolver.urllib.request.urlopen")
    def test_http_error_is_wrapped(self, mock_urlopen) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://wtl-alm.astana-motors.kz/api/users", code=401, msg="Unauthorized", hdrs=None, fp=None
        )
        with self.assertRaises(WebitelApiError):
            _resolver().resolve("10078")


if __name__ == "__main__":
    unittest.main()
