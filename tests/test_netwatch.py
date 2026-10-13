"""scripts/pi/netwatch.sh: the escalation ladder (log -> bounce Wi-Fi ->
reboot) driven with fake ip / ping / nmcli / systemctl / sleep on PATH, plus
the reboot backoff and the persistent log that survive a reboot, the link
gate on the reboot and the bounce deferral while a figure plays (life-ops #121).

Needs bash (Git Bash on Windows, /bin/bash on a Pi); skipped without one.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'pi', 'netwatch.sh')
BASH = shutil.which('bash')


@unittest.skipUnless(BASH, 'bash is not available')
class NetwatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='netwatch-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, 'bin')
        self.state = os.path.join(self.tmp, 'state')
        self.calls = os.path.join(self.tmp, 'calls.log')
        self.uptime = os.path.join(self.tmp, 'uptime')
        # the two files that survive a reboot; their dir does not exist yet
        self.persist = os.path.join(self.tmp, 'var', 'netwatch-reboots')
        self.log = os.path.join(self.tmp, 'var', 'netwatch.log')
        os.makedirs(self.bin)
        self.set_uptime(5000)
        self.gateway_up = os.path.join(self.tmp, 'gateway-up')
        self.set_gateway(up=True)
        # The two marker files musicfig / NetworkManager would provide: the
        # link is down and nothing is playing unless a test says otherwise.
        self.link_up = os.path.join(self.tmp, 'link-up')
        self.playing = os.path.join(self.tmp, 'run', 'playing')
        os.makedirs(os.path.dirname(self.playing))
        # Every fake appends its name and arguments to calls.log; ping's exit
        # status follows the gateway-up marker file so a test can flip it, and
        # nmcli's `dev show` answer follows the link-up one.
        self.fake('ip', 'echo "default via 192.168.1.1 dev wlan0 proto dhcp"')
        self.fake('ping', '[ -e "$GATEWAY_UP" ]')
        self.fake('nmcli', 'case "$*" in *"dev show"*) if [ -e "$LINK_UP" ]; then '
                           'echo "GENERAL.STATE:100 (connected)"; else '
                           'echo "GENERAL.STATE:30 (disconnected)"; fi;; esac')
        self.fake('systemctl', 'true')
        self.fake('sleep', 'true')

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, 'w', newline='\n') as f:
            f.write('#!/usr/bin/env bash\n'
                    'echo "%s $*" >> "$CALLS_LOG"\n%s\n' % (name, body))
        os.chmod(path, 0o755)

    def set_uptime(self, seconds):
        with open(self.uptime, 'w') as f:
            f.write('%d.00 %d.00\n' % (seconds, seconds * 3))

    def set_gateway(self, up):
        self._marker(self.gateway_up, up)

    def set_link(self, up):
        self._marker(self.link_up, up)

    def set_playing(self, playing):
        self._marker(self.playing, playing)

    def _marker(self, path, present):
        if present:
            open(path, 'w').close()
        elif os.path.exists(path):
            os.remove(path)

    def run_check(self, expect_rc=0, **env_overrides):
        env = dict(os.environ)
        env['PATH'] = self.bin + os.pathsep + env.get('PATH', '')
        env.update({
            'CALLS_LOG': self.calls,
            'GATEWAY_UP': self.gateway_up,
            'LINK_UP': self.link_up,
            'NETWATCH_STATE_DIR': self.state,
            'NETWATCH_STATE_PERSIST': self.persist,
            'NETWATCH_LOG': self.log,
            'NETWATCH_UPTIME_FILE': self.uptime,
            'NETWATCH_PLAYING_FILE': self.playing,
        })
        env.update(env_overrides)
        proc = subprocess.run([BASH, SCRIPT], env=env, capture_output=True, text=True)
        if expect_rc is not None:
            self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return proc.stdout

    def calls_made(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [line.strip() for line in f if line.strip()]

    def failures(self):
        return self.counter('failures')

    def counter(self, name):
        path = os.path.join(self.state, name)
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            return int(f.read().strip())

    def set_counter(self, name, n):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, name), 'w') as f:
            f.write('%d\n' % n)

    def reboots(self):
        if not os.path.exists(self.persist):
            return 0
        with open(self.persist) as f:
            return int(f.read().strip())

    def set_reboots(self, n):
        os.makedirs(os.path.dirname(self.persist), exist_ok=True)
        with open(self.persist, 'w') as f:
            f.write('%d\n' % n)

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [line.rstrip('\n') for line in f if line.strip()]

    def simulate_reboot(self):
        """What a reboot does to the watchdog's world: the tmpfs counter is
        gone, the files under /var/lib stay, and the box is freshly up."""
        shutil.rmtree(self.state, ignore_errors=True)
        self.set_uptime(0)

    def test_reachable_gateway_resets_the_counter_quietly(self):
        out = self.run_check()
        self.assertEqual(out, '')
        self.assertEqual(self.failures(), 0)
        self.assertNotIn('nmcli radio wifi off', self.calls_made())
        self.assertNotIn('systemctl reboot', self.calls_made())

    def test_pings_the_default_gateway(self):
        self.run_check()
        self.assertIn('ping -c 3 -W 2 192.168.1.1', self.calls_made())

    def test_explicit_target_wins_over_the_route(self):
        self.run_check(NETWATCH_TARGET='10.0.0.9')
        self.assertIn('ping -c 3 -W 2 10.0.0.9', self.calls_made())
        self.assertFalse([c for c in self.calls_made() if c.startswith('ip ')])

    def test_first_miss_only_logs(self):
        self.set_gateway(up=False)
        out = self.run_check()
        self.assertIn('consecutive failures: 1', out)
        self.assertEqual(self.failures(), 1)
        self.assertNotIn('nmcli radio wifi off', self.calls_made())
        self.assertNotIn('systemctl reboot', self.calls_made())

    def test_second_miss_bounces_wifi(self):
        self.set_gateway(up=False)
        self.run_check()
        out = self.run_check()
        self.assertIn('bouncing Wi-Fi', out)
        calls = self.calls_made()
        off, on = calls.index('nmcli radio wifi off'), calls.index('nmcli radio wifi on')
        self.assertLess(off, on)
        self.assertIn('sleep 5', calls[off:on])
        self.assertNotIn('systemctl reboot', calls)

    def test_fourth_miss_reboots_once_uptime_allows(self):
        self.set_gateway(up=False)
        for _ in range(3):
            self.run_check()
        self.assertNotIn('systemctl reboot', self.calls_made())
        out = self.run_check()
        self.assertIn('rebooting: 4 consecutive failures', out)
        self.assertIn('systemctl reboot', self.calls_made())

    def test_no_reboot_within_min_uptime(self):
        self.set_gateway(up=False)
        self.set_uptime(600)
        for _ in range(4):
            out = self.run_check()
        self.assertIn('would reboot but only up 600s', out)
        self.assertNotIn('systemctl reboot', self.calls_made())
        # still keeps trying the cheaper fix
        self.assertEqual(self.calls_made().count('nmcli radio wifi off'), 3)
        # and reboots as soon as the box has been up long enough
        self.set_uptime(1300)
        out = self.run_check()
        self.assertIn('rebooting: 5 consecutive failures', out)
        self.assertIn('systemctl reboot', self.calls_made())

    def test_recovery_resets_the_ladder(self):
        self.set_gateway(up=False)
        for _ in range(3):
            self.run_check()
        self.set_gateway(up=True)
        out = self.run_check()
        self.assertIn('reachable again after 3 failed check(s)', out)
        self.assertEqual(self.failures(), 0)
        self.set_gateway(up=False)
        out = self.run_check()
        self.assertIn('consecutive failures: 1', out)
        self.assertNotIn('systemctl reboot', self.calls_made())

    def test_no_default_route_counts_as_a_miss_without_pinging(self):
        self.fake('ip', 'true')
        out = self.run_check()
        self.assertIn('no default route', out)
        self.assertEqual(self.failures(), 1)
        self.assertFalse([c for c in self.calls_made() if c.startswith('ping ')])

    def test_failing_ip_counts_as_a_miss_instead_of_aborting(self):
        self.fake('ip', 'exit 2')
        out = self.run_check()
        self.assertIn('no default route', out)
        self.assertEqual(self.failures(), 1)

    def test_unwritable_state_dir_degrades_to_log_only(self):
        # a file where the directory should be: mkdir -p and the write both fail
        open(self.state, 'w').close()
        self.set_gateway(up=False)
        out = self.run_check()
        self.assertIn('cannot create', out)
        self.assertIn('cannot write', out)
        self.assertIn('consecutive failures: 1', out)

    def test_wifi_is_switched_back_on_if_the_bounce_is_interrupted(self):
        self.set_gateway(up=False)
        self.run_check()
        self.fake('sleep', 'kill -TERM $PPID')   # the pause is where a kill would land
        self.run_check(expect_rc=None)           # the script dies of the signal...
        self.assertEqual(self.calls_made()[-1], 'nmcli radio wifi on')   # ...after the trap

    def test_leading_zero_counters_do_not_kill_the_run(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, 'failures'), 'w') as f:
            f.write('08' + chr(10))
        os.makedirs(os.path.dirname(self.persist), exist_ok=True)
        with open(self.persist, 'w') as f:
            f.write('09' + chr(10))
        self.set_gateway(up=False)
        out = self.run_check()
        self.assertIn('consecutive failures: 9', out)

    def test_garbage_in_the_state_file_is_treated_as_zero(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, 'failures'), 'w') as f:
            f.write('nonsense\n')
        self.set_gateway(up=False)
        out = self.run_check()
        self.assertIn('consecutive failures: 1', out)

    def test_thresholds_are_overridable(self):
        self.set_gateway(up=False)
        self.set_counter('bounces', 2)   # the link gate is not a threshold; satisfy it
        out = self.run_check(NETWATCH_BOUNCE_AT='1', NETWATCH_REBOOT_AT='1', NETWATCH_MIN_UPTIME='0')
        self.assertIn('rebooting: 1 consecutive failures', out)
        self.assertIn('systemctl reboot', self.calls_made())

    # --- reboot backoff + persistent log (life-ops #121) ---

    def test_reboot_counter_increments_across_simulated_reboots(self):
        self.set_gateway(up=False)
        self.assertEqual(self.reboots(), 0)
        for _ in range(4):
            out = self.run_check()
        self.assertIn('threshold 1200s, watchdog reboot #1', out)
        self.assertEqual(self.reboots(), 1)
        self.assertEqual(self.calls_made().count('systemctl reboot'), 1)
        self.simulate_reboot()
        self.set_uptime(2400)                 # 40 min: the doubled threshold
        for _ in range(4):
            out = self.run_check()
        self.assertIn('threshold 2400s, watchdog reboot #2', out)
        self.assertEqual(self.reboots(), 2)
        self.assertEqual(self.calls_made().count('systemctl reboot'), 2)

    def test_reboot_threshold_doubles_per_reboot_and_caps_at_8x(self):
        self.set_gateway(up=False)
        self.set_counter('bounces', 2)   # two bounces already tried, link still down
        # (consecutive watchdog reboots so far, uptime the next one needs)
        for reboots, need in ((0, 1200), (1, 2400), (2, 4800), (3, 9600), (4, 9600), (12, 9600)):
            self.set_reboots(reboots)
            self.set_uptime(need - 1)
            out = self.run_check(NETWATCH_REBOOT_AT='1')
            self.assertIn('would reboot but only up %ds (< %ds after %d watchdog reboot(s))' % (need - 1, need, reboots), out)
            self.assertNotIn('systemctl reboot', self.calls_made())
            self.set_uptime(need)
            out = self.run_check(NETWATCH_REBOOT_AT='1')
            self.assertIn('(threshold %ds, watchdog reboot #%d)' % (need, reboots + 1), out)
            self.assertEqual(self.calls_made().count('systemctl reboot'), 1)
            self.assertEqual(self.reboots(), reboots + 1)
            os.remove(self.calls)

    def test_first_good_ping_resets_the_reboot_backoff(self):
        self.set_gateway(up=False)
        for _ in range(4):
            self.run_check()
        self.assertEqual(self.reboots(), 1)
        self.simulate_reboot()
        self.set_gateway(up=True)
        out = self.run_check()
        self.assertIn('reachable after 1 watchdog reboot(s); backoff reset', out)
        self.assertEqual(self.reboots(), 0)
        # back to the base threshold afterwards
        self.set_gateway(up=False)
        self.set_uptime(1200)
        for _ in range(4):
            out = self.run_check()
        self.assertIn('threshold 1200s, watchdog reboot #1', out)

    def test_quiet_checks_do_not_touch_the_persistent_files(self):
        self.run_check()
        self.assertFalse(os.path.exists(self.persist))
        self.assertFalse(os.path.exists(self.log))
        self.set_gateway(up=False)
        self.run_check()                      # failure 1 is journal-only
        self.assertFalse(os.path.exists(self.log))

    def test_persistent_log_receives_timestamped_action_lines(self):
        self.set_gateway(up=False)
        for _ in range(4):
            self.run_check()
        self.simulate_reboot()
        self.set_gateway(up=True)
        self.run_check()
        lines = self.log_lines()
        stamp = r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} '
        for line in lines:
            self.assertRegex(line, stamp)
        self.assertEqual([re.sub(stamp, '', l).split(':')[0] for l in lines],
                         ['bouncing Wi-Fi', 'bouncing Wi-Fi', 'rebooting',
                          'gateway 192.168.1.1 reachable after 1 watchdog reboot(s); backoff reset'])
        # "would reboot" and "reachable again" lines land there too
        self.set_gateway(up=False)
        self.set_uptime(100)
        for _ in range(4):
            self.run_check()
        self.set_gateway(up=True)
        self.run_check()
        tail = [re.sub(stamp, '', l) for l in self.log_lines()[-3:]]
        self.assertTrue(tail[0].startswith('would reboot but only up 100s (< 1200s after 0 watchdog reboot(s))'), tail)
        self.assertEqual(tail[1], 'bouncing Wi-Fi')   # the cheaper fix is still tried after a skipped reboot
        self.assertEqual(tail[2], 'gateway 192.168.1.1 reachable again after 4 failed check(s)')

    def test_persistent_log_is_truncated_to_the_last_500_lines(self):
        os.makedirs(os.path.dirname(self.log))
        with open(self.log, 'w') as f:
            for i in range(600):
                f.write('2026-01-01 00:00:00 old line %d\n' % i)
        self.set_gateway(up=False)
        self.run_check()
        self.run_check()                      # the bounce appends line 601
        lines = self.log_lines()
        self.assertEqual(len(lines), 500)
        self.assertTrue(lines[-1].endswith(' bouncing Wi-Fi'))
        self.assertEqual(lines[0], '2026-01-01 00:00:00 old line 101')
        self.assertFalse(os.path.exists(self.log + '.tmp'))

    def test_unwritable_log_and_reboot_counter_do_not_break_the_ladder(self):
        # a file where the directory should be: mkdir -p and the writes fail
        blocker = os.path.join(self.tmp, 'ro')
        open(blocker, 'w').close()
        self.set_gateway(up=False)
        for _ in range(4):
            out = self.run_check(NETWATCH_LOG=os.path.join(blocker, 'netwatch.log'),
                                 NETWATCH_STATE_PERSIST=os.path.join(blocker, 'netwatch-reboots'))
        self.assertIn('cannot write', out)
        self.assertIn('rebooting: 4 consecutive failures', out)
        self.assertIn('threshold 1200s', out)
        self.assertEqual(self.calls_made().count('nmcli radio wifi off'), 2)
        self.assertEqual(self.calls_made().count('systemctl reboot'), 1)
        self.assertFalse(os.path.exists(self.log))

    # --- reboot only once two bounces have left the link down (life-ops #121) ---

    def test_link_state_is_read_from_nmcli_for_the_configured_interface(self):
        self.set_gateway(up=False)
        self.run_check(NETWATCH_IFACE='wlan1')
        self.assertIn('nmcli -t -f GENERAL.STATE dev show wlan1', self.calls_made())
        self.run_check()
        self.assertIn('nmcli -t -f GENERAL.STATE dev show wlan0', self.calls_made())

    def test_only_state_100_counts_as_the_link_being_up(self):
        # Every other NetworkManager state, no output and a failing or
        # missing nmcli all read as "down": with two bounces behind it that
        # means a reboot; "100 (connected)" means the mesh-fault line instead.
        cases = (
            ('echo "GENERAL.STATE:100 (connected)"', False),
            ('echo "GENERAL.STATE:30 (disconnected)"', True),
            ('echo "GENERAL.STATE:70 (connecting (getting IP configuration))"', True),
            ('echo "GENERAL.STATE:110 (deactivating)"', True),
            ('echo "GENERAL.STATE:120 (connection failed)"', True),
            ('echo "GENERAL.STATE:"', True),
            ('true', True),
            ('echo "Error: Device wlan0 not found." >&2; exit 10', True),
        )
        for body, reboots in cases:
            with self.subTest(body=body):
                shutil.rmtree(self.state, ignore_errors=True)
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                self.set_reboots(0)             # each simulated reboot would raise the backoff
                self.set_gateway(up=False)
                self.set_counter('bounces', 2)
                self.fake('nmcli', body)
                out = self.run_check(NETWATCH_REBOOT_AT='1')
                self.assertEqual('systemctl reboot' in self.calls_made(), reboots, out)
                self.assertEqual('mesh/uplink fault, not rebooting' in out, not reboots, out)

    def test_reboot_needs_two_bounces_without_the_link_coming_back(self):
        self.set_gateway(up=False)
        self.set_uptime(5000)
        out = self.run_check(NETWATCH_BOUNCE_AT='1', NETWATCH_REBOOT_AT='1')
        self.assertIn('would reboot but only 0 bounce(s) tried without the link returning (need 2)', out)
        self.assertEqual(self.counter('bounces'), 1)
        out = self.run_check(NETWATCH_BOUNCE_AT='1', NETWATCH_REBOOT_AT='1')
        self.assertIn('would reboot but only 1 bounce(s) tried', out)
        self.assertEqual(self.counter('bounces'), 2)
        self.assertNotIn('systemctl reboot', self.calls_made())
        out = self.run_check(NETWATCH_BOUNCE_AT='1', NETWATCH_REBOOT_AT='1')
        self.assertIn('rebooting: 3 consecutive failures, 2 bounces without the link returning', out)
        self.assertIn('systemctl reboot', self.calls_made())
        # the default ladder is unchanged for a wedged chip: 4 misses, 2 bounces, reboot
        shutil.rmtree(self.state)
        os.remove(self.calls)
        for _ in range(4):
            out = self.run_check()
        self.assertIn('rebooting: 4 consecutive failures, 2 bounces', out)
        self.assertEqual(self.calls_made().count('nmcli radio wifi off'), 2)
        self.assertEqual(self.calls_made().count('systemctl reboot'), 1)

    def test_link_up_without_gateway_is_a_mesh_fault_not_a_reboot(self):
        self.set_gateway(up=False)
        for _ in range(3):
            self.run_check()
        self.assertEqual(self.counter('bounces'), 2)
        # the chip reassociated (the AP is up) but nothing answers behind it
        self.set_link(up=True)
        out = self.run_check()
        self.assertIn('link on wlan0 is up again after 2 bounce(s)', out)
        self.assertIn('link up, gateway unreachable - mesh/uplink fault, not rebooting (4 consecutive failures)', out)
        self.assertNotIn('systemctl reboot', self.calls_made())
        self.assertIn('bouncing Wi-Fi', out)          # the cheap fix is still tried
        self.assertEqual(self.counter('bounces'), 1)  # ...and counted from zero again
        for _ in range(5):
            out = self.run_check()
        self.assertIn('mesh/uplink fault, not rebooting (9 consecutive failures)', out)
        self.assertNotIn('systemctl reboot', self.calls_made())
        self.assertEqual([l for l in self.log_lines() if 'mesh/uplink fault' in l][-1].split(' ', 2)[2],
                         'link up, gateway unreachable - mesh/uplink fault, not rebooting (9 consecutive failures)')
        # the AP goes away too: two more bounces without the link, then the reboot
        self.set_link(up=False)
        out = self.run_check()
        self.assertIn('would reboot but only 1 bounce(s) tried', out)
        out = self.run_check()
        self.assertIn('rebooting: 11 consecutive failures, 2 bounces', out)
        self.assertIn('systemctl reboot', self.calls_made())

    def test_recovery_resets_the_bounce_count(self):
        self.set_gateway(up=False)
        for _ in range(3):
            self.run_check()
        self.assertEqual(self.counter('bounces'), 2)
        self.set_gateway(up=True)
        self.run_check()
        self.assertEqual(self.counter('bounces'), 0)
        self.set_gateway(up=False)
        for _ in range(4):
            out = self.run_check()
        self.assertIn('rebooting: 4 consecutive failures, 2 bounces', out)

    # --- no bounce while a figure is playing (life-ops #121) ---

    def test_bounce_is_deferred_while_playing_at_most_three_times(self):
        self.set_gateway(up=False)
        self.set_playing(True)
        self.run_check()
        self.assertNotIn('systemctl is-active --quiet musicfig', self.calls_made())   # failure 1 never bounces
        for n in (1, 2, 3):
            out = self.run_check()
            self.assertIn('playing; bounce deferred (%d/3)' % n, out)
            self.assertEqual(self.counter('deferrals'), n)
            self.assertNotIn('nmcli radio wifi off', self.calls_made())
        # failure 4 would be the reboot step: no bounce has been tried, so no reboot either
        self.assertIn('would reboot but only 0 bounce(s) tried', out)
        self.assertNotIn('systemctl reboot', self.calls_made())
        out = self.run_check()
        self.assertIn('bouncing Wi-Fi', out)
        self.assertNotIn('bounce deferred', out)
        self.assertEqual(self.calls_made().count('nmcli radio wifi off'), 1)
        self.assertEqual(self.counter('deferrals'), 0)
        # ...and the next bounce gets its own three cycles of grace
        out = self.run_check()
        self.assertIn('playing; bounce deferred (1/3)', out)
        self.assertEqual(self.calls_made().count('nmcli radio wifi off'), 1)
        # the deferral lines are in the persistent log too
        self.assertEqual(sum(1 for l in self.log_lines() if l.endswith(' playing; bounce deferred (1/3)')), 2)

    def test_bounce_goes_ahead_once_playback_stops(self):
        self.set_gateway(up=False)
        self.set_playing(True)
        self.run_check()
        out = self.run_check()
        self.assertIn('bounce deferred (1/3)', out)
        self.set_playing(False)
        out = self.run_check()
        self.assertIn('bouncing Wi-Fi', out)
        self.assertEqual(self.counter('deferrals'), 0)

    def test_marker_without_a_running_musicfig_does_not_defer(self):
        # a stale marker from a musicfig that is not active means nothing
        self.fake('systemctl', '[ "$1" != is-active ]')
        self.set_gateway(up=False)
        self.set_playing(True)
        self.run_check()
        out = self.run_check()
        self.assertIn('systemctl is-active --quiet musicfig', self.calls_made())
        self.assertIn('bouncing Wi-Fi', out)
        self.assertNotIn('bounce deferred', out)
        # and without the marker the service is not even asked
        os.remove(self.calls)
        self.set_playing(False)
        self.run_check()
        self.assertNotIn('systemctl is-active --quiet musicfig', self.calls_made())

    def test_recovery_resets_the_deferrals(self):
        self.set_gateway(up=False)
        self.set_playing(True)
        self.run_check()
        self.run_check()
        self.run_check()
        self.assertEqual(self.counter('deferrals'), 2)
        self.set_gateway(up=True)
        self.run_check()
        self.assertEqual(self.counter('deferrals'), 0)


if __name__ == '__main__':
    unittest.main()
