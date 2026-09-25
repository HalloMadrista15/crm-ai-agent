import unittest

from crm_ai_agent.adapters.webitel_playwright.selectors import SELECTOR_INVENTORY, SelectorEntry


class TestWebitelSelectorInventory(unittest.TestCase):
    def test_inventory_is_populated_from_real_recon(self) -> None:
        self.assertGreater(len(SELECTOR_INVENTORY), 0)
        for key, entry in SELECTOR_INVENTORY.items():
            self.assertIsInstance(entry, SelectorEntry)
            self.assertEqual(entry.name, key)
            self.assertIn(entry.strategy, ("attribute", "placeholder", "text"))
            self.assertTrue(entry.value)

    def test_password_input_selector_is_present(self) -> None:
        entry = SELECTOR_INVENTORY["user_edit.password_input"]
        self.assertIn("Password", entry.value)

    def test_row_edit_button_uses_icon_attribute(self) -> None:
        entry = SELECTOR_INVENTORY["users_list.row_edit_button"]
        self.assertEqual(entry.value, 'button[icon="edit"]')


if __name__ == "__main__":
    unittest.main()
