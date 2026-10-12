"""scripts/report_network.py: the pad's view of the house network as the
gsd-cloud Network page receives it - its own Wi-Fi link, the watchdog's
state and the LAN it can see - with every tool allowed to be missing."""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    'report_network', os.path.join(HERE, '..', 'scripts', 'report_network.py'))
rn = importlib.util.module_from_spec(spec)
sys.modules['report_network'] = rn
spec.loader.exec_module(rn)

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)

NMCLI = (
    'no:NETGEAR93:5C\\:35\\:FC\\:D7\\:9B\\:74:11:2462 MHz:40\n'
    'yes:happyhome206:F0\\:70\\:84\\:2C\\:CB\\:60:36:5180 MHz:84\n'
    'no:happyhome206:5C\\:35\\:FC\\:D7\\:9E\\:54:149:5745 MHz:62\n'
)
ADDRS = (
    '1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n'
    '3: wlan0    inet 192.168.0.104/24 brd 192.168.0.255 scope global dynamic noprefixroute wlan0\\       valid_lft 80000sec\n'
)
ROUTE = 'default via 192.168.0.1 dev wlan0 proto dhcp src 192.168.0.104 metric 600\n'
LINKS = (
    '1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000\n'
    '    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n'
    '2: eth0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq state DOWN mode DEFAULT group default qlen 1000\n'
    '    link/ether d8:3a:dd:11:22:33 brd ff:ff:ff:ff:ff:ff\n'
    '3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP mode DORMANT group default qlen 1000\n'
    '    link/ether D8:3A:DD:AA:BB:CC brd ff:ff:ff:ff:ff:ff\n'
)
NEIGH = (
    '192.168.0.1 dev wlan0 lladdr F0:70:84:2C:CB:60 router REACHABLE\n'
    '192.168.0.55 dev wlan0  FAILED\n'
    '192.168.0.7 dev wlan0 lladdr aa:bb:cc:dd:ee:07 STALE\n'
    '192.168.0.23 dev wlan0 lladdr aa:bb:cc:dd:ee:23 DELAY\n'
    '192.168.0.24 dev wlan0 lladdr aa:bb:cc:dd:ee:24 PROBE\n'
    '192.168.0.99 dev wlan0  INCOMPLETE\n'
    '192.168.0.104 dev wlan0 lladdr d8:3a:dd:aa:bb:cc PERMANENT\n'
    '192.168.0.3 dev wlan0 lladdr aa:bb:cc:dd:ee:03 REACHABLE\n'
)
JOURNAL = (
    'netwatch: cannot reach 192.168.0.1 (consecutive failures: 1)\n'
    'netwatch: cannot reach 192.168.0.1 (consecutive failures: 2)\n'
    'netwatch: bouncing Wi-Fi\n'
    'netwatch: gateway 192.168.0.1 reachable again after 2 failed check(s)\n'
)


def fake_run(outputs):
    """A _run that answers from {tool: stdout} - `ip` keyed by its object
    (addr / route / link / neigh) - and "" for anything else, like the real
    one does for a tool that is not there."""
    def run(cmd, timeout=5):
        key = cmd[0]
        if key == 'ip':
            key = next(w for w in cmd[1:] if not w.startswith('-'))
        return outputs.get(key, '')
    return run


ALL_TOOLS = {'nmcli': NMCLI, 'addr': ADDRS, 'route': ROUTE, 'link': LINKS, 'neigh': NEIGH, 'journalctl': JOURNAL}


class WifiParseTests(unittest.TestCase):
    def test_active_row_is_the_link_and_escaped_colons_are_unescaped(self):
        link = rn.parse_wifi(NMCLI)
        self.assertEqual(link, {'ssid': 'happyhome206', 'bssid': 'f0:70:84:2c:cb:60',
                                'channel': 36, 'freq_mhz': 5180, 'signal_pct': 84})

    def test_inactive_rows_only_or_no_wifi_means_no_link(self):
        inactive = '\n'.join(line for line in NMCLI.splitlines() if not line.startswith('yes:'))
        self.assertIsNone(rn.parse_wifi(inactive))
        self.assertIsNone(rn.parse_wifi(''))
        self.assertIsNone(rn.parse_wifi('garbage\nno:x\n'))

    def test_ssid_with_a_colon_survives(self):
        link = rn.parse_wifi('yes:Cafe\\: Upstairs:AA\\:BB\\:CC\\:DD\\:EE\\:FF:6:2437 MHz:50\n')
        self.assertEqual((link['ssid'], link['bssid'], link['channel']), ('Cafe: Upstairs', 'aa:bb:cc:dd:ee:ff', 6))


class SelfTests(unittest.TestCase):
    def test_wlan0_wins_then_the_default_route_interface(self):
        self.assertEqual(rn.self_interface(ADDRS, ROUTE), ('wlan0', '192.168.0.104'))
        wired = '2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\\       valid_lft forever\n'
        self.assertEqual(rn.self_interface(wired, 'default via 10.0.0.1 dev eth0 proto dhcp\n'), ('eth0', '10.0.0.5'))
        self.assertEqual(rn.self_interface('', ''), (None, None))
        self.assertEqual(rn.self_interface(wired, 'default via 10.0.0.1 dev wlan1\n'), (None, None))

    def test_mac_of_the_interface_lowercased(self):
        self.assertEqual(rn.iface_mac(LINKS, 'wlan0'), 'd8:3a:dd:aa:bb:cc')
        self.assertEqual(rn.iface_mac(LINKS, 'eth0'), 'd8:3a:dd:11:22:33')
        self.assertIsNone(rn.iface_mac(LINKS, 'wlan1'))
        self.assertIsNone(rn.iface_mac('', None))

    def test_netwatch_counter_and_journal(self):
        with tempfile.TemporaryDirectory() as d:
            failures = os.path.join(d, 'failures')
            with open(failures, 'w') as f:
                f.write('2\n')
            status = rn.netwatch_status(failures, JOURNAL)
            self.assertEqual(status, {'failures_now': 2, 'blips_this_boot': 4,
                                      'last_line': 'netwatch: gateway 192.168.0.1 reachable again after 2 failed check(s)'})
            # no counter file (fresh boot, or no netwatch) = 0, silent journal = nothing
            self.assertEqual(rn.netwatch_status(os.path.join(d, 'missing'), ''),
                             {'failures_now': 0, 'blips_this_boot': 0, 'last_line': None})

    def test_uptime_from_proc(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('12345.67 9999.00\n')
        try:
            self.assertEqual(rn.read_uptime_s(f.name), 12345)
        finally:
            os.unlink(f.name)
        self.assertIsNone(rn.read_uptime_s(os.path.join(tempfile.gettempdir(), 'no-such-uptime')))


class LanTests(unittest.TestCase):
    def test_neigh_rows_need_a_lladdr_and_a_live_state_macs_lowercase(self):
        devices = rn.parse_neigh(NEIGH)
        self.assertEqual([(d['ip'], d['mac'], d['state']) for d in devices], [
            ('192.168.0.1', 'f0:70:84:2c:cb:60', 'REACHABLE'),
            ('192.168.0.7', 'aa:bb:cc:dd:ee:07', 'STALE'),
            ('192.168.0.23', 'aa:bb:cc:dd:ee:23', 'DELAY'),
            ('192.168.0.24', 'aa:bb:cc:dd:ee:24', 'PROBE'),
            ('192.168.0.3', 'aa:bb:cc:dd:ee:03', 'REACHABLE'),
        ])

    def test_sweep_pings_every_host_of_the_one_slash_24(self):
        calls = []
        with mock.patch.object(rn, '_run', side_effect=lambda cmd, timeout=5: calls.append(cmd) or ''):
            rn.ping_sweep('192.168.0')
        targets = sorted(c[-1] for c in calls)
        self.assertEqual(len(calls), 254)
        self.assertEqual(targets, sorted(f'192.168.0.{n}' for n in range(1, 255)))
        self.assertTrue(all(c[:3] == ['ping', '-c1', '-W1'] for c in calls))
        self.assertIsNone(rn.subnet_base('not an ip'))
        self.assertEqual(rn.subnet_base('10.1.2.3'), '10.1.2')

    def test_collect_lan_drops_self_sorts_by_ip_and_caps(self):
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': NEIGH})), \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            lan = rn.collect_lan('192.168.0.104', 'd8:3a:dd:aa:bb:cc', sweep=False)
        self.assertEqual([d['ip'] for d in lan], ['192.168.0.1', '192.168.0.3', '192.168.0.7', '192.168.0.23', '192.168.0.24'])
        self.assertTrue(all('name' not in d for d in lan))
        many = ''.join(f'10.0.{n // 256}.{n % 256} dev eth0 lladdr 02:00:00:00:{n // 256:02x}:{n % 256:02x} STALE\n'
                       for n in range(1, 700))
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': many})), \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            self.assertEqual(len(rn.collect_lan('10.0.0.1', None, sweep=False)), rn.MAX_LAN)

    def test_name_lookups_stop_at_the_time_cap(self):
        """A resolver that costs 2 s per call against a 5 s cap names the
        first three devices; the rest are left nameless, not waited for."""
        clock = {'t': 0.0}
        asked = []

        def resolver(ip, remaining):
            asked.append((ip, remaining))
            clock['t'] += 2.0
            return f'host-{ip.rsplit(".", 1)[1]}.local'
        devices = [{'ip': f'192.168.0.{n}', 'mac': 'x', 'state': 'STALE'} for n in range(1, 7)]
        rn.resolve_names(devices, resolver, cap_s=5, clock=lambda: clock['t'])
        self.assertEqual([d.get('name') for d in devices],
                         ['host-1.local', 'host-2.local', 'host-3.local', None, None, None])
        # each call is told how much of the cap is left, never more
        self.assertEqual([r for _ip, r in asked], [5.0, 3.0, 1.0])

    def test_avahi_output_and_absence(self):
        with mock.patch.object(rn, '_run', return_value='192.168.0.7\tkitchen.local\n'):
            self.assertEqual(rn.avahi_name('192.168.0.7', 5), 'kitchen.local')
        with mock.patch.object(rn, '_run', return_value=''):
            self.assertIsNone(rn.avahi_name('192.168.0.7', 5))


class RunTests(unittest.TestCase):
    def test_missing_binary_is_empty(self):
        self.assertEqual(rn._run(['definitely-not-a-binary-on-this-box-xyz', '--version']), '')

    def test_timeout_is_empty(self):
        self.assertEqual(rn._run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.3), '')

    def test_non_zero_exit_is_empty_and_success_is_stdout(self):
        self.assertEqual(rn._run([sys.executable, '-c', 'print("x"); raise SystemExit(3)']), '')
        self.assertEqual(rn._run([sys.executable, '-c', 'print("ok")']).strip(), 'ok')


class PayloadTests(unittest.TestCase):
    def test_shape_with_every_tool_present(self):
        with mock.patch.object(rn, '_run', side_effect=fake_run(ALL_TOOLS)), \
                mock.patch.object(rn, 'read_uptime_s', return_value=4242), \
                mock.patch.object(rn, 'NETWATCH_FAILURES_FILE', '/nonexistent/failures'), \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            self_info = rn.collect_self()
            lan = rn.collect_lan(self_info['ip'], self_info['mac'], sweep=False)
        p = rn.build_payload('kitchen', 'kitchen', self_info, lan, now=NOW)
        self.assertEqual(p['reported_at'], '2026-10-12T15:00:00+00:00')
        self.assertEqual(p['self'], {
            'ip': '192.168.0.104', 'mac': 'd8:3a:dd:aa:bb:cc', 'uptime_s': 4242,
            'link': {'ssid': 'happyhome206', 'bssid': 'f0:70:84:2c:cb:60', 'channel': 36, 'freq_mhz': 5180, 'signal_pct': 84},
            'netwatch': {'failures_now': 0, 'blips_this_boot': 4,
                         'last_line': 'netwatch: gateway 192.168.0.1 reachable again after 2 failed check(s)'},
        })
        self.assertEqual(len(p['lan']), 5)
        self.assertEqual(p['lan'][0], {'ip': '192.168.0.1', 'mac': 'f0:70:84:2c:cb:60', 'state': 'REACHABLE'})
        self.assertEqual(rn.find_secret_keys(p), [])

    def test_no_tools_at_all_degrades_to_nulls(self):
        with mock.patch.object(rn, '_run', return_value=''), \
                mock.patch.object(rn, 'read_uptime_s', return_value=None), \
                mock.patch.object(rn, 'NETWATCH_FAILURES_FILE', '/nonexistent/failures'):
            self_info = rn.collect_self()
            lan = rn.collect_lan(self_info['ip'], self_info['mac'])
        self.assertEqual(self_info, {'ip': None, 'mac': None, 'uptime_s': None, 'link': None,
                                     'netwatch': {'failures_now': 0, 'blips_this_boot': 0, 'last_line': None}})
        self.assertEqual(lan, [])

    def test_secret_guard_strips_keys_and_refuses_blobs(self):
        self_info = {'ip': '1.2.3.4', 'mac': None, 'uptime_s': 1, 'link': None,
                     'netwatch': {'failures_now': 0, 'blips_this_boot': 0, 'last_line': None},
                     'api_key': 'should-never-leave'}
        p = rn.build_payload('r', 'h', self_info, [], now=NOW)
        self.assertNotIn('api_key', p['self'])
        blob = 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVmZ2hpamtsbW5vcA=='
        with self.assertRaises(ValueError):
            rn.build_payload('r', 'h', self_info, [{'ip': '1.2.3.5', 'mac': 'aa', 'state': 'STALE', 'name': blob}], now=NOW)


class _Reply:
    def __init__(self, status, text=''):
        self.status_code = status
        self.text = text


class SendTests(unittest.TestCase):
    PAYLOAD = {'room': 'r', 'lan': [{'ip': '1', 'mac': 'a', 'state': 'STALE'}, {'ip': '2', 'mac': 'b', 'state': 'STALE'}]}

    def test_2xx_is_success_sends_the_key_header_and_logs_the_count(self):
        with mock.patch.object(rn.requests, 'post', return_value=_Reply(200)) as post, \
                self.assertLogs('report_network', level='INFO') as logs:
            self.assertTrue(rn.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(post.call_args.kwargs['headers'], {'X-API-Key': 'k'})
        self.assertEqual(post.call_args.kwargs['json'], self.PAYLOAD)
        self.assertIn('network: r -> 2 devices accepted', logs.output[-1])

    def test_4xx_is_final(self):
        with mock.patch.object(rn.requests, 'post', return_value=_Reply(401, 'nope')) as post, \
                mock.patch.object(rn.time, 'sleep'):
            self.assertFalse(rn.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(post.call_count, 1)

    def test_5xx_and_connection_errors_retry_then_give_up(self):
        replies = [rn.requests.ConnectionError(), _Reply(503), _Reply(200)]
        with mock.patch.object(rn.requests, 'post', side_effect=replies), \
                mock.patch.object(rn.time, 'sleep') as sleep:
            self.assertTrue(rn.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(sleep.call_count, 2)
        with mock.patch.object(rn.requests, 'post', return_value=_Reply(500)) as post, \
                mock.patch.object(rn.time, 'sleep'):
            self.assertFalse(rn.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(post.call_count, rn.RETRIES)


class MainTests(unittest.TestCase):
    def test_no_key_file_means_not_set_up_and_exit_0(self):
        with mock.patch.dict(os.environ, {'MUSICFIG_REPORT_KEY_FILE': '/nonexistent/key'}), \
                mock.patch.object(rn.requests, 'post') as post, \
                mock.patch.object(rn, '_run') as run, \
                self.assertLogs('report_network', level='INFO') as logs:
            self.assertEqual(rn.main([]), 0)
        post.assert_not_called()
        run.assert_not_called()
        self.assertIn('network reporting is not set up here', logs.output[-1])

    def test_unreadable_key_file_is_a_failure_not_silence(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {'MUSICFIG_REPORT_KEY_FILE': d}), \
                    mock.patch.object(rn.requests, 'post') as post:
                self.assertEqual(rn.main([]), 1)
            post.assert_not_called()

    def test_dry_run_prints_without_a_key_and_sends_nothing(self):
        env = {'MUSICFIG_REPORT_KEY_FILE': '/nonexistent/key', 'MUSICFIG_ROOM': 'kitchen'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(rn, '_run', return_value='') as run, \
                mock.patch.object(rn.requests, 'post') as post, \
                mock.patch('sys.stdout', new=io.StringIO()) as out:
            self.assertEqual(rn.main(['--dry-run']), 0)
        post.assert_not_called()
        self.assertIn('"room": "kitchen"', out.getvalue())
        self.assertIn('"lan": []', out.getvalue())
        # no address = nothing to sweep, so ping is never run
        self.assertFalse(any(c.args[0][0] == 'ping' for c in run.call_args_list))

    def test_no_sweep_skips_the_pings_but_still_reads_neighbours(self):
        with mock.patch.object(rn, '_run', side_effect=fake_run(ALL_TOOLS)) as run, \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            lan = rn.collect_lan('192.168.0.104', None, sweep=False)
        self.assertNotIn('ping', [c.args[0][0] for c in run.call_args_list])
        self.assertEqual(len(lan), 5)
        # and with the sweep on, the same address is pinged across its /24
        with mock.patch.object(rn, '_run', side_effect=fake_run(ALL_TOOLS)) as run, \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            rn.collect_lan('192.168.0.104', None, sweep=True)
        pinged = [c.args[0][-1] for c in run.call_args_list if c.args[0][0] == 'ping']
        self.assertEqual(len(pinged), 254)
        self.assertTrue(all(ip.startswith('192.168.0.') for ip in pinged))

    def test_send_path_uses_the_key_and_the_url_from_the_environment(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('the-key\n')
        try:
            env = {'MUSICFIG_REPORT_KEY_FILE': f.name, 'MUSICFIG_ROOM': 'kitchen',
                   'MUSICFIG_NETWORK_REPORT_URL': 'https://example.test/api/network/report'}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(rn, '_run', return_value=''), \
                    mock.patch.object(rn.requests, 'post', return_value=_Reply(200)) as post:
                self.assertEqual(rn.main(['--no-sweep']), 0)
        finally:
            os.unlink(f.name)
        self.assertEqual(post.call_args.args[0], 'https://example.test/api/network/report')
        self.assertEqual(post.call_args.kwargs['headers'], {'X-API-Key': 'the-key'})
        self.assertEqual(post.call_args.kwargs['json']['room'], 'kitchen')


if __name__ == '__main__':
    unittest.main()
