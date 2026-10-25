#!/usr/bin/env python3
#
# gsd-pads - blank or restore the LEGO pad's lights without stopping musicfig.
# scripts/pi/install.sh installs it as /usr/local/bin/gsd-pads (life-ops #75);
# the pad counterpart of gsd-screen (gsd-cloud scripts/pi-kiosk/screen.sh).
#
#   gsd-pads off       every pad dark, and kept dark through tag events
#                      (figures still play); recorded, so a restart of
#                      musicfig in the night does not light them again
#   gsd-pads on        the idle colour back (green with `lights: on`,
#                      dark with `lights: off`)
#   gsd-pads status    "on" / "off" plus the pad count, exit 0 either way
#
# Any user on the Pi may run it (the ssh aliases land as kiosk): it talks to
# the running musicfig over its localhost web port (GET /pads, POST
# /pads/off|on; app/pads.py) because the service owns the USB pad. With no
# pad plugged in the state is still recorded and applied when one appears
# (exit 0). Exit 1 when musicfig is not answering, 2 on usage.
#
#   ssh kitchen gsd-screen off; ssh kitchen gsd-pads off     # dark house
#
# Standard library only: it runs with the Pi's /usr/bin/python3, not the
# service's venv.
import json
import os
import sys
import urllib.error
import urllib.request

# run.py binds 127.0.0.1:5000. The Host header matches config.py's
# SERVER_NAME (localhost:5000): Flask before 3.1 answers 404 to any other host.
URL = os.environ.get('MUSICFIG_URL', 'http://127.0.0.1:5000')
HOST_HEADER = os.environ.get('MUSICFIG_HOST', 'localhost:5000')
TIMEOUT = 5  # seconds; a stuck service must not hang the fleet loop

USAGE = 'usage: gsd-pads on|off|status'


def call(method, path):
    req = urllib.request.Request(URL + path, method=method, headers={'Host': HOST_HEADER})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def describe(state):
    """('off', '1 pad') / ('on', 'no pad attached')."""
    pads = int(state.get('pads') or 0)
    if pads:
        where = '%d pad%s' % (pads, '' if pads == 1 else 's')
    else:
        where = 'no pad attached'
    return ('off' if state.get('off') else 'on'), where


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if len(argv) == 1 else ''
    if cmd not in ('on', 'off', 'status'):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        if cmd == 'status':
            state = call('GET', '/pads')
        else:
            state = call('POST', '/pads/' + cmd)
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, 'reason', None) or e
        print('gsd-pads: musicfig is not answering at %s (%s) - is it running? '
              '(systemctl is-active musicfig)' % (URL, reason), file=sys.stderr)
        return 1
    mode, where = describe(state)
    if cmd != 'status' and not state.get('changed', True):
        print('already %s (%s)' % (mode, where))
        return 0
    if cmd == 'off' and not state.get('pads'):
        where += '; applies when one is plugged in'
    print('%s (%s)' % (mode, where))
    return 0


if __name__ == '__main__':
    sys.exit(main())
