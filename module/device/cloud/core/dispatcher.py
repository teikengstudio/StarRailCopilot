from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .auth import WEB_VERIFY_URL, parse_cookie_header
from .config import CoreConfig
from .log import emit_log_callback, get_logger

# ---------------------------------------------------------------------------
# 服务端点
# ---------------------------------------------------------------------------
# WEB_VERIFY_URL 由 ``core.auth`` 提供, 这里仅做共享 import 以保持单一来源;
# Dispatcher 调用 webVerifyForGame 是为了换 channel_token, 与 Authenticator
# 的"探活"语义不同, 因此不会引入 Authenticator 类。
BASE_URL = "https://cg-hkrpg-api.mihoyo.com/hkrpg_cn/cg"
WEB_LOGIN_URL = "https://hkrpg-sdk.mihoyo.com/hkrpg_cn/combo/granter/login/webLogin"
WALLET_GET_PATH = "/wallet/wallet/get"

# ---------------------------------------------------------------------------
# 业务标识（线上所有客户端共享，无需随机化）
# ---------------------------------------------------------------------------
BIZ_KEY = "hkrpg_cn"
APP_KEY = "1fbf60a3582bf2ed05810954ee2349b9"
COMBO_APP_KEY = "4650f3a396d34d576c3d65df26415394"

# ---------------------------------------------------------------------------
# 设备指纹 — 调度阶段上报的硬件/平台参数
# ---------------------------------------------------------------------------
# 已迁移到 core.config.DEFAULT_CORE_CONFIG["device_profile"] / client_profile.json。
# FALLBACK_DEVICE_ID = "88631514-ee79-4cd7-820d-48c70d9a222d"
# DEVICE_OS = "Linux undefined"
# DEVICE_MODEL = "Unknown"
# DEVICE_PROCESSOR_COUNT = 16
# DEVICE_PROCESSOR_FREQ = 16
# DEVICE_PROCESSOR_TYPE = "Unknown"
# DEVICE_MEMORY_SIZE = 8
# DEVICE_SOC = "Unknown"

# ---------------------------------------------------------------------------
# 调度请求头 — 客户端特征字段
# ---------------------------------------------------------------------------
# 已迁移到 core.config.DEFAULT_CORE_CONFIG / client_profile.json 的客户端画像字段。
# DISPATCH_APP_VERSION = "4.3.0"
# DISPATCH_DEVICE_NAME = "Unknown"
# DISPATCH_DEVICE_MODEL = "Unknown"
# DISPATCH_SYS_VERSION = "Linux undefined"
# SEC_CH_UA = '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
# USER_AGENT = (
#     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
#     " (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
# )
# 下面这几个是"桌面(键鼠)"画像的兜底默认值；运行时优先取
# core_config.platform_profile（见 core/config.py: PLATFORM_PRESETS），
# 切到 touch 模式时 client_type=18 / cps=touch_mihoyo / sec-ch-ua-mobile=?1。
DISPATCH_CLIENT_TYPE = "19"
DISPATCH_VENDOR_ID = "2"
DISPATCH_CPS = "keyboard_mihoyo"
DISPATCH_LANGUAGE = "zh-cn"

# Sec-CH-UA 浏览器特征
SEC_CH_UA_PLATFORM = '"Linux"'
SEC_CH_UA_MOBILE = "?0"

# ---------------------------------------------------------------------------
# 网页登录请求头 — 设备/浏览器特征字段
# ---------------------------------------------------------------------------
WEB_CLIENT_TYPE = "25"
# 已迁移到 core.config.DEFAULT_CORE_CONFIG["browser_profile"] / client_profile.json。
# WEB_DEVICE_NAME = "Chrome"
# WEB_DEVICE_MODEL = "Chrome%20145.0.0.0"
# WEB_DEVICE_OS = "Linux%2064-bit"
WEB_LANGUAGE = "zh-cn"

# Web 验证用 App ID
WEB_VERIFY_APP_ID = "c90mr1bwo2rk"
WEB_SDK_VERSION = "2.50.1"
WEB_MDK_VERSION = "2.49.0"
WEB_APP_ID = "8"
WEB_CHANNEL_ID = "1"

# ---------------------------------------------------------------------------
# 排队 / 画质参数
# ---------------------------------------------------------------------------
# 已迁移到 core.config.DEFAULT_CORE_CONFIG["session_profile"] / client_profile.json。
# DEFAULT_RESOLUTION = "1920x1080"
# DEFAULT_FPS = 30
# DEFAULT_BIT_RATE = 10240000
DEFAULT_CODEC_TYPE = 1
DEFAULT_ENV = "2"
# 已迁移到 core.config.DEFAULT_CORE_CONFIG["device_profile"] / client_profile.json。
# DEFAULT_DPI = 96
DEFAULT_LANG = "zh-CN"
DEFAULT_NET_STATE = 4
QUEUE_TYPE_NORMAL = ""
QUEUE_TYPE_COIN = "coin"

logger = get_logger("dispatcher")


@dataclass
class DispatchConfig:
    queue_type: str = ""
    node: str = ""
    # None = 跟随 core_config.platform_profile（桌面=7 / 手机触控=8）
    speed_client_type: int | None = None
    cookie: str | None = None
    combo_token: str = ""
    core_config: CoreConfig = field(default_factory=CoreConfig)
    root_dir: Path = Path(__file__).resolve().parent.parent


class Dispatcher:

    def __init__(self, config: DispatchConfig):
        """初始化实例并保存运行所需的状态。"""
        self.config = config
        self.session = requests.Session()
        self._runtime_combo_token = ""
        self.last_account_sync: dict | None = None
        # _cookie_value 用; 仅在 cookie 文本变化时重新解析
        self._cookie_cache_source: str | None = None
        self._cookie_cache: dict[str, str] = {}

    @property
    def cookie(self) -> str | None:
        """返回显式配置中的 Cookie；None 表示调用方没有提供。"""
        return self.config.cookie

    # ------------------------------------------------------------------
    # 平台画像（桌面键鼠 / 手机触控）
    # ------------------------------------------------------------------
    @property
    def platform_profile(self):
        """当前平台画像：client_type / cps / sec-ch-ua / speed_client_type。"""
        return self.config.core_config.platform_profile

    @property
    def speed_client_type(self) -> int:
        """上报的 speed_client_type：显式配置优先，否则跟随平台画像。"""
        if self.config.speed_client_type is not None:
            return int(self.config.speed_client_type)
        return int(getattr(self.platform_profile, "speed_client_type", 7))

    @property
    def is_touch_mode(self) -> bool:
        """是否处于手机(触控)模式。"""
        return str(getattr(self.platform_profile, "mode", "desktop")) == "touch"

    @property
    def combo_token(self) -> str:
        """返回本次账号同步生成或配置中预置的 combo token。"""
        return self._runtime_combo_token or self.config.combo_token

    def _cookie_value(self, name: str, default: str = "") -> str:
        """从 Cookie 字符串中读取指定键值, 内部缓存解析结果。"""
        cookie = self.cookie or ""
        if cookie != self._cookie_cache_source:
            self._cookie_cache_source = cookie
            self._cookie_cache = parse_cookie_header(cookie)
        return self._cookie_cache.get(name, default)

    @property
    def _configured_device_id(self) -> str:
        """返回配置中的默认设备 ID。"""
        return self.config.core_config.device_profile.device_id

    @property
    def _device_id(self) -> str:
        """返回 Cookie 中的设备 ID，缺失时使用配置默认值。"""
        return self._cookie_value("_MHYUUID", self._configured_device_id)

    @staticmethod
    def _combo_parts(value: str) -> dict[str, str]:
        """解析 combo token 中的键值对。"""
        out = {}
        for part in value.split(";"):
            if "=" in part:
                key, val = part.strip().split("=", 1)
                out[key] = val
        return out

    @staticmethod
    def _json(value) -> str:
        """生成紧凑 JSON 文本。"""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _line(self, line_callback, message: str, level: int = logging.INFO) -> None:
        """输出调度日志并转发回调。"""
        logger.log(level, message)
        emit_log_callback(line_callback, message, level)

    def _sleep(self, seconds: int, stop_event=None) -> None:
        """可被停止事件打断地等待一段时间。"""
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("dispatch stopped")
            time.sleep(min(0.25, deadline - time.monotonic()))

    def _require_credentials(self) -> None:
        """确保调度所需凭据已配置。"""
        if self.cookie is None:
            raise RuntimeError("missing Cookie: caller did not provide cookie")

    def _post(self, url: str, body: dict, headers: dict, retries: int = 0) -> dict:
        """发送 JSON POST 请求并处理重试。"""
        last_error = None
        for attempt in range(retries + 1):
            try:
                response = self.session.post(url, headers=headers, data=self._json(body).encode(), timeout=20)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError(f"{url} returned non-JSON HTTP {response.status_code}: {response.text[:200]}") from exc
                if response.status_code < 200 or response.status_code >= 300:
                    raise RuntimeError(f"{url} HTTP {response.status_code}: {self._json(data)[:500]}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt == retries:
                    break
                time.sleep(1.5 * (attempt + 1))
        raise last_error

    def _get(self, url: str, headers: dict, params: dict | None = None, retries: int = 0) -> dict:
        """发送 GET 请求并处理重试。"""
        last_error = None
        for attempt in range(retries + 1):
            try:
                response = self.session.get(url, headers=headers, params=params or {}, timeout=20)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError(f"{url} returned non-JSON HTTP {response.status_code}: {response.text[:200]}") from exc
                if response.status_code < 200 or response.status_code >= 300:
                    raise RuntimeError(f"{url} HTTP {response.status_code}: {self._json(data)[:500]}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt == retries:
                    break
                time.sleep(1.5 * (attempt + 1))
        raise last_error

    def _dispatch_post(self, path: str, body: dict, headers: dict, retries: int = 0) -> dict:
        """向调度 API 发送 JSON 请求。"""
        return self._post(f"{BASE_URL}{path}", body, headers, retries=retries)

    def _dispatch_get(self, path: str, headers: dict, params: dict | None = None, retries: int = 0) -> dict:
        """向云游戏 API 发送 GET 请求。"""
        return self._get(f"{BASE_URL}{path}", headers, params=params, retries=retries)

    @staticmethod
    def _assert_ok(name: str, response: dict) -> None:
        """校验接口响应的 retcode。"""
        if response.get("retcode") == 0:
            return
        raise RuntimeError(f"{name} failed: retcode={response.get('retcode')}, message={response.get('message')}")

    def _dispatch_headers(self) -> dict:
        """构造调度 API 请求头。"""
        device = self.config.core_config.device_profile
        browser = self.config.core_config.browser_profile
        platform = self.platform_profile
        headers = {
            "x-rpc-cg_game_biz": BIZ_KEY,
            "x-rpc-op_biz": "clgm_hkrpg-cn",
            "x-rpc-app_id": WEB_APP_ID,
            "x-rpc-channel": "mihoyo",
            "x-rpc-device_id": self._device_id,
            "x-rpc-device_name": device.device_name,
            "x-rpc-language": DISPATCH_LANGUAGE,
            "x-rpc-app_version": browser.app_version,
            "x-rpc-client_type": str(platform.client_type),
            "x-rpc-device_model": device.model,
            "x-rpc-cps": platform.cps,
            "x-rpc-sys_version": device.sys_version,
            "x-rpc-vendor_id": DISPATCH_VENDOR_ID,
            "sec-ch-ua-platform": platform.sec_ch_ua_platform,
            "sec-ch-ua": browser.sec_ch_ua,
            "sec-ch-ua-mobile": platform.sec_ch_ua_mobile,
            "user-agent": browser.user_agent,
            "origin": "https://sr.mihoyo.com",
            "referer": "https://sr.mihoyo.com/",
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
        }
        if self.cookie is not None:
            headers["cookie"] = self.cookie
        if self.combo_token:
            headers["x-rpc-combo_token"] = self.combo_token
        return headers

    def _web_headers(self) -> dict:
        """构造网页登录相关请求头。"""
        browser = self.config.core_config.browser_profile
        platform = self.platform_profile
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://sr.mihoyo.com",
            "referer": "https://sr.mihoyo.com/",
            "user-agent": browser.user_agent,
            "sec-ch-ua-platform": platform.sec_ch_ua_platform,
            "sec-ch-ua": browser.sec_ch_ua,
            "sec-ch-ua-mobile": platform.sec_ch_ua_mobile,
            "x-rpc-client_type": str(platform.web_client_type),
            "x-rpc-device_id": self._device_id,
            "x-rpc-device_fp": self._cookie_value("DEVICEFP"),
            "x-rpc-device_name": browser.web_device_name,
            "x-rpc-device_model": browser.web_device_model,
            "x-rpc-device_os": browser.web_device_os,
            "x-rpc-game_biz": BIZ_KEY,
            "x-rpc-language": WEB_LANGUAGE,
        }
        if self.cookie is not None:
            headers["cookie"] = self.cookie
        return headers

    def _sign(self, payload: dict) -> str:
        """计算调度接口签名。"""
        signing_text = "&".join(f"{key}={payload[key]}" for key in sorted(payload))
        return hmac.new(APP_KEY.encode(), signing_text.encode(), hashlib.sha256).hexdigest()

    def _combo_signature(self, payload: dict[str, str]) -> str:
        """计算 x-rpc-combo_token 中的 si 字段。"""
        signing_text = "&".join(f"{key}={payload[key]}" for key in sorted(payload))
        return hmac.new(COMBO_APP_KEY.encode(), signing_text.encode(), hashlib.sha256).hexdigest()

    def _build_combo_token(self, login_data: dict) -> str:
        """用 webLogin 返回值生成本次请求使用的 x-rpc-combo_token。"""
        payload = {
            "app_id": str(login_data.get("app_id") or WEB_APP_ID),
            "channel_id": str(login_data.get("channel_id") or WEB_CHANNEL_ID),
            "open_id": str(login_data.get("open_id") or self._cookie_value("account_id_v2") or self._cookie_value("account_id")),
            "combo_token": str(login_data.get("combo_token") or ""),
        }
        missing = [key for key, value in payload.items() if not value]
        if missing:
            raise RuntimeError(f"webLogin did not return required combo fields: {', '.join(missing)}")
        combo_sign = self._combo_signature(payload)
        return ";".join([
            f"ai={payload['app_id']}",
            f"ci={payload['channel_id']}",
            f"oi={payload['open_id']}",
            f"ct={payload['combo_token']}",
            f"si={combo_sign}",
            f"bi={BIZ_KEY}",
        ])

    def _signed(self, payload: dict) -> dict:
        """给请求体附加签名字段。"""
        return {"sign": self._sign(payload), **payload}

    def _node_payload(self, node: dict) -> dict:
        """构造节点校验请求体。"""
        sign_base = {"biz_key": BIZ_KEY, "node_id": node["node_id"]}
        if node.get("region_ids"):
            sign_base["regions"] = self._json(node["region_ids"])
        return {"sign": self._sign(sign_base), **sign_base, "regions": node.get("region_ids") or []}

    def _ticket_payload(self, ticket: str) -> dict:
        """构造排队 ticket 请求体。"""
        return self._signed({"biz_key": BIZ_KEY, "ticket": ticket})

    @staticmethod
    def _wallet_summary(wallet: dict | None) -> dict:
        """提取钱包中与游戏时长相关的字段。"""
        wallet = wallet or {}
        coin = wallet.get("coin") or {}
        free_time = wallet.get("free_time") or {}
        play_card = wallet.get("play_card") or {}
        exchange = int(coin.get("exchange") or 10)
        coin_num = int(coin.get("coin_num") or 0)
        return {
            "coin_num": coin_num,
            "coin_exchange": exchange,
            "coin_minutes": coin_num // exchange if exchange > 0 else 0,
            "free_time_minutes": int(free_time.get("free_time") or 0),
            "play_card_remaining_sec": int(play_card.get("remaining_sec") or 0),
            "cost_method": wallet.get("cost_method"),
            "status": wallet.get("status"),
        }

    @staticmethod
    def _queue_summary(queue: dict | None) -> dict | None:
        """提取队列预计时长与人数。"""
        if not queue:
            return None
        return {
            "queue_type": queue.get("queue_type"),
            "queue_len": queue.get("queue_len"),
            "branch_queue_len": queue.get("branch_queue_len"),
            "queue_length": queue.get("queue_length"),
            "queue_rank": queue.get("queue_rank"),
            "waiting_time_min": queue.get("waiting_time_min"),
            "query_interval": queue.get("query_interval"),
            "raw": queue,
        }

    def _sdk_login(self, channel_token: str, web_login_data: dict | None = None) -> dict:
        """组装 SDK 登录响应数据。"""
        web_login_data = web_login_data or {}
        web_data = {}
        if isinstance(web_login_data.get("data"), str):
            try:
                web_data = json.loads(web_login_data["data"])
            except json.JSONDecodeError:
                web_data = {}
        open_id = web_login_data.get("open_id") or self._cookie_value("account_id_v2") or self._cookie_value("account_id")
        return {
            "ret": 0,
            "msg": "成功",
            "data": {
                "device_id": self._device_id,
                "app_id": int(web_login_data.get("app_id") or WEB_APP_ID),
                "channel_id": int(web_login_data.get("channel_id") or WEB_CHANNEL_ID),
                "channel_token": channel_token,
                "combo_id": str(web_login_data.get("combo_id") or "0"),
                "open_id": str(open_id),
                "combo_token": web_login_data.get("combo_token") or "",
                "account_type": int(web_login_data.get("account_type") or WEB_CHANNEL_ID),
                "guest": bool(web_data.get("guest", False)),
            },
        }

    def _sync_web_account(self) -> dict:
        """同步对应的远端状态。"""
        verify_headers = self._web_headers()
        verify_headers.update({
            "x-rpc-app_id": WEB_VERIFY_APP_ID,
            "x-rpc-app_version": "",
            "x-rpc-lifecycle_id": "",
            "x-rpc-mi_referrer": "https://sr.mihoyo.com/cloud/#/",
            "x-rpc-sdk_version": WEB_SDK_VERSION,
        })
        verify = self._post(WEB_VERIFY_URL, {}, verify_headers, retries=1)
        self._assert_ok("webVerifyForGame", verify)
        channel_token = ((verify.get("data") or {}).get("token") or {}).get("token") or ""
        if not channel_token:
            raise RuntimeError("webVerifyForGame did not return channel token")

        combo_headers = self._web_headers()
        combo_headers.update({"x-rpc-app_id": WEB_APP_ID, "x-rpc-channel_id": WEB_CHANNEL_ID, "x-rpc-mdk_version": WEB_MDK_VERSION})
        web_login = self._post(WEB_LOGIN_URL, {"app_id": int(WEB_APP_ID), "channel_id": int(WEB_CHANNEL_ID)}, combo_headers, retries=1)
        self._assert_ok("webLogin", web_login)
        web_login_data = web_login.get("data") or {}
        self._runtime_combo_token = self._build_combo_token(web_login_data)
        # Official WebUI initializes the cloud account before reading time or dispatching.
        cloud_login = self._dispatch_post("/gamer/api/login", {}, self._dispatch_headers())
        self._assert_ok("cloudLogin", cloud_login)
        sdk_login = self._sdk_login(channel_token, web_login_data)
        self.last_account_sync = {
            "sdk_login": sdk_login,
            "channel_token": channel_token,
            "combo_token": self._runtime_combo_token,
            "channel_token_len": len(channel_token),
            "combo_token_len": len(self._runtime_combo_token),
            "raw_combo_token_len": len(web_login_data.get("combo_token") or ""),
            "open_id": sdk_login["data"]["open_id"],
        }
        return self.last_account_sync

    def init(self) -> dict:
        """初始化调度器账号态，必须在 run/wallet/queue 前调用。"""
        self._require_credentials()
        return self._sync_web_account()

    def _require_initialized(self) -> dict:
        """确保调用方已经显式完成 init。"""
        if not self.last_account_sync:
            raise RuntimeError("Dispatcher.init() must be called before this operation")
        return self.last_account_sync

    def sync_account(self) -> dict:
        """只同步账号凭据，不执行调度。"""
        try:
            return self.init()
        finally:
            self.session.close()

    def close(self) -> None:
        """关闭调度器持有的 HTTP 会话。"""
        self.session.close()

    def wallet_info(self, *, cost_method: str | None = None, get_type: str | None = None) -> dict:
        """查询账号当前可用的星云币时长、免费时长和畅玩卡状态。"""
        self._require_initialized()
        params = {}
        if cost_method:
            params["cost_method"] = cost_method
        if get_type:
            params["get_type"] = get_type
        wallet = self._dispatch_get(WALLET_GET_PATH, self._dispatch_headers(), params=params, retries=1)
        self._assert_ok("walletGet", wallet)
        data = wallet.get("data") or {}
        return {"summary": self._wallet_summary(data), "data": data}

    def queue_estimate(self, line_callback=None, status_callback=None) -> dict:
        """在正式 dispatch 前查询普通队列和星云币优先队列的预估信息。"""
        self._require_initialized()
        emit_log_callback(status_callback, "queue estimate started", logging.DEBUG)
        headers = self._dispatch_headers()
        status = self._dispatch_post("/dispatcher/api/statusCheck", {"biz_key": BIZ_KEY}, headers)
        self._assert_ok("statusCheck", status)

        nodes_body = {"biz_key": BIZ_KEY, "node": self.config.node, "speed_client_type": self.speed_client_type}
        nodes_res = self._dispatch_post("/dispatcher/api/getNodesInfo", nodes_body, headers)
        self._assert_ok("getNodesInfo", nodes_res)
        node = self._selected_node((nodes_res.get("data") or {}).get("nodes") or [])
        self._line(line_callback, f"estimate selected node: {node.get('node_id')} {node.get('node_name') or ''}", logging.DEBUG)

        pre_dispatch = self._dispatch_post("/dispatcher/api/preDispatchVerify", self._node_payload(node), headers)
        self._assert_ok("preDispatchVerify", pre_dispatch)
        data = pre_dispatch.get("data") or {}
        return {
            "node": {
                "node_id": node.get("node_id"),
                "node_name": node.get("node_name"),
                "net_state": node.get("net_state"),
                "queue_state": node.get("queue_state"),
                "queue_value": node.get("queue_value"),
            },
            "normal": self._queue_summary(data.get("queue_info")),
            "coin": self._queue_summary(data.get("prior_queue_info")),
            "raw": data,
        }

    @property
    def _device_info(self) -> str:
        """生成调度上报用设备信息。"""
        account_id = self._cookie_value("account_id_v2") or self._cookie_value("account_id")
        device_id = self._device_id
        device = self.config.core_config.device_profile
        info = {
            "operationSystem": device.os,
            "deviceModel": device.model,
            "processorCount": device.cpu_cores,
            "processorFrequency": device.cpu_freq,
            "processorType": device.cpu_type,
            "systemMemorySize": device.memory_gb,
            "DeviceSoC": device.soc,
            "serial_number": f"{device_id}_{account_id}" if account_id else device_id,
        }
        return base64.b64encode(json.dumps(info, separators=(",", ":")).encode()).decode()

    @property
    def _user_data(self) -> str:
        """生成调度用用户数据 JSON。"""
        device = self.config.core_config.device_profile
        return self._json({
            "dpi": device.dpi,
            "w": device.screen_width,
            "h": device.screen_height,
            "lang": DEFAULT_LANG,
            "di": self._device_info,
        })

    def _paas_dispatch_payload(self, node: dict) -> dict:
        """构造 paasDispatch 请求体。"""
        session = self.config.core_config.session_profile
        sign_base = {
            "bit_rate": session.bit_rate,
            "biz_key": BIZ_KEY,
            "cmd_line": "",
            "user_data": self._user_data,
            "codec_type": DEFAULT_CODEC_TYPE,
            "env": DEFAULT_ENV,
            "ext_data": "",
            "fps": session.fps,
            "node_id": node["node_id"],
            "resolution": session.resolution,
        }
        if node.get("region_ids"):
            sign_base["regions"] = self._json(node["region_ids"])
        return {
            "hint": None,
            "net_state": DEFAULT_NET_STATE,
            "queue_type": self.config.queue_type,
            "sign": self._sign(sign_base),
            "using_new_cmd_line": True,
            "queue_switch": False,
            **sign_base,
            "regions": node.get("region_ids") or [],
        }

    @staticmethod
    def _selected_node(nodes: list[dict]) -> dict:
        """从节点列表中选择推荐节点。"""
        if not nodes:
            raise RuntimeError("getNodesInfo returned no nodes")
        return next((node for node in nodes if node.get("recommend")), nodes[0])

    @staticmethod
    def _masked(value: str) -> str:
        """脱敏显示敏感标识。"""
        if len(value) < 4:
            return "<missing>" if not value else "***"
        return f"{value[:2]}***{value[-2:]}"

    @staticmethod
    def _finish_summary(result: dict | None) -> dict | None:
        """提取 finish_result 的关键摘要。"""
        if not result:
            return None
        return {
            "hasSdkParam": bool(result.get("sdk_param")),
            "sdkParamLength": len(result.get("sdk_param") or ""),
            "cloudProvider": result.get("cloud_provider"),
            "regionId": result.get("region_id"),
            "nodeId": result.get("node_id"),
            "queueType": result.get("queue_type"),
            "gameId": result.get("game_id"),
        }

    def _emit_finish_result(self, result: dict, line_callback=None) -> None:
        """记录最新值并转发给回调。"""
        self._line(line_callback, "finish_result: " + self._json(self._finish_summary(result)), level=logging.DEBUG)

    def _log_account_sync(self, account_sync: dict, line_callback=None) -> None:
        """输出账号同步摘要。"""
        masked_open_id = self._masked(str(account_sync.get("open_id") or ""))
        self._line(line_callback, f"登陆成功 openId={masked_open_id}", level=logging.INFO)
        self._line(line_callback, "web account sync detail: " + self._json({
            "hasSdkLogin": bool(account_sync.get("sdk_login")),
            "channelTokenLength": account_sync.get("channel_token_len"),
            "comboTokenLength": account_sync.get("combo_token_len"),
            "openId": masked_open_id,
        }), level=logging.DEBUG)

    def _status_check(self, headers: dict, line_callback=None) -> None:
        """执行调度状态检查。"""
        status = self._dispatch_post("/dispatcher/api/statusCheck", {"biz_key": BIZ_KEY}, headers)
        self._assert_ok("statusCheck", status)
        self._line(line_callback, "statusCheck OK", level=logging.DEBUG)

    def _list_ping_servers(self, headers: dict, line_callback=None) -> None:
        """请求 ping server 列表并输出数量。"""
        body = {"biz_key": BIZ_KEY, "ext_data": self._json({"platform": self.platform_profile.ext_data_platform})}
        ping_servers = self._dispatch_post("/dispatcher/api/listPingServer", body, headers)
        self._assert_ok("listPingServer", ping_servers)
        ping_count = len(((ping_servers.get("data") or {}).get("ping_svr")) or [])
        self._line(line_callback, f"listPingServer OK: {ping_count} ping servers", level=logging.DEBUG)

    def _get_selected_node(self, headers: dict, line_callback=None, *, log_prefix: str = "selected node") -> dict:
        """获取节点列表并选择本次使用的节点。"""
        nodes_body = {"biz_key": BIZ_KEY, "node": self.config.node, "speed_client_type": self.speed_client_type}
        nodes_res = self._dispatch_post("/dispatcher/api/getNodesInfo", nodes_body, headers)
        self._assert_ok("getNodesInfo", nodes_res)
        node = self._selected_node((nodes_res.get("data") or {}).get("nodes") or [])
        self._line(
            line_callback,
            f"{log_prefix}: {node.get('node_id')} {node.get('node_name') or ''} {node.get('net_state') or ''}".rstrip(),
            level=logging.DEBUG,
        )
        return node

    def _pre_dispatch_verify(self, node: dict, headers: dict, line_callback=None) -> dict:
        """执行预调度验证并返回响应 data。"""
        pre_dispatch = self._dispatch_post("/dispatcher/api/preDispatchVerify", self._node_payload(node), headers)
        self._assert_ok("preDispatchVerify", pre_dispatch)
        self._line(line_callback, "preDispatchVerify OK", level=logging.DEBUG)
        return pre_dispatch.get("data") or {}

    def _finish_dispatch_result(self, result: dict, line_callback=None) -> dict:
        """统一记录调度完成结果并返回原始 finish_result。"""
        self._line(
            line_callback,
            f"排队完成，队列类型：'{'星云币优先队列' if result.get('queue_type') == QUEUE_TYPE_COIN else '普通队列'}' 消耗：'{'普通时长' if str(result.get('cost_method')) == '0' else '星云币时长'}'",
            level=logging.INFO,
        )
        self._emit_finish_result(result, line_callback)
        return result

    def _log_queue_info(self, queue_info: dict, line_callback=None) -> None:
        """输出排队状态中最有用的字段。"""
        ticket = str(queue_info.get("ticket") or "")
        node_name = queue_info.get("node_name") or "未知节点"
        node_id = queue_info.get("node_id") or "?"
        queue_rank = queue_info.get("queue_rank") or "?"
        queue_length = queue_info.get("queue_length") or queue_info.get("queue_len") or "?"
        branch_queue_len = queue_info.get("branch_queue_len") or "?"
        waiting_time_min = queue_info.get("waiting_time_min") or "?"
        query_interval = queue_info.get("query_interval") or "?"
        queue_type = "星云币优先队列" if queue_info.get("queue_type") == QUEUE_TYPE_COIN else "普通队列"
        self._line(
            line_callback,
            (
                f"开始排队：节点={node_name}({node_id})，队列类型='{queue_type}'，"
                f"当前排名={queue_rank}/{queue_length}，总队列数={branch_queue_len}，"
                f"预计等待={waiting_time_min}分钟，查询间隔={query_interval}秒，ticket={self._masked(ticket)}"
            ),
            level=logging.INFO,
        )

    def _log_ticket_poll(self, attempt: int, ticket_status: str, ticket_data: dict, line_callback=None) -> None:
        """输出一次排队轮询结果。"""
        if ticket_status != "QUEUEING":
            return
        queue_info = ticket_data.get("queue_info") or {}
        queue_rank = queue_info.get("queue_rank", ticket_data.get("queue_rank"))
        queue_rank = "?" if queue_rank is None else queue_rank
        queue_length = queue_info.get("queue_length") or queue_info.get("queue_len") or ticket_data.get("queue_length") or "?"
        branch_queue_len = queue_info.get("branch_queue_len") or ticket_data.get("branch_queue_len") or "?"
        waiting_time_min = queue_info.get("waiting_time_min", ticket_data.get("waiting_time_min"))
        waiting_time_min = "?" if waiting_time_min is None else waiting_time_min
        self._line(
            line_callback,
            (
                f"排队轮询 {attempt}："
                f"当前排名={queue_rank}/{queue_length}，总队列数={branch_queue_len}，"
                f"预计等待={waiting_time_min}分钟"
            ),
            level=logging.INFO,
        )

    def _poll_dispatch_ticket(
            self,
            ticket_payload: dict,
            query_interval: int,
            headers: dict,
            line_callback=None,
            stop_event=None,
    ) -> dict:
        """轮询排队 ticket，成功后 ack 并返回 finish_result。"""
        attempt = 0
        while True:
            attempt += 1
            self._line(
                line_callback,
                f"poll {attempt}; waiting {query_interval}s",
                level=logging.DEBUG,
            )
            self._sleep(query_interval, stop_event=stop_event)
            ticket_info = self._dispatch_post(
                "/dispatcher/api/getDispatchTicketInfo",
                ticket_payload,
                headers,
                retries=2,
            )
            self._assert_ok("getDispatchTicketInfo", ticket_info)
            ticket_data = ticket_info.get("data") or {}
            ticket_status = ticket_data.get("ticket_status")
            self._log_ticket_poll(attempt, str(ticket_status), ticket_data, line_callback)
            if ticket_status == "SUCCESS":
                ack = self._dispatch_post("/dispatcher/api/ackDispatchTicket", ticket_payload, headers)
                self._assert_ok("ackDispatchTicket", ack)
                self._line(line_callback, "ackDispatchTicket OK", level=logging.DEBUG)
                return self._finish_dispatch_result(ticket_data["finish_result"], line_callback)
            if ticket_status != "QUEUEING":
                raise RuntimeError(f"ticket failed: {ticket_status}")
            query_interval = int(ticket_data.get("queue_info", {}).get("query_interval") or query_interval)

    def run(self, line_callback=None, status_callback=None, stop_event=None) -> dict:
        """执行完整的云游戏调度流程。"""
        account_sync = self._require_initialized()
        emit_log_callback(status_callback, "开始获取连接凭证", logging.INFO)

        headers = self._dispatch_headers()
        self._log_account_sync(account_sync, line_callback)
        self._status_check(headers, line_callback)
        self._list_ping_servers(headers, line_callback)
        node = self._get_selected_node(headers, line_callback)
        self._pre_dispatch_verify(node, headers, line_callback)

        dispatch_payload = self._paas_dispatch_payload(node)
        self._line(line_callback, f"paasDispatch queue_type={dispatch_payload.get('queue_type')!r}", level=logging.DEBUG)
        dispatch = self._dispatch_post("/dispatcher/api/paasDispatch", dispatch_payload, headers)
        self._assert_ok("paasDispatch", dispatch)
        result_code = (dispatch.get("data") or {}).get("result_code")
        self._line(line_callback, f"paasDispatch result: {result_code}", level=logging.DEBUG)

        if result_code == "FINISHED":
            return self._finish_dispatch_result(dispatch["data"]["finish_result"], line_callback)
        if result_code != "QUEUED":
            raise RuntimeError(f"paasDispatch did not finish or queue: {result_code}")

        queue_info = dispatch["data"]["queue_info"]
        self._log_queue_info(queue_info, line_callback)
        ticket_payload = self._ticket_payload(queue_info["ticket"])
        query_interval = int(queue_info.get("query_interval") or 10)
        try:
            return self._poll_dispatch_ticket(ticket_payload, query_interval, headers, line_callback, stop_event)
        except Exception:
            leave = self._dispatch_post(
                "/dispatcher/api/leaveDispatchQueue",
                {**ticket_payload, "leave_type": 1}, headers,
            )
            self._assert_ok("leaveDispatchQueue", leave)
            raise


__all__ = ["DispatchConfig", "Dispatcher", "QUEUE_TYPE_COIN", "QUEUE_TYPE_NORMAL"]
