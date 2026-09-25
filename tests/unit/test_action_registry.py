import unittest

from crm_ai_agent.domain.entities import PlanAction
from crm_ai_agent.domain.enums import RiskLevel
from crm_ai_agent.safety.action_registry import (
    ActionRegistryError,
    get_action_spec,
    is_ticket_type_allowed,
    validate_against_registry,
)


def _make_action(**overrides) -> PlanAction:
    defaults = dict(
        action_id="a1",
        action_type="edit_crm_user_name",
        arguments={"user_id": "u1", "first_name": "Ivan", "last_name": "Petrov"},
        preconditions=[],
        expected_before_state={},
        expected_after_state={},
        risk_level=RiskLevel.MEDIUM,
        idempotency_key="idem-1",
    )
    defaults.update(overrides)
    return PlanAction(**defaults)


class TestActionRegistry(unittest.TestCase):
    def test_get_action_spec_rejects_unknown_action_type(self) -> None:
        with self.assertRaises(ActionRegistryError):
            get_action_spec("delete_everything")

    def test_valid_edit_crm_user_name_action_passes(self) -> None:
        action = _make_action()
        spec = validate_against_registry(action)
        self.assertEqual(spec.action_type.value, "edit_crm_user_name")
        self.assertTrue(spec.required_approval)

    def test_missing_required_argument_is_rejected(self) -> None:
        action = _make_action(arguments={"user_id": "u1", "first_name": "Ivan"})
        with self.assertRaises(ActionRegistryError):
            validate_against_registry(action)

    def test_unregistered_extra_argument_is_rejected(self) -> None:
        action = _make_action(
            arguments={"user_id": "u1", "first_name": "Ivan", "last_name": "Petrov", "phone": "+1234567890"}
        )
        with self.assertRaises(ActionRegistryError):
            validate_against_registry(action)

    def test_mismatched_risk_level_is_rejected(self) -> None:
        action = _make_action(risk_level=RiskLevel.LOW)
        with self.assertRaises(ActionRegistryError):
            validate_against_registry(action)

    def test_internal_only_action_is_rejected_by_default(self) -> None:
        action = _make_action(
            action_type="close_ticket",
            arguments={},
            risk_level=RiskLevel.LOW,
        )
        with self.assertRaises(ActionRegistryError):
            validate_against_registry(action)

    def test_internal_only_action_allowed_when_explicitly_permitted(self) -> None:
        action = _make_action(
            action_type="close_ticket",
            arguments={},
            risk_level=RiskLevel.LOW,
        )
        spec = validate_against_registry(action, allow_internal_only=True)
        self.assertTrue(spec.internal_only)

    def test_ticket_type_restriction(self) -> None:
        spec = get_action_spec("edit_crm_user_name")
        self.assertTrue(is_ticket_type_allowed(spec, "change_user_name"))
        self.assertFalse(is_ticket_type_allowed(spec, "unknown"))

    def test_unrestricted_action_allows_any_ticket_type(self) -> None:
        # add_ticket_comment declares no allowed_ticket_types restriction.
        spec = get_action_spec("add_ticket_comment")
        self.assertTrue(is_ticket_type_allowed(spec, "change_user_name"))
        self.assertTrue(is_ticket_type_allowed(spec, "anything_else"))


class TestProvisionWebitelUserAction(unittest.TestCase):
    """provision_webitel_user: designed from real read-only Webitel recon
    (see docs/open_questions.md). Roles/License/group are cloned from a
    template user; General info is entered fresh; the password itself is
    deliberately not a plan argument (generated live at execution time)."""

    def _make_provision_action(self, **overrides) -> PlanAction:
        defaults = dict(
            action_id="a1",
            action_type="provision_webitel_user",
            arguments={
                "template_user_login": "10078",
                "new_user_login": "10079",
                "new_user_name": "Yerlan Akhmetov",
                "new_user_extension": "10079",
                "roles": "agent,supervisor",
                "license": "CALL_CENTER,CHAT",
                "group": "7719499780",
                "temporary_password": "true",
            },
            preconditions=["template_user_exists", "new_login_available"],
            expected_before_state={},
            expected_after_state={"roles": "agent,supervisor", "license": "CALL_CENTER,CHAT", "group": "7719499780"},
            risk_level=RiskLevel.HIGH,
            idempotency_key="idem-provision-1",
        )
        defaults.update(overrides)
        return PlanAction(**defaults)

    def test_valid_provision_action_passes_and_requires_approval(self) -> None:
        action = self._make_provision_action()
        spec = validate_against_registry(action)
        self.assertEqual(spec.risk_level, RiskLevel.HIGH)
        self.assertTrue(spec.required_approval)

    def test_password_is_not_an_accepted_argument(self) -> None:
        """The password must never be baked into a plan — it is generated
        live during execution, not decided ahead of approval."""
        action = self._make_provision_action(
            arguments={
                "template_user_login": "10078",
                "new_user_login": "10079",
                "new_user_name": "Yerlan Akhmetov",
                "new_user_extension": "10079",
                "roles": "agent",
                "license": "CALL_CENTER",
                "group": "7719499780",
                "temporary_password": "true",
                "password": "hunter2",
            }
        )
        with self.assertRaises(ActionRegistryError):
            validate_against_registry(action)

    def test_missing_group_argument_is_rejected(self) -> None:
        args = self._make_provision_action().arguments
        incomplete = {k: v for k, v in args.items() if k != "group"}
        action = self._make_provision_action(arguments=incomplete)
        with self.assertRaises(ActionRegistryError):
            validate_against_registry(action)

    def test_restricted_to_access_to_information_system_ticket_type(self) -> None:
        spec = get_action_spec("provision_webitel_user")
        self.assertTrue(is_ticket_type_allowed(spec, "access_to_information_system"))
        self.assertFalse(is_ticket_type_allowed(spec, "change_user_name"))


if __name__ == "__main__":
    unittest.main()
