"""appletv.youtube_deep_link: every link style in tags.yml becomes the
youtube:// form that tvOS opens (plain https links are refused by tvOS)."""
import unittest

from app.appletv import youtube_deep_link


class YoutubeDeepLinkTests(unittest.TestCase):
    WANT = "youtube://www.youtube.com/watch?v=MmB9b5njVbA"

    def test_bare_id(self):
        self.assertEqual(youtube_deep_link("MmB9b5njVbA"), self.WANT)

    def test_watch_url(self):
        self.assertEqual(youtube_deep_link("https://www.youtube.com/watch?v=MmB9b5njVbA"), self.WANT)
        self.assertEqual(youtube_deep_link("https://m.youtube.com/watch?feature=share&v=MmB9b5njVbA&t=12"), self.WANT)

    def test_short_link(self):
        self.assertEqual(youtube_deep_link("https://youtu.be/MmB9b5njVbA?si=abc"), self.WANT)

    def test_existing_scheme_is_kept(self):
        self.assertEqual(youtube_deep_link("youtube://v/MmB9b5njVbA"), "youtube://v/MmB9b5njVbA")

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
