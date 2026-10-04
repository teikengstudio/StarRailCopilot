from __future__ import annotations

import base64
import json
import re
import struct
import time
from dataclasses import dataclass

from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import CoreConfig, normalize_core_config

CG_SDK_VERSION = "6.2.0.24"

# ---------------------------------------------------------------------------
# SdkDeviceInfo 上报的设备硬件指纹
# ---------------------------------------------------------------------------
# 已迁移到 core.config.DEFAULT_CORE_CONFIG / client_profile.json。
# DEVICE_INFO_CLIENT_LIB = "python-aiortc"
# DEVICE_INFO_DEVICE_NAME = "Unknown"
# DEVICE_INFO_GPU_MODEL = "amd radeon vega series / radeon vega mobile"
# DEVICE_INFO_OS = "Linux undefined"
# DEVICE_INFO_WIDTH = 1707
# DEVICE_INFO_HEIGHT = 1067

CMD_START_GAME_REQ = 20001
CMD_STOP_GAME_REQ = 20004
CMD_RTC_KEEP_PLAYING = 20015
CMD_RTC_NOT_PLAYING_TIPS = 20014
CMD_RTC_RESUME_REQ = 20008
CMD_RTC_KEEPALIVE_CFG = 20021
CMD_KEY_FRAME_REQ = 20032
CMD_RTC_SET_GRAPHICS_MODE = 20036
CMD_RTC_SET_BITRATE_MULTIPLIER = 20039
CMD_RTC_DATA_CHANNEL = 20010
CMD_RELIABLE_MESSAGE_QUEUE_DATA = 1900
CMD_SDK_DEVICE_INFO = 20017
CMD_HEARTBEAT = 20029

CMD_KCP_CONNECT_SYNC = 1250
CMD_KCP_CONNECT_SYNC_ACK = 1251
CMD_KCP_CONNECT_ACK = 1252
CMD_KCP_PING = 1253
CMD_KCP_PONG = 1254

MAGIC_HEAD = b"\x45\x67"
MAGIC_TAIL = b"\x89\xab"
SDK_GAME_DATA_AES_KEY = "OK20kydiRu47rOH7HNXzA12xxtlYVOUx"

FRAME_SIGNALING = 1
FRAME_PROXY = 2
FRAME_HANDSHAKE = 3
FRAME_KEEPALIVE = 4

DATA_TYPE_KEYBOARD_DOWN = 1
DATA_TYPE_KEYBOARD_UP = 2
DATA_TYPE_MOUSE_MOVE = 3
DATA_TYPE_MOUSE_LBUTTON_DOWN = 4
DATA_TYPE_MOUSE_LBUTTON_UP = 5
DATA_TYPE_MOUSE_RBUTTON_DOWN = 6
DATA_TYPE_MOUSE_RBUTTON_UP = 7
DATA_TYPE_MOUSE_MBUTTON_DOWN = 8
DATA_TYPE_MOUSE_MBUTTON_UP = 9
DATA_TYPE_MOUSE_ZDELTA = 10
DATA_TYPE_IME_INPUT = 12
DATA_TYPE_IME_CLIPBOARD = 14
# 触控: RtcDataChannel.type = TOUCH_EVENT_LIST(21), 载荷在 touchList(#13)
DATA_TYPE_TOUCH_EVENT_LIST = 21

# 触点动作 (SDK 枚举 TouchActionType: Down=0 / Up=1 / Move=2)
TOUCH_ACTION_DOWN = 0
TOUCH_ACTION_UP = 1
TOUCH_ACTION_MOVE = 2
TOUCH_ACTION_TYPES: dict[str, int] = {
    "down": TOUCH_ACTION_DOWN,
    "up": TOUCH_ACTION_UP,
    "move": TOUCH_ACTION_MOVE,
}
# SDK TouchController.sendTouchEventList: 0 === e.length || e.length > 10 → 拒绝
MAX_TOUCH_POINTS = 10

FRAME_TYPE_NAMES = {
    FRAME_SIGNALING: "SIGNALING",
    FRAME_PROXY: "PROXY",
    FRAME_HANDSHAKE: "HANDSHAKE",
}

CMD_NAMES = {
    1250: "KcpConnectSync",
    1251: "KcpConnectSyncAck",
    1252: "KcpConnectAck",
    1253: "KcpPing",
    1254: "KcpPong",
    1900: "ReliableMessageQueueData",
    20001: "StartGameReq",
    20002: "StartGameRsp",
    20004: "StopGameReq",
    20005: "StopGameRsp",
    20008: "RtcResumeReq",
    20014: "RtcNotPlayingTips",
    20015: "RtcKeepPlaying",
    20016: "SdkStatsInfo",
    20017: "SdkDeviceInfo",
    20018: "RtcGameStasReport",
    20021: "RtcKeepaliveCfg",
    20032: "KeyFrameReq",
    20036: "RtcSetGraphicsMode",
    20039: "RtcSetBitrateMultiplier",
}

MOUSE_BUTTON_TYPES = {
    "left": (DATA_TYPE_MOUSE_LBUTTON_DOWN, DATA_TYPE_MOUSE_LBUTTON_UP),
    "right": (DATA_TYPE_MOUSE_RBUTTON_DOWN, DATA_TYPE_MOUSE_RBUTTON_UP),
    "middle": (DATA_TYPE_MOUSE_MBUTTON_DOWN, DATA_TYPE_MOUSE_MBUTTON_UP),
}

_SDK_PARAM_SCALAR_FIELDS = {
    1: "sid",
    2: "ca_id",
    3: "pod_id",
    4: "game_token",
    5: "resolution",
    6: "target_fps",
    7: "cmd_line",
    8: "game_svr_addr",
    9: "rtc_udp_port",
    11: "game_server_wss_url",
    13: "big_isp",
    14: "game_control_channel_url",
}
_SDK_PARAM_REPEATED_FIELDS = {
    10: "additional_game_svr_addrs",
    12: "additional_game_server_wss_urls",
    15: "additional_game_control_channel_urls",
}


@dataclass
class SdkStartGameParams:
    sid: str = ""
    ca_id: str = ""
    pod_id: str = ""
    game_token: str = ""
    resolution: str = ""
    target_fps: int = 0
    cmd_line: str = ""
    game_svr_addr: str = ""
    rtc_udp_port: int = 0
    game_server_wss_url: str = ""
    big_isp: int = 0
    game_control_channel_url: str = ""
    additional_game_svr_addrs: tuple[str, ...] = ()
    additional_game_server_wss_urls: tuple[str, ...] = ()
    additional_game_control_channel_urls: tuple[str, ...] = ()

    @property
    def account_id(self) -> str:
        """从启动命令行中解析账号 ID。"""
        match = re.search(r"(?:^|\s)-aid\s+([^\s]+)", self.cmd_line)
        return match.group(1) if match else ""

    @property
    def rtc_wss_url(self) -> str:
        """返回用于 RTC 信令连接的 WebSocket 地址。"""
        if self.game_server_wss_url.endswith("/rtc-sdk"):
            return self.game_server_wss_url
        return f"{self.game_server_wss_url}/rtc-sdk"

    @property
    def candidate_ip(self) -> str:
        """从游戏服务器地址中提取候选 IP。"""
        return self.game_svr_addr.split(":", 1)[0] if self.game_svr_addr else ""


class Protocol:
    PACKET_HEADER_SIZE = 10
    WS_FRAME_HEADER_SIZE = 8

    @staticmethod
    def _varint(value: int) -> bytes:
        """编码 Protobuf varint。"""
        out = bytearray()
        while value > 0x7F:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value)
        return bytes(out)

    @classmethod
    def _key(cls, field_no: int, wire_type: int) -> bytes:
        """编码 Protobuf 字段键。"""
        return cls._varint((field_no << 3) | wire_type)

    @classmethod
    def string(cls, field_no: int, value: str) -> bytes:
        """编码字符串字段。"""
        if not value:
            return b""
        raw = value.encode("utf-8")
        return cls._key(field_no, 2) + cls._varint(len(raw)) + raw

    @classmethod
    def uint32(cls, field_no: int, value: int) -> bytes:
        """编码 uint32 字段。"""
        if not value:
            return b""
        return cls._key(field_no, 0) + cls._varint(value)

    @classmethod
    def bool(cls, field_no: int, value: bool) -> bytes:
        """编码布尔字段。"""
        if not value:
            return b""
        return cls._key(field_no, 0) + cls._varint(1)

    @classmethod
    def int64(cls, field_no: int, value: int) -> bytes:
        """编码 int64 字段。"""
        if not value:
            return b""
        return cls._key(field_no, 0) + cls._varint(value)

    @classmethod
    def double(cls, field_no: int, value: float) -> bytes:
        """编码 double 字段。"""
        if not value:
            return b""
        return cls._key(field_no, 1) + struct.pack("<d", value)

    @classmethod
    def float(cls, field_no: int, value: float) -> bytes:
        """编码 float 字段。"""
        if not value:
            return b""
        return cls._key(field_no, 5) + struct.pack("<f", value)

    @classmethod
    def message(cls, field_no: int, value: bytes, include_empty: bool = False) -> bytes:
        """编码嵌套消息字段。"""
        if not value and not include_empty:
            return b""
        return cls._key(field_no, 2) + cls._varint(len(value)) + value

    @staticmethod
    def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
        """从指定偏移读取 Protobuf varint。"""
        result = 0
        shift = 0
        while offset < len(data):
            byte = data[offset]
            offset += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result, offset
            shift += 7
        raise ValueError("unterminated varint")

    @classmethod
    def proto_fields(cls, data: bytes) -> list[tuple[int, int, object]]:
        """解析 Protobuf 字段列表。

        支持 wire type 0(varint) / 1(double) / 2(length-delimited) / 5(float)。
        鼠标用 double、触控用 float, 因此 1/5 必须能解 —— 早期版本遇到它们会抛异常,
        现在统一返回 Python 数值 (int / float / str / bytes)。
        """
        fields = []
        offset = 0
        while offset < len(data):
            tag, offset = cls._read_varint(data, offset)
            field_no = tag >> 3
            wire_type = tag & 7
            if wire_type == 0:
                value, offset = cls._read_varint(data, offset)
            elif wire_type == 1:
                if offset + 8 > len(data):
                    raise ValueError(f"truncated double for field {field_no}")
                value = struct.unpack("<d", data[offset : offset + 8])[0]
                offset += 8
            elif wire_type == 2:
                length, offset = cls._read_varint(data, offset)
                raw = data[offset : offset + length]
                offset += length
                try:
                    value = raw.decode("utf-8")
                except UnicodeDecodeError:
                    value = raw
            elif wire_type == 5:
                if offset + 4 > len(data):
                    raise ValueError(f"truncated float for field {field_no}")
                value = struct.unpack("<f", data[offset : offset + 4])[0]
                offset += 4
            else:
                raise ValueError(f"unsupported wire type {wire_type} for field {field_no}")
            fields.append((field_no, wire_type, value))
        return fields

    @classmethod
    def sdk_params(cls, sdk_param_b64: str) -> SdkStartGameParams:
        """解析 SDK 启动参数。"""
        fields = cls.proto_fields(base64.b64decode(sdk_param_b64))
        repeated: dict[str, list] = {name: [] for name in _SDK_PARAM_REPEATED_FIELDS.values()}
        values: dict[str, object] = {}
        for field_no, _wire_type, value in fields:
            if field_no in _SDK_PARAM_SCALAR_FIELDS:
                values[_SDK_PARAM_SCALAR_FIELDS[field_no]] = value
            elif field_no in _SDK_PARAM_REPEATED_FIELDS:
                repeated[_SDK_PARAM_REPEATED_FIELDS[field_no]].append(value)
        for key, items in repeated.items():
            values[key] = tuple(items)
        return SdkStartGameParams(**values)

    @classmethod
    def packet(cls, cmd_id: int, message: bytes, head: bytes = b"") -> bytes:
        """封装云游戏协议包。"""
        return b"".join([
            MAGIC_HEAD,
            struct.pack(">H", cmd_id),
            struct.pack(">H", len(head)),
            struct.pack(">I", len(message)),
            head,
            message,
            MAGIC_TAIL,
        ])

    @classmethod
    def parse_packet(cls, data: bytes) -> dict:
        """解析云游戏协议包。"""
        if len(data) < cls.PACKET_HEADER_SIZE + len(MAGIC_TAIL):
            raise ValueError("packet too short")
        if data[:2] != MAGIC_HEAD:
            raise ValueError("invalid packet magic head")
        cmd_id = struct.unpack(">H", data[2:4])[0]
        head_len = struct.unpack(">H", data[4:6])[0]
        msg_len = struct.unpack(">I", data[6:10])[0]
        msg_start = cls.PACKET_HEADER_SIZE + head_len
        msg_end = msg_start + msg_len
        tail_end = msg_end + len(MAGIC_TAIL)
        if len(data) < tail_end:
            raise ValueError("truncated packet")
        if data[msg_end:tail_end] != MAGIC_TAIL:
            raise ValueError("invalid packet magic tail")
        return {"cmd_id": cmd_id, "head": data[cls.PACKET_HEADER_SIZE:msg_start], "message": data[msg_start:msg_end]}

    @staticmethod
    def ws_frame(frame_type: int, payload: bytes | str) -> bytes:
        """封装 WebSocket 外层帧。"""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return struct.pack(">II", frame_type, len(payload)) + payload

    @classmethod
    def parse_ws_frame(cls, data: bytes) -> dict:
        """解析 WebSocket 外层帧。"""
        if len(data) < cls.WS_FRAME_HEADER_SIZE:
            raise ValueError("websocket frame too short")
        frame_type, length = struct.unpack(">II", data[:cls.WS_FRAME_HEADER_SIZE])
        end = cls.WS_FRAME_HEADER_SIZE + length
        if len(data) < end:
            raise ValueError("truncated websocket frame")
        return {"frame_type": frame_type, "length": length, "payload": data[cls.WS_FRAME_HEADER_SIZE:end]}

    @classmethod
    def proxy_frame(cls, cmd_id: int, message: bytes = b"") -> bytes:
        """生成代理协议帧。"""
        return cls.ws_frame(FRAME_PROXY, cls.packet(cmd_id, message))

    @classmethod
    def start_game_frame(cls, params: SdkStartGameParams, terminal_type: int = 10, since_start_sec: int | None = None, link_tasks_ms: int | None = None) -> bytes:
        """生成启动游戏请求帧。"""
        since_start_sec = 1 if since_start_sec is None else since_start_sec
        link_tasks_ms = 1 if link_tasks_ms is None else link_tasks_ms
        message = b"".join([
            cls.string(1, params.game_token),
            cls.string(2, params.account_id),
            cls.string(3, CG_SDK_VERSION),
            cls.uint32(4, terminal_type),
            cls.string(6, params.sid),
            cls.string(7, params.pod_id),
            cls.uint32(9, since_start_sec),
            cls.uint32(10, link_tasks_ms),
            cls.message(12, b"", include_empty=True),
        ])
        return cls.proxy_frame(CMD_START_GAME_REQ, message)

    @classmethod
    def key_frame_request(cls) -> bytes:
        """生成关键帧请求。"""
        return cls.proxy_frame(CMD_KEY_FRAME_REQ)

    @classmethod
    def keep_playing(cls, params: SdkStartGameParams) -> bytes:
        """生成继续游玩保活帧。"""
        return cls.proxy_frame(CMD_RTC_KEEP_PLAYING, cls.string(1, params.game_token))

    @classmethod
    def stop_game(cls, params: SdkStartGameParams) -> bytes:
        return cls.proxy_frame(CMD_STOP_GAME_REQ, cls.string(1, params.game_token))

    @classmethod
    def resume(cls, params: SdkStartGameParams) -> bytes:
        """生成恢复游戏请求帧。"""
        return cls.proxy_frame(CMD_RTC_RESUME_REQ, cls.string(1, params.game_token))

    @classmethod
    def keepalive_config(cls, kickout_duration: int = 300000, tips_duration: int = 20000) -> bytes:
        """生成保活配置帧。"""
        return cls.proxy_frame(CMD_RTC_KEEPALIVE_CFG, cls.uint32(1, kickout_duration) + cls.uint32(2, tips_duration))

    @classmethod
    def graphics_mode(cls, mode: int = 0) -> bytes:
        """生成画质模式配置帧。"""
        return cls.proxy_frame(CMD_RTC_SET_GRAPHICS_MODE, cls.uint32(1, mode))

    @classmethod
    def bitrate_multiplier(cls, multiplier: float = 1.875) -> bytes:
        """生成码率倍率配置帧。"""
        return cls.proxy_frame(CMD_RTC_SET_BITRATE_MULTIPLIER, cls.float(1, multiplier))

    @classmethod
    def device_info(
            cls,
            params: SdkStartGameParams,
            transport_protocol: str = "udp",
            terminal_type: int = 10,
            core_config: CoreConfig | dict | None = None,
    ) -> bytes:
        """生成设备信息上报帧。"""
        config = normalize_core_config(core_config)
        device = config.device_profile
        message = b"".join([
            cls.string(1, params.game_token),
            cls.string(2, CG_SDK_VERSION),
            cls.uint32(3, terminal_type),
            cls.string(4, device.device_id),
            cls.string(6, device.device_name),
            cls.string(8, device.gpu_model),
            cls.string(9, device.os),
            cls.uint32(11, device.screen_width),
            cls.uint32(12, device.screen_height),
            cls.string(17, params.candidate_ip),
            cls.string(20, transport_protocol),
        ])
        return cls.proxy_frame(CMD_SDK_DEVICE_INFO, message)

    @classmethod
    def encode_rmq(cls, msg_id: int, msg_type: int, seq_id: int, seq_cnt: int, total_len: int, data_len: int, data: bytes, tag: str = "") -> bytes:
        """编码可靠消息队列数据。"""
        return b"".join([
            cls.uint32(1, msg_id), cls.uint32(2, msg_type), cls.uint32(3, seq_id),
            cls.uint32(4, seq_cnt), cls.uint32(5, total_len), cls.uint32(6, data_len),
            cls.message(7, data), cls.string(8, tag),
        ])

    @classmethod
    def parse_rmq(cls, data: bytes) -> dict:
        """解析可靠消息队列数据。"""
        out = {"msg_id": 0, "msg_type": 0, "seq_id": 0, "seq_cnt": 0, "total_len": 0, "data_len": 0, "data": b"", "tag": ""}
        for field_no, _wire_type, value in cls.proto_fields(data):
            if field_no == 1: out["msg_id"] = value
            elif field_no == 2: out["msg_type"] = value
            elif field_no == 3: out["seq_id"] = value
            elif field_no == 4: out["seq_cnt"] = value
            elif field_no == 5: out["total_len"] = value
            elif field_no == 6: out["data_len"] = value
            elif field_no == 7: out["data"] = value if isinstance(value, bytes) else value.encode("utf-8")
            elif field_no == 8: out["tag"] = value
        return out

    @classmethod
    def rmq_ack_frame(cls, ack_msg_id: int, outgoing_msg_id: int = 1) -> bytes:
        """生成可靠消息队列 ACK 帧。"""
        ack = cls.uint32(1, ack_msg_id)
        rmq = cls.encode_rmq(outgoing_msg_id, 4, 1, 1, len(ack), len(ack), ack)
        return cls.proxy_frame(CMD_RELIABLE_MESSAGE_QUEUE_DATA, rmq)

    @classmethod
    def sdk_message(cls, name: str, data: bytes) -> bytes:
        """封装 SDK 消息体。"""
        return cls.string(1, name) + cls.message(2, data)

    @classmethod
    def parse_sdk_message(cls, data: bytes) -> dict:
        """解析SDK 消息体。"""
        out = {"name": "", "data": b""}
        for field_no, _wire_type, value in cls.proto_fields(data):
            if field_no == 1: out["name"] = value
            elif field_no == 2: out["data"] = value if isinstance(value, bytes) else value.encode("utf-8")
        return out

    @staticmethod
    def _aes(data: bytes, decrypt: bool) -> bytes:
        """执行 SDK game-data AES 加解密。"""
        cipher = Cipher(algorithms.AES(SDK_GAME_DATA_AES_KEY.encode("utf-8")), modes.ECB())
        if decrypt:
            decryptor = cipher.decryptor()
            padded = decryptor.update(data) + decryptor.finalize()
            unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    @classmethod
    def decrypt_sdk_json(cls, encrypted_base64: bytes) -> dict:
        """解密 SDK game-data JSON。"""
        plaintext = cls._aes(base64.b64decode(encrypted_base64), decrypt=True)
        return json.loads(plaintext.decode("utf-8"))

    @classmethod
    def encrypt_sdk_json(cls, message: dict) -> bytes:
        """加密 SDK game-data JSON。"""
        plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(cls._aes(plaintext, decrypt=False))

    @classmethod
    def sdk_game_data_frame(cls, message: dict, outgoing_msg_id: int = 1, name: str = "SDK") -> bytes:
        """封装 SDK game-data 回包帧。"""
        sdk_message = cls.sdk_message(name, cls.encrypt_sdk_json(message))
        rmq = cls.encode_rmq(outgoing_msg_id, 1, 1, 1, len(sdk_message), len(sdk_message), sdk_message)
        return cls.proxy_frame(CMD_RELIABLE_MESSAGE_QUEUE_DATA, rmq)

    @classmethod
    def heartbeat_packet(cls, heartbeat_id: int, send_timestamp_ms: int | None = None) -> bytes:
        """生成心跳包。"""
        if send_timestamp_ms is None:
            send_timestamp_ms = int(time.time() * 1000)
        return cls.packet(CMD_HEARTBEAT, cls.uint32(1, heartbeat_id) + cls.int64(2, send_timestamp_ms))

    @classmethod
    def input_mouse(cls, data_type: int, x: float = 0, y: float = 0, dx: float = 0, dy: float = 0, wheel_delta: float = 0) -> bytes:
        """生成鼠标输入包。"""
        mouse = b"".join([
            cls.double(1, x), cls.double(2, y), cls.double(3, wheel_delta),
            cls.double(4, dx), cls.double(5, dy),
        ])
        data = cls.uint32(1, data_type) + cls.message(3, mouse)
        return cls.packet(CMD_RTC_DATA_CHANNEL, data)

    @classmethod
    def input_keyboard(cls, data_type: int, key_code: int, capslock_toggled: bool = False, numlock_toggled: bool = False) -> bytes:
        """生成键盘输入包。"""
        keyboard = cls.uint32(1, key_code) + cls.bool(4, capslock_toggled) + cls.bool(5, numlock_toggled)
        data = cls.uint32(1, data_type) + cls.message(2, keyboard)
        return cls.packet(CMD_RTC_DATA_CHANNEL, data)

    @classmethod
    def touch_event(cls, event: dict) -> bytes:
        """生成单个触点 (``proto.common.RtcTouchEvent``)。

        字段号 / 线格式照抄 SDK ``cg-sdk.js`` 的
        ``RtcTouchEvent.serializeBinaryToWriter``::

            #1 fingerId          uint32
            #2 actionType        uint32   (0=Down 1=Up 2=Move)
            #3 pointX            float    ← 注意是 float32, 不是鼠标用的 double
            #4 pointY            float
            #5 pressSizeX        float
            #6 pressSizeY        float
            #7 isPrimary         bool
            #8 rttTraceSendTime  int64

        参数:
            event: ``{"finger_id","action","x","y","press_x","press_y","is_primary",
                "rtt_trace_send_time"}``；``action`` 可以是数字或 ``down``/``up``/``move``。
        """
        action = event.get("action", TOUCH_ACTION_DOWN)
        if isinstance(action, str):
            action = TOUCH_ACTION_TYPES.get(action.lower(), TOUCH_ACTION_DOWN)
        return b"".join([
            cls.uint32(1, int(event.get("finger_id", 0))),
            cls.uint32(2, int(action)),
            cls.float(3, float(event.get("x", 0.0))),
            cls.float(4, float(event.get("y", 0.0))),
            cls.float(5, float(event.get("press_x", 0.0))),
            cls.float(6, float(event.get("press_y", 0.0))),
            cls.bool(7, bool(event.get("is_primary", False))),
            cls.int64(8, int(event.get("rtt_trace_send_time", 0))),
        ])

    @classmethod
    def input_touch(cls, events: list[dict]) -> bytes:
        """生成触控输入包 (``RtcDataChannel.touchList``, 一次 1~10 个触点)。

        外层: ``#1 type = 21 (TOUCH_EVENT_LIST)``、``#13 touchList``；
        内层 ``RtcTouchEventList``: ``#1 event_list`` 重复 ``RtcTouchEvent``。
        """
        if not events:
            raise ValueError("touch 事件列表不能为空")
        if len(events) > MAX_TOUCH_POINTS:
            raise ValueError(f"一次最多 {MAX_TOUCH_POINTS} 个触点, 收到 {len(events)} 个")
        touch_list = b"".join(
            cls.message(1, cls.touch_event(event), include_empty=True) for event in events
        )
        data = cls.uint32(1, DATA_TYPE_TOUCH_EVENT_LIST) + cls.message(13, touch_list, include_empty=True)
        return cls.packet(CMD_RTC_DATA_CHANNEL, data)

    @classmethod
    def touch_events_from_action(
        cls, action: dict, normalize_size: tuple[float, float] = (0.0, 0.0)
    ) -> list[dict]:
        """把输入动作展开成触点列表（坐标按 0.0-1.0 归一化）。

        支持两种写法::

            {"type": "touch_down", "x": 0.5, "y": 0.5, "finger_id": 0, "is_primary": true}
            {"type": "touch", "events": [{"action": "down", "x": .5, "y": .5},
                                         {"action": "down", "x": .3, "y": .7, "finger_id": 1}]}
        """
        if action.get("type") == "touch":
            events: list[dict] = []
            for item in action.get("events") or []:
                event = dict(item)
                x, y = cls.normalized_xy(event, normalize_size)
                event["x"], event["y"] = x, y
                events.append(event)
            return events
        x, y = cls.normalized_xy(action, normalize_size)
        event = {key: value for key, value in action.items() if key != "type"}
        event["action"] = str(action.get("type", "")).split("_", 1)[-1] or "down"
        event["x"], event["y"] = x, y
        return [event]

    @classmethod
    def input_ime(cls, text: str) -> bytes:
        """生成 IME 文本输入包。"""
        ime = cls.string(1, text)
        data = cls.uint32(1, DATA_TYPE_IME_INPUT) + cls.message(5, ime)
        return cls.packet(CMD_RTC_DATA_CHANNEL, data)

    @classmethod
    def input_clipboard(cls, text: str) -> bytes:
        """生成剪贴板文本输入包。"""
        clipboard = cls.string(1, text)
        data = cls.uint32(1, DATA_TYPE_IME_CLIPBOARD) + cls.message(7, clipboard)
        return cls.packet(CMD_RTC_DATA_CHANNEL, data)

    @staticmethod
    def normalized_xy(action: dict, normalize_size: tuple[float, float]) -> tuple[float, float]:
        """返回 0.0-1.0 鼠标坐标；像素坐标会按给定宽高归一化。"""
        x = float(action.get("x", 0))
        y = float(action.get("y", 0))
        width, height = normalize_size
        if width > 0 and x > 1: x /= width
        if height > 0 and y > 1: y /= height
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    @classmethod
    def input_from_action(cls, action: dict, normalize_size: tuple[float, float] = (0.0, 0.0)) -> bytes | None:
        """生成输入动作对应的数据包。"""
        action_type = action.get("type")
        if action_type == "move":
            x, y = cls.normalized_xy(action, normalize_size)
            return cls.input_mouse(DATA_TYPE_MOUSE_MOVE, x=x, y=y, dx=float(action.get("dx", 0)), dy=float(action.get("dy", 0)))
        if action_type in ("down", "up"):
            button = action.get("button", "left")
            if button not in MOUSE_BUTTON_TYPES:
                raise ValueError(f"unsupported mouse button: {button}")
            down_type, up_type = MOUSE_BUTTON_TYPES[button]
            x, y = cls.normalized_xy(action, normalize_size)
            return cls.input_mouse(down_type if action_type == "down" else up_type, x=x, y=y)
        if action_type == "scroll":
            return cls.input_mouse(DATA_TYPE_MOUSE_ZDELTA, wheel_delta=float(action.get("delta", 0)))
        if action_type == "key_down":
            return cls.input_keyboard(DATA_TYPE_KEYBOARD_DOWN, int(action.get("key_code", 0)), bool(action.get("capslock", False)), bool(action.get("numlock", False)))
        if action_type == "key_up":
            return cls.input_keyboard(DATA_TYPE_KEYBOARD_UP, int(action.get("key_code", 0)), bool(action.get("capslock", False)), bool(action.get("numlock", False)))
        if action_type == "ime":
            text = str(action.get("text") or "")
            return cls.input_ime(text) if text else None
        if action_type == "clipboard":
            text = str(action.get("text") or "")
            return cls.input_clipboard(text) if text else None
        if action_type in ("touch", "touch_down", "touch_move", "touch_up"):
            events = cls.touch_events_from_action(action, normalize_size)
            return cls.input_touch(events) if events else None
        return None

    @staticmethod
    def rewrite_candidate(candidate_json: dict, params: SdkStartGameParams) -> dict:
        """用调度结果重写 ICE candidate 地址。"""
        candidate = candidate_from_sdp(candidate_json["candidate"])
        if candidate.protocol in ("udp", "tcp") and params.candidate_ip and params.rtc_udp_port:
            candidate.ip = params.candidate_ip
            candidate.port = params.rtc_udp_port
        candidate.sdpMid = candidate_json.get("sdp-mid")
        candidate.sdpMLineIndex = candidate_json.get("sdp-mline-index")
        candidate_json = dict(candidate_json)
        candidate_json["candidate"] = candidate_to_sdp(candidate)
        return candidate_json


__all__ = ["Protocol", "SdkStartGameParams"]
