"""Authentication challenge binding, cancellation, and stale-result checks."""

import base64
import concurrent.futures
import json
import tempfile
import time
import unittest
from types import SimpleNamespace

from module.device.cloud.account import CloudAccountError
from module.device.cloud.login import CloudLogin


class HumanAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.login = CloudLogin('auth-test', root_dir=self.temp.name)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.addCleanup(self.login.cancel)

    def challenge(self, use_v4):
        token, cancel, _done = self.login._new_attempt()
        future = self.pool.submit(self.login._aigis, {'session_id': 'test-session', 'data': {
            'gt': 'test-captcha', 'use_v4': use_v4, 'challenge': 'test-challenge'}}, token, cancel)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.login.snapshot()['state'] == 'captcha_required':
                return self.login.snapshot()['challenge_id'], future
            time.sleep(0.01)
        self.login.cancel()
        future.result(timeout=1)
        self.fail('Captcha state was not published')

    def test_captcha_results_are_bound_and_single_use(self):
        for use_v4 in (False, True):
            with self.subTest(use_v4=use_v4):
                challenge_id, future = self.challenge(use_v4)
                result = {'captcha_id': 'test-captcha', 'lot_number': 'lot', 'pass_token': 'pass',
                          'gen_time': '1', 'captcha_output': 'output'} if use_v4 else {
                    'geetest_challenge': 'test-challenge-extra', 'geetest_validate': 'valid', 'geetest_seccode': 'valid|jordan'}
                with self.assertRaises(CloudAccountError):
                    self.login.submit_captcha('other-id', result)
                with self.assertRaises(CloudAccountError):
                    self.login.submit_captcha(challenge_id, {**result, 'extra': 'forbidden'})
                mismatched = dict(result)
                mismatched['captcha_id' if use_v4 else 'geetest_challenge'] = 'other-challenge'
                with self.assertRaises(CloudAccountError):
                    self.login.submit_captcha(challenge_id, mismatched)
                self.login.submit_captcha(challenge_id, result)
                header = future.result(timeout=2)
                self.assertEqual(json.loads(base64.b64decode(header.split(';', 1)[1])), result)
                with self.assertRaises(CloudAccountError):
                    self.login.submit_captcha(challenge_id, result)

    def test_cancelling_pending_challenge_never_persists_credentials(self):
        self.login.account.save_cookie('ltoken_v2=original-cookie')
        challenge_id, future = self.challenge(True)
        self.login.cancel(challenge_id)
        with self.assertRaises(CloudAccountError):
            future.result(timeout=2)
        self.assertEqual(self.login.account.read()['cookie'], 'ltoken_v2=original-cookie')
        self.assertIsNone(self.login.snapshot()['challenge'])

    def test_superseded_login_cannot_commit_a_late_success(self):
        self.login.account.save_cookie('ltoken_v2=original-cookie')
        old_token, old_cancel, _done = self.login._new_attempt()
        self.login._new_attempt()
        applied = []
        game = SimpleNamespace(_apply_credentials=applied.append)
        with self.assertRaises(CloudAccountError):
            self.login._commit(game, old_token, old_cancel, None, 'ltoken_v2=late-cookie', True)
        self.assertEqual(applied, [])
        self.assertEqual(self.login.account.read()['cookie'], 'ltoken_v2=original-cookie')

    def test_expired_challenge_rejects_new_validation(self):
        challenge_id, future = self.challenge(True)
        try:
            with self.login._lock:
                self.login._challenge['expires_at'] = 0
            with self.assertRaises(CloudAccountError):
                self.login.submit_captcha(challenge_id, {'captcha_id': 'test-captcha', 'lot_number': 'lot',
                                                        'pass_token': 'pass', 'gen_time': '1', 'captcha_output': 'output'})
        finally:
            self.login.cancel()
            with self.assertRaises(CloudAccountError):
                future.result(timeout=2)


if __name__ == '__main__':
    unittest.main()
