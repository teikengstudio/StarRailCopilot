"""Per-instance cloud credentials; passwords are stored only as RSA ciphertext."""

import json
import math
import os
import re
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from deploy.Windows.atomic import atomic_read_text, atomic_write
from filelock import FileLock

from .core.auth import (
    PasswordLoginError, RETCODE_TOKEN_INVALID, build_password_login_body,
    parse_cookie_header, validate_rsa_ciphertext,
)
from .core.config import CoreConfig, DEFAULT_CORE_CONFIG
# ponytail: one process-wide lock for tiny account files; split if IO contention matters.
_ACCOUNT_LOCK = threading.RLock()
_PROFILE_SECTIONS = frozenset(DEFAULT_CORE_CONFIG)
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}
_RISK_CODES = frozenset({-3101, -3235, -3254, -3257, -4400, -3208, -3006})


class CloudAccountError(RuntimeError):
    """Safe public failure with no remote payload or credential values."""

    def __init__(self, message, *, kind="configuration", retcode=None, aigis=None, verify=None):
        super().__init__(message)
        self.kind = kind
        self.retcode = retcode if type(retcode) is int else None
        self.aigis = aigis
        self.verify = verify


def _validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) - _PROFILE_SECTIONS:
        raise CloudAccountError("Cloud profile must contain only supported profile sections.")

    def validate_json(value, depth=0):
        if depth > 12 or isinstance(value, (dict, list)) and len(value) > 256:
            raise CloudAccountError("Cloud profile metadata structure is too large.")
        if isinstance(value, str) and len(value) > 8192:
            raise CloudAccountError("Cloud profile metadata text exceeds 8 KiB.")
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 128 for key in value):
                raise CloudAccountError("Cloud profile keys must be short strings.")
            for item in value.values():
                validate_json(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                validate_json(item, depth + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise CloudAccountError("Cloud profile numbers must be finite.")
        elif value is not None and not isinstance(value, (str, int, bool)):
            raise CloudAccountError("Cloud profile must contain JSON values only.")

    try:
        validate_json(profile)
        encoded = json.dumps(profile, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise CloudAccountError("Cloud profile is not valid JSON.") from None
    if len(encoded) > 65536:
        raise CloudAccountError("Cloud profile exceeds 64 KiB.")
    for section, values in profile.items():
        if not isinstance(values, dict):
            raise CloudAccountError("Each cloud profile section must be an object.")
        for key, value in values.items():
            default = DEFAULT_CORE_CONFIG[section].get(key)
            if default is None:
                continue  # Browser-collected JSON metadata is retained verbatim.
            if isinstance(default, str) and not isinstance(value, str):
                raise CloudAccountError("Cloud profile text fields must be strings.")
            if isinstance(default, (int, float)) and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise CloudAccountError("Cloud profile numeric fields must be numbers.")
            if key in {"screen_width", "screen_height", "dpi", "cpu_cores", "memory_gb", "fps"} and value < 0:
                raise CloudAccountError("Cloud profile dimensions and capacities cannot be negative.")
    device_id = profile.get("device_profile", {}).get("device_id")
    if device_id is not None and (
        not device_id or len(device_id) > 128
        or any(ord(char) < 33 or ord(char) > 126 or char in ";*" for char in device_id)
    ):
        raise CloudAccountError("Cloud device ID must be a non-empty safe header value.")
    try:
        CoreConfig(profile)
    except (ValueError, TypeError, AttributeError):
        raise CloudAccountError("Cloud platform profile mode is unsupported.") from None


def _merge_profile(current, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            _merge_profile(current[key], value)
        else:
            current[key] = deepcopy(value)


def _sync_device(data):
    device = data["profile"].setdefault("device_profile", {})
    imported_id = parse_cookie_header(data["cookie"]).get("_MHYUUID", "")
    device_id = imported_id if imported_id and "*" not in imported_id else device.get("device_id")
    device_id = device_id or str(uuid.uuid4())
    changed = device.get("device_id") != device_id
    device["device_id"] = device_id
    _validate_profile(data["profile"])
    return changed


class CloudAccount:
    """Store credentials in config/cloud/<config_name>.json, outside normal pins."""

    def __init__(self, config_name, root_dir=None):
        if (
            not isinstance(config_name, str)
            or not re.fullmatch(r"[\w-]{1,128}", config_name)
            or config_name.upper() in _RESERVED_NAMES
        ):
            raise CloudAccountError("Unsafe cloud configuration name.")
        self.config_name = config_name
        self.root_dir = Path(root_dir) if root_dir is not None else Path(__file__).resolve().parents[3]
        self.path = self.root_dir / "config" / "cloud" / (config_name + ".json")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            os.chmod(self.path.parent, 0o700)
        self._file_lock = FileLock(str(self.path) + '.lock', timeout=10)

    def _write(self, data):
        try:
            atomic_write(str(self.path), json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n")
            if os.name != 'nt':
                os.chmod(self.path, 0o600)
        except (OSError, ValueError, TypeError):
            raise CloudAccountError("Unable to save cloud account configuration.") from None

    def read(self) -> dict:
        """Read a fresh snapshot and persist a device ID on first creation."""
        with _ACCOUNT_LOCK, self._file_lock:
            try:
                text = atomic_read_text(str(self.path))
                if not text and self.path.exists():
                    raise ValueError("Empty existing account file")
                data = json.loads(text) if text else {}
            except (OSError, ValueError, UnicodeError):
                raise CloudAccountError("Unable to read cloud account configuration; file was not changed.") from None
            if not isinstance(data, dict):
                raise CloudAccountError("Cloud account configuration must be a JSON object.")
            data.setdefault("cookie", "")
            data.setdefault("password_login", {"account": "", "password": ""})
            data.setdefault("profile", {})
            cookie = data["cookie"]
            if not isinstance(cookie, str) or "\r" in cookie or "\n" in cookie or len(cookie) > 65536:
                raise CloudAccountError("Saved cloud cookie must be a single header line.")
            credentials = data["password_login"]
            if not isinstance(credentials, dict) or set(credentials) != {"account", "password"}:
                raise CloudAccountError("Saved cloud password fields are invalid.")
            if credentials != {"account": "", "password": ""}:
                try:
                    for value in credentials.values():
                        validate_rsa_ciphertext(value)
                except (ValueError, TypeError):
                    raise CloudAccountError("Saved cloud password must contain two valid RSA ciphertext blocks.") from None
            _validate_profile(data["profile"])
            if _sync_device(data):
                self._write(data)
            return data

    def _update(self, updates):
        with _ACCOUNT_LOCK, self._file_lock:
            data = self.read()
            data.update(updates)
            if "cookie" in updates:
                _sync_device(data)
            self._write(data)

    def save_password(self, account: str, password: str) -> None:
        """Encrypt both inputs before any IO; never retain their plaintext."""
        if not isinstance(account, str) or not isinstance(password, str):
            raise CloudAccountError("Cloud account and password must be text.")
        try:
            encrypted = build_password_login_body(account, password)
        except (ValueError, TypeError):
            raise CloudAccountError("Cloud account or password is empty or exceeds the RSA field limit.") from None
        finally:
            account = password = None
        self._update({"password_login": encrypted})

    def clear_password(self) -> None:
        """Clear only the saved ciphertext, leaving cookie and profile intact."""
        self._update({"password_login": {"account": "", "password": ""}})

    def save_cookie(self, cookie: str) -> None:
        if not isinstance(cookie, str) or "\r" in cookie or "\n" in cookie or len(cookie) > 65536:
            raise CloudAccountError("Cloud cookie must be a single header line.")
        self._update({"cookie": cookie})

    def save_profile(self, profile: dict) -> None:
        """Merge browser data; change device_id only when explicitly supplied."""
        _validate_profile(profile)
        with _ACCOUNT_LOCK, self._file_lock:
            data = self.read()
            _merge_profile(data["profile"], profile)
            explicit_id = profile.get("device_profile", {}).get("device_id")
            if explicit_id and data["cookie"]:
                data["cookie"] = re.sub(
                    r"(^|;\s*)_MHYUUID=[^;]*",
                    lambda match: match[1] + "_MHYUUID=" + explicit_id,
                    data["cookie"],
                )
                if "_MHYUUID" not in parse_cookie_header(data["cookie"]):
                    data["cookie"] += "; _MHYUUID=" + explicit_id
            _validate_profile(data["profile"])
            self._write(data)

    @staticmethod
    def _check(authenticator, cookie):
        try:
            valid, info = authenticator.check(cookie)
        except Exception:
            raise CloudAccountError("Cloud login check failed; no password login was attempted.", kind="network") from None
        if not isinstance(info, dict):
            raise CloudAccountError("Cloud login check returned an invalid response.", kind="network")
        return valid, info

    @staticmethod
    def _raise_check_failure(info):
        code = info.get("retcode")
        code = code if type(code) is int else None
        if code == -2:
            raise CloudAccountError("Cloud login check failed due to a network or response error; no retry was attempted.", kind="network", retcode=code)
        if code == -1:
            raise CloudAccountError("Cloud cookie is incomplete; replace it or clear it to use saved password login.", kind="credentials", retcode=code)
        if code in _RISK_CODES:
            raise CloudAccountError("Cloud authentication requires official captcha, identity verification, or account recovery; automatic login stopped.", kind="risk", retcode=code)
        safe_code = str(code) if type(code) is int else "unknown"
        raise CloudAccountError("Cloud login check was rejected (retcode " + safe_code + "); automatic login stopped.", kind="authentication", retcode=code)

    def ensure_login(self, game, on_aigis=None, on_sms_code=None, cancel_event=None,
                     commit=None, force_password=False) -> None:
        """Apply a valid cookie, or attempt one saved-ciphertext login on expiry.

        Automatic recovery uses password only for an absent cookie or token-invalid
        retcode -100. Explicit password login may bypass that check. Human challenge
        callbacks remain on the original request/session; no SMS is sent without a
        caller supplying on_sms_code. commit(cookie, persist) can make WebUI attempt
        cancellation atomic with persistence and application.
        """
        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise CloudAccountError("Cloud login was cancelled.", kind="cancelled")

        def apply(cookie, persist):
            check_cancelled()
            if commit is not None:
                commit(cookie, persist)
            else:
                if persist:
                    self.save_cookie(cookie)
                check_cancelled()
                game._apply_credentials(cookie)

        check_cancelled()
        data = self.read()
        cookie = data["cookie"]
        authenticator = game.authenticator
        if cookie and not force_password:
            valid, info = self._check(authenticator, cookie)
            if valid:
                apply(cookie, False)
                return
            if info.get("retcode") != RETCODE_TOKEN_INVALID:
                self._raise_check_failure(info)
        credentials = data["password_login"]
        if not credentials["account"] or not credentials["password"]:
            raise CloudAccountError("Cloud credentials are missing or expired; save an account/password or a valid cookie.", kind="credentials")
        bootstrap = parse_cookie_header(cookie)
        bootstrap.setdefault("_MHYUUID", data["profile"]["device_profile"]["device_id"])
        existing_cookie = "; ".join(f"{key}={value}" for key, value in bootstrap.items())
        try:
            new_cookie = authenticator.login_password(
                credentials["account"], credentials["password"], encrypted=True,
                existing_cookie=existing_cookie, verify=False, on_status=lambda message: None,
                on_aigis=on_aigis, on_sms_code=on_sms_code,
            )
        except PasswordLoginError as exc:
            check_cancelled()
            code = exc.retcode if type(exc.retcode) is int else None
            if code == -2:
                raise CloudAccountError("Cloud password login encountered a network error; no retry was attempted.", kind="network", retcode=code) from None
            if exc.verify is not None:
                verification_type = getattr(exc.verify, "verify_type", None)
                kind = {1: "captcha", 2: "identity"}.get(verification_type, "account_restricted")
                message = ("Cloud login requires human Geetest verification." if kind == "captcha" else
                           "Cloud account requires official %s verification; open the official account website."
                           % ("identity/SMS" if kind == "identity" else "account"))
                raise CloudAccountError(
                    message, kind=kind, retcode=code, aigis=exc.aigis, verify=exc.verify,
                ) from None
            if exc.aigis is not None:
                raise CloudAccountError(
                    "Cloud login requires human Geetest verification.",
                    kind="captcha", retcode=code, aigis=exc.aigis,
                ) from None
            if code in _RISK_CODES:
                raise CloudAccountError("Cloud password login requires official captcha, identity verification, or account recovery; no retry was attempted.", kind="risk", retcode=code) from None
            safe_code = str(code) if type(code) is int else "unknown"
            raise CloudAccountError("Cloud password login failed (retcode " + safe_code + "); check credentials on the official website.", kind="authentication", retcode=code) from None
        except Exception:
            check_cancelled()
            raise CloudAccountError("Cloud password login failed; no retry was attempted.", kind="authentication") from None
        valid, info = self._check(authenticator, new_cookie)
        if not valid:
            self._raise_check_failure(info)
        apply(new_cookie, True)
