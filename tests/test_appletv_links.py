"""appletv.youtube_deep_link: every link style in tags.yml becomes the
youtube:// form that tvOS opens (plain https links are refused by tvOS)."""
import asyncio
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
        self.power = self
        self.apps = self
        self.remote_control = self

    @property
    def power_state(self):
        self.calls.append("poll")
        if self.calls.count("poll") >= 2:
            self._state = "PowerState.On"
        return self._state

    async def turn_off(self):
        self.calls.append("off")
        self._state = "PowerState.Off"

    async def turn_on(self):
        self.calls.append("on")

    async def launch_app(self, url):
        self.calls.append(("launch", url))

    async def select(self):
        self.calls.append("select")

    def close(self):
        pass


class LaunchSequenceTests(unittest.TestCase):
    def run_seq(self, fake, **kw):
        async def fake_connect(name=None, ip=None):
            return fake
        with mock.patch.object(appletv, "connect", fake_connect), mock.patch.object(appletv.asyncio, "sleep", mock.AsyncMock()):
            return asyncio.run(appletv.launch_sequence_async("youtube://x", **kw))

    def test_plain_launch(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake))
        self.assertEqual(fake.calls, [("launch", "youtube://x")])

    def test_cec_wake_then_launch_then_select(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, cec_wake=True, ok_delay=4))
        self.assertEqual(fake.calls[:2], ["off", "on"])
        self.assertIn(("launch", "youtube://x"), fake.calls)
        self.assertEqual(fake.calls[-1], "select")
        self.assertLess(fake.calls.index(("launch", "youtube://x")), fake.calls.index("select"))

    def test_failed_wake_still_launches(self):
        fake = FakeAtv()

        async def boom():
            raise RuntimeError("no power support")
        fake.turn_off = boom
        self.assertTrue(self.run_seq(fake, cec_wake=True))
        self.assertIn(("launch", "youtube://x"), fake.calls)

    def test_bad_ok_delay_is_ignored(self):
        fake = FakeAtv()
        self.assertTrue(self.run_seq(fake, ok_delay="four"))
        self.assertEqual(fake.calls, [("launch", "youtube://x")])

    def test_launch_failure_is_false(self):
        fake = FakeAtv()

        async def boom(url):
            raise RuntimeError("Open URL failed")
        fake.launch_app = boom
        self.assertFalse(self.run_seq(fake, ok_delay=4))
        self.assertNotIn("select", fake.calls)

    def test_no_apple_tv(self):
        async def none_connect(name=None, ip=None):
            return None
        with mock.patch.object(appletv, "connect", none_connect):
            self.assertFalse(asyncio.run(appletv.launch_sequence_async("youtube://x")))


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

    def test_reset_config_drops_cached_connection(self):
        import app.appletv as appletv

        class FakeAtv:
            closed = False

            def close(self):
                self.closed = True

        fake = FakeAtv()
        appletv._atv, appletv._atv_name, appletv._config_loaded = fake, "Game Room", True
        appletv.reset_config()
        self.assertTrue(fake.closed)
        self.assertIsNone(appletv._atv)
        self.assertFalse(appletv._config_loaded)

    def test_whitespace_trimmed(self):
        self.assertEqual(youtube_deep_link("  MmB9b5njVbA \n"), self.WANT)


if __name__ == "__main__":
    unittest.main()
