#!/usr/bin/env python

import os
import yaml
from pathlib import Path

class Tags():

    def __init__(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        env_tags_file = os.environ.get('MUSICFIG_TAGS_FILE')
        if env_tags_file:
            self.tags_file = env_tags_file
        else:
            if Path(current_dir + '/../tags.yml').is_file():
                self.tags_file = current_dir + '/../tags.yml'
            if Path('/config/tags.yml').is_file():
                self.tags_file = '/config/tags.yml'
        self.last_updated = ''

    def load_tags(self):
        """Load the NFC tag config file if it has changed.
        """
        if (self.last_updated != os.stat(self.tags_file).st_mtime):
            with open(self.tags_file, 'r') as stream:
                self.tags = yaml.load(stream, Loader=yaml.FullLoader)
            self.last_updated = os.stat(self.tags_file).st_mtime
            return self.tags


# Keys that must not leak from a resolved entry into the action handlers.
_ROOM_KEYS = ('rooms',)


def resolve_for_room(tags, room):
    """Return a copy of `tags` with every identifier entry resolved for the
    pad in `room` (the Pi's MUSICFIG_ROOM, e.g. kitchen / guest-room).

    Per-tag overrides:
        identifier:
          04a68312fe4a80:
            name: Peter Pan
            airplay: peterpan.mp3        # default action, any room
            homepod: "@room"             # the HomePod of the room the pad is in
            rooms:
              kitchen:                   # kitchen pad: play on the Pi's own speaker
                airplay: null            # null removes an inherited key
                mp3: peterpan.mp3
              game-room:
                homepod: Basement (2)    # explicit target for this room

    Top-level map from room to that room's HomePod name, used by "@room":
        room_homepods:
          guest-room: Guest Room HomePod
          game-room: Basement (2)

    A room with no HomePod (or no entry) makes "@room" resolve to nothing, so
    the airplay action is dropped rather than sent to the wrong speaker.
    """
    if not tags:
        return tags
    room_homepods = tags.get('room_homepods') or {}
    out = dict(tags)
    resolved = {}
    for ident, entry in (tags.get('identifier') or {}).items():
        if not isinstance(entry, dict):
            resolved[ident] = entry
            continue
        merged = {k: v for k, v in entry.items() if k not in _ROOM_KEYS}
        overrides = (entry.get('rooms') or {}).get(room) or {}
        merged.update(overrides)
        # null in an override deletes the inherited key
        merged = {k: v for k, v in merged.items() if v is not None}
        if merged.get('homepod') == '@room':
            target = room_homepods.get(room)
            if target:
                merged['homepod'] = target
            else:
                merged.pop('homepod', None)
                merged.pop('airplay', None)
        resolved[ident] = merged
    out['identifier'] = resolved
    return out

