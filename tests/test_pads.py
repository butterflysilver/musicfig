"""`gsd-pads off|on|status` (life-ops #75): the flag + file in app.lego, the
gate in the pad driver's light commands, the pad loop's repaint, the
/pads HTTP hook, and the CLI (scripts/pi/pads.py) with the service stubbed."""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

import app.lego as lego
from app import app as flask_app

# The CLI is a standalone script (installed as /usr/local/bin/gsd-pads); load
# it by path the way tests/test_playing_marker.py loads app/homepod.py.
_PATH = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'pi', 'pads.py')
_spec = importlib.util.spec_from_file_location('musicfig_gsd_pads', _PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

OFF, GREEN, BLUE = [0, 0, 0], [0, 255, 0], [0, 0, 255]


class PadsStateMixin:
    """A temp pads-off file, the flag cleared, no driver registered."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pads-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.marker = os.path.join(self.tmp, 'pads-off')
        for name, value in (('PADS_OFF_FILE', self.marker), ('_pads_off', False), ('_pad_driver', None)):
            patcher = mock.patch.object(lego, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        lego._pads_changed.clear()
        self.addCleanup(lego._pads_changed.clear)

    def driver(self, dev='pad'):
        """A Dimensions with the USB layer replaced by a call log."""
        d = lego.Dimensions.__new__(lego.Dimensions)
        d.dev = dev
        d.sent = []
        d.send_command = d.sent.append
        return d


class FlagTests(PadsStateMixin, unittest.TestCase):
    def test_off_records_the_flag_and_the_file(self):
        with self.assertLogs(lego.logger, level='INFO') as logs:
            self.assertTrue(lego.set_pads_off(True))
        self.assertTrue(lego.pads_off())
        self.assertTrue(os.path.exists(self.marker))
        self.assertTrue(lego._pads_changed.is_set())
        self.assertIn('pads off', logs.output[-1])

    def test_on_clears_both(self):
        lego.set_pads_off(True)
        lego._pads_changed.clear()
        with self.assertLogs(lego.logger, level='INFO') as logs:
            self.assertFalse(lego.set_pads_off(False))
        self.assertFalse(lego.pads_off())
        self.assertFalse(os.path.exists(self.marker))
        self.assertTrue(lego._pads_changed.is_set())
        self.assertIn('pads on', logs.output[-1])

    def test_on_with_no_file_is_harmless(self):
        lego.set_pads_off(False)
        self.assertFalse(os.path.exists(self.marker))

    def test_unwritable_file_keeps_the_flag_and_warns(self):
        with mock.patch.object(lego, 'PADS_OFF_FILE', os.path.join(self.tmp, 'no-such-dir', 'pads-off')):
            with self.assertLogs(lego.logger, level='WARNING') as logs:
                self.assertTrue(lego.set_pads_off(True))
        self.assertTrue(lego.pads_off())
        self.assertIn('pads-off marker', logs.output[0])

    def test_startup_picks_the_flag_up_from_the_file(self):
        self.assertFalse(lego.load_pads_off())
        open(self.marker, 'w').close()
        with self.assertLogs(lego.logger, level='INFO'):
            self.assertTrue(lego.load_pads_off())
        self.assertTrue(lego.pads_off())

    def test_status_counts_the_attached_pad(self):
        self.assertEqual(lego.pads_status(), {'off': False, 'pads': 0})   # before startLego
        d = self.driver(dev=None)
        with mock.patch.object(lego, '_pad_driver', d):
            self.assertEqual(lego.pad_count(), 0)                        # unplugged
            d.dev = 'pad'
            lego.set_pads_off(True)
            self.assertEqual(lego.pads_status(), {'off': True, 'pads': 1})


class DriverGateTests(PadsStateMixin, unittest.TestCase):
    def test_lights_pass_while_on(self):
        d = self.driver()
        d.switch_pad(0, GREEN)
        d.fade_pad(1, 10, 3, BLUE)
        d.flash_pad(1, 10, 10, 6, BLUE)
        self.assertEqual(len(d.sent), 3)

    def test_only_a_dark_paint_passes_while_off(self):
        d = self.driver()
        lego.set_pads_off(True)
        d.switch_pad(1, BLUE)                 # a tap registered
        d.switch_pad(2, GREEN)                # a lift's idle repaint
        d.fade_pad(1, 10, 3, BLUE)
        d.flash_pad(1, 10, 10, 6, [255, 0, 0])
        self.assertEqual(d.sent, [])
        d.switch_pad(0, OFF)                  # how `off` itself blanks the pads
        self.assertEqual(len(d.sent), 1)
        self.assertEqual(d.sent[0][4:8], [0, 0, 0, 0])

    def test_on_lets_the_lights_through_again(self):
        d = self.driver()
        lego.set_pads_off(True)
        lego.set_pads_off(False)
        d.switch_pad(0, GREEN)
        self.assertEqual(len(d.sent), 1)


class RepaintTests(PadsStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base = lego.Base.__new__(lego.Base)
        self.base.OFF = OFF
        self.base.base = self.driver()

    def test_nothing_pending_paints_nothing(self):
        self.base.repaintPads(GREEN)
        self.assertEqual(self.base.base.sent, [])

    def test_off_paints_every_pad_dark_once(self):
        lego.set_pads_off(True)
        self.base.repaintPads(GREEN)
        self.base.repaintPads(GREEN)          # the next loop pass: already done
        self.assertEqual(len(self.base.base.sent), 1)
        self.assertEqual(self.base.base.sent[0][4:8], [0, 0, 0, 0])
        self.assertFalse(lego._pads_changed.is_set())

    def test_on_restores_the_idle_colour(self):
        lego.set_pads_off(True)
        self.base.repaintPads(GREEN)
        lego.set_pads_off(False)
        self.base.repaintPads(GREEN)
        self.assertEqual(self.base.base.sent[-1][4:8], [0] + GREEN)

    def test_on_with_lights_off_config_leaves_the_pads_dark(self):
        lego.set_pads_off(True)
        self.base.repaintPads(OFF)
        lego.set_pads_off(False)
        self.base.repaintPads(OFF)            # idle colour is OFF with `lights: off`
        self.assertEqual([s[5:8] for s in self.base.base.sent], [[0, 0, 0], [0, 0, 0]])

    def test_no_pad_attached_drops_the_paint_quietly(self):
        self.base.base = self.driver(dev=None)
        self.base.base.send_command = lego.Dimensions.send_command.__get__(self.base.base)
        lego.set_pads_off(True)
        self.base.repaintPads(GREEN)          # send_command: dev is None -> return
        self.assertTrue(lego.pads_off())      # the flag holds for when a pad appears


class HttpHookTests(PadsStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = flask_app.test_client()

    def test_status(self):
        r = self.client.get('/pads')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {'off': False, 'pads': 0})

    def test_off_then_on_report_changed(self):
        r = self.client.post('/pads/off')
        self.assertEqual(r.get_json(), {'off': True, 'pads': 0, 'changed': True})
        self.assertTrue(os.path.exists(self.marker))
        r = self.client.post('/pads/off')
        self.assertEqual(r.get_json()['changed'], False)
        r = self.client.post('/pads/on')
        self.assertEqual(r.get_json(), {'off': False, 'pads': 0, 'changed': True})
        self.assertFalse(os.path.exists(self.marker))

    def test_other_modes_and_methods_are_refused(self):
        self.assertEqual(self.client.post('/pads/toggle').status_code, 404)
        # a GET lands on the app's root static route (templates/), hence 404 not 405
        self.assertIn(self.client.get('/pads/off').status_code, (404, 405))
        self.assertFalse(lego.pads_off())


class CliTests(unittest.TestCase):
    """scripts/pi/pads.py against a stubbed service: each request is answered
    from `self.answers` (or raises), the calls are logged."""

    def setUp(self):
        self.calls = []
        self.answers = {}
        self.error = None

        class Resp(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def urlopen(req, timeout=None):
            self.calls.append((req.get_method(), req.selector, req.get_header('Host'), timeout))
            if self.error is not None:
                raise self.error
            return Resp(json.dumps(self.answers[req.selector]).encode('utf-8'))

        patcher = mock.patch.object(cli.urllib.request, 'urlopen', urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            rc = cli.main(list(argv))
        return rc, out.getvalue().strip(), err.getvalue().strip()

    def test_off_with_a_pad(self):
        self.answers['/pads/off'] = {'off': True, 'pads': 1, 'changed': True}
        self.assertEqual(self.run_cli('off'), (0, 'off (1 pad)', ''))
        self.assertEqual(self.calls, [('POST', '/pads/off', 'localhost:5000', cli.TIMEOUT)])

    def test_off_with_no_pad_attached_exits_0_with_a_message(self):
        self.answers['/pads/off'] = {'off': True, 'pads': 0, 'changed': True}
        rc, out, err = self.run_cli('off')
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(out, 'off (no pad attached; applies when one is plugged in)')

    def test_already_off(self):
        self.answers['/pads/off'] = {'off': True, 'pads': 1, 'changed': False}
        self.assertEqual(self.run_cli('off'), (0, 'already off (1 pad)', ''))

    def test_on(self):
        self.answers['/pads/on'] = {'off': False, 'pads': 1, 'changed': True}
        self.assertEqual(self.run_cli('on'), (0, 'on (1 pad)', ''))
        self.answers['/pads/on'] = {'off': False, 'pads': 0, 'changed': False}
        self.assertEqual(self.run_cli('on'), (0, 'already on (no pad attached)', ''))

    def test_status_is_a_get(self):
        self.answers['/pads'] = {'off': True, 'pads': 1}
        self.assertEqual(self.run_cli('status'), (0, 'off (1 pad)', ''))
        self.answers['/pads'] = {'off': False, 'pads': 0}
        self.assertEqual(self.run_cli('status'), (0, 'on (no pad attached)', ''))
        self.assertEqual([c[0] for c in self.calls], ['GET', 'GET'])

    def test_service_down_exits_1_and_says_so(self):
        self.error = urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))
        rc, out, err = self.run_cli('off')
        self.assertEqual((rc, out), (1, ''))
        self.assertIn('musicfig is not answering', err)
        self.assertIn('Connection refused', err)
        self.assertIn('systemctl is-active musicfig', err)

    def test_http_error_and_garbage_are_reported_not_raised(self):
        self.error = urllib.error.HTTPError('http://127.0.0.1:5000/pads', 500, 'boom', {}, None)
        self.assertEqual(self.run_cli('status')[0], 1)
        self.error = ValueError('not json')
        self.assertEqual(self.run_cli('status')[0], 1)

    def test_usage(self):
        for argv in ((), ('toggle',), ('off', 'on')):
            rc, out, err = self.run_cli(*argv)
            self.assertEqual((rc, out, err), (2, '', cli.USAGE), argv)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
