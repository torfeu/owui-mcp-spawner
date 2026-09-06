import unittest

from pydantic import ValidationError

from app.schema import MCPConfig, ServerConfig
from app.security import (SECRET_MASK, AmbiguousMask, drop_masked_values,
                          is_secret_field, keep_masked_values, mask_secrets,
                          validate_package_spec)


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

    def test_a_dictionary_under_a_secret_name_is_judged_by_its_own_keys(self):
        """A dictionary brings its own names, and those are the better
        evidence — blanking a whole subtree over the name above it would hide
        ordinary settings that happen to sit next to a credential."""
        masked = mask_secrets({"auth": {"user": "anna", "password": "s3cret"},
                               "credentials": {"host": "db.local"}})
        self.assertEqual("anna", masked["auth"]["user"])
        self.assertEqual(SECRET_MASK, masked["auth"]["password"])
        self.assertEqual("db.local", masked["credentials"]["host"])

    def test_a_secret_name_is_matched_by_word_and_not_by_substring(self):
        """"key" used to fire on "keywords", "author" and "monkey". Harmless
        while it only hid a string — reading a config and writing it back put
        the value straight back — and no longer harmless once the masking
        reached into lists, where a redacted backup lost the values for good."""
        for name in ("keywords", "author", "monkey", "keyboard", "allow_delete",
                     "EXTRA_ALLOWED_TOOLS"):
            self.assertFalse(is_secret_field(name), name)
        for name in ("api_key", "API_KEY", "apiKey", "apikey", "MCP_TOKEN",
                     "app_password_file", "unsplash_access_key", "auth_token",
                     "client_secret", "passphrase", "keys", "tokens"):
            self.assertTrue(is_secret_field(name), name)

    def test_an_ordinary_keyword_list_survives_a_redacted_backup(self):
        """The reported loss, end to end: masked in the export, and then the
        restore dropped it and called it a missing credential."""
        values = {"keywords": ["birds", "nests"], "api_keys": ["K1"]}
        masked = mask_secrets(values)
        self.assertEqual(["birds", "nests"], masked["keywords"])
        kept, dropped = drop_masked_values(masked)
        self.assertEqual(["birds", "nests"], kept["keywords"])
        self.assertEqual(["api_keys.0"], dropped)

    def test_a_list_under_a_secret_name_has_no_inner_names_and_is_masked(self):
        """`{"api_keys": ["…"]}` is the same secret as `{"api_key": "…"}`, and
        it went out in the clear through the config API and into a redacted
        backup. A list has no keys of its own to judge by, so the name above it
        is all there is."""
        masked = mask_secrets({"api_keys": ["K1", "K2"], "tokens": ["T"]})
        self.assertEqual([SECRET_MASK, SECRET_MASK], masked["api_keys"])
        self.assertEqual([SECRET_MASK], masked["tokens"])

    def test_a_list_that_is_not_named_a_secret_stays_visible(self):
        masked = mask_secrets({"hosts": ["a.local", "b.local"],
                               "accounts": [{"name": "anna", "token": "T"}]})
        self.assertEqual(["a.local", "b.local"], masked["hosts"])
        self.assertEqual("anna", masked["accounts"][0]["name"])
        self.assertEqual(SECRET_MASK, masked["accounts"][0]["token"])

    def test_a_masked_list_entry_is_not_installed_as_the_literal_mask(self):
        kept, dropped = drop_masked_values({"api_keys": [SECRET_MASK, SECRET_MASK]})
        self.assertEqual({"api_keys": []}, kept)
        self.assertEqual(["api_keys.0", "api_keys.1"], dropped)

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

    # ── masked values inside lists ──────────────────────────────────────────

    ACCOUNTS = {"accounts": [{"name": "anna", "token": "T_ANNA"},
                             {"name": "bob", "token": "T_BOB"}]}

    def masked_accounts(self):
        return mask_secrets(self.ACCOUNTS)["accounts"]

    def test_deleting_a_list_entry_does_not_hand_its_secret_to_the_next_one(self):
        """The reported case, and the reason position is not enough: with
        index matching, removing anna gave bob *her* token — a swapped
        credential, which fails as a wrong login rather than a missing one."""
        anna, bob = self.masked_accounts()
        kept = keep_masked_values({"accounts": [bob]}, self.ACCOUNTS)
        self.assertEqual([{"name": "bob", "token": "T_BOB"}], kept["accounts"])

    def test_reordering_keeps_each_secret_with_its_own_entry(self):
        anna, bob = self.masked_accounts()
        kept = keep_masked_values({"accounts": [bob, anna]}, self.ACCOUNTS)
        self.assertEqual([{"name": "bob", "token": "T_BOB"},
                          {"name": "anna", "token": "T_ANNA"}], kept["accounts"])

    def test_an_added_entry_leaves_the_existing_secrets_alone(self):
        anna, bob = self.masked_accounts()
        kept = keep_masked_values(
            {"accounts": [anna, bob, {"name": "cid", "token": "T_CID"}]}, self.ACCOUNTS)
        self.assertEqual(["T_ANNA", "T_BOB", "T_CID"],
                         [a["token"] for a in kept["accounts"]])

    def test_an_entry_renamed_while_masked_is_refused_not_guessed(self):
        """Renamed or new — the two look the same from here, and picking one
        would mean writing somebody's credential onto another account."""
        anna, _bob = self.masked_accounts()
        with self.assertRaises(AmbiguousMask) as caught:
            keep_masked_values(
                {"accounts": [anna, {"name": "bobby", "token": SECRET_MASK}]},
                self.ACCOUNTS)
        self.assertIn("entry 2", str(caught.exception))

    def test_a_list_of_bare_secrets_round_trips_and_takes_edits_in_place(self):
        previous = {"api_keys": ["K1", "K2"]}
        masked = mask_secrets(previous)
        self.assertEqual(previous, keep_masked_values(masked, previous))
        # One replaced, the other left masked: position still says which.
        self.assertEqual({"api_keys": ["NEU", "K2"]},
                         keep_masked_values({"api_keys": ["NEU", SECRET_MASK]}, previous))

    def test_deleting_from_a_list_of_bare_secrets_is_refused(self):
        """Nothing distinguishes one mask from another, so which one survived
        cannot be told — and guessing is how the wrong key gets kept."""
        with self.assertRaises(AmbiguousMask):
            keep_masked_values({"api_keys": [SECRET_MASK]}, {"api_keys": ["K1", "K2"]})

    def test_two_identical_entries_survive_an_untouched_save(self):
        previous = {"a": [{"n": "x", "token": "T1"}, {"n": "x", "token": "T2"}]}
        self.assertEqual(previous, keep_masked_values(mask_secrets(previous), previous))

    def test_package_specs_reject_shell_metacharacters(self):
        self.assertTrue(validate_package_spec("httpx>=0.27"))
        self.assertFalse(validate_package_spec("httpx; rm -rf /"))
        self.assertFalse(validate_package_spec("$(malicious)"))


if __name__ == "__main__":
    unittest.main()
