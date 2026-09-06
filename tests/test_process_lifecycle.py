"""Start and stop meeting each other — the window where a runner gets orphaned.

Starting an instance takes as long as its venv does: `ensure_venv()` may build
an environment and run pip, and the start holds its own lock the whole time.
Stopping must not wait for that, so stop deliberately does not take that lock —
which used to mean a stop could confirm, and a start still spawn a runner half a
minute later. Status `stopped`, pid set, port held, and the watchdog looks only
at instances marked `running`: nothing on the dashboard said anything was there.

The two tests that carry this module are the two orders. A stop landing while
the venv is being prepared must leave nothing behind, and a stop landing after
the runner exists must find its pid and kill it.

The third is the one the lock alone did not cover: a start that is overtaken
while it waits for its port. It used to recognise a stop only by the shared
status, and a *later* start puts that status back to running — so the first
start woke up, concluded it had never been stopped, and wrote its long-dead pid
over the live one. Each start now holds a number instead, and a stop or a newer
start takes it away for good.

Everything is faked down to `Popen`: this suite runs on the server, where
spawning a real runner would take a real port.
"""
import os
import pathlib
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import app.config_store as config_store
import app.process_manager as pm
from app.schema import MCPConfig, MCPInstance, MCPStatus


class FakeProc:
    """A Popen that is alive until something kills it."""
    _next_pid = 90000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.alive = True
        self.signals = []

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.signals.append("TERM")
        self.alive = False

    def kill(self):
        self.signals.append("KILL")
        self.alive = False


def config(instance_id="demo", port=8399):
    return MCPConfig(
        id=instance_id, name=instance_id,
        server={"host": "127.0.0.1", "port": port, "endpoint": "/mcp"},
        tool_source={"type": "openwebui_json", "path": f"tools/{instance_id}.json"},
    )


class LifecycleRaceTests(unittest.TestCase):
    """Every path start and stop write to, pointed somewhere harmless."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.pids_file = root / "pids.json"
        self.log = root / "demo.log"

        self.cfg = config()
        self.inst = MCPInstance(id="demo", name="demo", status=MCPStatus.stopped,
                                port=8399, host="127.0.0.1", endpoint="/mcp")
        config_store._state["demo"] = self.inst
        self.addCleanup(lambda: config_store._state.pop("demo", None))

        # A fresh lock pair per test — the registries are module state, and a
        # test holding a lock from a previous run would hang the next one. The
        # generation counter goes with them: it is per instance, and "demo"
        # is reused by every test here.
        pm._start_locks.clear()
        pm._spawn_locks.clear()
        pm._generations.clear()

        self.procs = []

        def spawn(*a, **kw):
            proc = FakeProc()
            self.procs.append(proc)
            return proc

        def kill(pid, sig):
            # Signals land on the fakes, never on a real process — the pids
            # here are invented, and the one thing worse than a flaky test is
            # a test that signals whatever else holds that number.
            for proc in self.procs:
                if proc.pid == pid:
                    proc.alive = False
                    proc.signals.append(sig)
                    return
            raise ProcessLookupError(pid)

        for target in (
            patch.object(pm, "PIDS_FILE", self.pids_file),
            patch.object(pm, "load_config", lambda _id: self.cfg),
            patch.object(pm, "get_runtime_log_path", lambda _id: self.log),
            patch.object(pm, "subprocess", **{"Popen.side_effect": spawn}),
            patch.object(pm, "_port_answering", lambda *a, **kw: True),
            patch.object(pm, "_pid_is_our_runner", lambda pid: True),
            patch.object(pm, "_is_pid_alive", self._alive),
            patch.object(pm, "os", types.SimpleNamespace(
                environ=os.environ, kill=kill, waitpid=os.waitpid, WNOHANG=os.WNOHANG)),
            patch("app.shared_proxy.instances_localhost_only", lambda: True),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _alive(self, pid):
        return any(p.pid == pid and p.alive for p in self.procs)

    def start_in_thread(self):
        result = {}
        thread = threading.Thread(target=lambda: result.update(
            zip(("ok", "error"), pm.start_instance("demo"))))
        thread.start()
        return thread, result

    def test_a_stop_during_venv_preparation_leaves_no_runner_behind(self):
        """The reported case. The stop confirms while the start is still inside
        ensure_venv(); whatever the start does afterwards, nothing may survive
        it — an untracked runner holds the port and answers on it."""
        preparing = threading.Event()
        release = threading.Event()

        def slow_venv(_venv):
            preparing.set()
            release.wait(5)
            return True, ""

        with patch.object(pm, "ensure_venv", slow_venv):
            thread, result = self.start_in_thread()
            self.assertTrue(preparing.wait(5), "start never reached ensure_venv")

            ok, err = pm.stop_instance("demo")
            self.assertTrue(ok, err)

            release.set()
            thread.join(10)

        self.assertFalse(result["ok"])
        self.assertEqual("Instance was stopped during startup", result["error"])
        self.assertEqual(MCPStatus.stopped, self.inst.status)
        self.assertIsNone(self.inst.pid)
        self.assertFalse([p for p in self.procs if p.alive],
                         "a runner outlived a confirmed stop")

    def test_a_stop_after_the_spawn_kills_the_runner_it_finds(self):
        """The other order: the start got as far as publishing its pid. The
        stop must use it — that is the reason the pid is published while the
        instance still says 'starting'."""
        spawned = threading.Event()
        release = threading.Event()

        def wait_for_port(*a, **kw):
            spawned.set()
            release.wait(5)
            return True

        with patch.object(pm, "ensure_venv", lambda _v: (True, "")), \
             patch.object(pm, "_port_answering", wait_for_port):
            thread, result = self.start_in_thread()
            self.assertTrue(spawned.wait(5), "start never spawned")
            self.assertEqual(1, len(self.procs))
            self.assertEqual(self.procs[0].pid, self.inst.pid)

            ok, err = pm.stop_instance("demo")
            release.set()
            thread.join(10)

        self.assertTrue(ok, err)
        self.assertFalse(self.procs[0].alive)
        self.assertFalse(result["ok"])
        self.assertEqual(MCPStatus.stopped, self.inst.status)
        self.assertIsNone(self.inst.pid)

    def test_a_start_overtaken_by_a_newer_one_does_not_publish_its_own_pid(self):
        """Start A waits for its port; a stop kills A; start B takes over and
        registers itself; then A wakes up. A must not overwrite B — the manager
        and the watchdog would follow a pid that is gone while B holds the
        port, which is the orphan from the other direction."""
        waiting = threading.Event()
        release = threading.Event()

        def wait_for_port(*a, **kw):
            if not waiting.is_set():
                waiting.set()
                release.wait(5)
            return True

        with patch.object(pm, "ensure_venv", lambda _v: (True, "")), \
             patch.object(pm, "_port_answering", wait_for_port):
            thread_a, result_a = self.start_in_thread()
            self.assertTrue(waiting.wait(5), "A never reached the port check")
            proc_a = self.procs[0]

            ok, err = pm.stop_instance("demo")
            self.assertTrue(ok, err)
            self.assertFalse(proc_a.alive)

            ok, err = pm.start_instance("demo")
            self.assertTrue(ok, err)
            proc_b = self.procs[1]

            release.set()
            thread_a.join(10)

        self.assertFalse(result_a["ok"], "the overtaken start reported success")
        self.assertTrue(proc_b.alive)
        self.assertEqual(proc_b.pid, self.inst.pid)
        self.assertEqual(MCPStatus.running, self.inst.status)
        self.assertEqual(proc_b.pid, pm._load_pids()["demo"]["pid"])

    def test_an_overtaken_start_does_not_mark_the_new_runner_failed(self):
        """The same race through the other exit: A's own process dies while B
        is already running. A must not write that failure onto the instance."""
        waiting = threading.Event()
        release = threading.Event()

        def wait_for_port(*a, **kw):
            if not waiting.is_set():
                waiting.set()
                release.wait(5)
            return True

        with patch.object(pm, "ensure_venv", lambda _v: (True, "")), \
             patch.object(pm, "_port_answering", wait_for_port):
            thread_a, result_a = self.start_in_thread()
            self.assertTrue(waiting.wait(5))
            pm.stop_instance("demo")                 # kills A's process
            ok, err = pm.start_instance("demo")      # B takes over
            self.assertTrue(ok, err)
            proc_b = self.procs[1]
            release.set()
            thread_a.join(10)

        self.assertFalse(result_a["ok"])
        self.assertEqual(MCPStatus.running, self.inst.status)
        self.assertEqual(proc_b.pid, self.inst.pid)
        self.assertIn("demo", pm._load_pids())

    def test_a_stop_still_killing_does_not_deregister_a_newer_runner(self):
        """Killing a process can run for seconds, and a start arriving in that
        window is allowed to take over. What the stop must not then do is
        finish its own bookkeeping: clearing the state and pids.json would
        leave the new runner alive, unregistered and invisible to the
        watchdog — the orphan again, from a third direction."""
        with patch.object(pm, "ensure_venv", lambda _v: (True, "")):
            ok, err = pm.start_instance("demo")
            self.assertTrue(ok, err)
            proc_a = self.procs[0]

            killing, release = threading.Event(), threading.Event()
            original = pm._wait_pid_gone

            def slow_wait(pid, timeout):
                if not killing.is_set():
                    killing.set()
                    release.wait(5)
                return original(pid, timeout)

            with patch.object(pm, "_wait_pid_gone", slow_wait):
                stop_result = {}
                stopping = threading.Thread(target=lambda: stop_result.update(
                    zip(("ok", "error"), pm.stop_instance("demo"))))
                stopping.start()
                self.assertTrue(killing.wait(5), "the stop never reached the wait")

                ok, err = pm.start_instance("demo")
                self.assertTrue(ok, err)
                proc_b = self.procs[1]

                release.set()
                stopping.join(10)

        self.assertTrue(stop_result["ok"], stop_result)   # A really is gone
        self.assertFalse(proc_a.alive)
        self.assertTrue(proc_b.alive)
        self.assertEqual(MCPStatus.running, self.inst.status)
        self.assertEqual(proc_b.pid, self.inst.pid)
        self.assertEqual(proc_b.pid, pm._load_pids()["demo"]["pid"])

    def test_a_stop_landing_between_starting_and_the_claim_is_not_forgotten(self):
        """The narrow window the generation itself opened: the state said
        `starting` and the number had not been taken yet, so a stop that ran
        to completion raised the counter — and the start then took a *higher*
        number and looked current. Publishing the status and claiming the
        number are one step now."""
        announced, release = threading.Event(), threading.Event()
        original, paused = pm.set_instance_state, []

        def watch(inst):
            original(inst)
            if not paused and inst.status == MCPStatus.starting:
                paused.append(1)
                announced.set()
                release.wait(5)

        with patch.object(pm, "ensure_venv", lambda _v: (True, "")), \
             patch.object(pm, "set_instance_state", watch):
            thread, result = self.start_in_thread()
            self.assertTrue(announced.wait(5), "the start never announced itself")
            ok, err = pm.stop_instance("demo")
            self.assertTrue(ok, err)
            release.set()
            thread.join(10)

        self.assertFalse(result["ok"], "the start ran on through a completed stop")
        self.assertFalse([p for p in self.procs if p.alive],
                         "a runner outlived a confirmed stop")
        self.assertEqual(MCPStatus.stopped, self.inst.status)
        self.assertIsNone(self.inst.pid)

    def test_an_undisturbed_start_still_ends_up_running_and_tracked(self):
        with patch.object(pm, "ensure_venv", lambda _v: (True, "")):
            ok, err = pm.start_instance("demo")
        self.assertTrue(ok, err)
        self.assertEqual(MCPStatus.running, self.inst.status)
        self.assertEqual(self.procs[0].pid, self.inst.pid)
        self.assertEqual(self.procs[0].pid, pm._load_pids()["demo"]["pid"])
        self.assertTrue(self.procs[0].alive)

    def test_a_stop_and_a_start_of_one_instance_never_produce_two_runners(self):
        """Fifty rounds of the same race with the timing left to the machine.
        Whatever order they land in, at most one runner may be alive and the
        pid on the instance must be that runner's — or none at all."""
        with patch.object(pm, "ensure_venv", lambda _v: (True, "")):
            for _ in range(50):
                thread, _result = self.start_in_thread()
                pm.stop_instance("demo")
                thread.join(10)
                alive = [p for p in self.procs if p.alive]
                self.assertLessEqual(len(alive), 1, "more than one runner alive")
                if alive:
                    self.assertEqual(alive[0].pid, self.inst.pid)
                    pm.stop_instance("demo")
                else:
                    self.assertIsNone(self.inst.pid)
