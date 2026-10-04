"""Offline cloud credential and session transition checks.

Run: python -m unittest discover -s tests -p test_cloud_login.py
"""

import asyncio
import base64
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import padding, rsa

from module.device.cloud.account import CloudAccount, CloudAccountError
from module.device.cloud.core.auth import Authenticator
from module.device.cloud.core.config import CoreConfig
from module.device.cloud.core.protocol import Protocol, SdkStartGameParams
from module.device.cloud.core.session import GameSession


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = CloudAccount('src-test', root_dir=self.temp.name)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.public = patch('module.device.cloud.core.auth.load_rsa_public_key', return_value=self.key.public_key())
        self.public.start()
        self.addCleanup(self.public.stop)

    def test_encrypted_login_survives_restart_and_expiry(self):
        self.account.save_password('test@example.invalid', 'test-password')
        self.account.save_cookie('_MHYUUID=original-device; ltoken_v2=expired')
        self.account.save_profile({'browser_profile': {'user_agent': 'TestBrowser/1'}})
        raw = self.account.path.read_text()
        self.assertNotIn('test@example.invalid', raw)
        self.assertNotIn('test-password', raw)
        restarted = CloudAccount('src-test', root_dir=self.temp.name)
        encrypted = restarted.read()['password_login']
        for name, expected in [('account', b'test@example.invalid'), ('password', b'test-password')]:
            self.assertEqual(self.key.decrypt(base64.b64decode(encrypted[name]), padding.PKCS1v15()), expected)
        auth = Authenticator(headers={'user-agent': 'TestBrowser/1'})
        checks = iter([(False, {'retcode': -100}), (True, {'retcode': 0})])
        transmitted = []

        def login_response(session, headers, body, **kwargs):
            transmitted.append(body)
            session.cookies.set('ltoken_v2', 'new-token', domain='.mihoyo.com', path='/')
            return {'retcode': 0, 'data': {'user_info': {}}}, SimpleNamespace(headers={})

        game = SimpleNamespace(authenticator=auth, _apply_credentials=lambda cookie: setattr(auth, 'applied_cookie', cookie))
        with patch.object(auth, 'check', side_effect=lambda cookie: next(checks)), patch.object(auth, '_post_password_login', side_effect=login_response):
            restarted.ensure_login(game)
        self.assertEqual(transmitted, [encrypted])
        self.assertIn('ltoken_v2=new-token', restarted.read()['cookie'])
        self.assertEqual(restarted.read()['profile']['device_profile']['device_id'], 'original-device')
        self.assertEqual(restarted.read()['profile']['browser_profile']['user_agent'], 'TestBrowser/1')
        self.assertEqual(restarted.read()['password_login'], encrypted)

    def test_network_and_risk_failures_never_attempt_password(self):
        self.account.save_password('test@example.invalid', 'test-password')
        self.account.save_cookie('ltoken_v2=existing')
        for code in [-2, -1, -3101, -3235, -999]:
            with self.subTest(code=code):
                auth = Authenticator()
                game = SimpleNamespace(authenticator=auth)
                with patch.object(auth, 'check', return_value=(False, {'retcode': code})), patch.object(auth, 'login_password') as login:
                    with self.assertRaises(CloudAccountError):
                        self.account.ensure_login(game)
                    login.assert_not_called()

    def test_invalid_fresh_cookie_is_not_saved_or_retried(self):
        self.account.save_password('test@example.invalid', 'test-password')
        self.account.save_cookie('ltoken_v2=old')
        original = self.account.read()['cookie']
        auth = Authenticator()
        with patch.object(auth, 'check', side_effect=[(False, {'retcode': -100}), (False, {'retcode': -100})]), patch.object(auth, 'login_password', return_value='ltoken_v2=invalid-new') as login:
            with self.assertRaises(CloudAccountError):
                self.account.ensure_login(SimpleNamespace(authenticator=auth))
            self.assertEqual(login.call_count, 1)
        self.assertEqual(self.account.read()['cookie'], original)

    def test_profile_updates_preserve_identity_and_credentials(self):
        original_id = self.account.read()['profile']['device_profile']['device_id']
        self.account.save_password('test@example.invalid', 'test-password')
        self.account.save_cookie('_MHYUUID=imported-device; ltoken_v2=token')
        self.account.save_profile({'device_profile': {'cpu_cores': 8}, 'browser_profile': {'user_agent_data': {'platform': 'Windows'}}})
        self.assertNotEqual(original_id, self.account.read()['profile']['device_profile']['device_id'])
        self.assertEqual(self.account.read()['profile']['device_profile']['device_id'], 'imported-device')
        self.account.clear_password()
        data = self.account.read()
        self.assertEqual(data['password_login'], {'account': '', 'password': ''})
        self.assertEqual(data['cookie'], '_MHYUUID=imported-device; ltoken_v2=token')
        self.assertEqual(data['profile']['device_profile']['cpu_cores'], 8)
        for name in ['../other', 'src/other', 'CON', '']:
            with self.assertRaises(CloudAccountError):
                CloudAccount(name, root_dir=self.temp.name)
        with self.assertRaises(CloudAccountError):
            self.account.save_cookie('cookie=value\nInjected: header')
        with self.assertRaises(CloudAccountError):
            self.account.save_profile({'device_profile': {'cpu_cores': float('nan')}})
        self.account.path.write_text('')
        with self.assertRaises(CloudAccountError):
            self.account.read()
        self.assertEqual(self.account.path.read_text(), '')


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_exit_waits_for_acknowledgement(self):
        params = SdkStartGameParams(game_token='test-token', sid='test-session', resolution='1280x720')
        packet = Protocol.parse_packet(Protocol.parse_ws_frame(Protocol.stop_game(params))['payload'])
        self.assertEqual(packet['cmd_id'], 20004)
        self.assertEqual(Protocol.proto_fields(packet['message']), [(1, 2, 'test-token')])
        session = GameSession.__new__(GameSession)
        session.params = params
        session._stop_ack = asyncio.Event()
        session._stop_lock = asyncio.Lock()
        sent = []
        closed = asyncio.Event()

        async def send(frame):
            sent.append(frame)

        async def close():
            closed.set()

        session.ws = SimpleNamespace(state=SimpleNamespace(name='OPEN'), close=close)
        session._ws_send = send
        stopping = asyncio.create_task(session.stop(timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        self.assertFalse(closed.is_set())
        await session._handle_stop_game(Protocol.uint32(1, 1))
        await stopping
        await session.stop(timeout=1)
        self.assertEqual(sent, [Protocol.stop_game(params)])
        self.assertTrue(closed.is_set())

    async def test_touch_terminal_is_used_for_start_and_device_report(self):
        params = SdkStartGameParams(game_token='test-token', sid='test-session', resolution='1280x720')
        session = GameSession.__new__(GameSession)
        session.params = params
        session.config = SimpleNamespace(core_config=CoreConfig({'platform_profile': {'mode': 'touch'}, 'device_profile': {'device_id': 'test-device'}}))
        session.device_info_sent = False
        session.ws = object()
        session.stop_event = None
        frames = []

        async def send(frame):
            frames.append(frame)

        async def receive():
            return Protocol.proxy_frame(20002, Protocol.uint32(1, 1))

        session._ws_send = send
        session._ws_recv = receive
        await session._await_start_game_rsp()
        await session._send_device_info_once()
        fields = [{n: v for n, _wire, v in Protocol.proto_fields(Protocol.parse_packet(Protocol.parse_ws_frame(frame)['payload'])['message'])} for frame in frames]
        self.assertEqual(fields[0][4], 9)
        self.assertEqual(fields[1][3], 9)
        self.assertEqual(fields[1][4], 'test-device')


if __name__ == '__main__':
    unittest.main()
