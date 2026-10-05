"""Synchronous SRC/WebUI access to one asynchronous cloud session."""

import asyncio
import concurrent.futures
from dataclasses import replace
import logging
import math
import re
import ssl
import threading
import time

from .account import CloudAccount, CloudAccountError
from .core.cloud_game import CloudGame, CloudGameCallbacks
from .core.config import CoreConfig
from .core.models import CloudGameConfig


class CloudConnectionError(RuntimeError):
    """Public error text that never includes remote payloads or credentials."""


def _public_error(exc):
    if isinstance(exc, (CloudAccountError, CloudConnectionError)):
        return str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "Cloud TLS certificate verification failed: %s." % exc.verify_message
    if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
        return "Cloud connection or video frame timed out."
    code = re.search(r"retcode=(-?\d+)", str(exc))
    if code:
        if code[1] == "-110003":
            return "云游戏可用时长不足（retcode=-110003）。请检查官方页面的免费时长、星云币或畅玩卡余额。"
        return "Cloud service rejected the request (retcode=%s)." % code[1]
    return "Cloud connection failed (%s)." % type(exc).__name__


def validate_input(action):
    if not isinstance(action, dict):
        raise ValueError("Invalid cloud input")
    if action.get("type") == "clipboard":
        if set(action) != {"type", "text"} or not isinstance(action["text"], str):
            raise ValueError("Invalid clipboard input")
        if len(action["text"].encode("utf-8")) > 16384 or "\x00" in action["text"]:
            raise ValueError("Clipboard text exceeds the limit")
        return dict(action)
    if action.get("type") != "touch" or set(action) != {"type", "action", "x", "y", "finger_id"}:
        raise ValueError("Invalid touch input")
    if action["action"] not in ("down", "move", "up"):
        raise ValueError("Invalid touch action")
    if type(action["finger_id"]) is not int or not 0 <= action["finger_id"] < 10:
        raise ValueError("Invalid finger id")
    for name, limit in (("x", 1280), ("y", 720)):
        value = action[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < limit:
            raise ValueError("Touch coordinates are outside the frame")
    return dict(action)


class CloudClient:
    def __init__(self, config_name, queue_type="", root_dir=None, auth_handler=None,
                 queue_getter=None, on_frame=None, on_connected=None):
        if queue_type not in ("", "coin"):
            raise ValueError("Unsupported cloud queue type")
        self.account = CloudAccount(config_name, root_dir=root_dir)
        self.queue_type = queue_type
        self.auth_handler = auth_handler
        self.queue_getter = queue_getter
        self.on_frame = on_frame
        self.on_connected = on_connected
        self.status = "Disconnected"
        self.error = None
        self.generation = 0
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._thread = None
        self._loop = None
        self._game = None
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._allocation_uncertain = False
        self._pressed = {}

    @property
    def running(self):
        return self._connected.is_set() and not self._stop_event.is_set()

    def _status(self, message, level=logging.INFO):
        # Remote status messages can contain instance addresses and credentials.
        if "queue" in message.lower() and not self.running:
            self.status = "Waiting in queue"

    def start(self, cancel_event=None):
        """Concurrent callers wait for the same startup, never allocate twice."""
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                raise CloudConnectionError("Cloud connection was cancelled.")
            if self._allocation_uncertain:
                raise CloudConnectionError("Previous cloud instance exit is uncertain; confirm its exit before reconnecting.")
            if self.running:
                return
            if self._thread is None or not self._thread.is_alive():
                self.error = None
                self.status = "Authenticating"
                self._stop_event = cancel_event if cancel_event is not None else threading.Event()
                self._ready = threading.Event()
                self._connected.clear()
                self._thread = threading.Thread(target=self._worker, name="src-cloud-session", daemon=True)
                self._thread.start()
            ready = self._ready
        ready.wait()
        if self.error:
            raise CloudConnectionError(self.error)
        if not self.running:
            raise CloudConnectionError("Cloud connection was cancelled or closed.")

    def _worker(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            if not self._stop_event.is_set() or self.error:
                self.error = self.error or _public_error(exc)
                self.status = self.error
        finally:
            self._connected.clear()
            self._ready.set()
            if not self.error:
                self.status = "Disconnected"

    def _authenticate(self):
        if self.auth_handler is not None:
            self.auth_handler(self._game, cancel_event=self._stop_event)
        else:
            self.account.ensure_login(self._game)

    def _dispatch(self):
        for attempt in range(2):
            queue_type = self.queue_getter() if self.queue_getter else self.queue_type
            if queue_type not in ("", "coin"):
                raise ValueError("Unsupported cloud queue type")
            self.queue_type = queue_type
            self._game.config = replace(self._game.config, queue_type=queue_type)
            self._game._reset_dispatcher()
            try:
                return self._game.dispatch(stop_event=self._stop_event)
            except Exception as exc:
                if self._stop_event.is_set():
                    if str(exc) != "dispatch stopped":
                        self.error = "Cloud queue exit was not acknowledged; check the cloud queue."
                        self._allocation_uncertain = True
                    raise
                if attempt or not isinstance(exc, RuntimeError) or not re.search(r"retcode=-100\b", str(exc)):
                    raise
                self.status = "Authenticating"
                self._authenticate()

    async def _cancel_watcher(self, connection):
        while not self._stop_event.is_set() and not connection.done():
            await asyncio.sleep(0.05)
        if not self._stop_event.is_set():
            return
        deadline = asyncio.get_running_loop().time() + 20
        while not connection.done():
            session = self._game.game_session
            if session is not None and session.ws is not None and session.ws.state.name == "OPEN":
                await session.stop()
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Cloud cancellation timed out")
            await asyncio.sleep(0.05)

    async def _connect_once(self, finish):
        self._game.last_session = None
        connection = asyncio.create_task(self._game.connect(finish_result=finish))
        cancellation = asyncio.create_task(self._cancel_watcher(connection))
        capture = None
        try:
            await asyncio.sleep(0)
            capture = asyncio.create_task(self._game.capture_video_frame(timeout=60))
            done, _ = await asyncio.wait((connection, capture, cancellation), return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done:
                await cancellation
            if connection in done:
                await connection
                if not self._stop_event.is_set():
                    raise ConnectionError("Cloud session ended before video")
                return
            if capture in done:
                frame = capture.result()
                if frame is None:
                    raise TimeoutError("No cloud video frame")
                if frame[0].size != (1280, 720):
                    raise ValueError("Cloud stream resolution is not 1280x720")
                if not self._stop_event.is_set():
                    self._connected.set()
                    self.generation += 1
                    if self.on_connected:
                        self.on_connected()
                    self.status = "Connected"
                    self._ready.set()
            done, _ = await asyncio.wait((connection, cancellation), return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done:
                await cancellation
            await connection
            if not self._stop_event.is_set():
                raise ConnectionError("Cloud transport ended")
        finally:
            self._ready.clear()
            self._connected.clear()
            self.release_touches()
            for task in (capture, cancellation, connection):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (capture, cancellation, connection) if task is not None), return_exceptions=True)

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        profile = self.account.read()["profile"]
        profile.setdefault("platform_profile", {})["mode"] = "touch"
        profile.setdefault("session_profile", {}).update(resolution="1280x720", fps=30)
        self._game = CloudGame(
            CloudGameConfig(core_config=CoreConfig(profile), root_dir=self.account.root_dir,
                            queue_type=self.queue_type, ws_log_payload=False,
                            video_frame_interval=0.1 if self.on_frame else None),
            callbacks=CloudGameCallbacks(on_status=self._status, on_video_frame=self.on_frame),
        )
        finish = None
        try:
            await asyncio.to_thread(self._authenticate)
            if self._stop_event.is_set():
                return
            self.status = "Waiting in queue"
            finish = await asyncio.to_thread(self._dispatch)
            retries = 0
            while True:
                self.status = "Disconnecting" if self._stop_event.is_set() else "Connecting"
                try:
                    await self._connect_once(finish)
                    return
                except Exception as exc:
                    session = self._game.last_session
                    released = session is not None and session._stop_ack.is_set()
                    if released and not self._stop_event.is_set():
                        raise CloudConnectionError("The cloud service ended this session. Check cloud time or account restrictions before connecting again.") from None
                    if self._stop_event.is_set():
                        if not released:
                            self._allocation_uncertain = True
                            self.error = "Cloud exit was not acknowledged; check remaining cloud time."
                        return
                    # Business/auth rejections do not prove the old allocation gone.
                    if not isinstance(exc, (ConnectionError, OSError, TimeoutError)) or retries >= 3:
                        self._allocation_uncertain = not released
                        if self._allocation_uncertain:
                            raise CloudConnectionError(
                                _public_error(exc) + " Previous instance exit is uncertain; no new instance was allocated."
                            ) from None
                        raise
                    self.status = "Reconnecting"
                    delay = (1, 2, 4)[retries]
                    retries += 1
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline and not self._stop_event.is_set():
                        await asyncio.sleep(0.05)
                    # Reattach only to the existing dispatch result. An unknown
                    # rejection is never evidence authorizing another allocation.
        finally:
            self._connected.clear()
            if finish is not None:
                session = self._game.last_session
                self._allocation_uncertain = session is None or not session._stop_ack.is_set()
            self._game.dispatcher.close()

    def wallet_info(self):
        """Read balances using the active session, or HTTP only when disconnected."""
        game = self._game if self.running else None
        own_game = game is None
        try:
            if own_game:
                profile = self.account.read()["profile"]
                profile.setdefault("platform_profile", {})["mode"] = "touch"
                game = CloudGame(CloudGameConfig(core_config=CoreConfig(profile), root_dir=self.account.root_dir))
                self.account.ensure_login(game)
                return game.get_wallet_info()["summary"]
            return game.dispatcher.wallet_info()["summary"]
        except Exception as exc:
            raise CloudConnectionError(_public_error(exc)) from None
        finally:
            if own_game and game is not None:
                game.dispatcher.close()

    def capture(self, timeout=5):
        if not 0 < timeout <= 60:
            raise ValueError("Invalid capture timeout")
        if not self.running or self._loop is None or not self._loop.is_running():
            raise CloudConnectionError(self.error or "Cloud session is not connected.")
        future = asyncio.run_coroutine_threadsafe(self._game.capture_video_frame(timeout=timeout), self._loop)
        try:
            frame = future.result(timeout=timeout + 1)
        except Exception as exc:
            future.cancel()
            raise CloudConnectionError(_public_error(exc)) from None
        if frame is None:
            raise CloudConnectionError("Cloud video frame timed out.")
        return frame[0]

    def _input_ready(self):
        session = self._game.game_session if self._game else None
        if not self.running or session is None or not (session.has_game_control_channel or session.has_rtc_channel):
            raise CloudConnectionError("Cloud input channel is not ready.")

    def touch(self, x, y, action, finger_id=0):
        x, y = float(x), float(y)
        validate_input(dict(type="touch", x=x, y=y, action=action, finger_id=finger_id))
        with self._lock:
            self._input_ready()
            if not self._game.send_touch(float(x) / 1280, float(y) / 720, action, finger_id=finger_id, is_primary=finger_id == 0):
                raise CloudConnectionError("Cloud touch input could not be queued.")
            if action == "up":
                self._pressed.pop(finger_id, None)
            else:
                self._pressed[finger_id] = (x, y)

    def clipboard(self, text):
        validate_input(dict(type="clipboard", text=text))
        with self._lock:
            self._input_ready()
            if not self._game.send_input({"type": "clipboard", "text": text}):
                raise CloudConnectionError("Cloud clipboard input could not be queued.")

    def release_touches(self):
        with self._lock:
            for finger_id, (x, y) in self._pressed.items():
                if self._game is not None:
                    self._game.send_touch(x / 1280, y / 720, "up", finger_id=finger_id, is_primary=finger_id == 0)
            self._pressed.clear()

    def stop(self):
        """Interrupt queued startup or wait for acknowledged remote exit."""
        with self._stop_lock:
            with self._lock:
                self.release_touches()
                thread = self._thread
                if thread is None or not thread.is_alive():
                    if self._allocation_uncertain:
                        raise CloudConnectionError(self.error or "Previous cloud instance exit is uncertain.")
                    return
                self.status = "Disconnecting"
                self._stop_event.set()
            thread.join(timeout=65)
            if thread.is_alive():
                self.error = "Cloud connection is still closing; do not start another session."
                self.status = self.error
                raise CloudConnectionError(self.error)
            if self._allocation_uncertain:
                raise CloudConnectionError(self.error or "Cloud exit was not acknowledged; check remaining cloud time.")
