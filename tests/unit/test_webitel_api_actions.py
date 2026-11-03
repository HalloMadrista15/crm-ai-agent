"""Offline tests for WebitelApiActions: urllib.request.urlopen is mocked,
nothing here touches the real network or the real Webitel system. Proves
the plan-building and execute-request logic against realistic response
shapes confirmed via a shared Postman collection (2026-09-11).
"""

import json
import unittest
from io import BytesIO
from unittest.mock import patch

from crm_ai_agent.adapters.webitel_api.actions import WebitelApiActions, WebitelApiError, generate_password, group_schema
from crm_ai_agent.adapters.webitel_api.session import WebitelApiSession
from crm_ai_agent.ports.entity_resolver import SubjectNotFoundError

# 16 letters/digits, never 0, o or O.
_PASSWORD_FORMAT = r"^[1-9A-NP-Za-np-z]{16}$"

SESSION = WebitelApiSession(cookie_header="WBTLAUTH=fake; WBTLCSRF=fake", access_token="fake-token")

TEMPLATE_USER = {
    "id": "1138",
    "extension": "10078",
    "name": "madi muratbay",
    "roles": [{"id": "3", "name": "sysadmin"}, {"id": "28", "name": "users"}],
    "license": [{"id": "05f2f0e6-bf94-4d84-b53a-d2d05a617035"}],
    "devices": [{"id": "147005", "name": "10078"}],
    "profile": {"group": "7719499780"},
}

TARGET_USER = {
    "id": "9999",
    "extension": "10079",
    "name": "new employee",
    "roles": [{"id": "1041", "name": "user-ALM-MK-10"}],
    "license": [],
    "devices": [{"id": "222222", "name": "10079"}],
    "profile": {},
    "username": "10079",
}


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._buf = BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _device_write() -> list[_FakeResponse]:
    """GET + PUT of TARGET_USER's device, which a password reset sets as well."""
    return [
        _FakeResponse({"id": "222222", "name": "10079", "account": "10079", "user": {"id": "9999", "name": "new employee"}}),
        _FakeResponse({}),
    ]


def _actions() -> WebitelApiActions:
    return WebitelApiActions(base_url="https://wtl-alm.astana-motors.kz", session=SESSION)


class TestFromInfoEnv(unittest.TestCase):
    def test_defaults_to_almaty(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            info_env = Path(tmp) / "info.env"
            info_env.write_text("WEBITEL_ALMATY_BASE_URL=https://alm.example.invalid\n", encoding="utf-8")
            storage_state = Path(tmp) / "storage.json"
            storage_state.write_text(
                '{"cookies": [{"name": "WBTLAUTH", "value": "x"}, {"name": "WBTLCSRF", "value": "y"}],'
                ' "origins": [{"origin": "https://alm.example.invalid", "localStorage": [{"name": "access-token", "value": "z"}]}]}',
                encoding="utf-8",
            )
            actions = WebitelApiActions.from_info_env(info_env, storage_state)
            self.assertEqual(actions._base_url, "https://alm.example.invalid")

    def test_selects_astana_when_requested(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            info_env = Path(tmp) / "info.env"
            info_env.write_text(
                "WEBITEL_ALMATY_BASE_URL=https://alm.example.invalid\n"
                "WEBITEL_ASTANA_BASE_URL=https://ast.example.invalid\n",
                encoding="utf-8",
            )
            storage_state = Path(tmp) / "storage.json"
            storage_state.write_text(
                '{"cookies": [{"name": "WBTLAUTH", "value": "x"}, {"name": "WBTLCSRF", "value": "y"}],'
                ' "origins": [{"origin": "https://ast.example.invalid", "localStorage": [{"name": "access-token", "value": "z"}]}]}',
                encoding="utf-8",
            )
            actions = WebitelApiActions.from_info_env(info_env, storage_state, city="astana")
            self.assertEqual(actions._base_url, "https://ast.example.invalid")

    def test_raises_clear_error_for_missing_city_key(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            info_env = Path(tmp) / "info.env"
            info_env.write_text("WEBITEL_ALMATY_BASE_URL=https://alm.example.invalid\n", encoding="utf-8")
            storage_state = Path(tmp) / "storage.json"
            storage_state.write_text(
                '{"cookies": [{"name": "WBTLAUTH", "value": "x"}, {"name": "WBTLCSRF", "value": "y"}],'
                ' "origins": [{"origin": "https://alm.example.invalid", "localStorage": [{"name": "access-token", "value": "z"}]}]}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                WebitelApiActions.from_info_env(info_env, storage_state, city="astana")
            self.assertIn("WEBITEL_ASTANA_BASE_URL", str(ctx.exception))


class TestGetRawUser(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_returns_full_record(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse(TEMPLATE_USER),
        ]
        user = _actions().get_raw_user("10078")
        self.assertEqual(user, TEMPLATE_USER)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_raises_when_extension_not_found(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse({"items": []})
        with self.assertRaises(SubjectNotFoundError):
            _actions().get_raw_user("99999")


class TestBuildPlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_carries_template_roles_license_group_and_target_id(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse(TEMPLATE_USER),
            _FakeResponse({"items": [{"id": "9999", "extension": "10079"}]}),
            _FakeResponse(TARGET_USER),
        ]
        plan = _actions().build_plan(template_extension="10078", target_extension="10079")

        self.assertEqual(plan["roles"], TEMPLATE_USER["roles"])
        self.assertEqual(plan["license"], TEMPLATE_USER["license"])
        self.assertEqual(plan["group"], "7719499780")
        self.assertEqual(plan["target_id"], "9999")
        self.assertEqual(plan["target_name"], "new employee")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_build_plan_never_issues_a_put(self, mock_urlopen) -> None:
        """Structural guarantee: building a plan is pure read. If this ever
        needs a PUT mock in its side_effect list, build_plan stopped being
        read-only and that is a regression."""
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse(TEMPLATE_USER),
            _FakeResponse({"items": [{"id": "9999", "extension": "10079"}]}),
            _FakeResponse(TARGET_USER),
        ]
        _actions().build_plan(template_extension="10078", target_extension="10079")
        for call in mock_urlopen.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), "GET")


def _user_lookups() -> list[_FakeResponse]:
    """Template 10078 then target 10079, as build_plan looks them up."""
    return [
        _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
        _FakeResponse(TEMPLATE_USER),
        _FakeResponse({"items": [{"id": "9999", "extension": "10079"}]}),
        _FakeResponse(TARGET_USER),
    ]


def _grant(grantee_id: str, name: str, granted: str, *, person: bool = False) -> dict:
    grantee = {"id": grantee_id, "name": name, **({"user": True} if person else {})}
    return {"grantee": grantee, "granted": granted}


class TestBuildPlanWithPermissions(unittest.TestCase):
    """Shapes from the real GET /api/users/{id}/acl (2026-10-16)."""

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_lists_template_role_grants_the_target_lacks(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _user_lookups() + [
            # target's current permissions
            _FakeResponse({"page": 1, "items": [
                _grant("1138", "madi muratbay", "xrwdxrwd", person=True),
                _grant("2401", "semiadmin", "rw"),
                _grant("4995", "practice", "r"),
            ]}),
            # template's, over two pages
            _FakeResponse({"page": 1, "next": True, "items": [
                _grant("2", "adm", "xrwdxrwd", person=True),
                _grant("2401", "semiadmin", "rw"),
            ]}),
            _FakeResponse({"page": 2, "items": [
                _grant("4995", "practice", "rw"),
                _grant("6143", "user-alm-aic-301", "r"),
            ]}),
        ]

        plan = _actions().build_plan(template_extension="10078", target_extension="10079", copy_permissions=True)

        self.assertIs(plan["copy_permissions"], True)
        # semiadmin is already the same, adm is a person — neither is copied.
        self.assertEqual(plan["permissions"], [
            {"grantee": "4995", "name": "practice", "granted": "rw", "current": "r"},
            {"grantee": "6143", "name": "user-alm-aic-301", "granted": "r", "current": ""},
        ])
        urls = [call.args[0].full_url for call in mock_urlopen.call_args_list[4:]]
        self.assertEqual(urls, [
            "https://wtl-alm.astana-motors.kz/api/users/9999/acl?page=1&size=100",
            "https://wtl-alm.astana-motors.kz/api/users/1138/acl?page=1&size=100",
            "https://wtl-alm.astana-motors.kz/api/users/1138/acl?page=2&size=100",
        ])
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_without_the_checkbox_permissions_are_not_read(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _user_lookups()
        plan = _actions().build_plan(template_extension="10078", target_extension="10079")
        self.assertNotIn("permissions", plan)
        self.assertEqual(mock_urlopen.call_count, 4)


def _acl(*grants: dict) -> _FakeResponse:
    return _FakeResponse({"page": 1, "items": list(grants)})


def _patches(mock_urlopen) -> list:
    """(url, body) of every PATCH sent, in order."""
    return [
        (c.args[0].full_url, json.loads(c.args[0].data))
        for c in mock_urlopen.call_args_list
        if c.args[0].get_method() == "PATCH"
    ]


ACL_URL = "https://wtl-alm.astana-motors.kz/api/users/9999/acl"


class TestExecutePlanWithPermissions(unittest.TestCase):
    """Webitel's ACL PATCH flips the letters it names (confirmed 2026-10-18
    on test user 30120): "r"+"w" -> "rw", "rw"+"rw" -> grantee gone."""

    PLAN = {
        "target_id": "9999", "roles": [], "license": [], "group": "x", "copy_permissions": True,
        "permissions": [
            {"grantee": "4995", "name": "practice", "granted": "rw", "current": "r"},
            {"grantee": "6143", "name": "user-alm-aic-301", "granted": "r", "current": ""},
        ],
    }

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_missing_grantee_is_sent_once_and_a_different_one_is_cleared_first(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(TARGET_USER), _FakeResponse({"id": "9999"}),
            _acl(_grant("4995", "practice", "r")),  # what the target has now
            _FakeResponse({}), _FakeResponse({}), _FakeResponse({}),
            _acl(_grant("4995", "practice", "rw"), _grant("6143", "user-alm-aic-301", "r")),  # read back
        ]

        result = _actions().execute_plan(self.PLAN)

        self.assertEqual(_patches(mock_urlopen), [
            (ACL_URL, [{"grantee": 4995, "grants": "r"}]),   # clear practice's "r"...
            (ACL_URL, [{"grantee": 4995, "grants": "rw"}]),  # ...then give it "rw"
            (ACL_URL, [{"grantee": 6143, "grants": "r"}]),
        ])
        self.assertEqual(result["permissions_copied"], 2)
        self.assertNotIn("permissions_error", result)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_grantee_that_already_matches_is_never_sent(self, mock_urlopen) -> None:
        """Sending it again would flip it off — what happened to a new
        user's default semiadmin/practice in the first version."""
        both = (_grant("4995", "practice", "rw"), _grant("6143", "user-alm-aic-301", "r"))
        mock_urlopen.side_effect = [_FakeResponse(TARGET_USER), _FakeResponse({"id": "9999"}), _acl(*both), _acl(*both)]

        result = _actions().execute_plan(self.PLAN)

        self.assertEqual(_patches(mock_urlopen), [])
        self.assertEqual(result["permissions_copied"], 2)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_grant_webitel_silently_drops_is_reported(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(TARGET_USER), _FakeResponse({"id": "9999"}),
            _acl(),
            _FakeResponse({}), _FakeResponse({}),
            _acl(_grant("6143", "user-alm-aic-301", "r")),
        ]

        result = _actions().execute_plan(self.PLAN)

        self.assertIn("practice (rw)", result["permissions_error"])
        self.assertNotIn("user-alm-aic-301", result["permissions_error"])
        self.assertEqual(result["permissions_copied"], 1)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_failed_permissions_patch_keeps_the_new_password(self, mock_urlopen) -> None:
        import urllib.error

        mock_urlopen.side_effect = [
            _FakeResponse(TARGET_USER),
            _FakeResponse({"id": "9999"}),
            *_device_write(),
            _acl(),
            urllib.error.HTTPError(url="x", code=403, msg="Forbidden", hdrs=None, fp=BytesIO(b"no acl rights")),
            _FakeResponse({}),
            _acl(_grant("6143", "user-alm-aic-301", "r")),
        ]

        result = _actions().execute_plan({**self.PLAN, "reset_password": True})

        self.assertRegex(result["new_password"], _PASSWORD_FORMAT)
        self.assertIn("practice (rw)", result["permissions_error"])
        self.assertIn("HTTP 403", result["permissions_error"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_nothing_to_copy_sends_no_patch(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [_FakeResponse(TARGET_USER), _FakeResponse({"id": "9999"})]
        result = _actions().execute_plan({**self.PLAN, "permissions": []})
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertNotIn("permissions_copied", result)


class TestExecutePlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_execute_refetches_target_and_puts_merged_record(self, mock_urlopen) -> None:
        plan = {
            "target_id": "9999",
            "roles": TEMPLATE_USER["roles"],
            "license": TEMPLATE_USER["license"],
            "group": "7719499780",
        }
        mock_urlopen.side_effect = [
            _FakeResponse(TARGET_USER),  # fresh re-fetch
            _FakeResponse({"id": "9999", "status": "ok"}),  # PUT response
        ]

        result = _actions().execute_plan(plan)

        self.assertEqual(result, {"id": "9999", "status": "ok"})

        put_call = mock_urlopen.call_args_list[1]
        put_request = put_call.args[0]
        self.assertEqual(put_request.get_method(), "PUT")
        sent_body = json.loads(put_request.data)

        # Roles/license/group are updated...
        self.assertEqual(sent_body["roles"], TEMPLATE_USER["roles"])
        self.assertEqual(sent_body["license"], TEMPLATE_USER["license"])
        self.assertEqual(sent_body["profile"]["group"], "7719499780")
        # ...but every other field from the fresh target record survives
        # untouched (PUT replaces the whole record).
        self.assertEqual(sent_body["username"], "10079")
        self.assertEqual(sent_body["devices"], TARGET_USER["devices"])
        self.assertEqual(sent_body["name"], "new employee")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_execute_wraps_http_error(self, mock_urlopen) -> None:
        import urllib.error

        plan = {"target_id": "9999", "roles": [], "license": [], "group": ""}
        mock_urlopen.side_effect = [
            _FakeResponse(TARGET_USER),
            urllib.error.HTTPError(
                url="https://wtl-alm.astana-motors.kz/api/users/9999",
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=BytesIO(b'{"detail":"no permission"}'),
            ),
        ]
        with self.assertRaises(WebitelApiError):
            _actions().execute_plan(plan)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_execute_never_touches_name_or_username_fields(self, mock_urlopen) -> None:
        """Explicit regression guard: execute_plan (update an EXISTING
        user) must never itself set name/username, and must not touch the
        password unless the plan explicitly opts in via reset_password —
        those otherwise come from wherever the target record already has
        them (created by the separate build_create_plan/execute_create_plan
        path below), never invented here."""
        plan = {"target_id": "9999", "roles": [], "license": [], "group": "x"}
        mock_urlopen.side_effect = [_FakeResponse(TARGET_USER), _FakeResponse({})]

        result = _actions().execute_plan(plan)

        put_request = mock_urlopen.call_args_list[1].args[0]
        sent_body = json.loads(put_request.data)
        self.assertNotIn("password", sent_body)
        self.assertEqual(sent_body["name"], TARGET_USER["name"])
        self.assertEqual(sent_body["username"], TARGET_USER["username"])
        self.assertNotIn("new_password", result)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_execute_resets_password_when_explicitly_requested(self, mock_urlopen) -> None:
        """2026-09-21 operator request: an explicit opt-in
        (plan['reset_password'] = True) generates a fresh random password
        (never a hardcoded one) and returns it as new_password so it can
        be shown to the approver once."""
        plan = {"target_id": "9999", "roles": [], "license": [], "group": "x", "reset_password": True}
        mock_urlopen.side_effect = [_FakeResponse(TARGET_USER), _FakeResponse({"id": "9999"}), *_device_write()]

        result = _actions().execute_plan(plan)

        put_request = mock_urlopen.call_args_list[1].args[0]
        sent_body = json.loads(put_request.data)
        self.assertRegex(sent_body["password"], _PASSWORD_FORMAT)
        self.assertEqual(result["new_password"], sent_body["password"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_execute_reset_password_generates_different_values_each_call(self, mock_urlopen) -> None:
        plan = {"target_id": "9999", "roles": [], "license": [], "group": "x", "reset_password": True}
        mock_urlopen.side_effect = [
            _FakeResponse(TARGET_USER), _FakeResponse({}), *_device_write(),
            _FakeResponse(TARGET_USER), _FakeResponse({}), *_device_write(),
        ]

        result1 = _actions().execute_plan(plan)
        result2 = _actions().execute_plan(plan)

        self.assertNotEqual(result1["new_password"], result2["new_password"])


class TestGeneratePassword(unittest.TestCase):
    def test_only_letters_and_digits_without_zero_or_letter_o(self) -> None:
        for _ in range(2000):
            password = generate_password()
            self.assertRegex(password, _PASSWORD_FORMAT)
            self.assertTrue(any(c.isupper() for c in password), password)
            self.assertTrue(any(c.islower() for c in password), password)
            self.assertTrue(any(c.isdigit() for c in password), password)

    def test_every_allowed_character_turns_up(self) -> None:
        # Guards against an alphabet that's narrower than intended.
        seen = set("".join(generate_password() for _ in range(500)))
        expected = set("123456789ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijklmnpqrstuvwxyz")
        self.assertEqual(seen, expected)

    def test_length_can_be_chosen(self) -> None:
        self.assertEqual(len(generate_password(10)), 10)


class TestFindNeighbors(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_skips_missing_extensions_and_sorts_by_extension(self, mock_urlopen) -> None:
        # radius=2 around "10087" checks 10085, 10086, 10088, 10089 (in that
        # probe order); only 10086 and 10088 exist.
        mock_urlopen.side_effect = [
            _FakeResponse({"items": []}),  # 10085: not found
            _FakeResponse({"items": [{"id": "81", "extension": "10086"}]}),
            _FakeResponse({"id": "81", "extension": "10086", "name": "Meeting Room Yellow", "roles": [], "profile": {"group": "ALM-MK"}}),
            _FakeResponse({"items": [{"id": "82", "extension": "10088"}]}),
            _FakeResponse({"id": "82", "extension": "10088", "name": "Utemisov Murat", "roles": [{"name": "users"}], "profile": {"group": "ALM-MK"}}),
            _FakeResponse({"items": []}),  # 10089: not found
        ]
        neighbors = _actions().find_neighbors("10087", radius=2)

        self.assertEqual([n["extension"] for n in neighbors], ["10086", "10088"])
        self.assertEqual(neighbors[0]["name"], "Meeting Room Yellow")
        self.assertEqual(neighbors[1]["roles"], ["users"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_non_numeric_extension_raises(self, mock_urlopen) -> None:
        with self.assertRaises(WebitelApiError):
            _actions().find_neighbors("not-a-number", radius=2)
        mock_urlopen.assert_not_called()

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_never_issues_a_write(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = lambda *a, **kw: _FakeResponse({"items": []})
        _actions().find_neighbors("10087", radius=1)
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")


class TestBuildCreatePlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_carries_template_roles_license_group_and_new_identity(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),  # template search
            _FakeResponse(TEMPLATE_USER),  # template full record
            _FakeResponse({"items": []}),  # new_extension free-check: not found
        ]
        plan = _actions().build_create_plan(
            template_extension="10078", new_username="10087", new_name="New Person", new_extension="10087"
        )

        self.assertEqual(plan["roles"], TEMPLATE_USER["roles"])
        self.assertEqual(plan["license"], TEMPLATE_USER["license"])
        self.assertEqual(plan["group"], "7719499780")
        self.assertEqual(plan["new_username"], "10087")
        self.assertEqual(plan["new_name"], "New Person")
        self.assertEqual(plan["new_extension"], "10087")
        self.assertNotIn("password", plan)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_when_new_extension_already_taken(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),  # template search
            _FakeResponse(TEMPLATE_USER),  # template full record
            _FakeResponse({"items": [{"id": "9999", "extension": "10079"}]}),  # new_extension search: found!
            _FakeResponse(TARGET_USER),  # new_extension full record
        ]
        with self.assertRaises(WebitelApiError):
            _actions().build_create_plan(
                template_extension="10078", new_username="10079", new_name="Someone", new_extension="10079"
            )

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_build_create_plan_never_issues_a_write(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse(TEMPLATE_USER),
            _FakeResponse({"items": []}),
        ]
        _actions().build_create_plan(
            template_extension="10078", new_username="10087", new_name="New Person", new_extension="10087"
        )
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")


class TestCreateWithPermissions(unittest.TestCase):
    PLAN = {
        "new_username": "10087", "new_name": "New Person", "new_extension": "10087",
        "roles": [], "license": [], "group": "", "copy_permissions": True,
        "permissions": [{"grantee": "6143", "name": "user-alm-aic-301", "granted": "r", "current": ""}],
    }

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_lists_every_template_role_grant(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse(TEMPLATE_USER),
            _FakeResponse({"items": []}),  # new extension is free
            _FakeResponse({"page": 1, "items": [
                _grant("2", "adm", "xrwdxrwd", person=True),
                _grant("2401", "semiadmin", "rw"),
                _grant("6143", "user-alm-aic-301", "r"),
            ]}),
        ]
        plan = _actions().build_create_plan(
            template_extension="10078", new_username="10087", new_name="New Person", new_extension="10087",
            copy_permissions=True,
        )
        self.assertEqual(plan["permissions"], [
            {"grantee": "2401", "name": "semiadmin", "granted": "rw", "current": ""},
            {"grantee": "6143", "name": "user-alm-aic-301", "granted": "r", "current": ""},
        ])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_new_user_gets_the_planned_permissions_last(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "5555"}),  # POST /api/users
            _FakeResponse({"id": "device-1"}),  # POST /api/devices
            _FakeResponse({"id": "5555"}),  # GET refetch, no devices -> no PUT
            _acl(),  # the new user's permissions before
            _FakeResponse({}),  # PATCH acl
            _acl(_grant("6143", "user-alm-aic-301", "r")),  # read back
        ]
        result = _actions().execute_create_plan(self.PLAN)

        self.assertEqual(_patches(mock_urlopen),
                         [("https://wtl-alm.astana-motors.kz/api/users/5555/acl", [{"grantee": 6143, "grants": "r"}])])
        self.assertEqual(result["permissions_copied"], 1)
        self.assertRegex(result["temporary_password"], _PASSWORD_FORMAT)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_roles_webitel_gave_the_new_user_by_default_are_left_alone(self, mock_urlopen) -> None:
        """2026-10-18: the first version re-sent a new user's default
        semiadmin/practice and so took them away (the PATCH flips)."""
        plan = {**self.PLAN, "permissions": [
            {"grantee": "2401", "name": "semiadmin", "granted": "rw", "current": ""},
            {"grantee": "6143", "name": "user-alm-aic-301", "granted": "r", "current": ""},
        ]}
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "5555"}), _FakeResponse({"id": "device-1"}), _FakeResponse({"id": "5555"}),
            _acl(_grant("2401", "semiadmin", "rw")),  # Webitel's default
            _FakeResponse({}),
            _acl(_grant("2401", "semiadmin", "rw"), _grant("6143", "user-alm-aic-301", "r")),
        ]
        result = _actions().execute_create_plan(plan)

        self.assertEqual([body for _, body in _patches(mock_urlopen)], [[{"grantee": 6143, "grants": "r"}]])
        self.assertEqual(result["permissions_copied"], 2)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_failed_permissions_patch_keeps_the_temporary_password(self, mock_urlopen) -> None:
        import urllib.error

        mock_urlopen.side_effect = [
            _FakeResponse({"id": "5555"}),
            _FakeResponse({"id": "device-1"}),
            _FakeResponse({"id": "5555"}),
            _acl(),
            urllib.error.HTTPError(url="x", code=403, msg="Forbidden", hdrs=None, fp=BytesIO(b"no acl rights")),
            _acl(),  # read back: nothing set
        ]
        result = _actions().execute_create_plan(self.PLAN)
        self.assertRegex(result["temporary_password"], _PASSWORD_FORMAT)
        self.assertIn("HTTP 403", result["permissions_error"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_without_the_checkbox_no_patch(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [_FakeResponse({"id": "5555"}), _FakeResponse({"id": "device-1"}), _FakeResponse({"id": "5555"})]
        result = _actions().execute_create_plan({**self.PLAN, "copy_permissions": False})
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertNotIn("permissions_copied", result)


class TestExecuteCreatePlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_posts_new_user_with_cloned_roles_license_group(self, mock_urlopen) -> None:
        plan = {
            "new_username": "10087",
            "new_name": "New Person",
            "new_extension": "10087",
            "roles": TEMPLATE_USER["roles"],
            "license": TEMPLATE_USER["license"],
            "group": "7719499780",
        }
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "5555", "status": "ok"}),  # POST /api/users
            _FakeResponse({"id": "device-1"}),  # POST /api/devices
            _FakeResponse({"id": "5555"}),  # GET refetch (no "devices" key -> no default-device PUT)
        ]

        result = _actions().execute_create_plan(plan)

        post_request = mock_urlopen.call_args_list[0].args[0]
        self.assertEqual(post_request.get_method(), "POST")
        sent_body = json.loads(post_request.data)

        self.assertEqual(sent_body["username"], "10087")
        self.assertEqual(sent_body["name"], "New Person")
        self.assertEqual(sent_body["extension"], "10087")
        self.assertEqual(sent_body["roles"], TEMPLATE_USER["roles"])
        self.assertEqual(sent_body["license"], TEMPLATE_USER["license"])
        self.assertEqual(sent_body["profile"]["group"], "7719499780")
        self.assertFalse(sent_body["force_password_change"])
        self.assertRegex(sent_body["password"], _PASSWORD_FORMAT)

        # The generated password is returned to the caller (so it can be
        # shown to the approver once) but is a fresh random value, never a
        # hardcoded/reused one.
        self.assertEqual(result["temporary_password"], sent_body["password"])
        self.assertEqual(result["id"], "5555")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_also_creates_a_matching_device(self, mock_urlopen) -> None:
        """Real-world gap confirmed 2026-09-21: a user created with no
        device can't actually register a SIP phone/softphone. The device
        must reuse the same generated password and reference the new
        user's id (real request shape confirmed from Webitel's own "New
        device" form)."""

        plan = {
            "new_username": "10087",
            "new_name": "New Person",
            "new_extension": "10087",
            "roles": [],
            "license": [],
            "group": "",
        }
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "5555", "status": "ok"}),
            _FakeResponse({"id": "device-1"}),
            _FakeResponse({"id": "5555"}),  # GET refetch (no "devices" key -> no default-device PUT)
        ]

        result = _actions().execute_create_plan(plan)

        device_request = mock_urlopen.call_args_list[1].args[0]
        self.assertEqual(device_request.get_method(), "POST")
        self.assertIn("/api/devices", device_request.full_url)
        device_body = json.loads(device_request.data)

        self.assertEqual(device_body["name"], "10087")
        self.assertEqual(device_body["account"], "10087")
        self.assertEqual(device_body["password"], result["temporary_password"])
        self.assertEqual(device_body["user"]["id"], "5555")
        self.assertEqual(device_body["user"]["name"], "New Person")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_sets_the_new_device_as_the_users_default_device(self, mock_urlopen) -> None:
        """Real gap confirmed 2026-09-21: creating a device links it to the
        user (it appears in the user's own ``devices`` list automatically),
        but Webitel's own "Default device" field (required in its UI) is a
        separate, singular ``device`` property that's left unset unless
        explicitly PUT — found by creating a real user through this exact
        code path and seeing "Default device" still empty in the UI."""

        plan = {"new_username": "10087", "new_name": "New Person", "new_extension": "10087", "roles": [], "license": [], "group": ""}
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "5555"}),  # POST /api/users
            _FakeResponse({"id": "device-1"}),  # POST /api/devices
            _FakeResponse({"id": "5555", "name": "New Person", "devices": [{"id": "150444", "name": "10087"}]}),  # GET refetch
            _FakeResponse({}),  # PUT setting device
        ]

        _actions().execute_create_plan(plan)

        put_request = mock_urlopen.call_args_list[3].args[0]
        self.assertEqual(put_request.get_method(), "PUT")
        self.assertIn("/api/users/5555", put_request.full_url)
        sent_body = json.loads(put_request.data)
        self.assertEqual(sent_body["device"], {"id": "150444", "name": "10087"})
        # Everything else from the fresh re-fetch survives unchanged.
        self.assertEqual(sent_body["name"], "New Person")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_each_call_generates_a_different_password(self, mock_urlopen) -> None:
        plan = {"new_username": "a", "new_name": "A", "new_extension": "1", "roles": [], "license": [], "group": ""}
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "1"}),
            _FakeResponse({}),
            _FakeResponse({"id": "1"}),  # GET refetch, no devices
            _FakeResponse({"id": "2"}),
            _FakeResponse({}),
            _FakeResponse({"id": "2"}),  # GET refetch, no devices
        ]

        result1 = _actions().execute_create_plan(plan)
        result2 = _actions().execute_create_plan(plan)

        self.assertNotEqual(result1["temporary_password"], result2["temporary_password"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_execute_create_wraps_http_error(self, mock_urlopen) -> None:
        import urllib.error

        plan = {"new_username": "a", "new_name": "A", "new_extension": "1", "roles": [], "license": [], "group": ""}
        mock_urlopen.side_effect = [
            urllib.error.HTTPError(
                url="https://wtl-alm.astana-motors.kz/api/users",
                code=409,
                msg="Conflict",
                hdrs=None,
                fp=BytesIO(b'{"detail":"already exists"}'),
            ),
        ]
        with self.assertRaises(WebitelApiError):
            _actions().execute_create_plan(plan)


class TestBuildDeletePlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_carries_user_identity_and_no_presence_warning_when_idle(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # get_raw_user search
            _FakeResponse({"id": "6078", "extension": "10087", "name": "Test User 10087", "username": "10087", "profile": {"group": "ALM-MK"}}),  # get_raw_user detail
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # get_presence_status search: no "presence" key
        ]
        plan = _actions().build_delete_plan("10087")
        self.assertEqual(
            plan,
            {
                "extension": "10087",
                "id": "6078",
                "name": "Test User 10087",
                "username": "10087",
                "group": "ALM-MK",
                "presence_status": None,
                "presence": [],
                "in_call": False,
                "device_ids": [],
            },
        )

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_carries_device_ids_to_delete(self, mock_urlopen) -> None:
        """Real-world gap confirmed 2026-09-21: deleting a user does NOT
        cascade-delete their device — it's left orphaned unless removed
        explicitly, so build_delete_plan must surface it up front."""

        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
            _FakeResponse(
                {
                    "id": "6078",
                    "extension": "10087",
                    "name": "Test User 10087",
                    "username": "10087",
                    "profile": {},
                    "devices": [{"id": "150443", "name": "10087"}],
                }
            ),
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
        ]
        plan = _actions().build_delete_plan("10087")
        self.assertEqual(plan["device_ids"], ["150443"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_surfaces_presence_status_when_active(self, mock_urlopen) -> None:
        """Real-world case confirmed 2026-09-21: deleting a user with an
        active SIP registration/call tends to fail in Webitel — the plan
        must surface this so the operator isn't surprised at confirm
        time."""

        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "1138", "extension": "10078"}]}),
            _FakeResponse({"id": "1138", "extension": "10078", "name": "madi muratbay", "username": "10078", "profile": {}}),
            _FakeResponse({"items": [{"id": "1138", "extension": "10078", "presence": {"status": "{sip}"}}]}),
        ]
        plan = _actions().build_delete_plan("10078")
        self.assertEqual(plan["presence_status"], "{sip}")
        self.assertEqual(plan["presence"], ["SIP"])
        self.assertFalse(plan["in_call"])

    def test_presence_labels_match_webitels_own_status_chips(self) -> None:
        labels = WebitelApiActions.presence_labels
        self.assertEqual(labels(None), [])
        self.assertEqual(labels(""), [])
        self.assertEqual(labels("{sip}"), ["SIP"])
        self.assertEqual(labels("{web,sip,dlg}"), ["Web", "SIP", "Dlg"])
        self.assertEqual(labels("{dnd}"), ["DnD"])
        # An unknown marker still shows up — never silently treated as idle.
        self.assertEqual(labels("{wss}"), ["wss"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_never_issues_a_write(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
            _FakeResponse({"id": "6078", "extension": "10087", "name": "x", "profile": {}}),
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
        ]
        _actions().build_delete_plan("10087")
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")


class TestExecuteDeletePlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_deletes_after_refetch_confirms_same_id(self, mock_urlopen) -> None:
        plan = {"extension": "10087", "id": "6078", "name": "Test User 10087"}
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # re-fetch search
            _FakeResponse({"id": "6078", "extension": "10087", "name": "Test User 10087"}),  # re-fetch full
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # presence re-check: idle
            _FakeResponse({}),  # POST .../logout response
            _FakeResponse(""),  # DELETE response (empty body)
        ]
        result = _actions().execute_delete_plan(plan)

        self.assertEqual(
            result,
            {
                "deleted": True,
                "id": "6078",
                "extension": "10087",
                "name": "Test User 10087",
                "deleted_device_ids": [],
            },
        )

        logout_request = mock_urlopen.call_args_list[3].args[0]
        self.assertEqual(logout_request.get_method(), "POST")
        self.assertIn("/api/users/6078/logout", logout_request.full_url)

        delete_request = mock_urlopen.call_args_list[4].args[0]
        self.assertEqual(delete_request.get_method(), "DELETE")
        self.assertIn("permanent=true", delete_request.full_url)
        self.assertIn("/api/users/6078", delete_request.full_url)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_deletes_devices_before_deleting_the_user(self, mock_urlopen) -> None:
        plan = {"extension": "10087", "id": "6078", "name": "Test User 10087", "device_ids": ["150443", "150444"]}
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # re-fetch search
            _FakeResponse({"id": "6078", "extension": "10087", "name": "Test User 10087"}),  # re-fetch full
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # presence re-check: idle
            _FakeResponse({}),  # POST .../logout
            _FakeResponse({}),  # DELETE device 150443
            _FakeResponse({}),  # DELETE device 150444
            _FakeResponse(""),  # DELETE user
        ]

        result = _actions().execute_delete_plan(plan)

        self.assertEqual(result["deleted_device_ids"], ["150443", "150444"])

        device_delete_1 = mock_urlopen.call_args_list[4].args[0]
        self.assertEqual(device_delete_1.get_method(), "DELETE")
        self.assertIn("/api/devices/150443", device_delete_1.full_url)

        device_delete_2 = mock_urlopen.call_args_list[5].args[0]
        self.assertEqual(device_delete_2.get_method(), "DELETE")
        self.assertIn("/api/devices/150444", device_delete_2.full_url)

        user_delete = mock_urlopen.call_args_list[6].args[0]
        self.assertEqual(user_delete.get_method(), "DELETE")
        self.assertIn("/api/users/6078", user_delete.full_url)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_logout_happens_before_delete_even_when_idle(self, mock_urlopen) -> None:
        """Logout is unconditional, not gated on build_delete_plan's
        presence_status — that snapshot could be stale by confirm time,
        and logging out an already-idle user is a harmless no-op."""

        plan = {"extension": "10087", "id": "6078", "name": "x", "presence_status": None}
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
            _FakeResponse({"id": "6078", "extension": "10087"}),
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # presence re-check: idle
            _FakeResponse({}),
            _FakeResponse(""),
        ]
        _actions().execute_delete_plan(plan)

        methods = [call.args[0].get_method() for call in mock_urlopen.call_args_list]
        self.assertEqual(methods, ["GET", "GET", "GET", "POST", "DELETE"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_to_delete_someone_in_a_call_even_if_the_plan_said_idle(self, mock_urlopen) -> None:
        """A call may start between "Показать план" and "Подтвердить": the
        status is re-checked at delete time, and a user in Dlg is neither
        logged out (that would drop the call) nor deleted."""

        plan = {"extension": "10087", "id": "6078", "name": "x", "presence": [], "in_call": False}
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
            _FakeResponse({"id": "6078", "extension": "10087"}),
            _FakeResponse({"items": [{"id": "6078", "extension": "10087", "presence": {"status": "{sip,dlg}"}}]}),
        ]
        with self.assertRaises(WebitelApiError) as ctx:
            _actions().execute_delete_plan(plan)
        self.assertIn("Dlg", str(ctx.exception))
        methods = [call.args[0].get_method() for call in mock_urlopen.call_args_list]
        self.assertEqual(methods, ["GET", "GET", "GET"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_when_extension_now_belongs_to_a_different_user(self, mock_urlopen) -> None:
        plan = {"extension": "10087", "id": "6078", "name": "Test User 10087"}
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "9999", "extension": "10087"}]}),  # different id now!
            _FakeResponse({"id": "9999", "extension": "10087", "name": "someone else"}),
        ]
        with self.assertRaises(WebitelApiError):
            _actions().execute_delete_plan(plan)
        # No DELETE call attempted.
        for call in mock_urlopen.call_args_list:
            self.assertNotEqual(call.args[0].get_method(), "DELETE")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_wraps_http_error(self, mock_urlopen) -> None:
        import urllib.error

        plan = {"extension": "10087", "id": "6078", "name": "x"}
        mock_urlopen.side_effect = [
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),
            _FakeResponse({"id": "6078", "extension": "10087"}),
            _FakeResponse({"items": [{"id": "6078", "extension": "10087"}]}),  # presence re-check: idle
            _FakeResponse({}),  # POST .../logout succeeds
            urllib.error.HTTPError(
                url="https://wtl-alm.astana-motors.kz/api/users/6078",
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=BytesIO(b'{"detail":"no permission"}'),
            ),
        ]
        with self.assertRaises(WebitelApiError):
            _actions().execute_delete_plan(plan)


class TestPasswordReset(unittest.TestCase):
    """The device write itself is patched out here: what matters is that
    user and devices end up with one and the same password."""

    TEST_USER = {"id": "6197", "name": "TEST1", "username": "30120", "extension": "30120", "roles": [{"id": "28"}],
                 "device": {"id": "150571", "name": "30120"}, "devices": [{"id": "150571", "name": "30120"}]}
    PLAN = {"extension": "30120", "id": "6197", "name": "TEST1", "devices": [{"id": "150571", "name": "30120"}], "city": "almaty"}

    def _found(self, user: dict | None = None) -> list[_FakeResponse]:
        user = user or self.TEST_USER
        return [_FakeResponse({"items": [{"id": user["id"], "extension": user["extension"]}]}), _FakeResponse(user)]

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_lists_the_devices_and_who_is_online(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            *self._found(),
            _FakeResponse({"items": [{"id": "6197", "extension": "30120", "presence": {"status": "{sip}"}}]}),
        ]
        plan = _actions().build_password_plan("30120")
        self.assertEqual((plan["devices"], plan["presence"]), ([{"id": "150571", "name": "30120"}], ["SIP"]))
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")

    @patch.object(WebitelApiActions, "_set_device_password")
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_user_and_device_get_the_same_new_password(self, mock_urlopen, set_device) -> None:
        mock_urlopen.side_effect = [*self._found(), _FakeResponse({})]
        result = _actions().execute_password_plan(self.PLAN)

        put = mock_urlopen.call_args_list[2].args[0]
        sent = json.loads(put.data)
        self.assertEqual((put.get_method(), put.full_url), ("PUT", "https://wtl-alm.astana-motors.kz/api/users/6197"))
        self.assertRegex(sent["password"], _PASSWORD_FORMAT)
        self.assertEqual({k: v for k, v in sent.items() if k != "password"}, self.TEST_USER)
        set_device.assert_called_once_with("150571", sent["password"])
        self.assertEqual((result["new_password"], result["devices_updated"]), (sent["password"], ["30120"]))

    @patch.object(WebitelApiActions, "_set_device_password", side_effect=WebitelApiError("HTTP 400"))
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_failed_device_keeps_the_users_new_password(self, mock_urlopen, _set_device) -> None:
        mock_urlopen.side_effect = [*self._found(), _FakeResponse({})]
        result = _actions().execute_password_plan(self.PLAN)
        self.assertRegex(result["new_password"], _PASSWORD_FORMAT)
        self.assertIn("HTTP 400", result["devices_error"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_when_the_extension_now_belongs_to_someone_else(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = self._found({**self.TEST_USER, "id": "7000"})
        with self.assertRaises(WebitelApiError):
            _actions().execute_password_plan(self.PLAN)
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")

    @patch.object(WebitelApiActions, "_set_device_password")
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_copys_reset_checkbox_also_sets_the_device(self, mock_urlopen, set_device) -> None:
        plan = {"target_id": "9999", "roles": [], "license": [], "group": "x", "reset_password": True}
        mock_urlopen.side_effect = [_FakeResponse(TARGET_USER), _FakeResponse({})]
        result = _actions().execute_plan(plan)
        set_device.assert_called_once_with("222222", result["new_password"])
        self.assertEqual(result["devices_updated"], ["10079"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_device_write_sends_what_webitels_devices_form_sends(self, mock_urlopen) -> None:
        """Body as captured from the form's «Save» on test device 10085
        (2026-11-03); the GET leaves empty fields out."""
        mock_urlopen.side_effect = [
            _FakeResponse({"id": "150647", "name": "10085", "account": "10085", "user": {"id": "6270", "name": "TEST"},
                           "logged_in": "1792387450078"}),
            _FakeResponse({}),
        ]
        _actions()._set_device_password("150647", "N3wPassw0rdAbcde")

        put = mock_urlopen.call_args_list[1].args[0]
        self.assertEqual((put.get_method(), put.full_url), ("PUT", "https://wtl-alm.astana-motors.kz/api/devices/150647"))
        self.assertEqual(json.loads(put.data), {
            "name": "10085", "account": "10085", "password": "N3wPassw0rdAbcde", "user": {"id": "6270", "name": "TEST"},
            "phone": {}, "ip": "", "brand": "", "model": "", "mac": "", "hotdesks": [], "hotdesk": False,
        })

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_device_write_keeps_a_desk_phones_details(self, mock_urlopen) -> None:
        desk_phone = {"name": "10090", "account": "10090", "user": {"id": "7", "name": "X"}, "phone": {"id": "3", "name": "Yealink"},
                      "ip": "10.0.0.5", "brand": "Yealink", "model": "T46U", "mac": "80:5e:c0:00:00:01",
                      "hotdesks": ["101"], "hotdesk": True}
        mock_urlopen.side_effect = [_FakeResponse({"id": "9", "logged_in": "1", **desk_phone}), _FakeResponse({})]
        _actions()._set_device_password("9", "N3wPassw0rdAbcde")
        self.assertEqual(json.loads(mock_urlopen.call_args_list[1].args[0].data), {**desk_phone, "password": "N3wPassw0rdAbcde"})

    @patch.object(WebitelApiActions, "_set_device_password")
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_copy_without_reset_leaves_devices_alone(self, mock_urlopen, set_device) -> None:
        plan = {"target_id": "9999", "roles": [], "license": [], "group": "x"}
        mock_urlopen.side_effect = [_FakeResponse(TARGET_USER), _FakeResponse({})]
        result = _actions().execute_plan(plan)
        set_device.assert_not_called()
        self.assertNotIn("devices_updated", result)


# The operator's group number template exactly as pasted from Webitel's Flow
# code editor (2026-11-02, Almaty).
_OPERATOR_GROUP_TEMPLATE = json.loads("""[
  {"set": {"continue_on_fail": "true", "hangup_after_bridge": "true"}},
  {"ringback": {"all": true,
                "call": {"name": "%(1000,6000,425);loops=-1", "type": "tone"},
                "hold": {"name": "%(1000,6000,425);loops=-1", "type": "tone"},
                "transfer": {"id": 488}}},
  {"ringReady": ""},
  {"preAnswer": ""},
  {"recordSession": {"action": "start", "bridged": true, "followTransfer": true,
                     "minSec": 2, "stereo": true, "type": "mp3"}},
  {"log": ">> ALM-OP-30151 <<<"},
  {"bridge": {"endpoints": [
      {"extension": "30152", "parameters": {"leg_timeout": "30"}, "type": "user"},
      {"extension": "30153", "parameters": {"leg_timeout": "30"}, "type": "user"},
      {"extension": "30154", "parameters": {"leg_timeout": "30"}, "type": "user"},
      {"extension": "30155", "parameters": {"leg_timeout": "30"}, "type": "user"},
      {"extension": "30156", "parameters": {"leg_timeout": "30"}, "type": "user"},
      {"extension": "30157", "parameters": {"leg_timeout": "30"}, "type": "user"}],
    "strategy": "fork"}},
  {"bridge": {"bridged": [{"log": "Ok call is bridged"}, {"set": {"busy": "2"}}, {"log": "END call is bridged"}]}},
  {"hangup": ""}
]""")


def _astana_members(*extensions: str) -> str:
    return ", ".join(
        f'{{"extension": "{e}", "parameters": {{"leg_timeout": "15"}}, "type": "user"}}' for e in extensions
    )


_ASTANA_START = """
  {"set": {"continue_on_fail": "true", "hangup_after_bridge": "true"}},
  {"ringReady": ""},
  {"preAnswer": ""},
  {"recordSession": {"action": "start", "bridged": true, "followTransfer": true,
                     "minSec": 2, "stereo": true, "type": "mp3"}},"""

# Both of the operator's Astana group schemas as pasted (2026-11-03): one hands
# an unanswered call on to 21700 (an int there, and only if 21700 is idle),
# the other just ends.
_ASTANA_WITH_FALLBACK = json.loads("[" + _ASTANA_START + """
  {"log": ">> BMW Astana - 21701 <<<"},
  {"bridge": {"endpoints": [""" + _astana_members("21703", "21704", "21705", "21706", "21707") + """],
              "strategy": "fork"}},
  {"bridge": {"endpoints": [{"extension": 21700, "idle": true, "type": "user"}]}},
  {"hangup": ""}
]""")
_ASTANA_WITHOUT_FALLBACK = json.loads("[" + _ASTANA_START + """
  {"log": ">> AST-AMP - 28020 <<<"},
  {"bridge": {"endpoints": [""" + _astana_members(*(str(n) for n in range(21322, 21330))) + """],
              "strategy": "fork"}},
  {"hangup": ""}
]""")


def _user_found(extension: str, name: str) -> list[_FakeResponse]:
    """get_raw_user's two GETs for an existing user."""
    return [
        _FakeResponse({"items": [{"id": "u" + extension, "extension": extension}]}),
        _FakeResponse({"id": "u" + extension, "extension": extension, "name": name}),
    ]


class TestGroupSchema(unittest.TestCase):
    def test_matches_the_operators_template(self) -> None:
        schema = group_schema("almaty", "ALM-OP-30151", ["30152", "30153", "30154", "30155", "30156", "30157"])
        self.assertEqual(schema, _OPERATOR_GROUP_TEMPLATE)

    def test_only_name_and_members_change(self) -> None:
        schema = group_schema("almaty", "Касса", ["10087"])
        self.assertEqual(schema[5], {"log": ">> Касса <<<"})
        self.assertEqual([e["extension"] for e in schema[6]["bridge"]["endpoints"]], ["10087"])
        template = [step for i, step in enumerate(_OPERATOR_GROUP_TEMPLATE) if i not in (5, 6)]
        self.assertEqual([step for i, step in enumerate(schema) if i not in (5, 6)], template)

    def test_astana_with_a_fallback_matches_the_operators_schema(self) -> None:
        schema = group_schema("astana", "BMW Astana - 21701", ["21703", "21704", "21705", "21706", "21707"], "21700")
        self.assertEqual(schema, _ASTANA_WITH_FALLBACK)

    def test_astana_without_a_fallback_matches_the_operators_schema(self) -> None:
        schema = group_schema("astana", "AST-AMP - 28020", [str(n) for n in range(21322, 21330)])
        self.assertEqual(schema, _ASTANA_WITHOUT_FALLBACK)

    def test_almatys_template_has_no_fallback(self) -> None:
        with self.assertRaises(WebitelApiError):
            group_schema("almaty", "G", ["30152"], "30100")

    def test_a_city_without_a_template_is_refused(self) -> None:
        with self.assertRaises(WebitelApiError):
            group_schema("shymkent", "G", ["30152"])


_NO_SCHEMA = {"items": [{"id": 12, "name": "ALM-OP-30151 old"}]}  # only a partial name match
_NO_ROUTE = {"page": 1, "items": [{"id": 5, "name": "internal", "pattern": "^30[0-9]{3}$"}]}


class TestFindRoute(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_reads_every_page_and_matches_the_exact_pattern(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse({"page": 1, "next": True, "items": [{"id": 1, "name": "a", "pattern": "^301510$"}]}),
            _FakeResponse({"page": 2, "items": [{"id": 2, "name": "TEST-AUTO", "pattern": "^30151$"}]}),
        ]
        self.assertEqual(_actions().find_route("30151")["name"], "TEST-AUTO")
        self.assertIn("page=2", mock_urlopen.call_args_list[1].args[0].full_url)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_none_when_no_route_has_it(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse(_NO_ROUTE)
        self.assertIsNone(_actions().find_route("30151"))


class TestBuildGroupPlan(unittest.TestCase):
    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_plan_lists_members_and_carries_the_schema(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA),
            _FakeResponse(_NO_ROUTE),
            *_user_found("30152", "Иван"),
            *_user_found("30153", "Мария"),
            _FakeResponse({"items": []}),  # the group number is nobody's extension
        ]
        plan = _actions().build_group_plan(
            city="almaty", name="ALM-OP-30151", number="30151", extensions=["30152", "30153", "30152"],
        )

        self.assertEqual(plan["members"], [{"extension": "30152", "name": "Иван"}, {"extension": "30153", "name": "Мария"}])
        self.assertEqual((plan["number"], plan["number_user"]), ("30151", ""))
        self.assertEqual(plan["schema"], group_schema("almaty", "ALM-OP-30151", ["30152", "30153"]))
        self.assertEqual(mock_urlopen.call_count, 7)  # the repeated 30152 is looked up once
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_flags_a_number_that_is_a_users_extension(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA), _FakeResponse(_NO_ROUTE), *_user_found("30152", "Иван"), *_user_found("30151", "Олег"),
        ]
        plan = _actions().build_group_plan(name="G", number="30151", extensions=["30152"], city="almaty")
        self.assertEqual(plan["number_user"], "Олег")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_only_digits_go_into_the_route_pattern(self, mock_urlopen) -> None:
        for number in ("30.*", "301 51", "^30151$", "٣٠١٥١"):
            with self.assertRaises(WebitelApiError, msg=number):
                _actions().build_group_plan(name="G", number=number, extensions=["30152"], city="almaty")
        mock_urlopen.assert_not_called()

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_the_group_number_among_its_members(self, mock_urlopen) -> None:
        with self.assertRaises(WebitelApiError):
            _actions().build_group_plan(name="G", number="30151", extensions=["30152", "30151"], city="almaty")
        mock_urlopen.assert_not_called()

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_a_name_already_taken(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse({"items": [{"id": 370, "name": "TEST-AUTO"}]})
        with self.assertRaises(WebitelApiError) as ctx:
            _actions().build_group_plan(name="TEST-AUTO", number="30151", extensions=["30152"], city="almaty")
        self.assertIn("уже есть", str(ctx.exception))

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_a_number_a_route_already_leads(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA),
            _FakeResponse({"items": [{"id": 9, "name": "TEST-AUTO", "pattern": "^55555$"}]}),
        ]
        with self.assertRaises(WebitelApiError) as ctx:
            _actions().build_group_plan(name="G", number="55555", extensions=["30152"], city="almaty")
        self.assertIn("TEST-AUTO", str(ctx.exception))

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_names_every_missing_member_at_once(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA),
            _FakeResponse(_NO_ROUTE),
            _FakeResponse({"items": []}),  # 30158: no such user
            *_user_found("30152", "Иван"),
            _FakeResponse({"items": []}),  # 30159: no such user
        ]
        with self.assertRaises(WebitelApiError) as ctx:
            _actions().build_group_plan(name="G", number="30151", extensions=["30158", "30152", "30159"], city="almaty")
        self.assertIn("30158, 30159", str(ctx.exception))

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_astana_fallback_is_looked_up_and_shown(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA), _FakeResponse(_NO_ROUTE),
            *_user_found("21703", "Иван"), *_user_found("21700", "Ресепшн"),
            _FakeResponse({"items": []}),
        ]
        plan = _actions().build_group_plan(
            city="astana", name="BMW Astana - 21701", number="21701", extensions=["21703"], fallback="21700",
        )
        self.assertEqual((plan["fallback"], plan["fallback_name"], plan["ring_seconds"]), ("21700", "Ресепшн", "15"))
        self.assertEqual(plan["schema"], group_schema("astana", "BMW Astana - 21701", ["21703"], "21700"))

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_missing_fallback_is_named_with_the_missing_members(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA), _FakeResponse(_NO_ROUTE),
            _FakeResponse({"items": []}),  # 21709
            _FakeResponse({"items": []}),  # 21799
        ]
        with self.assertRaises(WebitelApiError) as ctx:
            _actions().build_group_plan(city="astana", name="G", number="21701", extensions=["21709"], fallback="21799")
        self.assertIn("21709, 21799", str(ctx.exception))

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_bad_fallbacks_are_refused_before_any_request(self, mock_urlopen) -> None:
        for city, fallback in (("astana", "21701"), ("astana", "021700"), ("astana", "2170*"), ("almaty", "30100")):
            with self.assertRaises(WebitelApiError, msg=(city, fallback)):
                _actions().build_group_plan(city=city, name="G", number="21701", extensions=["21703"], fallback=fallback)
        mock_urlopen.assert_not_called()


class TestExecuteGroupPlan(unittest.TestCase):
    PLAN = {"name": "ALM-OP-30151", "number": "30151", "members": [{"extension": "30152", "name": "Иван"}],
            "schema": [{"hangup": ""}], "city": "almaty"}

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_posts_the_schema_then_the_route_to_it(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA), _FakeResponse(_NO_ROUTE),
            _FakeResponse({"id": 371, "name": "ALM-OP-30151"}),
            _FakeResponse({"id": 88, "name": "ALM-OP-30151"}),
        ]
        result = _actions().execute_group_plan(self.PLAN)

        schema_post, route_post = (c.args[0] for c in mock_urlopen.call_args_list[2:])
        self.assertEqual((schema_post.get_method(), schema_post.full_url),
                         ("POST", "https://wtl-alm.astana-motors.kz/api/routing/schema"))
        self.assertEqual(json.loads(schema_post.data), {
            "name": "ALM-OP-30151", "type": "voice", "editor": False,
            "schema": group_schema("almaty", "ALM-OP-30151", ["30152"]), "payload": {}, "tags": [],
        })
        self.assertEqual((route_post.get_method(), route_post.full_url),
                         ("POST", "https://wtl-alm.astana-motors.kz/api/routing/outbound/calls"))
        self.assertEqual(json.loads(route_post.data), {
            "name": "ALM-OP-30151",
            "schema": {"type": "voice", "editor": False, "id": "371", "name": "ALM-OP-30151"},
            "pattern": "^30151$",
            "description": "",
        })
        self.assertEqual(result, {"id": 371, "name": "ALM-OP-30151", "number": "30151", "members": ["30152"], "route_id": 88})

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_failed_route_still_reports_the_created_schema(self, mock_urlopen) -> None:
        import urllib.error

        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA), _FakeResponse(_NO_ROUTE),
            _FakeResponse({"id": 371}),
            urllib.error.HTTPError(url="x", code=400, msg="Bad Request", hdrs=None, fp=BytesIO(b"bad pattern")),
        ]
        result = _actions().execute_group_plan(self.PLAN)
        self.assertEqual(result["id"], 371)
        self.assertIn("HTTP 400", result["route_error"])

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_when_the_name_was_taken_since_the_plan(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeResponse({"items": [{"id": 371, "name": "ALM-OP-30151"}]})
        with self.assertRaises(WebitelApiError):
            _actions().execute_group_plan(self.PLAN)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_refuses_when_a_route_took_the_number_since_the_plan(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA),
            _FakeResponse({"items": [{"id": 9, "name": "other", "pattern": "^30151$"}]}),
        ]
        with self.assertRaises(WebitelApiError):
            _actions().execute_group_plan(self.PLAN)
        for call in mock_urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "GET")

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_a_tampered_number_never_reaches_the_pattern(self, mock_urlopen) -> None:
        with self.assertRaises(WebitelApiError):
            _actions().execute_group_plan({**self.PLAN, "number": ".*"})
        mock_urlopen.assert_not_called()

    @patch("crm_ai_agent.adapters.webitel_api.actions.urllib.request.urlopen")
    def test_astana_posts_its_own_template_with_the_fallback(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _FakeResponse(_NO_SCHEMA), _FakeResponse(_NO_ROUTE), _FakeResponse({"id": 55}), _FakeResponse({"id": 9}),
        ]
        plan = {"city": "astana", "name": "BMW Astana - 21701", "number": "21701", "fallback": "21700",
                "members": [{"extension": e, "name": ""} for e in ("21703", "21704", "21705", "21706", "21707")]}
        _actions().execute_group_plan(plan)
        self.assertEqual(json.loads(mock_urlopen.call_args_list[2].args[0].data)["schema"], _ASTANA_WITH_FALLBACK)


if __name__ == "__main__":
    unittest.main()
