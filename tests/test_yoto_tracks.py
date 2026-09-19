"""yoto_tracks.fetch_tracks: reply parsing and failure modes (no network)."""
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


if __name__ == '__main__':
    unittest.main()
