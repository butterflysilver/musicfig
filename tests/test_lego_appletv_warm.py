"""Base.appleTvLaunchArgs / resumeAppleTvIfSame: the warm window (a TV launch
in the last appletv_warm_minutes) only skips the sleep/wake when the Apple TV
really reports awake. Someone turning the TV off with the remote inside the
window must get a cold launch, not a Play sent to a sleeping box."""
import time
import unittest
from unittest import mock

import app.appletv as appletv
import app.lego as lego

TAGS = {'appletv_cec_wake': True, 'appletv_url_ok_delay': '12, 5', 'appletv_url_ok_key': 'select, play_pause'}
COLD = dict(cec_wake=True, ok_delay='12, 5', ok_key='select, play_pause')


class LaunchArgsTests(unittest.TestCase):
    def setUp(self):
        self.base = lego.Base.__new__(lego.Base)
        self.base.appletv_last_launch = time.time() - 30  # inside the 10 min window

    def test_warm_and_awake_is_play_only(self):
        with mock.patch.object(appletv, 'is_on_sync', return_value=True):
            self.assertEqual(self.base.appleTvLaunchArgs(TAGS), dict(cec_wake=False, ok_delay=4, ok_key='play_pause'))

    def test_warm_but_asleep_is_a_cold_launch(self):
        with mock.patch.object(appletv, 'is_on_sync', return_value=False):
            with self.assertLogs('app.lego', level='INFO') as logs:
                self.assertEqual(self.base.appleTvLaunchArgs(TAGS), COLD)
        self.assertIn('not awake; doing a cold launch', logs.output[-1])

    def test_cold_window_never_asks_the_box(self):
        self.base.appletv_last_launch = time.time() - 3600
        with mock.patch.object(appletv, 'is_on_sync', side_effect=AssertionError('should not be called')):
            self.assertEqual(self.base.appleTvLaunchArgs(TAGS), COLD)


class ResumeTests(unittest.TestCase):
    URL = 'https://tv.apple.com/us/movie/monsters-inc/umc.cmc.3ilsdzy6ghi4adtg3tnwf6jcb'

    def setUp(self):
        self.base = lego.Base.__new__(lego.Base)
        self.base.base = mock.Mock()
        self.base.PURPLE = 'purple'
        self.base.appletv_pause_thread = None
        self.base.appletv_paused_url = self.URL
        self.base.appletv_paused_at = time.time() - 20

    def test_same_figure_on_an_awake_box_resumes(self):
        with mock.patch.object(appletv, 'is_on_sync', return_value=True), \
                mock.patch.object(appletv, 'pause', return_value=True) as pause:
            self.assertTrue(self.base.resumeAppleTvIfSame(self.URL, 1, {}))
        pause.assert_called_once()
        self.assertIsNone(self.base.appletv_paused_url)

    def test_same_figure_on_a_sleeping_box_relaunches_instead(self):
        with mock.patch.object(appletv, 'is_on_sync', return_value=False), \
                mock.patch.object(appletv, 'pause', side_effect=AssertionError('no Play on a sleeping box')):
            with self.assertLogs('app.lego', level='INFO') as logs:
                self.assertFalse(self.base.resumeAppleTvIfSame(self.URL, 1, {}))
        self.assertIn('not awake; relaunching', logs.output[-1])
        self.assertIsNone(self.base.appletv_paused_url)  # the full launch path takes over


if __name__ == '__main__':
    unittest.main()
