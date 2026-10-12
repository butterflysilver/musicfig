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
    def run(cmd, timeout=5, **_kw):
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

    def test_nmcli_reads_the_last_scan_for_wlan0_or_every_device_without_one(self):
        with mock.patch.object(rn, '_run', side_effect=fake_run(ALL_TOOLS)) as run, \
                mock.patch.object(rn, 'read_uptime_s', return_value=None):
            rn.collect_self()
        nmcli = [c.args[0] for c in run.call_args_list if c.args[0][0] == 'nmcli']
        self.assertEqual(nmcli, [['nmcli', '-t', '-f', 'ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL', 'dev', 'wifi', 'list',
                                  '--rescan', 'no', 'ifname', 'wlan0']])
        wired = dict(ALL_TOOLS, addr='2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\n',
                     route='default via 10.0.0.1 dev eth0 proto dhcp\n')
        with mock.patch.object(rn, '_run', side_effect=fake_run(wired)) as run, \
                mock.patch.object(rn, 'read_uptime_s', return_value=None):
            rn.collect_self()
        nmcli = [c.args[0] for c in run.call_args_list if c.args[0][0] == 'nmcli']
        self.assertEqual(nmcli[0][-2:], ['--rescan', 'no'])
        self.assertNotIn('ifname', nmcli[0])

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
        # a /24 can never hold more than 254 rows; the cap is belt and braces
        # against a neighbour table that lies (same ip, many macs)
        many = ''.join(f'10.0.0.{n % 254 + 1} dev eth0 lladdr 02:00:00:00:{n // 256:02x}:{n % 256:02x} STALE\n'
                       for n in range(0, 700))
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': many})), \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            self.assertEqual(len(rn.collect_lan('10.0.0.200', None, sweep=False)), rn.MAX_LAN)
        # no address of our own = no /24 to report, whatever the table holds
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': NEIGH})):
            self.assertEqual(rn.collect_lan(None, None, sweep=False), [])

    def test_collect_lan_keeps_only_the_pads_slash_24_and_one_row_per_device(self):
        """A dual-homed Pi with docker: 172.17.0.x containers and 10.x rows
        are not house devices, and a LAN device learned on both eth0 and
        wlan0 is one device, reported with its most alive state."""
        neigh = (
            '172.17.0.2 dev docker0 lladdr 02:42:ac:11:00:02 REACHABLE\n'
            '10.8.0.1 dev tun0 lladdr 00:11:22:33:44:55 STALE\n'
            '192.168.0.1 dev eth0 lladdr f0:70:84:2c:cb:60 STALE\n'
            '192.168.0.1 dev wlan0 lladdr f0:70:84:2c:cb:60 REACHABLE\n'
            '192.168.0.7 dev wlan0 lladdr aa:bb:cc:dd:ee:07 STALE\n'
            '192.168.0.7 dev eth0 lladdr aa:bb:cc:dd:ee:07 STALE\n'
            '192.168.0.146 dev wlan0 lladdr d8:3a:dd:00:01:46 REACHABLE\n'   # the Pi's own eth0, seen from wlan0
            '192.168.0.147 dev eth0 lladdr d8:3a:dd:00:01:47 REACHABLE\n'    # the Pi's own wlan0, seen from eth0
        )
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': neigh})), \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            lan = rn.collect_lan('192.168.0.147', 'd8:3a:dd:00:01:47', sweep=False)
        self.assertEqual(lan, [
            {'ip': '192.168.0.1', 'mac': 'f0:70:84:2c:cb:60', 'state': 'REACHABLE'},
            {'ip': '192.168.0.7', 'mac': 'aa:bb:cc:dd:ee:07', 'state': 'STALE'},
            {'ip': '192.168.0.146', 'mac': 'd8:3a:dd:00:01:46', 'state': 'REACHABLE'},
        ])

    def test_names_come_from_one_batched_avahi_call_capped_as_a_whole(self):
        """One avahi-resolve-address for every ip, bounded by RESOLVE_CAP_S
        in total and keeping the lines that arrived; the ips it did not
        answer for stay nameless rather than each cost their own timeout."""
        devices = [{'ip': f'192.168.0.{n}', 'mac': 'x', 'state': 'STALE'} for n in range(1, 7)]
        partial = '192.168.0.1\tgfiber.local\n192.168.0.3\tkitchen.local\n'
        with mock.patch.object(rn, '_run', return_value=partial) as run:
            rn.resolve_names(devices)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ['avahi-resolve-address', '-4'] + [d['ip'] for d in devices])
        self.assertEqual(run.call_args.kwargs, {'timeout': rn.RESOLVE_CAP_S, 'keep_partial': True})
        self.assertEqual([d.get('name') for d in devices],
                         ['gfiber.local', None, 'kitchen.local', None, None, None])
        # nothing came back (timed out before any line, or avahi absent) = nameless, not failed
        with mock.patch.object(rn, '_run', return_value=''):
            self.assertEqual(rn.resolve_names([{'ip': '1.2.3.4', 'mac': 'x', 'state': 'STALE'}]),
                             [{'ip': '1.2.3.4', 'mac': 'x', 'state': 'STALE'}])
        self.assertEqual(rn.avahi_names([]), {})

    def test_avahi_is_skipped_when_the_binary_is_absent(self):
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': NEIGH})) as run, \
                mock.patch.object(rn.shutil, 'which', return_value=None):
            rn.collect_lan('192.168.0.104', None, sweep=False)
        self.assertNotIn('avahi-resolve-address', [c.args[0][0] for c in run.call_args_list])
        with mock.patch.object(rn, '_run', side_effect=fake_run({'neigh': NEIGH, 'avahi-resolve-address': '192.168.0.7\tkitchen.local\n'})), \
                mock.patch.object(rn.shutil, 'which', return_value='/usr/bin/avahi-resolve-address'):
            lan = rn.collect_lan('192.168.0.104', None, sweep=False)
        self.assertEqual([d.get('name') for d in lan], [None, None, 'kitchen.local', None, None])


class RunTests(unittest.TestCase):
    def test_missing_binary_is_empty(self):
        self.assertEqual(rn._run(['definitely-not-a-binary-on-this-box-xyz', '--version']), '')

    def test_timeout_is_empty(self):
        self.assertEqual(rn._run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.3), '')

    def test_non_zero_exit_is_empty_and_success_is_stdout(self):
        self.assertEqual(rn._run([sys.executable, '-c', 'print("x"); raise SystemExit(3)']), '')
        self.assertEqual(rn._run([sys.executable, '-c', 'print("ok")']).strip(), 'ok')

    def test_failures_say_why_at_debug_level(self):
        with self.assertLogs('report_network', level='DEBUG') as logs:
            rn._run(['definitely-not-a-binary-on-this-box-xyz'])
            rn._run([sys.executable, '-c', 'import sys; sys.stderr.write("boom"); raise SystemExit(3)'])
            rn._run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.3)
        self.assertIn('definitely-not-a-binary-on-this-box-xyz: FileNotFoundError', logs.output[0])
        self.assertIn('exit 3 boom', logs.output[1])
        self.assertIn('timed out after 0.3s', logs.output[2])

    def test_keep_partial_keeps_what_arrived_before_a_timeout_or_a_bad_exit(self):
        """A batch lookup prints a line per hit and exits non-zero for the
        misses (or is still waiting on them at the cap): the hits count."""
        hits_then_fail = 'import sys; print("1.2.3.4\\thit.local"); sys.exit(1)'
        self.assertEqual(rn._run([sys.executable, '-c', hits_then_fail], keep_partial=True).strip(), '1.2.3.4\thit.local')
        hits_then_hang = 'import sys, time; print("1.2.3.4\\thit.local", flush=True); time.sleep(10)'
        self.assertEqual(rn._run([sys.executable, '-c', hits_then_hang], timeout=1, keep_partial=True).strip(),
                         '1.2.3.4\thit.local')
        self.assertEqual(rn._run([sys.executable, '-c', hits_then_fail]), '')


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
            rn.build_payload('r', 'h', dict(self_info, netwatch=dict(self_info['netwatch'], last_line=blob)), [], now=NOW)

    def test_an_odd_mdns_name_is_cut_or_dropped_never_refused(self):
        """A name the pad did not choose must not make it exit 1 every 5
        minutes: long names are cut to one DNS label, blob-looking ones
        dropped, and the device itself is still reported."""
        self_info = {'ip': None, 'mac': None, 'uptime_s': None, 'link': None,
                     'netwatch': {'failures_now': 0, 'blips_this_boot': 0, 'last_line': None}}
        blob = 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVmZ2hpamtsbW5vcA=='   # 68 chars, no dots
        long_name = 'a-very-long-but-honest-printer-name-with-dashes-' + 'x' * 40 + '.local'
        lan = [{'ip': '1.2.3.5', 'mac': 'aa', 'state': 'STALE', 'name': blob},
               {'ip': '1.2.3.6', 'mac': 'ab', 'state': 'STALE', 'name': long_name},
               {'ip': '1.2.3.7', 'mac': 'ac', 'state': 'STALE', 'name': 'kitchen.local'}]
        p = rn.build_payload('r', 'h', self_info, lan, now=NOW)
        # cut to 63 chars nothing is long enough to look like a blob to the shared guard (64+)
        self.assertEqual([d.get('name') for d in p['lan']], [blob[:63], long_name[:63], 'kitchen.local'])
        self.assertEqual([d['mac'] for d in p['lan']], ['aa', 'ab', 'ac'])
        self.assertEqual(rn.find_secret_keys(p), [])
        self.assertEqual(lan[0]['name'], blob)  # the caller's list is not touched
        # and should the guard ever get stricter, a flagged name is dropped, not refused
        stricter = lambda value, path='': ['<value>'] if value == blob[:63] else rn.report_pad.find_secret_keys(value, path)  # noqa: E731
        with mock.patch.object(rn, 'find_secret_keys', side_effect=stricter):
            p = rn.build_payload('r', 'h', self_info, lan, now=NOW)
        self.assertEqual([d.get('name') for d in p['lan']], [None, long_name[:63], 'kitchen.local'])


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

    def test_unknown_flag_exits_2_and_sends_nothing(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('the-key\n')
        try:
            with mock.patch.dict(os.environ, {'MUSICFIG_REPORT_KEY_FILE': f.name}), \
                    mock.patch.object(rn, '_run', return_value='') as run, \
                    mock.patch.object(rn.requests, 'post') as post, \
                    mock.patch('sys.stderr', new=io.StringIO()):
                with self.assertRaises(SystemExit) as exit_:
                    rn.main(['--dry_run'])
        finally:
            os.unlink(f.name)
        self.assertEqual(exit_.exception.code, 2)
        post.assert_not_called()
        run.assert_not_called()

    def test_dry_run_logs_why_a_field_is_null(self):
        env = {'MUSICFIG_REPORT_KEY_FILE': '/nonexistent/key', 'MUSICFIG_ROOM': 'kitchen'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(rn.subprocess, 'run', side_effect=FileNotFoundError('no such tool')), \
                mock.patch('sys.stdout', new=io.StringIO()), \
                self.assertLogs('report_network', level='DEBUG') as logs:
            self.assertEqual(rn.main(['--dry-run']), 0)
        self.assertIn('nmcli: FileNotFoundError', '\n'.join(logs.output))
        self.assertIn('journalctl: FileNotFoundError', '\n'.join(logs.output))

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
