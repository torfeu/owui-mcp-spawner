"""The system monitor: the numbers, and what happens when they cannot be taken.

Two things carry this feature. The rates, because a counter divided by a
too-short interval is where a monitor starts lying — and a fabricated 0.0 in a
tile reads as "idle", not as "unknown". And the missing-psutil path, because
that is the state every existing installation is in for the minutes between an
rsync and the pip install: the manager has to keep serving, and the endpoint
has to say what is wrong instead of returning a 500 that looks like a bug.
"""
import contextlib
import os
import pathlib
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.system_stats as system_stats
from app.admin_server import app


class MachineStatsTests(unittest.TestCase):
    def setUp(self):
        # Every rate is measured against module state; start each test blind.
        self._reset()
        self.addCleanup(self._reset)

    @staticmethod
    def _reset():
        system_stats._last_net = None
        system_stats._last_net_rates = {"up_bps": None, "down_bps": None}
        system_stats._last_cpu_at = 0.0
        system_stats._last_cpu_value = None
        system_stats._procs.clear()
        system_stats._proc_cpu.clear()

    def test_first_network_sample_reports_nothing_rather_than_zero(self):
        first = system_stats._net_rates()
        self.assertIsNone(first["up_bps"])
        self.assertIsNone(first["down_bps"])

    def test_network_rate_is_bytes_per_second_between_two_samples(self):
        counters = type("C", (), {"bytes_sent": 1000, "bytes_recv": 2000})
        with patch.object(system_stats.psutil, "net_io_counters", return_value=counters):
            system_stats._net_rates()
        system_stats._last_net = (system_stats.time.monotonic() - 2.0, 1000, 2000)
        later = type("C", (), {"bytes_sent": 3000, "bytes_recv": 2500})
        with patch.object(system_stats.psutil, "net_io_counters", return_value=later):
            rates = system_stats._net_rates()
        self.assertAlmostEqual(rates["up_bps"], 1000.0, delta=50)
        self.assertAlmostEqual(rates["down_bps"], 250.0, delta=50)

    def test_a_second_reader_gets_the_last_rate_instead_of_stealing_the_sample(self):
        # This is the bug the first live run showed: with a dashboard polling
        # every four seconds, an MCP call landing beside a poll came back
        # "unknown" although a good measurement existed a moment earlier.
        system_stats._last_net = (system_stats.time.monotonic() - 2.0, 1000, 2000)
        counters = type("C", (), {"bytes_sent": 3000, "bytes_recv": 2500})
        with patch.object(system_stats.psutil, "net_io_counters", return_value=counters):
            first = system_stats._net_rates()
            second = system_stats._net_rates()          # immediately after
        self.assertIsNotNone(first["up_bps"])
        self.assertEqual(first, second)

    def test_a_reader_inside_the_window_leaves_the_baseline_alone(self):
        # Overwriting it would move the start of the interval the next reader
        # measures over, and shrink it towards nothing.
        system_stats._last_net = (system_stats.time.monotonic() - 2.0, 1000, 2000)
        baseline = system_stats._last_net
        counters = type("C", (), {"bytes_sent": 3000, "bytes_recv": 2500})
        with patch.object(system_stats.psutil, "net_io_counters", return_value=counters):
            system_stats._net_rates()
            taken = system_stats._last_net
            system_stats._net_rates()
        self.assertNotEqual(baseline, taken)
        self.assertEqual(taken, system_stats._last_net)

    def test_a_second_cpu_reader_does_not_take_a_new_sample(self):
        system_stats._last_cpu_at = system_stats.time.monotonic() - 1.0
        with patch.object(system_stats.psutil, "cpu_percent", return_value=42.0) as sampler:
            first = system_stats._cpu_percent()
            second = system_stats._cpu_percent()
        self.assertEqual(42.0, first)
        self.assertEqual(42.0, second)
        self.assertEqual(1, sampler.call_count)

    def test_a_counter_that_went_backwards_is_unknown_not_negative(self):
        # An interface going down (or a reboot) resets the counters. Reporting
        # the negative delta would paint a huge spike in the wrong direction.
        system_stats._last_net = (system_stats.time.monotonic() - 2.0, 10_000, 10_000)
        counters = type("C", (), {"bytes_sent": 5, "bytes_recv": 5})
        with patch.object(system_stats.psutil, "net_io_counters", return_value=counters):
            rates = system_stats._net_rates()
        self.assertIsNone(rates["up_bps"])
        self.assertIsNone(rates["down_bps"])

    def test_machine_stats_carry_memory_disk_and_the_measured_path(self):
        system_stats.machine_stats()          # prime
        system_stats._last_cpu_at -= 1.0      # …and let the next one count
        stats = system_stats.machine_stats()
        self.assertGreater(stats["memory"]["total"], 0)
        self.assertGreaterEqual(stats["memory"]["total"], stats["memory"]["used"])
        self.assertGreater(stats["disk"]["total"], 0)
        self.assertEqual(stats["disk"]["path"], str(system_stats.BASE_DIR))
        self.assertIsInstance(stats["cpu_percent"], float)


class Part:
    def __init__(self, device, mountpoint, fstype="ext4"):
        self.device, self.mountpoint, self.fstype = device, mountpoint, fstype


class Usage:
    def __init__(self, total, free):
        self.total, self.free = total, free
        self.percent = round((total - free) / total * 100, 1) if total else 0.0


GB = 1024 ** 3


class DiskTests(unittest.TestCase):
    """A machine with more than one drive showed only the one it boots from."""

    # The reference server, as `psutil.disk_partitions` reports it.
    SERVER = {
        "/": (Part("/dev/mapper/ubuntu--vg-ubuntu--lv", "/"), Usage(464 * GB, 346 * GB)),
        "/boot": (Part("/dev/nvme0n1p2", "/boot"), Usage(2 * GB, 1 * GB)),
        "/boot/efi": (Part("/dev/nvme0n1p1", "/boot/efi", "vfat"), Usage(GB, GB)),
        "/data": (Part("/dev/nvme1n1p1", "/data"), Usage(937 * GB, 677 * GB)),
        "/mnt/backup": (Part("/dev/sda1", "/mnt/backup"), Usage(457 * GB, 323 * GB)),
    }

    def _with(self, table, base="/home/user/mcp-manager"):
        parts = [entry[0] for entry in table.values()]
        usage = {mount: entry[1] for mount, entry in table.items()}
        return (patch.object(system_stats, "_partitions", return_value=parts),
                patch.object(system_stats.psutil, "disk_usage",
                             side_effect=lambda m: usage[m]),
                patch.object(system_stats, "BASE_DIR", pathlib.Path(base)))

    def test_every_real_drive_is_reported(self):
        with contextlib.ExitStack() as stack:
            for ctx in self._with(self.SERVER):
                stack.enter_context(ctx)
            disks = system_stats._disks()

        self.assertEqual(["/", "/data", "/mnt/backup"], sorted(d["mount"] for d in disks))

    def test_boot_partitions_are_not_storage(self):
        with contextlib.ExitStack() as stack:
            for ctx in self._with(self.SERVER):
                stack.enter_context(ctx)
            mounts = [d["mount"] for d in system_stats._disks()]

        self.assertNotIn("/boot", mounts)
        self.assertNotIn("/boot/efi", mounts)

    def test_the_install_disk_is_marked_and_comes_first(self):
        with contextlib.ExitStack() as stack:
            for ctx in self._with(self.SERVER):
                stack.enter_context(ctx)
            disks = system_stats._disks()

        self.assertEqual("/", disks[0]["mount"])
        self.assertTrue(disks[0]["install"])
        self.assertEqual(1, sum(1 for d in disks if d["install"]))

    def test_an_install_on_the_data_drive_belongs_to_that_drive(self):
        # Longest matching mount wins: "/data/..." is not on "/".
        with contextlib.ExitStack() as stack:
            for ctx in self._with(self.SERVER, base="/data/mcp-manager"):
                stack.enter_context(ctx)
            disks = system_stats._disks()

        self.assertEqual("/data", disks[0]["mount"])
        self.assertTrue(disks[0]["install"])

    def test_one_container_seen_through_several_mounts_counts_once(self):
        # APFS reports each volume of a container with the container's size;
        # without this the tile row would carry five copies of one disk.
        apfs = {
            "/": (Part("/dev/disk3s1", "/", "apfs"), Usage(245 * GB, 14 * GB)),
            "/System/Volumes/Data": (Part("/dev/disk3s5", "/System/Volumes/Data", "apfs"),
                                     Usage(245 * GB, 14 * GB)),
            "/System/Volumes/Preboot": (Part("/dev/disk3s2", "/System/Volumes/Preboot", "apfs"),
                                        Usage(245 * GB, 14 * GB)),
        }
        with contextlib.ExitStack() as stack:
            for ctx in self._with(apfs, base="/Users/x/project"):
                stack.enter_context(ctx)
            disks = system_stats._disks()

        self.assertEqual(1, len(disks))
        self.assertEqual("/", disks[0]["mount"])

    def test_a_mount_that_cannot_be_read_is_skipped_not_fatal(self):
        parts = [Part("/dev/sda1", "/mnt/locked"), Part("/dev/sdb1", "/data")]
        usage = {"/data": Usage(937 * GB, 677 * GB)}

        def read(mount):
            if mount not in usage:
                raise PermissionError(mount)
            return usage[mount]

        with patch.object(system_stats, "_partitions", return_value=parts), \
             patch.object(system_stats.psutil, "disk_usage", side_effect=read), \
             patch.object(system_stats, "BASE_DIR", pathlib.Path("/data/app")):
            disks = system_stats._disks()

        self.assertEqual(["/data"], [d["mount"] for d in disks])


class InstanceStatsTests(unittest.TestCase):
    def setUp(self):
        system_stats._procs.clear()
        system_stats._proc_cpu.clear()
        self.addCleanup(system_stats._procs.clear)
        self.addCleanup(system_stats._proc_cpu.clear)

    def test_the_running_test_process_is_measured(self):
        stats = system_stats.instance_stats({"self": os.getpid()})
        self.assertIn("self", stats)
        self.assertGreater(stats["self"]["rss"], 0)
        # First look at a process: psutil has no baseline, so no CPU figure yet.
        self.assertIsNone(stats["self"]["cpu_percent"])
        # Let the sampling window pass — otherwise the second reader is handed
        # the first one's answer, which is the point of the window.
        when, value = system_stats._proc_cpu[os.getpid()]
        system_stats._proc_cpu[os.getpid()] = (when - 1.0, value)
        again = system_stats.instance_stats({"self": os.getpid()})
        self.assertIsInstance(again["self"]["cpu_percent"], float)

    def test_a_pid_that_is_gone_is_simply_absent(self):
        # 2**31-1 is above every configured pid_max; nothing can hold it.
        stats = system_stats.instance_stats({"ghost": 2 ** 31 - 1})
        self.assertEqual({}, stats)

    def test_instances_without_a_pid_are_skipped(self):
        self.assertEqual({}, system_stats.instance_stats({"stopped": None}))

    def test_a_second_reader_is_handed_the_previous_cpu_figure(self):
        # Two readers a moment apart must not turn a busy instance into 0 %.
        system_stats.instance_stats({"self": os.getpid()})
        when, _ = system_stats._proc_cpu[os.getpid()]
        system_stats._proc_cpu[os.getpid()] = (when - 1.0, 17.5)
        stats = system_stats.instance_stats({"self": os.getpid()})   # takes a sample
        measured = stats["self"]["cpu_percent"]
        again = system_stats.instance_stats({"self": os.getpid()})   # inside the window
        self.assertEqual(measured, again["self"]["cpu_percent"])

    def test_processes_that_ended_are_dropped_from_the_cache(self):
        system_stats.instance_stats({"self": os.getpid()})
        self.assertIn(os.getpid(), system_stats._procs)
        system_stats.instance_stats({})
        self.assertEqual({}, system_stats._procs)


class SystemRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # The suite runs on the server too, where a password *is* configured.
        # These tests are about what the route answers, not about its auth —
        # the one test below that cares sets a hash of its own.
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

    def test_stats_answer_with_machine_and_instances(self):
        body = self.client.get("/api/system/stats").json()
        self.assertTrue(body["available"])
        self.assertEqual("", body["reason"])
        self.assertIn("memory", body["machine"])
        self.assertIsInstance(body["instances"], dict)

    def test_without_psutil_the_route_explains_itself_instead_of_failing(self):
        with patch.object(system_stats, "psutil", None):
            response = self.client.get("/api/system/stats")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertFalse(body["available"])
        self.assertIn("psutil", body["reason"])
        self.assertIsNone(body["machine"])
        self.assertEqual({}, body["instances"])

    def test_the_route_is_behind_the_same_auth_as_the_rest(self):
        auth._password_hash = "0" * 64          # setUp restores it
        self.assertEqual(401, self.client.get("/api/system/stats").status_code)


if __name__ == "__main__":
    unittest.main()


class StartedAtTests(unittest.TestCase):
    """When an instance started — read from the process, never remembered."""

    def test_the_running_process_answers_with_its_start(self):
        import os
        from app.system_stats import started_at

        value = started_at(os.getpid())
        self.assertIsInstance(value, float)
        self.assertLess(value, time.time() + 1)

    def test_nothing_to_ask_means_nothing_to_answer(self):
        from app.system_stats import started_at

        self.assertIsNone(started_at(None))
        self.assertIsNone(started_at(0))
        # A pid nothing is behind: not an error, just no answer.
        self.assertIsNone(started_at(2 ** 22 - 1))

    def test_a_stopped_instance_carries_no_time(self):
        # "stopped since" is a different fact, and one the manager does not
        # know — showing the last start for it would be a small lie.
        from app.api_helpers import _instance_to_dict
        from app.schema import MCPInstance, MCPStatus

        inst = MCPInstance(id="x", name="X", category="", status=MCPStatus.stopped,
                           port=8101, host="127.0.0.1", endpoint="/mcp", pid=None)
        self.assertIsNone(_instance_to_dict(inst)["started_at"])

    def test_a_running_instance_carries_the_process_start(self):
        import os
        from app.api_helpers import _instance_to_dict
        from app.schema import MCPInstance, MCPStatus

        inst = MCPInstance(id="x", name="X", category="", status=MCPStatus.running,
                           port=8101, host="127.0.0.1", endpoint="/mcp", pid=os.getpid())
        self.assertIsInstance(_instance_to_dict(inst)["started_at"], float)
