"""appletv: a lift-to-pause (lego's appletv-pause thread) and a launch (the
pad thread) in flight at the same time must not close each other's pyatv
session. Each is its own asyncio.run() over the module's connection cache,
and connect()'s stale-loop check cannot tell a finished loop from the other
thread's live one (life-ops #116). The module is loaded on its own so the
jukebox (app/__init__.py) never starts."""
import asyncio
import importlib.util
import os
import threading
import time
import types
import unittest
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), '..', 'app', 'appletv.py')
_spec = importlib.util.spec_from_file_location('musicfig_appletv_race', _PATH)
appletv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(appletv)

COMMAND_TIME = 0.25  # seconds a fake command stays "on the wire"


class FakeSession:
    """One pyatv connection. A command takes a while, and fails the way tvOS
    does ("Open URL failed") if the session was closed underneath it."""

    def __init__(self, log):
        self.log = log
        self.closed = False
        self.in_flight = 0
        self.closed_while_busy = False
        self.power = self
        self.apps = self
        self.remote_control = self

    @property
    def power_state(self):
        return "PowerState.On"

    async def _command(self, name):
        self.in_flight += 1
        self.log.append((name, "start", time.monotonic()))
        try:
            await asyncio.sleep(COMMAND_TIME)
            if self.closed:
                raise RuntimeError("Open URL failed")
        finally:
            self.in_flight -= 1
            self.log.append((name, "end", time.monotonic()))

    async def launch_app(self, url):
        await self._command("launch")

    async def play_pause(self):
        await self._command("play_pause")

    async def turn_on(self):
        await self._command("turn_on")

    def close(self):
        if self.in_flight:
            self.closed_while_busy = True
        self.closed = True
        return None


class FakeDevice:
    name = "Game Room"
    address = "192.168.1.50"
    all_identifiers = ()

    def set_credentials(self, *a):
        pass


class ConcurrentActionsTests(unittest.TestCase):
    """Real connect(): a scan that answers at once and a pyatv.connect that
    hands out a fresh FakeSession per call, so the cache / stale-loop logic
    under test is the module's own."""

    def setUp(self):
        self.sessions = []
        self.log = []

        async def scan(timeout=5):
            return [FakeDevice()]

        async def pyatv_connect(target, loop):
            s = FakeSession(self.log)
            self.sessions.append(s)
            return s
        self._patches = [
            mock.patch.object(appletv, "scan_for_apple_tvs", scan),
            mock.patch.object(appletv, "pyatv", types.SimpleNamespace(connect=pyatv_connect)),
            mock.patch.object(appletv, "COMPANION_CREDENTIALS", None),
            mock.patch.object(appletv, "AIRPLAY_CREDENTIALS", None),
        ]
        for p in self._patches:
            p.start()
        appletv._atv = appletv._atv_name = appletv._atv_loop = None
        # app/__init__.py (imported by the other test files) runs dictConfig,
        # which disables every logger that already exists - this module's
        # included, depending on which test file was loaded first.
        appletv.logger.disabled = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        appletv._atv = appletv._atv_name = appletv._atv_loop = None

    def run_together(self, *actions):
        """Start each action on its own thread at the same instant (barrier),
        wait for all of them, return their results in order."""
        barrier = threading.Barrier(len(actions))
        results = [None] * len(actions)

        def runner(i, action):
            barrier.wait(timeout=5)
            results[i] = action()
        threads = [threading.Thread(target=runner, args=(i, a), name=f"action-{i}", daemon=True)
                   for i, a in enumerate(actions)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), "an action never finished: a launch or pause was lost")
        return results

    def test_pause_and_launch_at_the_same_time_both_succeed(self):
        paused, launched = self.run_together(
            lambda: appletv.pause(),
            lambda: appletv.launch_url("https://tv.apple.com/us/movie/x"))
        self.assertTrue(paused)
        self.assertTrue(launched)
        # Neither action's session was closed while a command of its own was
        # on the wire (on master the second thread's connect() did exactly that).
        self.assertFalse(any(s.closed_while_busy for s in self.sessions))
        self.assertEqual(len(self.sessions), 2)  # one session per action, no retry connects
        names = sorted(n for n, phase, _ in self.log if phase == "start")
        self.assertEqual(names, ["launch", "play_pause"])
        # The two commands never overlapped: the launch waited for the pause
        # press (or the other way round), it did not run alongside it.
        spans = sorted((t, phase) for _n, phase, t in self.log)
        self.assertEqual([phase for _t, phase in spans], ["start", "end", "start", "end"])

    def test_launch_and_power_check_at_the_same_time(self):
        """The placement's is_on_sync() (appleTvAwake) and a lift's pause are
        the other pair that can meet; a power check must answer, not False."""
        awake, paused = self.run_together(
            lambda: appletv.is_on_sync(),
            lambda: appletv.pause())
        self.assertTrue(awake)
        self.assertTrue(paused)
        self.assertFalse(any(s.closed_while_busy for s in self.sessions))

    def test_lock_is_released_after_a_failed_action(self):
        """A pause that raised must not leave every later launch waiting."""
        async def boom(name=None, ip=None):
            raise OSError("network is unreachable")
        with mock.patch.object(appletv, "connect", boom):
            self.assertFalse(appletv.pause())
            self.assertFalse(appletv.launch_url("https://tv.apple.com/us/movie/x"))
            self.assertFalse(appletv.is_on_sync())
        self.assertFalse(appletv._action_lock.locked())
        self.assertTrue(appletv.launch_url("https://tv.apple.com/us/movie/x"))  # the lock was free

    def test_every_sync_entry_point_holds_the_lock_while_connected(self):
        seen = {}

        async def fake_connect(name=None, ip=None):
            seen[threading.current_thread().name] = appletv._action_lock.locked()
            return FakeSession(self.log)
        with mock.patch.object(appletv, "connect", fake_connect), \
                mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()):
            for label, action in (
                    ("pause", lambda: appletv.pause()),
                    ("launch_url", lambda: appletv.launch_url("x://y")),
                    ("launch_youtube", lambda: appletv.launch_youtube("MmB9b5njVbA")),
                    ("is_on_sync", lambda: appletv.is_on_sync()),
                    ("turn_on", lambda: appletv.turn_on()),
                    ("launch_disney", lambda: appletv.launch_disney("abc")),
                    ("launch_netflix", lambda: appletv.launch_netflix("123"))):
                seen.clear()
                self.assertTrue(action(), label)
                self.assertEqual(seen, {threading.current_thread().name: True}, label)
        self.assertFalse(appletv._action_lock.locked())

    def test_launch_waits_for_a_pause_in_flight_and_says_so(self):
        """A launch that finds the pause thread mid-action logs it, waits as
        long as that pause still takes, and then runs: it is never dropped."""
        results = []
        appletv._action_lock.acquire()  # stands in for a pause still on the wire
        try:
            with self.assertLogs(appletv.logger, level="INFO") as cm:
                t = threading.Thread(target=lambda: results.append(appletv.launch_url("https://tv.apple.com/us/movie/x")),
                                     name="pad", daemon=True)
                t.start()
                # a loaded Pi can take a while to start the thread: poll for the
                # log line instead of trusting a fixed wait
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not any("busy with another action" in r.getMessage() for r in cm.records):
                    time.sleep(0.02)
                self.assertTrue(t.is_alive())  # waiting on the lock, not skipped
                self.assertEqual(results, [])
            self.assertTrue(any("busy with another action; pad waits" in m for m in cm.output))
        finally:
            appletv._action_lock.release()
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(results, [True])

    def test_launch_gives_up_after_the_wait_limit(self):
        """A pause that never returns (TCP connect with no pyatv timeout) must
        not hold the pad loop forever: the launch fails loudly instead."""
        appletv._action_lock.acquire()
        try:
            with unittest.mock.patch.object(appletv, "ACTION_WAIT_S", 0.2), \
                    self.assertLogs(appletv.logger, level="ERROR") as cm:
                self.assertFalse(appletv.launch_url("https://tv.apple.com/us/movie/x"))
            self.assertTrue(any("still busy after 0.2s" in m for m in cm.output))
        finally:
            appletv._action_lock.release()
        self.assertFalse(appletv._action_lock.locked())


if __name__ == "__main__":
    unittest.main()
