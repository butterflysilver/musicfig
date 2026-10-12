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
