"""Slowing down password guessing, and not slowing down anything else.

Two groups. The first drives app/lockout.py through a fake clock: three
rejected credentials cost 60 seconds, the next three 120, then 240, and a quiet
quarter of an hour wipes the slate. The second goes through the real API and
checks what is *not* counted — a request without a credential, and a valid
credential that merely lacks the scope for a route.
"""
import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.lockout as lockout
import app.settings_store as settings_store
from app.admin_server import app


def isolate_settings_file(case):
    """Point runtime/settings.json at a temp file for one test.

    `auth.set_agent_token()` does not only set an environment variable — it
    *persists*, through `save_settings`. A test calling it therefore writes the
    live settings file of whatever machine the suite runs on: on the server
    that is the file holding the password hash, the three API tokens and the
    user-JWT secret. It is restored in a `finally`, so the content survives —
    but only as long as nothing goes wrong in between, and in the window the
    live file carries a token that is printed in this source. That is not a
    thing to leave resting on a `finally`.

    Same shape as `isolate_agent_store` and `isolate_usage_db`: env or module
    path to a temp file, cache dropped, put back by `addCleanup`. Found the way
    those two were — by diffing the runtime directory around a suite run, this
    time on the server, where the file exists and locally it did not.
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    target = pathlib.Path(tmp.name) / "settings.json"
    target.write_text(json.dumps(settings_store.load_settings()))
    patcher = patch.object(settings_store, "SETTINGS_FILE", target)
    patcher.start()
    case.addCleanup(patcher.stop)
    settings_store._cache = None
    case.addCleanup(lambda: setattr(settings_store, "_cache", None))


class LockoutCounterTests(unittest.TestCase):
    """The counter itself, on a clock we control."""

    def setUp(self):
        lockout.clear_all()

    def tearDown(self):
        lockout.clear_all()

    def test_first_two_failures_are_free(self):
        self.assertEqual(lockout.record_failure("10.0.0.1", now=0), 0)
        self.assertEqual(lockout.record_failure("10.0.0.1", now=1), 0)
        self.assertEqual(lockout.blocked_for("10.0.0.1", now=2), 0)

    def test_delay_doubles_every_three_failures(self):
        delays = []
        now = 0.0
        for _ in range(9):
            delay = lockout.record_failure("10.0.0.1", now=now)
            if delay:
                delays.append(delay)
                now += delay          # sit out the block, then keep guessing
            now += 1
        self.assertEqual(delays, [60, 120, 240])

    def test_block_expires_on_its_own(self):
        for _ in range(3):
            lockout.record_failure("10.0.0.1", now=0)
        self.assertEqual(lockout.blocked_for("10.0.0.1", now=1), 59)
        self.assertEqual(lockout.blocked_for("10.0.0.1", now=60), 0)

    def test_addresses_are_counted_separately(self):
        for _ in range(3):
            lockout.record_failure("10.0.0.1", now=0)
        self.assertEqual(lockout.blocked_for("10.0.0.2", now=0), 0)

    def test_success_clears_the_record(self):
        for _ in range(2):
            lockout.record_failure("10.0.0.1", now=0)
        lockout.reset("10.0.0.1")
        # The third failure after a reset is a first failure again.
        self.assertEqual(lockout.record_failure("10.0.0.1", now=1), 0)

    def test_quiet_quarter_hour_forgets_the_address(self):
        for _ in range(2):
            lockout.record_failure("10.0.0.1", now=0)
        later = lockout.FORGET_AFTER_SECONDS + 1
        self.assertEqual(lockout.record_failure("10.0.0.1", now=later), 0)
        self.assertEqual(lockout.blocked_for("10.0.0.1", now=later), 0)

    def test_doubling_survives_a_long_run(self):
        """No ceiling on the policy, but no absurd numbers either."""
        now = 0.0
        delay = 0
        for _ in range(200):
            got = lockout.record_failure("10.0.0.1", now=now)
            if got:
                delay = got
                now += got
            now += 1
        self.assertEqual(delay, lockout.FIRST_DELAY_SECONDS * 2 ** lockout._MAX_DOUBLINGS)


class LockoutThroughTheApiTests(unittest.TestCase):
    """What the dependencies do — including what they refuse to count."""

    def setUp(self):
        self.client = TestClient(app)
        isolate_settings_file(self)
        self.original_hash = auth._password_hash
        auth._password_hash = hashlib.sha256(b"right-password").hexdigest()
        lockout.clear_all()

    def tearDown(self):
        auth._password_hash = self.original_hash
        lockout.clear_all()

    def _try(self, password):
        return self.client.get(
            "/api/auth-check", headers={"Authorization": f"Bearer {password}"}
        )

    def test_third_wrong_password_turns_401_into_429(self):
        self.assertEqual(self._try("nope").status_code, 401)
        self.assertEqual(self._try("nope").status_code, 401)
        blocked = self._try("nope")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "60")

    def test_right_password_is_refused_while_blocked(self):
        for _ in range(3):
            self._try("nope")
        # The block is about the address, not about what is being offered.
        self.assertEqual(self._try("right-password").status_code, 429)

    def test_missing_credential_never_counts(self):
        for _ in range(5):
            self.assertEqual(self.client.get("/api/auth-check").status_code, 401)
        # The UI asks before anyone has logged in — that must stay free.
        self.assertEqual(self._try("right-password").status_code, 200)

    def test_success_wipes_earlier_failures(self):
        self._try("nope")
        self._try("nope")
        self.assertEqual(self._try("right-password").status_code, 200)
        self.assertEqual(self._try("nope").status_code, 401)
        self.assertEqual(self._try("nope").status_code, 401)
        # Would be the third strike without the reset in between.
        self.assertEqual(self._try("right-password").status_code, 200)

    def test_valid_token_at_the_wrong_door_does_not_count(self):
        """403 is a configured client in the wrong place, not a guess."""
        original = os.environ.get("MCP_MANAGER_AGENT_TOKEN")
        auth.set_agent_token("agent-token")
        try:
            for _ in range(5):
                response = self.client.put(
                    "/api/settings",
                    json={"read_token": "x"},
                    headers={"Authorization": "Bearer agent-token"},
                )
                self.assertEqual(response.status_code, 403)
            self.assertEqual(self._try("right-password").status_code, 200)
        finally:
            auth.set_agent_token(original)


if __name__ == "__main__":
    unittest.main()
