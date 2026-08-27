"""Usage tracking: every tool call with time, instance and function.

Recorded in the runner, because with one port per instance the manager never
sees a tool call. Only `tools/call` counts. Two tables: `calls` is pruned,
`totals` is not — so "ever used" survives any retention setting.
"""
import asyncio
import hashlib
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.activity as activity
import app.auth as auth
from app.admin_server import app


class UsageDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "usage.db"
        activity.close()
        self.patch = patch.object(activity, "DB_PATH", self.db)
        self.patch.start()
        activity._pending.clear()

    def tearDown(self):
        activity.close()
        self.patch.stop()
        activity._pending.clear()
        self.tmp.cleanup()

    def test_a_call_is_recorded_with_time_instance_and_function(self):
        activity.record_call("demo", "get_paragraph")
        usage = activity.read_usage("demo")
        self.assertEqual(1, usage["calls"])
        self.assertEqual("get_paragraph", usage["last_tool"])
        self.assertAlmostEqual(time.time(), usage["last_call"], delta=5)

    def test_calls_are_counted_per_function(self):
        for _ in range(3):
            activity.record_call("demo", "search_law")
        activity.record_call("demo", "get_toc")

        summary = activity.usage_summary(days=7)["demo"]
        self.assertEqual(4, summary["calls"])
        self.assertEqual(3, summary["tools"]["search_law"]["calls"])
        self.assertEqual(1, summary["tools"]["get_toc"]["calls"])
        # The point of the whole exercise: a function nobody calls is visible
        # as such, so it can be removed from the tool instead of costing context.
        self.assertNotIn("never_called", summary["tools"])

    def test_the_timestamp_is_taken_when_the_call_happens(self):
        # Not when the batch is written — otherwise ten calls of one chat all
        # land on the same second and the spacing between them is lost.
        with patch.object(activity, "_schedule_flush"):
            activity.record_call("demo", "a")
            early = activity._pending[0][0]
            time.sleep(1.1)
            activity.record_call("demo", "b")
            late = activity._pending[1][0]
        self.assertLess(early, late)

    def test_a_burst_is_written_as_one_batch_and_counted_exactly(self):
        async def burst():
            for _ in range(10):
                activity.record_call("demo", "a")
            await activity.flush()

        asyncio.run(burst())
        self.assertEqual(10, activity.read_usage("demo")["calls"])
        self.assertEqual([], activity._pending)

    def test_pruning_keeps_the_totals(self):
        # "Ever used" must survive the retention window, otherwise the setting
        # would quietly falsify the most valuable answer: never used?
        old = int(time.time()) - 90 * 86400
        activity._write_batch([(old, "demo", "ancient"), (int(time.time()), "demo", "fresh")])

        removed = activity.prune(days=30)

        self.assertEqual(1, removed)
        self.assertEqual(2, activity.read_usage("demo")["calls"])  # totals intact
        summary = activity.usage_summary(days=30)["demo"]
        self.assertEqual(1, summary["tools"]["ancient"]["calls"])  # ever used
        self.assertEqual(0, summary["tools"]["ancient"]["recent"])  # but not lately
        self.assertEqual(1, summary["tools"]["fresh"]["recent"])

    def test_retention_zero_keeps_everything(self):
        activity._write_batch([(int(time.time()) - 400 * 86400, "demo", "a")])
        self.assertEqual(0, activity.prune(days=0))
        self.assertEqual(1, activity.usage_summary(days=1000)["demo"]["calls"])

    def test_recent_counts_only_the_selected_window(self):
        now = int(time.time())
        activity._write_batch([
            (now - 10 * 86400, "demo", "a"),
            (now - 2 * 86400, "demo", "a"),
            (now, "demo", "a"),
        ])
        self.assertEqual(2, activity.usage_summary(days=7)["demo"]["recent"])
        self.assertEqual(3, activity.usage_summary(days=30)["demo"]["recent"])

    def test_daily_counts_feed_the_sparkline(self):
        now = int(time.time())
        activity._write_batch([(now, "demo", "a"), (now, "demo", "a"),
                               (now - 2 * 86400, "demo", "a")])
        buckets = activity.bucket_counts("demo", days=7)
        self.assertEqual(7, len(buckets))
        self.assertEqual(2, buckets[-1])
        self.assertEqual(3, sum(buckets))

    def test_buckets_are_calendar_days_not_rolling_windows(self):
        """A call just before midnight belongs to that date, not to "22h ago".

        With rolling 24h buckets the same event moved between buckets over the
        course of a day, which makes "which day was busy" unanswerable.
        """
        import datetime as dt

        midnight = dt.datetime.combine(dt.date.today(), dt.time.min)
        late_yesterday = int((midnight - dt.timedelta(minutes=5)).timestamp())
        early_today = int((midnight + dt.timedelta(minutes=5)).timestamp())
        activity._write_batch([
            (late_yesterday, "demo", "a"),
            (early_today, "demo", "a"),
            (early_today, "demo", "a"),
        ])
        buckets = activity.bucket_counts("demo", days=7)
        self.assertEqual(2, buckets[-1], "today")
        self.assertEqual(1, buckets[-2], "yesterday")

    def test_the_window_labels_end_on_today(self):
        import datetime as dt

        labels = activity.bucket_labels(7)
        self.assertEqual(7, len(labels))
        self.assertEqual(dt.date.today().isoformat(), labels[-1])
        self.assertEqual(
            (dt.date.today() - dt.timedelta(days=6)).isoformat(), labels[0]
        )

    def test_one_query_carries_every_instance(self):
        now = int(time.time())
        activity._write_batch([(now, "one", "a"), (now, "two", "b"), (now, "two", "b")])
        matrix = activity.bucket_matrix(days=7)
        self.assertEqual(1, matrix["one"][-1])
        self.assertEqual(2, matrix["two"][-1])
        self.assertNotIn("three", matrix)

    def test_the_shortest_window_is_cut_into_hours(self):
        """"The last 24 hours" is a question about now, so it rolls with the clock.

        Longer windows snap to dates; this one must not, or "today" at 00:30
        would be an empty chart.
        """
        now = int(time.time())
        activity._write_batch([
            (now, "demo", "a"),
            (now - 3 * 3600, "demo", "a"),
            (now - 25 * 3600, "demo", "a"),  # just outside
        ])
        self.assertEqual("hour", activity.bucket_unit(1))
        buckets = activity.bucket_counts("demo", days=1)
        self.assertEqual(24, len(buckets))
        self.assertEqual(2, sum(buckets), "the call from 25h ago is outside")
        self.assertEqual(1, buckets[-1], "the current hour")

    def test_hour_labels_carry_their_date(self):
        labels = activity.bucket_labels(1)
        self.assertEqual(24, len(labels))
        # Two dates in the window, so the hour alone would be ambiguous.
        self.assertRegex(labels[0], r"^\d{4}-\d{2}-\d{2}T\d{2}$")
        self.assertEqual(len(set(labels)), 24)

    def test_forget_removes_events_and_totals(self):
        activity.record_call("demo", "a")
        activity.forget("demo")
        self.assertEqual({}, activity.read_usage("demo"))
        self.assertEqual({}, activity.usage_summary())
        activity.forget("demo")  # idempotent

    def test_an_unusable_database_never_breaks_a_tool_call(self):
        activity.close()
        with patch.object(activity, "DB_PATH", pathlib.Path("/proc/nope/usage.db")):
            activity.record_call("demo", "a")  # must not raise
            self.assertEqual({}, activity.read_usage("demo"))
            self.assertEqual({}, activity.usage_summary())
        activity.close()

    def test_never_used_instances_simply_do_not_appear(self):
        self.assertEqual({}, activity.read_usage("untouched"))
        self.assertEqual({}, activity.usage_summary())


class UsageApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.tmp = tempfile.TemporaryDirectory()
        self.tool_path = pathlib.Path(self.tmp.name) / "demo.json"
        self.tool_path.write_text(json.dumps({"content": "x", "specs": []}))

    def tearDown(self):
        auth._password_hash = self.original_hash
        self.tmp.cleanup()

    def _config(self):
        from app.schema import MCPConfig, ServerConfig, ToolSourceConfig

        return MCPConfig(
            id="demo", name="Demo", server=ServerConfig(port=8123),
            tool_source=ToolSourceConfig(path=str(self.tool_path)),
        )

    def test_usage_is_served_with_the_function_catalog(self):
        # One fetch when the info dialog opens, instead of a second round trip.
        with (
            patch("app.routes.instances.load_config", return_value=self._config()),
            patch("app.api_helpers.resolve_tool_path", return_value=self.tool_path),
            patch("app.routes.instances.read_usage",
                  return_value={"calls": 7, "last_call": 1785000000, "last_tool": "search_law"}),
        ):
            data = self.client.get("/api/instances/demo/specs").json()
        self.assertEqual(7, data["usage"]["calls"])
        self.assertEqual("search_law", data["usage"]["last_tool"])

    def test_usage_needs_authentication_like_the_rest_of_the_endpoint(self):
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        self.assertEqual(401, self.client.get("/api/instances/demo/specs").status_code)

    def test_the_polled_instance_list_stays_free_of_usage(self):
        # The list is polled every few seconds per tab — a database query per
        # instance per poll is exactly what this must not become.
        fake_state = type("State", (), {
            "id": "demo", "name": "Demo", "description": "", "category": "",
            "status": type("Status", (), {"value": "running"})(),
            "port": 8123, "host": "127.0.0.1", "endpoint": "/mcp", "pid": 1, "error": "",
        })()
        with (
            patch("app.routes.instances.get_all_states", return_value=[fake_state]),
            patch("app.routes.instances.load_all_configs", return_value={"demo": self._config()}),
            patch("app.api_helpers.resolve_tool_path", return_value=self.tool_path),
        ):
            row = self.client.get("/api/instances").json()[0]
            row_with_specs = self.client.get("/api/instances?include=specs").json()[0]
        self.assertNotIn("usage", row)
        self.assertNotIn("usage", row_with_specs)


class UsageEndpointTests(unittest.TestCase):
    """`GET /api/usage` — the statistics view and the control tool read this."""

    def setUp(self):
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "usage.db"
        activity.close()
        self.patch = patch.object(activity, "DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        activity.close()
        self.patch.stop()
        auth._password_hash = self.original_hash
        self.tmp.cleanup()

    def _configs(self, *ids):
        return {
            i: type("Config", (), {"id": i, "name": i.title(), "category": "Recht", "venv": "default"})()
            for i in ids
        }

    def _states(self, *ids):
        return [
            type("State", (), {"id": i, "status": type("S", (), {"value": "running"})()})()
            for i in ids
        ]

    def _get(self, url="/api/usage"):
        with (
            patch("app.routes.usage.load_all_configs", return_value=self._configs("busy", "idle")),
            patch("app.routes.usage.get_all_states", return_value=self._states("busy", "idle")),
        ):
            return self.client.get(url).json()

    def test_never_used_instances_are_listed_with_zeros(self):
        # The whole reason for the report: what can be stopped?
        activity._write_batch([(int(time.time()), "busy", "search")])
        rows = {row["id"]: row for row in self._get()["instances"]}
        self.assertEqual(1, rows["busy"]["calls"])
        self.assertEqual(0, rows["idle"]["calls"])
        self.assertEqual([], rows["idle"]["tools"])

    def test_ranking_follows_the_window_not_the_lifetime_total(self):
        # "What was once important" is a different question from "what do I use".
        now = int(time.time())
        activity._write_batch(
            [(now - 60 * 86400, "idle", "old")] * 50 + [(now, "busy", "fresh")] * 2
        )
        order = [row["id"] for row in self._get("/api/usage?days=7")["instances"]]
        self.assertEqual(["busy", "idle"], order)

    def test_per_function_numbers_are_included(self):
        now = int(time.time())
        activity._write_batch([(now, "busy", "search")] * 3 + [(now, "busy", "toc")])
        tools = {t["name"]: t for t in self._get()["instances"][0]["tools"]}
        self.assertEqual(3, tools["search"]["calls"])
        self.assertEqual(1, tools["toc"]["calls"])

    def test_the_window_is_clamped_to_something_sane(self):
        self.assertEqual(1, self._get("/api/usage?days=0")["days"])
        self.assertEqual(3650, self._get("/api/usage?days=99999")["days"])

    def test_usage_requires_authentication(self):
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        self.assertEqual(401, self.client.get("/api/usage").status_code)


if __name__ == "__main__":
    unittest.main()
