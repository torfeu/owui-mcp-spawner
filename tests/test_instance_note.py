"""The private note on an instance, and the description next to it.

The note exists to tell instances apart that look alike from the outside —
several `external_mcp_bridge` copies, each pointing at a different external MCP.
It is for whoever runs this server, so it must not travel: not to an anonymous
visitor of the dashboard, and not into the OpenWebUI export, where the
description goes.
"""
import json
from unittest.mock import patch

import app.config_store as config_store
import app.routes.instances as instances_route
from tests.test_save_routes import SaveRouteTestCase


class InstanceNoteTests(SaveRouteTestCase):

    def setUp(self):
        super().setUp()
        # Nothing here should restart anything; if it tried, it must not reach
        # the real process manager.
        patcher = patch.object(instances_route, "restart_instance", lambda _id: (True, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored(self):
        return json.loads((self.configs / "demo.json").read_text())

    def listed(self, headers=None):
        rows = self.client.get("/api/instances", headers=headers or {}).json()
        return next(row for row in rows if row["id"] == "demo")

    def test_a_note_is_saved_and_listed_for_the_admin(self):
        response = self.save_config(port=8397, note="  Wikidata — wd-mcp.wmcloud.org, no auth  ")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("Wikidata — wd-mcp.wmcloud.org, no auth", self.stored()["note"])
        self.assertEqual("Wikidata — wd-mcp.wmcloud.org, no auth",
                         self.listed(self.headers())["note"])

    def test_a_guest_never_sees_the_note(self):
        self.save_config(port=8397, note="private")
        self.assertNotIn("note", self.listed())
        detail = self.client.get("/api/instances/demo").json()
        self.assertNotIn("note", detail)

    def test_the_note_stays_out_of_the_openwebui_export(self):
        self.save_config(port=8397, note="private")
        export = self.client.get("/api/instances/demo/export", headers=self.headers())
        self.assertEqual(200, export.status_code, export.text)
        self.assertNotIn("private", export.text)

    def test_a_save_without_the_field_keeps_the_note(self):
        self.save_config(port=8397, note="keep me")
        self.save_config(port=8397)
        self.assertEqual("keep me", self.stored()["note"])

    def test_wrong_shapes_are_refused(self):
        self.assertEqual(422, self.save_config(port=8397, note=["a"]).status_code)
        too_long = "x" * (instances_route.NOTE_MAX_CHARS + 1)
        self.assertEqual(422, self.save_config(port=8397, note=too_long).status_code)
        self.assertEqual("", self.stored().get("note", ""))

    def test_a_changed_description_shows_without_a_manager_restart(self):
        """The dashboard reads the in-memory state; name and category were
        written back into it, the description was not."""
        self.save_config(port=8397, description="points at Wikidata")
        self.assertEqual("points at Wikidata", config_store._state["demo"].description)
        self.assertEqual("points at Wikidata", self.listed(self.headers())["description"])
