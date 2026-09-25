import unittest
from pathlib import Path

from crm_ai_agent.adapters.crm_fixture.reader import FixtureCrmReader, parse_ticket_export
from crm_ai_agent.domain.enums import TrustLevel
from crm_ai_agent.ports.crm import CrmTicketReader, TicketNotFoundError
from crm_ai_agent.safety.canonical_hash import compute_snapshot_hash

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tickets"


class TestFixtureCrmReader(unittest.TestCase):
    def test_reader_satisfies_the_crm_ticket_reader_port(self) -> None:
        reader = FixtureCrmReader(FIXTURES_DIR)
        self.assertIsInstance(reader, CrmTicketReader)

    def test_reads_a_ticket_with_approval_evidence(self) -> None:
        reader = FixtureCrmReader(FIXTURES_DIR)
        snapshot = reader.read_ticket("EXT-1001")
        self.assertEqual(snapshot.ticket_id, "t-1001")
        self.assertEqual(snapshot.status, "in_progress")
        self.assertEqual(snapshot.trusted_fields["assignee"], "operator1")
        self.assertEqual(len(snapshot.fragments), 2)

        trust_levels = {f.trust_level for f in snapshot.fragments}
        self.assertIn(TrustLevel.REQUESTER_TEXT, trust_levels)
        self.assertIn(TrustLevel.CRM_WORKFLOW_APPROVAL, trust_levels)

    def test_prompt_injection_style_claim_stays_tagged_as_requester_text(self) -> None:
        """EXT-1002's description text claims the request is pre-approved,
        but it must be parsed with trust_level=REQUESTER_TEXT regardless of
        what the text itself claims — the reader does not interpret content,
        it only records provenance. The Policy Engine (Stage 2) is what
        later refuses to treat this as real approval evidence."""
        reader = FixtureCrmReader(FIXTURES_DIR)
        snapshot = reader.read_ticket("EXT-1002")
        self.assertEqual(len(snapshot.fragments), 1)
        self.assertEqual(snapshot.fragments[0].trust_level, TrustLevel.REQUESTER_TEXT)
        self.assertNotIn(
            TrustLevel.CRM_WORKFLOW_APPROVAL, {f.trust_level for f in snapshot.fragments}
        )

    def test_missing_fixture_raises_ticket_not_found(self) -> None:
        reader = FixtureCrmReader(FIXTURES_DIR)
        with self.assertRaises(TicketNotFoundError):
            reader.read_ticket("EXT-DOES-NOT-EXIST")

    def test_snapshot_hash_is_deterministic_across_reads(self) -> None:
        reader = FixtureCrmReader(FIXTURES_DIR)
        snap1 = reader.read_ticket("EXT-1001")
        snap2 = reader.read_ticket("EXT-1001")
        # Two independent reads get different snapshot_id/snapshot objects,
        # but identical content must hash identically.
        self.assertEqual(compute_snapshot_hash(snap1), compute_snapshot_hash(snap2))

    def test_parse_ticket_export_rejects_unknown_trust_level(self) -> None:
        data = {
            "ticket_id": "t-x",
            "status": "open",
            "title": "x",
            "captured_at": "2026-09-01T00:00:00+00:00",
            "fragments": [
                {
                    "section": "description",
                    "source_reference": "ticket:X#description",
                    "trust_level": "made_up_trust_level",
                    "text": "hello",
                }
            ],
        }
        with self.assertRaises(ValueError):
            parse_ticket_export(data, snapshot_id="s-x")


if __name__ == "__main__":
    unittest.main()
