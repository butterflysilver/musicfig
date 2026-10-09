"""Base.stopYoto: a lift stops the Yoto in the background, a replacement
(another figure placed) stops it before returning so the stop can never
overtake the new figure's play command."""
import threading
import unittest
from unittest import mock

import app.lego as lego


class StopYotoTests(unittest.TestCase):
    def setUp(self):
        # Base.__init__ claims the USB pad; the method under test needs none of it.
        self.base = lego.Base.__new__(lego.Base)
        self.base.yoto_player = 'y2mvs76l93Qme2esTgJ5ZY5Q'

    def test_nothing_playing_sends_nothing(self):
        self.base.yoto_player = None
        with mock.patch.object(lego.yoto_tracks, 'player_command') as cmd:
            self.base.stopYoto()
            self.base.stopYoto(wait=True)
        cmd.assert_not_called()

    def test_replacement_stops_before_returning(self):
        calls = []
        with mock.patch.object(lego.yoto_tracks, 'player_command',
                               side_effect=lambda p, a: calls.append((p, a, threading.current_thread().name)) or True):
            with self.assertLogs('app.lego', level='INFO') as logs:
                self.base.stopYoto(wait=True)
        # sent on the calling thread, already done when stopYoto returns
        self.assertEqual(calls, [('y2mvs76l93Qme2esTgJ5ZY5Q', 'stop', threading.current_thread().name)])
        self.assertIsNone(self.base.yoto_player)
        self.assertIn('Yoto player stopped (figure replaced)', logs.output[-1])

    def test_lift_stops_in_the_background(self):
        done = threading.Event()
        calls = []

        def cmd(player, action):
            calls.append((player, action, threading.current_thread().name))
            done.set()
            return True

        with mock.patch.object(lego.yoto_tracks, 'player_command', side_effect=cmd):
            with self.assertLogs('app.lego', level='INFO') as logs:
                self.base.stopYoto()
                self.assertIsNone(self.base.yoto_player)  # cleared immediately
                self.assertTrue(done.wait(2), 'stop never sent')
                for t in threading.enumerate():
                    if t.name == 'yoto-stop':
                        t.join(2)
        self.assertEqual(calls, [('y2mvs76l93Qme2esTgJ5ZY5Q', 'stop', 'yoto-stop')])
        self.assertIn('Yoto player stopped (figure lifted)', logs.output[-1])

    def test_failed_stop_is_not_logged_as_stopped(self):
        with mock.patch.object(lego.yoto_tracks, 'player_command', return_value=False):
            with self.assertNoLogs('app.lego', level='INFO'):
                self.base.stopYoto(wait=True)
        self.assertIsNone(self.base.yoto_player)


if __name__ == '__main__':
    unittest.main()
