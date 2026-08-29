"""Listing the venv pool, with the leftovers a real installation collects.

An interpreter migration parks the old venv next to the new one under a name
like `default.py312-migrated-20260816-001110`. That name can never come from
this module, and asking venv_dir() about it raises — which used to happen
inside the listing itself and took /api/venvs down with it, on a server where
the only visible symptom was a settings dialog that quietly showed one venv.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app.venv_manager as venv_manager


class VenvListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = patch.object(venv_manager, "VENVS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _make_venv(self, name: str) -> None:
        """A directory that looks like a venv to the listing."""
        bin_dir = self.root / name / ("Scripts" if venv_manager.os.name == "nt" else "bin")
        bin_dir.mkdir(parents=True)
        (bin_dir / ("python.exe" if venv_manager.os.name == "nt" else "python")).touch()

    def test_lists_the_venvs_it_finds(self):
        self._make_venv("default")
        self._make_venv("docs-tool")
        self.assertEqual(venv_manager.list_venvs(), ["default", "docs-tool"])

    def test_a_migration_backup_does_not_take_the_list_down(self):
        self._make_venv("default")
        self._make_venv("default.py312-migrated-20260816-001110")
        self.assertEqual(venv_manager.list_venvs(), ["default"])

    def test_other_impossible_names_are_skipped_too(self):
        self._make_venv("default")
        for name in ("with space", "dot.ted", "sla$h"):
            self._make_venv(name)
        self.assertEqual(venv_manager.list_venvs(), ["default"])

    def test_a_directory_without_an_interpreter_is_not_a_venv(self):
        (self.root / "half-built").mkdir()
        self.assertEqual(venv_manager.list_venvs(), [])

    def test_missing_pool_directory_is_empty_not_an_error(self):
        with patch.object(venv_manager, "VENVS_DIR", self.root / "nope"):
            self.assertEqual(venv_manager.list_venvs(), [])

    def test_the_name_rule_itself_still_refuses_the_backup(self):
        """Skipping it in the listing must not soften venv_dir()."""
        with self.assertRaises(ValueError):
            venv_manager.venv_dir("default.py312-migrated-20260816-001110")


if __name__ == "__main__":
    unittest.main()
