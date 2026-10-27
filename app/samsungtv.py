#!/usr/bin/env python
"""
Samsung TV integration for Musicfig over the TV's LOCAL network API.

Replaces the SmartThings cloud integration (a Personal Access Token that
expired after 24 h). Everything here stays on the LAN:

* Power state: GET http://<tv>:8001/api/v2/ answers {"device": {"PowerState":
  "on"|"standby", "name": ..., "modelName": ...}} with no auth.
* Remote keys: a websocket on port 8002 (samsungtvws). The first connection
  pops a one-time "Allow" prompt on the TV; the token it hands back lives in
  samsungtv_token_file (never in tags.yml, never logged).
* Wake from standby: a Wake-on-LAN magic packet to the TV's Wi-Fi MAC, then
  KEY_POWER over the websocket once it answers ("Power On with Mobile" must
  be on in the TV's settings).

Input selection is NOT this module's job: the Apple TV and the Xbox take the
input themselves over HDMI-CEC when they start (lego's appletv_cec_wake path).
The TV only has to be on and free of Samsung's overlay / home bar (KEY_EXIT).
The local API never says which input is live, and on this set (QN80F) the
numbered KEY_HDMI1-4 are ignored; KEY_HDMI only steps to the next input.
"""

import argparse
import logging
import os
import socket
import sys
import time
import requests
# Optional dependency: samsungtvws is in requirements.txt (pure Python), but a
# Pi updated without a pip install would be missing it. Without it the power
# state still works (plain HTTP); only the remote keys are unavailable.
try:
    from samsungtvws import SamsungTVWS
except ImportError:  # pragma: no cover - depends on the install
    SamsungTVWS = None

logger = logging.getLogger(__name__)

REST_PORT = 8001           # http://<tv>:8001/api/v2/ (device info, no auth)
WS_PORT = 8002             # wss remote control (token after the Allow prompt)
WOL_PORT = 9
HTTP_TIMEOUT = 3           # seconds; the TV is on the LAN, slower = not there
WS_TIMEOUT = 5             # seconds; covers the Allow prompt only at pair time
EXIT_SETTLE = 0.8          # seconds after KEY_EXIT before the caller launches
WAKE_POLL = 1.0            # seconds between /api/v2/ polls while waking
DEFAULT_TOKEN_FILE = '/etc/musicfig/samsungtv.token'
DEFAULT_WAKE_TIMEOUT = 12  # seconds; the TV reports "on" ~3 s after the wake

# Configuration - set these in tags.yml
SAMSUNGTV_HOST = None          # IP (or name) of the TV
SAMSUNGTV_MAC = None           # the TV's Wi-Fi MAC, for Wake-on-LAN
SAMSUNGTV_TOKEN_FILE = DEFAULT_TOKEN_FILE
SAMSUNGTV_NAME = None          # how the pad shows up in the TV's device list
SAMSUNGTV_WAKE_TIMEOUT = DEFAULT_WAKE_TIMEOUT

# The SmartThings keys the cloud version read. Present without samsungtv_host
# they earn one warning and the TV counts as unconfigured.
LEGACY_KEYS = ('smartthings_token', 'smartthings_tv_device_id', 'smartthings_appletv_input')
NEW_KEYS = ('samsungtv_host', 'samsungtv_mac', 'samsungtv_token_file', 'samsungtv_name',
            'samsungtv_wake_timeout')

# lego.py still sends the SmartThings key names (HOME, OK, BACK, ...); map them
# to the websocket KEY_* names. Raw KEY_* names pass through untouched.
KEY_MAP = {
    'HOME': 'KEY_HOME',
    'UP': 'KEY_UP',
    'DOWN': 'KEY_DOWN',
    'LEFT': 'KEY_LEFT',
    'RIGHT': 'KEY_RIGHT',
    'OK': 'KEY_ENTER',
    'ENTER': 'KEY_ENTER',
    'SELECT': 'KEY_ENTER',
    'BACK': 'KEY_RETURN',
    'RETURN': 'KEY_RETURN',
    'EXIT': 'KEY_EXIT',
    'MENU': 'KEY_MENU',
    'INFO': 'KEY_INFO',
    'MUTE': 'KEY_MUTE',
    'VOLUP': 'KEY_VOLUP',
    'VOLDOWN': 'KEY_VOLDOWN',
    'PLAY': 'KEY_PLAY',
    'PAUSE': 'KEY_PAUSE',
    'STOP': 'KEY_STOP',
    'REWIND': 'KEY_REWIND',
    'FF': 'KEY_FF',
    'PLAY_BACK': 'KEY_PLAY_BACK',
    'SOURCE': 'KEY_SOURCE',
    'HDMI': 'KEY_HDMI',
    'POWER': 'KEY_POWER',
}

# Configuration loaded flag
_config_loaded = False


def map_key(key):
    """SmartThings-era name (OK, BACK, FF...) or a raw KEY_* name -> the
    websocket KEY_* name; None when the name is unknown."""
    if not key:
        return None
    name = str(key).strip().upper()
    if name.startswith('KEY_'):
        return name
    return KEY_MAP.get(name)


def default_name():
    """The name the TV shows in its device list: musicfig <room>."""
    room = os.environ.get('MUSICFIG_ROOM') or socket.gethostname().split('.')[0]
    return 'musicfig %s' % room


def device_info():
    """
    GET /api/v2/ on the TV (no auth).

    Returns:
        the "device" dict (PowerState, name, modelName, ...), or None when the
        TV does not answer / answers nonsense
    """
    if not SAMSUNGTV_HOST:
        return None
    url = 'http://%s:%d/api/v2/' % (SAMSUNGTV_HOST, REST_PORT)
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        device = response.json().get('device')
    except (requests.exceptions.RequestException, ValueError, AttributeError) as e:
        logger.debug('Samsung TV %s: /api/v2/ not answering: %s' % (SAMSUNGTV_HOST, e))
        return None
    if not isinstance(device, dict):
        logger.debug('Samsung TV %s: /api/v2/ answered without a device block' % SAMSUNGTV_HOST)
        return None
    return device


def power_state():
    """'on' / 'standby' as the TV reports it, or None when it is unreachable."""
    device = device_info()
    if device is None:
        return None
    state = device.get('PowerState')
    return str(state).lower() if state else None


def is_tv_on():
    """True when the TV reports PowerState on; False for standby or unreachable."""
    return power_state() == 'on'


def get_current_input():
    """
    The local API never reports which input is live (the SmartThings one did).

    Returns:
        None, always
    """
    logger.debug('Samsung TV: the local API cannot tell which input is live')
    return None


def set_input_source(input_source):
    """
    Input selection is HDMI-CEC-driven: the Apple TV / Xbox take the input
    themselves when they start, and this set ignores the numbered KEY_HDMI1-4.

    Returns:
        False, always
    """
    logger.info('Samsung TV: input selection is HDMI-CEC-driven (the source device takes '
                'the input itself); ignoring request for %s' % input_source)
    return False


def _remote():
    """A fresh websocket remote; opened and closed around every key."""
    return SamsungTVWS(SAMSUNGTV_HOST, port=WS_PORT, token_file=SAMSUNGTV_TOKEN_FILE,
                       name=SAMSUNGTV_NAME or default_name(), timeout=WS_TIMEOUT)


def send_key(key):
    """
    Send a remote control key press to the TV over the websocket.

    Args:
        key: a SmartThings-era name (HOME, OK, BACK, EXIT, FF, ...) or a raw
             KEY_* name (KEY_ENTER, KEY_HDMI, ...)

    Returns:
        True if the TV took the key, False otherwise (never raises)
    """
    if not configured():
        logger.debug('Samsung TV not configured; key %s dropped' % key)
        return False
    ws_key = map_key(key)
    if not ws_key:
        logger.warning('Samsung TV: unknown key %r' % (key,))
        return False
    if SamsungTVWS is None:
        logger.error('Samsung TV: samsungtvws is not installed (pip install -r requirements.txt)')
        return False
    remote = None
    try:
        logger.info('Samsung TV: sending %s' % ws_key)
        remote = _remote()
        remote.open()
        remote.send_key(ws_key)
        return True
    except Exception as e:
        # The exception text never carries the token (samsungtvws raises on
        # the unauthorized / failed events, which have none).
        logger.warning('Samsung TV: key %s failed: %s: %s' % (ws_key, type(e).__name__, e))
        return False
    finally:
        if remote is not None:
            try:
                remote.close()
            except Exception as e:
                logger.debug('Samsung TV: close after %s: %s' % (ws_key, e))


def wol_packet(mac):
    """The Wake-on-LAN magic packet for a MAC (any of the usual spellings)."""
    digits = str(mac).replace(':', '').replace('-', '').replace('.', '').strip()
    if len(digits) != 12:
        raise ValueError('MAC %r is not 12 hex digits' % (mac,))
    return b'\xff' * 6 + bytes.fromhex(digits) * 16


def send_wol(mac=None, host=None, times=3):
    """
    Wake-on-LAN: the magic packet to the LAN broadcast AND straight to the TV
    (the Wi-Fi mesh does not always pass a broadcast on), `times` each.

    Returns:
        True if at least one packet left, False otherwise (never raises)
    """
    mac = mac or SAMSUNGTV_MAC
    host = host or SAMSUNGTV_HOST
    if not mac:
        logger.warning('Samsung TV: no samsungtv_mac, cannot wake the TV by Wake-on-LAN')
        return False
    try:
        packet = wol_packet(mac)
    except ValueError as e:
        logger.error('Samsung TV: %s' % e)
        return False
    targets = [('<broadcast>', WOL_PORT)]
    if host:
        targets.append((host, WOL_PORT))
    sent = 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        logger.error('Samsung TV: cannot open a UDP socket for Wake-on-LAN: %s' % e)
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(HTTP_TIMEOUT)
        for _ in range(times):
            for target in targets:
                try:
                    sock.sendto(packet, target)
                    sent += 1
                except OSError as e:
                    logger.debug('Samsung TV: Wake-on-LAN to %s:%d failed: %s' % (target[0], target[1], e))
    finally:
        sock.close()
    if not sent:
        logger.warning('Samsung TV: no Wake-on-LAN packet could be sent')
    else:
        logger.info('Samsung TV: Wake-on-LAN sent (%d packets)' % sent)
    return sent > 0


def turn_on():
    """
    Wake the TV: Wake-on-LAN, then KEY_POWER over the websocket once the TV
    answers in standby (KEY_POWER toggles, so it is sent once at most), and
    wait up to samsungtv_wake_timeout for PowerState on.

    Returns:
        True once the TV reports on, False otherwise (never raises)
    """
    if not configured():
        logger.debug('Samsung TV not configured; turn_on dropped')
        return False
    state = power_state()
    if state == 'on':
        return True
    logger.info('Samsung TV is %s, waking it...' % (state or 'not answering'))
    send_wol()
    power_sent = False
    deadline = time.time() + SAMSUNGTV_WAKE_TIMEOUT
    while True:
        state = power_state()
        if state == 'on':
            logger.info('Samsung TV is on')
            return True
        if state == 'standby' and not power_sent:
            power_sent = send_key('KEY_POWER')
        if time.time() >= deadline:
            break
        time.sleep(WAKE_POLL)
    logger.error('Samsung TV did not come on within %s s (last state: %s)'
                 % (SAMSUNGTV_WAKE_TIMEOUT, state or 'not answering'))
    return False


def turn_off():
    """
    Put the TV in standby (KEY_POWER, which toggles, so only when it is on).

    Returns:
        True when the TV is off / was already off, False otherwise (never raises)
    """
    if not configured():
        logger.debug('Samsung TV not configured; turn_off dropped')
        return False
    state = power_state()
    if state == 'standby':
        return True
    if state is None:
        logger.warning('Samsung TV is not answering; cannot turn it off')
        return False
    return send_key('KEY_POWER')


def ensure_on(auto_turn_on=True):
    """
    Get the TV ready for a source device: on, and free of Samsung's overlay /
    home bar (KEY_EXIT). The Apple TV / Xbox take the input itself over
    HDMI-CEC once it is.

    Args:
        auto_turn_on: wake a TV in standby first (and wait for it)

    Returns:
        True when the TV is on, False otherwise (never raises)
    """
    try:
        if not configured():
            logger.warning('Samsung TV not configured (samsungtv_host)')
            return False
        state = power_state()
        if state != 'on':
            if not auto_turn_on:
                logger.info('Samsung TV is %s and auto_turn_on is off' % (state or 'not answering'))
                return False
            if not turn_on():
                return False
        # EXIT closes Samsung overlays (home bar, TV Plus) and goes full screen
        # to whatever input is live; its failure is logged by send_key.
        if send_key('KEY_EXIT'):
            time.sleep(EXIT_SETTLE)
        return True
    except Exception as e:  # belt and braces: a tag action must never die here
        logger.error('Samsung TV: ensure_on failed: %s: %s' % (type(e).__name__, e))
        return False


def switch_to_appletv(auto_turn_on=True):
    """
    Get the TV ready for the Apple TV (= ensure_on: the Apple TV takes the
    input itself over HDMI-CEC).

    Returns:
        True when the TV is on, False otherwise (never raises)
    """
    return ensure_on(auto_turn_on=auto_turn_on)


# Configuration loading from tags.yml
def reset_config():
    """Reset config loaded flag to allow reloading."""
    global _config_loaded
    _config_loaded = False


def load_config(tags):
    """Load Samsung TV configuration from tags dict."""
    global SAMSUNGTV_HOST, SAMSUNGTV_MAC, SAMSUNGTV_TOKEN_FILE, SAMSUNGTV_NAME
    global SAMSUNGTV_WAKE_TIMEOUT, _config_loaded

    if _config_loaded:
        return

    if tags.get('samsungtv_host'):
        SAMSUNGTV_HOST = str(tags['samsungtv_host']).strip()
        logger.info('Samsung TV host from config: %s' % SAMSUNGTV_HOST)
    elif any(k in tags for k in LEGACY_KEYS):
        legacy = ', '.join(k for k in LEGACY_KEYS if k in tags)
        logger.warning('Samsung TV: the SmartThings cloud keys (%s) are no longer used - its token '
                       'expired after 24 h. Use the TV\'s local API instead: samsungtv_host (IP), '
                       'samsungtv_mac (Wake-on-LAN), optional samsungtv_token_file, samsungtv_name, '
                       'samsungtv_wake_timeout; then pair once with `python -m app.samsungtv pair`. '
                       'TV control is off until then.' % legacy)

    if tags.get('samsungtv_mac'):
        SAMSUNGTV_MAC = str(tags['samsungtv_mac']).strip()
        logger.info('Samsung TV MAC from config: %s' % SAMSUNGTV_MAC)

    if tags.get('samsungtv_token_file'):
        SAMSUNGTV_TOKEN_FILE = str(tags['samsungtv_token_file']).strip()
        logger.info('Samsung TV token file from config: %s' % SAMSUNGTV_TOKEN_FILE)

    if tags.get('samsungtv_name'):
        SAMSUNGTV_NAME = str(tags['samsungtv_name']).strip()
        logger.info('Samsung TV remote name from config: %s' % SAMSUNGTV_NAME)

    if 'samsungtv_wake_timeout' in tags:
        try:
            SAMSUNGTV_WAKE_TIMEOUT = float(tags['samsungtv_wake_timeout'])
            if SAMSUNGTV_WAKE_TIMEOUT <= 0:
                raise ValueError('must be positive')
            logger.info('Samsung TV wake timeout from config: %s s' % SAMSUNGTV_WAKE_TIMEOUT)
        except (TypeError, ValueError) as e:
            SAMSUNGTV_WAKE_TIMEOUT = DEFAULT_WAKE_TIMEOUT
            logger.warning('Samsung TV: samsungtv_wake_timeout %r ignored (%s); using %s s'
                           % (tags['samsungtv_wake_timeout'], e, DEFAULT_WAKE_TIMEOUT))

    _config_loaded = True


def activated():
    """Check if the Samsung TV integration is configured and its library available."""
    return bool(SAMSUNGTV_HOST) and SamsungTVWS is not None


def configured():
    """Check if the Samsung TV is configured (a host is all it takes)."""
    return bool(SAMSUNGTV_HOST)


# CLI for the one-time pairing and a status check:
#   python -m app.samsungtv pair --host H --mac M [--token-file F] [--name N]
#   python -m app.samsungtv status --host H
def _cli_pair(args):
    global SAMSUNGTV_HOST, SAMSUNGTV_MAC, SAMSUNGTV_TOKEN_FILE, SAMSUNGTV_NAME
    SAMSUNGTV_HOST = args.host
    SAMSUNGTV_MAC = args.mac
    SAMSUNGTV_TOKEN_FILE = args.token_file
    SAMSUNGTV_NAME = args.name or default_name()
    if SamsungTVWS is None:
        print('FAILED: samsungtvws is not installed (pip install -r requirements.txt)')
        return 1
    if not is_tv_on():
        if not args.mac:
            print('FAILED: the TV is not on and no --mac was given to wake it')
            return 1
        print('TV is in standby, waking it...')
        if not turn_on():
            print('FAILED: the TV did not come on')
            return 1
    print('Pairing as "%s": press Allow on the TV when it asks...' % SAMSUNGTV_NAME)
    remote = None
    try:
        remote = _remote()
        remote.open()
        remote.send_key('KEY_INFO')
    except Exception as e:
        print('FAILED: %s: %s' % (type(e).__name__, e))
        return 1
    finally:
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass
    if os.path.isfile(SAMSUNGTV_TOKEN_FILE) and os.path.getsize(SAMSUNGTV_TOKEN_FILE) > 0:
        print('OK: paired, token stored in %s' % SAMSUNGTV_TOKEN_FILE)
        return 0
    print('FAILED: the TV answered but no token landed in %s' % SAMSUNGTV_TOKEN_FILE)
    return 1


def _cli_status(args):
    global SAMSUNGTV_HOST
    SAMSUNGTV_HOST = args.host
    device = device_info()
    if device is None:
        print('%s: not answering on port %d' % (args.host, REST_PORT))
        return 1
    print('PowerState: %s' % device.get('PowerState', '?'))
    print('name:       %s' % device.get('name', '?'))
    print('model:      %s' % device.get('modelName', '?'))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.samsungtv',
                                     description='Samsung TV local API: one-time pairing and status.')
    sub = parser.add_subparsers(dest='command', required=True)
    pair = sub.add_parser('pair', help='pair with the TV once (press Allow on the TV)')
    pair.add_argument('--host', required=True, help='IP of the TV')
    pair.add_argument('--mac', help="the TV's Wi-Fi MAC, to wake it first if it is in standby")
    pair.add_argument('--token-file', default=DEFAULT_TOKEN_FILE,
                      help='where the token goes (default %(default)s)')
    pair.add_argument('--name', help='name shown on the TV (default: musicfig <room>)')
    pair.set_defaults(func=_cli_pair)
    status = sub.add_parser('status', help='print PowerState / name / model')
    status.add_argument('--host', required=True, help='IP of the TV')
    status.set_defaults(func=_cli_status)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
