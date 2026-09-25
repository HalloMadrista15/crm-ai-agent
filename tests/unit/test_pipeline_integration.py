"""End-to-end test of everything built so far, wired together without any
network call, Telegram bot, or paid LLM: CRM fixture reader -> rule-based
interpreter -> fixture entity resolver -> Plan Compiler (Policy Engine +
Action Registry + canonical hashing inside it).

This is what proves the free rule-based interpreter and the fixture-backed
adapters produce output the rest of the safety stack accepts and acts on
correctly, for both a clean request and a suspicious one.
"""

import unittest
from pathlib import Path

from crm_ai_agent.adapters.crm_fixture.entity_resolver import FixtureEntityResolver
from crm_ai_agent.adapters.crm_fixture.reader import FixtureCrmReader
from crm_ai_agent.adapters.rule_based_interpreter.interpreter import RuleBasedTicketInterpreter
from crm_ai_agent.domain.enums import PolicyDecisionType
from crm_ai_agent.safety.plan_compiler import compile_edit_user_name_plan

TICKETS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tickets"
USERS_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "crm_users.json"


class TestPipelineIntegration(unittest.TestCase):
    def test_clean_approved_request_compiles_a_plan_ready_for_approval(self) -> None:
        reader = FixtureCrmReader(TICKETS_DIR)
        resolver = FixtureEntityResolver(USERS_FIXTURE)

        snapshot = reader.read_ticket("EXT-1001")
        interpretation = RuleBasedTicketInterpreter().interpret(snapshot)
        self.assertEqual(interpretation.requested_changes.get("last_name"), "Ivanova")

        resolution = resolver.resolve(interpretation.subject_reference)
        self.assertEqual(resolution.match_count, 1)
        current_state = resolver.get_current_state(resolution.entity_id)

        compiled = compile_edit_user_name_plan(
            snapshot=snapshot,
            interpretation=interpretation,
            resolution=resolution,
            current_state=current_state,
            policy_version="mvp-1",
        )

        self.assertEqual(compiled.evaluation.decision, PolicyDecisionType.ALLOW_FOR_APPROVAL)
        self.assertIsNotNone(compiled.plan)
        action = compiled.plan.actions[0]
        self.assertEqual(action.arguments["user_id"], "crm-user-1")
        self.assertEqual(action.arguments["last_name"], "Ivanova")
        self.assertEqual(action.arguments["first_name"], "Ivan")  # unchanged field carried through

    def test_suspicious_unapproved_request_never_produces_a_plan(self) -> None:
        reader = FixtureCrmReader(TICKETS_DIR)
        resolver = FixtureEntityResolver(USERS_FIXTURE)

        snapshot = reader.read_ticket("EXT-1002")
        interpretation = RuleBasedTicketInterpreter().interpret(snapshot)
        # No approval evidence, no extractable name, and risk phrases present.
        self.assertEqual(interpretation.requested_changes, {})
        self.assertTrue(interpretation.risk_flags)

        # EXT-1002 has no requester_email, so subject_reference falls back
        # to the ticket ID, which the user fixture obviously does not know.
        resolution = resolver.resolve(interpretation.subject_reference)
        self.assertEqual(resolution.match_count, 0)

        with self.assertRaises(Exception):
            # Unresolved subject: the compiler must refuse outright rather
            # than compile a plan naming no one.
            compile_edit_user_name_plan(
                snapshot=snapshot,
                interpretation=interpretation,
                resolution=resolution,
                current_state={},
                policy_version="mvp-1",
            )


if __name__ == "__main__":
    unittest.main()
