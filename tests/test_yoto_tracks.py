"""yoto_tracks: fetch_tracks reply parsing, and play_card / player_command
against the Yoto MCP player routes - failure modes included, no network."""
import importlib.util
import os
import unittest
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), '..', 'app', 'yoto_tracks.py')
_spec = importlib.util.spec_from_file_location('musicfig_yoto_tracks', _PATH)
yt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(yt)


class _Reply:
    def __init__(self, status, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._payload


GOOD = {
    "card_id": "j5VA8",
    "title": "Frozen",
    "tracks": [
        {"title": "One", "url": "https://signed.example/1.mp3", "playable": True},
        {"title": "Two", "url": "yoto:#abc", "playable": False},
        {"title": None, "url": "https://signed.example/3.mp3", "playable": True},
    ],
}


class FetchTracksTests(unittest.TestCase):
    def setUp(self):
        self.key = mock.patch.object(yt, "_shared_key", return_value="secret-for-tests")
        self.key.start()
        self.addCleanup(self.key.stop)

    def test_playable_tracks_in_order(self):
        with mock.patch.object(yt.requests, "get", return_value=_Reply(200, GOOD)) as get:
            tracks = yt.fetch_tracks("j5VA8")
        self.assertEqual(tracks, [("One", "https://signed.example/1.mp3"),
                                  ("track 3", "https://signed.example/3.mp3")])
        url = get.call_args.args[0]
        self.assertTrue(url.endswith("/api/cards/j5VA8/tracks"))
        self.assertEqual(get.call_args.kwargs["headers"], {yt.HEADER: "secret-for-tests"})

    def test_http_failures_return_empty(self):
        for status in (401, 404, 500, 503):
            with mock.patch.object(yt.requests, "get", return_value=_Reply(status, {})):
                self.assertEqual(yt.fetch_tracks("j5VA8"), [], status)

    def test_bad_json_and_bad_shape_return_empty(self):
        with mock.patch.object(yt.requests, "get", return_value=_Reply(200, bad_json=True)):
            self.assertEqual(yt.fetch_tracks("j5VA8"), [])
        with mock.patch.object(yt.requests, "get", return_value=_Reply(200, {"tracks": "nope"})):
            self.assertEqual(yt.fetch_tracks("j5VA8"), [])

    def test_network_error_returns_empty(self):
        with mock.patch.object(yt.requests, "get", side_effect=yt.requests.ConnectionError()):
            self.assertEqual(yt.fetch_tracks("j5VA8"), [])

    def test_missing_key_or_id_returns_empty(self):
        self.assertEqual(yt.fetch_tracks("   "), [])
        with mock.patch.object(yt, "_shared_key", return_value=None):
            with mock.patch.object(yt.requests, "get") as get:
                self.assertEqual(yt.fetch_tracks("j5VA8"), [])
                get.assert_not_called()

    def test_card_id_is_url_quoted(self):
        with mock.patch.object(yt.requests, "get", return_value=_Reply(200, GOOD)) as get:
            yt.fetch_tracks("a/b c")
        self.assertTrue(get.call_args.args[0].endswith("/api/cards/a%2Fb%20c/tracks"))


PLAYING = {"action": "play", "player": {"id": "y3-001", "name": "Momo's Library", "online": True},
           "card_id": "j5VA8", "title": "Frozen"}


class PlayerRoutesTests(unittest.TestCase):
    def setUp(self):
        self.key = mock.patch.object(yt, "_shared_key", return_value="secret-for-tests")
        self.key.start()
        self.addCleanup(self.key.stop)

    def test_play_card_defaults_to_any_online_player(self):
        with mock.patch.object(yt.requests, "post", return_value=_Reply(200, PLAYING)) as post:
            playing = yt.play_card("j5VA8")
        self.assertEqual(playing, {"id": "y3-001", "name": "Momo's Library", "title": "Frozen"})
        self.assertTrue(post.call_args.args[0].endswith("/api/players/%40online/play"))
        self.assertEqual(post.call_args.kwargs["json"], {"card_id": "j5VA8"})
        self.assertEqual(post.call_args.kwargs["headers"], {yt.HEADER: "secret-for-tests"})

    def test_play_card_by_name_is_url_quoted(self):
        with mock.patch.object(yt.requests, "post", return_value=_Reply(200, PLAYING)) as post:
            yt.play_card("j5VA8", " Momo's Library ")
        self.assertTrue(post.call_args.args[0].endswith("/api/players/Momo%27s%20Library/play"))

    def test_play_card_failures_return_none(self):
        for status, body in ((409, {"error": "player offline", "player": {"name": "Mini"}}),
                             (404, {"error": "card not found: j5VA8"}), (401, {}), (502, {}),
                             (200, {"action": "play"})):          # reply without a player id
            with mock.patch.object(yt.requests, "post", return_value=_Reply(status, body)):
                self.assertIsNone(yt.play_card("j5VA8"), status)
        with mock.patch.object(yt.requests, "post", return_value=_Reply(200, bad_json=True)):
            self.assertIsNone(yt.play_card("j5VA8"))
        with mock.patch.object(yt.requests, "post", side_effect=yt.requests.ConnectionError()):
            self.assertIsNone(yt.play_card("j5VA8"))

    def test_play_card_needs_a_card_id_and_a_key(self):
        with mock.patch.object(yt.requests, "post") as post:
            self.assertIsNone(yt.play_card("  "))
            with mock.patch.object(yt, "_shared_key", return_value=None):
                self.assertIsNone(yt.play_card("j5VA8"))
            post.assert_not_called()

    def test_player_command(self):
        with mock.patch.object(yt.requests, "post", return_value=_Reply(200, {"action": "stop"})) as post:
            self.assertTrue(yt.player_command("y3-001", "stop"))
        self.assertTrue(post.call_args.args[0].endswith("/api/players/y3-001/stop"))
        self.assertIsNone(post.call_args.kwargs["json"])
        with mock.patch.object(yt.requests, "post", return_value=_Reply(409, {"error": "player offline"})):
            self.assertFalse(yt.player_command("y3-001", "pause"))
        with mock.patch.object(yt.requests, "post") as post:
            self.assertFalse(yt.player_command("y3-001", "play"))     # play goes through play_card
            self.assertFalse(yt.player_command("y3-001", "eject"))
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
