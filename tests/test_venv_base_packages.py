"""The base packages every instance venv gets — and why `mcp` carries a ceiling.

`mcp` 2.x rebuilt the low-level `Server` API. `mcp_runner.build_server()` still
registers handlers with the 1.x decorators, so an instance venv that resolves
`mcp` to 2.x dies at startup with "'Server' object has no attribute
'list_tools'". Existing venvs keep the 1.x they installed long ago and show
nothing; every venv created after the 2.0 release falls over. These tests fail
if the ceiling is dropped without porting the runner.
"""
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

import app.venv_manager as venv_manager

BASE_DIR = Path(__file__).parent.parent


def _mcp_requirement(specs: list[str]) -> Requirement:
    for spec in specs:
        req = Requirement(spec)
        if req.name == "mcp":
            return req
    raise AssertionError(f"no 'mcp' requirement among {specs}")


class McpCeilingTests(unittest.TestCase):
    def test_instance_venvs_never_resolve_mcp_2(self):
        req = _mcp_requirement(venv_manager.BASE_PACKAGES)
        self.assertFalse(req.specifier.contains(Version("2.0.0")))
        self.assertTrue(req.specifier.contains(Version("1.29.1")))

    def test_the_manager_itself_carries_the_same_ceiling(self):
        data = tomllib.loads((BASE_DIR / "pyproject.toml").read_text())
        req = _mcp_requirement(data["project"]["dependencies"])
        self.assertFalse(req.specifier.contains(Version("2.0.0")))
        self.assertTrue(req.specifier.contains(Version("1.29.1")))


if __name__ == "__main__":
    unittest.main()
