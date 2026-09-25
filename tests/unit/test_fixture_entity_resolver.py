import unittest
from pathlib import Path

from crm_ai_agent.adapters.crm_fixture.entity_resolver import FixtureEntityResolver
from crm_ai_agent.ports.entity_resolver import EntityResolver, SubjectNotFoundError

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "crm_users.json"


class TestFixtureEntityResolver(unittest.TestCase):
    def test_satisfies_the_entity_resolver_port(self) -> None:
        self.assertIsInstance(FixtureEntityResolver(FIXTURE_PATH), EntityResolver)

    def test_resolves_a_unique_match(self) -> None:
        resolver = FixtureEntityResolver(FIXTURE_PATH)
        result = resolver.resolve("ivan.petrov@example.com")
        self.assertEqual(result.match_count, 1)
        self.assertEqual(result.entity_id, "crm-user-1")

    def test_zero_matches_for_unknown_query(self) -> None:
        resolver = FixtureEntityResolver(FIXTURE_PATH)
        result = resolver.resolve("nobody@example.com")
        self.assertEqual(result.match_count, 0)
        self.assertIsNone(result.entity_id)

    def test_multiple_matches_are_reported_as_ambiguous(self) -> None:
        resolver = FixtureEntityResolver(FIXTURE_PATH)
        result = resolver.resolve("duplicate@example.com")
        self.assertEqual(result.match_count, 2)
        self.assertIsNone(result.entity_id)

    def test_get_current_state_returns_name_fields(self) -> None:
        resolver = FixtureEntityResolver(FIXTURE_PATH)
        state = resolver.get_current_state("crm-user-1")
        self.assertEqual(state, {"first_name": "Ivan", "last_name": "Petrov"})

    def test_get_current_state_raises_for_unknown_entity_id(self) -> None:
        resolver = FixtureEntityResolver(FIXTURE_PATH)
        with self.assertRaises(SubjectNotFoundError):
            resolver.get_current_state("does-not-exist")


if __name__ == "__main__":
    unittest.main()
