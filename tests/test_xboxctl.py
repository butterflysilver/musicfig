"""xboxctl: every action targets the pad's own xbox_console_id (life-ops #129)
so a game-room figure never wakes the basement Xbox. Fake client, no network."""
import asyncio
import importlib.util
import os
import unittest
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), '..', 'app', 'xboxctl.py')
_spec = importlib.util.spec_from_file_location('musicfig_xboxctl', _PATH)
xb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xb)


class _Console:
    def __init__(self, cid, name):
        self.id, self.name = cid, name


class _FakeSmartglass:
    def __init__(self, consoles):
        self.consoles = consoles
        self.calls = []

    async def get_console_list(self):
        self.calls.append(('list',))
        return mock.Mock(result=self.consoles)

    async def wake_up(self, cid):
        self.calls.append(('wake', cid))

    async def turn_off(self, cid):
        self.calls.append(('off', cid))

    async def launch_app(self, cid, app):
        self.calls.append(('launch', cid, app))

    async def get_console_status(self, cid):
        self.calls.append(('status', cid))
        return {'id': cid}


class XboxTargetTests(unittest.TestCase):
    def setUp(self):
        self.sg = _FakeSmartglass([_Console('AAAA', 'Game Room Xbox'), _Console('BBBB', 'Basement Xbox')])
        client = mock.Mock(smartglass=self.sg)

        async def fake_client():
            return client
        mock.patch.object(xb, '_get_authenticated_client', fake_client).start()
        self.addCleanup(mock.patch.stopall)
        xb._config = {}

    def run_(self, coro):
        return asyncio.run(coro)

    def test_configured_console_is_the_default_target(self):
        xb.load_config({'xbox_client_id': 'c', 'xbox_console_id': 'BBBB'})
        self.assertTrue(self.run_(xb.power_on()))
        self.assertTrue(self.run_(xb.launch_app('app.one')))
        self.assertTrue(self.run_(xb.power_off()))
        self.run_(xb.get_console_status())
        self.assertEqual(self.sg.calls, [('wake', 'BBBB'), ('launch', 'BBBB', 'app.one'), ('off', 'BBBB'), ('status', 'BBBB')])

    def test_explicit_console_wins_over_the_configured_one(self):
        xb.load_config({'xbox_console_id': 'BBBB'})
        self.run_(xb.power_on('AAAA'))
        self.run_(xb.launch_app('app.one', 'AAAA'))
        self.assertEqual(self.sg.calls, [('wake', 'AAAA'), ('launch', 'AAAA', 'app.one')])

    def test_no_console_anywhere_falls_back_to_the_account_list(self):
        xb.load_config({'xbox_client_id': 'c'})          # no xbox_console_id
        self.run_(xb.power_on())                          # wakes every console, as before
        self.run_(xb.launch_app('app.one'))               # first console
        self.assertEqual(self.sg.calls, [('list',), ('wake', 'AAAA'), ('wake', 'BBBB'), ('list',), ('launch', 'AAAA', 'app.one')])

    def test_console_id_is_normalised(self):
        xb.load_config({'xbox_console_id': ' BBBB '})
        self.assertEqual(xb._target(None), 'BBBB')
        xb.load_config({'xbox_console_id': 1234})         # YAML may hand back a number
        self.assertEqual(xb._target(None), '1234')
        xb.load_config({})
        self.assertIsNone(xb._target(None))

    def test_sync_wrappers_use_the_configured_console(self):
        xb.load_config({'xbox_console_id': 'BBBB'})
        self.assertTrue(xb.sync_power_on())
        self.assertTrue(xb.sync_launch_app('app.one'))
        self.assertEqual(self.sg.calls, [('wake', 'BBBB'), ('launch', 'BBBB', 'app.one')])


if __name__ == '__main__':
    unittest.main()


class XboxRobustnessTests(unittest.TestCase):
    """The three failures the first live card tap hit (2026-10-12): a stale
    access token, a cached client on a closed loop, and xbox-webapi choking
    on Xbox Live's newer reply after the command was already accepted."""

    def setUp(self):
        xb._config = {}
        xb._xbl_client = xb._http_client = None

    def test_reply_validation_error_counts_as_sent_but_nothing_else_does(self):
        from pydantic import BaseModel, ValidationError

        class CommandResponse(BaseModel):
            result: str

        class Other(BaseModel):
            result: str

        try:
            CommandResponse(result=None)
        except ValidationError as e:
            reply_error = e
        try:
            Other(result=None)
        except ValidationError as e:
            other_error = e
        self.assertTrue(xb._command_was_sent(reply_error))
        self.assertFalse(xb._command_was_sent(other_error))
        self.assertFalse(xb._command_was_sent(RuntimeError('Event loop is closed')))

    def test_power_on_reports_success_when_only_the_reply_was_unparseable(self):
        from pydantic import BaseModel, ValidationError

        class CommandResponse(BaseModel):
            result: str

        async def wake_up(cid):
            CommandResponse(result=None)
        client = mock.Mock(smartglass=mock.Mock(wake_up=wake_up))

        async def fake_client():
            return client
        with mock.patch.object(xb, '_get_authenticated_client', fake_client):
            xb.load_config({'xbox_console_id': 'AAAA'})
            self.assertTrue(asyncio.run(xb.power_on()))

    def test_run_forgets_the_cached_client_and_closes_it(self):
        closed = []

        class FakeHttp:
            async def aclose(self):
                closed.append(True)

        async def action():
            xb._http_client = FakeHttp()
            xb._xbl_client = object()
            return 'done'
        self.assertEqual(xb._run(action()), 'done')
        self.assertEqual(closed, [True])
        self.assertIsNone(xb._http_client)
        self.assertIsNone(xb._xbl_client)

    def test_authenticated_client_refreshes_the_oauth_token_before_use(self):
        import json, tempfile, os
        tmp = tempfile.mkdtemp()
        token_file = os.path.join(tmp, 'xbox_tokens.json')
        json.dump({'access_token': 'old', 'refresh_token': 'r', 'client_id': 'c', 'user_id': 'u'}, open(token_file, 'w'))
        calls = []

        class FakeMgr:
            def __init__(self, *a, **k):
                self.oauth = None

            async def refresh_oauth_token(self):
                calls.append('refresh_oauth')
                return mock.Mock(access_token='new', refresh_token='r2', token_type='bearer', expires_in=3600, scope='s', user_id='u')

            async def refresh_tokens(self):
                calls.append(('refresh_tokens', self.oauth.access_token))

        fake_mod = mock.Mock(AuthenticationManager=FakeMgr)
        import sys
        with mock.patch.object(xb, 'TOKEN_FILE', token_file), \
                mock.patch.dict(sys.modules, {'xbox.webapi.authentication.manager': fake_mod,
                                              'xbox.webapi.api.client': mock.Mock(XboxLiveClient=lambda mgr: 'client'),
                                              'xbox.webapi.authentication.models': mock.Mock(OAuth2TokenResponse=lambda **k: mock.Mock(**k))}):
            xb.load_config({'xbox_client_id': 'c', 'xbox_client_secret': 's'})
            self.assertEqual(asyncio.run(xb._get_authenticated_client()), 'client')
        self.assertEqual(calls, ['refresh_oauth', ('refresh_tokens', 'new')])
        self.assertEqual(json.load(open(token_file))['access_token'], 'new')
        xb._xbl_client = xb._http_client = None
