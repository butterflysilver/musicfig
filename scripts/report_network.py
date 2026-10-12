#!/usr/bin/env python
"""Report this pad's view of the house network to gsd-cloud (the Network page).

Every pad is a sensor on the Wi-Fi: it knows which mesh node it hangs off
(the BSSID), how strong the link is, what the network watchdog has seen this
boot, and - after a quick ping sweep of its own /24 - which devices answer
on the LAN. This gathers all of that into one small payload and POSTs it to
gsd-cloud, which shows every device across every room and whether the mesh
and the pads are healthy.

Nothing here can carry a secret (MACs, IPs, names), but the same guard the
figure report runs (scripts/report_pad.py) runs anyway: secret-looking keys
are stripped and the script refuses to send a payload that still holds one.

Every external command goes through _run(), which returns "" on any failure
(missing binary, timeout, non-zero exit) and logs the reason at debug level,
so a Pi that lacks a tool - or a laptop with none of them - reports nulls and
an empty LAN instead of crashing, and --dry-run says why each null is null.
`lan` holds only the pad's own /24 (docker bridges and other subnets in the
neighbour table are ignored), without the pad itself (it is `self`), one row
per (ip, mac) with the best neighbour state, sorted by IP and capped at
MAX_LAN rows. The sweep never touches anything but that one /24.

Usage:
    report_network.py             send (exit 0 on success, 1 on failure)
    report_network.py --dry-run   print the payload instead of sending it
    report_network.py --no-sweep  skip the ping sweep (neighbour table only)
    (an unknown flag exits 2 without sending anything)

Environment (musicfig-network.service sets the same ones musicfig-report
uses): MUSICFIG_ROOM (default: the hostname), MUSICFIG_NETWORK_REPORT_URL
(default https://gsd-cloud.vercel.app/api/network/report),
MUSICFIG_REPORT_KEY_FILE (default /etc/musicfig/pad-report.key; no file =
reporting is not set up on this pad, exit 0 quietly).
"""
import argparse
import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name):
    """Load a sibling script by file (the scripts dir is not a package)."""
    spec = importlib.util.spec_from_file_location(f'report_network_{name}', os.path.join(SCRIPTS_DIR, f'{name}.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_pad = _load_sibling('report_pad')
strip_secrets = report_pad.strip_secrets
find_secret_keys = report_pad.find_secret_keys
read_key = report_pad.read_key

logger = logging.getLogger('report_network')

DEFAULT_REPORT_URL = 'https://gsd-cloud.vercel.app/api/network/report'
DEFAULT_KEY_FILE = '/etc/musicfig/pad-report.key'
UPTIME_FILE = '/proc/uptime'
NETWATCH_FAILURES_FILE = '/run/musicfig-netwatch/failures'
TIMEOUT_S = 20
RETRIES = 3

WIFI_IFACE = 'wlan0'
# --rescan no: read NetworkManager's last scan instead of triggering one
# every 5 minutes (a scan drops the link for a moment on some chips).
NMCLI_WIFI = ['nmcli', '-t', '-f', 'ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL', 'dev', 'wifi', 'list', '--rescan', 'no']
# Neighbour states that mean "we saw this MAC answer", best first; FAILED /
# INCOMPLETE rows are addresses nobody answered for and carry no lladdr anyway.
NEIGH_STATES = ('REACHABLE', 'DELAY', 'PROBE', 'STALE')
SWEEP_WORKERS = 64
SWEEP_CAP_S = 6
RESOLVE_CAP_S = 5
# avahi gives its own resolvers RESOLVE_CAP_S before it reports the misses
# and exits; the grace lets it exit by itself (stdout flushed) rather than
# be killed at the cap with its buffered hits lost.
RESOLVE_GRACE_S = 2
MAX_LAN = 512
MAX_NAME_LEN = 63


def _text(value) -> str:
    """str from what subprocess hands back. TimeoutExpired.stdout is BYTES
    on POSIX even with text=True (only Windows gives str)."""
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value if isinstance(value, str) else ''


def _run(cmd, timeout=5, keep_partial=False, quiet=False) -> str:
    """stdout of `cmd`, or "" on any failure: missing binary, timeout,
    non-zero exit. Nothing in this script may crash because a tool is
    absent; a missing piece just becomes null / empty in the payload, and
    the reason goes to the debug log (--dry-run shows it) unless quiet=True
    (a sweep's expected misses would bury the real reasons).

    keep_partial=True returns the stdout of a non-zero exit instead of "":
    for a batch lookup whose hits are each a line and whose misses only
    cost the exit status. On a timeout it returns what the child had
    flushed before the kill, which for a printf-buffered tool is nothing -
    so give such a tool a timeout past its own, and the exit-status path."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        if not quiet:
            logger.debug('%s: timed out after %ss', cmd[0], timeout)
        return _text(e.stdout) if keep_partial else ''
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        if not quiet:
            logger.debug('%s: %s', cmd[0], e.__class__.__name__)
        return ''
    if out.returncode != 0:
        if not quiet:
            logger.debug('%s: exit %d %s', cmd[0], out.returncode, _text(out.stderr)[:200].strip())
        return _text(out.stdout) if keep_partial else ''
    return _text(out.stdout)


def _int(text) -> Optional[int]:
    try:
        return int(str(text).strip().split()[0])
    except (ValueError, IndexError, AttributeError):
        return None


def _split_terse(line: str) -> list:
    """Fields of one `nmcli -t` line. Terse mode separates fields with ':'
    and escapes a ':' inside a value (BSSIDs, some SSIDs) as '\\:'."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ':':
            fields.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append(''.join(cur))
    return fields


def parse_wifi(text: str) -> Optional[dict]:
    """The link we are on from `nmcli -t -f ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL
    dev wifi`: the ACTIVE=yes row, or None when there is none (Ethernet, no
    Wi-Fi, no nmcli). FREQ comes as "5220 MHz"."""
    for line in text.splitlines():
        fields = _split_terse(line.strip())
        if len(fields) < 6 or fields[0] != 'yes':
            continue
        _active, ssid, bssid, chan, freq, signal = fields[:6]
        return {
            'ssid': ssid or None,
            'bssid': bssid.lower() or None,
            'channel': _int(chan),
            'freq_mhz': _int(freq),
            'signal_pct': _int(signal),
        }
    return None


def parse_addrs(text: str) -> dict:
    """{iface: ip} from `ip -4 -o addr show` (first address per interface)."""
    addrs = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == 'inet':
            addrs.setdefault(parts[1], parts[3].split('/')[0])
    return addrs


def own_ips(addr_text: str) -> set:
    """Every IPv4 address this box has, from `ip -4 -o addr show`."""
    return {parts[3].split('/')[0] for parts in (line.split() for line in addr_text.splitlines())
            if len(parts) >= 4 and parts[2] == 'inet'}


def own_macs(link_text: str) -> set:
    """Every lowercase link/ether MAC this box has, from `ip link show`."""
    return {parts[1].lower() for parts in (line.split() for line in link_text.splitlines())
            if len(parts) >= 2 and parts[0] == 'link/ether'}


def default_route_iface(text: str) -> Optional[str]:
    """The `dev` of the first default route in `ip -4 route show default`."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == 'default' and 'dev' in parts:
            i = parts.index('dev')
            if i + 1 < len(parts):
                return parts[i + 1]
    return None


def self_interface(addr_text: str, route_text: str):
    """(iface, ip) of the pad: wlan0 when it has an address, else whatever
    interface carries the default route. (None, None) when neither."""
    addrs = parse_addrs(addr_text)
    if addrs.get(WIFI_IFACE):
        return WIFI_IFACE, addrs[WIFI_IFACE]
    iface = default_route_iface(route_text)
    if iface and addrs.get(iface):
        return iface, addrs[iface]
    return None, None


def iface_mac(link_text: str, iface: Optional[str]) -> Optional[str]:
    """The lowercase MAC of `iface` from `ip link show`."""
    if not iface:
        return None
    current = None
    for line in link_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(':').isdigit():
            current = parts[1].rstrip(':').split('@')[0]
        elif current == iface and len(parts) >= 2 and parts[0] == 'link/ether':
            return parts[1].lower()
    return None


def read_uptime_s(path: str = None) -> Optional[int]:
    try:
        with open(path or UPTIME_FILE, 'r', encoding='utf-8') as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError) as e:
        logger.debug('%s: %s', path or UPTIME_FILE, e.__class__.__name__)
        return None


def netwatch_status(failures_file: str = None, journal: str = None) -> dict:
    """What the network watchdog (scripts/pi/netwatch.sh) has seen: the
    consecutive-failure counter it keeps in tmpfs (0 when absent) and the
    "netwatch:" lines it logged this boot."""
    failures = 0
    try:
        with open(failures_file or NETWATCH_FAILURES_FILE, 'r', encoding='utf-8') as f:
            digits = ''.join(ch for ch in f.read() if ch.isdigit())
        failures = int(digits) if digits else 0
    except (OSError, ValueError) as e:
        logger.debug('%s: %s (failures_now = 0)', failures_file or NETWATCH_FAILURES_FILE, e.__class__.__name__)
        failures = 0
    if journal is None:
        journal = _run(['journalctl', '-u', 'musicfig-netwatch', '-b', '-o', 'cat'], timeout=10)
    blips = [line.strip() for line in journal.splitlines() if 'netwatch:' in line]
    return {
        'failures_now': failures,
        'blips_this_boot': len(blips),
        'last_line': blips[-1] if blips else None,
    }


def subnet_base(ip: str) -> Optional[str]:
    """'192.168.0' for 192.168.0.104: the one /24 the sweep may touch."""
    parts = ip.split('.')
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return None
    return '.'.join(parts[:3])


def ping_sweep(base: str, cap_s: float = SWEEP_CAP_S, workers: int = SWEEP_WORKERS) -> None:
    """One `ping -c1 -W1` at every host of base.0/24, concurrently, so the
    kernel's neighbour table holds everything that answers before `ip neigh`
    is read. The replies themselves are not needed. Whatever has not finished
    at the cap is abandoned (each ping gives up on its own within ~1 s)."""
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(_run, ['ping', '-c1', '-W1', f'{base}.{n}'], timeout=2, quiet=True)
               for n in range(1, 255)]
    try:
        wait(futures, timeout=cap_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def parse_neigh(text: str) -> list:
    """Devices from `ip -4 neigh`: rows with a lladdr whose state is one of
    NEIGH_STATES, MACs lowercase."""
    devices = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or 'lladdr' not in parts:
            continue
        i = parts.index('lladdr')
        if i + 1 >= len(parts):
            continue
        state = parts[-1]
        if state not in NEIGH_STATES:
            continue
        devices.append({'ip': parts[0], 'mac': parts[i + 1].lower(), 'state': state})
    return devices


def _device_key(device: dict):
    """Sort key: by address, then best neighbour state first, so a device
    the kernel saw on two interfaces keeps its most alive row when deduped."""
    parts = device['ip'].split('.')
    return (tuple(int(p) if p.isdigit() else 0 for p in parts), NEIGH_STATES.index(device['state']))


def avahi_names(ips: list, timeout: float = RESOLVE_CAP_S + RESOLVE_GRACE_S) -> dict:
    """{ip: mDNS name} from ONE `avahi-resolve-address -4 ip ip ...`: avahi
    resolves them all in parallel, prints a `ip<TAB>name` line per hit,
    gives up on the misses after its own ~RESOLVE_CAP_S and exits non-zero,
    so the hits are kept whatever the exit status. The timeout is the hard
    cap past that: a kill there loses whatever avahi had not flushed."""
    if not ips:
        return {}
    out = _run(['avahi-resolve-address', '-4', *ips], timeout=timeout, keep_partial=True)
    names = {}
    for line in out.splitlines():
        parts = line.split('\t') if '\t' in line else line.split()
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            names.setdefault(parts[0].strip(), parts[1].strip())
    return names


def resolve_names(devices: list, lookup: Callable[[list], dict] = avahi_names) -> list:
    """Add `name` to each device the lookup knows, in place; the rest stay
    nameless rather than hold the report."""
    names = lookup([d['ip'] for d in devices])
    for device in devices:
        name = names.get(device['ip'])
        if name:
            device['name'] = name
    return devices


def collect_self():
    """(self record, every ip this box has, every MAC this box has). The
    record is the payload's `self`; the sets let collect_lan leave out the
    box's other interfaces (a dual-homed Pi sees its own eth0 from wlan0)."""
    addr_text = _run(['ip', '-4', '-o', 'addr', 'show'])
    link_text = _run(['ip', 'link', 'show'])
    iface, ip = self_interface(addr_text, _run(['ip', '-4', 'route', 'show', 'default']))
    # Ask about wlan0 alone when that is the link; a Pi without one (or on
    # Ethernet) gets the unfiltered list, which still has the ACTIVE row.
    wifi_cmd = NMCLI_WIFI + (['ifname', WIFI_IFACE] if iface == WIFI_IFACE else [])
    record = {
        'ip': ip,
        'mac': iface_mac(link_text, iface),
        'uptime_s': read_uptime_s(),
        'link': parse_wifi(_run(wifi_cmd, timeout=10)),
        'netwatch': netwatch_status(),
    }
    return record, own_ips(addr_text), own_macs(link_text)


def collect_lan(self_ip: Optional[str], own_ip_set=(), own_mac_set=(), sweep: bool = True) -> list:
    """The devices on the pad's own /24 and nothing else: a Pi with docker
    bridges or a second NIC has neighbour rows for other subnets, and a LAN
    device learned on two interfaces appears once per interface, so rows
    are kept to the pad's /24 and deduped on (ip, mac), best state first.
    Rows are not filtered by interface on purpose - on a dual-homed Pi the
    sweep's answers can land on the other NIC's neighbour table. Every
    address and MAC of the box itself (own_ip_set / own_mac_set) is left
    out; `self_ip` is always one of them."""
    base = subnet_base(self_ip) if self_ip else None
    if not base:
        return []
    if sweep:
        ping_sweep(base)
    skip_ips, skip_macs = set(own_ip_set) | {self_ip}, set(own_mac_set)
    devices = [d for d in parse_neigh(_run(['ip', '-4', 'neigh']))
               if subnet_base(d['ip']) == base and d['ip'] not in skip_ips and d['mac'] not in skip_macs]
    devices.sort(key=_device_key)
    seen, unique = set(), []
    for device in devices:
        if (device['ip'], device['mac']) not in seen:
            seen.add((device['ip'], device['mac']))
            unique.append(device)
    devices = unique[:MAX_LAN]
    if devices and shutil.which('avahi-resolve-address'):
        resolve_names(devices)
    return devices


def _safe_name(name) -> Optional[str]:
    """An mDNS name the guard will accept: cut to MAX_NAME_LEN (one DNS
    label), and dropped rather than refused if it still looks like a blob -
    one odd hostname must not make the pad exit 1 every 5 minutes."""
    name = str(name)[:MAX_NAME_LEN]
    if not name or find_secret_keys(name):
        logger.debug('dropping a name the secret guard flagged')
        return None
    return name


def build_payload(room: str, host: str, self_info: dict, lan: list, now: Optional[datetime] = None) -> dict:
    lan = strip_secrets(lan)
    for device in lan:
        if isinstance(device, dict) and 'name' in device:
            name = _safe_name(device['name'])
            if name:
                device['name'] = name
            else:
                del device['name']
    payload = {
        'room': room,
        'host': host,
        'reported_at': (now or datetime.now(timezone.utc)).isoformat(timespec='seconds'),
        'self': strip_secrets(self_info),
        'lan': lan,
    }
    leaked = find_secret_keys(payload)
    if leaked:
        raise ValueError('refusing to report: secret-looking keys %s' % leaked)
    return payload


def send(payload: dict, url: str, key: str) -> bool:
    """POST with a few retries on connection errors / 5xx. True on 2xx."""
    delay = 2
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(url, json=payload, headers={'X-API-Key': key}, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            logger.warning('network: attempt %d could not reach %s (%s)', attempt, url, e.__class__.__name__)
        else:
            if 200 <= r.status_code < 300:
                logger.info('network: %s -> %d devices accepted', payload['room'], len(payload['lan']))
                return True
            if r.status_code < 500:
                logger.error('network: rejected with HTTP %d: %s', r.status_code, r.text[:300])
                return False
            logger.warning('network: attempt %d got HTTP %d', attempt, r.status_code)
        if attempt < RETRIES:
            time.sleep(delay)
            delay *= 2
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Report this pad\'s view of the network to gsd-cloud.')
    parser.add_argument('--dry-run', action='store_true', help='print the payload (with why each null is null) instead of sending it')
    parser.add_argument('--no-sweep', action='store_true', help='skip the ping sweep; neighbour table only')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)  # an unknown flag exits 2, never sends
    dry_run, sweep = args.dry_run, not args.no_sweep
    logging.basicConfig(level=logging.DEBUG if dry_run else logging.INFO, format='%(message)s')
    key_file = os.environ.get('MUSICFIG_REPORT_KEY_FILE') or DEFAULT_KEY_FILE
    try:
        key = read_key(key_file)
    except OSError as e:
        logger.error('network: cannot read the key file %s (%s)', key_file, e)
        return 1
    if key is None and not dry_run:
        logger.info('network: no key at %s; network reporting is not set up here', key_file)
        return 0
    host = socket.gethostname().split('.')[0]
    room = os.environ.get('MUSICFIG_ROOM') or host
    self_info, ips, macs = collect_self()
    lan = collect_lan(self_info['ip'], ips, macs, sweep=sweep)
    try:
        payload = build_payload(room, host, self_info, lan)
    except ValueError as e:
        logger.error('%s', e)
        return 1
    if dry_run:
        print(json.dumps(payload, indent=2))
        return 0
    url = os.environ.get('MUSICFIG_NETWORK_REPORT_URL') or DEFAULT_REPORT_URL
    return 0 if send(payload, url, key) else 1


if __name__ == '__main__':
    sys.exit(main())
