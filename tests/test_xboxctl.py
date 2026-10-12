"""xboxctl: every action targets the pad's own xbox_console_id (life-ops #129)
so a game-room figure never wakes the basement Xbox. Fake client, no network."""
import asyncio
import importlib.util
import shutil
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


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP %d' % self.status_code)

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class _FakeSmartglass:
    SG_URL = 'https://sg.test'
    HEADERS_SG = {'x-xbl-contract-version': '4'}
    _smartglass_session_id = 'sess'

    def __init__(self, consoles):
        self.consoles = consoles
        self.calls = []

    async def get_console_list(self):
        self.calls.append(('list',))
        return mock.Mock(result=self.consoles)

    async def get_console_status(self, cid):
        self.calls.append(('status', cid))
        return {'id': cid}


class _FakeSession:
    """client.session: records every /commands POST on the smartglass fake as
    (command, console[, product id]) and answers with the next queued Live reply
    (default: delivered)."""
    def __init__(self, sg):
        self.sg = sg
        self.replies = []

    async def post(self, url, json=None, headers=None):
        assert url == 'https://sg.test/commands' and headers == self.sg.HEADERS_SG
        if json['command'] == 'ActivateApplicationWithOneStoreProductId':
            self.sg.calls.append(('launch', json['linkedXboxId'], json['parameters'][0]['oneStoreProductId']))
        else:
            self.sg.calls.append((json['command'], json['linkedXboxId']))
        payload = self.replies.pop(0) if self.replies else {'status': {'errorCode': 'OK'}}
        return payload if isinstance(payload, _FakeResponse) else _FakeResponse(payload)


class XboxTargetTests(unittest.TestCase):
    def setUp(self):
        self.sg = _FakeSmartglass([_Console('AAAA', 'Game Room Xbox'), _Console('BBBB', 'Basement Xbox')])
        self.session = _FakeSession(self.sg)
        client = mock.Mock(smartglass=self.sg, session=self.session)

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
        self.assertEqual(self.sg.calls, [('WakeUp', 'BBBB'), ('launch', 'BBBB', 'app.one'), ('TurnOff', 'BBBB'), ('status', 'BBBB')])

    def test_explicit_console_wins_over_the_configured_one(self):
        xb.load_config({'xbox_console_id': 'BBBB'})
        self.run_(xb.power_on('AAAA'))
        self.run_(xb.launch_app('app.one', 'AAAA'))
        self.assertEqual(self.sg.calls, [('WakeUp', 'AAAA'), ('launch', 'AAAA', 'app.one')])

    def test_no_console_anywhere_falls_back_to_the_account_list(self):
        xb.load_config({'xbox_client_id': 'c'})          # no xbox_console_id
        self.run_(xb.power_on())                          # wakes every console, as before
        self.run_(xb.launch_app('app.one'))               # first console
        self.assertEqual(self.sg.calls, [('list',), ('WakeUp', 'AAAA'), ('WakeUp', 'BBBB'), ('list',), ('launch', 'AAAA', 'app.one')])

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
        self.assertEqual(self.sg.calls, [('WakeUp', 'BBBB'), ('launch', 'BBBB', 'app.one')])


if __name__ == '__main__':
    unittest.main()


class XboxRobustnessTests(unittest.TestCase):
    """The three failures the first live card tap hit (2026-10-12): a stale
    access token, a cached client on a closed loop, and xbox-webapi choking
    on Xbox Live's newer reply after the command was already accepted."""

    def setUp(self):
        xb._config = {}
        xb._xbl_client = xb._http_client = None

    def _client(self, replies):
        sg = _FakeSmartglass([_Console('AAAA', 'Game Room Xbox')])
        session = _FakeSession(sg)
        session.replies = list(replies)
        client = mock.Mock(smartglass=sg, session=session)

        async def fake_client():
            return client
        mock.patch.object(xb, '_get_authenticated_client', fake_client).start()
        self.addCleanup(mock.patch.stopall)
        xb.load_config({'xbox_console_id': 'AAAA'})
        return sg

    def test_wake_is_a_failure_when_live_could_not_deliver_it(self):
        """The 2026-10-12 case: HTTP 200, destination null, ErrorCallingWNS -
        the console was shut down. Must read as a failure (red flash)."""
        sg = self._client([{'destination': None, 'opId': 'x',
                            'status': {'errorCode': 'ErrorCallingWNS', 'errorMessage': 'Send command failed'}}])
        with self.assertLogs(xb.logger, level='ERROR') as cm:
            self.assertFalse(asyncio.run(xb.power_on()))
        self.assertTrue(any('ErrorCallingWNS' in m for m in cm.output))
        self.assertEqual(sg.calls, [('WakeUp', 'AAAA')])

    def test_commands_succeed_only_on_an_ok_status(self):
        sg = self._client([{'status': {'errorCode': 'OK'}}, {'opId': 'x'}, None, {'status': {'errorCode': 'OK'}}])
        self.assertTrue(asyncio.run(xb.power_on()))              # OK
        self.assertFalse(asyncio.run(xb.launch_app('app.one')))  # no status at all
        self.assertFalse(asyncio.run(xb.power_off()))            # not JSON
        self.assertTrue(asyncio.run(xb.launch_app('app.one')))   # OK
        self.assertEqual(sg.calls, [('WakeUp', 'AAAA'), ('launch', 'AAAA', 'app.one'),
                                    ('TurnOff', 'AAAA'), ('launch', 'AAAA', 'app.one')])

    def test_http_error_on_the_command_is_a_failure(self):
        """A 5xx from Live goes through _send's own raise_for_status."""
        sg = self._client([])
        session = xb._get_authenticated_client  # patched by _client; grab the fake session via a call
        client = asyncio.run(session())
        client.session.replies = [_FakeResponse({'status': {'errorCode': 'OK'}}, status=503)]
        with self.assertLogs(xb.logger, level='ERROR') as cm:
            self.assertFalse(asyncio.run(xb.power_on()))
        self.assertTrue(any('HTTP 503' in m for m in cm.output))
        self.assertEqual(sg.calls, [('WakeUp', 'AAAA')])

    def test_corrupt_token_file_is_a_logged_failure_not_a_crash(self):
        import os
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        token_file = os.path.join(tmp, 'xbox_tokens.json')
        with open(token_file, 'w') as f:
            f.write('{not json')
        with mock.patch.object(xb, 'TOKEN_FILE', token_file), self.assertLogs(xb.logger, level='ERROR'):
            self.assertIsNone(asyncio.run(xb._get_authenticated_client()))
        with open(token_file, 'w') as f:
            f.write('{"access_token": "a"}')   # no refresh_token
        with mock.patch.object(xb, 'TOKEN_FILE', token_file), self.assertLogs(xb.logger, level='ERROR'):
            self.assertIsNone(asyncio.run(xb._get_authenticated_client()))

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
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(lambda: setattr(xb, '_xbl_client', None))
        self.addCleanup(lambda: setattr(xb, '_http_client', None))
        with open(token_file, 'w') as f:
            json.dump({'access_token': 'old', 'refresh_token': 'r', 'client_id': 'c', 'user_id': 'u'}, f)
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
        with open(token_file) as f:
            self.assertEqual(json.load(f)['access_token'], 'new')
