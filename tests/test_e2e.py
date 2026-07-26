import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = PROJECT_ROOT / "runtime" / "venvs" / "default"

TOOL_CODE = '''"""
title: Integration Echo
description: Temporary end-to-end test tool
author: Test Suite
version: 0.1.0
"""
from pydantic import BaseModel, Field

class Tools:
    class Valves(BaseModel):
        prefix: str = Field(default="ECHO", description="Output prefix")

    def __init__(self):
        self.valves = self.Valves()

    def echo(self, text: str) -> str:
        """Echo the supplied text.

        Args:
            text: Text to echo
        """
        return f"{self.valves.prefix}:{text}"
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def call_mcp(url: str, text: str) -> str:
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            if [tool.name for tool in tools.tools] != ["echo"]:
                raise AssertionError("Unexpected MCP tool list")
            result = await session.call_tool("echo", {"text": text})
            if result.isError:
                raise AssertionError(f"MCP call failed: {result.content}")
            return result.content[0].text


@unittest.skipUnless(
    (DEFAULT_VENV / "bin" / "python").exists()
    and (DEFAULT_VENV / ".mcp-manager-ready").exists(),
    "runtime/venvs/default is required for the isolated integration test",
)
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="owui-mcp-e2e-")
        cls.root = Path(cls.tempdir.name)
        shutil.copytree(PROJECT_ROOT / "app", cls.root / "app")
        shutil.copytree(PROJECT_ROOT / "web", cls.root / "web")
        (cls.root / "configs").mkdir()
        (cls.root / "tools").mkdir()
        (cls.root / "runtime" / "logs").mkdir(parents=True)
        (cls.root / "runtime" / "history").mkdir()
        (cls.root / "runtime" / "venvs").mkdir()
        (cls.root / "runtime" / "venvs" / "default").symlink_to(DEFAULT_VENV)
        (cls.root / "runtime" / ".venv_migrated").write_text("done\n")
        (cls.root / "runtime" / "settings.json").write_text("{}\n")
        (cls.root / "runtime" / "pids.json").write_text("{}\n")

        cls.manager_port = free_port()
        cls.instance_port = free_port()
        cls.shared_port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.manager_port}"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(cls.root)
        cls.manager_log = open(cls.root / "manager-test.log", "w")
        cls.manager = subprocess.Popen(
            [
                sys.executable,
                str(cls.root / "app" / "manager.py"),
                "--host", "127.0.0.1",
                "--port", str(cls.manager_port),
            ],
            cwd=cls.root,
            env=env,
            stdout=cls.manager_log,
            stderr=subprocess.STDOUT,
        )
        cls.client = httpx.Client(base_url=cls.base_url, timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.manager.poll() is not None:
                cls.manager_log.flush()
                log = (cls.root / "manager-test.log").read_text(errors="replace")
                raise RuntimeError(f"Test manager exited during startup:\n{log}")
            try:
                if cls.client.get("/api/auth-status").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for the test manager")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.post("/api/instances/integration_echo/stop")
            cls.client.delete("/api/instances/integration_echo")
            cls.client.put("/api/settings", json={"shared_port": None})
        except Exception:
            pass
        cls.client.close()
        if cls.manager.poll() is None:
            cls.manager.terminate()
            try:
                cls.manager.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.manager.kill()
                cls.manager.wait(timeout=5)
        cls.manager_log.close()
        cls.tempdir.cleanup()

    def assert_response(self, response: httpx.Response, status: int = 200):
        self.assertEqual(status, response.status_code, response.text)
        return response

    def wait_running(self, timeout: float = 20) -> dict:
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            response = self.client.get("/api/instances/integration_echo")
            if response.status_code == 200:
                last = response.json()
                if last.get("status") == "running":
                    return last
            time.sleep(0.2)
        self.fail(f"Instance did not become running: {last}")

    def test_complete_tool_lifecycle(self):
        validation = self.assert_response(
            self.client.post("/api/tools/validate", json={"code": TOOL_CODE})
        ).json()
        self.assertTrue(validation["valid"], validation)

        created = self.assert_response(self.client.post("/api/tools/create", json={
            "id": "integration_echo",
            "name": "Integration Echo",
            "description": "Temporary test instance",
            "category": "Tests",
            "code": TOOL_CODE,
            "port": self.instance_port,
            "venv": "default",
        })).json()
        self.assertEqual("integration_echo", created["id"])

        config = self.assert_response(
            self.client.get("/api/instances/integration_echo/config")
        ).json()
        self.assertEqual("ECHO", config["values"]["prefix"])
        self.assertEqual("Tests", config["category"])

        self.assert_response(self.client.post("/api/instances/integration_echo/start"))
        self.wait_running()
        direct_url = f"http://127.0.0.1:{self.instance_port}/mcp"
        self.assertEqual("ECHO:direct", asyncio.run(call_mcp(direct_url, "direct")))

        self.assert_response(self.client.post("/api/instances/integration_echo/lock"))
        self.assert_response(
            self.client.put("/api/instances/integration_echo", json={"category": "Blocked"}),
            403,
        )
        self.assert_response(self.client.post("/api/instances/integration_echo/restart"), 403)
        self.assert_response(self.client.post("/api/instances/integration_echo/stop"))
        self.assert_response(self.client.post("/api/instances/integration_echo/unlock"))

        self.assert_response(self.client.post("/api/instances/integration_echo/start"))
        self.wait_running()
        settings = self.assert_response(
            self.client.put("/api/settings", json={"shared_port": self.shared_port})
        ).json()
        self.assertIn("shared_port", settings["changed"])
        self.wait_running()
        shared_url = f"http://127.0.0.1:{self.shared_port}/mcp/integration_echo"
        self.assertEqual("ECHO:proxy", asyncio.run(call_mcp(shared_url, "proxy")))

        exported = self.assert_response(
            self.client.get("/api/instances/integration_echo/export")
        ).json()
        self.assertIn(f":{self.shared_port}/mcp/integration_echo", exported[0]["url"])

        self.assert_response(self.client.post("/api/instances/integration_echo/stop"))
        self.assert_response(self.client.delete("/api/instances/integration_echo"))
        self.assert_response(self.client.put("/api/settings", json={"shared_port": None}))
        self.assert_response(self.client.get("/api/instances/integration_echo"), 404)


if __name__ == "__main__":
    unittest.main()
