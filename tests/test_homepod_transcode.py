"""homepod.ffmpeg_command / transcode switch (pure, no network)."""
import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

# Load app/homepod.py directly; pyatv may be absent on a dev machine.
sys.modules.setdefault("pyatv", types.ModuleType("pyatv"))
_PATH = os.path.join(os.path.dirname(__file__), '..', 'app', 'homepod.py')
_spec = importlib.util.spec_from_file_location('musicfig_homepod', _PATH)
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)


class TranscodeTests(unittest.TestCase):
    def test_ffmpeg_command_writes_mp3_segments(self):
        cmd = hp.ffmpeg_command("https://x.example/a.m4a?sig=1", "/tmp/out")
        self.assertEqual(cmd[0], hp.FFMPEG)
        self.assertIn("-nostdin", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], "https://x.example/a.m4a?sig=1")
        self.assertEqual(cmd[cmd.index("-f") + 1], "segment")
        times = cmd[cmd.index("-segment_times") + 1].split(",")
        self.assertEqual(times[:3], [str(hp.FIRST_SEGMENT_SECONDS),
                                     str(hp.FIRST_SEGMENT_SECONDS + hp.SEGMENT_SECONDS),
                                     str(hp.FIRST_SEGMENT_SECONDS + hp.SEGMENT_SECONDS + hp.LONG_SEGMENT_SECONDS)])
        self.assertGreater(int(times[-1]), 7 * 3600)
        self.assertTrue(cmd[-1].endswith("seg%05d.mp3"))

    def test_match_exact_then_partial(self):
        devices = {"Guest Room HomePod": "A", "192.168.0.106": "A", "Office": "B"}
        self.assertEqual(hp._match("Office", devices), "B")
        self.assertEqual(hp._match("guest room", devices), "A")
        self.assertIsNone(hp._match("Basement", devices))
        self.assertEqual(hp._segment_path("/tmp/out", 3), os.path.join("/tmp/out", "seg00003.mp3"))

    def test_transcode_switch(self):
        with mock.patch.dict(os.environ, {"MUSICFIG_TRANSCODE": "no"}):
            self.assertFalse(hp._transcode_enabled())
        with mock.patch.dict(os.environ, {"MUSICFIG_TRANSCODE": "yes"}):
            self.assertTrue(hp._transcode_enabled())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MUSICFIG_TRANSCODE", None)
            self.assertTrue(hp._transcode_enabled())

    def test_is_url(self):
        self.assertTrue(hp._is_url("https://a/b"))
        self.assertFalse(hp._is_url("/var/lib/musicfig/music/a.mp3"))


if __name__ == '__main__':
    unittest.main()
