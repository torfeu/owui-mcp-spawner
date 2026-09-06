"""Backup and restore — the archive, and the one rule the restore obeys.

Two promises carry this. A backup asked for **without** secrets contains none:
not in the settings, not in the instance valves, not the content key — the
whole point of that option is that the file may be kept somewhere a credential
may not. And the restore **writes only what is not there**, everywhere, not
only for instances: a setting already set stays, an agent already known stays,
and `content.key` is replaced only where there is none, because a new key
silently invalidates every download link this server ever handed out.

Every test points configs, tools and the runtime files at temp directories.
This suite runs on the server, and a restore test that reached the real
`configs/` would overwrite the live installation — the reason the route is in
`ADMIN_ONLY` in `test_api.py` as well.
"""
import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.agent_identity as agent_identity
import app.auth as auth
import app.backup as backup
import app.config_store as config_store
import app.content_store as content_store
import app.policy as policy
import app.settings_store as settings_store
from app.admin_server import app
from app.security import SECRET_MASK

# The real shape on disk: an OpenWebUI export is a list holding one tool
# object. Writing the fixture as a bare dict is what let a first version of the
# restore refuse every actual installation while the suite stayed green.
TOOL_OBJECT = {
    "id": "demo", "name": "demo",
    "content": "class Tools:\n    def hi(self) -> str:\n        \"\"\"Hi.\"\"\"\n        return 'hi'\n",
    "specs": [{"name": "hi", "description": "Hi.", "parameters": {"type": "object", "properties": {}}}],
}
TOOL_JSON = [TOOL_OBJECT]


def config(instance_id="demo", port=8101, tool_path="", **extra):
    return {
        "id": instance_id, "name": instance_id, "description": "", "category": "Tests",
        "locked": False,
        "server": {"host": "127.0.0.1", "port": port, "endpoint": "/mcp"},
        "tool_source": {"type": "openwebui_json", "path": tool_path or f"tools/{instance_id}.json"},
        "values": {"api_key": "s3cret", "base_url": "https://example.org"},
        "venv": "default", **extra,
    }


class IsolatedStateTestCase(unittest.TestCase):
    """Every path this module writes to, pointed at a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for name in ("configs", "tools", "runtime"):
            (self.root / name).mkdir()

        self.configs = self.root / "configs"
        self.tools = self.root / "tools"
        self.settings_file = self.root / "runtime" / "settings.json"
        self.key_file = self.root / "runtime" / "content.key"

        for target, value in (
            (patch.object(config_store, "CONFIGS_DIR", self.configs), None),
            (patch.object(backup, "CONFIGS_DIR", self.configs), None),
            (patch.object(backup, "TOOLS_DIR", self.tools), None),
            (patch.object(settings_store, "SETTINGS_FILE", self.settings_file), None),
            (patch.object(content_store, "SECRET_FILE", self.key_file), None),
            (patch.object(backup, "CONTENT_KEY_FILE", self.key_file), None),
        ):
            target.start()
            self.addCleanup(target.stop)
        settings_store._cache = None
        self.addCleanup(lambda: setattr(settings_store, "_cache", None))
        content_store._secret_cache.clear()
        self.addCleanup(content_store._secret_cache.clear)

        env = patch.dict(os.environ, {
            "MCP_AGENT_IDENTITIES_FILE": str(self.root / "runtime" / "agent_identities.json"),
            "MCP_IDENTITY_POLICY": str(self.root / "runtime" / "identity_policy.json"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(agent_identity.ENV_IDENTITIES, None)
        agent_identity._file_cache.clear()
        self.addCleanup(agent_identity._file_cache.clear)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))

    # ── helpers ──────────────────────────────────────────────────────────────

    def write_instance(self, instance_id="demo", **kw):
        tool_path = self.tools / f"{instance_id}.json"
        tool_path.write_text(json.dumps([{**TOOL_OBJECT, "id": instance_id}]))
        raw = config(instance_id, tool_path=str(tool_path), **kw)
        (self.configs / f"{instance_id}.json").write_text(json.dumps(raw))
        return raw

    def write_settings(self, **values):
        self.settings_file.write_text(json.dumps(values))
        settings_store._cache = None


class ExportTests(IsolatedStateTestCase):
    def test_the_archive_carries_the_tool_code_not_just_the_config(self):
        # The plan's collect-list named configs and settings but not tools/.
        # Without the code a restored config points at a file that is not
        # there — an instance that looks installed and cannot run.
        self.write_instance()
        entry = backup.build(include_secrets=True)["instances"][0]
        self.assertEqual("demo", entry["id"])
        self.assertIn("class Tools", entry["tool"][0]["content"])

    def test_without_secrets_nothing_secret_is_in_the_file(self):
        self.write_instance()
        self.write_settings(mcp_bearer_token="shared", user_jwt_secret="jwt",
                            password_hash="abc", read_token="r", agent_token="a",
                            content_max_mb=200)
        self.key_file.write_text("k" * 43)

        payload = backup.build(include_secrets=False)
        text = json.dumps(payload)
        for secret in ("shared", "jwt", "abc", "s3cret", "k" * 43):
            self.assertNotIn(secret, text, f"{secret!r} leaked into a redacted backup")
        self.assertFalse(payload["contains_secrets"])
        self.assertNotIn("content_key", payload)
        # The settings that are not credentials do travel — otherwise the
        # option would be "backup nothing".
        self.assertEqual(200, payload["settings"]["content_max_mb"])
        self.assertEqual(SECRET_MASK, payload["instances"][0]["config"]["values"]["api_key"])
        self.assertEqual("https://example.org",
                         payload["instances"][0]["config"]["values"]["base_url"])

    def test_a_nested_credential_does_not_travel_in_a_redacted_backup(self):
        """The archive said contains_secrets: false and carried the password
        anyway — valve values are dict[str, Any], and the masking only ever
        looked at the top level."""
        self.write_instance(values={"connection": {"password": "nested-s3cret",
                                                   "host": "db.local"}})
        payload = backup.build(include_secrets=False)
        self.assertNotIn("nested-s3cret", json.dumps(payload))
        values = payload["instances"][0]["config"]["values"]
        self.assertEqual(SECRET_MASK, values["connection"]["password"])
        self.assertEqual("db.local", values["connection"]["host"])

    def test_with_secrets_everything_needed_to_restore_is_there(self):
        self.write_instance()
        self.write_settings(mcp_bearer_token="shared", content_max_mb=200)
        self.key_file.write_text("k" * 43)

        payload = backup.build(include_secrets=True)
        self.assertTrue(payload["contains_secrets"])
        self.assertEqual("shared", payload["settings"]["mcp_bearer_token"])
        self.assertEqual("k" * 43, payload["content_key"])
        self.assertEqual("s3cret", payload["instances"][0]["config"]["values"]["api_key"])

    def test_an_instance_whose_tool_file_vanished_is_reported_not_fatal(self):
        raw = self.write_instance()
        pathlib.Path(raw["tool_source"]["path"]).unlink()
        self.write_instance("second")
        payload = backup.build()
        self.assertEqual(["demo"], payload["tool_files_missing"])
        self.assertEqual(2, len(payload["instances"]))   # the other one still travels

    def test_the_agent_roster_and_the_rules_travel_in_either_mode(self):
        # Hashes, not tokens — agent_identity stores them that way precisely so
        # that this file can exist. And the rules are the whole point of a
        # backup: they are what nobody wants to reassign by hand.
        agent_identity.create("codex", name="Codex", role="KI")
        policy.save_policy({"users": {"anna": {"instances": {"demo": "*"}}}})
        payload = backup.build(include_secrets=False)
        self.assertEqual(["codex"], [r["sub"] for r in payload["agent_identities"]])
        self.assertIn("anna", payload["policy"]["users"])
        # The hash travels — without it nothing could be restored — but never a
        # token: the module keeps only hashes, and that is what makes this file
        # safe to carry the roster at all.
        for record in payload["agent_identities"]:
            self.assertTrue(record["token_hash"].startswith("sha256:"), record)
            self.assertNotIn("token", set(record) - {"token_hash"})

    def test_the_filename_says_which_kind_of_file_this_is(self):
        self.assertIn("-with-secrets", backup.filename(True))
        self.assertNotIn("secret", backup.filename(False))


class RestoreTests(IsolatedStateTestCase):
    def archive(self, *ids, secrets=True, **payload):
        for instance_id in ids:
            self.write_instance(instance_id)
        built = backup.build(include_secrets=secrets)
        for path in self.configs.glob("*.json"):
            path.unlink()      # the target machine is empty again
        for path in self.tools.glob("*.json"):
            path.unlink()
        return {**built, **payload}

    def test_an_empty_machine_gets_everything_back(self):
        archive = self.archive("demo", "second")
        report = backup.restore(archive)
        self.assertEqual(["demo", "second"], sorted(r["id"] for r in report["instances_restored"]))
        self.assertTrue((self.configs / "demo.json").exists())
        self.assertIn("class Tools", (self.tools / "demo.json").read_text())
        # The credentials came back with it, so the instance can actually run.
        restored = json.loads((self.configs / "demo.json").read_text())
        self.assertEqual("s3cret", restored["values"]["api_key"])

    def test_an_existing_instance_is_never_overwritten(self):
        # The rule the whole restore rests on. A mistaken click on a running
        # server must cost nothing.
        archive = self.archive("demo")
        self.write_instance("demo", port=9999)
        before = (self.configs / "demo.json").read_text()

        report = backup.restore(archive)
        self.assertEqual([], report["instances_restored"])
        self.assertEqual([{"id": "demo", "reason": "already exists"}], report["instances_skipped"])
        self.assertEqual(before, (self.configs / "demo.json").read_text())

    def test_a_taken_port_is_reassigned_and_said_so(self):
        archive = self.archive("demo")
        with (patch.object(backup, "is_port_free", return_value=False),
              patch.object(backup, "find_free_port", return_value=8199)):
            report = backup.restore(archive)
        self.assertEqual([{"id": "demo", "was": 8101, "now": 8199}], report["ports_reassigned"])
        restored = json.loads((self.configs / "demo.json").read_text())
        self.assertEqual(8199, restored["server"]["port"])

    def test_a_redacted_backup_does_not_install_asterisks_as_a_password(self):
        # An instance holding "********" fails in a way that reads like a
        # broken tool. Missing outright fails loudly, and is reported here.
        archive = self.archive("demo", secrets=False)
        report = backup.restore(archive)
        restored = json.loads((self.configs / "demo.json").read_text())
        self.assertNotIn("api_key", restored["values"])
        self.assertEqual("https://example.org", restored["values"]["base_url"])
        self.assertEqual(["api_key"], report["instances_restored"][0]["credentials_missing"])

    def test_both_shapes_of_tool_file_are_accepted(self):
        # A list of one is what OpenWebUI exports and what lies under tools/;
        # a bare object is what tool_loader also accepts. Refusing either would
        # make the restore useless on one of the two.
        for shape, tool in (("list of one", [TOOL_OBJECT]), ("bare object", TOOL_OBJECT)):
            with self.subTest(shape=shape):
                archive = self.archive("demo")
                archive["instances"][0]["tool"] = tool
                report = backup.restore(archive)
                self.assertEqual(["demo"], [r["id"] for r in report["instances_restored"]], report)
                self.assertIn("class Tools", (self.tools / "demo.json").read_text())
                (self.configs / "demo.json").unlink()
                (self.tools / "demo.json").unlink()

    def test_an_entry_without_tool_code_is_refused_rather_than_half_installed(self):
        archive = self.archive("demo")
        archive["instances"][0]["tool"] = None
        report = backup.restore(archive)
        self.assertEqual([], report["instances_restored"])
        self.assertEqual("no tool code in the backup", report["instances_failed"][0]["reason"])
        self.assertFalse((self.configs / "demo.json").exists())

    def test_one_broken_entry_does_not_cost_the_others(self):
        archive = self.archive("demo", "second")
        archive["instances"].insert(0, {"id": "junk", "config": "not an object"})
        report = backup.restore(archive)
        self.assertEqual(["demo", "second"], sorted(r["id"] for r in report["instances_restored"]))
        self.assertEqual(1, len(report["instances_failed"]))

    def test_settings_already_set_are_kept(self):
        self.write_settings(content_max_mb=999)
        archive = self.archive("demo")
        archive["settings"] = {"content_max_mb": 200, "content_warn_percent": 70}
        report = backup.restore(archive)
        self.assertEqual(["content_max_mb"], report["settings_skipped"])
        self.assertEqual(["content_warn_percent"], report["settings_restored"])
        self.assertEqual(999, settings_store.load_settings()["content_max_mb"])

    def test_the_content_key_is_only_written_where_there_is_none(self):
        archive = self.archive("demo")
        archive["content_key"] = "n" * 43

        self.key_file.write_text("o" * 43)
        content_store._secret_cache.clear()
        report = backup.restore(archive)
        self.assertIn("skipped", report["content_key"])
        self.assertEqual("o" * 43, self.key_file.read_text().strip())

        self.key_file.unlink()
        content_store._secret_cache.clear()
        report = backup.restore(archive)
        self.assertEqual("restored", report["content_key"])
        self.assertEqual("n" * 43, self.key_file.read_text().strip())
        self.assertEqual(0o600, self.key_file.stat().st_mode & 0o777)

    def test_a_known_agent_keeps_its_hash_and_a_new_one_is_added(self):
        agent_identity.create("codex", name="Codex", role="KI")
        archive = self.archive("demo")
        archive["agent_identities"] = [
            {"sub": "codex", "name": "Impostor", "role": "x", "token_hash": "deadbeef", "created_at": 1},
            {"sub": "claude", "name": "Claude", "role": "KI", "token_hash": "abc123", "created_at": 2},
        ]
        report = backup.restore(archive)
        self.assertEqual(["codex"], report["agents_skipped"])
        self.assertEqual(["claude"], report["agents_restored"])
        kept = {r["sub"]: r["name"] for r in agent_identity.load()}
        self.assertEqual("Codex", kept["codex"])     # not replaced by the archive's
        self.assertEqual("Claude", kept["claude"])

    def test_rules_for_a_user_who_already_has_some_are_left_alone(self):
        policy.save_policy({"users": {"anna": {"instances": {"kept": "*"}}}})
        archive = self.archive("demo")
        archive["policy"] = {"users": {"anna": {"instances": {"other": "*"}},
                                       "bob": {"instances": {"demo": "*"}}}}
        report = backup.restore(archive)
        self.assertEqual(["anna"], report["policy_users_skipped"])
        self.assertEqual(["bob"], report["policy_users_restored"])
        users = policy.load_policy(force=True)["users"]
        self.assertEqual({"kept": "*"}, users["anna"]["instances"])

    def test_a_restore_names_the_nested_credential_it_could_not_bring(self):
        archive = self.archive("demo")
        archive["instances"][0]["config"]["values"] = {
            "connection": {"password": SECRET_MASK, "host": "db.local"}}
        report = backup.restore(archive)
        restored = report["instances_restored"][0]
        self.assertEqual(["connection.password"], restored["credentials_missing"])
        # The mask itself must not be installed: an instance holding "********"
        # fails in a way that reads like a broken tool.
        saved = json.loads((self.configs / "demo.json").read_text())
        self.assertEqual({"connection": {"host": "db.local"}}, saved["values"])

    def test_a_whole_policy_comes_back_on_an_empty_target(self):
        """Roles and the two globals are policy too, and a restore that keeps
        only `users` hands back a server that denies the people it listed:
        rules keyed by role match nobody, and `default` reverts to deny."""
        archive = self.archive("demo")
        archive["policy"] = {
            "default": {"deny": False},
            "match_email": True,
            "roles": {"KI": {"instances": {"demo": "*"}}},
            "users": {"anna": {"instances": {"demo": ["hi"]}}},
        }
        report = backup.restore(archive)
        self.assertEqual(["anna"], report["policy_users_restored"])
        self.assertEqual(["KI"], report["policy_roles_restored"])
        self.assertEqual(["default", "match_email"], report["policy_globals_restored"])

        restored = policy.load_policy(force=True)
        self.assertEqual({"instances": {"demo": "*"}}, restored["roles"]["KI"])
        self.assertEqual({"deny": False}, restored["default"])
        self.assertTrue(restored["match_email"])

    def test_a_role_only_policy_is_not_skipped_for_having_no_users(self):
        archive = self.archive("demo")
        archive["policy"] = {"roles": {"KI": {"instances": "*"}}}
        report = backup.restore(archive)
        self.assertEqual(["KI"], report["policy_roles_restored"])
        self.assertEqual("*", policy.load_policy(force=True)["roles"]["KI"]["instances"])

    def test_roles_and_globals_this_server_already_decided_are_left_alone(self):
        policy.save_policy({"default": {"deny": True},
                            "roles": {"KI": {"instances": {"kept": "*"}}}})
        archive = self.archive("demo")
        archive["policy"] = {"default": {"deny": False},
                             "match_email": True,
                             "roles": {"KI": {"instances": {"other": "*"}},
                                       "Mensch": {"instances": {"demo": "*"}}}}
        report = backup.restore(archive)
        self.assertEqual(["KI"], report["policy_roles_skipped"])
        self.assertEqual(["Mensch"], report["policy_roles_restored"])
        self.assertEqual(["default"], report["policy_globals_skipped"])
        self.assertEqual(["match_email"], report["policy_globals_restored"])

        kept = policy.load_policy(force=True)
        self.assertEqual({"deny": True}, kept["default"])
        self.assertEqual({"kept": "*"}, kept["roles"]["KI"]["instances"])

    def test_a_policy_this_server_cannot_read_is_left_where_it_is(self):
        """The one case where "what is there stays" mattered most and did not
        hold: an unreadable policy was treated as an empty target and replaced
        wholesale. A broken policy denies everything — the one written over it
        would have granted."""
        broken = b'{"roles": {"KI": '
        policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
        policy.policy_path().write_bytes(broken)

        archive = self.archive("demo")
        archive["policy"] = {"roles": {"Fremd": {"instances": "*"}},
                             "default": {"deny": False}}
        report = backup.restore(archive)

        self.assertEqual(broken, policy.policy_path().read_bytes())
        self.assertEqual([], report["policy_roles_restored"])
        self.assertEqual([], report["policy_globals_restored"])
        self.assertIn("could not be read", report["policy_failed"])
        # The rest of the restore is independent and still runs.
        self.assertEqual(["demo"], [r["id"] for r in report["instances_restored"]])

    def test_a_policy_the_validator_refuses_is_reported_not_claimed(self):
        archive = self.archive("demo")
        archive["policy"] = {"roles": {"KI": {"token": "nope"}}}
        report = backup.restore(archive)
        self.assertEqual([], report["policy_roles_restored"])
        self.assertIn("secrets do not belong", report["policy_failed"])
        self.assertEqual({}, policy.load_policy(force=True))
        # The rest of the restore still ran.
        self.assertEqual(["demo"], [r["id"] for r in report["instances_restored"]])

    def test_a_dry_run_leaves_roles_and_globals_alone(self):
        archive = self.archive("demo")
        archive["policy"] = {"default": {"deny": False},
                             "roles": {"KI": {"instances": "*"}}}
        preview = backup.restore(archive, dry_run=True)
        self.assertEqual(["KI"], preview["policy_roles_restored"])
        self.assertEqual(["default"], preview["policy_globals_restored"])
        self.assertEqual({}, policy.load_policy(force=True))

    def test_a_dry_run_reports_the_same_and_writes_nothing(self):
        archive = self.archive("demo")
        archive["settings"] = {"content_warn_percent": 70}
        archive["content_key"] = "n" * 43

        preview = backup.restore(archive, dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(["demo"], [r["id"] for r in preview["instances_restored"]])
        self.assertEqual(["content_warn_percent"], preview["settings_restored"])
        self.assertFalse((self.configs / "demo.json").exists())
        self.assertFalse((self.tools / "demo.json").exists())
        self.assertFalse(self.key_file.exists())
        self.assertEqual({}, settings_store.load_settings())

        real = backup.restore(archive)
        self.assertEqual([r["id"] for r in preview["instances_restored"]],
                         [r["id"] for r in real["instances_restored"]])

    def test_a_file_that_is_not_a_backup_is_refused_by_name(self):
        for payload, why in (
            ({}, "no format"),
            ({"format": "something-else", "format_version": 1}, "wrong format"),
            ({"format": backup.FORMAT, "format_version": 99}, "from the future"),
        ):
            with self.subTest(why=why):
                with self.assertRaises(backup.BackupError):
                    backup.restore(payload)


class BackupRouteTests(IsolatedStateTestCase):
    PASSWORD = "admin-password"

    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        # Set explicitly: a route test that assumes no password is set is green
        # here and red on the server, where one is.
        original = auth._password_hash
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

    @property
    def admin(self):
        return {"Authorization": f"Bearer {self.PASSWORD}"}

    def test_the_download_names_the_file_and_says_what_is_in_it(self):
        self.write_instance()
        response = self.client.get("/api/backup?secrets=true", headers=self.admin)
        self.assertEqual(200, response.status_code)
        self.assertIn("-with-secrets", response.headers["content-disposition"])
        self.assertTrue(response.json()["contains_secrets"])

        plain = self.client.get("/api/backup", headers=self.admin).json()
        self.assertFalse(plain["contains_secrets"])   # secrets are opt-in

    def test_neither_route_opens_without_the_password(self):
        with patch.object(backup, "restore") as restore:
            self.assertEqual(401, self.client.get("/api/backup").status_code)
            self.assertEqual(401, self.client.post("/api/backup/restore", json={}).status_code)
        restore.assert_not_called()

    def test_a_dry_run_over_the_route_writes_nothing(self):
        self.write_instance()
        archive = self.client.get("/api/backup?secrets=true", headers=self.admin).json()
        for path in self.configs.glob("*.json"):
            path.unlink()
        report = self.client.post("/api/backup/restore", json={**archive, "dry_run": True},
                                  headers=self.admin).json()
        self.assertEqual(["demo"], [r["id"] for r in report["instances_restored"]])
        self.assertFalse((self.configs / "demo.json").exists())

    def test_junk_is_a_422_and_never_touches_the_configs(self):
        self.write_instance()
        before = (self.configs / "demo.json").read_text()
        for body in ({}, {"format": "nope"}, {"format": backup.FORMAT, "format_version": 99}):
            with self.subTest(body=body):
                self.assertEqual(422, self.client.post("/api/backup/restore", json=body,
                                                       headers=self.admin).status_code)
        self.assertEqual(before, (self.configs / "demo.json").read_text())


if __name__ == "__main__":
    unittest.main()
