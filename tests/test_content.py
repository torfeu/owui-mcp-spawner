"""The content store: where a tool's files land, and who gets them back out.

Three things carry the whole feature, and all three are here. The path checks,
because `content/<instance>/<name>` is assembled from a name a tool chose. The
per-file token, because it is the only thing between a link in a chat and every
other file on the server. And the rewriting, because a tool written for
OpenWebUI hands back a link that resolves against OpenWebUI — that rewrite is
the single point where "runs unchanged" would otherwise fail.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.content_store as content_store
import app.settings_store as settings_store
from app.admin_server import app
from app.tool_loader import OpenWebUITool, create_tools_instance


def isolate(case, **settings) -> pathlib.Path:
    """Give *case* its own content folder, token secret and settings file.

    The suite's promise is that it leaves the real installation alone, and this
    module deletes files for a living.
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    (root / "content").mkdir()

    settings_file = root / "settings.json"
    settings_file.write_text(json.dumps(settings))

    for target, attr, value in (
        (content_store, "CONTENT_DIR", root / "content"),
        (content_store, "SECRET_FILE", root / "content.key"),
        (settings_store, "SETTINGS_FILE", settings_file),
    ):
        patcher = patch.object(target, attr, value)
        patcher.start()
        case.addCleanup(patcher.stop)
    return root / "content"


def write_file(root: pathlib.Path, instance: str, name: str, size: int = 10,
               age_days: float = 0) -> pathlib.Path:
    folder = root / instance
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


class PathTests(unittest.TestCase):
    """Nothing addressed from outside may leave the instance folder."""

    def setUp(self):
        self.root = isolate(self)
        write_file(self.root, "demo", "report.docx")

    def test_a_plain_name_resolves_inside_the_instance_folder(self):
        path = content_store.resolve_file("demo", "report.docx")
        self.assertIsNotNone(path)
        # Compared resolved: on macOS the temp folder lives behind the
        # /var → /private/var symlink, and resolve_file returns the real path.
        self.assertEqual((self.root / "demo" / "report.docx").resolve(), path)

    def test_traversal_and_separators_are_refused(self):
        for name in ("../secret", "..", ".", "sub/file.txt", "..\\secret",
                     "/etc/passwd", "", "a\x00b"):
            with self.subTest(name=name):
                self.assertIsNone(content_store.resolve_file("demo", name))

    def test_an_absolute_path_does_not_win_over_the_folder(self):
        # The one that bites in path joining: Path("/a") / "/etc/passwd" is
        # "/etc/passwd". The separator check catches it before it gets there.
        self.assertIsNone(content_store.resolve_file("demo", "/etc/passwd"))

    def test_a_symlink_pointing_out_of_the_folder_is_refused(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret")
        link = self.root / "demo" / "link.txt"
        link.symlink_to(outside)
        # The name is harmless — only resolving it shows where it goes.
        self.assertIsNone(content_store.resolve_file("demo", "link.txt"))

    def test_an_instance_id_is_not_a_path_either(self):
        for instance in ("../other", "a/b", "", "."):
            with self.subTest(instance=instance):
                self.assertIsNone(content_store.resolve_file(instance, "report.docx"))

    def test_dotfiles_stay_out_of_reach(self):
        write_file(self.root, "demo", ".hidden")
        self.assertIsNone(content_store.resolve_file("demo", ".hidden"))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate(self)
        write_file(self.root, "demo", "a.docx")
        write_file(self.root, "demo", "b.docx")

    def test_a_token_opens_exactly_one_file(self):
        token_a = content_store.file_token("demo", "a.docx")
        self.assertTrue(content_store.token_valid("demo", "a.docx", token_a))
        self.assertFalse(content_store.token_valid("demo", "b.docx", token_a))

    def test_a_token_does_not_carry_over_to_another_instance(self):
        token = content_store.file_token("demo", "a.docx")
        self.assertFalse(content_store.token_valid("other", "a.docx", token))

    def test_an_empty_or_wrong_token_is_refused(self):
        for token in ("", "0" * 16, None):
            with self.subTest(token=token):
                self.assertFalse(content_store.token_valid("demo", "a.docx", token))

    def test_the_secret_is_written_once_and_reused(self):
        # A regenerated secret would invalidate every link already handed out,
        # which is why the secret lives in a file and not in memory.
        first = content_store.file_token("demo", "a.docx")
        content_store.SECRET_FILE.chmod(0o600)
        self.assertEqual(first, content_store.file_token("demo", "a.docx"))


class LinkRewriteTests(unittest.TestCase):
    """The one point where "the tool runs unchanged" would otherwise fail."""

    def setUp(self):
        self.root = isolate(self, content_base_url="https://mcp.example/")
        write_file(self.root, "docs", "spike_db4cbd.docx")

    def rewrite(self, text: str) -> str:
        return content_store.rewrite_links(text, "docs", "/cache/files/")

    def test_the_openwebui_link_becomes_our_download_url(self):
        out = self.rewrite("[Report](/cache/files/spike_db4cbd.docx)")
        expected = content_store.file_url("docs", "spike_db4cbd.docx")
        self.assertEqual(f"[Report]({expected})", out)
        self.assertIn("https://mcp.example/content/docs/spike_db4cbd.docx?t=", out)

    def test_nothing_else_in_the_text_is_touched(self):
        text = (
            "Copy exactly the text between the lines.\n"
            "----\n"
            "Here is your file: [Report](/cache/files/spike_db4cbd.docx)\n"
            "Mention /cache/files/ in passing, and https://example.org/a.docx too.\n"
            "----\n"
        )
        out = self.rewrite(text)
        self.assertEqual(1, out.count("?t="))
        for line in ("Copy exactly the text between the lines.", "----",
                     "https://example.org/a.docx"):
            self.assertIn(line, out)

    def test_a_link_to_a_file_that_is_not_there_stays_as_it_is(self):
        # Otherwise a tool that mentions the prefix, or wrote its file
        # elsewhere, would get a token for something that does not exist.
        text = "[Missing](/cache/files/never_written.docx)"
        self.assertEqual(text, self.rewrite(text))

    def test_a_traversal_dressed_up_as_a_link_is_not_rewritten(self):
        text = "[x](/cache/files/../../etc/passwd)"
        self.assertEqual(text, self.rewrite(text))

    def test_without_a_base_url_the_link_is_relative(self):
        with patch.object(content_store, "base_url", return_value=""):
            out = self.rewrite("[Report](/cache/files/spike_db4cbd.docx)")
        self.assertIn("(/content/docs/spike_db4cbd.docx?t=", out)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate(self, content_max_mb=1, content_warn_percent=80)

    def fill(self, fraction: float) -> None:
        write_file(self.root, "demo", "big.bin", size=int(1024 * 1024 * fraction))

    def test_no_warning_while_there_is_room(self):
        self.fill(0.5)
        self.assertEqual("", content_store.quota_warning("demo"))

    def test_the_warning_appears_at_the_threshold(self):
        self.fill(0.9)
        warning = content_store.quota_warning("demo")
        self.assertIn("nearly full", warning)
        # One line, and it says what to do — a small model invents a cause for
        # anything longer.
        self.assertEqual(1, len(warning.splitlines()))
        self.assertIn("delete old files", warning)

    def test_a_full_folder_says_full_and_not_nearly(self):
        self.fill(1.2)
        self.assertTrue(content_store.is_full("demo"))
        self.assertIn("is full", content_store.quota_warning("demo"))

    def test_without_a_limit_nothing_ever_warns(self):
        with patch.object(content_store, "max_bytes", return_value=0):
            self.fill(5)
            self.assertEqual("", content_store.quota_warning("demo"))
            self.assertFalse(content_store.is_full("demo"))

    def test_the_quota_is_measured_per_instance(self):
        # One instance filling the disk must not put a warning in front of
        # another instance's results — that is a sentence it cannot act on.
        self.fill(1.2)
        self.assertEqual("", content_store.quota_warning("other"))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate(self)

    def names(self, instance="demo"):
        return sorted(p.name for p in (self.root / instance).iterdir())

    def test_file_age_drops_only_what_is_past_the_window(self):
        write_file(self.root, "demo", "old.docx", age_days=40)
        write_file(self.root, "demo", "new.docx", age_days=1)
        self.assertEqual(1, content_store.prune(days=30, mode="file_age"))
        self.assertEqual(["new.docx"], self.names())

    def test_whole_folder_empties_as_soon_as_the_oldest_is_obsolete(self):
        write_file(self.root, "demo", "old.docx", age_days=40)
        write_file(self.root, "demo", "new.docx", age_days=1)
        self.assertEqual(2, content_store.prune(days=30, mode="whole_folder"))
        self.assertEqual([], self.names())

    def test_whole_folder_keeps_everything_while_the_oldest_is_young(self):
        write_file(self.root, "demo", "a.docx", age_days=5)
        write_file(self.root, "demo", "b.docx", age_days=1)
        self.assertEqual(0, content_store.prune(days=30, mode="whole_folder"))
        self.assertEqual(["a.docx", "b.docx"], self.names())

    def test_an_empty_folder_is_not_a_special_case(self):
        (self.root / "demo").mkdir()
        for mode in ("file_age", "whole_folder"):
            with self.subTest(mode=mode):
                self.assertEqual(0, content_store.prune(days=1, mode=mode))

    def test_zero_days_means_keep_forever(self):
        write_file(self.root, "demo", "ancient.docx", age_days=4000)
        self.assertEqual(0, content_store.prune(days=0, mode="file_age"))
        self.assertEqual(["ancient.docx"], self.names())

    def test_each_instance_folder_is_judged_on_its_own(self):
        write_file(self.root, "a", "old.docx", age_days=40)
        write_file(self.root, "b", "new.docx", age_days=1)
        content_store.prune(days=30, mode="whole_folder")
        self.assertEqual([], self.names("a"))
        self.assertEqual(["new.docx"], self.names("b"))


class ValveAutofillTests(unittest.TestCase):
    """The manager fills the output valve — unless the user already did."""

    CODE = (
        "from pydantic import BaseModel\n"
        "class Tools:\n"
        "    class Valves(BaseModel):\n"
        "        docx_export_dir: str = ''\n"
        "        output_dir: str = ''\n"
        "        api_key: str = ''\n"
        "    def __init__(self):\n"
        "        self.valves = self.Valves()\n"
        "    def run(self) -> str:\n"
        "        return 'ok'\n"
    )

    def instance(self, values, content_dir="/srv/content/demo"):
        tool = OpenWebUITool({"content": self.CODE, "specs": []})
        return create_tools_instance(tool, values, content_dir)

    def test_an_empty_output_valve_is_filled_with_the_instance_folder(self):
        valves = self.instance({}).valves
        self.assertEqual("/srv/content/demo", valves.docx_export_dir)
        self.assertEqual("/srv/content/demo", valves.output_dir)

    def test_a_value_the_user_set_is_never_overwritten(self):
        valves = self.instance({"docx_export_dir": "/mnt/share"}).valves
        self.assertEqual("/mnt/share", valves.docx_export_dir)
        self.assertEqual("/srv/content/demo", valves.output_dir)

    def test_valves_that_are_not_about_output_stay_untouched(self):
        valves = self.instance({"api_key": "abc"}).valves
        self.assertEqual("abc", valves.api_key)

    def test_without_the_store_switched_on_nothing_is_filled(self):
        valves = self.instance({}, content_dir=None).valves
        self.assertEqual("", valves.docx_export_dir)


class DownloadRouteTests(unittest.TestCase):
    """The chat link: one file, for whoever holds its token."""

    def setUp(self):
        self.root = isolate(self)
        write_file(self.root, "demo", "report.docx", size=32)
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.addCleanup(setattr, auth, "_password_hash", self.original_hash)

    def url(self, name="report.docx", instance="demo"):
        return f"/content/{instance}/{name}?t={content_store.file_token(instance, name)}"

    def test_the_file_comes_back_with_its_token(self):
        response = self.client.get(self.url())
        self.assertEqual(200, response.status_code)
        self.assertEqual(b"x" * 32, response.content)

    def test_it_is_served_as_a_download_and_never_rendered(self):
        headers = self.client.get(self.url()).headers
        self.assertIn("attachment", headers["content-disposition"])
        self.assertEqual("nosniff", headers["x-content-type-options"])

    def test_no_token_and_a_wrong_token_are_the_same_404(self):
        for suffix in ("", "?t=", "?t=" + "0" * 16):
            with self.subTest(suffix=suffix):
                response = self.client.get(f"/content/demo/report.docx{suffix}")
                self.assertEqual(404, response.status_code)

    def test_a_token_for_one_file_does_not_fetch_another(self):
        write_file(self.root, "demo", "private.docx")
        token = content_store.file_token("demo", "report.docx")
        response = self.client.get(f"/content/demo/private.docx?t={token}")
        self.assertEqual(404, response.status_code)

    def test_there_is_no_directory_listing(self):
        for path in ("/content/demo", "/content/demo/", "/content/"):
            with self.subTest(path=path):
                self.assertEqual(404, self.client.get(path).status_code)

    def test_traversal_in_the_url_finds_nothing(self):
        outside = self.root.parent / "settings.json"
        token = content_store.file_token("demo", "../settings.json")
        response = self.client.get(f"/content/demo/..%2Fsettings.json?t={token}")
        self.assertEqual(404, response.status_code)
        self.assertTrue(outside.exists())


class ContentApiTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate(self, content_max_mb=1)
        write_file(self.root, "demo", "a.docx", size=100)
        write_file(self.root, "demo", "b.docx", size=200)
        write_file(self.root, "other", "c.docx", size=50)
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.addCleanup(setattr, auth, "_password_hash", self.original_hash)

    def test_the_overview_counts_every_folder(self):
        body = self.client.get("/api/content").json()
        self.assertEqual(350, body["total_bytes"])
        self.assertEqual(3, body["total_files"])
        self.assertEqual({"demo", "other"}, {i["instance"] for i in body["instances"]})

    def test_one_instance_lists_its_files_with_ready_made_links(self):
        body = self.client.get("/api/content/demo").json()
        self.assertEqual(300, body["bytes"])
        names = {item["name"] for item in body["items"]}
        self.assertEqual({"a.docx", "b.docx"}, names)
        for item in body["items"]:
            self.assertIn("?t=", item["url"])

    def test_a_single_file_can_be_deleted(self):
        self.assertEqual(200, self.client.delete("/api/content/demo/a.docx").status_code)
        self.assertFalse((self.root / "demo" / "a.docx").exists())
        self.assertTrue((self.root / "demo" / "b.docx").exists())

    def test_deleting_a_file_that_is_not_there_is_a_404(self):
        self.assertEqual(404, self.client.delete("/api/content/demo/nope.docx").status_code)

    def test_an_instance_folder_can_be_emptied_without_losing_the_folder(self):
        response = self.client.delete("/api/content/demo")
        self.assertEqual(2, response.json()["removed"])
        self.assertTrue((self.root / "demo").is_dir())
        self.assertEqual([], list((self.root / "demo").iterdir()))

    def test_everything_can_go_at_once(self):
        self.assertEqual(3, self.client.delete("/api/content").json()["removed"])
        self.assertEqual(0, self.client.get("/api/content").json()["total_files"])


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    """What the model actually receives — built by the real build_server()."""

    def setUp(self):
        self.root = isolate(self, content_max_mb=1, content_warn_percent=80)
        self.record = patch("app.mcp_runner.record_call")
        self.record.start()
        self.addCleanup(self.record.stop)
        self.calls = []

    def server(self, enabled=True):
        from app.mcp_runner import build_server
        from app.schema import ContentConfig, MCPConfig, ServerConfig, ToolSourceConfig

        calls = self.calls

        class Tools:
            def make_document(self) -> str:
                calls.append("make_document")
                return ("Copy exactly the text between the lines.\n"
                        "----\n"
                        "[Report](/cache/files/report.docx)\n"
                        "----")

        cfg = MCPConfig(
            id="demo", name="demo",
            server=ServerConfig(port=9999),
            tool_source=ToolSourceConfig(path="tools/demo.json"),
            content=ContentConfig(enabled=enabled),
        )
        defs = [{"name": "make_document", "description": "",
                 "inputSchema": {"type": "object", "properties": {}}}]
        return build_server(cfg, defs, Tools())

    async def call(self, server):
        from mcp import types
        from tests.test_runner_identity import make_context

        handler = server.get_request_handler("tools/call").handler
        result = await handler(
            make_context(None),
            types.CallToolRequestParams(name="make_document", arguments={}),
        )
        return "\n".join(part.text for part in result.content)

    async def test_the_link_in_the_result_points_at_our_download_route(self):
        write_file(self.root, "demo", "report.docx")
        text = await self.call(self.server())
        self.assertIn("/content/demo/report.docx?t=", text)
        self.assertNotIn("/cache/files/report.docx", text)

    async def test_with_the_store_off_the_result_is_passed_through_untouched(self):
        write_file(self.root, "demo", "report.docx")
        text = await self.call(self.server(enabled=False))
        self.assertIn("/cache/files/report.docx", text)

    async def test_the_warning_stands_in_front_of_the_result(self):
        # Behind it, it would be swallowed: the result is an instruction block
        # telling the model to copy everything between the lines.
        write_file(self.root, "demo", "report.docx", size=int(1024 * 1024 * 0.9))
        text = await self.call(self.server())
        self.assertTrue(text.startswith("Note: the file storage"), text[:80])
        self.assertLess(text.index("Note: the file storage"), text.index("Copy exactly"))

    async def test_a_full_folder_only_warns_while_blocking_is_off(self):
        write_file(self.root, "demo", "report.docx", size=int(1024 * 1024 * 1.5))
        text = await self.call(self.server())
        self.assertEqual(["make_document"], self.calls)
        self.assertIn("is full", text)

    async def test_with_blocking_on_the_tool_is_not_even_called(self):
        write_file(self.root, "demo", "report.docx", size=int(1024 * 1024 * 1.5))
        with patch.object(content_store, "block_when_full", return_value=True):
            text = await self.call(self.server())
        self.assertEqual([], self.calls)
        self.assertIn("File storage is full", text)
