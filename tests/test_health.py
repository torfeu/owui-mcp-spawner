"""The health check: what "running" is worth, and how reluctant a restart is.

Two things carry this. That the check is skipped rather than failed while an
instance is still coming up — the spawn lock is held then, and a verdict handed
down in that window is a race, not a diagnosis. And that auto-restart gives up:
a tool broken for good must not restart itself every minute, because the runtime
log is where the reason for the breakage is and a restart loop buries it.
"""
import asyncio
import time
import unittest
from unittest.mock import patch

import app.health as health
from app.schema import MCPInstance, MCPStatus


def instance(instance_id="demo", status=MCPStatus.running, pid=4242,
             host="127.0.0.1", port=8101):
    return MCPInstance(id=instance_id, name=instance_id, status=status, pid=pid,
                       host=host, port=port, endpoint="/mcp")


def run(coro):
    return asyncio.run(coro)


class ProbeTargetTests(unittest.TestCase):
    def test_a_wildcard_bind_is_probed_over_the_loopback(self):
        # Connecting to 0.0.0.0 is not portable; the process manager's own port
        # check makes the same substitution.
        self.assertEqual("http://127.0.0.1:8101/mcp",
                         health._probe_url(instance(host="0.0.0.0")))

    def test_a_real_host_is_kept(self):
        self.assertEqual("http://192.168.1.100:8101/mcp",
                         health._probe_url(instance(host="192.168.1.100")))


class ReasonTests(unittest.TestCase):
    """A failure reason nobody can act on is the same as no reason at all."""

    def test_a_task_group_wrapper_is_unwrapped_to_its_cause(self):
        # This is what a refused connection really looks like coming out of the
        # SDK: the group's own message names the plumbing, not the problem.
        group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)",
                               [OSError("All connection attempts failed")])
        self.assertEqual("All connection attempts failed", health._reason(group))

    def test_nested_groups_are_flattened_and_repeats_dropped(self):
        inner = ExceptionGroup("inner", [OSError("Connection refused"),
                                         OSError("Connection refused")])
        outer = ExceptionGroup("outer", [inner, OSError("Not Found")])
        self.assertEqual("Connection refused; Not Found", health._reason(outer))

    def test_an_exception_without_a_message_still_names_its_type(self):
        self.assertEqual("TimeoutError", health._reason(TimeoutError()))


class RecordTests(unittest.TestCase):
    def setUp(self):
        health._health.clear()
        self.addCleanup(health._health.clear)

    def test_nothing_is_reported_before_the_first_probe(self):
        self.assertIsNone(health.for_instance("demo"))

    def test_a_good_answer_carries_the_tool_count(self):
        run(health._record(instance(), True, 7, ""))
        entry = health.for_instance("demo")
        self.assertEqual("ok", entry["status"])
        self.assertEqual(7, entry["tools"])
        self.assertEqual(0, entry["failures"])

    def test_failures_accumulate_and_the_reason_is_kept(self):
        with patch.object(health, "autorestart", return_value=False):
            run(health._record(instance(), False, None, "connection refused"))
            run(health._record(instance(), False, None, "connection refused"))
        entry = health.for_instance("demo")
        self.assertEqual("failing", entry["status"])
        self.assertEqual(2, entry["failures"])
        self.assertEqual("connection refused", entry["error"])

    def test_a_good_answer_clears_the_failure_count(self):
        with patch.object(health, "autorestart", return_value=False):
            run(health._record(instance(), False, None, "timeout"))
        run(health._record(instance(), True, 3, ""))
        self.assertEqual(0, health.for_instance("demo")["failures"])

    def test_no_restart_while_auto_restart_is_off(self):
        with patch.object(health, "autorestart", return_value=False), \
             patch("app.process_manager.restart_instance") as restart:
            for _ in range(10):
                run(health._record(instance(), False, None, "timeout"))
        restart.assert_not_called()

    def test_the_restart_waits_for_the_configured_number_of_failures(self):
        with patch.object(health, "autorestart", return_value=True), \
             patch.object(health, "failures_before_restart", return_value=3), \
             patch("app.process_manager.restart_instance") as restart:
            run(health._record(instance(), False, None, "timeout"))
            run(health._record(instance(), False, None, "timeout"))
            self.assertEqual(0, restart.call_count)
            run(health._record(instance(), False, None, "timeout"))
            self.assertEqual(1, restart.call_count)
        restart.assert_called_with("demo")

    def test_the_backoff_holds_the_second_restart_back(self):
        with patch.object(health, "autorestart", return_value=True), \
             patch.object(health, "failures_before_restart", return_value=1), \
             patch("app.process_manager.restart_instance") as restart:
            run(health._record(instance(), False, None, "timeout"))     # restart 1
            run(health._record(instance(), False, None, "timeout"))     # too soon
            self.assertEqual(1, restart.call_count)
            # Pretend the first restart was long enough ago.
            health._health["demo"]["last_restart"] = time.time() - health.RESTART_BACKOFF[0] - 1
            run(health._record(instance(), False, None, "timeout"))
            self.assertEqual(2, restart.call_count)

    def test_it_gives_up_after_the_cap(self):
        with patch.object(health, "autorestart", return_value=True), \
             patch.object(health, "failures_before_restart", return_value=1), \
             patch("app.process_manager.restart_instance") as restart:
            for _ in range(health.MAX_RESTARTS + 4):
                # Every attempt gets a clean slate on the timing, so only the
                # cap can stop it.
                if "demo" in health._health:
                    health._health["demo"]["last_restart"] = 0.0
                run(health._record(instance(), False, None, "timeout"))
        self.assertEqual(health.MAX_RESTARTS, restart.call_count)
        self.assertEqual(health.MAX_RESTARTS, health.for_instance("demo")["restarts"])

    def test_restarts_are_forgiven_only_after_a_long_healthy_stretch(self):
        with patch.object(health, "autorestart", return_value=True), \
             patch.object(health, "failures_before_restart", return_value=1), \
             patch("app.process_manager.restart_instance"):
            run(health._record(instance(), False, None, "timeout"))
        run(health._record(instance(), True, 2, ""))
        # One good answer is not proof of recovery — the flap would be endless.
        self.assertEqual(1, health.for_instance("demo")["restarts"])
        health._health["demo"]["healthy_since"] = time.time() - health.RESTART_RESET_AFTER - 1
        run(health._record(instance(), True, 2, ""))
        self.assertEqual(0, health.for_instance("demo")["restarts"])


class IdentityNoteTests(unittest.TestCase):
    """An instance that hides its catalog is healthy, and has to say why."""

    def setUp(self):
        health._health.clear()
        self.addCleanup(health._health.clear)

    def test_an_empty_catalog_behind_required_identity_is_explained(self):
        run(health._record(instance(), True, 0, "", identity_required=True))
        entry = health.for_instance("demo")
        self.assertEqual("ok", entry["status"])
        self.assertIn("requires a verified user", entry["note"])

    def test_an_instance_that_lists_tools_carries_no_note(self):
        # A `required` instance with a machine identity does return a catalog.
        run(health._record(instance(), True, 6, "", identity_required=True))
        self.assertEqual("", health.for_instance("demo")["note"])

    def test_an_ordinary_empty_catalog_is_not_explained_away(self):
        # Without the identity gate, zero tools is zero tools.
        run(health._record(instance(), True, 0, "", identity_required=False))
        self.assertEqual("", health.for_instance("demo")["note"])

    def test_a_failure_clears_the_note(self):
        run(health._record(instance(), True, 0, "", identity_required=True))
        with patch.object(health, "autorestart", return_value=False):
            run(health._record(instance(), False, None, "timeout"))
        self.assertEqual("", health.for_instance("demo")["note"])


class PassTests(unittest.TestCase):
    def setUp(self):
        health._health.clear()
        self.addCleanup(health._health.clear)

    def test_an_instance_that_is_still_starting_is_skipped(self):
        probed = []

        async def fake_probe(inst):
            probed.append(inst.id)
            return True, 1, ""

        with patch("app.health.get_all_states", return_value=[instance()]), \
             patch("app.process_manager.start_in_progress", return_value=True), \
             patch.object(health, "probe", fake_probe):
            run(health.check_once())

        self.assertEqual([], probed)
        self.assertIsNone(health.for_instance("demo"))

    def test_a_running_instance_is_probed(self):
        async def fake_probe(inst):
            return True, 5, ""

        with patch("app.health.get_all_states", return_value=[instance()]), \
             patch("app.process_manager.start_in_progress", return_value=False), \
             patch.object(health, "probe", fake_probe):
            run(health.check_once())

        self.assertEqual(5, health.for_instance("demo")["tools"])

    def test_a_pass_reads_the_identity_mode_from_the_configs(self):
        class Cfg:
            identity_mode = "required"

        async def fake_probe(inst):
            return True, 0, ""

        with patch("app.health.get_all_states", return_value=[instance()]), \
             patch("app.health.load_all_configs", return_value={"demo": Cfg()}), \
             patch("app.process_manager.start_in_progress", return_value=False), \
             patch.object(health, "probe", fake_probe):
            run(health.check_once())

        self.assertIn("requires a verified user", health.for_instance("demo")["note"])

    def test_unreadable_configs_do_not_stop_a_pass(self):
        async def fake_probe(inst):
            return True, 4, ""

        with patch("app.health.get_all_states", return_value=[instance()]), \
             patch("app.health.load_all_configs", side_effect=OSError("boom")), \
             patch("app.process_manager.start_in_progress", return_value=False), \
             patch.object(health, "probe", fake_probe):
            run(health.check_once())

        self.assertEqual("ok", health.for_instance("demo")["status"])

    def test_a_stopped_instance_loses_its_history(self):
        # Otherwise a stale failure count would trigger a restart the moment
        # somebody starts it again.
        health._health["demo"] = {"status": "failing", "checked_at": 0.0, "tools": None,
                                  "error": "x", "failures": 9, "restarts": 0,
                                  "last_restart": 0.0, "healthy_since": 0.0}
        with patch("app.health.get_all_states",
                   return_value=[instance(status=MCPStatus.stopped, pid=None)]), \
             patch("app.process_manager.start_in_progress", return_value=False):
            run(health.check_once())

        self.assertIsNone(health.for_instance("demo"))


class SettingTests(unittest.TestCase):
    def test_a_garbled_or_out_of_range_value_falls_back_into_bounds(self):
        for stored, expected in ((0, 1), (999, 20), ("seven", 3), (None, 3), (5, 5)):
            with patch.object(health, "load_settings",
                              return_value={"health_failures_before_restart": stored}):
                self.assertEqual(expected, health.failures_before_restart(), stored)

    def test_checking_is_on_and_restarting_is_off_by_default(self):
        with patch.object(health, "load_settings", return_value={}):
            self.assertTrue(health.enabled())
            self.assertFalse(health.autorestart())

    def test_the_settings_route_reports_the_three_switches(self):
        from fastapi.testclient import TestClient
        import app.auth as auth
        from app.admin_server import app

        # On the server this suite runs against an installation that has a
        # password; the question here is what the payload carries, not whether
        # the route is guarded.
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))
        body = TestClient(app).get("/api/settings").json()

        for key in ("health_check_enabled", "health_autorestart",
                    "health_failures_before_restart", "health_max_restarts"):
            self.assertIn(key, body)


class SpawnLockTests(unittest.TestCase):
    def test_the_predicate_sees_a_held_lock(self):
        from app import process_manager

        self.assertFalse(process_manager.start_in_progress("nobody-holds-this"))
        lock = process_manager._start_lock_for("held")
        lock.acquire()
        try:
            self.assertTrue(process_manager.start_in_progress("held"))
        finally:
            lock.release()
        self.assertFalse(process_manager.start_in_progress("held"))


if __name__ == "__main__":
    unittest.main()
