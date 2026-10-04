"""Cloud protocol device using SRC's existing screenshot and control contracts."""

import collections
import time

import numpy as np

import module.config.server as server
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import ensure_int, ensure_time, random_rectangle_point
from module.device.device import Device
from module.exception import GameNotRunningError, RequestHumanTakeover, ScriptError
from module.logger import logger

from .client import CloudClient, CloudConnectionError


class CloudDevice(Device):
    def __init__(self, config):
        self.config = config
        self.config.override(Emulator_ScreenshotMethod='cloud_direct', Emulator_ControlMethod='cloud_direct')
        self.serial = 'cloud_direct'
        self.package = 'cloud_direct'
        self.orientation = 0
        self.is_mumu_family = False
        self.detect_record = set()
        self.click_record = collections.deque(maxlen=30)
        self.stuck_timer = Timer(60, count=60).start()
        self._screenshot_interval = Timer(0.1)
        self._screen_size_checked = False
        self._screen_black_checked = False
        server.server = 'CN-Official'
        server.lang = self.config.Emulator_GameLanguage
        from module.webui.setting import State
        if State.cloud_bridge is not None:
            from .runtime import CloudProxy
            self.client = CloudProxy(State.cloud_bridge)
        else:
            self.client = CloudClient(config.config_name, queue_type='coin' if config.Emulator_CloudPriorQueue else '')
        self._session_generation = 0
        self.screenshot_interval_set()

    @property
    def screenshot_method_override(self):
        return 'cloud_direct'

    @cached_property
    def screenshot_methods(self):
        return {'cloud_direct': self.screenshot_cloud}

    @cached_property
    def click_methods(self):
        return {'cloud_direct': self.click_cloud}

    def _checkpoint(self):
        if hasattr(self.client, 'before_action'):
            self.client.before_action()
        generation = getattr(self.client, 'generation', 0)
        if generation != self._session_generation:
            self._session_generation = generation
            self.stuck_record_clear()
            raise GameNotRunningError('Cloud session changed; refresh game page state')

    def screenshot_cloud(self):
        if not self.client.running:
            self.app_start()
        try:
            self._checkpoint()
            return np.array(self.client.capture())
        except CloudConnectionError as exc:
            logger.warning(str(exc))
            raise GameNotRunningError('Cloud video is unavailable') from None

    def app_start(self):
        try:
            if isinstance(self.client, CloudClient):
                self.client.queue_type = 'coin' if self.config.Emulator_CloudPriorQueue else ''
            self.client.start()
            if not self._session_generation:
                self._session_generation = getattr(self.client, 'generation', 0)
            self.stuck_record_clear()
        except CloudConnectionError as exc:
            logger.warning(str(exc))
            raise RequestHumanTakeover(str(exc)) from None

    def app_stop(self):
        try:
            self.client.stop()
            self._session_generation = 0
        except CloudConnectionError as exc:
            logger.warning(str(exc))
            raise RequestHumanTakeover(str(exc)) from None

    def app_current(self):
        return self.package if self.client.running else ''

    def app_is_running(self):
        return self.client.running

    def get_orientation(self):
        return 0  # The negotiated stream is always landscape 1280x720.

    def release_during_wait(self):
        self.app_stop()

    def set_clipboard(self, text):
        try:
            self._checkpoint()
            self.client.clipboard(text)
        except CloudConnectionError as exc:
            raise GameNotRunningError(str(exc)) from None

    def dump_hierarchy(self):
        raise ScriptError('Cloud protocol mode has no Android UI hierarchy')

    def _touch(self, x, y, action):
        try:
            if action != 'up':
                self._checkpoint()
            self.client.touch(x, y, action)
        except CloudConnectionError as exc:
            raise GameNotRunningError(str(exc)) from None

    def click_cloud(self, x, y):
        self._touch(x, y, 'down')
        try:
            time.sleep(0.08)
        finally:
            self._touch(x, y, 'up')

    def long_click(self, button, duration=(1, 1.2)):
        self.handle_control_check(button)
        x, y = ensure_int(*random_rectangle_point(button.button))
        self._touch(x, y, 'down')
        try:
            time.sleep(ensure_time(duration))
        finally:
            self._touch(x, y, 'up')

    def swipe(self, p1, p2, duration=(0.1, 0.2), name='SWIPE', distance_check=True):
        self.handle_control_check(name)
        p1, p2 = ensure_int(p1, p2)
        if distance_check and np.linalg.norm(np.subtract(p1, p2)) < 10:
            return
        seconds = max(0.02, ensure_time(duration))
        steps = max(1, int(seconds / 0.02))
        self._touch(*p1, 'down')
        try:
            for step in range(1, steps + 1):
                time.sleep(seconds / steps)
                point = np.add(p1, np.subtract(p2, p1) * (step / steps))
                self._touch(*point, 'move')
        finally:
            self._touch(*p2, 'up')

    def drag(self, p1, p2, segments=1, shake=(0, 15), point_random=(-10, -10, 10, 10),
             shake_random=(-5, -5, 5, 5), swipe_duration=0.25, shake_duration=0.1, name='DRAG'):
        self.swipe(p1, p2, duration=swipe_duration, name=name, distance_check=False)
