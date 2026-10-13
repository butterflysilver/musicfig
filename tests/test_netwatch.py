"""scripts/pi/netwatch.sh: the escalation ladder (log -> bounce Wi-Fi ->
reboot) driven with fake ip / ping / nmcli / systemctl / sleep on PATH, plus
the reboot backoff and the persistent log that survive a reboot (life-ops #121).

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
        # Every fake appends its name and arguments to calls.log; ping's exit
        # status follows the gateway-up marker file so a test can flip it.
        self.fake('ip', 'echo "default via 192.168.1.1 dev wlan0 proto dhcp"')
        self.fake('ping', '[ -e "$GATEWAY_UP" ]')
        self.fake('nmcli', 'true')
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
        if up:
            open(self.gateway_up, 'w').close()
        elif os.path.exists(self.gateway_up):
            os.remove(self.gateway_up)

    def run_check(self, expect_rc=0, **env_overrides):
        env = dict(os.environ)
        env['PATH'] = self.bin + os.pathsep + env.get('PATH', '')
        env.update({
            'CALLS_LOG': self.calls,
            'GATEWAY_UP': self.gateway_up,
            'NETWATCH_STATE_DIR': self.state,
            'NETWATCH_STATE_PERSIST': self.persist,
            'NETWATCH_LOG': self.log,
            'NETWATCH_UPTIME_FILE': self.uptime,
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
        with open(os.path.join(self.state, 'failures')) as f:
            return int(f.read().strip())

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

    def test_garbage_in_the_state_file_is_treated_as_zero(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, 'failures'), 'w') as f:
            f.write('nonsense\n')
        self.set_gateway(up=False)
        out = self.run_check()
        self.assertIn('consecutive failures: 1', out)

    def test_thresholds_are_overridable(self):
        self.set_gateway(up=False)
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


if __name__ == '__main__':
    unittest.main()
