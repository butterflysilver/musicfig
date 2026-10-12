#!/usr/bin/env python
"""Report this pad's figure map to gsd-cloud (the Figures page).

Reads the tags file musicfig itself reads, resolves it for this room the way
the pad does (room overrides, "@room" HomePods / Yoto players), turns every
identifier into one small record - figure name, what it does here and on
what - and POSTs the list to gsd-cloud. The website then shows every figure
across every room.

Nothing secret leaves the Pi: Apple TV pairing credentials, the sync box
token, webhooks and anything else whose key looks like a secret are dropped
before the payload is built, and the script refuses to send a payload that
still contains one.

Usage:
    report_pad.py             send (exit 0 on success, 1 on failure)
    report_pad.py --dry-run   print the payload instead of sending it
    report_pad.py --no-titles skip the Yoto title lookups (offline / tests)

Environment (the musicfig-report.service unit sets the same ones the pad
service uses): MUSICFIG_TAGS_FILE, MUSICFIG_ROOM, MUSICFIG_REPORT_URL
(default https://gsd-cloud.vercel.app/api/pads/report),
MUSICFIG_REPORT_KEY_FILE (default /etc/musicfig/pad-report.key; no file =
reporting is not set up on this pad, exit 0 quietly).
"""
import importlib.util
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app')


def _load(name):
    """Load one app module by file. Importing the `app` package would start
    the jukebox itself (pad, web UI) next to the running service."""
    spec = importlib.util.spec_from_file_location(f'report_pad_{name}', os.path.join(APP_DIR, f'{name}.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nfctags = _load('tags')
yoto_tracks = _load('yoto_tracks')

logger = logging.getLogger('report_pad')

DEFAULT_REPORT_URL = 'https://gsd-cloud.vercel.app/api/pads/report'
DEFAULT_KEY_FILE = '/etc/musicfig/pad-report.key'
TIMEOUT_S = 20
RETRIES = 3

# Any key matching this never leaves the Pi, at any depth of the tags file.
SECRET_KEY_RE = re.compile(r'credential|token|secret|password|api[-_]?key|hook', re.I)
# A string value that is only an opaque blob (no spaces, no URL punctuation)
# of 64+ chars is a credential, whatever key it sits under.
SECRET_VALUE_RE = re.compile(r'[A-Za-z0-9+/=_-]{64,}')
MAX_VALUE_LEN = 500

# Tag keys that are actions, most specific first: a tag carrying several
# (musicfig runs them all) is reported under the first one, the rest are
# listed in details.also.
ACTION_KEYS = (
    ('appletv_url', 'appletv'),
    ('deeplink', 'deeplink'),
    ('youtube', 'youtube'),
    ('netflix', 'netflix'),
    ('disney', 'disney'),
    ('yoto', 'yoto'),
    ('yoto_airplay', 'yoto_airplay'),
    ('airplay', 'airplay'),
    ('spotify', 'spotify'),
    ('playlist', 'playlist'),
    ('mp3', 'mp3'),
    ('xbox', 'xbox'),
    ('command', 'command'),
    ('slack', 'slack'),
)
ACTIONS = tuple(a for _k, a in ACTION_KEYS)

_SLUG_SMALL_WORDS = {'a', 'an', 'and', 'of', 'the', 'in', 'on', 'at', 'to', 'for', 'vs'}


def strip_secrets(value):
    """Deep copy of `value` without any key that looks like a secret."""
    if isinstance(value, dict):
        return {k: strip_secrets(v) for k, v in value.items()
                if not SECRET_KEY_RE.search(str(k))}
    if isinstance(value, list):
        return [strip_secrets(v) for v in value]
    return value


def find_secret_keys(value, path=''):
    """Every path in `value` that looks like a secret: a key matching
    SECRET_KEY_RE, or a string value that is an opaque blob (very long, or
    64+ chars of nothing but base64/hex characters - what a pairing
    credential or token looks like). Should be empty after strip_secrets;
    the send refuses otherwise."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f'{path}.{k}' if path else str(k)
            if SECRET_KEY_RE.search(str(k)):
                found.append(here)
            found.extend(find_secret_keys(v, here))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found.extend(find_secret_keys(v, f'{path}[{i}]'))
    elif isinstance(value, str):
        if len(value) > MAX_VALUE_LEN or SECRET_VALUE_RE.fullmatch(value):
            found.append(path or '<value>')
    return found


def title_from_url(url: str) -> Optional[str]:
    """A readable title from a tv.apple.com (or similar slug-style) link:
    .../movie/monsters-inc/umc.cmc.xxx -> "Monsters Inc". None when the URL
    has no slug worth showing (YouTube ids, bare schemes)."""
    try:
        parts = [p for p in urlparse(url).path.split('/') if p]
    except (ValueError, AttributeError):
        return None
    slug = None
    for marker in ('movie', 'show', 'episode', 'collection', 'title'):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                slug = parts[i + 1]
                break
    if not slug or not re.search(r'[a-z]', slug) or re.fullmatch(r'umc\.cmc\.[\w]+', slug):
        return None
    words = [w for w in re.split(r'[-_]+', slug) if w]
    out = []
    for i, w in enumerate(words):
        if i and w in _SLUG_SMALL_WORDS:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return ' '.join(out) or None


def _device_for(action: str, entry: dict, resolved_tags: dict) -> Optional[str]:
    if action in ('appletv', 'deeplink', 'youtube', 'netflix', 'disney'):
        return resolved_tags.get('appletv_name') or None
    if action in ('airplay', 'yoto_airplay'):
        return entry.get('homepod') or None
    if action == 'yoto':
        return entry.get('yoto_player') or None
    return None


def figure_record(uid: str, entry, resolved_entry, resolved_tags: dict, titles) -> dict:
    """One figure as the Figures page wants it. `entry` is the raw tag entry
    (for the name and the rooms map), `resolved_entry` the per-room
    resolution (None when the tag resolves to nothing here)."""
    name = ''
    if isinstance(entry, dict):
        name = str(entry.get('name') or '').strip()
    rec = {'uid': uid, 'name': name or uid, 'action': 'none',
           'target': None, 'title': None, 'device': None, 'details': {}}
    if not isinstance(resolved_entry, dict):
        return rec
    present = [(k, a) for k, a in ACTION_KEYS if resolved_entry.get(k) not in (None, '')]
    if not present:
        return rec
    key, action = present[0]
    target = resolved_entry.get(key)
    rec['action'] = action
    rec['target'] = str(target) if not isinstance(target, bool) else None
    rec['device'] = _device_for(action, resolved_entry, resolved_tags)
    explicit = resolved_entry.get('title')
    if explicit:
        rec['title'] = str(explicit)
    elif action in ('yoto', 'yoto_airplay'):
        rec['title'] = titles(str(target))
    elif action in ('appletv', 'deeplink', 'youtube', 'netflix', 'disney'):
        rec['title'] = title_from_url(str(target))
    elif action in ('mp3', 'playlist', 'airplay'):
        rec['title'] = os.path.splitext(os.path.basename(str(target)))[0].replace('_', ' ')
    if len(present) > 1:
        rec['details']['also'] = ', '.join(a for _k, a in present[1:])
    if isinstance(entry, dict) and isinstance(entry.get('rooms'), dict):
        rec['details']['room_overrides'] = ', '.join(sorted(str(r) for r in entry['rooms']))
    for k in ('shuffle', 'position_ms', 'xbox_app'):
        if resolved_entry.get(k) not in (None, ''):
            rec['details'][k] = resolved_entry[k]
    return rec


def build_payload(raw_tags: dict, room: str, host: str, version: Optional[str],
                  titles=None, now: Optional[datetime] = None) -> dict:
    """The report for `room` from the raw tags document. `titles(card_id)`
    resolves Yoto card titles (None -> no lookups)."""
    titles = titles or (lambda _cid: None)
    cache = {}

    def cached_title(card_id):
        if card_id not in cache:
            cache[card_id] = titles(card_id)
        return cache[card_id]

    safe = strip_secrets(raw_tags or {})
    resolved = nfctags.resolve_for_room(safe, room) or {}
    resolved_ids = resolved.get('identifier') or {}
    figures = []
    for uid, entry in (safe.get('identifier') or {}).items():
        figures.append(figure_record(str(uid), entry, resolved_ids.get(uid), resolved, cached_title))
    payload = {
        'room': room,
        'host': host,
        'musicfig_version': version,
        'reported_at': (now or datetime.now(timezone.utc)).isoformat(timespec='seconds'),
        'figures': figures,
    }
    leaked = find_secret_keys(payload)
    if leaked:
        raise ValueError('refusing to report: secret-looking keys %s' % leaked)
    return payload


def musicfig_version() -> Optional[str]:
    try:
        out = subprocess.run(['git', '-C', os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'rev-parse', '--short', 'HEAD'],
                             capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def read_key(path: str) -> Optional[str]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            key = f.read().strip()
    except OSError:
        return None
    return key or None


def send(payload: dict, url: str, key: str) -> bool:
    """POST with a few retries on connection errors / 5xx. True on 2xx."""
    delay = 2
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(url, json=payload, headers={'X-API-Key': key}, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            logger.warning('report: attempt %d could not reach %s (%s)', attempt, url, e.__class__.__name__)
        else:
            if 200 <= r.status_code < 300:
                logger.info('report: %s -> %d figures accepted', payload['room'], len(payload['figures']))
                return True
            if r.status_code < 500:
                logger.error('report: rejected with HTTP %d: %s', r.status_code, r.text[:300])
                return False
            logger.warning('report: attempt %d got HTTP %d', attempt, r.status_code)
        if attempt < RETRIES:
            time.sleep(delay)
            delay *= 2
    return False


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    dry_run = '--dry-run' in argv
    no_titles = '--no-titles' in argv
    key_file = os.environ.get('MUSICFIG_REPORT_KEY_FILE') or DEFAULT_KEY_FILE
    key = read_key(key_file)
    if key is None and not dry_run:
        logger.info('report: no key at %s; pad reporting is not set up here', key_file)
        return 0
    tags_file = os.environ.get('MUSICFIG_TAGS_FILE') or '/etc/musicfig/tags.yml'
    try:
        with open(tags_file, 'r', encoding='utf-8') as f:
            raw = yaml.load(f, Loader=yaml.FullLoader) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.error('report: cannot read %s (%s)', tags_file, e.__class__.__name__)
        return 1
    room = os.environ.get('MUSICFIG_ROOM') or socket.gethostname().split('.')[0]
    titles = None if no_titles else yoto_tracks.card_title
    try:
        payload = build_payload(raw, room, socket.gethostname().split('.')[0], musicfig_version(), titles)
    except ValueError as e:
        logger.error('%s', e)
        return 1
    if dry_run:
        print(json.dumps(payload, indent=2))
        return 0
    url = os.environ.get('MUSICFIG_REPORT_URL') or DEFAULT_REPORT_URL
    return 0 if send(payload, url, key) else 1


if __name__ == '__main__':
    sys.exit(main())
