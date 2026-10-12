"""scripts/report_pad.py: the pad's figure map as the gsd-cloud Figures page
receives it - resolved per room, titled, and with every secret stripped."""
import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    'report_pad', os.path.join(HERE, '..', 'scripts', 'report_pad.py'))
rp = importlib.util.module_from_spec(spec)
sys.modules['report_pad'] = rp
spec.loader.exec_module(rp)

TAGS = {
    'appletv_name': 'Guest Room AppleTV',
    'appletv_companion_credentials': 'SECRET-PAIRING-BLOB',
    'syncbox_token': 'SECRET-SYNCBOX',
    'slack_hook': 'https://hooks.slack.com/SECRET',
    'room_homepods': {'guest-room': 'Guest Room HomePod'},
    'room_yoto_players': {'guest-room': "Momo's Library"},
    'identifier': {
        '04809ada204b80': {
            'name': 'Creeper',
            'appletv_url': 'https://tv.apple.com/us/movie/a-charlie-brown-thanksgiving/umc.cmc.7grdg',
            'rooms': {'kitchen': {'appletv_url': None}},
        },
        '04a68312fe4a80': {
            'name': 'Peter Pan',
            'airplay': 'peterpan.mp3',
            'homepod': '@room',
            'rooms': {'kitchen': {'airplay': None, 'mp3': 'peterpan.mp3'}},
        },
        '04112233445566': {'name': 'He-Man read-along', 'yoto': 'abc12', 'yoto_player': '@room'},
        '04831a62714080': {'name': 'Ironman', 'spotify': 'track:0TI8', 'position_ms': 11000,
                           'command': 'uname -a'},
        '04ec806a0b4080': {'name': 'Nothing yet'},
    },
}
NOW = datetime(2026, 10, 11, 23, 0, tzinfo=timezone.utc)


def build(room, titles=lambda cid: {'abc12': 'Masters of the Universe'}.get(cid)):
    return rp.build_payload(TAGS, room, 'guest-room', 'f0b0fb1', titles, now=NOW)


class PayloadTests(unittest.TestCase):
    def figures(self, room):
        return {f['uid']: f for f in build(room)['figures']}

    def test_envelope(self):
        p = build('guest-room')
        self.assertEqual((p['room'], p['host'], p['musicfig_version']), ('guest-room', 'guest-room', 'f0b0fb1'))
        self.assertEqual(p['reported_at'], '2026-10-11T23:00:00+00:00')
        self.assertEqual(len(p['figures']), 5)

    def test_apple_tv_figure_gets_a_title_and_the_room_box(self):
        f = self.figures('guest-room')['04809ada204b80']
        self.assertEqual(f['name'], 'Creeper')
        self.assertEqual(f['action'], 'appletv')
        self.assertEqual(f['title'], 'A Charlie Brown Thanksgiving')
        self.assertEqual(f['device'], 'Guest Room AppleTV')
        self.assertEqual(f['details']['room_overrides'], 'kitchen')

    def test_room_override_to_null_reports_none(self):
        f = self.figures('kitchen')['04809ada204b80']
        self.assertEqual(f['action'], 'none')
        self.assertIsNone(f['target'])

    def test_airplay_resolves_the_rooms_homepod_or_falls_to_mp3(self):
        guest = self.figures('guest-room')['04a68312fe4a80']
        self.assertEqual((guest['action'], guest['device'], guest['title']),
                         ('airplay', 'Guest Room HomePod', 'peterpan'))
        kitchen = self.figures('kitchen')['04a68312fe4a80']
        self.assertEqual((kitchen['action'], kitchen['device']), ('mp3', None))
        # a room with no HomePod drops the airplay action entirely
        basement = self.figures('basement')['04a68312fe4a80']
        self.assertEqual(basement['action'], 'none')

    def test_yoto_card_gets_its_title_and_player(self):
        f = self.figures('guest-room')['04112233445566']
        self.assertEqual((f['action'], f['target'], f['title'], f['device']),
                         ('yoto', 'abc12', 'Masters of the Universe', "Momo's Library"))
        elsewhere = self.figures('basement')['04112233445566']
        self.assertEqual(elsewhere['device'], '@online')

    def test_yoto_titles_are_looked_up_once_per_card(self):
        calls = []

        def titles(cid):
            calls.append(cid)
            return 'T'
        tags = {'identifier': {'a1': {'yoto': 'x1'}, 'a2': {'yoto_airplay': 'x1'}, 'a3': {'yoto': 'x2'}}}
        rp.build_payload(tags, 'guest-room', 'h', None, titles, now=NOW)
        self.assertEqual(sorted(calls), ['x1', 'x2'])

    def test_several_actions_report_the_first_and_list_the_rest(self):
        f = self.figures('guest-room')['04831a62714080']
        self.assertEqual(f['action'], 'spotify')
        self.assertEqual(f['details'], {'also': 'command', 'position_ms': 11000})

    def test_unmapped_figure_is_still_listed(self):
        f = self.figures('guest-room')['04ec806a0b4080']
        self.assertEqual((f['name'], f['action']), ('Nothing yet', 'none'))

    def test_explicit_title_wins(self):
        tags = {'identifier': {'a1': {'name': 'X', 'youtube': 'https://youtu.be/abc', 'title': 'Bluey S1E1'}}}
        f = rp.build_payload(tags, 'r', 'h', None, now=NOW)['figures'][0]
        self.assertEqual((f['action'], f['title']), ('youtube', 'Bluey S1E1'))


class SecretTests(unittest.TestCase):
    def test_payload_never_contains_a_secret_looking_key(self):
        p = build('guest-room')
        self.assertEqual(rp.find_secret_keys(p), [])
        self.assertNotIn('SECRET', str(p))

    def test_secret_keys_are_stripped_at_any_depth(self):
        safe = rp.strip_secrets({'a': {'appletv_companion_credentials': 'x', 'ok': 1},
                                 'list': [{'api_key': 'y', 'keep': 2}], 'hook_url': 'z'})
        self.assertEqual(safe, {'a': {'ok': 1}, 'list': [{'keep': 2}]})

    def test_build_refuses_a_blob_pasted_into_a_visible_field(self):
        """Keys are fixed, so the only way a credential could reach the
        payload is as a VALUE (a blob pasted into title:/name:). Refused."""
        blob = 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVmZ2hpamtsbW5vcA=='
        tags = {'identifier': {'a1': {'name': 'X', 'yoto': 'c1', 'title': blob}}}
        with self.assertRaises(ValueError):
            rp.build_payload(tags, 'r', 'h', None, now=NOW)
        long_tags = {'identifier': {'a1': {'name': 'y' * 600, 'yoto': 'c1'}}}
        with self.assertRaises(ValueError):
            rp.build_payload(long_tags, 'r', 'h', None, now=NOW)

    def test_pyatv_style_credential_is_refused_wherever_it_sits(self):
        """Real pairing credentials are colon-separated hex segments."""
        cred = ':'.join(['a1b2c3d4e5f6' * 4] * 4)  # 4 x 48 hex, colon-joined, < 500 chars
        for field in ('title', 'name'):
            tags = {'identifier': {'a1': {'name': 'X', 'yoto': 'c1', field: cred}}}
            with self.assertRaises(ValueError, msg=field):
                rp.build_payload(tags, 'r', 'h', None, now=NOW)

    def test_command_lines_never_leave_the_pi(self):
        tags = {'identifier': {'a1': {'name': 'X', 'command': "curl -u admin:hunter2 http://x/api -H 'Authorization: Bearer abc'"}}}
        f = rp.build_payload(tags, 'r', 'h', None, now=NOW)['figures'][0]
        self.assertEqual((f['action'], f['target']), ('command', 'curl'))
        self.assertNotIn('hunter2', str(f))
        tags = {'identifier': {'a1': {'command': 'TOKEN=abc123 /usr/local/bin/notify --all'}}}
        f = rp.build_payload(tags, 'r', 'h', None, now=NOW)['figures'][0]
        self.assertEqual(f['target'], '/usr/local/bin/notify')
        self.assertNotIn('abc123', str(f))

    def test_ordinary_values_pass_the_blob_check(self):
        tags = {'identifier': {'a1': {'name': 'Creeper', 'appletv_url':
                'https://tv.apple.com/us/movie/a-charlie-brown-thanksgiving/umc.cmc.7grdgbjdq3tiy37f4dc3yeyms'}}}
        p = rp.build_payload(tags, 'r', 'h', None, now=NOW)
        self.assertEqual(rp.find_secret_keys(p), [])


class TitleTests(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(rp.title_from_url('https://tv.apple.com/us/movie/monsters-inc/umc.cmc.3ils'), 'Monsters Inc')
        self.assertEqual(rp.title_from_url('https://tv.apple.com/us/show/the-snoopy-show/umc.cmc.x'), 'The Snoopy Show')
        self.assertEqual(rp.title_from_url('https://tv.apple.com/us/movie/a-charlie-brown-thanksgiving/umc.cmc.7'),
                         'A Charlie Brown Thanksgiving')
        self.assertIsNone(rp.title_from_url('https://www.youtube.com/watch?v=dQw4w9WgXcQ'))
        self.assertIsNone(rp.title_from_url('deeplink://whatever'))
        self.assertIsNone(rp.title_from_url(''))


class _Reply:
    def __init__(self, status, text=''):
        self.status_code = status
        self.text = text


class SendTests(unittest.TestCase):
    PAYLOAD = {'room': 'r', 'figures': []}

    def test_2xx_is_success_and_sends_the_key_header(self):
        with mock.patch.object(rp.requests, 'post', return_value=_Reply(200)) as post:
            self.assertTrue(rp.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(post.call_args.kwargs['headers'], {'X-API-Key': 'k'})
        self.assertEqual(post.call_args.kwargs['json'], self.PAYLOAD)

    def test_4xx_is_final(self):
        with mock.patch.object(rp.requests, 'post', return_value=_Reply(401, 'nope')) as post, \
                mock.patch.object(rp.time, 'sleep'):
            self.assertFalse(rp.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(post.call_count, 1)

    def test_5xx_and_connection_errors_retry_then_give_up(self):
        replies = [rp.requests.ConnectionError(), _Reply(503), _Reply(200)]
        with mock.patch.object(rp.requests, 'post', side_effect=replies), \
                mock.patch.object(rp.time, 'sleep') as sleep:
            self.assertTrue(rp.send(self.PAYLOAD, 'https://x/api', 'k'))
        self.assertEqual(sleep.call_count, 2)
        with mock.patch.object(rp.requests, 'post', return_value=_Reply(500)), \
                mock.patch.object(rp.time, 'sleep'):
            self.assertFalse(rp.send(self.PAYLOAD, 'https://x/api', 'k'))


class MainTests(unittest.TestCase):
    def test_no_key_file_means_not_set_up_and_exit_0(self):
        with mock.patch.dict(os.environ, {'MUSICFIG_REPORT_KEY_FILE': '/nonexistent/key'}):
            with mock.patch.object(rp.requests, 'post') as post:
                self.assertEqual(rp.main([]), 0)
        post.assert_not_called()

    def test_unreadable_key_file_is_a_failure_not_silence(self):
        """A key that landed with the wrong owner or as a directory must make
        the unit fail, not log "not set up" forever."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {'MUSICFIG_REPORT_KEY_FILE': d}):
                with mock.patch.object(rp.requests, 'post') as post:
                    self.assertEqual(rp.main([]), 1)
            post.assert_not_called()

    def test_malformed_tags_file_is_a_logged_failure(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.yml', delete=False, encoding='utf-8') as f:
            f.write("identifier:\n  - just\n  - a list\n")
            path = f.name
        try:
            env = {'MUSICFIG_REPORT_KEY_FILE': '/nonexistent/key', 'MUSICFIG_TAGS_FILE': path}
            with mock.patch.dict(os.environ, env), self.assertLogs('report_pad', level='ERROR') as logs:
                self.assertEqual(rp.main(['--dry-run', '--no-titles']), 1)
        finally:
            os.unlink(path)
        self.assertIn('not a musicfig tags file', logs.output[-1])

    def test_title_lookups_stop_after_three_consecutive_misses_only(self):
        calls = []
        answers = {'b': 'T'}

        def lookup(cid):
            calls.append(cid)
            return answers.get(cid)
        titles = rp._titles_until_first_failure(lookup)
        # one deleted card between two good ones does not blank the rest
        self.assertEqual([titles(c) for c in ('a', 'b', 'a', 'b')], [None, 'T', None, 'T'])
        # three misses in a row = the MCP is unreachable; stop asking
        self.assertEqual([titles(c) for c in ('x', 'y', 'z', 'b')], [None, None, None, None])
        self.assertEqual(calls, ['a', 'b', 'a', 'b', 'x', 'y', 'z'])

    def test_device_fallbacks_match_the_pad(self):
        tags = {'default_homepod': 'Office HomePod',
                'identifier': {'a1': {'airplay': 'x.mp3'}, 'a2': {'yoto': 'c1'}}}
        figs = {f['uid']: f for f in rp.build_payload(tags, 'r', 'h', None, now=NOW)['figures']}
        self.assertEqual(figs['a1']['device'], 'Office HomePod')
        self.assertEqual(figs['a2']['device'], '@online')

    def test_dry_run_prints_without_a_key(self):
        import io
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.yml', delete=False, encoding='utf-8') as f:
            f.write("appletv_companion_credentials: SECRET\nidentifier:\n  a1:\n    name: X\n    yoto: c1\n")
            path = f.name
        try:
            env = {'MUSICFIG_REPORT_KEY_FILE': '/nonexistent/key', 'MUSICFIG_TAGS_FILE': path,
                   'MUSICFIG_ROOM': 'kitchen'}
            with mock.patch.dict(os.environ, env), mock.patch('sys.stdout', new=io.StringIO()) as out:
                self.assertEqual(rp.main(['--dry-run', '--no-titles']), 0)
            printed = out.getvalue()
        finally:
            os.unlink(path)
        self.assertIn('"room": "kitchen"', printed)
        self.assertIn('"yoto"', printed)
        self.assertNotIn('SECRET', printed)


if __name__ == '__main__':
    unittest.main()
