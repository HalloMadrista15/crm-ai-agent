"""Offline tests for CrmPlaywrightTicketReader's constructor guard only —
actually reading a ticket requires a real Playwright browser and a live
CRM session, which is out of scope for the offline unit suite. See
scripts/crm_probe_field_extraction.py for how the field-id pattern this
reader relies on was confirmed against the real system.
"""

import tempfile
import unittest
from pathlib import Path

from crm_ai_agent.adapters.crm_playwright.ticket_reader import CrmPlaywrightTicketReader


class TestCrmPlaywrightTicketReaderConstruction(unittest.TestCase):
    def test_raises_if_no_saved_session_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "does-not-exist.json"
            with self.assertRaises(FileNotFoundError):
                CrmPlaywrightTicketReader(storage_state_path=missing_path)

    def test_constructs_when_session_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crm_storage_state.json"
            path.write_text("{}", encoding="utf-8")
            reader = CrmPlaywrightTicketReader(storage_state_path=path)
            self.assertIsInstance(reader, CrmPlaywrightTicketReader)


if __name__ == "__main__":
    unittest.main()
