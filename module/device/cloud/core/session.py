from __future__ import annotations

import asyncio
import json
import logging
import textwrap
import time
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from urllib3.util.ssl_match_hostname import CertificateError, match_hostname

from .config import CoreConfig, normalize_core_config
from .log import emit_log_callback, get_logger
from .protocol import (
    CMD_KCP_CONNECT_ACK,
    CMD_KCP_CONNECT_SYNC,
    CMD_KCP_CONNECT_SYNC_ACK,
    CMD_KCP_PING,
    CMD_KCP_PONG,
    CMD_NAMES,
    CMD_RELIABLE_MESSAGE_QUEUE_DATA,
    CMD_RTC_NOT_PLAYING_TIPS,
    FRAME_HANDSHAKE,
    FRAME_PROXY,
    FRAME_SIGNALING,
    FRAME_TYPE_NAMES,
    Protocol,
)

logger = get_logger("session")

COLOR_RESET = "\033[0m"
COLOR_SEND = "\033[38;5;39m"
COLOR_RECV = "\033[38;5;201m"
COLOR_META = "\033[2m"

# ---------------------------------------------------------------------------
# SDK 应答 — 设备/平台特征常量
# ---------------------------------------------------------------------------
SDK_FALLBACK_APP_ID = 8
SDK_FALLBACK_CHANNEL_ID = 1
SDK_CPS = "keyboard_mihoyo"  # 桌面(键鼠)兜底；手机触控模式取 platform_profile.cps
SDK_CHANNEL_ID_RESPONSE = "1"
SDK_COMBO_ID_FALLBACK = "0"
SDK_PROTOCOL_VERSION = {"major": 13, "minimum": 0}
# 已迁移到 core.config.DEFAULT_CORE_CONFIG["protocol_profile"] / client_profile.json。
# SDK_WEBVIEW_UA = (
#     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari/537.36"
# )

# ---------------------------------------------------------------------------
# 信令 / 启动配置 — 固定协议参数
# ---------------------------------------------------------------------------
GAME_CONTROL_ORIGIN = "https://sr.mihoyo.com"
# 已迁移到 core.config.DEFAULT_CORE_CONFIG["session_profile"] / client_profile.json。
# STARTUP_GRAPHICS_MODE = 0
# STARTUP_BITRATE_MULTIPLIER = 1.875
STARTUP_TRANSPORT_PROTOCOL = "udp"
START_GAME_LINK_TASKS_MS = 200
CLIENT_HELLO_JSON = '{"client_type":"web","type":"client hello"}'


class _CloudTLSObject(ssl.SSLObject):
    def do_handshake(self):
        super().do_handshake()
        try:
            match_hostname(self.getpeercert(), self.server_hostname)
        except CertificateError as exc:
            error = ssl.SSLCertVerificationError(str(exc))
            error.verify_message = str(exc)
            raise error from None


async def connect_websocket(url: str, *, timeout: float = 10, headers: Mapping[str, str] | None = None):
    """Connect with certificate and hostname verification enabled."""
    request_headers = dict(headers or {})
    origin = request_headers.pop("Origin", None)
    user_agent = request_headers.pop("User-Agent", None)
    ssl_context = ssl.create_default_context()
    # OpenSSL rejects wildcard matching for underscore labels used by cloud nodes.
    # Check the exact hostname during TLS, before any WebSocket credentials are sent.
    if '_' in (urlsplit(url).hostname or ''):
        ssl_context.check_hostname = False
        ssl_context.sslobject_class = _CloudTLSObject
    return await connect(
        url,
        ssl=ssl_context,
        origin=origin,
        additional_headers=request_headers or None,
        user_agent_header=user_agent,
        open_timeout=timeout,
        ping_interval=None,
        max_size=None,
    )


class LogText:
    @staticmethod
    def colorize(text: str, color: str, enabled: bool) -> str:
        """按需给日志文本添加终端颜色。"""
        if not enabled:
            return text
        return f"{color}{text}{COLOR_RESET}"

    @staticmethod
    def shorten(value: str, limit: int) -> str:
        """按长度限制截断日志文本。"""
        if limit <= 0 or len(value) <= limit:
            return value
        return value[:limit] + f"\n... truncated {len(value) - limit} chars ..."

    @staticmethod
    def hex_dump(data: bytes, limit: int) -> str:
        """把二进制数据格式化为十六进制文本。"""
        raw = data if limit <= 0 else data[:limit]
        hex_text = raw.hex()
        lines = textwrap.wrap(hex_text, 96)
        out = "\n".join(lines)
        if limit > 0 and len(data) > limit:
            out += f"\n... truncated {len(data) - limit} bytes ..."
        return out

    @staticmethod
    def safe_decode(data: bytes) -> str:
        """以容错方式把字节解码为文本。"""
        return data.decode("utf-8", errors="replace")


class SdkGameDataHandler:
    def __init__(
            self,
            sdk_login: dict | None = None,
            cookie: str | None = None,
            combo_token: str = "",
            channel_token: str = "",
            clipboard_getter: Callable[[], str] | None = None,
            core_config: CoreConfig | dict | None = None,
    ) -> None:
        """初始化实例并保存运行所需的状态。"""
        self.cloud_data: dict[str, str] = {}
        self.sdk_login = sdk_login
        self.cookie = cookie or ""
        self.combo_token = combo_token
        self.channel_token = channel_token
        self.clipboard_text = ""
        self.clipboard_getter = clipboard_getter
        self.core_config = normalize_core_config(core_config)

    def _cookie_value(self, name: str, default: str = "") -> str:
        """从 Cookie 字符串中读取指定键值。"""
        prefix = name + "="
        for part in self.cookie.split(";"):
            item = part.strip()
            if item.startswith(prefix):
                return unquote_plus(item[len(prefix):])
        return default

    @staticmethod
    def _combo_token(value: str) -> dict[str, str]:
        """解析 combo token 中的键值对。"""
        out = {}
        for part in value.split(";"):
            if "=" in part:
                key, val = part.strip().split("=", 1)
                out[key] = val
        return out

    def _response(self, index: int, data) -> dict:
        """构造 SDK 回调响应。"""
        if isinstance(data, str):
            payload = data
        else:
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return {
            "f": "on_get_invoke_response",
            "p": json.dumps({"index": index, "data": payload}, ensure_ascii=False, separators=(",", ":")),
        }

    def _invoke_response(self, index: int, result=None) -> dict:
        """构造 invoke 成功响应。"""
        return self._response(index, {} if result is None else result)

    def _error_response(self, index: int, function_name: str, kind: str = "get") -> dict:
        """构造 SDK 调用缺失错误响应。"""
        return self._response(index, {"ret": -10, "msg": f"function not found in {kind}:{function_name}"})

    def _login_response(self) -> dict:
        """生成 SDK 登录查询响应。"""
        if self.sdk_login:
            return self.sdk_login

        combo = self._combo_token(self.combo_token)
        device_id = self._cookie_value("_MHYUUID")
        open_id = combo.get("oi") or self._cookie_value("account_id_v2") or self._cookie_value("account_id")
        app_id = int(combo.get("ai") or SDK_FALLBACK_APP_ID)
        channel_id = int(combo.get("ci") or SDK_FALLBACK_CHANNEL_ID)
        # account_type 与 channel_id 取同一来源（combo "ci"）：网页 SDK 对当前渠道
        # 二者一致，留此注释以免被误判为复制粘贴遗漏。
        account_type = int(combo.get("ci") or SDK_FALLBACK_CHANNEL_ID)
        valid = bool(self.channel_token and open_id and combo.get("ct"))
        return {
            "ret": 0 if valid else -1,
            "msg": "成功" if valid else "missing web account token",
            "data": {
                "device_id": device_id,
                "app_id": app_id,
                "channel_id": channel_id,
                "channel_token": self.channel_token,
                "combo_id": SDK_COMBO_ID_FALLBACK,
                "open_id": open_id,
                "combo_token": combo.get("ct", ""),
                "account_type": account_type,
                "guest": False,
            },
        }

    def _agreement_response(self) -> dict:
        """生成用户协议查询响应。"""
        return {
            "ret": 1,
            "msg": "成功",
            "data": {
                "is_show": False,
                "protocol": SDK_PROTOCOL_VERSION,
            },
        }

    def set_clipboard_text(self, text: str) -> None:
        """保存本地剪贴板文本供云端拉取。"""
        self.clipboard_text = text

    def _clipboard_response(self, key: str) -> dict:
        """生成云端剪贴板读取响应。"""
        content = self.clipboard_text
        if self.clipboard_getter is not None:
            try:
                content = self.clipboard_getter()
                self.clipboard_text = content
            except Exception as exc:
                logger.warning("clipboard getter failed: %s", exc)
        return {"ret": 0, "key": key, "content": content}

    def _init_callbacks(self) -> list[dict]:
        """生成 SDK 初始化回调列表。"""
        return [
            {
                "f": "on_set_box_config",
                "p": json.dumps(
                    {
                        "save_image_loading": True,
                        "save_image_time_out": "10",
                        "get_clipboard_data_timeout": "3",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            {
                "f": "on_init_response",
                "p": json.dumps(
                    {
                        "index": -1,
                        "data": json.dumps(
                            {"ret": 0, "msg": "mihoyo web sdk init success"},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]

    def handle(self, message: dict) -> list[dict]:
        """处理 SDK game-data 调用并生成应答消息。"""
        function_name = message.get("f", "")
        index = int(message.get("i") or 0)
        raw_params = message.get("p", "")
        if function_name == "cloud_get_clipboard_data":
            return [self._response(index, self._clipboard_response(str(raw_params)))]
        if function_name == "cloud_get_data":
            key = str(raw_params)
            return [self._response(index, self.cloud_data.get(key, ""))]
        if function_name == "cloud_set_data":
            self._store_cloud_data(raw_params)
            if index < 0:
                return []
            return [self._response(index, "")]
        if function_name == "invoke_return":
            return self._handle_invoke_return(index, raw_params)
        if function_name in ("invoke", "invokeF", "invokeP"):
            return self._handle_invoke(index, function_name, raw_params)
        if function_name == "webview":
            return self._handle_webview(index, raw_params)
        logger.debug("unhandled SDK message f=%s p=%s", function_name, raw_params)
        return []

    def _store_cloud_data(self, raw_params) -> None:
        """解析并写入 cloud_set_data 的键值。"""
        try:
            params = json.loads(raw_params)
        except Exception:
            return
        if "key" in params or "name" in params:
            key = str(params.get("key") or params.get("name") or "")
            value = params.get("data", "")
            if key:
                self.cloud_data[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return
        for key, value in params.items():
            self.cloud_data[str(key)] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def _handle_invoke_return(self, index: int, raw_params) -> list[dict]:
        """处理 invoke_return 调用。"""
        try:
            nested = json.loads(raw_params)
        except json.JSONDecodeError:
            return [self._error_response(index, "invoke_return", "invoke")]
        nested_name = nested.get("f", "")
        nested_params = nested.get("p", "")
        if nested_name == "info_get_cps":
            # 手机(触控)模式下要回 "touch_mihoyo", 跟随 platform_profile
            return [self._invoke_response(index, getattr(self.core_config.platform_profile, "cps", SDK_CPS))]
        if nested_name == "info_get_uapc":
            return [self._invoke_response(index, "")]
        if nested_name in ("info_get_channel_id", "info_get_sub_channel_id"):
            return [self._invoke_response(index, SDK_CHANNEL_ID_RESPONSE)]
        if nested_name == "get_disk_type":
            return [self._error_response(index, "diskType", "get")]
        if nested_name == "cloud_keep_alive":
            return [self._invoke_response(index, "")]
        if nested_name in (
                "launch_show_user_agreement_with_parameters",
                "launch_show_user_agreement_with_parameters_compliance",
        ):
            return [self._invoke_response(index, {"ret": 0, "msg": "OK"})]
        logger.debug("unhandled SDK invoke_return f=%s p=%s", nested_name, nested_params)
        return [self._error_response(index, nested_name, "invoke")]

    def _handle_invoke(self, index: int, function_name: str, raw_params) -> list[dict]:
        """处理 invoke / invokeF / invokeP 调用。"""
        try:
            nested = json.loads(raw_params)
        except json.JSONDecodeError:
            nested = {}
        nested_name = nested.get("f") or nested.get("invokeF") or nested.get("invokeP") or function_name
        if nested_name == "login_login":
            return [self._invoke_response(index, self._login_response())]
        if nested_name in (
                "launch_show_user_agreement_with_parameters",
                "launch_show_user_agreement_with_parameters_compliance",
        ):
            return [self._invoke_response(index, self._agreement_response())]
        if nested_name == "Init":
            return self._init_callbacks()
        if index < 0 and str(nested_name).startswith("report_"):
            return []
        if index < 0 and nested_name in (
                "all_set_env",
                "all_set_language",
                "all_set_volume",
                "info_set_game_parameters",
                "info_set_game_version",
                "login_set_server_id",
                "report_set_info",
                "watermark_set_enable",
                "web_set_joypad_enable",
                "web_set_linear",
        ):
            return []
        logger.debug("unhandled SDK %s nested=%s p=%s", function_name, nested_name, raw_params)
        return [self._error_response(index, str(nested_name), "invoke")]

    def _handle_webview(self, index: int, raw_params) -> list[dict]:
        """处理 webview 调用。"""
        try:
            nested = json.loads(raw_params)
        except json.JSONDecodeError:
            nested = {}
        if nested.get("f") == "get_global_user_agent":
            return [self._invoke_response(index, self.core_config.protocol_profile.sdk_webview_ua)]
        if index < 0 and nested.get("f") in ("set_global_user_agent", "pre_load"):
            return []
        logger.debug("unhandled SDK webview nested=%s p=%s", nested.get("f"), raw_params)
        return [self._error_response(index, str(nested.get("f") or "webview"), "webview")]


class WsPayloadFormatter:
    @staticmethod
    def summarize(payload: bytes | str, payload_limit: int = 0) -> tuple[str, list[str]]:
        """把 WebSocket 载荷解析成摘要和详情。"""
        if isinstance(payload, str):
            return f"text len={len(payload)}", [payload]

        try:
            frame = Protocol.parse_ws_frame(payload)
        except Exception as exc:
            return f"raw-binary len={len(payload)} parse_error={exc}", [LogText.hex_dump(payload, payload_limit)]

        frame_type = frame["frame_type"]
        frame_name = FRAME_TYPE_NAMES.get(frame_type, f"TYPE_{frame_type}")
        body = frame["payload"]
        summary = f"frame={frame_name}({frame_type}) ws_bytes={len(payload)} payload_bytes={len(body)}"
        details: list[str] = []

        if frame_type in (FRAME_SIGNALING, FRAME_HANDSHAKE):
            text = LogText.safe_decode(body)
            if frame_type == FRAME_SIGNALING:
                try:
                    obj = json.loads(text)
                    summary += f" signaling_type={obj.get('type')}"
                except json.JSONDecodeError:
                    pass
            details.append(text)
            return summary, details

        if frame_type == FRAME_PROXY:
            try:
                packet = Protocol.parse_packet(body)
                cmd_id = packet["cmd_id"]
                cmd_name = CMD_NAMES.get(cmd_id, "Unknown")
                msg = packet["message"]
                summary += f" cmd={cmd_id}({cmd_name}) msg_bytes={len(msg)}"
                if cmd_id == CMD_RELIABLE_MESSAGE_QUEUE_DATA:
                    try:
                        rmq = Protocol.parse_rmq(msg)
                        summary += (
                            f" rmq_id={rmq['msg_id']} rmq_type={rmq['msg_type']}"
                            f" seq={rmq['seq_id']}/{rmq['seq_cnt']}"
                            f" len={rmq['data_len']}/{rmq['total_len']}"
                        )
                    except Exception as exc:
                        summary += f" rmq_parse_error={exc}"
                elif cmd_id in (20002, 20005, CMD_RTC_NOT_PLAYING_TIPS, 20018, 20021):
                    try:
                        details.append("proto_fields=" + repr(Protocol.proto_fields(msg)))
                    except Exception as exc:
                        details.append(f"proto_fields_error={exc}")
                details.append("packet_message_hex=\n" + LogText.hex_dump(msg, payload_limit))
            except Exception as exc:
                summary += f" packet_parse_error={exc}"
                details.append("proxy_payload_hex=\n" + LogText.hex_dump(body, payload_limit))
            return summary, details

        details.append("payload_hex=\n" + LogText.hex_dump(body, payload_limit))
        return summary, details


class TrackConsumer:
    def __init__(
            self,
            snapshot_dir: Path | None = None,
            snapshot_interval: float = 5.0,
            video_frame_callback=None,
            video_frame_interval: float | None = 0.1,
            video_frame_request_event: threading.Event | None = None,
            video_connected_event: asyncio.Event | None = None,
            video_connected_callback: Callable[[], None] | None = None,
            video_ended_callback: Callable[[], None] | None = None,
    ) -> None:
        """初始化实例并保存运行所需的状态。"""
        self.snapshot_dir = snapshot_dir
        self.snapshot_interval = snapshot_interval
        self.video_frame_callback = video_frame_callback
        self.video_frame_interval = video_frame_interval
        self.video_frame_request_event = video_frame_request_event
        self.video_connected_event = video_connected_event
        self.video_connected_callback = video_connected_callback
        self.video_ended_callback = video_ended_callback

    async def consume(self, track) -> None:
        """消费媒体轨道并按需回调视频帧或保存快照。"""
        count = 0
        next_snapshot_at = time.monotonic()
        next_video_callback_at = time.monotonic()
        try:
            while True:
                frame = await asyncio.wait_for(track.recv(), 30.0) if track.kind == 'video' else await track.recv()
                count += 1
                if count == 1:
                    self._on_first_frame(track, frame)
                if count % 120 == 0:
                    self._log_progress(track, frame, count)
                if track.kind == "video":
                    next_snapshot_at, next_video_callback_at = await self._handle_video_frame(
                        frame, count, next_snapshot_at, next_video_callback_at
                    )
        except (MediaStreamError, asyncio.TimeoutError):
            logger.debug("%s track ended after %s frames", track.kind, count)
            if track.kind == "video" and self.video_ended_callback is not None:
                self.video_ended_callback()

    def _on_first_frame(self, track, frame) -> None:
        """记录并通知首帧到达。"""
        if track.kind == "video":
            logger.debug(
                "video first frame received size=%sx%s",
                getattr(frame, "width", "?"), getattr(frame, "height", "?"),
            )
            if self.video_connected_event is not None:
                self.video_connected_event.set()
            if self.video_connected_callback is not None:
                self.video_connected_callback()
        else:
            logger.debug("%s first frame received", track.kind)

    @staticmethod
    def _log_progress(track, frame, count: int) -> None:
        """周期性记录轨道进度。"""
        pts = getattr(frame, "pts", None)
        width = getattr(frame, "width", None)
        height = getattr(frame, "height", None)
        logger.debug("%s frame count=%s pts=%s size=%sx%s", track.kind, count, pts, width, height)

    async def _handle_video_frame(
            self, frame, count: int, next_snapshot_at: float, next_video_callback_at: float
    ) -> tuple[float, float]:
        """处理一帧视频：按需回调与保存快照，返回更新后的下次触发时间。"""
        requested = self.video_frame_request_event is not None and self.video_frame_request_event.is_set()
        auto_trigger = self.video_frame_interval is not None and time.monotonic() >= next_video_callback_at
        need_callback = self.video_frame_callback is not None and (requested or auto_trigger)
        need_snapshot = self.snapshot_dir is not None and time.monotonic() >= next_snapshot_at
        if not (need_callback or need_snapshot):
            return next_snapshot_at, next_video_callback_at

        image = frame.reformat(format="rgb24").to_image()
        if need_callback:
            self.video_frame_callback(image, count)
            if requested and self.video_frame_request_event is not None:
                self.video_frame_request_event.clear()
            if auto_trigger and self.video_frame_interval is not None:
                next_video_callback_at = time.monotonic() + self.video_frame_interval
        if need_snapshot:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.snapshot_dir / f"video_{int(time.time())}_{count:06d}.jpg"
            await asyncio.to_thread(image.save, path, "JPEG", quality=90)
            logger.info("saved snapshot %s", path)
            next_snapshot_at = time.monotonic() + self.snapshot_interval
        return next_snapshot_at, next_video_callback_at


class ControlActionScript:
    @staticmethod
    def pair(value: str) -> tuple[float, float]:
        """解析逗号分隔的坐标对。"""
        left, right = value.split(",", 1)
        return float(left), float(right)

    @staticmethod
    def resolution(value: str) -> tuple[float, float]:
        """解析宽高分辨率字符串。"""
        try:
            left, right = value.lower().split("x", 1)
            width = float(left)
            height = float(right)
        except Exception:
            return 0.0, 0.0
        return width, height

    @classmethod
    def load(cls, path: str | None, click: str | None) -> list[dict]:
        """从文件或点击参数加载控制动作。"""
        actions = []
        if path:
            actions.extend(json.loads(Path(path).read_text()))
        if click:
            x, y = cls.pair(click)
            actions.append({"at": 1.0, "type": "click", "x": x, "y": y, "button": "left"})
        return sorted(actions, key=lambda action: float(action.get("at", 0)))

    @staticmethod
    async def run(channel, actions: list[dict], normalize_size: tuple[float, float] = (0.0, 0.0)) -> None:
        """按时间轴执行控制动作脚本。"""
        started = time.monotonic()
        for action in actions:
            wait = float(action.get("at", 0)) - (time.monotonic() - started)
            if wait > 0:
                await asyncio.sleep(wait)
            action_type = action.get("type")
            if action_type in ("move", "down", "up", "scroll", "key_down", "key_up"):
                packet = Protocol.input_from_action(action, normalize_size)
                if packet is not None:
                    channel.send(packet)
                logger.debug("sent action type=%s", action_type)
            elif action_type in ("touch", "touch_down", "touch_move", "touch_up"):
                packet = Protocol.input_from_action(action, normalize_size)
                if packet is not None:
                    channel.send(packet)
                logger.debug(
                    "sent touch type=%s finger=%s x=%s y=%s",
                    action_type,
                    action.get("finger_id", 0),
                    action.get("x"),
                    action.get("y"),
                )
            elif action_type == "touch_tap":
                # 触屏点击 = down → 短暂停留 → up(同点), 供动作脚本使用
                packet = Protocol.input_from_action({**action, "type": "touch_down"}, normalize_size)
                if packet is not None:
                    channel.send(packet)
                await asyncio.sleep(float(action.get("duration", 0.08)))
                packet = Protocol.input_from_action({**action, "type": "touch_up"}, normalize_size)
                if packet is not None:
                    channel.send(packet)
                logger.debug("sent touch tap x=%s y=%s", action.get("x"), action.get("y"))
            elif action_type == "click":
                down_action = {**action, "type": "down"}
                packet = Protocol.input_from_action(down_action, normalize_size)
                if packet is not None:
                    channel.send(packet)
                await asyncio.sleep(float(action.get("duration", 0.08)))
                up_action = {**action, "type": "up"}
                packet = Protocol.input_from_action(up_action, normalize_size)
                if packet is not None:
                    channel.send(packet)
                logger.debug(
                    "sent mouse click button=%s x=%s y=%s",
                    action.get("button", "left"),
                    action.get("x"),
                    action.get("y"),
                )
            else:
                raise ValueError(f"unsupported control action type: {action_type}")

    @classmethod
    async def run_when_channel_opens(
            cls,
            channel,
            actions: list[dict],
            normalize_size: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """等待 DataChannel 打开后执行控制动作。"""
        for _ in range(100):
            if getattr(channel, "readyState", "") == "open":
                logger.debug("datachannel ready %s", channel.label)
                await cls.run(channel, actions, normalize_size)
                return
            await asyncio.sleep(0.1)
        logger.warning("datachannel action skipped; channel did not open")


class GameControlChannel:
    def __init__(
            self,
            urls: list[str],
            session_id: int,
            status_callback=None,
            core_config: CoreConfig | dict | None = None,
    ) -> None:
        """初始化实例并保存运行所需的状态。"""
        self.urls = [url for url in urls if url]
        self.session_id = session_id
        self.status_callback = status_callback
        self.core_config = normalize_core_config(core_config)
        self.websocket = None
        self.alive = False
        self.last_pong_at = time.monotonic()
        self.recv_task = None
        self.ping_task = None

    def _url_with_session_id(self, url: str) -> str:
        """给控制通道 URL 附加 session_id。"""
        parts = urlsplit(url)
        scheme = "wss" if parts.scheme in ("", "http", "https", "ws", "wss") else parts.scheme
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["sessionId"] = str(self.session_id)
        return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def status(self, message: str, level: int = logging.INFO) -> None:
        """输出控制通道状态并转发回调。"""
        logger.log(level, message)
        emit_log_callback(self.status_callback, message, level)

    def valid(self) -> bool:
        """判断当前连接状态是否仍可发送数据。"""
        return self.alive and self.websocket is not None

    async def connect(self) -> bool:
        """依次尝试连接游戏控制通道地址。"""
        for url in self.urls:
            full_url = self._url_with_session_id(url)
            try:
                self.status(f"game control channel connecting {urlsplit(full_url).netloc}", level=logging.DEBUG)
                headers = {
                    "Origin": GAME_CONTROL_ORIGIN,
                    "User-Agent": self.core_config.browser_profile.user_agent,
                }
                self.websocket = await connect_websocket(full_url, timeout=5, headers=headers)
                self.recv_task = asyncio.create_task(self.recv_loop())
                await self.send_control_packet(CMD_KCP_CONNECT_SYNC)
                return True
            except Exception as exc:
                self.status(f"game control channel connect failed: {type(exc).__name__}: {exc}", level=logging.DEBUG)
                self.websocket = None
        return False

    async def send_control_packet(self, cmd_id: int) -> None:
        """发送控制通道握手或心跳包。"""
        if self.websocket is None:
            return
        payload = Protocol.packet(cmd_id, Protocol.uint32(1, self.session_id))
        await self.websocket.send(b"\x00" + payload)

    async def send_data(self, payload: bytes) -> bool:
        """通过游戏控制通道发送输入数据。"""
        if not self.valid() or self.websocket is None:
            return False
        await self.websocket.send(b"\x01" + payload)
        return True

    async def recv_loop(self) -> None:
        """持续接收并处理游戏控制通道消息。"""
        try:
            assert self.websocket is not None
            while True:
                message = await self.websocket.recv()
                if isinstance(message, str):
                    message = message.encode()
                if not message:
                    continue
                kind = message[0]
                payload = message[1:]
                if kind == 0:
                    await self._handle_control_packet(payload)
                elif kind == 1:
                    pass
        except Exception as exc:
            self.status(f"game control channel recv stopped: {type(exc).__name__}: {exc}", level=logging.DEBUG)
        finally:
            self.alive = False

    async def _handle_control_packet(self, payload: bytes) -> None:
        """处理一条控制（kind=0）数据包。"""
        packet = Protocol.parse_packet(payload)
        cmd_id = packet["cmd_id"]
        if cmd_id == CMD_KCP_CONNECT_SYNC_ACK:
            await self.send_control_packet(CMD_KCP_CONNECT_ACK)
            self.alive = True
            self.last_pong_at = time.monotonic()
            self.status("game control channel ready", level=logging.DEBUG)
            if self.ping_task is None or self.ping_task.done():
                self.ping_task = asyncio.create_task(self.ping_loop())
        elif cmd_id == CMD_KCP_PONG:
            self.last_pong_at = time.monotonic()
        else:
            self.status(f"game control channel unknown control cmd={cmd_id}", level=logging.DEBUG)

    async def ping_loop(self) -> None:
        """定时发送控制通道 ping 并检测超时。"""
        while self.valid():
            await self.send_control_packet(CMD_KCP_PING)
            if time.monotonic() - self.last_pong_at > 5:
                self.status("game control channel pong timeout", level=logging.DEBUG)
                await self.close()
                return
            await asyncio.sleep(2.0)

    async def close(self) -> None:
        """关闭控制通道及其后台任务。"""
        self.alive = False
        if self.ping_task is not None:
            self.ping_task.cancel()
        if self.recv_task is not None:
            self.recv_task.cancel()
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None


@dataclass
class SessionConfig:
    finish_result: dict | None = None
    sdk_login: dict | None = None
    max_seconds: int = 0
    snapshot_dir: str | None = None
    snapshot_interval: float = 5.0
    video_frame_interval: float | None = 0.1
    control_actions: list[dict] | None = None
    ws_log_payload: bool = True
    ws_payload_limit: int = 2048
    color: bool = False
    cookie: str | None = None
    combo_token: str = ""
    channel_token: str = ""
    clipboard_getter: Callable[[], str] | None = None
    core_config: CoreConfig = field(default_factory=CoreConfig)
    video_frame_request_event: threading.Event | None = None


class GameSession:
    """单次 ``/rtc-sdk`` WebRTC 会话的完整状态机。

    一个实例对应一次云游戏连接。它把原先散落在巨型函数闭包里的所有可变
    状态收敛为实例属性，并把嵌套闭包改写成方法，便于阅读、维护与测试：

    - ``ws`` / ``session``：信令用的 WebSocket 及其 curl 会话。
    - ``gcc`` / ``rtc_channel``：游戏控制通道与 RTC DataChannel，输入优先走
      前者，不可用时回退到后者。
    - ``rmq_*``：可靠消息队列（RMQ）的收发序号与分片重组缓冲。
    - ``device_info_sent`` / ``startup_config_sent``：保证只发一次的标志。

    生命周期由 :meth:`run` 驱动：建立 WebSocket → 发送 StartGame/握手 →
    在 offer/answer/ICE 完成后下发启动配置 → 持续应答 RMQ + SDK game-data，
    直到超时或 ``stop_event`` 被设置。
    """

    def __init__(
            self,
            config: "SessionConfig",
            *,
            video_frame_callback=None,
            ws_event_callback=None,
            status_callback=None,
            stop_event=None,
            input_ready_callback=None,
    ) -> None:
        """初始化实例并保存运行所需的状态。"""
        self.config = config
        self.video_frame_callback = video_frame_callback
        self.ws_event_callback = ws_event_callback
        self.status_callback = status_callback
        self.stop_event = stop_event
        self.input_ready_callback = input_ready_callback

        # ---- 由 finish_result 解析出的连接参数 ----
        finish_doc = self._load_finish_result()
        self.params = Protocol.sdk_params(finish_doc["sdk_param"])
        self.normalize_size = ControlActionScript.resolution(self.params.resolution)
        self.actions = config.control_actions or []
        self.snapshot_path = Path(config.snapshot_dir) if config.snapshot_dir else None

        # ---- SDK game-data 应答器 ----
        account_sync = finish_doc.get("_account_sync")
        account_sync = account_sync if isinstance(account_sync, dict) else {}
        self.sdk_game_data = SdkGameDataHandler(
            sdk_login=config.sdk_login or finish_doc.get("sdk_login") or account_sync.get("sdk_login"),
            cookie=config.cookie,
            combo_token=config.combo_token,
            channel_token=config.channel_token,
            clipboard_getter=config.clipboard_getter,
            core_config=config.core_config,
        )

        # ---- 运行时状态 ----
        self.pc = RTCPeerConnection()
        self.ws = None  # 信令 WebSocket
        self.gcc: GameControlChannel | None = None  # 游戏控制通道（输入首选）
        self.rtc_channel = None  # RTC DataChannel（输入回退）
        self.video_connected = asyncio.Event()
        self.first_video = self.video_connected  # 向后兼容别名
        self._video_received = False
        self._stop_ack = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self.heartbeat_task: asyncio.Task | None = None
        self.keep_playing_task: asyncio.Task | None = None
        self.stop_watcher_task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

        # 受管理的 fire-and-forget 任务集合：避免被 GC 提前回收，并统一记录异常。
        self._tasks: set[asyncio.Task] = set()

        # 只发一次的握手步骤标志。
        self.device_info_sent = False
        self.startup_config_sent = False

        # RMQ 收发状态：发送序号自增，接收侧按 total_len 重组分片。
        self.rmq_ack_outgoing_id = 1
        self.rmq_game_outgoing_id = 1
        self._rmq_rx_data = b""
        self._rmq_rx_msg_id = 0
        self._rmq_rx_total_len = 0

        # 输入被丢弃时的限流日志时间戳（避免刷屏）。
        self._last_input_skip_log_at = 0.0

    def _load_finish_result(self) -> dict:
        """读取会话所需的 finish_result。"""
        if self.config.finish_result is not None:
            return self.config.finish_result
        raise RuntimeError("missing finish_result; load it in UI or dispatch before connect")

    # ------------------------------------------------------------------
    # 后台任务管理
    # ------------------------------------------------------------------
    def _spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """启动一个受管理的后台任务，保存引用并记录异常。"""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """后台任务结束时清理引用并记录未捕获异常。"""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("background task %s failed: %r", task.get_name(), exc)

    @property
    def game_control_urls(self) -> list[str]:
        """汇总可用的游戏控制通道地址。"""
        return [self.params.game_control_channel_url, *self.params.additional_game_control_channel_urls]

    @property
    def has_game_control_channel(self) -> bool:
        """判断独立游戏控制通道是否可用。"""
        return self.gcc is not None and self.gcc.valid()

    @property
    def has_rtc_channel(self) -> bool:
        """判断 RTC DataChannel 是否处于打开状态。"""
        return self.rtc_channel is not None and getattr(self.rtc_channel, "readyState", "") == "open"

    @property
    def stopped(self) -> bool:
        """判断外部停止事件是否已触发。"""
        return self.stop_event is not None and self.stop_event.is_set()

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------
    def status(self, message: str, level: int = logging.INFO) -> None:
        """输出一条面向用户的状态文本：写日志并转发给 UI 回调。"""
        logger.log(level, message)
        emit_log_callback(self.status_callback, message, level)

    def _log_ws(self, direction: str, payload: bytes | str, flags=None) -> None:
        """格式化并输出一条 WebSocket 收发记录，同时喂给 ws_event_callback。"""
        is_send = direction == "SEND"
        line_color = COLOR_SEND if is_send else COLOR_RECV
        arrow = ">>" if is_send else "<<"
        summary, details = WsPayloadFormatter.summarize(payload, self.config.ws_payload_limit)
        if flags is not None:
            summary += f" flags={flags}"
        now = time.strftime("%H:%M:%S")
        logger.debug(LogText.colorize(f"[{now}] WS {arrow} {summary}", line_color, self.config.color))
        if self.ws_event_callback is not None:
            self.ws_event_callback({
                "time": now,
                "direction": direction,
                "arrow": arrow,
                "summary": summary,
                "details": details if self.config.ws_log_payload else [],
            })
        if not self.config.ws_log_payload:
            return
        for detail in details:
            if detail.startswith(("packet_message_hex=", "proxy_payload_hex=", "payload_hex=")):
                label, value = detail.split("=", 1)
                logger.debug(LogText.colorize(f"{label}:\n{value}", line_color, self.config.color))
            else:
                logger.debug(LogText.colorize(LogText.shorten(detail, self.config.ws_payload_limit), line_color,
                                              self.config.color))

    async def _ws_send(self, payload: bytes) -> None:
        """发送一帧二进制 WebSocket 数据。"""
        self._log_ws("SEND", payload)
        await self.ws.send(payload)

    async def _ws_recv(self) -> bytes:
        """阻塞接收一帧 WebSocket 数据并记录日志。"""
        payload = await self.ws.recv()
        self._log_ws("RECV", payload)
        return payload

    async def _send_signaling(self, obj: dict) -> None:
        """以 SIGNALING 帧发送一段 WebRTC 信令 JSON。"""
        await self._ws_send(Protocol.ws_frame(FRAME_SIGNALING, json.dumps(obj)))

    # ------------------------------------------------------------------
    # 输入转发（供 CloudGame 从外部线程跨线程调用）
    # ------------------------------------------------------------------
    def send_input(self, action: dict) -> bool:
        """把一个输入动作排进本会话事件循环。可从任意线程安全调用。"""
        if self.loop is None:
            return False
        self.loop.call_soon_threadsafe(self._dispatch_input, action)
        return True

    def _dispatch_input(self, action: dict) -> None:
        """把输入动作编码并选择可用通道发送。"""
        if action.get("type") == "clipboard":
            text = str(action.get("text") or "")
            self.sdk_game_data.set_clipboard_text(text)
            packet = Protocol.input_clipboard(text)
        else:
            packet = Protocol.input_from_action(action, self.normalize_size)
        if packet is None:
            return
        action_type = action.get("type")
        # 优先走游戏控制通道，其次回退到 RTC DataChannel。
        if self.has_game_control_channel:
            self._spawn(self.gcc.send_data(packet), name="gcc-send-input")
            self.status(f"input {action_type} via game control channel", level=logging.DEBUG)
            return
        if self.has_rtc_channel:
            self.rtc_channel.send(packet)
            self.status(f"input {action_type} via rtc datachannel fallback", level=logging.DEBUG)
            return
        now = time.monotonic()
        if now - self._last_input_skip_log_at >= 2.0:
            self._last_input_skip_log_at = now
            self.status(f"input {action_type} skipped: no open game control/rtc datachannel", level=logging.DEBUG)

    async def wait_for_video_connected(self) -> bool:
        """等待视频流首帧到达。

        返回 ``True`` 表示成功收到首帧；返回 ``False`` 表示会话已结束
        （如连接断开）且未收到首帧。
        """
        await self.video_connected.wait()
        return self._video_received

    # ------------------------------------------------------------------
    # 一次性握手步骤
    # ------------------------------------------------------------------
    async def _send_device_info_once(self, protocol: str = "udp") -> None:
        """上报一次 SdkDeviceInfo（重复调用会被标志拦截）。"""
        if self.ws is None or self.device_info_sent:
            return
        await self._ws_send(Protocol.device_info(
            self.params, protocol,
            terminal_type=9 if self.config.core_config.is_touch else 10,
            core_config=self.config.core_config,
        ))
        self.device_info_sent = True
        logger.debug("sent SdkDeviceInfo transport=%s", protocol)

    async def _send_startup_config_once(self, reason: str) -> None:
        """ICE / DataChannel 就绪后下发整套启动配置，仅执行一次。

        顺序与网页 SDK 一致：画质模式 → 码率倍率 → 保活配置 → 恢复请求 →
        设备信息。任一条缺失都可能让云端停在初始化阶段。
        """
        if self.ws is None or self.startup_config_sent:
            return
        self.startup_config_sent = True
        logger.debug("sending startup config after %s", reason)
        session = self.config.core_config.session_profile
        await self._ws_send(Protocol.graphics_mode(session.graphics_mode))
        await self._ws_send(Protocol.bitrate_multiplier(session.bitrate_multiplier))
        await self._ws_send(Protocol.keepalive_config())
        await self._ws_send(Protocol.resume(self.params))
        await self._send_device_info_once(STARTUP_TRANSPORT_PROTOCOL)

    async def _send_keep_playing(self, reason: str) -> None:
        """通知云端本端仍在游玩，避免空闲保活窗口触发踢出。"""
        if self.ws is None:
            return
        await self._ws_send(Protocol.keep_playing(self.params))
        logger.debug("sent RtcKeepPlaying reason=%s", reason)

    async def _keep_playing_loop(self) -> None:
        """按网页端行为定期发送续玩包。"""
        try:
            while not self.stopped:
                await asyncio.sleep(60)
                if self.ws is None:
                    return
                await self._send_keep_playing("periodic")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("keep playing loop stopped: %s", exc)

    async def _request_key_frame_later(self, delay: float = 1.0) -> None:
        """answer 之后延迟请求一个关键帧，加速首帧出图。"""
        await asyncio.sleep(delay)
        await self._ws_send(Protocol.key_frame_request())
        logger.debug("sent KeyFrameReq")

    async def _start_game_control_channel(self, session_id: int) -> None:
        """握手拿到 session_id 后，尝试建立独立的游戏控制通道。"""
        if self.has_game_control_channel:
            return
        if not any(self.game_control_urls):
            self.status("game control channel skipped: no url", level=logging.DEBUG)
            return
        self.gcc = GameControlChannel(
            self.game_control_urls,
            session_id,
            status_callback=self.status_callback,
            core_config=self.config.core_config,
        )
        if not await self.gcc.connect():
            self.status("game control channel unavailable; using rtc datachannel fallback", level=logging.DEBUG)

    # ------------------------------------------------------------------
    # RTCPeerConnection 事件
    # ------------------------------------------------------------------
    def _register_pc_handlers(self) -> None:
        """把所有 pc 事件回调注册到对应的实例方法上。"""
        self.pc.on("track")(self._on_track)
        self.pc.on("datachannel")(self._on_datachannel)
        self.pc.on("iceconnectionstatechange")(self._on_ice_state_change)
        self.pc.on("connectionstatechange")(self._on_connection_state_change)

    def _on_track(self, track) -> None:
        """处理远端媒体轨道并启动消费任务。"""
        self.status(f"track {track.kind}", level=logging.DEBUG)
        is_video = track.kind == "video"
        consumer = TrackConsumer(
            self.snapshot_path,
            self.config.snapshot_interval,
            video_frame_callback=self.video_frame_callback,
            video_frame_interval=self.config.video_frame_interval,
            video_frame_request_event=self.config.video_frame_request_event,
            video_connected_event=self.video_connected if is_video else None,
            video_connected_callback=self._mark_video_received if is_video else None,
            video_ended_callback=self._video_ended if is_video else None,
        )
        self._spawn(consumer.consume(track), name=f"consume-{track.kind}")

    def _video_ended(self) -> None:
        if self.ws is not None and self.ws.state.name == "OPEN":
            self._spawn(self.ws.close(), name="video-ended")

    def _mark_video_received(self) -> None:
        """标记已收到视频首帧。"""
        self._video_received = True

    def _on_datachannel(self, channel) -> None:
        """处理远端 DataChannel 并注册通道回调。"""
        logger.debug("datachannel %s ordered=%s", channel.label, channel.ordered)
        self.rtc_channel = channel
        if self.actions:
            self._spawn(
                ControlActionScript.run_when_channel_opens(channel, self.actions, self.normalize_size),
                name="control-actions",
            )

        @channel.on("open")
        def on_open():
            """处理 DataChannel 打开事件。"""
            logger.debug("datachannel open %s", channel.label)
            if self.heartbeat_task is None or self.heartbeat_task.done():
                self.heartbeat_task = asyncio.create_task(self._heartbeat_loop(channel))
            self._spawn(self._send_startup_config_once("datachannel open"), name="startup-config")

        @channel.on("close")
        def on_close():
            """处理 DataChannel 关闭事件。"""
            if self.rtc_channel is channel:
                self.rtc_channel = None
            self.status("rtc datachannel closed", level=logging.DEBUG)

        @channel.on("message")
        def on_message(message):
            """记录 DataChannel 收到的消息大小。"""
            size = len(message) if isinstance(message, (bytes, bytearray)) else len(str(message))
            logger.debug("datachannel message bytes %d", size)

    async def _heartbeat_loop(self, channel) -> None:
        """每秒发一个心跳；通道可用时走游戏控制通道，否则走 DataChannel。"""
        heartbeat_id = 1
        while getattr(channel, "readyState", "") == "open":
            packet = Protocol.heartbeat_packet(heartbeat_id)
            if self.has_game_control_channel:
                await self.gcc.send_data(packet)
            else:
                channel.send(packet)
            heartbeat_id += 1
            await asyncio.sleep(1.0)

    async def _on_ice_state_change(self) -> None:
        """根据 ICE 状态补发启动配置。"""
        state = self.pc.iceConnectionState
        if state == "failed":
            logger.warning("iceConnectionState %s", state)
            if self.ws is not None:
                await self.ws.close()
        else:
            logger.debug("iceConnectionState %s", state)
        if state in ("connected", "completed"):
            await self._send_startup_config_once(f"ICE {state}")

    async def _on_connection_state_change(self) -> None:
        """记录 PeerConnection 连接状态变化。"""
        state = self.pc.connectionState
        if state == "connected":
            logger.info("connected")
        elif state == "failed":
            logger.warning("connectionState %s", state)
        else:
            logger.debug("connectionState %s", state)
        if state == "failed" and self.ws is not None:
            await self.ws.close()

    async def _on_icecandidate(self, candidate) -> None:
        """把本地 ICE candidate 发送给信令服务。"""
        if candidate is None:
            return
        await self._send_signaling({
            "candidate": candidate_to_sdp(candidate),
            "sdp-mid": candidate.sdpMid,
            "sdp-mline-index": candidate.sdpMLineIndex,
            "type": "candidates",
        })

    # ------------------------------------------------------------------
    # RMQ / SDK game-data 应答
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_start_game_rsp(message: bytes) -> None:
        """校验 StartGameRsp（cmd 20002），retcode != 1 时抛出。"""
        fields = {fn: v for fn, _wt, v in Protocol.proto_fields(message)}
        retcode = fields.get(1, 0)
        if retcode != 1:
            raise RuntimeError(f"StartGameRsp failed: retcode={retcode}")
        logger.debug("StartGameRsp OK")

    async def _handle_sdk_game_data(self, rmq_payload: bytes, source_msg_id: int) -> None:
        """解出 SdkGameDataMessage，解密内层 JSON，交给应答器并回包。"""
        try:
            sdk_message = Protocol.parse_sdk_message(rmq_payload)
        except (ValueError, IndexError) as exc:
            logger.warning("SDK game-data parse failed msg_id=%s: %s", source_msg_id, exc)
            return
        if sdk_message["name"] != "SDK":
            logger.debug("SDK game-data ignored name=%s bytes=%d", sdk_message["name"], len(sdk_message["data"]))
            return
        try:
            message = Protocol.decrypt_sdk_json(sdk_message["data"])
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("SDK game-data decrypt failed msg_id=%s: %s", source_msg_id, exc)
            return
        logger.debug("SDK game-data recv %s", json.dumps(message)[:1000])
        for response in self.sdk_game_data.handle(message):
            await self._ws_send(
                Protocol.sdk_game_data_frame(response, outgoing_msg_id=self.rmq_game_outgoing_id)
            )
            logger.debug(
                "SDK game-data send rmq_msg_id=%s %s",
                self.rmq_game_outgoing_id,
                json.dumps(response)[:1000],
            )
            self.rmq_game_outgoing_id += 1

    def _reset_rmq_buffer(self) -> None:
        """复位 RMQ 分片重组缓冲。"""
        self._rmq_rx_data = b""
        self._rmq_rx_msg_id = 0
        self._rmq_rx_total_len = 0

    async def _handle_rmq_message(self, rmq: dict) -> None:
        """处理一条 RMQ 消息：忽略 ack，按 total_len 重组分片后再 ACK。

        重组假设同一时刻只有一条消息在分片且按序到达。若在旧消息未完成时
        又收到新消息的起始分片（type 1/2），会丢弃残缺缓冲并告警，避免错位。
        """
        if rmq["msg_type"] == 4:  # 对端发来的 ack，无需处理
            logger.debug("rmq ack received data=%s", rmq["data"].hex())
            return
        if rmq["msg_type"] not in (1, 2, 3):
            logger.warning("rmq unsupported type %s", rmq["msg_type"])
            return

        # type 1/2 视为消息起始分片；若缓冲非空说明上一条未完成，丢弃并告警。
        if rmq["msg_type"] in (1, 2):
            if self._rmq_rx_data:
                logger.warning(
                    "rmq new message started before previous completed; discarding %d buffered bytes",
                    len(self._rmq_rx_data),
                )
            self._reset_rmq_buffer()
            self._rmq_rx_msg_id = rmq["msg_id"]
            self._rmq_rx_total_len = rmq["total_len"]
        self._rmq_rx_data += rmq["data"]

        total_len = self._rmq_rx_total_len or rmq["total_len"]
        current_len = len(self._rmq_rx_data)
        if current_len < total_len:
            logger.debug("rmq buffered msg_id=%s bytes=%d/%d", self._rmq_rx_msg_id, current_len, total_len)
            return
        if current_len > total_len:
            logger.warning("rmq buffer larger than expected msg_id=%s bytes=%d/%d", self._rmq_rx_msg_id, current_len,
                           total_len)

        # 分片到齐，取出完整消息并复位缓冲。
        complete_msg_id = self._rmq_rx_msg_id or rmq["msg_id"]
        complete_data = self._rmq_rx_data[:total_len]
        self._reset_rmq_buffer()

        await self._handle_sdk_game_data(complete_data, complete_msg_id)
        await self._ws_send(
            Protocol.rmq_ack_frame(complete_msg_id, outgoing_msg_id=self.rmq_ack_outgoing_id)
        )
        self.rmq_ack_outgoing_id += 1
        logger.debug("sent RMQ ack msg_id=%s seq_id=%s", complete_msg_id, rmq["seq_id"])

    # ------------------------------------------------------------------
    # 收到的各类 WebSocket 帧分发
    # ------------------------------------------------------------------
    async def _handle_proxy_frame(self, payload: bytes) -> None:
        """处理 PROXY 帧内的协议包。"""
        packet = Protocol.parse_packet(payload)
        cmd_id = packet["cmd_id"]
        message = packet["message"]
        logger.debug("proxy packet %s message bytes %d", cmd_id, len(message))
        if cmd_id == 20002:
            self._parse_start_game_rsp(message)
        elif cmd_id == 20005:
            await self._handle_stop_game(message)
            return
        if cmd_id == CMD_RTC_NOT_PLAYING_TIPS:
            await self._send_keep_playing("not_playing_tips")
        if cmd_id == CMD_RELIABLE_MESSAGE_QUEUE_DATA:
            rmq = Protocol.parse_rmq(message)
            logger.debug(
                "rmq data msg_id=%s type=%s seq=%s/%s len=%s/%s tag=%s",
                rmq["msg_id"], rmq["msg_type"], rmq["seq_id"], rmq["seq_cnt"],
                rmq["data_len"], rmq["total_len"], rmq["tag"],
            )
            await self._handle_rmq_message(rmq)

    async def _handle_stop_game(self, message: bytes) -> None:
        """处理 StopGameRsp（cmd 20005）：记录原因并关闭信令连接。"""
        fields = {fn: v for fn, _wt, v in Protocol.proto_fields(message)}
        self._stop_ack.set()
        stop_info = fields.get(2, "")
        title = ""
        if isinstance(stop_info, str):
            try:
                title = json.loads(stop_info).get("title", "")
            except Exception:
                title = stop_info
        logger.error("StopGameRsp received: %s", title or "game stopped")
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass

    async def _handle_handshake_frame(self, payload: bytes) -> None:
        """处理握手帧并启动控制通道。"""
        text = payload.decode("utf-8", errors="replace")
        logger.debug("handshake %s", text)
        try:
            session_id = int(json.loads(text).get("session_id") or 0)
        except (ValueError, json.JSONDecodeError):
            session_id = 0
        if session_id:
            self._spawn(self._start_game_control_channel(session_id), name="start-gcc")

    async def _handle_signaling_frame(self, payload: bytes) -> None:
        """处理 offer、answer 和 candidate 信令。"""
        text = payload.decode("utf-8", errors="replace")
        obj = json.loads(text)
        if obj.get("type") == "offer":
            logger.debug("signaling offer bytes %d", len(obj["sdp"]))
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=obj["sdp"], type="offer"))
            answer = await self.pc.createAnswer()
            await self.pc.setLocalDescription(answer)
            await self._send_signaling({
                "sdp": self.pc.localDescription.sdp,
                "type": self.pc.localDescription.type,
            })
            logger.debug("sent answer bytes %d", len(self.pc.localDescription.sdp))
            self._spawn(self._request_key_frame_later(), name="key-frame-req")
        elif obj.get("type") == "candidates":
            obj = Protocol.rewrite_candidate(obj, self.params)
            candidate = candidate_from_sdp(obj["candidate"])
            candidate.sdpMid = obj.get("sdp-mid")
            candidate.sdpMLineIndex = obj.get("sdp-mline-index")
            await self.pc.addIceCandidate(candidate)
            logger.debug("added remote candidate %s %s %s", candidate.protocol, candidate.ip, candidate.port)
        else:
            logger.debug("signaling %s %s", obj.get("type"), text[:300])

    async def stop(self, timeout: float = 10.0) -> None:
        """End the remote instance before closing local transports."""
        async with self._stop_lock:
            if self._stop_ack.is_set():
                return
            if self.ws is None or self.ws.state.name != "OPEN":
                raise ConnectionError("No open signaling channel for cloud exit")
            try:
                await self._ws_send(Protocol.stop_game(self.params))
                await asyncio.wait_for(self._stop_ack.wait(), timeout)
            finally:
                await self.ws.close()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _stop_watcher(self) -> None:
        """轮询 stop_event，被设置后关闭 WebSocket 以打断主循环的阻塞 recv。"""
        if self.stop_event is None:
            return
        while not self.stop_event.is_set():
            await asyncio.sleep(0.1)
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass

    async def _await_start_game_rsp(self) -> None:
        """发送 StartGameReq 并阻塞等待 StartGameRsp 成功。"""
        await self._ws_send(Protocol.start_game_frame(
            self.params, terminal_type=9 if self.config.core_config.is_touch else 10,
            link_tasks_ms=START_GAME_LINK_TASKS_MS,
        ))
        logger.debug("sent StartGameReq")

        # 等待 StartGameRsp 成功后再发送 client hello，避免服务端状态错乱。
        while not self.stopped:
            message = await self._ws_recv()
            if isinstance(message, str):
                logger.debug("text frame %s", message[:200])
                continue
            frame = Protocol.parse_ws_frame(message)
            if frame["frame_type"] != FRAME_PROXY:
                logger.debug("unexpected frame type=%s before StartGameRsp", frame["frame_type"])
                continue
            packet = Protocol.parse_packet(frame["payload"])
            if packet["cmd_id"] == 20002:
                self._parse_start_game_rsp(packet["message"])
                return
            if packet["cmd_id"] == 20005:
                await self._handle_stop_game(packet["message"])
                return
            logger.debug("unexpected proxy cmd_id=%s before StartGameRsp", packet["cmd_id"])

    async def _main_loop(self) -> None:
        """StartGameRsp 之后的主收发循环。"""
        deadline = None if self.config.max_seconds <= 0 else self.loop.time() + self.config.max_seconds
        while (deadline is None or self.loop.time() < deadline) and not self.stopped:
            message = await self._ws_recv()
            if isinstance(message, str):
                logger.debug("text frame %s", message[:200])
                continue
            frame = Protocol.parse_ws_frame(message)
            frame_type = frame["frame_type"]
            if frame_type == FRAME_PROXY:
                await self._handle_proxy_frame(frame["payload"])
            elif frame_type == FRAME_HANDSHAKE:
                await self._handle_handshake_frame(frame["payload"])
            elif frame_type == FRAME_SIGNALING:
                await self._handle_signaling_frame(frame["payload"])
            else:
                logger.debug("frame %s bytes %d", frame_type, len(frame["payload"]))
        if deadline is not None and not self.stopped:
            logger.debug("finished timeout window")

    async def run(self) -> None:
        """建立连接并跑完整个会话，直到超时或被 stop_event 中断。"""
        self.loop = asyncio.get_running_loop()
        self.video_connected.clear()
        self._video_received = False
        self._register_pc_handlers()
        if self.input_ready_callback is not None:
            self.input_ready_callback(True)

        logger.debug("connecting %s", self.params.rtc_wss_url)
        headers = {"User-Agent": self.config.core_config.browser_profile.user_agent}

        try:
            self.ws = await connect_websocket(self.params.rtc_wss_url, timeout=15, headers=headers)
            if self.stop_event is not None:
                self.stop_watcher_task = asyncio.create_task(self._stop_watcher())
            logger.debug("websocket open")
            await self._await_start_game_rsp()
            if self._stop_ack.is_set():
                return

            await self._ws_send(Protocol.ws_frame(FRAME_HANDSHAKE, CLIENT_HELLO_JSON))
            logger.debug("sent client hello")
            self.keep_playing_task = asyncio.create_task(self._keep_playing_loop())

            # ICE candidate 回调依赖 self.ws 已就绪，故在此处注册。
            self.pc.on("icecandidate")(self._on_icecandidate)

            await self._main_loop()
        except ConnectionClosed:
            logger.debug("websocket connection closed")
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        """释放所有资源：解绑输入回调、取消后台任务、关闭通道与连接。

        唤醒 ``wait_for_video_connected`` 的等待者：只 set 不 clear，
        语义（成功 / 失败）由 ``_video_received`` 表达，避免唤醒后又被
        clear 造成竞态或永久阻塞。
        """
        self.video_connected.set()
        if self.input_ready_callback is not None:
            self.input_ready_callback(False)
        if self.stop_watcher_task is not None:
            self.stop_watcher_task.cancel()
        if self.keep_playing_task is not None:
            self.keep_playing_task.cancel()
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self.gcc is not None:
            await self.gcc.close()
        try:
            await self.pc.close()
        except Exception:
            pass
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass


__all__ = ["SessionConfig", "GameSession", "GameControlChannel", "ControlActionScript"]