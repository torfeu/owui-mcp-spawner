import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app.tool_editor import validate_tool_code


CONTROL_TOOL_PATH = Path(__file__).resolve().parents[1] / "examples" / "mcp-manager-control.json"


def load_control_tools():
    exported = json.loads(CONTROL_TOOL_PATH.read_text())[0]
    namespace = {}
    exec(exported["content"], namespace)
    return namespace["Tools"]()


class ControlToolSpecTests(unittest.TestCase):
    """The MCP runner serves the JSON 'specs' descriptions (tool_loader), while the
    schema comes from the code — so a hand-edited spec silently drifts from its
    docstring. Regenerate the specs instead of editing them."""

    def test_specs_match_the_docstrings_of_the_shipped_code(self):
        exported = json.loads(CONTROL_TOOL_PATH.read_text())[0]
        result = validate_tool_code(exported["content"])

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(
            {t["name"]: t for t in result["tools"]},
            {s["name"]: s for s in exported["specs"]},
        )

    def test_write_actions_document_the_lock(self):
        # These endpoints call require_not_locked() and answer 403 on a locked
        # instance; start/stop deliberately keep working.
        tools = load_control_tools()
        blocked_by_lock = (
            "save_tool_code", "reinstall_instance", "restart_instance", "delete_instance",
            "update_instance_category", "update_instance_values",
            "update_instance_dependencies", "update_instance_venv",
        )

        for name in blocked_by_lock:
            with self.subTest(tool=name):
                self.assertIn("locked", getattr(tools, name).__doc__)


class ControlToolRegressionTests(unittest.TestCase):
    def test_export_requires_code_read_permission(self):
        tools = load_control_tools()
        tools.valves.allow_export_tool = True
        tools.valves.allow_get_code = False
        tools._get = Mock()

        result = tools.export_tool("demo", "Demo", "Description")

        self.assertIn("allow_get_code", result)
        tools._get.assert_not_called()

    def test_value_update_reports_automatic_restart(self):
        tools = load_control_tools()
        tools._put = Mock(return_value=json.dumps({"ok": True, "restarted": True}))

        result = tools.update_instance_values("demo", {"api_url": "new"})

        self.assertIn("already restarted", result)
        self.assertNotIn("did not restart", result)

    def test_value_update_only_suggests_restart_when_manager_did_not_restart(self):
        tools = load_control_tools()
        tools._put = Mock(return_value=json.dumps({"ok": True, "restarted": False}))

        result = tools.update_instance_values("demo", {"api_url": "new"})

        self.assertIn("did not restart", result)
        self.assertIn("If it is currently running", result)

    def test_dependency_update_does_not_reinstall_twice(self):
        tools = load_control_tools()
        tools._put = Mock(return_value=json.dumps({"ok": True, "restarted": True}))
        tools._post = Mock()

        result = tools.update_instance_dependencies("demo", ["httpx>=0.27"])

        self.assertIn('"ok": true', result)
        tools._put.assert_called_once()
        tools._post.assert_not_called()

    def test_upload_omits_empty_category_override(self):
        tools = load_control_tools()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "id": "demo"}

        with patch("httpx.post", return_value=response) as post:
            tools.upload_tool("[]")

        self.assertEqual({}, post.call_args.kwargs["data"])

    def test_upload_sends_non_empty_category_override(self):
        tools = load_control_tools()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "id": "demo"}

        with patch("httpx.post", return_value=response) as post:
            tools.upload_tool("[]", "  Smart Home  ")

        self.assertEqual({"category": "Smart Home"}, post.call_args.kwargs["data"])


class LifecycleToolTests(unittest.TestCase):
    """auto_start is not a valve, and there was no other way to reach it.

    That gap is why `{"lifecycle": {"auto_start": false}}` was sent to
    update_instance_values, where it was stored as a valve of that name and
    reported as saved while the real setting never moved.
    """

    def test_it_sends_the_lifecycle_field_not_the_values_field(self):
        tools = load_control_tools()
        tools._put = Mock(return_value='{"ok": true, "restarted": false}')

        tools.update_instance_lifecycle("standort_umgebung", auto_start="false")

        path, body = tools._put.call_args.args
        self.assertEqual("/api/instances/standort_umgebung", path)
        self.assertEqual({"lifecycle": {"auto_start": False}}, body)

    def test_an_empty_argument_leaves_that_setting_alone(self):
        tools = load_control_tools()
        tools._put = Mock(return_value='{"ok": true}')

        tools.update_instance_lifecycle("demo", restart_on_change="true")

        self.assertEqual({"lifecycle": {"restart_on_change": True}}, tools._put.call_args.args[1])

    def test_both_can_be_set_at_once(self):
        tools = load_control_tools()
        tools._put = Mock(return_value='{"ok": true}')

        tools.update_instance_lifecycle("demo", auto_start="on", restart_on_change="off")

        self.assertEqual({"lifecycle": {"auto_start": True, "restart_on_change": False}},
                         tools._put.call_args.args[1])

    def test_a_value_that_is_neither_true_nor_false_is_refused(self):
        tools = load_control_tools()
        tools._put = Mock()

        result = tools.update_instance_lifecycle("demo", auto_start="maybe")

        self.assertIn("must be", result)
        tools._put.assert_not_called()

    def test_calling_it_with_nothing_to_change_says_so(self):
        tools = load_control_tools()
        tools._put = Mock()

        result = tools.update_instance_lifecycle("demo")

        self.assertIn("Nothing to change", result)
        tools._put.assert_not_called()

    def test_the_valve_switches_it_off(self):
        tools = load_control_tools()
        tools.valves.allow_update_lifecycle = False
        tools._put = Mock()

        self.assertIn("allow_update_lifecycle", tools.update_instance_lifecycle("demo", "true"))
        tools._put.assert_not_called()

    def test_the_values_tool_points_at_this_one(self):
        # The docstring is what the model reads before it improvises.
        tools = load_control_tools()
        doc = tools.update_instance_values.__doc__
        self.assertIn("update_instance_lifecycle", doc)


class SystemStatsToolTests(unittest.TestCase):
    """What the model gets to see of the machine.

    Same house rule as the file storage: a short answer by default, detail on
    request. And one rule of its own — an unmeasured value must arrive as '?',
    never as a zero the model would report as "idle", and a manager that cannot
    measure must hand over its reason instead of an empty result the model
    would explain by guessing.
    """

    STATS = {
        "available": True, "reason": "",
        "machine": {
            "cpu_percent": 34.2, "cpu_count": 16,
            "memory": {"used": 9 * 1024 ** 3, "total": 31 * 1024 ** 3, "percent": 29.6},
            "disk": {"free": 210 * 1024 ** 3, "total": 2 * 1024 ** 4, "percent": 89.2,
                     "path": "/home/user/mcp-manager"},
            "network": {"up_bps": 148 * 1024, "down_bps": None},
        },
        "instances": {
            "small": {"rss": 40 * 1024 ** 2, "processes": 1, "cpu_percent": 2.0},
            "big": {"rss": 700 * 1024 ** 2, "processes": 3, "cpu_percent": None},
        },
    }

    def test_the_overview_is_prose_and_not_a_json_dump(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        result = tools.get_system_stats()

        self.assertIn("CPU 34 % of 16 cores", result)
        self.assertIn("9.0 GB of 31.0 GB", result)
        self.assertIn("2.0 TB", result)          # a disk that outgrew GB
        self.assertNotIn("{", result)

    def test_every_drive_is_listed_with_the_installation_marked(self):
        # A machine with a data drive and a backup drive was being described by
        # its system drive alone.
        tools = load_control_tools()
        stats = json.loads(json.dumps(self.STATS))
        gb = 1024 ** 3
        stats["machine"]["disks"] = [
            {"mount": "/", "free": 346 * gb, "total": 464 * gb, "percent": 25.4, "install": True},
            {"mount": "/data", "free": 677 * gb, "total": 937 * gb, "percent": 27.7,
             "install": False},
        ]
        tools._get = Mock(return_value=json.dumps(stats))

        line = [l for l in tools.get_system_stats().splitlines() if l.startswith("Disks:")][0]

        self.assertIn("/data 677.0 GB free of 937.0 GB", line)
        self.assertIn("installation", line)

    def test_an_older_manager_reporting_one_disk_still_reads_correctly(self):
        # The window between an rsync and the restart: new tool, old API.
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        line = [l for l in tools.get_system_stats().splitlines() if l.startswith("Disks:")][0]

        self.assertIn("2.0 TB", line)
        self.assertNotIn("installation", line)   # nothing to distinguish from

    def test_the_heaviest_instance_comes_first(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        lines = tools.get_system_stats().splitlines()
        instance_lines = [line for line in lines if line.startswith("- ")]

        self.assertTrue(instance_lines[0].startswith("- big:"), instance_lines)
        self.assertIn("3 processes", instance_lines[0])

    def test_an_unmeasured_value_is_a_question_mark_not_a_zero(self):
        # "0 % CPU" and "0 B/s" would be read as idle. They are not measured.
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        result = tools.get_system_stats()

        self.assertIn("down ?", result)
        self.assertIn("? CPU", result)
        self.assertNotIn("0 % CPU", result)

    def test_top_caps_the_list_and_says_what_it_left_out(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        result = tools.get_system_stats(top=1)

        self.assertIn("- big:", result)
        self.assertNotIn("- small:", result)
        self.assertIn("1 more not shown", result)

    def test_one_instance_can_be_asked_for_on_its_own(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        result = tools.get_system_stats("small")

        self.assertTrue(result.startswith("small: "), result)
        self.assertIn("40.0 MB RAM", result)

    def test_an_instance_that_is_not_measured_says_which_ones_are(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.STATS))

        result = tools.get_system_stats("stopped_one")

        self.assertIn("only running instances are measured", result)
        self.assertIn("big, small", result)

    def test_without_psutil_the_reason_is_handed_over_verbatim(self):
        # The small model must not be left to invent a cause for an empty result.
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(
            {"available": False, "reason": "psutil is not installed", "machine": None,
             "instances": {}}))

        result = tools.get_system_stats()

        self.assertIn("psutil is not installed", result)

    def test_the_valve_switches_it_off(self):
        tools = load_control_tools()
        tools.valves.allow_get_system = False
        tools._get = Mock()

        result = tools.get_system_stats()

        self.assertIn("allow_get_system", result)
        tools._get.assert_not_called()


class ContentToolTests(unittest.TestCase):
    """What the model gets to see of the file storage.

    The rule that matters here is the house rule: little by default, detail on
    request. The overview must not be a JSON dump, and a .docx must never end
    up in the chat as bytes.
    """

    OVERVIEW = {
        "instances": [
            {"instance": "docs", "files": 2, "bytes": 3 * 1024 * 1024,
             "oldest": None, "limit_bytes": 10 * 1024 * 1024, "percent": 30},
        ],
        "total_bytes": 3 * 1024 * 1024, "total_files": 2,
    }
    FILES = {
        "instance": "docs", "bytes": 3 * 1024 * 1024, "files": 2,
        "items": [
            {"name": "report.docx", "size": 2 * 1024 * 1024, "modified": None,
             "url": "/content/docs/report.docx?t=abc"},
            {"name": "notes.md", "size": 1024, "modified": None,
             "url": "/content/docs/notes.md?t=def"},
        ],
    }

    def test_the_overview_is_one_line_per_instance_and_not_a_json_dump(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.OVERVIEW))

        result = tools.list_content()

        self.assertIn("docs: 2 file(s), 3.0 MB", result)
        self.assertIn("30 % of quota", result)
        self.assertNotIn("{", result)

    def test_naming_an_instance_gives_the_files_with_their_links(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.FILES))

        result = tools.list_content("docs")

        self.assertIn("report.docx", result)
        # Relative links from the manager are made absolute here — the model
        # hands this to a user, who has no manager URL to resolve it against.
        self.assertIn("http://127.0.0.1:7860/content/docs/report.docx?t=abc", result)

    def test_a_binary_file_is_described_and_never_dumped(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.FILES))
        response = Mock(status_code=200, content=b"PK\x03\x04\x00binary")

        with patch("httpx.get", return_value=response):
            result = tools.read_content("docs", "report.docx")

        self.assertIn("binary docx file", result)
        self.assertIn("/content/docs/report.docx?t=abc", result)

    def test_a_text_file_comes_back_and_is_capped(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.FILES))
        response = Mock(status_code=200, content=("x" * 50).encode())

        with patch("httpx.get", return_value=response):
            full = tools.read_content("docs", "notes.md")
            cut = tools.read_content("docs", "notes.md", max_chars=10)

        self.assertEqual("x" * 50, full)
        self.assertTrue(cut.startswith("x" * 10))
        self.assertIn("cut off after 10 characters of 50", cut)

    def test_deleting_is_off_until_it_is_switched_on(self):
        tools = load_control_tools()
        tools._delete = Mock()

        result = tools.delete_content("docs", "report.docx")

        self.assertIn("allow_content_delete", result)
        tools._delete.assert_not_called()

    def test_an_empty_filename_empties_the_whole_folder(self):
        tools = load_control_tools()
        tools.valves.allow_content_delete = True
        tools._delete = Mock(return_value="{}")

        tools.delete_content("docs")

        tools._delete.assert_called_once_with("/api/content/docs")


if __name__ == "__main__":
    unittest.main()
