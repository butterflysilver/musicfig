"""Room-aware tag resolution (app.tags.resolve_for_room).

Run with:  python -m unittest tests.test_tags_rooms
"""
import importlib.util
import os
import unittest

# Load app/tags.py directly: importing the `app` package builds the Flask
# app and needs a config module, which a unit test should not depend on.
_TAGS = os.path.join(os.path.dirname(__file__), '..', 'app', 'tags.py')
_spec = importlib.util.spec_from_file_location('musicfig_tags', _TAGS)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
resolve_for_room = _mod.resolve_for_room


BASE = {
    'mp3_dir': '/music',
    'room_homepods': {'guest-room': 'Guest Room HomePod', 'game-room': 'Basement (2)'},
    'identifier': {
        'aaaa': {
            'name': 'Peter Pan',
            'airplay': 'peterpan.mp3',
            'homepod': '@room',
            'rooms': {
                'kitchen': {'airplay': None, 'mp3': 'peterpan.mp3'},
                'game-room': {'homepod': 'Office'},
            },
        },
        'bbbb': {'name': 'Plain', 'mp3': 'plain.mp3'},
        'cccc': 'not-a-dict',
    },
}


class ResolveForRoomTests(unittest.TestCase):

    def test_default_room_gets_room_homepod(self):
        t = resolve_for_room(BASE, 'guest-room')['identifier']['aaaa']
        self.assertEqual(t['airplay'], 'peterpan.mp3')
        self.assertEqual(t['homepod'], 'Guest Room HomePod')
        self.assertNotIn('rooms', t)
        self.assertNotIn('mp3', t)

    def test_room_override_replaces_and_null_deletes(self):
        t = resolve_for_room(BASE, 'kitchen')['identifier']['aaaa']
        self.assertEqual(t['mp3'], 'peterpan.mp3')
        self.assertNotIn('airplay', t)
        # kitchen has no HomePod: "@room" must not leak through
        self.assertNotIn('homepod', t)

    def test_explicit_room_homepod_beats_at_room(self):
        t = resolve_for_room(BASE, 'game-room')['identifier']['aaaa']
        self.assertEqual(t['homepod'], 'Office')
        self.assertEqual(t['airplay'], 'peterpan.mp3')

    def test_unknown_room_drops_airplay_instead_of_misrouting(self):
        t = resolve_for_room(BASE, 'garage')['identifier']['aaaa']
        self.assertNotIn('homepod', t)
        self.assertNotIn('airplay', t)
        self.assertEqual(t['name'], 'Peter Pan')

    def test_entries_without_rooms_pass_through(self):
        r = resolve_for_room(BASE, 'kitchen')['identifier']
        self.assertEqual(r['bbbb'], {'name': 'Plain', 'mp3': 'plain.mp3'})
        self.assertEqual(r['cccc'], 'not-a-dict')

    def test_input_is_not_mutated(self):
        resolve_for_room(BASE, 'kitchen')
        self.assertIn('rooms', BASE['identifier']['aaaa'])
        self.assertEqual(BASE['identifier']['aaaa']['homepod'], '@room')

    def test_empty_config(self):
        self.assertEqual(resolve_for_room({}, 'kitchen'), {})
        self.assertIsNone(resolve_for_room(None, 'kitchen'))


if __name__ == '__main__':
    unittest.main()
