"""Tests for the encryption of the passwords a shared console remembers."""

import unittest

from crm_ai_agent.webapp.saved_secrets import SecretError, is_sealed, seal, unseal, workspace_key

TOKEN = "0123456789abcdef0123456789abcdef"
OTHER_TOKEN = "fedcba9876543210fedcba9876543210"


class TestSavedSecrets(unittest.TestCase):
    def setUp(self) -> None:
        self.key = workspace_key(TOKEN)

    def test_opens_to_the_same_password(self) -> None:
        for password in ("Qwerty123", "пароль с пробелом ", "a=b#c\"'", ""):
            self.assertEqual(unseal(self.key, "ws", "CRM_PASSWORD", seal(self.key, "ws", "CRM_PASSWORD", password)), password)

    def test_nothing_of_the_password_or_the_key_is_in_the_stored_value(self) -> None:
        stored = seal(self.key, "ws", "CRM_PASSWORD", "Qwerty123")
        self.assertTrue(is_sealed(stored))
        self.assertNotIn("Qwerty", stored)
        self.assertNotIn(TOKEN, stored)
        self.assertNotIn("\n", stored)  # one line of an env file

    def test_the_same_password_never_looks_the_same_twice(self) -> None:
        self.assertNotEqual(seal(self.key, "ws", "CRM_PASSWORD", "x"), seal(self.key, "ws", "CRM_PASSWORD", "x"))

    def test_another_browsers_key_does_not_open_it(self) -> None:
        stored = seal(self.key, "ws", "CRM_PASSWORD", "x")
        with self.assertRaises(SecretError):
            unseal(workspace_key(OTHER_TOKEN), "ws", "CRM_PASSWORD", stored)

    def test_moved_to_another_workspace_or_setting_it_does_not_open(self) -> None:
        stored = seal(self.key, "ws", "CRM_PASSWORD", "x")
        for workspace, name in (("other", "CRM_PASSWORD"), ("ws", "WEBITEL_PASSWORD")):
            with self.assertRaises(SecretError, msg=(workspace, name)):
                unseal(self.key, workspace, name, stored)

    def test_a_damaged_value_is_an_error_not_a_wrong_password(self) -> None:
        stored = seal(self.key, "ws", "CRM_PASSWORD", "x")
        flipped = stored[:-2] + ("A" if stored[-2] != "A" else "B") + stored[-1]
        for broken in (flipped, stored[:20], "enc1:", "enc1:не base64", "enc1:AAAA"):
            with self.assertRaises(SecretError, msg=broken):
                unseal(self.key, "ws", "CRM_PASSWORD", broken)

    def test_the_key_depends_on_the_whole_cookie_and_is_stable(self) -> None:
        self.assertEqual(workspace_key(TOKEN), workspace_key(TOKEN))
        self.assertEqual(len(workspace_key(TOKEN)), 32)
        self.assertNotEqual(workspace_key(TOKEN), workspace_key(TOKEN[:-1] + "0"))


if __name__ == "__main__":
    unittest.main()
