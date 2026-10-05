"""One WebUI-owned cloud connection and a spawn-safe scheduler bridge."""

import io
import queue
import threading
import time
import uuid

from module.logger import logger

from .client import CloudClient, CloudConnectionError, _public_error, validate_input

_runtimes = {}
_runtimes_lock = threading.Lock()


def get_runtime(config_name):
    # CloudAccount validates the name before any connection or bridge is created.
    with _runtimes_lock:
        if config_name not in _runtimes:
            _runtimes[config_name] = CloudRuntime(config_name)
        return _runtimes[config_name]


def shutdown_all():
    with _runtimes_lock:
        runtimes = list(_runtimes.values())
    for runtime in runtimes:
        runtime.shutdown()


class CloudRuntime:
    def __init__(self, config_name):
        self.config_name = config_name
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._stop_pending = threading.Event()
        self._heartbeat_expired = False
        self._viewers = set()
        self._scheduler = False
        self._scheduler_allowed = False
        self._pause_requested = False
        self._paused = False
        self._control_owner = None
        self._pending_owner = None
        self._pressed = {}
        self._generation = 0
        self._jpeg = None
        self._frame_at = 0.0
        self._fps = 0.0
        self._fps_count = 0
        self._fps_started = time.monotonic()
        self._connect_thread = None
        self._closing = False
        self._error = None
        self._grace_timer = None
        self._grace_epoch = 0
        self._bridge = None
        self._shutdown = threading.Event()
        self._capture_slots = threading.BoundedSemaphore(4)
        self.client = CloudClient(config_name, auth_handler=self._authenticate,
                                  queue_getter=self._queue_type, on_frame=self._frame,
                                  on_connected=self._connected)

    def _authenticate(self, game, cancel_event=None):
        from .login import get_login
        get_login(self.config_name).authenticate(game, cancel_event=cancel_event)

    def _queue_type(self):
        from module.config.utils import read_file
        config = read_file(str(self.client.account.root_dir / "config" / (self.config_name + ".json")))
        # Only the boolean opt-in authorizes a paid allocation.
        enabled = config.get("Alas", {}).get("Emulator", {}).get("CloudPriorQueue", False)
        return "coin" if enabled is True else ""

    def _frame(self, image, count):
        now = time.monotonic()
        with self._lock:
            if now - self._frame_at < 0.1:
                return
        if image.size != (1280, 720):
            return
        stream = io.BytesIO()
        image.save(stream, "JPEG", quality=75)
        with self._lock:
            self._jpeg = stream.getvalue()
            self._frame_at = now
            self._fps_count += 1
            elapsed = now - self._fps_started
            if elapsed >= 1:
                self._fps = self._fps_count / elapsed
                self._fps_count = 0
                self._fps_started = now

    def _connected(self):
        with self._lock:
            self._generation += 1
            self._pressed.clear()
            self._control_owner = self._pending_owner = None
            self._paused = self._pause_requested = False

    @property
    def bridge(self):
        from module.webui.setting import State
        with self._lock:
            if self._bridge is None:
                if State.manager is None:
                    raise RuntimeError("The cloud bridge requires the WebUI manager")
                self._bridge = {"commands": State.manager.Queue(128),
                                "replies": State.manager.dict(),
                                "state": State.manager.dict(),
                                "heartbeat": State.manager.dict(at=time.monotonic())}
                self._bridge["state"].update(self.snapshot())
                threading.Thread(target=self._commands, name="cloud-bridge", daemon=True).start()
                threading.Thread(target=self._publish, name="cloud-status", daemon=True).start()
            return self._bridge

    def snapshot(self):
        with self._lock:
            self._grant_control()
            return {"state": "Disconnecting" if self._closing else self.client.status,
                    "running": self.client.running, "error": self._error or self.client.error,
                    "connected": self.client.running,
                    "busy": self._closing or self.client.status in ("Authenticating", "Waiting in queue", "Connecting", "Reconnecting"),
                    "queue_type": self.client.queue_type, "paused": self._paused,
                    "queue_log": self.client.queue_log,
                    "pause_requested": self._pause_requested,
                    "pending_owner": self._pending_owner,
                    "control_owner": self._control_owner, "scheduler": self._scheduler,
                    "generation": self._generation,
                    "frame_age": time.monotonic() - self._frame_at if self._frame_at else None,
                    "frame_width": 1280, "frame_height": 720,
                    "fps": self._fps if self.client.running and time.monotonic() - self._frame_at <= 1 else 0,
                    "target_fps": 10}

    def latest_frame(self):
        with self._lock:
            return self._jpeg if self.client.running else None

    def connect(self):
        with self._lock:
            if self._shutdown.is_set():
                raise CloudConnectionError("Cloud runtime is shut down.")
            if self._closing:
                raise CloudConnectionError("Cloud session is still closing.")
            if self.client.running or self._connect_thread and self._connect_thread.is_alive():
                return
            self._error = None
            self._cancel_event = threading.Event()
            self._connect_thread = threading.Thread(target=self._connect, args=(self._cancel_event,), name="cloud-connect", daemon=True)
            self._connect_thread.start()

    def _connect(self, cancel_event):
        try:
            self.client.start(cancel_event=cancel_event)
        except Exception as exc:
            if not cancel_event.is_set():
                with self._lock:
                    self._error = _public_error(exc)

    def _release_touches(self):
        self.client.release_touches()
        self._pressed.clear()

    def _stop(self, manual=False, epoch=None, release_scheduler=False, revoke=True):
        with self._stop_lock:
            with self._lock:
                if manual and self._scheduler:
                    raise CloudConnectionError("The scheduler owns this session; stop the script first.")
                if epoch is not None and (epoch != self._grace_epoch or self._viewers or self._scheduler):
                    return
                self._closing = True
                if release_scheduler:
                    self._scheduler = False
                    if revoke:
                        self._scheduler_allowed = False
                    self._generation += 1
                self._cancel_event.set()
                self._release_touches()
                self._pending_owner = self._control_owner = None
                self._paused = self._pause_requested = False
                self._cancel_grace()
            try:
                from .login import get_login
                get_login(self.config_name).cancel()
                self.client.stop()
                thread = self._connect_thread
                if thread is not None:
                    thread.join(timeout=1)
            except Exception as exc:
                with self._lock:
                    self._error = _public_error(exc)
                raise
            finally:
                with self._lock:
                    self._closing = False
                    self._jpeg = None

    def disconnect(self):
        self._stop(manual=True)

    def scheduler_acquire(self):
        with self._lock:
            if self._closing or self._shutdown.is_set():
                raise CloudConnectionError("Cloud session is closing.")
            self._scheduler = True
            self._scheduler_allowed = True
            self._heartbeat_expired = False
            if self._bridge is not None:
                self._bridge["heartbeat"]["at"] = time.monotonic()
            self._cancel_grace()
            if self._control_owner is not None:
                self._pause_requested = self._paused = True
            return self._generation

    def scheduler_release(self, revoke=True):
        # Parent stop/child exit must never wait for a paused worker to acknowledge.
        self._stop(release_scheduler=True, revoke=revoke)

    def request_pause(self):
        with self._lock:
            self._pause_requested = True
            if not self._scheduler:
                self._paused = True
            return self._paused

    def _grant_control(self):
        if self._pending_owner in self._viewers and self._control_owner is None:
            if not self._scheduler or self._paused:
                self._control_owner = self._pending_owner
                self._pending_owner = None

    def _checkpoint(self):
        with self._lock:
            if not self._scheduler:
                raise CloudConnectionError("Cloud scheduler lease was released.")
            if self._pause_requested and not self._paused:
                self._release_touches()
                self._paused = True
            self._grant_control()
            return self._paused

    def resume(self):
        with self._lock:
            self._release_touches()
            self._pending_owner = self._control_owner = None
            if self._paused or self._pause_requested:
                self._generation += 1
            self._paused = self._pause_requested = False

    @staticmethod
    def _viewer_id(viewer_id):
        if not isinstance(viewer_id, str) or not 1 <= len(viewer_id) <= 128:
            raise ValueError("Invalid viewer id")

    def attach(self, viewer_id):
        self._viewer_id(viewer_id)
        with self._lock:
            if len(self._viewers) >= 64 and viewer_id not in self._viewers:
                raise CloudConnectionError("Cloud preview viewer limit reached.")
            self._viewers.add(viewer_id)
            self._cancel_grace()

    def detach(self, viewer_id):
        with self._lock:
            self.release_control(viewer_id)
            self._viewers.discard(viewer_id)
            if not self._viewers and not self._scheduler:
                self._cancel_grace()
                epoch = self._grace_epoch
                self._grace_timer = threading.Timer(15, self._expire_viewers, args=(epoch,))
                self._grace_timer.daemon = True
                self._grace_timer.start()

    def _cancel_grace(self):
        self._grace_epoch += 1
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None

    def _expire_viewers(self, epoch):
        try:
            self._stop(epoch=epoch)
        except Exception:
            pass

    def take_control(self, viewer_id):
        self._viewer_id(viewer_id)
        with self._lock:
            if viewer_id not in self._viewers or self._closing or not self.client.running:
                return False
            if self._control_owner not in (None, viewer_id) or self._pending_owner not in (None, viewer_id):
                return False
            if self._control_owner == viewer_id:
                return True
            self._pending_owner = viewer_id
            self._pause_requested = self._scheduler
            self._grant_control()
            return self._control_owner == viewer_id

    def release_control(self, viewer_id):
        with self._lock:
            if viewer_id == self._control_owner or viewer_id == self._pending_owner:
                self.resume()

    def input(self, viewer_id, action):
        action = validate_input(action)
        with self._lock:
            if viewer_id != self._control_owner or viewer_id not in self._viewers or self._closing:
                raise CloudConnectionError("This viewer does not own cloud input.")
            if self._scheduler and not self._paused:
                raise CloudConnectionError("Wait for scheduler pause acknowledgement.")
            self._send_input(action)

    def _send_input(self, action):
        if action["type"] == "clipboard":
            self.client.clipboard(action["text"])
            self._generation += 1 if self._control_owner else 0
            return
        finger = action["finger_id"]
        if action["action"] == "down" and finger in self._pressed:
            raise ValueError("Finger is already pressed")
        if action["action"] != "down" and finger not in self._pressed:
            raise ValueError("Finger is not pressed")
        self.client.touch(action["x"], action["y"], action["action"], finger)
        if action["action"] == "up":
            self._pressed.pop(finger, None)
        else:
            self._pressed[finger] = (action["x"], action["y"])
        if self._control_owner:
            self._generation += 1

    def _scheduler_input(self, action, generation):
        action = validate_input(action)
        with self._lock:
            if not self._scheduler or self._closing:
                raise CloudConnectionError("Cloud scheduler lease was released.")
            # Checkpoint releases script fingers before handing over. A finally
            # up from that script must never release the browser's finger.
            if action["type"] == "touch" and action["action"] == "up" and (self._paused or generation != self._generation or action["finger_id"] not in self._pressed):
                return
            self._scheduler_ready(generation)
            self._send_input(action)

    def _scheduler_ready(self, generation):
        if not self._scheduler or self._closing:
            raise CloudConnectionError("Cloud scheduler lease was released.")
        if self._paused or self._control_owner is not None or self._pause_requested:
            raise CloudConnectionError("Cloud scheduler is paused.")
        if type(generation) is not int or generation != self._generation:
            raise CloudConnectionError("Cloud screen changed; restart recognition.")

    def capture(self, timeout=5):
        return self.client.capture(timeout=timeout)

    def _reply(self, request_id, ok, value):
        with self._lock:
            replies = self._bridge["replies"]
            if ok and isinstance(value, tuple) and isinstance(value[1], bytes):
                frames = [(key, reply) for key, reply in replies.items() if reply[1] and isinstance(reply[2], tuple)]
                if len(frames) >= 4:
                    oldest = min(frames, key=lambda item: item[1][0])[0]
                    replies.pop(oldest, None)
            if len(replies) >= 256:
                oldest = min(replies.items(), key=lambda item: item[1][0])[0]
                replies.pop(oldest, None)
            replies[request_id] = (time.monotonic(), ok, value)

    def _capture_reply(self, request_id, timeout, generation):
        try:
            with self._lock:
                self._scheduler_ready(generation)
            image = self.capture(timeout)
            with self._lock:
                self._scheduler_ready(generation)
                self._reply(request_id, True, (image.size, image.tobytes()))
        except Exception as exc:
            self._reply(request_id, False, _public_error(exc))
        finally:
            self._capture_slots.release()

    def _wallet_reply(self, request_id):
        try:
            self._reply(request_id, True, self.client.wallet_info())
        except Exception as exc:
            self._reply(request_id, False, _public_error(exc))

    def _commands(self):
        while not self._shutdown.is_set():
            try:
                request_id, method, args = self._bridge["commands"].get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                return
            try:
                if method == "capture":
                    timeout, generation = args
                    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
                        raise ValueError("Invalid capture timeout")
                    if not self._capture_slots.acquire(blocking=False):
                        raise CloudConnectionError("Cloud capture request limit reached.")
                    threading.Thread(target=self._capture_reply, args=(request_id, timeout, generation), daemon=True).start()
                    continue
                if method == "start" and not args:
                    with self._lock:
                        if not self._scheduler_allowed:
                            raise CloudConnectionError("Cloud scheduler lease was revoked; start the script again from WebUI.")
                        self.scheduler_acquire()
                        self.connect()
                    value = None
                elif method == "wallet" and not args:
                    threading.Thread(target=self._wallet_reply, args=(request_id,), daemon=True).start()
                    continue
                elif method == "checkpoint" and not args:
                    value = self._checkpoint()
                elif method == "input" and len(args) == 2:
                    value = self._scheduler_input(args[0], args[1])
                elif method == "stop" and not args:
                    # Connection/stop work must not block checkpoint/input dispatch.
                    if self._stop_pending.is_set():
                        raise CloudConnectionError("Cloud stop is already in progress.")
                    self._stop_pending.set()
                    threading.Thread(target=self._stop_reply, args=(request_id,), daemon=True).start()
                    continue
                else:
                    raise ValueError("Unsupported cloud bridge command")
                self._bridge["state"].update(self.snapshot())
                self._reply(request_id, True, value)
            except Exception as exc:
                self._reply(request_id, False, _public_error(exc))

    def _stop_reply(self, request_id):
        try:
            self.scheduler_release(revoke=False)
            self._reply(request_id, True, None)
        except Exception as exc:
            self._reply(request_id, False, _public_error(exc))
        finally:
            self._stop_pending.clear()

    def _publish(self):
        while not self._shutdown.wait(0.1):
            try:
                self._bridge["state"].update(self.snapshot())
                with self._lock:
                    expired = self._scheduler and not self._closing and not self._heartbeat_expired and time.monotonic() - self._bridge["heartbeat"].get("at", 0) > 30
                    if expired:
                        self._heartbeat_expired = True
                        threading.Thread(target=self._expire_scheduler, daemon=True).start()
                for request_id, reply in list(self._bridge["replies"].items()):
                    if time.monotonic() - reply[0] > 70:
                        self._bridge["replies"].pop(request_id, None)
            except (EOFError, OSError, BrokenPipeError):
                return

    def _expire_scheduler(self):
        try:
            self.scheduler_release()
        except Exception:
            pass

    def shutdown(self):
        self._shutdown.set()
        try:
            self.scheduler_release()
        except Exception:
            pass
        finally:
            self._shutdown.set()


class CloudProxy:
    """Only allowlisted commands cross the multiprocessing manager boundary."""

    def __init__(self, bridge):
        self.bridge = bridge
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

    @property
    def running(self):
        return bool(self.bridge["state"].get("running", False))

    @property
    def status(self):
        return self.bridge["state"].get("state", "Disconnected")

    @property
    def error(self):
        return self.bridge["state"].get("error")

    @property
    def generation(self):
        return self.bridge["state"].get("generation", 0)

    def _call(self, method, *args, timeout=10):
        request_id = uuid.uuid4().hex
        try:
            self.bridge["commands"].put((request_id, method, args), timeout=2)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                reply = self.bridge["replies"].pop(request_id, None)
                if reply is not None:
                    if not reply[1]:
                        raise CloudConnectionError(reply[2])
                    return reply[2]
                time.sleep(0.02)
        except (EOFError, OSError, queue.Full):
            raise CloudConnectionError("Cloud WebUI bridge is unavailable.") from None
        raise CloudConnectionError("Cloud bridge request timed out.")

    def start(self, cancel_event=None):
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive() or self._heartbeat_stop.is_set():
            self._heartbeat_stop = threading.Event()
            self._heartbeat_thread = threading.Thread(target=self._heartbeat, args=(self._heartbeat_stop,), daemon=True)
            self._heartbeat_thread.start()
        self._call("start")
        last_queue_log = ""
        while not self.running:
            if cancel_event is not None and cancel_event.is_set():
                self.stop()
                raise CloudConnectionError("Cloud connection was cancelled.")
            if self.error:
                raise CloudConnectionError(self.error)
            if not self.bridge["state"].get("scheduler", False):
                raise CloudConnectionError("Cloud scheduler lease was released.")
            queue_log = self.bridge["state"].get("queue_log", "")
            if queue_log and queue_log != last_queue_log:
                logger.info(queue_log)
                last_queue_log = queue_log
            time.sleep(0.05)

    def _heartbeat(self, stop_event):
        while not stop_event.is_set():
            try:
                self.bridge["heartbeat"]["at"] = time.monotonic()
            except (EOFError, OSError):
                return
            stop_event.wait(1)

    def before_action(self):
        while self._call("checkpoint"):
            time.sleep(0.05)
        return self.generation

    def _operation_checkpoint(self):
        generation = self.generation
        if self.before_action() != generation:
            raise CloudConnectionError("Cloud screen changed; restart recognition.")
        return generation

    def capture(self, timeout=5):
        from PIL import Image
        generation = self._operation_checkpoint()
        size, pixels = self._call("capture", timeout, generation, timeout=timeout + 2)
        return Image.frombytes("RGB", size, pixels)

    def wallet_info(self):
        self.before_action()
        return self._call("wallet", timeout=25)

    def touch(self, x, y, action, finger_id=0):
        x, y = float(x), float(y)
        generation = self._operation_checkpoint() if action != "up" else self.generation
        self._call("input", validate_input(dict(type="touch", x=x, y=y, action=action, finger_id=finger_id)), generation)

    def clipboard(self, text):
        generation = self._operation_checkpoint()
        self._call("input", validate_input(dict(type="clipboard", text=text)), generation)

    def stop(self):
        self._heartbeat_stop.set()
        self._call("stop", timeout=70)
