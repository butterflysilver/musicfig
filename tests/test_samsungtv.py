"""app/samsungtv.py over the TV's local API (life-ops #183): config loading
incl. the legacy SmartThings warning, the SmartThings-era key names lego.py
still sends, switch_to_appletv on / standby / unreachable, the Wake-on-LAN
packet, and the never-raises promise. No network: requests.get, the websocket
remote and the UDP socket are all fakes."""
import itertools
import os
import tempfile
import unittest
from unittest import mock

import requests

import app.samsungtv as stv

HOST = '192.0.2.48'          # TEST-NET, never routed
MAC = 'AA:BB:CC:DD:EE:FF'
TAGS = {'samsungtv_host': HOST, 'samsungtv_mac': MAC}
LEGACY = {'smartthings_token': 'x', 'smartthings_tv_device_id': 'y', 'smartthings_appletv_input': 'HDMI3'}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError('HTTP %d' % self.status_code)

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def _state(power):
    return _Resp({'device': {'PowerState': power, 'name': '[TV] Big TV', 'modelName': 'QN75QN80F'}})


class _FakeRemote:
    """SamsungTVWS stand-in: records keys, can fail on open or send, and can
    drop a token file the way the real one does after the Allow prompt."""
    instances = []
    write_token = None  # patched on the class by the pair tests

    def __init__(self, host, port=8001, token_file=None, name='SamsungTvRemote', timeout=None, token=None):
        self.host, self.port, self.token_file, self.name, self.timeout = host, port, token_file, name, timeout
        self.keys, self.opened, self.closed = [], 0, 0
        _FakeRemote.instances.append(self)

    def open(self):
        self.opened += 1
        if self.write_token:
            with open(self.token_file, 'w') as fh:
                fh.write(self.write_token)

    def send_key(self, key, times=1, key_press_delay=None, cmd='Click'):
        self.keys.append(key)

    def close(self):
        self.closed += 1


class SamsungTvTestCase(unittest.TestCase):
    """Fresh module globals, a fake remote and no sleeping, for every test."""
    def setUp(self):
        stv.reset_config()
        for name, value in (('SAMSUNGTV_HOST', None), ('SAMSUNGTV_MAC', None),
                            ('SAMSUNGTV_TOKEN_FILE', stv.DEFAULT_TOKEN_FILE), ('SAMSUNGTV_NAME', None),
                            ('SAMSUNGTV_WAKE_TIMEOUT', stv.DEFAULT_WAKE_TIMEOUT)):
            mock.patch.object(stv, name, value).start()
        _FakeRemote.instances = []
        self.remote_cls = mock.patch.object(stv, 'SamsungTVWS', _FakeRemote).start()
        self.get = mock.patch.object(stv.requests, 'get').start()
        self.sleep = mock.patch.object(stv.time, 'sleep').start()
        self.wol = mock.patch.object(stv, 'send_wol', return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def keys_sent(self):
        return [k for r in _FakeRemote.instances for k in r.keys]


class ConfigTests(SamsungTvTestCase):
    def test_new_keys_configure_the_tv(self):
        stv.load_config({'samsungtv_host': HOST, 'samsungtv_mac': MAC, 'samsungtv_token_file': '/tmp/t',
                         'samsungtv_name': 'musicfig test', 'samsungtv_wake_timeout': '20'})
        self.assertTrue(stv.configured())
        self.assertTrue(stv.activated())
        self.assertEqual((stv.SAMSUNGTV_HOST, stv.SAMSUNGTV_MAC, stv.SAMSUNGTV_TOKEN_FILE, stv.SAMSUNGTV_NAME,
                          stv.SAMSUNGTV_WAKE_TIMEOUT), (HOST, MAC, '/tmp/t', 'musicfig test', 20.0))

    def test_defaults(self):
        stv.load_config({'samsungtv_host': HOST})
        self.assertTrue(stv.configured())
        self.assertEqual(stv.SAMSUNGTV_TOKEN_FILE, '/etc/musicfig/samsungtv.token')
        self.assertEqual(stv.SAMSUNGTV_WAKE_TIMEOUT, 12)
        self.assertIsNone(stv.SAMSUNGTV_NAME)
        with mock.patch.dict(os.environ, {'MUSICFIG_ROOM': 'game-room'}):
            self.assertEqual(stv.default_name(), 'musicfig game-room')

    def test_bad_wake_timeout_falls_back(self):
        with self.assertLogs('app.samsungtv', level='WARNING') as logs:
            stv.load_config({'samsungtv_host': HOST, 'samsungtv_wake_timeout': 'soon'})
        self.assertEqual(stv.SAMSUNGTV_WAKE_TIMEOUT, stv.DEFAULT_WAKE_TIMEOUT)
        self.assertIn('samsungtv_wake_timeout', logs.output[0])

    def test_nothing_configured(self):
        stv.load_config({'mp3_dir': '/x'})
        self.assertFalse(stv.configured())
        self.assertFalse(stv.activated())

    def test_legacy_keys_warn_once_and_stay_unconfigured(self):
        with self.assertLogs('app.samsungtv', level='WARNING') as logs:
            stv.load_config(dict(LEGACY))
            stv.load_config(dict(LEGACY))  # second call is a no-op until reset_config()
        self.assertEqual(len(logs.output), 1)
        for key in ('smartthings_token', 'samsungtv_host', 'samsungtv_mac', 'samsungtv_token_file',
                    'samsungtv_name', 'samsungtv_wake_timeout', 'app.samsungtv pair'):
            self.assertIn(key, logs.output[0])
        self.assertFalse(stv.configured())
        # Unconfigured = no network: not the HTTP status, not the websocket, not WoL.
        self.assertFalse(stv.switch_to_appletv())
        self.assertFalse(stv.send_key('OK'))
        self.assertFalse(stv.turn_on())
        self.get.assert_not_called()
        self.assertEqual(_FakeRemote.instances, [])
        self.wol.assert_not_called()

    def test_legacy_keys_beside_the_new_ones_are_ignored_quietly(self):
        tags = dict(LEGACY, **TAGS)
        with self.assertNoLogs('app.samsungtv', level='WARNING'):
            stv.load_config(tags)
        self.assertTrue(stv.configured())

    def test_reset_config_reloads(self):
        stv.load_config({'samsungtv_host': HOST})
        stv.load_config({'samsungtv_host': '192.0.2.99'})
        self.assertEqual(stv.SAMSUNGTV_HOST, HOST)
        stv.reset_config()
        stv.load_config({'samsungtv_host': '192.0.2.99'})
        self.assertEqual(stv.SAMSUNGTV_HOST, '192.0.2.99')


class KeyMappingTests(SamsungTvTestCase):
    def test_smartthings_names_map_to_websocket_keys(self):
        expected = {
            'HOME': 'KEY_HOME', 'LEFT': 'KEY_LEFT', 'DOWN': 'KEY_DOWN', 'RIGHT': 'KEY_RIGHT', 'UP': 'KEY_UP',
            'OK': 'KEY_ENTER', 'BACK': 'KEY_RETURN', 'EXIT': 'KEY_EXIT', 'MENU': 'KEY_MENU', 'MUTE': 'KEY_MUTE',
            'PLAY': 'KEY_PLAY', 'PAUSE': 'KEY_PAUSE', 'STOP': 'KEY_STOP', 'REWIND': 'KEY_REWIND', 'FF': 'KEY_FF',
            'SOURCE': 'KEY_SOURCE', 'POWER': 'KEY_POWER',
        }
        for old, new in expected.items():
            self.assertEqual(stv.map_key(old), new, old)
            self.assertEqual(stv.map_key(old.lower()), new, old)

    def test_raw_keys_pass_through(self):
        self.assertEqual(stv.map_key('KEY_HDMI'), 'KEY_HDMI')
        self.assertEqual(stv.map_key(' key_info '), 'KEY_INFO')

    def test_unknown_key_is_refused_without_a_connection(self):
        self.assertIsNone(stv.map_key('TELEPORT'))
        self.assertIsNone(stv.map_key(None))
        stv.load_config(TAGS)
        with self.assertLogs('app.samsungtv', level='WARNING'):
            self.assertFalse(stv.send_key('TELEPORT'))
        self.assertEqual(_FakeRemote.instances, [])

    def test_lego_xbox_navigation_sends_mapped_keys(self):
        """The exact sequence lego.py's xbox path sends."""
        stv.load_config(TAGS)
        for key in ('HOME', 'LEFT', 'DOWN', 'RIGHT', 'DOWN', 'DOWN', 'DOWN', 'OK'):
            self.assertTrue(stv.send_key(key))
        self.assertEqual(self.keys_sent(), ['KEY_HOME', 'KEY_LEFT', 'KEY_DOWN', 'KEY_RIGHT',
                                            'KEY_DOWN', 'KEY_DOWN', 'KEY_DOWN', 'KEY_ENTER'])


class SendKeyTests(SamsungTvTestCase):
    def test_opens_a_fresh_remote_per_key_and_closes_it(self):
        stv.load_config(dict(TAGS, samsungtv_token_file='/tmp/stv.token', samsungtv_name='musicfig test'))
        self.assertTrue(stv.send_key('OK'))
        self.assertTrue(stv.send_key('EXIT'))
        self.assertEqual(len(_FakeRemote.instances), 2)
        remote = _FakeRemote.instances[0]
        self.assertEqual((remote.host, remote.port, remote.token_file, remote.name, remote.timeout),
                         (HOST, 8002, '/tmp/stv.token', 'musicfig test', stv.WS_TIMEOUT))
        for remote in _FakeRemote.instances:
            self.assertEqual((remote.opened, remote.closed), (1, 1))

    def test_default_name_is_musicfig_room(self):
        stv.load_config(TAGS)
        with mock.patch.dict(os.environ, {'MUSICFIG_ROOM': 'game-room'}):
            stv.send_key('OK')
        self.assertEqual(_FakeRemote.instances[0].name, 'musicfig game-room')

    def test_open_failure_is_false_and_closed(self):
        stv.load_config(TAGS)
        with mock.patch.object(_FakeRemote, 'open', side_effect=ConnectionRefusedError('refused')):
            with self.assertLogs('app.samsungtv', level='WARNING') as logs:
                self.assertFalse(stv.send_key('OK'))
        self.assertIn('ConnectionRefusedError', logs.output[0])
        self.assertEqual(_FakeRemote.instances[0].closed, 1)

    def test_send_failure_is_false(self):
        stv.load_config(TAGS)
        with mock.patch.object(_FakeRemote, 'send_key', side_effect=OSError('socket closed')):
            self.assertFalse(stv.send_key('OK'))
        self.assertEqual(_FakeRemote.instances[0].closed, 1)

    def test_without_the_library(self):
        stv.load_config(TAGS)
        with mock.patch.object(stv, 'SamsungTVWS', None):
            self.assertFalse(stv.activated())
            self.assertTrue(stv.configured())
            with self.assertLogs('app.samsungtv', level='ERROR'):
                self.assertFalse(stv.send_key('OK'))

    def test_token_never_reaches_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, 'samsungtv.token')
            with open(token_file, 'w') as fh:
                fh.write('SECRET-TOKEN-12345678')
            stv.load_config(dict(TAGS, samsungtv_token_file=token_file))
            with mock.patch.object(_FakeRemote, 'open', side_effect=RuntimeError('unauthorized')):
                with self.assertLogs('app.samsungtv', level='DEBUG') as logs:
                    stv.send_key('OK')
            self.assertNotIn('SECRET-TOKEN', '\n'.join(logs.output))


class PowerStateTests(SamsungTvTestCase):
    def setUp(self):
        super().setUp()
        stv.load_config(TAGS)

    def test_on(self):
        self.get.return_value = _state('on')
        self.assertTrue(stv.is_tv_on())
        self.assertEqual(stv.power_state(), 'on')
        self.get.assert_called_with('http://%s:8001/api/v2/' % HOST, timeout=stv.HTTP_TIMEOUT)

    def test_standby(self):
        self.get.return_value = _state('standby')
        self.assertFalse(stv.is_tv_on())
        self.assertEqual(stv.power_state(), 'standby')

    def test_unreachable(self):
        self.get.side_effect = requests.exceptions.ConnectTimeout('timeout')
        self.assertIsNone(stv.power_state())
        self.assertFalse(stv.is_tv_on())

    def test_garbage_answers(self):
        for resp in (_Resp(None), _Resp({'nope': 1}), _Resp({'device': 'x'}), _Resp({}, status=500)):
            self.get.return_value = resp
            self.assertIsNone(stv.power_state(), resp._payload)

    def test_input_source_is_not_this_modules_job(self):
        with self.assertLogs('app.samsungtv', level='DEBUG') as logs:
            self.assertIsNone(stv.get_current_input())
        self.assertIn('cannot tell which input', logs.output[0])
        with self.assertLogs('app.samsungtv', level='INFO') as logs:
            self.assertFalse(stv.set_input_source('HDMI3'))
        self.assertIn('HDMI-CEC', logs.output[0])
        self.get.assert_not_called()
        self.assertEqual(_FakeRemote.instances, [])


class _FakeSocket:
    def __init__(self, *args):
        self.sent, self.options, self.closed, self.timeout = [], [], False, None
        self.fail_for = set()

    def setsockopt(self, level, opt, value):
        self.options.append((level, opt, value))

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, data, target):
        if target[0] in self.fail_for:
            raise OSError('no route')
        self.sent.append((data, target))

    def close(self):
        self.closed = True


class WakeOnLanTests(SamsungTvTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.stopall()  # the base fakes, minus the send_wol stub
        for name, value in (('SAMSUNGTV_HOST', None), ('SAMSUNGTV_MAC', None)):
            mock.patch.object(stv, name, value).start()
        self.socks = []

        def make_socket(*args):
            s = _FakeSocket(*args)
            self.socks.append(s)
            return s
        mock.patch.object(stv.socket, 'socket', side_effect=make_socket).start()

    def test_packet_shape(self):
        packet = stv.wol_packet(MAC)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b'\xff' * 6)
        self.assertEqual(packet[6:], bytes.fromhex('aabbccddeeff') * 16)
        self.assertEqual(stv.wol_packet('aa-bb-cc-dd-ee-ff'), packet)
        self.assertEqual(stv.wol_packet('aabb.ccdd.eeff'), packet)
        with self.assertRaises(ValueError):
            stv.wol_packet('AA:BB:CC')

    def test_broadcast_and_unicast_three_times(self):
        stv.load_config(TAGS)
        self.assertTrue(stv.send_wol())
        sock = self.socks[0]
        self.assertIn((stv.socket.SOL_SOCKET, stv.socket.SO_BROADCAST, 1), sock.options)
        targets = [t for _d, t in sock.sent]
        self.assertEqual(targets, [('<broadcast>', 9), (HOST, 9)] * 3)
        self.assertTrue(all(d == stv.wol_packet(MAC) for d, _t in sock.sent))
        self.assertTrue(sock.closed)

    def test_no_mac_sends_nothing(self):
        stv.load_config({'samsungtv_host': HOST})
        with self.assertLogs('app.samsungtv', level='WARNING'):
            self.assertFalse(stv.send_wol())
        self.assertEqual(self.socks, [])

    def test_bad_mac_sends_nothing(self):
        stv.load_config({'samsungtv_host': HOST, 'samsungtv_mac': 'not-a-mac'})
        with self.assertLogs('app.samsungtv', level='ERROR'):
            self.assertFalse(stv.send_wol())
        self.assertEqual(self.socks, [])

    def test_one_route_failing_still_counts(self):
        stv.load_config(TAGS)
        with mock.patch.object(_FakeSocket, 'sendto', autospec=True) as sendto:
            def flaky(self_, data, target):
                if target[0] == '<broadcast>':
                    raise OSError('no route')
                self_.sent.append((data, target))
            sendto.side_effect = flaky
            self.assertTrue(stv.send_wol())
        self.assertEqual(len(self.socks[0].sent), 3)
        self.assertTrue(self.socks[0].closed)


class TurnOnTests(SamsungTvTestCase):
    def setUp(self):
        super().setUp()
        stv.load_config(TAGS)

    def test_already_on(self):
        self.get.return_value = _state('on')
        self.assertTrue(stv.turn_on())
        self.wol.assert_not_called()
        self.assertEqual(_FakeRemote.instances, [])

    def test_standby_wol_then_one_power_key_then_on(self):
        self.get.side_effect = [_state('standby'), _state('standby'), _state('standby'), _state('on')]
        self.assertTrue(stv.turn_on())
        self.wol.assert_called_once_with()
        self.assertEqual(self.keys_sent(), ['KEY_POWER'])  # toggles: never twice

    def test_unreachable_then_standby_then_on(self):
        """Deep standby: no answer until the WoL lands, KEY_POWER once it does."""
        self.get.side_effect = [requests.exceptions.ConnectTimeout('t'), requests.exceptions.ConnectTimeout('t'),
                                _state('standby'), _state('on')]
        self.assertTrue(stv.turn_on())
        self.assertEqual(self.keys_sent(), ['KEY_POWER'])

    def test_power_key_retried_while_the_websocket_is_not_up_yet(self):
        self.get.side_effect = [_state('standby'), _state('standby'), _state('standby'), _state('on')]
        with mock.patch.object(_FakeRemote, 'open', side_effect=[ConnectionRefusedError('not yet'), None]):
            self.assertTrue(stv.turn_on())
        self.assertEqual(len(_FakeRemote.instances), 2)
        self.assertEqual(self.keys_sent(), ['KEY_POWER'])

    def test_times_out(self):
        self.get.return_value = _state('standby')
        with mock.patch.object(stv.time, 'time', side_effect=itertools.count(1000, 1.0)):
            with mock.patch.object(stv, 'SAMSUNGTV_WAKE_TIMEOUT', 5):
                with self.assertLogs('app.samsungtv', level='ERROR') as logs:
                    self.assertFalse(stv.turn_on())
        self.assertIn('did not come on within 5 s', logs.output[-1])
        self.assertEqual(self.keys_sent(), ['KEY_POWER'])
        self.assertLessEqual(self.get.call_count, 8)

    def test_turn_off(self):
        self.get.return_value = _state('on')
        self.assertTrue(stv.turn_off())
        self.assertEqual(self.keys_sent(), ['KEY_POWER'])
        self.get.return_value = _state('standby')
        self.assertTrue(stv.turn_off())
        self.get.side_effect = requests.exceptions.ConnectionError('down')
        self.assertFalse(stv.turn_off())
        self.assertEqual(self.keys_sent(), ['KEY_POWER'])


class SwitchToAppleTvTests(SamsungTvTestCase):
    def setUp(self):
        super().setUp()
        stv.load_config(TAGS)

    def test_on_sends_exit_and_settles(self):
        self.get.return_value = _state('on')
        self.assertTrue(stv.switch_to_appletv())
        self.assertEqual(self.keys_sent(), ['KEY_EXIT'])
        self.wol.assert_not_called()
        self.sleep.assert_called_once_with(stv.EXIT_SETTLE)

    def test_standby_wakes_then_exits(self):
        self.get.side_effect = [_state('standby'), _state('standby'), _state('standby'), _state('on')]
        self.assertTrue(stv.switch_to_appletv())
        self.wol.assert_called_once()
        self.assertEqual(self.keys_sent(), ['KEY_POWER', 'KEY_EXIT'])

    def test_standby_without_auto_turn_on(self):
        self.get.return_value = _state('standby')
        self.assertFalse(stv.switch_to_appletv(auto_turn_on=False))
        self.wol.assert_not_called()
        self.assertEqual(self.keys_sent(), [])

    def test_unreachable_is_false(self):
        self.get.side_effect = requests.exceptions.ConnectTimeout('t')
        with mock.patch.object(stv.time, 'time', side_effect=itertools.count(0, 2.0)):
            with mock.patch.object(stv, 'SAMSUNGTV_WAKE_TIMEOUT', 4):
                self.assertFalse(stv.switch_to_appletv())
        self.assertEqual(self.keys_sent(), [])

    def test_exit_failure_still_reports_the_tv_on(self):
        self.get.return_value = _state('on')
        with mock.patch.object(_FakeRemote, 'open', side_effect=ConnectionRefusedError('no pairing')):
            self.assertTrue(stv.switch_to_appletv())
        self.sleep.assert_not_called()

    def test_never_raises(self):
        self.get.side_effect = RuntimeError('something odd in requests')
        with self.assertLogs('app.samsungtv', level='ERROR') as logs:
            self.assertFalse(stv.switch_to_appletv())
        self.assertIn('ensure_on failed', logs.output[0])
        with mock.patch.object(stv, 'power_state', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):  # only real exceptions are swallowed
                stv.switch_to_appletv()


class EnsureOnTests(SamsungTvTestCase):
    """The Xbox branch in lego.py calls ensure_on(): same guts as
    switch_to_appletv, no HOME (that would open the home bar just cleared)."""
    def setUp(self):
        super().setUp()
        stv.load_config(TAGS)

    def test_on_is_exit_only(self):
        self.get.return_value = _state('on')
        self.assertTrue(stv.ensure_on())
        self.assertEqual(self.keys_sent(), ['KEY_EXIT'])
        self.assertNotIn('KEY_HOME', self.keys_sent())

    def test_standby_wakes_first(self):
        self.get.side_effect = [_state('standby'), _state('standby'), _state('standby'), _state('on')]
        self.assertTrue(stv.ensure_on())
        self.wol.assert_called_once()
        self.assertEqual(self.keys_sent(), ['KEY_POWER', 'KEY_EXIT'])

    def test_switch_to_appletv_delegates(self):
        with mock.patch.object(stv, 'ensure_on', return_value=True) as ensure_on:
            self.assertTrue(stv.switch_to_appletv(auto_turn_on=False))
        ensure_on.assert_called_once_with(auto_turn_on=False)

    def test_unconfigured(self):
        stv.reset_config()
        with mock.patch.object(stv, 'SAMSUNGTV_HOST', None):
            with self.assertLogs('app.samsungtv', level='WARNING'):
                self.assertFalse(stv.ensure_on())
        self.get.assert_not_called()


class CliTests(SamsungTvTestCase):
    def test_status(self):
        self.get.return_value = _state('on')
        with mock.patch('builtins.print') as out:
            self.assertEqual(stv.main(['status', '--host', HOST]), 0)
        printed = '\n'.join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn('PowerState: on', printed)
        self.assertIn('Big TV', printed)
        self.assertIn('QN75QN80F', printed)
        self.get.side_effect = requests.exceptions.ConnectTimeout('t')
        with mock.patch('builtins.print'):
            self.assertEqual(stv.main(['status', '--host', HOST]), 1)

    def test_pair_stores_the_token(self):
        self.get.return_value = _state('on')
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, 'samsungtv.token')
            with mock.patch.object(_FakeRemote, 'write_token', 'tok'):
                with mock.patch('builtins.print') as out:
                    rc = stv.main(['pair', '--host', HOST, '--mac', MAC, '--token-file', token_file,
                                   '--name', 'musicfig test'])
            self.assertEqual(rc, 0)
            printed = '\n'.join(str(c.args[0]) for c in out.call_args_list)
            self.assertIn('OK', printed)
            self.assertIn(token_file, printed)
            self.assertNotIn('tok\n', printed)
            remote = _FakeRemote.instances[0]
            self.assertEqual((remote.name, remote.token_file, remote.keys), ('musicfig test', token_file, ['KEY_INFO']))

    def test_pair_failure(self):
        self.get.return_value = _state('on')
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, 'samsungtv.token')
            with mock.patch.object(_FakeRemote, 'open', side_effect=RuntimeError('denied on the TV')):
                with mock.patch('builtins.print') as out:
                    rc = stv.main(['pair', '--host', HOST, '--token-file', token_file])
            self.assertEqual(rc, 1)
            self.assertIn('FAILED', str(out.call_args_list[-1].args[0]))

    def test_pair_wakes_a_standby_tv_first(self):
        self.get.side_effect = [_state('standby'), _state('standby'), _state('standby'), _state('on')]
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, 'samsungtv.token')
            with mock.patch.object(_FakeRemote, 'write_token', 'tok'):
                with mock.patch('builtins.print'):
                    rc = stv.main(['pair', '--host', HOST, '--mac', MAC, '--token-file', token_file])
        self.assertEqual(rc, 0)
        self.assertEqual(self.keys_sent(), ['KEY_POWER', 'KEY_INFO'])

    def test_pair_standby_without_mac(self):
        self.get.return_value = _state('standby')
        with mock.patch('builtins.print') as out:
            self.assertEqual(stv.main(['pair', '--host', HOST]), 1)
        self.assertIn('--mac', str(out.call_args_list[-1].args[0]))
        self.assertEqual(_FakeRemote.instances, [])


if __name__ == '__main__':
    unittest.main()
