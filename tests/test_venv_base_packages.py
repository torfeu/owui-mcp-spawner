"""The base packages every instance venv gets — and why `mcp` carries a floor.

The runner speaks the mcp 2.x low-level `Server` API (handlers as constructor
arguments). Against the 1.x line it does not start at all, so an instance venv
must never resolve `mcp` to 1.x. The other direction is what these tests once
guarded: before v0.2.2 the pin was a ceiling, because the runner still used the
1.x decorators. Whichever way the runner is ported, both places have to move
together — the manager's own dependency and the venvs it builds — and these
tests fail if only one of them does.
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


class McpFloorTests(unittest.TestCase):
    def test_instance_venvs_never_resolve_mcp_1(self):
        req = _mcp_requirement(venv_manager.BASE_PACKAGES)
        self.assertFalse(req.specifier.contains(Version("1.29.1")))
        self.assertTrue(req.specifier.contains(Version("2.1.1")))

    def test_the_manager_itself_carries_the_same_floor(self):
        data = tomllib.loads((BASE_DIR / "pyproject.toml").read_text())
        req = _mcp_requirement(data["project"]["dependencies"])
        self.assertFalse(req.specifier.contains(Version("1.29.1")))
        self.assertTrue(req.specifier.contains(Version("2.1.1")))


if __name__ == "__main__":
    unittest.main()
