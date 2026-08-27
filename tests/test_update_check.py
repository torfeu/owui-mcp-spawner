import hashlib
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.update_check as update_check
from app.admin_server import app


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient: records the call, returns a canned answer."""

    calls = []

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __call__(self, *args, **kwargs):
        FakeClient.timeout = kwargs.get("timeout")
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        FakeClient.calls.append(url)
        if self._error:
            raise self._error
        return self._response


class VersionComparisonTests(unittest.TestCase):
    def test_newer_versions_are_compared_numerically_not_as_strings(self):
        # The whole reason packaging.version is used: "0.1.10" < "0.1.9" as text.
        self.assertTrue(update_check.is_newer("0.1.10", "0.1.9"))
        self.assertTrue(update_check.is_newer("v0.2.0", "0.1.9"))
        self.assertTrue(update_check.is_newer("1.0.0", "0.9.9"))
        self.assertFalse(update_check.is_newer("0.1.9", "0.1.10"))
        self.assertFalse(update_check.is_newer("0.1.2", "0.1.2"))
        self.assertFalse(update_check.is_newer("v0.1.2", "0.1.2"))

    def test_unparsable_or_missing_tags_never_claim_an_update(self):
        for latest, current in (("", "0.1.2"), ("nightly", "0.1.2"),
                                ("0.1.3", ""), ("release-x", "0.1.2")):
            self.assertFalse(update_check.is_newer(latest, current), (latest, current))


class CheckNowTests(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        FakeClient.calls = []
        self.patches = [
            patch("app.update_check.load_settings", side_effect=lambda: dict(self.settings)),
            patch("app.update_check.save_settings", side_effect=self._save),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _save(self, updates):
        for k, v in updates.items():
            if v is None:
                self.settings.pop(k, None)
            else:
                self.settings[k] = v

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_disabled_check_never_contacts_github(self):
        self.settings = {}
        result = self._run(update_check.check_now())
        self.assertEqual([], FakeClient.calls)
        self.assertFalse(result["enabled"])
        self.assertEqual("", result["latest_version"])

    def test_enabled_check_stores_the_release_and_flags_the_update(self):
        self.settings = {"update_check": True}
        fake = FakeClient(FakeResponse({
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/torfeu/owui-mcp-spawner/releases/tag/v9.9.9",
        }))
        with patch("app.update_check.httpx.AsyncClient", fake):
            result = self._run(update_check.check_now())
        self.assertEqual([update_check.RELEASES_URL], FakeClient.calls)
        self.assertTrue(result["update_available"])
        self.assertEqual("v9.9.9", result["latest_version"])
        self.assertTrue(self.settings["update_last_checked"])

    def test_a_fresh_cache_is_not_refetched_unless_forced(self):
        self.settings = {
            "update_check": True, "update_latest_version": "v0.0.1",
            "update_last_checked": int(time.time()),
        }
        fake = FakeClient(FakeResponse({"tag_name": "v9.9.9", "html_url": "x"}))
        with patch("app.update_check.httpx.AsyncClient", fake):
            self._run(update_check.check_now())
            self.assertEqual([], FakeClient.calls)
            self._run(update_check.check_now(force=True))
            self.assertEqual([update_check.RELEASES_URL], FakeClient.calls)

    def test_network_failures_keep_the_previous_cache(self):
        # Offline, DNS dead, GitHub down, rate limited: stay quiet, keep what we had.
        for failure in (FakeClient(error=OSError("no route to host")),
                        FakeClient(FakeResponse({}, status=403))):
            with self.subTest(failure=failure):
                self.settings = {
                    "update_check": True, "update_latest_version": "v0.1.9",
                    "update_html_url": "https://example.invalid/rel",
                    "update_last_checked": 1,
                }
                with patch("app.update_check.httpx.AsyncClient", failure):
                    result = self._run(update_check.check_now())
                self.assertEqual("v0.1.9", result["latest_version"])
                self.assertEqual(1, self.settings["update_last_checked"])


class UpdateSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.settings = {}
        self.patches = [
            patch("app.update_check.load_settings", side_effect=lambda: dict(self.settings)),
            patch("app.update_check.save_settings", side_effect=self._save),
            # The route imports save_settings inside the handler, so the patch
            # has to sit on the module it imports from — not on the route module.
            patch("app.settings_store.save_settings", side_effect=self._save),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        auth._password_hash = self.original_hash

    def _save(self, updates):
        for k, v in updates.items():
            if v is None:
                self.settings.pop(k, None)
            else:
                self.settings[k] = v

    def test_settings_report_the_check_as_off_by_default(self):
        data = self.client.get("/api/settings").json()["update"]
        self.assertFalse(data["enabled"])
        self.assertFalse(data["update_available"])
        self.assertEqual("", data["latest_version"])

    def test_anonymous_requests_never_see_the_update_state(self):
        # /api/auth-status is open and carries the bare version, but the
        # judgement "outdated" stays behind the login.
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        self.assertEqual(401, self.client.get("/api/settings").status_code)
        self.assertNotIn("update", self.client.get("/api/auth-status").json())

    def test_enabling_the_check_runs_one_immediately(self):
        FakeClient.calls = []
        fake = FakeClient(FakeResponse({"tag_name": "v9.9.9", "html_url": "u"}))
        with patch("app.update_check.httpx.AsyncClient", fake):
            response = self.client.put("/api/settings", json={"update_check": True})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn("update_check", response.json()["changed"])
        self.assertEqual([update_check.RELEASES_URL], FakeClient.calls)
        self.assertTrue(self.settings["update_check"])

    def test_disabling_the_check_drops_the_cached_result(self):
        self.settings = {
            "update_check": True, "update_latest_version": "v9.9.9",
            "update_html_url": "u", "update_last_checked": 123,
        }
        response = self.client.put("/api/settings", json={"update_check": False})
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(self.settings["update_check"])
        for key in ("update_latest_version", "update_html_url", "update_last_checked"):
            self.assertNotIn(key, self.settings)

    def test_non_boolean_update_check_is_rejected(self):
        response = self.client.put("/api/settings", json={"update_check": "yes"})
        self.assertEqual(400, response.status_code)

    def test_check_now_button_reports_an_available_update(self):
        self.settings = {"update_check": True}
        FakeClient.calls = []
        fake = FakeClient(FakeResponse({"tag_name": "v9.9.9", "html_url": "u"}))
        with patch("app.update_check.httpx.AsyncClient", fake):
            data = self.client.post("/api/settings/update-check").json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["update_available"])
        self.assertEqual("v9.9.9", data["latest_version"])
        self.assertEqual("v9.9.9", self.settings["update_latest_version"])

    def test_check_now_button_reports_being_up_to_date(self):
        import app as app_package

        self.settings = {"update_check": True}
        fake = FakeClient(FakeResponse({"tag_name": app_package.__version__, "html_url": "u"}))
        with patch("app.update_check.httpx.AsyncClient", fake):
            data = self.client.post("/api/settings/update-check").json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["update_available"])

    def test_check_now_ignores_the_24h_cache(self):
        # The button means "now" — a fresh cache must not short-circuit it.
        self.settings = {"update_check": True, "update_last_checked": int(time.time())}
        FakeClient.calls = []
        fake = FakeClient(FakeResponse({"tag_name": "v9.9.9", "html_url": "u"}))
        with patch("app.update_check.httpx.AsyncClient", fake):
            self.client.post("/api/settings/update-check")
        self.assertEqual([update_check.RELEASES_URL], FakeClient.calls)

    def test_check_now_works_while_disabled_but_persists_nothing(self):
        # A click is the explicit request the automatic check is missing — but
        # a one-off look must leave no trace, like switching the check off.
        self.settings = {}
        FakeClient.calls = []
        fake = FakeClient(FakeResponse({"tag_name": "v9.9.9", "html_url": "u"}))
        with patch("app.update_check.httpx.AsyncClient", fake):
            data = self.client.post("/api/settings/update-check").json()
        self.assertEqual([update_check.RELEASES_URL], FakeClient.calls)
        self.assertTrue(data["update_available"])
        self.assertFalse(data["enabled"])
        self.assertEqual({}, self.settings)

    def test_check_now_reports_failures_instead_of_claiming_up_to_date(self):
        self.settings = {"update_check": True}
        fake = FakeClient(error=OSError("no route to host"))
        with patch("app.update_check.httpx.AsyncClient", fake):
            data = self.client.post("/api/settings/update-check").json()
        self.assertFalse(data["ok"])
        self.assertFalse(data["update_available"])
        self.assertIn("no route to host", data["error"])

    def test_check_now_requires_authentication(self):
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        self.assertEqual(401, self.client.post("/api/settings/update-check").status_code)


if __name__ == "__main__":
    unittest.main()
