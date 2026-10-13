"""The "a figure is playing" marker the network watchdog reads (life-ops
#121): lego.set_playing keeps the file while any source plays, and
homepod reports a stream's start and end through its on_playing hook."""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import app.lego as lego

# app/homepod.py loaded on its own (pyatv may be absent on a dev machine),
# the way tests/test_homepod_transcode.py does, so the stream coroutine can
# be driven with a stubbed pyatv.
sys.modules.setdefault("pyatv", types.ModuleType("pyatv"))
_PATH = os.path.join(os.path.dirname(__file__), '..', 'app', 'homepod.py')
_spec = importlib.util.spec_from_file_location('musicfig_homepod_marker', _PATH)
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)


class SetPlayingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='playing-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.marker = os.path.join(self.tmp, 'playing')
        patcher = mock.patch.object(lego, 'PLAYING_FILE', self.marker)
        patcher.start()
        self.addCleanup(patcher.stop)
        lego._playing_sources.clear()
        lego._playing_warned = False

    def exists(self):
        return os.path.exists(self.marker)

    def test_marker_exists_while_any_source_plays(self):
        self.assertFalse(self.exists())
        lego.set_playing('mp3', True)
        self.assertTrue(self.exists())
        lego.set_playing('homepod', True)
        lego.set_playing('mp3', False)
        self.assertTrue(self.exists())          # the HomePod stream is still going
        lego.set_playing('homepod', False)
        self.assertFalse(self.exists())

    def test_repeated_and_redundant_updates_are_harmless(self):
        lego.set_playing('mp3', False)          # nothing was playing
        self.assertFalse(self.exists())
        lego.set_playing('mp3', True)
        os.remove(self.marker)                  # someone cleaned /run by hand
        lego.set_playing('mp3', True)           # no change, no rewrite
        self.assertFalse(self.exists())
        lego.set_playing('mp3', False)          # nothing to remove, no error
        self.assertFalse(self.exists())

    def test_missing_runtime_directory_is_reported_once_and_ignored(self):
        with mock.patch.object(lego, 'PLAYING_FILE', os.path.join(self.tmp, 'no-such-dir', 'playing')):
            with self.assertLogs(lego.logger, level='INFO') as logs:
                lego.set_playing('mp3', True)
                lego.set_playing('mp3', False)
                lego.set_playing('mp3', True)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('network watchdog will not see playback', logs.output[0])

    def test_mp3_monitor_states_drive_the_marker(self):
        # what Base.initMp3's monitor thread does with each player event
        for state, playing in (('PLAYING', True), ('PLAYING', True), ('PAUSED', False),
                               ('PLAYING', True), ('FINISHED', False), ('READY', False)):
            lego.set_playing('mp3', state == 'PLAYING')
            self.assertEqual(self.exists(), playing, state)

    def test_lego_plugs_the_marker_into_homepod(self):
        lego.homepod.on_playing(True)
        self.assertTrue(self.exists())
        lego.homepod.on_playing(False)
        self.assertFalse(self.exists())


class HomepodHookTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        hp.on_playing = self.seen.append
        self.addCleanup(setattr, hp, 'on_playing', None)

    def stream(self, sources, **patches):
        started, ok = threading.Event(), [False]
        with mock.patch.multiple(hp, **patches):
            asyncio.run(hp._stream_sources_async(sources, 'Office', started, ok))
        self.assertTrue(started.is_set())
        return ok[0]

    def test_stream_reports_start_and_end(self):
        atv = mock.Mock()
        device = mock.Mock()
        device.name = 'Office'
        with mock.patch.object(hp.pyatv, 'connect', mock.AsyncMock(return_value=atv), create=True):
            ok = self.stream(['https://x.example/a.mp3', 'https://x.example/b.mp3'],
                             _get_device=mock.AsyncMock(return_value=device),
                             _play_source=mock.AsyncMock())
        self.assertTrue(ok)
        self.assertEqual(self.seen, [True, False])
        atv.close.assert_called_once()

    def test_stream_that_never_starts_only_reports_the_end(self):
        ok = self.stream([], _get_device=mock.AsyncMock(return_value=None))
        self.assertFalse(ok)
        self.assertEqual(self.seen, [False])

    def test_hook_failure_never_ends_the_stream(self):
        hp.on_playing = mock.Mock(side_effect=RuntimeError('marker exploded'))
        atv = mock.Mock()
        with mock.patch.object(hp.pyatv, 'connect', mock.AsyncMock(return_value=atv), create=True):
            with self.assertLogs(hp.logger, level='WARNING') as logs:
                ok = self.stream(['https://x.example/a.mp3'],
                                 _get_device=mock.AsyncMock(return_value=mock.Mock()),
                                 _play_source=mock.AsyncMock())
        self.assertTrue(ok)
        self.assertEqual(hp.on_playing.call_args_list, [mock.call(True), mock.call(False)])
        self.assertTrue(all('on_playing hook failed' in line for line in logs.output))

    def test_no_hook_is_fine(self):
        hp.on_playing = None
        hp._notify_playing(True)   # nothing to call, nothing raised


if __name__ == '__main__':
    unittest.main()
