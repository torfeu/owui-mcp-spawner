"""Two creates of one id — the check that went stale while the install ran.

An instance id was checked against the configs on disk and then let go. What
follows the check is dependency installation and validation, which take as long
as pip does, and only afterwards is the config written. So two creates of the
same id both passed the check, both installed, both answered `ok` with their
own port — and the second config overwrote the first. One caller was told about
an instance that does not exist, on a port nothing listens on.

Ports were already held across that window; ids were not. These tests pin that
they are now, and that two *different* ids may still be created side by side —
serialising every install would be a cure worse than the disease.
"""
import asyncio
import json
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import app.api_helpers as api_helpers
import app.config_store as config_store

CODE = ('class Tools:\n    def hi(self) -> str:\n        """Hi."""\n        return "hi"\n')
VALID = {"valid": True, "errors": [], "warnings": [], "tools": [], "valves": {}}


class IdReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.configs = root / "configs"
        self.tools = root / "tools"
        self.configs.mkdir()
        self.tools.mkdir()

        self.installing = threading.Event()
        self.release = threading.Event()

        def install(*a, **kw):
            # First one in stops here, holding the window open.
            if not self.installing.is_set():
                self.installing.set()
                self.release.wait(5)
            return True, ""

        for target in (
            patch.object(config_store, "CONFIGS_DIR", self.configs),
            patch.object(api_helpers, "TOOLS_DIR", self.tools),
            patch.object(api_helpers, "install_dependencies", install),
            patch.object(api_helpers, "validate_tool_code", lambda *a, **kw: dict(VALID)),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(lambda: [config_store._state.pop(i, None)
                                 for i in ("demo", "other")])

    def provision(self, tool_id="demo", name="demo", port=None):
        return api_helpers._provision_new_tool(
            tool_id=tool_id, name=name, description="", category="",
            code=CODE, requirements=[], venv="default", port=port)

    def test_two_creates_of_one_id_produce_exactly_one_instance(self):
        async def go():
            first = asyncio.create_task(self.provision(name="first", port=8391))
            await asyncio.to_thread(self.installing.wait, 5)
            # The second arrives while the first is still installing — the exact
            # window in which the on-disk check said "free".
            second = asyncio.create_task(self.provision(name="second", port=8392))
            await asyncio.sleep(0)
            self.release.set()
            return await asyncio.gather(first, second, return_exceptions=True)

        results = asyncio.run(go())
        ok = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, HTTPException)]
        self.assertEqual(1, len(ok), f"expected one success, got {results}")
        self.assertEqual(1, len(refused), f"expected one refusal, got {results}")
        self.assertEqual(409, refused[0].status_code)

        configs = sorted(p.name for p in self.configs.glob("*.json"))
        self.assertEqual(["demo.json"], configs)
        saved = json.loads((self.configs / "demo.json").read_text())
        self.assertEqual(ok[0]["port"], saved["server"]["port"])

    def test_two_different_ids_are_still_created_side_by_side(self):
        async def go():
            first = asyncio.create_task(self.provision("demo", "demo", port=8393))
            await asyncio.to_thread(self.installing.wait, 5)
            second = asyncio.create_task(self.provision("other", "other", port=8394))
            await asyncio.sleep(0)
            self.release.set()
            return await asyncio.gather(first, second, return_exceptions=True)

        results = asyncio.run(go())
        self.assertTrue(all(isinstance(r, dict) for r in results), results)
        self.assertEqual(["demo.json", "other.json"],
                         sorted(p.name for p in self.configs.glob("*.json")))

    def test_a_refused_create_gives_its_reservation_back(self):
        """A reservation that leaks would make the id unusable until restart —
        worse than the race it was added to close."""
        self.release.set()   # nothing to hold open here
        with patch.object(api_helpers, "validate_tool_code",
                          lambda *a, **kw: {"valid": False, "errors": ["broken"]}):
            with self.assertRaises(HTTPException):
                asyncio.run(self.provision(port=8395))
        self.assertNotIn("demo", api_helpers._inflight_ids)

        result = asyncio.run(self.provision(port=8396))
        self.assertTrue(result["ok"])

    def test_an_id_already_on_disk_is_still_refused_the_same_way(self):
        self.release.set()
        asyncio.run(self.provision(port=8397))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(self.provision(port=8398))
        self.assertEqual(409, caught.exception.status_code)
