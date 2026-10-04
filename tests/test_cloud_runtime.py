"""Control ownership and safe pause/resume transitions."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from module.device.cloud.client import CloudConnectionError
from module.device.cloud.runtime import CloudRuntime


class LocalClient:
    def __init__(self, *args, **kwargs):
        self.running = True
        self.status = 'Connected'
        self.error = None
        self.queue_type = ''
        self.held = set()
        self.stops = 0

    def touch(self, x, y, action, finger_id=0):
        if action == 'up':
            self.held.discard(finger_id)
        else:
            self.held.add(finger_id)

    def release_touches(self):
        self.held.clear()

    def stop(self):
        self.stops += 1
        self.running = False


class RuntimeControlTests(unittest.TestCase):
    def setUp(self):
        with patch('module.device.cloud.runtime.CloudClient', LocalClient):
            self.runtime = CloudRuntime('control-test')
        self.addCleanup(self.runtime.shutdown)

    def test_control_waits_for_pause_and_polling_keeps_manual_finger_down(self):
        runtime = self.runtime
        runtime.scheduler_acquire()
        runtime.attach('first')
        runtime.attach('second')
        self.assertFalse(runtime.take_control('first'))
        self.assertFalse(runtime.snapshot()['paused'])
        self.assertTrue(runtime._checkpoint())
        self.assertEqual(runtime.snapshot()['control_owner'], 'first')
        self.assertFalse(runtime.take_control('second'))
        action = dict(type='touch', action='down', x=640, y=360, finger_id=0)
        with self.assertRaises(CloudConnectionError):
            runtime.input('second', action)
        runtime.input('first', action)
        for _ in range(3):
            self.assertTrue(runtime._checkpoint())
        self.assertEqual(runtime.client.held, {0})
        generation = runtime.snapshot()['generation']
        runtime.release_control('first')
        state = runtime.snapshot()
        self.assertFalse(state['paused'])
        self.assertIsNone(state['control_owner'])
        self.assertEqual(runtime.client.held, set())
        self.assertGreater(state['generation'], generation)

    def test_departing_pending_viewer_cancels_pause_request(self):
        runtime = self.runtime
        runtime.scheduler_acquire()
        runtime.attach('first')
        runtime.take_control('first')
        runtime.detach('first')
        self.assertFalse(runtime._checkpoint())
        self.assertIsNone(runtime.snapshot()['pending_owner'])
        self.assertIsNone(runtime.snapshot()['control_owner'])

    def test_old_viewer_expiry_cannot_stop_reattached_or_scheduler_session(self):
        runtime = self.runtime
        runtime.attach('first')
        runtime.detach('first')
        expired_epoch = runtime._grace_epoch
        runtime.attach('second')
        runtime._expire_viewers(expired_epoch)
        self.assertEqual(runtime.client.stops, 0)
        runtime.scheduler_acquire()
        runtime.detach('second')
        runtime._expire_viewers(runtime._grace_epoch)
        self.assertEqual(runtime.client.stops, 0)
        self.assertTrue(runtime.client.running)


class DeviceGenerationTests(unittest.TestCase):
    def test_reloaded_config_cannot_switch_protocol_clicks_back_to_adb(self):
        from module.device.cloud.device import CloudDevice
        device = CloudDevice.__new__(CloudDevice)
        sent = []
        device._touch = lambda x, y, action: sent.append(action)
        for _ in range(2):
            config = SimpleNamespace(Emulator_ScreenshotMethod='scrcpy', Emulator_ControlMethod='MaaTouch')
            config.override = lambda **values: config.__dict__.update(values)
            device.config = config
            device.click_methods[device.config.Emulator_ControlMethod](10, 20)
        self.assertEqual(sent, ['down', 'up', 'down', 'up'])
        self.assertEqual(config.Emulator_ScreenshotMethod, 'cloud_direct')

    def test_first_start_accepts_current_session_but_reconnect_invalidates_screen(self):
        from module.device.cloud.device import CloudDevice
        from module.exception import GameNotRunningError
        from module.webui.setting import State
        client = SimpleNamespace(generation=7, start=lambda: None, stop=lambda: None,
                                 before_action=lambda: None)
        config = SimpleNamespace(config_name='device-test', override=lambda **kwargs: None,
                                 Emulator_GameLanguage='cn', Optimization_ScreenshotInterval=0.1,
                                 Emulator_ScreenshotMethod='cloud_direct')
        with patch.object(State, 'cloud_bridge', {}), patch('module.device.cloud.runtime.CloudProxy', return_value=client):
            device = CloudDevice(config)
        device.app_start()
        device._checkpoint()
        client.generation = 8
        with self.assertRaises(GameNotRunningError):
            device._checkpoint()
        device._checkpoint()
        device.app_stop()
        client.generation = 9
        device.app_start()
        device._checkpoint()


if __name__ == '__main__':
    unittest.main()
