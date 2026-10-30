"""Regression tests for the app-lock PIN security boundary."""
import unittest
from unittest.mock import patch

import security


class PinSecurityTests(unittest.TestCase):
    def test_set_pin_rejects_empty_and_short_values_before_persisting(self):
        with patch.object(security, "save_lock_settings") as save:
            for value in ("", "12345", None, 7):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "6 and 64"):
                        security.set_pin(value)
            save.assert_not_called()

    def test_set_pin_rejects_values_longer_than_the_ui_limit(self):
        with patch.object(security, "save_lock_settings") as save:
            with self.assertRaisesRegex(ValueError, "6 and 64"):
                security.set_pin("x" * 65)
            save.assert_not_called()

    def test_set_pin_persists_a_hash_not_the_plaintext_pin(self):
        with (
            patch.object(security, "generate_salt", return_value="00" * 16),
            patch.object(security, "hash_pin", return_value="hashed-value") as hash_value,
            patch.object(security, "save_lock_settings") as save,
        ):
            security.set_pin("123456", autolock_minutes=10)

        hash_value.assert_called_once_with("123456", "00" * 16)
        save.assert_called_once_with(
            enabled=True, salt="00" * 16, pin_hash="hashed-value",
            autolock_minutes=10,
        )
