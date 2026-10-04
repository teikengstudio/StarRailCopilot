"""Process-local human authentication shared by settings and cloud sessions."""

import base64
import json
import secrets
import threading
import time
from copy import deepcopy

from .account import CloudAccount, CloudAccountError
from .core.auth import QRLoginError, build_aigis_header, parse_cookie_header

_LOGINS = {}
_LOGINS_LOCK = threading.Lock()
_CONFIG_KEYS = frozenset({"gt", "challenge", "use_v4", "risk_type", "new_captcha", "success"})
_V3_FIELDS = frozenset({"geetest_challenge", "geetest_validate", "geetest_seccode"})
_V4_FIELDS = frozenset({"captcha_id", "lot_number", "pass_token", "gen_time", "captcha_output"})
OFFICIAL_ACCOUNT_URL = "https://user.mihoyo.com/"


class CloudLogin:
    def __init__(self, config_name, root_dir=None):
        self.account = CloudAccount(config_name, root_dir=root_dir)
        self._lock = threading.RLock()
        self._serial = threading.Lock()
        self._token = None
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._done.set()
        self._active = False
        self._state = "idle"
        self._error = None
        self._qr_png = None
        self._challenge = None
        self._challenge_id = None
        self._result = None

    def snapshot(self):
        with self._lock:
            return dict(state=self._state, error=self._error, qr_png=self._qr_png,
                        challenge=deepcopy(self._challenge), challenge_id=self._challenge_id)

    def cancel(self, challenge_id=None):
        with self._lock:
            if challenge_id is not None and challenge_id != self._challenge_id:
                return
            self._cancel.set()
            self._token = None
            self._active = False
            self._state, self._error = "cancelled", None
            self._qr_png = self._challenge = self._challenge_id = self._result = None
            self._done.set()

    def _new_attempt(self):
        with self._lock:
            self._cancel.set()
            self._done.set()
            self._token = secrets.token_urlsafe(24)
            self._cancel, self._done = threading.Event(), threading.Event()
            self._active = True
            self._state, self._error = "authenticating", None
            self._qr_png = self._challenge = self._challenge_id = self._result = None
            return self._token, self._cancel, self._done

    def _check(self, token, cancel, external=None):
        if token != self._token or cancel.is_set() or external is not None and external.is_set():
            raise CloudAccountError("Cloud login was cancelled.", kind="cancelled")

    def _update(self, token, cancel, **values):
        with self._lock:
            self._check(token, cancel)
            for key, value in values.items():
                setattr(self, "_" + key, value)

    def _game(self):
        from .core.cloud_game import CloudGame
        from .core.config import CoreConfig
        from .core.models import CloudGameConfig
        return CloudGame(config=CloudGameConfig(core_config=CoreConfig(self.account.read()["profile"])))

    def _commit(self, game, token, cancel, external, cookie, persist):
        # Cancellation and persistence share the lock: a superseded attempt cannot save later.
        with self._lock:
            self._check(token, cancel, external)
            if persist:
                self.account.save_cookie(cookie)
            game._apply_credentials(cookie)

    def _aigis(self, challenge, token, cancel, external=None):
        config = challenge.get("data") if isinstance(challenge, dict) else None
        if not isinstance(config, dict) or not isinstance(config.get("gt"), str) or not config["gt"]:
            raise CloudAccountError("Invalid official Geetest challenge.", kind="captcha")
        safe = {key: value for key, value in config.items() if key in _CONFIG_KEYS}
        if any(not isinstance(value, (str, bool, int)) for value in safe.values()):
            raise CloudAccountError("Invalid official Geetest configuration.", kind="captcha")
        if len(json.dumps(safe)) > 16384:
            raise CloudAccountError("Official Geetest configuration is too large.", kind="captcha")
        session_id = challenge.get("session_id") or ""
        if not isinstance(session_id, str) or len(session_id) > 1024 or any(c in session_id for c in ";\r\n"):
            raise CloudAccountError("Invalid official Geetest session.", kind="captcha")
        challenge_id = secrets.token_urlsafe(24)
        expires_at = time.time() + 180
        self._update(token, cancel, state="captcha_required", challenge_id=challenge_id,
                     challenge=dict(type="geetest", data=safe, session_id=session_id, expires_at=expires_at),
                     result=None)
        while True:
            with self._lock:
                self._check(token, cancel, external)
                if time.time() >= expires_at:
                    raise CloudAccountError("Human verification expired; start login again.", kind="captcha")
                if self._result is not None:
                    result = self._result
                    self._result = self._challenge = self._challenge_id = None
                    self._state = "authenticating"
                    break
            cancel.wait(0.15)
        if session_id:
            return build_aigis_header(session_id, result)
        payload = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(payload).decode("ascii")

    def submit_captcha(self, challenge_id, result):
        with self._lock:
            challenge = self._challenge
            if (not self._active or self._cancel.is_set() or challenge_id != self._challenge_id
                    or not challenge or challenge.get("type") != "geetest"
                    or time.time() >= challenge["expires_at"] or self._result is not None):
                raise CloudAccountError("Human verification is expired or already used.", kind="captcha")
            fields = _V4_FIELDS if challenge["data"].get("use_v4") else _V3_FIELDS
            if not isinstance(result, dict) or set(result) != fields:
                raise CloudAccountError("Invalid Geetest validation fields.", kind="captcha")
            if any(not isinstance(value, str) or not value or len(value) > 8192
                   or "\r" in value or "\n" in value for value in result.values()):
                raise CloudAccountError("Invalid Geetest validation values.", kind="captcha")
            if fields == _V4_FIELDS and result["captcha_id"] != challenge["data"]["gt"]:
                raise CloudAccountError("Geetest validation belongs to another captcha.", kind="captcha")
            if fields == _V3_FIELDS and challenge["data"].get("challenge") and not result[
                    "geetest_challenge"].startswith(challenge["data"]["challenge"]):
                raise CloudAccountError("Geetest validation belongs to another challenge.", kind="captcha")
            self._result = dict(result)

    def _finish(self, token, cancel, done, error=None):
        with self._lock:
            if token == self._token:
                self._active = False
                self._qr_png = self._challenge = self._challenge_id = self._result = None
                if cancel.is_set() or getattr(error, "kind", None) == "cancelled":
                    self._state, self._error = "cancelled", None
                elif isinstance(error, QRLoginError):
                    self._state = "qr_expired" if error.status in {"Expired", "Timeout"} else "error"
                    self._error = "QR code expired; refresh to continue." if self._state == "qr_expired" else "QR login was rejected; refresh or use the official account website."
                elif error is not None:
                    kind = getattr(error, "kind", "authentication")
                    self._state = {"credentials": "login_required", "identity": "identity_required",
                                   "account_restricted": "account_restricted"}.get(kind, "error")
                    self._error = str(error) if isinstance(error, CloudAccountError) else "Cloud login failed; refresh and try again."
                    if kind in {"identity", "account_restricted"}:
                        self._challenge = dict(type=kind, official_url=OFFICIAL_ACCOUNT_URL)
                else:
                    self._state, self._error = "authenticated", None
            done.set()

    def _run(self, game, token, cancel, done, qr=False, external=None, propagate=False,
             force_password=False):
        error = None
        try:
            with self._serial:
                with self._lock:
                    self._check(token, cancel, external)
                commit = lambda cookie, persist: self._commit(game, token, cancel, external, cookie, persist)
                if qr:
                    data = self.account.read()
                    bootstrap = parse_cookie_header(data["cookie"])
                    bootstrap.setdefault("_MHYUUID", data["profile"]["device_profile"]["device_id"])
                    cookie = game.authenticator.login_qrcode(
                        existing_cookie="; ".join("%s=%s" % item for item in bootstrap.items()),
                        save_png=False, terminal=False, verify=False, cancel_event=cancel,
                        on_status=lambda message: None,
                        on_qr=lambda png: self._update(token, cancel, qr_png=png, state="qr_waiting"),
                        on_qr_status=lambda status: self._update(token, cancel, state={
                            "Scanned": "qr_scanned", "Confirmed": "authenticating",
                            "Created": "qr_waiting", "Init": "qr_waiting",
                        }.get(status, "qr_waiting")),
                    )
                    with self._lock:
                        self._check(token, cancel, external)
                    valid, info = self.account._check(game.authenticator, cookie)
                    if not valid:
                        self.account._raise_check_failure(info)
                    commit(cookie, True)
                else:
                    self.account.ensure_login(
                        game, on_aigis=lambda challenge: self._aigis(challenge, token, cancel, external),
                        cancel_event=cancel, commit=commit, force_password=force_password,
                    )
        except Exception as exc:
            with self._lock:
                cancelled = token != self._token or cancel.is_set() or external is not None and external.is_set()
            error = CloudAccountError("Cloud login was cancelled.", kind="cancelled") if cancelled else exc
            if propagate:
                raise error
        finally:
            self._finish(token, cancel, done, error)

    def _begin(self, qr):
        token, cancel, done = self._new_attempt()

        def worker():
            try:
                game = self._game()
            except Exception as exc:
                self._finish(token, cancel, done, exc)
                return
            self._run(game, token, cancel, done, qr=qr, force_password=not qr)

        threading.Thread(target=worker, name="src-cloud-login", daemon=True).start()

    def begin_qr(self):
        self._begin(qr=True)

    def begin_password(self):
        self._begin(qr=False)

    def authenticate(self, game, cancel_event=None):
        with self._lock:
            done = self._done if self._active else None
        if done is not None:
            while not done.wait(0.15):
                if cancel_event is not None and cancel_event.is_set():
                    raise CloudAccountError("Cloud login was cancelled.", kind="cancelled")
            with self._lock:
                if self._state != "authenticated":
                    raise CloudAccountError(self._error or "Cloud login was cancelled.", kind=self._state)
        token, cancel, done = self._new_attempt()
        self._run(game, token, cancel, done, external=cancel_event, propagate=True)


def get_login(config_name):
    with _LOGINS_LOCK:
        if config_name not in _LOGINS:
            _LOGINS[config_name] = CloudLogin(config_name)
        return _LOGINS[config_name]
