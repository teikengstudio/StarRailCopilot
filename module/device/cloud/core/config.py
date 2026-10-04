from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from collections.abc import Mapping
from typing import Any


# ---------------------------------------------------------------------------
# 平台画像预设 —— "桌面(键鼠)" vs "手机(触控)"
# ---------------------------------------------------------------------------
# 数值全部来自云游戏 SDK ``web.4f0790db.js`` 模块 26975 的平台推导：
#   WebPC=16, WebMac=17, WebTouch=18, WebKeyboard=19          （调度 x-rpc-client_type）
#   CloudWebPC=22, CloudWebMac=23, CloudWebTouch=24, CloudWebKeyboard=25 （账号 x-rpc-client_type）
#   cps 前缀：pc / mac / keyboard / android / ios / touch     （x-rpc-cps = "<前缀>_mihoyo"）
#   speed_client_type：Web/Mac/Keyboard=7, Android/Ios/Touch=8
#   sec-ch-ua-mobile：桌面 "?0"，移动 "?1"
#   listPingServer 的 ext_data.platform = 调度 x-rpc-client_type
PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "desktop": {
        "mode": "desktop",
        "terminal_type": "WebKeyboard",
        "client_type": 19,
        "web_client_type": 25,
        "cps": "keyboard_mihoyo",
        "speed_client_type": 7,
        "device_family": "pc",
        "sec_ch_ua_platform": '"Linux"',
        "sec_ch_ua_mobile": "?0",
        "ext_data_platform": 19,
    },
    "touch": {
        "mode": "touch",
        "terminal_type": "WebTouch",
        "client_type": 18,
        "web_client_type": 24,
        "cps": "touch_mihoyo",
        "speed_client_type": 8,
        "device_family": "android",
        "sec_ch_ua_platform": '"Android"',
        "sec_ch_ua_mobile": "?1",
        "ext_data_platform": 18,
    },
}
DEFAULT_PLATFORM_MODE = "desktop"


DEFAULT_CORE_CONFIG: dict[str, dict[str, Any]] = {
    "device_profile": {
        "device_id": "",
        "os": "Unknown",
        "model": "Unknown",
        "cpu_cores": 0,
        "cpu_freq": 0,
        "cpu_type": "Unknown",
        "memory_gb": 0,
        "soc": "Unknown",
        "gpu_model": "Unknown",
        "screen_width": 0,
        "screen_height": 0,
        "dpi": 0,
        "device_name": "Unknown",
        "sys_version": "Unknown",
    },
    "browser_profile": {
        "user_agent": "Unknown",
        "sec_ch_ua": "",
        "app_version": "4.3.0",
        "web_device_name": "Unknown",
        "web_device_model": "Unknown",
        "web_device_os": "Unknown",
    },
    "protocol_profile": {
        "client_lib": "python-aiortc",
        "sdk_webview_ua": "Unknown",
    },
    "session_profile": {
        "graphics_mode": 0,
        "bitrate_multiplier": 1.875,
        "resolution": "1920x1080",
        "fps": 30,
        "bit_rate": 10240000,
    },
    "platform_profile": deepcopy(PLATFORM_PRESETS[DEFAULT_PLATFORM_MODE]),
}


def _deep_update(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def expand_platform_profile(data: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``platform_profile.mode`` 展开成完整预设（用户显式给的字段优先）。

    两种写法等价::

        {"platform_profile": {"mode": "touch"}}
        {"platform_profile": {"mode": "touch", "client_type": 18, "cps": "touch_mihoyo", ...}}

    参数:
        data: 用户提供的 core config 片段（通常是 ``client_profile.json``）。

    返回:
        新的 dict；``platform_profile`` 已被替换为"预设 + 用户覆盖"的完整画像。
        未提供 ``platform_profile`` 时原样返回。

    异常:
        ``ValueError``: ``mode`` 不是 :data:`PLATFORM_PRESETS` 里的取值。
    """
    prepared = dict(data)
    profile = prepared.get("platform_profile")
    if not isinstance(profile, Mapping):
        return prepared
    mode = str(profile.get("mode") or DEFAULT_PLATFORM_MODE).lower()
    preset = PLATFORM_PRESETS.get(mode)
    if preset is None:
        raise ValueError(
            f"未知的 platform_profile.mode: {mode!r}（支持 {sorted(PLATFORM_PRESETS)}）"
        )
    merged = deepcopy(preset)
    _deep_update(merged, profile)
    merged["mode"] = mode
    prepared["platform_profile"] = merged
    return prepared


def _to_namespace(value: Any) -> Any:
    if isinstance(value, Mapping):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    return value


def strip_json_comments(text: str) -> str:
    """Remove // comments while preserving string contents."""
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        char = text[i]
        next_char = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "\"":
                in_string = False
            i += 1
            continue
        if char == "\"":
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and next_char == "/":
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def loads_json_with_comments(text: str) -> dict[str, Any]:
    data = json.loads(strip_json_comments(text))
    if not isinstance(data, dict):
        raise ValueError("core config must be a JSON object")
    return data


class CoreConfig:
    """Core runtime profile with attribute access."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        merged = deepcopy(DEFAULT_CORE_CONFIG)
        if data:
            # 先展开 platform_profile.mode, 再合并 —— 这样"只写 mode"也能拿到完整预设
            _deep_update(merged, expand_platform_profile(data))
        self.raw = merged
        for key, value in merged.items():
            setattr(self, key, _to_namespace(value))

    @property
    def platform_mode(self) -> str:
        """当前平台模式：``"desktop"``（键鼠）或 ``"touch"``（手机触控）。"""
        return str(getattr(self.platform_profile, "mode", DEFAULT_PLATFORM_MODE))

    @property
    def is_touch(self) -> bool:
        """是否处于手机(触控)模式。"""
        return self.platform_mode == "touch"


def normalize_core_config(value: CoreConfig | Mapping[str, Any] | None) -> CoreConfig:
    if isinstance(value, CoreConfig):
        return value
    return CoreConfig(value)


__all__ = [
    "CoreConfig",
    "DEFAULT_CORE_CONFIG",
    "DEFAULT_PLATFORM_MODE",
    "PLATFORM_PRESETS",
    "expand_platform_profile",
    "loads_json_with_comments",
    "normalize_core_config",
    "strip_json_comments",
]
