"""What a save says, and what a save keeps.

Two rules for the routes that write a config or a tool's code.

Writing a config or a tool's code puts it on disk. Whether the running runner
picked it up is a second question, and `restart_instance()` answers it with a
reason when it fails. Both routes used to throw that answer away and set
`restarted = True` unconditionally, so a restart that failed — a port taken
meanwhile, a venv that no longer builds — reached the dashboard and the control
tool as a change that was live. It was not: the old runner kept serving the old
code, and nothing said so.

The rule these tests pin: a failed restart never reports `restarted: true`, and
the reason travels with it. The save itself still succeeded, so `ok` stays true
— that separation is the point.
"""
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api_helpers as api_helpers
import app.auth as auth
import app.config_store as config_store
import app.lockout as lockout
import app.routes.instances as instances_route
import app.routes.tools as tools_route
from app.admin_server import app
from app.schema import MCPInstance, MCPStatus
from app.security import SECRET_MASK

CODE = ('"""\nversion: 1.0.0\n"""\n\n\nclass Tools:\n'
        '    def hi(self) -> str:\n        """Say hi."""\n        return "hi"\n')

PASSWORD = "admin-password"


def config(port=8397):
    return {
        "id": "demo", "name": "demo", "description": "", "category": "Tests",
        "locked": False,
        "server": {"host": "127.0.0.1", "port": port, "endpoint": "/mcp"},
        "tool_source": {"type": "openwebui_json", "path": "tools/demo.json"},
        "values": {}, "venv": "default",
    }


class SaveRouteTestCase(unittest.TestCase):
    """Every path the two write routes touch, pointed at a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.configs = root / "configs"
        self.tools = root / "tools"
        self.configs.mkdir()
        self.tools.mkdir()

        (self.configs / "demo.json").write_text(json.dumps(config()))
        self.tool_file = self.tools / "demo.json"
        self.tool_file.write_text(json.dumps([{
            "id": "demo", "name": "demo", "content": CODE, "specs": [],
            "meta": {"description": "", "manifest": {}},
        }]))

        # The routes resolve the tool path through the config, so it has to
        # point at the temp copy rather than at the installed one.
        cfg = config()
        cfg["tool_source"]["path"] = str(self.tool_file)
        (self.configs / "demo.json").write_text(json.dumps(cfg))

        config_store._state["demo"] = MCPInstance(
            id="demo", name="demo", status=MCPStatus.running, pid=4242,
            port=8397, host="127.0.0.1", endpoint="/mcp")
        self.addCleanup(lambda: config_store._state.pop("demo", None))

        for target in (
            patch.object(config_store, "CONFIGS_DIR", self.configs),
            # Saving code snapshots the previous tool JSON. Without this the
            # snapshots land in the real runtime/history/demo/ — this suite
            # runs on the server, where that directory belongs to an instance.
            patch.object(api_helpers, "HISTORY_DIR", root / "history"),
            patch.object(tools_route, "install_dependencies", lambda *a, **kw: (True, "")),
        ):
            target.start()
            self.addCleanup(target.stop)

        # Module state: a lock left held by a previous test would 409 this one.
        config_store._config_locks.clear()

        self.client = TestClient(app)
        lockout.clear_all()
        self.original_hash = auth._password_hash
        auth._password_hash = hashlib.sha256(PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", self.original_hash))

    def headers(self):
        return {"Authorization": f"Bearer {PASSWORD}"}

    def save_code(self):
        return self.client.put("/api/instances/demo/tool-code",
                               json={"code": CODE}, headers=self.headers())

    def save_config(self, port=8398, **extra):
        body = config(port=port)
        body["tool_source"]["path"] = str(self.tool_file)
        body.update(extra)
        return self.client.put("/api/instances/demo", json=body, headers=self.headers())

    def stored_values(self):
        return json.loads((self.configs / "demo.json").read_text())["values"]


class RestartReportingTests(SaveRouteTestCase):
    """Saved is not the same as live, and the answer must say which."""

    def test_a_failed_restart_after_saving_code_is_not_reported_as_restarted(self):
        with patch.object(tools_route, "restart_instance",
                          lambda _id: (False, "runner crashed")):
            response = self.save_code()
        body = response.json()
        self.assertEqual(200, response.status_code, body)
        self.assertTrue(body["ok"])          # the file *was* written
        self.assertFalse(body["restarted"])
        self.assertEqual("runner crashed", body["restart_error"])

    def test_a_successful_restart_after_saving_code_still_says_so(self):
        with patch.object(tools_route, "restart_instance", lambda _id: (True, "")):
            body = self.save_code().json()
        self.assertTrue(body["restarted"])
        self.assertEqual("", body["restart_error"])

    def test_a_failed_restart_after_saving_config_is_not_reported_as_restarted(self):
        with patch.object(instances_route, "restart_instance",
                          lambda _id: (False, "port 8398 is already in use")):
            body = self.save_config().json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["restarted"])
        self.assertEqual("port 8398 is already in use", body["restart_error"])

    def test_a_successful_restart_after_saving_config_still_says_so(self):
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            body = self.save_config().json()
        self.assertTrue(body["restarted"])
        self.assertEqual("", body["restart_error"])

    def test_a_config_change_that_needs_no_restart_reports_neither(self):
        with patch.object(instances_route, "restart_instance",
                          lambda _id: (False, "should not have been called")):
            body = self.save_config(port=8397).json()   # same port: nothing to apply
        self.assertFalse(body["restarted"])
        self.assertEqual("", body["restart_error"])


class MetadataPreservationTests(SaveRouteTestCase):
    """The second rule: saving code changes the code, not the file around it.

    The tool JSON was regenerated from scratch on every save, so `meta.manifest`
    came back as `{}`, `created_at` was reset to now, and any field an import
    had brought along was gone — after a save that changed nothing but a line
    of Python. For a tool whose version lives only in its manifest, that is
    where the version display loses its source.
    """

    def setUp(self):
        super().setUp()
        self.tool_file.write_text(json.dumps([{
            "id": "demo", "user_id": "u1", "name": "demo", "content": CODE, "specs": [],
            "meta": {"description": "imported", "manifest": {"version": "2.4.0",
                                                             "author": "someone"}},
            "access_control": {"read": ["team"]},
            "is_active": True, "created_at": 1000, "updated_at": 1000,
        }]))

    def saved(self):
        with patch.object(tools_route, "restart_instance", lambda _id: (True, "")):
            response = self.save_code()
        self.assertEqual(200, response.status_code, response.json())
        return json.loads(self.tool_file.read_text())[0]

    def test_the_manifest_survives_a_save_that_did_not_touch_it(self):
        meta = self.saved()["meta"]
        self.assertEqual({"version": "2.4.0", "author": "someone"}, meta["manifest"])

    def test_fields_this_spawner_does_not_generate_are_kept(self):
        tool = self.saved()
        self.assertEqual({"read": ["team"]}, tool["access_control"])
        self.assertEqual("u1", tool["user_id"])

    def test_the_creation_time_is_not_reset_by_an_edit(self):
        tool = self.saved()
        self.assertEqual(1000, tool["created_at"])
        self.assertGreater(tool["updated_at"], 1000)

    def test_the_code_and_its_schemas_are_still_the_ones_being_saved(self):
        new_code = CODE.replace('def hi(self)', 'def hello(self)')
        with patch.object(tools_route, "restart_instance", lambda _id: (True, "")):
            self.client.put("/api/instances/demo/tool-code",
                            json={"code": new_code}, headers=self.headers())
        tool = json.loads(self.tool_file.read_text())[0]
        self.assertEqual(new_code, tool["content"])
        self.assertEqual(["hello"], [s["name"] for s in tool["specs"]])

    def test_a_tool_without_a_previous_json_still_gets_a_complete_one(self):
        self.tool_file.unlink()
        tool = self.saved()
        self.assertEqual("demo", tool["id"])
        self.assertEqual(CODE, tool["content"])
        self.assertEqual({}, tool["meta"]["manifest"])


class NestedSecretRoundTripTests(SaveRouteTestCase):
    """Read the config, save it back untouched — the credential must survive.

    The masking used to stop at the top level, so `connection.password` was
    handed out in clear text by GET /config and travelled into a redacted
    backup. Making the mask recursive is only half of it: the write-back has to
    recognise a nested `********` as "leave this alone", or the edit dialog
    would store eight stars as the password the first time anyone opens it and
    presses save.
    """

    SECRET = "nested-s3cret"

    def setUp(self):
        super().setUp()
        cfg = config()
        cfg["tool_source"]["path"] = str(self.tool_file)
        cfg["values"] = {"connection": {"password": self.SECRET, "host": "db.local"},
                         "note": "plain"}
        (self.configs / "demo.json").write_text(json.dumps(cfg))

    def read_config(self):
        response = self.client.get("/api/instances/demo/config", headers=self.headers())
        self.assertEqual(200, response.status_code)
        return response.json()

    def test_a_nested_credential_is_not_handed_out(self):
        values = self.read_config()["values"]
        self.assertEqual(SECRET_MASK, values["connection"]["password"])
        self.assertEqual("db.local", values["connection"]["host"])
        self.assertEqual("plain", values["note"])

    def test_saving_the_config_back_unchanged_keeps_the_credential(self):
        fetched = self.read_config()
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            response = self.save_config(port=8397, values=fetched["values"])
        self.assertEqual(200, response.status_code, response.json())
        self.assertEqual(self.SECRET, self.stored_values()["connection"]["password"])

    def test_a_nested_credential_someone_actually_changed_is_stored(self):
        fetched = self.read_config()
        fetched["values"]["connection"]["password"] = "brand-new"
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            self.save_config(port=8397, values=fetched["values"])
        self.assertEqual("brand-new", self.stored_values()["connection"]["password"])

    def test_a_neighbour_of_a_masked_field_can_still_be_edited(self):
        fetched = self.read_config()
        fetched["values"]["connection"]["host"] = "db.remote"
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            self.save_config(port=8397, values=fetched["values"])
        stored = self.stored_values()
        self.assertEqual("db.remote", stored["connection"]["host"])
        self.assertEqual(self.SECRET, stored["connection"]["password"])

    def test_an_untraceable_masked_list_entry_is_refused_by_the_route(self):
        """A 422, not a quiet write: the config keeps what it had, and the
        message says what to do about it."""
        cfg = json.loads((self.configs / "demo.json").read_text())
        cfg["values"] = {"accounts": [{"name": "anna", "token": "T_ANNA"},
                                      {"name": "bob", "token": "T_BOB"}]}
        (self.configs / "demo.json").write_text(json.dumps(cfg))

        fetched = self.read_config()["values"]
        fetched["accounts"][1]["name"] = "bobby"          # umbenannt, Token maskiert
        response = self.save_config(port=8397, values=fetched)

        self.assertEqual(422, response.status_code)
        self.assertIn("cannot be traced back", response.json()["detail"])
        self.assertEqual("T_BOB", self.stored_values()["accounts"][1]["token"])

    def test_deleting_a_list_entry_keeps_the_survivor_s_own_secret(self):
        cfg = json.loads((self.configs / "demo.json").read_text())
        cfg["values"] = {"accounts": [{"name": "anna", "token": "T_ANNA"},
                                      {"name": "bob", "token": "T_BOB"}]}
        (self.configs / "demo.json").write_text(json.dumps(cfg))

        fetched = self.read_config()["values"]
        del fetched["accounts"][0]
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            response = self.save_config(port=8397, values=fetched)
        self.assertEqual(200, response.status_code, response.json())
        self.assertEqual([{"name": "bob", "token": "T_BOB"}],
                         self.stored_values()["accounts"])


class ConcurrentCommitTests(SaveRouteTestCase):
    """One change at a time per instance, and what that is worth.

    Saving code installs packages and validates in the venv — minutes, on a new
    dependency — and used to write a config read before all that, overwriting
    whatever had been saved meanwhile. `locked: true` went with it: a flag set
    to protect the instance, cleared by a save that began before it was set.

    Checking the state again at the commit was tried first and closed one
    interleaving at a time; each round of review found the next, because these
    routes prepare an environment from one state and save into another. So an
    instance is now held for the whole route and a second change is refused
    with a 409 rather than queued — a request that waits out a pip install is
    worse than one that says "not now", and waiting on a lock inside an async
    route would stall every other request in the process.

    The re-read and the checks at the commit stay: they cost nothing, and they
    are what makes a path that forgets to take the lock fail loudly.
    """

    def start_code_save(self):
        """Begin a code save that stops inside validation.

        The patches live for the whole test and the thread is joined before
        they are undone — a save thread that outlives them writes into the
        *real* configs directory, which is exactly what happened once.
        """
        import threading
        holding, release = threading.Event(), threading.Event()

        def slow(code, python, **kw):
            holding.set()
            release.wait(5)
            return {"valid": True, "errors": [], "warnings": [],
                    "tools": [], "valves": {"setting": 1}}

        for target in (
            patch.object(tools_route, "validate_tool_code", slow),
            patch.object(tools_route, "ensure_venv", lambda venv: (True, "")),
            patch.object(tools_route, "install_dependencies", lambda *a, **kw: (True, "")),
            patch.object(tools_route, "restart_instance", lambda _id: (True, "")),
        ):
            target.start()
            self.addCleanup(target.stop)

        result = {}
        thread = threading.Thread(target=lambda: result.update(
            {"status": self.save_code().status_code}))
        # Registered after the patches, so it runs before they are undone.
        self.addCleanup(lambda: (release.set(), thread.join(10)))
        thread.start()
        self.assertTrue(holding.wait(5), "the code save never reached validation")
        return release, thread, result

    def test_a_config_change_during_a_code_save_is_refused(self):
        release, thread, result = self.start_code_save()
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")), \
             patch.object(instances_route, "_valve_names_from_tool_file",
                          lambda cfg: {"setting"}):
            refused = self.save_config(port=8397, values={"setting": 2})
        self.assertEqual(409, refused.status_code, refused.json())
        self.assertIn("Another change", refused.json()["detail"])

        release.set()
        thread.join(10)
        self.assertEqual(200, result["status"])
        # Refused means refused: nothing of it was written.
        self.assertEqual(1, self.stored_values()["setting"])

    def test_the_same_change_works_once_the_first_one_is_done(self):
        """The refusal is a "not now", not a "no"."""
        release, thread, result = self.start_code_save()
        release.set()
        thread.join(10)
        self.assertEqual(200, result["status"])

        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")), \
             patch.object(instances_route, "_valve_names_from_tool_file",
                          lambda cfg: {"setting"}):
            response = self.save_config(port=8397, values={"setting": 2})
        self.assertEqual(200, response.status_code, response.json())
        self.assertEqual(2, self.stored_values()["setting"])

    def test_a_venv_change_during_a_code_save_is_refused(self):
        """What was installed and validated must match where it is saved. The
        two cannot drift apart any more, because the second change never
        starts."""
        release, thread, result = self.start_code_save()
        with patch.object(instances_route, "install_dependencies", lambda *a, **kw: (True, "")), \
             patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            refused = self.save_config(port=8397, venv="alternate")
        self.assertEqual(409, refused.status_code)

        release.set()
        thread.join(10)
        self.assertEqual("default",
                         json.loads((self.configs / "demo.json").read_text())["venv"])

    def test_a_lock_set_during_a_code_save_stops_it(self):
        """Locking stays open at any moment — it is a safety action, and
        refusing it for the length of an install would be the wrong trade. The
        save catches it at the commit instead and writes nothing."""
        release, thread, result = self.start_code_save()
        locked = self.client.post("/api/instances/demo/lock", headers=self.headers())
        self.assertEqual(200, locked.status_code, locked.text)

        release.set()
        thread.join(10)
        self.assertEqual(409, result["status"], "the save wrote through a lock")
        self.assertTrue(json.loads((self.configs / "demo.json").read_text())["locked"],
                        "the lock was cleared by the save")
