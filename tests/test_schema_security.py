import unittest

from pydantic import ValidationError

from app.schema import MCPConfig, ServerConfig
from app.security import (SECRET_MASK, drop_masked_values, keep_masked_values,
                          mask_secrets, validate_package_spec)


class SchemaAndSecurityTests(unittest.TestCase):
    def test_server_rejects_non_local_hosts(self):
        with self.assertRaises(ValidationError):
            ServerConfig(host="example.com", port=8101)

    def test_server_rejects_privileged_ports(self):
        with self.assertRaises(ValidationError):
            ServerConfig(port=80)

    def test_config_rejects_unsafe_ids(self):
        with self.assertRaises(ValidationError):
            MCPConfig.model_validate({
                "id": "../../escape",
                "name": "Unsafe",
                "server": {"port": 8101},
                "tool_source": {"path": "tools/example.json"},
            })

    def test_secret_values_are_masked(self):
        values = {
            "api_key": "secret",
            "password": "secret",
            "base_url": "https://example.com",
            "enabled": True,
        }
        masked = mask_secrets(values)
        self.assertEqual(SECRET_MASK, masked["api_key"])
        self.assertEqual(SECRET_MASK, masked["password"])
        self.assertEqual(values["base_url"], masked["base_url"])
        self.assertTrue(masked["enabled"])

    def test_a_secret_one_level_down_is_masked_too(self):
        """Valve values are dict[str, Any]. A credential the config API handed
        out unmasked also travelled into a backup taken *without* secrets — one
        that called itself contains_secrets: false."""
        masked = mask_secrets({
            "connection": {"password": "s3cret", "host": "db.local"},
            "accounts": [{"token": "t0ken", "user": "anna"}],
            "note": "plain",
        })
        self.assertEqual(SECRET_MASK, masked["connection"]["password"])
        self.assertEqual(SECRET_MASK, masked["accounts"][0]["token"])
        self.assertEqual("db.local", masked["connection"]["host"])
        self.assertEqual("anna", masked["accounts"][0]["user"])
        self.assertEqual("plain", masked["note"])

    def test_masking_does_not_spread_to_what_sits_under_a_secret_name(self):
        """is_secret_field matches substrings, so "key" fires on "keywords".
        A rule that blanked whole subtrees would hide ordinary settings."""
        masked = mask_secrets({"keywords": {"topic": "birds"},
                               "author": {"name": "anna"}})
        self.assertEqual("birds", masked["keywords"]["topic"])
        self.assertEqual("anna", masked["author"]["name"])

    def test_reading_and_writing_back_unchanged_keeps_the_real_secret(self):
        """The half of the fix that matters more than the fix: masking without
        this turns a read-then-save into a config whose password is stars."""
        values = {"connection": {"password": "s3cret", "host": "db.local"},
                  "api_key": "K", "accounts": [{"token": "t0ken", "user": "anna"}]}
        self.assertEqual(values, keep_masked_values(mask_secrets(values), values))

    def test_a_new_value_still_replaces_the_old_one(self):
        previous = {"connection": {"password": "old"}}
        echoed = {"connection": {"password": "new"}}
        self.assertEqual(echoed, keep_masked_values(echoed, previous))

    def test_a_mask_with_nothing_behind_it_is_not_stored(self):
        self.assertEqual({"connection": {}},
                         keep_masked_values({"connection": {"password": SECRET_MASK}}, {}))

    def test_a_redacted_backup_names_the_credentials_it_could_not_bring(self):
        kept, dropped = drop_masked_values(
            {"connection": {"password": SECRET_MASK, "host": "h"}, "note": "n"})
        self.assertEqual({"connection": {"host": "h"}, "note": "n"}, kept)
        self.assertEqual(["connection.password"], dropped)

    def test_package_specs_reject_shell_metacharacters(self):
        self.assertTrue(validate_package_spec("httpx>=0.27"))
        self.assertFalse(validate_package_spec("httpx; rm -rf /"))
        self.assertFalse(validate_package_spec("$(malicious)"))


if __name__ == "__main__":
    unittest.main()
