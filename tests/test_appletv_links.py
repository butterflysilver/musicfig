"""appletv.youtube_deep_link: every link style in tags.yml becomes the
youtube:// form that tvOS opens (plain https links are refused by tvOS)."""
import asyncio
import threading
import unittest
from unittest import mock

import app.appletv as appletv
from app.appletv import youtube_deep_link


class FakeAtv:
    """Enough of a pyatv device for launch_sequence_async: records the order of
    power / launch / remote calls; wakes on the second power poll."""

    def __init__(self):
        self.calls = []
        self._state = "PowerState.On"
        self.never_wakes = False
        self.fail_launches = 0
        self.power = self
        self.apps = self
        self.remote_control = self

    @property
    def power_state(self):
        self.calls.append("poll")
        if self.calls.count("poll") >= 2 and not self.never_wakes:
            self._state = "PowerState.On"
        return self._state

    async def turn_off(self):
        self.calls.append("off")
        self._state = "PowerState.Off"

    async def turn_on(self):
        self.calls.append("on")

    async def launch_app(self, url):
        self.calls.append(("launch", url))
        if self.fail_launches > 0:
            self.fail_launches -= 1
            raise RuntimeError("Open URL failed")

    async def select(self):
        self.calls.append("select")

    async def play_pause(self):
        self.calls.append("play_pause")

    def close(self):
        self.calls.append("close")


class LaunchSequenceTests(unittest.TestCase):
    def run_seq(self, fake, **kw):
        """connect() always hands back the same fake; a reconnect is visible
        as a "close" in the call list (disconnect closes the cached device)."""
        async def fake_connect(name=None, ip=None):
            appletv._atv = fake
            appletv._atv_loop = asyncio.get_running_loop()
            return fake
        appletv._atv = None
        with mock.patch.object(appletv, "connect", fake_connect), mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()):
            try:
                return asyncio.run(appletv.launch_sequence_async("youtube://x", **kw))
            finally:
                appletv._atv = None
                appletv._atv_loop = None

    def test_plain_launch(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake))
        self.assertEqual(fake.calls, [("launch", "youtube://x")])

    def test_cec_wake_reconnects_around_the_sleep_then_launches_then_selects(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, cec_wake=True, ok_delay=4))
        # off -> fresh session -> on -> poll ... -> fresh session -> launch -> select
        self.assertEqual(fake.calls[:3], ["off", "close", "on"])
        self.assertIn(("launch", "youtube://x"), fake.calls)
        launch_at = fake.calls.index(("launch", "youtube://x"))
        self.assertEqual(fake.calls[launch_at - 1], "close")  # launched on a reconnected session
        self.assertEqual(fake.calls[-1], "select")

    def test_cec_wake_timeout_retries_turn_on_then_launches_on_a_fresh_session(self):
        fake = FakeAtv()
        fake.never_wakes = True
        self.assertTrue(self.run_seq(fake, cec_wake=True))
        self.assertEqual(fake.calls.count("on"), 2)  # second turn_on after the short first window
        self.assertEqual(fake.calls.count("poll"), 8 + 17)
        self.assertEqual(fake.calls[-2:], ["close", ("launch", "youtube://x")])

    def test_launch_retry_wakes_the_box_first(self):
        fake = FakeAtv()
        fake.fail_launches = 1
        self.assertTrue(self.run_seq(fake))
        launches = [i for i, c in enumerate(fake.calls) if c == ("launch", "youtube://x")]
        self.assertEqual(len(launches), 2)
        self.assertIn("on", fake.calls[launches[0]:launches[1]])  # turn_on between the attempts

    def test_failed_wake_still_launches(self):
        fake = FakeAtv()

        async def boom():
            raise RuntimeError("no power support")
        fake.turn_off = boom
        self.assertTrue(self.run_seq(fake, cec_wake=True))
        self.assertIn(("launch", "youtube://x"), fake.calls)

    def test_launch_retries_once_on_a_fresh_session(self):
        fake = FakeAtv()
        fake.fail_launches = 1
        self.assertTrue(self.run_seq(fake, ok_delay=4))
        launches = [c for c in fake.calls if c == ("launch", "youtube://x")]
        self.assertEqual(len(launches), 2)
        self.assertEqual(fake.calls[1], "close")  # reconnect between the two attempts
        self.assertEqual(fake.calls[-1], "select")

    def test_play_pause_as_the_ok_key(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, ok_delay=2, ok_key="play_pause"))
        self.assertEqual(fake.calls[-1], "play_pause")

    def test_key_sequence_select_then_play_pause(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, ok_delay=3, ok_key="select, play_pause"))
        self.assertEqual(fake.calls[-2:], ["select", "play_pause"])

    def test_parse_ok_delays(self):
        self.assertEqual(appletv.parse_ok_delays(7, 2), [7.0, 7.0])
        self.assertEqual(appletv.parse_ok_delays("12, 5", 3), [12.0, 5.0, 5.0])
        self.assertEqual(appletv.parse_ok_delays([3], 2), [3.0, 3.0])
        self.assertEqual(appletv.parse_ok_delays("four", 1), [0.0])
        self.assertEqual(appletv.parse_ok_delays(None, 1), [0.0])

    def test_per_key_delays_are_applied_in_order(self):
        fake = FakeAtv()
        sleeps = []
        here = threading.current_thread()

        async def rec(n):
            if threading.current_thread() is here:  # not a background scan's sleep (life-ops #123)
                sleeps.append(n)
        with mock.patch.object(appletv.asyncio, "sleep", rec):
            async def fake_connect(name=None, ip=None):
                return fake
            with mock.patch.object(appletv, "connect", fake_connect):
                self.assertTrue(asyncio.run(appletv.launch_sequence_async("x://y", ok_delay="12, 5", ok_key="select, play_pause")))
        self.assertEqual(sleeps[-2:], [12.0, 5.0])
        self.assertEqual(fake.calls[-2:], ["select", "play_pause"])

    def test_parse_ok_keys(self):
        self.assertEqual(appletv.parse_ok_keys("select, play_pause"), ["select", "play_pause"])
        self.assertEqual(appletv.parse_ok_keys(["play"]), ["play"])
        self.assertEqual(appletv.parse_ok_keys(""), ["select"])
        self.assertEqual(appletv.parse_ok_keys(None), ["select"])

    def test_unknown_ok_key_falls_back_to_select(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, ok_delay=2, ok_key="menu"))
        self.assertEqual(fake.calls[-1], "select")

    def test_bad_ok_delay_is_ignored(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, ok_delay="four"))
        self.assertEqual(fake.calls, [("launch", "youtube://x")])

    def test_launch_failure_twice_is_false(self):
        fake = FakeAtv()
        fake.fail_launches = 99
        self.assertFalse(self.run_seq(fake, ok_delay=4))
        self.assertNotIn("select", fake.calls)
        self.assertEqual(len([c for c in fake.calls if c == ("launch", "youtube://x")]), 2)

    def test_no_apple_tv(self):
        async def none_connect(name=None, ip=None):
            return None
        with mock.patch.object(appletv, "connect", none_connect):
            self.assertFalse(asyncio.run(appletv.launch_sequence_async("youtube://x")))


class ConnectionCacheTests(unittest.TestCase):
    def test_connection_from_a_finished_loop_is_dropped(self):
        """Each tag action is its own asyncio.run(); a device cached by the
        previous run must not be reused (its commands fail with Open URL failed)."""
        fake = FakeAtv()
        appletv._atv, appletv._atv_name = fake, "Game Room"
        appletv._atv_loop = object()  # some other, finished loop

        async def no_scan(timeout=5):
            return []
        with mock.patch.object(appletv, "scan_for_apple_tvs", no_scan):
            got = asyncio.run(appletv.connect())
        self.assertIsNone(got)  # had to rescan (and found nothing) instead of reusing the stale device
        self.assertIn("close", fake.calls)
        appletv._atv = appletv._atv_loop = None

    def test_close_tasks_from_a_dead_transport_are_awaited_and_swallowed(self):
        """pyatv's close() hands back the disconnect tasks it scheduled; on a
        connection from a finished loop they raise "Event loop is closed".
        Un-awaited they were reported as ERROR at every asyncio.run() shutdown
        (one per lift-to-pause on the living-room Pi)."""
        class ClosingAtv(FakeAtv):
            task = None

            def close(self):
                self.calls.append("close")

                async def dead_disconnect():
                    # pyatv's _sessionStop: a send over the old transport
                    # after a yield, then "Event loop is closed".
                    try:
                        await asyncio.sleep(0)
                    finally:
                        raise RuntimeError("Event loop is closed")
                self.task = asyncio.create_task(dead_disconnect())
                return {self.task}

        fake = ClosingAtv()
        appletv._atv, appletv._atv_name = fake, "Living room"
        appletv._atv_loop = object()

        async def no_scan(timeout=5):
            return []

        async def run():
            with mock.patch.object(appletv, "scan_for_apple_tvs", no_scan):
                got = await appletv.connect()
            # The discriminating check: on master the task was still pending
            # here and asyncio.run()'s shutdown reported its exception.
            self.assertTrue(fake.task.done())
            self.assertIsInstance(fake.task.exception(), RuntimeError)
            return got
        self.assertIsNone(asyncio.run(run()))
        self.assertIn("close", fake.calls)
        appletv._atv = appletv._atv_loop = None

    def test_stale_loop_close_does_not_wait_the_full_timeout(self):
        """life-ops #125: a close task that hangs on the dead transport held
        every lift/launch for the full 3 s; the stale path now drops it after
        STALE_CLOSE_TIMEOUT."""
        class HangingAtv(FakeAtv):
            task = None

            def close(self):
                self.calls.append("close")
                self.task = asyncio.create_task(asyncio.sleep(60))  # a Companion round trip that never answers
                return {self.task}

        fake = HangingAtv()
        appletv._atv, appletv._atv_name = fake, "Living room"
        appletv._atv_loop = object()

        async def no_scan(timeout=5):
            return []

        async def run():
            t0 = asyncio.get_running_loop().time()
            with mock.patch.object(appletv, "scan_for_apple_tvs", no_scan):
                await appletv.connect()
            return asyncio.get_running_loop().time() - t0
        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 1.5)
        self.assertTrue(fake.task.done())  # cancelled, not left for shutdown to complain about
        appletv._atv = appletv._atv_loop = None

    def test_close_quietly_edge_paths(self):
        async def run():
            class Raises:
                def close(self):
                    raise RuntimeError("no running event loop")
            await appletv._close_quietly(Raises())  # must not raise

            class NoTasks:
                def close(self):
                    return None
            await appletv._close_quietly(NoTasks())

            class NeverFinishes:
                def close(self):
                    self.task = asyncio.create_task(asyncio.sleep(60))
                    return {self.task}
            nf = NeverFinishes()
            with self.assertLogs(appletv.logger, level="INFO") as cm:
                t0 = asyncio.get_running_loop().time()
                await appletv._close_quietly(nf, timeout=0.2)
                elapsed = asyncio.get_running_loop().time() - t0
            self.assertLess(elapsed, 2)
            self.assertTrue(nf.task.done())  # cancelled by wait_for, not left behind
            self.assertTrue(any("did not finish within 0.2s" in m for m in cm.output))
        asyncio.run(run())

    def test_disconnect_tolerates_close_without_tasks(self):
        fake = FakeAtv()  # close() returns None, like the other fakes
        appletv._atv, appletv._atv_name = fake, "Game Room"

        async def run():
            appletv._atv_loop = asyncio.get_running_loop()
            await appletv.disconnect()
        asyncio.run(run())
        self.assertIsNone(appletv._atv)
        self.assertEqual(fake.calls, ["close"])

    def test_is_on_reads_the_power_state_property(self):
        """power_state is a pyatv property; awaiting it raised TypeError and
        is_on() answered False for an awake box (M2 depends on this)."""
        class Awake(FakeAtv):
            @property
            def power_state(self):
                return "PowerState.On"

        class Asleep(FakeAtv):
            @property
            def power_state(self):
                return "PowerState.Off"

        for fake, want in ((Awake(), True), (Asleep(), False)):
            async def fake_connect(name=None, ip=None, fake=fake):
                return fake
            with mock.patch.object(appletv, "connect", fake_connect):
                self.assertIs(appletv.is_on_sync(), want)

    def test_is_on_sync_is_false_when_unreachable_and_never_raises(self):
        async def boom(name=None, ip=None):
            raise OSError("network is unreachable")
        with mock.patch.object(appletv, "connect", boom):
            self.assertIs(appletv.is_on_sync(), False)

    def test_pause_presses_play_pause_once(self):
        fake = FakeAtv()

        async def fake_connect(name=None, ip=None):
            return fake
        with mock.patch.object(appletv, "connect", fake_connect), mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()):
            self.assertTrue(appletv.pause())
        self.assertEqual(fake.calls, ["play_pause"])

    def test_pause_without_apple_tv_is_false(self):
        async def none_connect(name=None, ip=None):
            return None
        with mock.patch.object(appletv, "connect", none_connect):
            self.assertFalse(appletv.pause())

    def test_bad_appletv_ip_does_not_raise_and_is_ignored(self):
        appletv.reset_config()
        appletv.load_config({"appletv_ip": "Game-Room.local"})
        self.assertIsNone(appletv.APPLE_TV_IP)
        appletv.reset_config()
        appletv.load_config({"appletv_ip": "192.168.1.210"})
        self.assertEqual(appletv.APPLE_TV_IP, "192.168.1.210")
        appletv.reset_config()
        appletv.APPLE_TV_IP = None

    def test_launch_url_swallows_scan_errors(self):
        async def boom(name=None, ip=None):
            raise OSError("network is unreachable")
        with mock.patch.object(appletv, "connect", boom):
            self.assertFalse(appletv.launch_url("https://tv.apple.com/us/movie/x"))

    def test_launch_url_rejects_empty(self):
        self.assertFalse(appletv.launch_url("   "))


class DeeplinkAppTests(unittest.TestCase):
    """deeplink: tag -> the Deeplink app's web server, pyatv when it is down."""

    def setUp(self):
        self._server = appletv.DEEPLINK_SERVER
        appletv.DEEPLINK_SERVER = "http://192.168.1.50"

    def tearDown(self):
        appletv.DEEPLINK_SERVER = self._server

    def test_apple_tv_app_movie_link_is_sent_in_the_authors_working_form(self):
        # Jennifer's 2024 exchange with the app's author: the full https link works, raw.
        link = "https://tv.apple.com/us/movie/a-charlie-brown-thanksgiving/umc.cmc.7grdgbjdq3tiy37f4dc3yeyms"
        self.assertEqual(appletv.deeplink_open_url(link, server="http://Living-Room.local"),
                         "http://Living-Room.local/open?target=" + link)

    def test_query_characters_inside_the_target_are_escaped(self):
        self.assertEqual(appletv.deeplink_open_url("plutotv://live-tv/star-trek-1?x=1&y=2"),
                         "http://192.168.1.50/open?target=plutotv://live-tv/star-trek-1%3Fx%3D1%26y%3D2")

    def test_server_ok_means_no_pyatv(self):
        seen = []
        with mock.patch.object(appletv, "_deeplink_get", lambda url, timeout: (seen.append(url), (200, b"ok"))[1]), \
             mock.patch.object(appletv, "launch_sequence_async", side_effect=AssertionError("pyatv used")):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x"))
        self.assertEqual(seen, ["http://192.168.1.50/open?target=https://tv.apple.com/us/movie/x"])

    def test_cold_start_wakes_the_box_before_asking_the_server(self):
        """L3 from the #69 review: a Deeplink-server success never switched
        the TV input. With cec_wake the sleep/wake now runs first, and the
        pyatv fallback must not sleep the box a second time."""
        fake = FakeAtv()
        order = []

        async def fake_connect(name=None, ip=None):
            return fake

        async def fake_cec(atv, timeout=25, name=None, ip=None):
            order.append("cec")
            return True, atv

        import urllib.error

        def get(url, timeout):
            order.append("server")
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        async def fallback(url, cec_wake=False, **kw):
            order.append(("pyatv", cec_wake))
            return True
        with mock.patch.object(appletv, "connect", fake_connect), \
             mock.patch.object(appletv, "_cec_wake", fake_cec), \
             mock.patch.object(appletv, "_deeplink_get", get), \
             mock.patch.object(appletv, "DEEPLINK_APP_ID", None), \
             mock.patch.object(appletv, "launch_sequence_async", fallback):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x", cec_wake=True))
        self.assertEqual(order, ["cec", "server", ("pyatv", False)])

    def test_failed_pre_wake_still_sends_turn_on_before_the_server(self):
        """Our own turn_off may have slept the box; a server request on a
        sleeping box times out and would be read as "opened"."""
        order = []

        async def no_wake(atv_name=None, atv_ip=None):
            order.append("cec-failed")
            return False

        async def turn_on(atv_name=None, atv_ip=None):
            order.append("turn_on")
            return True
        with mock.patch.object(appletv, "cec_wake_async", no_wake),              mock.patch.object(appletv, "turn_on_async", turn_on),              mock.patch.object(appletv.time, "sleep", lambda s: order.append(("sleep", s))),              mock.patch.object(appletv, "_deeplink_get", lambda url, timeout: (order.append("server"), (200, b"ok"))[1]):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x", cec_wake=True))
        self.assertEqual(order, ["cec-failed", "turn_on", ("sleep", 3), "server"])

    def test_warm_deeplink_does_not_touch_power(self):
        async def no_cec(*a, **kw):
            raise AssertionError("sleep/wake on a warm launch")
        with mock.patch.object(appletv, "_cec_wake", no_cec), \
             mock.patch.object(appletv, "_deeplink_get", lambda url, timeout: (200, b"ok")):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x", cec_wake=False))

    def test_server_ok_then_play_pause_press_over_pyatv(self):
        fake = FakeAtv()

        async def fake_connect(name=None, ip=None):
            return fake
        with mock.patch.object(appletv, "_deeplink_get", lambda url, timeout: (200, b"ok")), \
             mock.patch.object(appletv, "connect", fake_connect), \
             mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x", ok_delay=3, ok_key="play_pause"))
        self.assertEqual(fake.calls, ["play_pause"])

    def test_server_down_foregrounds_the_app_then_succeeds(self):
        import urllib.error
        fake = FakeAtv()
        tries = []

        def flaky(url, timeout):
            tries.append(url)
            if len(tries) == 1:
                raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
            return 200, b"ok"

        async def fake_connect(name=None, ip=None):
            return fake
        with mock.patch.object(appletv, "_deeplink_get", flaky), mock.patch.object(appletv, "connect", fake_connect), \
             mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()), \
             mock.patch.object(appletv, "launch_sequence_async", side_effect=AssertionError("pyatv fallback used")):
            self.assertTrue(appletv.launch_deeplink("plutotv://live-tv/star-trek-1"))
        self.assertEqual(len(tries), 2)
        self.assertEqual(fake.calls, [("launch", "com.celerity.DeepLink")])

    def test_server_down_even_after_foregrounding_falls_back_to_pyatv(self):
        import urllib.error

        def down(url, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        fake = FakeAtv()
        launched = []

        async def fake_connect(name=None, ip=None):
            return fake

        async def fake_seq(url, cec_wake=False, ok_delay=0, atv_name=None, atv_ip=None, ok_key="select"):
            launched.append((url, cec_wake))
            return True
        with mock.patch.object(appletv, "_deeplink_get", down), mock.patch.object(appletv, "connect", fake_connect), \
             mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()), \
             mock.patch.object(appletv, "launch_sequence_async", fake_seq):
            self.assertTrue(appletv.launch_deeplink("plutotv://live-tv/star-trek-1", cec_wake=True))
        # cold start: sleep/wake first (L3), then the app foregrounding, and the
        # pyatv fallback is told the wake is already done
        self.assertEqual(fake.calls, ["off", "on", "poll", "poll", ("launch", "com.celerity.DeepLink")])
        self.assertEqual(launched, [("plutotv://live-tv/star-trek-1", False)])

    def test_server_error_status_falls_back(self):
        import urllib.error

        def boom(url, timeout):
            raise urllib.error.HTTPError(url, 500, "server error", {}, None)

        async def fake_seq(url, **kw):
            return True
        with mock.patch.object(appletv, "_deeplink_get", boom), \
             mock.patch.object(appletv, "launch_sequence_async", fake_seq):
            self.assertTrue(appletv.launch_deeplink("https://example.com/x.m3u8"))

    def test_bare_hostname_server_and_pre_encoded_target(self):
        self.assertEqual(appletv.deeplink_open_url("https://tv.apple.com/us/movie/a%20b/umc.1", server="Game-Room.local"),
                         "http://Game-Room.local/open?target=https://tv.apple.com/us/movie/a%20b/umc.1")

    def test_bad_ok_delay_on_the_deeplink_path_does_not_raise(self):
        with mock.patch.object(appletv, "_deeplink_get", lambda url, timeout: (200, b"ok")):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x", ok_delay="four"))

    def test_no_server_configured_goes_straight_to_pyatv(self):
        appletv.DEEPLINK_SERVER = None

        async def fake_seq(url, **kw):
            return url == "youtube://www.youtube.com/watch?v=abc"
        with mock.patch.object(appletv, "_deeplink_get", side_effect=AssertionError("no server call expected")), \
             mock.patch.object(appletv, "launch_sequence_async", fake_seq):
            self.assertTrue(appletv.launch_deeplink("youtube://www.youtube.com/watch?v=abc"))

    def test_http_exception_from_the_server_falls_back(self):
        import http.client

        def bad(url, timeout):
            raise http.client.BadStatusLine("garbage")

        async def fake_seq(url, **kw):
            return True
        with mock.patch.object(appletv, "_deeplink_get", bad), mock.patch.object(appletv, "launch_sequence_async", fake_seq):
            self.assertTrue(appletv.launch_deeplink("https://example.com/x.m3u8"))

    def test_timeout_counts_as_opened_not_as_down(self):
        import socket

        def slow(url, timeout):
            raise socket.timeout("timed out")
        with mock.patch.object(appletv, "_deeplink_get", slow), \
             mock.patch.object(appletv, "launch_sequence_async", side_effect=AssertionError("pyatv used")), \
             mock.patch.object(appletv, "_foreground_deeplink_app", side_effect=AssertionError("foregrounded")):
            self.assertTrue(appletv.launch_deeplink("https://tv.apple.com/us/movie/x"))

    def test_saved_name_uses_the_name_form(self):
        self.assertEqual(appletv.deeplink_open_url("Toy Story"), "http://192.168.1.50/open?name=Toy%20Story")

    def test_unknown_saved_name_is_an_error_not_a_pyatv_fallback(self):
        import urllib.error

        def missing(url, timeout):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        with mock.patch.object(appletv, "_deeplink_get", missing),              mock.patch.object(appletv, "launch_sequence_async", side_effect=AssertionError("pyatv used")):
            self.assertFalse(appletv.launch_deeplink("No Such Bookmark"))

    def test_empty_target(self):
        self.assertFalse(appletv.launch_deeplink(" "))


class YoutubeDeepLinkTests(unittest.TestCase):
    WANT = "youtube://www.youtube.com/watch?v=MmB9b5njVbA"

    def test_bare_id(self):
        self.assertEqual(youtube_deep_link("MmB9b5njVbA"), self.WANT)

    def test_watch_url(self):
        self.assertEqual(youtube_deep_link("https://www.youtube.com/watch?v=MmB9b5njVbA"), self.WANT)
        self.assertEqual(youtube_deep_link("https://m.youtube.com/watch?feature=share&v=MmB9b5njVbA&t=12"), self.WANT)

    def test_short_link(self):
        self.assertEqual(youtube_deep_link("https://youtu.be/MmB9b5njVbA?si=abc"), self.WANT)

    def test_other_youtube_scheme_forms_are_normalised(self):
        self.assertEqual(youtube_deep_link("youtube://v/MmB9b5njVbA"), self.WANT)
        self.assertEqual(youtube_deep_link("youtube://MmB9b5njVbA"), self.WANT)
        self.assertEqual(youtube_deep_link("youtube://www.youtube.com/watch?v=MmB9b5njVbA"), self.WANT)

    def test_reset_config_marks_cached_connection_stale_and_next_connect_closes_it(self):
        """reset_config() runs on the pad thread with no event loop, where
        pyatv's close() cannot schedule its disconnect tasks (it leaked the
        socket per tags.yml reload). It only forgets the loop; the next
        action's connect() then closes the old device before reconnecting."""
        import app.appletv as appletv

        class FakeAtv:
            closed = False

            def close(self):
                self.closed = True

        fake = FakeAtv()
        appletv._atv, appletv._atv_name, appletv._config_loaded = fake, "Game Room", True
        appletv._atv_loop = object()
        appletv.reset_config()
        self.assertFalse(fake.closed)  # nothing to run the close tasks on here
        self.assertIsNone(appletv._atv_loop)
        self.assertFalse(appletv._config_loaded)

        async def no_scan(timeout=5):
            return []
        with mock.patch.object(appletv, "scan_for_apple_tvs", no_scan):
            self.assertIsNone(asyncio.run(appletv.connect()))
        self.assertTrue(fake.closed)
        self.assertIsNone(appletv._atv)

    def test_whitespace_trimmed(self):
        self.assertEqual(youtube_deep_link("  MmB9b5njVbA \n"), self.WANT)


if __name__ == "__main__":
    unittest.main()
