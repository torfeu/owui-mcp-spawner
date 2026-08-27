"""Per-user access rules: who may run what, and under which account.

app/policy.py maps the verified user id ("sub") to the instances and tools that
person may reach, plus the account a tool acts as for them. The rule that has
to hold in every direction is deny by default: no file, no entry, no instance,
broken file — all of them mean no.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import app.policy as policy
from app.identity import Identity

ANNA = Identity(sub="sub-anna", email="anna@example.org", name="Anna", role="user")
BEN = Identity(sub="sub-ben", email="ben@example.org", name="Ben", role="user")


class PolicyTestCase(unittest.TestCase):
    """Base: a policy file in a temp dir, and a cache that never leaks between tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "identity_policy.json"
        self.env = patch.dict(os.environ, {"MCP_IDENTITY_POLICY": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._reset_cache)
        self._reset_cache()

    def _reset_cache(self):
        policy._cache = None
        policy._cache_key = None
        policy._missing_file_warned = False

    def write(self, data: dict):
        self.path.write_text(json.dumps(data))
        self._reset_cache()


class DenyByDefaultTests(PolicyTestCase):
    def test_without_a_policy_file_nobody_reaches_anything(self):
        self.assertFalse(is_allowed := policy.is_tool_allowed(ANNA, "inst", "read_item"))
        self.assertEqual(policy.visible_tools(ANNA, "inst", ["read_item"]), [])

    def test_an_unknown_user_is_denied(self):
        self.write({"users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}}})
        self.assertFalse(policy.is_tool_allowed(BEN, "inst", "read_item"))

    def test_an_unidentified_caller_is_denied(self):
        self.write({"users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}}})
        self.assertFalse(policy.is_tool_allowed(None, "inst", "read_item"))

    def test_an_instance_that_is_not_listed_is_denied(self):
        self.write({"users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}}})
        self.assertFalse(policy.is_tool_allowed(ANNA, "other_inst", "read_item"))

    def test_a_broken_policy_file_denies_rather_than_falling_back(self):
        """A policy that keeps applying after someone broke the file is worse
        than a loud stop: nobody would notice the rules went stale."""
        self.path.write_text("{ this is not json")
        self._reset_cache()
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "read_item"))
        self.assertEqual(policy.visible_tools(ANNA, "inst", ["read_item"]), [])
        with self.assertRaises(policy.PolicyError):
            policy.load_policy()

    def test_an_explicit_relaxed_default_lets_unlisted_users_through(self):
        """The escape hatch for installations that want identity in their tools
        without running an allow-list. Opt-in, and never the default."""
        self.write({"default": {"deny": False}, "users": {}})
        self.assertTrue(policy.is_tool_allowed(BEN, "inst", "read_item"))


class ToolRulesTests(PolicyTestCase):
    def setUp(self):
        super().setUp()
        self.write({
            "users": {
                ANNA.sub: {"instances": {"inst": ["read_item", "search_items"]}},
                BEN.sub: {"instances": {"inst": "*"}},
            }
        })

    def test_a_listed_tool_is_allowed(self):
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))

    def test_an_unlisted_tool_is_denied_even_though_the_instance_is_allowed(self):
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "delete_item"))

    def test_a_star_means_every_tool_of_that_instance(self):
        self.assertTrue(policy.is_tool_allowed(BEN, "inst", "delete_item"))

    def test_listings_are_filtered_to_what_the_user_may_run(self):
        catalog = ["read_item", "search_items", "delete_item"]
        self.assertEqual(
            policy.visible_tools(ANNA, "inst", catalog), ["read_item", "search_items"]
        )
        self.assertEqual(policy.visible_tools(BEN, "inst", catalog), catalog)

    def test_hiding_and_denying_agree(self):
        """A hidden tool must also be a refused tool.

        Filtering the listing is presentation — a model that knows the name
        from an earlier conversation can still ask for it by hand, and that
        request lands on is_tool_allowed().
        """
        catalog = ["read_item", "search_items", "delete_item"]
        visible = set(policy.visible_tools(ANNA, "inst", catalog))
        for name in catalog:
            with self.subTest(tool=name):
                self.assertEqual(policy.is_tool_allowed(ANNA, "inst", name), name in visible)


class MatchingTests(PolicyTestCase):
    def test_e_mail_is_not_matched_unless_asked_for(self):
        """An address can be reassigned to a different person; a sub cannot."""
        self.write({"users": {"some-other-sub": {
            "email": ANNA.email, "instances": {"inst": "*"}}}})
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "read_item"))

    def test_e_mail_matching_works_when_switched_on(self):
        self.write({"match_email": True, "users": {"some-other-sub": {
            "email": ANNA.email, "instances": {"inst": "*"}}}})
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))

    def test_an_ambiguous_e_mail_matches_nobody(self):
        self.write({"match_email": True, "users": {
            "sub-1": {"email": ANNA.email, "instances": {"inst": "*"}},
            "sub-2": {"email": ANNA.email, "instances": {"inst": "*"}},
        }})
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "read_item"))

    def test_the_sub_wins_over_an_e_mail_entry(self):
        self.write({"match_email": True, "users": {
            ANNA.sub: {"instances": {"inst": ["read_item"]}},
            "sub-2": {"email": ANNA.email, "instances": {"inst": "*"}},
        }})
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "delete_item"))


class RoleTests(PolicyTestCase):
    """Roles are the closest thing to groups the forwarded token allows.

    OpenWebUI sends no groups, but it does send a role, and that one is signed.
    A role is base equipment; the personal entry sits on top.
    """

    def test_a_role_grants_without_a_personal_entry(self):
        self.write({"roles": {"admin": {"instances": {"inst": ["read_item"]}}}})
        admin = Identity(sub="whoever", role="admin")
        self.assertTrue(policy.is_tool_allowed(admin, "inst", "read_item"))
        self.assertFalse(policy.is_tool_allowed(Identity(sub="x", role="user"), "inst", "read_item"))

    def test_a_personal_entry_adds_to_the_role_instead_of_replacing_it(self):
        """Otherwise every personal entry would have to repeat the role's
        grants, and forgetting one would silently take access away."""
        self.write({
            "roles": {"admin": {"instances": {"laws": "*"}}},
            "users": {ANNA.sub: {"instances": {"secrets": ["read_item"]}}},
        })
        anna = Identity(sub=ANNA.sub, role="admin")
        self.assertTrue(policy.is_tool_allowed(anna, "laws", "anything"))
        self.assertTrue(policy.is_tool_allowed(anna, "secrets", "read_item"))

    def test_the_personal_entry_wins_for_the_same_instance(self):
        self.write({
            "roles": {"admin": {"instances": {"inst": "*"}}},
            "users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}},
        })
        anna = Identity(sub=ANNA.sub, role="admin")
        self.assertTrue(policy.is_tool_allowed(anna, "inst", "read_item"))
        self.assertFalse(policy.is_tool_allowed(anna, "inst", "delete_item"))

    def test_an_explicit_deny_beats_the_role(self):
        self.write({
            "roles": {"admin": {"instances": {"inst": "*"}}},
            "users": {ANNA.sub: {"deny": True}},
        })
        self.assertFalse(policy.is_tool_allowed(Identity(sub=ANNA.sub, role="admin"), "inst", "read_item"))

    def test_a_role_can_carry_a_shared_account(self):
        path = pathlib.Path(self.tmp.name) / "service"
        path.write_text("service-secret")
        self.write({"roles": {"agent": {"account": "service", "credentials_file": str(path)}}})
        credentials = policy.credentials_for(Identity(sub="codex-agent", role="agent"))
        self.assertEqual(credentials.account, "service")
        self.assertEqual(credentials.secret, "service-secret")


class NameMatchingTests(PolicyTestCase):
    def test_matching_by_name_is_off_by_default(self):
        """The display name is the one claim a user can change themselves, so
        it is never an identifier unless someone asks for it explicitly."""
        self.write({"users": {"other-sub": {"name": "Anna", "instances": {"inst": "*"}}}})
        self.assertFalse(policy.is_tool_allowed(Identity(sub="x", name="Anna"), "inst", "read_item"))

    def test_it_works_when_switched_on(self):
        self.write({"match_name": True,
                    "users": {"other-sub": {"name": "Anna", "instances": {"inst": "*"}}}})
        self.assertTrue(policy.is_tool_allowed(Identity(sub="x", name="Anna"), "inst", "read_item"))

    def test_an_ambiguous_name_matches_nobody(self):
        self.write({"match_name": True, "users": {
            "sub-1": {"name": "Anna", "instances": {"inst": "*"}},
            "sub-2": {"name": "Anna", "instances": {"inst": "*"}},
        }})
        self.assertFalse(policy.is_tool_allowed(Identity(sub="x", name="Anna"), "inst", "read_item"))


class SavePolicyTests(PolicyTestCase):
    def test_a_valid_policy_round_trips_and_takes_effect_at_once(self):
        policy.save_policy({"users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}}})
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))
        self.assertEqual(json.loads(self.path.read_text())["users"][ANNA.sub]["instances"]["inst"],
                         ["read_item"])

    def test_a_secret_in_the_policy_is_refused(self):
        """The whole point of credentials_file is that secrets live in a file
        with mode 600, not in a JSON the web UI reads and backups copy."""
        for field in ("password", "app_password", "secret", "token"):
            with self.subTest(field=field), self.assertRaises(policy.PolicyError) as caught:
                policy.save_policy({"users": {ANNA.sub: {field: "hunter2"}}})
            self.assertIn("credentials_file", str(caught.exception))

    def test_shapes_that_would_mean_something_else_are_refused(self):
        for bad in (
            {"users": []},
            {"users": {ANNA.sub: "everything"}},
            {"users": {ANNA.sub: {"instances": ["inst"]}}},
            {"users": {ANNA.sub: {"instances": {"inst": "read_item"}}}},
            {"users": {ANNA.sub: {"account": 42}}},
            {"roles": {"admin": {"instances": {"inst": [1, 2]}}}},
        ):
            with self.subTest(policy=bad), self.assertRaises(policy.PolicyError):
                policy.save_policy(bad)

    def test_an_unknown_instance_is_allowed(self):
        # Rules may precede the instance they govern; refusing that would force
        # people to create instances in a particular order.
        policy.save_policy({"users": {ANNA.sub: {"instances": {"not_yet_created": "*"}}}})
        self.assertTrue(policy.is_tool_allowed(ANNA, "not_yet_created", "anything"))

    def test_a_failed_save_leaves_the_previous_policy_in_place(self):
        policy.save_policy({"users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}}})
        with self.assertRaises(policy.PolicyError):
            policy.save_policy({"users": {ANNA.sub: {"password": "hunter2"}}})
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))


class CredentialsTests(PolicyTestCase):
    def _secret_file(self, name: str, content: str, mode: int = 0o600) -> pathlib.Path:
        path = pathlib.Path(self.tmp.name) / name
        path.write_text(content)
        path.chmod(mode)
        return path

    def test_each_user_gets_their_own_account(self):
        anna_file = self._secret_file("anna", "anna-secret")
        ben_file = self._secret_file("ben", "ben-secret")
        self.write({"users": {
            ANNA.sub: {"account": "anna", "credentials_file": str(anna_file)},
            BEN.sub: {"account": "ben", "credentials_file": str(ben_file)},
        }})
        self.assertEqual(policy.credentials_for(ANNA).secret, "anna-secret")
        self.assertEqual(policy.credentials_for(BEN).secret, "ben-secret")
        self.assertEqual(policy.credentials_for(ANNA).account, "anna")

    def test_a_user_without_an_account_gets_nothing_rather_than_a_default(self):
        """No fallback to whatever the instance was configured with — that
        fallback is exactly the shared-account problem this replaces."""
        self.write({"users": {ANNA.sub: {"instances": {"inst": "*"}}}})
        self.assertIsNone(policy.credentials_for(ANNA))

    def test_an_unknown_user_gets_nothing(self):
        self.write({"users": {ANNA.sub: {
            "account": "anna", "credentials_file": str(self._secret_file("anna", "s"))}}})
        self.assertIsNone(policy.credentials_for(BEN))
        self.assertIsNone(policy.credentials_for(None))

    def test_the_secret_is_read_per_access_so_rotation_takes_effect(self):
        path = self._secret_file("anna", "old-secret")
        self.write({"users": {ANNA.sub: {"account": "anna", "credentials_file": str(path)}}})
        credentials = policy.credentials_for(ANNA)
        self.assertEqual(credentials.secret, "old-secret")
        path.write_text("new-secret")
        self.assertEqual(credentials.secret, "new-secret")

    def test_a_loose_file_mode_is_flagged_but_still_served(self):
        """Refusing service at 23:00 over a file mode helps nobody; saying so
        in the log does. Tightening it is the operator's job."""
        path = self._secret_file("anna", "anna-secret", mode=0o644)
        self.write({"users": {ANNA.sub: {"account": "anna", "credentials_file": str(path)}}})
        with self.assertLogs(policy.logger, level="WARNING") as captured:
            self.assertEqual(policy.credentials_for(ANNA).secret, "anna-secret")
        self.assertIn("chmod 600", "\n".join(captured.output))

    def test_the_credential_is_the_last_non_empty_line(self):
        """Credential files grow a history: the previous value stays above the
        new one during a rotation, and people write a comment on top. Sending
        the whole file would fail as "wrong password" — the least helpful place
        to look. A plain one-line file behaves exactly as expected."""
        path = self._secret_file("anna", "# rotated 2026-08-11\nold-secret\nnew-secret\n")
        self.write({"users": {ANNA.sub: {"account": "anna", "credentials_file": str(path)}}})
        self.assertEqual(policy.credentials_for(ANNA).secret, "new-secret")

        path.write_text("only-one\n")
        self.assertEqual(policy.credentials_for(ANNA).secret, "only-one")

    def test_an_empty_credentials_file_fails_loudly(self):
        path = self._secret_file("anna", "\n   \n")
        self.write({"users": {ANNA.sub: {"account": "anna", "credentials_file": str(path)}}})
        with self.assertRaises(policy.PolicyError):
            _ = policy.credentials_for(ANNA).secret

    def test_a_missing_secret_file_fails_loudly(self):
        self.write({"users": {ANNA.sub: {
            "account": "anna", "credentials_file": str(pathlib.Path(self.tmp.name) / "gone")}}})
        with self.assertRaises(policy.PolicyError):
            _ = policy.credentials_for(ANNA).secret

    def test_the_current_user_is_resolved_from_the_call_context(self):
        """What a tool actually calls: no argument, no valve, no cached client."""
        from app.identity import identity_scope
        self.write({"users": {ANNA.sub: {
            "account": "anna", "credentials_file": str(self._secret_file("anna", "anna-secret"))}}})
        self.assertIsNone(policy.credentials_for_current_user())
        with identity_scope(ANNA):
            self.assertEqual(policy.credentials_for_current_user().secret, "anna-secret")
        self.assertIsNone(policy.credentials_for_current_user())

    def test_no_secret_appears_in_a_repr(self):
        path = self._secret_file("anna", "anna-secret")
        self.write({"users": {ANNA.sub: {"account": "anna", "credentials_file": str(path)}}})
        self.assertNotIn("anna-secret", repr(policy.credentials_for(ANNA)))


class CacheTests(PolicyTestCase):
    def test_an_edited_policy_takes_effect_without_a_restart(self):
        """Rules are read on every call; the cache keys on mtime and size, so
        an operator revoking access does not have to restart anything."""
        self.write({"users": {ANNA.sub: {"instances": {"inst": ["read_item"]}}}})
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))

        # Rewritten in place, cache untouched on purpose — the reload has to
        # come from the file check, not from the test resetting the cache.
        os.utime(self.path, None)
        self.path.write_text(json.dumps({"users": {ANNA.sub: {"instances": {}}}}))
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "read_item"))


if __name__ == "__main__":
    unittest.main()
