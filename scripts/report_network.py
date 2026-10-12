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
(missing binary, timeout, non-zero exit), so a Pi that lacks a tool - or a
laptop with none of them - reports nulls and an empty LAN instead of crashing.
The pad itself is dropped from `lan` (it is `self`); the list is sorted by IP
and capped at MAX_LAN rows. The sweep never touches anything but the one /24
the pad's own address sits in.

Usage:
    report_network.py             send (exit 0 on success, 1 on failure)
    report_network.py --dry-run   print the payload instead of sending it
    report_network.py --no-sweep  skip the ping sweep (neighbour table only)

Environment (musicfig-network.service sets the same ones musicfig-report
uses): MUSICFIG_ROOM (default: the hostname), MUSICFIG_NETWORK_REPORT_URL
(default https://gsd-cloud.vercel.app/api/network/report),
MUSICFIG_REPORT_KEY_FILE (default /etc/musicfig/pad-report.key; no file =
reporting is not set up on this pad, exit 0 quietly).
"""
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
NMCLI_WIFI = ['nmcli', '-t', '-f', 'ACTIVE,SSID,BSSID,CHAN,FREQ,SIGNAL', 'dev', 'wifi']
# Neighbour states that mean "we saw this MAC answer"; FAILED / INCOMPLETE
# rows are addresses nobody answered for and carry no lladdr anyway.
NEIGH_STATES = ('REACHABLE', 'STALE', 'DELAY', 'PROBE')
SWEEP_WORKERS = 64
SWEEP_CAP_S = 6
RESOLVE_CAP_S = 5
RESOLVE_EACH_S = 2
MAX_LAN = 512


def _run(cmd, timeout=5) -> str:
    """stdout of `cmd`, or "" on any failure: missing binary, timeout,
    non-zero exit. Nothing in this script may crash because a tool is
    absent; a missing piece just becomes null / empty in the payload."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ''
    if out.returncode != 0:
        return ''
    return out.stdout or ''


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
    except (OSError, ValueError, IndexError):
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
    except (OSError, ValueError):
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
    futures = [pool.submit(_run, ['ping', '-c1', '-W1', f'{base}.{n}'], 2) for n in range(1, 255)]
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


def _ip_key(device: dict):
    parts = device['ip'].split('.')
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


def avahi_name(ip: str, timeout: float) -> Optional[str]:
    out = _run(['avahi-resolve-address', '-4', ip], timeout=max(0.1, min(RESOLVE_EACH_S, timeout)))
    parts = out.split()
    return parts[1] if len(parts) >= 2 else None


def resolve_names(devices: list, resolver: Callable[[str, float], Optional[str]],
                  cap_s: float = RESOLVE_CAP_S, clock: Callable[[], float] = time.monotonic) -> list:
    """Add `name` to each device the resolver knows, in place, until cap_s of
    wall time has gone: the remainder stay nameless rather than hold the
    report. Each call gets at most the time left before the cap."""
    deadline = clock() + cap_s
    for device in devices:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        name = resolver(device['ip'], remaining)
        if name:
            device['name'] = name
    return devices


def collect_self() -> dict:
    iface, ip = self_interface(_run(['ip', '-4', '-o', 'addr', 'show']),
                               _run(['ip', '-4', 'route', 'show', 'default']))
    return {
        'ip': ip,
        'mac': iface_mac(_run(['ip', 'link', 'show']), iface),
        'uptime_s': read_uptime_s(),
        'link': parse_wifi(_run(NMCLI_WIFI, timeout=10)),
        'netwatch': netwatch_status(),
    }


def collect_lan(self_ip: Optional[str], self_mac: Optional[str], sweep: bool = True) -> list:
    base = subnet_base(self_ip) if self_ip else None
    if sweep and base:
        ping_sweep(base)
    devices = [d for d in parse_neigh(_run(['ip', '-4', 'neigh']))
               if d['ip'] != self_ip and d['mac'] != self_mac]
    devices.sort(key=_ip_key)
    devices = devices[:MAX_LAN]
    if devices and shutil.which('avahi-resolve-address'):
        resolve_names(devices, avahi_name)
    return devices


def build_payload(room: str, host: str, self_info: dict, lan: list, now: Optional[datetime] = None) -> dict:
    payload = {
        'room': room,
        'host': host,
        'reported_at': (now or datetime.now(timezone.utc)).isoformat(timespec='seconds'),
        'self': strip_secrets(self_info),
        'lan': strip_secrets(lan),
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
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    dry_run = '--dry-run' in argv
    sweep = '--no-sweep' not in argv
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
    self_info = collect_self()
    lan = collect_lan(self_info['ip'], self_info['mac'], sweep=sweep)
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
