import unittest

from crm_ai_agent.adapters.crm_playwright.artifacts import ArtifactType, sanitize_text_for_artifact
from crm_ai_agent.adapters.crm_playwright.reader import (
    PlaywrightAdapterNotImplementedError,
    PlaywrightCrmTicketReader,
)
from crm_ai_agent.adapters.crm_playwright.selectors import SELECTOR_INVENTORY, SelectorEntry


class TestPlaywrightAdapterStub(unittest.TestCase):
    def test_constructing_the_real_adapter_always_raises(self) -> None:
        with self.assertRaises(PlaywrightAdapterNotImplementedError):
            PlaywrightCrmTicketReader(base_url="https://example.invalid")

    def test_selector_inventory_entries_are_all_confirmed_attribute_selectors(self) -> None:
        # Every entry recorded so far came from a real observed page dump
        # (see selectors.py's module docstring) — never a guessed CSS/XPath/
        # text selector, which would be the lowest-priority, least stable
        # choice. Most use Creatio's stable data-item-marker attribute
        # (label only); "value_control_id_prefix" instead documents the
        # confirmed id-prefix pattern for the actual value controls (see
        # adapters/crm_playwright/ticket_reader.py).
        self.assertGreater(len(SELECTOR_INVENTORY), 0)
        for key, entry in SELECTOR_INVENTORY.items():
            self.assertIsInstance(entry, SelectorEntry)
            self.assertEqual(entry.name, key)
            self.assertEqual(entry.strategy, "attribute")
            self.assertTrue("data-item-marker" in entry.value or "id^=" in entry.value)

    def test_is_access_note_status_selector_is_present(self) -> None:
        entry = SELECTOR_INVENTORY["is_access_note.status"]
        self.assertIn("TsiOfficeNoteState", entry.value)


class TestArtifactSanitization(unittest.TestCase):
    def test_redacts_email_addresses(self) -> None:
        result = sanitize_text_for_artifact("Contact: ivan.petrov@example.com for details")
        self.assertNotIn("ivan.petrov@example.com", result)
        self.assertIn("[REDACTED_EMAIL]", result)

    def test_redacts_phone_like_numbers(self) -> None:
        result = sanitize_text_for_artifact("Call me at +1 234-567-8901 anytime")
        self.assertNotIn("234-567-8901", result)
        self.assertIn("[REDACTED_PHONE]", result)

    def test_leaves_unrelated_text_untouched(self) -> None:
        text = "Ticket status changed to In Progress"
        self.assertEqual(sanitize_text_for_artifact(text), text)

    def test_artifact_type_enum_matches_expected_values(self) -> None:
        self.assertEqual(ArtifactType.SCREENSHOT.value, "SCREENSHOT")
        self.assertEqual(ArtifactType.TRACE.value, "TRACE")
        self.assertEqual(ArtifactType.DOM_SNIPPET.value, "DOM_SNIPPET")


if __name__ == "__main__":
    unittest.main()
