"""Installing packages — one resolver call, and what "success" is worth.

Requirements used to be installed one pip call at a time. Two of them can
disagree about a shared package: the second call replaces the version the first
one needed, both calls exit 0, and the function reported success over a venv
that now contradicts itself. Nothing said so until an import failed at runtime,
in a log far away from the install that caused it.

They go into one call now, so pip resolves them against each other — a
combination that cannot work is refused before anything is written. And exit
code 0 is not taken as proof on its own: `pip check` runs before and after, and
anything it complains about that was not there before belongs to this install.
Before *and* after, because a venv shared with another instance may already be
inconsistent, and a problem this install did not cause must not make it fail.

Faked down to `subprocess.run`: a real pip run needs wheels, seconds and a
network policy, and what is pinned here is the shape of the calls and what is
made of their results. The conflicting-wheels case was checked against real pip
by hand — pip's resolver refuses it outright, which is the better outcome.
"""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import app.dependency_manager as dm


class Recorder:
    """Stands in for subprocess.run: answers by command, remembers the calls."""

    def __init__(self, check_before="", check_after="", install_rc=0, install_err=""):
        self.calls = []
        self.check_before = check_before
        self.check_after = check_after
        self.install_rc = install_rc
        self.install_err = install_err
        self._checked = 0

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[-1] == "check":
            self._checked += 1
            out = self.check_before if self._checked == 1 else self.check_after
            return type("R", (), {"returncode": 1 if out else 0,
                                  "stdout": out, "stderr": ""})()
        return type("R", (), {"returncode": self.install_rc, "stdout": "",
                              "stderr": self.install_err})()

    @property
    def installs(self):
        return [c for c in self.calls if "install" in c]


class DependencyTestCase(unittest.TestCase):
    """Every path install_dependencies writes to, pointed somewhere harmless."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        log = pathlib.Path(self.tmp.name) / "install.log"
        # The note about unfinished business is a real file under runtime/;
        # this suite runs on the server, where that belongs to a live venv.
        self.conflicts = pathlib.Path(self.tmp.name) / "venv-conflicts"
        for target in (
            patch.object(dm, "ensure_venv", lambda venv, log=None: (True, "")),
            patch.object(dm, "python_path", lambda venv: pathlib.Path("/nowhere/python")),
            patch.object(dm, "get_install_log_path", lambda instance_id: log),
            patch.object(dm, "CONFLICTS_DIR", self.conflicts),
        ):
            target.start()
            self.addCleanup(target.stop)

    def install(self, recorder, deps=("alpha==1.0", "beta==1.0"), upgrade=False):
        with patch.object(dm.subprocess, "run", recorder):
            return dm.install_dependencies("demo", list(deps), upgrade, "default")


class InstallTests(DependencyTestCase):
    def test_every_requirement_goes_into_one_pip_call(self):
        """Separately, a later package can replace a version an earlier one
        needs — and pip has no way to know, because it is never told about
        both at once."""
        recorder = Recorder()
        ok, err = self.install(recorder)
        self.assertTrue(ok, err)
        self.assertEqual(1, len(recorder.installs))
        self.assertEqual(["alpha==1.0", "beta==1.0"], recorder.installs[0][-2:])

    def test_upgrade_still_reaches_pip(self):
        recorder = Recorder()
        self.install(recorder, upgrade=True)
        self.assertIn("--upgrade", recorder.installs[0])

    def test_a_conflict_this_install_introduced_is_not_reported_as_success(self):
        recorder = Recorder(check_after="alpha 1.0 has requirement shared==1.0, "
                                        "but you have shared 2.0.")
        ok, err = self.install(recorder)
        self.assertFalse(ok)
        self.assertIn("inconsistent", err)
        self.assertIn("shared 2.0", err)

    def test_a_conflict_that_was_already_there_is_not_blamed_on_this_install(self):
        """A shared venv can be inconsistent before anyone touches it. Failing
        every later install over that would make the venv unusable for a
        problem this instance did not cause."""
        old = "gamma 1.0 has requirement other==1.0, but you have other 2.0."
        recorder = Recorder(check_before=old, check_after=old)
        ok, err = self.install(recorder)
        self.assertTrue(ok, err)

    def test_a_check_that_cannot_run_does_not_fail_the_install(self):
        def explode(cmd, **kwargs):
            if cmd[-1] == "check":
                raise OSError("no pip check here")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(dm.subprocess, "run", explode):
            ok, err = dm.install_dependencies("demo", ["alpha==1.0"], False, "default")
        self.assertTrue(ok, err)

    def test_a_failed_install_names_what_it_tried(self):
        recorder = Recorder(install_rc=1, install_err="ResolutionImpossible")
        ok, err = self.install(recorder)
        self.assertFalse(ok)
        self.assertIn("alpha==1.0", err)
        self.assertIn("beta==1.0", err)
        self.assertIn("ResolutionImpossible", err)

    def test_nothing_to_install_asks_pip_nothing(self):
        recorder = Recorder()
        with patch.object(dm.subprocess, "run", recorder):
            ok, err = dm.install_dependencies("demo", [], False, "default")
        self.assertTrue(ok, err)
        self.assertEqual([], recorder.calls)

    def test_an_unsafe_spec_is_still_refused_before_pip_runs(self):
        recorder = Recorder()
        ok, err = self.install(recorder, deps=["httpx; rm -rf /"])
        self.assertFalse(ok)
        self.assertIn("Invalid/unsafe", err)
        self.assertEqual([], recorder.calls)


class UnresolvedConflictTests(DependencyTestCase):
    """A repeat of a failed install must not inherit its own damage.

    An install that ends inconsistent leaves the packages where they are. On
    the next attempt `pip check` says the same thing it said before, the
    "already there, not ours" exemption applies — and the very same call, run
    twice, reported success over a venv that was still broken. Confirmed
    against real pip: (True, "") while `pip check` still exits 1.
    """

    BROKEN = "alpha 1.0 has requirement shared==1.0, but you have shared 2.0."

    def test_repeating_a_failed_install_fails_again(self):
        first = Recorder(check_after=self.BROKEN)
        ok, _err = self.install(first)
        self.assertFalse(ok)

        # Same call, same broken environment — now it reads as pre-existing.
        again = Recorder(check_before=self.BROKEN, check_after=self.BROKEN)
        ok, err = self.install(again)
        self.assertFalse(ok, "a repeat laundered the failure into success")
        self.assertIn("shared 2.0", err)

    def test_a_conflict_that_got_resolved_stops_counting(self):
        self.install(Recorder(check_after=self.BROKEN))
        ok, err = self.install(Recorder(check_before="", check_after=""))
        self.assertTrue(ok, err)
        # And the note is gone, so a genuinely foreign conflict later on still
        # gets its exemption.
        ok, err = self.install(Recorder(check_before=self.BROKEN, check_after=self.BROKEN))
        self.assertTrue(ok, err)

    def test_a_foreign_conflict_is_not_adopted_when_our_own_one_appears(self):
        """The whole sequence, not the two kinds separately: a foreign conflict
        is standing, this install causes its own on top, and later its own is
        resolved. The foreign one must be exactly as exempt at the end as it
        was at the start — written down with ours, it would block every install
        in this venv from then on."""
        foreign = "gamma 1.0 has requirement other==1.0, but you have other 2.0."
        ok, _ = self.install(Recorder(check_before=foreign,
                                      check_after=f"{foreign}\n{self.BROKEN}"))
        self.assertFalse(ok)
        self.assertEqual([self.BROKEN],
                         json.loads((self.conflicts / "default.json").read_text()))

        # Ours is fixed, the foreign one is not. That must be a success.
        ok, err = self.install(Recorder(check_before=foreign, check_after=foreign))
        self.assertTrue(ok, err)
        self.assertFalse((self.conflicts / "default.json").exists())

    def test_a_foreign_conflict_is_still_not_blamed_on_this_install(self):
        foreign = "gamma 1.0 has requirement other==1.0, but you have other 2.0."
        ok, err = self.install(Recorder(check_before=foreign, check_after=foreign))
        self.assertTrue(ok, err)
        self.assertFalse((self.conflicts / "default.json").exists())
